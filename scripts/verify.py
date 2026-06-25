#!/usr/bin/env python3
"""verify.py — SCALABLE verification for a built product. Quality-at-scale is the real ceiling, so
verification must grow with the budget: the higher the rigor, the more (and more adversarial) checking
a product gets before it's trusted. This is the "spend more tokens -> higher assurance" lever.

Rigor tiers (each ADDS to the previous):
  1  baseline    — the product's own test suite must pass (pytest).
  2  hardened    — + static security scan of the product code, + (for HTTP services) boot + load test.
  3+ adversarial — + N independent agents (N grows with rigor) that TRY TO BREAK the product: each writes
                   edge-case tests under tests/adversarial/ that must then pass; real defects get fixed.
                   More rigor = more adversaries hunting bugs = higher assurance.

    verify.py run <product> [rigor]
    verify.py selftest
Run with the agent-os venv python.
"""
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit     # noqa: E402
import factory   # noqa: E402

MAX_ADVERSARIES = int(os.environ.get("AOS_MAX_ADVERSARIES", "8"))
MAX_FIX = int(os.environ.get("AOS_MAX_VERIFY_FIX", "2"))
# High-severity code smells worth blocking on (a lightweight per-product scan, not the platform scanner).
_RISKY = [
    (r"shell\s*=\s*True", "subprocess shell=True (injection risk)"),
    (r"\beval\s*\(", "eval() on dynamic input"),
    (r"\bexec\s*\(", "exec() of dynamic code"),
    (r"pickle\.loads?\(", "pickle of untrusted data (RCE risk)"),
    (r"(?i)(api[_-]?key|secret|password|token)\s*=\s*['\"][A-Za-z0-9/\+_\-]{12,}['\"]", "hardcoded secret"),
    (r"0\.0\.0\.0", "binds 0.0.0.0 (never expose)"),
    (r"verify\s*=\s*False", "TLS verification disabled"),
]


def _is_service(repo: Path):
    return next(repo.glob("src/*/__main__.py"), None) is not None


def static_security(repo: Path):
    findings = []
    for f in repo.rglob("*.py"):
        if "node_modules" in f.parts or "/tests/" in str(f):
            continue
        try:
            txt = f.read_text()
        except Exception:
            continue
        for pat, msg in _RISKY:
            if re.search(pat, txt):
                findings.append({"file": str(f.relative_to(repo)), "issue": msg})
    return (len(findings) == 0), findings


def adversarial(repo: Path, n: int, api_key=None):
    """Spawn n agents that try to BREAK the product: each writes failing edge-case tests under
    tests/adversarial/, then defects are fixed (bounded). Returns (ok, summary)."""
    factory._ctx.api_key = api_key
    (repo / "tests" / "adversarial").mkdir(parents=True, exist_ok=True)

    def attack(i):
        factory._ctx.product = repo.name; factory._ctx.run = f"verify-{repo.name}"; factory._ctx.stage = f"ADVERSARY:{i}"
        factory.agent("qa-security", str(repo),
                      f"You are adversary #{i}. Read the product under src/ and its docs. Try HARD to BREAK it: "
                      f"find edge cases, boundary conditions, invalid inputs, and contract violations the "
                      f"existing tests MISS. Write NEW tests under tests/adversarial/adv_{i}.py that exercise "
                      f"these — real, meaningful assertions (NOT trivially-passing filler). It's fine (expected) "
                      f"if some FAIL — that means you found a real defect.")
        return i
    with ThreadPoolExecutor(max_workers=min(n, int(os.environ.get("AOS_FLEET_WORKERS", "5")))) as ex:
        list(as_completed([ex.submit(attack, i) for i in range(n)]))
    ok, out = factory.run_tests(str(repo), target="tests/adversarial")
    fixes = 0
    while not ok and fixes < MAX_FIX:                 # real defects the adversaries exposed -> fix them
        fixes += 1
        factory.agent("builder", str(repo),
                      f"Adversarial tests exposed real defects:\n\n{out[-1600:]}\n\nFix the product under src/ "
                      f"so `python -m pytest -q tests/adversarial` passes WITHOUT weakening those tests.")
        ok, out = factory.run_tests(str(repo), target="tests/adversarial")
    return ok, f"{n} adversaries, {fixes} fix loops, {'ROBUST' if ok else 'DEFECTS REMAIN'}"


def verify(product, rigor=1, api_key=None):
    """Run scalable verification at the given rigor. Returns a structured result; each tier must pass."""
    repo = factory.PRODUCTS / product
    if not repo.exists():
        return {"product": product, "error": "no such product"}
    result = {"product": product, "rigor": rigor, "passes": []}

    ok, _ = factory.run_tests(str(repo))             # tier 1: baseline suite
    result["passes"].append({"check": "test-suite", "ok": ok})
    if ok and rigor >= 2:                             # tier 2: static security + service runtime/load
        sec_ok, findings = static_security(repo)
        result["passes"].append({"check": "static-security", "ok": sec_ok, "findings": findings[:5]})
        if _is_service(repo):
            pkg = product.replace("-", "_")
            rt_ok, rt = factory.run_e2e_qa(str(repo), pkg)
            result["passes"].append({"check": "runtime+load", "ok": rt_ok, "detail": rt[-200:]})
    if all(p["ok"] for p in result["passes"]) and rigor >= 3:   # tier 3+: adversarial (scales with rigor)
        n = min(MAX_ADVERSARIES, rigor)
        adv_ok, adv = adversarial(repo, n, api_key)
        result["passes"].append({"check": "adversarial", "ok": adv_ok, "agents": n, "detail": adv})

    result["passed"] = all(p["ok"] for p in result["passes"])
    audit.append(actor="verify", action="Verify", resource=product, decision="passed" if result["passed"] else "failed",
                 payload={"rigor": rigor, "checks": [p["check"] for p in result["passes"]]})
    return result


def _selftest():
    """Offline check of the tiered logic + static scanner with mocked agents/tests (no spend)."""
    import tempfile
    workdir = Path(tempfile.mkdtemp()); (workdir / "prod" / "src" / "p").mkdir(parents=True)
    (workdir / "prod" / "src" / "p" / "bad.py").write_text("import subprocess\nsubprocess.run(x, shell=True)\n")
    real_p, real_rt, real_tests = factory.PRODUCTS, factory.run_e2e_qa, factory.run_tests
    factory.PRODUCTS = workdir
    factory.run_tests = lambda *a, **k: (True, "ok")
    factory.run_e2e_qa = lambda *a, **k: (True, "load ok")
    try:
        sec_ok, findings = static_security(workdir / "prod")
        r1 = verify("prod", rigor=1)
        r2 = verify("prod", rigor=2)
        ok = (not sec_ok and any("shell=True" in f["issue"] for f in findings)         # scanner catches it
              and r1["passed"] and len(r1["passes"]) == 1                              # rigor1 = baseline only
              and not r2["passed"] and len(r2["passes"]) >= 2)                          # rigor2 adds security -> fails on the smell
        print(f"static-security findings: {len(findings)} ; rigor1 checks={len(r1['passes'])} ; rigor2 checks={len(r2['passes'])}")
        print("PASS: scalable verification (tiered, static-security catches risk) ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    finally:
        factory.PRODUCTS, factory.run_e2e_qa, factory.run_tests = real_p, real_rt, real_tests


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "run":
        import json
        print(json.dumps(verify(a[1], int(a[2]) if len(a) > 2 else 1), indent=2))
    else:
        sys.exit("usage: verify.py run <product> [rigor] | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
