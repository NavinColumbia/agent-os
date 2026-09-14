#!/usr/bin/env python3
"""Evidence-backed succession, role cover, and fenced operational takeover.

This module does not infer incapacity from elapsed time.  It gives agents and
managers the durable facts needed to judge cover, records that judgment, and
rotates a fencing epoch whenever authority changes so stale holders fail closed.
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import audit  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

_ensured = False
_ensure_lock = threading.Lock()


def ensure():
    global _ensured
    if _ensured:
        return True
    with _ensure_lock:
        if not _ensured:
            migration = SCRIPTS.parent / "postgres" / "initdb" / "64-succession-continuity.sql"
            with connection() as c, c.cursor() as cur:
                cur.execute(migration.read_text())
            _ensured = True
    return True


def _required(value, label):
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{label} is required")
    return value


def _evidence(value, label="evidence"):
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label} is required")
    return dict(value)


def normalize_role(role_name, scope_ref, duty_manager, *, criticality="important",
                   minimum_backups=1, readiness_max_age_s=2592000, takeover_policy=None):
    if criticality not in {"routine", "important", "critical"}:
        raise ValueError("unknown role criticality")
    minimum_backups, readiness_max_age_s = int(minimum_backups), int(readiness_max_age_s)
    if minimum_backups < 0:
        raise ValueError("minimum_backups cannot be negative")
    if readiness_max_age_s < 60:
        raise ValueError("readiness_max_age_s cannot be below 60")
    policy = dict(takeover_policy or {})
    return {"role_name": _required(role_name, "role_name"),
            "scope_ref": _required(scope_ref, "scope_ref"),
            "duty_manager": _required(duty_manager, "duty_manager"),
            "criticality": criticality, "minimum_backups": minimum_backups,
            "readiness_max_age_s": readiness_max_age_s, "takeover_policy": policy}


def validate_appointment(kind, appointee, appointed_by, decision_evidence, *, priority=1):
    if kind not in {"primary", "backup", "acting"}:
        raise ValueError("unknown appointment kind")
    priority = int(priority)
    if priority < 1:
        raise ValueError("appointment priority must be positive")
    return {"appointment_kind": kind, "appointee": _required(appointee, "appointee"),
            "appointed_by": _required(appointed_by, "appointed_by"), "priority": priority,
            "decision_evidence": _evidence(decision_evidence, "appointment decision evidence")}


def validate_takeover(predecessor, successor, trigger_kind, trigger_evidence,
                      manager_decision, decision_evidence, authorized_by):
    if trigger_kind not in {"absence", "unresponsive", "planned_handoff", "incident",
                            "manager_judgment"}:
        raise ValueError("unknown takeover trigger")
    successor = _required(successor, "successor")
    predecessor = str(predecessor).strip() if predecessor is not None else None
    if predecessor and predecessor == successor:
        raise ValueError("successor must differ from predecessor")
    return {"predecessor": predecessor, "successor": successor, "trigger_kind": trigger_kind,
            "trigger_evidence": _evidence(trigger_evidence, "takeover trigger evidence"),
            "manager_decision": _required(manager_decision, "manager decision"),
            "decision_evidence": _evidence(decision_evidence, "manager decision evidence"),
            "authorized_by": _required(authorized_by, "authorized_by")}


def validate_takeover_candidate(appointment_kind, availability_state, readiness_verified,
                                decision_evidence, *, authorized_by, duty_manager):
    """Guard normal cover while allowing an explicit, auditable emergency override."""
    proof = _evidence(decision_evidence, "manager decision evidence")
    delegated = str(authorized_by) == str(duty_manager) or bool(proof.get("delegation_ref"))
    if not delegated:
        raise PermissionError("takeover requires duty-manager authority or delegation evidence")
    safe = (appointment_kind == "backup" and availability_state in {"available", "degraded"}
            and bool(readiness_verified))
    if not safe and not (proof.get("continuity_override") and proof.get("risk_acceptance")):
        raise ValueError("unsafe takeover requires continuity_override and risk_acceptance evidence")
    return {"normal_cover": safe, "emergency_override": not safe}


def assert_fence(expected_holder, expected_epoch, supplied_holder, supplied_epoch):
    """Fail closed when work was authorized before a handoff or for another holder."""
    if str(supplied_holder) != str(expected_holder) or int(supplied_epoch) != int(expected_epoch):
        raise PermissionError("stale or foreign role fence")
    return True


def evaluate_coverage(role, appointments, availability, readiness, *, now=None):
    """Explain one role's continuity gaps from plain records for UI and tests."""
    now = now or datetime.now(timezone.utc)
    active = [dict(a) for a in appointments if a.get("status", "active") == "active" and
              (a.get("valid_until") is None or a["valid_until"] > now)]
    current = {}
    for event in sorted(availability, key=lambda x: (x.get("effective_at") or datetime.min.replace(
            tzinfo=timezone.utc), x.get("availability_event_id", 0))):
        if (event.get("effective_at") or now) <= now:
            current[str(event["subject"])] = event.get("state", "unknown")
    verified = set()
    max_age = int(role.get("readiness_max_age_s", 2592000))
    for item in readiness:
        verified_at = item.get("verified_at")
        if (item.get("readiness_state") == "verified" and verified_at and
                (now - verified_at).total_seconds() <= max_age and
                (item.get("expires_at") is None or item["expires_at"] > now)):
            verified.add(str(item["to_subject"]))
    primaries = [a for a in active if a.get("appointment_kind") == "primary"]
    backups = [a for a in active if a.get("appointment_kind") == "backup"]
    acting = [a for a in active if a.get("appointment_kind") == "acting"]
    usable = [a for a in backups if current.get(str(a.get("appointee")), "unknown") in
              {"available", "degraded"} and str(a.get("appointee")) in verified]
    gaps = []
    if len(primaries) != 1:
        gaps.append({"code": "primary_coverage", "severity": "critical",
                     "detail": f"expected 1 active primary, found {len(primaries)}"})
    required = int(role.get("minimum_backups", 1))
    if len(backups) < required:
        gaps.append({"code": "backup_depth", "severity": "critical" if role.get("criticality") == "critical" else "warning",
                     "detail": f"requires {required} backups, found {len(backups)}"})
    if required and len(usable) < required:
        gaps.append({"code": "takeover_readiness", "severity": "critical",
                     "detail": f"requires {required} available, evidence-ready backups; found {len(usable)}"})
    if primaries:
        primary = str(primaries[0].get("appointee"))
        if current.get(primary) == "unavailable" and not acting:
            gaps.append({"code": "uncovered_absence", "severity": "critical",
                         "detail": f"primary {primary} is unavailable without an acting appointment"})
    return {"role_id": role.get("role_id"), "duty_manager": role.get("duty_manager"),
            "primary": primaries[0].get("appointee") if len(primaries) == 1 else None,
            "backups": [a.get("appointee") for a in sorted(backups, key=lambda a: a.get("priority", 1))],
            "acting": [a.get("appointee") for a in acting], "ready_backups": len(usable),
            "gaps": gaps, "healthy": not gaps}


def create_role(tenant_id, role_name, scope_ref, duty_manager, *, created_by,
                criticality="important", minimum_backups=1, readiness_max_age_s=2592000,
                takeover_policy=None, org_id=None, role_id=None):
    ensure()
    fact = normalize_role(role_name, scope_ref, duty_manager, criticality=criticality,
                          minimum_backups=minimum_backups,
                          readiness_max_age_s=readiness_max_age_s,
                          takeover_policy=takeover_policy)
    rid = str(role_id or f"cr-{uuid.uuid4().hex}")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO continuity_roles
            (role_id,tenant_id,org_id,role_name,scope_ref,criticality,minimum_backups,
             duty_manager,readiness_max_age_s,takeover_policy,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (rid, str(tenant_id), org_id, fact["role_name"], fact["scope_ref"],
             fact["criticality"], fact["minimum_backups"], fact["duty_manager"],
             fact["readiness_max_age_s"], json.dumps(fact["takeover_policy"]), created_by))
    return {"role_id": rid, "fence_epoch": 0, **fact}


def appoint(tenant_id, role_id, appointee, kind, *, appointed_by, decision_evidence,
            priority=1, valid_from=None, valid_until=None, appointment_id=None):
    ensure()
    if kind == "acting":
        raise ValueError("acting authority must be created by take_over so its fence is rotated")
    fact = validate_appointment(kind, appointee, appointed_by, decision_evidence,
                                priority=priority)
    aid = str(appointment_id or f"ca-{uuid.uuid4().hex}")
    tid = str(tenant_id)
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO continuity_appointments
            (appointment_id,tenant_id,role_id,appointee,appointment_kind,priority,
             valid_from,valid_until,appointed_by,decision_evidence)
            VALUES (%s,%s,%s,%s,%s,%s,COALESCE(%s,now()),%s,%s,%s)""",
            (aid, tid, role_id, fact["appointee"], kind, fact["priority"], valid_from,
             valid_until, fact["appointed_by"], json.dumps(fact["decision_evidence"])))
        if kind == "primary":
            cur.execute("""SELECT current_holder,fence_epoch FROM continuity_roles
                           WHERE tenant_id=%s AND role_id=%s FOR UPDATE""", (tid, role_id))
            row = cur.fetchone()
            if not row:
                raise KeyError(role_id)
            epoch = int(row[1]) + 1
            cur.execute("""UPDATE continuity_roles SET current_holder=%s,fence_epoch=%s,updated_at=now()
                           WHERE tenant_id=%s AND role_id=%s""", (appointee, epoch, tid, role_id))
            cur.execute("""INSERT INTO continuity_fence_events
                (tenant_id,role_id,previous_holder,resulting_holder,previous_epoch,resulting_epoch,
                 event_kind,actor,decision_evidence) VALUES (%s,%s,%s,%s,%s,%s,'appointment',%s,%s)""",
                (tid, role_id, row[0], appointee, row[1], epoch, appointed_by,
                 json.dumps(fact["decision_evidence"])))
    return {"appointment_id": aid, "status": "active", **fact}


def record_availability(tenant_id, subject, state, reason, evidence, *, reported_by,
                        effective_at=None, expected_until=None):
    ensure()
    if state not in {"available", "degraded", "unavailable", "unknown"}:
        raise ValueError("unknown availability state")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO continuity_availability_events
            (tenant_id,subject,state,reason,effective_at,expected_until,evidence,reported_by)
            VALUES (%s,%s,%s,%s,COALESCE(%s,now()),%s,%s,%s) RETURNING availability_event_id""",
            (str(tenant_id), _required(subject, "subject"), state, _required(reason, "reason"),
             effective_at, expected_until, json.dumps(_evidence(evidence)), reported_by))
        event_id = cur.fetchone()[0]
    return {"availability_event_id": event_id, "state": state}


def record_transfer(tenant_id, role_id, from_subject, to_subject, artifact_kind,
                    artifact_ref, evidence, *, transfer_id=None):
    ensure()
    if artifact_kind not in {"runbook", "walkthrough", "access_validation", "simulation",
                             "decision_log", "other"}:
        raise ValueError("unknown knowledge-transfer artifact kind")
    if str(from_subject) == str(to_subject):
        raise ValueError("knowledge transfer requires distinct participants")
    xid = str(transfer_id or f"kt-{uuid.uuid4().hex}")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO continuity_knowledge_transfers
            (transfer_id,tenant_id,role_id,from_subject,to_subject,artifact_kind,artifact_ref,evidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (xid, str(tenant_id), role_id, _required(from_subject, "from_subject"),
             _required(to_subject, "to_subject"), artifact_kind,
             _required(artifact_ref, "artifact_ref"), json.dumps(_evidence(evidence))))
    return {"transfer_id": xid, "readiness_state": "submitted"}


def verify_transfer(tenant_id, transfer_id, outcome, evidence, *, verified_by, expires_at=None):
    ensure()
    if outcome not in {"verified", "failed"}:
        raise ValueError("transfer outcome must be verified or failed")
    proof = _evidence(evidence, "verification evidence")
    tid = str(tenant_id)
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT to_subject FROM continuity_knowledge_transfers
                       WHERE tenant_id=%s AND transfer_id=%s AND readiness_state='submitted'
                       FOR UPDATE""", (tid, transfer_id))
        row = cur.fetchone()
        if not row:
            raise ValueError("transfer is missing or no longer submitted")
        if outcome == "verified" and str(verified_by) == str(row[0]):
            raise PermissionError("successor cannot self-verify takeover readiness")
        cur.execute("""UPDATE continuity_knowledge_transfers SET readiness_state=%s,
            evidence=evidence || %s::jsonb,verified_by=CASE WHEN %s='verified' THEN %s END,
            verified_at=CASE WHEN %s='verified' THEN now() END,expires_at=%s
            WHERE tenant_id=%s AND transfer_id=%s AND readiness_state='submitted'""",
            (outcome, json.dumps({"verification": proof}), outcome, verified_by, outcome,
             expires_at, tid, transfer_id))
    return {"transfer_id": transfer_id, "readiness_state": outcome}


def take_over(tenant_id, role_id, successor, trigger_kind, trigger_evidence, *,
              manager_decision, decision_evidence, authorized_by, takeover_id=None):
    """Atomically appoint an acting holder and rotate authority to a new fence epoch."""
    ensure()
    tid = str(tenant_id)
    xid = str(takeover_id or f"to-{uuid.uuid4().hex}")
    aid = f"ca-{uuid.uuid4().hex}"
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT current_holder,fence_epoch,duty_manager,readiness_max_age_s
                       FROM continuity_roles
                       WHERE tenant_id=%s AND role_id=%s AND status='active' FOR UPDATE""",
                    (tid, role_id))
        row = cur.fetchone()
        if not row:
            raise KeyError(role_id)
        fact = validate_takeover(row[0], successor, trigger_kind, trigger_evidence,
                                 manager_decision, decision_evidence, authorized_by)
        cur.execute("""SELECT appointment_kind FROM continuity_appointments
                       WHERE tenant_id=%s AND role_id=%s AND appointee=%s AND status='active'
                         AND appointment_kind='backup' AND valid_from<=now()
                         AND (valid_until IS NULL OR valid_until>now())
                       ORDER BY priority LIMIT 1""", (tid, role_id, successor))
        candidate = cur.fetchone()
        cur.execute("""SELECT state FROM continuity_availability_events
                       WHERE tenant_id=%s AND subject=%s AND effective_at<=now()
                       ORDER BY effective_at DESC,availability_event_id DESC LIMIT 1""",
                    (tid, successor))
        available = cur.fetchone()
        cur.execute("""SELECT EXISTS (
            SELECT 1 FROM continuity_knowledge_transfers k
            WHERE k.tenant_id=%s AND k.role_id=%s AND k.to_subject=%s
              AND k.readiness_state='verified' AND k.verified_at >=
                  now()-(%s*interval '1 second')
              AND (k.expires_at IS NULL OR k.expires_at>now()))""",
            (tid, role_id, successor, row[3]))
        readiness = cur.fetchone()
        validate_takeover_candidate(candidate[0] if candidate else None,
                                    available[0] if available else "unknown",
                                    bool(readiness and readiness[0]), decision_evidence,
                                    authorized_by=authorized_by, duty_manager=row[2])
        epoch = int(row[1]) + 1
        cur.execute("""INSERT INTO continuity_appointments
            (appointment_id,tenant_id,role_id,appointee,appointment_kind,priority,
             appointed_by,decision_evidence) VALUES (%s,%s,%s,%s,'acting',1,%s,%s)""",
            (aid, tid, role_id, fact["successor"], authorized_by,
             json.dumps(fact["decision_evidence"])))
        cur.execute("""INSERT INTO continuity_takeovers
            (takeover_id,tenant_id,role_id,predecessor,successor,trigger_kind,trigger_evidence,
             manager_decision,decision_evidence,authorized_by,fence_epoch,acting_appointment_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (xid, tid, role_id, fact["predecessor"], fact["successor"], trigger_kind,
             json.dumps(fact["trigger_evidence"]), fact["manager_decision"],
             json.dumps(fact["decision_evidence"]), authorized_by, epoch, aid))
        cur.execute("""UPDATE continuity_roles SET current_holder=%s,fence_epoch=%s,updated_at=now()
                       WHERE tenant_id=%s AND role_id=%s""", (successor, epoch, tid, role_id))
        cur.execute("""INSERT INTO continuity_fence_events
            (tenant_id,role_id,takeover_id,previous_holder,resulting_holder,previous_epoch,
             resulting_epoch,event_kind,actor,decision_evidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'takeover',%s,%s)""",
            (tid, role_id, xid, row[0], successor, row[1], epoch, authorized_by,
             json.dumps(fact["decision_evidence"])))
    audit.append(actor=authorized_by, action="ContinuityTakeover", resource=role_id,
                 decision=manager_decision, payload={"takeover_id": xid, "successor": successor,
                 "fence_epoch": epoch}, tenant_id=tid)
    return {"takeover_id": xid, "acting_appointment_id": aid, "holder": successor,
            "fence_epoch": epoch, "state": "active"}


def release_takeover(tenant_id, takeover_id, return_holder, release_evidence, *, released_by):
    """End acting cover and rotate the fence again; old acting tokens become invalid."""
    ensure()
    tid = str(tenant_id)
    proof = _evidence(release_evidence, "release evidence")
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT t.role_id,t.successor,t.fence_epoch,t.acting_appointment_id,
                              r.current_holder,r.fence_epoch,r.duty_manager
                       FROM continuity_takeovers t JOIN continuity_roles r
                         ON r.tenant_id=t.tenant_id AND r.role_id=t.role_id
                       WHERE t.tenant_id=%s AND t.takeover_id=%s AND t.state='active'
                       FOR UPDATE OF t,r""", (tid, takeover_id))
        row = cur.fetchone()
        if not row:
            raise KeyError(takeover_id)
        if str(released_by) != str(row[6]) and not proof.get("delegation_ref"):
            raise PermissionError("release requires duty-manager authority or delegation evidence")
        assert_fence(row[4], row[5], row[1], row[2])
        holder = _required(return_holder, "return_holder")
        epoch = int(row[5]) + 1
        cur.execute("""UPDATE continuity_takeovers SET state='released',ended_at=now(),
            ended_by=%s,release_evidence=%s WHERE tenant_id=%s AND takeover_id=%s""",
            (released_by, json.dumps(proof), tid, takeover_id))
        cur.execute("""UPDATE continuity_appointments SET status='ended',ended_at=now(),ended_by=%s
                       WHERE tenant_id=%s AND appointment_id=%s""",
                    (released_by, tid, row[3]))
        cur.execute("""UPDATE continuity_roles SET current_holder=%s,fence_epoch=%s,updated_at=now()
                       WHERE tenant_id=%s AND role_id=%s""", (holder, epoch, tid, row[0]))
        cur.execute("""INSERT INTO continuity_fence_events
            (tenant_id,role_id,takeover_id,previous_holder,resulting_holder,previous_epoch,
             resulting_epoch,event_kind,actor,decision_evidence)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'release',%s,%s)""",
            (tid, row[0], takeover_id, row[4], holder, row[5], epoch, released_by,
             json.dumps(proof)))
    return {"takeover_id": takeover_id, "state": "released", "holder": holder,
            "fence_epoch": epoch}


def continuity_gaps(tenant_id, duty_manager):
    """Return an explained coverage matrix scoped to one responsible duty manager."""
    ensure()
    tid = str(tenant_id)
    with tenant_connection(tid) as c, c.cursor() as cur:
        cur.execute("""SELECT role_id,role_name,scope_ref,criticality,minimum_backups,
                              duty_manager,readiness_max_age_s,current_holder,fence_epoch
                       FROM continuity_roles WHERE tenant_id=%s AND duty_manager=%s
                         AND status='active' ORDER BY criticality DESC,role_name""",
                    (tid, duty_manager))
        columns = ("role_id", "role_name", "scope_ref", "criticality", "minimum_backups",
                   "duty_manager", "readiness_max_age_s", "current_holder", "fence_epoch")
        roles = [dict(zip(columns, row)) for row in cur.fetchall()]
        result = []
        for role in roles:
            cur.execute("""SELECT appointee,appointment_kind,priority,status,valid_until
                           FROM continuity_appointments WHERE tenant_id=%s AND role_id=%s""",
                        (tid, role["role_id"]))
            apps = [dict(zip(("appointee", "appointment_kind", "priority", "status", "valid_until"), row))
                    for row in cur.fetchall()]
            subjects = [a["appointee"] for a in apps]
            cur.execute("""SELECT DISTINCT ON (subject) availability_event_id,subject,state,effective_at
                           FROM continuity_availability_events WHERE tenant_id=%s AND subject=ANY(%s)
                           ORDER BY subject,effective_at DESC,availability_event_id DESC""",
                        (tid, subjects))
            avail = [dict(zip(("availability_event_id", "subject", "state", "effective_at"), row))
                     for row in cur.fetchall()]
            cur.execute("""SELECT to_subject,readiness_state,verified_at,expires_at
                           FROM continuity_knowledge_transfers WHERE tenant_id=%s AND role_id=%s""",
                        (tid, role["role_id"]))
            ready = [dict(zip(("to_subject", "readiness_state", "verified_at", "expires_at"), row))
                     for row in cur.fetchall()]
            result.append(evaluate_coverage(role, apps, avail, ready))
    return {"duty_manager": duty_manager, "roles": result,
            "gaps": [dict(gap, role_id=row["role_id"]) for row in result for gap in row["gaps"]]}
