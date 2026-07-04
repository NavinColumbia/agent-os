#!/usr/bin/env python3
"""test_audit_tamper.py — prove the audit log's tamper-EVIDENCE actually works.

The gate checks `audit.py verify` returns INTACT — but that only proves the live chain is currently
consistent, NOT that verify() would CATCH an edit (the entire point of a tamper-evident log). And you can't
tamper the live global chain to test it without corrupting everyone's trail. So this drives the REAL hash
functions (audit._canonical + audit._chain_hash — the exact math append/verify use) over a SYNTHETIC chain
and asserts: a valid chain recomputes identically, and editing ANY business field (or the payload) changes
that entry's hash AND every hash after it. Pure — no DB, no audit_log, safe to run any time.

    python test_audit_tamper.py     # prints PASS / FAIL
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit  # noqa: E402

KEY = b"test-hmac-key-not-a-secret"   # any key works — tamper-evidence is a property of the linking, not the key
ok = True


def chk(cond, label):
    global ok
    print(("PASS" if cond else "FAIL") + f": {label}")
    ok = ok and bool(cond)


def _chain(entries):
    """Build the entry_hash chain exactly as audit.append does: each links to the previous via prev_hash."""
    hashes, prev = [], ""
    for e in entries:
        canonical = audit._canonical(e["actor"], e["action"], e["resource"], e["decision"], e["payload"], prev)
        h = audit._chain_hash(KEY, canonical)
        hashes.append(h)
        prev = h
    return hashes


base = [{"actor": "controller", "action": "Spend", "resource": f"prod{i}",
         "decision": "approved", "payload": {"usd": i}} for i in range(4)]
good = _chain(base)

# 1) a valid chain is reproducible — verify() (which recomputes) passes.
chk(good == _chain(base), "a valid chain recomputes to identical hashes (verify passes on an untampered chain)")

# 2) editing a business field changes THAT entry's hash — verify() would flag a mismatch = tamper caught.
t_decision = [dict(e) for e in base]
t_decision[1]["decision"] = "denied"                      # flip an approval to a denial (the classic forgery)
c1 = _chain(t_decision)
chk(c1[1] != good[1], "editing a decision field changes its entry_hash (tamper DETECTED at that row)")
chk(c1[2] != good[2] and c1[3] != good[3], "the tamper PROPAGATES — every later hash changes too (no silent splice)")

# 3) editing the payload (e.g. changing a $ amount) is caught.
t_payload = [dict(e) for e in base]
t_payload[0] = dict(t_payload[0], payload={"usd": 999999})
chk(_chain(t_payload)[0] != good[0], "editing the payload ($ amount) changes the entry_hash (tamper DETECTED)")

# 4) editing the actor (impersonation) is caught.
t_actor = [dict(e) for e in base]
t_actor[2] = dict(t_actor[2], actor="attacker")
chk(_chain(t_actor)[2] != good[2], "editing the actor (impersonation) changes the entry_hash (tamper DETECTED)")

# 5) a different key yields different hashes — the HMAC binds to the secret (no forgery without the key).
alt = []
prev = ""
for e in base:
    c = audit._canonical(e["actor"], e["action"], e["resource"], e["decision"], e["payload"], prev)
    h = audit._chain_hash(b"a-different-key", c)
    alt.append(h)
    prev = h
chk(alt[0] != good[0], "the chain hash binds to the HMAC secret (can't be forged without the key)")

print("PASS: audit log is genuinely tamper-EVIDENT — any edit to any field is detected + propagates"
      if ok else "FAIL: tamper-evidence is broken")
sys.exit(0 if ok else 1)
