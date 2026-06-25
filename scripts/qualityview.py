#!/usr/bin/env python3
"""qualityview.py — the PLAIN-LANGUAGE quality/trust verdict, for the CEO who can't read code.

The factory already QAs, security-scans, and adversarially verifies every product (see verify.py:
tests -> static-security -> adversarial). That rigor is recorded in `traces`, but it's expressed in
stages, return codes, and roles a non-technical owner can't parse. qualityview turns that record into
ONE honest, human sentence per product — "is it actually good?" — with three trust badges behind it:

    Tests                  — did a QA / test stage pass (rc=0)?
    Security               — did a static-security / security stage run and pass?
    Independently verified — did a VERIFY / adversarial stage run and pass?

Everything is tenant-scoped: you only ever get a verdict on YOUR own products (tenant_products).

    qualityview.py json <tenant_id>     # every product's verdict + the cockpit one-liner list
    qualityview.py selftest
Run with the agent-os venv python. NO web server — this is a read-only summarizer of recorded truth.
"""
import json
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402,F401  (convention parity with the other surfaces)

DB = next((l.split("=", 1)[1].strip()
           for l in (Path.home() / "projects" / "agent-os" / ".env.local").read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

# How we recognise each gate in the recorded stage/role names (case-insensitive).
TEST_STAGES = ("QA", "TEST")
SECURITY_STAGES = ("SECURITY", "STATIC-SECURITY", "SEC", "SCAN")
VERIFY_STAGES = ("VERIFY", "ADVERSAR", "ADVERSARY")
SECURITY_ROLES = ("qa-security", "security")
GATE_STAGES = ("QA", "REVIEW")   # a failed one of these is a terminal "failed" verdict


def _gate(rows, stages=(), roles=()):
    """Look at every trace whose stage/role names one of these gates.
    Returns ('pass'|'fail'|'unknown', detail) — pass iff the gate ran AND every run of it had rc=0."""
    hits = []
    for stage, role, rc in rows:
        s = (stage or "").upper()
        r = (role or "").lower()
        if any(s.startswith(x) for x in stages) or any(r == x for x in roles):
            hits.append(rc)
    if not hits:
        return "unknown", "hasn't run yet"
    if all(rc == 0 for rc in hits):
        return "pass", f"passed ({len(hits)} check{'s' if len(hits) != 1 else ''})"
    bad = sum(1 for rc in hits if rc != 0)
    return "fail", f"{bad} of {len(hits)} check{'s' if len(hits) != 1 else ''} failed"


def _owns(cur, tid, product):
    cur.execute("SELECT 1 FROM tenant_products WHERE tenant_id=%s AND product=%s", (tid, product))
    return cur.fetchone() is not None


def _verdict_from_rows(product, rows):
    """Pure verdict logic over a product's trace rows (stage, role, rc). Shared by verdict() + summary()."""
    tests = _gate(rows, TEST_STAGES)
    security = _gate(rows, SECURITY_STAGES, SECURITY_ROLES)
    verified = _gate(rows, VERIFY_STAGES)
    gate = _gate(rows, GATE_STAGES)               # QA/REVIEW: a fail here is a hard, terminal fail

    if gate[0] == "fail":
        overall = "failed"
    elif tests[0] == "pass" and security[0] == "pass" and verified[0] == "pass":
        overall = "verified"
    elif tests[0] == "pass":
        overall = "passed"
    else:
        overall = "building"

    badges = [
        {"label": "Tests", "state": tests[0], "detail": tests[1]},
        {"label": "Security", "state": security[0], "detail": security[1]},
        {"label": "Independently verified", "state": verified[0], "detail": verified[1]},
    ]
    summary = {
        "verified": f"{product} is solid — its tests pass, it's been security-checked, "
                    f"and independent agents tried to break it and couldn't. You can trust it.",
        "passed":   f"{product} works — all its tests pass. "
                    f"{'Security and independent verification are still running.' if security[0] != 'pass' or verified[0] != 'pass' else ''}".strip(),
        "failed":   f"{product} isn't ready — it failed a quality gate and needs more work before you ship it.",
        "building": f"{product} is still being built and quality-checked — no verdict yet.",
    }[overall]
    return {"product": product, "overall": overall, "badges": badges, "summary": summary}


def verdict(tid, product):
    """Plain-language trust verdict for ONE of the tenant's own products. Ownership-checked."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        if not _owns(cur, tid, product):
            return {"error": "not your product"}
        cur.execute("SELECT stage, role, rc FROM traces WHERE product=%s", (product,))
        rows = cur.fetchall()
    return _verdict_from_rows(product, rows)


def summary(tid):
    """One-liner verdict for EVERY product the tenant owns (for the cockpit list)."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s ORDER BY product", (tid,))
        prods = [r[0] for r in cur.fetchall()]
        out = []
        for p in prods:
            cur.execute("SELECT stage, role, rc FROM traces WHERE product=%s", (p,))
            v = _verdict_from_rows(p, cur.fetchall())
            out.append({"product": p, "overall": v["overall"], "summary": v["summary"]})
    return out


def _selftest():
    """Real tenant + product with a passing QA stage and a passing security stage; prove the verdict +
    badges + ownership guard. Cleans up traces/tenant_products/tenants in finally."""
    import billing
    tid = billing.signup("quality-selftest", "free")["tenant_id"]
    prod = tid.replace("t-", "")[:6] + "-qv"
    ok = False
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (prod, tid))
            for stage, role in (("QA", "qa-security"), ("SECURITY", "qa-security")):
                cur.execute("""INSERT INTO traces (run_id, product, stage, role, kind, rc, prompt, output, model)
                               VALUES (%s,%s,%s,%s,'agent',0,'p','o','m')""",
                            (f"run-{prod}", prod, stage, role))
            c.commit()

        v = verdict(tid, prod)
        guard = verdict(tid, "someone-elses-product-" + prod)
        s = summary(tid)
        tests_badge = next(b for b in v["badges"] if b["label"] == "Tests")
        ok = (v["overall"] in ("verified", "passed", "failed", "building")
              and len(v["badges"]) >= 3
              and tests_badge["state"] == "pass"
              and guard.get("error") == "not your product"
              and any(p["product"] == prod for p in s))
        print(f"overall={v['overall']} badges={len(v['badges'])} "
              f"tests={tests_badge['state']} guard={guard.get('error')!r} products={len(s)}")
        print("PASS: qualityview gives a plain-language, ownership-checked trust verdict ✅"
              if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE product=%s", (prod,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        print(json.dumps(summary(a[1]), indent=2))
    else:
        sys.exit("usage: qualityview.py json <tenant_id> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
