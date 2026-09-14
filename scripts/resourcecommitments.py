#!/usr/bin/env python3
"""Durable reservations, commitments, actuals, and reconciliation.

This is an evidence ledger, not a budgeting robot.  It snapshots the authority
under which a resource was promised and exposes pressure/variance facts so an
agent manager can decide whether to proceed, re-plan, seek broader authority, or
release capacity.  An explicit reservation may therefore reveal over-allocation;
it never makes the condition disappear by silently dropping somebody's work.
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import audit  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

_AUTHORITY_KINDS = {"standing", "manager", "human", "contractual"}
_LIVE = {"reserved", "committed"}
_TRANSITIONS = {
    "reserved": {"committed", "released", "expired", "cancelled"},
    "committed": {"released", "expired", "reconciling"},
    "released": {"reconciling", "reconciled"},
    "expired": {"reconciling", "reconciled"},
    "reconciling": {"reconciled"},
    "reconciled": set(),
    "cancelled": set(),
}
_ensured = False
_ensure_lock = threading.Lock()


def ensure():
    """Install the additive migration on upgraded and fresh deployments."""
    global _ensured
    if _ensured:
        return True
    with _ensure_lock:
        if not _ensured:
            migration = SCRIPTS.parent / "postgres" / "initdb" / "63-resource-commitments.sql"
            with connection() as c, c.cursor() as cur:
                cur.execute(migration.read_text())
            _ensured = True
    return True


def _required(value, label):
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{label} is required")
    return value


def _amount(value, label, *, allow_zero=False):
    try:
        out = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{label} must be numeric") from None
    if not out.is_finite() or out < 0 or (not allow_zero and out == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be {qualifier}")
    return out


def normalize_pool(name, resource_kind, unit, capacity, accountable_owner,
                   *, window_start=None, window_end=None):
    capacity = _amount(capacity, "capacity", allow_zero=True)
    if window_start and window_end and window_end <= window_start:
        raise ValueError("resource window_end must be after window_start")
    return {"name": _required(name, "pool name"),
            "resource_kind": _required(resource_kind, "resource kind").lower(),
            "unit": _required(unit, "resource unit"), "capacity": capacity,
            "accountable_owner": _required(accountable_owner, "accountable owner"),
            "window_start": window_start, "window_end": window_end}


def normalize_commitment(description, expected_amount, reserved_amount, *, objective_id=None,
                         work_contract_id=None, authority_kind, authority_reference,
                         authority_snapshot, approval_reference=None, approved_by,
                         valid_from=None, expires_at=None):
    """Validate the complete management/authority context for one reservation."""
    if not objective_id and not work_contract_id:
        raise ValueError("objective_id or work_contract_id is required")
    kind = str(authority_kind or "").strip().lower()
    if kind not in _AUTHORITY_KINDS:
        raise ValueError(f"unknown authority kind: {kind!r}")
    if not isinstance(authority_snapshot, dict) or not authority_snapshot:
        raise ValueError("authority_snapshot is required")
    start = valid_from or datetime.now(timezone.utc)
    if expires_at is not None and expires_at <= start:
        raise ValueError("expires_at must be after valid_from")
    return {
        "description": _required(description, "commitment description"),
        "expected_amount": _amount(expected_amount, "expected amount", allow_zero=True),
        "reserved_amount": _amount(reserved_amount, "reserved amount"),
        "objective_id": str(objective_id) if objective_id else None,
        "work_contract_id": str(work_contract_id) if work_contract_id else None,
        "authority_kind": kind,
        "authority_reference": _required(authority_reference, "authority reference"),
        "authority_snapshot": dict(authority_snapshot),
        "approval_reference": str(approval_reference).strip() if approval_reference else None,
        "approved_by": _required(approved_by, "approved_by"),
        "valid_from": start, "expires_at": expires_at,
    }


def lifecycle_transition(current, target, *, actual_amount=0, reason=None):
    """Pure state guard used by release, expiry, commitment, and reconciliation."""
    current, target = str(current), str(target)
    if current not in _TRANSITIONS or target not in _TRANSITIONS[current]:
        raise ValueError(f"invalid resource commitment transition: {current} -> {target}")
    actual = _amount(actual_amount, "actual amount", allow_zero=True)
    if target == "cancelled" and actual:
        raise ValueError("a commitment with actual usage must be reconciled, not cancelled")
    if target in {"released", "expired"} and not str(reason or "").strip():
        raise ValueError(f"{target} transition requires a reason")
    return {"from_status": current, "to_status": target,
            "requires_reconciliation": bool(actual) and target in {"released", "expired"}}


def oversubscription_facts(pools, commitments):
    """Calculate pool pressure from plain records without prescribing an action."""
    by_pool = defaultdict(list)
    for item in commitments:
        by_pool[str(item["pool_id"])].append(item)
    facts = []
    seen = set()
    for pool in pools:
        pid = str(pool["pool_id"])
        if pid in seen:
            raise ValueError(f"duplicate resource pool: {pid}")
        seen.add(pid)
        capacity = _amount(pool["capacity"], "capacity", allow_zero=True)
        rows = by_pool.pop(pid, [])
        live = [r for r in rows if r.get("status") in _LIVE]
        reserved = sum((_amount(r.get("reserved_amount", 0), "reserved amount", allow_zero=True)
                        for r in live), Decimal(0))
        expected = sum((_amount(r.get("expected_amount", 0), "expected amount", allow_zero=True)
                        for r in live), Decimal(0))
        actual = sum((_amount(r.get("actual_amount", 0), "actual amount", allow_zero=True)
                      for r in rows), Decimal(0))
        over = max(Decimal(0), reserved - capacity)
        facts.append({
            "pool_id": pid, "resource_kind": pool.get("resource_kind"),
            "unit": pool.get("unit"), "capacity": capacity,
            "reserved": reserved, "expected": expected, "actual": actual,
            "available": capacity - reserved, "oversubscribed_by": over,
            "oversubscribed": over > 0,
            "utilization": (reserved / capacity if capacity else (Decimal(0) if not reserved else None)),
            "live_commitment_ids": [str(r["commitment_id"]) for r in live],
        })
    if by_pool:
        raise ValueError(f"commitments reference unknown pools: {sorted(by_pool)}")
    return facts


def variance_facts(expected_amount, reserved_amount, actual_amount):
    """Return neutral reconciliation math; management interprets the variance."""
    expected = _amount(expected_amount, "expected amount", allow_zero=True)
    reserved = _amount(reserved_amount, "reserved amount", allow_zero=True)
    actual = _amount(actual_amount, "actual amount", allow_zero=True)
    variance = actual - expected
    return {"expected_amount": expected, "reserved_amount": reserved,
            "actual_amount": actual, "variance_amount": variance,
            "variance_ratio": (variance / expected if expected else None),
            "unused_reservation": reserved - actual}


def create_pool(tenant_id, name, resource_kind, unit, capacity, accountable_owner,
                *, created_by, org_id=None, pool_id=None, window_start=None, window_end=None):
    ensure()
    p = normalize_pool(name, resource_kind, unit, capacity, accountable_owner,
                       window_start=window_start, window_end=window_end)
    pid = str(pool_id or f"rp-{uuid.uuid4().hex}")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO resource_pools
            (pool_id,tenant_id,org_id,name,resource_kind,unit,capacity,window_start,window_end,
             accountable_owner,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (pid, str(tenant_id), org_id, p["name"], p["resource_kind"], p["unit"],
             p["capacity"], p["window_start"], p["window_end"],
             p["accountable_owner"], created_by))
    return {"pool_id": pid, **p, "state": "active"}


def reserve(tenant_id, pool_id, description, expected_amount, reserved_amount, *, created_by,
            objective_id=None, work_contract_id=None, authority_kind,
            authority_reference, authority_snapshot, approval_reference=None, approved_by,
            commitment_id=None, valid_from=None, expires_at=None):
    """Durably reserve a resource; returns current pressure as evidence."""
    ensure()
    n = normalize_commitment(
        description, expected_amount, reserved_amount, objective_id=objective_id,
        work_contract_id=work_contract_id, authority_kind=authority_kind,
        authority_reference=authority_reference, authority_snapshot=authority_snapshot,
        approval_reference=approval_reference, approved_by=approved_by,
        valid_from=valid_from, expires_at=expires_at)
    cid = str(commitment_id or f"rc-{uuid.uuid4().hex}")
    tenant = str(tenant_id)
    with tenant_connection(tenant) as c, c.cursor() as cur:
        cur.execute("""SELECT capacity,state,resource_kind,unit FROM resource_pools
                       WHERE tenant_id=%s AND pool_id=%s FOR UPDATE""", (tenant, str(pool_id)))
        pool = cur.fetchone()
        if not pool:
            raise KeyError(pool_id)
        if pool[1] != "active":
            raise ValueError(f"resource pool is {pool[1]}")
        cur.execute("""INSERT INTO resource_commitments
            (commitment_id,tenant_id,pool_id,objective_id,work_contract_id,description,
             expected_amount,reserved_amount,authority_kind,authority_reference,
             authority_snapshot,approval_reference,approved_by,valid_from,expires_at,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (cid, tenant, str(pool_id), n["objective_id"], n["work_contract_id"],
             n["description"], n["expected_amount"], n["reserved_amount"], n["authority_kind"],
             n["authority_reference"], json.dumps(n["authority_snapshot"]),
             n["approval_reference"], n["approved_by"], n["valid_from"], n["expires_at"], created_by))
        cur.execute("""INSERT INTO resource_commitment_events
            (tenant_id,commitment_id,actor,event_kind,to_status,facts)
            VALUES (%s,%s,%s,'reserved','reserved',%s)""",
            (tenant, cid, created_by, json.dumps({"authority_reference": n["authority_reference"]})))
        cur.execute("""SELECT commitment_id,expected_amount,reserved_amount,actual_amount,status
                       FROM resource_commitments WHERE tenant_id=%s AND pool_id=%s""",
                    (tenant, str(pool_id)))
        rows = [{"commitment_id": r[0], "pool_id": str(pool_id), "expected_amount": r[1],
                 "reserved_amount": r[2], "actual_amount": r[3], "status": r[4]}
                for r in cur.fetchall()]
    pressure = oversubscription_facts([{"pool_id": str(pool_id), "capacity": pool[0],
        "resource_kind": pool[2], "unit": pool[3]}], rows)[0]
    audit.append(actor=created_by, action="ResourceReserved", resource=cid,
                 decision="oversubscribed" if pressure["oversubscribed"] else "reserved",
                 payload={"pool_id": str(pool_id), "oversubscribed_by": str(pressure["oversubscribed_by"]),
                          "authority_reference": n["authority_reference"]}, tenant_id=tenant)
    return {"commitment_id": cid, **n, "pool_id": str(pool_id), "status": "reserved",
            "pressure": pressure}


def transition(tenant_id, commitment_id, target, actor, *, reason=None):
    ensure()
    tenant = str(tenant_id)
    with tenant_connection(tenant) as c, c.cursor() as cur:
        cur.execute("""SELECT status,actual_amount FROM resource_commitments
                       WHERE tenant_id=%s AND commitment_id=%s FOR UPDATE""",
                    (tenant, str(commitment_id)))
        row = cur.fetchone()
        if not row:
            raise KeyError(commitment_id)
        result = lifecycle_transition(row[0], target, actual_amount=row[1], reason=reason)
        effective = "reconciling" if result["requires_reconciliation"] else str(target)
        cur.execute("""UPDATE resource_commitments SET status=%s,release_reason=%s,
                       released_at=CASE WHEN %s IN ('released','expired') THEN now() ELSE released_at END,
                       reconciliation_due_at=CASE WHEN %s='reconciling' THEN now() ELSE reconciliation_due_at END,
                       version=version+1,updated_at=now()
                       WHERE tenant_id=%s AND commitment_id=%s""",
                    (effective, reason, target, effective, tenant, str(commitment_id)))
        cur.execute("""INSERT INTO resource_commitment_events
            (tenant_id,commitment_id,actor,event_kind,from_status,to_status,facts)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (tenant, str(commitment_id), actor, str(target), row[0], effective,
             json.dumps({"reason": reason, "requested_status": target})))
    return {**result, "to_status": effective, "commitment_id": str(commitment_id)}


def commit(tenant_id, commitment_id, actor):
    """Confirm that a reservation has become an external/internal commitment."""
    return transition(tenant_id, commitment_id, "committed", actor)


def release(tenant_id, commitment_id, actor, reason):
    """Release unused capacity; consumed commitments enter reconciliation."""
    return transition(tenant_id, commitment_id, "released", actor, reason=reason)


def cancel(tenant_id, commitment_id, actor):
    """Cancel an unused reservation before it becomes a commitment."""
    return transition(tenant_id, commitment_id, "cancelled", actor)


def record_actual(tenant_id, commitment_id, delta_amount, *, kind, source_reference,
                  evidence, recorded_by, occurred_at=None, idempotency_key=None):
    """Append idempotent usage/refund evidence and update the current projection."""
    ensure()
    kind = str(kind).lower()
    if kind not in {"usage", "refund", "correction"}:
        raise ValueError("actual kind must be usage, refund, or correction")
    delta = Decimal(str(delta_amount))
    if not delta.is_finite() or delta == 0:
        raise ValueError("actual delta must be finite and non-zero")
    if kind == "usage" and delta < 0:
        raise ValueError("usage delta must be positive")
    if kind == "refund" and delta > 0:
        raise ValueError("refund delta must be negative")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("actual evidence is required")
    tenant, cid = str(tenant_id), str(commitment_id)
    key = str(idempotency_key or f"actual-{uuid.uuid4().hex}")
    with tenant_connection(tenant) as c, c.cursor() as cur:
        cur.execute("""SELECT status,actual_amount FROM resource_commitments
                       WHERE tenant_id=%s AND commitment_id=%s FOR UPDATE""", (tenant, cid))
        row = cur.fetchone()
        if not row:
            raise KeyError(commitment_id)
        if row[0] in {"reconciled", "cancelled"}:
            raise ValueError(f"cannot record actuals on a {row[0]} commitment")
        if Decimal(str(row[1])) + delta < 0:
            raise ValueError("actual adjustment would make total usage negative")
        cur.execute("""INSERT INTO resource_actuals
            (tenant_id,commitment_id,idempotency_key,delta_amount,kind,source_reference,
             evidence,recorded_by,occurred_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id,idempotency_key) DO NOTHING RETURNING actual_id""",
            (tenant, cid, key, delta, kind, _required(source_reference, "source reference"),
             json.dumps(evidence), recorded_by, occurred_at or datetime.now(timezone.utc)))
        inserted = cur.fetchone()
        if not inserted:
            cur.execute("""SELECT actual_id,commitment_id,delta_amount FROM resource_actuals
                           WHERE tenant_id=%s AND idempotency_key=%s""", (tenant, key))
            old = cur.fetchone()
            if old[1] != cid or Decimal(str(old[2])) != delta:
                raise ValueError("idempotency key was already used for a different actual")
            return {"actual_id": old[0], "duplicate": True, "commitment_id": cid}
        cur.execute("""UPDATE resource_commitments SET actual_amount=actual_amount+%s,
                       version=version+1,updated_at=now()
                       WHERE tenant_id=%s AND commitment_id=%s RETURNING actual_amount""",
                    (delta, tenant, cid))
        total = cur.fetchone()[0]
    return {"actual_id": inserted[0], "duplicate": False, "commitment_id": cid,
            "delta_amount": delta, "actual_amount": total}


def reconcile(tenant_id, commitment_id, disposition, rationale, evidence, *, actor):
    ensure()
    disposition = str(disposition).lower()
    if disposition not in {"accepted", "adjusted", "disputed"}:
        raise ValueError("unknown reconciliation disposition")
    rationale = _required(rationale, "reconciliation rationale")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("reconciliation evidence is required")
    tenant, cid = str(tenant_id), str(commitment_id)
    with tenant_connection(tenant) as c, c.cursor() as cur:
        cur.execute("""SELECT status,expected_amount,reserved_amount,actual_amount
                       FROM resource_commitments WHERE tenant_id=%s AND commitment_id=%s FOR UPDATE""",
                    (tenant, cid))
        row = cur.fetchone()
        if not row:
            raise KeyError(commitment_id)
        current = row[0]
        if current not in {"committed", "released", "expired", "reconciling"}:
            raise ValueError(f"cannot reconcile a {current} commitment")
        variance = variance_facts(row[1], row[2], row[3])["variance_amount"]
        final_status = "reconciling" if disposition == "disputed" else "reconciled"
        cur.execute("""INSERT INTO resource_reconciliations
            (tenant_id,commitment_id,expected_amount,reserved_amount,actual_amount,variance_amount,
             disposition,rationale,evidence,reconciled_by)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING reconciliation_id""",
            (tenant, cid, row[1], row[2], row[3], variance, disposition, rationale,
             json.dumps(evidence), actor))
        rid = cur.fetchone()[0]
        cur.execute("""UPDATE resource_commitments SET status=%s,
                       reconciled_at=CASE WHEN %s='reconciled' THEN now() ELSE NULL END,
                       reconciliation_due_at=CASE WHEN %s='reconciling' THEN now() ELSE NULL END,
                       version=version+1,updated_at=now()
                       WHERE tenant_id=%s AND commitment_id=%s""",
                    (final_status, final_status, final_status, tenant, cid))
        cur.execute("""INSERT INTO resource_commitment_events
            (tenant_id,commitment_id,actor,event_kind,from_status,to_status,facts)
            VALUES (%s,%s,%s,'reconciled',%s,%s,%s)""",
            (tenant, cid, actor, current, final_status,
             json.dumps({"disposition": disposition, "variance_amount": str(variance)})))
    return {"reconciliation_id": rid, "commitment_id": cid, "status": final_status,
            "expected_amount": row[1], "reserved_amount": row[2],
            "actual_amount": row[3], "variance_amount": variance,
            "disposition": disposition}


def expire_due(tenant_id, *, actor="resource-controller", limit=100):
    """Claim and expire due reservations; actual usage moves them to reconciliation."""
    ensure()
    tenant = str(tenant_id)
    expired = []
    with tenant_connection(tenant) as c, c.cursor() as cur:
        cur.execute("""SELECT commitment_id,status,actual_amount FROM resource_commitments
                       WHERE tenant_id=%s AND status IN ('reserved','committed')
                         AND expires_at IS NOT NULL AND expires_at<=now()
                       ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT %s""", (tenant, int(limit)))
        for cid, old, actual in cur.fetchall():
            status = "reconciling" if Decimal(str(actual)) else "expired"
            cur.execute("""UPDATE resource_commitments SET status=%s,release_reason='authority expired',
                           released_at=now(),reconciliation_due_at=CASE WHEN %s='reconciling' THEN now() END,
                           version=version+1,updated_at=now()
                           WHERE tenant_id=%s AND commitment_id=%s""",
                        (status, status, tenant, cid))
            cur.execute("""INSERT INTO resource_commitment_events
                (tenant_id,commitment_id,actor,event_kind,from_status,to_status,facts)
                VALUES (%s,%s,%s,'expired',%s,%s,'{}')""", (tenant, cid, actor, old, status))
            expired.append({"commitment_id": cid, "status": status})
    return expired


def pool_facts(tenant_id, pool_ids=None):
    """Return current reservation pressure for periodic or state-change review."""
    ensure()
    tenant = str(tenant_id)
    ids = [str(x) for x in (pool_ids or [])]
    with tenant_connection(tenant) as c, c.cursor() as cur:
        cur.execute("""SELECT pool_id,resource_kind,unit,capacity FROM resource_pools
                       WHERE tenant_id=%s AND (cardinality(%s::text[])=0 OR pool_id=ANY(%s))
                       ORDER BY pool_id""", (tenant, ids, ids))
        pools = [{"pool_id": r[0], "resource_kind": r[1], "unit": r[2], "capacity": r[3]}
                 for r in cur.fetchall()]
        cur.execute("""SELECT commitment_id,pool_id,expected_amount,reserved_amount,actual_amount,status
                       FROM resource_commitments WHERE tenant_id=%s
                         AND (cardinality(%s::text[])=0 OR pool_id=ANY(%s))""", (tenant, ids, ids))
        commitments = [{"commitment_id": r[0], "pool_id": r[1], "expected_amount": r[2],
                        "reserved_amount": r[3], "actual_amount": r[4], "status": r[5]}
                       for r in cur.fetchall()]
    return oversubscription_facts(pools, commitments)
