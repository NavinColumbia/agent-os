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
    dogfood.py cron                # scheduler entry point: detach a full `run` and return at once
    dogfood.py personas            # list the rotating personas
    dogfood.py selftest            # offline wiring check (Explorer/findings/alerts/audit stubbed)
Run with the agent-os venv python.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
QA = SCRIPTS / "qa"
if str(QA) not in sys.path:
    sys.path.insert(0, str(QA))

BASE = "http://127.0.0.1:8099"
SOURCE = "dogfood"
LOG = "/tmp/aos-dogfood.log"
MAX_STEPS = int(os.environ.get("AOS_DOGFOOD_MAX_STEPS", "20"))      # per-story exploration ceiling
BUDGET_S = int(os.environ.get("AOS_DOGFOOD_BUDGET_S", "3600"))      # per-persona wall-clock budget

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
            r = findings.file(SOURCE, role, title, detail, severity=sev)
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


def _next_persona() -> str:
    """Rotate by pass count so a different persona drives each daily run."""
    try:
        import audit
        n = audit.count(action="DogfoodPass")
    except Exception:
        n = 0
    return _ORDER[n % len(_ORDER)]


# ─────────────────────────────────────────────────────────────────────────────────────────────────────
# One persona pass: seed a tenant, drive every journey through the live console via the Explorer.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────
def run_once(persona: str = None, base: str = BASE) -> dict:
    import audit
    import qa_explorer
    persona = persona if persona in PERSONAS else (persona or _next_persona())
    if persona not in PERSONAS:
        persona = _next_persona()
    run_id = uuid.uuid4().hex[:6]
    counters = {"filed": 0, "deduped": 0, "alerts": 0, "file_errors": 0, "alert_errors": 0}
    started = time.time()

    # The live console being DOWN is itself a blocking product finding — file + alert, don't skip.
    if not _console_up(base):
        _file_bug(persona, {"id": "J0-availability", "title": "The console is reachable",
                            "goal": f"open the live console at {base}"},
                  {"bug": f"the live console at {base} is DOWN/unhealthy — no user can do anything",
                   "severity": "critical", "blocking": True, "expected": "console serves /health",
                   "url": base}, counters, run_id)
        summary = {"persona": persona, "run": run_id, "console_up": False, "stories": 0, "bugs": 1,
                   **counters}
        audit.append(actor=SOURCE, action="DogfoodPass", resource=persona, decision="console_down",
                     payload=summary)
        return summary

    tok, org, tid = _seed_tenant(base, persona)
    vision = _vision(persona)
    stories = _journeys(run_id)
    bugs_found = 0
    stories_run = 0
    story_status = {}
    for story in stories:
        if time.time() - started > BUDGET_S:      # graceful budget stop — never a hard kill mid-story
            story_status[story["id"]] = "skipped-budget"
            continue
        token = None if story.get("fresh") else tok
        ex = None
        collected = []
        cb = lambda bug, _s=story: (collected.append(bug),
                                    _file_bug(persona, _s, bug, counters, run_id))
        try:
            ex = qa_explorer.Explorer(base, vision, token=token, org=str(org))
            ex.explore(story, max_steps=MAX_STEPS, on_bug=cb)
            story_status[story["id"]] = "blocked" if any(b.get("blocking") for b in collected) \
                else ("failed" if collected else "passed")
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
        bugs_found += len(collected)
        stories_run += 1

    summary = {"persona": persona, "run": run_id, "console_up": True, "tenant": tid, "org": str(org),
               "stories": stories_run, "story_status": story_status, "bugs": bugs_found,
               "elapsed_s": int(time.time() - started), **counters}
    audit.append(actor=SOURCE, action="DogfoodPass", resource=persona, decision="filed",
                 payload=summary)
    return summary


def run_all(base: str = BASE) -> list:
    """Every persona in one pass (the 'per-deploy / big-audit' mode)."""
    return [run_once(p, base=base) for p in _ORDER]


def cron() -> dict:
    """The scheduler entry point. scheduler.tick() kills any job at JOB_TIMEOUT (120s); a real browser
    pass takes far longer — so detach the actual run into its own session and return immediately."""
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

    # ---- stub the world -------------------------------------------------------------------------
    calls = []                                    # global ordered call log — proves alert immediacy

    class FakeExplorer:
        """Emits a HIGH bug on the signup journey and a BLOCKING CRITICAL bug on direct-build."""
        seen = []

        def __init__(self, url, vision, token=None, org="0"):
            FakeExplorer.seen.append({"url": url, "vision": vision, "token": token, "org": org})
            self._token = token

        def explore(self, story, max_steps=25, on_bug=None):
            calls.append(("explore", story["id"]))
            assert max_steps == MAX_STEPS
            if story["id"] == "J1-signup-verify":
                on_bug({"bug": "verification code input rejects paste", "severity": "high",
                        "blocking": False, "expected": "code entry works", "url": "http://c/#signup",
                        "action": {"cmd": "fill"}, "shot": "/tmp/s1.png"})
            if story["id"] == "J3-direct-build":
                on_bug({"bug": "confirming the build kickoff does NOTHING — dead button", "severity": "critical",
                        "blocking": True, "expected": "build kicks off with an ETA", "url": "http://c/#chat",
                        "action": {"cmd": "click"}, "shot": "/tmp/s3.png"})
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
    ok = False
    try:
        qa_explorer.Explorer = FakeExplorer
        sys.modules["findings"] = fake_findings
        sys.modules["alerts"] = fake_alerts
        sys.modules["audit"] = fake_audit
        g["_seed_tenant"] = lambda base, persona: ("TOK", "7", 42)
        g["_console_up"] = lambda base=BASE: True

        res = run_once("first-run")

        # DRIVES the console through the Explorer on ALL mandated journeys (not a one-off text agent).
        explored = [c[1] for c in calls if c[0] == "explore"]
        assert explored == list(REQUIRED_JOURNEYS), explored
        assert res["stories"] == 5 and res["console_up"] is True

        # persona brief IS the vision; signup runs tokenless; signed-in journeys use the seeded tenant.
        assert all(PERSONAS["first-run"] in s["vision"] for s in FakeExplorer.seen)
        assert FakeExplorer.seen[0]["token"] is None, "signup journey must start a FRESH session"
        assert all(s["token"] == "TOK" and s["org"] == "7" for s in FakeExplorer.seen[1:])

        # every bug FILED with its TRUE severity (critical NOT downgraded), routed to the persona owner.
        assert res["filed"] == 2 and len(filed) == 2, (res, filed)
        assert filed[0]["severity"] == "high" and filed[1]["severity"] == "critical", filed
        assert all(f["role"] == "frontend-engineer" and f["source"] == SOURCE for f in filed)
        assert "screenshot: /tmp/s3.png" in filed[1]["detail"]

        # the BLOCKING finding alerted IMMEDIATELY: alert fired between filing it and the NEXT journey.
        assert len(alerts_raised) == 1 and alerts_raised[0]["severity"] == "critical"
        i_file = calls.index(("file", filed[1]["title"], "critical"))
        i_alert = next(i for i, c in enumerate(calls) if c[0] == "alert")
        i_next = calls.index(("explore", "J4-wait-watch"))
        assert i_file < i_alert < i_next, "blocking alert must fire inside on_bug, before the next journey"
        assert alerts_raised[0]["signature"].startswith("dogfood:")

        # story statuses are grounded in what was seen.
        assert res["story_status"]["J3-direct-build"] == "blocked"
        assert res["story_status"]["J1-signup-verify"] == "failed"
        assert res["story_status"]["J2-create-company"] == "passed"

        # audited (stubbed — no DB rows from a selftest) with the full summary payload.
        assert len(audited) == 1 and audited[0]["action"] == "DogfoodPass"
        assert audited[0]["payload"]["alerts"] == 1

        # DEDUP: a second identical run must not re-file the same open findings.
        calls.clear()
        FakeExplorer.seen.clear()
        res2 = run_once("first-run")
        assert res2["filed"] == 0 and res2["deduped"] == 2, res2
        assert len(filed) == 2, "identical open findings must not pile up across daily runs"

        # console DOWN is itself a blocking, alerted finding — never a silent skip.
        g["_console_up"] = lambda base=BASE: False
        filed.clear()
        res3 = run_once("buyer")
        assert res3["console_up"] is False and res3["filed"] == 1
        assert filed[0]["severity"] == "critical" and len(alerts_raised) == 2

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
    elif a[0] == "cron":
        cron()
    elif a[0] == "run":
        if len(a) > 1 and a[1] == "all":
            for s in run_all():
                print(json.dumps(s))
        else:
            print(json.dumps(run_once(a[1] if len(a) > 1 else None), indent=2))
    else:
        sys.exit("usage: dogfood.py run [persona|all] | cron | personas | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
