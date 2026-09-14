#!/usr/bin/env python3
"""Evidence-backed incident command for a human-like agent company.

The module is deliberately a coordination ledger, not a rules engine pretending
to be a manager. It makes ownership, cadence, decisions, verification and learning
durable; agents decide what the evidence means. Lifecycle guards prevent optimistic
"resolved" or "closed" labels from outrunning the facts.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import audit  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

SEVERITIES = {"SEV1", "SEV2", "SEV3", "SEV4"}
STATUSES = ("declared", "triage", "contained", "recovering", "resolved", "closed")
ACTION_STATUSES = ("open", "in_progress", "implemented", "verified", "closed", "cancelled")
_ensured = False
_ensure_lock = threading.Lock()


def ensure():
    """Install the additive schema on upgraded deployments as well as fresh ones."""
    global _ensured
    if _ensured:
        return True
    with _ensure_lock:
        if not _ensured:
            migration = SCRIPTS.parent / "postgres" / "initdb" / "61-incident-command.sql"
            with connection() as c, c.cursor() as cur:
                cur.execute(migration.read_text())
            _ensured = True
    return True


def normalize_incident(title, severity, commander, deputy, impact,
                       communication_cadence_s=900):
    """Validate command facts without deciding whether an incident should exist."""
    title, commander, deputy = (str(x or "").strip() for x in (title, commander, deputy))
    severity = str(severity or "").upper()
    if not title:
        raise ValueError("title is required")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(SEVERITIES)}")
    if not commander or not deputy:
        raise ValueError("commander and deputy are required")
    if commander == deputy:
        raise ValueError("commander and deputy must be different people")
    impact = dict(impact or {})
    if not impact:
        raise ValueError("impact must state the observed or suspected effect")
    cadence = int(communication_cadence_s)
    if cadence < 60:
        raise ValueError("communication cadence cannot be below 60 seconds")
    return {"title": title, "severity": severity, "commander": commander,
            "deputy": deputy, "impact": impact, "communication_cadence_s": cadence}


def lifecycle_transition(current, target, *, containment_verified=False,
                         recovery_verified=False, postmortem_published=False,
                         open_corrective_actions=0):
    """Pure lifecycle guard. Failed verification can explicitly reopen earlier work."""
    current, target = str(current), str(target)
    if current not in STATUSES or target not in STATUSES:
        raise ValueError("unknown incident lifecycle status")
    allowed = {
        "declared": {"triage"}, "triage": {"contained"},
        "contained": {"triage", "recovering"},
        "recovering": {"contained", "resolved"},
        "resolved": {"recovering", "closed"}, "closed": {"recovering"},
    }
    if target not in allowed[current]:
        raise ValueError(f"invalid incident transition: {current} -> {target}")
    if target == "contained" and not containment_verified:
        raise ValueError("containment requires a passed verification")
    if target == "resolved" and not recovery_verified:
        raise ValueError("resolution requires a passed recovery verification")
    if target == "closed":
        if not postmortem_published:
            raise ValueError("closure requires a published postmortem")
        if int(open_corrective_actions):
            raise ValueError("closure requires all corrective actions closed or cancelled")
    return target


def action_transition(current, target, *, owner, due_at, implementation_evidence=None,
                      verification_evidence=None, verified_by=None):
    """Pure corrective-action guard; implementation is distinct from verification."""
    if current not in ACTION_STATUSES or target not in ACTION_STATUSES:
        raise ValueError("unknown corrective action status")
    if not str(owner or "").strip() or due_at is None:
        raise ValueError("corrective action requires an owner and deadline")
    allowed = {"open": {"in_progress", "cancelled"},
               "in_progress": {"implemented", "cancelled"},
               "implemented": {"in_progress", "verified"},
               "verified": {"implemented", "closed"},
               "closed": set(), "cancelled": set()}
    if target not in allowed[current]:
        raise ValueError(f"invalid corrective action transition: {current} -> {target}")
    if target == "implemented" and not dict(implementation_evidence or {}):
        raise ValueError("implementation requires evidence")
    if target == "verified":
        if not str(verified_by or "").strip() or not dict(verification_evidence or {}):
            raise ValueError("verification requires a verifier and evidence")
        if str(verified_by) == str(owner):
            raise ValueError("corrective action owner cannot independently verify their own work")
    return target


def responder_transition(current, target, *, actor, responder):
    """Require the assigned responder to acknowledge; release stays command-controlled."""
    allowed = {"assigned": {"acknowledged"}, "acknowledged": {"active", "released"},
               "active": {"released"}, "released": set()}
    if current not in allowed or target not in allowed[current]:
        raise ValueError(f"invalid responder transition: {current} -> {target}")
    if target == "acknowledged" and str(actor) != str(responder):
        raise PermissionError("only the assigned responder may acknowledge")
    return target


def declare(tenant_id, title, severity, commander, deputy, impact, *, created_by,
            communication_cadence_s=900, source_ref=None, incident_id=None):
    ensure()
    fact = normalize_incident(title, severity, commander, deputy, impact,
                              communication_cadence_s)
    iid = str(incident_id or f"inc-{uuid.uuid4().hex}")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO company_incidents
            (incident_id,tenant_id,title,severity,commander,deputy,impact,
             communication_cadence_s,next_communication_at,source_ref,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s)""",
            (iid, str(tenant_id), fact["title"], fact["severity"], fact["commander"],
             fact["deputy"], json.dumps(fact["impact"]), fact["communication_cadence_s"],
             source_ref, created_by))
        cur.execute("""INSERT INTO incident_timeline
            (tenant_id,incident_id,actor,event_type,summary,evidence)
            VALUES (%s,%s,%s,'declared',%s,%s)""",
            (str(tenant_id), iid, created_by, fact["title"], json.dumps({"impact": fact["impact"],
             "severity": fact["severity"], "commander": fact["commander"],
             "deputy": fact["deputy"]})))
    audit.append(actor=created_by, action="IncidentDeclared", resource=iid,
                 decision=fact["severity"], payload={"commander": commander, "deputy": deputy},
                 tenant_id=str(tenant_id))
    return {"incident_id": iid, "status": "declared", **fact}


def append_event(tenant_id, incident_id, actor, event_type, summary, *, evidence=None,
                 audience=None, occurred_at=None):
    """Append a fact/decision/update; communication updates reset the promised cadence."""
    ensure()
    if not all(str(x or "").strip() for x in (actor, event_type, summary)):
        raise ValueError("actor, event_type and summary are required")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO incident_timeline
            (tenant_id,incident_id,actor,event_type,summary,evidence,audience,occurred_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,now())) RETURNING event_id""",
            (str(tenant_id), incident_id, actor, event_type, summary,
             json.dumps(evidence or {}), audience, occurred_at))
        event_id = cur.fetchone()[0]
        if event_type == "communication":
            cur.execute("""UPDATE company_incidents SET
                next_communication_at=now()+(communication_cadence_s*interval '1 second'),
                updated_at=now() WHERE tenant_id=%s AND incident_id=%s""",
                (str(tenant_id), incident_id))
    return {"event_id": event_id}


def assign_responder(tenant_id, incident_id, responder, role, objective, *, assigned_by):
    ensure()
    if not all(str(x or "").strip() for x in (responder, role, objective)):
        raise ValueError("responder, role and objective are required")
    aid = f"ira-{uuid.uuid4().hex}"
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO incident_responders
            (assignment_id,tenant_id,incident_id,responder,role,objective,assigned_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (aid, str(tenant_id), incident_id, responder, role, objective, assigned_by))
    return {"assignment_id": aid, "status": "assigned", "responder": responder}


def transition_responder(tenant_id, assignment_id, target, actor):
    ensure()
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT incident_id,responder,status FROM incident_responders
                       WHERE tenant_id=%s AND assignment_id=%s FOR UPDATE""",
                    (str(tenant_id), assignment_id))
        row = cur.fetchone()
        if not row:
            raise KeyError(assignment_id)
        nxt = responder_transition(row[2], target, actor=actor, responder=row[1])
        cur.execute("""UPDATE incident_responders SET status=%s,
            acknowledged_at=CASE WHEN %s='acknowledged' THEN now() ELSE acknowledged_at END,
            released_at=CASE WHEN %s='released' THEN now() ELSE released_at END
            WHERE tenant_id=%s AND assignment_id=%s""",
            (nxt, nxt, nxt, str(tenant_id), assignment_id))
        cur.execute("""INSERT INTO incident_timeline
            (tenant_id,incident_id,actor,event_type,summary,evidence)
            VALUES (%s,%s,%s,'responder_status',%s,%s)""",
            (str(tenant_id), row[0], actor, f"{row[1]}: {row[2]} -> {nxt}",
             json.dumps({"assignment_id": assignment_id})))
    return {"assignment_id": assignment_id, "previous_status": row[2], "status": nxt}


def verify(tenant_id, incident_id, phase, assertion, method, evidence, outcome, *, verified_by):
    ensure()
    if phase not in {"containment", "recovery", "recurrence"}:
        raise ValueError("unknown verification phase")
    if outcome not in {"passed", "failed", "inconclusive"}:
        raise ValueError("unknown verification outcome")
    if not dict(evidence or {}):
        raise ValueError("verification requires evidence")
    vid = f"iv-{uuid.uuid4().hex}"
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO incident_verifications
            (verification_id,tenant_id,incident_id,phase,assertion,method,evidence,outcome,verified_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (vid, str(tenant_id), incident_id, phase, assertion, method,
             json.dumps(evidence), outcome, verified_by))
        cur.execute("""INSERT INTO incident_timeline
            (tenant_id,incident_id,actor,event_type,summary,evidence)
            VALUES (%s,%s,%s,'verification',%s,%s)""",
            (str(tenant_id), incident_id, verified_by, f"{phase} verification {outcome}",
             json.dumps({"verification_id": vid, "assertion": assertion})))
    return {"verification_id": vid, "outcome": outcome}


def create_corrective_action(tenant_id, incident_id, title, owner, due_at, *, created_by,
                             priority="medium", recurrence_key=None):
    """Create owned, dated prevention work linked to the originating incident."""
    ensure()
    if not all(str(x or "").strip() for x in (title, owner, due_at)):
        raise ValueError("corrective action requires title, owner and deadline")
    if priority not in {"low", "medium", "high", "critical"}:
        raise ValueError("unknown corrective action priority")
    aid = f"ica-{uuid.uuid4().hex}"
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO incident_corrective_actions
            (action_id,tenant_id,incident_id,title,owner,priority,due_at,recurrence_key,created_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (aid, str(tenant_id), incident_id, title, owner, priority, due_at,
             recurrence_key, created_by))
    return {"action_id": aid, "status": "open", "owner": owner, "due_at": str(due_at)}


def transition_corrective_action(tenant_id, action_id, target, actor, *,
                                 implementation_evidence=None, verification_evidence=None):
    """Advance prevention work; independent evidence is required before closure."""
    ensure()
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT incident_id,status,owner,due_at FROM incident_corrective_actions
                       WHERE tenant_id=%s AND action_id=%s FOR UPDATE""",
                    (str(tenant_id), action_id))
        row = cur.fetchone()
        if not row:
            raise KeyError(action_id)
        nxt = action_transition(row[1], target, owner=row[2], due_at=row[3],
                                implementation_evidence=implementation_evidence,
                                verification_evidence=verification_evidence,
                                verified_by=actor if target == "verified" else None)
        cur.execute("""UPDATE incident_corrective_actions SET status=%s,
            implementation_evidence=CASE WHEN %s::jsonb='{}'::jsonb THEN implementation_evidence
                                         ELSE %s::jsonb END,
            verification_evidence=CASE WHEN %s::jsonb='{}'::jsonb THEN verification_evidence
                                       ELSE %s::jsonb END,
            verified_by=CASE WHEN %s='verified' THEN %s ELSE verified_by END,
            closed_at=CASE WHEN %s='closed' THEN now() ELSE closed_at END,updated_at=now()
            WHERE tenant_id=%s AND action_id=%s""",
            (nxt, json.dumps(implementation_evidence or {}), json.dumps(implementation_evidence or {}),
             json.dumps(verification_evidence or {}), json.dumps(verification_evidence or {}),
             nxt, actor, nxt, str(tenant_id), action_id))
        cur.execute("""INSERT INTO incident_timeline
            (tenant_id,incident_id,actor,event_type,summary,evidence)
            VALUES (%s,%s,%s,'corrective_action',%s,%s)""",
            (str(tenant_id), row[0], actor, f"{action_id}: {row[1]} -> {nxt}",
             json.dumps({"action_id": action_id})))
    return {"action_id": action_id, "previous_status": row[1], "status": nxt}


def record_postmortem(tenant_id, incident_id, owner, root_causes, *,
                      contributing_factors=None, lessons=None, recurrence_assessment=None,
                      status="draft", postmortem_id=None):
    """Create/update the one durable postmortem and link it to the incident."""
    ensure()
    roots = list(root_causes or [])
    recurrence = dict(recurrence_assessment or {})
    if not str(owner or "").strip() or not roots:
        raise ValueError("postmortem requires an owner and at least one root cause")
    if status not in {"draft", "review", "published"}:
        raise ValueError("unknown postmortem status")
    if status == "published":
        required = {"method", "observation_window", "evidence", "result"}
        missing = sorted(k for k in required if not recurrence.get(k))
        if missing:
            raise ValueError("published postmortem recurrence assessment missing: "
                             + ", ".join(missing))
    pid = str(postmortem_id or f"pm-{uuid.uuid4().hex}")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO incident_postmortems
            (postmortem_id,tenant_id,incident_id,status,owner,root_causes,
             contributing_factors,lessons,recurrence_assessment,published_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s='published' THEN now() END)
            ON CONFLICT (incident_id) DO UPDATE SET status=EXCLUDED.status,owner=EXCLUDED.owner,
              root_causes=EXCLUDED.root_causes,contributing_factors=EXCLUDED.contributing_factors,
              lessons=EXCLUDED.lessons,recurrence_assessment=EXCLUDED.recurrence_assessment,
              published_at=CASE WHEN EXCLUDED.status='published' THEN now()
                                ELSE incident_postmortems.published_at END,updated_at=now()
            RETURNING postmortem_id""",
            (pid, str(tenant_id), incident_id, status, owner, json.dumps(roots),
             json.dumps(list(contributing_factors or [])), json.dumps(list(lessons or [])),
             json.dumps(recurrence), status))
        pid = cur.fetchone()[0]
        cur.execute("""UPDATE company_incidents SET postmortem_id=%s,updated_at=now()
                       WHERE tenant_id=%s AND incident_id=%s""",
                    (pid, str(tenant_id), incident_id))
    return {"postmortem_id": pid, "incident_id": incident_id, "status": status}


def transition(tenant_id, incident_id, target, actor, rationale):
    """Lock, derive evidence gates, and atomically move the lifecycle."""
    ensure()
    rationale = str(rationale or "").strip()
    if not rationale:
        raise ValueError("incident transition requires a rationale")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT status FROM company_incidents
                       WHERE tenant_id=%s AND incident_id=%s FOR UPDATE""",
                    (str(tenant_id), incident_id))
        row = cur.fetchone()
        if not row:
            raise KeyError(incident_id)
        cur.execute("""SELECT phase,outcome FROM incident_verifications
                       WHERE incident_id=%s ORDER BY verified_at DESC""", (incident_id,))
        latest = {}
        for phase, outcome in cur.fetchall():
            latest.setdefault(phase, outcome)
        cur.execute("""SELECT count(*) FROM incident_corrective_actions
                       WHERE incident_id=%s AND status NOT IN ('closed','cancelled')""", (incident_id,))
        open_actions = cur.fetchone()[0]
        cur.execute("""SELECT p.status FROM incident_postmortems p
                       WHERE p.incident_id=%s""", (incident_id,))
        pm = cur.fetchone()
        nxt = lifecycle_transition(row[0], target,
            containment_verified=latest.get("containment") == "passed",
            recovery_verified=latest.get("recovery") == "passed",
            postmortem_published=bool(pm and pm[0] == "published"),
            open_corrective_actions=open_actions)
        stamp = {"contained": "contained_at", "closed": "closed_at"}.get(nxt)
        set_stamp = f",{stamp}=now()" if stamp else ""
        if nxt == "resolved":
            set_stamp = ",recovered_at=now(),resolved_at=now()"
        cur.execute(f"""UPDATE company_incidents SET status=%s,updated_at=now(){set_stamp}
                         WHERE tenant_id=%s AND incident_id=%s""",
                    (nxt, str(tenant_id), incident_id))
        cur.execute("""INSERT INTO incident_timeline
            (tenant_id,incident_id,actor,event_type,summary,evidence)
            VALUES (%s,%s,%s,'status_change',%s,%s)""",
            (str(tenant_id), incident_id, actor, f"{row[0]} -> {nxt}",
             json.dumps({"rationale": rationale})))
    audit.append(actor=actor, action="IncidentTransition", resource=incident_id,
                 decision=nxt, payload={"from": row[0], "rationale": rationale[:300]},
                 tenant_id=str(tenant_id))
    return {"incident_id": incident_id, "previous_status": row[0], "status": nxt}


def attention_evidence(limit=100):
    """System-wide facts for the duty manager; elapsed time is not a verdict."""
    ensure()
    with connection() as c, c.cursor() as cur:
        cur.execute("""SELECT incident_id,tenant_id,title,severity,status,commander,deputy,
                              next_communication_at,now()-next_communication_at AS overdue_by
                       FROM company_incidents WHERE status NOT IN ('resolved','closed')
                         AND next_communication_at<=now()
                       ORDER BY severity,next_communication_at LIMIT %s""", (int(limit),))
        comms = cur.fetchall()
        cur.execute("""SELECT action_id,tenant_id,incident_id,title,owner,priority,status,due_at,
                              now()-due_at AS overdue_by
                       FROM incident_corrective_actions
                       WHERE status NOT IN ('closed','cancelled') AND due_at<=now()
                       ORDER BY priority,due_at LIMIT %s""", (int(limit),))
        actions = cur.fetchall()
        cur.execute("""SELECT i.incident_id,i.tenant_id,i.title,i.status
                       FROM company_incidents i LEFT JOIN incident_responders r
                         ON r.incident_id=i.incident_id AND r.status IN ('acknowledged','active')
                       WHERE i.status NOT IN ('resolved','closed')
                       GROUP BY i.incident_id HAVING count(r.assignment_id)=0 LIMIT %s""", (int(limit),))
        unstaffed = cur.fetchall()
    out = [{"kind": "incident_communication_due", "incident_id": r[0], "tenant_id": r[1],
            "title": r[2], "severity": r[3], "status": r[4], "commander": r[5],
            "deputy": r[6], "due_at": str(r[7]), "overdue_by": str(r[8])} for r in comms]
    out += [{"kind": "corrective_action_due", "action_id": r[0], "tenant_id": r[1],
             "incident_id": r[2], "title": r[3], "owner": r[4], "priority": r[5],
             "status": r[6], "due_at": str(r[7]), "overdue_by": str(r[8])} for r in actions]
    out += [{"kind": "incident_response_unacknowledged", "incident_id": r[0],
             "tenant_id": r[1], "title": r[2], "status": r[3]} for r in unstaffed]
    return out
