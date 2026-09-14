#!/usr/bin/env python3
"""Objective ownership contracts for a human-like agent organization.

This module does not decide who should do work.  Agents make that judgment.  It
provides the organizational facts needed to make it safely: a measurable outcome,
one DRI, a manager, capacity cost, check-in promise, continuity coverage, and an
offer/acknowledgement protocol.  Delegation never creates an ownership vacuum:
the old DRI remains accountable until the proposed DRI explicitly accepts.

``attention_evidence`` is intentionally diagnostic.  Time is a reason for a
manager to look, not proof that slow work failed and not an automatic stop signal.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import audit  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

_RISK = {"low", "medium", "high", "critical"}
_TERMINAL = {"completed", "cancelled"}
_ensured = False
_ensure_lock = threading.Lock()


def ensure():
    """Install the additive schema on upgraded deployments as well as fresh ones."""
    global _ensured
    if _ensured:
        return True
    with _ensure_lock:
        if _ensured:
            return True
        migration = SCRIPTS.parent / "postgres" / "initdb" / "59-work-contracts.sql"
        with connection() as c, c.cursor() as cur:
            cur.execute(migration.read_text())
        _ensured = True
    return True


def normalize_contract(objective, acceptance_contract, accountable_owner, manager_owner,
                       *, backup_owner=None, priority=3, risk="medium", capacity_units=1,
                       update_cadence_s=900, constraints=None, dependencies=None):
    """Validate facts/invariants without making a staffing decision."""
    objective = str(objective or "").strip()
    owner = str(accountable_owner or "").strip()
    manager = str(manager_owner or "").strip()
    acceptance = dict(acceptance_contract or {})
    if not objective:
        raise ValueError("objective is required")
    if not owner or not manager:
        raise ValueError("accountable_owner and manager_owner are required")
    if not acceptance:
        raise ValueError("acceptance_contract must state observable completion evidence")
    risk = str(risk or "").lower()
    if risk not in _RISK:
        raise ValueError(f"unknown risk: {risk}")
    priority = int(priority)
    capacity_units = float(capacity_units)
    cadence = int(update_cadence_s)
    if not 0 <= priority <= 5:
        raise ValueError("priority must be between 0 and 5")
    if capacity_units <= 0:
        raise ValueError("capacity_units must be positive")
    if cadence < 30:
        raise ValueError("update cadence cannot be below 30 seconds")
    return {"objective": objective, "acceptance_contract": acceptance,
            "accountable_owner": owner, "manager_owner": manager,
            "backup_owner": str(backup_owner).strip() if backup_owner else None,
            "priority": priority, "risk": risk, "capacity_units": capacity_units,
            "update_cadence_s": cadence, "constraints": dict(constraints or {}),
            "dependencies": list(dependencies or [])}


def create(tenant_id, objective, acceptance_contract, accountable_owner, manager_owner,
           *, created_by, org_id=None, parent_contract_id=None, contract_id=None, **terms):
    ensure()
    normalized = normalize_contract(objective, acceptance_contract, accountable_owner,
                                    manager_owner, **terms)
    cid = str(contract_id or f"wc-{uuid.uuid4().hex}")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO work_contracts
            (contract_id,tenant_id,org_id,parent_contract_id,objective,acceptance_contract,
             constraints,dependencies,accountable_owner,manager_owner,backup_owner,priority,
             risk,capacity_units,update_cadence_s,next_checkin_at,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    now()+(%s*interval '1 second'),%s)""",
            (cid, str(tenant_id), org_id, parent_contract_id, normalized["objective"],
             json.dumps(normalized["acceptance_contract"]), json.dumps(normalized["constraints"]),
             json.dumps(normalized["dependencies"]), normalized["accountable_owner"],
             normalized["manager_owner"], normalized["backup_owner"], normalized["priority"],
             normalized["risk"], normalized["capacity_units"], normalized["update_cadence_s"],
             normalized["update_cadence_s"], created_by))
    audit.append(actor=created_by, action="WorkContractCreated", resource=cid,
                 decision="owner_accountable", payload={"owner": normalized["accountable_owner"]},
                 tenant_id=str(tenant_id))
    return {"contract_id": cid, **normalized, "status": "active", "ownership_revision": 1}


def offer_delegation(tenant_id, contract_id, proposed_owner, *, proposed_by,
                     reason, context=None, expected_output=None, reply_within_s=300):
    """Offer a handoff; ownership deliberately remains unchanged."""
    ensure()
    if not str(proposed_owner or "").strip():
        raise ValueError("proposed_owner is required")
    if not str(reason or "").strip():
        raise ValueError("delegation reason is required")
    did = f"wd-{uuid.uuid4().hex}"
    wait_s = max(30, int(reply_within_s))
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT accountable_owner,objective,acceptance_contract,constraints,
                              dependencies,capacity_units,ownership_revision,status
                       FROM work_contracts WHERE tenant_id=%s AND contract_id=%s FOR UPDATE""",
                    (str(tenant_id), str(contract_id)))
        row = cur.fetchone()
        if not row:
            raise KeyError(contract_id)
        if row[7] in _TERMINAL:
            raise ValueError(f"cannot delegate a {row[7]} contract")
        brief = {"objective": row[1], "acceptance_contract": row[2], "constraints": row[3],
                 "dependencies": row[4], "capacity_units": float(row[5]), "reason": reason,
                 "context": context or {}, "expected_output": expected_output or {}}
        cur.execute("""INSERT INTO work_delegations
            (delegation_id,tenant_id,contract_id,from_owner,proposed_owner,proposed_by,
             brief,reply_due_at,ownership_revision)
            VALUES (%s,%s,%s,%s,%s,%s,%s,now()+(%s*interval '1 second'),%s)""",
            (did, str(tenant_id), str(contract_id), row[0], str(proposed_owner), proposed_by,
             json.dumps(brief), wait_s, row[6]))
    audit.append(actor=proposed_by, action="DelegationOffered", resource=did,
                 decision="awaiting_ack", payload={"contract_id": contract_id,
                 "from": row[0], "to": proposed_owner}, tenant_id=str(tenant_id))
    return {"delegation_id": did, "status": "offered", "owner": row[0],
            "proposed_owner": str(proposed_owner), "brief": brief}


def delegation_transition(status, response, *, actor, proposed_owner,
                          current_revision, offered_revision):
    """Pure state-machine guard used by the durable response path."""
    choice = str(response or "").lower()
    if status != "offered":
        raise ValueError(f"delegation is already {status}")
    if actor != proposed_owner:
        raise PermissionError("only the proposed owner may answer this delegation")
    if choice not in {"accepted", "rejected", "countered"}:
        raise ValueError("response must be accepted, rejected, or countered")
    if choice == "accepted" and int(current_revision) != int(offered_revision):
        raise ValueError("ownership changed after this offer; issue a fresh delegation")
    return {"delegation_status": choice, "transfer_owner": choice == "accepted"}


def respond(tenant_id, delegation_id, actor, response, *, rationale="", counter=None):
    """Acknowledge an offer and atomically transfer ownership only on acceptance."""
    ensure()
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT d.contract_id,d.status,d.proposed_owner,d.ownership_revision,
                              w.ownership_revision,w.accountable_owner
                       FROM work_delegations d JOIN work_contracts w ON w.contract_id=d.contract_id
                       WHERE d.tenant_id=%s AND d.delegation_id=%s FOR UPDATE OF d,w""",
                    (str(tenant_id), str(delegation_id)))
        row = cur.fetchone()
        if not row:
            raise KeyError(delegation_id)
        transition = delegation_transition(row[1], response, actor=actor, proposed_owner=row[2],
                                           current_revision=row[4], offered_revision=row[3])
        detail = {"rationale": str(rationale), "counter": counter or {}}
        cur.execute("""UPDATE work_delegations SET status=%s,response=%s,responded_at=now()
                       WHERE delegation_id=%s""",
                    (transition["delegation_status"], json.dumps(detail), str(delegation_id)))
        if transition["transfer_owner"]:
            cur.execute("""UPDATE work_contracts SET accountable_owner=%s,
                             ownership_revision=ownership_revision+1,updated_at=now()
                           WHERE contract_id=%s""", (actor, row[0]))
    audit.append(actor=actor, action="DelegationResponded", resource=str(delegation_id),
                 decision=transition["delegation_status"], payload={"contract_id": row[0],
                 "previous_owner": row[5]}, tenant_id=str(tenant_id))
    return {**transition, "contract_id": row[0],
            "accountable_owner": actor if transition["transfer_owner"] else row[5]}


def update(tenant_id, contract_id, actor, kind, evidence=None, *, substantive=False):
    """Record grounded progress. Mere heartbeats do not reset the progress promise."""
    ensure()
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO work_contract_updates
            (tenant_id,contract_id,actor,kind,substantive,evidence)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING update_id""",
            (str(tenant_id), str(contract_id), actor, kind, bool(substantive),
             json.dumps(evidence or {})))
        uid = cur.fetchone()[0]
        if substantive:
            cur.execute("""UPDATE work_contracts SET last_substantive_update_at=now(),
                             next_checkin_at=now()+(update_cadence_s*interval '1 second'),updated_at=now()
                           WHERE tenant_id=%s AND contract_id=%s""",
                        (str(tenant_id), str(contract_id)))
    return {"update_id": uid, "substantive": bool(substantive)}


def attention_evidence(tenant_id, limit=100):
    """Facts needing agentic review; no elapsed-time conclusion is hardcoded."""
    ensure()
    now = datetime.now(timezone.utc)
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT w.contract_id,w.objective,w.accountable_owner,w.manager_owner,w.backup_owner,
                              w.status,w.next_checkin_at,w.last_substantive_update_at
                       FROM work_contracts w
                       JOIN tenants t ON t.tenant_id=w.tenant_id
                       WHERE w.tenant_id=%s AND w.status IN ('active','blocked')
                         AND COALESCE(w.constraints->>'execution_scope','production')='production'
                         AND (w.next_checkin_at<=now() OR w.backup_owner IS NULL)
                       ORDER BY w.next_checkin_at LIMIT %s""", (str(tenant_id), int(limit)))
        contracts = cur.fetchall()
        cur.execute("""SELECT d.delegation_id,d.contract_id,d.from_owner,d.proposed_owner,d.reply_due_at
                       FROM work_delegations d
                       JOIN work_contracts w ON w.contract_id=d.contract_id AND w.tenant_id=d.tenant_id
                       JOIN tenants t ON t.tenant_id=d.tenant_id
                       WHERE d.tenant_id=%s AND d.status='offered'
                         AND COALESCE(w.constraints->>'execution_scope','production')='production'
                         AND d.reply_due_at<=now() ORDER BY d.reply_due_at LIMIT %s""",
                    (str(tenant_id), int(limit)))
        handoffs = cur.fetchall()
    out = []
    for cid, objective, owner, manager, backup, status, due, progress_at in contracts:
        reasons = []
        if due <= now:
            reasons.append("checkin_due")
        if not backup:
            reasons.append("no_continuity_owner")
        out.append({"kind": "work_contract", "contract_id": cid, "objective": objective,
                    "accountable_owner": owner, "manager_owner": manager, "status": status,
                    "evidence": reasons, "next_checkin_at": str(due),
                    "last_substantive_update_at": str(progress_at)})
    out.extend({"kind": "delegation_reply_overdue", "delegation_id": did,
                "contract_id": cid, "accountable_owner": frm, "proposed_owner": to,
                "reply_due_at": str(due)} for did, cid, frm, to, due in handoffs)
    return out


def owner_load(tenant_id, owners=None):
    """Return committed load facts for an agentic staffing/capacity decision."""
    ensure()
    owners = [str(x) for x in (owners or []) if str(x).strip()]
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT accountable_owner,count(*),sum(capacity_units),
                              count(*) FILTER (WHERE risk IN ('high','critical')),
                              min(next_checkin_at)
                       FROM work_contracts w JOIN tenants t ON t.tenant_id=w.tenant_id
                       WHERE w.tenant_id=%s AND w.status IN ('active','blocked')
                         AND COALESCE(w.constraints->>'execution_scope','production')='production'
                         AND (cardinality(%s::text[])=0 OR accountable_owner=ANY(%s))
                       GROUP BY accountable_owner ORDER BY sum(capacity_units) DESC""",
                    (str(tenant_id), owners, owners))
        return [{"owner": r[0], "active_contracts": r[1], "capacity_units": float(r[2]),
                 "high_risk_contracts": r[3], "next_checkin_at": str(r[4])}
                for r in cur.fetchall()]
