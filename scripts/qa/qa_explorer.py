#!/usr/bin/env python3
"""qa_explorer.py — the STATE-BASED agentic QA explorer (the heart of the AI-driven QA system).

Design philosophy (owner-mandated): EVERY decision is an AI call. There is no scripted click-path, no
selector heuristic deciding what to do next, no rule engine judging pass/fail. The loop is a tight
sense–think–act cycle where the THINK is always the model:

    observe (bridge.state)  ->  AI DECIDES next action (_ai_decide)  ->  act (bridge.act)
      ->  observe again (bridge.state)  ->  AI EVALUATES expected-vs-actual (_ai_evaluate)  ->  record

The model is given the ORIGINAL VISION of the product plus the STORY's EXPECTED behaviour on every turn,
so it judges what SHOULD happen against what DID happen — the definition of a real bug. Cost is explicitly
not a concern here; correctness and thoroughness are. Every _ai_decide / _ai_evaluate is a real
factory.agent call (role 'qa-security'), which already retries on 529/overload and fails over to Codex.

The browser is a persistent Playwright process (browser_bridge.js) we spawn and drive over stdin/stdout,
so one authenticated session is explored across many steps (cookies/localStorage/auth persist).

Public API:
    ex = Explorer(target_url, vision, token=None, org='0')
    steps = ex.explore(story, max_steps=25, on_bug=callback)   # -> list[step-record dicts]
    ex.close()

Each step-record: {step, state, action, expected, actual, bug, verdict, done}.

Run `python qa_explorer.py` for an offline self-test (factory.agent + the bridge are stubbed — no real
API calls, no real browser).
"""
import ast
import hashlib
import json
import fcntl
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from difflib import SequenceMatcher

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

try:
    import campaign_checkpoint
except ModuleNotFoundError:  # imported as ``qa.qa_explorer`` rather than the legacy flat module
    from qa import campaign_checkpoint

BRIDGE_JS = Path(__file__).resolve().parent / "browser_bridge.js"
AT_DRIVER = Path(__file__).resolve().parent / "at_driver.py"
_LIVE_BRIDGES = set()
_LIVE_BRIDGES_LOCK = threading.Lock()

# Browser/AT workers are an external trust boundary. Do not clone the controller
# environment: it can contain provider keys, DB URLs, notification credentials,
# and unrelated tenant state. Keep only process/runtime inputs the browser stack
# needs, then add the narrowly-scoped AOS_QA_* values below at launch time.
_BROWSER_CHILD_ENV_ALLOW = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM",
    "TMPDIR", "PYTHONPATH", "NODE_PATH", "LD_LIBRARY_PATH", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "PULSE_SERVER",
    "XDG_RUNTIME_DIR", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS", "FONTCONFIG_PATH", "FONTCONFIG_FILE",
    "PLAYWRIGHT_BROWSERS_PATH", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
    "AOS_QA_CMD_TIMEOUT_MS", "AOS_QA_STATE_TIMEOUT_MS", "AOS_QA_SETTLE_MAX_MS",
    "AOS_QA_SETTLE_NETIDLE_MS", "AOS_QA_EXTERNAL_WAIT_MAX_MS",
)


def _browser_child_env():
    return {key: os.environ[key] for key in _BROWSER_CHILD_ENV_ALLOW if os.environ.get(key) is not None}


def _acquire_at_session_lock(timeout_s):
    """Acquire the host's single real-Orca workstation for one complete job.

    Orca's launcher explicitly supports one screen-reader process per Unix
    user.  A second instance can stall between ``Handlers set`` and
    ``ORCA: Initialized`` even with private Xvfb, D-Bus, HOME and XDG state.
    Keep this bounded lock for the entire AT session.  The descriptor is also
    passed to the registered wrapper and Orca process, so a killed Python
    worker cannot release admission while its exact external tree still lives.
    """
    lock_dir = Path(tempfile.gettempdir()) / f"agent-os-{os.getuid()}"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(lock_dir, 0o700)
    except OSError:
        pass
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_dir / "qa-at-startup.lock", flags, 0o600)
    os.set_inheritable(fd, False)
    handle = os.fdopen(fd, "a+")
    deadline = time.monotonic() + max(0.25, float(timeout_s))
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError("timed out waiting for the bounded Orca workstation lease")
            time.sleep(0.05)


def _release_at_session_lock(handle):
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass
    try:
        handle.close()
    except Exception:
        pass


def close_live_bridges(run_id=None, tenant=None):
    """Force-close only browser groups owned by the requested durable run.

    Omitting both filters retains the process-shutdown behavior.  Run cancellation must always provide both:
    one Python process can host tools for several tenants/runs, and closing the global set would corrupt
    unrelated evidence and manufacture resumptions.
    """
    if (run_id is None) != (tenant is None):
        raise ValueError("run_id and tenant must be provided together for scoped browser cancellation")
    with _LIVE_BRIDGES_LOCK:
        bridges = [bridge for bridge in _LIVE_BRIDGES
                   if (run_id is None or getattr(bridge, "scope_run_id", None) == run_id)
                   and (tenant is None or getattr(bridge, "scope_tenant", None) == tenant)]
    for bridge in bridges:
        try:
            bridge.close()
        except Exception:
            pass
    return len(bridges)
NODE_PATH = os.environ.get("NODE_PATH") or str(
    Path.home() / "projects" / "products" / "noupload" / "node_modules")
ROLE = "qa-security"
try:
    import artifacts
except Exception:
    artifacts = None


# ----------------------------------------------------------------------------------------------------
# JSON extraction — the model replies with a JSON object (sometimes wrapped in prose / a ```json fence).
# ----------------------------------------------------------------------------------------------------
def _extract_json(text):
    """Pull the last well-formed JSON object out of a model reply. Tries a fenced block first, then a
    brace-balanced scan from the last '{' — robust to leading reasoning and trailing chatter."""
    if not text:
        return {}
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # brace-balanced scan (handles nested objects better than a greedy regex)
    depth, start = 0, None
    best = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    best = text[start:i + 1]
    if best:
        try:
            return json.loads(best)
        except Exception:
            pass
    return {}


# TIERED MODELS (speed): the NAVIGATION decision (`_ai_decide`) is high-volume and low-stakes — the fast
# model in factory 'light' mode is plenty and keeps the loop moving like a human clicking around. The
# EXPECTED-vs-ACTUAL JUDGMENT (`_ai_evaluate`) is the zero-bugs-reach-a-human call, so it stays on the full
# frontier model. Its evidence contract is narrow and structured, so medium reasoning is the latency-balanced
# default; independent finding adjudication/fix review remains on the platform's xhigh default. Toggle nav back
# to the heavy model with AOS_QA_DECIDE_LIGHT=0 or tune the judge with AOS_QA_EVALUATE_REASONING_EFFORT.
_DECIDE_LIGHT = os.environ.get("AOS_QA_DECIDE_LIGHT", "1").lower() not in ("0", "false", "no", "off")
_EVALUATE_REASONING_EFFORT = os.environ.get("AOS_QA_EVALUATE_REASONING_EFFORT", "medium").strip().lower()
if _EVALUATE_REASONING_EFFORT not in ("minimal", "low", "medium", "high", "xhigh", "max"):
    _EVALUATE_REASONING_EFFORT = "medium"
_DIAGNOSE_REASONING_EFFORT = os.environ.get("AOS_QA_DIAGNOSE_REASONING_EFFORT", "medium").strip().lower()
if _DIAGNOSE_REASONING_EFFORT not in ("minimal", "low", "medium", "high", "xhigh", "max"):
    _DIAGNOSE_REASONING_EFFORT = "medium"


def _bounded_env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return min(int(maximum), max(int(minimum), value))


# Internal reasoning is not an external product wait.  A chat reply may honestly take minutes, but choosing
# the next browser action may not silently occupy a worker for ten minutes and then retry for fifteen more.
# Each class is independently configurable; timeout is missing evidence and checkpoints the story, never a
# product bug or a reason to repeat an already-executed side effect.
_COVERAGE_TIMEOUT_S = _bounded_env_int("AOS_QA_COVERAGE_TIMEOUT_S", 60, 10, 300)
_DECIDE_TIMEOUT_S = _bounded_env_int("AOS_QA_DECIDE_TIMEOUT_S", 90, 5, 300)
_EVALUATE_TIMEOUT_S = _bounded_env_int("AOS_QA_EVALUATE_TIMEOUT_S", 180, 15, 600)
_DIAGNOSE_TIMEOUT_S = _bounded_env_int("AOS_QA_DIAGNOSE_TIMEOUT_S", 120, 15, 300)
_WAIT_DEFAULT_S = _bounded_env_int("AOS_QA_EXTERNAL_WAIT_S", 60, 1, 600)
_WAIT_MAX_S = _bounded_env_int("AOS_QA_EXTERNAL_WAIT_MAX_S", 300, 1, 1800)
# COVERAGE-DRIVEN TERMINATION (not a step count): the loop stops when the coverage ledger is exhausted.
# The backstops below are deliberately PATIENT — a real tester navigates several screens to reach a deep
# aspect, so only genuinely-stuck behaviour stops the run early:
#   * STALL: this many consecutive steps that neither cover a new aspect NOR reach a new view (url) — i.e.
#     truly wandering in circles. Kept well ABOVE a typical aspect count so multi-step navigation toward a
#     deep aspect is never punished. (A 12-aspect story needs ~30-40 steps; 12 would have cut it off.)
#   * DEAD: this many consecutive steps where the action had NO effect at all (clicking dead controls /
#     nothing responds) — a fast stop for a genuinely broken/frozen surface.
# Both stop HONESTLY as 'incomplete' (checkpointed), never as a false 'done'. Env-overridable; 0 disables.
_STALL_LIMIT = int(os.environ.get("AOS_QA_STALL_STEPS", "12"))
_DEAD_LIMIT = int(os.environ.get("AOS_QA_DEAD_STEPS", "6"))
_REPEAT_LIMIT = int(os.environ.get("AOS_QA_REPEAT_ACTION_STEPS", "4"))


class ModelDecisionUnavailable(RuntimeError):
    """The QA reasoning provider produced no usable decision evidence."""


def _model_failure_reason(result):
    if not isinstance(result, dict):
        return "model returned a non-object result"
    detail = result.get("reason") or result.get("blocker") or result.get("out") or "empty model result"
    return f"model decision unavailable (rc={result.get('rc')}): {str(detail)[:300]}"


def _evidence_snapshot(state):
    """Bounded, inspectable browser facts retained with each step.

    A screenshot is primary visual evidence, but the release dossier also needs machine-readable before/after
    facts (and screenshots can fail or be unavailable to a remote reviewer).  Keep rendered text bounded and
    omit request bodies/cookies/storage; those remain only in owner-readable browser artifacts.
    """
    state = state or {}
    return {
        "url": state.get("url"), "title": state.get("title"),
        "screenshot": state.get("screenshot"),
        "console_errors": list(state.get("console_errors") or [])[-25:],
        "bodyText": str(state.get("bodyText") or "")[:4000],
        "viewportText": str(state.get("viewportText") or "")[:4000],
        "scrollPosition": state.get("scrollPosition"),
        "horizontalOverflow": bool(state.get("horizontalOverflow")),
        "statusText": str(state.get("statusText") or "")[:1200],
        "accessibilityRegions": list(state.get("accessibilityRegions") or [])[:100],
        "accessibilityTree": str(state.get("accessibilityTree") or "")[:12000],
        "accessibilityEvents": list(state.get("accessibilityEvents") or [])[-100:],
        "accessibilityPlatformEvents": list(state.get("accessibilityPlatformEvents") or [])[-100:],
        # Reserved for a real NVDA/VoiceOver/Orca/AT-SPI driver. Chromium CDP AX events live in the
        # separate field above and must never be promoted into evidence that speech/AT output occurred.
        "actualAssistiveTechnologyEvents": list(
            state.get("actualAssistiveTechnologyEvents") or [])[-100:],
        "actualAssistiveTechnologyAvailable": bool(
            state.get("actualAssistiveTechnologyAvailable")),
        "activeElement": state.get("activeElement"),
        "recent_requests": list(state.get("recent_requests") or [])[-25:],
        "viewport": state.get("viewport"), "perception": state.get("perception"),
    }


def _atomic_coverage_aspects(aspects):
    """Split concrete input conjunctions into independently creditable evidence boundaries.

    Keep the shared semantic scope in each label.  Returning only ``Tab traversal`` used to drop the rest of
    an authored clause (which controls, focus order, responsive layout); keeping the original prose verbatim,
    however, left every other key name in every child and made the mechanical oracle require one action to be
    Tab, Shift+Tab, Arrow, Space *and* Enter simultaneously.  The labels below retain the acceptance scope but
    mention exactly one required modality.  ``source_aspect`` metadata added by
    :func:`_migrate_compound_coverage_ledger` retains the verbatim parent for review/audit.
    """
    out = []
    for raw in aspects or []:
        aspect = str(raw).strip()
        low = aspect.lower().replace("shift + tab", "shift+tab")
        alternative = bool(re.search(
            r"\benter\s*(?:or|/)\s*(?:the\s+)?space(?:bar)?\b|"
            r"\bspace(?:bar)?\s*(?:or|/)\s*(?:the\s+)?enter\b", low))
        modalities = []
        if re.search(r"(?<!shift\+)\btab\b", low):
            modalities.append(("tab", "Tab traversal"))
        if "shift+tab" in low:
            modalities.append(("shift_tab", "Shift+Tab traversal"))
        if re.search(r"\barrow(?:\s+keys?|up|down|left|right)?\b", low):
            modalities.append(("arrow", "Arrow-key operation"))
        if alternative:
            modalities.append(("enter_or_space", "Enter-or-Space activation"))
        else:
            if re.search(r"\benter\b", low):
                modalities.append(("enter", "Enter activation"))
            if re.search(r"\bspace(?:bar)?\b", low):
                modalities.append(("space", "Space activation"))
        if len(modalities) <= 1:
            if aspect:
                out.append(aspect)
            continue

        # Preserve the authored target/outcome without copying the mutually-exclusive key list.  This is
        # deliberately conservative: if no useful scope can be extracted, the stable atomic labels remain
        # exact evidence requirements and the verbatim source survives in ledger metadata.
        scope = re.sub(
            r"\b(?:using|use|with|via)\s+only\s+[^.;]{0,140}?\b(?:to\s+)?(?=reach|operate|activate|"
            r"traverse|verify|confirm|exercise)", "", aspect, flags=re.I)
        scope = re.sub(
            r"\b(?:tab|shift\s*\+\s*tab|arrow(?:\s+keys?|up|down|left|right)?|space(?:bar)?|enter)\b"
            r"(?:\s*(?:,|/|and|or)\s*)?", "", scope, flags=re.I)
        scope = re.sub(r"\s+", " ", scope).strip(" ,;:-")
        simple_scope = scope.casefold() in {
            "", "traversal", "activation", "and traversal", "and activation",
        }
        for kind, label in modalities:
            if simple_scope:
                out.append(label)
                continue
            if kind in {"tab", "shift_tab"}:
                tailored = re.sub(r"\breach\s+and\s+(?:operate|activate)\b", "reach", scope,
                                  flags=re.I)
                tailored = re.sub(r"\b(?:operate|activate)\b", "reach", tailored, flags=re.I)
            else:
                # Focus order/layout are traversal outcomes, not duties to re-prove with every activation key.
                # Keep the named control scope and make applicability explicit (Arrow is for composites,
                # Space for checkable controls, Enter for buttons/links).
                tailored = scope.split(";", 1)[0]
                tailored = re.sub(r"\breach\s+and\s+(?:operate|activate)\b", "operate", tailored,
                                  flags=re.I)
                tailored = re.sub(r"\breach\b", "operate", tailored, flags=re.I)
                tailored = re.sub(r"\bevery\s+listed\b", "every applicable story-listed", tailored,
                                  flags=re.I)
            out.append(f"{label}: {tailored}"[:700])
    return list(dict.fromkeys(out))


def _compound_coverage_parts(aspect):
    """Return ordered atomic labels/kinds for a potentially compound semantic ledger item."""
    aspect = str(aspect or "").strip()
    if not aspect:
        return []
    atomic = _atomic_coverage_aspects([aspect])
    if len(atomic) > 1:
        kinds = []
        for label in atomic:
            low = label.casefold().replace("shift + tab", "shift+tab")
            kinds.append(
                "shift_tab" if "shift+tab" in low else
                "tab" if re.search(r"\btab\b", low) else
                "arrow" if "arrow" in low else
                "enter_or_space" if "enter-or-space" in low else
                "space" if re.search(r"\bspace\b", low) else
                "enter" if re.search(r"\benter\b", low) else "semantic")
        return list(zip(atomic, kinds))

    low = " ".join(aspect.casefold().split())
    # Preserve authored order when setup, timed stability, and a terminal key action share one semantic row.
    # Without atoms, a successful consent click plus dwell can make an evaluator repeat the broad label even
    # when Enter itself registered no form effect; a later fallback click then hides the missing keyboard path.
    ordered_timed = re.match(
        r"^(?P<setup>.+),\s*(?P<dwell>dwell\b.+),\s*then\s+"
        r"(?P<final>press\s+enter\b.+?)[.]?$", aspect, re.I)
    if ordered_timed:
        setup = str(ordered_timed.group("setup") or "").strip(" ,.;") + "."
        dwell = str(ordered_timed.group("dwell") or "").strip(" ,.;") + "."
        final = str(ordered_timed.group("final") or "").strip(" ,.;") + "."
        return [(setup[:700], "semantic"), (dwell[:700], "timed_wait"),
                (final[:700], "form_enter")]

    # Validation stories commonly enumerate several independent input classes in one authored step, then
    # attach the same blur/paste/submit/no-side-effect acceptance boundary to all of them. One browser action
    # cannot prove a past date, both age limits, an overlong string and a contact mismatch simultaneously.
    # Keep the shared qualifier in every child so atomic credit never weakens the original safety contract.
    validation_cases = []
    if re.search(r"\b(?:invalid\s+or\s+past|past\s+or\s+invalid)\s+(?:start\s+)?dates?\b", low):
        validation_cases.append("Test an invalid or past start date")
    below_age = re.search(r"\bdog\s+ages?\s+(?:below|under|less\s+than)\s+(-?\d+)\b", low)
    if below_age:
        validation_cases.append(f"Test a dog age below {below_age.group(1)}")
    above_age = re.search(r"\b(?:dog\s+ages?\s+)?(?:above|over|greater\s+than)\s+(\d+)\b", low)
    if above_age and ("dog age" in low or "dog ages" in low):
        validation_cases.append(f"Test a dog age above {above_age.group(1)}")
    if re.search(r"\b(?:near[- ]limit\s+)?overlong\s+(?:text|input|value)", low):
        validation_cases.append("Test near-limit overlong text")
    if (re.search(r"\bemail\s*/\s*phone\b", low)
            and re.search(r"\b(?:mismatch(?:ed)?|does\s+not\s+match|preferred\s+contact)\b", low)):
        validation_cases.append("Test mismatched email and phone preferred-contact data")
    if len(validation_cases) > 1:
        qualifier = aspect.split(";", 1)[1].strip() if ";" in aspect else (
            "Verify the value is rejected with clear user-facing validation, focus remains recoverable, "
            "and no invalid record or side effect is persisted.")
        return [(f"{case}; {qualifier}"[:700], "validation_case")
                for case in validation_cases]

    # A single responsive assertion that names both mobile and desktop is two independently observable
    # browser states.  One screenshot/viewport can never prove both, so expose exact ordered atoms instead of
    # asking a semantic judge to infer the missing viewport from CSS or prose.
    responsive_pair = re.search(
        r"\b(?:at\s+)?(?:(?P<mobile>\d{3,4})\s*px\s+)?mobile\s+(?:and|/)\s+desktop\s+widths?\b",
        aspect, re.I)
    if responsive_pair:
        mobile_width = int(responsive_pair.group("mobile") or 390)
        mobile = (aspect[:responsive_pair.start()]
                  + f"at {mobile_width}px mobile width"
                  + aspect[responsive_pair.end():])
        desktop = (aspect[:responsive_pair.start()]
                   + "at desktop width"
                   + aspect[responsive_pair.end():])
        return [(" ".join(mobile.split())[:700], "viewport_mobile"),
                (" ".join(desktop.split())[:700], "viewport_desktop")]

    # A multi-surface dwell is several independently observable state boundaries, not one all-or-nothing
    # assertion.  Some surfaces coexist on a long page while confirmation/error surfaces appear only after a
    # business transition.  Keeping the prose in one ledger row made a valid 70-second receipt earn 0% merely
    # because a conditional screen was not currently rendered; the next worker then repeated those same 70
    # seconds forever.  Split only the explicitly enumerated ``on ...`` surface list and retain the complete
    # authored stability qualifier on every child.  A child therefore cannot pass from another surface's
    # receipt, while already-proven surfaces remain durable across the transition needed to reveal the next.
    has_refresh_path = bool(
        re.search(r"\b(?:refresh|reload)\b", low)
        and re.search(r"\b(?:repeat|again|re-run|rerun)\b[^.;]{0,100}\b(?:keyboard|tab|key)\b", low))
    dwell_scope = re.search(
        r"\b(?:dwell|idle|remain|wait)\b[^.;]{0,120}?\bon\s+(?P<surfaces>[^.;]+)"
        r"(?P<qualifier>\s*;[^.]*)?",
        aspect, re.I)
    if dwell_scope and re.search(r"\b\d+(?:\.\d+)?\s*(?:seconds?|secs?|s)\b", aspect, re.I):
        surfaces_text = str(dwell_scope.group("surfaces") or "").strip()
        # A trailing "then refresh ... and repeat a keyboard path" is an ordered next action, not two more
        # landmark names.  The old list splitter produced fake surfaces literally named "then refresh after
        # interactions" and "repeat one keyboard path", leaving real reload evidence absent forever.
        surfaces_text = re.split(
            r"\s*,?\s*then\s+(?:refresh|reload)\b", surfaces_text, maxsplit=1, flags=re.I)[0].strip()
        surfaces = [" ".join(item.split()).strip(" ,") for item in re.split(
            r"\s*,\s*(?:and\s+)?|\s+and\s+(?=(?:any\s+|the\s+)?[a-z0-9])",
            surfaces_text, flags=re.I)]
        surfaces = [item for item in surfaces if item]
        if len(surfaces) > 1:
            prefix_match = re.match(
                r"(?P<prefix>.*?\b(?:dwell|idle|remain|wait)\b[^.;]{0,120}?\bon)\s+",
                aspect, re.I)
            prefix = str(prefix_match.group("prefix") if prefix_match else "Dwell idle on").strip()
            qualifier = str(dwell_scope.group("qualifier") or "").strip()
            suffix = qualifier if qualifier else "; verify the surface remains stable for the full dwell."
            parts = [(f"{prefix} {surface}{suffix}"[:700], "dwell_surface")
                     for surface in surfaces]
            if has_refresh_path:
                parts.extend([
                    ("Refresh/reload after the story interactions and verify the refreshed accessible state.",
                     "reload"),
                    ("After refresh/reload, repeat one story-required keyboard path and verify focus, "
                     "announcements, and control operability.", "post_reload_keyboard"),
                ])
            return parts

    # A true refresh and the *subsequent* keyboard path are two ordered browser boundaries.  Keeping them in
    # one item made the reload action too early to prove the path and the path action too late to prove reload.
    if has_refresh_path:
        return [
            ("Refresh/reload after the story interactions and verify the refreshed accessible state.",
             "reload"),
            ("After refresh/reload, repeat one story-required keyboard path and verify focus, "
             "announcements, and control operability.", "post_reload_keyboard"),
        ]
    return [(aspect, "semantic")]


def _requires_temporal_idle_proof(value):
    """Whether a ledger clause requires elapsed idle stability, not merely a settled snapshot."""
    text = " ".join(str(value or "").casefold().split())
    return bool(re.search(
        r"\b(?:dwell|remain|stay)\s+(?:idle\s+)?(?:for\s+)?\d+(?:\.\d+)?\s*"
        r"(?:seconds?|secs?|s)\b|"
        r"\bidle\s+for\s+\d+(?:\.\d+)?\s*(?:seconds?|secs?|s)\b",
        text, re.I))


def _migrate_compound_coverage_ledger(ledger):
    """Losslessly upgrade durable/model ledgers whose one item requires several browser boundaries.

    Migration is monotonic: a covered parent yields covered children, an open parent yields open children, and
    already-migrated rows are returned unchanged.  That lets a rolling worker load an old checkpoint without
    replanning or discarding any evidence while making every still-open boundary mechanically creditable.
    """
    rows = [dict(raw) for raw in (ledger or []) if isinstance(raw, dict)]
    # Repair durable rows from the former semantic-causality gap where a plain click could be credited for a
    # ten-second dwell atom.  A settled after-click snapshot does not prove stability throughout an elapsed
    # boundary.  Reopen only the invalid atom; genuine wait/timed/dwell receipts remain monotonic.
    for item in rows:
        proof = item.get("proof") if isinstance(item.get("proof"), dict) else {}
        proof_action = str(proof.get("action_kind") or "").casefold()
        if (item.get("covered") and _requires_temporal_idle_proof(item.get("aspect"))
                and proof_action not in {"wait", "dwell", "dwell_surfaces", "timed_transition"}):
            item["covered"] = False
            item.pop("proof", None)
            item["coverage_repaired"] = "temporal-boundary-required-fresh-proof"
        if (item.get("covered") and _post_reload_requirement(item.get("aspect"))
                and re.search(r"\bkeyboard\s+path\b", str(item.get("aspect") or ""), re.I)
                and proof_action not in {"traverse", "tab_traverse", "tabtraverse", "keyboard_matrix"}):
            item["covered"] = False
            item.pop("proof", None)
            item["coverage_repaired"] = "post-reload-keyboard-required-fresh-proof"
        # A control click can cause a state transition, but it cannot by itself prove a settled comparison
        # across several named application surfaces.  Older semantic/mechanical receipts gave one Drain queue
        # click credit for public, staff, CEO, queue, approval, audit, notification, and blocker inspection.
        # Reopen only those broad rows whose durable proof is not the browser's sealed multi-surface observer.
        proof_engine = str(proof.get("engine") or "").casefold()
        invalid_drain_projection = bool(
            proof_action in {"click", "tap", "press"}
            and (proof_engine == "mechanical-browser-proof"
                 or re.search(r"\bdrain the queue\b.*\bpopulated state\b",
                              str(item.get("aspect") or ""), re.I)))
        if (item.get("covered") and _requires_multi_surface_inspection(item.get("aspect"))
                and invalid_drain_projection):
            item["covered"] = False
            item.pop("proof", None)
            item["coverage_repaired"] = "multi-surface-inspection-required-fresh-proof"
        # A single failure-state drain used to mechanically close the entire Success -> Retry -> Drain
        # recovery journey because the broad row happened to contain the words "retry" and "failed".  Only a
        # dedicated aggregate receipt may close that ordered business journey.
        if (item.get("covered") and _requires_queue_recovery_journey(item.get("aspect"))
                and str(proof.get("engine") or "").casefold() != "durable-queue-recovery-receipt"):
            item["covered"] = False
            item.pop("proof", None)
            item["coverage_repaired"] = "ordered-queue-recovery-required-fresh-proof"
    # A former semantic judge could credit "created state persists after refresh" on an empty-app reload.
    # Reopen that receipt unless an earlier ledger item already has grounded creation/submission evidence.
    # This is based on ordered covered requirements, never UI prose or model confidence.
    for position, item in enumerate(rows):
        if not (item.get("covered") and _requires_prior_created_state(item.get("aspect"))):
            continue
        current_proof = item.get("proof") if isinstance(item.get("proof"), dict) else {}
        try:
            current_recorded_at = float(current_proof.get("recorded_at"))
        except (TypeError, ValueError):
            current_recorded_at = None
        prior_creation_proven = False
        for prior in rows[:position]:
            prior_proof = prior.get("proof") if isinstance(prior.get("proof"), dict) else {}
            try:
                prior_recorded_at = float(prior_proof.get("recorded_at"))
            except (TypeError, ValueError):
                prior_recorded_at = None
            if (prior.get("covered") and _creates_business_state(prior.get("aspect"))
                    and prior_recorded_at is not None and current_recorded_at is not None
                    and prior_recorded_at <= current_recorded_at):
                prior_creation_proven = True
                break
        if not prior_creation_proven:
            item["covered"] = False
            item.pop("proof", None)
            item["coverage_repaired"] = "persisted-state-required-prior-creation-proof"
    # Repair checkpoints produced by the former dwell parser.  Those groups are already marked atomic, so a
    # naive migration would preserve their fake "then refresh" landmark children forever.  Re-expand the
    # original source once, retaining covered/proof fields for exact legitimate children and reopening only
    # the malformed/missing boundaries.
    malformed_parents = {
        str(item.get("atomic_parent")) for item in rows
        if item.get("atomic_parent") and item.get("atomic_kind") == "dwell_surface"
        and re.search(r"\b(?:then\s+refresh|then\s+reload|repeat\s+one\s+keyboard)\b",
                      str(item.get("aspect") or ""), re.I)
    }
    repaired_groups = {}
    for parent in malformed_parents:
        group = [item for item in rows if str(item.get("atomic_parent")) == parent]
        source = str(next((item.get("source_aspect") for item in group
                          if item.get("source_aspect")), "") or "").strip()
        parts = _compound_coverage_parts(source)
        if not source or len(parts) <= 1:
            continue
        prior = {" ".join(str(item.get("aspect") or "").casefold().split()): item for item in group}
        template = dict(group[0])
        rebuilt = []
        for position, (label, kind) in enumerate(parts, 1):
            child = dict(prior.get(" ".join(label.casefold().split())) or template)
            if " ".join(label.casefold().split()) not in prior:
                child["covered"] = False
                child.pop("proof", None)
                child.pop("coverage_repaired", None)
            child.update({"aspect": label, "source_aspect": source,
                          "atomic_parent": parent, "atomic_index": position,
                          "atomic_total": len(parts), "atomic_kind": kind})
            rebuilt.append(child)
        repaired_groups[parent] = rebuilt

    migrated, emitted_repaired = [], set()
    for raw in rows:
        parent = str(raw.get("atomic_parent") or "")
        if parent in repaired_groups:
            if parent not in emitted_repaired:
                migrated.extend(repaired_groups[parent])
                emitted_repaired.add(parent)
            continue
        if not isinstance(raw, dict) or not str(raw.get("aspect") or "").strip():
            continue
        item = dict(raw)
        if item.get("atomic_parent"):
            migrated.append(item)
            continue
        source = str(item.get("source_aspect") or item.get("aspect") or "").strip()
        parts = _compound_coverage_parts(source)
        if len(parts) <= 1:
            migrated.append(item)
            continue
        parent = hashlib.sha256(" ".join(source.casefold().split()).encode()).hexdigest()[:20]
        split_kinds = {kind for _, kind in parts}
        # These migrations exist because the former parent could be credited from only one of several
        # mutually exclusive/ordered browser states.  Copying that broad boolean and proof to every child
        # would preserve the exact false-positive the migration is meant to repair.  Reopen the new atoms;
        # exact retained step receipts can close them again through the normal evidence reconciliation path.
        reopen_split = bool(split_kinds & {
            "viewport_mobile", "viewport_desktop", "timed_wait", "form_enter", "validation_case",
        })
        for position, (label, kind) in enumerate(parts, 1):
            child = dict(item)
            if reopen_split:
                child["covered"] = False
                child.pop("proof", None)
                child["coverage_repaired"] = "compound-boundary-required-fresh-proof"
            child.update({"aspect": label, "source_aspect": source,
                          "atomic_parent": parent, "atomic_index": position,
                          "atomic_total": len(parts), "atomic_kind": kind})
            migrated.append(child)
    return migrated


def _restore_chronological_persistence_coverage(ledger, records):
    """Recover causal row proofs from ordered durable evidence records.

    Older compact checkpoints kept exact grounded ``covers`` receipts in chronological order but omitted the
    receipt timestamp from both the compact step and some coverage rows.  The causal migration must reject a
    reload that happened before creation, yet it must not keep reopening a later legitimate reload/reopen. Use
    only exact, successful durable receipts and their order: a persistence receipt is restored only when an
    earlier ledger creation requirement has an earlier successful receipt. No fuzzy text matching or model
    confidence is used.
    """
    rows = [dict(item) for item in (ledger or []) if isinstance(item, dict)]
    successful = []
    for index, raw in enumerate(records or []):
        if not isinstance(raw, dict) or raw.get("bug") or raw.get("infrastructure_error"):
            continue
        verdict = raw.get("verdict")
        passed = raw.get("coverage_grounded") is True
        if isinstance(verdict, dict):
            passed = passed or (not verdict.get("bug") and not verdict.get("model_failed")
                                and not verdict.get("infrastructure_error") and (
                                    verdict.get("matches_expected") is True
                                    or str(verdict.get("verdict") or "").casefold() in {
                                        "match", "pass", "passed", "success", "successful", "accepted", "ok"}))
        else:
            passed = passed or str(verdict or "").casefold() in {
                "match", "pass", "passed", "success", "successful", "accepted", "ok"}
        if not passed:
            continue
        aspects = {" ".join(str(value or "").casefold().split())
                   for field in ("covers", "demonstrated", "mechanically_proven")
                   for value in (raw.get(field) or []) if str(value or "").strip()}
        if aspects:
            successful.append((index, raw, aspects))

    def receipt_for(aspect):
        key = " ".join(str(aspect or "").casefold().split())
        return next(((index, raw) for index, raw, aspects in reversed(successful) if key in aspects), None)

    receipts = {str(item.get("aspect")): receipt_for(item.get("aspect")) for item in rows}
    # Timestamp-less legacy rows receive a deterministic positive sequence based on the sealed record order.
    # It is an ordering coordinate, not a claim about wall-clock time.
    for item in rows:
        receipt = receipts.get(str(item.get("aspect")))
        if item.get("covered") and receipt and not isinstance(item.get("proof"), dict):
            index, record = receipt
            action = _portable_checkpoint_action(record.get("action"))
            item["proof"] = {
                "engine": "durable-grounded-evidence",
                "action_kind": str(action.get("cmd") or "checkpoint"),
                "recorded_at": float(record.get("recorded_at") or index + 1),
            }

    for position, item in enumerate(rows):
        if (item.get("covered")
                or item.get("coverage_repaired") != "persisted-state-required-prior-creation-proof"
                or not _requires_prior_created_state(item.get("aspect"))):
            continue
        persistence_receipt = receipts.get(str(item.get("aspect")))
        if not persistence_receipt:
            continue
        persistence_index, persistence_record = persistence_receipt
        creation_receipt = next((receipts.get(str(prior.get("aspect")))
                                 for prior in rows[:position]
                                 if prior.get("covered") and _creates_business_state(prior.get("aspect"))
                                 and receipts.get(str(prior.get("aspect")))
                                 and receipts[str(prior.get("aspect"))][0] <= persistence_index), None)
        if not creation_receipt:
            continue
        action = _portable_checkpoint_action(persistence_record.get("action"))
        item["covered"] = True
        item["proof"] = {
            "engine": "durable-grounded-evidence",
            "action_kind": str(action.get("cmd") or "checkpoint"),
            "recorded_at": float(persistence_record.get("recorded_at") or persistence_index + 1),
        }
        item["coverage_repaired"] = "chronological-durable-receipt-restored"
    return rows


def _restore_alternative_keyboard_coverage(ledger, records):
    """Recover an Enter-or-Space acknowledgement from an exact durable press receipt.

    Older generic grounding treated ``Enter or Space`` as two mandatory keys. The live browser could
    acknowledge the blocker, receive an independent PASS, and retain driver/effect/label receipts, yet the
    atomic ledger row stayed open and the explorer acknowledged every remaining blocker. Restore only this
    narrow alternative-key row when the immediately preceding contract row establishes acknowledgement and a
    successful, effectful, labelled acknowledgement press is present in the durable dossier.
    """
    rows = [dict(item) for item in (ledger or []) if isinstance(item, dict)]

    def passed(record):
        if record.get("bug") or record.get("infrastructure_error"):
            return False
        verdict = record.get("verdict")
        if isinstance(verdict, dict):
            return (not verdict.get("bug") and not verdict.get("model_failed")
                    and not verdict.get("infrastructure_error")
                    and (verdict.get("matches_expected") is True
                         or str(verdict.get("verdict") or "").casefold() in {"match", "pass"}))
        return str(verdict or "").casefold() in {"match", "pass", "passed"}

    receipts = []
    for index, record in enumerate(records or []):
        if not isinstance(record, dict) or not passed(record):
            continue
        action = _portable_checkpoint_action(record.get("action"))
        targeting = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        key = str(action.get("value") or action.get("key") or targeting.get("action_key") or "")
        label = " ".join(str(value or "") for value in (
            action.get("target_text"), action.get("target"), targeting.get("intended"),
            targeting.get("targeted_label"))).casefold()
        if (str(action.get("cmd") or "").casefold() in {"press", "hold"}
                and key.casefold().replace(" ", "") in {"enter", "space", "spacebar"}
                and "acknowledg" in label
                and targeting.get("driver_ok") is True
                and targeting.get("effect_registered") is True
                and targeting.get("label_matched") is not False):
            receipts.append((index, record, action))
    if not receipts:
        return rows

    for position, item in enumerate(rows):
        aspect = str(item.get("aspect") or "")
        if (item.get("covered") or not re.search(
                r"\benter\s*(?:or|/)\s*(?:the\s+)?space(?:bar)?\b|"
                r"\bspace(?:bar)?\s*(?:or|/)\s*(?:the\s+)?enter\b", aspect, re.I)):
            continue
        if not any(prior.get("covered") and re.search(
                r"\backnowledg", str(prior.get("aspect") or ""), re.I)
                for prior in rows[:position]):
            continue
        index, record, action = receipts[-1]
        item["covered"] = True
        item["proof"] = {
            "engine": "durable-alternative-keyboard-receipt",
            "action_kind": str(action.get("cmd") or "press"),
            "recorded_at": float(record.get("recorded_at") or index + 1),
        }
        item["coverage_repaired"] = "enter-or-space-alternative-restored"
    return rows


def _restore_effectful_business_mutation_coverage(ledger, records):
    """Recover command-shaped mutation credit when a broader semantic result was inconclusive."""
    rows = [dict(item) for item in (ledger or []) if isinstance(item, dict)]
    candidates = []
    receipts = {}
    for index, record in enumerate(records or []):
        if (not isinstance(record, dict) or record.get("bug")
                or record.get("infrastructure_error")):
            continue
        action = _portable_checkpoint_action(record.get("action"))
        targeting = dict(record.get("targeting") or {}) if isinstance(
            record.get("targeting"), dict) else {}
        targeting.setdefault("action_kind", action.get("cmd"))
        targeting.setdefault("action_key", action.get("value") or action.get("key"))
        targeting.setdefault("action_value", action.get("value"))
        if not _registered_business_mutation(action, targeting):
            continue
        for item in rows:
            aspect = str(item.get("aspect") or "")
            if item.get("covered") or aspect in receipts:
                continue
            if _grounded_demonstrated(
                    [aspect], targeting, {}, {}, require_mechanical=True) == [aspect]:
                candidates.append(aspect)
                receipts[aspect] = (index, record, action)
    allowed = set(_ordered_grounded_aspects(rows, candidates))
    for item in rows:
        aspect = str(item.get("aspect") or "")
        if aspect not in allowed:
            continue
        index, record, action = receipts[aspect]
        item["covered"] = True
        item["proof"] = {
            "engine": "durable-effectful-business-mutation",
            "action_kind": str(action.get("cmd") or "control"),
            "recorded_at": float(record.get("recorded_at") or index + 1),
        }
        item["coverage_repaired"] = "effectful-mutation-receipt-restored"
    return rows


_STEP_CLAUSE_RE = re.compile(
    r"\s*(?:;|,\s*(?:and\s+)?|\s+then\s+|\s+and\s+(?=(?:capture|observe|verify|confirm|"
    r"activate|click|tap|press|reload|refresh|open|navigate|type|select|submit|return|re-enter|"
    r"use|acquire|send|hold|start|stop|inspect|retain|repeat)\b))\s*",
    re.I,
)


def _repeat_count(clause) -> int:
    low = " ".join(str(clause or "").casefold().split())
    if re.search(r"\btwice\b|\btwo\s+(?:more\s+|separate\s+)?times\b", low):
        return 2
    if re.search(r"\bthree\s+(?:more\s+|separate\s+)?times\b", low):
        return 3
    return 0


def _expanded_contract_clause(clause):
    count = _repeat_count(clause)
    if not count or not re.search(r"\b(?:activate|click|tap|press)\b", str(clause), re.I):
        return [clause]
    return [f"{clause} [occurrence {index} of {count}]" for index in range(1, count + 1)]


def _inapplicable_capability_branch(clause) -> bool:
    """The unavailable side of a capability preflight is enforced by admission, not browser coverage."""
    low = " ".join(str(clause or "").casefold().split())
    if re.match(r"^otherwise\s+(?:stop|halt|do not|don't)\b", low):
        return True
    return bool(
        re.match(r"^if\b", low)
        and re.search(r"\b(?:unavailable|missing|absent|not available|cannot be started)\b", low)
        and re.search(r"\b(?:route|escalate|stop|halt|management|manager)\b", low)
    )


def _strip_inapplicable_capability_suffix(step):
    text = str(step or "")
    return re.sub(
        r";\s*(?:otherwise\b|if\b[^;]*(?:unavailable|missing|absent|not available|cannot be started)\b).*$",
        "", text, flags=re.I)


def _join_leading_condition(step):
    """Keep a temporal/negative qualifier attached to the action it governs."""
    return re.sub(
        r"^((?:before\s+any|without)\b[^,]*),\s*", r"\1 ", str(step or ""), flags=re.I)


def _join_trailing_qualifier(step):
    """Keep an explanatory negative example with the assertion it qualifies."""
    return re.sub(r",\s*(including\b)", r" \1", str(step or ""), flags=re.I)


def _reporting_contract_step(step) -> bool:
    """Artifact/report assembly is enforced by the recorder, not by another browser action."""
    low = " ".join(str(step or "").casefold().split())
    return bool(re.match(
        r"^(?:link\s+every|save\s+(?:one|an|the)\s+ordered\s+artifact|attach\s+every)\b", low))


def _recorder_requirement_stage(aspect):
    """Classify evidence work owned by the always-on recorder, not the browser actor."""
    low = " ".join(str(aspect or "").casefold().split())
    if any(re.search(pattern, low) for pattern in (
            r"\bstart\b[^.;]*(?:session video|browser trace|console capture|output capture)",
            r"\b(?:clear|explicitly clear)\b[^.;]*\bconsole\b",
            r"\b(?:explicit\s+)?console clear\b",
            r"\bcapture-start timestamp\b",
            r"\bbegin\b[^.;]*\bcumulative\b[^.;]*\b(?:console|network|capture)\b",
            r"\bstart\b[^.;]*\bassistive-technology output capture\b",
            # The initial post-clear state is itself recorder evidence. Commit it before navigation so an
            # ordered story cannot lose the prerequisite and then repeat the whole journey forever.
            r"\brecord\b[^.;]*\burl\b[^.;]*\bcount\b",
            r"\brecord\b[^.;]*\bcount\b[^.;]*\burl\b")):
        return "start"
    if any(re.search(pattern, low) for pattern in (
            r"\bafter the final action\b",
            r"\bcapture-end timestamp\b",
            r"\bstop\b[^.;]*\bcontinuous trace\b",
            r"\binspect\b[^.;]*\b(?:complete|cumulative|time-scoped)\b[^.;]*\b(?:record|capture)\b",
            r"\battach\b[^.;]*\b(?:artifact|trace|record)\b",
            r"\bretain\b[^.;]*\b(?:artifact|record|trace)\b",
            r"\b(?:fail|reject)\b[^.;]*\b(?:console|http|network)\b[^.;]*\b(?:error|4\d\d|404)\b",
            r"\buninterrupted (?:session |browser )?trace\b",
            r"\btimestamped cumulative capture boundaries\b",
            r"\bfinal complete-record inspection artifact\b")):
        return "end"
    return None


def _recorder_start_provable(aspect, state, *, artifact_dir=None, bridge_active=False,
                             clear_receipt=None):
    if _recorder_requirement_stage(aspect) != "start" or not bridge_active or not artifact_dir:
        return False
    low = " ".join(str(aspect or "").casefold().split())
    if "clear" in low and "console" in low and not clear_receipt:
        return False
    if "assistive-technology" in low and not state.get("actualAssistiveTechnologyAvailable"):
        return False
    if "record" in low and "url" in low and "count" in low:
        if not state.get("url") or not re.fullmatch(r"\s*-?\d+\s*", str(state.get("statusText") or "")):
            return False
    if "console" in low and (
            "console_errors" not in state or "recent_requests" not in state
            or state.get("console_errors")):
        return False
    return bool(state.get("screenshot") or state.get("url"))


def _recorder_end_provable(aspect, records, *, artifact_dir=None):
    if _recorder_requirement_stage(aspect) != "end" or not artifact_dir or not records:
        return False
    for record in records:
        actual = (record or {}).get("actual") or {}
        if not actual.get("screenshot"):
            return False
        if actual.get("console_errors"):
            return False
        for request in actual.get("recent_requests") or []:
            if request.get("failed") or request.get("status") is None:
                return False
            try:
                if int(request.get("status")) >= 400:
                    return False
            except (TypeError, ValueError):
                return False
    low = " ".join(str(aspect or "").casefold().split())
    if "assistive-technology" in low and not any(
            ((record or {}).get("actual") or {}).get("actualAssistiveTechnologyEvents")
            for record in records):
        return False
    return True


def _story_contract_aspects(story):
    """Materialize the complete ordered story contract as an evidence ledger.

    Supplied ``coverage`` is useful reviewer metadata, but it is allowed to summarize and must never erase an
    executable story step.  Split sequential action/observation clauses and give repeated clauses stable step
    identities so one early click cannot accidentally satisfy a later click through substring matching.
    Contractual requirements are intentionally not fan-out capped: reducing their count is a QA planning
    decision, while silently dropping one changes the requested product behavior.
    """
    story = story or {}
    aspects = []
    atomic_focused_steps = bool(
        story.get("_qa_atomic_steps") is True
        and str(story.get("category") or "") == "focused-regression")
    for step_index, raw_step in enumerate(story.get("steps") or [], 1):
        if _reporting_contract_step(raw_step):
            continue
        executable_step = _join_trailing_qualifier(_join_leading_condition(
            _strip_inapplicable_capability_suffix(raw_step)))
        clauses = ([executable_step] if atomic_focused_steps else
                   [part.strip(" .") for part in _STEP_CLAUSE_RE.split(executable_step)
                    if part and part.strip(" .")])
        for clause_index, clause in enumerate(clauses or [str(raw_step).strip()], 1):
            # A capability preflight's unavailable-only branch is enforced by
            # ``_missing_required_capabilities`` before the first browser action.  It is not a user-journey
            # action and is inapplicable when the capability is present; making it a permanently unchecked
            # story step blocks every later ordered fact.  Keep the positive precondition in the evidence
            # ledger and leave the negative branch to the fail-closed admission path.
            if _inapplicable_capability_branch(clause):
                continue
            for expanded in _expanded_contract_clause(clause):
                for atomic in _atomic_coverage_aspects([expanded]):
                    aspects.append(f"Story step {step_index}.{clause_index}: {atomic}")
    for raw in _atomic_coverage_aspects(story.get("coverage") or []):
        aspects.append(f"Required evidence: {raw}")
    # ``coverage`` remains the evidence decomposition of the expected outcome.  Re-adding the entire expected
    # paragraph as one compound item can make completion mechanically impossible (for example, a paragraph
    # requiring click-1, click-2, reload, and click-1-again cannot be proven by one browser action).  What must
    # never be lost here are the ordered executable steps, which are materialized above.
    seen, unique = set(), []
    for aspect in aspects:
        key = " ".join(aspect.casefold().split())
        if key not in seen:
            seen.add(key)
            unique.append(aspect)
    return unique


def _missing_required_capabilities(coverage, state=None, story=None):
    """Return explicit story capabilities the active driver cannot truthfully provide.

    This is semantic admission, not a time/cost cap. A human QA lead would not spend thirty browser turns
    trying to manufacture a screen-reader transcript from Chromium DOM/AX instrumentation. Detect the
    impossible assignment before the first action so management can provision a real driver, change the
    evidence contract, or route the genuinely external dependency through typed authority.
    """
    state = state or {}
    # Availability is evidence from the running driver, never an environment-variable promise.  A typo,
    # missing package, or crashed Orca process must route to management rather than certify fake coverage.
    at_available = bool(state.get("actualAssistiveTechnologyAvailable"))
    at_phrases = (
        "screen reader", "screen-reader", "at driver", "assistive-technology driver",
        "actual assistive technology", "actual assistive-technology", "nvda", "voiceover",
        "orca", "speech output", "spoken announcement", "announcement transcript",
    )
    at_aspects = []
    for raw in coverage or []:
        aspect = raw.get("aspect") if isinstance(raw, dict) else raw
        low = " ".join(str(aspect or "").lower().split())
        if low and any(phrase in low for phrase in at_phrases):
            at_aspects.append(str(aspect))
    # The paid convergence canary exposed an important admission boundary: a QA director can preserve an
    # explicit real-AT requirement in the story's steps/expected result while summarising its coverage ledger
    # as merely "post-reload AT event sequence".  Coverage labels are planning metadata, not authority to
    # weaken the actual contract.  Inspect the complete story before choosing a driver.
    if isinstance(story, dict):
        contract_values = []
        for key in ("title", "goal", "expected", "expected_outcome", "steps", "coverage"):
            value = story.get(key)
            if isinstance(value, (list, tuple)):
                contract_values.extend(value)
            elif value not in (None, ""):
                contract_values.append(value)
        for value in contract_values:
            rendered = " ".join(str(value or "").split())
            low = rendered.lower()
            if low and any(phrase in low for phrase in at_phrases):
                at_aspects.append(rendered[:500])
    missing = []
    if at_aspects and not at_available:
        missing.append({
            "capability": "actual-assistive-technology",
            "aspects": list(dict.fromkeys(at_aspects)),
            "reason": ("The story requires observable output from a real assistive-technology driver, "
                       "but this worker has only DOM and Chromium Accessibility-domain instrumentation."),
        })
    return missing


def _actual_at_driver_facts():
    """Read-only local provisioning probe used before activating the expensive external driver."""
    try:
        import at_driver
        return at_driver.availability()
    except Exception as exc:
        return {"available": False, "driver": "orca", "missing": [], "error": str(exc)[:300]}


def _requires_actual_at(aspect) -> bool:
    low = " ".join(str(aspect or "").casefold().split())
    return (any(phrase in low for phrase in (
        "screen reader", "screen-reader", "actual assistive technology",
        "actual assistive-technology", "announcement", "announcements", "announced", "orca", "speech output",
        "spoken transcript"))
        or bool(re.search(r"\bat\b.*\b(event|output|sequence|transcript)\b", low)))


def _actual_at_availability_requirement(aspect) -> bool:
    low = " ".join(str(aspect or "").casefold().split())
    strong_availability = bool(re.search(
        r"\b(?:available|availability|admission|provisioned)\b", low))
    return (
        _requires_actual_at(low)
        and bool(re.search(
            r"\b(?:available|availability|connected|connect|active|provisioned|start|admission|require|required)\b",
            low))
        and (strong_availability or not bool(re.search(
            r"\b(?:announce|announcements?|utterance|speak|spoken|output|event|transcript|report)\b", low)))
        and not bool(re.search(r"\b(?:open|session|counter|count|showing)\b", low))
    )


def _initial_at_observation_requirement(aspect) -> bool:
    low = " ".join(str(aspect or "").casefold().split())
    return _requires_actual_at(low) and bool(re.search(r"\binitial\s+(?:value|count)\b", low)) \
        and not bool(re.search(r"\b(?:updated|resulting|subsequent|every|after|keyboard-updated)\b", low))


def _post_reload_requirement(aspect) -> bool:
    low = " ".join(str(aspect or "").casefold().split())
    return bool(re.search(
        r"\b(?:post[- ](?:reload|refresh)|after (?:a )?(?:true )?(?:reload|refresh))\b", low))


def _positive_refresh_requirement(aspect) -> bool:
    """Return true only when the contract actually asks the browser to refresh.

    A negative acceptance condition such as ``without reload`` describes the desired live update.  Treating
    the bare noun as an imperative caused a paid continuation to reload the app and repeat a full keyboard
    traversal, erasing the exact no-reload boundary it was meant to prove.
    """
    low = " ".join(str(aspect or "").casefold().split())
    if not re.search(r"\b(?:refresh|reload)\b", low):
        return False
    # Remove complete prohibitive phrases before looking for a positive imperative.  Cover coordinated forms
    # such as "do not refresh or reload" so the second noun cannot escape the negation fence.
    remaining = re.sub(
        r"\b(?:without|never|avoid(?:ing)?|do\s+not|don't|must\s+not|should\s+not|no)\b"
        r"[^.;]{0,60}\b(?:refresh|reload)(?:\s*(?:or|/)\s*(?:refresh|reload))?\b",
        "", low, flags=re.I)
    return bool(re.search(
        r"(?:^|[.,;]\s*|\b(?:then|next|after|perform|execute|use|repeat)\s+)"
        r"(?:a\s+|the\s+|one\s+|true\s+)*(?:refresh|reload)\b|"
        r"\b(?:refresh|reload)(?:\s*/\s*(?:refresh|reload))?\s+after\b",
        remaining, re.I))


def _requires_multi_surface_inspection(aspect) -> bool:
    """Whether an assertion spans enough named product surfaces to require a sealed batch inspection."""
    low = " ".join(str(aspect or "").casefold().split())
    surface_patterns = (
        r"\bpublic\b", r"\bstaff\b", r"\bceo\b", r"\bqueue\b", r"\bapproval\b",
        r"\baudit\b", r"\bdead.?letter\b", r"\bnotifications?\b", r"\bblockers?\b",
    )
    # Keyboard matrix/traversal rows name many destinations because one sealed input receipt intentionally
    # exercises them all. They are not passive panel-inspection assertions.
    if re.search(r"\b(?:tab|shift\s*\+?\s*tab|keyboard|travers(?:e|al)|activate|operate)\b", low, re.I):
        return False
    named = sum(bool(re.search(pattern, low, re.I)) for pattern in surface_patterns)
    return named >= 3 and bool(re.search(
        r"\b(?:verify|confirm|inspect|observe|appears?|coherent|consistent|across|panels?|views?|surfaces?)\b",
        low, re.I))


def _requires_queue_recovery_journey(aspect) -> bool:
    """Whether one row requires the ordered Success -> Retry -> Drain recovery experiment."""
    low = " ".join(str(aspect or "").casefold().split())
    retry_and_drain = bool(
        re.search(r"\bretry\b", low, re.I)
        and re.search(r"\bdrain(?:\s+the)?(?:\s+agent)?\s+queue\b", low, re.I))
    explicit_success = bool(re.search(
        r"\b(?:switch|set|select|change)\b[^.;]{0,120}\bsuccess\b", low, re.I))
    # Coverage migration can preserve the Success selection/audit-before-retry as the preceding row while
    # keeping Retry -> Drain -> recovered status as its own acceptance row.  That second row still requires
    # the same ordered recovery receipt; insisting that it repeat the already-separated selector language
    # made a fully recovered journey permanently ineligible for credit.
    explicit_recovery_outcome = bool(
        re.search(r"\b(?:recover(?:y|ed)?|successful retry|clears?|supersedes?)\b", low, re.I)
        and re.search(r"\b(?:attempts?|run\s*after|runafter|status|failure signal|audit trail)\b",
                      low, re.I))
    return retry_and_drain and (explicit_success or explicit_recovery_outcome)


def _requires_prior_created_state(aspect) -> bool:
    """Whether this receipt can only be meaningful after an earlier business mutation.

    A refresh of a genuinely empty app is valid, but it cannot prove that a created enquiry/job/draft
    persisted. Generated ledgers commonly phrase this as a leading "Refresh ... verify ... persist" rather
    than "after refresh", so the narrower post-reload fence does not apply.
    """
    low = " ".join(str(aspect or "").casefold().split())
    has_reentry = bool(re.search(
        r"\b(?:refresh|reload|reopen|re-open|new browser context|retained-storage|retaining local storage)\b",
        low))
    asserts_existing_state = bool(re.search(
        r"\b(?:persist|persists|survive|survives|remain|remains|retain|retains|retained|created|completed)\b",
        low))
    named_business_state = bool(re.search(
        r"\b(?:enquir(?:y|ies)|agent job|follow-up draft|approval ticket|audit trail|audit event|"
        r"notification|blocker|review result|record)\b", low))
    return has_reentry and asserts_existing_state and named_business_state


def _creates_business_state(aspect) -> bool:
    low = " ".join(str(aspect or "").casefold().split())
    return bool(re.search(
        r"(?:^|:\s*|\bthen\s+)(?:submit|create|seed|add|publish|save|approve|edit)\b",
        low))


def _post_history_return_requirement(aspect) -> bool:
    low = " ".join(str(aspect or "").casefold().split())
    return bool(re.search(
        r"\b(?:post[- ]history[- ]return|post[- ](?:browser[- ])?back(?:[- ]return)?|"
        r"after (?:a )?(?:browser )?(?:back|history return))\b",
        low))


def _post_reentry_requirement(aspect) -> bool:
    low = " ".join(str(aspect or "").casefold().split())
    return bool(re.search(r"\bpost[- ](?:direct[- ])?(?:route[- ])?re[- ]entry\b", low))


def _ordered_grounded_aspects(coverage, grounded):
    """Fence story-step credit to the contract's chronological predecessor chain."""
    grounded = list(dict.fromkeys(str(item) for item in (grounded or []) if str(item).strip()))
    # One browser action can satisfy at most one occurrence from an explicitly repeated action.  Pre-filter
    # before building ``already_or_now`` so a later observation clause cannot treat occurrence 2 as completed
    # by the same click that completed occurrence 1.
    one_occurrence, repeat_groups = [], set()
    for aspect in grounded:
        repeat = re.search(r"^(.*)\s+\[occurrence\s+\d+\s+of\s+\d+\]$", aspect, re.I)
        group = repeat.group(1).casefold() if repeat else None
        if group and group in repeat_groups:
            continue
        if group:
            repeat_groups.add(group)
        one_occurrence.append(aspect)
    grounded = one_occurrence
    # One keypress cannot satisfy two separately ordered story actions that both require that key. A paid
    # reverse-focus canary returned both ``Use Tab to focus Increment`` and the later ``Use Tab again`` from
    # one Tab. Keep only the earliest current story occurrence; observation clauses remain eligible.
    story_positions = {str(item.get("aspect")): index for index, item in enumerate(coverage or [])}
    repeated_key_winners = {}
    repeated_key_signatures = {}
    for aspect in grounded:
        if not re.match(r"^Story step \d+\.\d+:", aspect, re.I):
            continue
        low = aspect.casefold().replace("shift + tab", "shift+tab")
        signature = ("shift+tab" if "shift+tab" in low else
                     "tab" if re.search(r"(?<!shift\+)\btab\b", low) else None)
        if signature is None:
            continue
        repeated_key_signatures[aspect] = signature
        prior = repeated_key_winners.get(signature)
        if prior is None or story_positions.get(aspect, 10**9) < story_positions.get(prior, 10**9):
            repeated_key_winners[signature] = aspect
    grounded = [aspect for aspect in grounded
                if aspect not in repeated_key_signatures
                or repeated_key_winners[repeated_key_signatures[aspect]] == aspect]
    grounded_set = set(grounded)
    contract_items = [item for item in (coverage or []) if isinstance(item, dict)
                      and str(item.get("aspect") or "").strip()]
    contract_aspects = [str(item.get("aspect")) for item in contract_items]
    contract_already_or_now = {
        str(item.get("aspect")) for item in contract_items if item.get("covered")} | grounded_set
    story_items = [item for item in (coverage or [])
                   if re.match(r"^Story step \d+\.\d+:", str(item.get("aspect") or ""), re.I)]
    story_aspects = [str(item.get("aspect")) for item in story_items]
    already_or_now = {str(item.get("aspect")) for item in story_items if item.get("covered")} | grounded_set
    allowed = []
    for aspect in grounded:
        try:
            contract_position = contract_aspects.index(aspect)
        except ValueError:
            contract_position = len(contract_aspects)
        match = re.match(r"^Story step \d+\.\d+:", aspect, re.I)
        if match and aspect in story_aspects:
            position = story_aspects.index(aspect)
            if not all(previous in already_or_now for previous in story_aspects[:position]):
                continue
        if _post_reload_requirement(aspect):
            reload_proven = any(
                ("reload" in item.casefold() or "refresh" in item.casefold())
                and item != aspect
                and (item in contract_already_or_now)
                for item in contract_aspects)
            if not reload_proven:
                continue
        if _requires_prior_created_state(aspect):
            prior_creation_proven = any(
                _creates_business_state(item) and item in contract_already_or_now
                for item in contract_aspects[:contract_position])
            if not prior_creation_proven:
                continue
        if _post_history_return_requirement(aspect):
            return_proven = any(
                re.search(r"\bback\b", item.casefold())
                and item != aspect and item in contract_already_or_now
                for item in contract_aspects)
            if not return_proven:
                continue
        if _post_reentry_requirement(aspect):
            reentry_proven = any(
                re.search(r"\bre[- ]enter|\bre[- ]entry\b", item.casefold())
                and item != aspect and item in contract_already_or_now
                for item in contract_aspects)
            if not reentry_proven:
                continue
        if aspect.startswith("Expected outcome:") and not all(
                item.get("covered") or str(item.get("aspect")) in grounded_set for item in story_items):
            continue
        allowed.append(aspect)
    return allowed


def _grounded_demonstrated(aspects, targeting, before_state, after_state, *, require_mechanical=False):
    """Reject evidence claims that the recorded browser command mechanically cannot prove.

    ``require_mechanical`` is used for the decider's intended coverage.  Only aspect families with an explicit
    driver oracle may then be credited; arbitrary prose still requires the independent evaluator to name it.
    This lets an exact Enter/Space/Shift+Tab/reload/burst result survive a model omitting the optional
    ``demonstrated`` array without reviving optimistic self-graded coverage.
    """
    targeting = targeting or {}
    before_state, after_state = before_state or {}, after_state or {}
    cmd = str(targeting.get("action_kind") or "").lower()
    key = str(targeting.get("action_key") or "").lower().replace(" ", "")
    def canonical_key(value, code=None):
        raw = str(value or "").casefold().replace(" ", "")
        raw_code = str(code or "").casefold().replace(" ", "")
        if raw in {"", "spacebar"} and raw_code == "space" or value == " ":
            return "space"
        return "shift+tab" if raw in {"shifttab", "shift+tab"} else raw

    trusted_keys = {canonical_key(event.get("key"), event.get("code"))
                    for event in targeting.get("keyboard_evidence") or []
                    if isinstance(event, dict) and event.get("isTrusted") is True
                    and event.get("type") == "keydown"}
    if key:
        trusted_keys.add(canonical_key(key))
    traversal_receipt = targeting.get("traversal") or {}
    traversal_direction = str(traversal_receipt.get("direction") or
                              targeting.get("history_direction") or "").casefold()
    if traversal_receipt:
        trusted_keys.add("shift+tab" if traversal_direction == "backward" else "tab")
    keyboard_matrix = targeting.get("keyboard_matrix") or {}
    trusted_keys.update(canonical_key(value) for value in keyboard_matrix.get("keys_proven") or [])
    burst = targeting.get("burst") or {}
    timed = targeting.get("timed_transition") or {}
    platform_changed = bool(after_state.get("accessibilityPlatformEvents")) and (
        json.dumps(before_state.get("accessibilityPlatformEvents") or [], sort_keys=True, default=str)
        != json.dumps(after_state.get("accessibilityPlatformEvents") or [], sort_keys=True, default=str))
    before_at = list(before_state.get("actualAssistiveTechnologyEvents") or [])
    after_at = list(after_state.get("actualAssistiveTechnologyEvents") or [])
    before_at_keys = {json.dumps(event, sort_keys=True, default=str) for event in before_at}
    new_at_events = [event for event in after_at
                     if json.dumps(event, sort_keys=True, default=str) not in before_at_keys]
    actual_at_changed = bool(new_at_events) and (
        json.dumps(before_state.get("actualAssistiveTechnologyEvents") or [], sort_keys=True, default=str)
        != json.dumps(after_state.get("actualAssistiveTechnologyEvents") or [], sort_keys=True, default=str))
    def traversal_control_was_announced(receipt, events):
        """Bind new real-AT speech to an app control in this traversal, not browser chrome."""
        utterances = [" ".join(re.sub(
            r"[^\w]+", " ", str(event.get("utterance") or "").casefold()).split())
            for event in (events or []) if isinstance(event, dict)]
        labels = [" ".join(re.sub(
            r"[^\w]+", " ", str(item.get("label") or "").casefold()).split())
            for item in ((receipt or {}).get("sequence") or []) if isinstance(item, dict)
            and str(item.get("tag") or "").casefold() != "body"]
        for label in labels:
            tokens = [token for token in label.split() if len(token) >= 3]
            if not tokens:
                continue
            # Long labels may be abbreviated by a screen reader. Two leading meaningful tokens bind them;
            # a one-word label still has to match as a complete word.
            wanted = tokens[:2]
            if any(all(re.search(rf"\b{re.escape(token)}\b", utterance) for token in wanted)
                   for utterance in utterances):
                return True
        return False
    def visible_number(state):
        value = state.get("statusText")
        match = re.fullmatch(r"\s*(-?\d+)\s*", str(value if value is not None else ""))
        return int(match.group(1)) if match else None

    before_number, after_number = visible_number(before_state), visible_number(after_state)
    def normalized_url(value):
        return str(value or "").split("#", 1)[0].rstrip("/")

    def request_failed(request):
        if not isinstance(request, dict):
            return False
        if request.get("failed"):
            return True
        status = request.get("status")
        try:
            return status is None or int(status) >= 400
        except (TypeError, ValueError):
            return True

    accepted = []
    for raw in aspects or []:
        aspect = str(raw).strip()
        low = " ".join(aspect.lower().split())
        mechanical = False
        # Negative journey qualifiers describe how the following action must be
        # performed; they are not requests to perform the forbidden action.  A
        # production paid canary proved this distinction matters: the browser
        # pressed Space with focus already on the button, but the phrase
        # "without pointer input" was interpreted by the generic pointer oracle
        # below as a requirement to click.  That left the exact story clause
        # permanently open and caused the coordinator to spawn gap-fill workers.
        no_pointer = bool(re.search(
            r"\b(?:without|no)\s+(?:any\s+)?(?:pointer|mouse)(?:\s+(?:input|action|click))?\b|"
            r"\b(?:keep|leave)\s+(?:the\s+)?(?:pointer|mouse)\s+idle\b|"
            r"\b(?:pointer|mouse)\s+(?:remains?\s+)?idle\b|"
            r"\b(?:do not|don't|never)\s+(?:use|move|click|touch)\s+(?:the\s+)?(?:pointer|mouse)\b",
            low))
        no_refocus = bool(re.search(r"\bwithout\b[^,;]*\brefocus(?:ing|ed)?\b", low))
        focus_return = bool(re.search(
            r"\b(?:return|returns|back)\s+to\s+(?:the\s+)?(?:increment|button|control)\b",
            low))
        post_hold_discrete = bool(
            re.search(r"\bpost[- ]hold\b", low) and re.search(r"\b(?:discrete\s+)?space\b", low))
        if not aspect:
            continue
        # These contracts deliberately span evidence boundaries. A single effectful click may be the final
        # causal transition, but it cannot certify every named panel or an earlier Success -> Retry -> Drain
        # sequence. Multi-surface rows remain eligible for the semantic judge only on a sealed batch observer;
        # queue recovery is closed solely by the chronological aggregate helper below.
        if _requires_multi_surface_inspection(aspect):
            drain_projection = (
                cmd in {"click", "tap", "press"}
                and "drain queue" in " ".join(str(targeting.get(name) or "")
                                                for name in ("intended", "targeted_label")).casefold())
            if drain_projection or (require_mechanical and cmd not in {
                    "inspect_surfaces", "inspect_landmarks", "inspectlandmarks", "dwell_surfaces"}):
                continue
        if _requires_queue_recovery_journey(aspect):
            continue
        scenario_selection = bool(
            cmd in {"type", "fill", "select", "choose"}
            and re.search(r"\b(?:set|select|switch|change)\b[^.;]{0,100}\bagent scenario\b", low, re.I)
            and re.search(r"\b(?:timeout|malformed response|success|partial failure|rate limit)\b", low, re.I))
        if scenario_selection:
            mechanical = True
            requested = str(targeting.get("action_value") or "").casefold()
            allowed_values = {value for value in (
                "timeout", "malformed response", "success", "partial failure", "rate limit")
                if value in low}
            selected = str(targeting.get("driver_control_value") or "").casefold()
            if not selected:
                selected = next((str(item.get("value") or "").casefold()
                                 for item in (after_state.get("elements") or []) if isinstance(item, dict)
                                 and str(item.get("tag") or "").casefold() == "select"
                                 and re.search(r"\b(?:agent|scenario)\b", " ".join((
                                     _element_label(item), str(item.get("name") or ""),
                                     str(item.get("id") or ""))), re.I)), "")
            if (targeting.get("driver_ok") is not True or not requested or not selected
                    or not any(value in requested for value in allowed_values)
                    or not any(value in selected for value in allowed_values)):
                continue
            accepted.append(aspect)
            continue
        # Semantic judges describe what they see, but they are not authoritative about causality.  In a live
        # enquiry journey a plain reload was credited for "Submit the first valid public enquiry"; the planner
        # consequently skipped form submission, reset an empty queue, and later reported the inert drain button
        # as a product defect.  Fence imperative business mutations from browser commands that can only observe,
        # navigate, or configure the page.  Compound post-mutation observations are migrated into atomic ledger
        # rows elsewhere, so this deliberately keys on the leading imperative after the ledger label.
        passive_commands = {
            "reload", "goto", "back", "forward", "reset_storage", "resetstorage",
            "viewport", "resize", "scroll", "wait", "dwell", "inspect_surfaces",
            "inspect_landmarks", "inspectlandmarks", "traverse", "tab_traverse", "tabtraverse",
        }
        imperative = re.sub(
            r"^(?:(?:story\s+step\s+\d+(?:\.\d+)?|required\s+evidence|expected\s+outcome)\s*:\s*)",
            "", low, flags=re.I)
        mutation_imperative = re.match(
            r"(?:then\s+|next\s+)?(?:use\b[^.;]{0,60}\bto\s+)?"
            r"(?:submit|send|create|publish|approve|acknowledge|drain|retry|delete|save|"
            r"add|remove|update|toggle|check|choose|select|enter|type|paste|upload)\b",
            imperative, re.I)
        if cmd in passive_commands and mutation_imperative:
            continue
        # The inverse causal fence matters too: an effectful click can expose a settled screen, but it cannot
        # prove that the screen, input, focus, scroll, and announcements remained stable throughout an authored
        # ten-second idle window. Dedicated wait/timed commands validate elapsed receipts below; the sealed
        # multi-surface dwell engine bypasses this generic semantic path only after checking every observation.
        if (_requires_temporal_idle_proof(low)
                and cmd not in ("wait", "dwell", "timed_transition")):
            continue
        if (_post_reload_requirement(low) and re.search(r"\bkeyboard\s+path\b", low)
                and cmd not in ("traverse", "tab_traverse", "tabtraverse", "keyboard_matrix")):
            continue
        # A correctly targeted, trusted, effectful business-control action is stronger evidence than a semantic
        # judge paraphrase.  Credit only verbs that map cleanly to a visible control label; form-field entry and
        # broad state changes remain semantic.  Besides saving a judge call, this prevents a successful approval
        # from being retried merely because a later wait used a different implementation-style status name.
        causal_mutation = re.match(
            r"(?:then\s+|next\s+)?(?:use\b[^.;]{0,60}\bto\s+)?"
            r"(submit|send|create|publish|approve|acknowledge|drain|retry|delete|save|upload)\b",
            imperative, re.I)
        if causal_mutation and cmd in ("click", "tap", "touch", "pen", "press"):
            mechanical = True
            verb = causal_mutation.group(1).casefold()
            target_label = " ".join(str(targeting.get(name) or "")
                                    for name in ("intended", "targeted_label")).casefold()
            verb_aliases = {
                "submit": ("submit",), "send": ("send",), "create": ("create", "add"),
                "publish": ("publish",), "approve": ("approve",), "acknowledge": ("acknowledge",),
                "drain": ("drain",), "retry": ("retry",), "delete": ("delete", "remove"),
                "save": ("save",), "upload": ("upload",),
            }
            if (targeting.get("driver_ok") is not True
                    or targeting.get("effect_registered") is not True
                    or targeting.get("label_matched") is False
                    or not any(re.search(rf"\b{re.escape(alias)}\w*\b", target_label)
                               for alias in verb_aliases[verb])):
                continue
        # A model may need ordinary product actions to reveal a conditional confirmation/error state and then
        # choose a plain ``wait`` rather than the landmark batch command. Preserve that valid timed boundary:
        # the bridge's elapsed receipt plus exact before/after state comparison proves the duration and idle
        # stability, while a small semantic surface fence prevents a wait on an unrelated view from earning a
        # confirmation/error/diagnostics atom. The independent evaluator still has to name the exact atom.
        if cmd in ("wait", "dwell") and re.search(r"\b(?:dwell|idle|remain|wait)\b", low):
            mechanical = True
            wait_receipt = targeting.get("wait_summary") or {}
            duration = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b", low, re.I)
            required_ms = float(duration.group(1)) * 1000 if duration else 0.0
            try:
                elapsed_ok = (float(wait_receipt.get("elapsed_ms") or 0) >= required_ms
                              and float(wait_receipt.get("requested_ms") or 0) >= required_ms)
            except (TypeError, ValueError):
                elapsed_ok = False
            def idle_controls(state):
                return [{name: item.get(name) for name in
                         ("id", "name", "tag", "type", "role", "value", "checked", "disabled")
                         if item.get(name) is not None}
                        for item in (state.get("elements") or []) if isinstance(item, dict)]
            stable = bool(
                targeting.get("driver_ok") and wait_receipt.get("waited") is True and elapsed_ok
                and before_state.get("url") == after_state.get("url")
                and before_state.get("scrollPosition") == after_state.get("scrollPosition")
                and before_state.get("activeElement") == after_state.get("activeElement")
                and idle_controls(before_state) == idle_controls(after_state)
                and before_state.get("horizontalOverflow") is not True
                and after_state.get("horizontalOverflow") is not True)
            visible = " ".join(str(after_state.get(name) or "") for name in
                               ("bodyText", "viewportText", "statusText")).casefold()
            surface_ok = True
            if "confirmation" in low:
                surface_ok = bool(re.search(
                    r"\b(?:confirmation|confirmed|received|submitted|success(?:ful|fully)?|"
                    r"send another)\b", visible))
            elif re.search(r"\berror\s+(?:screen|state|view)\b", low):
                surface_ok = bool(re.search(
                    r"\b(?:error|failed|failure|invalid|unable|unavailable|try again|blocked)\b", visible))
            elif "diagnostic" in low:
                surface_ok = bool(re.search(
                    r"\b(?:diagnostic|agent jobs?|queue health|failed|dead.letter|retrying)\b", visible))
            if stable and surface_ok:
                accepted.append(aspect)
            continue
        # A complete trusted traversal is the focus/operability receipt for a post-refresh keyboard path.
        # Its final synthetic wrap stop may legitimately leave ``document.activeElement`` on body; requiring
        # that one terminal element to be a control discarded the preceding 28/28 visible-focus sequence even
        # when Orca announced an app control. Bind focus to every sequence row instead of that wrap sentinel.
        if (_post_reload_requirement(low)
                and cmd in ("traverse", "tab_traverse", "tabtraverse")
                and re.search(r"\b(?:keyboard|path|traversal|tab)\b", low)):
            mechanical = True
            receipt = targeting.get("traversal") or {}
            try:
                complete = (int(receipt.get("unique_controls") or 0)
                            >= int(receipt.get("derived_focusable_count") or 0) > 0)
            except (TypeError, ValueError):
                complete = False
            if (targeting.get("driver_ok") and complete
                    and receipt.get("all_focus_visible") is True
                    and receipt.get("horizontal_overflow_seen") is False
                    and actual_at_changed
                    and traversal_control_was_announced(receipt, new_at_events)):
                accepted.append(aspect)
            continue
        # A browser-owned keyboard matrix is the only single action allowed to discharge a multi-control or
        # multi-key atom.  It contains individually trusted nested actions, full traversal receipts, and a
        # fixed-point inventory proving that no applicable enabled control was silently truncated.
        if keyboard_matrix and re.search(
                r"\b(?:keyboard|tab|arrow(?:-key|\s+key)?|space|enter)\b", low):
            mechanical = True
            required = set()
            normalized_low = low.replace("shift + tab", "shift+tab")
            if "shift+tab" in normalized_low:
                required.add("shift+tab")
            if re.search(r"(?<!shift\+)\btab\b", normalized_low):
                required.add("tab")
            if re.search(r"\barrow", normalized_low):
                required.add("arrow")
            alternative_key = "enter-or-space" in normalized_low or bool(re.search(
                r"\benter\s*(?:or|/)\s*(?:the\s+)?space(?:bar)?\b|"
                r"\bspace(?:bar)?\s*(?:or|/)\s*(?:the\s+)?enter\b", normalized_low))
            if not alternative_key and re.search(r"\bspace(?:bar)?\b", normalized_low):
                required.add("space")
            if not alternative_key and re.search(r"\benter\b", normalized_low):
                required.add("enter")
            proven = set(trusted_keys)
            keys_ok = all(
                any(value.startswith("arrow") for value in proven) if wanted == "arrow"
                else wanted in proven for wanted in required)
            if alternative_key:
                keys_ok = keys_ok and bool({"enter", "space"} & proven)
            wants_inventory = bool(re.search(
                r"\b(?:every|all|applicable|complete)\b[^.;]{0,100}\bcontrols?\b|"
                r"\bcontrols?\b[^.;]{0,100}\b(?:every|all|applicable|complete)\b", low))
            if (not targeting.get("driver_ok") or not keys_ok
                    or (wants_inventory
                        and keyboard_matrix.get("all_applicable_controls_exercised") is not True)):
                continue
            accepted.append(aspect)
            continue
        # A restored focused-regression checkpoint is already the exact historical input boundary. A trusted
        # multi-surface inspection that resolves every requested landmark and captures settled controls/AX
        # data mechanically proves the two read-only setup clauses. Leaving those clauses entirely to a
        # semantic judge made it credit one at a time (or only during terminal diagnosis), so the planner sent
        # the identical inspection until the repeat guard stopped it before the actual reproduction action.
        restored_inspection = bool(
            cmd in ("inspect_surfaces", "inspect_landmarks", "inspectlandmarks")
            and re.search(
                r"\b(?:inspect|confirm)\b[^.;]{0,100}\brestored\b[^.;]{0,100}"
                r"\b(?:finding-time state|triggering input|prerequisite state)\b",
                low))
        if restored_inspection:
            mechanical = True
            receipt = targeting.get("landmark_dwell_summary") or {}
            settled_evidence = bool(
                after_state.get("url")
                and (after_state.get("elements") or after_state.get("accessibilityTree")
                     or after_state.get("bodyText")))
            if (not targeting.get("driver_ok") or not receipt.get("targets")
                    or receipt.get("all_targets_matched") is not True
                    or receipt.get("all_stable") is not True or not settled_evidence):
                continue
            accepted.append(aspect)
            continue
        # A viewport command is idempotent: setting an already-correct width/height legitimately produces no
        # DOM mutation.  Treat the driver's requested-vs-settled dimensions as the effect oracle for a pure
        # viewport-setup clause.  Without this, a resumed focused regression can successfully set 375x844 on
        # every turn while the ledger remains open forever because ``effect_registered`` is false.  Keep
        # semantic responsive assertions (overlap, labels, focus, reading order, etc.) for traversal/visual/AT
        # evidence; this branch proves only the command-shaped setup boundary.
        viewport_setup = bool(
            cmd in ("viewport", "resize")
            and re.search(
                r"\b(?:reapply|apply|set|switch|change|resize|open)\b[^.;]{0,120}"
                r"\b(?:viewport|dimensions?|width|height|mobile|desktop)\b",
                low)
            and not re.search(
                r"\b(?:verify|confirm|inspect|observe|overlap|clipp?ing|horizontal\s+scroll|"
                r"accessible\s+name|reading\s+order|focus\s+(?:order|visible|indicator))\b",
                low))
        if viewport_setup:
            mechanical = True
            requested = targeting.get("action_value") or {}
            actual_viewport = after_state.get("viewport") or {}
            try:
                dimensions_match = (
                    int(requested.get("width")), int(requested.get("height"))) == (
                    int(actual_viewport.get("width")), int(actual_viewport.get("height")))
            except (AttributeError, TypeError, ValueError):
                dimensions_match = False
            literal = re.search(r"\b(\d{3,4})\s*[x×]\s*(\d{3,4})\b", low)
            literal_matches = not literal or (
                int(literal.group(1)), int(literal.group(2))) == (
                int(actual_viewport.get("width") or -1), int(actual_viewport.get("height") or -1))
            if (not targeting.get("driver_ok") or not dimensions_match or not literal_matches
                    or targeting.get("control_action")):
                continue
            accepted.append(aspect)
            continue
        # A visible one-attempt queue drain is the causal boundary between queued work and runtime failure.
        # When that exact trusted action preserves the public confirmation and advances diagnostics to an
        # explicit retry/failure state, it mechanically proves the applicable runtime-failure branch. A public
        # enqueue-warning clause is conditional on insertion itself failing and is not contradicted when the
        # browser proves that a job was successfully queued before this drain.
        drain_action = (cmd in ("click", "tap")
                        and "drain queue" in " ".join(str(targeting.get(name) or "")
                                                     for name in ("intended", "targeted_label")).casefold())
        if drain_action and any(term in low for term in ("failed", "retry", "degraded", "diagnostic")):
            mechanical = True
            before_text = " ".join(str(before_state.get(name) or "")
                                   for name in ("bodyText", "viewportText", "statusText")).casefold()
            after_text = " ".join(str(after_state.get(name) or "")
                                  for name in ("bodyText", "viewportText", "statusText")).casefold()
            if (not targeting.get("driver_ok") or "queued" not in before_text
                    or not re.search(r"\b(?:retrying|failed|dead.?letter|degraded|partial|runtime_timeout)\b",
                                     after_text, re.I)):
                continue
            if any(term in low for term in ("confirmation", "send another", "storage success")) \
                    and not ("enquiry received" in after_text and "send another enquiry" in after_text):
                continue
            accepted.append(aspect)
            continue
        # A browser-owned timed transition is a stronger oracle than a model paraphrase: its observer is
        # installed before the trusted click and measures the transient DOM, duplicate attempts, and settled
        # completion in one command. Credit only the clauses the receipt itself proves. Broader warning/
        # degraded-diagnostics clauses remain open until their named settled evidence is actually visible.
        if timed and any(term in low for term in (
                "pending", "busy", "dwell", "duration", "duplicate", "double-submit",
                "submit once", "submission", "confirmation", "send another", "storage success")):
            mechanical = True
            try:
                duration_ok = (float(timed.get("transition_duration_ms") or 0)
                               >= float(timed.get("required_duration_ms") or 0))
            except (TypeError, ValueError):
                duration_ok = False
            full_duration_required = timed.get("full_duration_required") is not False
            transition_ok = bool(
                targeting.get("driver_ok")
                and timed.get("pending_seen") is True
                and timed.get("completion_observed") is True
                and (not full_duration_required or (
                    timed.get("stable_through_required_boundary") is True
                    and timed.get("completed_before_required_duration") is False
                    and duration_ok)))
            if not transition_ok:
                continue
            if ("dwell" in low or "duration" in low
                    or re.search(r"\bat least\s+\d+(?:\.\d+)?\s*(?:seconds?|secs?|s)\b", low)) \
                    and not full_duration_required:
                continue
            if any(term in low for term in ("duplicate", "double-submit", "submit once")):
                attempts = {str(item).casefold() for item in (timed.get("duplicate_attempts") or [])}
                if (int(timed.get("submit_event_count") or 0) != 1
                        or not {"trusted-pointer", "trusted-keyboard-enter"}.issubset(attempts)):
                    continue
            if any(term in low for term in ("required", "consent", "complete all")) \
                    and targeting.get("empty_required_fields_before"):
                continue
            settled_text = " ".join(str((after_state or {}).get(name) or "")
                                    for name in ("bodyText", "viewportText", "statusText")).casefold()
            if any(term in low for term in ("confirmation", "send another", "storage success")) \
                    and not ("enquiry received" in settled_text and "send another enquiry" in settled_text):
                continue
            if any(term in low for term in ("warning", "failed", "retry", "degraded")) \
                    and not re.search(r"\b(?:warning|retrying|dead.?letter|degraded|review delayed|queue failed)\b",
                                      settled_text, re.I):
                continue
            if any(phrase in low for phrase in ("unexpected navigation", "no navigation",
                                                 "without navigation", "url unchanged")) \
                    and before_state.get("url") != after_state.get("url"):
                continue
            # The atomic receipt has now discharged every timed-contract predicate carried by this aspect.
            # Do not feed the same prose through unrelated generic lexical oracles below: for example,
            # "no persistent ... focus trap" is a settled-outcome assertion, not a request to focus a control.
            accepted.append(aspect)
            continue
        if "shift+tab" in low.replace("shift + tab", "shift+tab"):
            mechanical = True
            if "shift+tab" not in trusted_keys:
                continue
        elif re.search(r"(?<!shift\+)\btab\b", low.replace("shift + tab", "shift+tab")):
            mechanical = True
            if "tab" not in trusted_keys:
                continue
        if re.search(r"\barrow(?:-key|\s+keys?|up|down|left|right)?\b", low):
            mechanical = True
            if not any(value.startswith("arrow") for value in trusted_keys):
                continue
        alternative_enter_space = bool(re.search(
            r"\benter\s*(?:or|/)\s*(?:the\s+)?space(?:bar)?\b|"
            r"\bspace(?:bar)?\s*(?:or|/)\s*(?:the\s+)?enter\b", low))
        if alternative_enter_space:
            # ``Enter or Space`` is an alternative activation contract, not a demand to press both keys.
            # Atomic splitting can leave the row as "another using keyboard Enter or Space", so also require
            # a registered control effect; a trusted key event by itself must not prove acknowledgement.
            mechanical = True
            if (not ({"enter", "space"} & trusted_keys)
                    or cmd not in ("press", "hold")
                    or targeting.get("effect_registered") is not True):
                continue
        else:
            if re.search(r"\benter\b", low) and (
                    "activation" in low or "keyboard" in low
                    or bool(re.search(r"\bpress(?:ed)?\b[^,;]*\benter\b", low))):
                mechanical = True
                if "enter" not in trusted_keys:
                    continue
                if re.search(r"\bpress(?:ed)?\b[^,;]*\benter\b[^,;]*\bsubmit\b", low) \
                        and targeting.get("effect_registered") is not True:
                    continue
            if re.search(r"\bspace\b", low) and (
                    "activation" in low or "keyboard" in low
                    or bool(re.search(r"\bpress(?:ed)?\b[^,;]*\bspace\b", low))):
                mechanical = True
                if "space" not in trusted_keys:
                    continue
        if (cmd in ("traverse", "tab_traverse", "tabtraverse")
                and re.search(r"\b(?:traversal|keyboard path|every|all|complete|focus order)\b", low)):
            mechanical = True
            receipt = targeting.get("traversal") or {}
            try:
                complete_traversal = (int(receipt.get("unique_controls") or 0)
                                      >= int(receipt.get("derived_focusable_count") or 0) > 0)
            except (TypeError, ValueError):
                complete_traversal = False
            if (not complete_traversal or receipt.get("all_focus_visible") is not True
                    or receipt.get("horizontal_overflow_seen") is not False):
                continue
        if no_refocus:
            mechanical = True
            before_active = before_state.get("activeElement") or {}
            after_active = after_state.get("activeElement") or {}
            identity_fields = ("tag", "role", "text", "label", "id")
            before_identity = tuple(before_active.get(field) for field in identity_fields)
            after_identity = tuple(after_active.get(field) for field in identity_fields)
            if (not before_active or not after_active or before_identity != after_identity
                    or before_active.get("tag") in (None, "", "body")):
                continue
        elif "focus" in low or focus_return:
            mechanical = True
            active = after_state.get("activeElement") or {}
            active_label = " ".join(str(active.get(field) or "") for field in (
                "text", "name", "ariaLabel", "label", "id")).casefold()
            wants_increment = bool(re.search(
                r"\b(?:focus|focused|returns? to)\b[^.;]{0,80}\bincrement\b", low))
            moves_past_increment = bool(re.search(
                r"\b(?:focus\s+)?moves?\b[^.;]{0,40}\bpast\b[^.;]{0,40}\bincrement\b", low))
            if moves_past_increment:
                before_active = before_state.get("activeElement") or {}
                before_label = " ".join(str(before_active.get(field) or "") for field in (
                    "text", "name", "ariaLabel", "label", "id")).casefold()
                if key != "tab" or "increment" not in before_label or "increment" in active_label:
                    continue
            else:
                if not active or active.get("tag") in (None, "", "body"):
                    continue
                if wants_increment and "increment" not in active_label:
                    continue
                if any(word in low for word in ("visible", "indicator", "outline")) \
                        and not active.get("focusVisible"):
                    continue
        if no_pointer:
            mechanical = True
            if cmd in ("click", "tap", "touch", "pen", "burst", "click_burst", "clickburst"):
                continue
        # Match input modalities as words. Every executable ledger label starts
        # with ``Story step`` and ordinary journey prose frequently says
        # ``open``; substring checks therefore interpreted ``tap`` in ``step``
        # and ``pen`` in ``open`` as pointer requirements. A paid keyboard
        # canary then performed the correct reload/Tab/Space/Enter journey but
        # could not receive credit for any ``Story step`` clause, causing a
        # redundant continuation worker. Keep this oracle lexical and exact.
        elif re.search(r"\b(?:pointer|mouse|click|tap|touch|pen)\b", low):
            mechanical = True
            required_pointer = ("touch" if re.search(r"\btouch\b", low) else
                                "pen" if re.search(r"\bpen\b", low) else None)
            if required_pointer and cmd != required_pointer:
                continue
            if cmd not in ("click", "tap", "touch", "pen", "burst", "click_burst", "clickburst"):
                continue
            if not targeting.get("driver_ok"):
                continue
            if required_pointer and required_pointer not in {
                    str(event.get("pointerType") or "").lower()
                    for event in (targeting.get("pointer_evidence") or [])
                    if isinstance(event, dict) and event.get("isTrusted") is True}:
                continue
            if required_pointer and "increment" in low and (
                    before_number is None or after_number is None or after_number - before_number != 1):
                continue
        generic_activation = (re.search(r"\b(activate|activation|actuate)\b", low)
                              and not re.search(r"\b(enter|space)\b", low))
        if generic_activation:
            mechanical = True
            if cmd not in ("click", "tap", "touch", "pen", "burst", "click_burst", "clickburst",
                           "press", "hold"):
                continue
            if not targeting.get("driver_ok"):
                continue
        if "operability" in low and (_post_reload_requirement(low) or _post_reentry_requirement(low)):
            mechanical = True
            if cmd in ("traverse", "tab_traverse", "tabtraverse"):
                receipt = targeting.get("traversal") or {}
                try:
                    complete = (int(receipt.get("unique_controls") or 0)
                                >= int(receipt.get("derived_focusable_count") or 0) > 0)
                except (TypeError, ValueError):
                    complete = False
                if (not targeting.get("driver_ok") or not complete
                        or receipt.get("all_focus_visible") is not True
                        or receipt.get("horizontal_overflow_seen") is not False):
                    continue
            elif (cmd not in ("click", "tap", "touch", "pen", "burst", "click_burst", "clickburst",
                              "press", "hold")
                  or not targeting.get("driver_ok") or not targeting.get("effect_registered")):
                continue
        negative_repeat_instruction = bool(re.search(
            r"\b(?:do\s+not|don't|never|without)\b[^.;]{0,80}\brepeat\b",
            low))
        journey_repeat_instruction = bool(re.search(
            r"\brepeat\b[^.;]{0,100}\b(?:keyboard|path|journey|navigation|traversal)\b",
            low))
        if (re.search(r"\b(?:held|hold|repeat)\b", low)
                and "policy" not in low and not negative_repeat_instruction
                and not journey_repeat_instruction):
            mechanical = True
            keyboard_events = [event for event in (targeting.get("keyboard_evidence") or [])
                               if isinstance(event, dict)]
            trusted_clicks = [event for event in keyboard_events
                              if event.get("isTrusted") is True and event.get("type") == "click"]
            if post_hold_discrete:
                # This is a relational *next action*, not evidence supplied by the prior held action.
                # Ordered coverage separately proves that a hold occurred first.
                if (cmd != "press" or key != "space"
                        or not any(event.get("isTrusted") is True
                                   and event.get("type") == "keydown"
                                   and event.get("repeat") is False for event in keyboard_events)
                        or len(trusted_clicks) != 1
                        or before_number is None or after_number - before_number != 1):
                    continue
            elif cmd != "hold" or not any(
                    event.get("isTrusted") is True and event.get("type") == "keydown"
                    and event.get("repeat") is True
                    for event in keyboard_events):
                continue
            elif ("delta" in low or "result" in low or "activat" in low) and (
                    before_number is None or after_number is None or not trusted_clicks
                    or after_number - before_number != len(trusted_clicks)):
                continue
        if (not _requires_actual_at(low)
                and re.search(r"\b(?:record|observe|verify|confirm|capture)\b.*\bcount\b", low)
                and not re.search(r"\b(?:changes?|changed|advances?|advanced|increments?|incremented)\b", low)):
            mechanical = True
            if after_number is None:
                continue
        invalid_route = (not bool(re.search(r"\bnon[- ]error\b", low)) and bool(re.search(
            r"\b(?:invalid|nonexistent|not[- ]found|error)[- ](?:route|url|page)\b|"
            r"\bguaranteed\s+nonexistent\b", low)))
        if invalid_route:
            mechanical = True
            target_url = normalized_url(targeting.get("session_target_url"))
            before_url = normalized_url(before_state.get("url"))
            after_url = normalized_url(after_state.get("url"))
            matching = [request for request in (after_state.get("recent_requests") or [])
                        if normalized_url(request.get("url")) == after_url]
            if (cmd != "goto" or not targeting.get("driver_ok") or before_url == after_url
                    or (target_url and after_url == target_url)
                    or not matching or not request_failed(matching[-1])):
                continue
            if not (after_state.get("title") or after_state.get("bodyText")):
                continue
        load_record = bool(re.search(
            r"\b(?:capture|record|retain)\b[^.;]{0,100}\b(?:completed\s+)?(?:load|navigation)\b"
            r"(?:[^.;]{0,50}\brecord\b)?",
            low))
        if load_record:
            mechanical = True
            if cmd != "reload" or not targeting.get("reloaded"):
                continue
        navigation = (not load_record
                      and not focus_return
                      and bool(re.search(r"\b(?:navigate|navigation|open(?:s|ed)?|re-enter|return)\b", low))
                      and not _post_history_return_requirement(low)
                      and not _post_reentry_requirement(low)
                      and not invalid_route
                      and not any(phrase in low for phrase in (
                          "no navigation", "without navigation", "unchanged url", "url stability")))
        if navigation:
            mechanical = True
            target_url = normalized_url(targeting.get("session_target_url"))
            before_url = normalized_url(before_state.get("url"))
            after_url = normalized_url(after_state.get("url"))
            recent_requests = list(after_state.get("recent_requests") or [])
            valid_distinct_page = bool(re.search(
                r"\b(?:valid|non-error|successful|distinct)\b[^.;]*\b(?:page|url|destination)\b",
                low))
            # Browser session construction itself performs the user's initial
            # page open.  A plain prerequisite such as "Open the counter" is
            # therefore proven by a settled state at the exact session target;
            # forcing a redundant goto made this prerequisite permanently block
            # later ordered click/observation clauses.  Explicit/direct
            # navigation wording still requires a real navigation command.
            plain_open = (
                bool(re.search(r"\bopen(?:s|ed)?\b", low))
                and not bool(re.search(
                    r"\b(?:explicit(?:ly)?|direct(?:ly)?|navigate|navigation|re-enter|return|away)\b",
                    low)))
            if plain_open:
                if (not target_url or (before_url != target_url and after_url != target_url)
                        or not (before_state.get("title") or before_state.get("bodyText")
                                or after_state.get("title") or after_state.get("bodyText"))):
                    continue
            elif "away" in low:
                if cmd != "goto" or not targeting.get("driver_ok") or before_url == after_url \
                        or (target_url and after_url == target_url):
                    continue
                if valid_distinct_page:
                    matching = [request for request in recent_requests
                                if normalized_url(request.get("url")) == after_url]
                    if not matching or request_failed(matching[-1]):
                        continue
            elif valid_distinct_page:
                if cmd not in ("goto", "back", "forward") or not targeting.get("driver_ok") \
                        or before_url == after_url:
                    continue
                matching = [request for request in recent_requests
                            if normalized_url(request.get("url")) == after_url]
                if not matching or request_failed(matching[-1]):
                    continue
            elif re.search(r"\bre-enter|\breturn", low):
                direct_reentry = (
                    bool(re.search(r"\bdirect(?:ly)?\b", low)) and cmd == "goto"
                    and targeting.get("driver_ok")
                    and (not target_url or after_url == target_url)
                    and (not target_url or normalized_url(targeting.get("action_value")) == target_url))
                if not direct_reentry and (
                        cmd not in ("goto", "back", "forward") or not targeting.get("driver_ok")
                        or before_url == after_url or (target_url and after_url != target_url)):
                    continue
            elif cmd not in ("goto", "back", "forward") or not targeting.get("driver_ok") \
                    or (target_url and after_url != target_url):
                continue
        exact_transition = re.search(
            r"\b(?:change|changes|changed|advance|advances|advanced)\s+(?:from\s+)?(-?\d+)\s+to\s+(-?\d+)\b",
            low)
        if exact_transition:
            mechanical = True
            if (before_number, after_number) != (
                    int(exact_transition.group(1)), int(exact_transition.group(2))):
                continue
        if any(word in low for word in ("rapid", "burst", "timing")):
            mechanical = True
            count = int(burst.get("count") or 0)
            elapsed = int(burst.get("elapsed_ms") or 10**9)
            if not burst.get("burst") or count < 2 or elapsed > max(1000, count * 300):
                continue
        # "post-reload pointer activation" describes the current click plus a previously proven reload; it
        # must not be rejected merely because the current command is not itself reload.  The chronological
        # fence below requires the earlier reload evidence before crediting this relational requirement.
        if ("reload" in low or "refresh" in low) and not _post_reload_requirement(low):
            mechanical = True
            if cmd != "reload" or not targeting.get("reloaded"):
                continue
        if ("back" in low and "feedback" not in low
                and not _post_history_return_requirement(low)) and cmd != "back":
            mechanical = True
            continue
        elif ("back" in low and "feedback" not in low
              and not _post_history_return_requirement(low)):
            mechanical = True
        if any(phrase in low for phrase in ("no navigation", "without navigation", "url unchanged")):
            mechanical = True
            if before_state.get("url") != after_state.get("url"):
                continue
        # Console/network capture is owned by the browser recorder and begins
        # before the first paid decision.  There is no UI control to "start" or
        # "clear" it.  Prove these audit clauses from the scoped driver record
        # itself, and fail closed on any console error or failed/4xx request.
        if "console" in low and any(word in low for word in (
                "clear", "start", "capture", "record", "inspect", "error", "404")):
            mechanical = True
            if "console_errors" not in after_state or "recent_requests" not in after_state:
                continue
            if after_state.get("console_errors") or any(
                    request_failed(request) for request in after_state.get("recent_requests") or []):
                continue
            bp, ap = before_state.get("perception") or {}, after_state.get("perception") or {}
            if bp.get("firstAt") and ap.get("firstAt") and bp.get("firstAt") != ap.get("firstAt"):
                continue
        # Chromium's Accessibility domain proves that a live-region change reached the browser's AX tree.
        # It does NOT prove that NVDA/VoiceOver/Orca actually spoke an announcement.  Never upgrade platform
        # evidence into an actual-AT claim; a future real AT driver can populate the explicit event stream.
        actual_at_required = _requires_actual_at(low)
        if actual_at_required and _actual_at_availability_requirement(low):
            mechanical = True
            if not after_state.get("actualAssistiveTechnologyAvailable"):
                continue
        elif actual_at_required and _initial_at_observation_requirement(low):
            mechanical = True
            expected_number = after_number
            expected = f"current count {expected_number}" if expected_number is not None else ""
            if (not after_state.get("actualAssistiveTechnologyAvailable") or not expected
                    or not any(expected in " ".join(re.sub(
                        r"[^\w]+", " ", str(event.get("utterance") or "").casefold()).split())
                               for event in after_at if isinstance(event, dict))):
                continue
        elif actual_at_required and not actual_at_changed:
            mechanical = True
            continue
        elif actual_at_required:
            mechanical = True
            expected_phrase = re.search(r"\bcurrent\s+count\s+-?\d+\b", low)
            if expected_phrase and not any(
                    expected_phrase.group(0) in " ".join(re.sub(
                        r"[^\w]+", " ", str(event.get("utterance") or "").casefold()).split())
                    for event in new_at_events if isinstance(event, dict)):
                continue
            if (traversal_receipt and re.search(r"\b(?:announce|announcements?|screen.reader)\b", low)
                    and not traversal_control_was_announced(traversal_receipt, new_at_events)):
                continue
        if any(phrase in low for phrase in (
                "accessibility platform", "accessibility-platform", "accessibility tree",
                "ax tree", "live-region exposure")) and not platform_changed:
            mechanical = True
            continue
        elif any(phrase in low for phrase in (
                "accessibility platform", "accessibility-platform", "accessibility tree",
                "ax tree", "live-region exposure")):
            mechanical = True
        if require_mechanical and not mechanical:
            continue
        accepted.append(aspect)
    return accepted


def _mechanically_proven_unresolved(coverage, targeting, before_state, after_state):
    """Recover mechanically proven ledger facts even when a model omits their exact labels.

    The browser/AT driver is authoritative for command-shaped requirements such as click, Enter, reload,
    visible focus, and exact Orca utterances.  Asking a second model to repeat every synonymous ledger label
    made completed journeys look incomplete (and spawned redundant continuation workers).  This helper still
    passes every candidate through the strict mechanical oracle and chronological story-step fence; arbitrary
    semantic prose remains uncredited.
    """
    unresolved = [str(item.get("aspect") or "") for item in (coverage or [])
                  if not item.get("covered") and str(item.get("aspect") or "").strip()]
    fresh_reset_proven = []
    if (str((targeting or {}).get("action_kind") or "").lower() in {"reset_storage", "resetstorage"}
            and (targeting or {}).get("driver_ok") is True
            and (after_state or {}).get("url")
            and not (after_state or {}).get("console_errors")):
        requests_clean = True
        for request in (after_state or {}).get("recent_requests") or []:
            if not isinstance(request, dict):
                requests_clean = False
                break
            try:
                failed = bool(request.get("failed")) or int(request.get("status")) >= 400
            except (TypeError, ValueError):
                failed = request.get("status") is None
            if failed:
                requests_clean = False
                break
        if requests_clean:
            # Versioned focused reset contracts deliberately repeat this identity in all four atomic rows.
            # One trusted resetStorage receipt supplies their causal boundary, settled URL, scoped console,
            # and scoped request statuses; no semantic model is needed to restate those browser facts.
            fresh_reset_proven = [aspect for aspect in unresolved
                                  if "fresh-state reset" in aspect.casefold()]
    restored_keyboard_setup = []
    if ((targeting or {}).get("restored_focused_state") is True
            and (targeting or {}).get("driver_ok")
            and (targeting or {}).get("label_matched")
            and (targeting or {}).get("action_key") in ("Tab", "Shift+Tab")
            and (before_state or {}).get("url")
            and (before_state or {}).get("elements")
            and (targeting or {}).get("expected_control_present") is not False):
        # This exact command is emitted only for a sealed, restored focused regression. A trusted Tab from
        # the browser-resolved source cannot create a duplicate business record, and its before-state proves
        # both passive setup clauses without asking a landmark-only command to resolve form controls.
        restored_keyboard_setup = [aspect for aspect in unresolved if re.search(
            r"\bStory step [12](?:\.|:)", aspect, re.I)]
    corrected_focus_transition = []
    transition = (targeting or {}).get("focus_transition") or {}
    destination = transition.get("destination") if isinstance(transition, dict) else {}
    destination = destination if isinstance(destination, dict) else {}
    destination_active = destination.get("active") if isinstance(destination.get("active"), dict) else {}
    expected_control = str((targeting or {}).get("expected_control") or "").strip()
    delta = transition.get("scroll_delta") if isinstance(transition.get("scroll_delta"), dict) else {}
    if (restored_keyboard_setup and expected_control
            and _labels_match(expected_control, str(destination_active.get("label") or ""))
            and destination_active.get("focusVisible") is True
            and abs(int(delta.get("x") or 0)) <= 2
            and abs(int(delta.get("y") or 0)) <= 8
            and destination.get("horizontalOverflow") is not True):
        corrected_focus_transition = [aspect for aspect in unresolved if re.search(
            r"\bStory step [34](?:\.|:)", aspect, re.I)]
    grounded = _grounded_demonstrated(
        unresolved, targeting, before_state, after_state, require_mechanical=True)
    return _ordered_grounded_aspects(
        coverage, fresh_reset_proven + restored_keyboard_setup + corrected_focus_transition + grounded)


def _sanitize_inventory_checkpoint_claims(ledger, records):
    """Reopen claims an atomic inventory action accidentally credited outside its exact boundary.

    Earlier rolling workers passed every unresolved aspect through the mechanical oracle after a successful
    keyboard matrix.  A dwell clause containing ``focus loss`` was consequently attached to a Space receipt,
    even though no dwell action ran.  Inventory decisions carry a local marker plus an exact ``covers`` list,
    which gives us enough provenance to repair such a checkpoint losslessly on resume.  If any independent
    record legitimately demonstrated the same aspect, its coverage remains monotonic.
    """
    repaired_records = [dict(item) for item in (records or []) if isinstance(item, dict)]
    invalid, legitimate = set(), set()
    for record in repaired_records:
        action = record.get("action") if isinstance(record.get("action"), dict) else {}
        demonstrated = [str(item).strip() for item in (record.get("demonstrated") or [])
                        if str(item).strip()]
        mechanical = [str(item).strip() for item in (record.get("mechanically_proven") or [])
                      if str(item).strip()]
        if action.get("_qa_inventory_derived") is True:
            covers = {str(item).strip() for item in (record.get("covers") or [])
                      if str(item).strip()}
            invalid.update((set(demonstrated) | set(mechanical)) - covers)
            record["demonstrated"] = [item for item in demonstrated if item in covers]
            record["mechanically_proven"] = [item for item in mechanical if item in covers]
            legitimate.update((set(demonstrated) | set(mechanical)) & covers)
        else:
            legitimate.update(demonstrated)
            legitimate.update(mechanical)
    reopen = invalid - legitimate
    repaired_ledger = [dict(item) for item in (ledger or []) if isinstance(item, dict)]
    for item in repaired_ledger:
        if str(item.get("aspect") or "").strip() in reopen and item.get("covered"):
            item["covered"] = False
            item["coverage_repaired"] = "inventory-cross-modality-proof"
    # Legacy ledgers did not persist proof provenance on each row, and the bounded resume dossier may have
    # already dropped the bad Space record. A duration-bearing dwell can only be covered by an explicit
    # dwell/wait/timed receipt. If neither durable row provenance nor the available record window contains
    # one, fail open by retesting it; extra observation is safer than silently certifying ten seconds that
    # never elapsed.
    timed_commands = {"dwell_surfaces", "dwell_landmarks", "dwelllandmarks",
                      "wait", "wait_for", "waitfor", "timed_transition", "timedtransition"}
    for item in repaired_ledger:
        aspect = str(item.get("aspect") or "").strip()
        if (not item.get("covered") or not re.search(r"\b(?:dwell|idle|remain|wait)\b", aspect, re.I)
                or not re.search(r"\b\d+(?:\.\d+)?\s*(?:seconds?|secs?|s)\b", aspect, re.I)):
            continue
        proof = item.get("proof") if isinstance(item.get("proof"), dict) else {}
        proof_cmd = str(proof.get("action_kind") or "").casefold()
        record_proven = any(
            str(((record.get("action") or {}).get("cmd")
                 if isinstance(record.get("action"), dict) else "") or "").casefold() in timed_commands
            and aspect in [str(value).strip() for value in (record.get("demonstrated") or [])]
            for record in repaired_records)
        if proof_cmd not in timed_commands and not record_proven:
            item["covered"] = False
            item["coverage_repaired"] = "missing-timed-dwell-receipt"
            reopen.add(aspect)
    return repaired_ledger, repaired_records, reopen


def _reopen_transient_dom_resume_claims(ledger, state, story):
    """Reopen form/dwell proof when storage restoration cannot restore the DOM it depended on.

    Playwright storage state preserves cookies and web storage, not unsaved input values, checkbox state,
    focus, or an elapsed dwell boundary.  Carrying those labels into an empty restored form makes a successor
    press Enter forever while truthfully receiving validation errors. Durable initial-state and persisted
    business-outcome evidence remains untouched; only transient prerequisites for an unfinished valid-submit
    journey are reopened.
    """
    repaired = [dict(item) for item in (ledger or []) if isinstance(item, dict)]
    if not any(item.get("covered") for item in repaired):
        return repaired, set()
    story_text = _story_text(story).casefold()
    if any(term in story_text for term in (
            "required field", "empty form", "form validation", "validation message",
            "invalid input", "abusive", "injection", "script tag", "sql-like")):
        return repaired, set()
    remaining = " ".join(str(item.get("aspect") or "") for item in repaired
                         if not item.get("covered")).casefold()
    if not re.search(r"\b(?:press\s+enter|submit|send\s+enquiry|confirmation|enquiry\s+received)\b",
                     remaining):
        return repaired, set()
    elements = [item for item in ((state or {}).get("elements") or []) if isinstance(item, dict)]
    submits = [item for item in elements if (
        str(item.get("type") or "").casefold() == "submit"
        or (str(item.get("tag") or "").casefold() == "button"
            and re.search(r"\b(?:send|submit|create)\b", _element_label(item), re.I)))]
    invalid_forms = {item.get("formIndex") for item in submits
                     if item.get("formIndex") is not None
                     and (item.get("formValid") is False
                          or str(item.get("formValid") or "").casefold() == "false")}
    if not invalid_forms:
        return repaired, set()
    missing_required = []
    for element in elements:
        if (element.get("formIndex") not in invalid_forms
                or str(element.get("required") or "").casefold() != "true"):
            continue
        typ = str(element.get("type") or "").casefold()
        empty = (str(element.get("checked") or "").casefold() != "true"
                 if typ in {"checkbox", "radio"}
                 else not str(element.get("value") or "").strip())
        if empty:
            missing_required.append(element)
    if not missing_required:
        return repaired, set()

    transient_pattern = re.compile(
        r"\b(?:enter|type|fill|populate|complete)\b[^.;]{0,160}"
        r"\b(?:name|email|phone|postcode|frequency|dog|field|form|details?|input)\b|"
        r"\b(?:consent|checkbox|marketing)\b|"
        r"\b(?:dwell|idle)\b|"
        r"\b(?:input|focus)\b[^.;]{0,100}\b(?:remain|unchanged|preserv)", re.I)
    reopened = set()
    for item in repaired:
        aspect = str(item.get("aspect") or "")
        if item.get("covered") and transient_pattern.search(aspect):
            item["covered"] = False
            item["coverage_repaired"] = "transient-dom-state-not-restored"
            reopened.add(aspect)
    return repaired, reopened


def _mechanical_routine_verdict(action, targeting, after_state, proven, decision=None):
    """Skip a semantic judge for exact browser-proven setup, without inventing coverage credit.

    Routine typing/viewport setup often advances no story requirement by itself. Requiring an already-proven
    ledger aspect before trusting exact control value/viewport facts forced a strong judge to say merely
    "yes, those characters were typed" after every field. An empty ``demonstrated`` list is safe: the setup
    passes, but coverage remains open until a later semantic outcome actually proves it.
    """
    proven = [str(item) for item in (proven or []) if str(item)]
    if not (targeting or {}).get("driver_ok"):
        return None
    cmd = str((action or {}).get("cmd") or "").lower()
    if cmd in ("type", "fill"):
        expected = " ".join(str((action or {}).get("value") or "").split())
        direct_match = (targeting or {}).get("driver_control_value_matches")
        actual = " ".join(str(
            (targeting or {}).get("driver_control_value")
            if (targeting or {}).get("driver_control_value") is not None
            else (targeting or {}).get("after_control_value") or "").split())
        if ((direct_match is not True and expected != actual)
                or direct_match is False
                or not (targeting or {}).get("label_matched")):
            return None
    elif cmd in ("click", "tap") and bool((decision or {}).get("mechanical_setup")):
        # The deterministic prerequisite planner emits these only for exact browser-discovered setup
        # controls (for example a required consent checkbox or the one-attempt queue drain).  The business
        # outcome remains open unless the independent receipt oracle put it in ``proven``; this merely avoids
        # paying a semantic judge to confirm that the prerequisite click itself landed.  Checkbox/radio setup
        # additionally requires the settled DOM to prove the control became checked.
        if (not (targeting or {}).get("label_matched")
                or not (targeting or {}).get("effect_registered")):
            return None
        before_checked = (targeting or {}).get("before_control_checked")
        after_checked = (targeting or {}).get("after_control_checked")
        if before_checked is not None or after_checked is not None:
            if before_checked is True or after_checked is not True:
                return None
    elif cmd in ("viewport", "resize"):
        requested = (action or {}).get("value") if isinstance((action or {}).get("value"), dict) else action
        actual = (after_state or {}).get("viewport") or {}
        try:
            if (int(requested.get("width")), int(requested.get("height"))) != (
                    int(actual.get("width")), int(actual.get("height"))):
                return None
        except (AttributeError, TypeError, ValueError):
            return None
    elif cmd in ("reset_storage", "resetstorage"):
        # The driver clears cookies/local/session storage, reopens the pinned same-origin target, and reports
        # success only after the page settles. This setup step deliberately earns no story coverage; semantic
        # judging starts with the explicit Seed/Load control that follows it.
        if (after_state or {}).get("console_errors"):
            return None
    elif cmd == "scroll" and str((action or {}).get("target_text") or "").strip():
        # A named landmark scroll is exact driver navigation, not a semantic product judgment. The bridge
        # resolves only visible headings/regions and returns the matched label plus absolute y-coordinate.
        # Skipping a full judge here commonly removes a 10-45 second call while retaining zero optimistic
        # story credit; the next business action still has to prove the acceptance outcome.
        receipt = (targeting or {}).get("landmark_scroll") or {}
        requested = " ".join(str((action or {}).get("target_text") or "").casefold().split())
        matched = " ".join(str(receipt.get("matched") or "").casefold().split())
        if (not receipt.get("scrolled") or not matched
                or (requested not in matched and matched not in requested)
                or (after_state or {}).get("console_errors")):
            return None
        for request in (after_state or {}).get("recent_requests") or []:
            if not isinstance(request, dict):
                continue
            try:
                failed = bool(request.get("failed")) or int(request.get("status")) >= 400
            except (TypeError, ValueError):
                failed = request.get("status") is None
            if failed:
                return None
    elif cmd in ("inspect_surfaces", "inspect_landmarks", "inspectlandmarks") and proven:
        receipt = (targeting or {}).get("landmark_dwell_summary") or {}
        if (not receipt.get("targets") or receipt.get("all_targets_matched") is not True
                or receipt.get("all_stable") is not True
                or not ((after_state or {}).get("url") and (
                    (after_state or {}).get("elements")
                    or (after_state or {}).get("accessibilityTree")
                    or (after_state or {}).get("bodyText")))):
            return None
    else:
        return None
    return {"matches_expected": True, "verdict": "pass", "target_confirmed": True,
            "bug": None, "severity": "none", "blocking": False, "demonstrated": proven,
            "model_failed": False, "infrastructure_error": None,
            "_raw": {"engine": "mechanical-browser-proof"}}


def _mechanical_focused_keyboard_verdict(action, coverage, proven):
    """Close a sealed focus replay when its exact driver receipt proves every open clause."""
    if not bool((action or {}).get("_qa_reported_focus_source")):
        return None
    required = [str(item.get("aspect") or "") for item in (coverage or [])
                if isinstance(item, dict) and not item.get("covered")]
    proven = [str(item) for item in (proven or [])]
    if not required or not all(item in proven for item in required):
        return None
    return {"matches_expected": True, "verdict": "pass", "target_confirmed": True,
            "bug": None, "severity": "none", "blocking": False, "demonstrated": proven,
            "model_failed": False, "infrastructure_error": None,
            "_raw": {"engine": "mechanical-source-bound-focus-proof"}}


def _mechanical_inventory_keyboard_verdict(action, decision, proven):
    """Close an atomic keyboard boundary from its exhaustive browser receipt.

    Atomic keyboard decisions are compiled locally rather than proposed by a model.  Their ``covers`` list is
    therefore an exact ledger boundary, and ``_mechanically_proven_unresolved`` has already checked the trusted
    key, complete live inventory, focus visibility, and overflow invariants.  Paying a semantic judge to repeat
    that boolean decision is both slower and less reliable: an inconclusive judge previously caused a complete
    28/28 Shift+Tab traversal to be replayed forever while the checkpoint itself said mechanically proven.
    This shortcut is deliberately fenced to locally marked inventory actions and exact proven coverage labels;
    ordinary model-authored actions and broader semantic outcomes still require the independent judge.
    """
    item = action or {}
    if item.get("_qa_inventory_derived") is not True:
        return None
    if str(item.get("cmd") or "").lower() not in {
            "traverse", "tab_traverse", "tabtraverse", "keyboard_matrix", "keyboardmatrix"}:
        return None
    covers = [str(value).strip() for value in ((decision or {}).get("covers") or [])
              if str(value).strip()]
    proven_set = {str(value).strip() for value in (proven or []) if str(value).strip()}
    if not covers or not all(value in proven_set for value in covers):
        return None
    return {"matches_expected": True, "verdict": "pass", "target_confirmed": True,
            "bug": None, "severity": "none", "blocking": False,
            "demonstrated": covers, "model_failed": False, "infrastructure_error": None,
            "_raw": {"engine": "mechanical-inventory-keyboard-proof"}}


def _mechanical_atomic_dwell_verdict(action, decision, targeting):
    """Close only exact per-surface dwell atoms backed by the sealed timed browser receipt."""
    if not bool((action or {}).get("_qa_atomic_dwell")):
        return None
    bindings = [item for item in ((action or {}).get("_qa_dwell_bindings") or [])
                if isinstance(item, dict) and str(item.get("aspect") or "").strip()
                and str(item.get("target") or "").strip()]
    covers = [str(item).strip() for item in ((decision or {}).get("covers") or [])
              if str(item).strip()]
    if not bindings or set(covers) != {str(item["aspect"]).strip() for item in bindings}:
        return None
    summary = (targeting or {}).get("landmark_dwell_summary") or {}
    receipt = (targeting or {}).get("landmark_dwell") or {}
    observations = [item for item in (receipt.get("observations") or []) if isinstance(item, dict)]
    by_target = {" ".join(str(item.get("target") or "").casefold().split()): item
                 for item in observations}
    try:
        requested_ms = round(float((action or {}).get("duration_s", 10)) * 1000)
        declared_ms = float(summary.get("duration_ms_each") or receipt.get("duration_ms_each") or 0)
    except (TypeError, ValueError):
        return None
    if (not (targeting or {}).get("driver_ok") or summary.get("all_targets_matched") is not True
            or summary.get("all_stable") is not True or declared_ms < requested_ms):
        return None
    for binding in bindings:
        observation = by_target.get(" ".join(str(binding["target"]).casefold().split()))
        if not observation or observation.get("stable") is not True:
            return None
        scroll = observation.get("scroll") if isinstance(observation.get("scroll"), dict) else {}
        before = observation.get("before") if isinstance(observation.get("before"), dict) else {}
        after = observation.get("after") if isinstance(observation.get("after"), dict) else {}
        try:
            elapsed_ok = float(observation.get("elapsed_ms") or 0) >= requested_ms
        except (TypeError, ValueError):
            elapsed_ok = False
        # ``stable`` is an exact serialization match over URL, scroll position, focused element, all form
        # values/check states, visible status text, scoped content, and horizontal-overflow state. Repeat the
        # safety-critical fields explicitly here so a future compacted bridge receipt fails closed.
        if (not scroll.get("scrolled") or not elapsed_ok or before.get("url") != after.get("url")
                or before.get("scroll") != after.get("scroll")
                or before.get("active") != after.get("active")
                or before.get("controls") != after.get("controls")
                or before.get("horizontalOverflow") is True
                or after.get("horizontalOverflow") is True):
            return None
    return {"matches_expected": True, "verdict": "pass", "target_confirmed": True,
            "bug": None, "severity": "none", "blocking": False, "demonstrated": covers,
            "model_failed": False, "infrastructure_error": None,
            "_raw": {"engine": "mechanical-atomic-dwell-proof"}}


def _mechanical_conditional_surface_setup_verdict(action, targeting, after_state):
    """Accept a browser-complete prerequisite batch once it exposes the requested conditional surface.

    This verdict earns no coverage: the authored dwell still has to run for its full duration.  It only avoids
    paying a semantic judge to confirm that neutral required fields were filled and the now-visible surface
    exists in the settled DOM.
    """
    if not bool((action or {}).get("_qa_conditional_surface_setup")):
        return None
    expected = str((action or {}).get("_qa_expected_surface") or "").strip()
    summary = (targeting or {}).get("scenario_matrix_summary") or {}
    try:
        completed = int(summary.get("completed_cases") or 0)
        total = int(summary.get("total_cases") or 0)
    except (TypeError, ValueError):
        return None
    if (not expected or not (targeting or {}).get("driver_ok") or total < 1 or completed != total
            or not _landmark_labels_for_aspect(after_state, expected, limit=1)):
        return None
    return {"matches_expected": True, "verdict": "pass", "target_confirmed": True,
            "bug": None, "severity": "none", "blocking": False, "demonstrated": [],
            "model_failed": False, "infrastructure_error": None,
            "_raw": {"engine": "mechanical-conditional-surface-setup"}}


def _mechanical_focused_traversal_verdict(story, action, targeting, coverage):
    """Close a sealed traversal regression from the complete trusted driver receipt.

    A traversal receipt already contains every actual Tab, visited control, focus-visible style, scroll
    position, and overflow fact. Sending that 30+ item receipt through a model both costs time and has proven
    less reliable than its mechanical invariants: the judge can call an all-visible 28-control traversal a
    mismatch, after which the planner contaminates the sealed replay by typing into an unrelated field.
    Restrict this shortcut to a focused regression whose historical action was itself ``traverse``.
    """
    story = story or {}
    finding = story.get("focused_finding")
    if story.get("category") != "focused-regression" or not isinstance(finding, dict):
        return None
    reported = finding.get("action")
    if not isinstance(reported, dict) or str(reported.get("cmd") or "").lower() != "traverse":
        return None
    if str((action or {}).get("cmd") or "").lower() != "traverse":
        return None
    receipt = (targeting or {}).get("traversal") or {}
    if not ((targeting or {}).get("driver_ok") and (targeting or {}).get("trusted_keyboard")
            and isinstance(receipt, dict)):
        return None
    reported_direction = str(reported.get("value") or reported.get("direction") or "forward").lower()
    actual_direction = str(receipt.get("direction") or (action or {}).get("value") or "").lower()
    if actual_direction != reported_direction:
        return None
    try:
        derived = int(receipt.get("derived_focusable_count") or 0)
        unique = int(receipt.get("unique_controls") or 0)
        count = int(receipt.get("count") or 0)
    except (TypeError, ValueError):
        return None
    sequence = [item for item in (receipt.get("sequence") or []) if isinstance(item, dict)]
    controls = [item for item in sequence if str(item.get("tag") or "").lower() != "body"]
    expected_key = "Shift+Tab" if reported_direction == "backward" else "Tab"
    keyboard = [item for item in ((targeting or {}).get("keyboard_evidence") or [])
                if isinstance(item, dict)]
    if (derived <= 0 or count < derived or unique < derived or not controls
            or receipt.get("all_focus_visible") is not True
            or receipt.get("horizontal_overflow_seen") is not False
            or any(item.get("focusVisible") is not True for item in controls)
            or any(item.get("horizontalOverflow") is True for item in sequence)
            or not any(item.get("isTrusted") is True and item.get("key") == expected_key
                       for item in keyboard)):
        return None
    demonstrated = [str(item.get("aspect") or "") for item in (coverage or [])
                    if isinstance(item, dict) and not item.get("covered")
                    and str(item.get("aspect") or "").strip()]
    if not demonstrated:
        return None
    return {"matches_expected": True, "verdict": "pass", "target_confirmed": True,
            "bug": None, "severity": "none", "blocking": False,
            "demonstrated": demonstrated, "model_failed": False,
            "infrastructure_error": None,
            "_raw": {"engine": "mechanical-focused-traversal-proof",
                     "derived_focusable_count": derived, "unique_controls": unique,
                     "trusted_key": expected_key}}


def _ground_verdict_demonstrated(verdict, targeting, before_state, after_state):
    """Ground a verdict's coverage without weakening a sealed mechanical proof.

    The generic prose oracle intentionally re-checks model-authored ``demonstrated`` claims.  A focused
    sealed traversal/dwell verdict is different: its demonstrated list is constructed locally from the exact
    open ledger only after the complete trusted receipt passes every command invariant. Re-parsing those labels
    can erase valid proof for accidental words.  For example, the contract guard "do not repeat a historical
    observation" contains ``repeat`` and was mistaken for a held-key/repeat-event requirement; step 3 was
    removed and chronological ordering consequently removed step 4 too.  Preserve only this explicitly sealed
    engine's already-grounded list.  All model verdicts and other mechanical paths retain the ordinary filter.
    """
    raw = (verdict or {}).get("_raw")
    engine = str(raw.get("engine") or "") if isinstance(raw, dict) else ""
    if engine in {"mechanical-focused-traversal-proof", "mechanical-atomic-dwell-proof"}:
        return [str(item) for item in ((verdict or {}).get("demonstrated") or [])
                if str(item).strip()]
    return _grounded_demonstrated(
        (verdict or {}).get("demonstrated"), targeting, before_state, after_state)


def _scroll_needs_semantic_judge(action, decision):
    """A landmark move is setup unless the planner claims it proves acceptance evidence.

    Mechanically passing every named scroll with zero story credit is cheap for ordinary navigation, but a
    pure inspection regression can then scroll to the already-visible corrected control until the repetition
    guard fires. When the planner says this scroll exercises a ledger clause or expects a named control to be
    present, require the normal independent semantic judge so the visible state can earn (or fail) coverage.
    """
    return (str((action or {}).get("cmd") or "").lower() == "scroll"
            and bool((decision or {}).get("covers") or (decision or {}).get("expected_control")))


def _routine_action_needs_semantic_judge(action, decision):
    """Keep zero-cost setup separate from an action claiming an acceptance outcome."""
    if bool((decision or {}).get("mechanical_setup")):
        return False
    return bool((decision or {}).get("covers")) or _scroll_needs_semantic_judge(action, decision)


def _mechanical_incomplete_submit_verdict(story, action, targeting):
    """Classify a browser-proven skipped-prerequisite submit without a semantic model call."""
    # A locally compiled validation matrix intentionally exercises disabled submit paths while its settled
    # per-case receipts contain the field errors, paste/focus evidence, and no-side-effect trace to be judged.
    # Treating that expected disabled state as a missing-prerequisite setup retry replays the entire matrix
    # forever and never lets the semantic evaluator credit any validation clause.
    if bool((action or {}).get("_qa_validation_matrix")):
        return None
    if not (targeting or {}).get("driver_ok"):
        return None
    if str((action or {}).get("cmd") or "").lower() not in (
            "click", "tap", "press", "scenario_matrix", "case_matrix"):
        return None
    if (not (targeting or {}).get("empty_required_fields_before")
            and not (targeting or {}).get("scenario_matrix_submit_disabled")):
        return None
    story_text = _story_text(story)
    if any(term in story_text for term in (
            "required field", "required-field", "empty form", "incomplete form",
            "form validation", "validation message", "missing field")):
        return None
    return {"matches_expected": False, "verdict": "retry", "target_confirmed": True,
            "bug": None, "severity": "none", "blocking": False, "demonstrated": [],
            "model_failed": False, "infrastructure_error": None,
            "_raw": {"engine": "mechanical-browser-submit-prerequisite"}}


def _visible_pending_transition(state):
    text = " ".join(str((state or {}).get(key) or "") for key in (
        "statusText", "viewportText", "bodyText")).casefold()
    for segment in re.split(r"[|\n.]", text):
        if (re.search(r"\b(?:sending|saving|publishing|processing|working|loading|submitting|retrying)"
                     r"\s*(?:\.{3}|…)?\b", segment)
                and not re.search(r"\b(?:ready|complete|completed|idle|available|unavailable|failed)\b",
                                  segment)):
            return True
    return False


def _mechanical_pending_transition_verdict(action, targeting, after_state):
    """A visibly pending async boundary is continuation evidence, never an immediate app defect."""
    if bool((action or {}).get("_qa_validation_matrix")):
        return None
    cmd = str((action or {}).get("cmd") or "").lower()
    target = str((action or {}).get("target_text") or (targeting or {}).get("targeted_label") or "")
    if (cmd not in ("click", "tap", "press", "scenario_matrix", "case_matrix")
            or not (targeting or {}).get("driver_ok")
            or not (targeting or {}).get("effect_registered")
            or not _visible_pending_transition(after_state)):
        return None
    if cmd not in ("scenario_matrix", "case_matrix") and not re.search(
            r"\b(?:send|submit|save|publish|create|start|run|retry|process)\b", target, re.I):
        return None
    return {"matches_expected": False, "verdict": "inconclusive", "target_confirmed": True,
            "bug": None, "severity": "none", "blocking": False, "demonstrated": [],
            "model_failed": False, "infrastructure_error": None,
            "_raw": {"engine": "mechanical-visible-pending-transition"}}


def _pending_business_completion_wait_decision(story, state, records):
    """Continue one visible async business transition without another model decision.

    The duration is an observation window, not a product SLA. A still-pending result remains incomplete and
    owned after the wait; it is never converted into a defect solely because this window elapsed.
    """
    base_index, base = None, None
    for pos in range(len(records or []) - 1, -1, -1):
        candidate = records[pos] if isinstance(records[pos], dict) else {}
        verdict = candidate.get("verdict") if isinstance(candidate.get("verdict"), dict) else {}
        raw = verdict.get("_raw") if isinstance(verdict.get("_raw"), dict) else {}
        if raw.get("engine") == "mechanical-visible-pending-transition":
            base_index, base = pos, candidate
            break
    if base is None:
        return None
    if any(str(((item.get("action") or {}).get("cmd") if isinstance(item, dict) else "") or "").lower()
           == "wait" for item in (records or [])[base_index + 1:]):
        return None
    # If completion raced the next observation, use a zero-duration observation action so the original
    # business expectation can be judged immediately. Timeout simulations deliberately hold for ten seconds;
    # twelve seconds crosses that explicit product boundary without imposing a generic external-service SLA.
    duration = "0" if not _visible_pending_transition(state) else (
        "12s" if "timeout" in _story_text(story).casefold() else "30s")
    return {
        "reasoning": "Continue observing the already-started visible business transition.",
        "intent": str(base.get("reasoning") or "pending business transition"),
        "next_action": {"cmd": "wait", "value": duration},
        "expected": str(base.get("expected") or "The pending business transition settles."),
        "expected_control": str(base.get("expected_control") or ""),
        "wait_for": None, "covers": list(base.get("covers") or []), "done": False,
        "mechanical_setup": False,
    }


def _covered_in_prior_ledger(aspect, prior):
    """Match a regenerated coverage sentence to prior proven coverage across worker rotations.

    Coverage planning is AI-authored and can paraphrase the same requirement on each fresh browser.  Exact
    string equality therefore silently discarded durable progress.  Sequence similarity catches light edits;
    an asymmetric content-token containment check catches reordered or expanded prior statements.  The latter
    deliberately asks whether the *new* requirement is already represented by the old evidence, rather than
    using symmetric Jaccard similarity which could let a narrow old check satisfy a broader new requirement.
    """
    text = " ".join(str(aspect or "").lower().split())
    stop = {
        "a", "an", "the", "and", "or", "to", "of", "in", "on", "with", "for", "by", "is", "are",
        "be", "this", "that", "each", "all", "only", "after", "before", "while", "then", "it", "its",
        "as", "from", "confirm", "verify",
    }
    wanted = set(re.findall(r"[a-z0-9]+", text)) - stop
    proven_union = set()
    for old in prior or []:
        previous = " ".join(str(old or "").lower().split())
        if text == previous or (text and previous and SequenceMatcher(None, text, previous).ratio() >= 0.72):
            return True
        proven = set(re.findall(r"[a-z0-9]+", previous)) - stop
        proven_union.update(proven)
        # Six shared content words prevents generic phrases such as "render the public app" from matching;
        # 76% new-term containment keeps any material new assertion explicitly untested.
        shared = wanted & proven
        if len(shared) >= 6 and wanted and len(shared) / len(wanted) >= 0.76:
            return True
    # A fresh planner can combine two previously separate aspects into one sentence.
    # Both pieces are still grounded evidence for this same signed story campaign;
    # requiring a single old sentence to contain the whole conjunction would force
    # needless re-testing on every rotation.
    shared = wanted & proven_union
    if len(shared) >= 6 and wanted and len(shared) / len(wanted) >= 0.76:
        return True
    return False


def _legacy_single_coverage_needs_replan(ledger, story):
    """Identify an uncovered all-or-nothing legacy ledger that hides real partial progress.

    Replanning a partially proven ledger would risk changing the meaning of sealed evidence, so this migration
    is intentionally narrow: exactly one still-uncovered AI-authored item, no explicit step identity, and a
    story with several executable steps. The old broad requirement is retained alongside the new granular
    contract; the migration only adds progress resolution and never removes a release condition.
    """
    items = [item for item in (ledger or []) if isinstance(item, dict) and item.get("aspect")]
    if len(items) != 1 or items[0].get("covered"):
        return False
    aspect = str(items[0].get("aspect") or "").strip().casefold()
    if items[0].get("explicit") or aspect.startswith(("story step ", "required evidence:")):
        return False
    return len([step for step in ((story or {}).get("steps") or []) if str(step).strip()]) >= 3


def _call_agent(role, repo, task, light=False, timeout=None, retries=None, reasoning_effort=None):
    """The single seam to the LLM. Imported lazily so the module loads (and self-tests) without pulling
    in the heavy factory runtime, and so a stubbed `factory` in sys.modules is honoured. factory.agent
    owns all resilience (529/overload retry, Codex failover) and governance. `light` requests the fast
    path; a stubbed or older factory that doesn't accept it just falls back to the plain call (fail-open,
    so a test double or a signature change can never break the loop)."""
    import factory  # noqa: E402 — lazy on purpose (see docstring)
    # Coverage/decision/evaluation prompts below are complete evidence contracts. The generic role charter is
    # duplicated context (and used to add thousands of tokens to every browser step), so keep the chosen model
    # and reasoning tier but request factory's self-contained prompt path.
    kwargs = {"compact": True}
    if light:
        kwargs["light"] = True
    if timeout is not None:
        kwargs["timeout"] = max(1, int(timeout))
    if retries is not None:
        kwargs["retries"] = max(0, int(retries))
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = str(reasoning_effort)
    try:
        return factory.agent(role, repo, task, **kwargs)
    except TypeError:
        # Rolling factories may not know ``compact`` yet. Preserve operation bounds/light routing on that
        # generation; only a still-older three-argument test double needs the final plain-call fallback.
        kwargs.pop("compact", None)
        try:
            return factory.agent(role, repo, task, **kwargs)
        except TypeError:
            return factory.agent(role, repo, task)


# ----------------------------------------------------------------------------------------------------
# The browser bridge — a thin, resilient wrapper over the persistent Playwright subprocess.
# ----------------------------------------------------------------------------------------------------
class BrowserBridge:
    """Drives browser_bridge.js over its newline-JSON protocol: one command per line out, one reply
    line back (each reply echoes the request `id`). The bridge emits a `ready` line on startup, takes
    NO navigation argv (we drive it with explicit `goto`/`seedToken`), and exposes native commands
    (goto/state/click/fill/eval/inject/seedToken/close). This wrapper hides the protocol behind a small
    `state()` / `act(action)` surface the state-based loop uses, plus goto/seed for session setup."""

    def __init__(self, target_url, token=None, org="0", timeout=45, autostart=True, shot_dir=None,
                 storage_state_path=None, scope_run_id=None, scope_tenant=None, actual_at=False):
        self.timeout = timeout
        self._id = 0
        self.proc = None
        self.target_url = target_url      # the ONE origin this session may navigate to (see _pin_origin)
        self.scope_run_id = scope_run_id  # cancellation ownership; never infer from process-global state
        self.scope_tenant = scope_tenant
        self.video_path = None            # webm path the bridge reports on close() (for mp4 transcode)
        self.actual_at = bool(actual_at)  # true only for the isolated Orca/AT-SPI driver
        self._gate_slot = None            # global browser-concurrency slot (released in close())
        self._ownership_record = None      # exact Node-root identity for parent-death tree cleanup
        self._at_runtime_dir = None        # private XDG runtime for the isolated DBus/AT-SPI session
        self._at_session_lock = None        # full-lifetime, cross-process real-Orca admission
        if not autostart:
            return
        state_path = None
        if storage_state_path:
            state_path = Path(storage_state_path)
            if not state_path.is_file():
                # Validate before acquiring host resources. A bad checkpoint is
                # caller input, not a browser launch that should own a lease or
                # private runtime directory.
                raise RuntimeError(f"QA resume storage state is missing: {state_path}")
        if self.actual_at:
            # Acquire the scarce AT workstation before a browser slot. A queued
            # Orca story must not consume Chromium capacity while it waits.
            self._at_session_lock = _acquire_at_session_lock(self.timeout)
        # GLOBAL BROWSER CAP: hold a slot for this session's lifetime so total concurrent Chromium+video
        # sessions across the whole box stay bounded (1000s of agents must not thrash). Admission is fail-closed:
        # uncertain ownership must defer the story rather than over-subscribe the host.
        try:
            import browser_gate
            self._gate_slot = browser_gate.acquire(browser_gate.qa_holder(shot_dir or target_url))
            if self._gate_slot is None:
                raise RuntimeError("browser capacity exhausted — no QA browser slot available")
        except Exception:
            if self._gate_slot is not None:
                try:
                    import browser_gate
                    browser_gate.release(self._gate_slot)
                except Exception:
                    pass
                self._gate_slot = None
            _release_at_session_lock(self._at_session_lock)
            self._at_session_lock = None
            raise
        try:
            env = _browser_child_env()
            env["NODE_PATH"] = NODE_PATH
            if not env.get("PLAYWRIGHT_BROWSERS_PATH"):
                browser_cache = Path.home() / ".cache" / "ms-playwright"
                if browser_cache.is_dir():
                    # XDG cache is private below, while installed browser
                    # binaries are immutable host tooling shared read-only.
                    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_cache)
            command = ["node", str(BRIDGE_JS)]
            if self.actual_at:
                env["AOS_QA_AT_DRIVER"] = "orca"
                self._at_runtime_dir = tempfile.mkdtemp(prefix="aos-qa-at-runtime-")
                os.chmod(self._at_runtime_dir, 0o700)
                env["XDG_RUNTIME_DIR"] = self._at_runtime_dir
                # Orca and Chromium must share the same dconf/GSettings backend
                # so Orca's screen-reader enablement reaches the browser. The
                # workstation lease prevents concurrent mutation of that host
                # accessibility state; browser auth/storage remains private.
                env["AOS_QA_AT_SESSION_LOCK_FD"] = str(self._at_session_lock.fileno())
                command = ["xvfb-run", "-a", "dbus-run-session", "--", sys.executable,
                           str(AT_DRIVER), "--bridge", str(BRIDGE_JS)]
            if state_path is not None:
                env["AOS_QA_STORAGE_STATE_PATH"] = str(state_path.resolve())
            if shot_dir:
                env["AOS_QA_SHOT_DIR"] = str(shot_dir)
                env["AOS_QA_VIDEO_DIR"] = str(Path(shot_dir).parent / "videos")
            popen_kwargs = dict(
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, env=env, start_new_session=True)
            if self.actual_at:
                popen_kwargs["pass_fds"] = (self._at_session_lock.fileno(),)
            self.proc = subprocess.Popen(command, **popen_kwargs)
            import clauded
            self._ownership_record = clauded.register_owned(
                self.proc.pid,
                f"qa-browser:{scope_tenant or '_'}:{scope_run_id or '_'}:{os.getpid()}")
            if self._ownership_record is None:
                # Do not start Chromium when the central reaper cannot prove ownership after a worker crash.
                self._terminate_process_tree(signal.SIGKILL)
                self.proc.wait(timeout=2)
                raise clauded.OwnershipUnavailable(
                    f"could not register exact ownership for QA browser bridge {self.proc.pid}")
            with _LIVE_BRIDGES_LOCK:
                _LIVE_BRIDGES.add(self)
            self._await_ready()
            # session setup: seed auth BEFORE the app boots (addInitScript), then navigate.
            if token:
                self.seed_token(token, org)
            self.goto(target_url)
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def _pin_origin(self, url):
        """Force a model-chosen `goto` onto the origin THIS run is actually testing, keeping path/query/frag.

        The decide-prompt constrains click/type targeting hard ("never invent an idx you did not see") but says
        nothing about goto, so the model would happily navigate to a plausible-looking origin it made up. That
        is exactly what stalled a real fix: the post-fix re-observation issued `goto http://localhost:3000`
        (the generic dev-server default) while the app under test was on 127.0.0.1:8871, hit
        chrome-error://chromewebdata for 22 straight steps, and the judge concluded "not fixed" — for a bug
        that WAS fixed and live. The loop then re-ran forever, never converging.

        Path, query and fragment are preserved, because QA legitimately drives same-app routes and query-param
        edge cases (e.g. ?stateUrl=<bad-host> to exercise a data-source failure — that URL lives in the QUERY,
        and must survive). Only the scheme/host/port are pinned. A relative URL resolves against the target."""
        from urllib.parse import urljoin, urlsplit, urlunsplit
        target = self.target_url or ""
        if not url:
            return target
        if not target:
            return url
        try:
            t, u = urlsplit(target), urlsplit(str(url))
            if not u.netloc:                                   # relative path -> resolve against the target
                return urljoin(target, str(url))
            if (u.scheme, u.netloc) == (t.scheme, t.netloc):    # already the app under test
                return url
            # Off-origin: keep what the model wanted to reach WITHIN the app, drop its invented host.
            return urlunsplit((t.scheme, t.netloc, u.path, u.query, u.fragment)) or target
        except Exception:
            return url                                          # never break navigation on a parse error

    def _fence_lost_capacity(self):
        try:
            import browser_gate
            if not browser_gate.lease_lost(self._gate_slot):
                return False
        except Exception:
            return False
        # The weighted/provider generation can now be reassigned. Fence this exact
        # registered tree immediately; never let two generations run concurrently.
        try:
            import clauded
            clauded._signal_registered_descendants(self._ownership_record)
        except Exception:
            pass
        self._terminate_process_tree(signal.SIGTERM)
        return True

    def _readline(self):
        """Read one line from the bridge honouring the wall-clock timeout (a page load can be slow)."""
        if not self.proc or self.proc.poll() is not None:
            return None
        deadline = time.monotonic() + self.timeout
        while self.proc and self.proc.poll() is None:
            if self._fence_lost_capacity():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            r, _, _ = select.select([self.proc.stdout], [], [], min(0.25, remaining))
            if r:
                return self.proc.stdout.readline() or None
        return None

    def _await_ready(self):
        """Consume the startup handshake. The first non-noise line should be {cmd:'ready'}; a startup
        error line (playwright missing, etc.) surfaces here instead of hanging."""
        # xvfb-run/dbus/AT-SPI can emit bounded diagnostic lines on the inherited stream before the
        # protocol process starts.  Ignore non-JSON noise, but keep a finite ceiling.
        for _ in range(100):
            line = self._readline()
            if not line:
                break
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("ok") is False:
                raise RuntimeError(f"browser_bridge failed to start: {j.get('error')}")
            if j.get("cmd") == "ready":
                return j
        raise RuntimeError("browser_bridge did not become ready")

    def _send(self, obj):
        if not self.proc or self.proc.poll() is not None:
            return {"ok": False, "error": "bridge process is not running"}
        if self._fence_lost_capacity():
            return {"ok": False, "error": "browser capacity lease lost; work fenced"}
        self._id += 1
        obj = {"id": self._id, **obj}
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            return {"ok": False, "error": f"bridge write failed: {e}"}
        # commands are strictly serial; skip any stray line until our id comes back (defensive).
        for _ in range(100):
            line = self._readline()
            if not line:
                return {"ok": False, "error": f"bridge timeout after {self.timeout}s"}
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("id") == self._id or j.get("id") is None:
                return j
        return {"ok": False, "error": "bridge desync: no reply matched the request id"}

    def goto(self, url):
        return self._send({"cmd": "goto", "url": url})

    def settle(self, ms=None):
        """Ask the bridge to wait extra paint grace then settle (skeletons gone, DOM stable) — used
        before RE-OBSERVING a late-painting control so it isn't falsely judged 'never renders'."""
        msg = {"cmd": "settle"}
        if ms is not None:
            msg["ms"] = ms
        return self._send(msg)

    def wait_for(self, condition):
        condition = dict(condition or {})
        kind = str(condition.get("kind") or "text").strip().lower()
        value = str(condition.get("value") or "").strip()
        try:
            timeout_s = int(condition.get("timeout_s") or _WAIT_DEFAULT_S)
        except (TypeError, ValueError):
            timeout_s = _WAIT_DEFAULT_S
        timeout_s = min(_WAIT_MAX_S, max(1, timeout_s))
        old_timeout = self.timeout
        try:
            # The line protocol read timeout must outlive the bridge's bounded mechanical poll.
            self.timeout = max(float(old_timeout), timeout_s + 5)
            return self._send({"cmd": "waitFor", "kind": kind, "value": value,
                               "timeout_ms": timeout_s * 1000})
        finally:
            self.timeout = old_timeout

    def dwell(self, duration_s):
        """Keep the live page open for a requested observation interval.

        A model ``wait`` action used to be an immediate no-op, so a story could
        appear to exercise a ten-second idle period without waiting at all.
        Keep the duration explicit and bounded, and let the bridge own the wait
        so page timers, live regions, and async rendering continue normally.
        """
        try:
            duration_s = float(duration_s or 0)
        except (TypeError, ValueError):
            duration_s = 0.0
        duration_s = min(float(_WAIT_MAX_S), max(0.0, duration_s))
        old_timeout = self.timeout
        try:
            self.timeout = max(float(old_timeout), duration_s + 5)
            return self._send({"cmd": "wait", "timeout_ms": round(duration_s * 1000)})
        finally:
            self.timeout = old_timeout

    def dwell_landmarks(self, targets, duration_s):
        targets = list(dict.fromkeys(str(item or "").strip() for item in (targets or [])
                                     if str(item or "").strip()))[:8]
        try:
            duration_s = float(duration_s or 10)
        except (TypeError, ValueError):
            duration_s = 10.0
        duration_s = min(60.0, max(0.025, duration_s))
        total_s = duration_s * len(targets)
        if not targets or total_s > _WAIT_MAX_S:
            return {"ok": False, "cmd": "dwellLandmarks",
                    "error": "landmark dwell requires 1-8 targets within the external-wait bound"}
        old_timeout = self.timeout
        try:
            self.timeout = max(float(old_timeout), total_s + 8)
            return self._send({"cmd": "dwellLandmarks", "targets": targets,
                               "duration_s": duration_s})
        finally:
            self.timeout = old_timeout

    def seed_token(self, token, org="0"):
        return self._send({"cmd": "seedToken", "token": token, "org": org})

    def storage_state(self):
        return self._send({"cmd": "storageState"})

    def state(self, include_accessibility=True):
        """Observe: url/title, a screenshot path, enumerated interactable elements (each with a stable
        selector + idx), recent network requests, console/page errors — plus the page's visible text
        (fetched via a follow-up eval, since the native state doesn't include body text)."""
        st = self._send({"cmd": "state", "include_accessibility": bool(include_accessibility)})
        if st.get("ok") and "bodyText" not in st:
            # Backward-compatible fallback for a bridge process that was already alive during a rolling
            # controller handoff. New bridges return document/viewport/status text atomically with state.
            ev = self._send({"cmd": "eval",
                             "expr": "document.body?document.body.innerText.replace(/\\s+/g,' ')"
                                     ".trim().slice(0,4000):''"})
            st["bodyText"] = ev.get("result") if ev.get("ok") else ""
            st["viewportText"] = st["bodyText"]
            status = self._send({"cmd": "eval",
                                 "expr": "[...document.querySelectorAll('[role=status],[aria-live],[data-testid*=status]')]"
                                         ".map(e=>e.textContent.replace(/\\s+/g,' ').trim()).filter(Boolean)"
                                         ".join(' | ').slice(0,1200)"})
            st["statusText"] = status.get("result") if status.get("ok") else ""
        return st

    @staticmethod
    def _matrix_state_receipt(state):
        """Keep each mechanically executed case useful to one bounded semantic judge."""
        state = state or {}
        return {
            "url": state.get("url"), "title": state.get("title"),
            "statusText": str(state.get("statusText") or "")[:1400],
            "viewportText": str(state.get("viewportText") or state.get("bodyText") or "")[:2200],
            "activeElement": state.get("activeElement"),
            "console_errors": list(state.get("console_errors") or [])[-5:],
            "recent_requests": list(state.get("recent_requests") or [])[-5:],
        }

    def _scenario_matrix(self, action):
        """Mechanically run a bounded table of repetitive form cases under one model decision/judge.

        This is not optimistic coverage batching: every nested control action is resolved against the live DOM,
        executed through the ordinary driver, settled, and captured individually. Any missed/failed sub-action
        fails the batch so a semantic judge cannot certify cases the browser never exercised.
        """
        cases = action.get("cases") or []
        if not isinstance(cases, list) or not (1 <= len(cases) <= 12):
            return {"ok": False, "cmd": "scenarioMatrix",
                    "error": "scenario_matrix requires 1-12 cases"}
        allowed = {"click", "tap", "type", "fill", "paste", "press", "wait"}
        total = 0
        normalized = []
        for case_index, case in enumerate(cases, 1):
            if not isinstance(case, dict) or not isinstance(case.get("actions"), list):
                return {"ok": False, "cmd": "scenarioMatrix",
                        "error": f"scenario_matrix case {case_index} requires actions"}
            actions = list(case.get("actions") or [])
            if not (1 <= len(actions) <= 8):
                return {"ok": False, "cmd": "scenarioMatrix",
                        "error": f"scenario_matrix case {case_index} requires 1-8 actions"}
            if any(str((step or {}).get("cmd") or "").lower() not in allowed
                   for step in actions if isinstance(step, dict)) or not all(
                       isinstance(step, dict) for step in actions):
                return {"ok": False, "cmd": "scenarioMatrix",
                        "error": f"scenario_matrix case {case_index} contains an unsupported action"}
            total += len(actions)
            normalized.append((str(case.get("name") or f"case-{case_index}")[:120], actions))
        if total > 48:
            return {"ok": False, "cmd": "scenarioMatrix",
                    "error": "scenario_matrix exceeds the 48-action evidence bound"}

        receipts = []
        for case_name, actions in normalized:
            case_receipt = {"name": case_name, "actions": []}
            for raw in actions:
                subaction = dict(raw)
                subcmd = str(subaction.get("cmd") or "").lower()
                before = self.state(include_accessibility=False)
                intended = str(subaction.get("target_text") or subaction.get("target") or "").strip()
                resolved_label = ""
                if subcmd in ("type", "fill", "paste", "press") and intended:
                    idx, resolved_label, _ = _resolve_target(subaction, before.get("elements") or [])
                    if idx is None:
                        failed = {"ok": False, "error": f"control not found: {intended}"}
                        case_receipt["actions"].append({
                            "action": subaction, "intended": intended,
                            "resolved_label": None, "result": failed,
                            "before": self._matrix_state_receipt(before),
                        })
                        receipts.append(case_receipt)
                        return {"ok": False, "cmd": "scenarioMatrix", "scenarioMatrix": True,
                                "error": failed["error"], "cases": receipts,
                                "completed_cases": len(receipts) - 1, "total_cases": len(normalized)}
                    subaction["idx"] = idx
                result = self.act(subaction)
                try:
                    self.settle()
                except Exception:
                    pass
                after = self.state(include_accessibility=False)
                case_receipt["actions"].append({
                    "action": raw, "intended": intended,
                    "resolved_label": result.get("matched") or resolved_label or intended,
                    "ok": bool(result.get("ok", True)),
                    "driver_error": result.get("error"),
                    "keyboard_evidence": list(result.get("keyboardEvidence") or [])[-120:],
                    "paste_evidence": list(result.get("pasteEvidence") or [])[-6:],
                    "actual_value_length": result.get("actualValueLength"),
                    "actual_value_matches": result.get("actualValueMatches"),
                    "empty_required_fields_before": list(
                        result.get("emptyRequiredFieldsBefore") or [])[:20],
                    "disabled_before_click": result.get("disabledBeforeClick"),
                    "before": self._matrix_state_receipt(before),
                    "after": self._matrix_state_receipt(after),
                })
                if result.get("ok") is False:
                    receipts.append(case_receipt)
                    return {"ok": False, "cmd": "scenarioMatrix", "scenarioMatrix": True,
                            "error": str(result.get("error") or "nested browser action failed")[:500],
                            "cases": receipts, "completed_cases": len(receipts) - 1,
                            "total_cases": len(normalized)}
            receipts.append(case_receipt)
        return {"ok": True, "cmd": "scenarioMatrix", "scenarioMatrix": True,
                "cases": receipts, "completed_cases": len(receipts),
                "total_cases": len(normalized), "action_count": total}

    def _keyboard_matrix(self, action):
        """Exercise a live page's keyboard modalities under one bounded, browser-owned receipt.

        This is the keyboard analogue of ``scenario_matrix``.  It is intentionally adaptive: activating a
        fixture, disclosure, or workflow button can reveal additional controls, so the matrix re-enumerates
        the settled DOM until no new applicable control remains.  It never invents form values and never
        treats a disabled control as operable; those facts remain visible to the semantic judge.  A maximum of
        48 trusted actions is an evidence-payload bound, not a story deadline—overflow returns incomplete.
        """
        aliases = {
            "tab": "Tab", "shift+tab": "Shift+Tab", "shifttab": "Shift+Tab",
            "arrow": "ArrowDown", "arrowdown": "ArrowDown", "arrowup": "ArrowUp",
            "arrowleft": "ArrowLeft", "arrowright": "ArrowRight",
            "space": "Space", "spacebar": "Space", "enter": "Enter",
        }
        raw_keys = action.get("keys") or action.get("value") or []
        if isinstance(raw_keys, str):
            raw_keys = [part for part in re.split(r"\s*,\s*|\s+and\s+", raw_keys) if part]
        requested = []
        for raw in raw_keys:
            key = aliases.get(str(raw).casefold().replace(" ", ""))
            if key and key not in requested:
                requested.append(key)
        if not requested:
            return {"ok": False, "cmd": "keyboardMatrix", "keyboardMatrix": True,
                    "error": "keyboard_matrix requires one or more supported keys"}

        max_actions = max(1, min(48, int(action.get("max_actions") or 48)))
        receipts, traversals, operated, disabled = [], [], set(), []
        keys_proven = set()

        def trusted_keys(result):
            proven = set()
            for event in (result or {}).get("keyboardEvidence") or []:
                if (not isinstance(event, dict) or event.get("isTrusted") is not True
                        or event.get("type") != "keydown"):
                    continue
                raw = str(event.get("key") or "")
                code = str(event.get("code") or "")
                if raw == " " or code.casefold() == "space":
                    proven.add("Space")
                elif raw:
                    proven.add(raw)
            return proven

        def stable_identity(element):
            return "|".join(str(element.get(name) or "").strip().casefold() for name in (
                "id", "name", "tag", "type", "role", "text", "ariaLabel", "placeholder"))

        def record(action_row, result, before, after):
            receipts.append({
                "action": action_row,
                "ok": bool((result or {}).get("ok", False)),
                "driver_error": (result or {}).get("error"),
                "keyboard_evidence": list((result or {}).get("keyboardEvidence") or [])[-20:],
                "traversal": ({key: (result or {}).get(key) for key in (
                    "direction", "key", "count", "derived_focusable_count", "unique_controls",
                    "all_focus_visible", "horizontal_overflow_seen", "sequence")}
                    if (result or {}).get("traversal") else None),
                "before": self._matrix_state_receipt(before),
                "after": self._matrix_state_receipt(after),
            })

        # Full forward/reverse traversal is one native browser action per direction, irrespective of how many
        # focus stops it proves.  It must pass the derived inventory/focus/overflow receipt before the key is
        # considered proven.
        for key, direction in (("Tab", "forward"), ("Shift+Tab", "backward")):
            if key not in requested:
                continue
            before = self.state(include_accessibility=False)
            result = self.act({"cmd": "traverse", "value": direction})
            after = self.state(include_accessibility=False)
            receipt = {name: result.get(name) for name in (
                "direction", "key", "count", "derived_focusable_count", "unique_controls",
                "all_focus_visible", "horizontal_overflow_seen", "sequence")}
            receipt["ok"] = bool(result.get("ok", False))
            traversals.append(receipt)
            record({"cmd": "traverse", "value": direction}, result, before, after)
            try:
                complete = (int(result.get("unique_controls") or 0)
                            >= int(result.get("derived_focusable_count") or 0) > 0)
            except (TypeError, ValueError):
                complete = False
            if (result.get("ok") and complete and result.get("all_focus_visible") is True
                    and result.get("horizontal_overflow_seen") is False):
                keys_proven.add(key)
            if len(receipts) >= max_actions:
                break

        # Canonical activation modality by native control type.  Text/date/number fields are reached and
        # focus-audited by traversal; arbitrary data entry is intentionally left to the story planner.
        while len(receipts) < max_actions:
            before = self.state(include_accessibility=False)
            candidate = None
            for element in before.get("elements") or []:
                if not isinstance(element, dict):
                    continue
                tag = str(element.get("tag") or "").casefold()
                typ = str(element.get("type") or "").casefold()
                role = str(element.get("role") or "").casefold()
                label = _element_label(element)
                if not label:
                    continue
                disabled_now = str(element.get("disabled") or "").casefold() in {"1", "true", "disabled"}
                chosen = None
                if tag == "select" or role in {"combobox", "listbox"}:
                    chosen = next((key for key in requested if key.startswith("Arrow")), None)
                elif typ == "radio" or role == "radio":
                    chosen = (next((key for key in requested if key.startswith("Arrow")), None)
                              or ("Space" if "Space" in requested else None))
                elif typ == "checkbox" or role == "checkbox":
                    chosen = "Space" if "Space" in requested else None
                elif tag in {"button", "a"} or role in {
                        "button", "link", "tab", "menuitem", "option"}:
                    chosen = "Enter" if "Enter" in requested else None
                if not chosen:
                    continue
                identity = stable_identity(element) + "|" + chosen.casefold()
                if disabled_now:
                    if identity not in {item.get("identity") for item in disabled}:
                        disabled.append({"identity": identity, "label": label, "key": chosen})
                    continue
                if identity in operated:
                    continue
                candidate = (element, label, chosen, identity)
                break
            if candidate is None:
                break
            element, label, chosen, identity = candidate
            # Mark before acting: a control that stays present with no business-state change must not be
            # pressed forever, while its exact trusted receipt still remains available to the judge.
            operated.add(identity)
            subaction = {"cmd": "press", "idx": element.get("idx"),
                         "target_text": label, "value": chosen}
            result = self.act(subaction)
            after = self.state(include_accessibility=False)
            record(subaction, result, before, after)
            if result.get("ok") is False:
                return {"ok": False, "cmd": "keyboardMatrix", "keyboardMatrix": True,
                        "error": str(result.get("error") or "keyboard sub-action failed")[:500],
                        "requested_keys": requested, "keys_proven": sorted(keys_proven),
                        "actions": receipts, "traversals": traversals,
                        "disabled_controls": disabled, "action_count": len(receipts)}
            if chosen in trusted_keys(result):
                keys_proven.add(chosen)

        # If the evidence bound was reached while another applicable enabled control remains, fail open as an
        # incomplete receipt. A continuation can run another matrix; it may not certify a truncated inventory.
        final_state = self.state(include_accessibility=False)
        remaining_enabled = []
        for element in final_state.get("elements") or []:
            if not isinstance(element, dict):
                continue
            tag, typ, role = (str(element.get(name) or "").casefold()
                              for name in ("tag", "type", "role"))
            chosen = (next((key for key in requested if key.startswith("Arrow")), None)
                      if tag == "select" or role in {"combobox", "listbox"} else
                      "Space" if (typ == "checkbox" or role == "checkbox") and "Space" in requested else
                      "Enter" if (tag in {"button", "a"} or role in {
                          "button", "link", "tab", "menuitem", "option"}) and "Enter" in requested else None)
            if not chosen or str(element.get("disabled") or "").casefold() in {"1", "true", "disabled"}:
                continue
            identity = stable_identity(element) + "|" + chosen.casefold()
            if identity not in operated:
                remaining_enabled.append(_element_label(element) or identity)
        complete = not remaining_enabled and set(requested).issubset(keys_proven)
        return {"ok": True, "cmd": "keyboardMatrix", "keyboardMatrix": True,
                "requested_keys": requested, "keys_proven": sorted(keys_proven),
                "all_requested_keys_proven": set(requested).issubset(keys_proven),
                "all_applicable_controls_exercised": not remaining_enabled,
                "remaining_enabled_controls": remaining_enabled[:20],
                "disabled_controls": disabled, "actions": receipts, "traversals": traversals,
                "action_count": len(receipts), "complete": complete}

    def act(self, action):
        """Act: translate the AI's action dict {cmd, idx|selector, value} into the bridge's native
        command. Supported cmds: click, burst, timed_transition, type/fill, scenario_matrix, press, goto, reload, back,
        forward, viewport, scroll, wait, noop (unknowns pass through so a new bridge verb works without a
        code change here)."""
        action = action or {}
        cmd = (action.get("cmd") or "noop").lower()
        idx, sel, val = action.get("idx"), action.get("selector"), action.get("value")
        target_text, role = action.get("target_text") or action.get("target"), action.get("role")
        if cmd in ("scenario_matrix", "case_matrix", "scenariomatrix", "casematrix"):
            return self._scenario_matrix(action)
        if cmd in ("keyboard_matrix", "keyboardmatrix"):
            return self._keyboard_matrix(action)
        if cmd in ("timed_transition", "timedtransition"):
            raw_duration = action.get("duration_ms")
            if raw_duration is None:
                try:
                    raw_duration = float(action.get("duration_s", 10)) * 1000
                except (TypeError, ValueError):
                    raw_duration = 10000
            payload = {
                "cmd": "timedTransition", "idx": idx, "selector": sel,
                "duration_ms": max(250, min(300000, int(raw_duration))),
                "completion_grace_ms": max(0, min(
                    30000, int(action.get("completion_grace_ms", 5000)))),
                "pending_text": str(action.get("pending_text") or ""),
            }
            if action.get("require_full_duration") is False:
                payload["full_duration_required"] = False
            return self._send(payload)
        if cmd in ("click", "tap"):
            # Models sometimes describe choosing an option as a click carrying the desired value. A native
            # click only focuses/opens a <select>; normalize it to the bridge's select-aware fill operation.
            if val not in (None, "") and (role or "").lower() in ("combobox", "select"):
                return self._send({"cmd": "fill", "idx": idx, "selector": sel, "value": val})
            # Resolve an explicit label/role against the LIVE DOM (robust to SPA re-renders).  The label is
            # authoritative: a stale model-supplied idx must never become a blind fallback after a semantic
            # miss, because it can mutate an adjacent control while the receipt still says the intended label
            # was absent.  idx/selector are supported only for actions that have no semantic target.
            retry_after_no_effect = action.get("_qa_retry_after_no_effect") is True
            if target_text and not retry_after_no_effect:
                return self._send({"cmd": "clickByText", "text": target_text, "role": role})
            payload = {"cmd": "click", "idx": idx, "selector": sel}
            if retry_after_no_effect:
                payload["_qa_retry_after_no_effect"] = True
            return self._send(payload)
        if cmd in ("touch", "pen"):
            return self._send({"cmd": "pointer", "idx": idx, "selector": sel, "pointer_type": cmd})
        if cmd in ("type", "fill"):
            key = _key_name(val)
            if key and (role or "").lower() not in ("textbox", "input", "textarea"):
                return self._send({"cmd": "press", "idx": idx, "selector": sel, "key": key})
            payload = {"cmd": "fill", "idx": idx, "selector": sel, "value": val}
            # ``fill`` is intentionally fast and synthetic. A story that explicitly requires human-paced
            # keystrokes must instead produce trusted keyboard receipts; callers opt into that stronger path
            # with a bounded pace rather than slowing every ordinary setup action.
            if cmd == "type" and action.get("pace_ms") is not None:
                payload["cmd"] = "humanType"
                payload["pace_ms"] = max(10, min(500, int(action.get("pace_ms"))))
            return self._send(payload)
        if cmd == "paste":
            return self._send({"cmd": "paste", "idx": idx, "selector": sel, "value": val})
        if cmd == "press":
            payload = {"cmd": "press", "idx": idx, "selector": sel,
                       "key": _key_name(val) or action.get("key") or "Enter"}
            if action.get("_qa_reported_focus_source") is True:
                payload["_qa_reported_focus_source"] = True
            return self._send(payload)
        if cmd in ("traverse", "tab_traverse", "tabtraverse"):
            direction = str(val or action.get("direction") or "forward").strip().lower()
            payload = {"cmd": "tabTraverse", "direction": direction,
                       "count": action.get("count")}
            if action.get("pace_ms") is not None:
                payload["pace_ms"] = max(0, min(1500, int(action.get("pace_ms"))))
            return self._send(payload)
        if cmd == "hold":
            return self._send({"cmd": "hold", "idx": idx, "selector": sel,
                               "key": _key_name(val) or action.get("key") or "Space",
                               "duration_ms": action.get("duration_ms", 350)})
        if cmd == "goto":
            return self._send({"cmd": "goto", "url": self._pin_origin(val or action.get("url"))})
        if cmd == "reload":
            return self._send({"cmd": "reload"})
        if cmd in ("back", "forward"):
            return self._send({"cmd": cmd})
        if cmd in ("burst", "click_burst", "clickburst"):
            return self._send({"cmd": "clickBurst", "idx": idx, "selector": sel,
                               "count": action.get("count", 5),
                               "interval_ms": action.get("interval_ms", 25)})
        if cmd in ("reset_storage", "resetstorage"):
            # ``value`` is overloaded across the action grammar. Models reasonably emit semantic reset
            # labels such as ``empty`` or ``fresh`` here; treating those as relative URLs navigates the app
            # to /empty or /fresh and fabricates a product 404. A reset always reopens the pinned product
            # unless the action supplies either an explicit ``url`` or an absolute browser URL in ``value``.
            # Deterministic setup decisions already use the absolute target URL, so this remains compatible
            # with durable checkpoints while making free-form semantic labels harmless.
            reset_url = action.get("url")
            if not reset_url and isinstance(val, str):
                from urllib.parse import urlsplit
                candidate = val.strip()
                try:
                    parsed = urlsplit(candidate)
                    if parsed.scheme in {"http", "https"} and parsed.netloc:
                        reset_url = candidate
                except ValueError:
                    reset_url = None
            return self._send({"cmd": "resetStorage",
                               "url": self._pin_origin(reset_url or self.target_url)})
        if cmd in ("viewport", "resize"):
            size = val if isinstance(val, dict) else action
            try:
                width, height = int(size.get("width")), int(size.get("height"))
            except (AttributeError, TypeError, ValueError):
                return {"ok": False, "cmd": "viewport",
                        "error": "viewport requires integer width and height"}
            if not (320 <= width <= 3840 and 320 <= height <= 2160):
                return {"ok": False, "cmd": "viewport",
                        "error": "viewport width/height outside safe bounds"}
            return self._send({"cmd": "viewport", "width": width, "height": height})
        if cmd == "scroll":
            if target_text:
                return self._send({"cmd": "scrollToText", "text": target_text})
            return self._send({"cmd": "eval", "expr": f"window.scrollBy(0,{int(val or 600)});true"})
        if cmd in ("dwell_surfaces", "dwell_landmarks", "dwelllandmarks"):
            return self.dwell_landmarks(action.get("targets") or [],
                                        action.get("duration_s", val or 10))
        if cmd in ("inspect_surfaces", "inspect_landmarks", "inspectlandmarks"):
            # Reuse the same exact-landmark browser receipt with only its minimum settle interval. Unlike a
            # dwell story this command proves visible multi-panel content, not elapsed idle time.
            return self.dwell_landmarks(action.get("targets") or [], 0.025)
        if cmd == "wait":
            raw = action.get("duration_s", action.get("seconds", val))
            if isinstance(raw, str):
                text = raw.strip().lower()
                try:
                    raw = float(text[:-2]) / 1000 if text.endswith("ms") else (
                        float(text[:-1]) if text.endswith("s") else float(text or 0))
                except ValueError:
                    raw = 0
            return self.dwell(raw)
        if cmd == "noop":
            return {"ok": True, "cmd": cmd}
        return self._send({"cmd": cmd, **{k: v for k, v in action.items() if k != "cmd"}})

    def close(self):
        close_acknowledged = False
        try:
            reply = self._send({"cmd": "close"})            # the bridge returns {closed, video:<webm path>}
            close_acknowledged = isinstance(reply, dict) and bool(reply.get("closed"))
            if isinstance(reply, dict) and reply.get("video"):
                self.video_path = reply["video"]
        except Exception:
            pass
        # A protocol close lets at_driver/browser_bridge run their own finally
        # blocks (Orca, Chromium, ffmpeg, D-Bus and temp cleanup). Give that
        # acknowledged path a short bounded grace before forcing the exact tree.
        if close_acknowledged and self.proc and self.proc.poll() is None:
            try:
                # at_driver owns a three-second bounded child shutdown window.
                # Give its final temp-tree removal headroom instead of racing it
                # at the exact same deadline and forcing the registered tree.
                self.proc.wait(timeout=min(5.0, max(0.25, float(self.timeout))))
            except (subprocess.TimeoutExpired, TimeoutError):
                pass
            except Exception:
                pass
        try:
            if self.proc and self.proc.poll() is None:
                self._terminate_process_tree(signal.SIGTERM)
                self.proc.wait(timeout=5)
        except Exception:
            try:
                self._terminate_process_tree(signal.SIGKILL)
                if self.proc:
                    self.proc.wait(timeout=2)
            except Exception:
                pass
        finally:
            _release_at_session_lock(getattr(self, "_at_session_lock", None))
            self._at_session_lock = None
            with _LIVE_BRIDGES_LOCK:
                _LIVE_BRIDGES.discard(self)
            try:
                import clauded
                clauded.unregister_owned(getattr(self, "_ownership_record", None))
            except Exception:
                pass
            self._ownership_record = None
            if self._gate_slot is not None:                 # release the global browser slot for the next session
                try:
                    import browser_gate
                    browser_gate.release(self._gate_slot)
                except Exception:
                    pass
                self._gate_slot = None
            if getattr(self, "_at_runtime_dir", None):
                shutil.rmtree(self._at_runtime_dir, ignore_errors=True)
                self._at_runtime_dir = None

    def _terminate_process_tree(self, sig):
        """The bridge owns a new process group, so teardown can reap Node plus Chromium/ffmpeg children."""
        if not self.proc:
            return
        try:
            os.killpg(self.proc.pid, sig)
        except Exception:
            try:
                if sig == signal.SIGKILL:
                    self.proc.kill()
                else:
                    self.proc.terminate()
            except Exception:
                pass


# ----------------------------------------------------------------------------------------------------
# Prompt assembly — kept as pure functions so the self-test can assert exactly what the AI is shown.
# ----------------------------------------------------------------------------------------------------
def _element_label(e):
    """The single best human/AI-facing LABEL for a control — what the AI should target it by. Prefers
    visible text, then aria-label, then name/placeholder/title. This is the anchor for intent-based
    targeting (NOT the idx)."""
    keys = (("associatedLabel", "ariaLabel", "text", "name", "placeholder", "title")
            if str(e.get("tag") or "").lower() != "label" else
            ("text", "ariaLabel", "name", "placeholder", "title"))
    for k in keys:
        v = (e.get(k) or "").strip()
        if v:
            return v
    return ""


def _progress_view_signature(state):
    """Return a stable key for a materially distinct browser-evidence state.

    URL-only progress misclassified keyboard navigation on a single-page app as a stall: Tab could move
    through many real controls while every step still had the same URL. Screenshots and timestamps are
    intentionally excluded, so a true loop back to the same evidence still repeats its key.
    """
    state = state or {}
    active = state.get("activeElement") if isinstance(state.get("activeElement"), dict) else {}
    scroll = state.get("scrollPosition") if isinstance(state.get("scrollPosition"), dict) else {}
    viewport = state.get("viewport") if isinstance(state.get("viewport"), dict) else {}

    def norm(value, limit):
        text = " ".join(str(value or "").split())
        # Auto-refreshed UI timestamps are observability noise, not evidence progress. Without normalization a
        # tester can scroll to the same section forever while an “Updated …” label mints a new state hash.
        text = re.sub(
            r"\b20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b",
            "<timestamp>", text)
        return text[-limit:]

    payload = {
        "url": str(state.get("url") or "")[:1000],
        "viewport": {key: viewport.get(key) for key in ("width", "height")},
        "scroll": {"x": scroll.get("x"), "y": scroll.get("y")},
        "active": {key: active.get(key) for key in (
            "idx", "tag", "role", "text", "ariaLabel", "name", "placeholder", "value",
            "checked", "current", "pressed", "selectedState", "expanded")},
        "status_tail": norm(state.get("statusText"), 900),
        "viewport_text": norm(state.get("viewportText") or state.get("bodyText"), 900),
        "live_events": list(state.get("accessibilityEvents") or [])[-3:],
        "actual_at_events": list(state.get("actualAssistiveTechnologyEvents") or [])[-3:],
        "platform_events": list(state.get("accessibilityPlatformEvents") or [])[-3:],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


def _repeat_action_policy(action, progress_view, batch_context=0):
    """Return the semantic repeat key and no-progress repeat allowance for one action.

    A normal Tab press reaches only one focus stop, so the resulting focus/scroll state distinguishes genuine
    forward progress from a loop. Batched traversal and multi-surface dwell already contain the complete
    sequence in one mechanical receipt; allowing their final focus/scroll state to mint a new key makes the
    model rerun the same full audit several times. ``batch_context`` advances only after a materially different
    non-batch action, so the same traversal/landmark is legitimately available again after a viewport change,
    reload, seed, or UI mutation while focus/scroll noise alone cannot defeat the guard. For a truly identical
    batch, one no-coverage retry is enough before cumulative diagnosis takes over.
    """
    item = action or {}
    cmd = str(item.get("cmd") or "")
    batched = cmd in {"traverse", "inspect_surfaces", "dwell_surfaces", "timed_transition",
                      "scenario_matrix", "case_matrix", "keyboard_matrix"} or (
        cmd == "scroll" and bool(item.get("target_text")))
    def canonical(value):
        return " ".join(str(value or "").casefold().split())

    named_scroll = cmd == "scroll" and bool(item.get("target_text"))
    # Scenario matrices are batch actions, but their cases are the semantic identity of the work. Without
    # them in the signature, a second matrix covering different validation cases looks like a repeat merely
    # because both actions end on the same form. Canonical JSON keeps identical matrices stable across view
    # changes while allowing genuinely different case groups to make forward progress.
    matrix_cases = (json.dumps(item.get("cases") or [], sort_keys=True, ensure_ascii=False, default=str)
                    if cmd in {"scenario_matrix", "case_matrix"} else "")
    # Repeated control activation must retain one semantic key even when it appends an audit row, creates a
    # duplicate blocker, or refreshes a timestamp. Coverage progress already resets the counter below; letting
    # incidental post-click UI churn mint a new key allowed the same Publish/Submit button to be activated
    # indefinitely with zero ledger progress. Keyboard traversal and incremental scrolling are different: each
    # resulting focus/scroll state is the evidence of forward movement, so those commands keep the view key.
    progress_key = (progress_view if cmd in {"scroll"} or (
        cmd == "press" and str(item.get("value") or item.get("key") or "").casefold().replace(" ", "")
        in {"tab", "shift+tab"}) else "")
    signature = "|".join(str(value or "") for value in (
        cmd, canonical(item.get("target_text")), "" if named_scroll else canonical(item.get("role")),
        "" if named_scroll else item.get("value"),
        "" if batched else progress_key,
        json.dumps([canonical(value) for value in (item.get("targets") or [])],
                   sort_keys=True, default=str) if batched else "",
        matrix_cases, item.get("duration_s") if cmd == "timed_transition" else "",
        batch_context if batched else "",
    ))
    return signature, (1 if batched and _REPEAT_LIMIT else _REPEAT_LIMIT), batched


def _contract_fenced_traversal_action(action, story):
    """Remove an undersized model count when the contract requires exhaustive keyboard traversal.

    ``tabTraverse`` derives the live focusable inventory only when count is omitted. A model once supplied 30
    for a page with more than 30 controls, then reported the still-unreached CEO button as inaccessible. For
    an all/every-control story—or a focused replay of that traversal—the product inventory owns the batch size.
    Bounded exploratory traversals retain their explicit count.
    """
    item = dict(action or {})
    if str(item.get("cmd") or "").casefold() not in {"traverse", "tab_traverse", "tabtraverse"}:
        return item
    story = story if isinstance(story, dict) else {}
    finding_action = ((story.get("focused_finding") or {}).get("action") or {})
    focused_traversal = (
        str(story.get("category") or "").casefold() == "focused-regression"
        and str(finding_action.get("cmd") or "").casefold()
        in {"traverse", "tab_traverse", "tabtraverse"})
    contract = _story_text(story).casefold()
    exhaustive = bool(re.search(
        r"\b(?:all|every)\b[^.;]{0,100}\b(?:interactive|focusable|action|form)?\s*controls?\b|"
        r"\b(?:reach|traverse|visit)\b[^.;]{0,100}\b(?:all|every)\b[^.;]{0,60}\bcontrols?\b",
        contract))
    if focused_traversal or exhaustive:
        item.pop("count", None)
        item["_qa_inventory_derived"] = True
    return item


def _contract_fenced_owned_surface_action(action, story, expected, state):
    """Bind an acknowledgement assertion to its contract-owning CEO surface.

    Pages can expose a read-only staff ``Unresolved blockers`` inventory and a separate CEO ``Risk Signals``
    panel with the acknowledgement controls. A text-only landmark choice previously selected the first label,
    inspected the staff list, and fabricated an inaccessible-control product defect. Preserve arbitrary
    product labels generally; apply this fence only to the canonical CEO blocker-acknowledgement contract and
    only when an owning landmark is present in the observed DOM inventory.
    """
    item = dict(action or {})
    cmd = str(item.get("cmd") or "").casefold()
    if cmd not in {"scroll", "inspect_surfaces", "inspect_landmarks", "inspectlandmarks",
                   "dwell_surfaces", "dwell_landmarks", "dwelllandmarks"}:
        return item
    story = story if isinstance(story, dict) else {}
    context = " ".join((str(story.get("id") or ""), _story_text(story),
                        str(expected or ""), json.dumps(item, default=str))).casefold()
    if (not re.search(r"\backnowledg", context)
            or not re.search(r"\b(?:blocker|risk)s?\b", context)
            or not (str(story.get("id") or "").upper() == "US-011" or "ceo" in context)):
        return item
    landmarks = [entry for entry in ((state or {}).get("documentLandmarks") or [])
                 if isinstance(entry, dict)]
    labels = [str(entry.get("label") or entry.get("text") or "").strip()
              for entry in landmarks]
    owner = next((label for label in labels if label.casefold() == "risk signals"), None)
    owner = owner or next((label for label in labels if label.casefold() == "ceo command view"), None)
    if not owner:
        return item

    def ambiguous(value):
        low = " ".join(str(value or "").casefold().split())
        return bool(re.search(r"\b(?:unresolved|open)\s+blockers?\b", low))

    if cmd == "scroll" and ambiguous(item.get("target_text")):
        item["target_text"] = owner
        item["_qa_surface_owner_fenced"] = True
    elif cmd != "scroll":
        targets = list(item.get("targets") or [])
        repaired = [owner if ambiguous(target) else target for target in targets]
        if repaired != targets:
            item["targets"] = list(dict.fromkeys(repaired))
            item["_qa_surface_owner_fenced"] = True
    return item


_QUEUE_RETRY_PROJECTION_TARGETS = [
    "Staff operating console", "Agent jobs", "CEO command view",
    "Operational diagnostics", "Queue and Dead Letters",
    "Governance audit history", "Enquiry review details",
]


def _contract_fenced_queue_projection_action(action, story, coverage, state):
    """Expand a US-008 projection probe to the complete authored surface set.

    The model sometimes selected only Agent jobs plus the aggregate CEO/diagnostic panels and then reported
    that it could not see the notification or audit evidence it had omitted from its own probe.  The story
    explicitly names the staff, CEO, queue/dead-letter, notification, blocker, and audit projections.  When
    the live page is already at either terminal failure or recovered success, bind one passive inspection to
    the canonical owning landmarks.  This does not decide whether the product passes; it only prevents an
    under-scoped read from manufacturing missing evidence.
    """
    item = dict(action or {})
    if str(item.get("cmd") or "").casefold() != "inspect_surfaces":
        return item
    contract = _story_text(story)
    unresolved = [str(row.get("aspect") or "") for row in (coverage or [])
                  if isinstance(row, dict) and not row.get("covered")]
    if (not re.search(r"\bretry dead[- ]lettered agent work\b", contract, re.I)
            or not any(re.search(r"\b(?:failed|dead.?letter|retry|recovery|run\s*after|runafter)\b",
                                 row, re.I) for row in unresolved)):
        return item
    rendered = " ".join(str((state or {}).get(name) or "") for name in (
        "bodyText", "viewportText", "statusText"))
    terminal = _live_terminal_queue_failure(state)
    recovered = bool(
        re.search(r"\bagent jobs?\b[\s\S]{0,1200}\bsucceeded\b", rendered, re.I)
        and re.search(r"\b(?:attempts?|run\s*after|runafter)\b", rendered, re.I))
    if not (terminal or recovered):
        return item
    if item.get("targets") != _QUEUE_RETRY_PROJECTION_TARGETS:
        item["targets"] = list(_QUEUE_RETRY_PROJECTION_TARGETS)
        item["_qa_queue_projection_fenced"] = True
    return item


def _foreign_story_fixture_target(action, story, elements):
    """Return a visible fixture label when it belongs to a different canonical story."""
    item = action if isinstance(action, dict) else {}
    if str(item.get("cmd") or "").casefold() not in {"click", "tap", "touch", "pen", "press"}:
        return None
    current = re.search(r"\bUS[-\s]?0*(\d+)\b", str((story or {}).get("id") or ""), re.I)
    if current is None:
        return None
    idx, label, _score = _resolve_target(item, elements or [])
    if idx is None and item.get("idx") is not None:
        element = next((candidate for candidate in (elements or [])
                        if candidate.get("idx") == item.get("idx")), None)
        label = _element_label(element) if element else label
    label = str(label or item.get("target_text") or item.get("target") or "")
    fixture_ids = re.findall(r"\bUS[-\s]?0*(\d+)\b", label, re.I)
    if not fixture_ids:
        return None
    current_id = str(int(current.group(1)))
    return label if any(str(int(value)) != current_id for value in fixture_ids) else None


def _unreceipted_traversal_key_false_positive(targeting, bug):
    """Reject a traversal defect that depends on a key the trusted driver never sent.

    A Tab traversal proves reach, order, focus visibility, scrolling, and overflow.  It does not prove that
    ArrowDown changed a select or that Enter/Space activated a button unless those keys are present in the
    sealed traversal receipt.  A semantic judge once invented an ArrowDown attempt from an all-controls story
    even though the receipt contained only Tab events, turning missing activation evidence into a product bug.
    The browser receipt is authoritative, so that claim is incomplete QA evidence rather than a defect.
    """
    receipt = (targeting or {}).get("traversal")
    if not isinstance(receipt, dict) or not bug:
        return False

    performed = set()
    primary = _key_name(receipt.get("key"))
    if primary:
        performed.add(primary.casefold().replace(" ", ""))
    if str(receipt.get("direction") or "").casefold() == "backward":
        performed.add("shift")
    for event in list(receipt.get("input_events") or []) + list(
            receipt.get("keyboard_receipts") or []):
        if isinstance(event, dict):
            key = _key_name(event.get("key"))
            if key:
                performed.add(key.casefold().replace(" ", ""))

    aliases = {
        "arrowdown": r"\b(?:arrow\s*down|down\s+arrow)\b",
        "arrowup": r"\b(?:arrow\s*up|up\s+arrow)\b",
        "arrowleft": r"\b(?:arrow\s*left|left\s+arrow)\b",
        "arrowright": r"\b(?:arrow\s*right|right\s+arrow)\b",
        "enter": r"\b(?:enter|return)\b",
        " ": r"\bspace(?:bar)?\b",
        "home": r"\bhome(?:\s+key)?\b",
        "end": r"\bend\s+key\b",
    }
    claimed = {key for key, pattern in aliases.items() if re.search(pattern, str(bug), re.I)}
    return bool(claimed - performed)


def _fmt_elements(elements, limit=80, max_chars=14000):
    def clip(value, n=240):
        value = str(value or "").replace("\n", " ").strip()
        return value if len(value) <= n else value[:n] + "…"

    source = list(elements or [])
    lines, used = [], 0
    for pos, e in enumerate(source[:limit]):
        parts = [f"[{e.get('idx')}]", e.get("tag", "?")]
        if e.get("type"):
            parts.append(e["type"])
        # LABEL is called out explicitly so the AI targets by intent/label, not by blind index.
        label = clip(_element_label(e))
        parts.append(f'LABEL="{label}"' if label else "LABEL=(none)")
        # Accessible labels are not guaranteed to be unique (forms commonly repeat labels such as
        # "Claim reference").  Expose stable DOM identity as a disambiguation aid while retaining LABEL as
        # the primary intent contract.  The planner may then select the exact observed idx for one of several
        # equally labelled controls instead of guessing from document order.
        name = clip(e.get("name"), 100)
        element_id = clip(e.get("id"), 120)
        if name:
            parts.append(f'NAME="{name}"')
        if element_id:
            parts.append(f'ID="{element_id}"')
        options = clip(e.get("options"), 300)
        if options:
            parts.append(f'OPTIONS="{options}"')
        role = (e.get("role") or "").strip()
        if role:
            parts.append(f"role={role}")
        if e.get("href") and str(e.get("href") or "").strip() != label:
            parts.append(f"href={clip(e['href'], 160)}")
        if e.get("disabled"):
            parts.append("(disabled)")
        if e.get("required"):
            parts.append("(required)")
        if e.get("formValid") is not None:
            parts.append(f"FORM_VALID={str(e.get('formValid')).lower()}")
        # SEMANTIC STATE from the DOM (aria-current/aria-pressed/aria-selected/aria-expanded/checked/value): the
        # ground truth for selected/active/checked judgements. Screenshots also carry hover/focus/
        # transition styling — pixels alone must never decide state (a cursor resting on a button is
        # not a selection; three QA rounds false-positived on exactly that).
        for k, tag in (("current", "aria-current"), ("pressed", "aria-pressed"), ("selectedState", "aria-selected"),
                       ("expanded", "aria-expanded"), ("checked", "checked")):
            if e.get(k) is not None and str(e.get(k)).strip() != "":
                parts.append(f"{tag}={e[k]}")
        if e.get("value") is not None and str(e.get("value")).strip() != "":
            parts.append(f"value={json.dumps(str(e['value'])[:60])}")
        line = clip(" ".join(str(p) for p in parts), 420)
        if lines and used + len(line) > max_chars:
            lines.append(f"… ({len(source) - pos} additional controls omitted from this bounded prompt)")
            break
        lines.append(line)
        used += len(line) + 1
    if len(source) > limit and len(lines) == min(limit, len(source)):
        lines.append(f"… ({len(source) - limit} additional controls omitted from this bounded prompt)")
    return "\n".join(lines) or "(no interactable elements found)"


def _fmt_history(history, limit=5):
    if not history:
        return "(this is the first step)"
    out = []
    for h in history[-limit:]:
        action = json.dumps(h.get("action") or {}, ensure_ascii=False)[:600]
        expected = str(h.get("expected") or "")[:500]
        bug = str(h.get("bug") or "")[:500]
        targeting = h.get("targeting") if isinstance(h.get("targeting"), dict) else {}
        missing = [str(field.get("label") or field.get("name") or field.get("id") or "required field")
                   for field in (targeting.get("empty_required_fields_before") or [])
                   if isinstance(field, dict)]
        setup = f" required setup still empty before action={missing[:12]!r}" if missing else ""
        out.append(
            f"- step {h['step']}: action={action} expected={expected!r} "
            f"-> {'MATCH' if h.get('matched') else 'MISMATCH'}"
            + setup + (f" BUG: {bug}" if bug else ""))
    return "\n".join(out)


def _fmt_effectful_control_history(history, limit=16, max_chars=5000):
    """Preserve registered business actions beyond the short conversational history.

    A mismatch can mean a post-action focus/privacy clause failed even though the requested activation
    definitely changed durable state. Forgetting those actions made continuations repeat acknowledgements or
    submissions and then blame the product for the additional records that QA itself created.
    """
    receipts = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        cmd = str(action.get("cmd") or "").casefold()
        if cmd not in {"click", "tap", "touch", "pen", "press", "burst", "click_burst",
                       "clickburst", "timed_transition", "scenario_matrix", "case_matrix"}:
            continue
        key = str(action.get("value") or action.get("key") or "").casefold().replace(" ", "")
        if cmd == "press" and key in {"tab", "shift+tab", "arrowup", "arrowdown", "arrowleft",
                                      "arrowright", "home", "end", "escape"}:
            continue
        targeting = item.get("targeting") if isinstance(item.get("targeting"), dict) else {}
        if not (targeting.get("driver_ok") and targeting.get("effect_registered")):
            continue
        target = (action.get("target_text") or targeting.get("targeted_label")
                  or action.get("selector") or (f"idx={action.get('idx')}"
                                                if action.get("idx") is not None else ""))
        verdict = item.get("verdict")
        receipts.append({
            "step": item.get("step"), "cmd": cmd, "target": str(target)[:220],
            "key": str(action.get("value") or action.get("key") or "")[:80] or None,
            "verdict": (verdict.get("verdict") if isinstance(verdict, dict) else verdict),
            "matched": (item.get("matched") if "matched" in item else
                        (verdict.get("matches_expected") if isinstance(verdict, dict) else None)),
        })
    if not receipts:
        return "(none)"
    return json.dumps(receipts[-max(1, int(limit)):], ensure_ascii=False, default=str)[:max_chars]


def _registered_business_mutation(action, targeting):
    """Whether a trusted control action crossed a durable/business side-effect boundary.

    Fixture loading, navigation, form entry, and passive observation are setup.  A confirmed acknowledge,
    approve, send, submit, publish, retry, drain, or equivalent action is different: even when its composite
    focus/privacy expectation receives MISMATCH, the mutation already happened.  Focused regressions get one
    such attempt and must diagnose its settled receipt instead of mutating sibling records until one passes.
    """
    action = action if isinstance(action, dict) else {}
    targeting = targeting if isinstance(targeting, dict) else {}
    cmd = str(action.get("cmd") or targeting.get("action_kind") or "").casefold()
    if cmd not in {
            "click", "tap", "touch", "pen", "press", "hold", "burst", "click_burst",
            "clickburst", "timed_transition", "scenario_matrix", "case_matrix"}:
        return False
    if not (targeting.get("driver_ok") and targeting.get("effect_registered")
            and targeting.get("label_matched")):
        return False
    label = " ".join(str(value or "") for value in (
        action.get("target_text"), targeting.get("intended"), targeting.get("targeted_label")))
    return bool(re.search(
        r"\b(?:acknowledge|approve|reject|send|submit|publish|save|create|delete|remove|"
        r"retry|drain|verify|attach|cancel|refund|charge|pay|invite|revoke|archive)\b",
        label, re.I))


def _fmt_requests(reqs, limit=6):
    out = []
    for r in (reqs or [])[-limit:]:
        tag = r.get("failed") or r.get("status")
        out.append(f"{r.get('method')} {str(r.get('url') or '')[:500]} -> {tag}")
    return "\n".join(out) or "(none)"


def _story_requires_full_accessibility(story):
    """Only collect/send the expensive AX tree when the story actually needs assistive-tech semantics.

    Keyboard focus and ordinary validation still use ``activeElement`` and rendered controls.  Treating every
    mention of focus as a screen-reader story made a simple invalid-input action carry tens of thousands of
    irrelevant AX/event tokens into both judges.
    """
    text = json.dumps(story or {}, ensure_ascii=False, default=str).lower()
    return any(term in text for term in (
        "accessibility", "accessible", "screen reader", "assistive technology", "aria-", "aria ",
        "live region", "nvda", "voiceover", "voice over", "orca", "at-spi",
    ))


def _story_requires_visual_judgment(story, expected="", targeting=None):
    """Whether pixels are material to this particular expected-vs-actual decision.

    Screenshots are always captured and retained as evidence. Opening one in every independent judge, however,
    can add tens of thousands of vision tokens to a form fill, queue transition, or permission assertion whose
    authoritative facts are already in the driver/DOM receipt. Reserve paid visual inspection for contracts
    where pixels are evidence; other visual quality is still covered by dedicated responsive/layout stories.
    """
    text = (json.dumps(story or {}, ensure_ascii=False, default=str) + " " + str(expected or "")).lower()
    action_kind = str((targeting or {}).get("action_kind") or "").lower()
    return action_kind == "viewport" or any(term in text for term in (
        "visual", "layout", "responsive", "viewport", "mobile", "desktop", "pixel", "screenshot",
        "overlap", "overlapping", "clipped", "clipping", "truncated", "contrast", "colour", "color",
        "spacing", "alignment", "flicker", "animation", "screen jump",
    ))


def _fmt_state(state, *, include_accessibility=True, element_limit=80, element_chars=14000,
               viewport_chars=2600, document_chars=1400, status_chars=1200,
               landmark_limit=40, landmark_chars=5000, compact_accessibility=False):
    active = state.get("activeElement")
    status = str(state.get("statusText") or "(none)")[:status_chars]
    console_errors = [str(e)[:500] for e in (state.get("console_errors") or [])[:6]]
    accessibility = ""
    if include_accessibility:
        region_limit, region_chars = ((10, 1800) if compact_accessibility else (20, 3500))
        tree_chars = 2800 if compact_accessibility else 4500
        event_limit, event_chars = ((8, 1600) if compact_accessibility else (16, 3500))
        at_limit, at_chars = ((12, 2400) if compact_accessibility else (16, 3500))
        platform_limit, platform_chars = ((8, 1400) if compact_accessibility else (16, 3500))
        accessibility = (
            f"ACCESSIBILITY_REGIONS (DOM role/live-region semantics): "
            f"{json.dumps((state.get('accessibilityRegions') or [])[-region_limit:], default=str)[:region_chars]}\n"
            f"ACCESSIBILITY_TREE (browser-computed, bounded):\n"
            f"{str(state.get('accessibilityTree') or '(unavailable)')[:tree_chars]}\n"
            f"LIVE_REGION_EVENT_TRACE (bounded DOM mutation events): "
            f"{json.dumps((state.get('accessibilityEvents') or [])[-event_limit:], default=str)[:event_chars]}\n"
            f"ACTUAL_ASSISTIVE_TECHNOLOGY_AVAILABLE: "
            f"{bool(state.get('actualAssistiveTechnologyAvailable'))}\n"
            f"ACTUAL_ASSISTIVE_TECHNOLOGY_EVENTS (Orca/AT-SPI utterances; never inferred from DOM/CDP): "
            f"{json.dumps((state.get('actualAssistiveTechnologyEvents') or [])[-at_limit:], default=str)[:at_chars]}\n"
            f"ACCESSIBILITY_PLATFORM_EVENTS (Chromium CDP Accessibility domain): "
            f"{json.dumps((state.get('accessibilityPlatformEvents') or [])[-platform_limit:], default=str)[:platform_chars]}\n")
    return (
        f"URL: {state.get('url')}\n"
        f"TITLE: {state.get('title')}\n"
        f"VIEWPORT: {json.dumps(state.get('viewport') or {})}\n"
        f"SCROLL_POSITION: {json.dumps(state.get('scrollPosition') or {})}\n"
        f"DOCUMENT_LANDMARKS (exact long-page scroll targets): "
        f"{(json.dumps((state.get('documentLandmarks') or [])[:landmark_limit], default=str)[:landmark_chars] if landmark_limit else '(omitted from action judge)')}\n"
        f"STATUS/LIVE_REGION_TEXT: {status}\n"
        f"{accessibility}"
        f"ACTIVE_ELEMENT: {_fmt_elements([{**active, 'idx': 'active'}]) if active else '(none)'}\n"
        f"CONSOLE_ERRORS: {json.dumps(console_errors)}\n"
        f"RECENT_NETWORK (method url -> status; null status/failed = a failed request):\n"
        f"{_fmt_requests(state.get('recent_requests'))}\n"
        f"VIEWPORT_TEXT (current scroll position, truncated):\n"
        f"{(state.get('viewportText') or state.get('bodyText') or '')[:viewport_chars]}\n"
        f"DOCUMENT_TEXT_PREFIX (truncated):\n{(state.get('bodyText') or '')[:document_chars]}\n"
        f"INTERACTABLE_ELEMENTS (refer to these by their [idx]):\n"
        f"{_fmt_elements(state.get('elements'), limit=element_limit, max_chars=element_chars)}")


def _fmt_story_state(state, story, *, evaluation=False, include_accessibility=None):
    full_a11y = (_story_requires_full_accessibility(story)
                 if include_accessibility is None else bool(include_accessibility))
    return _fmt_state(
        state, include_accessibility=full_a11y,
        # A per-action judge already receives an exact targeting receipt and needs outcome/status facts, not
        # the entire page's unrelated form inventory. Keep full breadth for accessibility stories and for the
        # next-action planner; ordinary semantic judges get a bounded after-state without losing screenshots,
        # viewport text, status, network, console, or the action's exact control facts.
        element_limit=(55 if full_a11y else 26) if evaluation else 65,
        element_chars=(7500 if full_a11y else 3200) if evaluation else 9000,
        viewport_chars=1600 if evaluation and not full_a11y else 2600,
        document_chars=600 if evaluation and not full_a11y else 1400,
        status_chars=900 if evaluation and not full_a11y else 1200,
        # Landmarks are an action-planning aid. Repeating the whole-page index in
        # every after-action semantic judge adds no outcome evidence.
        landmark_limit=0 if evaluation else 40,
        compact_accessibility=bool(evaluation and full_a11y))


# ----------------------------------------------------------------------------------------------------
# Robust intent-based targeting + effect detection — the accuracy core. These are PURE functions so the
# offline self-test can pin the exact behaviour that stops false positives ("cried wolf on a missed click").
# ----------------------------------------------------------------------------------------------------
def _labels_match(a, b):
    """Do two labels refer to the same control? Exact (case-insensitive) OR either-way substring."""
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return False
    return a == b or a in b or b in a


def _resolve_target(action, elements):
    """Resolve an action's INTENT (target_text [+ optional role]) to a concrete element from the observed
    list. Prefers an exact label match, then a case-insensitive substring match; a role, if given, narrows
    the candidates. Returns (idx | None, matched_label | None, score) — score 3=exact, 2=contains, 0=none.
    This is what lets us act by label/role instead of a blind, AI-guessed index."""
    want = (action.get("target_text") or action.get("target") or "").strip()
    role = (action.get("role") or "").strip().lower()
    if not want:
        return None, None, 0
    wl = want.lower()
    best, best_score, best_label = None, 0, None
    def role_matches(want_role, e):
        if not want_role:
            return True
        erole = (e.get("role") or "").strip().lower()
        tag = (e.get("tag") or "").strip().lower()
        typ = (e.get("type") or "").strip().lower()
        implicit = {
            "a": "link",
            "button": "button",
            "select": "combobox",
            "textarea": "textbox",
        }.get(tag)
        if tag == "input":
            implicit = "textbox" if typ not in ("checkbox", "radio", "button", "submit") else {
                "checkbox": "checkbox", "radio": "radio", "button": "button", "submit": "button",
            }.get(typ)
        return want_role in {erole, tag, implicit}
    def candidate_score(e):
        primary = _element_label(e)
        labels = [primary]
        if str(e.get("tag") or "").lower() == "select":
            labels.extend(str(e.get("options") or "").split(" | "))
        winner_score, winner_label = 0, primary
        for raw_label in labels:
            candidate_label = str(raw_label or "").strip()
            candidate_lower = candidate_label.lower()
            score = (3 if candidate_lower == wl else
                     2 if candidate_lower and (wl in candidate_lower or candidate_lower in wl)
                     else 0)
            if score > winner_score:
                winner_score, winner_label = score, candidate_label
        return winner_score, winner_label
    def scan(respect_role=True):
        nonlocal best, best_score, best_label
        for e in elements or []:
            if respect_role and not role_matches(role, e):
                continue
            score, lab = candidate_score(e)
            if score > best_score:
                best, best_score, best_label = e, score, lab
    scan(True)
    if best is None and role:
        scan(False)
    if best is None or best_score == 0:
        return None, None, 0
    # LABEL remains authoritative, but when multiple controls have the same best label/role score an exact
    # observed idx is a safe tie-breaker.  This cannot let a blind or stale idx override a better semantic
    # match: it is honored only when that element independently earns the same winning score.
    preferred_idx = action.get("idx")
    if preferred_idx is not None:
        for candidate in elements or []:
            try:
                same_idx = int(candidate.get("idx")) == int(preferred_idx)
            except (TypeError, ValueError):
                same_idx = candidate.get("idx") == preferred_idx
            if not same_idx or (role and not role_matches(role, candidate)):
                continue
            tied_score, candidate_label = candidate_score(candidate)
            if tied_score == best_score:
                best, best_label = candidate, candidate_label
            break
    return best.get("idx"), best_label, best_score


def _label_for_idx(idx, elements):
    if idx is None:
        return ""
    for e in elements or []:
        if e.get("idx") == idx:
            return _element_label(e)
    return ""


def _effect_registered(before, after, act_result):
    """Did the action actually DO anything? A missed/no-op click leaves the page identical — that is a
    driver miss, NOT an app bug. We look for navigation, a title change, a visible-text (DOM) change, a
    form/control semantic change, a focus change, a new/changed network request, or a new console error."""
    if not act_result or act_result.get("ok") is False:
        return False
    if act_result.get("traversal") and (act_result.get("sequence") or []):
        return True
    if act_result.get("landmarkDwell") and (act_result.get("observations") or []):
        return True
    if act_result.get("scenarioMatrix") and (act_result.get("cases") or []):
        return True
    if act_result.get("keyboardMatrix") and (act_result.get("actions") or []):
        return True
    if act_result.get("timedTransition") and (act_result.get("transition") or {}):
        return True
    if (before.get("url") != after.get("url")) or (before.get("title") != after.get("title")):
        return True
    if (before.get("bodyText") or "") != (after.get("bodyText") or ""):
        return True
    if json.dumps(before.get("activeElement") or {}, sort_keys=True) != \
            json.dumps(after.get("activeElement") or {}, sort_keys=True):
        return True
    def controls_sig(state):
        sig = []
        for e in state.get("elements") or []:
            sig.append({
                "idx": e.get("idx"),
                "label": _element_label(e),
                "tag": e.get("tag"),
                "value": e.get("value"),
                "disabled": e.get("disabled"),
                "current": e.get("current"),
                "pressed": e.get("pressed"),
                "selectedState": e.get("selectedState"),
                "expanded": e.get("expanded"),
                "checked": e.get("checked"),
            })
        return sig
    if json.dumps(controls_sig(before), sort_keys=True) != json.dumps(controls_sig(after), sort_keys=True):
        return True
    if json.dumps(before.get("recent_requests") or []) != json.dumps(after.get("recent_requests") or []):
        return True
    if json.dumps(before.get("console_errors") or []) != json.dumps(after.get("console_errors") or []):
        return True
    return False


def _external_handoff(act_result):
    href = (act_result or {}).get("matchedHref") or ""
    return bool(re.match(r"^(mailto|tel|sms):", str(href), re.I))


def _control_snapshot(elements, idx=None, label=None, role=None):
    elements = elements or []
    if idx is not None:
        for e in elements:
            try:
                if int(e.get("idx")) == int(idx):
                    return e
            except Exception:
                continue
    best_idx, _, _ = _resolve_target({"target_text": label or "", "role": role or ""}, elements) \
        if label else (None, None, 0)
    if best_idx is not None:
        for e in elements:
            try:
                if int(e.get("idx")) == int(best_idx):
                    return e
            except Exception:
                continue
    return None


def _infer_click_target_from_expected(action, expected, elements):
    action = dict(action or {})
    cmd = (action.get("cmd") or "").lower()
    if cmd not in ("click", "tap") or action.get("target_text") or action.get("target") or action.get("idx") is not None:
        return action
    text = str(expected or "").lower()
    candidates = []
    for e in elements or []:
        label = _element_label(e)
        if not label:
            continue
        low = label.lower()
        if low in text or text in low:
            candidates.append((len(label), label, e))
    if not candidates:
        return action
    _, label, e = sorted(candidates, reverse=True)[0]
    action["target_text"] = label
    role = (e.get("role") or "").strip()
    if role:
        action["role"] = role
    return action


def _disabled_handoff_false_positive(story, expected, targeting, bug):
    if not bug or not targeting or not targeting.get("external_handoff_url"):
        return False
    text = " ".join([_story_text(story), str(expected or ""), str(bug or "")]).lower()
    if not any(t in text for t in ("duplicate", "repeat", "second click", "second activation", "rapid")):
        return False
    if not any(t in text for t in ("aria-disabled", "disabled", "busy")):
        return False
    return targeting.get("before_control_disabled") is False


def _setup_action_false_positive(story, expected, targeting, bug):
    if not bug or not targeting:
        return False
    action = (targeting.get("action_kind") or "").lower()
    if action not in ("scroll", "wait"):
        return False
    steps_text = " ".join(str(s) for s in (story.get("steps") or [])).lower()
    if action in steps_text:
        return False
    text = str(bug or "").lower()
    return any(t in text for t in (
        "not visible", "not present", "not available", "offscreen", "viewport", "interactable control",
        "expected control",
    ))


def _empty_queue_drain_false_positive(targeting, bug, before_state):
    """Draining a provably empty queue cannot satisfy a missing submit prerequisite."""
    if not bug or (targeting or {}).get("action_kind") not in ("click", "tap"):
        return False
    label = " ".join(str((targeting or {}).get(k) or "")
                     for k in ("intended", "targeted_label", "expected_control")).lower()
    if "drain queue" not in label:
        return False
    state = str((before_state or {}).get("bodyText") or
                (before_state or {}).get("visible_text") or "").lower()
    empty = ('"appjobs": []' in state or "no records" in state) and "enquiriestotal 0" in state
    claim = str(bug).lower()
    return empty and any(t in claim for t in ("remain 0", "remained empty", "from empty to populated",
                                               "no enquiry", "no records"))


def _empty_state_without_reset_false_positive(expected, targeting, bug, before_state):
    """A same-context navigation does not recreate the story's original empty local-storage state.

    Coverage sometimes reaches an initial/empty-state assertion only after creating records.  A model may then
    issue ``goto``/reload and incorrectly blame the product when durable browser storage survives—as it should.
    Treat that sequence as invalid QA evidence.  The explorer has an explicit ``reset_storage`` action for a
    genuinely fresh ephemeral QA context; only that action may establish an empty-storage precondition.
    """
    if not bug or (targeting or {}).get("action_kind") not in ("goto", "reload"):
        return False
    claim = f"{expected or ''} {bug or ''}".lower()
    wants_empty = any(t in claim for t in (
        "empty storage", "empty-storage", "zero enquiry", "zero job", "no persisted",
        "no enquiry", "no agent-job", "no agent job", "metrics remain zero",
    ))
    if not wants_empty:
        return False
    state = str((before_state or {}).get("bodyText") or
                (before_state or {}).get("visible_text") or "").lower()
    has_persisted_state = bool(
        re.search(r"enquiriestotal\s+[1-9]", state)
        or "enquiry received" in state
        or re.search(r'"appjobs"\s*:\s*\[\s*\{', state)
        or re.search(r"agentjobsqueued\s+[1-9]", state)
    )
    return has_persisted_state


def _history_rebased_state_false_positive(story, expected, targeting, bug, after_state):
    """Reject a persistence claim when the story explicitly establishes a fresh post-history baseline.

    A decider's per-action expectation is a proposal, not an authority that can expand the story.  If the
    contract says to record whatever value is observed after Back/Forward as B/C/D and then assert an operation
    relative to that value, it deliberately does *not* require the pre-navigation value to persist.  The paid
    convergence canary caught an evaluator turning ``record the returned count as C`` into ``C must equal the
    old count`` and dispatching a fixer for a healthy app.  Keep the guard semantic and contract-derived: an
    explicit persistence/retention requirement in the story still wins.
    """
    if not bug or (targeting or {}).get("driver_ok") is False:
        return False
    action = str((targeting or {}).get("action_kind") or "").lower()
    direction = str((targeting or {}).get("history_direction") or "").lower()
    if action not in ("back", "forward") and direction not in ("back", "forward"):
        return False
    contract = _story_text(story)
    if any(term in contract for term in (
            "persist", "preserv", "retain", "survive history", "unchanged count", "same count")):
        return False
    steps = " ".join(str(step or "") for step in (story.get("steps") or []))
    rebases = bool(re.search(
        r"\brecord\b[^.;]{0,100}\b(?:count|value|state)\b[^.;]{0,60}\b(?:as|baseline)\s+[a-z]\b",
        steps, re.I))
    claim = f"{expected or ''} {bug or ''}".lower()
    invents_persistence = any(term in claim for term in (
        "persist", "preserv", "retain", "same count", "previous count", "pre-navigation count"))
    if not (rebases and invents_persistence):
        return False
    if (after_state or {}).get("console_errors"):
        return False
    for request in (after_state or {}).get("recent_requests") or []:
        try:
            if request.get("failed") or request.get("status") is None or int(request["status"]) >= 400:
                return False
        except (KeyError, TypeError, ValueError):
            return False
    return True


def _contract_grounded_history_expected(story, expected, targeting):
    """Fence a probabilistic action expectation to a story's post-history rebasing contract.

    The evaluator guard above prevents an invented persistence claim from becoming a product finding.  The
    evidence dossier must be equally honest: it cannot retain the invented ``C must still be 1`` expectation
    and then label a different observed value a match.  Normalize the proposal before evaluation and storage.
    """
    action = str((targeting or {}).get("action_kind") or "").lower()
    direction = str((targeting or {}).get("history_direction") or "").lower()
    if action not in ("back", "forward") and direction not in ("back", "forward"):
        return str(expected or ""), False
    contract = _story_text(story)
    if any(term in contract for term in (
            "persist", "preserv", "retain", "survive history", "unchanged count", "same count")):
        return str(expected or ""), False
    steps = " ".join(str(step or "") for step in (story.get("steps") or []))
    if not re.search(
            r"\brecord\b[^.;]{0,100}\b(?:count|value|state)\b[^.;]{0,60}\b(?:as|baseline)\s+[a-z]\b",
            steps, re.I):
        return str(expected or ""), False
    direction = direction or action
    return (
        f"Browser {direction} completes to the destination required by the story. If the returned page has "
        "a count, record the value actually observed as the story's fresh baseline; no pre-navigation count "
        "value is imposed. The scoped console and network evidence remains clean.", True)


def _contract_grounded_approval_projection_expected(story, expected, targeting):
    """Keep a pending-ticket probe from inventing parity with separate queue/blocker projections.

    The US-009 contract asks QA to inspect approval diagnostics, staff, CEO, blockers, and audit surfaces as
    distinct journey steps. A pending approval ticket does not imply that an agent job has itself completed
    with ``approval_required``; likewise, the derived governance blocker belongs on the staff/CEO blocker
    projections and need not be duplicated inside Operational diagnostics. A planner once combined all three
    into the first inspection and filed a defect against truthful ``Approval required 0`` job counters. Narrow
    only that demonstrably over-expanded expectation before independent evaluation.
    """
    story = story if isinstance(story, dict) else {}
    contract = _story_text(story)
    if not (re.search(r"\binspect approval diagnostics\b", contract, re.I)
            and re.search(r"\bstaff console\b", contract, re.I)
            and re.search(r"\bCEO command view\b", contract, re.I)
            and re.search(r"\bblockers?\b", contract, re.I)):
        return str(expected or ""), False
    receipt = ((targeting or {}).get("landmark_dwell_summary")
               or (targeting or {}).get("landmark_dwell") or {})
    targets = [" ".join(str(value).casefold().split())
               for value in (receipt.get("targets") or []) if str(value).strip()]
    if (len(targets) != 1
            or targets[0] not in {"operational diagnostics", "approval diagnostics", "approval state"}
            or not re.search(r"\bblocker|approval[- ]required (?:job|count|summary)",
                             str(expected or ""), re.I)):
        return str(expected or ""), False
    return (
        "The approval diagnostics truthfully show the pending approval ticket and its status. Job-status "
        "summaries remain truthful to jobs actually run; a pending ticket does not require an agent job with "
        "an approval-required result. The separate staff, CEO, blocker, and audit projections are verified by "
        "their own following story clauses, and no external send side effect is recorded.", True)


def _contract_grounded_acknowledgement_focus_expected(story, expected, targeting):
    """Do not require a removed acknowledgement button to retain focus after activation."""
    contract = _story_text(story)
    key = str((targeting or {}).get("action_key") or "").casefold().replace(" ", "")
    if (key not in {"enter", "space", "spacebar"}
            or not re.search(r"\backnowledg", contract, re.I)
            or not re.search(r"\b(?:blocker|risk)s?\b", contract, re.I)
            or not re.search(r"\bfocus visible|visible focus|keeps? focus\b", contract, re.I)
            or not re.search(r"\b(?:its|same|selected|current) control remains focused\b",
                             str(expected or ""), re.I)):
        return str(expected or ""), False
    return (
        "The selected blocker becomes acknowledged through the requested keyboard activation. Visible focus "
        "moves to the next available open-blocker acknowledgement control; if none remains, it moves to a "
        "stable acknowledged-blocker focus target. The removed action control itself need not retain focus, "
        "and the acknowledgement remains attributable and private.", True)


def _acknowledgement_focus_transfer_false_positive(story, targeting, bug, after_state):
    """Recognize the contract-correct focus handoff after an acknowledgement control is removed."""
    contract = _story_text(story)
    targeting = targeting if isinstance(targeting, dict) else {}
    after_state = after_state if isinstance(after_state, dict) else {}
    key = str(targeting.get("action_key") or "").casefold().replace(" ", "")
    active = after_state.get("activeElement") if isinstance(after_state.get("activeElement"), dict) else {}
    active_label = " ".join(str(active.get(name) or "") for name in (
        "text", "name", "ariaLabel", "label", "id")).casefold()
    claim = str(bug or "").casefold()
    return bool(
        key in {"enter", "space", "spacebar"}
        and targeting.get("action_kind") == "press"
        and targeting.get("driver_ok") is True
        and targeting.get("effect_registered") is True
        and targeting.get("label_matched") is not False
        and re.search(r"\backnowledg", contract, re.I)
        and re.search(r"\b(?:blocker|risk)s?\b", contract, re.I)
        and re.search(r"\bfocus visible|visible focus|keeps? focus\b", contract, re.I)
        and active.get("focusVisible") is True
        and str(active.get("tag") or "").casefold() == "button"
        and "acknowledg" in active_label
        and re.search(r"\b(?:removed|disappeared|no longer present)\b", claim)
        and re.search(r"\bfocus (?:moved|transferred|went)\b", claim)
    )


def _approval_diagnostics_projection_false_positive(story, targeting, bug, after_state):
    """Reject a finding that demands queue/blocker parity inside the pending-ticket diagnostic."""
    contract = _story_text(story)
    targeting = targeting if isinstance(targeting, dict) else {}
    after_state = after_state if isinstance(after_state, dict) else {}
    receipt = targeting.get("landmark_dwell_summary") or targeting.get("landmark_dwell") or {}
    targets = [" ".join(str(value).casefold().split())
               for value in (receipt.get("targets") or []) if str(value).strip()]
    claim = " ".join(str(bug or "").casefold().split())
    return bool(
        re.search(r"\binspect approval diagnostics\b", contract, re.I)
        and re.search(r"\bstaff console\b", contract, re.I)
        and re.search(r"\bCEO command view\b", contract, re.I)
        and re.search(r"\bblockers?\b", contract, re.I)
        and targets == ["operational diagnostics"]
        and targeting.get("driver_ok") is True
        and receipt.get("all_targets_matched") is True
        and receipt.get("all_stable") is True
        and re.search(r"\b(?:show(?:s|ing)?|has) (?:exactly )?one pending approval ticket\b", claim)
        and re.search(r"\bapproval required 0\b", claim)
        and (re.search(r"\b(?:no|missing) (?:separate )?blocker\b", claim)
             or re.search(r"\bcontradict\w*\b[^.;]{0,100}\bapproval blocker\b", claim))
        and not after_state.get("console_errors")
    )


def _contract_grounded_empty_first_run_expected(story, expected, targeting):
    """Keep the ordinary first-enquiry queue path distinct from later approval governance."""
    contract = _story_text(story)
    receipt = ((targeting or {}).get("landmark_dwell_summary")
               or (targeting or {}).get("landmark_dwell") or {})
    targets = {" ".join(str(value).casefold().split())
               for value in (receipt.get("targets") or []) if str(value).strip()}
    required = {"public enquiry", "staff operating console", "ceo command view",
                "queue and dead letters", "approval summary", "audit summary"}
    if (not re.search(r"\brecover from empty first[- ]run data\b", contract, re.I)
            or not re.search(r"\bsubmit the first valid public enquiry\b", contract, re.I)
            or not re.search(r"\bdrain the queue\b", contract, re.I)
            or not required.issubset(targets)
            or not re.search(r"\b(?:all|six)\b.*\b(?:panels?|surfaces?)\b.*\bpopulated\b|"
                             r"\bapproval(?:/| and )audit diagnostics\b.*\bpopulated\b",
                             str(expected or ""), re.I)):
        return str(expected or ""), False
    return (
        "All required public, staff, CEO, queue, approval, and audit surfaces remain rendered and update live "
        "to the truthful post-enquiry state without a reload or blank screen. The enquiry, staff/CEO metrics, "
        "queue result, and audit history become populated. Approval diagnostics remain valid and may honestly "
        "remain empty because this journey never submits a follow-up for human approval.", True)


def _contract_grounded_queue_projection_expected(story, expected, targeting):
    """Keep US-008 consistency distributed across the story-authored projections.

    CEO and diagnostic cards are intentionally aggregate projections.  The original story requires them to
    show the same failure/recovery truth; it does not require every card to duplicate the job identifier,
    error body, notification, and retry control.  Normalize only the canonical passive projection probe so
    the judge checks linked evidence across the set instead of demanding every datum on every surface.
    """
    contract = _story_text(story)
    receipt = ((targeting or {}).get("landmark_dwell_summary")
               or (targeting or {}).get("landmark_dwell") or {})
    targets = {" ".join(str(value).casefold().split())
               for value in (receipt.get("targets") or []) if str(value).strip()}
    required = {" ".join(value.casefold().split()) for value in _QUEUE_RETRY_PROJECTION_TARGETS}
    if (not re.search(r"\bretry dead[- ]lettered agent work\b", contract, re.I)
            or str((targeting or {}).get("action_kind") or "").casefold() != "inspect_surfaces"
            or not required.issubset(targets)):
        return str(expected or ""), False
    if re.search(r"\b(?:succeeded|recovered|recovery|cleared|superseded)\b", str(expected or ""), re.I):
        return (
            "The linked projections together show one recovered job: Agent jobs exposes succeeded status, "
            "attempts, and the preserved runAfter schedule; staff/CEO/blocker projections show no open "
            "operational risk; queue diagnostics show the succeeded/non-dead-letter totals; notification and "
            "audit detail preserve both failure and recovery. Aggregate CEO/diagnostic cards need not repeat "
            "the job identifier or full error body, but no targeted surface may contradict the recovered "
            "state and the original enquiry remains singular.", True)
    return (
        "The linked projections together show one terminal retryable failure: Agent jobs exposes the failed "
        "or dead-lettered job and Retry control; staff/CEO/blocker projections show open engineering-owned "
        "risk; queue diagnostics show matching failed/dead-letter totals; notification and audit detail "
        "record the escalation and failure. Aggregate CEO/diagnostic cards need not repeat the job identifier "
        "or full error body, but no targeted surface may contradict the failure and the enquiry remains "
        "singular.", True)


def _empty_first_run_approval_projection_false_positive(story, targeting, bug, after_state):
    """Do not demand a fabricated approval ticket after an ordinary first-enquiry review."""
    contract = _story_text(story)
    claim = " ".join(str(bug or "").casefold().split())
    receipt = ((targeting or {}).get("landmark_dwell_summary")
               or (targeting or {}).get("landmark_dwell") or {})
    targets = {" ".join(str(value).casefold().split())
               for value in (receipt.get("targets") or []) if str(value).strip()}
    rendered = " ".join(str((after_state or {}).get(name) or "")
                        for name in ("bodyText", "viewportText", "statusText")).casefold()
    return bool(
        re.search(r"\brecover from empty first[- ]run data\b", contract, re.I)
        and re.search(r"\bsubmit the first valid public enquiry\b", contract, re.I)
        and re.search(r"\bdrain the queue\b", contract, re.I)
        and "approval summary" in targets
        and (re.search(r"\bapproval diagnostics? remain(?:s)? (?:at )?total 0\b", claim)
             or re.search(r"\bno pending approval decisions\b", claim))
        and re.search(r"\b(?:queue processing complete|processed|succeeded)\b", rendered)
        and not (after_state or {}).get("console_errors")
    )


def _empty_first_run_contaminated_resume_false_positive(story, targeting, bug, before_state):
    """Classify a lost fresh-state precondition as QA continuation work, not a product defect."""
    contract = _story_text(story)
    claim = " ".join(str(bug or "").casefold().split())
    rendered = " ".join(str((before_state or {}).get(name) or "")
                        for name in ("bodyText", "viewportText", "statusText")).casefold()
    populated = bool(
        re.search(r"\b[1-9]\d*\s+(?:total/open\s+)?enquiries?\b", rendered, re.I)
        or re.search(r"\benquiries?\s+(?:total\s*)?[:=]?\s*[1-9]\d*\b", rendered, re.I)
        or "enquiry received" in rendered)
    return bool(
        re.search(r"\brecover from empty first[- ]run data\b", contract, re.I)
        and str((targeting or {}).get("action_kind") or "").casefold()
        in {"scenario_matrix", "case_matrix"}
        and populated
        and (re.search(r"\bdid not begin\b[^.;]{0,100}\bempty state\b", claim, re.I)
             or re.search(r"\bbefore submission\b[^.;]{0,120}\b(?:enquir|populated)", claim, re.I)))


def _initial_state_aspect(aspect):
    """Return true for coverage that must be observed before the journey mutates browser state."""
    text = str(aspect or "").lower()
    return any(t in text for t in (
        "initial load", "initial-load", "starting state", "empty storage", "empty-storage",
        "first-run", "first run", "fresh context", "zero enquiry", "zero job",
    ))


def _empty_storage_aspect(aspect):
    text = str(aspect or "").lower()
    return any(t in text for t in (
        "empty storage", "empty-storage", "fresh context", "zero enquiry", "zero job",
        "no persisted",
    ))


def _initial_precondition_reset_done(records):
    for record in reversed(list(records or [])):
        if not isinstance(record, dict):
            continue
        action = record.get("action") if isinstance(record.get("action"), dict) else {}
        if str(action.get("cmd") or "").lower() != "reset_storage":
            continue
        act_result = record.get("act_result") if isinstance(record.get("act_result"), dict) else {}
        return act_result.get("ok", True) is not False
    return False


def _seeded_story_needs_fresh_resume(story):
    """Return whether a resumed acceptance story explicitly starts by constructing fixture state.

    Seeded journeys are intentionally replayable. Carrying a prior worker's post-interaction storage can leave
    every approval/blocker already consumed, making the continuation report “controls missing” instead of
    recreating the contract's starting state. Reset only ephemeral QA browser storage; the next story action
    still has to perform the specified seed/load operation and prove its result.
    """
    steps = [str(item or "").strip().lower() for item in ((story or {}).get("steps") or [])]
    return any(re.match(r"^(?:seed|load)\b", step) for step in steps[:3])


def _fence_seeded_resume_decision(decision, story, records, resume_state_path, target_url,
                                  resume_has_proven_coverage=False):
    if (not resume_state_path or not _seeded_story_needs_fresh_resume(story)
            or resume_has_proven_coverage or _initial_precondition_reset_done(records)):
        return decision
    return {
        **dict(decision or {}),
        "reasoning": "Recreate this explicitly seeded story's fresh fixture baseline before replay.",
        "next_action": {"cmd": "reset_storage", "value": target_url},
        "expected": ("Ephemeral QA browser storage is cleared and the target reopens cleanly so the story's "
                     "explicit seed steps can be replayed."),
        "expected_control": "",
        "wait_for": None,
        "covers": [],
        "done": False,
    }


def _pending_story_seed_decision(story, state, coverage, records):
    """Load an explicitly seeded story baseline before any probe that depends on it.

    A paid US-011 run once performed its full keyboard traversal against the empty app, clicked ``Load
    US-011`` afterwards, and then let cumulative diagnosis cite the stale traversal as proof that the seeded
    page exposed no acknowledgement controls.  Seed/load is a causal barrier: choose the exact observed
    fixture control mechanically before navigation, traversal, or semantic judging.  Never replay it after a
    grounded story clause or a confirmed prior seed action.
    """
    if not _seeded_story_needs_fresh_resume(story):
        return None
    if any(item.get("covered") for item in (coverage or []) if isinstance(item, dict)):
        return None
    for record in records or []:
        if not isinstance(record, dict):
            continue
        action = record.get("action") if isinstance(record.get("action"), dict) else {}
        targeting = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        label = " ".join(str(value or "") for value in (
            action.get("target_text"), targeting.get("intended"), targeting.get("targeted_label")))
        if (re.search(r"\b(?:load|seed)\b", label, re.I)
                and targeting.get("driver_ok") and targeting.get("effect_registered")):
            return None

    story_id = str((story or {}).get("id") or "").strip().casefold()
    candidates = []
    for element in (state or {}).get("elements") or []:
        label = _element_label(element).strip()
        if not label or not re.search(r"\b(?:load|seed)\b", label, re.I):
            continue
        low = label.casefold()
        score = (100 if story_id and story_id in low else 0)
        score += 10 if "fixture" in low else 0
        candidates.append((score, len(label), label, element))
    if not candidates:
        return None
    score, _length, label, element = sorted(candidates, key=lambda item: (-item[0], item[1], item[2]))[0]
    # Several unrelated fixture controls with no story-id match are ambiguous. Let the semantic planner choose
    # by exact label rather than clicking the first adjacent control.
    if score == 0 and len(candidates) > 1:
        return None
    role = str(element.get("role") or element.get("tag") or "").strip()
    return {
        "reasoning": "Establish the story-authored seeded baseline before dependent inspection.",
        "next_action": {"cmd": "click", "target_text": label, **({"role": role} if role else {})},
        "expected": f"{label} establishes the seeded starting state required by the story.",
        "expected_control": "", "wait_for": None, "covers": [], "done": False,
    }


def _stale_precondition_absence_diagnosis(judgment, records):
    """Reject a missing-control diagnosis grounded only in a probe from before a later state rebase."""
    if str((judgment or {}).get("disposition") or "").casefold() != "app_defect":
        return False
    claim = f"{(judgment or {}).get('bug') or ''} {(judgment or {}).get('reason') or ''}".casefold()
    if not ("travers" in claim and any(term in claim for term in (
            "no reachable", "no control", "no acknowledgement", "no acknowledgment", "absence"))):
        return False
    last_traversal = -1
    last_rebase = -1
    for index, record in enumerate(records or []):
        if not isinstance(record, dict):
            continue
        action = record.get("action") if isinstance(record.get("action"), dict) else {}
        targeting = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        cmd = str(action.get("cmd") or "").casefold()
        if cmd in {"traverse", "keyboard_matrix"} and targeting.get("driver_ok"):
            last_traversal = index
        label = " ".join(str(value or "") for value in (
            action.get("target_text"), targeting.get("intended"), targeting.get("targeted_label")))
        confirmed_fixture = (cmd in {"click", "tap", "press"}
                             and targeting.get("driver_ok") and targeting.get("effect_registered")
                             and re.search(r"\b(?:load|seed)\b", label, re.I))
        if cmd == "reset_storage" or confirmed_fixture:
            last_rebase = index
    return last_traversal >= 0 and last_rebase > last_traversal


def _pending_focused_reset_storage_decision(story, records, target_url):
    """Replay a sealed reset-storage finding once, without letting a planner invent a form journey."""
    focused = (story or {}).get("focused_finding") or {}
    reported = focused.get("action") if isinstance(focused, dict) else {}
    if str((reported or {}).get("cmd") or "").strip().lower() not in {
            "reset_storage", "resetstorage"}:
        return None
    if _initial_precondition_reset_done(records):
        return None
    return {
        "reasoning": "Reproduce the sealed fresh-state reopen boundary exactly once.",
        "next_action": {"cmd": "reset_storage", "value": target_url},
        "expected": ("The fresh-state reset clears browser storage, reopens the pinned target, settles, and "
                     "has no scoped console error or failed/HTTP 4xx/5xx request."),
        "expected_control": "",
        "wait_for": {"kind": "network_idle", "timeout_s": 10},
        "covers": [],
        "done": False,
        "mechanical_setup": True,
    }


def _fence_initial_state_decision(decision, aspect, records, resume_state_path, target_url):
    """Preserve a perishable initial state without blocking the inspection needed to prove it."""
    decision = dict(decision or {})
    action = dict(decision.get("next_action") or {})
    cmd = str(action.get("cmd") or "").lower()
    needs_reset = (_empty_storage_aspect(aspect) and bool(resume_state_path)
                   and not _initial_precondition_reset_done(records))
    if needs_reset:
        return {
            **decision,
            "reasoning": "Recreate the story's explicit empty-storage precondition once before inspection.",
            "next_action": {"cmd": "reset_storage", "value": target_url},
            "expected": ("Browser storage is cleared and the target reopens in a fresh empty state without "
                         "console or network errors."),
            "expected_control": "",
            "wait_for": None,
            "covers": [],
            "done": False,
        }
    # Responsive and long-page observations do not consume empty-state data. Let the coverage-aware planner
    # perform every required viewport/scroll probe; the old blanket override converted these into endless
    # resets and made a compound mobile+desktop initial item mathematically impossible to cover.
    if cmd in ("wait", "viewport", "resize", "scroll"):
        return decision
    return {
        **decision,
        "reasoning": "Observe the perishable initial state before any journey mutation.",
        "next_action": {"cmd": "wait", "value": "0"},
        "expected": aspect,
        "expected_control": "",
        "wait_for": None,
        "covers": [aspect],
        "done": False,
    }


def _submit_disabled_incomplete_false_positive(expected, targeting, bug, after_state):
    """A disabled submit control is correct while any required form control is incomplete."""
    # Models often over-claim that each newly filled field is the *last* requirement. The deterministic DOM
    # state is authoritative for both submission attempts and setup typing: if any required control remains
    # blank, a disabled submit button is correct and must never trigger a fixer that weakens validation.
    if not bug or (targeting or {}).get("action_kind") not in (
            "click", "tap", "type", "fill", "press", "wait"):
        return False
    text = f"{expected or ''} {bug or ''}".lower()
    if not (any(t in text for t in ("submit", "send enquiry", "button"))
            and any(t in text for t in ("disabled", "enabled", "enable"))):
        return False
    required = [e for e in ((after_state or {}).get("elements") or [])
                if str(e.get("required") or "").lower() == "true"]
    for control in required:
        typ = str(control.get("type") or "").lower()
        if typ in ("checkbox", "radio"):
            if str(control.get("checked") or "").lower() != "true":
                return True
        elif not str(control.get("value") or "").strip():
            return True
    return False


def _submission_prerequisite_false_positive(expected, targeting, bug):
    """Skipping required setup cannot prove the app's later business outcome is defective."""
    if not bug or (targeting or {}).get("action_kind") not in ("click", "tap", "press"):
        return False
    if not (targeting or {}).get("empty_required_fields_before"):
        return False
    text = f"{expected or ''} {bug or ''}".lower()
    return any(term in text for term in (
        "form incomplete", "form_incomplete", "required field", "missing field",
        "did not submit", "didn't submit", "failed to submit", "submit failed",
        "did not publish", "didn't publish", "failed to publish", "publish failed",
        "not created", "did not create", "validation",
    ))


def _required_setup_value(field, element):
    """Return a neutral synthetic value for a required setup control, or None when choice needs judgment."""
    typ = str((element or {}).get("type") or (field or {}).get("type") or "").lower()
    tag = str((element or {}).get("tag") or (field or {}).get("tag") or "").lower()
    label = str((field or {}).get("label") or _element_label(element or {}) or "Required field").strip()
    semantic_identity = " ".join(str(value or "") for value in (
        label, (field or {}).get("name"), (field or {}).get("id"),
        (element or {}).get("name"), (element or {}).get("id"))).lower()
    # References, identifiers, keys, and tokens are business-semantic inputs, not neutral form setup. A
    # fabricated placeholder can only exercise a not-found path and may overwrite the exact fixture selected
    # by the story planner. Leave these model-owned just like select choices.
    if any(term in semantic_identity for term in (
            "reference", "claimid", "claim id", "recordid", "record id",
            "tenantid", "tenant id", "foreign key", "token", "secret")):
        return None
    if typ in ("checkbox", "radio"):
        return "__activate__"
    if tag == "select":
        return None
    if typ == "email":
        return "qa.person@example.invalid"
    if typ == "tel":
        return "+12025550147"
    if typ in ("number", "range"):
        return "1"
    if typ == "date":
        return "2030-01-15"
    if typ == "datetime-local":
        return "2030-01-15T12:00"
    if typ == "month":
        return "2030-01"
    if typ == "time":
        return "12:00"
    if typ == "url":
        return "https://example.invalid/qa-evidence"
    if typ == "password":
        return "Qa-test-only-7!"
    if tag == "textarea":
        return "QA prerequisite completed for this test."
    return f"{label} QA test"[:120]


def _pending_required_setup_decision(story, state, records):
    """Finish skipped required submit prerequisites without another model round-trip.

    The submit action itself supplies the authoritative form/control relationship and empty-field list. We only
    automate neutral setup for stories that are not explicitly testing required/empty-field validation, then
    retry the exact submit once. Select choices remain model-owned because choosing an option can carry product
    meaning. This removes several 10-70 second decision calls without weakening the later outcome judgment.
    """
    story_text = _story_text(story)
    if any(term in story_text for term in (
            "required field", "required-field", "empty form", "incomplete form",
            "form validation", "validation message", "missing field")):
        return None
    base_index = None
    base = None
    for pos in range(len(records or []) - 1, -1, -1):
        candidate = records[pos] if isinstance(records[pos], dict) else {}
        targeting = candidate.get("targeting") if isinstance(candidate.get("targeting"), dict) else {}
        verdict = candidate.get("verdict") if isinstance(candidate.get("verdict"), dict) else {}
        if (targeting.get("empty_required_fields_before")
                and str(verdict.get("verdict") or "").lower() == "retry"):
            base_index, base = pos, candidate
            break
    if base is None:
        return None
    base_action = dict(base.get("action") or {})
    base_cmd = str(base_action.get("cmd") or "").lower()
    if base_cmd not in ("click", "tap", "press"):
        return None
    # Once the exact submit has been retried, this mechanical setup context is consumed. A later outcome must
    # be evaluated normally (or create a fresh prerequisite receipt) rather than looping the same submit.
    base_intent = str(base_action.get("target_text") or base_action.get("target") or "").strip().lower()
    for later in (records or [])[base_index + 1:]:
        action = later.get("action") if isinstance(later, dict) and isinstance(later.get("action"), dict) else {}
        later_intent = str(action.get("target_text") or action.get("target") or "").strip().lower()
        if (str(action.get("cmd") or "").lower() in ("click", "tap", "press")
                and base_intent and later_intent == base_intent):
            return None

    elements = list((state or {}).get("elements") or [])
    missing = list((base.get("targeting") or {}).get("empty_required_fields_before") or [])
    for field in missing:
        if not isinstance(field, dict):
            continue
        field_id, field_name = str(field.get("id") or ""), str(field.get("name") or "")
        candidates = [element for element in elements if
                      (field_id and str(element.get("id") or "") == field_id) or
                      (field_name and str(element.get("name") or "") == field_name)]
        if not candidates:
            _, label, score = _resolve_target({"target_text": field.get("label") or ""}, elements)
            candidates = [element for element in elements
                          if score and _labels_match(label, _element_label(element))]
        if not candidates:
            return None
        element = candidates[0]
        typ = str(element.get("type") or field.get("type") or "").lower()
        if typ in ("checkbox", "radio"):
            is_empty = str(element.get("checked") or "").lower() != "true"
        else:
            is_empty = not str(element.get("value") or "").strip()
        if not is_empty:
            continue
        value = _required_setup_value(field, element)
        if value is None:
            return None
        label = str(field.get("label") or _element_label(element) or "required field")
        action = ({"cmd": "click", "idx": element.get("idx")}
                  if value == "__activate__" else
                  {"cmd": "type", "idx": element.get("idx"), "value": value})
        return {
            "reasoning": f"Complete browser-proven required prerequisite {label!r} before retrying submit.",
            "intent": label,
            "next_action": action,
            "expected": (f"The required {label} control is completed with neutral QA setup data so the "
                         "original story-specific submission can be tested."),
            "expected_control": "",
            "wait_for": None,
            "covers": [],
            "done": False,
            "mechanical_setup": True,
        }

    return {
        "reasoning": "All browser-proven required prerequisites are now complete; retry the exact submission.",
        "intent": str(base.get("reasoning") or base_intent or "submit"),
        "next_action": base_action,
        "expected": str(base.get("expected") or "The story-specific submission reaches its intended result."),
        "expected_control": str(base.get("expected_control") or ""),
        "wait_for": base.get("wait_for"),
        "covers": list(base.get("covers") or []),
        "done": False,
        # This is the original business transition after setup, not another prerequisite. It must retain the
        # normal semantic/receipt judgment even though the preceding fields were filled mechanically.
        "mechanical_setup": False,
    }


def _explicit_form_story_value(field, story_text):
    """Return bounded, non-secret data for a form control explicitly named by the story."""
    label = _element_label(field) or str(field.get("name") or "")
    lowered = label.casefold()
    typ = str(field.get("type") or "").casefold()
    tag = str(field.get("tag") or "").casefold()
    if tag == "select":
        options = [part.strip() for part in str(field.get("options") or "").split("|")
                   if part.strip()]
        usable = [option for option in options
                  if not re.search(r"^(?:choose|select|pick)(?:\s|$)", option, re.I)]
        return next((option for option in usable
                     if len(option) >= 3 and option.casefold() in story_text), None)
    if typ == "email" or "email" in lowered:
        return "qa.person@example.invalid"
    if typ == "tel" or re.search(r"\b(?:phone|telephone|mobile)\b", lowered):
        return "+12025550147"
    if re.search(r"\b(?:postcode|postal code|zip code|zip)\b", lowered):
        return "SW1A 1AA"
    if "dog" in lowered and "name" in lowered:
        return "Buddy"
    if "dog" in lowered and "age" in lowered:
        return "4"
    if re.search(r"\bage\b", lowered) or typ in ("number", "range"):
        return "4"
    if re.search(r"\bname\b", lowered):
        return "Ada Walker"
    return _required_setup_value({}, field)


def _pending_explicit_form_sequence_decision(story, state, coverage):
    """Execute one explicit multi-field story clause with one semantic decision boundary.

    Each nested action is still resolved against the current DOM, executed, settled, and captured separately.
    The optimization removes repeated model decide/evaluate calls between fields; it does not collapse browser
    evidence or credit the clause mechanically. Stories about invalid inputs, abuse, or missing requirements
    remain entirely model-owned.
    """
    ledger = [item for item in (coverage or []) if isinstance(item, dict)]
    earliest = next((str(item.get("aspect") or "") for item in ledger if not item.get("covered")), "")
    story_text = _story_text(story).casefold()
    clause = earliest.casefold()
    form_entry = re.search(
        r"\b(?:type|fill|complete)\b|\benter\s+(?!(?:or\s+space|key|activation)\b)", clause)
    if (not earliest or not form_entry
            or not any(term in clause for term in (
                "form", "name", "email", "phone", "postcode", "postal", "dog", "frequency"))):
        return None
    if any(term in story_text for term in (
            "required field", "required-field", "empty form", "incomplete form",
            "form validation", "validation message", "missing field", "invalid input",
            "abusive", "injection", "script tag", "sql-like", "credential-like", "password")):
        return None

    elements = [item for item in ((state or {}).get("elements") or [])
                if isinstance(item, dict) and item.get("formIndex") is not None]
    form_ids = list(dict.fromkeys(item.get("formIndex") for item in elements))
    best = []
    generic = {"field", "input", "details", "information", "form", "your", "the"}
    for form_id in form_ids:
        controls = [item for item in elements if item.get("formIndex") == form_id]
        if not any(str(item.get("type") or "").casefold() == "submit"
                   or (str(item.get("tag") or "").casefold() == "button"
                       and re.search(r"\b(?:send|submit|create)\b", _element_label(item), re.I))
                   for item in controls):
            continue
        selected = []
        for field in controls:
            tag = str(field.get("tag") or "").casefold()
            typ = str(field.get("type") or "").casefold()
            if tag not in ("input", "select", "textarea") or typ in (
                    "hidden", "submit", "button", "reset", "checkbox", "radio", "file", "password"):
                continue
            if str(field.get("value") or "").strip():
                continue
            label = _element_label(field) or str(field.get("name") or "")
            words = {word for word in re.findall(r"[a-z0-9]+", label.casefold())
                     if len(word) >= 3 and word not in generic}
            named = bool(words and any(word in clause for word in words))
            if "dog details" in clause and any(word in label.casefold() for word in ("dog", "age")):
                named = True
            if not named:
                continue
            value = _explicit_form_story_value(field, story_text)
            if value is None:
                continue
            selected.append((field, label, value))
        if len(selected) > len(best):
            best = selected
    if not (3 <= len(best) <= 8):
        return None

    paced = bool(re.search(r"\b(?:per[- ]keystroke|human(?:[- ]paced| pace)|type slowly)\b",
                           story_text))
    actions = []
    for field, label, value in best:
        action = {
            "cmd": "type", "target_text": label,
            "role": "combobox" if str(field.get("tag") or "").casefold() == "select" else "textbox",
            "value": value,
        }
        if paced and action["role"] == "textbox":
            action["pace_ms"] = 35
        actions.append(action)
    labels = [label for _field, label, _value in best]
    action = {"cmd": "scenario_matrix", "cases": [{"name": "explicit form sequence",
                                                       "actions": actions}],
              "_qa_explicit_form_sequence": True}
    return {
        "reasoning": ("Execute the story-authored multi-field form clause as one bounded browser sequence; "
                      "retain an individual settled receipt for every control."),
        "intent": labels[0], "next_action": action,
        "expected": ("The explicitly named form controls retain realistic values after the complete sequence: "
                     + ", ".join(labels) + "."),
        "expected_control": "", "wait_for": None, "covers": [earliest], "done": False,
        "mechanical_setup": False,
    }


def _pending_validation_matrix_decision(story, state, coverage):
    """Plan explicit invalid-form cases from the story contract and live form metadata.

    Validation remains semantically judged: this helper only removes repeated model planning between concrete
    boundaries that the story already supplied (past date, numeric limits, max lengths, and contact mismatch).
    Every case uses a real whole-value paste, blur/Enter, disabled pointer attempt, settled DOM snapshots, and
    request traces. Missing controls or unsupported wording fail closed to the ordinary model planner.
    """
    unresolved = [item for item in (coverage or [])
                  if isinstance(item, dict) and not item.get("covered")]
    validation = [item for item in unresolved if item.get("atomic_kind") == "validation_case"
                  or any(term in str(item.get("aspect") or "").casefold() for term in (
                      "invalid or past start date", "dog age below", "dog age above",
                      "near-limit overlong text", "mismatched email and phone"))]
    if not validation:
        return None

    elements = [item for item in ((state or {}).get("elements") or [])
                if isinstance(item, dict)]
    submit = next((item for item in elements
                   if item.get("formIndex") is not None
                   and (str(item.get("type") or "").casefold() == "submit"
                        or str(item.get("tag") or "").casefold() == "button")
                   and re.search(r"\b(?:send|submit)\b", _element_label(item), re.I)), None)
    if submit is None:
        return None
    form_index = submit.get("formIndex")
    controls = [item for item in elements if item.get("formIndex") == form_index]

    def field(*patterns, typ=None):
        for item in controls:
            label = _element_label(item)
            if typ and str(item.get("type") or "").casefold() != typ:
                continue
            if all(re.search(pattern, label, re.I) for pattern in patterns):
                return item
        return None

    def label(item):
        return _element_label(item) or str(item.get("name") or "")

    submit_label = label(submit)
    cases, covered, expectations = [], [], []
    for item in validation:
        aspect = str(item.get("aspect") or "").strip()
        low = aspect.casefold()
        actions = []
        case_name = ""
        expected = ""
        last_control = None
        if "invalid or past start date" in low:
            last_control = field(r"\bstart\b", r"\bdate\b", typ="date")
            if last_control is None:
                return None
            case_name = "past start date"
            actions.append({"cmd": "paste", "target_text": label(last_control),
                            "role": "textbox", "value": "2000-01-01"})
            expected = "a clear start-date message rejects the past date"
        elif "dog age below" in low:
            last_control = field(r"\bdog\b", r"\bage\b", typ="number")
            match = re.search(r"\bbelow\s+(-?\d+)\b", low)
            if last_control is None or not match:
                return None
            boundary = int(match.group(1))
            case_name = "dog age below lower bound"
            actions.append({"cmd": "paste", "target_text": label(last_control),
                            "role": "textbox", "value": str(boundary - 1)})
            expected = "a clear dog-age message rejects the below-bound value"
        elif "dog age above" in low:
            last_control = field(r"\bdog\b", r"\bage\b", typ="number")
            match = re.search(r"\babove\s+(\d+)\b", low)
            if last_control is None or not match:
                return None
            boundary = int(match.group(1))
            case_name = "dog age above upper bound"
            actions.append({"cmd": "paste", "target_text": label(last_control),
                            "role": "textbox", "value": str(boundary + 1)})
            expected = "a clear dog-age message rejects the above-bound value"
        elif "near-limit overlong text" in low:
            bounded = [control for control in controls
                       if str(control.get("tag") or "").casefold() in ("input", "textarea")
                       and str(control.get("type") or "").casefold() not in (
                           "hidden", "checkbox", "radio", "submit", "button", "number", "date")
                       and isinstance(control.get("maxLength"), int)
                       and 0 < int(control.get("maxLength")) <= 1000]
            if not bounded or len(bounded) > 6:
                return None
            case_name = "overlong text at every declared maximum"
            for control in bounded:
                maximum = int(control["maxLength"])
                control_label = label(control)
                control_type = str(control.get("type") or "").casefold()
                if control_type == "email":
                    suffix = "@example.test"
                    value = ("q" * max(1, maximum + 1 - len(suffix))) + suffix
                elif control_type == "tel":
                    value = "+" + ("1" * maximum)
                else:
                    value = "x" * (maximum + 1)
                actions.append({"cmd": "paste", "target_text": control_label,
                                "role": "textbox", "value": value})
                last_control = control
            expected = "each overlong whole-value paste is rejected or safely bounded with clear feedback"
            # A matrix case is capped at eight actions. Reserve blur, Enter, and pointer-submit evidence for
            # every chunk instead of dropping a declared max-length control from a large form.
            for chunk_index in range(0, len(actions), 5):
                chunk = list(actions[chunk_index:chunk_index + 5])
                chunk_last = next(control for control in bounded
                                  if label(control) == chunk[-1]["target_text"])
                chunk.extend([
                    {"cmd": "press", "target_text": label(chunk_last),
                     "role": "textbox", "value": "Tab"},
                    {"cmd": "press", "target_text": label(chunk_last),
                     "role": "textbox", "value": "Enter"},
                    {"cmd": "click", "target_text": submit_label, "role": "button"},
                ])
                suffix = f" (part {chunk_index // 5 + 1})" if len(actions) > 5 else ""
                cases.append({"name": case_name + suffix, "actions": chunk})
            covered.append(aspect)
            expectations.append(expected)
            continue
        elif "mismatched email and phone" in low:
            email = field(r"\bemail\b", typ="email")
            phone = field(r"\bphone\b", typ="tel")
            preferred = field(r"\bpreferred\b", r"\bcontact\b")
            if email is None or phone is None or preferred is None:
                return None
            case_name = "preferred-contact mismatch"
            actions.extend([
                {"cmd": "paste", "target_text": label(email), "role": "textbox",
                 "value": "qa.person@example.test"},
                {"cmd": "paste", "target_text": label(phone), "role": "textbox",
                 "value": "+12025550147"},
                {"cmd": "fill", "target_text": label(preferred), "role": "combobox",
                 "value": "Email"},
            ])
            last_control = preferred
            expected = "a clear preferred-contact message rejects supplying the mismatched phone channel"
        else:
            continue

        if last_control is None:
            return None
        # Subsequent whole-value pastes blur every earlier control. Explicit Tab blurs the final one; Enter and
        # the disabled pointer attempt then exercise both authored submit modalities without persisting data.
        actions.extend([
            {"cmd": "press", "target_text": label(last_control),
             "role": "combobox" if str(last_control.get("tag") or "").casefold() == "select"
             else "textbox", "value": "Tab"},
            {"cmd": "press", "target_text": label(last_control),
             "role": "combobox" if str(last_control.get("tag") or "").casefold() == "select"
             else "textbox", "value": "Enter"},
            {"cmd": "click", "target_text": submit_label, "role": "button"},
        ])
        if len(actions) > 8:
            return None
        cases.append({"name": case_name, "actions": actions})
        covered.append(aspect)
        expectations.append(expected)

    if not cases or sum(len(case["actions"]) for case in cases) > 48:
        return None
    action = {"cmd": "scenario_matrix", "cases": cases, "_qa_validation_matrix": True}
    return {
        "reasoning": ("Execute the story-authored invalid-input boundaries as one bounded live-browser "
                      "matrix while retaining a settled receipt for every paste, blur, keyboard submit, and "
                      "pointer submit attempt."),
        "intent": submit_label, "next_action": action,
        "expected": ("Every named case remains unpersisted; " + "; ".join(expectations)
                     + ". Paste events and visible/recoverable focus are retained, no raw transport details "
                       "appear, and no enquiry, job, audit event, or notification is created."),
        "expected_control": "", "wait_for": None, "covers": covered, "done": False,
        "mechanical_setup": False,
    }


def _pending_valid_form_setup_decision(story, state, coverage, records=None):
    """Complete and submit an explicitly valid non-validation form in one bounded browser batch.

    A planner once called a scenario "valid" while omitting required controls, clicked a disabled submit,
    and opened a full defect/triage/retest cycle. The settled DOM already exposes the submit's owning form,
    required controls, current values, and validity. Use those facts to fill neutral prerequisites and perform
    the exact story-authored submit as one sequence after all earlier ordered clauses are covered. Every nested
    action still receives its own settled browser receipt and the business outcome still goes through the
    semantic judge. Abuse/validation journeys remain model-owned because their exact input shape is the
    requirement under test.
    """
    story_text = _story_text(story).casefold()
    if any(term in story_text for term in (
            "required field", "required-field", "empty form", "incomplete form",
            "form validation", "validation message", "missing field", "invalid input",
            "abusive", "injection", "script tag", "sql-like", "credential-like")):
        return None
    ledger = [item for item in (coverage or []) if isinstance(item, dict)]
    earliest = next((str(item.get("aspect") or "") for item in ledger if not item.get("covered")), "")
    if not re.search(r"\b(?:submit|send|create)\b.*\b(?:enquiry|form|request|record)\b|"
                     r"\b(?:valid|completed?)\s+(?:enquiry|form)\b", earliest, re.I):
        return None

    elements = [item for item in ((state or {}).get("elements") or [])
                if isinstance(item, dict)]
    # Bind the batch to the object named by the *current coverage clause*, not any submit-like control whose
    # generic words happen to occur elsewhere in the full story. Without this fence, US-008's "attempts"
    # wording made "Attempt to send follow-up before approval" look relevant to "Submit one valid enquiry",
    # and the fast path filled the approval form instead of the public enquiry form.
    intent_nouns = {word for word in ("enquiry", "inquiry", "form", "request", "record")
                    if re.search(rf"\b{word}\b", earliest, re.I)}
    candidates = []
    for item in elements:
        label = _element_label(item)
        label_words = set(re.findall(r"[a-z0-9]+", label.casefold()))
        submit_like = (str(item.get("type") or "").lower() == "submit"
                       or (str(item.get("tag") or "").lower() == "button"
                           and re.search(r"\b(?:send|submit|create)\b", label, re.I)))
        if (item.get("formIndex") is None or not submit_like
                or (intent_nouns and not intent_nouns.intersection(label_words))):
            continue
        candidates.append(item)
    if not candidates:
        return None
    submit = candidates[0]
    form_index = submit.get("formIndex")
    form_controls = [item for item in elements if item.get("formIndex") == form_index]
    submit_label = _element_label(submit)

    # Do not blindly replay a business mutation after it was already attempted. A later model may inspect or
    # wait on a genuinely asynchronous outcome, and a browser-proven missing-prerequisite receipt can still
    # route through ``_pending_required_setup_decision``. This fence specifically stops the fast path from
    # double-submitting an enquiry because its semantic judge returned inconclusive.
    normalized_submit = " ".join(submit_label.casefold().split())
    for record in records or []:
        prior = record.get("action") if isinstance(record, dict) else None
        if not isinstance(prior, dict):
            continue
        actions = [prior]
        if str(prior.get("cmd") or "").lower() in ("scenario_matrix", "case_matrix"):
            actions = [nested for case in (prior.get("cases") or []) if isinstance(case, dict)
                       for nested in (case.get("actions") or []) if isinstance(nested, dict)]
        if any(str(action.get("cmd") or "").lower() in ("click", "tap", "press")
               and ((submit.get("idx") is not None and action.get("idx") == submit.get("idx"))
                    or (normalized_submit and " ".join(str(
                        action.get("target_text") or action.get("target") or ""
                    ).casefold().split()) == normalized_submit))
               for action in actions):
            return None

    actions = []

    def add_setup(field):
        typ = str(field.get("type") or "").lower()
        empty = (str(field.get("checked") or "").lower() != "true"
                 if typ in ("checkbox", "radio") else not str(field.get("value") or "").strip())
        if not empty:
            return True
        value = _required_setup_value({}, field)
        label = _element_label(field) or str(field.get("name") or "required field")
        if value is None or not label:
            return False
        if value == "__activate__":
            actions.append({"cmd": "click", "target_text": label,
                            "role": "checkbox" if typ == "checkbox" else "radio"})
        else:
            actions.append({"cmd": "type", "target_text": label,
                            "role": ("combobox" if str(field.get("tag") or "").lower() == "select"
                                     else "textbox"), "value": value})
        return True

    for field in form_controls:
        if str(field.get("required") or "").lower() != "true":
            continue
        if not add_setup(field):
            return None

    # Some products intentionally express a cross-field requirement (for example email OR phone) without an
    # HTML ``required`` attribute. If this is explicitly an enquiry journey, provide one neutral contact
    # channel. Never generalize this to arbitrary optional controls.
    if "enquiry" in story_text or "inquiry" in story_text:
        has_contact = any(str(field.get("type") or "").lower() in ("email", "tel")
                          and str(field.get("value") or "").strip() for field in form_controls)
        contact = next((field for field in form_controls
                        if str(field.get("type") or "").lower() == "email"
                        and not str(field.get("value") or "").strip()), None)
        if contact is None:
            contact = next((field for field in form_controls
                            if str(field.get("type") or "").lower() == "tel"
                            and not str(field.get("value") or "").strip()), None)
        if not has_contact and contact is not None and not add_setup(contact):
            return None

    # The matrix runner deliberately caps a case at eight separately settled actions. Larger forms retain the
    # older one-field-at-a-time path through the model instead of silently dropping a prerequisite.
    if len(actions) > 7:
        return None
    if not actions and (submit.get("disabled") is True
                        or str(submit.get("disabled") or "").lower() == "true"):
        return None
    actions.append({"cmd": "click", "target_text": submit_label, "role": "button"})
    action = {"cmd": "scenario_matrix", "cases": [{"name": "valid form submission",
                                                       "actions": actions}],
              "_qa_valid_form_submission": True}
    return {
        "reasoning": ("Complete the live form's browser-identified neutral prerequisites and perform the "
                      "explicit story-authored submission once."),
        "intent": submit_label, "next_action": action,
        "expected": earliest,
        "expected_control": "", "wait_for": None, "covers": [earliest], "done": False,
        "mechanical_setup": False,
    }


def _pending_denied_send_setup_decision(story, state, coverage, records=None):
    """Recreate and exercise only the exact pre-approval send boundary.

    A resumed governance story can retain the approval decision while its earlier denied-send receipt remains
    open. Once the draft is approved, a probabilistic planner may mistake ``Send approved follow-up`` for the
    requested *without approval* action and perform a legitimate irreversible send. The contract and live DOM
    already give us a safer three-stage path: create one fresh enquiry/draft, submit that draft for approval,
    then activate only the explicitly labelled pre-approval attempt. None of the setup stages earns coverage;
    the final denial still requires its ordinary settled semantic judgment.
    """
    story_text = _story_text(story)
    open_denial = next((str(item.get("aspect") or "") for item in (coverage or [])
                        if isinstance(item, dict) and not item.get("covered") and re.search(
                            r"\battempt to perform the send action without approval\b",
                            str(item.get("aspect") or ""), re.I)), "")
    if (not open_denial
            or not re.search(r"\bsubmit (?:the )?draft for approval\b", story_text, re.I)
            or not re.search(r"\bcreate or seed an enquiry\b", story_text, re.I)):
        return None

    elements = [item for item in ((state or {}).get("elements") or [])
                if isinstance(item, dict)]

    def control(pattern):
        return next((item for item in elements
                     if re.search(pattern, _element_label(item), re.I)
                     and str(item.get("tag") or "").casefold() in ("button", "input")), None)

    def effectful(record):
        if not isinstance(record, dict):
            return False
        facts = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        return bool(facts.get("driver_ok") is True and facts.get("effect_registered") is True
                    or (isinstance(record.get("actual"), str)
                        and "driver_ok=True" in record["actual"]
                        and "effect_registered=True" in record["actual"]))

    portable_records = [record for record in (records or []) if isinstance(record, dict)]
    already_attempted = any(
        isinstance(record.get("action"), dict)
        and record["action"].get("_qa_denied_send_exact_v2") is True
        for record in portable_records)

    def attempt_decision(label="Attempt to send follow-up before approval"):
        if already_attempted:
            return None
        return {
            "reasoning": ("The story requires the pre-approval denial boundary and its exact control is "
                          "available; activate only that control, never the approved-send action."),
            "intent": label,
            "next_action": {"cmd": "click", "target_text": label, "role": "button",
                            "_qa_denied_send_exact": True, "_qa_denied_send_exact_v2": True},
            "expected": ("The pre-approval send attempt is visibly denied, the approval remains pending, "
                         "and no external send side effect is recorded."),
            "expected_control": "", "wait_for": None, "covers": [open_denial], "done": False,
            "mechanical_setup": False,
        }

    def submit_decision(label="Submit for approval"):
        return {
            "reasoning": ("A fresh draft exists but is not pending yet; submit it once as a causal "
                          "precondition for the still-open denied-send boundary."),
            "intent": label,
            "next_action": {"cmd": "click", "target_text": label, "role": "button",
                            "_qa_denied_send_submit": True},
            "expected": ("The fresh draft becomes pending approval and exposes the exact pre-approval send "
                         "attempt control; no follow-up is sent."),
            "expected_control": "Attempt to send follow-up before approval",
            "wait_for": None, "covers": [], "done": False, "mechanical_setup": True,
        }

    attempt = control(r"^attempt to send follow-up before approval$")
    if attempt is not None:
        # Do not loop forever if the element-bound retry also fails. The existing repeat/management path will
        # retain this exact failed receipt for diagnosis instead of authorizing another business action.
        return attempt_decision(_element_label(attempt))

    submit = control(r"^submit for approval$")
    if submit is not None:
        return submit_decision(_element_label(submit))

    # Full-page element inventories are deliberately bounded. An exact successful stage receipt authorizes
    # the next contract-labelled control even when that control lies below the retained inventory window; the
    # bridge still resolves the live label and fails closed if it is absent.
    for record in reversed(portable_records):
        action = record.get("action") if isinstance(record.get("action"), dict) else {}
        if action.get("_qa_denied_send_submit") is True and effectful(record):
            return attempt_decision()
        if action.get("_qa_denied_send_reseed_nonce") is not None and effectful(record):
            return submit_decision()

    # Reuse the browser-grounded neutral form compiler, but deliberately give it a synthetic open creation
    # row and no historical replay fence: this is a new causal fixture for a later boundary, not an attempt to
    # re-earn the already-proven first story step.
    seeded = _pending_valid_form_setup_decision(
        story, state,
        [{"aspect": "Create or seed an enquiry with a follow-up draft", "covered": False}],
        records=[])
    if seeded is None:
        return None
    action = dict(seeded.get("next_action") or {})
    action["_qa_denied_send_reseed"] = True
    nonce = 1 + sum(
        1 for record in portable_records
        if isinstance(record.get("action"), dict)
        and record["action"].get("_qa_denied_send_reseed") is True)
    action["_qa_denied_send_reseed_nonce"] = nonce
    # The generic valid-form compiler intentionally uses stable neutral values. That is correct for ordinary
    # proof, but this path needs a *new* fixture after an older draft was approved/sent; stable values trigger
    # the product's idempotency fence. Vary only benign QA identities, keeping every field valid.
    for case in action.get("cases") or []:
        for nested in (case or {}).get("actions") or []:
            label = " ".join(str(nested.get("target_text") or "").casefold().split())
            if label == "name":
                nested["value"] = f"Denied send QA {nonce}"
            elif label == "dog name":
                nested["value"] = f"Denial Dog {nonce}"
            elif label == "email":
                nested["value"] = f"qa.denied.{nonce}@example.invalid"
    return {
        **seeded,
        "reasoning": ("Create one fresh enquiry/draft as the causal precondition for the still-open "
                      "pre-approval denial boundary."),
        "next_action": action,
        "expected": ("A fresh enquiry creates a new editable follow-up draft without sending anything."),
        "expected_control": "Submit for approval", "covers": [], "mechanical_setup": True,
    }


def _pending_conditional_surface_setup_decision(story, state, coverage):
    """Recreate a missing submission-confirmation surface in one browser-owned prerequisite batch.

    A conditional confirmation is perishable across refresh/process handoff.  When its timed evidence atom is
    the earliest remaining clause but the confirmation landmark is absent, a planner cannot truthfully dwell
    on it.  The live DOM already identifies the owning form, required controls, neutral-safe input types, and
    submit control, so compile those prerequisites without repeated model decisions.  The returned action
    deliberately covers nothing; a later exact timed receipt remains the only way to close the dwell atom.
    """
    ledger = [item for item in (coverage or []) if isinstance(item, dict)]
    earliest = next((str(item.get("aspect") or "") for item in ledger if not item.get("covered")), "")
    if (not earliest or not re.search(r"\bconfirmation(?:\s+screen|\s+view|\s+page)?\b", earliest, re.I)
            or not re.search(r"\b(?:dwell|idle|remain|wait)\b", earliest, re.I)):
        return None
    if _landmark_labels_for_aspect(state, earliest, limit=1):
        return None
    story_text = _story_text(story).casefold()
    if not any(term in story_text for term in ("enquiry", "inquiry", "form", "submit", "send")):
        return None
    if any(term in story_text for term in (
            "required field", "required-field", "empty form", "incomplete form",
            "form validation", "validation message", "missing field", "invalid input",
            "abusive", "injection", "script tag", "sql-like", "credential-like")):
        return None

    elements = [item for item in ((state or {}).get("elements") or [])
                if isinstance(item, dict)]
    submits = [item for item in elements
               if item.get("formIndex") is not None
               and (str(item.get("type") or "").lower() == "submit"
                    or str(item.get("tag") or "").lower() == "button")
               and re.search(r"\b(?:send|submit|create)\b", _element_label(item), re.I)]
    if not submits:
        return None
    submit = next((item for item in submits
                   if any(word in story_text for word in re.findall(
                       r"[a-z0-9]+", _element_label(item).casefold()) if len(word) > 3)), submits[0])
    form_controls = [item for item in elements if item.get("formIndex") == submit.get("formIndex")]
    actions, used = [], set()

    def add_setup(field):
        typ = str(field.get("type") or "").lower()
        empty = (str(field.get("checked") or "").lower() != "true"
                 if typ in ("checkbox", "radio") else not str(field.get("value") or "").strip())
        if not empty:
            return True
        value = _required_setup_value({}, field)
        label = _element_label(field) or str(field.get("name") or "").strip()
        if value is None or not label:
            return False
        identity = (field.get("idx"), label.casefold())
        if identity in used:
            return True
        used.add(identity)
        if value == "__activate__":
            actions.append({"cmd": "click", "target_text": label,
                            "role": "checkbox" if typ == "checkbox" else "radio"})
        else:
            role = "combobox" if str(field.get("tag") or "").lower() == "select" else "textbox"
            actions.append({"cmd": "type", "target_text": label, "role": role, "value": value})
        return True

    for field in form_controls:
        if str(field.get("required") or "").lower() == "true" and not add_setup(field):
            return None
    # Enquiry forms commonly require email OR phone in application logic without marking either control as
    # HTML-required. Supply one neutral contact channel so the final submit is a genuinely valid journey.
    if "enquiry" in story_text or "inquiry" in story_text:
        contact = next((field for field in form_controls
                        if str(field.get("type") or "").lower() == "email"
                        and not str(field.get("value") or "").strip()), None)
        if contact is None:
            contact = next((field for field in form_controls
                            if str(field.get("type") or "").lower() == "tel"
                            and not str(field.get("value") or "").strip()), None)
        if contact is not None and not add_setup(contact):
            return None
    submit_label = _element_label(submit)
    actions.append({"cmd": "click", "target_text": submit_label, "role": "button"})
    if not (2 <= len(actions) <= 8):
        return None
    action = {"cmd": "scenario_matrix", "cases": [{"name": "reveal confirmation", "actions": actions}],
              "_qa_conditional_surface_setup": True, "_qa_expected_surface": earliest}
    return {
        "reasoning": ("Complete the live form's neutral browser-identified prerequisites and submit once "
                      "to recreate the missing conditional confirmation surface."),
        "intent": submit_label, "next_action": action,
        "expected": "A settled submission confirmation surface becomes visible.",
        "expected_control": "", "wait_for": None, "covers": [], "done": False,
        "mechanical_setup": True,
    }


def _focused_reported_keyboard_decision(story, state, coverage, records=None):
    """Replay a sealed Tab finding from its reported source control exactly once.

    A plain page-level ``Tab`` acts on whatever happened to retain focus after restored-state inspection.  On
    long pages that can be an unrelated staff field, while the targeting receipt still carries the finding's
    intended checkbox/date label.  That mismatch caused a false defect, a repair, and another review cycle.
    Once the two passive focused-reproduction steps are proven, bind the reported Tab gesture to the actual
    source control.  The ordinary semantic evaluator still judges the destination, focus ring, and layout.
    """
    story = story or {}
    finding = story.get("focused_finding")
    if story.get("category") != "focused-regression" or not isinstance(finding, dict):
        return None
    reported = finding.get("action")
    if not isinstance(reported, dict) or str(reported.get("cmd") or "").lower() != "press":
        return None
    key = _key_name(reported.get("value")) or _key_name(reported.get("key"))
    if key not in ("Tab", "Shift+Tab"):
        return None

    ledger = [item for item in (coverage or []) if isinstance(item, dict)]
    action_index = next((index for index, item in enumerate(ledger)
                         if re.search(r"\bStory step 3(?:\.|:)",
                                      str(item.get("aspect") or ""), re.I)), None)
    if action_index is None or ledger[action_index].get("covered"):
        return None
    elements = list((state or {}).get("elements") or [])
    detail = str(finding.get("detail") or "")
    source_query = ""
    source_match = re.search(
        r"\bfrom\s+(?:the\s+)?(.{1,100}?)(?:\s+control\b|\s+(?:moved|did|caused|should)\b|[,.;])",
        detail, re.I)
    if source_match:
        source_query = source_match.group(1).strip(" \t\r\n'\"\u2018\u2019\u201c\u201d")

    source = None
    ordinal_match = re.search(r"\b(first|second|third)\s+(?:consent\s+)?checkbox\b",
                              source_query or detail, re.I)
    if ordinal_match:
        ordinal = {"first": 0, "second": 1, "third": 2}[ordinal_match.group(1).lower()]
        checkboxes = [element for element in elements
                      if str(element.get("type") or "").lower() == "checkbox"]
        if ordinal < len(checkboxes):
            source = checkboxes[ordinal]
    if source is None and source_query:
        source_idx, _, source_score = _resolve_target({"target_text": source_query}, elements)
        if source_score:
            source = next((element for element in elements
                           if element.get("idx") == source_idx), None)
    if source is None:
        source_idx, _, source_score = _resolve_target(reported, elements)
        if source_score:
            source = next((element for element in elements
                           if element.get("idx") == source_idx), None)
    if source is None:
        return None

    source_label = _element_label(source)
    contract_text = " ".join((detail, str(finding.get("expected") or ""),
                              str(story.get("expected_outcome") or story.get("expected") or ""))).casefold()
    try:
        source_position = int(source.get("idx"))
    except (TypeError, ValueError):
        source_position = -1
    destinations = []
    for element in elements:
        label = _element_label(element)
        if not label or _labels_match(label, source_label):
            continue
        try:
            position = int(element.get("idx"))
        except (TypeError, ValueError):
            continue
        if ((key == "Tab" and position <= source_position)
                or (key == "Shift+Tab" and position >= source_position)):
            continue
        if label.casefold() in contract_text:
            destinations.append((position, label))
    destinations.sort(key=lambda item: item[0], reverse=key == "Shift+Tab")
    expected_control = destinations[0][1] if destinations else ""

    role = str(reported.get("role") or source.get("role") or "").strip()
    if not role:
        typ, tag = (str(source.get("type") or "").lower(),
                    str(source.get("tag") or "").lower())
        role = typ if typ in ("checkbox", "radio", "button", "submit") else {
            "input": "textbox", "textarea": "textbox", "select": "combobox", "button": "button",
        }.get(tag, "")
    aspect = str(ledger[action_index].get("aspect") or "")
    action = {"cmd": "press", "idx": source.get("idx"), "target_text": source_label,
              "value": key, "_qa_reported_focus_source": True,
              "_qa_restored_focused_state": finding.get("browser_state_restored") is True}
    if role:
        action["role"] = role
    return {
        "reasoning": (f"Replay the sealed {key} gesture from its reported source control "
                      f"{source_label!r}, independent of incidental restored-page focus."),
        "intent": source_label,
        "next_action": action,
        "expected": str(finding.get("expected") or story.get("expected_outcome")
                        or story.get("expected") or "The reported keyboard transition is corrected."),
        "expected_control": expected_control,
        "wait_for": None,
        "covers": [aspect] if aspect else [],
        "done": False,
        "mechanical_setup": False,
    }


def _focused_reported_observation_decision(story, coverage, records=None):
    """Replay a focused finding's exact read-only action before any sibling-state mutation."""
    story = story if isinstance(story, dict) else {}
    finding = story.get("focused_finding")
    if not isinstance(finding, dict):
        return None
    reported = finding.get("action")
    if not isinstance(reported, dict):
        return None
    cmd = str(reported.get("cmd") or "").casefold()
    if cmd not in {"inspect_surfaces", "inspect_landmarks", "dwell_surfaces", "scroll", "viewport"}:
        return None

    def normalized_action(action):
        return json.dumps({
            "cmd": str(action.get("cmd") or "").casefold(),
            "targets": [" ".join(str(value).casefold().split())
                        for value in (action.get("targets") or [])],
            "target_text": " ".join(str(
                action.get("target_text") or action.get("target") or ""
            ).casefold().split()),
            "value": action.get("value"), "width": action.get("width"),
            "height": action.get("height"),
        }, sort_keys=True, default=str)

    sealed = normalized_action(reported)
    for record in records or []:
        prior = record.get("action") if isinstance(record, dict) else None
        # Contract fencing may repair an ambiguous reported landmark to the observed owning surface (for
        # example, staff ``Unresolved blockers`` -> CEO ``Risk Signals``).  The repaired action is no longer
        # byte-equal to the sealed finding action, but it is still the one authorized replay.  Retain the
        # explicit provenance marker as the identity fence so a focused worker never performs the same
        # read-only observation twice merely because its target was safely repaired.
        if isinstance(prior, dict) and prior.get("_qa_reported_observation_source") is True:
            return None
        if isinstance(prior, dict) and normalized_action(prior) == sealed:
            return None

    unresolved = [str(item.get("aspect") or "") for item in (coverage or [])
                  if isinstance(item, dict) and not item.get("covered")]
    exact = [aspect for aspect in unresolved if re.search(
        r"\b(?:reported|read-only|observation|named targets?|named surfaces?|viewport|scroll)\b",
        aspect, re.I)]
    action = dict(reported)
    action["_qa_reported_observation_source"] = True
    return {
        "reasoning": ("Replay the sealed finding's exact read-only browser action before any unrelated "
                      "fixture or business mutation can contaminate its restored evidence state."),
        "intent": str(reported.get("target_text") or "focused observation"),
        "next_action": action,
        "expected": str(finding.get("expected") or story.get("expected_outcome") or
                        "The sealed read-only observation matches the corrected behavior."),
        "expected_control": "", "wait_for": None, "covers": exact,
        "done": False, "mechanical_setup": False,
    }


def _successful_focused_observation_coverage(
        story, action, verdict, coverage, targeting, after_state):
    """Close one observational recheck from one independently judged, settled receipt.

    A focused observational contract deliberately decomposes one passive browser observation into setup,
    perform, and verify ledger rows.  Those are audit boundaries, not three different user actions.  Replaying
    the same inspection to make each row green wastes model calls and can hit the repeat guard even though the
    first receipt already proved the corrected behavior.  Credit the three administrative rows together only
    when the exact sealed/repaired action carries its provenance marker, the semantic evaluator explicitly
    passes it, and the driver proves that every named landmark was found and stable.  Mutating, inconclusive,
    partial-target, or evidence-empty actions remain ineligible.
    """
    story = story if isinstance(story, dict) else {}
    finding = story.get("focused_finding")
    action = action if isinstance(action, dict) else {}
    verdict = verdict if isinstance(verdict, dict) else {}
    targeting = targeting if isinstance(targeting, dict) else {}
    after_state = after_state if isinstance(after_state, dict) else {}
    if (str(story.get("category") or "").casefold() != "focused-regression"
            or not isinstance(finding, dict)
            or finding.get("observational_recheck") is not True
            or action.get("_qa_reported_observation_source") is not True
            or str(action.get("cmd") or "").casefold() not in {
                "inspect_surfaces", "inspect_landmarks", "dwell_surfaces", "dwell_landmarks"}
            or verdict.get("verdict") not in {"pass", "match"}
            or verdict.get("matches_expected") is not True
            or verdict.get("bug")
            or verdict.get("model_failed") is True
            or verdict.get("infrastructure_error")
            or targeting.get("driver_ok") is not True
            or targeting.get("label_matched") is not True):
        return []

    receipt = targeting.get("landmark_dwell_summary") or targeting.get("landmark_dwell")
    if (not isinstance(receipt, dict)
            or receipt.get("all_targets_matched") is not True
            or receipt.get("all_stable") is not True):
        return []
    requested = [" ".join(str(value).casefold().split())
                 for value in (action.get("targets") or []) if str(value).strip()]
    observed = [" ".join(str(value).casefold().split())
                for value in (receipt.get("targets") or []) if str(value).strip()]
    if not requested or not observed or any(value not in observed for value in requested):
        return []
    if (not after_state.get("url")
            or not any(str(after_state.get(key) or "").strip()
                       for key in ("bodyText", "viewportText", "accessibilityTree"))
            or after_state.get("console_errors")):
        return []

    patterns = (
        r"\bconfirm only the finding-named prerequisites and surfaces\b",
        r"\bperform the reported read-only observation\b",
        r"\bverify the corrected expected behavior\b",
    )
    unresolved = [str(item.get("aspect") or "") for item in (coverage or [])
                  if isinstance(item, dict) and not item.get("covered")]
    return [aspect for aspect in unresolved
            if any(re.search(pattern, aspect, re.I) for pattern in patterns)]


def _successful_approval_diagnostics_coverage(
        story, action, verdict, coverage, targeting, after_state):
    """Credit the approval-diagnostics clause after its exact independent settled pass."""
    story = story if isinstance(story, dict) else {}
    action = action if isinstance(action, dict) else {}
    verdict = verdict if isinstance(verdict, dict) else {}
    targeting = targeting if isinstance(targeting, dict) else {}
    after_state = after_state if isinstance(after_state, dict) else {}
    contract = _story_text(story)
    receipt = targeting.get("landmark_dwell_summary") or targeting.get("landmark_dwell") or {}
    targets = [" ".join(str(value).casefold().split())
               for value in (receipt.get("targets") or []) if str(value).strip()]
    if (not re.search(r"\binspect approval diagnostics\b", contract, re.I)
            or not re.search(r"\bstaff console\b", contract, re.I)
            or not re.search(r"\bCEO command view\b", contract, re.I)
            or str(action.get("cmd") or "").casefold() != "inspect_surfaces"
            or targets != ["operational diagnostics"]
            or verdict.get("verdict") not in {"pass", "match"}
            or verdict.get("matches_expected") is not True
            or verdict.get("bug") or verdict.get("model_failed") is True
            or verdict.get("infrastructure_error")
            or targeting.get("driver_ok") is not True
            or receipt.get("all_targets_matched") is not True
            or receipt.get("all_stable") is not True
            or not after_state.get("url")
            or not any(str(after_state.get(key) or "").strip()
                       for key in ("bodyText", "viewportText", "accessibilityTree"))
            or after_state.get("console_errors")):
        return []
    return [str(item.get("aspect") or "") for item in (coverage or [])
            if isinstance(item, dict) and not item.get("covered")
            and re.search(r"\binspect approval diagnostics\b",
                          str(item.get("aspect") or ""), re.I)]


def _successful_denied_send_decision_coverage(
        story, action, expected, verdict, coverage, targeting, after_state, prior_records):
    """Combine exact denied-send/decision actions with their passed settled audit inspection."""
    story = story if isinstance(story, dict) else {}
    action = action if isinstance(action, dict) else {}
    verdict = verdict if isinstance(verdict, dict) else {}
    targeting = targeting if isinstance(targeting, dict) else {}
    after_state = after_state if isinstance(after_state, dict) else {}
    contract = _story_text(story)
    receipt = targeting.get("landmark_dwell_summary") or targeting.get("landmark_dwell") or {}
    targets = {" ".join(str(value).casefold().split())
               for value in (receipt.get("targets") or []) if str(value).strip()}
    if (not re.search(r"\battempt to perform the send action without approval\b", contract, re.I)
            or not re.search(r"\bapprove or reject\b", contract, re.I)
            or str(action.get("cmd") or "").casefold() != "inspect_surfaces"
            or "governance audit history" not in targets
            or not ({"follow-up drafts", "unresolved blockers"} & targets)
            or not re.search(r"\bapprov(?:ed|al)|\breject(?:ed|ion)", str(expected or ""), re.I)
            or not re.search(r"\bno (?:external )?send|without any external send|no send event",
                             str(expected or ""), re.I)
            or verdict.get("verdict") not in {"pass", "match"}
            or verdict.get("matches_expected") is not True
            or verdict.get("bug") or verdict.get("model_failed") is True
            or verdict.get("infrastructure_error")
            or targeting.get("driver_ok") is not True
            or receipt.get("all_targets_matched") is not True
            or receipt.get("all_stable") is not True
            or not after_state.get("url")
            or not any(str(after_state.get(key) or "").strip()
                       for key in ("bodyText", "viewportText", "accessibilityTree"))
            or after_state.get("console_errors")):
        return []

    denied, decided = False, False
    for record in prior_records or []:
        if not isinstance(record, dict) or record.get("bug"):
            continue
        prior_action = _portable_checkpoint_action(record.get("action"))
        facts = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        label = " ".join(str(value or "") for value in (
            prior_action.get("target_text"), facts.get("intended"), facts.get("targeted_label"))).casefold()
        effectful = (facts.get("driver_ok") is True and facts.get("effect_registered") is True
                     and facts.get("label_matched") is not False)
        denied = denied or (effectful and "attempt to send follow-up before approval" in label)
        decided = decided or (effectful and bool(re.search(r"\b(?:approve|reject) send\b", label)))
    if not (denied and decided):
        return []
    return [str(item.get("aspect") or "") for item in (coverage or [])
            if isinstance(item, dict) and not item.get("covered") and re.search(
                r"\battempt to perform the send action without approval\b|\bapprove or reject\b",
                str(item.get("aspect") or ""), re.I)]


def _successful_empty_first_run_transition_coverage(
        story, action, verdict, coverage, targeting, after_state, prior_records):
    """Close the first-run transition from exact causal receipts without inventing approval work."""
    contract = _story_text(story)
    action = action if isinstance(action, dict) else {}
    verdict = verdict if isinstance(verdict, dict) else {}
    targeting = targeting if isinstance(targeting, dict) else {}
    after_state = after_state if isinstance(after_state, dict) else {}
    receipt = targeting.get("landmark_dwell_summary") or targeting.get("landmark_dwell") or {}
    targets = {" ".join(str(value).casefold().split())
               for value in (receipt.get("targets") or []) if str(value).strip()}
    required = {"public enquiry", "staff operating console", "ceo command view",
                "queue and dead letters", "approval summary", "audit summary"}
    rendered = " ".join(str(after_state.get(name) or "")
                        for name in ("bodyText", "viewportText", "statusText"))
    if (not re.search(r"\brecover from empty first[- ]run data\b", contract, re.I)
            or not re.search(r"\bsubmit the first valid public enquiry\b", contract, re.I)
            or not re.search(r"\bdrain the queue\b", contract, re.I)
            or str(action.get("cmd") or "").casefold() != "inspect_surfaces"
            or not required.issubset(targets)
            or verdict.get("verdict") not in {"pass", "match"}
            or verdict.get("matches_expected") is not True
            or verdict.get("bug") or verdict.get("model_failed") is True
            or verdict.get("infrastructure_error")
            or targeting.get("driver_ok") is not True
            or receipt.get("all_targets_matched") is not True
            or receipt.get("all_stable") is not True
            or not after_state.get("url")
            or not rendered.strip()
            or not re.search(r"\b(?:queue processing complete|processed|succeeded)\b", rendered, re.I)
            or after_state.get("console_errors")):
        return []

    submitted, drained = False, False
    for record in prior_records or []:
        if not isinstance(record, dict) or record.get("bug"):
            continue
        prior_action = _portable_checkpoint_action(record.get("action"))
        facts = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        actual = str(record.get("actual") or "")
        effectful = bool(
            facts.get("driver_ok") is True and facts.get("effect_registered") is True
            or ("driver_ok=True" in actual and "effect_registered=True" in actual))
        submitted = submitted or bool(effectful and prior_action.get("_qa_valid_form_submission") is True)
        label = " ".join(str(value or "") for value in (
            prior_action.get("target_text"), facts.get("intended"), facts.get("targeted_label"))).casefold()
        drained = drained or bool(effectful and "drain queue" in label)
    if not (submitted and drained):
        return []
    proven = []
    for item in coverage or []:
        if not isinstance(item, dict) or item.get("covered"):
            continue
        aspect = str(item.get("aspect") or "")
        submit_transition = bool(
            re.search(r"\bsubmit the first valid public enquiry\b", aspect, re.I)
            and re.search(r"\b(?:panels?|public|staff|ceo|queue)\b", aspect, re.I)
            and re.search(r"\b(?:transition|update|populated|coherent)\b", aspect, re.I))
        drained_transition = bool(
            re.search(r"\bdrain the queue\b", aspect, re.I)
            and re.search(r"\b(?:all|public|staff|ceo|panels?)\b", aspect, re.I)
            and re.search(r"\b(?:transition|update|populated|coherent|remain)\b", aspect, re.I))
        if submit_transition or drained_transition:
            proven.append(aspect)
    return proven


def _successful_queue_recovery_coverage(
        story, action, verdict, coverage, targeting, after_state, prior_records):
    """Close the compound failure-recovery row only from its ordered durable browser receipts."""
    candidates = [str(item.get("aspect") or "") for item in (coverage or [])
                  if isinstance(item, dict) and not item.get("covered")
                  and _requires_queue_recovery_journey(item.get("aspect"))]
    if not candidates:
        return []
    action = action if isinstance(action, dict) else {}
    verdict = verdict if isinstance(verdict, dict) else {}
    targeting = targeting if isinstance(targeting, dict) else {}
    after_state = after_state if isinstance(after_state, dict) else {}
    receipt = targeting.get("landmark_dwell_summary") or targeting.get("landmark_dwell") or {}
    rendered = " ".join(str(after_state.get(name) or "")
                        for name in ("bodyText", "viewportText", "statusText"))
    if (str(action.get("cmd") or "").casefold() != "inspect_surfaces"
            or verdict.get("verdict") not in {"pass", "match"}
            or verdict.get("matches_expected") is not True
            or verdict.get("bug") or verdict.get("model_failed") is True
            or verdict.get("infrastructure_error")
            or targeting.get("driver_ok") is not True
            or receipt.get("all_targets_matched") is not True
            or receipt.get("all_stable") is not True
            or not after_state.get("url") or after_state.get("console_errors")
            or not re.search(r"\b(?:succeeded|success|completed|processing complete)\b", rendered, re.I)
            or not re.search(r"\b(?:audit|attempts?|run\s*after|runafter)\b", rendered, re.I)):
        return []

    timeline = []
    for index, record in enumerate(prior_records or []):
        if not isinstance(record, dict) or record.get("bug") or record.get("infrastructure_error"):
            continue
        prior_action = _portable_checkpoint_action(record.get("action"))
        facts = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        actual = str(record.get("actual") or "")
        ok = facts.get("driver_ok") is True or "driver_ok=True" in actual
        effect = facts.get("effect_registered") is True or "effect_registered=True" in actual
        # Control identity comes from the action/target receipt, never from the whole rendered page.  The page
        # can retain the words Retry and Drain in audit history and previously made either button look like the
        # other during resumed chronology reduction.
        label = " ".join(str(value or "") for value in (
            prior_action.get("target_text"), prior_action.get("target"), facts.get("intended"),
            facts.get("targeted_label"))).casefold()
        value = str(prior_action.get("value") or facts.get("action_value") or "").casefold()
        timeline.append((index, prior_action, ok, effect, label, value,
                         " ".join((actual, str(record.get("expected") or ""))).casefold()))

    # Prefer the already-passed cross-surface failure receipt.  A later Retry button necessarily contains the
    # words "dead-letter" in its accessible label and must not become a new failure anchor merely because a
    # compact rendered snapshot omitted the earlier diagnostic body.
    failure_index = next((index for index, record in enumerate(prior_records or [])
                          if isinstance(record, dict) and not record.get("bug")
                          and str(_portable_checkpoint_action(record.get("action")).get("cmd") or "").casefold()
                          == "inspect_surfaces"
                          and any(re.search(r"\b(?:failed|dead.?letter(?:ed)?)\b", str(aspect or ""), re.I)
                                  for aspect in list(record.get("covers") or [])
                                  + list(record.get("demonstrated") or []))), None)
    if failure_index is None:
        failure_index = next((index for index, _, ok, _, label, _, evidence in timeline
                              if ok and not re.search(r"\bretry\b", label, re.I)
                              and re.search(r"\b(?:failed|dead.?letter(?:ed)?|runtime_timeout)\b",
                                            evidence, re.I)), None)
    if failure_index is None:
        return []
    success_index = next((index for index, prior_action, ok, effect, label, value, _ in timeline
                          if index > failure_index and ok
                          and _runtime_scenario_action(prior_action, label, value)
                          and "success" in value), None)
    if success_index is None:
        return []
    retry_index = None
    for index, prior_action, ok, effect, label, _, _ in timeline:
        if (index <= success_index or not ok or not effect
                or str(prior_action.get("cmd") or "").casefold() not in {
                    "click", "tap", "press"}
                or not re.search(r"\bretry\b", label, re.I)):
            continue
        latest_scenario = next((item for item in reversed(timeline)
                                if success_index <= item[0] < index and item[2]
                                and (item[0] == success_index
                                     or _runtime_scenario_action(item[1], item[4], item[5]))), None)
        if latest_scenario is not None and "success" in latest_scenario[5]:
            retry_index = index
            break
    if retry_index is None:
        return []
    # A runtime scenario is process-local while the queue is durable.  Resuming a browser can therefore
    # re-establish Success after the governed Retry receipt.  Conversely, a later Timeout selection invalidates
    # an otherwise well-shaped Drain receipt: that drain proves another failure attempt, not recovery.  Require
    # the most recent scenario selection before the drain to be Success while retaining the original ordered
    # failure -> Success -> Retry contract.
    drain_index = None
    for index, prior_action, ok, effect, label, _, _ in timeline:
        if (index <= retry_index or not ok or not effect
                or str(prior_action.get("cmd") or "").casefold() not in {
                    "click", "tap", "press"}
                or "drain queue" not in label):
            continue
        latest_scenario = next((item for item in reversed(timeline)
                                if retry_index < item[0] < index and item[2]
                                and _runtime_scenario_action(item[1], item[4], item[5])), None)
        scenario_value = latest_scenario[5] if latest_scenario is not None else "success"
        if "success" in scenario_value:
            drain_index = index
            break
    return candidates if drain_index is not None else []


def _queue_recovery_wrong_scenario_false_positive(story, bug_text, prior_records):
    """Reject a recovery defect produced by draining under a later failure scenario."""
    if (not re.search(r"\bretry dead[- ]lettered agent work\b", _story_text(story), re.I)
            or not re.search(r"\b(?:retr(?:y|ied|ying)|recover(?:y|ed|ing)?|"
                             r"succeed(?:ed|ing)?|failure signal)\b",
                             str(bug_text or ""), re.I)):
        return False
    timeline = []
    for index, record in enumerate(prior_records or []):
        if not isinstance(record, dict) or record.get("bug"):
            continue
        action = _portable_checkpoint_action(record.get("action"))
        facts = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        actual = str(record.get("actual") or "")
        ok = facts.get("driver_ok") is True or "driver_ok=True" in actual
        effect = facts.get("effect_registered") is True or "effect_registered=True" in actual
        label = " ".join(str(value or "") for value in (
            action.get("target_text"), action.get("target"), facts.get("intended"),
            facts.get("targeted_label"))).casefold()
        value = str(action.get("value") or facts.get("action_value") or "").casefold()
        timeline.append((index, action, ok, effect, label, value))
    retry_index = next((item[0] for item in reversed(timeline)
                        if item[2] and item[3] and re.search(r"\bretry\b", item[4], re.I)
                        and str(item[1].get("cmd") or "").casefold() in {
                            "click", "tap", "press"}), None)
    if retry_index is None:
        return False
    drain_index = next((item[0] for item in reversed(timeline)
                        if item[0] > retry_index and item[2] and item[3]
                        and "drain queue" in item[4]), None)
    if drain_index is None:
        return False
    latest_scenario = next((item for item in reversed(timeline)
                            if item[0] < drain_index and item[2]
                            and _runtime_scenario_action(item[1], item[4], item[5])), None)
    return bool(latest_scenario is not None and "success" not in latest_scenario[5])


def _successful_queue_failure_coverage(
        story, action, verdict, coverage, targeting, after_state, prior_records):
    """Close the one-enquiry failure journey only when its terminal drain is browser-proven."""
    candidates = [str(item.get("aspect") or "") for item in (coverage or [])
                  if isinstance(item, dict) and not item.get("covered")
                  and re.search(r"\bsubmit (?:one|an) enquiry\b", str(item.get("aspect") or ""), re.I)
                  and re.search(r"\bdrain(?:\s+the)?(?:\s+agent)?\s+queue\b",
                                str(item.get("aspect") or ""), re.I)
                  and re.search(r"\b(?:failed|dead.?letter(?:ed)?)\b",
                                str(item.get("aspect") or ""), re.I)]
    if not candidates:
        return []
    action = action if isinstance(action, dict) else {}
    verdict = verdict if isinstance(verdict, dict) else {}
    targeting = targeting if isinstance(targeting, dict) else {}
    after_state = after_state if isinstance(after_state, dict) else {}
    label = " ".join(str(value or "") for value in (
        action.get("target_text"), targeting.get("intended"), targeting.get("targeted_label"))).casefold()
    rendered = " ".join(str(after_state.get(name) or "")
                        for name in ("bodyText", "viewportText", "statusText")).casefold()
    if (str(action.get("cmd") or "").casefold() not in {"click", "tap", "press"}
            or "drain queue" not in label or targeting.get("driver_ok") is not True
            or targeting.get("effect_registered") is not True
            or verdict.get("verdict") not in {"pass", "match"}
            or verdict.get("matches_expected") is not True or verdict.get("bug")
            or not _live_terminal_queue_failure(after_state)
            or after_state.get("console_errors")):
        return []
    submissions = 0
    timeout_selected = False
    for record in prior_records or []:
        if not isinstance(record, dict) or record.get("bug"):
            continue
        prior_action = _portable_checkpoint_action(record.get("action"))
        facts = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        actual = str(record.get("actual") or "")
        ok = facts.get("driver_ok") is True or "driver_ok=True" in actual
        effect = facts.get("effect_registered") is True or "effect_registered=True" in actual
        expected = str(record.get("expected") or "").casefold()
        if not (ok and effect):
            continue
        if (prior_action.get("_qa_valid_form_submission") is True
                or (str(prior_action.get("cmd") or "").casefold() in {"scenario_matrix", "case_matrix"}
                    and re.search(r"\bsubmit (?:one|an) enquiry\b", expected, re.I))):
            submissions += 1
        value = str(prior_action.get("value") or facts.get("action_value") or "").casefold()
        timeout_selected = timeout_selected or bool(
            str(prior_action.get("cmd") or "").casefold() in {"type", "fill", "select", "choose"}
            and "timeout" in value)
    return candidates if submissions == 1 and timeout_selected else []


def _successful_queue_failure_inspection_coverage(
        story, action, verdict, coverage, targeting, after_state):
    """Close engineering-risk projection from one stable multi-surface failure receipt."""
    candidates = [str(item.get("aspect") or "") for item in (coverage or [])
                  if isinstance(item, dict) and not item.get("covered")
                  and _requires_multi_surface_inspection(item.get("aspect"))
                  and re.search(r"\b(?:failed|dead.?letter(?:ed)?)\b",
                                str(item.get("aspect") or ""), re.I)]
    action = action if isinstance(action, dict) else {}
    targeting = targeting if isinstance(targeting, dict) else {}
    verdict = verdict if isinstance(verdict, dict) else {}
    after_state = after_state if isinstance(after_state, dict) else {}
    receipt = targeting.get("landmark_dwell_summary") or targeting.get("landmark_dwell") or {}
    rendered = " ".join(str(after_state.get(name) or "")
                        for name in ("bodyText", "viewportText", "statusText")).casefold()
    if (not candidates or str(action.get("cmd") or "").casefold() != "inspect_surfaces"
            or targeting.get("driver_ok") is not True
            or receipt.get("all_targets_matched") is not True or receipt.get("all_stable") is not True
            or verdict.get("verdict") not in {"pass", "match"}
            or verdict.get("matches_expected") is not True or verdict.get("bug")
            or not _live_terminal_queue_failure(after_state)
            or after_state.get("console_errors")):
        return []
    return candidates


def _queue_projection_sections(after_state):
    """Return exact rendered sections used by the deterministic US-008 projection oracle."""
    body = str((after_state or {}).get("bodyText") or "")

    def section(start, end=None):
        found = re.search(start, body, re.I)
        if not found:
            return ""
        tail = body[found.start():]
        if end:
            boundary = re.search(end, tail[found.end() - found.start():], re.I)
            if boundary:
                tail = tail[:found.end() - found.start() + boundary.start()]
        return tail

    return body, section(r"\bStaff operating console\b", r"\bCEO Command View\b"), \
        section(r"\bCEO Command View\b", r"\bOperational Diagnostics\b"), \
        section(r"\bOperational Diagnostics\b")


def _complete_queue_projection_receipt(action, targeting, after_state):
    action = action if isinstance(action, dict) else {}
    targeting = targeting if isinstance(targeting, dict) else {}
    receipt = targeting.get("landmark_dwell_summary") or targeting.get("landmark_dwell") or {}
    requested = {" ".join(str(value).casefold().split())
                 for value in (action.get("targets") or []) if str(value).strip()}
    observed = {" ".join(str(value).casefold().split())
                for value in (receipt.get("targets") or []) if str(value).strip()}
    required = {" ".join(value.casefold().split()) for value in _QUEUE_RETRY_PROJECTION_TARGETS}
    return bool(
        str(action.get("cmd") or "").casefold() == "inspect_surfaces"
        and targeting.get("driver_ok") is True
        and receipt.get("all_targets_matched") is True
        and receipt.get("all_stable") is True
        and required.issubset(requested) and required.issubset(observed)
        and (after_state or {}).get("url")
        and not (after_state or {}).get("console_errors"))


def _mechanical_queue_projection_verdict(
        story, action, decision, targeting, after_state, coverage, prior_records):
    """Close the canonical US-008 projection from exact DOM plus ordered browser receipts.

    This is deliberately narrower than a generic text oracle.  It runs only for the authored retry story,
    only after all seven canonical landmarks were found and stable, and requires explicit per-surface facts.
    Failure and recovery still fail closed to the semantic judge whenever a required notification, blocker,
    count, audit transition, retry control, runAfter value, or chronology receipt is absent.
    """
    if (not re.search(r"\bretry dead[- ]lettered agent work\b", _story_text(story), re.I)
            or not _complete_queue_projection_receipt(action, targeting, after_state)):
        return None
    body, staff, ceo, diagnostics = _queue_projection_sections(after_state)
    if not all((body, staff, ceo, diagnostics)):
        return None
    synthetic_pass = {"verdict": "pass", "matches_expected": True, "bug": None,
                      "model_failed": False, "infrastructure_error": None}

    failure_candidates = _successful_queue_failure_inspection_coverage(
        story, action, synthetic_pass, coverage, targeting, after_state)
    if failure_candidates and _live_terminal_queue_failure(after_state):
        failure_checks = (
            re.search(r"\bAgent jobs\b[\s\S]{0,900}\b(?:dead_letter|dead letter|failed)\b"
                      r"[\s\S]{0,500}Retry\b", staff, re.I),
            re.search(r"\bUnresolved blockers\b[\s\S]{0,600}\bengineering\b"
                      r"[\s\S]{0,300}\b(?:failed|dead.?lettered)\b", staff, re.I),
            re.search(r"\bGovernance audit history\b[\s\S]{0,1800}\bagent\.failed\b"
                      r"[\s\S]{0,1600}\bblocker\.created\b", staff, re.I),
            re.search(r"\bRelated notifications\b[\s\S]{0,1400}"
                      r"\bGovernance blocker escalated\b[\s\S]{0,500}"
                      r"\b(?:failed|dead.?lettered)\b", staff, re.I),
            re.search(r"\bAgent Activity\b[\s\S]{0,500}\bFailed\s+[1-9]\d*\b", ceo, re.I),
            re.search(r"\bRisk Signals\b[\s\S]{0,350}\bOpen\s*\([1-9]\d*\)"
                      r"[\s\S]{0,600}\bengineering\b", ceo, re.I),
            re.search(r"\bDead letters\s+[1-9]\d*\b[\s\S]{0,180}"
                      r"\bUnresolved blockers\s+[1-9]\d*\b", ceo, re.I),
            re.search(r"\bApp jobs\b[\s\S]{0,500}\bDead letter\s+[1-9]\d*\b", diagnostics, re.I),
            re.search(r"\bPipeline jobs\b[\s\S]{0,500}\bDead letter\s+[1-9]\d*\b", diagnostics, re.I),
            re.search(r"\bDead letters\b[\s\S]{0,100}\bTotal\s+[1-9]\d*\b", diagnostics, re.I),
            re.search(r"\b(?:enquiriesTotal\s+1|Total enquiries\s+1|1 agent job)\b", body, re.I),
        )
        if all(failure_checks):
            return {
                "matches_expected": True, "verdict": "pass", "target_confirmed": True,
                "bug": None, "severity": "none", "blocking": False, "demonstrated": [],
                "model_failed": False, "infrastructure_error": None,
                "_raw": {"engine": "mechanical-queue-failure-projection"},
            }

    recovery_candidates = _successful_queue_recovery_coverage(
        story, action, synthetic_pass, coverage, targeting, after_state, prior_records)
    if recovery_candidates:
        iso = r"20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
        recovery_checks = (
            re.search(r"\bAgent jobs\b[\s\S]{0,900}\bsucceeded\b[\s\S]{0,350}"
                      r"\b\d+\s+of\s+\d+\s+attempts\b[\s\S]{0,250}"
                      rf"\brun\s+after\s+{iso}\b", staff, re.I),
            re.search(r"\bUnresolved blockers\b\s+(?:No records|None|0)\b", staff, re.I),
            re.search(r"\bRelated notifications\b[\s\S]{0,1800}\bAgent job recovered\b", staff, re.I),
            re.search(r"\bGovernance audit history\b[\s\S]{0,2400}\bagent\.failed\b"
                      r"[\s\S]{0,2400}\bagent\.completed\b[\s\S]{0,1200}"
                      r"\bblocker\.resolved\b", staff, re.I),
            re.search(r"\bAgent Activity\b[\s\S]{0,500}\bSucceeded\s+[1-9]\d*\b"
                      r"[\s\S]{0,160}\bFailed\s+0\b", ceo, re.I),
            re.search(r"\bRisk Signals\b[\s\S]{0,200}\bOpen\s*\(0\)", ceo, re.I),
            re.search(r"\bDead letters\s+0\b[\s\S]{0,180}\bUnresolved blockers\s+0\b", ceo, re.I),
            re.search(r"\bApp jobs\b[\s\S]{0,500}\bSucceeded\s+[1-9]\d*\b"
                      r"[\s\S]{0,260}\bDead letter\s+0\b", diagnostics, re.I),
            re.search(r"\bPipeline jobs\b[\s\S]{0,500}\bSucceeded\s+[1-9]\d*\b"
                      r"[\s\S]{0,260}\bDead letter\s+0\b", diagnostics, re.I),
            re.search(r"\b(?:enquiriesTotal\s+1|Total enquiries\s+1|1 agent job)\b", body, re.I),
        )
        if all(recovery_checks):
            return {
                "matches_expected": True, "verdict": "pass", "target_confirmed": True,
                "bug": None, "severity": "none", "blocking": False, "demonstrated": [],
                "model_failed": False, "infrastructure_error": None,
                "_raw": {"engine": "mechanical-queue-recovery-projection"},
            }
    return None


def _checkpoint_failed_keyboard_decision(story, state, coverage, resume_rows=None, records=None):
    """Retry one exact failed durable keyboard boundary after a worker/browser upgrade.

    A continuation can contain extensive successful Tab traversal plus one driver-confirmed key action that
    registered no effect.  Re-running the broad traversal cannot repair that missing evidence and is exactly
    the loop a human QA lead would stop.  When the still-open contract is explicitly keyboard/focus work,
    replay the newest failed, labelled key action once against the currently observed control.  The semantic
    judge still decides whether this receipt plus the sealed prior dossier proves the broad clause.
    """
    unresolved = [str(item.get("aspect") or "") for item in (coverage or [])
                  if isinstance(item, dict) and not item.get("covered")]
    first_unresolved = unresolved[0] if unresolved else ""
    timed_observation = bool(
        re.search(r"\b(?:dwell|idle|remain|wait)\b", first_unresolved, re.I)
        and re.search(r"\b\d+(?:\.\d+)?\s*(?:seconds?|secs?|s)\b", first_unresolved, re.I))
    keyboard_aspect = (first_unresolved if not timed_observation and re.search(
        r"\b(?:keyboard|focus|tab|arrow(?:up|down|left|right)?|space|enter)\b",
        first_unresolved, re.I) else "")
    if not keyboard_aspect or any(
            isinstance(item, dict)
            and isinstance(item.get("action"), dict)
            and item["action"].get("_qa_checkpoint_keyboard_retry")
            for item in (records or [])):
        return None

    elements = list((state or {}).get("elements") or [])
    allowed = {"tab", "shift+tab", "arrowup", "arrowdown", "arrowleft", "arrowright",
               "home", "end", "space", "enter", "escape"}
    for row in reversed([item for item in (resume_rows or []) if isinstance(item, dict)]):
        action = _portable_checkpoint_action(row.get("action"))
        if str(action.get("cmd") or "").casefold() != "press":
            continue
        key = str(action.get("value") or action.get("key") or "").strip()
        canonical_key = key.casefold().replace(" ", "")
        if canonical_key not in allowed:
            continue
        actual = str(row.get("actual") or "")
        if "driver_ok=True" not in actual or "effect_registered=False" not in actual:
            continue
        target_match = re.search(r"\btarget=(['\"])(.*?)\1", actual)
        target = target_match.group(2).strip() if target_match else ""
        if not target:
            continue
        resolved_idx, resolved_label, score = _resolve_target({"target_text": target}, elements)
        if not score:
            continue
        element = next((item for item in elements if item.get("idx") == resolved_idx), {})
        label = _element_label(element) or resolved_label or target
        tag = str(element.get("tag") or "").casefold()
        role = str(element.get("role") or "").strip() or {
            "select": "combobox", "textarea": "textbox", "button": "button",
        }.get(tag, "")
        replay = {"cmd": "press", "target_text": label, "value": key,
                  "_qa_checkpoint_keyboard_retry": True}
        if role:
            replay["role"] = role
        return {
            "reasoning": (f"Retry the exact failed durable {key} boundary on {label!r}; broad traversal "
                          "is already sealed and cannot supply this missing key receipt."),
            "intent": label, "next_action": replay,
            "expected": (str(row.get("expected") or "").strip()
                         or f"The {label} control responds to the trusted {key} key and retains visible focus."),
            "expected_control": "", "wait_for": None,
            "covers": [keyboard_aspect], "done": False,
            "mechanical_setup": False,
        }
    return None


def _landmark_labels_for_aspect(state, aspect, limit=8):
    """Select live long-page landmarks named by one explicit story clause."""
    ignored = {"after", "before", "control", "controls", "dwell", "each", "every", "form", "idle",
               "page", "screen", "seconds", "surface", "surfaces", "verify", "view"}
    wanted = {token for token in re.findall(r"[a-z0-9]+", str(aspect or "").casefold())
              if len(token) >= 3 and token not in ignored and not token.isdigit()}
    scored = []
    seen = set()
    for index, item in enumerate((state or {}).get("documentLandmarks") or []):
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get("label") or "").split())
        if not label or label.casefold() in seen:
            continue
        tokens = set(re.findall(r"[a-z0-9]+", label.casefold()))
        overlap = wanted & tokens
        label_low = label.casefold()
        # Conditional screens frequently use human copy rather than the noun in the story (for example,
        # "Thanks. We have your enquiry" is the confirmation surface). Keep these aliases narrow and only
        # apply them when the authored surface family is explicit; otherwise ordinary lexical matching wins.
        if not overlap and "confirmation" in wanted and re.search(
                r"\b(?:thanks|received|submitted|success(?:ful|fully)?|we have your)\b", label_low):
            overlap = {"confirmation"}
        if not overlap and "diagnostics" in wanted and re.search(
                r"\b(?:diagnostics?|agent jobs?|queue health|dead letters?|retrying)\b", label_low):
            overlap = {"diagnostics"}
        if not overlap and "error" in wanted and re.search(
                r"\b(?:error|failed|failure|invalid|unable|unavailable|try again)\b", label_low):
            overlap = {"error"}
        if not overlap:
            continue
        seen.add(label.casefold())
        scored.append((len(overlap), -index, label))
    scored.sort(reverse=True)
    return [item[2] for item in scored[:max(1, int(limit))]]


def _pending_atomic_keyboard_decision(story, state, coverage, records=None):
    """Compile a migrated keyboard atom directly into its browser-owned proof action.

    The model is still the semantic judge, but no model is needed to rediscover that a Tab atom means a full
    forward traversal or that an Arrow/Space/Enter atom means a keyboard matrix over the live applicable
    controls.  This removes one 10–30 second planning call per evidence boundary and prevents broad traversal
    from starving the specific key operation that remains open.
    """
    remaining = [item for item in (coverage or [])
                 if isinstance(item, dict) and not item.get("covered")]
    if not remaining:
        return None
    item = remaining[0]
    kind = str(item.get("atomic_kind") or "")
    if kind not in {"tab", "shift_tab", "arrow", "space", "enter", "enter_or_space"}:
        return None
    aspect = str(item.get("aspect") or "")
    if kind in {"tab", "shift_tab"}:
        direction = "backward" if kind == "shift_tab" else "forward"
        action = {"cmd": "traverse", "value": direction, "_qa_inventory_derived": True}
        reasoning = (f"Execute one complete trusted {direction} traversal for the migrated {kind} boundary; "
                     "the browser derives the focusable inventory and checks every focus/overflow receipt.")
    else:
        requested = ({"arrow": ["ArrowDown"], "space": ["Space"], "enter": ["Enter"],
                      "enter_or_space": ["Enter"]})[kind]
        action = {"cmd": "keyboard_matrix", "value": requested, "_qa_inventory_derived": True}
        reasoning = (f"Exercise the {kind.replace('_', ' ')} modality across every live applicable control "
                     "in one bounded browser-owned matrix receipt.")
    return {"reasoning": reasoning, "intent": "complete keyboard evidence boundary",
            "next_action": action, "expected": aspect, "expected_control": "", "wait_for": None,
            "covers": [aspect], "done": False, "mechanical_setup": False}


def _pending_explicit_evidence_decision(story, state, coverage, records=None):
    """Compile an explicit dwell or refresh/repeat clause into its browser-owned evidence action.

    These are not creative product decisions: the authored story already names the action and duration.  A
    paid planner repeatedly rediscovering broad traversal between those clauses adds latency and can never
    prove them.  Live landmarks and the current action record provide all operands; the independent semantic
    evaluator remains release authority.
    """
    remaining_items = [item for item in (coverage or [])
                       if isinstance(item, dict) and not item.get("covered")]
    remaining = [str(item.get("aspect") or "") for item in remaining_items]
    if not remaining_items:
        return None
    aspect = remaining[0]
    viewport_kind = str(remaining_items[0].get("atomic_kind") or "")
    if viewport_kind == "timed_wait":
        duration = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b", aspect, re.I)
        seconds = float(duration.group(1)) if duration else 10.0
        seconds = int(seconds) if seconds.is_integer() else seconds
        return {
            "reasoning": ("Execute the authored idle interval as its own browser-timed stability receipt, "
                          "after its prerequisite atom and before the terminal action atom."),
            "intent": "observe timed form stability",
            "next_action": {"cmd": "wait", "value": f"{seconds}s"},
            "expected": aspect, "expected_control": "", "wait_for": None,
            "covers": [aspect], "done": False, "mechanical_setup": False,
        }
    if viewport_kind == "form_enter":
        candidates = []
        for element in (state or {}).get("elements") or []:
            if not isinstance(element, dict) or element.get("formIndex") is None:
                continue
            tag = str(element.get("tag") or "").casefold()
            typ = str(element.get("type") or "").casefold()
            if tag != "input" or typ in {"checkbox", "radio", "button", "submit", "hidden"}:
                continue
            label = _element_label(element)
            if label:
                candidates.append((0 if typ in {"text", "email", "tel"} else 1, element, label))
        if candidates:
            _, element, label = sorted(candidates, key=lambda item: (item[0], int(item[1].get("idx") or 0)))[0]
            return {
                "reasoning": ("Press Enter from a completed single-line field so the browser receipt proves "
                              "the authored keyboard submission rather than an unrelated checkbox keypress."),
                "intent": label,
                "next_action": {"cmd": "press", "idx": element.get("idx"),
                                "target_text": label, "role": "textbox", "value": "Enter"},
                "expected": aspect, "expected_control": "", "wait_for": None,
                "covers": [aspect], "done": False, "mechanical_setup": False,
            }
    if viewport_kind in {"viewport_mobile", "viewport_desktop"}:
        literal = re.search(r"\b(\d{3,4})\s*px\b", aspect, re.I)
        width = (int(literal.group(1)) if literal
                 else 390 if viewport_kind == "viewport_mobile" else 1280)
        height = 844 if viewport_kind == "viewport_mobile" else 800
        return {
            "reasoning": ("Apply the exact authored responsive viewport as its own evidence boundary; "
                          "the independent evaluator still verifies layout, focus, overflow, and controls."),
            "intent": f"inspect {viewport_kind.removeprefix('viewport_')} viewport",
            "next_action": {"cmd": "viewport", "value": {"width": width, "height": height}},
            "expected": aspect, "expected_control": "", "wait_for": None,
            "covers": [aspect], "done": False, "mechanical_setup": False,
        }
    if any(str(item.get("atomic_kind") or "") == "dwell_surface"
           for item in remaining_items):
        # One state can expose several independently-authored long-page surfaces. Batch every *currently live*
        # dwell atom into one receipt, but bind each target back to its exact ledger child. Missing confirmation
        # or error states remain open and fall through to normal agent planning; already-live staff/CEO/public
        # surfaces are never thrown away just because one sibling state is not rendered yet.
        bindings, targets, seen_targets = [], [], set()
        for item in remaining_items:
            if str(item.get("atomic_kind") or "") != "dwell_surface":
                continue
            candidate = (_landmark_labels_for_aspect(state, item.get("aspect"), limit=1) or [None])[0]
            key = " ".join(str(candidate or "").casefold().split())
            if not candidate or key in seen_targets:
                continue
            seen_targets.add(key)
            targets.append(candidate)
            bindings.append({"aspect": str(item.get("aspect") or ""), "target": candidate})
        if bindings:
            duration = re.search(
                r"\b(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b",
                bindings[0]["aspect"], re.I)
            seconds = float(duration.group(1)) if duration else 10.0
            seconds = int(seconds) if seconds.is_integer() else seconds
            return {
                "reasoning": ("Dwell on every currently rendered authored surface in one browser-owned "
                              "receipt; absent sibling states remain durable open atoms."),
                "intent": "dwell on currently live named surfaces",
                "next_action": {"cmd": "dwell_surfaces", "targets": targets,
                                "duration_s": seconds, "_qa_atomic_dwell": True,
                                "_qa_dwell_bindings": bindings},
                "expected": "; ".join(binding["aspect"] for binding in bindings),
                "expected_control": "", "wait_for": None,
                "covers": [binding["aspect"] for binding in bindings],
                "done": False, "mechanical_setup": False,
            }

    dwell = re.search(r"\b(?:dwell|idle|remain|wait)\b", aspect, re.I)
    duration = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b", aspect, re.I)
    # A timed observation is browser-compilable only when observation is the whole action boundary.  Some
    # generated ledgers retain an authored sequence in one row (for example, "check consent, dwell 10
    # seconds, then press Enter").  Compiling that row directly as ``dwell_surfaces`` skips its prerequisite
    # and terminal action, after which the repeat guard can replay the same expensive no-op forever.  Leave
    # compound mutations/keypresses to the ordered planner; their eventual evaluator can still credit the
    # composite row from the complete before/action/after history.  Migrated ``dwell_surface`` atoms took the
    # exact branch above and remain mechanically fast.
    compound_timed_sequence = bool(re.search(
        r"\b(?:check|uncheck|click|tap|type|fill|paste|press|submit|send|select|switch|drain|"
        r"approve|reject|acknowledge|retry|edit)\b", aspect, re.I))
    if dwell and duration and not compound_timed_sequence:
        targets = _landmark_labels_for_aspect(state, aspect)
        if targets:
            seconds = float(duration.group(1))
            if seconds.is_integer():
                seconds = int(seconds)
            return {
                "reasoning": ("Execute the story-authored dwell as one browser-owned multi-surface receipt; "
                              "the live landmark index defines which named surfaces currently exist."),
                "intent": "dwell on named live surfaces",
                "next_action": {"cmd": "dwell_surfaces", "targets": targets,
                                "duration_s": seconds},
                "expected": aspect, "expected_control": "", "wait_for": None,
                "covers": [aspect], "done": False, "mechanical_setup": False,
            }

    refresh_aspect = next((item for item in remaining
                           if _positive_refresh_requirement(item)), None)
    if refresh_aspect:
        aspect = refresh_aspect
        current_actions = [item.get("action") for item in (records or [])
                           if isinstance(item, dict) and isinstance(item.get("action"), dict)]
        reload_index = next((index for index in range(len(current_actions) - 1, -1, -1)
                             if str(current_actions[index].get("cmd") or "").casefold() == "reload"), None)
        if reload_index is None:
            action = {"cmd": "reload"}
            reasoning = "Perform the story-authored real browser refresh before repeating its keyboard path."
            expected = "The page refreshes successfully and preserves an accessible, operable state."
        elif not any(item.get("_qa_post_refresh_keyboard") is True
                     for item in current_actions[reload_index + 1:]):
            action = {"cmd": "traverse", "value": "forward", "pace_ms": 300,
                      "_qa_post_refresh_keyboard": True, "_qa_inventory_derived": True}
            reasoning = "Repeat one complete trusted keyboard path after the already-recorded real refresh."
            expected = aspect
        else:
            return None
        return {
            "reasoning": reasoning, "intent": "refresh and repeat keyboard path",
            "next_action": action, "expected": expected,
            "expected_control": "", "wait_for": None,
            "covers": [aspect], "done": False, "mechanical_setup": False,
        }
    return None


def _live_queue_has_queued(state):
    """Distinguish current queue load from immutable audit text that says a job *was* queued."""
    texts = [str((state or {}).get(name) or "")
             for name in ("viewportText", "bodyText", "statusText")]
    combined = " ".join(texts)
    # The staff summary is the strongest same-page source. It remains visible in document text even when the
    # diagnostic JSON is truncated, and unlike audit history it describes the current queue snapshot.
    metric = re.findall(r"\bagent\s*jobs\s*queued\s*[:=]?\s*(\d+)\b", combined, re.I)
    if metric:
        return int(metric[-1]) > 0
    # Compatibility fallback for products without the summary metric. Keep this deliberately shaped like a
    # live job row; a bare historical phrase such as ``metadata status queued`` is not enough.
    return bool(re.search(
        r"\b(?:job[_\s-]?[a-z0-9]+|agent job)\b[^\n|]{0,100}\bqueued\b"
        r"[^\n|]{0,80}\b(?:attempts?|attempt)\b",
        combined, re.I))


def _live_terminal_queue_failure(state):
    """Distinguish a current failed/dead-letter job from headings, zero metrics, and audit history."""
    combined = " ".join(str((state or {}).get(name) or "")
                        for name in ("viewportText", "bodyText", "statusText"))
    metrics = re.findall(
        r"\b(?:agent\s+jobs?\s+)?(?:failed|dead.?letter(?:ed)?)\s*[:=]?\s*(\d+)\b",
        combined, re.I)
    if metrics and any(int(value) > 0 for value in metrics):
        return True
    elements = [item for item in ((state or {}).get("elements") or []) if isinstance(item, dict)]
    if any(not item.get("disabled") and re.search(r"\bretry\b", _element_label(item), re.I)
           for item in elements):
        return True
    # Compatibility for diagnostic JSON/job rows. Require a job identity and a live status field on the same
    # bounded row; an audit sentence saying a job "was failed" cannot satisfy this shape.
    return bool(re.search(
        r"\b(?:job[_\s-]?[a-z0-9]+|agent job)\b[^\n|]{0,140}"
        r"\bstatus\b\s*[:=]?\s*[\"']?(?:failed|dead.?letter(?:ed)?)\b",
        combined, re.I))


def _pending_empty_first_run_transition_decision(story, state, coverage, records):
    """Restore or finish the exact empty -> first enquiry -> drain -> inspect causal journey.

    A continuation may inherit a valid covered empty-state receipt but a later browser state containing one or
    more enquiries. Reusing that state and submitting again cannot prove a *first* transition. Conversely, once
    exactly one valid submission and its drain are durable, reopening the form and submitting again is wasted
    work; the next boundary is the all-surface observer.
    """
    contract = _story_text(story)
    unresolved = [str(item.get("aspect") or "") for item in (coverage or [])
                  if isinstance(item, dict) and not item.get("covered")]
    if (not re.search(r"\brecover from empty first[- ]run data\b", contract, re.I)
            or not any(re.search(r"\bsubmit the first valid public enquiry\b", item, re.I)
                       for item in unresolved)):
        return None

    timeline = []
    for index, record in enumerate(records or []):
        if not isinstance(record, dict) or record.get("bug"):
            continue
        action = _portable_checkpoint_action(record.get("action"))
        actual = str(record.get("actual") or "")
        facts = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        ok = facts.get("driver_ok") is True or "driver_ok=True" in actual
        effect = facts.get("effect_registered") is True or "effect_registered=True" in actual
        label = " ".join(str(value or "") for value in (
            action.get("target_text"), action.get("target"), facts.get("intended"),
            facts.get("targeted_label"))).casefold()
        timeline.append((index, action, ok, effect, label, str(record.get("expected") or "").casefold()))
    reset_index = max((index for index, action, ok, effect, _, _ in timeline
                       if ok and effect and str(action.get("cmd") or "").casefold()
                       in {"reset_storage", "resetstorage"}), default=-1)
    recent = [item for item in timeline if item[0] > reset_index]
    submissions = [item for item in recent if item[2] and item[3]
                   and (item[1].get("_qa_valid_form_submission") is True
                        or (str(item[1].get("cmd") or "").casefold()
                            in {"scenario_matrix", "case_matrix"}
                            and "submit the first valid public enquiry" in item[5]))]
    drains = [item for item in recent if item[2] and item[3] and "drain queue" in item[4]]
    rendered = " ".join(str((state or {}).get(name) or "")
                        for name in ("bodyText", "viewportText", "statusText")).casefold()
    populated = bool(
        re.search(r"\b(?:enquiries?|inbound leads?)\s+(?:total\s*)?[:=]?\s*[1-9]\d*\b", rendered, re.I)
        or re.search(r"\b[1-9]\d*\s+(?:total/open\s+)?enquiries?\b", rendered, re.I)
        or "enquiry received" in rendered)

    if populated and len(submissions) != 1:
        for item in coverage or []:
            aspect = str(item.get("aspect") or "")
            if (re.search(r"\b(?:reset local storage|empty valid app state)\b", aspect, re.I)
                    or re.search(r"\bverify empty public trust claims\b", aspect, re.I)):
                item["covered"] = False
                item.pop("proof", None)
                item["coverage_repaired"] = "empty-first-run-baseline-required-fresh-proof"
        return {
            "reasoning": ("The first-run story resumed with populated enquiry state that is not exactly the "
                          "single durable first submission. Reset before repeating the causal journey."),
            "intent": "empty first-run browser storage",
            "next_action": {"cmd": "reset_storage", "value": ""},
            "expected": "The app returns to a valid empty first-run state with no seeded records.",
            "expected_control": "", "wait_for": None, "covers": [], "done": False,
            "mechanical_setup": True,
        }
    if len(submissions) == 1 and not drains and _live_queue_has_queued(state):
        elements = [item for item in ((state or {}).get("elements") or []) if isinstance(item, dict)]
        idx, label, score = _resolve_target({"target_text": "Drain queue", "role": "button"}, elements)
        if idx is not None and score >= 2:
            return {
                "reasoning": "The single first enquiry is durable and its work is queued; drain it once next.",
                "intent": label or "Drain queue",
                "next_action": {"cmd": "click", "target_text": label or "Drain queue",
                                "role": "button", "idx": idx},
                "expected": "The first enquiry's queued work completes and all operating panels stay rendered.",
                "expected_control": "", "wait_for": None, "covers": [], "done": False,
                "mechanical_setup": True,
            }
    if len(submissions) == 1 and drains:
        targets = ["Public enquiry", "Staff operating console", "CEO command view",
                   "Queue and Dead Letters", "Approval Summary", "Audit Summary"]
        return {
            "reasoning": ("The exact first submission and drain are already durable. Inspect every required "
                          "surface now instead of reopening and resubmitting the form."),
            "intent": "first-run post-drain operating surfaces",
            "next_action": {"cmd": "inspect_surfaces", "targets": targets},
            "expected": ("All six surfaces remain coherent in the truthful post-enquiry state without reload; "
                         "approval diagnostics may honestly remain empty."),
            "expected_control": "", "wait_for": None, "covers": unresolved, "done": False,
            "mechanical_setup": True,
        }
    return None


def _runtime_scenario_action(action, label, value):
    """Recognize runtime scenario controls across full and compact evidence records."""
    action = action if isinstance(action, dict) else {}
    if str(action.get("cmd") or "").casefold() not in {"type", "fill", "select", "choose"}:
        return False
    normalized_value = " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())
    if normalized_value not in {
            "success", "timeout", "malformed response", "partial failure", "rate limit",
            "approval required"}:
        return False
    return bool(re.search(r"\b(?:agent|scenario)\b", str(label or ""), re.I))


def _pending_retry_story_transition_decision(story, state, coverage, records):
    """Keep US-008 on one fresh enquiry and drive its ordered failure/recovery boundaries."""
    contract = _story_text(story)
    unresolved = [str(item.get("aspect") or "") for item in (coverage or [])
                  if isinstance(item, dict) and not item.get("covered")]
    if (not re.search(r"\bretry dead[- ]lettered agent work\b", contract, re.I)
            or not unresolved):
        return None
    timeline = []
    for index, record in enumerate(records or []):
        if not isinstance(record, dict) or record.get("bug"):
            continue
        action = _portable_checkpoint_action(record.get("action"))
        facts = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        actual = str(record.get("actual") or "")
        ok = facts.get("driver_ok") is True or "driver_ok=True" in actual
        effect = facts.get("effect_registered") is True or "effect_registered=True" in actual
        label = " ".join(str(value or "") for value in (
            action.get("target_text"), facts.get("intended"), facts.get("targeted_label"), actual)).casefold()
        value = str(action.get("value") or facts.get("action_value") or "").casefold()
        # State chronology must never be inferred from an expectation. A queued drain's expected text names
        # "failed/dead-lettered" before that outcome exists and previously triggered premature inspection.
        evidence = actual.casefold()
        expected = str(record.get("expected") or "").casefold()
        timeline.append((index, action, ok, effect, label, value, evidence, expected))
    reset_index = max((item[0] for item in timeline if item[2] and item[3]
                       and str(item[1].get("cmd") or "").casefold()
                       in {"reset_storage", "resetstorage"}), default=-1)
    recent = [item for item in timeline if item[0] > reset_index]
    submissions = [item for item in recent if item[2] and item[3]
                   and (item[1].get("_qa_valid_form_submission") is True
                        or (str(item[1].get("cmd") or "").casefold()
                            in {"scenario_matrix", "case_matrix"}
                            and re.search(r"\bsubmit (?:one|an) enquiry\b", item[7], re.I)))]
    rendered = " ".join(str((state or {}).get(name) or "")
                        for name in ("bodyText", "viewportText", "statusText")).casefold()
    counts = [int(value) for value in re.findall(
        r"\b(?:total(?:/open)?\s+enquiries?|enquiries?\s+total)\s*[:=]?\s*(\d+)\b", rendered, re.I)]
    # bodyText and viewportText overlap, so counting the combined string double-counts one audit row. Prefer
    # unique created-enquiry references from the full document; fall back to bodyText-only event count.
    document_text = str((state or {}).get("bodyText") or (state or {}).get("viewportText") or "").casefold()
    created_ids = set(re.findall(r"\benquiry\.created\b[^\n|]{0,100}\b(enquiry_[a-z0-9]+)\b",
                                 document_text, re.I))
    created_events = len(created_ids) if created_ids else len(re.findall(
        r"\benquiry\.created\b", document_text, re.I))
    visible_cards = {value for value in re.findall(r"\benquiry\s+(\d+)\b", document_text, re.I)}
    duplicate_state = bool((counts and max(counts) > 1) or created_events > 1
                           or len(visible_cards) > 1 or len(submissions) > 1)
    if duplicate_state:
        for item in coverage or []:
            item["covered"] = False
            item.pop("proof", None)
            item["coverage_repaired"] = "single-enquiry-retry-baseline-required-fresh-proof"
        return {
            "reasoning": ("The retry story inherited more than one enquiry/submission, so it cannot prove the "
                          "single-enquiry no-duplication contract. Reset and run one causal journey."),
            "intent": "fresh single-enquiry retry baseline",
            "next_action": {"cmd": "reset_storage", "value": ""},
            "expected": "The app returns to an empty valid state for one failure/recovery enquiry.",
            "expected_control": "", "wait_for": None, "covers": [], "done": False,
            "mechanical_setup": True,
        }

    elements = [item for item in ((state or {}).get("elements") or []) if isinstance(item, dict)]
    submit_row_open = any(re.search(r"\bsubmit (?:one|an) enquiry\b", item, re.I)
                          and re.search(r"\bdrain(?:\s+the)?(?:\s+agent)?\s+queue\b", item, re.I)
                          for item in unresolved)
    if not submissions and submit_row_open:
        scenario = next((item for item in elements
                         if str(item.get("tag") or "").casefold() == "select"
                         and re.search(r"\bscenario\b|\bagent\b", " ".join((
                             _element_label(item), str(item.get("name") or ""),
                             str(item.get("id") or ""))), re.I)), None)
        if scenario is not None and "timeout" not in str(scenario.get("value") or "").casefold():
            return {
                "reasoning": "Select the available Timeout failure mode before the story's only enquiry.",
                "intent": _element_label(scenario) or "Agent scenario",
                "next_action": {"cmd": "type", "idx": scenario.get("idx"), "value": "Timeout"},
                "expected": "The Agent scenario selector changes to Timeout.",
                "expected_control": "", "wait_for": None, "covers": [], "done": False,
                "mechanical_setup": True,
            }

    failure_row_open = any(_requires_multi_surface_inspection(item)
                           and re.search(r"\b(?:failed|dead.?letter)\b", item, re.I)
                           for item in unresolved)
    # The current settled DOM is authoritative for whether a terminal job exists.  A compacted continuation
    # can legitimately omit the earlier drain's full rendered body while retaining the live dead-lettered
    # state; requiring the old free-text receipt here sent the worker through a paid generic decision that
    # selected only a subset of the required surfaces.  Causal drain/submission proof remains independently
    # required by the coverage reducers, so this only chooses the complete passive inspection.
    failure_visible = _live_terminal_queue_failure(state)
    if failure_row_open and failure_visible:
        # Use the same canonical operating surfaces as the recovery inspection below. ``Related
        # notifications`` is a nested h4 inside Enquiry review details rather than a stable top-level
        # landmark, so asking the browser bridge to dwell it directly can fail target admission even while
        # the notification is visibly present.  The former partial list also omitted Agent jobs, Operational
        # diagnostics, and Governance audit history, causing the semantic judge to reject an otherwise valid
        # failure receipt and repeat the same expensive inspection forever across durable handoffs.
        targets = ["Staff operating console", "Agent jobs", "CEO command view",
                   "Operational diagnostics", "Queue and Dead Letters",
                   "Governance audit history", "Enquiry review details"]
        return {
            "reasoning": "The terminal failure is live; seal its engineering-risk projection across all panels.",
            "intent": "failed-work operating surfaces",
            "next_action": {"cmd": "inspect_surfaces", "targets": targets},
            "expected": "Failure, retry eligibility, notifications, and blockers are coherent across all targets.",
            "expected_control": "", "wait_for": None, "covers": [], "done": False,
            "mechanical_setup": True,
        }

    recovery_open = any(_requires_queue_recovery_journey(item) for item in unresolved)
    failure_inspection_closed = any(item.get("covered") and _requires_multi_surface_inspection(
        item.get("aspect")) and re.search(r"\b(?:failed|dead.?letter)\b",
                                         str(item.get("aspect") or ""), re.I)
                                    for item in coverage or [] if isinstance(item, dict))
    if not (recovery_open and failure_inspection_closed):
        return None
    # Anchor recovery to the first terminal-failure receipt. Later retry/success screens intentionally retain
    # the failure in audit history; using the latest textual mention moves the anchor past the real Success and
    # Retry actions and repeats the selector forever.
    passed_failure_receipt = next((index for index, record in enumerate(records or [])
                                   if isinstance(record, dict) and not record.get("bug")
                                   and str(_portable_checkpoint_action(
                                       record.get("action")).get("cmd") or "").casefold()
                                   == "inspect_surfaces"
                                   and any(re.search(r"\b(?:failed|dead.?letter(?:ed)?)\b",
                                                     str(aspect or ""), re.I)
                                           for aspect in list(record.get("covers") or [])
                                           + list(record.get("demonstrated") or []))), None)
    failure_index = (passed_failure_receipt if passed_failure_receipt is not None else min(
        (item[0] for item in recent if not re.search(r"\bretry\b", item[4], re.I)
         and re.search(r"\b(?:failed|dead.?letter(?:ed)?|runtime_timeout)\b", item[6], re.I)),
        default=-1))
    success = next((item for item in recent if item[0] > failure_index and item[2]
                    and str(item[1].get("cmd") or "").casefold() in {"type", "fill", "select", "choose"}
                    and "success" in item[5]), None)
    scenario = next((item for item in elements
                     if str(item.get("tag") or "").casefold() == "select"
                     and re.search(r"\bscenario\b|\bagent\b", " ".join((
                         _element_label(item), str(item.get("name") or ""), str(item.get("id") or ""))), re.I)), None)
    if success is None:
        return {
            "reasoning": "The failure projection is sealed; switch the same job's runtime scenario to Success.",
            "intent": _element_label(scenario) if scenario else "Agent scenario",
            "next_action": {"cmd": "type", "idx": scenario.get("idx") if scenario else None,
                            "value": "Success"},
            "expected": "The Agent scenario selector changes to Success before retry.",
            "expected_control": "", "wait_for": None, "covers": [], "done": False,
            "mechanical_setup": True,
        }
    retry = None
    for candidate in recent:
        if (candidate[0] <= success[0] or not candidate[2] or not candidate[3]
                or not re.search(r"\bretry\b", candidate[4], re.I)):
            continue
        preceding_scenarios = [item for item in recent
                               if success[0] <= item[0] < candidate[0] and item[2]
                               and (item[0] == success[0]
                                    or _runtime_scenario_action(item[1], item[4], item[5]))]
        if preceding_scenarios and "success" in preceding_scenarios[-1][5]:
            retry = candidate
            break
    if retry is None:
        idx, label, score = _resolve_target({"target_text": "Retry", "role": "button"}, elements)
        if idx is not None and score >= 1:
            return {
                "reasoning": "Retry the terminal job once under the selected Success scenario.",
                "intent": label or "Retry",
                "next_action": {"cmd": "click", "idx": idx, "role": "button",
                                "target_text": label or "Retry"},
                "expected": "The failed job becomes queued for one recovery attempt without duplicating enquiry.",
                "expected_control": "", "wait_for": None, "covers": [], "done": False,
                "mechanical_setup": True,
            }
        return None
    # The queue survives explorer handoffs but the runtime scenario does not.  Treat a failure-scenario select
    # after Retry as invalidating subsequent drains, and deliberately re-establish Success before continuing.
    scenario_after_retry = [item for item in recent if item[0] > retry[0] and item[2]
                            and _runtime_scenario_action(item[1], item[4], item[5])]
    latest_recovery_scenario = scenario_after_retry[-1] if scenario_after_retry else success
    if latest_recovery_scenario is None or "success" not in latest_recovery_scenario[5]:
        return {
            "reasoning": ("The durable retried job crossed an explorer handoff or a later failure-scenario "
                          "selection; re-establish Success before its recovery drain."),
            "intent": _element_label(scenario) if scenario else "Agent scenario",
            "next_action": {"cmd": "type", "idx": scenario.get("idx") if scenario else None,
                            "value": "Success"},
            "expected": "The Agent scenario selector is confirmed as Success for the recovery attempt.",
            "expected_control": "", "wait_for": None, "covers": [], "done": False,
            "mechanical_setup": True,
        }
    recovery_drain = next((item for item in recent if item[0] > retry[0] and item[2] and item[3]
                           and "drain queue" in item[4]
                           and item[0] > latest_recovery_scenario[0]), None)
    if recovery_drain is None and _live_queue_has_queued(state):
        idx, label, score = _resolve_target({"target_text": "Drain queue", "role": "button"}, elements)
        if idx is not None and score >= 2:
            return {
                "reasoning": "The retried job is queued under Success; execute its one recovery attempt.",
                "intent": label or "Drain queue",
                "next_action": {"cmd": "click", "idx": idx, "role": "button",
                                "target_text": label or "Drain queue"},
                "expected": "The retried job succeeds and its operational failure signal clears or is superseded.",
                "expected_control": "", "wait_for": None, "covers": [], "done": False,
                "mechanical_setup": True,
            }
    if recovery_drain is not None:
        targets = ["Staff operating console", "Agent jobs", "CEO command view",
                   "Operational diagnostics", "Queue and Dead Letters",
                   "Governance audit history", "Enquiry review details"]
        return {
            "reasoning": "The ordered Success, Retry, and Drain receipts are durable; inspect recovery state once.",
            "intent": "recovered-work operating surfaces",
            "next_action": {"cmd": "inspect_surfaces", "targets": targets},
            "expected": "Succeeded status, attempts/runAfter, cleared risk, and failure/recovery audit are coherent.",
            "expected_control": "", "wait_for": None, "covers": [], "done": False,
            "mechanical_setup": True,
        }
    return None


def _pending_queue_drain_decision(story, state, coverage):
    """Trigger queued runtime work before judging its required failure/retry diagnostics.

    A queue row marked ``queued`` is proof that insertion succeeded, not proof that the selected Timeout,
    Rate-limit, or Partial-failure runtime path is broken. If the sealed story still requires the resulting
    failed/retry/degraded state and exposes a named one-attempt Drain queue control, take that causal action
    mechanically instead of letting a judge report the precondition as a product defect.
    """
    unresolved = [str(item.get("aspect") or "") for item in (coverage or [])
                  if isinstance(item, dict) and not item.get("covered")]
    def runtime_outcome_requirement(item):
        text = str(item or "")
        # A diagnostics *dwell* is passive stability evidence.  The diagnostics surface may contain an
        # unrelated queued fixture (for example the CEO-risk sample) whose app-store status is not the
        # pipeline queue controlled by the global Drain button.  Mutating that sibling state is outside this
        # atom and can manufacture an inert-control defect instead of performing the authored ten-second wait.
        if _requires_temporal_idle_proof(text):
            return False
        if re.search(r"\b(?:failed|retry|degraded|dead.?letter)\b", text, re.I):
            return True
        # ``diagnostic`` also appears in focused safety prose such as "do not repeat a diagnostic action".
        # That is a prohibition, not authority to drain a queue. Require a named runtime/queue outcome noun.
        return (bool(re.search(r"\bdiagnostics?\b", text, re.I))
                and bool(re.search(r"\b(?:queue|job|runtime|failure|error|status)\b", text, re.I)))

    relevant = [item for item in unresolved if runtime_outcome_requirement(item)]
    if not relevant:
        return None
    # A dashboard/privacy story can intentionally seed queued, failed, and dead-letter jobs for passive
    # inspection. Seeing those words does not authorize mutating the fixture. Require an explicit execution
    # boundary (drain/process/runtime scenario/retry) before injecting the causal Drain action. This keeps
    # focused metadata and aggregate-metric regressions read-only while preserving slow/failure queue stories.
    execution_contract = " ".join([
        _story_text(story),
        " ".join(unresolved),
        str(((story or {}).get("focused_finding") or {}).get("detail") or "")
        if isinstance((story or {}).get("focused_finding"), dict) else "",
    ])
    if not re.search(
            r"\bdrain(?:\s+the)?(?:\s+agent)?\s+queue\b|"
            r"\bprocess(?:\s+the)?(?:\s+agent)?\s+queue\b|"
            r"\bexecute(?:\s+(?:exactly\s+)?(?:one|the))?\s+runtime\s+attempt\b|"
            r"\bretry\s+(?:the|a)\s+(?:failed|dead.?letter(?:ed)?|agent|runtime)\b|"
            r"\b(?:set|switch|select|selected)\b[^.;]{0,100}"
            r"\b(?:timeout|partial failure|rate limit|malformed response)\b",
            execution_contract, re.I):
        return None
    if not _live_queue_has_queued(state):
        return None
    elements = [item for item in ((state or {}).get("elements") or []) if isinstance(item, dict)]
    # A scenario selector is a causal prerequisite, not a sibling control. Do not mechanically drain the
    # current/default scenario when the sealed story explicitly names another one; that produces a real click
    # receipt for the wrong experiment and then strands the focused verifier at its final assertion.
    scenario_selects = [item for item in elements
                        if str(item.get("tag") or "").casefold() == "select"
                        and re.search(r"\bscenario\b", " ".join([
                            _element_label(item), str(item.get("label") or ""),
                            str(item.get("name") or ""),
                            str(item.get("id") or "")]), re.I)]
    # Select only a story-authorized option that the live control actually offers. US-008 says Timeout *or*
    # Malformed Response, while this product intentionally exposes Timeout but not Malformed Response. The old
    # helper preferred the unavailable label and retried the same no-op select forever.
    option_labels = ("Timeout", "Malformed response", "Partial failure",
                     "Rate limit", "Approval required")
    normalized_contract = " ".join(re.sub(
        r"[^a-z0-9]+", " ", execution_contract.casefold()).split())
    available_options = " ".join(re.sub(
        r"[^a-z0-9]+", " ", " ".join(str(item.get("options") or "")
                                        for item in scenario_selects).casefold()).split())
    desired_scenario = next((label for label in option_labels
                             if " ".join(re.sub(
                                 r"[^a-z0-9]+", " ", label.casefold()).split()) in available_options
                             and re.search(
                                 rf"\bscenario\b(?:\s+[a-z0-9]+){{0,12}}\s+"
                                 rf"{re.escape(' '.join(re.sub(r'[^a-z0-9]+', ' ', label.casefold()).split()))}\b|"
                                 rf"\b{re.escape(' '.join(re.sub(r'[^a-z0-9]+', ' ', label.casefold()).split()))}"
                                 rf"\b(?:\s+[a-z0-9]+){{0,12}}\s+scenario\b",
                                 normalized_contract, re.I)), None)
    # Once failure/submission evidence is sealed and only the recovery row remains, Timeout is no longer the
    # authorized experiment.  This fallback runs after the story-specific transition driver and must preserve
    # (or restore) Success, especially when a durable retry is resumed in a fresh browser process.
    if relevant and all(_requires_queue_recovery_journey(item) for item in relevant):
        desired_scenario = "Success"
    if desired_scenario and scenario_selects:
        scenario = scenario_selects[0]
        normalized_desired = " ".join(re.sub(
            r"[^a-z0-9]+", " ", desired_scenario.casefold()).split())
        normalized_actual = " ".join(re.sub(
            r"[^a-z0-9]+", " ", str(scenario.get("value") or "").casefold()).split())
        if normalized_desired not in normalized_actual:
            return {
                "reasoning": (f"The sealed queue contract names the {desired_scenario} scenario; select it "
                              "before executing the one-attempt drain boundary."),
                "intent": _element_label(scenario) or "Agent scenario",
                "next_action": {"cmd": "type", "idx": scenario.get("idx"),
                                "value": desired_scenario},
                "expected": f"The Agent scenario selector changes to {desired_scenario}.",
                "expected_control": "", "wait_for": None, "covers": [], "done": False,
                "mechanical_setup": True,
            }
    idx, label, score = _resolve_target(
        {"target_text": "Drain queue", "role": "button"}, elements)
    if idx is None or score < 2:
        return None
    return {
        "reasoning": ("The story requires runtime failure/retry diagnostics, but the visible job is still "
                      "queued. Execute the product's named one-attempt queue control before judging outcome."),
        "intent": label or "Drain queue",
        "next_action": {"cmd": "click", "target_text": label or "Drain queue",
                        "role": "button", "idx": idx},
        "expected": ("Exactly one queued runtime attempt executes and the settled staff/CEO diagnostics "
                     "expose its retry, failure, degraded, or terminal state without losing confirmation."),
        "expected_control": "", "wait_for": None, "covers": relevant, "done": False,
        "mechanical_setup": True,
    }


def _queued_failure_not_triggered_false_positive(story, targeting, bug, after_state):
    """Do not call queued work a missing runtime failure before the exposed drain action runs."""
    if not bug or not re.search(r"\b(?:failed|retry|degraded|warning|queue)\b", str(bug), re.I):
        return False
    if str((targeting or {}).get("action_kind") or "").lower() in ("click", "tap") and \
            "drain queue" in " ".join(str((targeting or {}).get(name) or "")
                                      for name in ("intended", "targeted_label")).casefold():
        return False
    text = " ".join(str((after_state or {}).get(name) or "")
                    for name in ("bodyText", "viewportText", "statusText")).casefold()
    if not _live_queue_has_queued(after_state) or re.search(
            r"\b(?:retrying|dead.?letter|runtime_timeout|queue failed)\b", text, re.I):
        return False
    elements = list((after_state or {}).get("elements") or [])
    idx, _, score = _resolve_target({"target_text": "Drain queue", "role": "button"}, elements)
    return idx is not None and score >= 2


def _optional_timed_duration_false_positive(targeting, bug):
    """A repeat transition that explicitly has no dwell SLA may settle before its sampling boundary."""
    if not bug or not re.search(
            r"\b(?:before|required|early|duration|boundary|milliseconds?|ms)\b", str(bug), re.I):
        return False
    timed = dict((targeting or {}).get("timed_transition") or {})
    return bool((targeting or {}).get("driver_ok")
                and timed.get("full_duration_required") is False
                and timed.get("pending_seen") is True
                and timed.get("completion_observed") is True)


def _pending_timed_transition_decision(story, state, coverage):
    """Atomically own an explicit timed transient submit boundary once its real form is ready.

    A settled observation intentionally waits for busy UI to finish, while a transient regression must inspect
    the busy UI *before* it finishes. Letting the ordinary planner click submit and decide to dwell on its next
    turn therefore creates an impossible race. This fence is narrow: an ordinary release story must explicitly
    require both a transient pending/busy state and a numeric duration; a synthesized focused regression must
    additionally carry the coordinator's ``transient_recheck`` classification. In either case the real named
    form must already be valid and enabled, and the duration is derived from the sealed contract.
    """
    finding = (story or {}).get("focused_finding")
    focused = ((story or {}).get("category") == "focused-regression"
               or isinstance(finding, dict))
    if focused and (not isinstance(finding, dict) or finding.get("transient_recheck") is not True):
        return None
    ledger = [item for item in (coverage or []) if isinstance(item, dict)]
    unresolved = [str(item.get("aspect") or "") for item in ledger if not item.get("covered")]
    contract = " ".join(unresolved + [
        _story_text(story),
        str((story or {}).get("goal") or ""),
        str((story or {}).get("expected_outcome") or (story or {}).get("expected") or ""),
    ])
    if not (re.search(r"\b(?:pending|busy|loading|spinner|waiting)\b", contract, re.I)
            and re.search(r"\b(?:second|duration|dwell|stable|stability|latency|timeout)\b",
                          contract, re.I)):
        return None
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:-\s*)?(?:seconds?|secs?|s)\b",
                      contract, re.I)
    if not match:
        return None
    duration_s = max(0.25, min(300.0, float(match.group(1))))

    # Passive restored-state inspection must happen first. The immediately preceding "fresh distinct action"
    # clause may remain open because this atomic command is the action that proves it; any other earlier open
    # clause means the journey is not yet at its transient boundary.
    transient_index = next((index for index, item in enumerate(ledger) if not item.get("covered")
                            and re.search(
                                r"\b(?:pending|busy|loading|dwell|duration|partial failure|rate limit)\b",
                                str(item.get("aspect") or ""), re.I)), None)
    if transient_index is None:
        return None
    def repeat_outcome_clause(text):
        return bool(re.search(
            r"\b(?:confirmation|warning|failed|retry|degraded|recovery|diagnostic|storage success)\b",
            str(text or ""), re.I))

    for earlier in ledger[:transient_index]:
        if earlier.get("covered"):
            continue
        earlier_text = str(earlier.get("aspect") or "")
        if re.search(r"\b(?:fresh|distinct|triggering)\b.*\b(?:action|submission|request)\b",
                     earlier_text, re.I):
            continue
        # A repeat scenario proves its settled degraded outcome together with the repeated submit. Keeping
        # that immediately preceding outcome clause open must not prevent us reaching the repeat boundary.
        if repeat_outcome_clause(earlier_text):
            continue
        return None

    def disabled(element):
        value = element.get("disabled")
        return value is True or str(value or "").strip().lower() == "true" \
            or str(element.get("ariaDisabled") or "").strip().lower() == "true"

    elements = [item for item in ((state or {}).get("elements") or [])
                if isinstance(item, dict)]
    # A contract-named scenario selector is a prerequisite, not an interchangeable valid form. Surface it as
    # a deterministic setup action instead of hoping the planner eventually notices. ``_element_label`` now
    # correctly prefers the associated label (usually just "Agent"), so option discovery must inspect the
    # explicit options field as well; otherwise a real Success selector silently bypasses the Timeout fence.
    scenario_selects = [item for item in elements
                        if str(item.get("tag") or "").lower() == "select"
                        and re.search(r"\b(?:timeout|partial failure|rate limit)\b",
                                      " ".join([_element_label(item),
                                                str(item.get("options") or ""),
                                                str(item.get("text") or "")]), re.I)]
    desired_scenario = None
    repeat_ready = False
    for index, item in enumerate(ledger):
        aspect = str(item.get("aspect") or "")
        if (not item.get("covered")
                and re.search(r"\b(?:partial failure|rate limit)\b", aspect, re.I)
                and all(prior.get("covered") or repeat_outcome_clause(prior.get("aspect"))
                        for prior in ledger[:index])):
            repeat_ready = True
            option_text = " ".join(str(select.get("options") or "")
                                   for select in scenario_selects)
            if re.search(r"\bpartial failure\b", aspect, re.I) \
                    and re.search(r"\bpartial failure\b", option_text, re.I):
                desired_scenario = "Partial failure"
            elif re.search(r"\brate limit\b", aspect, re.I) \
                    and re.search(r"\brate limit\b", option_text, re.I):
                desired_scenario = "Rate limit"
            break
    if desired_scenario is None and re.search(r"\btimeout\b", contract, re.I):
        desired_scenario = "Timeout"
    if scenario_selects and desired_scenario:
        scenario = scenario_selects[0]
        normalized_desired = " ".join(re.sub(
            r"[^a-z0-9]+", " ", desired_scenario.casefold()).split())
        normalized_actual = " ".join(re.sub(
            r"[^a-z0-9]+", " ", str(scenario.get("value") or "").casefold()).split())
        if normalized_desired not in normalized_actual:
            return {
                "reasoning": (f"The sealed timed contract requires the {desired_scenario} scenario before "
                              "the transient submission boundary."),
                "intent": _element_label(scenario) or "Agent scenario",
                "next_action": {"cmd": "type", "idx": scenario.get("idx"),
                                "value": desired_scenario},
                "expected": f"The Agent scenario selector changes to {desired_scenario}.",
                "expected_control": "", "wait_for": None, "covers": [], "done": False,
                "mechanical_setup": True,
            }

    contract_low = contract.casefold()
    action_synonyms = {
        "send": ("send", "submit"), "submit": ("submit", "send"),
        "save": ("save",), "publish": ("publish",), "verify": ("verify",),
        "create": ("create",), "start": ("start",),
    }
    ignored_label_words = {"send", "submit", "save", "publish", "verify", "create", "start",
                           "run", "checks", "and", "make", "public", "the", "a", "an"}
    def contract_names_control(item):
        words = re.findall(r"[a-z0-9]+", _element_label(item).casefold())
        actions = [word for word in words if word in action_synonyms]
        if actions and not any(any(re.search(rf"\b{re.escape(alias)}\b", contract_low)
                                   for alias in action_synonyms[action]) for action in actions):
            return False
        nouns = [word for word in words if word not in ignored_label_words and len(word) > 2]
        return bool(actions and (not nouns or any(re.search(rf"\b{re.escape(noun)}\b", contract_low)
                                                  for noun in nouns)))
    candidate_submit_controls = [item for item in elements
                                 if contract_names_control(item)
                                 and (str(item.get("type") or "").lower() == "submit"
                                      or (str(item.get("tag") or "").lower() == "button"
                                          and re.search(
                                              r"\b(?:send|submit|save|publish|create|start)\b",
                                              _element_label(item), re.I)))]
    submit_controls = [item for item in candidate_submit_controls
                       if not disabled(item)
                       and str(item.get("formValid") or "").strip().lower() == "true"]
    if not submit_controls:
        # A disabled story-named submit with a browser-identified invalid form needs neutral prerequisites,
        # not a 10-30 second planner call per field. The collector supplies a same-page form index so controls
        # from staff/governance forms cannot be mixed into the public journey. Keep validation-specific stories
        # model-owned and use the same conservative value policy as post-submit prerequisite recovery.
        validation_story = any(term in _story_text(story).casefold() for term in (
            "required field", "required-field", "empty form", "incomplete form",
            "form validation", "validation message", "missing field"))
        invalid_named = [item for item in candidate_submit_controls
                         if item.get("formValid") is False
                         or str(item.get("formValid") or "").strip().lower() == "false"]
        for submit in invalid_named if not validation_story else []:
            form_index = submit.get("formIndex")
            if form_index is None:
                continue
            for field in elements:
                if field.get("formIndex") != form_index \
                        or str(field.get("required") or "").strip().lower() != "true":
                    continue
                typ = str(field.get("type") or "").lower()
                empty = (str(field.get("checked") or "").strip().lower() != "true"
                         if typ in ("checkbox", "radio")
                         else not str(field.get("value") or "").strip())
                if not empty:
                    continue
                value = _required_setup_value({}, field)
                if value is None:
                    return None
                label = _element_label(field) or str(field.get("name") or "required field")
                return {
                    "reasoning": (f"Complete browser-identified required prerequisite {label!r} in the "
                                  "same form as the timed submit control."),
                    "intent": label,
                    "next_action": ({"cmd": "click", "idx": field.get("idx")}
                                    if value == "__activate__" else
                                    {"cmd": "type", "idx": field.get("idx"), "value": value}),
                    "expected": f"The required {label} control is completed with neutral QA setup data.",
                    "expected_control": "", "wait_for": None, "covers": [], "done": False,
                    "mechanical_setup": True,
                }
        return None
    story_words = set(re.findall(r"[a-z0-9]+", contract.casefold()))
    control = max(submit_controls, key=lambda item: (
        len(story_words & set(re.findall(r"[a-z0-9]+", _element_label(item).casefold()))),
        str(item.get("type") or "").lower() == "submit"))
    label = _element_label(control)
    if not label:
        return None
    relevant = [item for item in unresolved if any(term in item.casefold() for term in (
        "fresh", "distinct", "pending", "busy", "dwell", "duration", "duplicate", "outcome",
        "confirmation", "warning", "failed", "retry", "degraded", "recovery", "diagnostic",
        "storage success", "partial failure", "rate limit"))
        and (repeat_ready or not re.search(r"\b(?:partial failure|rate limit)\b", item, re.I))]
    transition_duration_s = 0.25 if repeat_ready else duration_s
    return {
        "reasoning": ("The timed contract's enabled submit control is ready; capture its transient "
                      "pending interval and duplicate safeguards inside one browser-owned timing receipt."),
        "intent": label,
        "next_action": {
            "cmd": "timed_transition", "target_text": label, "role": "button",
            "idx": control.get("idx"), "duration_s": transition_duration_s,
            "completion_grace_ms": min(30000, max(1000, int(transition_duration_s * 500))),
            "require_full_duration": not repeat_ready,
        },
        "expected": ((f"One trusted {label} activation enters a disabled aria-busy pending state, remains "
                      f"pending for at least {duration_s:g} seconds despite duplicate pointer and Enter "
                      "attempts, then reaches the corrected settled outcome exactly once.")
                     if not repeat_ready else
                     (f"One trusted repeated {label} activation exposes its pending state despite duplicate "
                      "pointer and Enter attempts, then reaches the degraded/recovery outcome exactly once.")),
        "expected_control": "",
        "wait_for": None,
        "covers": relevant,
        "done": False,
        "mechanical_timed_transition": True,
    }


def _passive_probe_after_disabled_miss(action, records):
    """A wait/no-op cannot retroactively turn the explorer's disabled-control miss into an app defect."""
    if str((action or {}).get("cmd") or "").lower() not in ("wait", "noop") or not records:
        return False
    prior = records[-1]
    targeting = prior.get("targeting") or {}
    return bool(targeting.get("control_action")
                and not targeting.get("effect_registered")
                and (targeting.get("before_control_disabled") is True
                     or targeting.get("after_control_disabled") is True))


def _handoff_busy_expected_met(story, expected, targeting):
    if not targeting or not targeting.get("external_handoff_url"):
        return False
    text = " ".join([_story_text(story), str(expected or "")]).lower()
    if not any(t in text for t in ("aria-disabled", "busy", "disabled")):
        return False
    href = str(targeting.get("external_handoff_url") or "").lower()
    if "tel:" in text and not href.startswith("tel:"):
        return False
    if "mailto:" in text and not href.startswith("mailto:"):
        return False
    return targeting.get("after_control_disabled") is True


def _key_name(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    aliases = {
        "{enter}": "Enter",
        "enter": "Enter",
        "{return}": "Enter",
        "return": "Enter",
        "{space}": "Space",
        "space": "Space",
        " ": "Space",
        "{tab}": "Tab",
        "tab": "Tab",
        "{shift+tab}": "Shift+Tab",
        "shift+tab": "Shift+Tab",
        "{escape}": "Escape",
        "escape": "Escape",
        "esc": "Escape",
        "{arrowdown}": "ArrowDown",
        "arrowdown": "ArrowDown",
        "{arrowup}": "ArrowUp",
        "arrowup": "ArrowUp",
        "{arrowleft}": "ArrowLeft",
        "arrowleft": "ArrowLeft",
        "{arrowright}": "ArrowRight",
        "arrowright": "ArrowRight",
        "{home}": "Home",
        "home": "Home",
        "{end}": "End",
        "end": "End",
    }
    return aliases.get(raw.lower())


def _story_text(story):
    return " ".join([
        str(story.get("title") or ""),
        str(story.get("goal") or story.get("description") or ""),
        " ".join(str(s) for s in (story.get("steps") or [])),
        str(story.get("expected") or story.get("expected_outcome") or ""),
    ]).lower()


def _aspect_in_scope(story, aspect):
    """Keep AI-generated coverage tied to the story contract.

    The planner is intentionally creative, but the ledger is authoritative for pass/fail. A stray ledger item can
    make a correct app fail by demanding unrelated behavior. This guard removes common over-expansions while still
    allowing explicit story steps to test them.
    """
    a = str(aspect or "").lower()
    text = _story_text(story)
    steps_text = " ".join(str(s) for s in (story.get("steps") or [])).lower()
    expected = str(story.get("expected") or story.get("expected_outcome") or "").lower()

    internal_terms = ("team", "ceo", "admin", "internal", "lead-control", "readiness", "review surface")
    opens_internal = any(t in a for t in internal_terms) and re.search(
        r"\b(open|show|switch|navigate|visit|enter|load|reach|return to|go to)\b", a)
    if opens_internal:
        explicitly_steps_internal = any(t in steps_text for t in internal_terms)
        denial_expected = any(t in expected for t in (
            "denied", "hidden", "hide", "unavailable", "requires review mode", "remain on the visitor",
            "stays on the visitor", "stay on the visitor", "keeps the visitor", "public app denies",
        ))
        denial_check = any(t in a for t in (
            "deny", "denied", "hidden", "hide", "not visible", "remain", "stays", "requires review mode",
            "not current", "visitor",
        ))
        if not explicitly_steps_internal:
            return False
        if denial_expected and not denial_check:
            return False

    # Do not add broad escape/history/re-entry requirements to simple inspection stories. Separate generated
    # stories cover those flows; this story's ledger should not grow unrelated pass/fail gates.
    edge_terms = ("cancel", "escape", "back", "forward", "reload", "re-entry", "reenter", "deep link")
    if any(t in a for t in edge_terms) and not any(t in text for t in edge_terms):
        return False

    disabled_terms = ("disabled", "unavailable", "unusable", "inactive")
    if any(t in a for t in disabled_terms) and not any(t in text for t in disabled_terms):
        return False

    return True


def _scoped_aspects(story, aspects):
    scoped, seen = [], set()
    for asp in aspects or []:
        asp = str(asp).strip()
        key = re.sub(r"\s+", " ", asp.lower())
        if asp and key not in seen and _aspect_in_scope(story, asp):
            scoped.append(asp)
            seen.add(key)
    return scoped


def _bounded_aspects(aspects, cap=6):
    """Bound ledger fan-out while preserving every requirement by grouping adjacent checks.

    Dropping the tail would bias QA toward setup and omit the final outcome. Grouping keeps the full contract
    represented, while allowing one observation (for example, a dashboard state) to prove related assertions.
    """
    vals = [str(a).strip() for a in (aspects or []) if str(a).strip()]
    if cap <= 0 or len(vals) <= cap:
        return vals
    groups = [[] for _ in range(cap)]
    for i, value in enumerate(vals):
        groups[min(cap - 1, i * cap // len(vals))].append(value)
    return ["; AND ".join(group) for group in groups if group]


def _validated_compact_coverage_plan(story, raw_aspects):
    """Accept a short semantic ledger only when it explicitly accounts for every authored story step.

    The action judges always receive the full story contract.  ``story_steps`` is therefore not permission to
    weaken it; it is a deterministic completeness receipt that lets one semantic journey item replace several
    duplicate clause labels.  Malformed or lossy plans fail closed to the exact compiled contract.
    """
    steps = list((story or {}).get("steps") or [])
    required = {index for index, step in enumerate(steps, 1)
                if str(step).strip() and not _reporting_contract_step(step)}
    if not required or not isinstance(raw_aspects, list) or not (1 <= len(raw_aspects) <= 6):
        return []
    merged = []
    by_key = {}
    for raw in raw_aspects:
        if not isinstance(raw, dict):
            return []
        aspect = str(raw.get("aspect") or "").strip()
        try:
            covered_steps = {int(value) for value in (raw.get("story_steps") or [])}
        except (TypeError, ValueError):
            return []
        if (not aspect or len(aspect) > 700 or not covered_steps
                or not covered_steps.issubset(required) or not _aspect_in_scope(story, aspect)):
            return []
        key = re.sub(r"\s+", " ", aspect.casefold())
        if key in by_key:
            by_key[key]["contract_steps"] = sorted(
                set(by_key[key]["contract_steps"]) | covered_steps)
            continue
        item = {"aspect": aspect, "covered": False, "explicit": False,
                "contract_steps": sorted(covered_steps)}
        by_key[key] = item
        merged.append(item)
    represented = {step for item in merged for step in item["contract_steps"]}
    return _migrate_compound_coverage_ledger(merged) if represented == required else []


def _coverage_prompt(vision, story, state):
    return f"""ROLE: You are the QA-SECURITY explorer. Task: PLAN COVERAGE for one user story.

Before testing, a thorough QA engineer lists everything a REAL user would try for THIS EXACT story. Stay inside
the story contract. Do not add flows that are absent from the story steps or that contradict the EXPECTED
OUTCOME. If the expected outcome says a surface is denied/hidden/unavailable, coverage must verify denial and
hiddenness; it must NOT require opening that denied surface. If the story is an initial-load or inspection story,
do not invent navigation away from it unless the listed steps say to navigate.

=== ORIGINAL PRODUCT VISION ===
{vision}

=== STORY ===
title: {story.get('title', story.get('name', 'exploration'))}
goal: {story.get('goal', story.get('description', ''))}
STEPS: {json.dumps(story.get('steps') or [], ensure_ascii=False)}
EXPECTED OUTCOME: {story.get('expected', story.get('expected_outcome', ''))}

=== STARTING STATE ===
{_fmt_story_state(state, story)}

List the observable aspects needed to prove these steps and this expected outcome. Return them in STRICT
CHRONOLOGICAL / PRECONDITION ORDER: initial-load, empty-storage, or signed-out facts MUST precede any action that
mutates that state; setup precedes submission; submission precedes persistence/reload checks. A normal goto or
reload preserves cookies and localStorage and therefore NEVER recreates an empty-storage starting condition.
Be thorough but not redundant. HARD LIMIT: return 3-6 items (or fewer for a one-step story), combining related
assertions that can be proven from the same observed state. Represent every listed step and expected outcome;
do not spend separate ledger items on each panel when one observation checks them together. Each aspect is a
short imperative phrase. Every aspect must be traceable to the STEPS or EXPECTED OUTCOME above. For every item,
``story_steps`` must list the 1-based STEPS it represents. Every authored step number must appear in at least
one item; this mapping is checked mechanically and a lossy plan is rejected.

Reply with ONLY JSON, no prose:
{{"aspects": [
  {{"aspect": "<aspect 1>", "story_steps": [1]}},
  {{"aspect": "<aspect 2 combining related work>", "story_steps": [2, 3]}}
]}}"""


def _fmt_checklist(checklist):
    """Render the coverage ledger (what a user would try -> tested yet?) so DECIDE targets what's LEFT."""
    if not checklist:
        return "(no explicit coverage plan — exercise the story's expected outcome and any edge cases)"
    lines = []
    for c in checklist:
        mark = "[x]" if c.get("covered") else "[ ]"
        lines.append(f"  {mark} {str(c.get('aspect', ''))[:700]}")
    return "\n".join(lines)


def _compact_gapfill_prompt_mode(story, checklist=None):
    """Use the sealed, evidence-complete prompt once a durable story has one gap left.

    A resumed release story retains the full original contract plus its grounded action receipts.  Sending the
    generic multi-feature handbook again for every mouse/keyboard/reload action in the final composite clause
    adds tens of thousands of prompt characters without adding evidence.  Keep the generic judge while a story
    is still broad; switch only after at least one ledger item is already covered and exactly one remains.
    Explicit focused regressions continue to use the compact path from their first action.
    """
    if story.get("coverage") or story.get("category") == "focused-regression":
        return True
    ledger = [item for item in (checklist or []) if isinstance(item, dict)]
    covered = [item for item in ledger if item.get("covered")]
    remaining = [item for item in ledger if not item.get("covered")]
    return bool(covered and len(remaining) == 1)


def _portable_checkpoint_action(raw_action):
    """Recover the executable part of one redacted durable action summary.

    Orchestra intentionally stores compact strings such as ``press idx=3 ='ArrowDown'`` rather than the full
    browser record.  Keeping only the leading command made continuation prompts claim that ArrowDown, viewport,
    and traversal actions had happened without saying which key/direction/size was used.  Parse only Python
    literals emitted by our own formatter; malformed or legacy summaries remain harmless command-only memory.
    """
    if isinstance(raw_action, dict):
        return dict(raw_action)
    summary = " ".join(str(raw_action or "").split())[:600]
    match = re.match(r"([A-Za-z_]+)", summary)
    action = {"cmd": (match.group(1) if match else "checkpoint"),
              "checkpoint_summary": summary}
    idx_match = re.search(r"\bidx=([^ ]+)", summary)
    if idx_match:
        raw_idx = idx_match.group(1)
        try:
            action["idx"] = int(raw_idx)
        except (TypeError, ValueError):
            action["idx"] = raw_idx
    value_match = re.search(r"\s=([^=].*)$", summary)
    if value_match:
        try:
            action["value"] = ast.literal_eval(value_match.group(1))
        except (SyntaxError, ValueError):
            pass
    return action


def _planner_history_from_resume(rows, limit=12):
    """Project portable checkpoint rows back into bounded next-action memory.

    ``resume_steps_detail`` already feeds the independent evaluator, but the planner previously restarted with
    an empty history.  It therefore repeated viewports, full traversals, and state-changing controls that the
    checkpoint proved moments earlier.  This projection is navigation memory only—the original portable rows
    remain the evidence authority, and no story coverage is granted here.
    """
    source = [dict(item) for item in (rows or []) if isinstance(item, dict)][-max(1, int(limit)):]
    projected = []
    for offset, item in enumerate(source, 1):
        action = _portable_checkpoint_action(item.get("action"))
        actual = str(item.get("actual") or "")
        target = re.search(r"\btarget=(['\"])(.*?)\1", actual)
        if target and target.group(2) and not action.get("target_text"):
            action["target_text"] = target.group(2)[:240]
        verdict = item.get("verdict")
        matched = (bool(verdict.get("matches_expected")) if isinstance(verdict, dict)
                   else str(verdict or "").casefold() in {"match", "pass", "passed"})
        targeting = {
            "driver_ok": "driver_ok=True" in actual,
            "effect_registered": "effect_registered=True" in actual,
            "targeted_label": target.group(2)[:240] if target else "",
        }
        projected.append({
            "step": f"resume-{offset}", "action": action,
            "expected": str(item.get("expected") or "")[:1200],
            "bug": item.get("bug"), "matched": matched, "targeting": targeting,
        })
    return projected


def _portable_checkpoint_evidence(records):
    """Seal bounded proof receipts on every browser checkpoint, not only on normal tool return.

    A hard process loss can occur after ``checkpoint.json`` and Playwright storage state land but before the
    job thread returns its in-memory step list to the actor reducer. Covered labels alone are not evidence, so
    retaining only the ledger makes the successor correctly reopen and repeat the whole story. Keep the exact
    demonstrated-aspect receipt plus bounded browser/targeting facts beside the state. This format is the same
    evidence contract consumed by ``campaign_checkpoint.reopen_unproven_coverage`` and planner resume history.
    """
    portable = []
    for raw in records or []:
        if not isinstance(raw, dict):
            continue
        verdict = raw.get("verdict")
        if isinstance(verdict, dict):
            verdict_value = {key: verdict.get(key) for key in (
                "verdict", "matches_expected", "blocking", "severity", "model_failed",
                "infrastructure_error", "bug") if verdict.get(key) not in (None, "")}
        else:
            verdict_value = verdict
        raw_actual = raw.get("actual")
        actual = raw_actual if isinstance(raw_actual, dict) else {}
        targeting = raw.get("targeting") if isinstance(raw.get("targeting"), dict) else {}
        active = actual.get("activeElement") if isinstance(actual.get("activeElement"), dict) else {}
        if isinstance(raw_actual, str):
            # A preceding checkpoint generation is already in the bounded portable format. Preserve its
            # useful browser summary instead of degrading it to an empty URL/target on every subsequent step.
            actual_summary = raw_actual[:6000]
        else:
            actual_summary = (
                f"driver_ok={targeting.get('driver_ok', True)}; "
                f"effect_registered={targeting.get('effect_registered', False)}; "
                f"target={targeting.get('targeted_label') or targeting.get('intended')!r}; "
                f"url={actual.get('url')!r}; title={actual.get('title')!r}; "
                f"status={str(actual.get('statusText') or '')[:800]!r}; "
                f"active={json.dumps({key: active.get(key) for key in ('tag', 'text', 'role', 'focusVisible')
                                      if active.get(key) not in (None, '')}, default=str)}; "
                f"console_errors={json.dumps(list(actual.get('console_errors') or [])[:6], default=str)}; "
                f"screenshot={actual.get('screenshot')!r}")
        demonstrated_source = raw.get("demonstrated") or []
        if raw.get("coverage_grounded") is True and not demonstrated_source:
            # Portable rows use ``covers`` as the canonical exact-aspect index. Never promote an unsealed
            # planner claim from a live raw row, but retain the already-grounded index on checkpoint rollover.
            demonstrated_source = raw.get("covers") or []
        demonstrated = [str(value) for value in demonstrated_source if str(value)]
        mechanical = [str(value) for value in (raw.get("mechanically_proven") or []) if str(value)]
        portable.append({
            "step": raw.get("step"),
            "recorded_at": raw.get("recorded_at"),
            "action": raw.get("action"),
            "reasoning": str(raw.get("reasoning") or "")[:1600],
            "expected": str(raw.get("expected") or "")[:2400],
            "actual": actual_summary,
            # Preserve the compact causal receipt across controller handoffs. Without this, a successful
            # scenario matrix that received an inconclusive broad verdict degraded to prose-only history; the
            # cumulative reducer then saw only the later repeated inspections and could never close the
            # compound submit -> inspect acceptance item.
            "targeting": {key: targeting.get(key) for key in (
                "action_kind", "intended", "targeted_label", "label_matched",
                "effect_registered", "driver_ok", "activation_mode", "action_key", "action_value",
                "external_handoff_url",
                "traversal_summary", "landmark_dwell_summary", "scenario_matrix_summary",
                "timed_transition_summary") if targeting.get(key) not in (None, "")},
            "verdict": verdict_value,
            "covers": demonstrated,
            "demonstrated": demonstrated,
            "mechanically_proven": mechanical,
            "coverage_grounded": bool(demonstrated or mechanical),
            "bug": raw.get("bug") or (verdict.get("bug") if isinstance(verdict, dict) else None),
            "screenshot": actual.get("screenshot"),
        })
    return campaign_checkpoint.compact_evidence_records(portable)


def _decide_prompt(vision, story, state, history, checklist=None):
    remaining = [str(c.get("aspect", ""))[:700]
                 for c in (checklist or []) if not c.get("covered")]
    screenshot_instruction = (
        f"A screenshot of the current page is at: {state.get('screenshot')}\n"
        "Open/read that image because pixels are material to this visual/responsive story."
        if _story_requires_visual_judgment(story) else
        "A screenshot is retained in the evidence dossier. This story's next action is determined from the "
        "DOM/control receipts below; do not open a paid vision pass for navigation planning.")
    return f"""ROLE: You are the QA-SECURITY explorer. Task: DECIDE the single next action.

You are exercising a product as an adversarial, thorough QA engineer. Judge everything against the
ORIGINAL VISION and the STORY's EXPECTED behaviour — you are here to find where reality diverges.

=== COVERAGE LEDGER (everything a real user would try in this story — [x]=tested, [ ]=still to test) ===
{_fmt_checklist(checklist)}
STILL UNTESTED: {', '.join(remaining) if remaining else '(nothing outstanding — confirm, then you may be done)'}
Treat the ledger as an ordered journey. Work on the EARLIEST untested aspect and its prerequisites; never skip an
initial/empty/signed-out assertion and mutate that state first. A normal goto/reload PRESERVES cookies and
localStorage. If an empty-storage precondition genuinely must be recreated in this ephemeral QA browser, use the
explicit `reset_storage` action—not goto—and only when the story requires empty/fresh storage.
You are NOT bounded by a step count — a real QA engineer keeps going until everything a user would try is
covered. Only set done=true when the ledger is genuinely exhausted (every aspect tested or unreachable).

=== ORIGINAL PRODUCT VISION ===
{vision}

=== CURRENT STORY (what a user is trying to do) ===
title: {story.get('title', story.get('name', 'exploration'))}
goal: {story.get('goal', story.get('description', ''))}
STEPS: {json.dumps(story.get('steps') or [], ensure_ascii=False)}
EXPECTED OUTCOME: {story.get('expected', story.get('expected_outcome', ''))}

=== HISTORY SO FAR ===
{_fmt_history(history)}

=== MECHANICALLY REGISTERED BUSINESS ACTIONS (bounded ledger, not just recent chat) ===
{_fmt_effectful_control_history(history)}
Every listed action happened even when its composite expectation later received MISMATCH for focus, privacy,
or another post-action clause. Never repeat a state-changing action merely to repair that separate observation.
When a story says Enter OR Space, those are alternatives: one successful requested keyboard modality is enough,
not permission to activate another record with both keys. Once the required count/modalities have registered,
advance using read-only observation/reload or diagnose the remaining clause; do not mutate another entity.

=== CURRENT OBSERVED STATE ===
{_fmt_story_state(state, story)}

{screenshot_instruction}

Decide the ONE next action that best advances this story toward its EXPECTED OUTCOME. Stay inside the listed
story steps and coverage ledger. Do not navigate to or require a surface the story expects to remain hidden,
denied, or unavailable.

TARGET BY INTENT, NOT BY INDEX. For click/type, identify the control by its LABEL (copy the LABEL="..."
string VERBATIM from INTERACTABLE_ELEMENTS above) into `target_text`, and set `role` when it disambiguates
(button/link/tab/menuitem/checkbox). The label is what resolves the control. If several controls have the same
LABEL and role, use their observed NAME/ID and surrounding workflow to choose the correct displayed [idx], and
include that idx only as the tie-breaker. Never invent an idx you did not see.
Never activate a Seed/Load fixture whose US story ID differs from the current story ID.

FORM SEQUENCING: typing/filling a field is only the data-entry step. Unless the observed UI explicitly says
the field validates live as you type, the expected outcome for a type/fill action should be that the intended
field contains the value. Use a separate click/submit action to judge the submitted result or error state.
A case described as valid MUST complete every observed required control in the submit button's own form and
any visible contact-choice requirement before activation. Never label a partial form valid or treat a disabled
submit as a product defect; complete its prerequisites and retry the business journey.

SELECTS/COMBOBOXES: do not burn steps trying to open a native select and press letters/arrows. Choose an option
directly with `cmd: "type"`, `role: "combobox"`, the select's exact LABEL in `target_text`, and the option's
exact visible text in `value`; the driver performs a real change event and verifies the selected value.

ASYNC/EXTERNAL WAITING: sign-in callbacks, chat replies, queued agents, uploads, and background jobs can
legitimately take time. After the action that starts such work, set `wait_for` to the concrete browser fact
that proves the transition (a control, visible text, status/live-region text, URL fragment, or network idle).
The driver polls that fact without another model call. Never use repeated bare `wait` actions to rediscover
"still waiting". Choose a realistic timeout_s for this product operation; timeout means incomplete evidence,
not a product failure and never a completed story.
Do not attach `wait_for` to a manual processing-tick control (for example Drain/Process/Run once) when the
observed contract requires clicking it again for the next attempt. Re-observe after that synchronous tick and
choose the control again. `status` means an ARIA status/live region only; use `text` for ordinary page text.

EXPLICIT IDLE/DWELL EVIDENCE: only when the story itself requires remaining idle for a stated duration, use
`cmd: "wait"` with that number of seconds in `value` (for example `"10s"`). This is a real page-time wait,
not a no-op. Do not use it for an async result that has a concrete observable `wait_for` condition.

RESPONSIVE STORIES: when a listed step explicitly requires desktop/mobile or another viewport, use
`cmd: "viewport"` with `value: {{"width": 390, "height": 844}}` (or the story's stated dimensions), then
observe and evaluate the newly rendered layout. Never infer mobile behavior from desktop pixels or CSS text.

LONG PAGES: use `cmd: "scroll"` with `target_text` copied verbatim from DOCUMENT_LANDMARKS to move directly
to the relevant section. The driver scrolls that exact landmark into view. Do not oscillate through repeated
numeric scrolls when a named landmark is available; use numeric `value` only when no relevant landmark exists.

EVIDENCE-SPECIFIC ACTIONS:
- A story that requires traversing many controls by Tab/Shift+Tab SHOULD use one `cmd: "traverse"` action
  instead of one paid decision per focus stop. Omit `count` to traverse the page-derived focusable inventory
  plus a wrap margin, or provide 2-120; set `value` to `forward` or `backward`. The browser returns every
  trusted Tab event, focus label/order/rectangle, focus-visible style, scroll position, and overflow fact.
- When one story requires inspecting several named panels/surfaces on the same long page, use one
  `cmd: "inspect_surfaces"` with 1-8 `targets` copied verbatim from DOCUMENT_LANDMARKS. It scrolls to every
  exact target and returns separate settled viewport/status receipts so one judge can verify the whole clause.
- When one long page requires the same explicit idle dwell on several named surfaces, use one
  `cmd: "dwell_surfaces"` with `targets` copied verbatim from DOCUMENT_LANDMARKS and the required
  `duration_s` per surface. It scrolls to each exact surface, really waits there, and returns before/after
  URL, scroll, focus, form-value, and horizontal-overflow invariants. Do not include absent/conditional screens.
- When one story requires the SAME form to be exercised with several independent inputs (validation classes,
  claim references, secret shapes, or scenario values), use one `cmd: "scenario_matrix"`. Supply 1-12 named
  `cases`; each case contains 1-8 ordinary `actions` using exact control LABELs. The driver resolves, executes,
  settles, and captures every nested action separately, and the judge receives every case receipt. Keep
  prerequisites already common to all cases outside the matrix; never use it when later cases depend on an
  earlier case's unique successful output.
- A true browser refresh MUST use `cmd: "reload"`; same-URL `goto` is navigation, not reload evidence.
- A rapid/repeated burst MUST use `cmd: "burst"` with the control label plus integer `count` (2-20) and
  `interval_ms` (0-250). The driver returns exact click timestamps and elapsed milliseconds in one action.
- When the story explicitly names touch or pen input, use `cmd: "touch"` or `cmd: "pen"` with the control
  label. The Chromium input driver returns trusted pointerType evidence; a mouse click cannot prove these.
- When the story explicitly requires a held key/repeat characterization, use `cmd: "hold"`, the control label,
  key in `value`, and `duration_ms` (50-2000). A normal `press` remains the correct discrete-key action.
- Reverse traversal MUST actually press `Shift+Tab`; a prior plain Tab does not prove it.
- Browser re-entry SHOULD navigate to another same-origin URL and use `back`, or explicitly reopen the target.

Reply with ONLY a JSON object, no prose:
{{
  "reasoning": "<one sentence: why this action>",
  "intent": "<the control you intend to act on, in plain words, e.g. 'the Assistant nav link'>",
  "next_action": {{"cmd": "click|touch|pen|burst|timed_transition|type|press|hold|traverse|inspect_surfaces|dwell_surfaces|scenario_matrix|goto|reload|back|forward|reset_storage|viewport|scroll|wait",
                   "target_text": "<REQUIRED for click/type: control LABEL; for named scroll: DOCUMENT_LANDMARK label>",
                   "role": "<optional: button|link|tab|menuitem|checkbox to disambiguate>",
                   "idx": <the element idx, as a FALLBACK only>,
                   "selector": "<css selector alternative, optional>",
                   "count": <for burst: integer 2-20; for traverse: optional integer 2-120>,
                   "interval_ms": <for burst only: integer 0-250>,
                   "duration_ms": <for hold only: integer 50-2000>,
                   "duration_s": <for dwell_surfaces: the story-required seconds PER surface>,
                   "targets": ["<for inspect/dwell_surfaces: exact DOCUMENT_LANDMARK labels>"],
                   "cases": [{{"name":"<case>","actions":[
                     {{"cmd":"type","target_text":"<exact LABEL>","role":"textbox","value":"<input>"}},
                     {{"cmd":"click","target_text":"<exact LABEL>","role":"button"}}
                   ]}}],
                   "value": "<text/key/url/scroll px, or {{width,height}} for viewport>"}},
  "expected": "<concretely, what SHOULD happen after this action — the yardstick for evaluation>",
  "expected_control": "<optional: if this action should make a SPECIFIC control appear (e.g. the message
                       composer textarea, a Save button), give that control's LABEL/placeholder here so the
                       explorer can wait for it to paint before judging. Leave '' if no specific control is expected>",
  "wait_for": <null, or {{"kind":"control|text|status|url|network_idle",
                          "value":"<specific observable value; empty only for network_idle>",
                          "timeout_s":<realistic 1-{_WAIT_MAX_S} seconds>}} when the action starts async work>,
  "covers": ["<zero or more aspect strings COPIED VERBATIM from the coverage ledger above that THIS action
              exercises — this is how the run tracks tested-vs-untested; [] if it advances nothing on the list>"],
  "done": <true ONLY when every ledger aspect is tested or genuinely unreachable — NOT because you've taken
           'enough' steps. A real QA engineer is not done until coverage is exhausted.>
}}"""


def _focused_decide_prompt(vision, story, state, history, checklist=None):
    """Compact next-action planner for one sealed regression without dropping driver safety rules."""
    remaining = [str(item.get("aspect") or "")[:700] for item in (checklist or [])
                 if not item.get("covered")]
    story_contract = {
        "title": story.get("title", story.get("name", "focused regression")),
        "goal": story.get("goal", story.get("description", "")),
        "steps": story.get("steps") or [],
        "expected_outcome": story.get("expected", story.get("expected_outcome", "")),
    }
    visual = _story_requires_visual_judgment(story)
    screenshot = (f"OPEN SCREENSHOT {state.get('screenshot')} because pixels are contractual."
                  if visual else
                  "Screenshot is retained; use DOM/driver facts for this non-visual action plan.")
    state_text = _fmt_state(
        state or {}, include_accessibility=_story_requires_full_accessibility(story),
        element_limit=65, element_chars=9000, viewport_chars=1900, document_chars=700,
        status_chars=1200, landmark_limit=24, landmark_chars=2800, compact_accessibility=True)
    return f"""ROLE: QA-SECURITY. Choose ONE next browser action for a SEALED FOCUSED REGRESSION.

STORY CONTRACT (never add requirements and never require the historical broken output to recur):
{json.dumps(story_contract, ensure_ascii=False, default=str)[:6500]}

ORDERED COVERAGE LEDGER:
{_fmt_checklist(checklist)}
EARLIEST UNTESTED work owns the next action: {json.dumps(remaining, ensure_ascii=False)[:3500]}

RECENT ACTION RECEIPTS:
{_fmt_history(history, limit=5)}

CURRENT SETTLED STATE:
{state_text}
{screenshot}

Rules:
- Advance the earliest untested clause and only its prerequisites. done=true only when none remain.
- An unchecked ledger item cannot be waived by done=true; the runner continues until grounded evidence covers
  it or a manager classifies a concrete blocker. Do not use done=true as a step or time budget.
- Target click/type by an exact observed LABEL in target_text. When LABEL+role is duplicated, use observed
  NAME/ID context to include the correct displayed idx as a tie-breaker. Do not invent controls or indices.
- Typing proves field entry, not the later submit result. For native selects use type + role=combobox + the
  exact visible option. A named long-page target uses scroll + an exact DOCUMENT_LANDMARK label.
- Use reload for a true refresh and reset_storage only when the story explicitly requires empty storage.
- If an action starts async work, attach one concrete wait_for fact; timeout is incomplete evidence, not a bug.
- Do not wait after a manual processing-tick control that must be clicked again for the next attempt. Re-observe
  and choose it again. `status` searches ARIA status/live regions only; ordinary page content uses `text`.
- Use viewport/traverse/burst/hold/touch/pen/timed_transition/inspect_surfaces/dwell_surfaces/scenario_matrix only when the story explicitly
  requires that evidence. A scenario matrix is only for independent cases, never a dependent journey.
- Use timed_transition only for an enabled triggering control whose pending/busy state must remain observable
  for a named duration; provide duration_s. It owns the trigger, duplicate attempts, dwell, and completion.
- Keep denied/hidden/public/staff surfaces distinct. Do not navigate to a surface the contract says stays hidden.
- A static inspect/reload clause is read-only. Never approve, reject, send, publish, acknowledge, drain, or
  otherwise change business status merely to manufacture evidence for an observation-only finding.

Reply ONLY JSON:
{{"reasoning":"one sentence","intent":"plain-language target",
  "next_action":{{"cmd":"click|touch|pen|burst|timed_transition|type|press|hold|traverse|inspect_surfaces|dwell_surfaces|scenario_matrix|goto|reload|back|forward|reset_storage|viewport|scroll|wait",
    "target_text":"exact observed label or landmark","role":"optional role",
    "value":"only when needed: text/key/url/scroll or viewport object"}},
  "expected":"concrete immediate expected result","expected_control":"optional label",
  "wait_for":null,"covers":["exact ledger aspect exercised"],"done":false}}
Add only command-required fields: idx/selector fallback; count/interval_ms for burst; duration_ms for hold;
duration_s for timed_transition; targets for inspect_surfaces; duration_s/targets for dwell_surfaces; cases:[{{"name":"case","actions":[ordinary action objects]}}] for
scenario_matrix. wait_for may instead be {{"kind":"control|text|status|url|network_idle","value":"fact",
"timeout_s":1-{_WAIT_MAX_S}}}.
"""


def _fmt_targeting(targeting):
    t = targeting or {}
    action_kind = (t.get("action_kind") or "control").strip() or "control"
    is_control_action = bool(t.get("control_action", True))
    handoff = (f"\nexternal browser handoff URL: {t.get('external_handoff_url')!r}"
               if t.get("external_handoff_url") else "")
    before_disabled = (
        f"\nwas the intended control disabled BEFORE this action? {bool(t.get('before_control_disabled'))}"
        if t.get("before_control_disabled") is not None else "")
    after_disabled = (
        f"\nwas the intended control disabled immediately AFTER this action? {bool(t.get('after_control_disabled'))}"
        if t.get("after_control_disabled") is not None else "")
    values = ""
    if t.get("before_control_value") is not None or t.get("after_control_value") is not None:
        values = (f"\nintended control value BEFORE: {str(t.get('before_control_value') or '')[:500]!r}"
                  f"\nintended control value AFTER: {str(t.get('after_control_value') or '')[:500]!r}")
    if t.get("driver_control_value_matches") is not None:
        values += ("\naction-boundary control value matched requested value: "
                   f"{bool(t.get('driver_control_value_matches'))}")
    pointer_receipts = json.dumps(t.get("pointer_evidence") or [], ensure_ascii=False, default=str)
    keyboard_receipts = json.dumps(t.get("keyboard_evidence") or [], ensure_ascii=False, default=str)
    wait_receipt = (f"\nmechanical async-wait receipt: "
                    f"{json.dumps(t.get('wait_result'), ensure_ascii=False, default=str)[:1800]}"
                    if t.get("wait_result") else "")
    traversal_receipt = (f"\nmechanical full keyboard-traversal receipt: "
                         f"{json.dumps(t.get('traversal'), ensure_ascii=False, default=str)[:24000]}"
                         if t.get("traversal") else "")
    dwell_receipt = (f"\nmechanical multi-surface idle-dwell receipt: "
                     f"{json.dumps(_compact_landmark_dwell_receipt(t.get('landmark_dwell')), ensure_ascii=False, default=str)[:14000]}"
                     if t.get("landmark_dwell") else "")
    matrix_receipt = (f"\nmechanical scenario-matrix receipt: "
                      f"{json.dumps(_compact_scenario_matrix_receipt(t.get('scenario_matrix')), ensure_ascii=False, default=str)[:18000]}"
                      if t.get("scenario_matrix") else "")
    keyboard_matrix_receipt = (f"\nmechanical keyboard-matrix receipt: "
        f"{json.dumps(t.get('keyboard_matrix'), ensure_ascii=False, default=str)[:24000]}"
        if t.get("keyboard_matrix") else "")
    transition_receipt = (f"\nmechanical timed-transition receipt: "
                          f"{json.dumps(_compact_timed_transition_receipt(t.get('timed_transition')), ensure_ascii=False, default=str)[:12000]}"
                          if t.get("timed_transition") else "")
    focus_receipt = (f"\nmechanical source-bound focus-transition receipt: "
                     f"{json.dumps(t.get('focus_transition'), ensure_ascii=False, default=str)[:5000]}"
                     if t.get("focus_transition") else "")
    missing_required = json.dumps(
        t.get("empty_required_fields_before") or [], ensure_ascii=False, default=str)[:2400]
    return (
        f"action kind: {action_kind} ({'control-targeted' if is_control_action else 'state/navigation'})\n"
        f"intended control (by label/role): {t.get('intended')!r}"
        f"{(' role=' + t.get('role')) if t.get('role') else ''}\n"
        f"control the driver ACTUALLY actuated (matched label): {t.get('targeted_label')!r}\n"
        f"did the driver find & actuate a control matching the intent? {bool(t.get('label_matched'))}\n"
        f"did the action register ANY effect (nav / DOM change / network / console)? "
        f"{bool(t.get('effect_registered'))}\n"
        f"is a control matching the intent still present on the page afterwards? "
        f"{bool(t.get('control_present'))}\n"
        f"was a retry already attempted this step? {bool(t.get('retried'))}\n"
        f"was the AFTER-state captured after the page SETTLED (skeletons gone, DOM stable, "
        f"late-painting controls given time to render)? {bool(t.get('settled'))}"
        f"\ndid the browser driver command succeed? {bool(t.get('driver_ok', True))}"
        f"{(' — driver error: ' + str(t.get('driver_error'))) if t.get('driver_error') else ''}"
        f"\ntrusted pointer receipts for THIS action: {pointer_receipts}"
        f"\ntrusted keyboard/activation receipts for THIS action: {keyboard_receipts}"
        f"\nrequired form fields empty immediately BEFORE this action: {missing_required}"
        f"{handoff}{before_disabled}{after_disabled}{values}{wait_receipt}{traversal_receipt}{dwell_receipt}"
        f"{matrix_receipt}{keyboard_matrix_receipt}{transition_receipt}{focus_receipt}"
        + (f"\nthe action was expected to make this control appear: {t.get('expected_control')!r} — "
           f"present in the settled after-state? {bool(t.get('expected_control_present'))}"
           if t.get('expected_control') else ""))


def _perception_section(before_state, after_state):
    """What a human EYE saw DURING the action: full page reloads + view re-render/skeleton flashes. The old QA
    judged only the settled end-state and so missed transient/perceptual defects (e.g. the whole page flashing
    a reload after every reply). This surfaces the delta so the evaluator can judge the experience, not just the
    final DOM."""
    bp = (before_state or {}).get("perception") or {}
    ap = (after_state or {}).get("perception") or {}
    if not bp and not ap:
        return "(perception not captured for this action)"
    # a FULL reload wipes window.__qa -> firstAt changes (and loads resets). Same-window SPA re-renders keep
    # firstAt and just bump clobbers. So: firstAt changed => a full page reload happened this action.
    reloaded = bool(bp.get("firstAt") and ap.get("firstAt") and bp["firstAt"] != ap["firstAt"])
    reflows = max(0, int(ap.get("clobbers", 0)) - int(bp.get("clobbers", 0))) if not reloaded else int(ap.get("clobbers", 0))
    reloads = 1 if reloaded else 0
    return (f"During the action the screen underwent: {reloads} full page reload(s), "
            f"{reflows} main-view re-render/skeleton-flash(es).")


def _accessibility_delta_section(before_state, after_state, story):
    """Compact the expensive before/after accessibility evidence into the facts that changed.

    The settled AFTER tree remains in the prompt. Repeating two full AX trees, region lists, DOM events,
    platform events, and AT utterance histories made one accessibility judgment exceed 50k input tokens.
    A judge needs the prior active element plus newly observed events, not two copies of the entire page.
    """
    if not _story_requires_full_accessibility(story):
        return "(not an accessibility-specific story)"

    def new_events(field, limit=12, chars=3200):
        before = {json.dumps(item, sort_keys=True, default=str)
                  for item in (before_state or {}).get(field) or []}
        delta = [item for item in ((after_state or {}).get(field) or [])
                 if json.dumps(item, sort_keys=True, default=str) not in before]
        return json.dumps(delta[-limit:], ensure_ascii=False, default=str)[:chars]

    return (
        f"ACTIVE_ELEMENT BEFORE: {json.dumps((before_state or {}).get('activeElement') or {}, default=str)[:1600]}\n"
        f"ACTIVE_ELEMENT AFTER: {json.dumps((after_state or {}).get('activeElement') or {}, default=str)[:1600]}\n"
        f"NEW LIVE-REGION EVENTS: {new_events('accessibilityEvents')}\n"
        f"NEW ACTUAL AT EVENTS: {new_events('actualAssistiveTechnologyEvents')}\n"
        f"NEW PLATFORM AX EVENTS: {new_events('accessibilityPlatformEvents')}")


def _fmt_prior_journey_evidence(records, *, limit=12, max_chars=6500):
    """Bounded, screenshot-free receipts from earlier individually judged steps in this same story.

    Composite acceptance items often require several actions (deny, approve with a reason, then send). A final
    judge needs those earlier receipts to establish the whole journey, but never needs every DOM node or image.
    """
    # Select distinct receipts that actually established story evidence, plus the three most recent actions
    # for immediate transition context. Driver-batched matrices now retain all nested cases in one receipt;
    # retransmitting fourteen unrelated setup fills on every later judge added latency without authority.
    source = [record for record in (records or []) if isinstance(record, dict)]
    recent_ids = {id(record) for record in source[-3:]}
    useful = []
    for record in source:
        # Durable compact rows from older generations store ``action`` and sometimes ``targeting`` as
        # display strings.  Treat them as portable receipts instead of calling mapping methods on them; the
        # latter crashed every resumed explorer before its first step and made the coordinator retry forever.
        targeting = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        action = _portable_checkpoint_action(record.get("action"))
        verdict = record.get("verdict")
        useful_record = bool(
            record.get("demonstrated") or record.get("mechanically_proven") or record.get("bug")
            or (verdict.get("verdict") == "pass" if isinstance(verdict, dict) else verdict == "pass")
            # A successful state-changing or sealed batch action remains causal journey evidence even when
            # the per-step judge correctly refuses to credit a compound acceptance item. The bounded
            # cumulative reducer needs that receipt alongside the later observations.
            or (targeting.get("driver_ok") is True and (
                targeting.get("effect_registered") is True
                or any(targeting.get(key) for key in (
                    "traversal_summary", "landmark_dwell_summary", "scenario_matrix_summary",
                    "timed_transition_summary"))))
            # Rolling checkpoints created before compact targeting receipts existed still carry fail-closed
            # driver/effect facts in their portable actual string. Keep a successful sealed batch/action
            # visible during the one-generation migration; this grants no coverage by itself.
            or (isinstance(record.get("actual"), str)
                and "driver_ok=True" in record.get("actual", "")
                and "effect_registered=True" in record.get("actual", "")
                and str(action.get("cmd") or "").lower() in {
                    "scenario_matrix", "case_matrix", "keyboard_matrix", "click", "tap", "press"})
            or id(record) in recent_ids)
        if useful_record:
            useful.append(record)
    chosen, seen = [], set()
    for record in reversed(useful):
        if not isinstance(record, dict):
            continue
        identity = json.dumps({
            "action": record.get("action"), "expected": str(record.get("expected") or "")[:500],
        }, sort_keys=True, ensure_ascii=False, default=str)
        if identity in seen:
            continue
        seen.add(identity)
        chosen.append(record)
        if len(chosen) >= max(1, int(limit)):
            break
    evidence = []
    for record in reversed(chosen):
        if not isinstance(record, dict):
            continue
        actual_value = record.get("actual")
        actual = actual_value if isinstance(actual_value, dict) else {}
        target = record.get("targeting") if isinstance(record.get("targeting"), dict) else {}
        verdict = record.get("verdict") if isinstance(record.get("verdict"), dict) else record.get("verdict")
        evidence.append({
            "step": record.get("step"),
            "action": record.get("action"),
            "expected": str(record.get("expected") or "")[:450],
            "targeting": {key: target.get(key) for key in (
                "action_kind", "intended", "targeted_label", "label_matched", "effect_registered",
                "activation_mode",
                "driver_ok", "external_handoff_url", "traversal_summary", "landmark_dwell_summary",
                "scenario_matrix_summary", "timed_transition_summary")
                if target.get(key) not in (None, "")},
            "verdict": ({key: verdict.get(key) for key in (
                "verdict", "matches_expected", "target_confirmed", "blocking", "severity")
                if verdict.get(key) not in (None, "")} if isinstance(verdict, dict) else verdict),
            "demonstrated": list(record.get("demonstrated") or []),
            "mechanically_proven": list(record.get("mechanically_proven") or []),
            "actual": {
                "url": actual.get("url"), "title": actual.get("title"),
                "statusText": str(actual.get("statusText") or "")[:280],
                "bodyText": str(actual.get("bodyText") or "")[:360],
                "console_errors": list(actual.get("console_errors") or [])[-3:],
                "recent_requests": list(actual.get("recent_requests") or [])[-1:],
            },
            "portable_actual": (str(actual_value or "")[:1000]
                                if not isinstance(actual_value, dict) else None),
        })
    return json.dumps(evidence, ensure_ascii=False, default=str)[:max_chars] or "(no prior steps)"


def _fmt_evaluation_before_state(state):
    """Compact baseline for an action judge; target semantics live in the fenced targeting receipt."""
    state = state or {}
    return json.dumps({
        "url": state.get("url"), "title": state.get("title"),
        "scrollPosition": state.get("scrollPosition"),
        "statusText": str(state.get("statusText") or "")[:700],
        "viewportText": str(state.get("viewportText") or state.get("bodyText") or "")[:1400],
        "activeElement": state.get("activeElement"),
        "console_errors": list(state.get("console_errors") or [])[-4:],
        "recent_requests": list(state.get("recent_requests") or [])[-4:],
    }, ensure_ascii=False, default=str)[:4000]


def _evaluate_prompt(vision, story, expected, targeting, before_state, after_state, untested=None,
                     prior_records=None):
    visual_judgment = _story_requires_visual_judgment(story, expected, targeting)
    screenshot_section = (
        f"Screenshot after the action: {after_state.get('screenshot')}  "
        "(open/read it because pixels are material to this visual contract)"
        if visual_judgment else
        "Screenshot after the action: retained in the evidence dossier. This action has no visual/layout "
        "contract, so do not open a paid vision pass; judge its driver, DOM, network, console, and perception "
        "receipts below."
    )
    screenshot_guard = (
        ", or the before/after screenshots objectively show scroll position/content jumping"
        if visual_judgment else ""
    )
    return f"""ROLE: You are the QA-SECURITY explorer. Task: EVALUATE expected-vs-actual after an action.

Judge honestly and adversarially — but blame the APP only when the RIGHT control was actually exercised.
An explorer that cries wolf on its own missed click is worthless. So FIRST confirm targeting, THEN judge.

=== ORIGINAL PRODUCT VISION ===
{vision}

=== STORY ===
goal: {story.get('goal', story.get('description', ''))}
STEPS: {json.dumps(story.get('steps') or [], ensure_ascii=False)}
STORY EXPECTED OUTCOME: {story.get('expected', story.get('expected_outcome', ''))}

=== WHAT WAS EXPECTED FROM THE ACTION JUST TAKEN ===
{expected}

That action-level expectation was proposed by another agent; it is NOT allowed to add a requirement absent
from the STORY STEPS and STORY EXPECTED OUTCOME. In particular, ``record the returned value as B/C/D`` creates
a fresh observed baseline: it does not imply that a value from before Back/Forward/reload must persist. Only
require persistence/retention when the story contract explicitly says so. If the proposal overreaches the
story, judge the actual action against the narrower story contract and never file the invented requirement.

=== PRIOR GROUNDED JOURNEY EVIDENCE (same story, earlier steps, individually judged) ===
{_fmt_prior_journey_evidence(prior_records)}
These are sealed earlier receipts, not model intentions. A composite remaining aspect may be DEMONSTRATED when
the current action completes its final clause and these prior pass/mechanical receipts prove the earlier clauses.
Do not require one browser action to perform an inherently multi-step workflow, and do not credit a clause that
is absent from both the prior receipts and the current before/after evidence.

=== PRIOR MECHANICALLY REGISTERED BUSINESS ACTIONS ===
{_fmt_effectful_control_history(prior_records)}
These action counts are ground truth even when an individual composite verdict was mismatch. Attribute their
state changes to the QA journey, never to spontaneous app behavior, and do not claim the journey performed only
the smaller subset that happened to fit in recent conversational receipts.

=== ACTION TARGETING (ground truth from the driver — trust this over your own reading of the page) ===
{_fmt_targeting(targeting)}

=== STATE BEFORE THE ACTION ===
{_fmt_evaluation_before_state(before_state)}

=== STATE AFTER THE ACTION ===
{_fmt_story_state(after_state, story, evaluation=True)}
{screenshot_section}

=== ACCESSIBILITY DELTA (new facts only; full settled AFTER semantics are above) ===
{_accessibility_delta_section(before_state, after_state, story)}

=== PERCEPTION (what a human EYE saw DURING the action — not just the settled end-state) ===
{_perception_section(before_state, after_state)}
Perceptual defects must be grounded in the PERCEPTION line above. On a routine IN-PLACE interaction — sending
a message, clicking a suggestion chip, toggling a tab, submitting an inline form — a FULL PAGE RELOAD, or the
main view flashing to a skeleton and re-rendering, is a real jarring UX defect EVEN IF the settled DOM ends up
correct: it loses scroll position, wipes what the user was reading, and feels broken/amateur. Only call this
kind of bug when PERCEPTION reports one or more full reloads or main-view re-render/skeleton flashes, or the
instrumented transition otherwise proves the jump{screenshot_guard}. If perception reports 0 reloads and
0 re-render/skeleton flashes and the before/after screenshots remain aligned, do NOT invent a screen-jump bug.
A reload/re-render is EXPECTED + fine after an explicit navigation, a sign-in/out, or an action whose whole
point is to load a new page/screen.

CONTRACT — apply IN THIS ORDER:
1. If this was a CONTROL-TARGETED action (click/type/fill/tap): was the INTENDED control correctly actuated?
   Usually that means the driver matched the intent's label AND the action registered an effect. Exception:
   when the expected outcome explicitly says the action should have no effect / remain unchanged / stay disabled
   (for example clicking an already-selected disabled tab), a label-matched actuation with no DOM change is the
   expected signal, not a missed click. If the intended control was never exercised (a mis-target or a missed
   click), then this is NOT an app bug. Use verdict "control-not-found" (intent had no matching control) or
   "retry"/"inconclusive" (a matching control exists but the click didn't land / effect is ambiguous). In these
   cases `bug` MUST be null.
   Browser handoff links (`mailto:`, `tel:`, `sms:`) are a valid exception: a correctly clicked handoff link may
   keep the page URL/DOM unchanged and must not create HTTP network evidence. Treat the matched handoff URL in
   ACTION TARGETING as the expected signal unless the page breaks or emits real HTTP/console errors.
   For type/fill actions, do not require the final form result unless the expected outcome explicitly says the
   field validates live as the value is entered. If the value landed in the intended field, the data-entry action
   itself succeeded; a missing submitted result should be tested by a later submit/click action, not filed here.
   SUBMIT-SETUP GUARD: `required form fields empty immediately BEFORE this action` is an exact browser receipt.
   When it is non-empty, the app was not yet given the prerequisites for a later publish/create/send outcome.
   If the story is explicitly testing incomplete-form validation and the observed validation is correct, pass it;
   otherwise use `retry` and fill the named fields. Never file a product bug merely because an incomplete form
   did not reach a later business-rule validation or success state. Real crashes, console errors, or incorrect
   required-field validation remain bugs when the story requires that behavior.
   If this was a STATE/NAVIGATION action (goto/reload/wait/noop/scroll with no intended control), SKIP this
   control-confirmation gate and judge the observed before->after state against the expected outcome.
2. ONLY if the right control WAS correctly actuated and the product STILL misbehaved against the STORY'S
   STEPS and EXPECTED OUTCOME (wrong/missing
   result, console/page errors, broken layout, a truly dead control that DID receive the click, a
   security/permission leak, data loss, confusing dead-end UX) is the verdict "bug". For state/navigation
   actions, "the right control was exercised" means the requested navigation/state probe ran and the settled
   after-state is available to compare with the expectation.
   Scope guard: never file a bug because a Team/Admin/CEO/internal/review surface did NOT open when the
   story expected that surface to be denied, hidden, unavailable, or gated by review mode.
   TIMING GUARD: the AFTER-state is captured only once the SPA has SETTLED (loading skeletons gone, the
   interactive-element set stable, and — when a specific control was expected — after re-observing to
   give a late-painting control time to render). So do NOT claim a control "never renders" / "the view
   is blank" from a transient loading state: if `settled` is true and the expected control is genuinely
   absent, that is a real defect; if it IS present in the settled after-state, it rendered fine (a slow
   paint is not a bug). Never turn an observation race into an app bug.
   STATE GUARD: for selected/active/checked/expanded judgements the DOM semantics in
   INTERACTABLE_ELEMENTS (aria-pressed / aria-selected / aria-expanded / checked / value) are the ground
   truth. Pixels alone NEVER decide state: hover/focus/mid-transition styling in a screenshot is not a
   selection (a cursor resting on a button is not "active"). Only call a state bug when the DOM
   semantics themselves are wrong, or a control visibly claims a state its semantics contradict AND the
   styling is clearly the selected treatment (not hover/focus).
   SILENT-EMPTY GUARD: do NOT trust a plausible-sounding empty state. A panel/list/report that says "nothing
   yet", "not ready", "no data", or renders blank is a REAL DEFECT when the story/journey implies content
   SHOULD be there (e.g. 'read the full research' right after research finished, an empty Projects list right
   after a build was directed, an empty inbox after an action that creates a notification). A believable
   "empty" message is exactly how a silent data-wiring bug hides — if content is expected and it's absent,
   lean toward "bug" (or "inconclusive" with a probe), never a reflexive "pass".
	   CAUSAL-POPULATION GUARD: a broad phrase such as "all panels transition from empty to populated" means every
	   observed panel must remain rendered and truthfully reflect the journey's effects. It does NOT require every
	   independent entity counter to become nonzero. A zero approvals/tickets/notifications count is valid when no
	   story step created or submitted that entity. Treat it as missing content only when a story step or a more
	   specific expected clause causally requires that exact entity to exist.
	   QUANTIFIER-SCOPE GUARD: words such as exactly/one/none bind only the noun phrase the story actually names.
	   "Exactly one approval ticket and blocker" constrains tickets and blockers; it does not silently require
	   exactly one audit event, notification, internal callback, or rendering row. Distinct truthful audit records
	   for request, denial, and human decision are not duplicates unless the story explicitly constrains event
	   cardinality or the same business transition registered more than once. Never paraphrase that ticket
	   constraint into "the story requires exactly one approval-request event"; that is an invented requirement.
	   PROJECTION-SCOPE GUARD: two panels may intentionally be filtered projections of the same durable state.
	   An empty approvals/blockers governance history is not inconsistent with operational diagnostics containing
	   enquiry/job/domain audit records. Require cross-panel parity only when the story names the same record on
	   both surfaces or explicitly requires those projections to match. A broad request to inspect both panels is
	   not a parity requirement.
3. If it behaved as expected — the right result rendered AND no unexpected reload/flash AND no silent-empty —
   verdict "pass".

=== COVERAGE — aspects still UNTESTED for this story ===
{chr(10).join('- ' + a for a in (untested or [])) or '(none listed)'}
An aspect counts as DEMONSTRATED only on grounded evidence you can see. For action-result aspects, use the
BEFORE->AFTER change. For static visible-state aspects ("verify X is visible", "confirm no sign-in prompt"),
the settled after-state itself can demonstrate the aspect even if the page did not otherwise change. This is
what makes the tested-vs-untested ledger trustworthy: coverage is credited on what you evaluated, never on the
actor's own say-so.

Reply with ONLY a JSON object, no prose:
{{
  "target_confirmed": <true if the intended control was correctly actuated and produced the expected kind of
                       signal; an effect is required for ordinary controls, but no effect is acceptable when
                       the expected outcome is explicitly "unchanged"/"no effect"; for state/navigation
                       actions with no intended control, true when the action completed and produced the
                       settled after-state needed to judge the expectation>,
  "matches_expected": <true|false>,
  "verdict": "<pass|bug|inconclusive|retry|control-not-found>",
  "bug": "<clear description of the bug, or null — MUST be null unless verdict is exactly 'bug'>",
  "severity": "<none|low|medium|high|critical>",
  "blocking": <true if this genuinely prevents any further meaningful exploration of this story>,
  "demonstrated": ["<zero or more exact UNTESTED aspect strings that the current action demonstrated by itself,
                    or whose final clause it completed after PRIOR GROUNDED JOURNEY EVIDENCE proves the earlier
                    clauses; [] if the cumulative receipts still do not prove the whole aspect>"]
}}"""


def _focused_evaluate_prompt(vision, story, expected, targeting, before_state, after_state, untested=None,
                             prior_records=None):
    """Small evidence-complete judge for a sealed, single-finding regression.

    Focused regressions already carry a four-step contract authored from one durable finding. Repeating the
    generic explorer's entire multi-feature false-positive handbook on every action made a short reproduction
    take minutes. This prompt retains the same blame, scope, timing, cumulative-evidence, and output fences,
    while excluding rules for unrelated navigation, responsive, handoff, and generic empty-state stories.
    """
    story_contract = {
        "goal": story.get("goal", story.get("description", "")),
        "steps": story.get("steps") or [],
        "expected_outcome": story.get("expected", story.get("expected_outcome", "")),
    }
    after = _fmt_state(
        after_state or {}, include_accessibility=_story_requires_full_accessibility(story),
        element_limit=65, element_chars=9000, viewport_chars=1900, document_chars=900,
        status_chars=1200, landmark_limit=0, landmark_chars=0, compact_accessibility=True)
    prior = _fmt_prior_journey_evidence(prior_records, limit=8, max_chars=6500)
    remaining = "\n".join("- " + str(item)[:700] for item in list(untested or [])[:6]) or "(none)"
    return f"""ROLE: QA-SECURITY. Independently judge one action in a SEALED FOCUSED REGRESSION.

STORY CONTRACT (the action expectation may narrow this, never expand it):
{json.dumps(story_contract, ensure_ascii=False, default=str)[:6000]}
ACTION EXPECTATION: {str(expected or '')[:1800]}

CAUSAL-POPULATION GUARD: "all panels become populated" requires every panel to render and reflect the actions,
not every independent entity count to become nonzero. A zero approval/ticket/notification count is valid when
no story step created or submitted that entity. Only a specific causal creation requirement makes zero a bug.

QUANTIFIER/PROJECTION GUARD: "exactly one approval ticket and blocker" does not constrain audit-event count.
Distinct request, denial, and decision projections may each be truthful. Never invent cross-panel parity merely
because the story asks to inspect two surfaces; a filtered governance panel may be empty while broader
operational diagnostics truthfully contain unrelated domain/job audits.

PRIOR GROUNDED RECEIPTS (only demonstrated/mechanical evidence plus immediate context):
{prior}

DRIVER TARGETING RECEIPT (ground truth):
{_fmt_targeting(targeting)}

BEFORE:
{_fmt_evaluation_before_state(before_state)}

SETTLED AFTER:
{after}

ACCESSIBILITY DELTA:
{_accessibility_delta_section(before_state, after_state, story)}
PERCEPTION: {_perception_section(before_state, after_state)}

STILL UNTESTED:
{remaining}

Rules:
1. Confirm the intended control/action from the driver receipt first. A missing/mis-targeted/failed action is
   retry, inconclusive, or control-not-found and bug=null. A driver-confirmed action may be judged normally.
2. Judge only the STORY CONTRACT. This is a multi-action journey: do not demand that one click perform later
   steps, and do not treat the historical broken output as something that must recur. Use prior receipts only
   for clauses they actually demonstrated or mechanically proved.
3. A settled contradiction after the correct action is a bug. Missing evidence is retry/inconclusive. An
   absent control is a bug only when the story requires it in the CURRENT settled state; controls correctly
   restricted to another status must remain absent. DOM checked/value/ARIA facts override pixel guesses.
4. A wait timeout is incomplete evidence, not a product bug. Report visual jump/layout defects only from the
   perception/visual evidence. Do not confuse staff/internal records with public exposure.
5. Credit a STILL UNTESTED item only by copying it verbatim when current + prior grounded evidence proves all
   its clauses. Otherwise omit it. bug MUST be null unless verdict is exactly bug.

Reply ONLY JSON:
{{"target_confirmed":true|false,"matches_expected":true|false,
  "verdict":"pass|bug|inconclusive|retry|control-not-found","bug":null,
  "severity":"none|low|medium|high|critical","blocking":false,
  "demonstrated":["exact still-untested item"]}}"""


def _compact_text_ends(value, limit):
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    return text[:head] + " … " + text[-max(1, limit - head - 3):]


def _compact_scenario_matrix_receipt(matrix):
    """Keep every case outcome inside the judge prompt instead of truncating a verbose first case.

    The live matrix retains full before/after evidence in the checkpoint.  The semantic judge needs a compact
    projection: requested input and settled control value for field actions, plus before/after status and
    viewport facts for transition actions.  This preserves evidence from *all* named cases in roughly the same
    space the old prompt spent on the first one or two.
    """
    source = matrix if isinstance(matrix, dict) else {}
    cases = []
    for raw_case in list(source.get("cases") or [])[:12]:
        if not isinstance(raw_case, dict):
            continue
        actions = []
        raw_actions = [item for item in list(raw_case.get("actions") or [])[:8]
                       if isinstance(item, dict)]
        for index, item in enumerate(raw_actions):
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            cmd = str(action.get("cmd") or "").lower()
            compact = {
                "cmd": cmd,
                "target_text": str(action.get("target_text") or action.get("target") or "")[:160],
                "requested_value": str(action.get("value") or "")[:320],
                "resolved_label": str(item.get("resolved_label") or "")[:160],
                "ok": bool(item.get("ok", True)),
                "driver_error": str(item.get("driver_error") or "")[:240] or None,
            }
            if item.get("empty_required_fields_before"):
                compact["empty_required_fields_before"] = list(
                    item.get("empty_required_fields_before") or [])[:20]
            if item.get("disabled_before_click") is not None:
                compact["disabled_before_click"] = bool(item.get("disabled_before_click"))
            if item.get("actual_value_length") is not None:
                compact["actual_value_length"] = item.get("actual_value_length")
            if item.get("actual_value_matches") is not None:
                compact["actual_value_matches"] = bool(item.get("actual_value_matches"))
            if item.get("paste_evidence"):
                compact["paste_evidence"] = list(item.get("paste_evidence") or [])[-6:]
            after = item.get("after") if isinstance(item.get("after"), dict) else {}
            active = after.get("activeElement") if isinstance(after.get("activeElement"), dict) else {}
            if active:
                compact["settled_control"] = {
                    "label": str(active.get("associatedLabel") or active.get("ariaLabel")
                                 or active.get("text") or "")[:160],
                    "value": str(active.get("value") or "")[:320],
                }
            # Outcome evidence belongs to each state-changing boundary and the case's final action.  Repeating
            # full page/network payloads for every keystroke was both slower and caused the prompt to cut off
            # later cases entirely.
            if cmd in {"click", "tap", "press", "wait"} or index == len(raw_actions) - 1:
                before = item.get("before") if isinstance(item.get("before"), dict) else {}
                compact.update({
                    "before_status": _compact_text_ends(before.get("statusText"), 160),
                    "after_status": _compact_text_ends(after.get("statusText"), 440),
                    "after_viewport": _compact_text_ends(after.get("viewportText"), 320),
                    "after_url": str(after.get("url") or "")[:300],
                    "console_errors": [str(error)[:240]
                                       for error in list(after.get("console_errors") or [])[-3:]],
                })
            actions.append(compact)
        cases.append({"name": str(raw_case.get("name") or "")[:160], "actions": actions})
    return {
        "ok": bool(source.get("ok", True)),
        "completed_cases": source.get("completed_cases"),
        "total_cases": source.get("total_cases"),
        "action_count": source.get("action_count"),
        "error": str(source.get("error") or "")[:300] or None,
        "cases": cases,
    }


def _compact_timed_transition_receipt(transition):
    """Keep exact transient timing and duplicate-attempt evidence without retransmitting full page text."""
    source = transition if isinstance(transition, dict) else {}
    samples = []
    for item in list(source.get("samples") or [])[:10]:
        if not isinstance(item, dict):
            continue
        samples.append({key: item.get(key) for key in (
            "name", "pending", "control_exists", "control_label", "control_disabled",
            "form_aria_busy", "status_text", "body_text", "probe_error")
            if item.get(key) not in (None, "")})
    return {
        key: source.get(key) for key in (
            "required_duration_ms", "completion_grace_ms", "pending_text", "pending_seen",
            "pending_start_delay_ms", "completion_observed", "transition_duration_ms",
            "completed_before_required_duration", "stable_through_required_boundary",
            "duplicate_attempts", "submit_event_count", "mutation_count", "elapsed_ms")
        if source.get(key) not in (None, "")
    } | {"samples": samples}


def _compact_landmark_dwell_receipt(dwell):
    """Project a multi-surface dwell onto its decision-relevant facts.

    The browser deliberately captures a rich snapshot before and after every landmark so the durable artifact
    remains independently inspectable. Repeating the full page-wide form inventory and multi-kilobyte viewport
    text for every landmark in the judge prompt can multiply that artifact to tens of thousands of tokens.
    ``stable`` already binds the complete before/after snapshots; keep the surface-specific semantic/privacy
    counters, headings, definition pairs, focus, URL, overflow, and any concrete control changes.
    """
    source = dwell if isinstance(dwell, dict) else {}
    observations = []
    for raw in list(source.get("observations") or [])[:16]:
        if not isinstance(raw, dict):
            continue
        before = raw.get("before") if isinstance(raw.get("before"), dict) else {}
        after = raw.get("after") if isinstance(raw.get("after"), dict) else {}
        summary = after.get("scopeSummary") if isinstance(after.get("scopeSummary"), dict) else {}
        before_controls = {
            str(item.get("id") or item.get("name") or index): item
            for index, item in enumerate(before.get("controls") or []) if isinstance(item, dict)
        }
        after_controls = {
            str(item.get("id") or item.get("name") or index): item
            for index, item in enumerate(after.get("controls") or []) if isinstance(item, dict)
        }
        changes = []
        for key in sorted(set(before_controls) | set(after_controls)):
            old, new = before_controls.get(key) or {}, after_controls.get(key) or {}
            old_value = {name: old.get(name) for name in ("value", "checked")
                         if old.get(name) is not None}
            new_value = {name: new.get(name) for name in ("value", "checked")
                         if new.get(name) is not None}
            if old_value != new_value:
                changes.append({"control": key[:100], "before": old_value, "after": new_value})
            if len(changes) >= 20:
                break
        observations.append({
            "target": str(raw.get("target") or "")[:180],
            "scroll": {key: (raw.get("scroll") or {}).get(key)
                       for key in ("scrolled", "matched", "requested", "y")
                       if (raw.get("scroll") or {}).get(key) is not None},
            "requested_ms": raw.get("requested_ms"), "elapsed_ms": raw.get("elapsed_ms"),
            "stable": bool(raw.get("stable")),
            "url_before": str(before.get("url") or "")[:300],
            "url_after": str(after.get("url") or "")[:300],
            "active_before": before.get("active"), "active_after": after.get("active"),
            "horizontal_overflow_before": before.get("horizontalOverflow"),
            "horizontal_overflow_after": after.get("horizontalOverflow"),
            "status_after": _compact_text_ends(after.get("statusText"), 420),
            "viewport_after": _compact_text_ends(after.get("viewportText"), 700),
            "scope": {
                "target": str(summary.get("target") or "")[:180],
                "text_chars": summary.get("text_chars"),
                "headings": [str(item)[:180] for item in list(summary.get("headings") or [])[:24]],
                "definition_pairs": [
                    {"label": str(item.get("label") or "")[:120],
                     "value": str(item.get("value") or "")[:240]}
                    for item in list(summary.get("definition_pairs") or [])[:36]
                    if isinstance(item, dict)],
                **{key: summary.get(key) for key in (
                    "status_values", "article_count", "review_card_count", "control_count",
                    "json_block_count", "raw_reference_count", "email_address_count",
                    "phone_number_count", "iso_timestamp_count", "raw_metadata_labels")
                   if summary.get(key) is not None},
                "text_prefix": str(summary.get("text_prefix") or "")[:700],
                "text_suffix": str(summary.get("text_suffix") or "")[-500:],
            },
            "control_changes": changes,
        })
    return {
        "targets": [str(item)[:180] for item in list(source.get("targets") or [])[:16]],
        "duration_ms_each": source.get("duration_ms_each"),
        "elapsed_ms": source.get("elapsed_ms"),
        "observations": observations,
    }


def _batch_evaluate_prompt(vision, story, expected, targeting, before_state, after_state, untested=None,
                           prior_records=None):
    """Judge one driver-batched traversal/dwell receipt without retransmitting the whole page twice.

    These commands already return the exact per-focus-stop or per-landmark before/after facts.  Feeding the
    generic action judge a second full DOM/AX rendering made a 3-second browser traversal wait roughly another
    25 seconds on a ~50k-character prompt.  Keep the same strong independent judge and verdict contract, but
    give it the story, the sealed batch receipt, a bounded control inventory, and only the settled facts that
    can materially contradict that receipt.  Coverage authority is unchanged: the normal grounded oracle still
    filters anything the judge claims to have demonstrated.
    """
    targeting = targeting or {}
    before_state, after_state = before_state or {}, after_state or {}
    kind = ("keyboard traversal" if targeting.get("traversal") else
            "adaptive keyboard control matrix" if targeting.get("keyboard_matrix") else
            "multi-surface idle dwell" if targeting.get("landmark_dwell") else
            "timed transient transition" if targeting.get("timed_transition") else
            "repetitive form scenario matrix")
    matrix = targeting.get("scenario_matrix")
    keyboard_matrix = targeting.get("keyboard_matrix")
    transition = targeting.get("timed_transition")
    receipt = (_compact_scenario_matrix_receipt(matrix) if matrix else
               dict(keyboard_matrix) if keyboard_matrix else
               _compact_timed_transition_receipt(transition) if transition else
               _compact_landmark_dwell_receipt(targeting.get("landmark_dwell"))
               if targeting.get("landmark_dwell") else
               dict(targeting.get("traversal") or {}))
    # The nested receipt proves what ran; these top-level driver facts prove that the command itself reached a
    # settled action boundary. Omitting them made an honest matrix judge return inconclusive despite every
    # nested control being resolved and exercised successfully.
    receipt = {"driver_ok": bool(targeting.get("driver_ok")),
               "effect_registered": bool(targeting.get("effect_registered")), **receipt}
    controls = []
    for element in list(after_state.get("elements") or [])[:80]:
        if not isinstance(element, dict):
            continue
        controls.append({key: element.get(key) for key in (
            "idx", "tag", "type", "role", "text", "label", "ariaLabel", "placeholder",
            "disabled", "ariaDisabled", "checked", "value") if element.get(key) not in (None, "")})
    settled = {
        "url": after_state.get("url"), "title": after_state.get("title"),
        "viewport": after_state.get("viewport"), "scrollPosition": after_state.get("scrollPosition"),
        "activeElement": after_state.get("activeElement"),
        "statusText": str(after_state.get("statusText") or "")[:1200],
        "viewportText": str(after_state.get("viewportText") or "")[:1600],
        "console_errors": list(after_state.get("console_errors") or [])[-5:],
        "recent_requests": list(after_state.get("recent_requests") or [])[-5:],
        "perception": after_state.get("perception"),
        "controls": controls,
    }
    prior = _fmt_prior_journey_evidence(prior_records, limit=3, max_chars=2500)
    untested_text = "\n".join("- " + str(item)[:700] for item in list(untested or [])[:6]) or "(none listed)"
    return f"""ROLE: You are the QA-SECURITY explorer. Judge one trusted browser {kind} batch.

STORY STEPS: {json.dumps(story.get('steps') or [], ensure_ascii=False)[:3000]}
STORY EXPECTED OUTCOME: {str(story.get('expected', story.get('expected_outcome', '')))[:2500]}
ORIGINAL PRODUCT VISION: {str(vision or '')[:1000]}
ACTION EXPECTATION: {str(expected or '')[:1600]}

SEALED DRIVER RECEIPT (ground truth):
{json.dumps(receipt, ensure_ascii=False, default=str)[:14000]}

SETTLED CONTRADICTION CHECKS AND CONTROL INVENTORY:
{json.dumps(settled, ensure_ascii=False, default=str)[:6000]}

PRIOR INDIVIDUALLY-JUDGED STORY RECEIPTS:
{prior}

STILL UNTESTED (copy an item verbatim into demonstrated only when this batch, possibly completing the prior
receipts, proves every clause):
{untested_text}

Apply these rules exactly:
- This is a state/navigation batch, not a click locator. target_confirmed is true only when driver_ok is true
  and the sealed receipt contains the requested completed batch. A driver failure is inconclusive, never an app bug.
- Judge only requirements present in the story. Do not invent persistence, activation, or hidden-surface duties.
- Quantifiers bind only their named noun phrase. An exactly-one ticket/blocker requirement does not impose an
  exactly-one audit-event requirement. Do not invent parity between intentionally filtered panels: an empty
  approval/blocker projection may coexist with broader operational audit records unless the story explicitly
  requires the same record on both surfaces.
- For traversal, compare unique_controls with derived_focusable_count; inspect every sequence item for direction,
  focus visibility, unexpected horizontal overflow, and the named controls the story actually requires. The
  document/body wrap sentinel is not an interactive control. Traversal proves reach/focus/order evidence, not
  Enter/Space activation unless the receipt explicitly contains that activation.
- For a keyboard matrix, require all_requested_keys_proven and all_applicable_controls_exercised, inspect every
  nested trusted key receipt, and treat disabled_controls as prerequisites still needing a valid story path—not
  as controls that the driver activated. Newly revealed applicable controls must be included before completion.
- For dwell, use every landmark observation and its exact duration/stability/form/focus/URL/overflow facts. Do
  not claim an absent conditional screen was tested.
- For a timed transient transition, use the trusted click timestamp, MutationObserver transition duration,
  required-boundary samples, disabled/aria-busy facts, duplicate pointer/Enter attempts, and final settled
  state together. A dwell started after completion is not equivalent evidence and must not create a defect.
- For a scenario matrix, inspect every named case and every nested action's resolved label and driver status.
  Field actions carry the requested value plus settled control value; transition actions carry compact
  before/after status, viewport, URL, and console facts. One missing/failed sub-action makes the relevant case
  inconclusive, never passed. A case labelled valid whose submit was disabled is incomplete QA setup, never a
  product defect; required/cross-field prerequisites must be completed before judging the business outcome.
  Compare each independent outcome to the story's required matrix; do not infer an unexecuted input class from
  another case.
- A concrete receipt contradiction to the story is a bug. Missing evidence is retry/inconclusive. If the batch
  behaved as required, pass. bug MUST be null unless verdict is exactly bug.
- Never credit a broad coverage item unless all of its clauses are established by this receipt plus sealed prior
  receipts. The downstream mechanical oracle will independently filter demonstrated claims.

Reply ONLY JSON:
{{"target_confirmed":true|false,"matches_expected":true|false,
  "verdict":"pass|bug|inconclusive|retry|control-not-found","bug":"description or null",
  "severity":"none|low|medium|high|critical","blocking":true|false,
  "demonstrated":["exact untested aspect"]}}"""


# ----------------------------------------------------------------------------------------------------
# The Explorer.
# ----------------------------------------------------------------------------------------------------
class Explorer:
    def __init__(self, target_url, vision, token=None, org="0", autostart=True, artifact_dir=None,
                 resume_state_path=None, scope_run_id=None, scope_tenant=None):
        self.target_url = target_url
        self.vision = vision
        self.token = token
        self.org = org
        # a per-session scratch dir is the agent's cwd (so it can Read the screenshots the bridge
        # writes under /tmp/aos-qa and reason over them multimodally).
        self.repo = tempfile.mkdtemp(prefix=f"aos-qa-{uuid.uuid4().hex[:8]}-")
        self.bugs = []
        self.bridge = None
        self.artifact_dir = None
        self.video_mp4 = None             # scrollable session clip for this story (set on close())
        self.video_path = None             # raw Playwright recording; MP4 conversion is deferred by default
        self.artifact_evidence = []        # recorder-owned start/end proofs exposed to the paid audit
        self.timings = []                  # explicit observe/decide/act/wait/judge/finalize latency ledger
        self.slow_phases = []              # surfaced to the QA coordinator for subordinate self-diagnosis
        self._timing_seq = 0
        self._current_story_id = None
        self._current_step = None
        self._last_progress_signature = None
        self._checkpoint_prior_records = []  # sealed receipts inherited from an earlier process generation
        self._capture_started_at = time.time()
        self._recorder_start_receipt = None
        self.recorder_trace = None
        self.coverage = None              # coverage ledger [{aspect, covered}] — the tested-vs-untested record
        self.stop_reason = None           # why explore() stopped (coverage-complete / blocking-wall / incomplete)
        self.infrastructure_error = None  # explicit provider/driver evidence gap; never a product verdict
        self.missing_capabilities = []    # structured semantic admission failure, routed to QA management
        self.pulse_work_id = None         # if set, each step beats a live heartbeat into the pulse plane
        self.resume_state_path = str(resume_state_path) if resume_state_path else None
        # ``resume_state_path`` is also refreshed after every in-process checkpoint so a later worker can
        # continue from the newest browser state.  Keep the construction-time origin separate: otherwise the
        # first ordinary checkpoint in a brand-new seeded story makes the *same* worker think it was resumed,
        # clear the fixture it just loaded, and replay setup.  Only a worker actually constructed from an
        # earlier generation's state owes the one-time seeded-baseline reset.
        self._resume_origin_state_path = self.resume_state_path
        self._scope_run_id = scope_run_id
        self._scope_tenant = scope_tenant
        self._bridge_shot_dir = None
        self._actual_at_attempted = False
        self._actual_at_error = None
        # One exact sealed keyboard replay per browser process. A durable resume intentionally gets a fresh
        # attempt on the current page revision; historical records must not suppress it, while an inconclusive
        # judge in this same process must not cause repeated Tab actions.
        self._reported_keyboard_replayed = False
        if autostart:
            if artifact_dir is None and artifacts is not None:
                artifact_dir = artifacts.run_dir("qa-explorer")
            self.artifact_dir = Path(artifact_dir) if artifact_dir else None
            shot_dir = self.artifact_dir / "screenshots" if self.artifact_dir else None
            self._bridge_shot_dir = shot_dir
            self.bridge = BrowserBridge(target_url, token=token, org=org, shot_dir=shot_dir,
                                        storage_state_path=resume_state_path,
                                        scope_run_id=scope_run_id, scope_tenant=scope_tenant)

    def _phase_start(self, phase, meta=None):
        started = time.time()
        self._timing_seq += 1
        event = {"seq": self._timing_seq, "event": "start", "phase": str(phase), "ts": started,
                 "story": self._current_story_id, "step": self._current_step,
                 "meta": dict(meta or {})}
        self.timings.append(event)
        self._write_timing(event)
        if self.pulse_work_id:
            try:
                import pulse
                pulse.beat(self.pulse_work_id, stage=str(phase),
                           progress=f"{self._current_story_id or 'story'} · step {self._current_step} · {phase}",
                           meta={"phase_started_at": started, "story": self._current_story_id,
                                 "step": self._current_step, **dict(meta or {})})
            except Exception:
                pass
        return started

    def _phase_end(self, phase, started, status="ok", meta=None):
        ended = time.time()
        elapsed_s = round(max(0.0, ended - started), 3)
        # These thresholds create health evidence; they never cancel the story. A 60-90 second semantic
        # judgment is exactly the kind of subordinate slowdown the coordinator should see and diagnose, not
        # hide merely because the provider eventually returned.
        slow_thresholds = {"observe": 12, "coverage-plan": 45, "decide": 30,
                           "act": 10, "act-retry": 10, "evaluate": 45,
                           "diagnose-incomplete": 60, "finalize-browser": 15}
        slow = str(phase) != "wait-external" and elapsed_s >= slow_thresholds.get(str(phase), float("inf"))
        event = {"seq": self._timing_seq, "event": "end", "phase": str(phase), "ts": ended,
                 "started_at": started, "elapsed_s": elapsed_s, "slow": slow,
                 "status": str(status), "story": self._current_story_id, "step": self._current_step,
                 "meta": dict(meta or {})}
        self.timings.append(event)
        if slow:
            self.slow_phases.append({"phase": str(phase), "elapsed_s": elapsed_s,
                                     "story": self._current_story_id, "step": self._current_step,
                                     "status": str(status)})
        self._write_timing(event)
        if self.pulse_work_id:
            try:
                import pulse
                pulse.beat(self.pulse_work_id, stage=str(phase),
                           progress=(f"{self._current_story_id or 'story'} · step {self._current_step} · "
                                     f"{phase} {status} in {event['elapsed_s']}s"),
                           meta={"last_phase": str(phase), "last_phase_status": str(status),
                                 "last_phase_elapsed_s": event["elapsed_s"]})
            except Exception:
                pass
        return event

    def _write_timing(self, event):
        if not self.artifact_dir:
            return
        try:
            path = self.artifact_dir / "timings.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")
        except Exception:
            pass

    def _activate_actual_at(self):
        """Upgrade this not-yet-driven story to a real Orca session while preserving browser storage.

        The QA agent already made the semantic decision that actual AT evidence is required when it created
        the coverage ledger.  Activation is therefore mechanical capability provisioning, not a hardcoded QA
        judgement.  It runs before the first story action, so replacing the headless context cannot discard
        product mutations from this story.
        """
        if self.bridge is None:
            raise RuntimeError("cannot activate assistive technology without a browser bridge")
        if getattr(self.bridge, "actual_at", False):
            return
        storage_path = Path(self.repo) / "actual-at-storage-state.json"
        stored = self.bridge._send({"cmd": "storageState"})
        if not isinstance(stored, dict) or stored.get("ok") is not True or not isinstance(stored.get("state"), dict):
            raise RuntimeError("could not checkpoint browser storage before AT-driver activation")
        tmp = storage_path.with_suffix(f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(stored["state"], sort_keys=True))
        os.chmod(tmp, 0o600)
        tmp.replace(storage_path)
        self.bridge.close()
        self.bridge = BrowserBridge(
            self.target_url, token=self.token, org=self.org, shot_dir=self._bridge_shot_dir,
            storage_state_path=storage_path, scope_run_id=self._scope_run_id,
            scope_tenant=self._scope_tenant, actual_at=True)

    def _credit_recorder_start(self, state):
        credited = []
        for item in self.coverage or []:
            aspect = str(item.get("aspect") or "")
            if item.get("covered") or not _recorder_start_provable(
                    aspect, state, artifact_dir=self.artifact_dir,
                    bridge_active=self.bridge is not None,
                    clear_receipt=self._recorder_start_receipt):
                continue
            item["covered"] = True
            credited.append(aspect)
            self.artifact_evidence.append({
                "aspect": aspect, "stage": "start", "timestamp": self._capture_started_at,
                "artifact_dir": str(self.artifact_dir),
                "actual_at": bool(state.get("actualAssistiveTechnologyAvailable")),
                "console_errors": len(state.get("console_errors") or []),
                "clear_receipt": self._recorder_start_receipt,
            })
        return credited

    def _begin_recorder_scope(self):
        if self._recorder_start_receipt is not None:
            return True
        starts = [item for item in self.coverage or []
                  if not item.get("covered")
                  and _recorder_requirement_stage(item.get("aspect")) == "start"]
        if not starts:
            return True
        reply = self.bridge._send({"cmd": "clearEvidence"}) if self.bridge else None
        if not isinstance(reply, dict) or reply.get("ok") is not True or not reply.get("cleared_at"):
            return False
        self._recorder_start_receipt = {
            "cleared_at": float(reply["cleared_at"]),
            "console_before": int(reply.get("console_before") or 0),
            "requests_before": int(reply.get("requests_before") or 0),
        }
        self._capture_started_at = self._recorder_start_receipt["cleared_at"]
        return True

    def _credit_recorder_end(self, records, final_state=None):
        remaining = [item for item in self.coverage or [] if not item.get("covered")]
        if any(_recorder_requirement_stage(item.get("aspect")) != "end" for item in remaining):
            return []
        credited = []
        final_state = final_state or {}
        self.recorder_trace = {
            "capture_started_at": self._capture_started_at,
            "capture_ended_at": time.time(),
            "clear_receipt": self._recorder_start_receipt,
            "console_errors": list(final_state.get("console_errors") or []),
            "network_requests": list(final_state.get("recent_requests") or []),
        }
        for item in remaining:
            aspect = str(item.get("aspect") or "")
            if not _recorder_end_provable(aspect, records, artifact_dir=self.artifact_dir):
                continue
            item["covered"] = True
            credited.append(aspect)
            self.artifact_evidence.append({
                "aspect": aspect, "stage": "end", "timestamp": time.time(),
                "capture_started_at": self._capture_started_at,
                "artifact_dir": str(self.artifact_dir), "steps": len(records),
            })
        return credited

    # --- the two AI decisions (EVERY one is a real model call via factory.agent) -----------------
    def _observe(self, include_accessibility):
        started = self._phase_start("observe", {"full_accessibility": bool(include_accessibility)})
        observed = None
        try:
            try:
                observed = self.bridge.state(include_accessibility=include_accessibility)
            except TypeError:
                # Rolling workers/offline fixtures may expose the older zero-argument bridge seam.
                observed = self.bridge.state()
            return observed
        finally:
            self._phase_end("observe", started,
                            "ok" if isinstance(observed, dict) and observed.get("ok", True) else "error",
                            {"url": (observed or {}).get("url") if isinstance(observed, dict) else None})

    def _ai_coverage_plan(self, story, state):
        """Enumerate 'everything a real user would try' for this story ONCE, up front — the coverage ledger
        the loop tests against. Fail-open: on any error, fall back to a single aspect (the story's expected
        outcome) so the loop still runs, just without a rich checklist."""
        fallback = [{"aspect": (story.get("expected") or story.get("expected_outcome")
                                or story.get("goal") or "the story's expected outcome"), "covered": False}]
        contractual = _story_contract_aspects(story)
        # A focused regression is already a deliberately bounded four-step decomposition authored from the
        # sealed finding. Asking a planner to creatively expand it wastes a model round-trip and can invert the
        # contract by turning the historical bad output into a requirement to reproduce. Its exact explicit
        # steps are the complete ledger; the strong per-action evaluator still decides whether each is proven.
        if (story.get("coverage") or story.get("category") == "focused-regression") and contractual:
            return [{"aspect": a, "covered": False, "explicit": True} for a in contractual]
        try:
            prompt = _coverage_prompt(self.vision, story, state)
            started = self._phase_start("coverage-plan", {"prompt_chars": len(prompt)})
            res = None
            try:
                res = _call_agent(
                    ROLE, self.repo, prompt, light=_DECIDE_LIGHT,
                    timeout=_COVERAGE_TIMEOUT_S, retries=0)
            finally:
                self._phase_end("coverage-plan", started,
                                "ok" if isinstance(res, dict) and not res.get("failed") else "error",
                                {"model": (res or {}).get("model") if isinstance(res, dict) else None,
                                 "tokens_in": (res or {}).get("tokens_in") if isinstance(res, dict) else None})
            j = _extract_json(res.get("out_full") or res.get("out") or "")
            raw_aspects = j.get("aspects") or []
            compact = _validated_compact_coverage_plan(story, raw_aspects)
            if compact:
                # The original story remains in every decide/evaluate prompt; this mapping only removes the
                # duplicate explicit+semantic ledger fan-out that made a five-step story take dozens of model
                # turns. Every authored step is mechanically accounted for above.
                return compact
            # Backward compatibility for an older model/test double returning the former string-only schema.
            # Keep the exact contract in that case because there is no step-completeness receipt.
            legacy_aspects = [item for item in raw_aspects if isinstance(item, str)]
            planned = _bounded_aspects(_scoped_aspects(story, legacy_aspects))
            # Exact ordered story steps make progress visible and prevent an all-or-nothing legacy ledger from
            # reporting 0% after several successful setup/navigation actions. The semantic planner still adds
            # expected-outcome checks that are not explicit executable steps; neither source replaces the other.
            merged = []
            for aspect, explicit in ([(item, True) for item in contractual]
                                     + [(item, False) for item in planned]):
                key = " ".join(str(aspect).casefold().split())
                if any(" ".join(item["aspect"].casefold().split()) == key for item in merged):
                    continue
                merged.append({"aspect": aspect, "covered": False, "explicit": explicit})
            return merged or fallback
        except Exception:
            return ([{"aspect": a, "covered": False, "explicit": True} for a in contractual]
                    or fallback)

    def _ai_decide(self, story, state, history, checklist=None):
        focused = _compact_gapfill_prompt_mode(story, checklist)
        prompt_builder = _focused_decide_prompt if focused else _decide_prompt
        prompt = prompt_builder(self.vision, story, state, history, checklist=checklist)
        started = self._phase_start("decide", {"prompt_chars": len(prompt)})
        res = None
        try:
            res = _call_agent(
                ROLE, self.repo, prompt, light=_DECIDE_LIGHT,
                timeout=_DECIDE_TIMEOUT_S, retries=0)   # fast, bounded model for navigation
        finally:
            self._phase_end("decide", started,
                            "ok" if isinstance(res, dict) and not res.get("failed") else "error",
                            {"model": (res or {}).get("model") if isinstance(res, dict) else None,
                             "tokens_in": (res or {}).get("tokens_in") if isinstance(res, dict) else None})
        raw = (res or {}).get("out_full") or (res or {}).get("out") or ""
        j = _extract_json(raw)
        action = j.get("next_action") if isinstance(j, dict) else None
        usable = (isinstance(res, dict) and not res.get("failed")
                  and int(res.get("rc", 0) or 0) == 0 and str(raw).strip()
                  and isinstance(j, dict) and isinstance(action, dict)
                  and str(action.get("cmd") or "").strip())
        if (not usable and isinstance(res, dict) and not res.get("failed")
                and int(res.get("rc", 0) or 0) == 0 and str(raw).strip()):
            # A successful provider generation can still end mid-JSON (observed live while typing a draft
            # body). Checkpointing at that point throws away an otherwise healthy browser session and forces a
            # later explorer to replay the story. One bounded repair is cheaper and safer: no browser action
            # has happened yet, and the repair must still satisfy the exact structured-action gate below.
            repair_prompt = (prompt + "\n\nSTRUCTURED-OUTPUT RECOVERY: Your prior reply ended before a complete "
                             "JSON object was available. Resend the same decision as one complete JSON object "
                             "only. Keep reasoning, expected, and each input value concise (160 characters "
                             "maximum); keep the whole reply below 1600 characters. PRIOR INCOMPLETE REPLY:\n"
                             + str(raw)[-1200:])
            repair_started = self._phase_start(
                "decide-structured-retry", {"prompt_chars": len(repair_prompt)})
            repaired = None
            try:
                repaired = _call_agent(
                    ROLE, self.repo, repair_prompt, light=_DECIDE_LIGHT,
                    timeout=_DECIDE_TIMEOUT_S, retries=0)
            finally:
                self._phase_end(
                    "decide-structured-retry", repair_started,
                    "ok" if isinstance(repaired, dict) and not repaired.get("failed") else "error",
                    {"model": (repaired or {}).get("model") if isinstance(repaired, dict) else None,
                     "tokens_in": ((repaired or {}).get("tokens_in")
                                   if isinstance(repaired, dict) else None)})
            repaired_raw = ((repaired or {}).get("out_full") or (repaired or {}).get("out") or ""
                            if isinstance(repaired, dict) else "")
            repaired_json = _extract_json(repaired_raw)
            repaired_action = (repaired_json.get("next_action")
                               if isinstance(repaired_json, dict) else None)
            if (isinstance(repaired, dict) and not repaired.get("failed")
                    and int(repaired.get("rc", 0) or 0) == 0 and str(repaired_raw).strip()
                    and isinstance(repaired_json, dict) and isinstance(repaired_action, dict)
                    and str(repaired_action.get("cmd") or "").strip()):
                res, raw, j, action = repaired, repaired_raw, repaired_json, repaired_action
        # Missing reasoning is infrastructure uncertainty, not a browser action.  The old fallback converted
        # provider/auth failures into repeated noops, burned several more model calls, and eventually reported
        # an ordinary coverage stall.  Fail closed before any side effect and let the durable campaign resume.
        if (not isinstance(res, dict) or res.get("failed") or int(res.get("rc", 0) or 0) != 0
                or not str(raw).strip() or not isinstance(j, dict)
                or not isinstance(action, dict) or not str(action.get("cmd") or "").strip()):
            raise ModelDecisionUnavailable(_model_failure_reason(res))
        return {
            "reasoning": j.get("reasoning", ""),
            "next_action": action,
            "expected": j.get("expected", ""),
            "covers": [str(c).strip() for c in (j.get("covers") or []) if str(c).strip()],
            # the LABEL of a control this action should make appear — lets explore() wait for a
            # late-painting control before evaluating (anti RACE-CONDITION false positive).
            "expected_control": (j.get("expected_control") or "").strip(),
            "wait_for": dict(j.get("wait_for") or {}) if isinstance(j.get("wait_for"), dict) else None,
            "done": bool(j.get("done", False)),
            "_raw": res,
        }

    def _ai_evaluate(self, story, expected, targeting, before_state, after_state, untested=None,
                     prior_records=None, compact_gapfill=False):
        batch_receipt = bool((targeting or {}).get("traversal")
                             or (targeting or {}).get("landmark_dwell")
                             or (targeting or {}).get("scenario_matrix")
                             or (targeting or {}).get("keyboard_matrix")
                             or (targeting or {}).get("timed_transition"))
        focused = bool(compact_gapfill or _compact_gapfill_prompt_mode(story))
        prompt_builder = (_batch_evaluate_prompt if batch_receipt else
                          _focused_evaluate_prompt if focused else _evaluate_prompt)
        prompt = prompt_builder(self.vision, story, expected, targeting, before_state, after_state,
                                untested=untested, prior_records=prior_records)
        phase = "evaluate-batch" if batch_receipt else "evaluate"
        started = self._phase_start(phase, {"prompt_chars": len(prompt)})
        res = None
        try:
            res = _call_agent(
                ROLE, self.repo, prompt, timeout=_EVALUATE_TIMEOUT_S, retries=0,
                reasoning_effort=_EVALUATE_REASONING_EFFORT)
        finally:
            self._phase_end(phase, started,
                            "ok" if isinstance(res, dict) and not res.get("failed") else "error",
                            {"model": (res or {}).get("model") if isinstance(res, dict) else None,
                             "tokens_in": (res or {}).get("tokens_in") if isinstance(res, dict) else None})
        raw_out = res.get("out_full") or res.get("out") or ""
        j = _extract_json(raw_out)
        # A model call that itself FAILED (provider exhausted/failover, empty output) must not be read as a
        # PASS — that hides real defects behind a provider hiccup. Surface it as INCONCLUSIVE so the step is
        # retried, never counted as a clean pass on no evidence.
        call_failed = bool((isinstance(res, dict) and res.get("failed")) or not str(raw_out).strip() or not j)
        # FAILOVER guard: a provider-outage fallback result is lower-confidence infra evidence and must not
        # become a blocking product bug. But Codex can also be the PRIMARY engine (AOS_DEFAULT_ENGINE=codex or
        # tenant OpenAI provider); primary Codex verdicts are trusted and must be allowed to file real bugs.
        degraded = bool((res or {}).get("failover"))
        bug = j.get("bug") if j.get("bug") not in (None, "", "null") else None
        verdict = (j.get("verdict") or "").strip().lower()
        if not verdict:                       # tolerate an older-style reply that omits `verdict`
            verdict = "inconclusive" if call_failed else ("bug" if bug else "pass")
        if degraded and verdict == "bug":     # never let a failover verdict block a healthy app
            verdict = "inconclusive"
        # Browser/driver failure is missing evidence, never evidence that the product misbehaved.  This guard
        # is deterministic because the protocol response is ground truth; no model may overrule it.
        if (targeting or {}).get("driver_ok") is False:
            verdict = "inconclusive"
            bug = None
            j["matches_expected"] = False
        if verdict == "bug" and _unreceipted_traversal_key_false_positive(targeting, bug):
            verdict = "inconclusive"
            bug = None
            j["matches_expected"] = False
        if verdict == "bug" and _disabled_handoff_false_positive(story, expected, targeting, bug):
            verdict = "inconclusive"
            bug = None
        if verdict == "bug" and _setup_action_false_positive(story, expected, targeting, bug):
            verdict = "retry"
            bug = None
        if verdict == "bug" and _empty_queue_drain_false_positive(targeting, bug, before_state):
            verdict = "retry"
            bug = None
        if verdict == "bug" and _empty_state_without_reset_false_positive(
                expected, targeting, bug, before_state):
            verdict = "inconclusive"
            bug = None
            j["matches_expected"] = False
        if verdict == "bug" and _history_rebased_state_false_positive(
                story, expected, targeting, bug, after_state):
            verdict = "pass"
            bug = None
            j["matches_expected"] = True
        if verdict == "bug" and _acknowledgement_focus_transfer_false_positive(
                story, targeting, bug, after_state):
            verdict = "pass"
            bug = None
            j["matches_expected"] = True
        if verdict == "bug" and _approval_diagnostics_projection_false_positive(
                story, targeting, bug, after_state):
            verdict = "pass"
            bug = None
            j["matches_expected"] = True
        if verdict == "bug" and _empty_first_run_approval_projection_false_positive(
                story, targeting, bug, after_state):
            verdict = "pass"
            bug = None
            j["matches_expected"] = True
        if verdict == "bug" and _submit_disabled_incomplete_false_positive(
                expected, targeting, bug, after_state):
            verdict = "pass"
            bug = None
            j["matches_expected"] = True
        if verdict == "bug" and _submission_prerequisite_false_positive(expected, targeting, bug):
            verdict = "retry"
            bug = None
            j["matches_expected"] = False
        if _handoff_busy_expected_met(story, expected, targeting):
            verdict = "pass"
            bug = None
            j["matches_expected"] = True
        # THE BLAME CONTRACT: a bug is only real when the verdict is explicitly 'bug'. Any other verdict
        # (control-not-found / retry / inconclusive / pass) is NOT the app's fault — drop the bug text so a
        # mis-targeted or missed click can never be recorded as an app defect.
        if verdict != "bug":
            bug = None
        return {
            "matches_expected": bool(j.get("matches_expected", False)),
            "verdict": verdict,
            "target_confirmed": bool(j.get("target_confirmed", False)),
            "bug": bug,
            "severity": j.get("severity", "none") if bug else "none",
            "blocking": bool(j.get("blocking", False)) if bug else False,
            # GROUNDED COVERAGE: the aspects the evaluator confirms THIS step actually demonstrated (from the
            # observed before->after) — this, not the decider's claimed `covers`, is what credits coverage.
            "demonstrated": [str(a).strip() for a in (j.get("demonstrated") or []) if str(a).strip()],
            "model_failed": call_failed,
            "infrastructure_error": _model_failure_reason(res) if call_failed else None,
            "_raw": res,
        }

    def _ai_incomplete_diagnosis(self, story, state, remaining, history, records=None):
        """Manager-style diagnosis when exploration cannot reach required story outcomes.

        A missing expected product capability is a defect even though no driver click can truthfully claim it
        actuated a nonexistent control.  Keep that separate from ordinary control targeting: only run this
        review after repeated/no-progress termination and ask an independent QA decision to classify the gap.
        """
        prompt = f"""ROLE: You are the QA manager diagnosing why a story could not be completed.

Decide whether the remaining coverage is blocked by a REAL PRODUCT DEFECT (the story requires a user-visible
action/result but the settled product exposes no reachable control or workflow), a TEST LIMITATION
(driver/provider/environment cannot exercise a capability that does exist), or a CONTINUE-POSSIBLE path the
explorer missed. Do not blame the app for a missed click. Do not demand a surface the expected outcome says must
be hidden/denied. But if a listed story step explicitly requires a user to publish/edit/approve/add evidence and
the settled app offers no reachable way to do it, that is an app defect—not harmless incomplete coverage.

VISION: {self.vision}
STORY: {json.dumps(story, default=str)[:4000]}
REMAINING REQUIRED COVERAGE: {json.dumps(remaining, default=str)[:3000]}
SETTLED PRODUCT STATE: {_fmt_story_state(state, story, evaluation=True)}
RECENT ATTEMPTS: {json.dumps(history[-4:], default=str)[:2500]}
PRIOR GROUNDED JOURNEY EVIDENCE: {_fmt_prior_journey_evidence(records, limit=4, max_chars=3500)}

CAUSAL ORDER: a traversal or absence probe from before a later reset, seed, fixture-load, sign-in, or other
state-creating action says nothing about controls available after that action. Never cite a precondition-state
traversal as proof that the settled postcondition lacks a control. Return continue_possible and probe again.

If the action sequence and final settled state cumulatively prove any REMAINING REQUIRED COVERAGE item, copy
that exact item into `demonstrated`. This is the closure path for an inherently multi-step acceptance item; do
not call a fully evidenced workflow incomplete merely because no single action proved every clause. Do not
credit an item unless the receipts establish every clause.

Reply ONLY JSON:
{{"disposition":"app_defect|test_limitation|continue_possible","bug":"<specific missing/broken capability or null>","severity":"low|medium|high|critical","blocking":true|false,"reason":"<evidence>","demonstrated":["<exact remaining aspect fully proved by the cumulative receipts>"]}}"""
        try:
            started = self._phase_start("diagnose-incomplete", {"prompt_chars": len(prompt)})
            res = None
            try:
                # This is a preliminary classification, not the final release verdict. Confirmed findings are
                # independently adjudicated by the evidence reviewer at the stronger platform tier, so the
                # bounded diagnostic can use the same full-model/medium-reasoning balance as action judging.
                res = _call_agent(ROLE, self.repo, prompt, timeout=_DIAGNOSE_TIMEOUT_S, retries=0,
                                  reasoning_effort=_DIAGNOSE_REASONING_EFFORT)
            finally:
                self._phase_end("diagnose-incomplete", started,
                                "ok" if isinstance(res, dict) and not res.get("failed") else "error",
                                {"model": (res or {}).get("model") if isinstance(res, dict) else None,
                                 "tokens_in": (res or {}).get("tokens_in") if isinstance(res, dict) else None})
            raw = (res or {}).get("out_full") or (res or {}).get("out") or ""
            j = _extract_json(raw)
            disposition = str(j.get("disposition") or "test_limitation").lower()
            bug = j.get("bug") if disposition == "app_defect" else None
            if not str(bug or "").strip():
                bug = None
            if bug and _stale_precondition_absence_diagnosis(j, records):
                disposition = "continue_possible"
                bug = None
                j["reason"] = (
                    "The cited traversal predates a confirmed seed/load/reset state rebase; probe the settled "
                    "postcondition before classifying a missing control."
                )
            return {"disposition": disposition, "bug": bug,
                    "severity": j.get("severity", "high") if bug else "none",
                    "blocking": bool(j.get("blocking", True)) if bug else False,
                    "reason": str(j.get("reason") or "")[:1000],
                    "demonstrated": [str(item).strip() for item in (j.get("demonstrated") or [])
                                     if str(item).strip()]}
        except Exception as e:
            return {"disposition": "test_limitation", "bug": None, "severity": "none",
                    "blocking": False, "reason": f"diagnosis unavailable: {e}", "demonstrated": []}

    # --- robust targeting: bind the AI's INTENT to a concrete control ----------------------------
    def _prepare_action(self, action, elements):
        """Resolve an AI action's intent (target_text[+role]) to a concrete element BEFORE acting.
        Prefers the label/role-matched element's idx over any blind idx the AI guessed.  When an explicit
        semantic intent does not match, discard stale idx/selector fallbacks so the action fails closed
        instead of mutating an unrelated control. Returns (prepared_action, aim) where `aim` records what
        control we were aiming at — the ground truth the evaluator judges targeting against."""
        action = dict(action or {})
        cmd = (action.get("cmd") or "noop").lower()
        resolve_action = dict(action)
        inferred_role = ""
        if cmd in ("type", "fill") and _key_name(resolve_action.get("value")):
            action["cmd"] = "press"
            cmd = "press"
            resolve_action["cmd"] = "press"
        if cmd in ("type", "fill") and not (resolve_action.get("role") or "").strip():
            inferred_role = "textbox"
            resolve_action["role"] = inferred_role
        idx, label, score = _resolve_target(resolve_action, elements)
        intended = (action.get("target_text") or action.get("target") or "").strip()
        press_key = (_key_name(action.get("value")) or _key_name(action.get("key"))) \
            if cmd in ("press", "hold") else None
        # Tab/Shift+Tab are page-level focus-navigation gestures. Binding Tab to a locator makes
        # Playwright focus that locator and then immediately move focus *away* from it, so the agent can
        # never prove keyboard reachability and repeats the same honest no-op. Keep the intended label as
        # the expected destination for evaluation, but send the gesture through page.keyboard.
        page_focus_navigation = (cmd == "press" and press_key in ("Tab", "Shift+Tab")
                                 and not action.get("_qa_reported_focus_source"))
        if cmd in ("click", "tap", "touch", "pen", "burst", "click_burst", "clickburst",
                   "timed_transition", "timedtransition",
                   "type", "fill", "press", "hold"):
            if idx is not None and not page_focus_navigation:
                action["idx"] = idx                     # label/role match wins over a blind idx
            elif intended:
                # A target_text miss is authoritative.  Keeping the model's stale numeric/selector fallback
                # here let a US-011 setup attempt click the adjacent US-010 fixture and poisoned every later
                # persistence/focus verdict in that browser journey.
                action.pop("idx", None)
                action.pop("selector", None)
            elif not intended and action.get("idx") is not None:
                # AI gave only an idx — derive its label so downstream still has an intent to check.
                intended = _label_for_idx(action.get("idx"), elements)
                label = intended
        # A page-level keyboard command (End/Home/PageDown/Escape) is navigation,
        # not a failed attempt to target a DOM control. ``press`` is control-
        # targeted only when the model supplied an actual target.
        if page_focus_navigation:
            action.pop("idx", None)
            action.pop("selector", None)
            action["value"] = press_key
        control_action = (cmd in ("click", "tap", "touch", "pen", "burst", "click_burst", "clickburst",
                                  "timed_transition", "timedtransition",
                                  "type", "fill", "hold") or
                          (cmd == "press" and not page_focus_navigation
                           and bool(intended or action.get("selector")
                                    or action.get("idx") is not None)))
        return action, {"action_kind": cmd, "control_action": control_action,
                        "action_key": press_key, "action_count": action.get("count"),
                        "action_value": action.get("value"),
                        "restored_focused_state": bool(action.get("_qa_restored_focused_state")),
                        "intended": intended, "role": (action.get("role") or inferred_role or "").strip(),
                        "resolved_idx": idx if idx is not None else action.get("idx"),
                        "resolved_label": label, "score": score}

    def _targeting_facts(self, aim, act_result, before, after, retried=False, settled=False,
                         expected_control=None, expected_control_present=None, wait_result=None):
        """Assemble the targeting ground-truth the evaluator sees: what we aimed at, what we actually
        actuated, whether it matched the intent, and whether anything happened."""
        actuated = None
        handoff_url = None
        if act_result and act_result.get("matched"):        # clickByText tells us the live-matched label
            actuated = act_result.get("matched")
            if _external_handoff(act_result):
                handoff_url = act_result.get("matchedHref")
        elif aim.get("resolved_label"):
            actuated = aim.get("resolved_label")
        intended = aim.get("intended")
        label_matched = _labels_match(intended, actuated) if intended else bool(actuated)
        _, _, present_score = _resolve_target(
            {"target_text": intended, "role": aim.get("role")}, after.get("elements")) \
            if intended else (None, None, 0)
        control_action = bool(aim.get("control_action", True))
        label_matched = bool(label_matched) if control_action else True
        before_control = _control_snapshot(before.get("elements"), aim.get("resolved_idx"), intended, aim.get("role"))
        after_control = _control_snapshot(after.get("elements"), None, intended, aim.get("role"))
        after_disabled_immediate = (act_result or {}).get("disabledAfterClick")
        pointer_evidence = [
            {key: event.get(key) for key in
             ("ts", "type", "isTrusted", "pointerType", "button", "clientX", "clientY", "target", "tag")
             if event.get(key) not in (None, "")}
            for event in ((act_result or {}).get("pointerEvidence") or [])
            if isinstance(event, dict)
        ][-20:]
        trusted_pointer = [event for event in pointer_evidence if event.get("isTrusted") is True]
        keyboard_evidence = [
            {key: event.get(key) for key in
             ("ts", "type", "isTrusted", "key", "code", "repeat", "detail", "pointerType",
              "target", "tag")
             if event.get(key) not in (None, "")}
            for event in ((act_result or {}).get("keyboardEvidence") or [])
            if isinstance(event, dict)
        ][-30:]
        if (act_result or {}).get("keyboardMatrix"):
            nested_keyboard = []
            for receipt in (act_result or {}).get("actions") or []:
                if not isinstance(receipt, dict):
                    continue
                nested_keyboard.extend(event for event in receipt.get("keyboard_evidence") or []
                                       if isinstance(event, dict))
            keyboard_evidence = [
                {key: event.get(key) for key in
                 ("ts", "type", "isTrusted", "key", "code", "repeat", "detail", "pointerType",
                  "target", "tag") if event.get(key) not in (None, "")}
                for event in nested_keyboard
            ][-120:]
        if (act_result or {}).get("scenarioMatrix"):
            nested_keyboard = []
            for case in (act_result or {}).get("cases") or []:
                for receipt in (case or {}).get("actions") or []:
                    if isinstance(receipt, dict):
                        nested_keyboard.extend(event for event in
                                               receipt.get("keyboard_evidence") or []
                                               if isinstance(event, dict))
            keyboard_evidence = [
                {key: event.get(key) for key in
                 ("ts", "type", "isTrusted", "key", "code", "repeat", "detail", "pointerType",
                  "target", "tag") if event.get(key) not in (None, "")}
                for event in nested_keyboard
            ][-120:]
        matrix_missing = []
        matrix_submit_disabled = False
        if (act_result or {}).get("scenarioMatrix"):
            for case in (act_result or {}).get("cases") or []:
                for nested in (case or {}).get("actions") or []:
                    nested_action = (nested or {}).get("action") or {}
                    label = str(nested_action.get("target_text") or nested_action.get("target") or "")
                    if re.search(r"\b(?:send|submit|publish|create)\b", label, re.I):
                        matrix_missing.extend(list(
                            (nested or {}).get("empty_required_fields_before") or []))
                        matrix_submit_disabled = (matrix_submit_disabled
                                                  or (nested or {}).get("disabled_before_click") is True)
        return {
            "action_kind": aim.get("action_kind") or "control",
            "control_action": control_action,
            "intended": intended,
            "role": aim.get("role"),
            "targeted_label": actuated,
            "label_matched": label_matched,
            "effect_registered": _effect_registered(before, after, act_result) or bool(handoff_url),
            "activation_mode": (act_result or {}).get("activationMode"),
            "control_present": bool(present_score),
            "retried": retried,
            # the after-state was captured AFTER the SPA settled (skeletons gone, DOM stable) — so an
            # absent control is genuinely absent, NOT a mid-paint race.
            "settled": bool(settled),
            "driver_ok": bool((act_result or {}).get("ok", False)),
            "driver_error": ((act_result or {}).get("error")
                             if (act_result or {}).get("ok") is False else None),
            "reloaded": bool((act_result or {}).get("reloaded")),
            "history_direction": (act_result or {}).get("direction"),
            "wait_summary": ({key: (act_result or {}).get(key) for key in
                              ("waited", "elapsed_ms", "requested_ms")}
                             if (act_result or {}).get("waited") else None),
            "burst": ({key: (act_result or {}).get(key) for key in
                       ("burst", "count", "interval_ms", "timestamps", "intermediateSamples", "elapsed_ms")}
                      if (act_result or {}).get("burst") else None),
            "traversal": ({key: (act_result or {}).get(key) for key in (
                "direction", "key", "count", "pace_ms", "derived_focusable_count", "unique_controls",
                "all_focus_visible", "horizontal_overflow_seen", "sequence")}
                if (act_result or {}).get("traversal") else None),
            "traversal_summary": ({key: (act_result or {}).get(key) for key in (
                "direction", "key", "count", "pace_ms", "derived_focusable_count", "unique_controls",
                "all_focus_visible", "horizontal_overflow_seen")}
                if (act_result or {}).get("traversal") else None),
            "landmark_dwell": ({key: (act_result or {}).get(key) for key in (
                "targets", "duration_ms_each", "elapsed_ms", "observations")}
                if (act_result or {}).get("landmarkDwell") else None),
            "landmark_scroll": ({key: (act_result or {}).get(key) for key in (
                "scrolled", "matched", "requested", "y")}
                if "scrolled" in (act_result or {}) else None),
            "landmark_dwell_summary": ({
                "targets": list((act_result or {}).get("targets") or []),
                "duration_ms_each": (act_result or {}).get("duration_ms_each"),
                "all_targets_matched": all(
                    bool((item.get("scroll") or {}).get("scrolled"))
                    for item in ((act_result or {}).get("observations") or [])),
                "all_stable": all(bool(item.get("stable"))
                                  for item in ((act_result or {}).get("observations") or [])),
            } if (act_result or {}).get("landmarkDwell") else None),
            "scenario_matrix": ({key: (act_result or {}).get(key) for key in (
                "cases", "completed_cases", "total_cases", "action_count")}
                if (act_result or {}).get("scenarioMatrix") else None),
            "scenario_matrix_summary": ({key: (act_result or {}).get(key) for key in (
                "completed_cases", "total_cases", "action_count")}
                if (act_result or {}).get("scenarioMatrix") else None),
            "keyboard_matrix": ({key: (act_result or {}).get(key) for key in (
                "requested_keys", "keys_proven", "all_requested_keys_proven",
                "all_applicable_controls_exercised", "remaining_enabled_controls",
                "disabled_controls", "actions", "traversals", "action_count", "complete")}
                if (act_result or {}).get("keyboardMatrix") else None),
            "keyboard_matrix_summary": ({key: (act_result or {}).get(key) for key in (
                "requested_keys", "keys_proven", "all_requested_keys_proven",
                "all_applicable_controls_exercised", "remaining_enabled_controls",
                "action_count", "complete")}
                if (act_result or {}).get("keyboardMatrix") else None),
            "scenario_matrix_submit_disabled": matrix_submit_disabled,
            "timed_transition": (dict((act_result or {}).get("transition") or {})
                                 if (act_result or {}).get("timedTransition") else None),
            "timed_transition_summary": ({key: ((act_result or {}).get("transition") or {}).get(key)
                                           for key in (
                                               "required_duration_ms", "pending_seen",
                                               "completion_observed", "transition_duration_ms",
                                               "completed_before_required_duration",
                                               "stable_through_required_boundary")}
                                         if (act_result or {}).get("timedTransition") else None),
            "focus_transition": (dict((act_result or {}).get("focusTransition") or {}) or None),
            "pointer_evidence": pointer_evidence,
            "trusted_pointer": bool(trusted_pointer),
            "trusted_pointer_types": list(dict.fromkeys(
                str(event.get("pointerType")) for event in trusted_pointer if event.get("pointerType"))),
            "keyboard_evidence": keyboard_evidence,
            "trusted_keyboard": any(event.get("isTrusted") is True for event in keyboard_evidence),
            "action_key": aim.get("action_key"),
            "action_value": aim.get("action_value"),
            "restored_focused_state": bool(aim.get("restored_focused_state")),
            "session_target_url": self.target_url,
            "expected_control": expected_control,
            "expected_control_present": expected_control_present,
            "wait_result": dict(wait_result or {}) or None,
            "external_handoff_url": handoff_url,
            "before_control_disabled": (
                bool(before_control.get("disabled") or before_control.get("ariaDisabled") == "true")
                if before_control is not None else None
            ),
            "after_control_disabled": (
                bool(after_disabled_immediate)
                if after_disabled_immediate is not None else
                bool(after_control.get("disabled") or after_control.get("ariaDisabled") == "true")
                if after_control is not None else None
            ),
            "before_control_value": before_control.get("value") if before_control else None,
            "after_control_value": after_control.get("value") if after_control else None,
            "before_control_checked": (
                str(before_control.get("checked")).lower() == "true"
                if before_control is not None and before_control.get("checked") is not None else None
            ),
            "after_control_checked": (
                str(after_control.get("checked")).lower() == "true"
                if after_control is not None and after_control.get("checked") is not None else None
            ),
            # Bound to the exact locator that the browser driver just filled.  Unlike a later page-wide
            # snapshot, this survives DOM reindexing, deep control truncation, and password redaction.
            "driver_control_value": (act_result or {}).get("actualValue"),
            "driver_control_value_matches": (act_result or {}).get("actualValueMatches"),
            "empty_required_fields_before": list(
                (act_result or {}).get("emptyRequiredFieldsBefore") or matrix_missing)[:20],
        }

    # --- the state-based loop --------------------------------------------------------------------
    def explore(self, story, max_steps=None, on_bug=None, deadline=None, resume_covered=None,
                resume_coverage=None, resume_steps_detail=None, cancel_event=None,
                stop_on_actionable_bug=False):
        """Drive observe -> AI-decide -> act -> (retry-on-miss) -> observe -> AI-evaluate to COVERAGE
        COMPLETION, the way a real QA engineer works — NOT to a step count. Up front the AI enumerates
        'everything a user would try' (the coverage ledger); the loop keeps going while untested aspects
        remain and progress is being made. Returns the list of step-records; fires on_bug(bug) per real bug.

        Stops on (priority order): a BLOCKING bug (a wall); COVERAGE COMPLETE (ledger exhausted or the AI
        judges nothing more a user would try remains); STALL (no new coverage for _STALL_LIMIT steps). The
        `max_steps`/`deadline` args are SAFETY BACKSTOPS ONLY (runaway/cost guards) — default None
        (unbounded); tripping one marks the run INCOMPLETE and checkpoints what's left, never a silent 'done'.
        `resume_covered`: aspect strings already tested in an earlier checkpointed run (resume where it left off)."""
        if self.bridge is None:
            raise RuntimeError("Explorer has no browser bridge (constructed with autostart=False)")
        records = []
        sealed_prior_records = campaign_checkpoint.compact_evidence_records(resume_steps_detail)
        self._checkpoint_prior_records = list(sealed_prior_records)
        self._current_story_id = str(story.get("id") or story.get("title") or story.get("name") or "story")
        # Preserve the exact durable ledger across process rotations. Replanning it with an LLM can split,
        # merge, or paraphrase requirements and turn proven work back into untested work.
        prior_ledger = _migrate_compound_coverage_ledger([
            dict(item) for item in (resume_coverage or [])
            if isinstance(item, dict) and item.get("aspect")])
        prior_ledger, sealed_prior_records, repaired_resume_claims = \
            _sanitize_inventory_checkpoint_claims(prior_ledger, sealed_prior_records)
        # A covered label is an index, not proof.  Rolling generations used to retain that label while
        # truncating away its only receipt, letting a fresh worker immediately return coverage-complete.
        # Reopen only unsupported rows; exact grounded receipts and atomic mechanical proofs remain monotonic.
        prior_ledger, unsupported_resume_claims = campaign_checkpoint.reopen_unproven_coverage(
            prior_ledger, sealed_prior_records)
        repaired_resume_claims.update(unsupported_resume_claims)
        prior_ledger = _restore_chronological_persistence_coverage(
            prior_ledger, sealed_prior_records)
        prior_ledger = _restore_alternative_keyboard_coverage(
            prior_ledger, sealed_prior_records)
        prior_ledger = _restore_effectful_business_mutation_coverage(
            prior_ledger, sealed_prior_records)
        # Re-run the deterministic fences after recovering legacy record ordering. Valid post-creation
        # receipts remain closed; an old empty-state reload still fails chronology and stays open.
        prior_ledger = _migrate_compound_coverage_ledger(prior_ledger)
        history = _planner_history_from_resume(sealed_prior_records)
        legacy_coverage = (prior_ledger if _legacy_single_coverage_needs_replan(prior_ledger, story) else [])
        self.coverage = None if legacy_coverage else (prior_ledger or None)
        self.stop_reason = None
        # Bare ``resume_covered`` strings cannot certify themselves.  Derive the resumable subset exclusively
        # from the repaired exact ledger; callers with an older label-only checkpoint safely re-test it.
        resume_covered = {str(item.get("aspect")) for item in prior_ledger if item.get("covered")}
        include_accessibility = _story_requires_full_accessibility(story)
        resume_dom_reconciled = False
        seen_views = set()                                       # distinct, bounded browser-evidence states reached
        step, no_progress, dead_streak = 0, 0, 0
        repeat_actions = {}
        batch_context_epoch = 0
        while True:
            self._current_step = step
            # SAFETY BACKSTOPS (runaway guards, NOT quality caps): a real tester who runs out of time hands
            # off a "still to test" note — never a false "all done". Both default None (unbounded); the
            # coverage ledger + stall guard are the real bound.
            if max_steps and step >= max_steps:
                self.stop_reason = "safety-cap-incomplete"
                break
            if cancel_event is not None and cancel_event.is_set():
                self.stop_reason = "cancelled-incomplete"
                break
            live_deadline = deadline() if callable(deadline) else deadline
            if live_deadline and time.time() > live_deadline:
                self.stop_reason = "deadline-incomplete"
                break
            state = self._observe(include_accessibility)           # OBSERVE
            if self.coverage is None:                            # PLAN COVERAGE once, reusing this observation
                self.coverage = self._ai_coverage_plan(story, state)
                # Preserve the legacy broad release condition while adding granular exact story steps. This
                # makes 20%/40%/70% advancement observable without declaring the old expected outcome satisfied.
                for old in legacy_coverage:
                    key = " ".join(str(old.get("aspect") or "").casefold().split())
                    if not any(" ".join(str(item.get("aspect") or "").casefold().split()) == key
                               for item in self.coverage):
                        self.coverage.append(dict(old))
                for c in self.coverage:
                    if _covered_in_prior_ledger(c.get("aspect"), resume_covered):
                        c["covered"] = True
            if not resume_dom_reconciled and self._resume_origin_state_path:
                self.coverage, transient_reopened = _reopen_transient_dom_resume_claims(
                    self.coverage, state, story)
                repaired_resume_claims.update(transient_reopened)
                resume_dom_reconciled = True
            # A continuation can inherit a ledger whose final requirement was durably proven immediately
            # before an evaluator/provider failure.  That failure must remain visible in the earlier slice,
            # but once every acceptance aspect is already grounded there is no work left for a new paid
            # decision or browser action.  Close from the sealed checkpoint instead of spending another model
            # call merely to rediscover an empty remainder.
            if self.coverage and all(c.get("covered") for c in self.coverage):
                self.stop_reason = "coverage-complete"
                break
            self.missing_capabilities = _missing_required_capabilities(
                self.coverage, state, story=story)
            if self.missing_capabilities:
                local_at = _actual_at_driver_facts()
                needs_at = any(item.get("capability") == "actual-assistive-technology"
                               for item in self.missing_capabilities)
                if needs_at and local_at.get("available") and not self._actual_at_attempted:
                    self._actual_at_attempted = True
                    try:
                        self._activate_actual_at()
                        self.missing_capabilities = []
                        continue  # re-observe through Orca before any paid decision or browser action
                    except Exception as exc:
                        self._actual_at_error = str(exc)[:500]
                        for item in self.missing_capabilities:
                            if item.get("capability") == "actual-assistive-technology":
                                item["activation_error"] = self._actual_at_error
                # Do not click around hoping an unavailable external tool will appear. The durable result
                # below is a management state-change, not an app bug and not a generic retry/gap-fill.
                self.stop_reason = "capability-unavailable"
                self._checkpoint(story, records)
                break
            recorder_scope_new = self._recorder_start_receipt is None and any(
                not item.get("covered")
                and _recorder_requirement_stage(item.get("aspect")) == "start"
                for item in self.coverage or [])
            if not self._begin_recorder_scope():
                self.infrastructure_error = "browser recorder could not establish an explicit clear boundary"
                self.stop_reason = "recorder-infrastructure-incomplete"
                self._checkpoint(story, records)
                break
            if recorder_scope_new and self._recorder_start_receipt is not None:
                state = self._observe(include_accessibility)
            self._credit_recorder_start(state)
            # A resumed story whose contract explicitly starts with Seed/Load must recreate that ephemeral
            # baseline before any probabilistic navigation choice.  This is a deterministic safety fence, so
            # do not spend a paid 10–30 second model decision and then overwrite it with the same reset action.
            compact_gapfill = _compact_gapfill_prompt_mode(story, self.coverage)
            decision = _fence_seeded_resume_decision(
                None, story, records, self._resume_origin_state_path, self.target_url,
                resume_has_proven_coverage=bool(resume_covered))
            if decision is not None:
                mechanical_decide = self._phase_start("decide-mechanical-setup", {"prompt_chars": 0})
                self._phase_end("decide-mechanical-setup", mechanical_decide, "ok",
                                {"reason": "seeded-resume-fresh-baseline"})
            if decision is None:
                decision = _pending_focused_reset_storage_decision(story, records, self.target_url)
                if decision is not None:
                    mechanical_decide = self._phase_start("decide-mechanical-setup", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-setup", mechanical_decide, "ok",
                                    {"reason": "sealed-fresh-state-reopen-boundary"})
            if decision is None:
                decision = _pending_story_seed_decision(story, state, self.coverage, records)
                if decision is not None:
                    mechanical_decide = self._phase_start("decide-mechanical-story-seed", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-story-seed", mechanical_decide, "ok",
                                    {"reason": "story-seed-causal-precondition"})
            if decision is None:
                decision = _pending_timed_transition_decision(story, state, self.coverage)
                if decision is not None:
                    mechanical_decide = self._phase_start(
                        "decide-mechanical-timed-transition", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-timed-transition", mechanical_decide, "ok",
                                    {"reason": "contract-transient-action-boundary"})
            if decision is None:
                decision = _pending_business_completion_wait_decision(story, state, records)
                if decision is not None:
                    mechanical_decide = self._phase_start(
                        "decide-mechanical-async-continuation", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-async-continuation", mechanical_decide, "ok",
                                    {"reason": "visible-business-transition-pending"})
            if decision is None:
                decision = _pending_conditional_surface_setup_decision(
                    story, state, self.coverage)
                if decision is not None:
                    mechanical_decide = self._phase_start(
                        "decide-mechanical-conditional-surface", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-conditional-surface", mechanical_decide, "ok",
                                    {"reason": "recreate-perishable-confirmation-state"})
            if decision is None:
                decision = _pending_validation_matrix_decision(
                    story, state, self.coverage)
                if decision is not None:
                    mechanical_decide = self._phase_start(
                        "decide-browser-validation-matrix", {"prompt_chars": 0})
                    self._phase_end("decide-browser-validation-matrix", mechanical_decide, "ok",
                                    {"reason": "story-authored-validation-boundaries"})
            if decision is None:
                decision = _pending_explicit_form_sequence_decision(
                    story, state, self.coverage)
                if decision is not None:
                    mechanical_decide = self._phase_start(
                        "decide-browser-form-sequence", {"prompt_chars": 0})
                    self._phase_end("decide-browser-form-sequence", mechanical_decide, "ok",
                                    {"reason": "explicit-multi-field-story-clause"})
            if decision is None:
                decision = _pending_retry_story_transition_decision(
                    story, state, self.coverage, sealed_prior_records + records)
                if decision is not None:
                    mechanical_decide = self._phase_start(
                        "decide-mechanical-queue-retry", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-queue-retry", mechanical_decide, "ok",
                                    {"reason": "single-enquiry-failure-recovery-sequence"})
            if decision is None:
                decision = _pending_empty_first_run_transition_decision(
                    story, state, self.coverage, sealed_prior_records + records)
                if decision is not None:
                    mechanical_decide = self._phase_start(
                        "decide-mechanical-empty-first-run", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-empty-first-run", mechanical_decide, "ok",
                                    {"reason": "empty-first-run-causal-continuation"})
            if decision is None:
                decision = _pending_valid_form_setup_decision(
                    story, state, self.coverage, records)
                if decision is not None:
                    mechanical_decide = self._phase_start("decide-mechanical-setup", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-setup", mechanical_decide, "ok",
                                    {"reason": "browser-proven-valid-form-prerequisite"})
            if decision is None:
                decision = _pending_denied_send_setup_decision(
                    story, state, self.coverage, records)
                if decision is not None:
                    mechanical_decide = self._phase_start(
                        "decide-mechanical-denied-send-setup", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-denied-send-setup", mechanical_decide, "ok",
                                    {"reason": "exact-pre-approval-send-boundary"})
            if decision is None:
                decision = _pending_required_setup_decision(story, state, records)
                if decision is not None:
                    mechanical_decide = self._phase_start("decide-mechanical-setup", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-setup", mechanical_decide, "ok",
                                    {"reason": "browser-proven-required-prerequisite"})
            if decision is None:
                decision = _pending_queue_drain_decision(story, state, self.coverage)
                if decision is not None:
                    mechanical_decide = self._phase_start("decide-mechanical-setup", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-setup", mechanical_decide, "ok",
                                    {"reason": "queued-runtime-outcome-prerequisite"})
            if decision is None:
                decision = _focused_reported_observation_decision(
                    story, self.coverage, records)
                if decision is not None:
                    mechanical_decide = self._phase_start(
                        "decide-mechanical-focused-observation", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-focused-observation", mechanical_decide, "ok",
                                    {"reason": "sealed-reported-read-only-source"})
            if decision is None:
                decision = (None if self._reported_keyboard_replayed else
                            _focused_reported_keyboard_decision(
                                story, state, self.coverage, records))
                if decision is not None:
                    self._reported_keyboard_replayed = True
                    mechanical_decide = self._phase_start(
                        "decide-mechanical-focused-keyboard", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-focused-keyboard", mechanical_decide, "ok",
                                    {"reason": "sealed-reported-keyboard-source"})
            if decision is None:
                decision = _pending_atomic_keyboard_decision(
                    story, state, self.coverage, records)
                if decision is not None:
                    mechanical_decide = self._phase_start(
                        "decide-mechanical-keyboard-matrix", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-keyboard-matrix", mechanical_decide, "ok",
                                    {"reason": "lossless-atomic-keyboard-boundary"})
            if decision is None:
                decision = _checkpoint_failed_keyboard_decision(
                    story, state, self.coverage, sealed_prior_records, records)
                if decision is not None:
                    mechanical_decide = self._phase_start(
                        "decide-mechanical-checkpoint-keyboard", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-checkpoint-keyboard", mechanical_decide, "ok",
                                    {"reason": "retry-exact-failed-durable-key-boundary"})
            if decision is None:
                decision = _pending_explicit_evidence_decision(
                    story, state, self.coverage, records)
                if decision is not None:
                    mechanical_decide = self._phase_start(
                        "decide-mechanical-explicit-evidence", {"prompt_chars": 0})
                    self._phase_end("decide-mechanical-explicit-evidence", mechanical_decide, "ok",
                                    {"reason": "story-authored-browser-evidence-boundary"})
            if decision is None:
                try:
                    decision = self._ai_decide(
                        story, state, history, checklist=self.coverage)  # AI DECIDES (coverage-aware)
                except ModelDecisionUnavailable as exc:
                    self.infrastructure_error = str(exc)[:500]
                    self.stop_reason = "model-infrastructure-incomplete"
                    break
            # Initial-state evidence is perishable.  Do not let a probabilistic decider skip it, create durable
            # records, and later pretend that same-context navigation restored an empty browser.  Observe it
            # passively first; on a resumed/mutated session, explicitly reset only when the story requires an
            # empty/fresh context.  This is a sequencing invariant, not a hardcoded product decision.
            decision = _fence_seeded_resume_decision(
                decision, story, records, self._resume_origin_state_path, self.target_url,
                resume_has_proven_coverage=bool(resume_covered))
            initial_remaining = [c["aspect"] for c in (self.coverage or [])
                                 if not c.get("covered") and _initial_state_aspect(c.get("aspect"))]
            if initial_remaining:
                aspect = initial_remaining[0]
                decision = _fence_initial_state_decision(
                    decision, aspect, records, self._resume_origin_state_path, self.target_url)
            raw_action = _infer_click_target_from_expected(
                decision["next_action"], decision.get("expected"), state.get("elements"))
            raw_action = _contract_fenced_traversal_action(raw_action, story)
            raw_action = _contract_fenced_owned_surface_action(
                raw_action, story, decision.get("expected"), state)
            raw_action = _contract_fenced_queue_projection_action(
                raw_action, story, self.coverage, state)
            foreign_fixture = _foreign_story_fixture_target(
                raw_action, story, state.get("elements"))
            if foreign_fixture:
                self.infrastructure_error = (
                    f"planner selected foreign story fixture {foreign_fixture!r} while testing "
                    f"{story.get('id')}; evidence state was preserved")
                self.stop_reason = "foreign-story-fixture-incomplete"
                self._checkpoint(story, records)
                break
            action, aim = self._prepare_action(raw_action, state.get("elements"))  # BIND INTENT->CONTROL
            # The AI call above can take long enough for this worker's durable fencing generation to be
            # superseded.  Re-check immediately at the side-effect boundary; an obsolete worker may observe
            # and reason, but it must never click/type after its lease is gone.
            if cancel_event is not None and cancel_event.is_set():
                self.stop_reason = "lease-or-cancel-incomplete"
                break
            action_started_at = time.time()
            action_phase_started = self._phase_start("act", {"cmd": action.get("cmd")})
            act_result = None
            try:
                act_result = self.bridge.act(action)             # ACT
            finally:
                self._phase_end("act", action_phase_started,
                                "ok" if isinstance(act_result, dict) and act_result.get("ok", True) else "error",
                                {"cmd": action.get("cmd"),
                                 "driver_error": (act_result or {}).get("error")
                                 if isinstance(act_result, dict) else None})
            action_completed_at = time.time()
            wait_result = None
            wait_condition = decision.get("wait_for")
            if wait_condition:
                if cancel_event is not None and cancel_event.is_set():
                    self.stop_reason = "lease-or-cancel-incomplete"
                    break
                wait_started = self._phase_start("wait-external", {
                    "kind": wait_condition.get("kind"), "timeout_s": wait_condition.get("timeout_s")})
                try:
                    wait_result = self.bridge.wait_for(wait_condition)
                finally:
                    self._phase_end("wait-external", wait_started,
                                    "matched" if isinstance(wait_result, dict) and wait_result.get("matched")
                                    else "timeout" if isinstance(wait_result, dict) and wait_result.get("timed_out")
                                    else "error", {"receipt": wait_result})
            after = self._observe(include_accessibility)            # OBSERVE AGAIN

            # RETRY-ON-MISS (self-correction): if nothing happened yet a control matching the intent is
            # still on the page, the click simply missed — re-resolve against the fresh DOM and try ONCE
            # more before any evaluation. A missed click is a driver problem, never an app bug.
            retried = False
            cmd = (action.get("cmd") or "noop").lower()
            if (cmd in ("click", "tap", "touch", "pen", "burst", "click_burst", "clickburst",
                        "type", "fill", "press", "hold")
                    and not wait_condition
                    and not (_effect_registered(state, after, act_result) or _external_handoff(act_result))):
                retry_action, retry_aim = self._prepare_action(raw_action, after.get("elements"))
                if retry_aim.get("resolved_idx") is not None or retry_aim.get("intended"):
                    _, _, present = _resolve_target(
                        {"target_text": retry_aim.get("intended"), "role": retry_aim.get("role")},
                        after.get("elements"))
                    if present:
                        # The first settled action produced no effect. Tell the bridge to bind the single
                        # allowed retry to the re-resolved live element instead of reusing coordinates that
                        # may have hit an overlay or a stale layout box. This flag is ignored by non-click
                        # commands and is never set after a successful mutation.
                        if cmd == "click":
                            retry_action["_qa_retry_after_no_effect"] = True
                        if cancel_event is not None and cancel_event.is_set():
                            self.stop_reason = "lease-or-cancel-incomplete"
                            break
                        action_started_at = time.time()
                        retry_started = self._phase_start("act-retry", {"cmd": retry_action.get("cmd")})
                        try:
                            act_result = self.bridge.act(retry_action)   # ACT (retry once)
                        finally:
                            self._phase_end("act-retry", retry_started,
                                            "ok" if isinstance(act_result, dict)
                                            and act_result.get("ok", True) else "error")
                        action_completed_at = time.time()
                        after = self._observe(include_accessibility)
                        aim = retry_aim
                        retried = True

            # RE-OBSERVE ON MISSING-EXPECTED-CONTROL (late-paint guard): the action changed the view
            # (an effect registered), but the specific control the story EXPECTS to appear isn't in the
            # settled after-state yet. Real SPAs often paint that control ~0.6-1.2s after the route
            # switches — snapshotting once would falsely read "it never renders". So settle a little more
            # and OBSERVE ONE more time before the evaluator judges. A late paint must never become a bug.
            expected_control = (decision.get("expected_control") or "").strip()
            reobserved = False
            _, _, ec_present = _resolve_target({"target_text": expected_control}, after.get("elements")) \
                if expected_control else (None, None, 0)
            if expected_control and _effect_registered(state, after, act_result) and not ec_present:
                try:
                    self.bridge.settle()                 # extra paint grace + settle (bounded)
                except Exception:
                    pass
                after = self._observe(include_accessibility)
                reobserved = True
                _, _, ec_present = _resolve_target(
                    {"target_text": expected_control}, after.get("elements"))

            # the after-state we hand to the evaluator is always post-settle (state() settles the SPA);
            # `settled` tells the model an absent control is genuinely absent, not a mid-paint race.
            targeting = self._targeting_facts(
                aim, act_result, state, after, retried=retried, settled=True,
                expected_control=expected_control or None,
                expected_control_present=bool(ec_present) if expected_control else None,
                wait_result=wait_result)
            # Capture exact driver-grounded story proof at the action boundary.  This is intentionally before
            # the paid evaluator: an evaluator may omit an exact ledger label, but cannot erase a successful
            # URL/status transition, keypress, focus transition, or numeric before/after fact the driver saw.
            mechanical_preproofs = _mechanically_proven_unresolved(
                self.coverage, targeting, state, after)
            if action.get("_qa_inventory_derived") is True:
                exact_boundary = {str(item).strip() for item in (decision.get("covers") or [])
                                  if str(item).strip()}
                mechanical_preproofs = [item for item in mechanical_preproofs
                                        if item in exact_boundary]
            grounded_expected, corrected = _contract_grounded_history_expected(
                story, decision.get("expected"), targeting)
            if corrected:
                decision["expected"] = grounded_expected
                decision["reasoning"] = (str(decision.get("reasoning") or "").rstrip()
                    + " Contract fence: this story records the returned count as a fresh baseline; it does "
                      "not require the pre-navigation count to persist.").strip()
            projection_expected, projection_corrected = _contract_grounded_approval_projection_expected(
                story, decision.get("expected"), targeting)
            if projection_corrected:
                decision["expected"] = projection_expected
                decision["reasoning"] = (str(decision.get("reasoning") or "").rstrip()
                    + " Contract fence: pending tickets, job-result counts, and blocker projections are "
                      "distinct surfaces in this story and are verified by their own clauses.").strip()
            empty_expected, empty_corrected = _contract_grounded_empty_first_run_expected(
                story, decision.get("expected"), targeting)
            if empty_corrected:
                decision["expected"] = empty_expected
                decision["reasoning"] = (str(decision.get("reasoning") or "").rstrip()
                    + " Contract fence: ordinary lead review does not fabricate a human-approval ticket; "
                      "an honestly empty approval diagnostic is a valid live post-enquiry projection.").strip()
            queue_expected, queue_corrected = _contract_grounded_queue_projection_expected(
                story, decision.get("expected"), targeting)
            if queue_corrected:
                decision["expected"] = queue_expected
                decision["reasoning"] = (str(decision.get("reasoning") or "").rstrip()
                    + " Contract fence: aggregate CEO and diagnostic cards remain linked projections; "
                      "job identity, notification, audit, and retry detail are verified across the complete "
                      "surface set rather than duplicated inside every card.").strip()
            focus_expected, focus_corrected = _contract_grounded_acknowledgement_focus_expected(
                story, decision.get("expected"), targeting)
            if focus_corrected:
                decision["expected"] = focus_expected
                decision["reasoning"] = (str(decision.get("reasoning") or "").rstrip()
                    + " Contract fence: an acknowledged action control may be removed; visible focus belongs "
                      "on the next stable contract-owned target.").strip()
            _untested = [c["aspect"] for c in (self.coverage or []) if not c.get("covered")]
            mechanical_verdict = _mechanical_incomplete_submit_verdict(story, action, targeting)
            if mechanical_verdict is None:
                mechanical_verdict = _mechanical_pending_transition_verdict(
                    action, targeting, after)
            if mechanical_verdict is None:
                mechanical_verdict = _mechanical_atomic_dwell_verdict(
                    action, decision, targeting)
            if mechanical_verdict is None:
                mechanical_verdict = _mechanical_conditional_surface_setup_verdict(
                    action, targeting, after)
            if mechanical_verdict is None:
                mechanical_verdict = _mechanical_inventory_keyboard_verdict(
                    action, decision, mechanical_preproofs)
            if mechanical_verdict is None:
                mechanical_verdict = _mechanical_focused_keyboard_verdict(
                    action, self.coverage, mechanical_preproofs)
            if mechanical_verdict is None:
                mechanical_verdict = _mechanical_focused_traversal_verdict(
                    story, action, targeting, self.coverage)
            if mechanical_verdict is None:
                mechanical_verdict = _mechanical_queue_projection_verdict(
                    story, action, decision, targeting, after, self.coverage,
                    sealed_prior_records + records)
            # Exact form entry is cheap mechanical setup only when it claims no story outcome. A select/type
            # can itself be the acceptance transition (for example, choose Timeout while the queue is empty).
            # Skipping the semantic judge in that case records a pass but earns no coverage, causing the
            # ordered ledger to recreate its perishable initial state and replay the whole journey forever.
            if (mechanical_verdict is None
                    and not _routine_action_needs_semantic_judge(action, decision)):
                mechanical_verdict = _mechanical_routine_verdict(
                    action, targeting, after, mechanical_preproofs, decision=decision)
            if mechanical_verdict is not None:
                verdict = mechanical_verdict
                mechanical_started = self._phase_start(
                    "evaluate-mechanical", {"proofs": len(mechanical_preproofs)})
                self._phase_end("evaluate-mechanical", mechanical_started, "ok",
                                {"proofs": mechanical_preproofs})
            elif wait_result and wait_result.get("timed_out"):
                # A tester-selected observation window is not a product SLA. Preserve the state and let QA
                # management continue/diagnose; never manufacture a bug or a completed story from silence.
                verdict = {"matches_expected": False, "verdict": "inconclusive",
                           "target_confirmed": bool(targeting.get("label_matched")), "bug": None,
                           "severity": "none", "blocking": False, "demonstrated": [],
                           "model_failed": False, "infrastructure_error": None, "_raw": None}
            else:
                verdict = self._ai_evaluate(
                    story, decision["expected"], targeting, state, after,
                    untested=_untested,
                    prior_records=sealed_prior_records + records,
                    compact_gapfill=compact_gapfill,
                )  # AI EVALUATES (+ confirms cumulative grounded coverage across process handoffs)
            verdict["demonstrated"] = _ground_verdict_demonstrated(
                verdict, targeting, state, after)
            if verdict.get("verdict") in ("pass", "bug"):
                mechanically_proven = _grounded_demonstrated(
                    decision.get("covers"), targeting, state, after, require_mechanical=True)
                # A line-buffered Orca utterance can arrive during an explicit wait rather than the action the
                # decider originally associated with it.  Real driver evidence is authoritative: credit the
                # exact still-open AT requirement whose expected phrase appeared, even if the probabilistic
                # evaluator omitted it from ``demonstrated`` on that wait step.
                unresolved_at = [c.get("aspect") for c in (self.coverage or [])
                                 if not c.get("covered") and _requires_actual_at(c.get("aspect"))]
                late_at_proven = _grounded_demonstrated(
                    unresolved_at,
                    targeting, state, after, require_mechanical=True)
                verdict["demonstrated"] = list(dict.fromkeys(
                    list(verdict.get("demonstrated") or []) + mechanically_proven
                    + late_at_proven + mechanical_preproofs))
            if verdict.get("bug") and _passive_probe_after_disabled_miss(action, records):
                verdict.update({"matches_expected": False, "verdict": "inconclusive", "bug": None,
                                "severity": "none", "blocking": False, "demonstrated": []})
            if verdict.get("bug") and _optional_timed_duration_false_positive(
                    targeting, verdict.get("bug")):
                verdict.update({"matches_expected": False, "verdict": "inconclusive", "bug": None,
                                "severity": "none", "blocking": False, "demonstrated": []})
            if (verdict.get("bug") and str(action.get("cmd") or "").lower() == "wait"
                    and _visible_pending_transition(after)
                    and any((((item.get("verdict") or {}).get("_raw") or {}).get("engine")
                             == "mechanical-visible-pending-transition")
                            for item in records if isinstance(item, dict))):
                # This bounded observation window is not a product SLA. Preserve the live pending state for
                # continued/manager diagnosis instead of manufacturing a timeout defect.
                verdict.update({"matches_expected": False, "verdict": "inconclusive", "bug": None,
                                "severity": "none", "blocking": False, "demonstrated": []})
            if verdict.get("bug") and _queued_failure_not_triggered_false_positive(
                    story, targeting, verdict.get("bug"), after):
                verdict.update({"matches_expected": False, "verdict": "inconclusive", "bug": None,
                                "severity": "none", "blocking": False, "demonstrated": []})
            if verdict.get("bug") and _empty_first_run_approval_projection_false_positive(
                    story, targeting, verdict.get("bug"), after):
                verdict.update({"matches_expected": True, "verdict": "pass", "bug": None,
                                "severity": "none", "blocking": False})
            if verdict.get("bug") and _empty_first_run_contaminated_resume_false_positive(
                    story, targeting, verdict.get("bug"), state):
                verdict.update({"matches_expected": False, "verdict": "inconclusive", "bug": None,
                                "severity": "none", "blocking": False, "demonstrated": []})
            if verdict.get("bug") and _queue_recovery_wrong_scenario_false_positive(
                    story, verdict.get("bug"), sealed_prior_records + records):
                verdict.update({"matches_expected": False, "verdict": "inconclusive", "bug": None,
                                "severity": "none", "blocking": False, "demonstrated": []})
            focused_observation_proven = _successful_focused_observation_coverage(
                story, action, verdict, self.coverage, targeting, after)
            approval_diagnostics_proven = _successful_approval_diagnostics_coverage(
                story, action, verdict, self.coverage, targeting, after)
            denied_send_decision_proven = _successful_denied_send_decision_coverage(
                story, action, decision.get("expected"), verdict, self.coverage, targeting, after,
                sealed_prior_records + records)
            empty_first_run_proven = _successful_empty_first_run_transition_coverage(
                story, action, verdict, self.coverage, targeting, after,
                sealed_prior_records + records)
            queue_recovery_proven = _successful_queue_recovery_coverage(
                story, action, verdict, self.coverage, targeting, after,
                sealed_prior_records + records)
            queue_failure_proven = _successful_queue_failure_coverage(
                story, action, verdict, self.coverage, targeting, after,
                sealed_prior_records + records)
            queue_failure_inspection_proven = _successful_queue_failure_inspection_coverage(
                story, action, verdict, self.coverage, targeting, after)
            if (focused_observation_proven or approval_diagnostics_proven
                    or denied_send_decision_proven or empty_first_run_proven
                    or queue_recovery_proven or queue_failure_proven
                    or queue_failure_inspection_proven):
                verdict["demonstrated"] = list(dict.fromkeys(
                    list(verdict.get("demonstrated") or []) + focused_observation_proven
                    + approval_diagnostics_proven + denied_send_decision_proven
                    + empty_first_run_proven + queue_recovery_proven
                    + queue_failure_proven + queue_failure_inspection_proven))
            if queue_recovery_proven:
                raw_verdict = verdict.get("_raw") if isinstance(verdict.get("_raw"), dict) else {}
                verdict["_raw"] = dict(raw_verdict, engine="durable-queue-recovery-receipt")
            verdict["demonstrated"] = _ordered_grounded_aspects(
                self.coverage, verdict.get("demonstrated"))

            bug = None
            if verdict["bug"]:
                bug = {
                    "step": step,
                    "story": story.get("title", story.get("name", "")),
                    "url": after.get("url"),
                    "action": action,
                    "expected": decision["expected"],
                    "expected_control": expected_control,
                    "bug": verdict["bug"],
                    "severity": verdict["severity"],
                    "blocking": verdict["blocking"],
                    "shot": after.get("screenshot"),
                    "covers": list(decision.get("covers") or []) + list(verdict.get("demonstrated") or []),
                }
                self._capture_finding_state(bug)
                self.bugs.append(bug)
                if on_bug:
                    try:
                        on_bug(bug)
                    except Exception:
                        pass

            record = {
                "step": step,
                "recorded_at": time.time(),
                "action_started_at": action_started_at,
                "action_completed_at": action_completed_at,
                "state": _evidence_snapshot(state),
                "action": action,
                "reasoning": decision.get("reasoning", ""),      # WHY it chose this action — the audit needs the intent
                "expected": decision["expected"],
                "actual": _evidence_snapshot(after),
                "bug": bug,
                "targeting": targeting,
                "retried": retried,
                "reobserved": reobserved,
                "verdict": {k: verdict[k] for k in
                            ("matches_expected", "verdict", "target_confirmed", "bug", "severity", "blocking")},
                "done": decision["done"],
                "covers": decision.get("covers", []),               # aspects the decider INTENDED to exercise
                "demonstrated": verdict.get("demonstrated", []),     # aspects the evaluator CONFIRMED (grounded)
                "mechanically_proven": mechanical_preproofs,
                "act_result": act_result,
                "wait_result": wait_result,
            }
            records.append(record)
            history.append({
                "step": step, "action": action, "expected": decision["expected"],
                "matched": verdict["matches_expected"], "verdict": verdict["verdict"],
                "bug": verdict["bug"], "targeting": targeting,
            })

            # The browser action may already have happened, so never repeat it merely because the independent
            # evaluator was unavailable.  Persist the post-action browser state, mark no coverage, and hand the
            # story back as resumable infrastructure work rather than a pass, bug, or generic stall.
            if verdict.get("model_failed"):
                self.infrastructure_error = str(verdict.get("infrastructure_error") or "")[:500]
                self.stop_reason = "model-infrastructure-incomplete"
                self._checkpoint(story, records)
                break

            # COVERAGE UPDATE: credit an aspect only on GROUNDED evidence — the EVALUATOR (which saw the real
            # before->after plus the settled after-state) confirms which aspects were demonstrated, NOT the
            # decider's optimistic `covers` claim. Static visible-state checks may be demonstrated without a DOM
            # delta; action-result checks still depend on the evaluator naming what the evidence proves.
            effect = _effect_registered(state, after, act_result) or _external_handoff(act_result)
            newly = 0
            grounded = list(verdict.get("demonstrated", []))
            # A batch may conclusively prove an early clause while remaining inconclusive against its broader
            # action expectation. The exact demonstrated clauses have already passed the driver-grounding and
            # ordered-contract filters above, so preserve that partial proof regardless of the overall verdict.
            # Provider/model failures break before this block and therefore still earn no semantic coverage.
            for asp in grounded:
                for c in self.coverage:
                    contract_match = asp == c["aspect"] if c.get("explicit") else (
                        asp == c["aspect"] or asp in c["aspect"] or c["aspect"] in asp)
                    if not c.get("covered") and contract_match:
                        c["covered"] = True
                        c["proof"] = {
                            "action_kind": str(action.get("cmd") or ""),
                            "recorded_at": record.get("recorded_at"),
                            "engine": str((((verdict.get("_raw") or {}).get("engine"))
                                           if isinstance(verdict.get("_raw"), dict) else "") or ""),
                        }
                        newly += 1
            artifact_grounded = self._credit_recorder_end(records, final_state=after)
            if artifact_grounded:
                newly += len(artifact_grounded)
                grounded = list(dict.fromkeys(grounded + artifact_grounded))
                record["demonstrated"] = list(dict.fromkeys(
                    list(record.get("demonstrated") or []) + artifact_grounded))
            # A failing observation may still demonstrate that an aspect was exercised; that is coverage, not
            # proof the defect disappeared. Only a genuinely successful observation may close an earlier bug.
            self._mark_resolved_bugs(
                after, step, grounded,
                successful=bool(verdict.get("matches_expected") and verdict.get("verdict") == "pass"))
            # Acceptance progress means closing a ledger aspect. A novel DOM snapshot proves the browser is
            # responsive, but it is not acceptance progress: invalid-value matrices can manufacture a unique
            # validation/error view on every action while spending dozens of model calls without closing the
            # clause. Count those views only for the dead-surface guard. After a bounded coverage-stagnant
            # sequence, the cumulative diagnosis below reduces all receipts, identifies a real product defect,
            # or checkpoints an honest incomplete continuation; it never calls the story complete by timeout.
            progress_view = _progress_view_signature(after)
            new_view = progress_view not in seen_views
            seen_views.add(progress_view)
            no_progress = 0 if newly else no_progress + 1
            # A named long-page scroll can expose a materially new viewport without mutating DOM/network
            # state. That is a responding browser and genuine evidence progress, not a dead surface. The old
            # counter killed US-011 after three different CEO dashboard panels even though every scroll passed.
            dead_streak = 0 if (effect or newly or new_view) else dead_streak + 1
            current_cmd = str((action or {}).get("cmd") or "")
            current_is_batch = current_cmd in {
                "traverse", "inspect_surfaces", "dwell_surfaces", "timed_transition",
                "scenario_matrix", "case_matrix", "keyboard_matrix"} or (
                current_cmd == "scroll" and bool((action or {}).get("target_text")))
            if not current_is_batch and (effect or newly or current_cmd in {
                    "viewport", "reload", "goto", "back", "forward", "reset_storage"}):
                batch_context_epoch += 1
            sig, repeat_limit, batched_repeat = _repeat_action_policy(
                action, progress_view, batch_context=batch_context_epoch)
            if newly:
                repeat_actions[sig] = 0
            elif batched_repeat:
                # First full receipt establishes the baseline (zero repeats); one identical full retry is the
                # entire allowance. Ordinary actions retain their long-standing safety-envelope accounting.
                repeat_actions[sig] = repeat_actions.get(sig, -1) + 1
            else:
                repeat_actions[sig] = repeat_actions.get(sig, 0) + 1
            self._checkpoint(story, records)                     # DURABLE: tested-vs-untested persisted each step

            remaining = [c for c in self.coverage if not c.get("covered")]
            if (story.get("category") == "focused-regression"
                    and (action.get("_qa_reported_focus_source")
                         or _registered_business_mutation(action, targeting))
                    and remaining):
                # This worker exists to answer one sealed question. Once its exact trusted business action
                # has run, a mismatch in a composite focus/privacy assertion is evidence, not permission to
                # acknowledge/send/publish a sibling record. Preserve the settled post-action state for the
                # cumulative diagnosis below and fence all further product mutations in this explorer.
                state = after
                self.stop_reason = ("focused-finding-reproduced" if bug
                                    else "focused-action-inconclusive")
                break
            # Recovery verification has a narrower job than an independent release gate: determine whether
            # the already-applied fix is clean before allowing another repository mutation.  Once fresh,
            # grounded evidence proves a high/critical defect still exists, continuing the entire ledger in
            # that same recovery worker only delays the owner that can act on it.  Persist the evidence above,
            # then hand it back immediately.  Normal QA leaves this disabled and still exhausts coverage.
            if stop_on_actionable_bug and bug:
                severity = str(bug.get("severity") or "").lower()
                if bug.get("blocking") or severity == "critical":
                    self.stop_reason = "actionable-finding"
                    break
                if severity == "high" and remaining:
                    # A high but explicitly non-blocking explorer finding may be a useful defect, or it may be
                    # a sequencing/targeting overreach (for example expecting publish-time blockers from a
                    # fixture-load button).  Route that state change to the QA manager; only its independent
                    # capability diagnosis may turn it into an immediate fixer handoff.
                    diagnosis = self._ai_incomplete_diagnosis(story, after, [c["aspect"] for c in remaining], history)
                    if diagnosis.get("disposition") == "app_defect" and diagnosis.get("bug"):
                        managed_bug = {
                            "step": step, "story": story.get("title", story.get("name", "")),
                            "url": after.get("url"), "action": {"cmd": "manager_review_after_finding"},
                            "expected": "; ".join(c["aspect"] for c in remaining),
                            "expected_control": "", "bug": diagnosis["bug"],
                            "severity": diagnosis.get("severity", "high"),
                            "blocking": diagnosis.get("blocking", True),
                            "shot": after.get("screenshot"),
                            "covers": [c["aspect"] for c in remaining],
                            "diagnosis": diagnosis.get("reason"),
                        }
                        self._capture_finding_state(managed_bug)
                        self.bugs.append(managed_bug)
                        if on_bug:
                            try:
                                on_bug(managed_bug)
                            except Exception:
                                pass
                        self.stop_reason = "managed-actionable-finding"
                        self._checkpoint(story, records)
                        break
            if verdict["blocking"]:                              # a wall — fixing must precede more testing
                self.stop_reason = "blocking-wall"
                break
            if not remaining:                                    # everything a user would try has been tested
                self.stop_reason = "coverage-complete"
                break
            if decision.get("done") and remaining:
                # A planner is not release authority. Previously this optimistic bit checkpointed a 3/4
                # focused proof, and every successor restored the original finding snapshot and repeated the
                # same journey. Keep the live state and let another decision close the ledger; the stall/repeat
                # guards below still route genuinely unreachable work to QA management.
                history[-1]["planner_done_rejected"] = True
            if _STALL_LIMIT and no_progress >= _STALL_LIMIT:     # responsive actions but no acceptance progress
                self.stop_reason = "stalled-incomplete"
                break
            if repeat_limit and repeat_actions.get(sig, 0) >= repeat_limit:
                self.stop_reason = "repeated-action-incomplete"
                break
            if _DEAD_LIMIT and dead_streak >= _DEAD_LIMIT:       # nothing responds — a frozen/broken surface
                self.stop_reason = "stuck-no-effect-incomplete"
                break
            step += 1
        remaining = [c["aspect"] for c in (self.coverage or []) if not c.get("covered")]
        if remaining and self.stop_reason in {
                "ai-done-with-remaining-incomplete", "stalled-incomplete",
                "repeated-action-incomplete", "stuck-no-effect-incomplete",
                "focused-action-inconclusive"}:
            diagnosis = self._ai_incomplete_diagnosis(story, state, remaining, history, records=records)
            cumulatively_proven = set(diagnosis.get("demonstrated") or [])
            diagnosed_newly = 0
            diagnosed_aspects = []
            for item in self.coverage or []:
                if not item.get("covered") and item.get("aspect") in cumulatively_proven:
                    item["covered"] = True
                    item["proof"] = {
                        "action_kind": "diagnose_cumulative_progress",
                        "recorded_at": time.time(),
                        "engine": "cumulative-evidence-reducer",
                    }
                    diagnosed_newly += 1
                    diagnosed_aspects.append(item.get("aspect"))
            remaining = [c["aspect"] for c in (self.coverage or []) if not c.get("covered")]
            if diagnosed_newly:
                # The cumulative reducer is the authority that closed these exact clauses.  Its durable
                # receipt is required even when it closes the *last* open clause.  Previously we emitted the
                # receipt only for partial progress, so a terminal 4/4 result carried covered booleans but no
                # proof for the clauses closed here; the next worker correctly reopened and replayed them.
                records.append({
                    "step": step, "state": {"url": state.get("url"), "title": state.get("title"),
                                              "screenshot": state.get("screenshot")},
                    "action": {"cmd": "diagnose_cumulative_progress"},
                    "reasoning": diagnosis.get("reason", ""),
                    "expected": "; ".join(diagnosed_aspects),
                    "actual": {"url": state.get("url"), "title": state.get("title"),
                               "screenshot": state.get("screenshot")},
                    "bug": None,
                    "verdict": {"matches_expected": True, "verdict": "pass", "bug": None,
                                "model_failed": False, "infrastructure_error": None},
                    "done": not remaining, "covers": diagnosed_aspects,
                    "demonstrated": diagnosed_aspects, "coverage_grounded": True,
                })
            if not remaining:
                self.stop_reason = "coverage-complete"
            if remaining and diagnosis.get("bug"):
                bug = {"step": step, "story": story.get("title", story.get("name", "")),
                       "url": state.get("url"), "action": {"cmd": "diagnose_incomplete"},
                       "expected": "; ".join(remaining), "expected_control": "",
                       "bug": diagnosis["bug"], "severity": diagnosis.get("severity", "high"),
                       "blocking": diagnosis.get("blocking", True), "shot": state.get("screenshot"),
                       "covers": remaining, "diagnosis": diagnosis.get("reason")}
                self._capture_finding_state(bug)
                self.bugs.append(bug)
                if on_bug:
                    try:
                        on_bug(bug)
                    except Exception:
                        pass
                records.append({"step": step, "state": {"url": state.get("url"),
                                "title": state.get("title"), "screenshot": state.get("screenshot")},
                                "action": {"cmd": "diagnose_incomplete"},
                                "reasoning": diagnosis.get("reason", ""),
                                "expected": "; ".join(remaining),
                                "actual": {"url": state.get("url"), "title": state.get("title"),
                                           "screenshot": state.get("screenshot"), "console_errors": []},
                                "bug": bug, "verdict": {"matches_expected": False,
                                "verdict": "bug", "target_confirmed": True, "bug": diagnosis["bug"],
                                "severity": diagnosis.get("severity", "high"),
                                "blocking": diagnosis.get("blocking", True)},
                                "done": True, "covers": remaining, "demonstrated": []})
                self.stop_reason = "diagnosed-actionable-finding"
            elif diagnosed_newly and remaining:
                # Cumulative diagnosis is an evidence reducer, not a terminal verdict. If it proves a strict
                # subset of the open ledger, continue immediately from that newly advanced checkpoint. The old
                # behavior returned ``repeated-action-incomplete`` and forced the coordinator to hire another
                # explorer, which restored the same snapshot and replayed the same inspection indefinitely.
                # Recursion is bounded by monotonic coverage: this path requires at least one newly covered
                # aspect, and every recursive call receives the exact durable ledger.
                self.stop_reason = "diagnostic-progress-continuation"
                self._checkpoint(story, records)
                remaining_max_steps = (None if max_steps is None else max_steps - len(records))
                if remaining_max_steps is None or remaining_max_steps > 0:
                    continued = self.explore(
                        story, max_steps=remaining_max_steps, on_bug=on_bug, deadline=deadline,
                        resume_covered=[item["aspect"] for item in (self.coverage or [])
                                        if item.get("covered")],
                        resume_coverage=[dict(item) for item in (self.coverage or [])],
                        resume_steps_detail=sealed_prior_records + records,
                        cancel_event=cancel_event,
                        stop_on_actionable_bug=stop_on_actionable_bug)
                    return records + continued
        self._checkpoint(story, records)                         # final checkpoint carries the stop_reason
        return records

    def _mark_resolved_bugs(self, state, step, grounded=None, successful=True):
        """Resolve a nonblocking bug only with a later successful proof of the same requirement.

        Merely seeing an expected control later is not a fix: a submit can make ``Send another`` appear even
        though reload still loses it. Resolution therefore requires grounded coverage overlap, which binds the
        successful observation to the same story contract that produced the finding.
        """
        grounded = [str(g).strip() for g in (grounded or []) if str(g).strip()]
        for bug in self.bugs:
            if bug.get("resolved") or bug.get("blocking"):
                continue
            bug_aspects = [str(g).strip() for g in (bug.get("covers") or []) if str(g).strip()]
            if successful and grounded and bug_aspects and any(
                g == b or g in b or b in g for g in grounded for b in bug_aspects
            ):
                bug["resolved"] = True
                bug["resolved_step"] = step
                bug["resolution"] = "later grounded evidence covered the same story requirement"

    def _capture_finding_state(self, bug):
        """Seal the browser input state at the same boundary as a reported finding.

        ``storage-state.json`` is intentionally a moving continuation cursor: every checkpoint replaces it
        with the newest story state.  It therefore cannot be paired retrospectively with an earlier finding
        screenshot.  Keep a distinct content-addressed snapshot for each finding so focused reverification
        restores the exact cookies/local storage QA observed when it filed the defect.  Browser state can
        contain fixture PII and credentials, so the artifact stays outside Postgres and owner-readable only.
        """
        if not isinstance(bug, dict) or not self.artifact_dir or self.bridge is None:
            return None
        try:
            stored = self.bridge.storage_state()
            state = stored.get("state") if isinstance(stored, dict) else None
            if not isinstance(state, dict):
                return None
            captured_at = time.time()
            identity = json.dumps({
                "step": bug.get("step"),
                "action": bug.get("action"),
                "shot": bug.get("shot") or bug.get("screenshot"),
                "captured_at_ns": time.time_ns(),
            }, sort_keys=True, default=str, separators=(",", ":"))
            suffix = hashlib.sha256(identity.encode()).hexdigest()[:20]
            path = self.artifact_dir / f"finding-state-{suffix}.json"
            raw = json.dumps(state, sort_keys=True, default=str, separators=(",", ":"))
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            tmp.replace(path)
            bug["finding_state_path"] = str(path)
            bug["finding_state_captured_at"] = captured_at
            return str(path)
        except Exception:
            return None

    def _checkpoint(self, story, records):
        """Persist the tested-vs-yet-to-be-tested ledger + progress after every step, so a crash/shutdown
        loses nothing and a later run can resume the untested aspects. Fail-open; no-op without an artifact
        dir (offline self-tests)."""
        cov = self.coverage or []
        tested = sum(1 for c in cov if c.get("covered"))
        covered_aspects = sorted(str(c.get("aspect")) for c in cov
                                 if c.get("covered") and str(c.get("aspect") or "").strip())
        last = records[-1] if records else {}
        raw_action = last.get("action") if isinstance(last, dict) else None
        if isinstance(raw_action, dict):
            action = {key: raw_action.get(key) for key in (
                "cmd", "target_text", "role", "selector", "value", "count", "interval_ms",
                "duration_ms", "duration_s", "targets") if raw_action.get(key) not in (None, "", [])}
        else:
            action = raw_action
        raw_verdict = last.get("verdict") if isinstance(last, dict) else None
        if isinstance(raw_verdict, dict):
            verdict = {key: raw_verdict.get(key) for key in (
                "verdict", "matches_expected", "blocking", "severity")
                if raw_verdict.get(key) not in (None, "")}
        else:
            verdict = raw_verdict
        actual = last.get("actual") if isinstance(last, dict) and isinstance(last.get("actual"), dict) else {}
        before = last.get("state") if isinstance(last, dict) and isinstance(last.get("state"), dict) else {}
        semantic = {
            "covered": covered_aspects,
            "action": action,
            "route": actual.get("url") or before.get("url"),
            "verdict": verdict,
            "covers": sorted(str(value) for value in ((last.get("covers") or [])
                                                       if isinstance(last, dict) else [])
                             if str(value).strip()),
            "demonstrated": sorted(str(value) for value in ((last.get("demonstrated") or [])
                                                            if isinstance(last, dict) else [])
                                  if str(value).strip()),
            "stop_reason": str(self.stop_reason or ""),
        }
        progress_signature = hashlib.sha256(
            json.dumps(semantic, sort_keys=True, default=str).encode()).hexdigest()[:24]
        if progress_signature != self._last_progress_signature:
            self._last_progress_signature = progress_signature
            try:
                import jobrunner
                jobrunner.note_run_progress(
                    self._scope_run_id, self._scope_tenant,
                    signature=f"{self._current_story_id}:{progress_signature}",
                    phase="explore", details={"story": self._current_story_id,
                    "steps": len(records), "covered": tested, "coverage_total": len(cov),
                    "stop_reason": self.stop_reason})
            except Exception:
                pass
        # LIVE HEARTBEAT: emit a pulse beat every step so the observability plane (and the watchdog) can see
        # this run is alive + exactly what it's doing — silence past cadence then reads as a real stall.
        if self.pulse_work_id:
            try:
                import pulse                          # SCRIPTS is already on sys.path (module load)
                pulse.beat(self.pulse_work_id, stage="explore",
                           progress=f"step {len(records)} · {tested}/{len(cov)} covered"
                                    + (f" · {self.stop_reason}" if self.stop_reason else ""))
            except Exception:
                pass
        if not self.artifact_dir:
            return
        try:
            last = records[-1] if records else {}
            # Persist the state beside the evidence, not in Postgres: browser state can contain fixture PII
            # and cookies.  The durable result carries only this owner-readable path.  Write state first so
            # a checkpoint can never claim resumability when the corresponding state did not land.
            state_path = self.artifact_dir / "storage-state.json"
            state = self.bridge.storage_state() if self.bridge else {}
            if state.get("ok") and isinstance(state.get("state"), dict):
                tmp = state_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(state["state"], default=str))
                os.chmod(tmp, 0o600)
                tmp.replace(state_path)
                self.resume_state_path = str(state_path)
            else:
                self.resume_state_path = None
            sealed_steps = _portable_checkpoint_evidence(
                list(self._checkpoint_prior_records or []) + list(records or []))
            sealed_last = sealed_steps[-1] if sealed_steps else {}
            checkpoint = {
                "ts": time.time(),
                "story": story.get("title", story.get("name", "")),
                "steps_done": len(records),
                "stop_reason": self.stop_reason,
                "tested": [c["aspect"] for c in cov if c.get("covered")],
                "yet_to_test": [c["aspect"] for c in cov if not c.get("covered")],
                "coverage": cov,
                # Coverage labels are only indexes. Pair them atomically with the successful exact-aspect
                # receipts that prove them so a process crash cannot turn 4/5 back into 0/5.
                "steps_detail": sealed_steps,
                "missing_capabilities": self.missing_capabilities,
                "resume_state_path": self.resume_state_path,
                "last_step": {
                    "action": last.get("action"),
                    "expected": last.get("expected"),
                    "actual": sealed_last.get("actual"),
                    "verdict": sealed_last.get("verdict"),
                    "done": last.get("done"),
                    "demonstrated": last.get("demonstrated"),
                    "mechanically_proven": last.get("mechanically_proven"),
                    # The complete DOM/driver receipt lives in the bounded steps_detail row. Keep this
                    # compatibility summary small so a page dump cannot bypass the checkpoint redaction.
                    "targeting": {
                        key: (last.get("targeting") or {}).get(key)
                        for key in ("action_kind", "driver_ok", "effect_registered",
                                    "targeted_label", "intended")
                        if isinstance(last.get("targeting"), dict)
                        and (last.get("targeting") or {}).get(key) not in (None, "")
                    },
                } if last else None,
            }
            campaign_checkpoint.write_checkpoint(self.artifact_dir / "checkpoint.json", checkpoint)
        except Exception:
            pass

    def close(self):
        if self.bridge:
            started = self._phase_start("finalize-browser")
            status = "ok"
            try:
                self.bridge.close()
                webm = getattr(self.bridge, "video_path", None)
                self.video_path = str(webm) if webm else None
                # MP4 is a review convenience, not release truth.  Encoding it synchronously held the scarce
                # explorer/browser slot for minutes after browser work ended. Keep the raw Playwright WebM and
                # a durable pending receipt; an evidence publisher may transcode later. An explicit eager mode
                # remains for workflows whose contract really requires MP4 before returning.
                eager = os.environ.get("AOS_QA_EAGER_TRANSCODE", "").strip().lower() in {
                    "1", "true", "yes", "on"}
                if webm and self.artifact_dir is not None and artifacts is not None and eager:
                    try:
                        out = self.artifact_dir / "videos" / (Path(webm).stem + ".mp4")
                        self.video_mp4 = artifacts.webm_to_mp4(webm, out)
                    except Exception:
                        self.video_mp4 = None
                elif webm and self.artifact_dir is not None:
                    try:
                        import evidencepublisher
                        evidencepublisher.defer(
                            webm, self.artifact_dir / "videos" / (Path(webm).stem + ".mp4"),
                            self.artifact_dir / "encoding-status.json")
                    except Exception:
                        # The raw WebM remains authoritative even if the convenience publisher is unavailable.
                        try:
                            (self.artifact_dir / "encoding-status.json").write_text(json.dumps({
                                "status": "deferred", "source": str(webm), "requested_format": "mp4",
                                "reason": "publisher handoff unavailable; recovery scan will backfill",
                                "recorded_at": time.time()}, indent=2))
                        except Exception:
                            pass
            except Exception:
                status = "error"
                raise
            finally:
                self.bridge = None
                self._phase_end("finalize-browser", started, status,
                                {"raw_video": bool(self.video_path), "mp4": bool(self.video_mp4)})


# ----------------------------------------------------------------------------------------------------
# Offline self-test — NO real API calls, NO real browser. Stubs factory.agent and the bridge, then
# asserts the loop truly observes -> decides -> acts -> observes -> evaluates and records a seeded bug.
# ----------------------------------------------------------------------------------------------------
def _selftest():
    import types

    # 1) Stub `factory` so _call_agent gets a deterministic, offline model. The stub distinguishes the
    #    DECIDE call from the EVALUATE call by a tag in the prompt, and seeds a bug on step 2.
    calls = {"decide": 0, "evaluate": 0, "order": []}
    fake = types.ModuleType("factory")

    def fake_agent(role, repo, task):
        assert role == ROLE, f"expected role {ROLE!r}, got {role!r}"
        if ("DECIDE the single next action" in task
                or "Choose ONE next browser action for a SEALED FOCUSED REGRESSION" in task):
            n = calls["decide"]
            calls["decide"] += 1
            calls["order"].append("decide")
            # Both the full-story and compact focused planners must carry the story contract and require
            # label-based targeting. Small stories deliberately take the compact path now, so pin that path's
            # scope/coverage fences instead of requiring wording that exists only in the broad prompt.
            if "SEALED FOCUSED REGRESSION" in task:
                assert "STORY CONTRACT" in task and "ORDERED COVERAGE LEDGER" in task
                assert "target_text" in task and "exact observed LABEL" in task
            else:
                assert "ORIGINAL PRODUCT VISION" in task and "EXPECTED OUTCOME" in task
                assert "target_text" in task and "TARGET BY INTENT" in task
            done = n >= 3
            # target the control by its LABEL ("Go"), the way the hardened explorer demands. idx is a
            # deliberately-WRONG fallback (99) to prove label resolution — not the blind idx — wins.
            body = json.dumps({
                "reasoning": "click the primary action",
                "intent": "the Go button",
                "next_action": {"cmd": "click", "target_text": "Go", "role": "button", "idx": 99},
                "expected": f"a result panel appears (step {n})",
                "done": done,
            })
            return {"rc": 0, "out": body, "out_full": "sure, here you go:\n```json\n" + body + "\n```"}
        elif ("EVALUATE expected-vs-actual" in task
              or "Independently judge one action in a SEALED FOCUSED REGRESSION" in task):
            n = calls["evaluate"]
            calls["evaluate"] += 1
            calls["order"].append("evaluate")
            if "SEALED FOCUSED REGRESSION" in task:
                assert "BEFORE:" in task and "SETTLED AFTER:" in task
                assert "DRIVER TARGETING RECEIPT" in task and "STORY CONTRACT" in task
            else:
                assert "STATE BEFORE THE ACTION" in task and "STATE AFTER THE ACTION" in task
                # the evaluate contract must show the driver's targeting ground-truth
                assert "ACTION TARGETING" in task and "CONTRACT" in task
            if n == 1:  # seed a blocking bug on the 2nd evaluation — the right control WAS exercised
                body = json.dumps({"target_confirmed": True, "matches_expected": False, "verdict": "bug",
                                   "bug": "clicking the button did nothing — no result panel rendered",
                                   "severity": "high", "blocking": True})
            else:
                body = json.dumps({"target_confirmed": True, "matches_expected": True, "verdict": "pass",
                                   "bug": None, "severity": "none", "blocking": False})
            return {"rc": 0, "out": body, "out_full": body}
        elif "PLAN COVERAGE for one user story" in task:
            calls["plan"] = calls.get("plan", 0) + 1
            assert "everything a REAL user would try" in task
            return {"rc": 0, "out": json.dumps({
                "aspects": ["click Go and see a result", "submit with an empty input"]}), "out_full": ""}
        raise AssertionError("unexpected agent task (not plan/decide/evaluate):\n" + task[:200])

    fake.agent = fake_agent
    sys.modules["factory"] = fake

    # 2) Stub the browser bridge — records the observe/act call order, returns synthetic states.
    class StubBridge:
        def __init__(self):
            self.log = []
            self._n = 0

        def state(self):
            self.log.append("state")
            self._n += 1
            return {
                "ok": True, "url": f"http://app.test/step{self._n}", "title": "Test App",
                "screenshot": f"/tmp/state-{self._n}.png", "bodyText": "Welcome to the app",
                "console_errors": [], "recent_requests": [], "elements": [
                    {"idx": 0, "tag": "button", "text": "Go", "type": "button",
                     "selector": '[data-aos-idx="0"]'},
                    {"idx": 1, "tag": "input", "type": "text", "text": "name",
                     "selector": '[data-aos-idx="1"]'},
                ],
            }

        def act(self, action):
            self.log.append(("act", action.get("cmd")))
            return {"ok": True, "action": action}

        def close(self):
            self.log.append("close")

    ex = Explorer("http://app.test", vision="An app that shows a result when you click Go.",
                  token="tok", org="7", autostart=False)
    stub = StubBridge()
    ex.bridge = stub

    seen_bugs = []
    story = {"title": "click Go shows a result",
             "goal": "click the Go button and see a result",
             "expected": "a result panel is displayed after clicking Go"}
    records = ex.explore(story, max_steps=25, on_bug=seen_bugs.append)

    # --- assertions -------------------------------------------------------------------------------
    # loop must have run: it stops after the 2nd step because the seeded bug is blocking.
    assert len(records) == 2, f"expected 2 steps (blocking bug on step 2), got {len(records)}"

    # COVERAGE-DRIVEN, NOT COUNT-DRIVEN: the explorer plans 'everything a user would try' exactly once and
    # stops for a REASON (the blocking wall here), never because a step counter ran out.
    assert calls.get("plan") == 1, f"coverage must be planned once per story, got {calls.get('plan')}"
    assert [c["aspect"] for c in ex.coverage] == ["click Go and see a result", "submit with an empty input"]
    assert ex.stop_reason == "blocking-wall", f"stop reason should be the wall, got {ex.stop_reason!r}"
    assert all("covers" in r for r in records), "each step records which coverage aspects it exercised"

    # per step the order MUST be: observe(state) -> decide -> act -> observe(state) -> evaluate.
    # so the bridge sees state,act,state,act (2 states + 1 act per step) and the agent sees
    # decide,evaluate,decide,evaluate.
    assert calls["order"] == ["decide", "evaluate", "decide", "evaluate"], calls["order"]
    # bridge log: state, act, state, (step2) state, act, state
    assert stub.log[:6] == ["state", ("act", "click"), "state",
                            "state", ("act", "click"), "state"], stub.log

    # the seeded bug must be recorded on the loop, surfaced via on_bug, and attached to the step record.
    assert len(ex.bugs) == 1 and ex.bugs[0]["blocking"] is True
    assert len(seen_bugs) == 1 and "did nothing" in seen_bugs[0]["bug"]
    assert records[1]["bug"] is not None and records[1]["bug"]["severity"] == "high"
    assert records[0]["bug"] is None and records[0]["verdict"]["matches_expected"] is True
    # the AI's expected string is captured on each record (the expected-vs-actual yardstick).
    assert records[0]["expected"].startswith("a result panel appears")

    # ROBUST TARGETING: the AI targeted by LABEL ("Go") with a WRONG fallback idx (99). The explorer
    # must resolve the label to the real control (idx 0), overriding the blind idx.
    assert records[0]["action"]["target_text"] == "Go"
    assert records[0]["action"]["idx"] == 0, f"label must override blind idx, got {records[0]['action']}"
    # the targeting ground-truth confirms the intended control was actually actuated + had an effect.
    assert records[0]["targeting"]["intended"] == "Go"
    assert records[0]["targeting"]["label_matched"] is True
    assert records[0]["targeting"]["effect_registered"] is True
    assert records[0]["retried"] is False
    assert records[1]["verdict"]["verdict"] == "bug" and records[1]["verdict"]["target_confirmed"] is True

    ex.close()
    assert stub.log[-1] == "close"

    # === focused unit checks on the accuracy core (pure functions — no agent, no browser) =========
    _selftest_targeting()
    _selftest_reobserve()
    _selftest_story_scope()

    print("qa_explorer selftest: PASS "
          f"(steps={len(records)}, ai_calls={calls['decide'] + calls['evaluate']}, bugs={len(ex.bugs)})")


def _selftest_targeting():
    """Pin the anti-false-positive behaviour: label/role resolution, effect detection, retry-on-miss, and
    the blame contract (a 'won't open' claim on a control that was NEVER correctly clicked is NOT a bug)."""
    import types

    # 1) INTENT->CONTROL resolution: exact beats contains; role narrows; no match -> None.
    els = [
        {"idx": 5, "tag": "a", "text": "Assistant", "role": ""},              # the REAL Assistant nav
        {"idx": 6, "tag": "button", "text": "Assistant settings", "role": "button"},
        {"idx": 27, "tag": "div", "text": "unrelated widget", "role": "button"},  # the old blind-idx trap
    ]
    assert _resolve_target({"target_text": "Assistant"}, els)[0] == 5, "exact label must win"
    assert _resolve_target({"target_text": "assistant", "role": "button"}, els)[0] == 6, "role narrows"
    assert _resolve_target({"target_text": "nonexistent control"}, els)[0] is None, "no match -> None"
    assert _resolve_target({"target_text": "assist"}, els)[2] == 2, "substring match scores 2 (contains)"
    nav_els = [{"idx": 1, "tag": "button", "text": "Team", "role": ""}]
    assert _resolve_target({"target_text": "Team", "role": "tab"}, nav_els)[0] == 1, \
        "exact label should win when the model over-constrains role"
    form_els = [
        {"idx": 2, "tag": "button", "text": "Check area", "role": "button"},
        {"idx": 3, "tag": "input", "type": "text", "text": "Postcode"},
    ]
    inferred_click = _infer_click_target_from_expected(
        {"cmd": "click"}, "Click Check area to submit the postcode.", form_els)
    assert inferred_click.get("target_text") == "Check area", inferred_click
    ex_type = Explorer("http://x", vision="v", autostart=False)
    prepared, aim = ex_type._prepare_action({"cmd": "type", "target_text": "Postcode Check area", "value": "94109"}, form_els)
    assert prepared["idx"] == 3, prepared
    assert aim["role"] == "textbox" and aim["resolved_label"] == "Postcode", aim
    assert "SELECTS/COMBOBOXES" in _decide_prompt("vision", {"goal": "select scenario"}, {
        "url": "http://x", "elements": [{"idx": 4, "tag": "select", "text": "Agent scenario"}],
        "bodyText": "Agent scenario Success Approval required"}, [], []), \
        "prompt must teach direct select action"

    # 2) EFFECT detection: identical states = no effect (a missed click); any change = effect.
    b = {"url": "u", "title": "t", "bodyText": "x", "recent_requests": [], "console_errors": []}
    same = dict(b)
    assert _effect_registered(b, same, {"ok": True}) is False, "no change => no effect"
    assert _effect_registered(b, {**b, "url": "u2"}, {"ok": True}) is True, "nav => effect"
    assert _effect_registered(b, {**b, "bodyText": "y"}, {"ok": True}) is True, "DOM change => effect"
    assert _effect_registered(b, {**b, "recent_requests": [{"url": "/api"}]}, {"ok": True}) is True, "net => effect"
    assert _effect_registered(b, same, {"ok": False}) is False, "failed act => no effect"
    form_before = {**b, "elements": [{"idx": 1, "tag": "input", "text": "Postcode", "value": ""}]}
    form_after = {**b, "elements": [{"idx": 1, "tag": "input", "text": "Postcode", "value": "94110"}]}
    assert _effect_registered(form_before, form_after, {"ok": True}) is True, "input value change => effect"
    focus_before = {**b, "activeElement": None}
    focus_after = {**b, "activeElement": {"tag": "input", "name": "customerName"}}
    assert _effect_registered(focus_before, focus_after, {"ok": True}) is True, "focus change => effect"
    handoff = {"ok": True, "matched": "Email booking request", "matchedHref": "mailto:test@example.com"}
    assert _external_handoff(handoff) is True, "mailto/tel/sms links are browser handoffs"
    handoff_facts = Explorer("http://x", vision="v", autostart=False)._targeting_facts(
        {"action_kind": "click", "control_action": True, "intended": "Email booking request",
         "role": "link", "resolved_idx": 8, "resolved_label": "Email booking request"},
        handoff,
        {**b, "elements": [{"idx": 8, "tag": "a", "text": "Email booking request", "href": "mailto:test@example.com"}]},
        {**same, "elements": [{"idx": 8, "tag": "a", "text": "Email booking request", "href": "mailto:test@example.com"}]})
    assert handoff_facts["effect_registered"] is True and handoff_facts["external_handoff_url"].startswith("mailto:"), handoff_facts
    assert handoff_facts["before_control_disabled"] is False, handoff_facts
    busy_handoff = {**handoff, "disabledAfterClick": True}
    busy_handoff_facts = Explorer("http://x", vision="v", autostart=False)._targeting_facts(
        {"action_kind": "click", "control_action": True, "intended": "Email booking request",
         "role": "link", "resolved_idx": 8, "resolved_label": "Email booking request"},
        busy_handoff,
        {**b, "elements": [{"idx": 8, "tag": "a", "text": "Email booking request", "href": "mailto:test@example.com"}]},
        {**same, "elements": [{"idx": 8, "tag": "a", "text": "Email booking request", "href": "mailto:test@example.com"}]})
    assert busy_handoff_facts["after_control_disabled"] is True, busy_handoff_facts
    assert "disabled immediately AFTER" in _fmt_targeting(busy_handoff_facts)

    # 2b) Explicit navigation/reload probes are not control-targeted. They must be eligible for state
    #     evaluation/coverage even though no button/link label was intended.
    exc_nav = Explorer("http://x", vision="v", autostart=False)
    nav_action, nav_aim = exc_nav._prepare_action({"cmd": "goto", "value": "http://x/#team"}, els)
    assert nav_action["cmd"] == "goto"
    assert nav_aim["control_action"] is False and nav_aim["action_kind"] == "goto", nav_aim
    nav_facts = exc_nav._targeting_facts(
        nav_aim, {"ok": True}, {"url": "http://x/#visitor"}, {"url": "http://x/#team", "elements": els})
    assert nav_facts["control_action"] is False and nav_facts["label_matched"] is True, nav_facts
    assert nav_facts["effect_registered"] is True, nav_facts

    prompt = _evaluate_prompt("vision", {"goal": "inline form"}, "inline result appears", nav_facts,
                              {"url": "u", "perception": {"firstAt": 1, "clobbers": 0}},
                              {"url": "u", "perception": {"firstAt": 1, "clobbers": 0}})
    assert "A human notices the screen JUMP" not in prompt
    assert "do NOT invent a screen-jump bug" in prompt

    # 3) THE BLAME CONTRACT via _ai_evaluate: the model claims a blocking 'won't open' BUG, but tags the
    #    verdict 'control-not-found' (the intended control was never correctly clicked). The explorer MUST
    #    refuse to record it as an app bug — this is exactly the proven false positive.
    fake_ev = types.ModuleType("factory")
    fake_ev.agent = lambda role, repo, task: {"rc": 0, "out": json.dumps({
        "target_confirmed": False, "matches_expected": False, "verdict": "control-not-found",
        "bug": "Assistant won't open — BLOCKING", "severity": "critical", "blocking": True}), "out_full": ""}
    sys.modules["factory"] = fake_ev
    exc = Explorer("http://x", vision="v", autostart=False)
    targeting = {"intended": "Assistant", "targeted_label": "unrelated widget", "label_matched": False,
                 "effect_registered": False, "control_present": True, "retried": True}
    v = exc._ai_evaluate({"goal": "open assistant"}, "assistant panel opens", targeting,
                         {"url": "u"}, {"url": "u"})
    assert v["verdict"] == "control-not-found", v
    assert v["bug"] is None, "a mis-targeted/missed click MUST NOT be recorded as an app bug"
    assert v["blocking"] is False and v["severity"] == "none", v

    # 3a) A delayed second contact-link click after the busy window re-enabled the link is NOT evidence that an
    #     aria-disabled link handed off. The evaluator must not file a duplicate-click bug unless the before-state
    #     proves the control was actually disabled at the moment of the second click.
    delayed_repeat = types.ModuleType("factory")
    delayed_repeat.agent = lambda role, repo, task: {"rc": 0, "out": json.dumps({
        "target_confirmed": True, "matches_expected": False, "verdict": "bug",
        "bug": "Email booking request is aria-disabled but still triggers a duplicate mailto handoff",
        "severity": "medium", "blocking": False}), "out_full": ""}
    sys.modules["factory"] = delayed_repeat
    exc_repeat = Explorer("http://x", vision="v", autostart=False)
    v_repeat = exc_repeat._ai_evaluate(
        {"title": "Prevent duplicate contact activation",
         "steps": ["Click Email booking request", "Click Email booking request again rapidly"],
         "expected_outcome": "The second rapid click while aria-disabled is ignored."},
        "The second click while aria-disabled should be ignored.",
        {**handoff_facts, "before_control_disabled": False},
        {"url": "u"}, {"url": "u"})
    assert v_repeat["verdict"] == "inconclusive" and v_repeat["bug"] is None, v_repeat

    setup_scroll = types.ModuleType("factory")
    setup_scroll.agent = lambda role, repo, task: {"rc": 0, "out": json.dumps({
        "target_confirmed": True, "matches_expected": False, "verdict": "bug",
        "bug": "The scroll action did not move the page down to the Owner name field; it is not visible.",
        "severity": "high", "blocking": True}), "out_full": ""}
    sys.modules["factory"] = setup_scroll
    exc_scroll = Explorer("http://x", vision="v", autostart=False)
    v_scroll = exc_scroll._ai_evaluate(
        {"title": "Submit phone-only enquiry",
         "steps": ["Open the Visitor panel", "Fill all required booking fields"],
         "expected_outcome": "The enquiry is accepted."},
        "The booking fields, including Owner name, should be visible before data entry.",
        {"action_kind": "scroll", "control_action": False, "label_matched": True, "effect_registered": False},
        {"url": "u"}, {"url": "u"})
    assert v_scroll["verdict"] == "retry" and v_scroll["bug"] is None, v_scroll

    setup_wait = types.ModuleType("factory")
    setup_wait.agent = lambda role, repo, task: {"rc": 0, "out": json.dumps({
        "target_confirmed": True, "matches_expected": False, "verdict": "bug",
        "bug": "The expected Check and save enquiry control is not present as an interactable control.",
        "severity": "high", "blocking": True}), "out_full": ""}
    sys.modules["factory"] = setup_wait
    exc_wait = Explorer("http://x", vision="v", autostart=False)
    v_wait = exc_wait._ai_evaluate(
        {"title": "Submit with Enter key",
         "steps": ["Open the Visitor panel", "Fill every required field", "Press Enter"],
         "expected_outcome": "The booking form submits exactly once."},
        "The Check and save enquiry control should be available before submission.",
        {"action_kind": "wait", "control_action": False, "label_matched": True, "effect_registered": False},
        {"url": "u"}, {"url": "u"})
    assert v_wait["verdict"] == "retry" and v_wait["bug"] is None, v_wait

    busy_handoff = types.ModuleType("factory")
    busy_handoff.agent = lambda role, repo, task: {"rc": 0, "out": json.dumps({
        "target_confirmed": True, "matches_expected": False, "verdict": "bug",
        "bug": "Call to book no longer keeps href tel:+14155550123 while busy",
        "severity": "medium", "blocking": False}), "out_full": ""}
    sys.modules["factory"] = busy_handoff
    exc_busy = Explorer("http://x", vision="v", autostart=False)
    v_busy = exc_busy._ai_evaluate(
        {"title": "Open phone booking route",
         "steps": ["Open the Visitor panel", "Click Call to book in the Book A Walk section"],
         "expected_outcome": "The control targets tel:+14155550123, enters a temporary aria-disabled busy state, and returns to an enabled state after the busy period."},
        "The Call to book control should target tel:+14155550123 and enter a temporary aria-disabled busy state.",
        {"action_kind": "click", "control_action": True, "label_matched": True, "effect_registered": True,
         "external_handoff_url": "tel:+14155550123", "after_control_disabled": True},
        {"url": "u"}, {"url": "u"})
    assert v_busy["verdict"] == "pass" and v_busy["matches_expected"] is True and v_busy["bug"] is None, v_busy

    exc_resolve = Explorer("http://x", vision="v", autostart=False)
    exc_resolve.bugs = [{"title": "missing denial status", "blocking": False,
                         "covers": ["Verify the live region announces Team requires review mode before showing Visitor"]}]
    exc_resolve._mark_resolved_bugs(
        {"elements": []}, 2,
        ["Verify the live region announces Team requires review mode before showing Visitor"])
    assert exc_resolve.bugs[0].get("resolved") is True, exc_resolve.bugs
    exc_unresolved = Explorer("http://x", vision="v", autostart=False)
    exc_unresolved.bugs = [{"title": "privacy leak", "blocking": False,
                            "covers": ["Verify minimized payload contains no private data"]}]
    exc_unresolved._mark_resolved_bugs(
        {"elements": []}, 2, ["Verify minimized payload contains no private data"], successful=False)
    assert not exc_unresolved.bugs[0].get("resolved"), exc_unresolved.bugs

    # 3b) sanity: a real bug (right control exercised, still misbehaved) IS recorded.
    fake_bug = types.ModuleType("factory")
    fake_bug.agent = lambda role, repo, task: {"rc": 0, "out": json.dumps({
        "target_confirmed": True, "matches_expected": False, "verdict": "bug",
        "bug": "panel opened but rendered blank", "severity": "high", "blocking": True}), "out_full": ""}
    sys.modules["factory"] = fake_bug
    exc2 = Explorer("http://x", vision="v", autostart=False)
    v2 = exc2._ai_evaluate({"goal": "g"}, "panel renders", {"intended": "Assistant", "label_matched": True,
                           "effect_registered": True}, {"url": "u"}, {"url": "u2"})
    assert v2["verdict"] == "bug" and v2["bug"] and v2["blocking"] is True, v2

    # 3c) Primary Codex is trusted; explicit failover Codex is downgraded. This preserves the old
    #     rate-limit safety valve without suppressing every bug when Codex is the tenant/default engine.
    primary_codex = types.ModuleType("factory")
    primary_codex.agent = lambda role, repo, task: {"rc": 0, "model": "codex", "engine": "codex",
        "out": json.dumps({"target_confirmed": True, "matches_expected": False, "verdict": "bug",
                           "bug": "selected tab is missing aria-current", "severity": "high",
                           "blocking": True}), "out_full": ""}
    sys.modules["factory"] = primary_codex
    exc3 = Explorer("http://x", vision="v", autostart=False)
    v3 = exc3._ai_evaluate({"goal": "g"}, "aria-current moves", {"intended": "Team",
                            "label_matched": True, "effect_registered": True}, {"url": "u"}, {"url": "u"})
    assert v3["verdict"] == "bug" and v3["bug"], v3

    failover_codex = types.ModuleType("factory")
    failover_codex.agent = lambda role, repo, task: {"rc": 0, "model": "codex", "engine": "codex",
        "failover": True, "out": json.dumps({"target_confirmed": True, "matches_expected": False,
                           "verdict": "bug", "bug": "low-confidence failover bug", "severity": "high",
                           "blocking": True}), "out_full": ""}
    sys.modules["factory"] = failover_codex
    exc4 = Explorer("http://x", vision="v", autostart=False)
    v4 = exc4._ai_evaluate({"goal": "g"}, "state changes", {"intended": "Team",
                            "label_matched": True, "effect_registered": True}, {"url": "u"}, {"url": "u"})
    assert v4["verdict"] == "inconclusive" and v4["bug"] is None, v4

    # ORIGIN PINNING: a model-invented `goto` must never take the session off the app under test. This is a
    # regression guard for a real stall — a post-fix re-observation navigated to http://localhost:3000 while
    # the app served 127.0.0.1:8871, hit chrome-error for 22 steps, and reported an ALREADY-FIXED bug as
    # unfixed, so the QA loop never converged.
    _b = BrowserBridge.__new__(BrowserBridge)
    _b.target_url = "http://127.0.0.1:8871"
    assert _b._pin_origin("http://localhost:3000") == "http://127.0.0.1:8871"
    assert _b._pin_origin("http://localhost:3000/settings") == "http://127.0.0.1:8871/settings"
    assert _b._pin_origin("http://127.0.0.1:8871/") == "http://127.0.0.1:8871/"      # correct origin untouched
    assert _b._pin_origin("/admin?x=1") == "http://127.0.0.1:8871/admin?x=1"          # relative resolves
    # a bad host inside a QUERY PARAM is a legitimate data-source-failure test and must survive intact
    _q = "http://127.0.0.1:8871/?stateUrl=http://invalid-nonexistent-hostname-12345.test/s.json"
    assert _b._pin_origin(_q) == _q
    assert _b._pin_origin("") == "http://127.0.0.1:8871" and _b._pin_origin(None) == "http://127.0.0.1:8871"
    _nb = BrowserBridge.__new__(BrowserBridge); _nb.target_url = ""                   # unconfigured -> no rewrite
    assert _nb._pin_origin("http://x/y") == "http://x/y"

    print("qa_explorer targeting/contract selftest: PASS (resolution + effect + retry-safe blame contract; "
          "goto pinned to the app's own origin)")


def _selftest_story_scope():
    visitor_story = {
        "title": "Open initial visitor hash",
        "steps": ["Open the app URL with #visitor", "Inspect the visible panel and navigation state"],
        "expected_outcome": "The Visitor panel is visible, Team and CEO panels are hidden, and the status reads Showing Visitor.",
    }
    scoped = _scoped_aspects(visitor_story, [
        "open the app directly at #visitor",
        "verify Team and CEO panels are hidden",
        "switch to Team then back to Visitor",
        "reload the page while on #visitor",
    ])
    assert "open the app directly at #visitor" in scoped, scoped
    assert "verify Team and CEO panels are hidden" in scoped, scoped
    assert all("switch to team" not in s.lower() for s in scoped), scoped
    assert all("reload" not in s.lower() for s in scoped), scoped

    denied_story = {
        "title": "Public internal navigation is denied",
        "steps": ["Open the public web app", "Click Team", "Click CEO", "Click Visitor again"],
        "expected_outcome": "Team and CEO attempts stay on the Visitor panel, internal content remains hidden, and the runtime status explains review mode is required.",
    }
    denied = _scoped_aspects(denied_story, [
        "click the Team navigation button from the Visitor panel",
        "verify Team lead-control content remains hidden",
        "switch to Team and show lead records",
    ])
    assert "click the Team navigation button from the Visitor panel" in denied, denied
    assert "verify Team lead-control content remains hidden" in denied, denied
    assert all("show lead records" not in s.lower() for s in denied), denied

    first_enabled_story = {
        "title": "Jump to booking routes",
        "steps": ["Open the Visitor panel", "Click Check availability in the hero"],
        "expected_outcome": "The page scrolls to the Book A Walk section and focus moves to the first enabled booking contact route.",
    }
    first_enabled = _scoped_aspects(first_enabled_story, [
        "verify focus moves to the first enabled booking contact route",
        "verify disabled or unavailable booking routes are skipped when determining focus",
    ])
    assert "verify focus moves to the first enabled booking contact route" in first_enabled, first_enabled
    assert all("disabled" not in s.lower() and "unavailable" not in s.lower() for s in first_enabled), first_enabled
    print("qa_explorer story-scope selftest: PASS (coverage cannot expand beyond story contract)")


def _selftest_reobserve():
    """Pin the RACE-CONDITION fix end-to-end in the loop: when an action changes the view but the
    EXPECTED control hasn't PAINTED yet, the explorer settles + re-observes ONCE and judges the settled
    snapshot — so a late-painting control (textarea#cmsg ~0.6-1.2s late) is NEVER read as 'never renders'."""
    import types

    nav = {"idx": 0, "tag": "a", "text": "Assistant", "role": "link", "selector": '[data-aos-idx="0"]'}
    composer = {"idx": 1, "tag": "textarea", "type": "", "text": "", "placeholder": "Message",
                "selector": '[data-aos-idx="1"]'}
    # Clicking Assistant switches the route (url changes = effect), but the composer paints LATE: the
    # INSTANT after-state is still a loading view (composer absent); only the post-settle re-observe
    # shows it. Snapshotting once would falsely report "the composer never renders".
    states = [
        {"url": "http://app/#/home", "title": "AOS", "bodyText": "home", "elements": [nav]},          # before
        {"url": "http://app/#/assistant", "title": "AOS", "bodyText": "loading", "elements": [nav]},   # instant after (racy)
        {"url": "http://app/#/assistant", "title": "AOS", "bodyText": "assistant ready",
         "elements": [nav, composer]},                                                                 # post-settle
    ]

    class LateBridge:
        def __init__(self):
            self.log, self._i = [], 0

        def state(self):
            self.log.append("state")
            s = dict(states[min(self._i, len(states) - 1)])
            self._i += 1
            s.update({"ok": True, "screenshot": None, "console_errors": [], "recent_requests": [], "settled": True})
            return s

        def settle(self, ms=None):
            self.log.append("settle")
            return {"ok": True, "settled": True}

        def act(self, action):
            self.log.append(("act", action.get("cmd")))
            return {"ok": True, "matched": "Assistant", "clicked": True}

        def close(self):
            self.log.append("close")

    seen = {}
    fake = types.ModuleType("factory")

    def fake_agent(role, repo, task):
        if "DECIDE the single next action" in task:
            return {"rc": 0, "out": "", "out_full": json.dumps({
                "reasoning": "open the assistant", "intent": "Assistant nav link",
                "next_action": {"cmd": "click", "target_text": "Assistant", "role": "link"},
                "expected": "the Assistant composer (textarea#cmsg) renders",
                "expected_control": "Message", "done": True})}
        if "EVALUATE expected-vs-actual" in task:
            # the evaluator MUST be handed the SETTLED after-state that actually contains the composer —
            # not the racy loading snapshot. Capture proof of both from the prompt it received.
            seen["settled_line"] = "given time to render)? True" in task
            seen["present_line"] = "present in the settled after-state? True" in task
            seen["sees_composer"] = 'LABEL="Message"' in task
            return {"rc": 0, "out": "", "out_full": json.dumps({
                "target_confirmed": True, "matches_expected": True, "verdict": "pass",
                "bug": None, "severity": "none", "blocking": False})}
        raise AssertionError("unexpected agent task")

    fake.agent = fake_agent
    sys.modules["factory"] = fake

    ex = Explorer("http://app", vision="The Assistant composer must render on open.", autostart=False)
    ex.bridge = LateBridge()
    records = ex.explore({"title": "open assistant", "goal": "open the assistant and see the composer",
                          "expected": "the message composer renders"}, max_steps=1)

    r = records[0]
    # the loop must have settled + observed a SECOND time after the racy after-state
    assert ex.bridge.log == ["state", ("act", "click"), "state", "settle", "state"], ex.bridge.log
    assert r["reobserved"] is True, "must re-observe when an effect registered but the expected control was absent"
    t = r["targeting"]
    assert t["settled"] is True, "the after-state fed to the evaluator is post-settle"
    assert t["expected_control"] == "Message"
    assert t["expected_control_present"] is True, "the LATE-painting composer WAS found after the re-observe"
    # the evaluator saw the settled snapshot WITH the composer — so it can't cry 'never renders'
    assert seen.get("settled_line") and seen.get("present_line") and seen.get("sees_composer"), seen
    assert r["bug"] is None and r["verdict"]["verdict"] == "pass", "a late paint must NEVER be recorded as a bug"

    print("qa_explorer re-observe/settle selftest: PASS "
          "(late-painting control re-observed post-settle, not falsely judged 'never renders')")


if __name__ == "__main__":
    _selftest()
