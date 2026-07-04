#!/usr/bin/env python3
"""northstar_accept.py — the north-star acceptance scorecard (REBUILD-PLAN task #13, 'the final proofs').

Aggregates the STRONGEST existing proofs into one grouped scorecard answering: 'how close is agent-os to
the north star?' Each proof maps a north-star CLAIM to a concrete, already-passing verification (a module
selftest or a direct check) — no new trust, just a single roll-up of what is genuinely proven.

Two tiers:
  auto  — deterministic + fast; run here, pass/fail is objective (rc==0 [+ optional marker]).
  live  — requires a real, billed run (a real build from a vision; a skeptic QA session). LISTED, not
          auto-run — with the command to run it. These are the true 'astonish a skeptic' finish line.

    python northstar_accept.py report      # run the auto proofs, print the scorecard
    python northstar_accept.py selftest     # verify the harness itself (fast-gate safe)
Run with the agent-os venv python.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")


def _c(cmd):
    return f"{PY} {cmd}"


# pillar, claim, command (auto) or None (live), success-marker (optional; '' = just rc==0)
PROOFS = [
    # ── ORGANISM: the company runs itself ────────────────────────────────────────────────
    ("Organism", "Durable actor runtime survives crashes (SKIP-LOCKED, resume)", _c("scripts/orchestra/store.py selftest"), ""),
    ("Organism", "Event bus + deadlock-guarded messaging", _c("scripts/orchestra/runtime.py selftest"), ""),
    ("Organism", "Agentic controller drives the full lifecycle (no regex waterfall)", _c("scripts/loopcontroller.py selftest"), ""),
    ("Organism", "Company memory spine (remember / recall / lessons)", _c("scripts/companymemory.py selftest"), ""),

    # ── TRUST: governed + honest (a skeptic can rely on it) ───────────────────────────────
    ("Trust", "Money circuit-breaker blocks pre-spend on a maxed org", _c("scripts/appguard.py selftest"), ""),
    ("Trust", "Approval gate: nothing risky happens without a human decision", _c("scripts/approvals.py selftest"), ""),
    ("Trust", "Explainability: 'why the AI did X' from the real trail", _c("scripts/explain.py selftest"), ""),
    ("Trust", "Audit chain is tamper-evident (append-only hash chain)", _c("scripts/audit.py verify"), "INTACT"),
    ("Trust", "Multi-tenant isolation: each tenant sees only their own", _c("scripts/tenancy.py test"), ""),

    # ── EXPERIENCE: the CEO of an AI company ──────────────────────────────────────────────
    ("Experience", "Chief-of-staff brief from grounded real state (+ awaiting guard)", _c("scripts/chiefofstaff.py selftest"), ""),
    ("Experience", "Per-tenant notification taxonomy (silent/standard/urgent)", _c("scripts/notifications.py selftest"), ""),
    ("Experience", "Console: every screen renders, no dead controls (action crawler)", _c("scripts/console.py selftest"), ""),

    # ── RESILIENCE: loud AND silent failures self-heal ────────────────────────────────────
    ("Resilience", "Sentinel observes silent failures (hung agents, dead workflows)", _c("scripts/sentinel.py selftest"), ""),
    ("Resilience", "Scheduler claims-then-runs (no txn/lock held during jobs)", _c("scripts/scheduler.py selftest"), ""),
    ("Resilience", "Kill-switch halts new work, allows in-flight drain", _c("scripts/killswitch.py selftest"), ""),
    ("Resilience", "Watchdog keeps the daemon fleet supervised", _c("scripts/watchdog.py selftest"), ""),
    ("Resilience", "Reaper self-heals orphans / leaked browsers / stale runs / stuck builds", _c("scripts/reap.py selftest"), ""),

    # ── BUSINESS: sellable ────────────────────────────────────────────────────────────────
    ("Business", "Billing / plans / usage metering", _c("scripts/billing.py selftest"), ""),
    ("Business", "Connection pool (C2 scale headroom)", _c("scripts/dbpool.py selftest"), ""),

    # ── LIVE-ONLY: the true 'astonish a skeptic' finish line (billed real runs) ────────────
    ("Live", "One-shot vision -> a real shipped product (build + QA + deploy)", None,
     "python scripts/dogfood.py run  (real fleet build from a vision)"),
    ("Live", "Skeptic astonished in the first 5 minutes (agentic QA explorer)", None,
     "python scripts/dogfood.py qa-explore  (agentic skeptic session over the live console)"),
    ("Business", "Real Stripe billing capture + webhooks + dunning (C3)", None,
     "NOT BUILT — needs the owner's Stripe account (C3 in REBUILD-PLAN)"),
]

TIMEOUT = 120


def run_proof(cmd, marker):
    try:
        r = subprocess.run(["bash", "-c", cmd], cwd=str(ROOT), capture_output=True,
                           text=True, timeout=TIMEOUT)
        out = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0 and (marker in out if marker else True)
        return ok, ("" if ok else f"rc={r.returncode}" + (f" (no '{marker}')" if marker and marker not in out else ""))
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)[:60]


def _last_skeptic():
    """The most recent agentic-skeptic (dogfood) result, so the live-proof line reflects REALITY, not just a
    command to run. Fail-open (None if unavailable)."""
    try:
        import psycopg
        sys.path.insert(0, str(ROOT / "scripts"))
        import trace  # noqa: E402 — shared DATABASE_URL
        with psycopg.connect(trace.DB) as c, c.cursor() as cur:
            cur.execute("""SELECT payload, ts FROM audit_log WHERE actor='dogfood' AND action='DogfoodPass'
                           ORDER BY id DESC LIMIT 1""")
            r = cur.fetchone()
        if not r:
            return None
        p = r[0] or {}
        ss = p.get("story_status", {}) or {}
        passed = sum(1 for v in ss.values() if v == "passed")
        return (f"last run [{p.get('persona','?')}]: {passed}/{len(ss)} journeys passed, "
                f"{p.get('bugs', '?')} bugs  ({str(r[1])[:16]})")
    except Exception:
        return None


def _pipeline_stats():
    """Real build-pipeline evidence for the 'vision -> shipped product' proof: how many products the fleet
    has LAUNCHED and — the trust bar — how many the quality gates BLOCKED (it refuses to ship broken work).
    Fail-open."""
    try:
        import psycopg
        sys.path.insert(0, str(ROOT / "scripts"))
        import trace  # noqa: E402
        with psycopg.connect(trace.DB) as c, c.cursor() as cur:
            cur.execute("SELECT decision, count(*) FROM audit_log WHERE action='ProductComplete' GROUP BY decision")
            d = dict(cur.fetchall())
        launched = d.get("LAUNCHED", 0)
        blocked = sum(v for k, v in d.items() if str(k).startswith("BLOCKED"))
        failed = d.get("FAILED", 0)
        if not (launched or blocked):
            return None
        return f"pipeline to date: {launched} LAUNCHED, {blocked} BLOCKED by QA/review/verify gates, {failed} hard-failed"
    except Exception:
        return None


def report():
    print("═══ NORTH-STAR ACCEPTANCE SCORECARD ═══\n")
    auto = [p for p in PROOFS if p[2] is not None]
    live = [p for p in PROOFS if p[2] is None]
    passed = 0
    pillar = None
    for pil, claim, cmd, marker in auto:
        if pil != pillar:
            pillar = pil
            print(f"\n── {pil} ──")
        ok, why = run_proof(cmd, marker)
        passed += 1 if ok else 0
        print(f"  {'✅' if ok else '❌'} {claim}" + (f"   [{why}]" if why else ""))
    print(f"\n── AUTO PROOFS: {passed}/{len(auto)} passing ──")
    print("\n── LIVE-ONLY (the real finish line — billed runs, listed not auto-run) ──")
    skeptic = _last_skeptic()
    pipeline = _pipeline_stats()
    for pil, claim, _c2, note in live:
        extra = ""
        if skeptic and "skeptic" in claim.lower():
            extra = f"\n      ✓ {skeptic}"
        elif pipeline and "shipped product" in claim.lower():
            extra = f"\n      ✓ {pipeline}"
        print(f"  ◻ {claim}\n      → {note}{extra}")
    frac = passed / len(auto) if auto else 0
    print(f"\nAUTO north-star readiness: {frac:.0%} ({passed}/{len(auto)}).  "
          f"Remaining to 100%: the {len(live)} live proofs + any ❌ above.")
    return passed == len(auto)


def _selftest():
    # The harness itself must be sound: non-empty, well-formed, and run_proof correctly scores a known
    # pass and a known fail — so a green scorecard means something.
    ok = True

    def chk(c, l):
        nonlocal ok
        print(("PASS" if c else "FAIL") + f": {l}")
        ok = ok and bool(c)

    chk(len(PROOFS) >= 15, f"scorecard has a real proof set ({len(PROOFS)} proofs)")
    chk(all(len(p) == 4 for p in PROOFS), "every proof is well-formed (pillar, claim, cmd, marker)")
    chk(any(p[2] is None for p in PROOFS), "live-only proofs are listed (the real finish line is honest)")
    good, _ = run_proof("true", "")
    bad, _ = run_proof("false", "")
    chk(good and not bad, "run_proof scores a known pass True and a known fail False (harness is honest)")
    chk(len({p[0] for p in PROOFS}) >= 4, "proofs span the north-star pillars (organism/trust/experience/resilience/business)")
    print("PASS: north-star acceptance harness is sound ✅" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "selftest":
        sys.exit(0 if _selftest() else 1)
    else:
        sys.exit(0 if report() else 1)
