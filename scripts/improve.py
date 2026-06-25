#!/usr/bin/env python3
"""improve.py — continuous improvement with a SAFE-DEPLOY gate ("ship → improve → don't break stuff").

A product, once built, keeps getting better — but never worse. Each round: snapshot the product, let an
agent improve it (robustness / quality / performance / a feature), then EVAL-GATE the result with scalable
verification. If verification passes, the improvement is promoted; if it regresses, it is reverted from
the snapshot. This is the online "stay live and get better" loop, with the verifier as the canary.

    improve.py run <product> [rounds] [rigor]
    improve.py selftest
Run with the agent-os venv python.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit     # noqa: E402
import factory   # noqa: E402
import verify    # noqa: E402

_IGNORE = shutil.ignore_patterns(".venv", ".git", "__pycache__", "*.pyc", ".pytest_cache", "*.db")


def _snapshot(repo: Path) -> Path:
    snap = Path(tempfile.mkdtemp(prefix=f"improve-{repo.name}-"))
    shutil.copytree(repo, snap, dirs_exist_ok=True, ignore=_IGNORE)
    return snap


def _restore(repo: Path, snap: Path):
    for child in repo.iterdir():
        if child.name in (".venv", ".git"):
            continue
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
    shutil.copytree(snap, repo, dirs_exist_ok=True)


def improve_once(product, rigor=2, focus="robustness, quality and performance", api_key=None) -> dict:
    """One safe improvement round. Promote only if verification passes; else revert. Never ships a regression."""
    repo = factory.PRODUCTS / product
    if not repo.exists():
        return {"product": product, "error": "no such product"}
    factory._ctx.api_key = api_key
    snap = _snapshot(repo)
    try:
        factory._ctx.product = product; factory._ctx.run = f"improve-{product}"; factory._ctx.stage = "IMPROVE"
        factory.agent("staff-engineer", str(repo),
                      f"Improve this product — focus on {focus}. Make it genuinely better WITHOUT changing the "
                      f"public interfaces or breaking existing behaviour, and add/extend tests for what you "
                      f"change. Do not remove or weaken existing tests.")
        v = verify.verify(product, rigor=rigor, api_key=api_key)        # eval-gate (the canary)
        if v.get("passed"):
            audit.append(actor="improve", action="Improve", resource=product, decision="promoted",
                         payload={"rigor": rigor, "focus": focus})
            return {"product": product, "improved": True, "rigor": rigor}
        _restore(repo, snap)                                           # regression -> revert, stay safe
        audit.append(actor="improve", action="Improve", resource=product, decision="reverted",
                     payload={"rigor": rigor, "reason": "verification regressed"})
        return {"product": product, "improved": False, "reverted": True}
    finally:
        shutil.rmtree(snap, ignore_errors=True)


def improve(product, rounds=1, rigor=2, api_key=None) -> dict:
    """Run up to `rounds` safe-improvement rounds; stop early once a round can't safely improve further."""
    promoted = 0
    for i in range(rounds):
        r = improve_once(product, rigor=rigor, api_key=api_key)
        if r.get("error"):
            return r
        if r.get("improved"):
            promoted += 1
        else:
            break
    return {"product": product, "rounds": rounds, "promoted": promoted}


def _selftest():
    """Offline: a passing verify PROMOTES the change; a failing verify REVERTS it (no regression ships).
    Two independent fresh products so each case is clean."""
    workdir = Path(tempfile.mkdtemp())
    for name in ("pass", "fail"):
        (workdir / name).mkdir(); (workdir / name / "keep.txt").write_text("base")
    real_p, real_agent, real_verify = factory.PRODUCTS, factory.agent, verify.verify
    factory.PRODUCTS = workdir
    factory.agent = lambda role, repo, task, **k: ((Path(repo) / "added.py").write_text("x"), {"rc": 0})[1]
    try:
        verify.verify = lambda product, rigor=2, api_key=None: {"passed": True}
        improve_once("pass")
        promoted = (workdir / "pass" / "added.py").exists()                 # kept
        verify.verify = lambda product, rigor=2, api_key=None: {"passed": False}
        improve_once("fail")
        reverted = (not (workdir / "fail" / "added.py").exists()) and (workdir / "fail" / "keep.txt").exists()
        ok = promoted and reverted
        print(f"promote-on-pass={promoted}  revert-on-fail={reverted}")
        print("PASS: continuous improvement with safe-deploy gate (promote/revert) ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    finally:
        factory.PRODUCTS, factory.agent, verify.verify = real_p, real_agent, real_verify


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "run":
        import json
        print(json.dumps(improve(a[1], int(a[2]) if len(a) > 2 else 1, int(a[3]) if len(a) > 3 else 2), indent=2))
    else:
        sys.exit("usage: improve.py run <product> [rounds] [rigor] | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
