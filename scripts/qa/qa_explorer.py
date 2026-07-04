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
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

BRIDGE_JS = Path(__file__).resolve().parent / "browser_bridge.js"
NODE_PATH = os.environ.get("NODE_PATH") or str(
    Path.home() / "projects" / "products" / "noupload" / "node_modules")
ROLE = "qa-security"


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


def _call_agent(role, repo, task):
    """The single seam to the LLM. Imported lazily so the module loads (and self-tests) without pulling
    in the heavy factory runtime, and so a stubbed `factory` in sys.modules is honoured. factory.agent
    owns all resilience (529/overload retry, Codex failover) and governance."""
    import factory  # noqa: E402 — lazy on purpose (see docstring)
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

    def __init__(self, target_url, token=None, org="0", timeout=45, autostart=True):
        self.timeout = timeout
        self._id = 0
        self.proc = None
        if not autostart:
            return
        env = {**os.environ, "NODE_PATH": NODE_PATH}
        self.proc = subprocess.Popen(
            ["node", str(BRIDGE_JS)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
        self._await_ready()
        # session setup: seed auth BEFORE the app boots (addInitScript), then navigate.
        if token:
            self.seed_token(token, org)
        self.goto(target_url)

    def _readline(self):
        """Read one line from the bridge honouring the wall-clock timeout (a page load can be slow)."""
        if not self.proc or self.proc.poll() is not None:
            return None
        r, _, _ = select.select([self.proc.stdout], [], [], self.timeout)
        if not r:
            return None
        return self.proc.stdout.readline() or None

    def _await_ready(self):
        """Consume the startup handshake. The first non-noise line should be {cmd:'ready'}; a startup
        error line (playwright missing, etc.) surfaces here instead of hanging."""
        for _ in range(5):
            line = self._readline()
            if not line:
                break
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("cmd") == "ready":
                return j
            if j.get("ok") is False:
                raise RuntimeError(f"browser_bridge failed to start: {j.get('error')}")
        raise RuntimeError("browser_bridge did not become ready")

    def _send(self, obj):
        if not self.proc or self.proc.poll() is not None:
            return {"ok": False, "error": "bridge process is not running"}
        self._id += 1
        obj = {"id": self._id, **obj}
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            return {"ok": False, "error": f"bridge write failed: {e}"}
        # commands are strictly serial; skip any stray line until our id comes back (defensive).
        for _ in range(4):
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

    def seed_token(self, token, org="0"):
        return self._send({"cmd": "seedToken", "token": token, "org": org})

    def state(self):
        """Observe: url/title, a screenshot path, enumerated interactable elements (each with a stable
        selector + idx), recent network requests, console/page errors — plus the page's visible text
        (fetched via a follow-up eval, since the native state doesn't include body text)."""
        st = self._send({"cmd": "state"})
        if st.get("ok"):
            ev = self._send({"cmd": "eval",
                             "expr": "document.body?document.body.innerText.replace(/\\s+/g,' ')"
                                     ".trim().slice(0,4000):''"})
            st["bodyText"] = ev.get("result") if ev.get("ok") else ""
        return st

    def act(self, action):
        """Act: translate the AI's action dict {cmd, idx|selector, value} into the bridge's native
        command. Supported cmds: click, type/fill, goto, scroll, wait, noop (unknowns pass through
        so a new bridge verb works without a code change here)."""
        action = action or {}
        cmd = (action.get("cmd") or "noop").lower()
        idx, sel, val = action.get("idx"), action.get("selector"), action.get("value")
        target_text, role = action.get("target_text") or action.get("target"), action.get("role")
        if cmd in ("click", "tap"):
            # Prefer label/role resolution against the LIVE DOM (robust to SPA re-renders); only fall
            # back to idx/selector when no control matches the intent. A miss returns clicked:false —
            # the loop treats that as a missed click to retry, NOT as an app bug.
            if target_text:
                r = self._send({"cmd": "clickByText", "text": target_text, "role": role})
                if r.get("ok") and r.get("clicked"):
                    return r
                if sel is None and idx is None:
                    return r  # nothing else to try; surface the miss
            return self._send({"cmd": "click", "idx": idx, "selector": sel})
        if cmd in ("type", "fill"):
            return self._send({"cmd": "fill", "idx": idx, "selector": sel, "value": val})
        if cmd == "goto":
            return self._send({"cmd": "goto", "url": val or action.get("url")})
        if cmd == "scroll":
            return self._send({"cmd": "eval", "expr": f"window.scrollBy(0,{int(val or 600)});true"})
        if cmd in ("wait", "noop"):
            return {"ok": True, "cmd": cmd}
        return self._send({"cmd": cmd, **{k: v for k, v in action.items() if k != "cmd"}})

    def close(self):
        try:
            self._send({"cmd": "close"})
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# ----------------------------------------------------------------------------------------------------
# Prompt assembly — kept as pure functions so the self-test can assert exactly what the AI is shown.
# ----------------------------------------------------------------------------------------------------
def _element_label(e):
    """The single best human/AI-facing LABEL for a control — what the AI should target it by. Prefers
    visible text, then aria-label, then name/placeholder/title. This is the anchor for intent-based
    targeting (NOT the idx)."""
    for k in ("text", "ariaLabel", "name", "placeholder", "title"):
        v = (e.get(k) or "").strip()
        if v:
            return v
    return ""


def _fmt_elements(elements, limit=80):
    lines = []
    for e in (elements or [])[:limit]:
        parts = [f"[{e.get('idx')}]", e.get("tag", "?")]
        if e.get("type"):
            parts.append(e["type"])
        # LABEL is called out explicitly so the AI targets by intent/label, not by blind index.
        label = _element_label(e)
        parts.append(f'LABEL="{label}"' if label else "LABEL=(none)")
        role = (e.get("role") or "").strip()
        if role:
            parts.append(f"role={role}")
        for k in ("name", "placeholder", "href"):
            if e.get(k) and (e.get(k) or "").strip() != label:
                parts.append(f"{k}={e[k]}")
        if e.get("disabled"):
            parts.append("(disabled)")
        # SEMANTIC STATE from the DOM (aria-pressed/aria-selected/aria-expanded/checked/value): the
        # ground truth for selected/active/checked judgements. Screenshots also carry hover/focus/
        # transition styling — pixels alone must never decide state (a cursor resting on a button is
        # not a selection; three QA rounds false-positived on exactly that).
        for k, tag in (("pressed", "aria-pressed"), ("selectedState", "aria-selected"),
                       ("expanded", "aria-expanded"), ("checked", "checked")):
            if e.get(k) is not None and str(e.get(k)).strip() != "":
                parts.append(f"{tag}={e[k]}")
        if e.get("value") is not None and str(e.get("value")).strip() != "":
            parts.append(f"value={json.dumps(str(e['value'])[:60])}")
        lines.append(" ".join(str(p) for p in parts))
    return "\n".join(lines) or "(no interactable elements found)"


def _fmt_history(history, limit=8):
    if not history:
        return "(this is the first step)"
    out = []
    for h in history[-limit:]:
        out.append(
            f"- step {h['step']}: action={json.dumps(h['action'])} expected={h['expected']!r} "
            f"-> {'MATCH' if h.get('matched') else 'MISMATCH'}"
            + (f" BUG: {h['bug']}" if h.get("bug") else ""))
    return "\n".join(out)


def _fmt_requests(reqs, limit=8):
    out = []
    for r in (reqs or [])[-limit:]:
        tag = r.get("failed") or r.get("status")
        out.append(f"{r.get('method')} {r.get('url')} -> {tag}")
    return "\n".join(out) or "(none)"


def _fmt_state(state):
    return (
        f"URL: {state.get('url')}\n"
        f"TITLE: {state.get('title')}\n"
        f"CONSOLE_ERRORS: {json.dumps((state.get('console_errors') or [])[:6])}\n"
        f"RECENT_NETWORK (method url -> status; null status/failed = a failed request):\n"
        f"{_fmt_requests(state.get('recent_requests'))}\n"
        f"VISIBLE_TEXT (truncated):\n{(state.get('bodyText') or '')[:1500]}\n"
        f"INTERACTABLE_ELEMENTS (refer to these by their [idx]):\n{_fmt_elements(state.get('elements'))}")


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
    for e in elements or []:
        erole = (e.get("role") or e.get("tag") or "").strip().lower()
        if role and erole != role:
            continue
        lab = _element_label(e)
        ll = lab.lower()
        if not ll:
            score = 0
        elif ll == wl:
            score = 3
        elif wl in ll or ll in wl:
            score = 2
        else:
            score = 0
        if score > best_score:
            best, best_score, best_label = e, score, lab
    if best is None or best_score == 0:
        return None, None, 0
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
    new/changed network request, or a new console error."""
    if not act_result or act_result.get("ok") is False:
        return False
    if (before.get("url") != after.get("url")) or (before.get("title") != after.get("title")):
        return True
    if (before.get("bodyText") or "") != (after.get("bodyText") or ""):
        return True
    if json.dumps(before.get("recent_requests") or []) != json.dumps(after.get("recent_requests") or []):
        return True
    if json.dumps(before.get("console_errors") or []) != json.dumps(after.get("console_errors") or []):
        return True
    return False


def _decide_prompt(vision, story, state, history):
    return f"""ROLE: You are the QA-SECURITY explorer. Task: DECIDE the single next action.

You are exercising a product as an adversarial, thorough QA engineer. Judge everything against the
ORIGINAL VISION and the STORY's EXPECTED behaviour — you are here to find where reality diverges.

=== ORIGINAL PRODUCT VISION ===
{vision}

=== CURRENT STORY (what a user is trying to do) ===
title: {story.get('title', story.get('name', 'exploration'))}
goal: {story.get('goal', story.get('description', ''))}
EXPECTED OUTCOME: {story.get('expected', story.get('expected_outcome', ''))}

=== HISTORY SO FAR ===
{_fmt_history(history)}

=== CURRENT OBSERVED STATE ===
{_fmt_state(state)}

A screenshot of the current page is at: {state.get('screenshot')}
Open/read that image to SEE the page (layout, rendering, visual bugs), not just its text.

Decide the ONE next action that best advances this story toward its EXPECTED OUTCOME (or that probes an
edge/error case a rigorous tester would try).

TARGET BY INTENT, NOT BY INDEX. For click/type, identify the control by its LABEL (copy the LABEL="..."
string VERBATIM from INTERACTABLE_ELEMENTS above) into `target_text`, and set `role` when it disambiguates
(button/link/tab/menuitem/checkbox). The idx is only a fallback locator — the label is what actually
resolves the control. If several controls share a label, add the role. Never invent an idx you did not see.

Reply with ONLY a JSON object, no prose:
{{
  "reasoning": "<one sentence: why this action>",
  "intent": "<the control you intend to act on, in plain words, e.g. 'the Assistant nav link'>",
  "next_action": {{"cmd": "click|type|goto|scroll|wait",
                   "target_text": "<REQUIRED for click/type: the control's LABEL, copied verbatim>",
                   "role": "<optional: button|link|tab|menuitem|checkbox to disambiguate>",
                   "idx": <the element idx, as a FALLBACK only>,
                   "selector": "<css selector alternative, optional>",
                   "value": "<text to type / url to goto / scroll px, if the cmd needs one>"}},
  "expected": "<concretely, what SHOULD happen after this action — the yardstick for evaluation>",
  "expected_control": "<optional: if this action should make a SPECIFIC control appear (e.g. the message
                       composer textarea, a Save button), give that control's LABEL/placeholder here so the
                       explorer can wait for it to paint before judging. Leave '' if no specific control is expected>",
  "done": <true only if the story's expected outcome is fully satisfied OR further exploration is pointless>
}}"""


def _fmt_targeting(targeting):
    t = targeting or {}
    return (
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
        + (f"\nthe action was expected to make this control appear: {t.get('expected_control')!r} — "
           f"present in the settled after-state? {bool(t.get('expected_control_present'))}"
           if t.get('expected_control') else ""))


def _evaluate_prompt(vision, story, expected, targeting, before_state, after_state):
    return f"""ROLE: You are the QA-SECURITY explorer. Task: EVALUATE expected-vs-actual after an action.

Judge honestly and adversarially — but blame the APP only when the RIGHT control was actually exercised.
An explorer that cries wolf on its own missed click is worthless. So FIRST confirm targeting, THEN judge.

=== ORIGINAL PRODUCT VISION ===
{vision}

=== STORY ===
goal: {story.get('goal', story.get('description', ''))}
STORY EXPECTED OUTCOME: {story.get('expected', story.get('expected_outcome', ''))}

=== WHAT WAS EXPECTED FROM THE ACTION JUST TAKEN ===
{expected}

=== ACTION TARGETING (ground truth from the driver — trust this over your own reading of the page) ===
{_fmt_targeting(targeting)}

=== STATE BEFORE THE ACTION ===
{_fmt_state(before_state)}

=== STATE AFTER THE ACTION ===
{_fmt_state(after_state)}
Screenshot after the action: {after_state.get('screenshot')}  (read it to judge visual/rendering correctness)

CONTRACT — apply IN THIS ORDER:
1. Was the INTENDED control correctly actuated? (driver matched the intent's label AND the action
   registered an effect). If NO — the intended control was never exercised (a mis-target or a missed
   click) — then this is NOT an app bug. Use verdict "control-not-found" (intent had no matching control)
   or "retry"/"inconclusive" (a matching control exists but the click didn't land / effect is ambiguous).
   In these cases `bug` MUST be null.
2. ONLY if the right control WAS correctly actuated and the product STILL misbehaved (wrong/missing
   result, console/page errors, broken layout, a truly dead control that DID receive the click, a
   security/permission leak, data loss, confusing dead-end UX) is the verdict "bug".
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
3. If it behaved as expected, verdict "pass".

Reply with ONLY a JSON object, no prose:
{{
  "target_confirmed": <true ONLY if the intended control was correctly actuated AND registered an effect>,
  "matches_expected": <true|false>,
  "verdict": "<pass|bug|inconclusive|retry|control-not-found>",
  "bug": "<clear description of the bug, or null — MUST be null unless verdict is exactly 'bug'>",
  "severity": "<none|low|medium|high|critical>",
  "blocking": <true if this genuinely prevents any further meaningful exploration of this story>
}}"""


# ----------------------------------------------------------------------------------------------------
# The Explorer.
# ----------------------------------------------------------------------------------------------------
class Explorer:
    def __init__(self, target_url, vision, token=None, org="0", autostart=True):
        self.target_url = target_url
        self.vision = vision
        self.token = token
        self.org = org
        # a per-session scratch dir is the agent's cwd (so it can Read the screenshots the bridge
        # writes under /tmp/aos-qa and reason over them multimodally).
        self.repo = tempfile.mkdtemp(prefix=f"aos-qa-{uuid.uuid4().hex[:8]}-")
        self.bugs = []
        self.bridge = None
        if autostart:
            self.bridge = BrowserBridge(target_url, token=token, org=org)

    # --- the two AI decisions (EVERY one is a real model call via factory.agent) -----------------
    def _ai_decide(self, story, state, history):
        prompt = _decide_prompt(self.vision, story, state, history)
        res = _call_agent(ROLE, self.repo, prompt)
        j = _extract_json(res.get("out_full") or res.get("out") or "")
        # defensive defaults so a malformed reply can't crash the loop; a missing action becomes a no-op
        # that the next evaluate will flag as "nothing happened".
        return {
            "reasoning": j.get("reasoning", ""),
            "next_action": j.get("next_action") or {"cmd": "noop"},
            "expected": j.get("expected", ""),
            # the LABEL of a control this action should make appear — lets explore() wait for a
            # late-painting control before evaluating (anti RACE-CONDITION false positive).
            "expected_control": (j.get("expected_control") or "").strip(),
            "done": bool(j.get("done", False)),
            "_raw": res,
        }

    def _ai_evaluate(self, story, expected, targeting, before_state, after_state):
        prompt = _evaluate_prompt(self.vision, story, expected, targeting, before_state, after_state)
        res = _call_agent(ROLE, self.repo, prompt)
        j = _extract_json(res.get("out_full") or res.get("out") or "")
        bug = j.get("bug") if j.get("bug") not in (None, "", "null") else None
        verdict = (j.get("verdict") or "").strip().lower()
        if not verdict:                       # tolerate an older-style reply that omits `verdict`
            verdict = "bug" if bug else "pass"
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
            "_raw": res,
        }

    # --- robust targeting: bind the AI's INTENT to a concrete control ----------------------------
    def _prepare_action(self, action, elements):
        """Resolve an AI action's intent (target_text[+role]) to a concrete element BEFORE acting.
        Prefers the label/role-matched element's idx over any blind idx the AI guessed; only falls back
        to the AI's idx when nothing matches. Returns (prepared_action, aim) where `aim` records what
        control we were aiming at — the ground truth the evaluator judges targeting against."""
        action = dict(action or {})
        cmd = (action.get("cmd") or "noop").lower()
        idx, label, score = _resolve_target(action, elements)
        intended = (action.get("target_text") or action.get("target") or "").strip()
        if cmd in ("click", "tap", "type", "fill"):
            if idx is not None:
                action["idx"] = idx                     # label/role match wins over a blind idx
            elif not intended and action.get("idx") is not None:
                # AI gave only an idx — derive its label so downstream still has an intent to check.
                intended = _label_for_idx(action.get("idx"), elements)
                label = intended
        return action, {"intended": intended, "role": (action.get("role") or "").strip(),
                        "resolved_idx": idx if idx is not None else action.get("idx"),
                        "resolved_label": label, "score": score}

    def _targeting_facts(self, aim, act_result, before, after, retried=False, settled=False,
                         expected_control=None, expected_control_present=None):
        """Assemble the targeting ground-truth the evaluator sees: what we aimed at, what we actually
        actuated, whether it matched the intent, and whether anything happened."""
        actuated = None
        if act_result and act_result.get("matched"):        # clickByText tells us the live-matched label
            actuated = act_result.get("matched")
        elif aim.get("resolved_label"):
            actuated = aim.get("resolved_label")
        intended = aim.get("intended")
        label_matched = _labels_match(intended, actuated) if intended else bool(actuated)
        _, _, present_score = _resolve_target(
            {"target_text": intended, "role": aim.get("role")}, after.get("elements")) \
            if intended else (None, None, 0)
        return {
            "intended": intended,
            "role": aim.get("role"),
            "targeted_label": actuated,
            "label_matched": label_matched,
            "effect_registered": _effect_registered(before, after, act_result),
            "control_present": bool(present_score),
            "retried": retried,
            # the after-state was captured AFTER the SPA settled (skeletons gone, DOM stable) — so an
            # absent control is genuinely absent, NOT a mid-paint race.
            "settled": bool(settled),
            "expected_control": expected_control,
            "expected_control_present": expected_control_present,
        }

    # --- the state-based loop --------------------------------------------------------------------
    def explore(self, story, max_steps=25, on_bug=None, deadline=None):
        """Run the observe -> AI-decide -> act -> (retry-on-miss) -> observe -> AI-evaluate loop for one
        story. Returns a list of step-records. Fires on_bug(bug_record) for each real bug the AI finds.
        `deadline` (epoch secs) bounds WALL-CLOCK: each AI step is a real (slow) model call, so a bounded
        run must be able to stop MID-story, not only between stories — else one story blows the whole
        budget (seen live: a 10-min budget overran to 14min+)."""
        if self.bridge is None:
            raise RuntimeError("Explorer has no browser bridge (constructed with autostart=False)")
        records, history = [], []
        for step in range(max_steps):
            if deadline and time.time() > deadline:              # wall-clock budget stop (mid-story)
                break
            state = self.bridge.state()                          # OBSERVE
            decision = self._ai_decide(story, state, history)    # AI DECIDES
            raw_action = decision["next_action"]
            action, aim = self._prepare_action(raw_action, state.get("elements"))  # BIND INTENT->CONTROL
            act_result = self.bridge.act(action)                 # ACT
            after = self.bridge.state()                          # OBSERVE AGAIN

            # RETRY-ON-MISS (self-correction): if nothing happened yet a control matching the intent is
            # still on the page, the click simply missed — re-resolve against the fresh DOM and try ONCE
            # more before any evaluation. A missed click is a driver problem, never an app bug.
            retried = False
            cmd = (action.get("cmd") or "noop").lower()
            if cmd in ("click", "tap", "type", "fill") and not _effect_registered(state, after, act_result):
                retry_action, retry_aim = self._prepare_action(raw_action, after.get("elements"))
                if retry_aim.get("resolved_idx") is not None or retry_aim.get("intended"):
                    _, _, present = _resolve_target(
                        {"target_text": retry_aim.get("intended"), "role": retry_aim.get("role")},
                        after.get("elements"))
                    if present:
                        act_result = self.bridge.act(retry_action)       # ACT (retry once)
                        after = self.bridge.state()                      # OBSERVE AGAIN
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
                after = self.bridge.state()              # OBSERVE AGAIN — post-settle
                reobserved = True
                _, _, ec_present = _resolve_target(
                    {"target_text": expected_control}, after.get("elements"))

            # the after-state we hand to the evaluator is always post-settle (state() settles the SPA);
            # `settled` tells the model an absent control is genuinely absent, not a mid-paint race.
            targeting = self._targeting_facts(
                aim, act_result, state, after, retried=retried, settled=True,
                expected_control=expected_control or None,
                expected_control_present=bool(ec_present) if expected_control else None)
            verdict = self._ai_evaluate(story, decision["expected"], targeting, state, after)  # AI EVALUATES

            bug = None
            if verdict["bug"]:
                bug = {
                    "step": step,
                    "story": story.get("title", story.get("name", "")),
                    "url": after.get("url"),
                    "action": action,
                    "expected": decision["expected"],
                    "bug": verdict["bug"],
                    "severity": verdict["severity"],
                    "blocking": verdict["blocking"],
                    "shot": after.get("screenshot"),
                }
                self.bugs.append(bug)
                if on_bug:
                    try:
                        on_bug(bug)
                    except Exception:
                        pass

            record = {
                "step": step,
                "state": {k: state.get(k) for k in ("url", "title", "screenshot", "console_errors")},
                "action": action,
                "expected": decision["expected"],
                "actual": {k: after.get(k) for k in ("url", "title", "screenshot", "console_errors")},
                "bug": bug,
                "targeting": targeting,
                "retried": retried,
                "reobserved": reobserved,
                "verdict": {k: verdict[k] for k in
                            ("matches_expected", "verdict", "target_confirmed", "bug", "severity", "blocking")},
                "done": decision["done"],
                "act_result": act_result,
            }
            records.append(record)
            history.append({
                "step": step, "action": action, "expected": decision["expected"],
                "matched": verdict["matches_expected"], "verdict": verdict["verdict"],
                "bug": verdict["bug"],
            })

            if decision["done"] or verdict["blocking"]:          # story satisfied, or a wall — stop
                break
        return records

    def close(self):
        if self.bridge:
            self.bridge.close()
            self.bridge = None


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
        if "DECIDE the single next action" in task:
            n = calls["decide"]
            calls["decide"] += 1
            calls["order"].append("decide")
            # the vision + story's expected outcome must be present in what the AI is shown
            assert "ORIGINAL PRODUCT VISION" in task and "EXPECTED OUTCOME" in task
            # the decide contract must ask the model to target by LABEL (target_text), not blind idx
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
        elif "EVALUATE expected-vs-actual" in task:
            n = calls["evaluate"]
            calls["evaluate"] += 1
            calls["order"].append("evaluate")
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
        raise AssertionError("unexpected agent task (neither decide nor evaluate):\n" + task[:200])

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

    # 2) EFFECT detection: identical states = no effect (a missed click); any change = effect.
    b = {"url": "u", "title": "t", "bodyText": "x", "recent_requests": [], "console_errors": []}
    same = dict(b)
    assert _effect_registered(b, same, {"ok": True}) is False, "no change => no effect"
    assert _effect_registered(b, {**b, "url": "u2"}, {"ok": True}) is True, "nav => effect"
    assert _effect_registered(b, {**b, "bodyText": "y"}, {"ok": True}) is True, "DOM change => effect"
    assert _effect_registered(b, {**b, "recent_requests": [{"url": "/api"}]}, {"ok": True}) is True, "net => effect"
    assert _effect_registered(b, same, {"ok": False}) is False, "failed act => no effect"

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

    print("qa_explorer targeting/contract selftest: PASS (resolution + effect + retry-safe blame contract)")


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
