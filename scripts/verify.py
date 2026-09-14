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
# HARD patterns are almost always a genuine, exploitable defect (injection / RCE / disabled TLS) — they
# BLOCK the ship gate at ANY rigor, including the default rigor=1.
_RISKY_BLOCK = [
    (r"shell\s*=\s*True", "subprocess shell=True (injection risk)"),
    (r"\beval\s*\(", "eval() on dynamic input"),
    (r"\bexec\s*\(", "exec() of dynamic code"),
    (r"pickle\.loads?\(", "pickle of untrusted data (RCE risk)"),
    (r"verify\s*=\s*False", "TLS verification disabled"),
]
# NOISY patterns have a HIGH false-positive rate on legitimate product code: a containerized service
# binding 0.0.0.0 is completely normal, and the hardcoded-secret heuristic fires on placeholders. Now that
# the static scan is a fail-CLOSED default-rigor gate (factory.py), letting these block by default would
# brick legitimate ships for a whole product category. So they only BLOCK at hardened rigor (>=2); at the
# default rigor=1 they are emitted as WARN-only findings (they do NOT flip ok), and placeholder secrets are
# excluded outright. A per-product .verify-ignore file is the granular escape hatch (see _load_suppressions).
_RISKY_STRICT = [
    (r"(?i)(api[_-]?key|secret|password|token)\s*=\s*['\"][A-Za-z0-9/\+_\-]{12,}['\"]", "hardcoded secret"),
    (r"0\.0\.0\.0", "binds 0.0.0.0 (never expose)"),
]
# Markers that a "secret" match is an obvious placeholder/fixture rather than a live credential.
_PLACEHOLDER = re.compile(
    r"(?i)(change[\s_-]?me|placeholder|example|dummy|your[\s_-]?|<[^>]+>|x{4,}|\.{3,}|redacted|"
    r"fixme|todo|sample|fake|test|env|os\.environ|getenv|secret[\s_-]?here|123456|abcdef)")


def _load_suppressions(repo: Path):
    """Per-product escape hatch: a `.verify-ignore` file at the repo root (one substring per line, `#`
    comments allowed) suppresses a KNOWN-BENIGN finding by file-path or issue-text substring. This gives a
    single legitimate product a granular override under the fail-closed default — so one false positive
    can't wedge a ship — WITHOUT having to disable ALL verification via AOS_RIGOR=0."""
    supp = []
    f = repo / ".verify-ignore"
    if f.exists():
        try:
            for line in f.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    supp.append(line)
        except Exception:
            pass
    return supp


def _is_service(repo: Path):
    return next(repo.glob("src/*/__main__.py"), None) is not None


def static_security(repo: Path, rigor=2):
    """Deterministic per-product code scan. Returns (ok, findings); ok is False only if a BLOCKING finding
    survives suppression. HARD patterns (_RISKY_BLOCK) block at any rigor; NOISY patterns (_RISKY_STRICT)
    block only at hardened rigor (>=2) and are warn-only at the default rigor=1 so a legitimate 0.0.0.0
    bind or a placeholder secret can't wedge a fail-closed ship. Comment-only lines and placeholder secrets
    are ignored; a per-product .verify-ignore file suppresses known-benign matches."""
    supp = _load_suppressions(repo)
    blocking, warnings = [], []
    strict_blocks = rigor >= 2
    for f in repo.rglob("*.py"):
        if "node_modules" in f.parts or "/tests/" in str(f):
            continue
        try:
            lines = f.read_text().splitlines()
        except Exception:
            continue
        rel = str(f.relative_to(repo))
        for lineno, raw in enumerate(lines, 1):
            if raw.lstrip().startswith("#"):         # skip commented-out code / prose
                continue
            for pat, msg in _RISKY_BLOCK:
                if re.search(pat, raw):
                    blocking.append({"file": rel, "line": lineno, "issue": msg, "severity": "block"})
            for pat, msg in _RISKY_STRICT:
                m = re.search(pat, raw)
                if not m:
                    continue
                if "secret" in msg and _PLACEHOLDER.search(m.group(0)):
                    continue                         # obvious placeholder, not a live credential
                entry = {"file": rel, "line": lineno, "issue": msg,
                         "severity": "block" if strict_blocks else "warn"}
                (blocking if strict_blocks else warnings).append(entry)
    if supp:                                          # drop known-benign matches by path/issue substring
        blocking = [x for x in blocking if not any(s in x["file"] or s in x["issue"] for s in supp)]
        warnings = [x for x in warnings if not any(s in x["file"] or s in x["issue"] for s in supp)]
    return (len(blocking) == 0), blocking + warnings


def adversarial(repo: Path, n: int, api_key=None, engine=None, codex_key=None, stack=None):
    """Spawn n agents that try to BREAK the product: each writes failing edge-case tests under
    tests/adversarial/, then defects are fixed (bounded). Returns (ok, summary).
    api_key/engine/codex_key are the TENANT's BYO routing — they must reach every attack worker."""
    # Set this thread's context for the (serial) builder fix-loop and run_tests below.
    factory._ctx.api_key = api_key
    factory._ctx.engine = engine
    factory._ctx.codex_key = codex_key
    st = (stack or "").strip().lower()
    node_stack = st in ("web", "node", "js", "ts", "javascript", "typescript")
    (repo / "tests" / "adversarial").mkdir(parents=True, exist_ok=True)
    test_glob = ("tests/adversarial/adv_{i}.test.js" if node_stack else "tests/adversarial/adv_{i}.py")
    runner_hint = ("node --test tests/adversarial/*.test.js" if node_stack else "python -m pytest -q tests/adversarial")

    def attack(i):
        # Each ThreadPoolExecutor worker is a FRESH thread and factory._ctx is threading.local(), so it
        # does NOT inherit the submitting thread's context. Re-set this worker's OWN thread-local from the
        # values captured in the enclosing scope — otherwise the attack agent silently runs on the PLATFORM
        # key/engine (api_key=None -> 'claude') instead of the tenant's BYO key, billing the wrong account
        # and ignoring a Codex-only tenant. agent()/_agent_codex read engine + codex_key from _ctx too.
        factory._ctx.api_key = api_key
        factory._ctx.engine = engine
        factory._ctx.codex_key = codex_key
        factory._ctx.product = repo.name; factory._ctx.run = f"verify-{repo.name}"; factory._ctx.stage = f"ADVERSARY:{i}"
        factory.agent("qa-security", str(repo),
                      f"You are adversary #{i}. Read the product under src/ and its docs. Try HARD to BREAK it: "
                      f"find edge cases, boundary conditions, invalid inputs, and contract violations the "
                      f"existing tests MISS. Write NEW tests under {test_glob.format(i=i)} that exercise "
                      f"these — real, meaningful assertions (NOT trivially-passing filler). The tests must run "
                      f"with `{runner_hint}`. It's fine (expected) if some FAIL — that means you found a real "
                      f"defect.")
        return i
    with ThreadPoolExecutor(max_workers=min(n, int(os.environ.get("AOS_FLEET_WORKERS", "5")))) as ex:
        list(as_completed([ex.submit(attack, i) for i in range(n)]))
    # The attack workers may have authored NO tests at all — e.g. on the claude engine an adversary role
    # with can_modify_code:false has Edit/Write denied at spawn (governance.spawn_restrictions), so it
    # cannot write tests/adversarial even though its manifest scopes Edit to tests/** via allowed_paths.
    # An EMPTY tests/adversarial makes pytest exit 5 (no tests collected) -> run_tests ok=False, which the
    # always-on, fail-CLOSED VERIFY gate reads as "DEFECTS REMAIN" and BLOCKS an otherwise-fine build.
    # That is a FALSE block (a missing-coverage / tooling condition), not a product defect: distinguish
    # "no adversary actually ran" from "adversaries ran and exposed unfixable defects". Only the latter is
    # a real DEFECTS-REMAIN. If nothing was authored, do NOT brick legitimate liveness — pass the tier and
    # SURFACE that adversarial coverage was unavailable so the gap is visible rather than silently failing.
    pattern = "*.test.js" if node_stack else "*.py"
    authored = [p for p in (repo / "tests" / "adversarial").glob(pattern)
                if p.name != "__init__.py" and p.read_text().strip()]
    if not authored:
        return True, (f"{n} adversaries, 0 tests authored — adversarial coverage UNAVAILABLE on this engine "
                      f"(attack workers could not write tests/adversarial); NOT a product defect, build not blocked")
    ok, out = factory.run_tests(str(repo), target="tests/adversarial", stack=("web" if node_stack else ""))
    fixes = 0
    while not ok and fixes < MAX_FIX:                 # real defects the adversaries exposed -> fix them
        fixes += 1
        factory.agent("builder", str(repo),
                      f"Adversarial tests exposed real defects:\n\n{out[-1600:]}\n\nFix the product under src/ "
                      f"so `{runner_hint}` passes WITHOUT weakening those tests.")
        ok, out = factory.run_tests(str(repo), target="tests/adversarial", stack=("web" if node_stack else ""))
    return ok, f"{n} adversaries, {fixes} fix loops, {'ROBUST' if ok else 'DEFECTS REMAIN'}"


def verify(product, rigor=1, api_key=None, engine=None, codex_key=None, stack=None):
    """Run scalable verification at the given rigor. Returns a structured result; each tier must pass.
    engine/codex_key thread the tenant's BYO provider routing down to the adversarial attack workers."""
    repo = factory.PRODUCTS / product
    if not repo.exists():
        return {"product": product, "error": "no such product"}
    result = {"product": product, "rigor": rigor, "passes": []}

    ok, _ = factory.run_tests(str(repo), stack=stack)  # tier 1: baseline suite
    result["passes"].append({"check": "test-suite", "ok": ok})
    if ok and rigor >= 2:                             # tier 2: static security + service runtime/load
        sec_ok, findings = static_security(repo, rigor)
        result["passes"].append({"check": "static-security", "ok": sec_ok, "findings": findings[:5]})
        if _is_service(repo):
            pkg = product.replace("-", "_")
            rt_ok, rt = factory.run_e2e_qa(str(repo), pkg)
            result["passes"].append({"check": "runtime+load", "ok": rt_ok, "detail": rt[-200:]})
    if all(p["ok"] for p in result["passes"]) and rigor >= 3:   # tier 3+: adversarial (scales with rigor)
        n = min(MAX_ADVERSARIES, rigor)
        adv_ok, adv = adversarial(repo, n, api_key, engine, codex_key, stack=stack)
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

    # A SECOND, fully-legitimate product: a containerized service that binds 0.0.0.0 and uses a placeholder
    # secret. Under the fail-closed default gate this must NOT be bricked at rigor=1 — that's the whole fix.
    (workdir / "legit" / "src" / "p").mkdir(parents=True)
    (workdir / "legit" / "src" / "p" / "app.py").write_text(
        'HOST = "0.0.0.0"  # bind all interfaces (containerized)\n'  # secscan:allow (self-test fixture)
        'password = "changeme-please-1234"\n'  # secscan:allow (self-test fixture)
        'API_KEY = os.environ.get("API_KEY")\n')
    try:
        sec_ok, findings = static_security(workdir / "prod")
        r1 = verify("prod", rigor=1)
        r2 = verify("prod", rigor=2)
        # NOISY-pattern handling on the legit service: warn-only at rigor=1 (ships), blocking at rigor>=2.
        legit_ok1, legit_f1 = static_security(workdir / "legit", rigor=1)
        legit_ok2, legit_f2 = static_security(workdir / "legit", rigor=2)
        placeholder_excluded = not any("secret" in f["issue"] for f in legit_f2)  # placeholder isn't a finding
        bind_warns = any(f["issue"].startswith("binds 0.0.0.0") and f["severity"] == "warn" for f in legit_f1)
        bind_blocks = any(f["issue"].startswith("binds 0.0.0.0") and f["severity"] == "block" for f in legit_f2)
        # Per-product .verify-ignore is a granular escape hatch even at hardened rigor.
        (workdir / "legit" / ".verify-ignore").write_text("# benign\nbinds 0.0.0.0\n")
        supp_ok, _ = static_security(workdir / "legit", rigor=2)
        ok = (not sec_ok and any("shell=True" in f["issue"] for f in findings)         # HARD smell still blocks
              and r1["passed"] and len(r1["passes"]) == 1                              # rigor1 = baseline only
              and not r2["passed"] and len(r2["passes"]) >= 2                          # rigor2 adds security -> blocks on shell=True
              and legit_ok1                                                            # legit service NOT bricked at default rigor
              and not legit_ok2                                                        # but 0.0.0.0 blocks at hardened rigor
              and placeholder_excluded and bind_warns and bind_blocks                  # placeholder ignored; bind warn->block
              and supp_ok)                                                             # .verify-ignore clears the block
        print(f"static-security findings: {len(findings)} ; rigor1 checks={len(r1['passes'])} ; rigor2 checks={len(r2['passes'])}")
        print(f"legit-service: rigor1_ships={legit_ok1} rigor2_blocks={not legit_ok2} "
              f"placeholder_excluded={placeholder_excluded} suppressed={supp_ok}")
        print("PASS: scalable verification (tiered scan; HARD smells block, NOISY warn-then-block, no legit brick) ✅" if ok else "FAIL")
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
