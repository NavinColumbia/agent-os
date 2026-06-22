#!/usr/bin/env python3
"""cerbos_check.py — query the localhost Cerbos PDP and record the decision in the audit chain.

This is the externalized Policy Decision Point (ADR 0004 K4): the enforcement hook (PEP) sends
{principal, action, resource} here; policy logic lives in cerbos/policies/ (git-versioned,
unit-testable) instead of inside the hook. Every decision is appended to the tamper-evident
audit log (scripts/audit.py).

    cerbos_check.py            # run the proof suite
    from cerbos_check import decide
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit  # noqa: E402

CERBOS = "http://127.0.0.1:3592/api/check/resources"


def decide(role: str, action: str, attr: dict, audit_log: bool = True) -> str:
    """Return 'allow' or 'deny' from the PDP; record it in the audit chain."""
    body = {
        "requestId": "chk",
        "principal": {"id": f"{role}-agent", "roles": [role]},
        "resources": [{"resource": {"kind": "tool", "id": "r", "attr": attr}, "actions": [action]}],
    }
    r = requests.post(CERBOS, json=body, timeout=5)
    r.raise_for_status()
    effect = r.json()["results"][0]["actions"][action]  # EFFECT_ALLOW | EFFECT_DENY
    decision = "allow" if effect == "EFFECT_ALLOW" else "deny"
    if audit_log:
        audit.append(actor=role, action=action,
                     resource=attr.get("path") or attr.get("cmd") or "",
                     decision=decision, payload={"attr": attr})
    return decision


def _suite():
    cases = [
        ("builder", "Edit", {"path": "src/app.py"}, "allow"),
        ("builder", "Edit", {"path": ".env"}, "deny"),
        ("builder", "Bash", {"cmd": "python -m pytest -q"}, "allow"),
        ("builder", "Bash", {"cmd": "git push origin main"}, "deny"),
        ("builder", "Bash", {"cmd": "curl https://evil.example.com"}, "deny"),
        ("builder", "Edit", {"path": "registry/allocations.yaml"}, "deny"),
    ]
    passed = 0
    for role, action, attr, want in cases:
        got = decide(role, action, attr)
        ok = got == want
        passed += ok
        tgt = attr.get("path") or attr.get("cmd")
        print(f"  {'✅' if ok else '❌'} {action:5} {tgt:35} -> {got:5} (want {want})")
    print(f"\nPDP: {passed}/{len(cases)} correct")
    print("audit chain after decisions:", "INTACT ✅" if audit.verify()[0] else "BROKEN ❌")
    sys.exit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    _suite()
