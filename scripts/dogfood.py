#!/usr/bin/env python3
"""dogfood.py — the STANDING acceptance/dogfood pass (docs/STANDARDS-acceptance.md).

The owner kept being the only one applying the "demanding real user judging the product vs best-in-class"
lens. This makes that lens RECUR on its own: a rotating demanding-user persona drives/inspects the live
console, judges whether a real goal can be accomplished + what's MISSING vs ChatGPT/Stripe/Linear, and
FILES each finding to an owner via findings.py (so it's tracked, not left in a head). Registered as a
scheduler job so it runs without the owner asking.

    dogfood.py run [persona]     # run one persona now, file findings
    dogfood.py personas          # list the rotating personas
    dogfood.py selftest          # verify wiring (no agent spend)
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
BASE = "http://127.0.0.1:8099"

# The rotating demanding-user personas — same lens as the on-demand dogfood fleet, one per scheduled run.
PERSONAS = {
    "first-run": "a non-technical founder using agent-os for the FIRST time: signup -> verify -> create a "
                 "company -> connect AI -> describe a product. Judge the whole getting-in arc vs ChatGPT/"
                 "Linear/Stripe onboarding. What's confusing, missing, or amateur in the first 10 minutes?",
    "async-wait": "a founder who kicked off a build/research and is WAITING. Judge the async experience: "
                  "ETA on kickoff? live progress? a ping when results land? a Stop/Cancel? any FALSE 'done' "
                  "while running? Benchmark vs Vercel/GitHub Actions/ChatGPT streaming.",
    "latency": "an impatient power user. Are quick replies fast (near-ChatGPT) or do they spin for many "
               "seconds? Is there streaming + a stop button? Flag slowness and where a fast model should be used.",
    "buyer": "a careful buyer checking provider connect (key + subscription), billing/upgrade, settings, "
             "data export/delete, and password reset. What capabilities are MISSING? Does anything LIE about "
             "its result (a fake 'connected'/'sent')? Would you trust it with money?",
    "edge": "a user who breaks things: wrong password, bad code, expired session, empty/denied actions, a "
            "network blip, mobile (390px), keyboard-only. Does every dead-end give a guiding next step, not "
            "a scary error? Flag every rough edge vs best-in-class.",
}
_ORDER = list(PERSONAS)

# Map a finding's area to the role that should own the fix (findings.py routes it to an active agent).
_ROLE_FOR = {"first-run": "frontend-engineer", "async-wait": "backend-engineer", "latency": "backend-engineer",
             "buyer": "backend-engineer", "edge": "frontend-engineer"}


def _next_persona() -> str:
    """Rotate by run count so a different persona runs each scheduled tick (no Date/random needed)."""
    try:
        import audit
        n = audit.count(action="DogfoodPass")          # how many passes have run
    except Exception:
        n = 0
    return _ORDER[n % len(_ORDER)]


def run_once(persona: str = None, api_key: str = None) -> dict:
    """Run one demanding-user persona against the LIVE app and FILE its findings to owners."""
    import json
    import re
    import factory
    import findings
    import audit
    persona = persona or _next_persona()
    brief = PERSONAS.get(persona) or PERSONAS[_ORDER[0]]
    prompt = (
        f"You are a DEMANDING, experienced product user (you use ChatGPT, Claude, Stripe, Linear daily). "
        f"Apply docs/STANDARDS-acceptance.md. The agent-os console is live at {BASE}; inspect it (curl the "
        f"HTML/JSON routes, read scripts/console.py only to confirm a suspicion) and judge it AS A USER for "
        f"this goal: {brief}\n"
        f"Find REAL gaps: missing capabilities, edge/first-run journeys, product-judgment vs best-in-class, "
        f"broken promises, latency, confusing copy. Return STRICT JSON: "
        f'{{"findings":[{{"title":"...","severity":"blocker|high|med|low","detail":"goal + expected vs actual + the best-in-class comparison + the fix"}}]}} '
        f"— empty findings only if it is genuinely excellent. No prose outside the JSON.")
    r = factory.agent("research-growth", str(SCRIPTS.parent), prompt, api_key=api_key)
    out = r.get("out_full") or r.get("out") or ""
    m = re.search(r"\{.*\}", out, re.S)
    found = []
    if m:
        try:
            found = json.loads(m.group(0)).get("findings", [])
        except Exception:
            found = []
    role = _ROLE_FOR.get(persona, "builder")
    filed = 0
    for f in found:
        sev = (f.get("severity") or "med").lower()
        if sev in ("blocker", "critical"):
            sev = "high"   # findings.py severity vocabulary
        try:
            findings.file("dogfood", role, f"[acceptance/{persona}] {f.get('title','(untitled)')}",
                          f.get("detail", ""), severity=sev if sev in ("high", "med", "low") else "med")
            filed += 1
        except Exception:
            pass
    audit.append(actor="dogfood", action="DogfoodPass", resource=persona,
                 decision="filed", payload={"found": len(found), "filed": filed})
    return {"persona": persona, "found": len(found), "filed": filed}


def _selftest():
    """Verify the wiring without spending a real agent call (stub factory.agent + findings.file)."""
    import factory
    import findings
    ok = True
    assert len(PERSONAS) >= 4 and all(PERSONAS.values()), "personas must be defined"
    assert (SCRIPTS.parent / "docs" / "STANDARDS-acceptance.md").exists(), "acceptance standard must exist"
    import scheduler
    assert any(n == "acceptance-dogfood" for n, _, _ in scheduler.DEFAULT_SCHEDULES), \
        "acceptance-dogfood must be a registered standing scheduler job"
    real_agent, real_file = factory.agent, findings.file
    captured = []
    factory.agent = lambda *a, **k: {"out_full": '{"findings":[{"title":"T","severity":"high","detail":"d"}]}'}
    findings.file = lambda *a, **k: captured.append(a) or 1
    try:
        res = run_once("first-run")
        ok = res["found"] == 1 and res["filed"] == 1 and captured and captured[0][0] == "dogfood"
    finally:
        factory.agent, findings.file = real_agent, real_file
    print(f"personas={len(PERSONAS)} standard=ok run-files-findings={ok} next={_next_persona()}")
    print("PASS: standing dogfood/acceptance pass wired (rotating personas -> findings.file) ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "personas":
        for k, v in PERSONAS.items():
            print(f"{k}: {v[:80]}…")
    elif a[0] == "run":
        print(run_once(a[1] if len(a) > 1 else None))
    else:
        sys.exit("usage: dogfood.py run [persona] | personas | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
