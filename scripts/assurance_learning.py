#!/usr/bin/env python3
"""Independent acceptance, blameless learning, and bounded coaching.

This module keeps three boundaries load-bearing:
* executors and their line managers cannot accept their own work;
* incidents produce system findings and independently verified countermeasures;
* coaching is evidence-linked, visible, expiring and developmental -- never a
  hidden score, leaderboard, compensation input, or single-metric target.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import audit  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402

_ensured = False
_lock = threading.Lock()
_VERDICTS = {"accepted", "rejected", "changes_requested"}
_FORBIDDEN_COACHING_KEYS = {
    "score", "rating", "rank", "leaderboard", "quota", "compensation",
    "promotion", "discipline", "termination", "productivity_score",
}


def ensure():
    global _ensured
    if _ensured:
        return True
    with _lock:
        if not _ensured:
            migration = SCRIPTS.parent / "postgres" / "initdb" / "60-assurance-learning.sql"
            with connection() as c, c.cursor() as cur:
                cur.execute(migration.read_text())
            _ensured = True
    return True


def validate_independence(executor_id, manager_id, reviewer_id, basis=None):
    """Validate structural separation; confidence or seniority cannot waive it."""
    executor, manager, reviewer = map(lambda x: str(x or "").strip(),
                                      (executor_id, manager_id, reviewer_id))
    if not all((executor, manager, reviewer)):
        raise ValueError("executor_id, manager_id, and reviewer_id are required")
    if len({executor, manager, reviewer}) != 3:
        raise ValueError("reviewer, executor, and manager must be three distinct identities")
    basis = dict(basis or {})
    if basis.get("reports_to_executor") or basis.get("review_incentive_owned_by_executor"):
        raise ValueError("reviewer has a structural conflict of interest")
    if not any(basis.get(k) for k in
               ("separate_reporting_line", "external_assurance", "cross_functional_mandate")):
        raise ValueError("independence_basis must establish a separate assurance mandate")
    return {"executor_id": executor, "manager_id": manager, "reviewer_id": reviewer,
            "independence_basis": basis}


def validate_verdict(status, verdict, *, actor, reviewer_id, evidence_ids,
                     rationale, criterion_results):
    verdict = str(verdict or "").lower()
    if status not in {"awaiting_evidence", "ready"}:
        raise ValueError(f"review is already {status}")
    if actor != reviewer_id:
        raise PermissionError("only the independent assigned reviewer may decide")
    if verdict not in _VERDICTS:
        raise ValueError(f"unknown verdict: {verdict}")
    evidence_ids = list(dict.fromkeys(str(x) for x in (evidence_ids or []) if str(x)))
    if not evidence_ids:
        raise ValueError("a verdict requires cited evidence")
    if not str(rationale or "").strip():
        raise ValueError("a verdict requires rationale")
    results = dict(criterion_results or {})
    if not results:
        raise ValueError("a verdict requires criterion-by-criterion results")
    if verdict == "accepted" and any(v not in (True, "pass", "met") for v in results.values()):
        raise ValueError("acceptance requires every criterion to be met")
    return {"verdict": verdict, "evidence_ids": evidence_ids,
            "rationale": str(rationale).strip(), "criterion_results": results}


def open_review(tenant_id, subject_type, subject_id, executor_id, manager_id,
                reviewer_id, acceptance_contract, *, submitted_by,
                independence_basis=None, work_contract_id=None, review_id=None):
    ensure()
    independent = validate_independence(executor_id, manager_id, reviewer_id,
                                        independence_basis)
    acceptance = dict(acceptance_contract or {})
    if not acceptance:
        raise ValueError("acceptance_contract is required")
    rid = str(review_id or f"ar-{uuid.uuid4().hex}")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO assurance_reviews
          (review_id,tenant_id,subject_type,subject_id,work_contract_id,executor_id,
           manager_id,reviewer_id,acceptance_contract,independence_basis,submitted_by)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
          (rid, str(tenant_id), str(subject_type), str(subject_id), work_contract_id,
           independent["executor_id"], independent["manager_id"], independent["reviewer_id"],
           json.dumps(acceptance), json.dumps(independent["independence_basis"]), submitted_by))
    audit.append(actor=submitted_by, action="AssuranceReviewOpened", resource=rid,
                 decision="independent_review_required", tenant_id=str(tenant_id),
                 payload={"subject_type": subject_type, "subject_id": subject_id,
                          "reviewer": reviewer_id})
    return {"review_id": rid, "status": "awaiting_evidence", **independent}


def add_evidence(tenant_id, review_id, kind, uri, digest, claims, *, submitted_by,
                 observed_at=None, metadata=None, evidence_id=None):
    ensure()
    if not all(str(x or "").strip() for x in (kind, uri, digest)) or not dict(claims or {}):
        raise ValueError("kind, uri, digest, and evidence claims are required")
    eid = str(evidence_id or f"ae-{uuid.uuid4().hex}")
    observed = observed_at or datetime.now(timezone.utc)
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO assurance_evidence
          (evidence_id,tenant_id,review_id,kind,uri,digest,claims,metadata,submitted_by,observed_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
          (eid, str(tenant_id), review_id, kind, uri, digest, json.dumps(dict(claims)),
           json.dumps(dict(metadata or {})), submitted_by, observed))
        cur.execute("""UPDATE assurance_reviews SET status='ready'
                       WHERE tenant_id=%s AND review_id=%s AND status='awaiting_evidence'""",
                    (str(tenant_id), review_id))
    return {"evidence_id": eid, "review_id": review_id}


def decide(tenant_id, review_id, actor, verdict, evidence_ids, rationale, criterion_results):
    """Atomically persist the immutable verdict; no silent reviewer override."""
    ensure()
    learning_incident_id = postmortem_id = None
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT status,reviewer_id,work_contract_id FROM assurance_reviews
                       WHERE tenant_id=%s AND review_id=%s FOR UPDATE""",
                    (str(tenant_id), review_id))
        row = cur.fetchone()
        if not row:
            raise KeyError(review_id)
        decision = validate_verdict(row[0], verdict, actor=actor, reviewer_id=row[1],
                                    evidence_ids=evidence_ids, rationale=rationale,
                                    criterion_results=criterion_results)
        cur.execute("""SELECT evidence_id FROM assurance_evidence
                       WHERE tenant_id=%s AND review_id=%s AND evidence_id=ANY(%s)""",
                    (str(tenant_id), review_id, decision["evidence_ids"]))
        present = {r[0] for r in cur.fetchall()}
        missing = set(decision["evidence_ids"]) - present
        if missing:
            raise ValueError(f"unknown evidence for this review: {sorted(missing)}")
        vid = f"av-{uuid.uuid4().hex}"
        cur.execute("""INSERT INTO assurance_verdicts
          (verdict_id,tenant_id,review_id,verdict,decided_by,rationale,criterion_results,evidence_ids)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
          (vid, str(tenant_id), review_id, decision["verdict"], actor,
           decision["rationale"], json.dumps(decision["criterion_results"]),
           json.dumps(decision["evidence_ids"])))
        cur.execute("""UPDATE assurance_reviews SET status=%s,decided_at=now()
                       WHERE review_id=%s""", (decision["verdict"], review_id))
        if row[2]:
            if decision["verdict"] == "accepted":
                cur.execute("""UPDATE work_contracts SET status='completed',completed_at=now(),
                               updated_at=now() WHERE tenant_id=%s AND contract_id=%s""",
                            (str(tenant_id), row[2]))
            else:
                cur.execute("""UPDATE work_contracts SET status='active',completed_at=NULL,
                               next_checkin_at=now(),updated_at=now()
                               WHERE tenant_id=%s AND contract_id=%s""",
                            (str(tenant_id), row[2]))
        if decision["verdict"] == "rejected":
            learning_incident_id = f"li-{uuid.uuid4().hex}"
            postmortem_id = f"pm-{uuid.uuid4().hex}"
            cur.execute("""INSERT INTO learning_incidents
              (incident_id,tenant_id,trigger_type,trigger_ref,severity,summary,detected_by,status)
              VALUES (%s,%s,'assurance_rejection',%s,'medium',%s,%s,'learning')""",
              (learning_incident_id, str(tenant_id), review_id,
               f"Independent assurance rejected {review_id}: {decision['rationale']}", actor))
            cur.execute("""INSERT INTO postmortems
              (postmortem_id,tenant_id,incident_id,facilitator_id,impact,timeline)
              VALUES (%s,%s,%s,%s,%s,%s)""",
              (postmortem_id, str(tenant_id), learning_incident_id, actor,
               json.dumps({"assurance_review": review_id}), json.dumps([])))
    audit.append(actor=actor, action="AssuranceVerdict", resource=review_id,
                 decision=decision["verdict"], tenant_id=str(tenant_id),
                 payload={"evidence_ids": decision["evidence_ids"]})
    return {"verdict_id": vid, "review_id": review_id,
            "learning_incident_id": learning_incident_id,
            "postmortem_id": postmortem_id, **decision}


def validate_corrective_verification(status, *, actor, verifier_id,
                                     implementation_evidence, verification_evidence):
    if status not in {"open", "in_progress", "implemented", "ineffective"}:
        raise ValueError(f"action is already {status}")
    if actor != verifier_id:
        raise PermissionError("the action owner cannot self-verify the countermeasure")
    if not list(implementation_evidence or []) or not list(verification_evidence or []):
        raise ValueError("verification requires implementation and outcome evidence")
    return True


def requires_postmortem(trigger_type, severity, *, recurrence_of=None):
    """State-based learning trigger, not an elapsed-time performance judgment."""
    trigger = str(trigger_type or "").lower()
    severity = str(severity or "").lower()
    if trigger not in {"assurance_rejection", "repeat_failure", "customer_impact",
                       "security", "reliability", "manual"}:
        raise ValueError(f"unknown incident trigger: {trigger}")
    if severity not in {"low", "medium", "high", "critical"}:
        raise ValueError(f"unknown incident severity: {severity}")
    return bool(recurrence_of or severity in {"high", "critical"} or
                trigger in {"assurance_rejection", "repeat_failure", "security"})


def open_incident(tenant_id, trigger_type, severity, summary, *, detected_by,
                  trigger_ref=None, recurrence_of=None, facilitator_id=None,
                  impact=None, timeline=None, incident_id=None):
    """Record an incident and automatically open a postmortem when policy triggers."""
    ensure()
    required = requires_postmortem(trigger_type, severity, recurrence_of=recurrence_of)
    if not str(summary or "").strip():
        raise ValueError("incident summary is required")
    if required and not str(facilitator_id or "").strip():
        raise ValueError("a triggered postmortem requires a facilitator")
    iid = str(incident_id or f"li-{uuid.uuid4().hex}")
    pmid = f"pm-{uuid.uuid4().hex}" if required else None
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO learning_incidents
          (incident_id,tenant_id,trigger_type,trigger_ref,severity,summary,detected_by,
           recurrence_of,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
          (iid, str(tenant_id), trigger_type, trigger_ref, severity, str(summary).strip(),
           detected_by, recurrence_of, "learning" if required else "open"))
        if required:
            cur.execute("""INSERT INTO postmortems
              (postmortem_id,tenant_id,incident_id,facilitator_id,impact,timeline)
              VALUES (%s,%s,%s,%s,%s,%s)""",
              (pmid, str(tenant_id), iid, facilitator_id,
               json.dumps(dict(impact or {})), json.dumps(list(timeline or []))))
    audit.append(actor=detected_by, action="LearningIncidentOpened", resource=iid,
                 decision="postmortem_required" if required else "recorded",
                 tenant_id=str(tenant_id), payload={"trigger": trigger_type,
                 "severity": severity, "postmortem_id": pmid})
    return {"incident_id": iid, "postmortem_required": required, "postmortem_id": pmid}


def add_finding(tenant_id, postmortem_id, system_condition, contributing_factors,
                evidence_refs, *, created_by, finding_id=None):
    """Record a system condition; the model has no culprit/person-at-fault field."""
    if not str(system_condition or "").strip() or not list(evidence_refs or []):
        raise ValueError("a finding requires a system condition and evidence")
    factors = dict(contributing_factors or {})
    blame_keys = {"culprit", "person_at_fault", "faulted_agent"}.intersection(_walk_keys(factors))
    if blame_keys:
        raise ValueError("postmortem findings must describe system conditions, not blame a person")
    ensure()
    fid = str(finding_id or f"lf-{uuid.uuid4().hex}")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO learning_findings
          (finding_id,tenant_id,postmortem_id,system_condition,contributing_factors,
           evidence_refs,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
          (fid, str(tenant_id), postmortem_id, str(system_condition).strip(),
           json.dumps(factors), json.dumps(list(evidence_refs)), created_by))
    return {"finding_id": fid, "postmortem_id": postmortem_id}


def add_corrective_action(tenant_id, postmortem_id, action, owner_id, verifier_id,
                          due_at, success_measure, *, finding_id=None, action_id=None):
    if not all(str(x or "").strip() for x in (action, owner_id, verifier_id)):
        raise ValueError("action, owner, and verifier are required")
    if owner_id == verifier_id:
        raise ValueError("corrective action owner and verifier must be independent")
    measure = dict(success_measure or {})
    if not measure:
        raise ValueError("corrective action requires an observable success measure")
    ensure()
    aid = str(action_id or f"ca-{uuid.uuid4().hex}")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO corrective_actions
          (action_id,tenant_id,postmortem_id,finding_id,action,owner_id,verifier_id,due_at,
           success_measure) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
          (aid, str(tenant_id), postmortem_id, finding_id, str(action).strip(), owner_id,
           verifier_id, due_at, json.dumps(measure)))
    return {"action_id": aid, "status": "open", "owner_id": owner_id,
            "verifier_id": verifier_id}


def mark_action_implemented(tenant_id, action_id, actor, implementation_evidence):
    evidence = list(implementation_evidence or [])
    if not evidence:
        raise ValueError("implementation evidence is required")
    ensure()
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""UPDATE corrective_actions SET status='implemented',
                       implementation_evidence=%s WHERE tenant_id=%s AND action_id=%s
                       AND owner_id=%s AND status IN ('open','in_progress','ineffective')""",
                    (json.dumps(evidence), str(tenant_id), action_id, actor))
        if cur.rowcount != 1:
            raise PermissionError("only the action owner may mark an open action implemented")
    return {"action_id": action_id, "status": "implemented"}


def verify_action(tenant_id, action_id, actor, verification_evidence):
    """Independently verify observed effect, not merely that a change was deployed."""
    ensure()
    evidence = list(verification_evidence or [])
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT status,verifier_id,implementation_evidence
                       FROM corrective_actions WHERE tenant_id=%s AND action_id=%s FOR UPDATE""",
                    (str(tenant_id), action_id))
        row = cur.fetchone()
        if not row:
            raise KeyError(action_id)
        validate_corrective_verification(row[0], actor=actor, verifier_id=row[1],
                                         implementation_evidence=row[2],
                                         verification_evidence=evidence)
        cur.execute("SELECT set_config('app.actor_id', %s, true)", (str(actor),))
        cur.execute("""UPDATE corrective_actions SET status='verified',
                       verification_evidence=%s,verified_at=now() WHERE action_id=%s""",
                    (json.dumps(evidence), action_id))
    return {"action_id": action_id, "status": "verified", "verified_by": actor}


def record_recurrence(tenant_id, incident_id, prior_incident_id, detected_by,
                      evidence, disposition, *, related_action_id=None,
                      recurrence_id=None):
    if incident_id == prior_incident_id:
        raise ValueError("a recurrence must reference a prior incident")
    if disposition not in {"new_pattern", "same_pattern", "countermeasure_failed"}:
        raise ValueError("unknown recurrence disposition")
    if not dict(evidence or {}):
        raise ValueError("recurrence requires evidence")
    ensure()
    rid = str(recurrence_id or f"rc-{uuid.uuid4().hex}")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO recurrence_checks
          (recurrence_id,tenant_id,incident_id,prior_incident_id,related_action_id,
           detected_by,evidence,disposition) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
          (rid, str(tenant_id), incident_id, prior_incident_id, related_action_id,
           detected_by, json.dumps(dict(evidence)), disposition))
        cur.execute("""UPDATE learning_incidents SET recurrence_of=%s,status='learning'
                       WHERE tenant_id=%s AND incident_id=%s""",
                    (prior_incident_id, str(tenant_id), incident_id))
        if disposition == "countermeasure_failed" and related_action_id:
            cur.execute("""UPDATE corrective_actions SET status='ineffective'
                           WHERE tenant_id=%s AND action_id=%s""",
                        (str(tenant_id), related_action_id))
    return {"recurrence_id": rid, "disposition": disposition}


def validate_postmortem_closure(finding_count, action_statuses):
    statuses = list(action_statuses or [])
    if int(finding_count) < 1:
        raise ValueError("postmortem closure requires at least one evidence-backed finding")
    if not statuses:
        raise ValueError("postmortem closure requires at least one corrective action")
    if any(status != "verified" for status in statuses):
        raise ValueError("every corrective action must be independently verified")
    return True


def close_postmortem(tenant_id, postmortem_id, actor):
    """Close the loop only after findings and countermeasures have proven effect."""
    ensure()
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT count(*) FROM learning_findings
                       WHERE tenant_id=%s AND postmortem_id=%s""",
                    (str(tenant_id), postmortem_id))
        finding_count = cur.fetchone()[0]
        cur.execute("""SELECT status FROM corrective_actions
                       WHERE tenant_id=%s AND postmortem_id=%s""",
                    (str(tenant_id), postmortem_id))
        statuses = [r[0] for r in cur.fetchall()]
        validate_postmortem_closure(finding_count, statuses)
        cur.execute("""UPDATE postmortems SET status='closed',closed_at=now()
                       WHERE tenant_id=%s AND postmortem_id=%s
                       RETURNING incident_id""", (str(tenant_id), postmortem_id))
        row = cur.fetchone()
        if not row:
            raise KeyError(postmortem_id)
        cur.execute("""UPDATE learning_incidents SET status='monitoring'
                       WHERE tenant_id=%s AND incident_id=%s""",
                    (str(tenant_id), row[0]))
    audit.append(actor=actor, action="PostmortemClosed", resource=postmortem_id,
                 decision="countermeasures_verified", tenant_id=str(tenant_id),
                 payload={"finding_count": finding_count, "action_count": len(statuses)})
    return {"postmortem_id": postmortem_id, "status": "closed",
            "incident_status": "monitoring"}


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key).lower()
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def normalize_coaching(subject_id, observer_id, work_context, observed_behavior,
                       impact, suggested_practice, evidence_refs, *, policy=None,
                       retention_days=90):
    """Create developmental feedback while refusing common Goodhart/punitive uses."""
    fields = {"subject_id": subject_id, "observer_id": observer_id,
              "work_context": work_context, "observed_behavior": observed_behavior,
              "impact": impact, "suggested_practice": suggested_practice,
              "evidence_refs": list(evidence_refs or []), "policy": dict(policy or {})}
    if not all(str(fields[k] or "").strip() for k in
               ("subject_id", "observer_id", "work_context", "observed_behavior", "impact",
                "suggested_practice")):
        raise ValueError("coaching requires specific context, behavior, impact, and practice")
    if not fields["evidence_refs"]:
        raise ValueError("coaching must cite reviewable evidence")
    prohibited = _FORBIDDEN_COACHING_KEYS.intersection(_walk_keys(fields))
    if prohibited:
        raise ValueError(f"coaching cannot be used for ranking or punitive decisions: {sorted(prohibited)}")
    policy = fields["policy"]
    if policy.get("single_metric_basis") or policy.get("hidden_from_subject") or policy.get("punitive_use"):
        raise ValueError("unsafe coaching scope: require diverse context, subject visibility, and non-punitive use")
    days = int(retention_days)
    if not 1 <= days <= 365:
        raise ValueError("coaching retention must be between 1 and 365 days")
    fields.pop("policy")
    return {**fields, "purpose": "developmental", "subject_visible": True,
            "punitive_use": False, "retention_days": days}


def record_coaching(tenant_id, subject_id, observer_id, work_context,
                    observed_behavior, impact, suggested_practice, evidence_refs,
                    *, policy=None, retention_days=90, observation_id=None):
    ensure()
    item = normalize_coaching(subject_id, observer_id, work_context, observed_behavior,
                              impact, suggested_practice, evidence_refs,
                              policy=policy, retention_days=retention_days)
    oid = str(observation_id or f"co-{uuid.uuid4().hex}")
    expires = datetime.now(timezone.utc) + timedelta(days=item.pop("retention_days"))
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO coaching_observations
          (observation_id,tenant_id,subject_id,observer_id,work_context,observed_behavior,
           impact,suggested_practice,evidence_refs,purpose,subject_visible,punitive_use,expires_at)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
          (oid, str(tenant_id), item["subject_id"], item["observer_id"],
           item["work_context"], item["observed_behavior"], item["impact"],
           item["suggested_practice"], json.dumps(item["evidence_refs"]),
           item["purpose"], item["subject_visible"], item["punitive_use"], expires))
    return {"observation_id": oid, "expires_at": expires.isoformat(), **item}
