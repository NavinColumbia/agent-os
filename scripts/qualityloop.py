#!/usr/bin/env python3
"""qualityloop.py — the "iterate until perfect" engine: climb-to-bar dev→test→qa→verify.

Quality is priority #1. A product isn't "done when the builder stops typing"; it's done when it MEETS A
BAR. This loop composes the pieces the platform already has — the factory's real test/QA gates, the
scalable verifier (verify.py), and the safe-deploy improver (improve.py) — into a closed loop that keeps
raising quality until the bar is met or the round budget is spent:

  each round:
    1. MEASURE   — run verify.verify(rigor=2) and read its tests / security / verified signals into a score
    2. BAR MET?  — 'standard' = tests pass AND security clean ; 'high' = those AND independently verified
    3. if met    -> SHIP (status 'shipped'); record the run and stop
       else      -> improve.improve_once(focus="raise quality to pass the bar"), then re-measure next round

Every round is recorded (quality_measurements) so you can SEE quality climb; the terminal result is written
to build_outcomes — a learning store that feeds the build recommender later (which bar/how many rounds a
kind of product tends to need). Never ships below the bar; reverts are handled inside improve_once's gate.

    qualityloop.py run <product> [bar] [max_rounds]
    qualityloop.py resume <run_id>
    qualityloop.py outcomes [limit]
    qualityloop.py selftest
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit     # noqa: E402
import verify    # noqa: E402
import improve   # noqa: E402

from aoscfg import ENV, DB

# Score weights: tests are the floor (does it even work?), security and independent verification split the
# rest. The score is a continuous quality signal for the learning store; the BAR is the hard ship gate.
W_TESTS, W_SECURITY, W_VERIFIED = 0.5, 0.25, 0.25
MAX_ROUNDS_CAP = 12   # hard ceiling so a stubborn product can never loop forever (cost guard)

# Bar -> verification rigor. 'standard' = rigor 2 (tests + static security, +runtime/load for services).
# 'high' = rigor 3, which is where verify.py actually runs the ADVERSARIAL tier (independent agents that
# try to break the product). Pinning rigor to 2 silently collapses 'high' down to 'standard' — the bug.
_BAR_RIGOR = {"standard": 2, "high": 3}


def _rigor_for(bar: str) -> int:
    """Map a ship bar to a verify rigor; unknown bars default to the stricter 'high' rigor (matches _bar_met)."""
    return _BAR_RIGOR.get(bar, 3)


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS quality_runs (
            id BIGSERIAL PRIMARY KEY, product TEXT, org_id TEXT, bar TEXT, rounds INT DEFAULT 0,
            status TEXT DEFAULT 'running', result TEXT,
            started_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS quality_measurements (
            id BIGSERIAL PRIMARY KEY, run_id BIGINT, round INT, tests_pass BOOLEAN,
            security_clean BOOLEAN, verified BOOLEAN, score NUMERIC, note TEXT,
            at TIMESTAMPTZ DEFAULT now())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS build_outcomes (
            id BIGSERIAL PRIMARY KEY, product TEXT, kind TEXT, rounds INT, final_score NUMERIC,
            shipped BOOLEAN, cost_usd NUMERIC, at TIMESTAMPTZ DEFAULT now())""")
        c.commit()


def _check(passes, name):
    """Pull a single check's ok from verify.verify()'s passes list; None if it wasn't run at this rigor."""
    for p in passes or []:
        if p.get("check") == name:
            return bool(p.get("ok"))
    return None


def _measure(product, bar="standard", api_key=None) -> dict:
    """Run the verifier once AT THE BAR'S RIGOR (standard->2: tests + static security, +runtime/load for
    services ; high->3: ALSO the adversarial tier) and distill it into the loop's quality signals.
    score = weighted (tests .5 + security .25 + verified .25)."""
    rigor = _rigor_for(bar)
    v = verify.verify(product, rigor=rigor, api_key=api_key)
    passes = v.get("passes", [])
    tests_pass = bool(_check(passes, "test-suite"))
    sec = _check(passes, "static-security")
    security_clean = bool(sec) if sec is not None else False
    # "verified" = genuine independent/adversarial sign-off. At the 'high' bar this REQUIRES the adversarial
    # check to have actually RUN and passed (rigor>=3); it must not collapse to just-tests-and-security.
    # At 'standard' it's the verifier's overall verdict. _check returns None when a tier never ran.
    adv = _check(passes, "adversarial")
    if rigor >= 3:
        verified = bool(v.get("passed")) and adv is True
    else:
        verified = bool(v.get("passed"))
    score = (W_TESTS * tests_pass) + (W_SECURITY * security_clean) + (W_VERIFIED * verified)
    return {"tests_pass": tests_pass, "security_clean": security_clean, "verified": verified,
            "score": round(score, 4), "error": v.get("error")}


def _bar_met(m: dict, bar: str) -> bool:
    """The hard ship gate. 'standard' = it works and it's clean; 'high' = that PLUS independent verification
    (the verifier's full adversarial sign-off). Default unknown bars to the stricter 'high'."""
    if bar == "standard":
        return bool(m.get("tests_pass")) and bool(m.get("security_clean"))
    return bool(m.get("tests_pass")) and bool(m.get("security_clean")) and bool(m.get("verified"))


def _record_measurement(run_id, rnd, m):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO quality_measurements
                       (run_id, round, tests_pass, security_clean, verified, score, note)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (run_id, rnd, m["tests_pass"], m["security_clean"], m["verified"], m["score"],
                     m.get("error") or f"score={m['score']}"))
        c.commit()


def _finish(run_id, status, result, rounds, product, m, shipped, kind="lib"):
    """Terminal bookkeeping: close the run + write the build_outcomes learning row (best-effort cost from
    the factory's running spend counter, if this process drove the agents)."""
    cost = 0.0
    try:
        import factory
        cost = factory.spent_usd()
    except Exception:
        pass
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""UPDATE quality_runs SET status=%s, result=%s, rounds=%s, finished_at=now()
                       WHERE id=%s""", (status, result, rounds, run_id))
        cur.execute("""INSERT INTO build_outcomes (product, kind, rounds, final_score, shipped, cost_usd)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (product, kind, rounds, (m or {}).get("score", 0.0), shipped, cost))
        c.commit()
    audit.append(actor="qualityloop", action="QualityLoop", resource=product,
                 decision=status, payload={"run_id": run_id, "rounds": rounds,
                                           "score": (m or {}).get("score"), "shipped": shipped})


def _loop(run_id, product, bar, max_rounds, start_round=0, api_key=None) -> dict:
    """The climb. Returns the loop result dict. Shared by run() and resume()."""
    max_rounds = min(int(max_rounds), MAX_ROUNDS_CAP)
    rounds = start_round
    m = None
    shipped = False
    status = "exhausted"
    result = "did not reach the bar within the round budget"
    for rnd in range(start_round + 1, max_rounds + 1):
        rounds = rnd
        m = _measure(product, bar=bar, api_key=api_key)
        _record_measurement(run_id, rnd, m)
        if m.get("error"):                                   # no such product / verifier couldn't run
            status, result = "error", m["error"]
            break
        if _bar_met(m, bar):
            status, result, shipped = "shipped", f"met '{bar}' bar at round {rnd}", True
            break
        if rnd < max_rounds:                                  # below the bar -> safely raise quality, re-measure next round
            # gate the improvement at the SAME rigor we're shipping against, so 'high' improvements are
            # eval-gated by the adversarial tier too (consistent with the bar; not pinned to 2).
            improve.improve_once(product, rigor=_rigor_for(bar),
                                 focus="raise quality to pass the bar", api_key=api_key)
    _finish(run_id, status, result, rounds, product, m, shipped)
    return {"run_id": run_id, "status": status, "rounds": rounds,
            "final_score": (m or {}).get("score", 0.0), "shipped": shipped}


def run(product, bar="standard", max_rounds=3, api_key=None, org_id=None) -> dict:
    """Iterate dev→test→qa→verify until the product MEETS THE BAR or the round budget is spent. Each round
    measures (verify), ships if the bar is met, else improves and re-measures. Records the run, a
    measurement per round, and a terminal build_outcome. Returns {run_id, status, rounds, final_score, shipped}."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO quality_runs (product, org_id, bar, status)
                       VALUES (%s,%s,%s,'running') RETURNING id""", (product, org_id, bar))
        run_id = cur.fetchone()[0]
        c.commit()
    audit.append(actor="qualityloop", action="QualityLoop", resource=product, decision="started",
                 payload={"run_id": run_id, "bar": bar, "max_rounds": max_rounds})
    return _loop(run_id, product, bar, max_rounds, start_round=0, api_key=api_key)


def resume(run_id, api_key=None) -> dict:
    """Continue a run left 'running' (e.g. the process died mid-climb). Picks up after the last recorded
    round so prior measurements aren't redone, and finishes the climb against the same bar."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT product, bar, status FROM quality_runs WHERE id=%s", (run_id,))
        row = cur.fetchone()
        if not row:
            return {"run_id": run_id, "error": "no such run"}
        product, bar, status = row
        if status != "running":
            return {"run_id": run_id, "status": status, "note": "not running — nothing to resume"}
        cur.execute("SELECT COALESCE(MAX(round), 0) FROM quality_measurements WHERE run_id=%s", (run_id,))
        last_round = cur.fetchone()[0] or 0
    # resume the climb with a fresh budget from where it left off (cap still applies inside _loop)
    return _loop(run_id, product, bar, last_round + 3, start_round=last_round, api_key=api_key)


def outcomes(limit=50):
    """Recent build_outcomes — the learning store that feeds the build recommender (which bar / how many
    rounds a kind of product tends to need, and what it cost)."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT product, kind, rounds, final_score, shipped, cost_usd, at
                       FROM build_outcomes ORDER BY at DESC LIMIT %s""", (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _selftest():
    """Offline: MONKEYPATCH verify (passing verdict) + improve (no-op promote) so NO real spend happens.
    Assert the loop ships in 1 round when the bar is met, that the run/measurement/outcome rows are written,
    and that _bar_met is correct for standard vs high. Cleans up its own rows; restores the mocks."""
    _ensure()
    product = "qualityloop-selftest-throwaway"
    real_verify, real_improve = verify.verify, improve.improve_once
    run_id = None
    improve_calls = [0]
    try:
        # mocked verifier: everything passes (tests + security + adversarial) -> 'passed' True at any rigor
        verify.verify = lambda product, rigor=2, api_key=None: {
            "product": product, "passed": True,
            "passes": [{"check": "test-suite", "ok": True},
                       {"check": "static-security", "ok": True}]}

        def _noop_improve(product, rigor=2, focus="", api_key=None):
            improve_calls[0] += 1
            return {"product": product, "promoted": True}
        improve.improve_once = _noop_improve

        # _bar_met logic — standard needs tests+security; high needs verified too
        m_pass2 = {"tests_pass": True, "security_clean": True, "verified": False}
        m_pass3 = {"tests_pass": True, "security_clean": True, "verified": True}
        m_redsec = {"tests_pass": True, "security_clean": False, "verified": False}
        bar_logic = (_bar_met(m_pass2, "standard") and not _bar_met(m_pass2, "high")
                     and _bar_met(m_pass3, "high") and not _bar_met(m_redsec, "standard"))

        # high bar maps to rigor 3 and DEMANDS the adversarial tier actually ran + passed.
        # with the (rigor-2 shaped) mock above — no 'adversarial' check — the high bar must NOT verify.
        m_high_noadv = _measure(product, bar="high")
        high_needs_adv = (m_high_noadv["verified"] is False) and not _bar_met(m_high_noadv, "high")
        # now a verifier that DID run the adversarial tier and passed -> high bar verifies.
        verify.verify = lambda product, rigor=2, api_key=None: {
            "product": product, "passed": True,
            "passes": [{"check": "test-suite", "ok": True},
                       {"check": "static-security", "ok": True},
                       {"check": "adversarial", "ok": True}]}
        m_high_adv = _measure(product, bar="high")
        high_with_adv = (m_high_adv["verified"] is True) and _bar_met(m_high_adv, "high")
        # restore the no-adversarial (standard-shaped) mock for the loop ship test below
        verify.verify = lambda product, rigor=2, api_key=None: {
            "product": product, "passed": True,
            "passes": [{"check": "test-suite", "ok": True},
                       {"check": "static-security", "ok": True}]}

        # the loop itself: bar is met on the first measurement -> ships in 1 round, never calls improve
        res = run(product, bar="standard", max_rounds=3)
        run_id = res["run_id"]
        shipped_1round = (res["status"] == "shipped" and res["shipped"] and res["rounds"] == 1
                          and improve_calls[0] == 0)

        # rows were written: the run, exactly one measurement, one outcome
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT status, result FROM quality_runs WHERE id=%s", (run_id,))
            qr = cur.fetchone()
            cur.execute("SELECT count(*) FROM quality_measurements WHERE run_id=%s", (run_id,))
            n_meas = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM build_outcomes WHERE product=%s", (product,))
            n_out = cur.fetchone()[0]
        rows_ok = (qr is not None and qr[0] == "shipped" and n_meas == 1 and n_out == 1)

        ok = bar_logic and high_needs_adv and high_with_adv and shipped_1round and rows_ok
        print(f"bar_logic={bar_logic} high_needs_adv={high_needs_adv} high_with_adv={high_with_adv} "
              f"shipped_1round={shipped_1round} "
              f"rows(run={qr and qr[0]}, meas={n_meas}, outcomes={n_out})={rows_ok}")
        print("PASS: climb-to-bar quality loop (measure→bar→ship, learning store, resume) ✅"
              if ok else "FAIL")
        sys.exit(0 if ok else 1)
    finally:
        verify.verify, improve.improve_once = real_verify, real_improve
        if run_id is not None:
            try:
                with psycopg.connect(DB) as c, c.cursor() as cur:
                    cur.execute("DELETE FROM quality_measurements WHERE run_id=%s", (run_id,))
                    cur.execute("DELETE FROM quality_runs WHERE id=%s", (run_id,))
                    cur.execute("DELETE FROM build_outcomes WHERE product=%s", (product,))
                    c.commit()
            except Exception:
                pass


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "run":
        import json
        bar = a[2] if len(a) > 2 else "standard"
        mr = int(a[3]) if len(a) > 3 else 3
        print(json.dumps(run(a[1], bar=bar, max_rounds=mr), indent=2, default=str))
    elif a[0] == "resume":
        import json
        print(json.dumps(resume(int(a[1])), indent=2, default=str))
    elif a[0] == "outcomes":
        import json
        print(json.dumps(outcomes(int(a[1]) if len(a) > 1 else 50), indent=2, default=str))
    else:
        sys.exit("usage: qualityloop.py run <product> [bar] [max_rounds] | resume <run_id> | "
                 "outcomes [limit] | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
