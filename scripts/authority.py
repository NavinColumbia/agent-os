#!/usr/bin/env python3
"""Durable delegation and escalation policy for the AI company.

This module answers one narrow but important question: *does the current actor
already have authority to keep working, should a manager review it, or is there
a real CEO boundary?*  It deliberately does not choose the next operational
action.  That remains an agentic manager decision.  The policy only prevents a
reversible internal failure, timeout, or uncertainty from being mislabeled as
``user_feedback``.

The durable rows are also the correlation spine between a blocked work item,
its management reviews, the eventual CEO request (when genuinely necessary),
and the answer that resumes work.
"""
from __future__ import annotations

import json
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import audit  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402


KINDS = frozenset({"internal_recovery", "credential", "legal", "spend", "irreversible", "business"})
HUMAN_BOUNDARIES = frozenset({"credential", "legal", "irreversible"})
INTERNAL_ACTIONS = frozenset({"approve", "continue", "retry", "reassign", "delegate",
                              "open_incident", "escalate_manager"})
DEFAULT_ENVELOPE = {
    "delegated_kinds": ["internal_recovery", "business"],
    "manager_review_below_confidence": 0.72,
    "business": {"max_risk": "medium", "reversible_only": True},
    "spend": {"per_action_usd": 25.0, "campaign_usd": 100.0},
}
_RISK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_ensured = False
_ensure_lock = threading.Lock()


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with connection() as c, c.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS authority_envelopes (
                id BIGSERIAL PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'company',
                revision INT NOT NULL DEFAULT 1,
                policy JSONB NOT NULL,
                active BOOLEAN NOT NULL DEFAULT true,
                created_by TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (tenant_id, scope, revision))""")
            cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS authority_envelopes_active_idx
                ON authority_envelopes (tenant_id, scope) WHERE active""")
            cur.execute("""CREATE TABLE IF NOT EXISTS authority_decisions (
                id BIGSERIAL PRIMARY KEY,
                correlation_id TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                org_id TEXT,
                thread_id BIGINT,
                work_ref TEXT NOT NULL,
                kind TEXT NOT NULL,
                proposal JSONB NOT NULL DEFAULT '{}',
                disposition TEXT NOT NULL,
                status TEXT NOT NULL,
                owner_role TEXT,
                envelope_id BIGINT,
                envelope_revision INT,
                agent_request_id BIGINT,
                rationale TEXT,
                review_due_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                resolved_at TIMESTAMPTZ)""")
            cur.execute("""CREATE INDEX IF NOT EXISTS authority_decisions_review_idx
                ON authority_decisions (review_due_at, id)
                WHERE status IN ('manager_review','human_required')""")
            cur.execute("""CREATE INDEX IF NOT EXISTS authority_decisions_tenant_idx
                ON authority_decisions (tenant_id, status, id DESC)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS authority_reviews (
                id BIGSERIAL PRIMARY KEY,
                decision_id BIGINT NOT NULL,
                tenant_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                trigger TEXT NOT NULL,
                action TEXT NOT NULL,
                rationale TEXT,
                state JSONB NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("""CREATE INDEX IF NOT EXISTS authority_reviews_decision_idx
                ON authority_reviews (decision_id, id)""")
            cur.execute("""CREATE INDEX IF NOT EXISTS authority_reviews_tenant_idx
                ON authority_reviews (tenant_id, id DESC)""")
        _ensured = True


def ensure():
    """Public idempotent startup hook for daemons and migrations."""
    _ensure()
    return True


def normalize_envelope(policy=None):
    """Return a validated policy merged onto safe company defaults."""
    out = deepcopy(DEFAULT_ENVELOPE)
    policy = policy or {}
    if not isinstance(policy, dict):
        raise ValueError("authority policy must be an object")
    for key in ("delegated_kinds", "manager_review_below_confidence"):
        if key in policy:
            out[key] = deepcopy(policy[key])
    for section in ("business", "spend"):
        if section in policy:
            if not isinstance(policy[section], dict):
                raise ValueError(f"authority policy {section} must be an object")
            out[section].update(deepcopy(policy[section]))
    delegated = set(out.get("delegated_kinds") or [])
    unknown = delegated - KINDS
    if unknown:
        raise ValueError(f"unknown delegated authority kinds: {sorted(unknown)}")
    # Possession of a missing credential, legal consent, or permission for an
    # irreversible act cannot be invented by a standing delegation document.
    forbidden = delegated & HUMAN_BOUNDARIES
    if forbidden:
        raise ValueError(f"intrinsic human boundaries cannot be delegated: {sorted(forbidden)}")
    out["delegated_kinds"] = sorted(delegated)
    out["manager_review_below_confidence"] = min(1.0, max(0.0, float(
        out.get("manager_review_below_confidence", 0.72))))
    out["spend"]["per_action_usd"] = max(0.0, float(out["spend"].get("per_action_usd", 0)))
    out["spend"]["campaign_usd"] = max(0.0, float(out["spend"].get("campaign_usd", 0)))
    risk = str(out["business"].get("max_risk") or "medium").lower()
    if risk not in _RISK:
        raise ValueError(f"unknown business risk: {risk}")
    out["business"]["max_risk"] = risk
    out["business"]["reversible_only"] = bool(out["business"].get("reversible_only", True))
    return out


def evaluate(kind, proposal=None, envelope=None):
    """Classify a proposal without doing work or creating a gate.

    ``delegated`` means an agent may decide and act. ``manager_review`` means
    uncertainty stays inside the org. ``human_required`` is reserved for a
    concrete authority boundary and includes the exact missing authority.
    """
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"unknown authority kind: {kind!r}")
    p = dict(proposal or {})
    env = normalize_envelope(envelope)
    confidence = min(1.0, max(0.0, float(p.get("confidence", 1.0))))

    if kind == "internal_recovery":
        return {"disposition": "delegated" if confidence >= env["manager_review_below_confidence"]
                else "manager_review", "authority_gap": "none",
                "reason": "reversible internal recovery remains inside the management chain"}

    if kind in HUMAN_BOUNDARIES:
        return {"disposition": "human_required", "authority_gap": kind,
                "reason": str(p.get("reason") or f"{kind} authority is not currently held")}

    if confidence < env["manager_review_below_confidence"]:
        return {"disposition": "manager_review", "authority_gap": "none",
                "reason": "actor confidence is below its management-review threshold"}

    if kind == "spend":
        amount = max(0.0, float(p.get("amount_usd") or 0.0))
        spent = max(0.0, float(p.get("campaign_spent_usd") or 0.0))
        spend = env["spend"]
        if amount <= spend["per_action_usd"] and spent + amount <= spend["campaign_usd"]:
            return {"disposition": "delegated", "authority_gap": "none",
                    "reason": "spend is inside the standing per-action and campaign authority"}
        return {"disposition": "human_required", "authority_gap": "spend",
                "reason": (f"requested ${amount:.2f} with ${spent:.2f} already spent exceeds "
                           "the standing spend envelope")}

    # Business judgment is delegated when it is reversible and inside the
    # explicitly accepted impact band; otherwise a manager first decides how
    # to reduce/reframe it. Irreversibility has its own human boundary kind.
    risk = str(p.get("risk") or "medium").lower()
    if risk not in _RISK:
        return {"disposition": "manager_review", "authority_gap": "none",
                "reason": f"unrecognized business risk {risk!r} needs managerial framing"}
    reversible = bool(p.get("reversible", True))
    if ("business" in env["delegated_kinds"]
            and _RISK[risk] <= _RISK[env["business"]["max_risk"]]
            and (reversible or not env["business"]["reversible_only"])):
        return {"disposition": "delegated", "authority_gap": "none",
                "reason": "business decision is inside the standing reversible-risk envelope"}
    if bool(p.get("management_exhausted")) and bool(p.get("requires_ceo_business_judgment")):
        return {"disposition": "human_required", "authority_gap": "business",
                "reason": str(p.get("reason") or
                              "the management chain identified a business judgment reserved for the CEO")}
    return {"disposition": "manager_review", "authority_gap": "none",
            "reason": "business proposal is outside the actor's delegation; manager must reframe or escalate"}


def set_envelope(tenant_id, policy, *, scope="company", actor="ceo"):
    """Install a new immutable revision and atomically make it active."""
    _ensure()
    normalized = normalize_envelope(policy)
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"authority:{tenant_id}:{scope}",))
        cur.execute("SELECT COALESCE(max(revision),0)+1 FROM authority_envelopes WHERE tenant_id=%s AND scope=%s",
                    (tenant_id, scope))
        revision = cur.fetchone()[0]
        cur.execute("UPDATE authority_envelopes SET active=false WHERE tenant_id=%s AND scope=%s AND active",
                    (tenant_id, scope))
        cur.execute("""INSERT INTO authority_envelopes
                       (tenant_id,scope,revision,policy,active,created_by)
                       VALUES (%s,%s,%s,%s,true,%s) RETURNING id""",
                    (tenant_id, scope, revision, json.dumps(normalized), actor))
        eid = cur.fetchone()[0]
    audit.append(actor=actor, action="AuthorityEnvelopeSet", resource=f"{tenant_id}:{scope}",
                 decision="active", payload={"revision": revision, "envelope_id": eid}, tenant_id=tenant_id)
    return {"id": eid, "revision": revision, "scope": scope, "policy": normalized}


def active_envelope(tenant_id, scope="company"):
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT id,revision,policy FROM authority_envelopes
                       WHERE tenant_id=%s AND scope=%s AND active ORDER BY revision DESC LIMIT 1""",
                    (tenant_id, scope))
        row = cur.fetchone()
    if not row:
        return {"id": None, "revision": 0, "scope": scope, "policy": normalize_envelope()}
    return {"id": row[0], "revision": row[1], "scope": scope,
            "policy": normalize_envelope(row[2])}


def authorize(tenant_id, kind, proposal=None, *, scope="company"):
    """Evaluate against the tenant's active standing authority without opening a decision row."""
    env = active_envelope(tenant_id, scope)
    return {**evaluate(kind, proposal, env["policy"]),
            "envelope_id": env["id"], "envelope_revision": env["revision"], "scope": scope}


def open_decision(tenant_id, work_ref, kind, proposal=None, *, correlation_id=None,
                  org_id=None, thread_id=None, owner_role="manager", scope="company",
                  review_after_s=300, request_human=None):
    """Durably route one decision, idempotently by ``correlation_id``.

    Human notification is created only after classification proves a real
    boundary. Manager review rows are runnable internal work, never a CEO gate.
    ``request_human`` is injectable for tests; production defaults to
    :func:`agent_request.ask`.
    """
    _ensure()
    corr = str(correlation_id or uuid.uuid4())
    env = active_envelope(tenant_id, scope)
    verdict = evaluate(kind, proposal, env["policy"])
    disposition = verdict["disposition"]
    status = {"delegated": "delegated", "manager_review": "manager_review",
              "human_required": "human_required"}[disposition]
    due = None if status == "delegated" else max(30, int(review_after_s or 300))
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO authority_decisions
            (correlation_id,tenant_id,org_id,thread_id,work_ref,kind,proposal,disposition,status,
             owner_role,envelope_id,envelope_revision,rationale,review_due_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    CASE WHEN %s IS NULL THEN NULL ELSE now()+(%s * interval '1 second') END)
            ON CONFLICT (correlation_id) DO NOTHING RETURNING id""",
            (corr, tenant_id, org_id, thread_id, str(work_ref), kind, json.dumps(proposal or {}),
             disposition, status, owner_role, env["id"], env["revision"], verdict["reason"], due, due))
        row = cur.fetchone()
        duplicate = not bool(row)
        if duplicate:
            cur.execute("""SELECT id,disposition,status,agent_request_id,rationale
                           FROM authority_decisions WHERE correlation_id=%s AND tenant_id=%s""",
                        (corr, tenant_id))
            old = cur.fetchone()
            if not old:
                raise PermissionError("correlation id belongs to another tenant")
            decision_id, disposition, status, request_id, old_reason = old
            # A process may have died after routing the boundary but before linking its request. Fall through
            # to the idempotent correlated ask so that crash recovery repairs the link without double-paging.
            if disposition != "human_required" or request_id is not None:
                return {"id": decision_id, "correlation_id": corr, "disposition": disposition,
                        "status": status, "agent_request_id": request_id, "reason": old_reason,
                        "duplicate": True}
        else:
            decision_id = row[0]

    request_id = request_id if duplicate else None
    if disposition == "human_required":
        if request_human is None:
            import agent_request
            request_human = agent_request.ask
        question = str((proposal or {}).get("question") or verdict["reason"])
        request_kind = "credential" if kind == "credential" else "decision"
        req = request_human(tenant_id, question, kind=request_kind, org_id=org_id, thread_id=thread_id,
                            correlation_id=f"authority:{corr}")
        request_id = int(req["request_id"])
        with tenant_connection(tenant_id) as c, c.cursor() as cur:
            cur.execute("""UPDATE authority_decisions SET agent_request_id=%s,updated_at=now()
                           WHERE id=%s AND tenant_id=%s""", (request_id, decision_id, tenant_id))
    audit.append(actor="authority", action="DecisionRouted", resource=str(work_ref), decision=disposition,
                 payload={"decision_id": decision_id, "correlation_id": corr, "kind": kind,
                          "authority_gap": verdict["authority_gap"], "agent_request_id": request_id},
                 tenant_id=tenant_id)
    return {"id": decision_id, "correlation_id": corr, **verdict, "status": status,
            "agent_request_id": request_id, "duplicate": duplicate}


def record_review(decision_id, tenant_id, actor, action, rationale, *, trigger="interval", state=None,
                  next_review_s=300):
    """Persist a manager decision caused by a timer or state change.

    Internal recovery can move up an arbitrary management chain, but it cannot
    silently turn into a human request. A manager wanting new CEO authority
    must name a non-internal typed boundary in a new correlated decision.
    """
    action = str(action or "").strip().lower()
    if action not in INTERNAL_ACTIONS:
        raise ValueError(f"unknown manager action: {action!r}")
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("SELECT kind,status FROM authority_decisions WHERE id=%s AND tenant_id=%s FOR UPDATE",
                    (decision_id, tenant_id))
        row = cur.fetchone()
        if not row:
            raise KeyError(f"authority decision {decision_id} not found")
        kind, status = row
        if status not in ("manager_review", "delegated"):
            raise ValueError(f"decision {decision_id} is not awaiting internal management")
        terminal = action in ("approve", "continue", "retry", "reassign", "delegate")
        new_status = "resolved" if terminal else "manager_review"
        due = None if terminal else max(30, int(next_review_s or 300))
        cur.execute("""INSERT INTO authority_reviews
            (decision_id,tenant_id,actor,trigger,action,rationale,state)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (decision_id, tenant_id, actor, trigger, action, rationale, json.dumps(state or {})))
        cur.execute("""UPDATE authority_decisions SET status=%s,rationale=%s,updated_at=now(),
                       review_due_at=CASE WHEN %s::INT IS NULL THEN NULL ELSE now()+(%s*interval '1 second') END,
                       resolved_at=CASE WHEN %s THEN now() ELSE NULL END
                       WHERE id=%s AND tenant_id=%s""",
                    (new_status, rationale, due, due, terminal, decision_id, tenant_id))
    audit.append(actor=actor, action="AuthorityManagerReview", resource=str(decision_id), decision=action,
                 payload={"trigger": trigger, "kind": kind, "status": new_status}, tenant_id=tenant_id)
    return {"id": decision_id, "status": new_status, "action": action, "review_due": bool(due)}


def state_changed(decision_id, tenant_id, state, *, actor="observer"):
    """Make a changed work state immediately eligible for an agentic review."""
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("SELECT status FROM authority_decisions WHERE id=%s AND tenant_id=%s FOR UPDATE",
                    (decision_id, tenant_id))
        row = cur.fetchone()
        if not row:
            raise KeyError(f"authority decision {decision_id} not found")
        if row[0] not in ("manager_review", "human_required"):
            return {"id": decision_id, "status": row[0], "review_due": False}
        cur.execute("""INSERT INTO authority_reviews
            (decision_id,tenant_id,actor,trigger,action,rationale,state)
            VALUES (%s,%s,%s,'state_change','observe','state changed; manager review due',%s)""",
            (decision_id, tenant_id, actor, json.dumps(state or {})))
        cur.execute("""UPDATE authority_decisions SET review_due_at=now(),updated_at=now()
                       WHERE id=%s AND tenant_id=%s""", (decision_id, tenant_id))
    return {"id": decision_id, "status": row[0], "review_due": True}


def due_reviews(limit=50):
    """Return durable manager/human follow-ups due now; consumers claim by idempotent review events."""
    _ensure()
    with connection() as c, c.cursor() as cur:
        cur.execute("""SELECT id,correlation_id,tenant_id,org_id,thread_id,work_ref,kind,proposal,
                              disposition,status,owner_role,agent_request_id,rationale
                       FROM authority_decisions
                       WHERE status IN ('manager_review','human_required')
                         AND review_due_at IS NOT NULL AND review_due_at <= now()
                       ORDER BY review_due_at,id LIMIT %s""", (max(1, min(500, int(limit))),))
        rows = cur.fetchall()
    keys = ("id", "correlation_id", "tenant_id", "org_id", "thread_id", "work_ref", "kind", "proposal",
            "disposition", "status", "owner_role", "agent_request_id", "rationale")
    return [dict(zip(keys, row)) for row in rows]


def reconcile_human_answers(limit=100):
    """Resolve correlated human gates after agent_request records an answer.

    This makes acknowledgement durable even if the process that originally
    asked has restarted. ``agent_request.answer`` already resolves the matching
    notification context key, so notification acknowledgement and work resume
    share the same request id rather than relying on text matching.
    """
    _ensure()
    import agent_request
    with connection() as c, c.cursor() as cur:
        cur.execute("""SELECT id,tenant_id,agent_request_id FROM authority_decisions
                       WHERE status='human_required' AND agent_request_id IS NOT NULL
                       ORDER BY id LIMIT %s""", (max(1, min(500, int(limit))),))
        rows = cur.fetchall()
    resolved = []
    for did, tid, rid in rows:
        if not agent_request.is_answered(rid, tenant_id=tid):
            continue
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("""UPDATE authority_decisions SET status='resolved',resolved_at=now(),
                           review_due_at=NULL,updated_at=now()
                           WHERE id=%s AND tenant_id=%s AND status='human_required'""", (did, tid))
            if cur.rowcount:
                resolved.append(did)
                cur.execute("""INSERT INTO authority_reviews
                    (decision_id,tenant_id,actor,trigger,action,rationale,state)
                    VALUES (%s,%s,'human','answer','acknowledge','correlated human answer received','{}')""",
                    (did, tid))
        if did in resolved:
            audit.append(actor="authority", action="HumanGateResolved", resource=str(did),
                         decision="resume", payload={"agent_request_id": rid}, tenant_id=tid)
    return resolved


def decision(decision_id, tenant_id):
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT id,correlation_id,tenant_id,org_id,thread_id,work_ref,kind,proposal,
                              disposition,status,owner_role,envelope_id,envelope_revision,agent_request_id,
                              rationale,review_due_at,created_at,updated_at,resolved_at
                       FROM authority_decisions WHERE id=%s AND tenant_id=%s""", (decision_id, tenant_id))
        row = cur.fetchone()
    if not row:
        return None
    keys = ("id", "correlation_id", "tenant_id", "org_id", "thread_id", "work_ref", "kind", "proposal",
            "disposition", "status", "owner_role", "envelope_id", "envelope_revision", "agent_request_id",
            "rationale", "review_due_at", "created_at", "updated_at", "resolved_at")
    out = dict(zip(keys, row))
    for key in ("review_due_at", "created_at", "updated_at", "resolved_at"):
        out[key] = out[key].isoformat() if isinstance(out[key], datetime) else out[key]
    return out
