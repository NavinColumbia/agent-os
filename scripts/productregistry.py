#!/usr/bin/env python3
"""productregistry.py — the SINGLE SOURCE OF TRUTH for a product's identity, location, and phase state, plus
VALIDATED HANDOFF CONTRACTS between build phases (fixes the root-cause class; see docs/ARCHITECTURE-ROOT-CAUSE.md).

The bug class it kills: the loopcontroller CEO pipeline had every phase INDEPENDENTLY re-derive shared truth
(the controller computed a product id '1-ceo-cockpit', the build layer computed 'ceo-cockpit' — they diverged,
so QA looked in the wrong folder), and each handoff TRUSTED the previous phase (a build that errored still
advanced to a QA that couldn't find it). One disease: no authoritative record + boundaries that assume instead
of verify.

The cure, in two parts:
  1. ONE registry row per product is the authoritative record: {product_id, repo_path, tenant, org, plan,
     per-phase outcome}. Every phase READS its id + path from here (`path()`), never recomputes — so two
     phases physically cannot disagree about what/where the product is.
  2. Every phase boundary is a CONTRACT: a phase records its outcome (`record_phase`), and the next phase's
     PRECONDITION is validated against the registry BEFORE it runs (`precondition`). QA does not start unless
     the build genuinely succeeded AND its artifact exists on disk; otherwise the caller routes back to the
     dev/build (bounded via `attempt`), escalating to the human only when the autonomous loop is exhausted.

    register(product_id, tenant, org, repo_path=None, plan=None) -> record
    path(product_id) -> the canonical repo path (THE single source of truth every phase reads)
    record_phase(product_id, phase, ok, artifact=None, verdict=None)
    precondition(product_id, phase) -> (ok, reason)     # the boundary contract
    attempt(product_id, phase) -> n                      # bounded auto-loop counter
    get(product_id) -> full record
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psycopg  # noqa: E402
import trace as _trace  # noqa: E402

DB = _trace.DB
PRODUCTS = Path.home() / "projects" / "products"


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS product_registry (
            product_id   TEXT PRIMARY KEY,
            tenant_id    TEXT, org_id TEXT,
            repo_path    TEXT NOT NULL,
            plan         JSONB,
            current_phase TEXT,
            phases       JSONB NOT NULL DEFAULT '{}',   -- {phase: {ok, artifact, verdict, ts}}
            attempts     JSONB NOT NULL DEFAULT '{}',   -- {phase: n} for the bounded auto-loop
            ts           TIMESTAMPTZ DEFAULT now(),
            updated_at   TIMESTAMPTZ DEFAULT now())""")
        c.commit()


def register(product_id, tenant_id=None, org_id=None, repo_path=None, plan=None):
    """Create (or return) the ONE authoritative record for this product. Idempotent + non-destructive: the
    canonical repo_path is fixed on first registration and never silently changed by a later call, so no phase
    can re-point the product. Returns the full record."""
    _ensure()
    repo_path = str(repo_path) if repo_path else str(PRODUCTS / product_id)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO product_registry (product_id, tenant_id, org_id, repo_path, plan)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (product_id) DO UPDATE SET
                         tenant_id=COALESCE(product_registry.tenant_id, EXCLUDED.tenant_id),
                         org_id=COALESCE(product_registry.org_id, EXCLUDED.org_id),
                         plan=COALESCE(EXCLUDED.plan, product_registry.plan),
                         updated_at=now()""",
                    (product_id, tenant_id, str(org_id) if org_id is not None else None,
                     repo_path, json.dumps(plan) if plan else None))
        c.commit()
    return get(product_id)


def get(product_id):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT product_id, tenant_id, org_id, repo_path, plan, current_phase, phases, attempts
                       FROM product_registry WHERE product_id=%s""", (product_id,))
        r = cur.fetchone()
    if not r:
        return None
    keys = ["product_id", "tenant_id", "org_id", "repo_path", "plan", "current_phase", "phases", "attempts"]
    return dict(zip(keys, r))


def path(product_id):
    """THE single source of truth for where this product lives on disk. Every phase (build, QA, deliver) reads
    its path from HERE — never recomputes it — so they cannot diverge. Falls back to the canonical default for
    an unregistered id (so a caller is never left without a path)."""
    r = get(product_id)
    return (r or {}).get("repo_path") or str(PRODUCTS / product_id)


def record_phase(product_id, phase, ok, artifact=None, verdict=None):
    """Record a phase's OUTCOME into the authoritative record — the thing the NEXT phase's contract checks."""
    _ensure()
    entry = {"ok": bool(ok), "artifact": str(artifact) if artifact else None,
             "verdict": (str(verdict)[:500] if verdict else None)}
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""UPDATE product_registry
                       SET phases = phases || %s::jsonb, current_phase=%s, updated_at=now()
                       WHERE product_id=%s""",
                    (json.dumps({phase: entry}), phase, product_id))
        c.commit()
    return entry


def attempt(product_id, phase):
    """Increment + return the bounded auto-loop counter for a phase (so a failing build/QA re-tries a few times
    autonomously before we ever escalate to the human)."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT COALESCE((attempts->>%s)::int,0) FROM product_registry WHERE product_id=%s",
                    (phase, product_id))
        row = cur.fetchone()
        n = (row[0] if row else 0) + 1
        cur.execute("UPDATE product_registry SET attempts = attempts || %s::jsonb WHERE product_id=%s",
                    (json.dumps({phase: n}), product_id))
        c.commit()
    return n


# --- the boundary CONTRACTS: a phase's precondition, validated against the authoritative record --------------
def _isdir(p):
    try:
        return bool(p) and os.path.isdir(p) and any(os.scandir(p))   # exists AND non-empty
    except Exception:
        return False


def precondition(product_id, phase):
    """(ok, reason) — is `phase` allowed to run? Validated against the registry, NOT a loose assumption:
      * qa      requires build succeeded AND its artifact actually exists on disk (kills F6/F7 deterministically).
      * deliver requires QA passed.
    Anything else (research/design/build) is always allowed. A failed precondition tells the caller to route
    back to the producing phase (auto), not to the human."""
    r = get(product_id)
    if not r:
        return False, f"product '{product_id}' is not registered (no single source of truth)"
    ph = r.get("phases") or {}
    if phase == "qa":
        b = ph.get("build") or {}
        if not b.get("ok"):
            return False, f"build did not succeed (build.ok={b.get('ok')}) — route back to build, not QA"
        if not _isdir(r["repo_path"]):
            return False, f"build produced no artifact at the registered path {r['repo_path']} — route back to build"
        return True, "build succeeded and artifact is present"
    if phase == "deliver":
        q = ph.get("qa") or {}
        if not q.get("ok"):
            return False, f"QA has not passed (qa.ok={q.get('ok')}) — do not deliver"
        return True, "QA passed"
    return True, "no precondition"


def _selftest():
    import tempfile, uuid, shutil
    pid = f"reg-selftest-{uuid.uuid4().hex[:8]}"
    d = Path(tempfile.mkdtemp(prefix=pid + "-"))
    ok = True

    def chk(c, label):
        nonlocal ok
        print(("PASS" if c else "FAIL") + f": {label}"); ok = ok and bool(c)

    try:
        rec = register(pid, tenant_id="t1", org_id="1", repo_path=str(d), plan={"name": "x"})
        chk(rec and rec["repo_path"] == str(d), "register writes the ONE canonical record")
        chk(path(pid) == str(d), "path() is the single source of truth every phase reads")
        # first registration's path is authoritative — a later register with a DIFFERENT path can't silently move it
        register(pid, repo_path="/tmp/somewhere-else")
        chk(path(pid) == str(d), "canonical path is immutable across re-registration (no phase can re-point it)")

        # boundary contract: QA is BLOCKED until build succeeded AND the artifact exists
        okq, why = precondition(pid, "qa")
        chk(not okq and "build did not succeed" in why, "QA precondition blocks before build records success")
        record_phase(pid, "build", ok=True, artifact=str(d))
        okq, why = precondition(pid, "qa")
        chk(not okq and "no artifact" in why, "QA still blocked while the registered path is empty (F6/F7 caught)")
        (d / "server.js").write_text("// real build")     # build now produced a real artifact
        okq, why = precondition(pid, "qa")
        chk(okq, "QA precondition passes once the artifact truly exists at the registered path")

        # deliver contract + bounded auto-loop counter
        okd, _ = precondition(pid, "deliver")
        chk(not okd, "deliver blocked until QA passes")
        record_phase(pid, "qa", ok=True, verdict="ALL PASSED")
        okd, _ = precondition(pid, "deliver")
        chk(okd, "deliver allowed once QA passed")
        chk(attempt(pid, "build") == 1 and attempt(pid, "build") == 2, "bounded auto-loop counter increments")

        print("productregistry selftest: PASS — single source of truth for id+path; validated phase-boundary "
              "contracts (QA can't run before a real build artifact exists); bounded auto-loop counter" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        shutil.rmtree(d, ignore_errors=True)
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM product_registry WHERE product_id=%s", (pid,)); c.commit()


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "selftest":
        sys.exit(_selftest())
    elif a[0] == "path":
        print(path(a[1]))
    elif a[0] == "get":
        print(json.dumps(get(a[1]), indent=2, default=str))
