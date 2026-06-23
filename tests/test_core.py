"""Real unit/integration tests for agent-os load-bearing invariants — the correctness properties an
acquirer's engineering due-diligence would actually probe. Run: .venv/bin/python -m pytest tests/ -q
(needs the local Postgres up, as the platform does)."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "platform"))


def _rid():
    return os.urandom(4).hex()


# ── governance: the audit log is tamper-evident ──────────────────────────────
def test_audit_chain_intact_after_append():
    import audit
    audit.append(actor="pytest", action="UnitTest", resource=f"r-{_rid()}", decision="executed")
    ok, reason = audit.verify()
    assert ok, f"audit chain broken: {reason}"


# ── self-healing: the responder routes safely (auto vs escalate vs unknown) ───
def test_responder_classifies_safe_vs_judgement_vs_novel():
    import responder
    assert responder.classify({"sig": "daemon:dashboard", "msg": "dashboard process is DOWN"}) == "auto"
    assert responder.classify({"sig": "alert:postgres is DOWN", "msg": "postgres is DOWN"}) == "auto"
    assert responder.classify({"sig": "alert:disk", "msg": "disk 95% — critical"}) == "auto"
    # judgement / approval calls must NEVER auto-act
    assert responder.classify({"sig": "alert:DEADLOCK", "msg": "DEADLOCK: 1 cycle(s)"}) == "escalate"
    assert responder.classify({"sig": "stall:x", "msg": "build x silent 9m"}) == "escalate"
    assert responder.classify({"sig": "alert:conflict", "msg": "conflict on src/**"}) == "escalate"
    # genuinely novel -> hand to the reasoning agent, not a blind action
    assert responder.classify({"sig": "unknown:weird", "msg": "never seen this"}) == "unknown"


# ── directory: conflict detection is correct and has no false positives ───────
def test_directory_detects_real_conflict_only():
    import directory
    p = f"prod-{_rid()}"
    directory.register(f"a@{p}", "builder", p, "BUILD", ["src/**", "tests/**"])
    directory.register(f"b@{p}", "staff-engineer", p, "REFACTOR", ["src/**"])
    directory.register(f"c@{p}", "design-ux", p, "DESIGN", ["design/**"])
    try:
        pairs = {frozenset(c["agents"]) for c in directory.conflicts() if c["product"] == p}
        assert frozenset({f"a@{p}", f"b@{p}"}) in pairs           # overlapping src/** -> conflict
        assert frozenset({f"a@{p}", f"c@{p}"}) not in pairs       # disjoint paths -> NO false conflict
    finally:
        for a in (f"a@{p}", f"b@{p}", f"c@{p}"):
            directory.release(a)


def test_directory_contact_is_brokered_not_socket():
    import directory
    m = directory.contact("x@p", "y@p", "ask", "hello")
    assert m["message_id"].startswith("dm-") and m["to"] == "y@p"   # durable message id, no connection


# ── billing: invoice math (base, overage, quota) is correct ──────────────────
def test_billing_base_invoice_with_no_usage():
    import billing
    t = billing.signup(f"acme-{_rid()}", "pro")
    inv = billing.invoice(t["tenant_id"])
    assert inv["base"] == 49 and inv["overage"]["cost"] == 0 and inv["total"] == 49


def test_billing_unknown_plan_rejected():
    import billing
    with pytest.raises(ValueError):
        billing.signup("x", "platinum-unicorn")


# ── snapshots: encryption round-trips and rejects the wrong passphrase ────────
def test_snapshot_crypto_roundtrip_and_wrong_pass():
    from cryptography.fernet import Fernet
    import snapshot
    salt = os.urandom(16)
    tok = Fernet(snapshot._key(b"right-pass", salt)).encrypt(b"secret-state")
    assert Fernet(snapshot._key(b"right-pass", salt)).decrypt(tok) == b"secret-state"
    with pytest.raises(Exception):
        Fernet(snapshot._key(b"wrong-pass", salt)).decrypt(tok)


# ── skills registry: catalog integrity (engines/statuses are valid) ──────────
def test_skills_catalog_is_well_formed():
    import skills
    valid_engine = {"claude", "tool", "device", "gpu", "paid"}
    valid_status = {"ready", "needs_setup", "needs_key"}
    names = set()
    for name, cat, desc, roles, tools, status, engine in skills.CATALOG:
        assert engine in valid_engine, f"{name}: bad engine {engine}"
        assert status in valid_status, f"{name}: bad status {status}"
        assert roles and tools, f"{name}: missing roles/tools"
        assert name not in names, f"duplicate skill {name}"
        names.add(name)
    assert len(names) >= 90
