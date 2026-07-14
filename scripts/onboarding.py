#!/usr/bin/env python3
"""onboarding.py — the guided FIRST-RUN wizard for a non-technical CEO.

The framing: you just signed up, and you are now the CEO of a small company of AI agents. This wizard walks
you through the four things that have to happen before your staff can ship for you — sign up (done), connect
a model provider (your agents' "brains": Claude and/or Codex — you may have only one), give the AI-processing
consent the app stores require, and kick off your first build. NO web server: this is the data/logic module a
UI (or the CLI below) renders.

The wizard never just trusts a stored cursor. Each step's "done" is recomputed from REAL state:
  provider     -> tenantproviders.resolve(tid) has a connected provider
  consent      -> consent.require_consent(tid) is True
  first_build  -> the tenant owns any row in tenant_products
onboarding_state only records the furthest step reached + a completed flag.

    onboarding.py selftest
    onboarding.py json <tenant>        # full wizard state as JSON
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit            # noqa: E402
import consent          # noqa: E402
import tenantproviders  # noqa: E402

from aoscfg import ENV, DB

FRAMING = (
    "Welcome, CEO — you're now running a small company of AI agents, and they're ready to build for you. "
    "Four quick steps and your staff can ship your first product; we'll walk you through each one."
)

# The four real steps, in order. blurb is the CEO-framed one-liner a screen renders.
STEPS = [
    {"key": "welcome", "title": "Meet your company",
     "blurb": "You're the CEO; the AI agents are your staff. They build, you decide."},
    {"key": "provider", "title": "Connect a model provider",
     "blurb": "Give your agents a brain — connect Claude and/or Codex. One is enough to start."},
    {"key": "consent", "title": "Approve AI processing",
     "blurb": "A one-time, revocable OK to send your prompts to your named AI provider for processing."},
    {"key": "first_build", "title": "Ship your first build",
     "blurb": "Pick a template or describe what you want — your agents take it from there."},
]
_ORDER = [s["key"] for s in STEPS] + ["done"]


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS onboarding_state (
            tenant_id TEXT PRIMARY KEY, step TEXT DEFAULT 'welcome',
            completed BOOLEAN DEFAULT false, updated_at TIMESTAMPTZ DEFAULT now())""")
        c.commit()


def _row(tid):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT step, completed FROM onboarding_state WHERE tenant_id=%s", (tid,))
        return cur.fetchone()


def _has_product(tid):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM tenant_products WHERE tenant_id=%s LIMIT 1", (tid,))
        return cur.fetchone() is not None


def _done_map(tid):
    """Real per-step completion. welcome is "done" once the tenant has advanced past it."""
    row = _row(tid)
    reached = _ORDER.index(row[0]) if (row and row[0] in _ORDER) else 0
    provider_done = tenantproviders.resolve(tid).get("provider") is not None
    consent_done = consent.require_consent(tid)
    build_done = _has_product(tid)
    return {
        "welcome": reached >= 1 or provider_done or consent_done or build_done,
        "provider": provider_done,
        "consent": consent_done,
        "first_build": build_done,
    }


def state(tid):
    """Everything a wizard screen needs: the framing, the four steps with REAL done-status, the first
    not-yet-done step to land on, and whether the whole journey is complete."""
    done = _done_map(tid)
    steps = [{"key": s["key"], "title": s["title"], "blurb": s["blurb"], "done": done[s["key"]]} for s in STEPS]
    nxt = next((s["key"] for s in steps if not s["done"]), "done")
    row = _row(tid)
    completed = bool(row and row[1]) or all(s["done"] for s in steps)
    return {"step": nxt, "completed": completed, "steps": steps, "framing": FRAMING}


def advance(tid, step):
    """Record progress: store the FURTHEST step reached (never go backwards). Returns fresh state()."""
    if step not in _ORDER:
        raise ValueError(f"unknown step {step}; choose {_ORDER}")
    _ensure()
    row = _row(tid)
    cur_idx = _ORDER.index(row[0]) if (row and row[0] in _ORDER) else 0
    furthest = step if _ORDER.index(step) >= cur_idx else row[0]
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO onboarding_state (tenant_id, step, updated_at) VALUES (%s,%s, now())
                       ON CONFLICT (tenant_id) DO UPDATE SET step=EXCLUDED.step, updated_at=now()""",
                    (tid, furthest))
        c.commit()
    audit.append(actor="onboarding", action="OnboardingAdvanced", resource=tid, decision="advanced",
                 payload={"step": furthest})
    return state(tid)


def _set_completed(tid, action):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO onboarding_state (tenant_id, step, completed, updated_at)
                       VALUES (%s,'done', true, now())
                       ON CONFLICT (tenant_id) DO UPDATE SET completed=true, step='done', updated_at=now()""",
                    (tid,))
        c.commit()
    audit.append(actor="onboarding", action=action, resource=tid, decision="completed", payload={})
    return state(tid)


def skip(tid):
    """CEO chooses to leave the guided tour — mark the journey done without forcing every step."""
    return _set_completed(tid, "OnboardingSkipped")


def complete(tid):
    """All steps satisfied — close out onboarding."""
    return _set_completed(tid, "OnboardingCompleted")


def _selftest():
    import billing
    tid = billing.signup("onboarding-selftest", "free")["tenant_id"]
    try:
        s0 = state(tid)
        fresh_step_ok = s0["step"] in ("provider", "welcome")
        not_completed = s0["completed"] is False
        four_steps = len(s0["steps"]) == 4
        by = {s["key"]: s for s in s0["steps"]}
        provider_undone = by["provider"]["done"] is False
        consent_undone = by["consent"]["done"] is False

        consent.record(tid)                         # give real consent
        s1 = state(tid)
        consent_now = {s["key"]: s for s in s1["steps"]}["consent"]["done"] is True

        ok = (fresh_step_ok and not_completed and four_steps and provider_undone
              and consent_undone and consent_now)
        print(f"fresh-step={s0['step']} completed={s0['completed']} steps={len(s0['steps'])} "
              f"provider-done={by['provider']['done']} consent-done(pre)={consent_undone is False} "
              f"consent-done(post)={consent_now}")
        print("PASS: onboarding computes step status from real state (fresh blocks at provider, "
              "consent flips done after record) ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM onboarding_state WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM ai_consent WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        print(json.dumps(state(a[1]), indent=2))
    else:
        sys.exit("usage: onboarding.py selftest | json <tenant>")


if __name__ == "__main__":
    _main(sys.argv[1:])
