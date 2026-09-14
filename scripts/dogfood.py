#!/usr/bin/env python3
"""dogfood.py — the STANDING acceptance pass: a demanding-user persona DRIVES THE LIVE CONSOLE daily.

REWRITE per ARCH-REVIEW-2026-07 (quality-engine): the old pass spawned a one-off TEXT agent that curled
routes and read source — it never clicked anything, so it could not feel what a user feels. This pass
drives the real console through scripts/qa/qa_explorer.Explorer (a real Playwright browser; EVERY
decide/evaluate is a factory.agent call, role qa-security) with the PERSONA BRIEF AS THE VISION, on the
REAL journeys a CEO actually lives:

    signup -> verify -> create company -> direct a build -> wait/watch -> review -> respond

Contract (docs/STANDARDS-acceptance.md + docs/STANDARDS-verification.md):
  * seeds its OWN tenant (billing.signup + provider connect + org + consent — exactly like
    scripts/console_actions_e2e.sh), so the signed-in journeys never depend on operator state;
    the signup journey runs in a FRESH, tokenless browser session so the real form is exercised.
  * every explorer bug is FILED via findings.py with its TRUE severity (critical stays critical —
    never downgraded), deduped by signature against the open backlog before filing;
  * any BLOCKING finding raises alerts.raise_alert() IMMEDIATELY (inside on_bug — minutes, not weeks);
  * schedulable: scheduler.DEFAULT_SCHEDULES keeps the 'acceptance-dogfood' job, DAILY, in `cron`
    mode (detaches the real run so scheduler.JOB_TIMEOUT can't guillotine a long browser pass);
  * the builder never grades its own homework: the explorer runs as qa-security against the live app.

    dogfood.py run [persona|all]   # drive the live console through the explorer now, file findings
    dogfood.py qa-explore [persona]# guarded live skeptic pass: preflight, then run one explorer persona
    dogfood.py preflight [persona] # no-spend readiness gate before a live run
    dogfood.py cron                # scheduler entry point: detach a full `run` and return at once
    dogfood.py personas            # list the rotating personas
    dogfood.py selftest            # offline wiring check (Explorer/findings/alerts/audit stubbed)
Set AOS_DOGFOOD_BASE to target a non-default console URL. Run with the agent-os venv python.
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
QA = SCRIPTS / "qa"
if str(QA) not in sys.path:
    sys.path.insert(0, str(QA))

BASE = os.environ.get("AOS_DOGFOOD_BASE", "http://127.0.0.1:8099")
SOURCE = "dogfood"
LOG = "/tmp/aos-dogfood.log"
MAX_STEPS = int(os.environ.get("AOS_DOGFOOD_MAX_STEPS", "150"))    # SAFETY backstop only — the explorer is
# coverage-driven (stops when everything a user would try is tested), not capped at N steps. The per-persona
# BUDGET_S wall-clock is the real bound here; this just guards a runaway. Env-overridable; was 20.
BUDGET_S = int(os.environ.get("AOS_DOGFOOD_BUDGET_S", "3600"))      # per-persona wall-clock budget
ALLOW_OPEN_FINDINGS = os.environ.get("AOS_DOGFOOD_ALLOW_OPEN_FINDINGS", "").lower() in ("1", "true", "yes")
DOGFOOD_DEPTH = os.environ.get("AOS_DOGFOOD_DEPTH", "critical").strip().lower()

# ─────────────────────────────────────────────────────────────────────────────────────────────────────
# The rotating demanding-user personas. Each brief becomes the Explorer's VISION — the yardstick every
# AI evaluate call judges expected-vs-actual against — so the same journeys are felt through a
# different unforgiving lens each day.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────
PERSONAS = {
    "first-run": "a non-technical founder using agent-os for the FIRST time. The first ten minutes must "
                 "feel as polished as ChatGPT/Linear/Stripe onboarding: every screen self-explanatory, "
                 "every next step obvious, nothing confusing, amateur, or dead.",
    "async-wait": "a founder who kicked off a build and is WAITING. The async experience must match "
                  "Vercel/GitHub Actions/ChatGPT streaming: an ETA on kickoff, live progress, a ping when "
                  "results land, a working Stop/Cancel, and NEVER a false 'done' while work is running.",
    "latency": "an impatient power user. Quick interactions must feel near-instant (ChatGPT-fast); "
               "anything that spins for many seconds without streaming, progress, or a stop control is a "
               "defect. Slowness is a bug, not a mood.",
    "buyer": "a careful buyer deciding whether to trust this product with money. Provider connect, "
             "billing/upgrade, settings, and account flows must be complete and HONEST — anything that "
             "lies about its result (a fake 'connected'/'sent'/'done') is disqualifying.",
    "edge": "a user who breaks things: wrong inputs, empty states, denied actions, expired sessions, "
            "going back mid-flow. Every dead-end must give a guiding next step, never a scary raw error "
            "or a silent no-op.",
    "feature-completeness": "an auditor checking that EVERY feature met along the journeys is COMPLETE "
            "end-to-end — no 'not available yet' stubs, no dead buttons, no gated flow without a path "
            "forward. A half-built feature reaching a user is a defect.",
    "ux-researcher": "a UX researcher applying Nielsen's 10 heuristics: system status always visible, "
            "error recovery everywhere, no jargon, no unnecessary steps, low cognitive load. Users bounce "
            "at the slightest inconvenience — rank every friction as churn risk.",
    "product-design": "a product designer judging IA, visual hierarchy, consistency, affordances, "
            "empty/loading/error states, and microcopy tone. Every screen must feel finished and "
            "trustworthy, never unfinished or amateur.",
    "accessibility": "an accessibility specialist: keyboard-only operation, focus order and visible "
            "focus, labels/roles for assistive tech, contrast, no color-only signals, adequate target "
            "sizes. WCAG failures on the core journeys are real defects.",
}
_ORDER = list(PERSONAS)

# Route a persona's findings to the role that should own the fix (findings.py assigns an active agent).
_ROLE_FOR = {"first-run": "frontend-engineer", "async-wait": "backend-engineer", "latency": "backend-engineer",
             "buyer": "backend-engineer", "edge": "frontend-engineer",
             "feature-completeness": "frontend-engineer", "ux-researcher": "design-ux",
             "product-design": "design-ux", "accessibility": "frontend-engineer"}


class _DogfoodInterrupted(KeyboardInterrupt):
    pass


def _install_interrupt_handlers():
    """Turn process shutdown into a checkpointable interruption.

    A live browser/model pass can run for a long time. If the shell, scheduler, or operator stops it mid-story,
    the evidence directory must still say which journey was in progress and what remains unproven.
    """
    previous = {}

    def _handler(signum, _frame):
        raise _DogfoodInterrupted(f"signal-{signum}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, _handler)
        except Exception:
            pass

    def restore():
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except Exception:
                pass

    return restore


def _vision(persona: str) -> str:
    """The Explorer's VISION: the product's north star seen through this persona's unforgiving lens."""
    return (
        "agent-os: every user is a CEO whose entire company is AI agents. The console must be "
        "indistinguishable from running a real elite company — frustration-free, honest, zero bugs "
        "reaching a human, quality that astonishes a skeptic (docs/NORTH-STAR.md).\n"
        f"YOU ARE: {PERSONAS.get(persona, PERSONAS[_ORDER[0]])}\n"
        "Judge every step of every journey against that bar. Anything that would make this user "
        "frustrated, confused, or distrustful is a bug with a real severity.")


# ─────────────────────────────────────────────────────────────────────────────────────────────────────
# The REAL journeys (the verdict's mandated arc) — each one is an Explorer story. `fresh: True` runs in
# a tokenless browser session (the real signup form); the rest run signed-in as the seeded tenant.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _journeys(run_id: str) -> list:
    email = f"dogfood+{run_id}@example.test"
    password = f"Dogfood!{run_id}"
    return [
        {"id": "J1-signup-verify", "fresh": True,
         "title": "Sign up and verify email (brand-new user)",
         "goal": f"Create a brand-new account through the real signup form: name 'Dogfood {run_id}', "
                 f"email {email}, password {password}. This self-hosted install shows the 6-digit "
                 f"verification code ON SCREEN after signup — read it from the page and enter it to "
                 f"verify. Judge the whole getting-in arc.",
         "steps": ["open the console signed-out", "switch to the sign-up form", "fill name/email/password",
                   "submit", "read the on-screen verification code", "enter the code", "land signed in"],
         "expected_outcome": "Signup succeeds with clear feedback, the verification code is presented and "
                             "accepted, and the new user lands signed-in with an obvious next step — no "
                             "dead-end, no raw error, no ambiguity."},
        {"id": "J2-create-company",
         "title": "Create a company",
         "goal": "As the signed-in CEO, create a NEW company (org) named 'Dogfood Ventures' with a "
                 "one-line vision, wherever the console offers company/org creation.",
         "steps": ["find company/org creation", "enter the name and a vision", "create it",
                   "confirm it appears and is selectable"],
         "expected_outcome": "The company is created with confirmation, appears in the company/org "
                             "switcher or list, and can be selected as the active company."},
        {"id": "J3-direct-build",
         "title": "Direct the org to build a product",
         "goal": "Direct your AI org to build a product: describe 'a one-page landing site for a "
                 "dog-walking service, with a booking enquiry form' to the assistant/controller and "
                 "confirm the kickoff when asked.",
         "steps": ["open the assistant/controller", "describe the product", "confirm/approve the kickoff",
                   "note what the org promises (ETA, progress, where to watch)"],
         "expected_outcome": "The build kicks off with an explicit acknowledgement, an ETA or progress "
                             "affordance, and a pointer to where progress is visible — never a silent "
                             "no-op or a fake instant 'done'."},
        {"id": "J4-wait-watch",
         "title": "Wait and watch the org work",
         "goal": "You are the CEO WAITING on work you just directed. Check the cockpit/projects/activity "
                 "surfaces for live progress on the build.",
         "steps": ["open the cockpit or projects view", "find the running work", "watch its status",
                   "look for a stop/cancel control and a live activity signal"],
         "expected_outcome": "Live, truthful status is visible (stage/progress/activity), a stop or "
                             "cancel affordance exists, and nothing claims 'done' while work is still "
                             "running."},
        {"id": "J5-review-respond",
         "title": "Review what the org produced/asked and respond",
         "goal": "Review what your org has produced or is asking of you (notifications, approvals, the "
                 "assistant thread) and RESPOND — reply in the thread or decide a pending approval.",
         "steps": ["check notifications/approvals/the thread", "open one item and read it fully",
                   "respond (a reply or a decision)", "confirm the response was registered"],
         "expected_outcome": "The CEO can see everything awaiting them, every artifact/message opens and "
                             "is readable, and a response is accepted and visibly acknowledged — nothing "
                             "is dropped on the floor."},
    ]


CRITICAL_COVERAGE = {
    "J1-signup-verify": ["complete signup and verify email successfully"],
    "J2-create-company": ["create a new company and confirm it is selectable"],
    "J3-direct-build": ["direct the assistant to start a product build and see an acknowledgement"],
    "J4-wait-watch": ["find truthful running work status, activity, and stop/cancel affordance"],
    "J5-review-respond": ["open a real item awaiting the CEO and confirm a response is registered"],
}


def _depth_stories(stories: list[dict]) -> list[dict]:
    """Live finish-line dogfood must reach every mandated CEO journey before spending time on all edge cases.
    `critical` supplies an explicit coverage ledger for the core path; `deep` keeps the AI-generated broad
    checklist for nightly/audit exploration."""
    if DOGFOOD_DEPTH in ("deep", "full", "broad"):
        return stories
    out = []
    for s in stories:
        d = dict(s)
        cov = CRITICAL_COVERAGE.get(d.get("id"))
        if cov:
            d["coverage"] = cov
            d["goal"] = (d.get("goal") or "") + (
                "\n\nLIVE FINISH-LINE MODE: exercise the critical happy path for this story first. "
                "Do not spend this pass on edge/error variants; deep dogfood covers those separately.")
        out.append(d)
    return out


# The journeys the pass MUST cover (asserted by the selftest — the mandate can't silently shrink).
REQUIRED_JOURNEYS = ("J1-signup-verify", "J2-create-company", "J3-direct-build",
                     "J4-wait-watch", "J5-review-respond")


# ─────────────────────────────────────────────────────────────────────────────────────────────────────
# Live-console plumbing: health + tenant seeding (like scripts/console_actions_e2e.sh).
# ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _console_up(base: str = BASE) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=4) as r:
            return r.status == 200 and b"console" in r.read()
    except Exception:
        return False


def _seed_tenant(base: str, persona: str) -> tuple:
    """billing.signup + tenantproviders.connect + /api/orgs/new + /api/settings/consent -> (token, org).
    The pass owns its tenant end-to-end, so signed-in journeys never depend on operator state."""
    import billing
    import tenantproviders
    r = billing.signup(f"Dogfood-{persona}", "free")
    tok, tid = r["api_token"], r["tenant_id"]
    try:
        tenantproviders.connect(tid, "anthropic", "subscription")
    except Exception:
        pass

    def _post(path, body):
        req = urllib.request.Request(
            f"{base}{path}", data=json.dumps(body).encode(),
            headers={"X-Tenant-Token": tok, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()

    org = "0"
    try:
        import re
        out = _post("/api/orgs/new", {"name": "Dogfood Co", "vision": "the daily acceptance pass"})
        m = re.search(r'"(?:org_id|id|org)"\s*:\s*"?(\d+)"?', out) or re.search(r"(\d+)", out)
        if m:
            org = m.group(1)
    except Exception:
        pass
    try:
        _post("/api/settings/consent", {"accept": True})
    except Exception:
        pass
    return tok, org, tid


# ─────────────────────────────────────────────────────────────────────────────────────────────────────
# Finding + alert wiring: TRUE severity, signature dedup, immediate blocking alert.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _true_severity(sev: str) -> str:
    """Preserve the explorer's TRUE severity. Only 'medium' is renamed to the house 'med'; critical is
    NEVER downgraded (the old pass squashed blocker/critical to high — that lie is what let blockers
    sit in a weekly queue)."""
    s = (sev or "med").strip().lower()
    if s == "medium":
        return "med"
    if s in ("critical", "high", "med", "low"):
        return s
    if s in ("none", ""):
        return "low"
    return "med"


def _norm_sig(text: str) -> str:
    """A stable signature for dedup: lowercase alphanumerics of the first 80 chars."""
    return "".join(ch for ch in (text or "").lower()[:80] if ch.isalnum())


def _already_open(title: str) -> bool:
    """Signature dedup against the open findings backlog — the same defect found on consecutive daily
    runs must not pile up as N copies. Fail-open (a dedup error must never swallow a finding)."""
    import findings
    try:
        sig = _norm_sig(title)
        return any(_norm_sig(f.get("title", "")) == sig for f in findings.open_findings())
    except Exception:
        return False


def _file_bug(persona: str, story: dict, bug: dict, counters: dict, run_id: str):
    """File ONE explorer bug via findings.py (true severity, deduped) and, if it BLOCKS the journey,
    raise an owned alert IMMEDIATELY — this runs inside the explorer's on_bug callback, so a blocker
    alerts within minutes of being seen, not at the end of the pass."""
    import alerts
    import findings
    role = _ROLE_FOR.get(persona, "builder")
    sev = _true_severity(bug.get("severity"))
    blocking = bool(bug.get("blocking"))
    title = f"[dogfood/{persona}] {story.get('id', '?')}: {(bug.get('bug') or 'unspecified defect')[:110]}"
    detail = (
        f"persona: {persona}\nstory: {story.get('title', '')} ({story.get('id', '')})\n"
        f"goal: {story.get('goal', '')}\n"
        f"expected: {bug.get('expected', '')}\n"
        f"actual (bug): {bug.get('bug', '')}\n"
        f"url: {bug.get('url', '')}\naction: {json.dumps(bug.get('action') or {})}\n"
        f"screenshot: {bug.get('shot', '')}\nblocking: {blocking}\nrun: {run_id}\n"
        f"found by: qa_explorer (qa-security) driving the LIVE console — standing daily acceptance pass")
    fid = None
    if _already_open(title):
        counters["deduped"] += 1
    else:
        try:
            r = findings.file(SOURCE, role, title, detail, severity=sev,
                              tenant_id="_platform")
            fid = r.get("finding_id")
            counters["filed"] += 1
        except Exception as e:
            counters["file_errors"] += 1
            print(f"[dogfood] findings.file failed for {title!r}: {e}", file=sys.stderr)
    if blocking:
        try:
            alerts.raise_alert(
                SOURCE, role,
                f"BLOCKING dogfood finding ({sev}) on the LIVE console — {title}"
                + (f" [finding #{fid}]" if fid else ""),
                severity="critical" if sev == "critical" else "high",
                signature=f"dogfood:{_norm_sig(title)}")
            counters["alerts"] += 1
        except Exception as e:
            counters["alert_errors"] += 1
            print(f"[dogfood] alerts.raise_alert failed: {e}", file=sys.stderr)


def _console_get(base: str, token: str, path: str) -> dict:
    req = urllib.request.Request(f"{base}{path}", headers={"X-Tenant-Token": token}, method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode() or "{}")


def _console_post(base: str, token: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{base}{path}", data=data,
                                 headers={"X-Tenant-Token": token, "Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode() or "{}")


def _record_finding_verification(finding_id: int, kind: str, passed: bool, evidence: dict) -> dict:
    import findings
    rec = findings.record_verification(finding_id, kind, passed, evidence, by="dogfood")
    if passed:
        resolved = findings.resolve(finding_id, by="dogfood", verification_id=rec["verification_id"])
        rec["resolved"] = bool(resolved.get("resolved"))
    return rec


def _next_persona() -> str:
    """Rotate by pass count so a different persona drives each daily run."""
    try:
        import audit
        n = audit.count(action="DogfoodPass")
    except Exception:
        n = 0
    return _ORDER[n % len(_ORDER)]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _artifact_ts(run_dir: Path) -> float:
    paths = [run_dir]
    paths += list(run_dir.glob("summary.json"))
    paths += list(run_dir.glob("*/result.json"))
    paths += list(run_dir.glob("*/checkpoint.json"))
    try:
        return max(p.stat().st_mtime for p in paths if p.exists())
    except Exception:
        return 0.0


def _has_browser_evidence(run_dir: Path) -> bool:
    patterns = ("*/screenshots/*.png", "*/videos/*.webm", "*/videos/*.mp4")
    return any(next(run_dir.glob(pat), None) is not None for pat in patterns)


def _story_dir_has_browser_evidence(story_dir: Path) -> bool:
    patterns = ("screenshots/*.png", "videos/*.webm", "videos/*.mp4")
    return any(next(story_dir.glob(pat), None) is not None for pat in patterns)


def _artifact_root() -> Path | None:
    try:
        import artifacts
        return Path(os.environ.get("AOS_DOGFOOD_EVIDENCE_ROOT", artifacts.root() / "dogfood-first-run"))
    except Exception:
        configured = os.environ.get("AOS_DOGFOOD_EVIDENCE_ROOT")
        return Path(configured) if configured else None


def _last_artifact_run(persona: str = None) -> dict:
    """Newest real dogfood artifact, including interrupted/partial runs.

    Audit rows are still useful, but they only record what the process got far enough to append. The artifact
    directory is the ground truth for live browser evidence, including partial runs that stop mid-journey.
    """
    root = _artifact_root()
    if not root or not root.exists():
        return {}
    candidates = []
    run_dirs = [p for p in root.iterdir() if p.is_dir()]
    run_dirs.sort(key=lambda p: p.name, reverse=True)
    for run_dir in run_dirs[:120]:
        if not run_dir.is_dir() or not (run_dir / "run-input.json").exists():
            continue
        run_input = _read_json(run_dir / "run-input.json") or {}
        summary = _read_json(run_dir / "summary.json") or {}
        run_persona = run_input.get("persona") or summary.get("persona")
        if persona and persona != "all" and run_persona != persona:
            continue
        # Ignore zero-duration selftest-style fixture summaries unless they contain actual browser evidence.
        if not _has_browser_evidence(run_dir) and int(summary.get("elapsed_s") or 0) <= 0:
            continue
        candidates.append(run_dir)
    if not candidates:
        return {}
    run_dir = max(candidates, key=_artifact_ts)
    run_input = _read_json(run_dir / "run-input.json") or {}
    summary = _read_json(run_dir / "summary.json") or {}
    stories = _read_json(run_dir / "stories.json") or []
    ids = [s.get("id") for s in stories if s.get("id")]
    status = dict(summary.get("story_status") or {})
    bugs = int(summary.get("bugs") or 0)
    if not summary:
        bugs = 0
    for rp in sorted(run_dir.glob("*/result.json")):
        res = _read_json(rp) or {}
        sid = res.get("story")
        if sid:
            status[sid] = res.get("status") or status.get(sid) or "unknown"
        if not summary:
            bugs += len(res.get("bugs") or [])
    for sid in ids:
        if sid in status:
            continue
        story_dirs = sorted(run_dir.glob(f"*-{sid}"))
        has_story_evidence = any(_story_dir_has_browser_evidence(d) for d in story_dirs)
        status[sid] = "interrupted" if has_story_evidence else "unproven"
    ids = ids or sorted(status)
    passed = sum(1 for sid in ids if status.get(sid) == "passed")
    total = len(ids) or len(status)
    return {"source": "artifact", "run": run_input.get("run") or summary.get("run"),
            "persona": run_input.get("persona") or summary.get("persona"),
            "stories": summary.get("stories"), "bugs": bugs, "passed": passed, "total": total,
            "story_status": status, "evidence_dir": str(run_dir), "artifact_dir": run_dir.name,
            "interrupted": bool(summary.get("interrupted")) or any(v == "interrupted" for v in status.values()),
            "incomplete": bool(total and passed < total)}


def _last_run(persona: str = None) -> dict:
    artifact = _last_artifact_run(persona)
    if artifact:
        return artifact
    try:
        from dbpool import connection
        with connection() as c, c.cursor() as cur:
            if persona and persona != "all":
                cur.execute("""SELECT payload, ts FROM audit_log
                               WHERE actor='dogfood' AND action='DogfoodPass'
                                 AND payload->>'persona'=%s
                               ORDER BY id DESC LIMIT 1""", (persona,))
            else:
                cur.execute("""SELECT payload, ts FROM audit_log
                               WHERE actor='dogfood' AND action='DogfoodPass'
                               ORDER BY id DESC LIMIT 1""")
            row = cur.fetchone()
        if not row:
            return {}
        payload, ts = row
        age_h = None
        try:
            age_h = round((datetime.now(timezone.utc) - ts).total_seconds() / 3600, 1)
        except Exception:
            pass
        ss = (payload or {}).get("story_status", {}) if isinstance(payload, dict) else {}
        passed = sum(1 for v in ss.values() if v == "passed")
        total = len(ss)
        return {"source": "audit", "ts": str(ts), "age_hours": age_h, "persona": (payload or {}).get("persona"),
                "stories": (payload or {}).get("stories"), "bugs": (payload or {}).get("bugs"),
                "passed": passed, "total": total, "story_status": ss,
                "evidence_dir": (payload or {}).get("evidence_dir"),
                "interrupted": bool((payload or {}).get("interrupted")) or any(v == "interrupted" for v in ss.values()),
                "incomplete": bool(total and passed < total)}
    except Exception as e:
        return {"error": str(e)[:160]}


def _open_dogfood_findings(limit=8) -> dict:
    try:
        import findings
        rows = [f for f in findings.open_findings() if str(f.get("source") or "") == SOURCE]
    except Exception as e:
        return {"error": str(e)[:160], "total": 0, "blocking": 0, "items": []}
    sev_rank = {"critical": 0, "high": 1, "med": 2, "medium": 2, "low": 3}
    rows.sort(key=lambda f: (sev_rank.get(str(f.get("severity") or "").lower(), 9),
                             -int(f.get("age_hours") or 0)))
    blocking = [f for f in rows if str(f.get("severity") or "").lower() in ("critical", "high")]
    return {
        "total": len(rows),
        "blocking": len(blocking),
        "allow_override": ALLOW_OPEN_FINDINGS,
        "items": [{"id": f.get("id"), "severity": f.get("severity"), "status": f.get("status"),
                   "owner": f.get("owner"), "age_hours": f.get("age_hours"),
                   "title": f.get("title")} for f in rows[:limit]],
    }


def preflight(persona: str = None, base: str = BASE) -> dict:
    """No-spend gate before a live dogfood run.

    It checks readiness and prints the exact live-run plan so a human/agent does not burn browser/model time
    without a bounded budget, artifact target, monitoring surfaces, and stop conditions. It does not seed a
    tenant, launch Playwright, or call any model.
    """
    selected = persona or _next_persona()
    valid_persona = selected in PERSONAS or selected == "all"
    personas = _ORDER if selected == "all" else ([selected] if selected in PERSONAS else [])
    stories = _depth_stories(_journeys("preflight"))
    required_ok = [s["id"] for s in stories] == list(REQUIRED_JOURNEYS)
    standards = SCRIPTS.parent / "docs" / "STANDARDS-acceptance.md"
    explorer = QA / "qa_explorer.py"
    budget_ok = BUDGET_S >= 300 and MAX_STEPS >= len(REQUIRED_JOURNEYS)
    console_ok = _console_up(base)
    qa_idle = not _qa_active()
    last_run = _last_run(selected if selected != "all" else None)
    open_findings = _open_dogfood_findings()
    known_blockers_clear = (open_findings.get("blocking", 0) == 0) or ALLOW_OPEN_FINDINGS
    checks = {
        "valid_persona": valid_persona,
        "required_journeys_intact": required_ok,
        "acceptance_standard_present": standards.exists(),
        "qa_explorer_present": explorer.exists(),
        "budget_bounds_sane": budget_ok,
        "console_health_ok": console_ok,
        "qa_browser_capacity_idle": qa_idle,
        "known_dogfood_blockers_clear": known_blockers_clear,
    }
    run_arg = "" if persona is None else f" {selected}"
    command = f"{sys.executable} {Path(__file__).resolve()} run{run_arg}"
    plan = {
        "ok": all(checks.values()),
        "base": base,
        "persona": selected,
        "personas_to_run": personas,
        "depth": DOGFOOD_DEPTH if DOGFOOD_DEPTH else "critical",
        "command": command,
        "budget_s_per_persona": BUDGET_S,
        "max_steps_per_story": MAX_STEPS,
        "journeys": [{"id": s["id"], "title": s["title"], "fresh": bool(s.get("fresh")),
                      "coverage": s.get("coverage")}
                     for s in stories],
        "artifacts": {
            "root": "artifacts.run_dir('dogfood-<persona>', started)",
            "required_files": ["run-input.json", "stories.json", "events.jsonl", "summary.json",
                               "NN-<journey-id>/result.json"],
        },
        "monitoring": [
            "console /health before launch",
            "artifact events.jsonl during the run",
            "findings.open_findings() for filed defects",
            "alerts.raise_alert() for blocking findings",
            f"{LOG} when launched through cron",
        ],
        "stop_conditions": [
            "console health is down",
            "a real QA/browser run is already active",
            "per-persona BUDGET_S wall-clock expires",
            "Explorer/browser crash files a critical blocking finding",
            "blocking journey findings alert immediately; fix before claiming 5/5",
        ],
        "checks": checks,
        "last_run": last_run,
        "open_dogfood_findings": open_findings,
        "override": {
            "allow_open_findings_env": "AOS_DOGFOOD_ALLOW_OPEN_FINDINGS=1",
            "use_only_for": "a deliberate verification run after reviewing known open dogfood blockers",
        },
    }
    return plan


# ─────────────────────────────────────────────────────────────────────────────────────────────────────
# One persona pass: seed a tenant, drive every journey through the live console via the Explorer.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────
def run_once(persona: str = None, base: str = BASE) -> dict:
    import audit
    import artifacts
    import qa_explorer
    os.environ.setdefault("AOS_QA_DISABLE_TRANSCODE", "1")
    if "AOS_QA_STALL_STEPS" not in os.environ:
        qa_explorer._STALL_LIMIT = int(os.environ.get("AOS_DOGFOOD_STALL_STEPS", "10"))
    persona = persona if persona in PERSONAS else (persona or _next_persona())
    if persona not in PERSONAS:
        persona = _next_persona()
    run_id = uuid.uuid4().hex[:6]
    counters = {"filed": 0, "deduped": 0, "alerts": 0, "file_errors": 0, "alert_errors": 0}
    started = time.time()
    evidence_dir = artifacts.run_dir(f"dogfood-{persona}", started)
    events_path = evidence_dir / "events.jsonl"
    restore_interrupts = _install_interrupt_handlers()

    def _write_json(name, payload):
        try:
            (evidence_dir / name).write_text(json.dumps(payload, indent=2, default=str))
        except Exception:
            pass

    def _event(kind, payload=None):
        try:
            with events_path.open("a") as f:
                f.write(json.dumps({"ts": time.time(), "kind": kind, "data": payload or {}},
                                   default=str) + "\n")
        except Exception:
            pass

    def _audit_summary(decision, summary):
        _write_json("summary.json", summary)
        _event("summary", summary)
        audit.append(actor=SOURCE, action="DogfoodPass", resource=persona, decision=decision,
                     payload=summary)
        return summary

    def _build_story_result(story, story_dir, collected, ex, status=None, stop_reason=None):
        coverage = (getattr(ex, "coverage", None) if ex is not None else []) or []
        return {"story": story.get("id"), "title": story.get("title"),
                "status": status or story_status.get(story["id"]), "bugs": collected,
                "artifact_dir": str(story_dir),
                "stop_reason": stop_reason or (getattr(ex, "stop_reason", None) if ex is not None else None),
                "remaining_coverage": [c.get("aspect") for c in coverage if not c.get("covered")]}

    def _write_story_result(story_dir, story_result):
        try:
            (story_dir / "result.json").write_text(json.dumps(story_result, indent=2, default=str))
        except Exception:
            pass
        _event("story_done", story_result)

    _write_json("run-input.json", {
        "persona": persona, "run": run_id, "base": base,
        "budget_s": BUDGET_S, "max_steps": MAX_STEPS,
        "vision": _vision(persona),
        "depth": DOGFOOD_DEPTH if DOGFOOD_DEPTH else "critical",
    })

    # The live console being DOWN is itself a blocking product finding — file + alert, don't skip.
    if not _console_up(base):
        _file_bug(persona, {"id": "J0-availability", "title": "The console is reachable",
                            "goal": f"open the live console at {base}"},
                  {"bug": f"the live console at {base} is DOWN/unhealthy — no user can do anything",
                   "severity": "critical", "blocking": True, "expected": "console serves /health",
                   "url": base}, counters, run_id)
        summary = {"persona": persona, "run": run_id, "console_up": False, "stories": 0, "bugs": 1,
                   "evidence_dir": str(evidence_dir), **counters}
        try:
            return _audit_summary("console_down", summary)
        finally:
            restore_interrupts()

    tok, org, tid = _seed_tenant(base, persona)
    vision = _vision(persona)
    stories = _depth_stories(_journeys(run_id))
    _write_json("stories.json", stories)
    bugs_found = 0
    stories_run = 0
    story_status = {}
    story_results = []
    current_story = None
    current_story_dir = None
    current_collected = None
    current_ex = None
    # Fair-share the budget ACROSS stories so a bounded run SAMPLES every journey instead of one deep
    # journey eating the whole budget (seen live: J1 consumed all 991s, J2-J5 were skipped-budget). Give
    # each story a fair slice of the REMAINING budget (computed per-story, not once) so the LAST journey —
    # J5 review-respond, which kept getting skipped-budget because earlier stories each overran their fixed
    # slice by one step — always gets whatever is left instead of zero. Each step is a slow model call.
    for idx, story in enumerate(stories):
        elapsed = time.time() - started
        if elapsed >= BUDGET_S:                   # graceful budget stop — never a hard kill mid-story
            story_status[story["id"]] = "skipped-budget"
            continue
        remaining_stories = len(stories) - idx
        per_story = max(90, (BUDGET_S - elapsed) / remaining_stories)
        token = None if story.get("fresh") else tok
        ex = None
        collected = []
        story_dir = evidence_dir / f"{idx + 1:02d}-{story['id']}"
        story_dir.mkdir(parents=True, exist_ok=True)
        _write_json(f"{idx + 1:02d}-{story['id']}.input.json", story)
        current_story, current_story_dir, current_collected, current_ex = story, story_dir, collected, None

        def cb(bug, _s=story):
            collected.append(bug)
            _event("bug", {"story": _s.get("id"), "bug": bug})
            _file_bug(persona, _s, bug, counters, run_id)

        try:
            _event("story_start", {"story": story.get("id"), "title": story.get("title"),
                                   "artifact_dir": str(story_dir)})
            try:
                ex = qa_explorer.Explorer(base, vision, token=token, org=str(org),
                                          artifact_dir=story_dir)
            except TypeError:
                ex = qa_explorer.Explorer(base, vision, token=token, org=str(org))
            current_ex = ex
            # per-story deadline (its fair slice) AND the global cap — whichever comes first — so every
            # story gets a turn and the whole run still stops on time.
            ex.explore(story, max_steps=MAX_STEPS, on_bug=cb,
                       deadline=min(started + BUDGET_S, time.time() + per_story))
            stop_reason = getattr(ex, "stop_reason", None)
            remaining = [c for c in (getattr(ex, "coverage", None) or []) if not c.get("covered")]
            incomplete = bool((stop_reason and "incomplete" in str(stop_reason)) or remaining)
            if incomplete and not collected:
                bug = {
                    "bug": "acceptance explorer could not complete the critical-path journey"
                           + (f" ({stop_reason})" if stop_reason else ""),
                    "severity": "high",
                    "blocking": True,
                    "expected": "the live critical-path journey completes with all coverage items proven",
                    "url": base,
                    "action": {"cmd": "explore", "story": story.get("id")},
                    "shot": "",
                    "remaining_coverage": [c.get("aspect") for c in remaining],
                }
                collected.append(bug)
                _event("bug", {"story": story.get("id"), "bug": bug})
                _file_bug(persona, story, bug, counters, run_id)
            story_status[story["id"]] = "blocked" if any(b.get("blocking") for b in collected) \
                else ("failed" if collected else ("incomplete" if incomplete else "passed"))
        except _DogfoodInterrupted as e:
            reason = str(e) or "interrupted"
            story_status[story["id"]] = "interrupted"
            story_result = _build_story_result(story, story_dir, collected, ex,
                                               status="interrupted", stop_reason=reason)
            story_results.append(story_result)
            _write_story_result(story_dir, story_result)
            bugs_found += len(collected)
            stories_run += 1
            for remaining_story in stories[idx + 1:]:
                story_status.setdefault(remaining_story["id"], "skipped-interrupted")
            summary = {"persona": persona, "run": run_id, "console_up": True, "tenant": tid,
                       "org": str(org), "stories": stories_run, "story_status": story_status,
                       "bugs": bugs_found, "elapsed_s": int(time.time() - started),
                       "evidence_dir": str(evidence_dir), "story_results": story_results,
                       "interrupted": True, "interrupt_reason": reason, **counters}
            _event("interrupted", {"story": story.get("id"), "reason": reason})
            try:
                return _audit_summary("interrupted", summary)
            finally:
                restore_interrupts()
        except Exception as e:                     # an explorer/browser crash is itself a blocking finding
            _file_bug(persona, story,
                      {"bug": f"the acceptance explorer crashed mid-journey: {e}",
                       "severity": "critical", "blocking": True,
                       "expected": "the journey is explorable end-to-end", "url": base},
                      counters, run_id)
            collected.append({"blocking": True})
            story_status[story["id"]] = "crashed"
        finally:
            if ex is not None:
                try:
                    ex.close()
                except Exception:
                    pass
        story_result = _build_story_result(story, story_dir, collected, ex)
        story_results.append(story_result)
        _write_story_result(story_dir, story_result)
        bugs_found += len(collected)
        stories_run += 1
        current_story, current_story_dir, current_collected, current_ex = None, None, None, None

    summary = {"persona": persona, "run": run_id, "console_up": True, "tenant": tid, "org": str(org),
               "stories": stories_run, "story_status": story_status, "bugs": bugs_found,
               "elapsed_s": int(time.time() - started), "evidence_dir": str(evidence_dir),
               "story_results": story_results, **counters}
    try:
        return _audit_summary("filed", summary)
    finally:
        restore_interrupts()


def run_all(base: str = BASE) -> list:
    """Every persona in one pass (the 'per-deploy / big-audit' mode)."""
    return [run_once(p, base=base) for p in _ORDER]


def qa_explore(persona: str = None, base: str = BASE) -> dict:
    """Guarded live-proof command for the north-star scorecard.

    `northstar_accept.py` lists `dogfood.py qa-explore` as the skeptic finish-line command; keep that command
    real. It runs the no-spend preflight first and refuses to launch the browser/model explorer if prerequisites
    are red. Default persona is `first-run`, matching the current live evidence line and the five-minute skeptic
    journey; callers can pass another persona explicitly.
    """
    selected = persona or "first-run"
    pf = preflight(selected, base=base)
    if not pf.get("ok"):
        return {"blocked": "preflight", "persona": selected, "preflight": pf}
    out = run_once(selected, base=base)
    out["preflight"] = {"ok": True, "command": pf.get("command"), "budget_s": pf.get("budget_s_per_persona")}
    return out


def verify_finding_515(base: str = BASE, use_http: bool | None = None, record: bool = True) -> dict:
    """Focused live verifier for dogfood #515 with no model spend.

    Recreates the exact false-idle shape: a CEO/org exists, there are zero product rows, but controller_state
    has an active workstream. Every owner-facing status surface must acknowledge that work instead of rendering
    a quiet/idle company. If the console is up, this uses the real HTTP API; selftests can force direct module
    mode with use_http=False.
    """
    import billing
    import chiefofstaff
    import cockpit
    import livestatus
    import loopcontroller
    import orgs
    import projectsview
    import traceview
    from dbpool import connection

    started = time.time()
    run_id = uuid.uuid4().hex[:6]
    signup = billing.signup(f"dogfood-515-{run_id}", "free")
    tid = signup["tenant_id"]
    token = signup.get("api_token")
    org_id = 0
    thread = 950000000 + int(run_id, 16)
    evidence: dict = {
        "finding_id": 515,
        "run": run_id,
        "base": base,
        "tenant_id": tid,
        "thread_id": thread,
        "checks": {},
        "surfaces": {},
    }
    try:
        if not token:
            with connection() as c, c.cursor() as cur:
                cur.execute("SELECT api_token FROM tenants WHERE tenant_id=%s", (tid,))
                token = cur.fetchone()[0]
        org = orgs.create(tid, "Dogfood 515 verifier", "Verify active work never renders as idle")
        org_id = int(org["org_id"])
        evidence["org_id"] = org_id

        loopcontroller._ensure()
        chiefofstaff._cache_put(tid, org_id, {"headline": "Team is ready and idle.",
                                              "needs_you": [], "team_did": [], "watch": [],
                                              "suggestion": "Start something."})
        with connection() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                              (thread_id, tenant_id, org_id, phase, product, awaiting,
                               job_kind, job_status, updated_at, execution_scope)
                           VALUES (%s,%s,%s,'IMPLEMENT',NULL,'fleet','build',
                                   'building the first product', now(),'test')""",
                        (thread, tid, org_id))

        http = _console_up(base) if use_http is None else bool(use_http)
        evidence["used_http_console"] = bool(http)
        if http:
            cp = _console_get(base, token, f"/api/cockpit?org={org_id}")
            projects = _console_get(base, token, f"/api/projects?org={org_id}").get("projects", [])
            live = _console_get(base, token, "/api/livestatus").get("products", [])
            obs = _console_get(base, token, f"/api/observability?org={org_id}")
            brief = _console_get(base, token, f"/api/brief?org={org_id}")
        else:
            cp = cockpit.cockpit(tid, org_id)
            projects = projectsview.list_projects(tid, org_id)
            live = livestatus.live_status(tid)
            obs = traceview.overview(tid, org_id)
            brief = chiefofstaff.brief(tid, org_id, use_cache=True)

        live_row = next((r for r in live if r.get("thread_id") == thread), None)
        project_row = next((p for p in projects if p.get("thread_id") == thread), None)
        activity_row = (obs.get("recent_activity") or [{}])[0]
        brief_text = " ".join(str(x) for x in [
            brief.get("headline"), *(brief.get("team_did") or []), brief.get("suggestion")
        ])
        checks = {
            "no_product_rows": (cp.get("summary") or {}).get("products") == 0,
            "cockpit_in_flight": (cp.get("summary") or {}).get("in_flight_workstreams", 0) >= 1
                                  and (cp.get("queue") or {}).get("active", 0) >= 1,
            "cockpit_can_cancel": any(w.get("thread_id") == thread and w.get("can_cancel")
                                      for w in cp.get("workstreams") or []),
            "projects_provisional": bool(project_row and project_row.get("provisional")
                                          and project_row.get("result") == "building"),
            "projects_can_cancel": bool(project_row and project_row.get("can_cancel")),
            "livestatus_running": bool(live_row and live_row.get("provisional") and live_row.get("running")),
            "observability_activity": obs.get("in_flight_workstreams", 0) >= 1
                                      and activity_row.get("workstream") is True
                                      and activity_row.get("thread_id") == thread,
            "observability_can_cancel": activity_row.get("workstream") is True
                                        and activity_row.get("thread_id") == thread
                                        and activity_row.get("can_cancel") is True,
            "brief_not_idle": "active workstream" in brief_text and "Team is ready and idle" not in brief_text,
        }
        evidence["checks"] = checks
        evidence["surfaces"] = {
            "cockpit_summary": cp.get("summary"),
            "cockpit_queue": cp.get("queue"),
            "cockpit_workstreams": cp.get("workstreams"),
            "project_row": project_row,
            "livestatus_row": live_row,
            "observability_activity": activity_row,
            "observability_in_flight_workstreams": obs.get("in_flight_workstreams"),
            "brief_headline": brief.get("headline"),
            "brief_team_did": brief.get("team_did"),
        }
        evidence["elapsed_s"] = round(time.time() - started, 2)
        passed = all(checks.values())
        out = {"ok": passed, "evidence": evidence}
        if record:
            out["verification"] = _record_finding_verification(515, "dogfood-515-focused", passed, evidence)
        return out
    finally:
        try:
            with connection() as c, c.cursor() as cur:
                cur.execute("DELETE FROM brief_cache WHERE tenant_id=%s", (tid,))
                cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread,))
                cur.execute("DELETE FROM orgs WHERE tenant_id=%s", (tid,))
                cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
        except Exception:
            pass


def verify_j5_review_respond(base: str = BASE, use_http: bool | None = None) -> dict:
    """Focused no-model proof for J5: CEO opens a real awaiting item and answers it.

    The representative path is an agent_request question because it is the generic agent->CEO handoff:
    a worker needs human input, Approvals must surface it, the CEO answers, and the durable request row must
    flip to answered so the blocked worker can resume.
    """
    import agent_request
    import approvals
    import billing
    from dbpool import connection

    started = time.time()
    run_id = uuid.uuid4().hex[:6]
    signup = billing.signup(f"dogfood-j5-{run_id}", "free")
    tid = signup["tenant_id"]
    token = signup.get("api_token")
    rid = None
    answer_text = f"Use the Northwest region for dogfood verifier {run_id}."
    evidence = {
        "run": run_id,
        "base": base,
        "tenant_id": tid,
        "checks": {},
        "surfaces": {},
    }
    try:
        if not token:
            with connection() as c, c.cursor() as cur:
                cur.execute("SELECT api_token FROM tenants WHERE tenant_id=%s", (tid,))
                token = cur.fetchone()[0]
        ask = agent_request.ask(tid, "Which launch region should the team use?", kind="decision")
        rid = ask["request_id"]
        evidence["request_id"] = rid

        http = _console_up(base) if use_http is None else bool(use_http)
        evidence["used_http_console"] = bool(http)
        if http:
            before = _console_get(base, token, "/api/approvals")
            item = next((i for i in before.get("items", [])
                         if i.get("kind") == "question" and str(i.get("ref")) == str(rid)), None)
            answer_res = _console_post(base, token, "/api/requests/answer",
                                       {"ref": rid, "text": answer_text})
            after = _console_get(base, token, "/api/approvals")
        else:
            before = approvals.inbox(tid)
            item = next((i for i in before.get("items", [])
                         if i.get("kind") == "question" and str(i.get("ref")) == str(rid)), None)
            answer_res = agent_request.answer(rid, answer_text, tenant_id=tid)
            after = approvals.inbox(tid)

        row = agent_request.get(rid, tenant_id=tid)
        still_open = any(i.get("kind") == "question" and str(i.get("ref")) == str(rid)
                         for i in after.get("items", []))
        checks = {
            "approvals_surfaces_question": bool(item),
            "question_readable": bool(item and item.get("title") and item.get("detail")),
            "answer_post_ok": (answer_res or {}).get("status") == "answered",
            "request_row_answered": bool(row and row.get("status") == "answered"
                                         and row.get("answer") == answer_text),
            "approvals_clears_answered_question": not still_open,
        }
        evidence["checks"] = checks
        evidence["surfaces"] = {
            "before_count": before.get("count"),
            "question_item": item,
            "answer_result": answer_res,
            "after_count": after.get("count"),
            "answered_row": row,
        }
        evidence["elapsed_s"] = round(time.time() - started, 2)
        return {"ok": all(checks.values()), "evidence": evidence}
    finally:
        try:
            with connection() as c, c.cursor() as cur:
                if rid is not None:
                    cur.execute("DELETE FROM agent_requests WHERE id=%s", (rid,))
                cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
        except Exception:
            pass


def verify_critical_readiness(base: str = BASE, use_http: bool | None = None) -> dict:
    """No-model verifier bundle before a spendful full first-run dogfood pass.

    This does not replace `qa-explore first-run`; it proves the known J4/J5 API affordances cheaply so the
    next browser/model run is checking end-to-end UX continuity, not rediscovering missing backend contracts.
    """
    started = time.time()
    j4 = verify_finding_515(base=base, use_http=use_http, record=False)
    j5 = verify_j5_review_respond(base=base, use_http=use_http)
    checks = {
        "j4_wait_watch_status_and_cancel": bool(j4.get("ok")),
        "j5_review_respond_question_answer": bool(j5.get("ok")),
    }
    return {"ok": all(checks.values()), "base": base, "checks": checks,
            "elapsed_s": round(time.time() - started, 2),
            "verifiers": {"j4": j4, "j5": j5}}


def _qa_active() -> bool:
    """Is a real build's browser QA (or a QA fleet) actively running right now? Scheduled acceptance-QA must
    YIELD to real work — two browser-QA passes competing for the box thrash each other (observed live: the
    dogfood pass stalled an active build's QA). Best-effort via the pulse plane; fail-open (treat as idle)."""
    try:
        import pulse
        for r in (pulse.live() or []):
            # Persisted status=stalled rows remain visible for incident history but consume no browser
            # capacity. The derived `stalled` boolean is only true for silent status=active rows.
            if (r.get("kind") in ("qa-run", "tool-job")
                    and r.get("status") == "active" and not r.get("stalled")):
                return True
    except Exception:
        pass
    return False


def cron() -> dict:
    """The scheduler entry point. scheduler.tick() kills any job at JOB_TIMEOUT (120s); a real browser
    pass takes far longer — so detach the actual run into its own session and return immediately.
    YIELDS to active real work: if a build's QA is already running, skip this pass rather than thrash the box."""
    if _qa_active():
        print("dogfood: skipping — a real build's QA is active (yielding the box to real work)")
        return {"skipped": "qa-active", "reason": "yielded to an active QA run to avoid browser contention"}
    with open(LOG, "ab") as lf:
        p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run"],
                             cwd=str(SCRIPTS.parent), stdout=lf, stderr=lf,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    print(f"dogfood: daily acceptance pass detached (pid={p.pid}, log={LOG})")
    return {"detached": True, "pid": p.pid, "log": LOG}


# ─────────────────────────────────────────────────────────────────────────────────────────────────────
# Offline selftest — NO real browser, NO agent spend, NO DB. Stubs Explorer + findings + alerts + audit
# and asserts the pass truly drives the journeys and wires findings/alerts per the verdict.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _selftest():
    import tempfile
    import types

    import qa_explorer

    # standing wiring: the scheduler job exists, is DAILY, and uses the detaching cron entry point.
    import scheduler
    job = next((j for j in scheduler.DEFAULT_SCHEDULES if j[0] == "acceptance-dogfood"), None)
    assert job, "acceptance-dogfood must stay a registered standing scheduler job"
    assert job[2] == 86400, f"cadence must be DAILY (86400s), got {job[2]}"
    assert "dogfood.py" in job[1] and job[1].rstrip().endswith("cron"), \
        f"the scheduler must call the detaching `cron` mode (JOB_TIMEOUT-safe), got: {job[1]}"
    assert (SCRIPTS.parent / "docs" / "STANDARDS-acceptance.md").exists()

    # the mandated journey arc can't silently shrink.
    ids = [s["id"] for s in _journeys("abc123")]
    assert ids == list(REQUIRED_JOURNEYS), f"journeys must cover the full mandated arc, got {ids}"
    assert _journeys("abc123")[0].get("fresh") is True, "signup must run in a FRESH tokenless session"
    assert len(PERSONAS) >= 5 and all(PERSONAS.values())

    # true-severity mapping: critical is NEVER downgraded.
    assert _true_severity("critical") == "critical"
    assert _true_severity("medium") == "med" and _true_severity("high") == "high"
    assert _true_severity("none") == "low" and _true_severity(None) == "med"

    # Preflight/latest-run truth: artifact evidence wins over stale audit rows and can represent partial
    # interrupted browser runs without requiring any DB row.
    old_root = os.environ.get("AOS_DOGFOOD_EVIDENCE_ROOT")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        run_dir = root / "20260102-030405"
        shot_dir = run_dir / "04-J4-wait-watch" / "screenshots"
        shot_dir.mkdir(parents=True)
        (shot_dir / "state.png").write_bytes(b"not-a-real-png-but-nonempty-browser-artifact")
        (run_dir / "run-input.json").write_text(json.dumps({"persona": "first-run", "run": "abc123"}))
        (run_dir / "stories.json").write_text(json.dumps([
            {"id": "J1-signup-verify"}, {"id": "J2-create-company"}, {"id": "J3-direct-build"},
            {"id": "J4-wait-watch"}, {"id": "J5-review-respond"},
        ]))
        (run_dir / "summary.json").write_text(json.dumps({
            "persona": "first-run", "run": "abc123", "bugs": 0, "stories": 4,
            "story_status": {"J1-signup-verify": "passed", "J2-create-company": "passed",
                             "J3-direct-build": "passed"},
            "interrupted": True,
        }))
        os.environ["AOS_DOGFOOD_EVIDENCE_ROOT"] = str(root)
        try:
            latest = _last_run("first-run")
        finally:
            if old_root is None:
                os.environ.pop("AOS_DOGFOOD_EVIDENCE_ROOT", None)
            else:
                os.environ["AOS_DOGFOOD_EVIDENCE_ROOT"] = old_root
        assert latest["source"] == "artifact" and latest["passed"] == 3 and latest["total"] == 5
        assert latest["interrupted"] is True and latest["incomplete"] is True
        assert latest["story_status"]["J4-wait-watch"] == "interrupted"
        assert latest["story_status"]["J5-review-respond"] == "unproven"

    # ---- stub the world -------------------------------------------------------------------------
    calls = []                                    # global ordered call log — proves alert immediacy

    class FakeExplorer:
        """Emits a HIGH bug on the signup journey and a BLOCKING CRITICAL bug on direct-build."""
        seen = []
        interrupt_on = None

        def __init__(self, url, vision, token=None, org="0"):
            FakeExplorer.seen.append({"url": url, "vision": vision, "token": token, "org": org})
            self._token = token
            self.stop_reason = None
            self.coverage = []

        def explore(self, story, max_steps=25, on_bug=None, deadline=None):
            calls.append(("explore", story["id"]))
            assert max_steps == MAX_STEPS
            assert deadline is not None and deadline > 0, "run_once must pass a wall-clock deadline"
            if story["id"] == FakeExplorer.interrupt_on:
                raise _DogfoodInterrupted("selftest-interrupt")
            if story["id"] == "J1-signup-verify":
                on_bug({"bug": "verification code input rejects paste", "severity": "high",
                        "blocking": False, "expected": "code entry works", "url": "http://c/#signup",
                        "action": {"cmd": "fill"}, "shot": "/tmp/s1.png"})
            if story["id"] == "J3-direct-build":
                on_bug({"bug": "confirming the build kickoff does NOTHING — dead button", "severity": "critical",
                        "blocking": True, "expected": "build kicks off with an ETA", "url": "http://c/#chat",
                        "action": {"cmd": "click"}, "shot": "/tmp/s3.png"})
            if story["id"] == "J4-wait-watch":
                self.stop_reason = "stalled-incomplete"
            if story["id"] == "J5-review-respond":
                self.stop_reason = "coverage-complete-ai-judged"
                self.coverage = [{"aspect": "respond to CEO item", "covered": False}]
            return []

        def close(self):
            calls.append(("close",))

    fake_findings = types.ModuleType("findings")
    filed = []
    fake_findings.file = lambda src, role, title, detail="", severity="med", **k: (
        calls.append(("file", title, severity)),
        filed.append({"source": src, "role": role, "title": title, "severity": severity, "detail": detail}),
        {"finding_id": len(filed)})[-1]
    fake_findings.open_findings = lambda: [{"title": f["title"]} for f in filed]

    fake_alerts = types.ModuleType("alerts")
    alerts_raised = []

    def fake_raise_alert(src, role, body, severity="warn", signature=None):
        # mirror the real alerts.py contract: at most ONE open alert per signature (dedup on repeat).
        if any(a["signature"] == signature for a in alerts_raised):
            calls.append(("alert-dedup", signature))
            return {"alert_id": None, "owner": None, "deduped": True}
        calls.append(("alert", severity))
        alerts_raised.append({"role": role, "body": body, "severity": severity, "signature": signature})
        return {"alert_id": len(alerts_raised), "owner": role, "deduped": False}

    fake_alerts.raise_alert = fake_raise_alert

    fake_audit = types.ModuleType("audit")
    audited = []
    fake_audit.append = lambda **k: audited.append(k)
    fake_audit.count = lambda **k: 0

    real_explorer = qa_explorer.Explorer
    real_mods = {n: sys.modules.get(n) for n in ("findings", "alerts", "audit")}
    g = globals()
    real_seed, real_up = g["_seed_tenant"], g["_console_up"]
    stub_old_root = os.environ.get("AOS_DOGFOOD_EVIDENCE_ROOT")
    stub_evidence_root = tempfile.TemporaryDirectory()
    ok = False
    try:
        os.environ["AOS_DOGFOOD_EVIDENCE_ROOT"] = stub_evidence_root.name
        qa_explorer.Explorer = FakeExplorer
        sys.modules["findings"] = fake_findings
        sys.modules["alerts"] = fake_alerts
        sys.modules["audit"] = fake_audit
        g["_seed_tenant"] = lambda base, persona: ("TOK", "7", 42)
        g["_console_up"] = lambda base=BASE: True

        pf = preflight("first-run")
        assert pf["ok"] is True and pf["persona"] == "first-run"
        assert pf["journeys"][0]["fresh"] is True and len(pf["journeys"]) == len(REQUIRED_JOURNEYS)
        assert "events.jsonl" in pf["artifacts"]["required_files"]
        assert any("BUDGET_S" in s for s in pf["stop_conditions"])

        res = run_once("first-run")

        # DRIVES the console through the Explorer on ALL mandated journeys (not a one-off text agent).
        explored = [c[1] for c in calls if c[0] == "explore"]
        assert explored == list(REQUIRED_JOURNEYS), explored
        assert res["stories"] == 5 and res["console_up"] is True

        # persona brief IS the vision; signup runs tokenless; signed-in journeys use the seeded tenant.
        assert all(PERSONAS["first-run"] in s["vision"] for s in FakeExplorer.seen)
        assert FakeExplorer.seen[0]["token"] is None, "signup journey must start a FRESH session"
        assert all(s["token"] == "TOK" and s["org"] == "7" for s in FakeExplorer.seen[1:])

        # every bug/incomplete critical journey FILED with its TRUE severity, routed to the persona owner.
        assert res["filed"] == 4 and len(filed) == 4, (res, filed)
        assert filed[0]["severity"] == "high" and filed[1]["severity"] == "critical", filed
        assert all(f["role"] == "frontend-engineer" and f["source"] == SOURCE for f in filed)
        assert "screenshot: /tmp/s3.png" in filed[1]["detail"]

        # the BLOCKING finding alerted IMMEDIATELY: alert fired between filing it and the NEXT journey.
        assert len(alerts_raised) == 3 and alerts_raised[0]["severity"] == "critical"
        i_file = calls.index(("file", filed[1]["title"], "critical"))
        i_alert = next(i for i, c in enumerate(calls) if c[0] == "alert")
        i_next = calls.index(("explore", "J4-wait-watch"))
        assert i_file < i_alert < i_next, "blocking alert must fire inside on_bug, before the next journey"
        assert alerts_raised[0]["signature"].startswith("dogfood:")

        # story statuses are grounded in what was seen.
        assert res["story_status"]["J3-direct-build"] == "blocked"
        assert res["story_status"]["J1-signup-verify"] == "failed"
        assert res["story_status"]["J2-create-company"] == "passed"
        assert res["story_status"]["J4-wait-watch"] == "blocked"
        assert res["story_status"]["J5-review-respond"] == "blocked"

        # audited (stubbed — no DB rows from a selftest) with the full summary payload.
        assert len(audited) == 1 and audited[0]["action"] == "DogfoodPass"
        assert audited[0]["payload"]["alerts"] == 3

        # DEDUP: a second identical run must not re-file the same open findings.
        calls.clear()
        FakeExplorer.seen.clear()
        res2 = run_once("first-run")
        assert res2["filed"] == 0 and res2["deduped"] == 4, res2
        assert len(filed) == 4, "identical open findings must not pile up across daily runs"

        # INTERRUPTED live pass: an operator/scheduler stop mid-story still writes a partial summary and
        # explicit story result, so readiness tools never have to infer from missing files.
        calls.clear()
        audited.clear()
        FakeExplorer.seen.clear()
        FakeExplorer.interrupt_on = "J4-wait-watch"
        res_int = run_once("first-run")
        FakeExplorer.interrupt_on = None
        assert res_int["interrupted"] is True and res_int["interrupt_reason"] == "selftest-interrupt"
        assert res_int["story_status"]["J4-wait-watch"] == "interrupted"
        assert res_int["story_status"]["J5-review-respond"] == "skipped-interrupted"
        assert len(audited) == 1 and audited[0]["decision"] == "interrupted"
        assert any(r["story"] == "J4-wait-watch" and r["status"] == "interrupted"
                   for r in res_int["story_results"])

        # north-star live proof command exists and is guarded by preflight before it spends.
        calls.clear()
        res_q = qa_explore("first-run")
        assert res_q["preflight"]["ok"] is True and res_q["stories"] == 5
        g["_console_up"] = lambda base=BASE: False
        blocked_q = qa_explore("first-run")
        assert blocked_q["blocked"] == "preflight" and blocked_q["preflight"]["ok"] is False
        g["_console_up"] = lambda base=BASE: True

        # console DOWN is itself a blocking, alerted finding — never a silent skip.
        g["_console_up"] = lambda base=BASE: False
        filed.clear()
        res3 = run_once("buyer")
        assert res3["console_up"] is False and res3["filed"] == 1
        assert filed[0]["severity"] == "critical" and len(alerts_raised) == 4

        # cron detaches (JOB_TIMEOUT-safe) — capture the Popen argv, don't actually spawn.
        popens = []
        real_popen = subprocess.Popen
        subprocess.Popen = lambda argv, **k: (popens.append((argv, k)),
                                              types.SimpleNamespace(pid=999))[-1]
        try:
            c = cron()
        finally:
            subprocess.Popen = real_popen
        assert c["detached"] and popens and popens[0][0][-1] == "run"
        assert popens[0][1].get("start_new_session") is True

        ok = True
    finally:
        qa_explorer.Explorer = real_explorer
        g["_seed_tenant"], g["_console_up"] = real_seed, real_up
        for n, m in real_mods.items():
            if m is not None:
                sys.modules[n] = m
            else:
                sys.modules.pop(n, None)
        if stub_old_root is None:
            os.environ.pop("AOS_DOGFOOD_EVIDENCE_ROOT", None)
        else:
            os.environ["AOS_DOGFOOD_EVIDENCE_ROOT"] = stub_old_root
        stub_evidence_root.cleanup()

    print(f"journeys={len(REQUIRED_JOURNEYS)} personas={len(PERSONAS)} filed_true_severity=ok "
          f"blocking_alert_immediate=ok dedup=ok console_down_alerts=ok cron_detaches=ok")
    print("PASS: standing acceptance pass DRIVES the live console through qa_explorer on the real "
          "journeys -> findings.file (true severity, deduped) -> immediate blocking alerts ✅"
          if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "personas":
        for k, v in PERSONAS.items():
            print(f"{k}: {v[:90]}…")
    elif a[0] == "preflight":
        pf = preflight(a[1] if len(a) > 1 else None)
        print(json.dumps(pf, indent=2))
        sys.exit(0 if pf.get("ok") else 1)
    elif a[0] == "cron":
        cron()
    elif a[0] == "run":
        if len(a) > 1 and a[1] == "all":
            for s in run_all():
                print(json.dumps(s))
        else:
            print(json.dumps(run_once(a[1] if len(a) > 1 else None), indent=2))
    elif a[0] == "qa-explore":
        out = qa_explore(a[1] if len(a) > 1 else None)
        print(json.dumps(out, indent=2))
        sys.exit(0 if not out.get("blocked") else 1)
    elif a[0] == "verify-finding" and len(a) > 1 and a[1] == "515":
        out = verify_finding_515()
        print(json.dumps(out, indent=2, default=str))
        sys.exit(0 if out.get("ok") else 1)
    elif a[0] == "verify-j5":
        out = verify_j5_review_respond()
        print(json.dumps(out, indent=2, default=str))
        sys.exit(0 if out.get("ok") else 1)
    elif a[0] == "verify-critical":
        out = verify_critical_readiness()
        print(json.dumps(out, indent=2, default=str))
        sys.exit(0 if out.get("ok") else 1)
    else:
        sys.exit("usage: dogfood.py preflight [persona] | qa-explore [persona] | "
                 "verify-finding 515 | verify-j5 | verify-critical | run [persona|all] | cron | personas | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
