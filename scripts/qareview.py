#!/usr/bin/env python3
"""Durable internal adjudication for disputed QA evidence.

The module has a deliberately narrow boundary:

* ``submit`` consumes the stable ``internal_review`` record emitted by QA management.
* ``state_changed`` makes a nonterminal case immediately runnable without creating a human gate.
* ``claim`` leases one case with a fencing token.
* ``adjudicate`` records read-only worker -> manager -> senior-manager reviews and returns a terminal
  disposition only when sealed finding-time evidence supports it, or when the senior names a typed external
  authority. Ordinary uncertainty is requeued for internal management; it never pages or parks the CEO.

No function mutates a product checkout, launches a process, or sends an external request.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from dbpool import connection, tenant_connection  # noqa: E402
import redact  # noqa: E402


ROLES = ("qa-evidence-reviewer", "qa-manager", "senior-qa-director")
TERMINAL = frozenset({"confirmed_defect", "verified_false_positive",
                      "needs_named_external_authority"})
RUNTIME_SUPERSEDING = frozenset({"superseded_by_current_revision",
                                 "superseded_by_fresh_evidence"})
EXTERNAL_AUTHORITY_TYPES = frozenset({
    "legal_counsel", "security_owner", "privacy_owner", "accessibility_authority",
    "customer_contract_owner", "regulated_domain_expert",
})
MIN_CONFIDENCE = 0.80
MAX_MANIFEST_BYTES = 1_000_000
MAX_EVIDENCE = 24
MAX_BROWSER_EVIDENCE = 12
MAX_PROMPT_EVIDENCE = 8
MAX_CONTEXT_LINES = 39
MAX_BROWSER_ARTIFACT_BYTES = 50_000_000
EVIDENCE_COLLECTION_REVIEW_S = 15 * 60
MANAGEMENT_TRIGGER = "qa_evidence_still_uncertain"
_ensured = False
_ensure_lock = threading.Lock()


class LeaseLost(RuntimeError):
    """The case changed generation or this claimant no longer owns its lease."""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def stable_case_id(tenant_id, review_id) -> str:
    return "qad-" + hashlib.sha256(f"{tenant_id}:{review_id}".encode()).hexdigest()[:32]


def management_case_key(tenant_id, review_id) -> str:
    """One management identity for a dispute, regardless of which observer found it."""
    return f"qa-dispute:{tenant_id}:{review_id}"


def management_case_state(evidence) -> dict:
    """Stable semantic state shared by runtime and duty observers.

    Volatile observation fields such as age and updated timestamps must not alter a management fingerprint:
    otherwise every duty sweep wakes another meeting even though the dispute itself did not change.
    """
    item = evidence or {}
    try:
        generation = max(1, int(item.get("state_generation") or 1))
    except (TypeError, ValueError):
        generation = 1
    return {"case_id": item.get("case_id"), "review_id": item.get("review_id"),
            "status": item.get("status") or "manager_review", "state_generation": generation}


def _provenance_ref(review) -> dict | None:
    finding = (review or {}).get("finding") or {}
    item = finding
    for _ in range(4):
        if not isinstance(item, dict):
            break
        ref = item.get("evidence_provenance")
        if isinstance(ref, dict):
            return ref
        item = item.get("recovery_trigger")
    ref = ((review or {}).get("triage") or {}).get("evidence_provenance")
    return ref if isinstance(ref, dict) else None


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with connection() as c, c.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS qa_evidence_disputes (
                case_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, review_id TEXT NOT NULL,
                thread_id BIGINT, run_id BIGINT, coordinator_actor_id BIGINT,
                authority_decision_id BIGINT, work_ref TEXT NOT NULL, repo TEXT NOT NULL,
                internal_review JSONB NOT NULL, internal_review_digest TEXT NOT NULL,
                state JSONB NOT NULL DEFAULT '{}', state_digest TEXT NOT NULL,
                state_generation BIGINT NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','leased','manager_review','resolved','external_authority')),
                current_tier INT NOT NULL DEFAULT 0 CHECK(current_tier BETWEEN 0 AND 2),
                lease_owner TEXT, lease_token TEXT, lease_until TIMESTAMPTZ,
                attempts INT NOT NULL DEFAULT 0, next_review_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                outcome JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), resolved_at TIMESTAMPTZ,
                UNIQUE (tenant_id,review_id))""")
            cur.execute("""CREATE INDEX IF NOT EXISTS qa_evidence_disputes_claim_idx
                ON qa_evidence_disputes(next_review_at,created_at)
                WHERE status IN ('pending','manager_review','leased')""")
            cur.execute("""CREATE INDEX IF NOT EXISTS qa_evidence_disputes_tenant_idx
                ON qa_evidence_disputes(tenant_id,status,updated_at DESC)""")
            cur.execute("ALTER TABLE qa_evidence_disputes ADD COLUMN IF NOT EXISTS run_id BIGINT")
            cur.execute("ALTER TABLE qa_evidence_disputes ADD COLUMN IF NOT EXISTS coordinator_actor_id BIGINT")
            cur.execute("ALTER TABLE qa_evidence_disputes ADD COLUMN IF NOT EXISTS authority_decision_id BIGINT")
            cur.execute("""CREATE TABLE IF NOT EXISTS qa_evidence_dispute_reviews (
                id BIGSERIAL PRIMARY KEY, case_id TEXT NOT NULL REFERENCES qa_evidence_disputes(case_id),
                tenant_id TEXT NOT NULL, state_digest TEXT NOT NULL,
                tier INT NOT NULL CHECK(tier BETWEEN 0 AND 2),
                actor_role TEXT NOT NULL, review JSONB NOT NULL, evidence_digest TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(case_id,state_digest,tier))""")
            cur.execute("""CREATE INDEX IF NOT EXISTS qa_evidence_dispute_reviews_tenant_idx
                ON qa_evidence_dispute_reviews(tenant_id,case_id,state_digest,tier)""")
            cur.execute("""CREATE OR REPLACE FUNCTION qa_dispute_immutable_input() RETURNS trigger AS $$
                BEGIN
                  IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
                     OR NEW.review_id IS DISTINCT FROM OLD.review_id
                     OR NEW.repo IS DISTINCT FROM OLD.repo
                     OR NEW.internal_review IS DISTINCT FROM OLD.internal_review
                     OR NEW.internal_review_digest IS DISTINCT FROM OLD.internal_review_digest THEN
                    RAISE EXCEPTION 'qa evidence dispute input is immutable';
                  END IF;
                  RETURN NEW;
                END $$ LANGUAGE plpgsql""")
            cur.execute("DROP TRIGGER IF EXISTS qa_dispute_immutable_input_guard ON qa_evidence_disputes")
            cur.execute("""CREATE TRIGGER qa_dispute_immutable_input_guard
                BEFORE UPDATE ON qa_evidence_disputes FOR EACH ROW
                EXECUTE FUNCTION qa_dispute_immutable_input()""")
        _ensured = True


def ensure():
    _ensure()
    return True


def _validate_internal_review(record) -> dict:
    if not isinstance(record, dict):
        raise ValueError("internal_review must be an object")
    item = json.loads(_canonical(record))
    if not str(item.get("review_id") or "").strip():
        raise ValueError("stable internal_review.review_id is required")
    if item.get("route") not in (None, "qa-internal-management"):
        raise ValueError("internal_review is not routed to QA internal management")
    finding = item.get("finding")
    if not isinstance(finding, dict) or not finding:
        raise ValueError("internal_review.finding is required")
    provenance = _provenance_ref(item)
    if not isinstance(provenance, dict):
        raise ValueError("finding-time evidence_provenance is required")
    if not provenance.get("manifest_path") or not provenance.get("manifest_sha256"):
        raise ValueError("sealed finding-time provenance reference is incomplete")
    return item


def _immutable_finding_identity(item) -> dict:
    """Semantic case identity, excluding context that legitimately grows between at-least-once deliveries.

    Independent triage prose and the related-finding rollup are evidence-routing context, not the disputed
    observation itself. Requiring those volatile fields to remain byte-identical caused an already-resolved
    case to reject its own replay after another explorer found a related symptom.
    """
    item = dict(item or {})
    finding = dict(item.get("finding") or {})
    for key in ("related_findings", "related_finding_count", "exploration_blocking",
                "_qa_adjudication"):
        finding.pop(key, None)
    # ``reason`` and ``triage`` describe why this observation is being routed *now*. They legitimately
    # change after revision re-verification or a terminal adjudication is attached. ``state`` is likewise
    # queue workflow, not observation identity. Including any of them here made a resolved case reject its
    # own at-least-once replay, after which the coordinator spawned another reviewer and browser forever.
    return {"review_id": item.get("review_id"), "route": item.get("route"),
            "story": item.get("story"), "finding": finding}


def submit(tenant_id, internal_review, *, repo, thread_id=None, run_id=None,
           coordinator_actor_id=None, work_ref=None, state=None) -> dict:
    """Persist one immutable stable review id; replay returns the existing case."""
    _ensure()
    item = _validate_internal_review(internal_review)
    review_id = str(item["review_id"])
    case_id = stable_case_id(str(tenant_id), review_id)
    root = str(Path(repo).resolve())
    state = dict(state or {"trigger": "internal_review_created"})
    input_digest, state_digest = _digest(item), _digest(state)
    work_ref = str(work_ref or f"qa-review:{review_id}")
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"qareview:{case_id}",))
        cur.execute("""INSERT INTO qa_evidence_disputes
            (case_id,tenant_id,review_id,thread_id,run_id,coordinator_actor_id,
             work_ref,repo,internal_review,
             internal_review_digest,state,state_digest)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(case_id) DO UPDATE SET
              thread_id=COALESCE(qa_evidence_disputes.thread_id,EXCLUDED.thread_id),
              run_id=COALESCE(qa_evidence_disputes.run_id,EXCLUDED.run_id),
              coordinator_actor_id=COALESCE(qa_evidence_disputes.coordinator_actor_id,
                                            EXCLUDED.coordinator_actor_id)
            RETURNING (xmax=0)""",
            (case_id, str(tenant_id), review_id, thread_id, run_id, coordinator_actor_id, work_ref, root,
             json.dumps(item), input_digest, json.dumps(state), state_digest))
        inserted = bool(cur.fetchone()[0])
        cur.execute("""SELECT tenant_id,internal_review_digest,status,outcome,state_generation,internal_review
                       FROM qa_evidence_disputes WHERE case_id=%s""", (case_id,))
        row = cur.fetchone()
        if not row or row[0] != str(tenant_id):
            raise PermissionError("review id belongs to another tenant")
        if (row[1] != input_digest
                and _immutable_finding_identity(row[5]) != _immutable_finding_identity(item)):
            raise ValueError("stable review_id was replayed with different immutable content")
    return {"case_id": case_id, "review_id": review_id, "status": row[2],
            "outcome": row[3], "state_generation": row[4], "duplicate": not inserted}


def state_changed(tenant_id, case_id, state, *, actor="qa-management") -> dict:
    """Requeue a nonterminal case only when its durable observed state actually changes."""
    _ensure()
    state = dict(state or {})
    _ = actor  # observer identity is intentionally excluded from the semantic state-change fingerprint
    digest = _digest(state)
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT status,state_digest,state_generation,outcome FROM qa_evidence_disputes
                       WHERE case_id=%s AND tenant_id=%s FOR UPDATE""", (case_id, str(tenant_id)))
        row = cur.fetchone()
        if not row:
            raise KeyError(f"QA evidence dispute {case_id} not found")
        if row[0] == "resolved" or row[1] == digest:
            return {"case_id": case_id, "status": row[0], "changed": False,
                    "state_generation": row[2], "outcome": row[3]}
        cur.execute("""UPDATE qa_evidence_disputes SET state=%s,state_digest=%s,
                       state_generation=state_generation+1,status='pending',current_tier=0,
                       lease_owner=NULL,lease_token=NULL,lease_until=NULL,next_review_at=now(),
                       outcome=NULL,resolved_at=NULL,updated_at=now()
                       WHERE case_id=%s AND tenant_id=%s RETURNING state_generation""",
                    (json.dumps(state), digest, case_id, str(tenant_id)))
        generation = cur.fetchone()[0]
    return {"case_id": case_id, "status": "pending", "changed": True,
            "state_generation": generation}


def supersede_by_revision(tenant_id, case_id, product_revision, *, reason=None) -> dict:
    """Resolve evidence about older product bytes without replaying its browser journey.

    A repository mutation invalidates the observation's authority, not its audit history.  Keeping the old
    dispute in ``manager_review`` caused duty management to wake the current-revision coordinator and
    reproduce the same stale finding indefinitely.  This terminal disposition retains the case and rationale
    while making the new revision's ordinary story run the only path back to release evidence.
    """
    _ensure()
    revision = str(product_revision or "").strip()
    if not revision:
        raise ValueError("product_revision is required")
    rationale = str(reason or "product bytes changed; current-revision story evidence is required")[:2000]
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT status,outcome,state_generation FROM qa_evidence_disputes
                       WHERE case_id=%s AND tenant_id=%s FOR UPDATE""",
                    (case_id, str(tenant_id)))
        row = cur.fetchone()
        if not row:
            raise KeyError(f"QA evidence dispute {case_id} not found")
        if row[0] == "resolved":
            return {"case_id": case_id, "status": "resolved", "changed": False,
                    "state_generation": row[2], "outcome": row[1]}
        outcome = {
            "disposition": "superseded_by_current_revision",
            "decided_by": "qa-revision-fence",
            "confidence": 1.0,
            "rationale": rationale,
            "evidence_ids": [],
            "external_authority": None,
            "state_generation": row[2],
            "product_revision": revision,
        }
        cur.execute("""UPDATE qa_evidence_disputes SET status='resolved',outcome=%s,resolved_at=now(),
                       lease_owner=NULL,lease_token=NULL,lease_until=NULL,updated_at=now()
                       WHERE case_id=%s AND tenant_id=%s""",
                    (json.dumps(outcome), case_id, str(tenant_id)))
    return {"case_id": case_id, "status": "resolved", "changed": True,
            "state_generation": row[2], "outcome": outcome}


def begin_evidence_collection(tenant_id, case_id, state, *, actor="qa-management") -> dict:
    """Fence a manager-requested evidence pass without replaying the same adjudication.

    A management action is an instruction, not new product evidence.  Keeping the case in
    ``manager_review`` while recording the collection generation prevents generic review workers from
    replaying the unchanged bundle.  ``state_changed`` makes the case claimable only after the coordinator
    supplies a fresh result.  A stale collection becomes visible to duty management again after a bounded
    health-review interval, so a lost coordinator event cannot strand the case.
    """
    _ensure()
    state = {**dict(state or {}), "trigger": "evidence_collection_started"}
    _ = actor
    digest = _digest(state)
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT status,state_digest,state_generation,outcome FROM qa_evidence_disputes
                       WHERE case_id=%s AND tenant_id=%s FOR UPDATE""", (case_id, str(tenant_id)))
        row = cur.fetchone()
        if not row:
            raise KeyError(f"QA evidence dispute {case_id} not found")
        if row[0] in ("resolved", "external_authority") or row[1] == digest:
            return {"case_id": case_id, "status": row[0], "changed": False,
                    "state_generation": row[2], "outcome": row[3]}
        cur.execute("""UPDATE qa_evidence_disputes SET state=%s,state_digest=%s,
                       state_generation=state_generation+1,status='manager_review',current_tier=0,
                       lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                       next_review_at=now()+(%s*interval '1 second'),outcome=NULL,resolved_at=NULL,
                       updated_at=now() WHERE case_id=%s AND tenant_id=%s
                       RETURNING state_generation""",
                    (json.dumps(state), digest, EVIDENCE_COLLECTION_REVIEW_S,
                     case_id, str(tenant_id)))
        generation = cur.fetchone()[0]
    return {"case_id": case_id, "status": "manager_review", "changed": True,
            "state_generation": generation, "evidence_collection": True}


def attach_authority(tenant_id, case_id, decision_id) -> dict:
    """Correlate a named external authority request without changing the reviewed semantic state."""
    _ensure()
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""UPDATE qa_evidence_disputes SET authority_decision_id=%s,updated_at=now()
                       WHERE case_id=%s AND tenant_id=%s
                       RETURNING status""", (int(decision_id), case_id, str(tenant_id)))
        row = cur.fetchone()
    if not row:
        raise KeyError(f"QA evidence dispute {case_id} not found")
    return {"case_id": case_id, "status": row[0], "authority_decision_id": int(decision_id)}


def _wake_runtime(tenant_id, case_id, state) -> dict:
    """Resume only the exact durable QA owner after a genuine semantic state change."""
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT run_id,coordinator_actor_id,thread_id,review_id
                       FROM qa_evidence_disputes WHERE case_id=%s AND tenant_id=%s""",
                    (case_id, str(tenant_id)))
        route = cur.fetchone()
    if not route:
        return {"woken": False, "reason": "case_not_found"}
    run_id, coordinator_id, thread_id, review_id = route
    event = False
    if run_id and coordinator_id:
        try:
            orchestra = SCRIPTS / "orchestra"
            if str(orchestra) not in sys.path:
                sys.path.insert(0, str(orchestra))
            import store
            store.resume_run(int(run_id), str(tenant_id))
            emitted = store.emit_once(
                int(run_id), str(tenant_id), None, int(coordinator_id), "context_update",
                {"qa_review_state_changed": {"review_id": review_id, "case_id": case_id,
                                             "state": state}},
                f"qa-review-state:{case_id}:{_digest(state)}")
            event = not bool((emitted or {}).get("error"))
        except Exception:
            event = False
    controller = False
    if thread_id:
        try:
            with connection() as c, c.cursor() as cur:
                cur.execute("""UPDATE controller_state s SET awaiting=NULL,
                                  job_status='QA management supplied new evidence; resuming',updated_at=now()
                               WHERE s.thread_id=%s AND s.tenant_id=%s AND s.phase='TESTQA'
                                 AND s.awaiting='internal_management'
                                 AND NOT EXISTS (SELECT 1 FROM controller_jobs j
                                                 WHERE j.thread_id=s.thread_id
                                                   AND j.status IN ('pending','running'))""",
                            (int(thread_id), str(tenant_id)))
                controller = cur.rowcount == 1
        except Exception:
            controller = False
    return {"woken": bool(event or controller), "event": event, "controller": controller,
            "run_id": run_id, "thread_id": thread_id}


def resume_with_state(tenant_id, case_id, state, *, actor="qa-management") -> dict:
    changed = state_changed(tenant_id, case_id, state, actor=actor)
    wake = _wake_runtime(tenant_id, case_id, state) if changed.get("changed") else {"woken": False}
    return {**changed, **wake}


def reconcile_authority_answers(limit=100) -> list[dict]:
    """Turn answered, correlated named-authority requests into QA state changes and exact run wakeups."""
    _ensure()
    try:
        import authority
        authority.reconcile_human_answers(limit=limit)
    except Exception:
        pass
    with connection() as c, c.cursor() as cur:
        cur.execute("""SELECT q.case_id,q.tenant_id,q.authority_decision_id
                       FROM qa_evidence_disputes q JOIN authority_decisions a
                         ON a.id=q.authority_decision_id AND a.tenant_id=q.tenant_id
                       WHERE q.status='external_authority' AND a.status='resolved'
                       ORDER BY q.updated_at LIMIT %s""", (max(1, min(500, int(limit))),))
        rows = cur.fetchall()
    out = []
    for case_id, tenant_id, decision_id in rows:
        try:
            import authority
            decision = authority.decision(decision_id, tenant_id) or {}
            answer = None
            if decision.get("agent_request_id"):
                import agent_request
                answer = agent_request.get(decision["agent_request_id"], tenant_id=tenant_id)
            state = {"trigger": "named_external_authority_answered",
                     "authority_decision_id": decision_id,
                     "answer": (answer or {}).get("answer"),
                     "answered_by": ((answer or {}).get("kind") or "named_authority")}
            out.append(resume_with_state(tenant_id, case_id, state, actor="authority-reconciler"))
        except Exception as exc:
            out.append({"case_id": case_id, "error": str(exc)[:300]})
    return out


def _runtime_superseding_resolution(memory, review_id) -> dict | None:
    """Return the coordinator's terminal current-evidence resolution for one historical dispute."""
    for item in reversed(list((memory or {}).get("finding_resolutions") or [])):
        if not isinstance(item, dict) or str(item.get("review_id") or "") != str(review_id or ""):
            continue
        if str(item.get("disposition") or "") in RUNTIME_SUPERSEDING:
            return dict(item)
    return None


def reconcile_runtime_resolutions(limit=100) -> list[dict]:
    """Close dispute rows already superseded by the owning coordinator's fresh evidence ledger.

    The coordinator can correctly retire a historical finding after a clean exact current-revision replay.
    Previously that terminal resolution lived only in ``orchestra_actors.memory.finding_resolutions`` while
    the corresponding ``qa_evidence_disputes`` row stayed in ``manager_review``. The controller's fail-closed
    checkpoint then waited forever. Reconcile only the exact routed run/coordinator/review identity and only
    temporal supersession dispositions; ordinary uncertainty and named-authority cases remain untouched.
    """
    ensure()
    bound = max(1, min(500, int(limit or 1)))
    with connection() as c, c.cursor() as cur:
        cur.execute("""SELECT q.case_id,q.tenant_id,q.review_id,q.state_generation,a.memory
                         FROM qa_evidence_disputes q
                         JOIN orchestra_actors a
                           ON a.actor_id=q.coordinator_actor_id AND a.run_id=q.run_id
                          AND a.tenant_id=q.tenant_id
                        WHERE q.status NOT IN ('resolved','closed','cancelled','external_authority')
                        ORDER BY q.updated_at,q.case_id LIMIT %s""", (bound,))
        candidates = cur.fetchall()
    resolved = []
    for case_id, tenant_id, review_id, generation, memory in candidates:
        resolution = _runtime_superseding_resolution(memory, review_id)
        if not resolution:
            continue
        disposition = str(resolution.get("disposition"))
        rationale = (str(resolution.get("rationale") or "").strip()
                     or "owning QA coordinator superseded the historical finding with fresh evidence")
        outcome = {
            "disposition": disposition,
            "decided_by": "qa-coordinator-current-evidence",
            "confidence": 1.0,
            "rationale": rationale[:2000],
            "evidence_ids": [],
            "external_authority": None,
            "state_generation": int(generation or 1),
        }
        with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
            cur.execute("""UPDATE qa_evidence_disputes
                              SET status='resolved',outcome=%s,resolved_at=now(),
                                  lease_owner=NULL,lease_token=NULL,lease_until=NULL,updated_at=now()
                            WHERE case_id=%s AND tenant_id=%s
                              AND status NOT IN ('resolved','closed','cancelled','external_authority')
                        RETURNING case_id""",
                        (json.dumps(outcome), case_id, str(tenant_id)))
            changed = cur.fetchone()
        if not changed:
            continue
        terminal_state = {"trigger": "runtime_current_evidence_reconciliation",
                          "status": "resolved", "case_id": case_id,
                          "review_id": review_id, "outcome": outcome}
        resolved.append({"case_id": case_id, "tenant_id": tenant_id,
                         "review_id": review_id, "disposition": disposition,
                         **_wake_runtime(tenant_id, case_id, terminal_state)})
    return resolved


def claim(tenant_id, claimant, *, lease_s=900) -> dict | None:
    """Lease one changed case. Expired leases are recoverable; the token fences stale claimants.

    ``manager_review`` deliberately is not timer-claimable. Its three reviews are keyed by the semantic
    state digest, so polling an unchanged case would only replay the same senior uncertainty forever. The
    owning QA manager must gather evidence (or receive an authority answer) and call ``state_changed``.
    """
    _ensure()
    token = uuid.uuid4().hex
    lease_s = max(30, min(3600, int(lease_s)))
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT case_id FROM qa_evidence_disputes
            WHERE tenant_id=%s AND (
              (status='pending' AND next_review_at<=now()) OR
              (status='leased' AND lease_until<now()))
            ORDER BY next_review_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1""", (str(tenant_id),))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("""UPDATE qa_evidence_disputes SET status='leased',lease_owner=%s,lease_token=%s,
                       lease_until=now()+(%s*interval '1 second'),attempts=attempts+1,updated_at=now()
                       WHERE case_id=%s RETURNING case_id,review_id,state_generation,current_tier,lease_until""",
                    (str(claimant), token, lease_s, row[0]))
        claimed = cur.fetchone()
    return {"case_id": claimed[0], "review_id": claimed[1], "state_generation": claimed[2],
            "current_tier": claimed[3], "lease_until": claimed[4], "lease_token": token,
            "claimant": str(claimant)}


def claim_case(tenant_id, case_id, claimant, *, lease_s=900) -> dict | None:
    """Lease exactly the case assigned to a case-bound QA review actor.

    ``claim`` serves generic queue workers. An orchestra actor already owns a particular case; claiming the
    tenant's oldest different case gives it a token that cannot adjudicate its assignment and strands both.
    """
    _ensure()
    token = uuid.uuid4().hex
    lease_s = max(30, min(3600, int(lease_s)))
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT case_id FROM qa_evidence_disputes
            WHERE case_id=%s AND tenant_id=%s AND (
              (status='pending' AND next_review_at<=now()) OR
              (status='leased' AND lease_until<now()))
            FOR UPDATE SKIP LOCKED""", (case_id, str(tenant_id)))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("""UPDATE qa_evidence_disputes SET status='leased',lease_owner=%s,lease_token=%s,
                       lease_until=now()+(%s*interval '1 second'),attempts=attempts+1,updated_at=now()
                       WHERE case_id=%s AND tenant_id=%s
                       RETURNING case_id,review_id,state_generation,current_tier,lease_until""",
                    (str(claimant), token, lease_s, case_id, str(tenant_id)))
        claimed = cur.fetchone()
    return {"case_id": claimed[0], "review_id": claimed[1], "state_generation": claimed[2],
            "current_tier": claimed[3], "lease_until": claimed[4], "lease_token": token,
            "claimant": str(claimant)}


def _case(tenant_id, case_id) -> dict:
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT review_id,repo,internal_review,internal_review_digest,state,state_digest,
                              state_generation,status,current_tier,outcome,lease_token,lease_until
                       FROM qa_evidence_disputes WHERE case_id=%s AND tenant_id=%s""",
                    (case_id, str(tenant_id)))
        row = cur.fetchone()
    if not row:
        raise KeyError(f"QA evidence dispute {case_id} not found")
    keys = ("review_id","repo","internal_review","internal_review_digest","state","state_digest",
            "state_generation","status","current_tier","outcome","lease_token","lease_until")
    return dict(zip(keys, row))


_SOURCE_CALL_RE = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
_SOURCE_DEFINITION_RES = (
    re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\("),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=.*=>"),
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
)

_BEHAVIOR_EMAIL_RE = re.compile(
    r"(?i)\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+\b")
_BEHAVIOR_PHONE_RE = re.compile(r"(?<![A-Za-z0-9])(?:\+?\d[\d(). -]{7,}\d)(?![A-Za-z0-9])")


def _behavior_text(value, limit=3000) -> str:
    """Bound and redact behavior receipts before they cross the read-only model boundary."""
    if value in (None, ""):
        return ""
    raw = value if isinstance(value, str) else _canonical(value)
    safe = redact.scrub(raw)
    safe = _BEHAVIOR_EMAIL_RE.sub("‹EMAIL-REDACTED›", safe)
    safe = _BEHAVIOR_PHONE_RE.sub("‹PHONE-REDACTED›", safe)
    safe = "".join(ch for ch in safe if ch in "\n\t" or ord(ch) >= 32)
    return safe[:max(0, int(limit))]


def _behavior_action(value) -> str:
    """Keep action/target identity while excluding typed values from reviewer evidence."""
    if isinstance(value, dict):
        value = {key: value.get(key) for key in (
            "cmd", "target_text", "role", "selector", "count", "interval_ms")
                 if value.get(key) not in (None, "")}
        return _behavior_text(value, 1200)
    # tools._fmt_action separates an entered value with `` =``. It is useful to know which control was
    # exercised, but never useful (or safe) to send the entered credential/PII to another model.
    return _behavior_text(re.split(r"\s+=", str(value or ""), maxsplit=1)[0], 1200)


def _artifact_receipt(artifact_root, candidate, label) -> dict:
    """Hash a bounded artifact only when its resolved path remains inside this browser result's dossier."""
    if not artifact_root or not candidate:
        return {}
    try:
        root = Path(str(artifact_root)).resolve(strict=True)
        path = Path(str(candidate))
        path = (root / path).resolve(strict=True) if not path.is_absolute() else path.resolve(strict=True)
        rel = path.relative_to(root)
        size = path.stat().st_size
        if not path.is_file() or size < 0 or size > MAX_BROWSER_ARTIFACT_BYTES:
            return {}
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {f"{label}_relpath": str(rel), f"{label}_sha256": digest.hexdigest(),
                f"{label}_bytes": size}
    except Exception:
        return {}


def _sealed_fresh_browser_evidence(case, review, state) -> list[dict]:
    """Turn grounded current-generation browser steps into citable, state-fenced evidence receipts.

    Coverage labels by themselves are never admitted. Each receipt must carry a browser actual, exact
    grounded aspects, and the same story identity as the disputed finding. Artifact contents remain outside
    Postgres/model prompts; a path-confined digest binds available screenshots/finding state to the receipt.
    """
    if not isinstance(state, dict):
        return []
    state_digest = str(case.get("state_digest") or _digest(state))
    if case.get("state_digest") and _digest(state) != state_digest:
        return []
    result = state.get("fresh_result")
    if not isinstance(result, dict):
        return []
    finding_story = str(review.get("story") or (review.get("finding") or {}).get("story") or "")
    result_story = str(result.get("story") or result.get("story_id") or "")
    if not finding_story or result_story != finding_story:
        return []
    steps = result.get("steps_detail")
    if not isinstance(steps, list):
        return []
    artifact_root = result.get("artifact_dir")
    product_revision = _behavior_text(state.get("product_revision"), 200)
    scope = _behavior_text(result.get("recovery_scope") or "focused", 80)
    stop_reason = _behavior_text(result.get("stop_reason"), 300)
    evidence, seen = [], set()
    for index, row in enumerate(steps):
        if len(evidence) >= MAX_BROWSER_EVIDENCE:
            break
        if not isinstance(row, dict):
            continue
        covers = [_behavior_text(item, 700) for item in (row.get("covers") or []) if str(item).strip()]
        actual = _behavior_text(row.get("actual"), 6000)
        # This is the critical anti-self-certification boundary: a model-authored coverage label or bug prose
        # without Explorer's grounded browser receipt is not evidence and cannot authorize a product edit.
        if row.get("coverage_grounded") is not True or not covers or not actual:
            continue
        bug = row.get("bug")
        # Browser runners attach local artifact paths to findings so the
        # sealer can hash them below. Those host paths are neither behavioral
        # evidence nor safe model/UI content. Admit only bounded finding prose;
        # the separate path-confined artifact receipts preserve provenance.
        safe_bug = (
            {
                key: bug[key]
                for key in ("title", "detail", "severity", "kind", "blocking")
                if key in bug and bug[key] not in (None, "")
            }
            if isinstance(bug, dict)
            else bug
        )
        receipt = {
            "kind": "sealed_fresh_browser_step", "state_digest": state_digest,
            "story": finding_story, "recovery_scope": scope, "product_revision": product_revision,
            "stop_reason": stop_reason, "step_index": index,
            "action": _behavior_action(row.get("action")),
            "expected": _behavior_text(row.get("expected"), 3000),
            "actual": actual, "verdict": _behavior_text(row.get("verdict"), 1200),
            "covers": covers, "coverage_grounded": True,
            "bug": _behavior_text(safe_bug, 3000),
        }
        receipt.update(_artifact_receipt(artifact_root, row.get("screenshot"), "screenshot"))
        finding_state = bug.get("finding_state_path") if isinstance(bug, dict) else None
        receipt.update(_artifact_receipt(artifact_root, finding_state, "finding_state"))
        evidence_id = "qae-" + hashlib.sha256(
            f"fresh-browser:{state_digest}:{_canonical(receipt)}".encode()).hexdigest()[:24]
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        evidence.append({"evidence_id": evidence_id, **receipt})
    return evidence


def _sanitized_review_value(value, depth=0):
    """Sanitize non-evidence context too; sealed receipts are the only behavioral authority."""
    if depth > 5:
        return "‹TRUNCATED›"
    if isinstance(value, dict):
        return {_behavior_text(key, 160): _sanitized_review_value(item, depth + 1)
                for key, item in list(value.items())[:40]
                if key not in {"fresh_result", "fresh_findings"}}
    if isinstance(value, list):
        return [_sanitized_review_value(item, depth + 1) for item in value[:40]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _behavior_text(value, 3000)


def _review_finding_summary(review) -> dict:
    """Project one disputed observation without replaying its scenario matrix or raw typed values."""
    finding = (review or {}).get("finding") or {}
    summary = {}
    for key, limit in (
        ("story", 200), ("kind", 100), ("title", 1000), ("detail", 3000),
        ("expected", 2000), ("severity", 100), ("finding_id", 300),
    ):
        if finding.get(key) not in (None, ""):
            summary[key] = _behavior_text(finding.get(key), limit)
    if finding.get("blocking") is not None:
        summary["blocking"] = bool(finding.get("blocking"))
    action = _behavior_action(finding.get("action"))
    if action:
        summary["action"] = action
    return summary


def _review_state_summary(state) -> dict:
    """Keep decision metadata while sealed evidence carries the contract and browser behavior."""
    state = state if isinstance(state, dict) else {}
    summary = {}
    for key, limit in (("trigger", 300), ("product_revision", 300),
                       ("verification_attempt", 100), ("contract_authority", 1000)):
        if state.get(key) not in (None, ""):
            summary[key] = _behavior_text(state.get(key), limit)
    return summary


def _prompt_evidence(evidence) -> list[dict]:
    """Select the smallest decision-complete evidence set for the story-specific judge prompt."""
    ranked = []
    for index, item in enumerate(evidence or []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        verdict = str(item.get("verdict") or "").lower()
        actionable_browser = (kind == "sealed_fresh_browser_step"
                              and (bool(item.get("bug"))
                                   or any(word in verdict for word in ("mismatch", "fail", "defect"))))
        if kind == "authoritative_story_contract":
            priority = 0
        elif actionable_browser:
            priority = 1
        elif not kind:
            priority = 2  # exact finding-time source citation
        elif kind == "sealed_fresh_browser_step":
            priority = 3
        else:
            priority = 4  # dependency context is useful only after direct evidence
        ranked.append((priority, index, item))
    return [item for _priority, _index, item in sorted(ranked)[:MAX_PROMPT_EVIDENCE]]


def _source_definition_spans(lines) -> dict[str, tuple[int, int]]:
    """Find bounded top-level helper definitions for sealed citation dependency context."""
    starts = []
    for index, line in enumerate(lines, start=1):
        match = next((candidate.match(line) for candidate in _SOURCE_DEFINITION_RES
                      if candidate.match(line)), None)
        if match:
            starts.append((index, match.group(1)))
    spans = {}
    for position, (start, symbol) in enumerate(starts):
        next_start = starts[position + 1][0] if position + 1 < len(starts) else len(lines) + 1
        end = min(next_start - 1, start + MAX_CONTEXT_LINES - 1)
        while end > start and not str(lines[end - 1]).strip():
            end -= 1
        spans.setdefault(symbol, (start, end))
    return spans


def _sealed_dependency_context(ref, verified_sources, seed_quotes, seen, seen_spans, limit) -> list[dict]:
    """Expand citations only to definitions they invoke, still bound to finding-time whole-file hashes."""
    extra = []
    for canonical_rel, source in verified_sources.items():
        if len(seen) >= limit:
            break
        lines, file_digest = source
        spans = _source_definition_spans(lines)
        queue = [(symbol, 0) for quote in seed_quotes.get(canonical_rel, [])
                 for symbol in _SOURCE_CALL_RE.findall(quote) if symbol in spans]
        visited = set()
        while queue and len(seen) < limit:
            symbol, depth = queue.pop(0)
            if symbol in visited or symbol not in spans or depth > 2:
                continue
            visited.add(symbol)
            start, end = spans[symbol]
            quote = "\n".join(lines[start - 1:end])
            span = (canonical_rel, start, end)
            evidence_id = "qae-" + hashlib.sha256(
                f"{ref['manifest_sha256']}:context:{canonical_rel}:{start}:{end}:{quote}".encode()
            ).hexdigest()[:24]
            if evidence_id not in seen and span not in seen_spans:
                seen.add(evidence_id)
                seen_spans.add(span)
                extra.append({"evidence_id": evidence_id, "kind": "sealed_dependency_context",
                              "symbol": symbol, "path": canonical_rel,
                              "start_line": start, "end_line": end, "quote": quote,
                              "file_sha256": file_digest,
                              "manifest_sha256": ref["manifest_sha256"]})
            if depth < 2:
                queue.extend((called, depth + 1) for called in _SOURCE_CALL_RE.findall(quote)
                             if called in spans and called not in visited)
    return extra


def _sealed_evidence(case) -> list[dict]:
    """Return exact citations only when manifest, repo, whole file, and quote all match finding-time bytes."""
    review = case["internal_review"]
    if _digest(review) != case["internal_review_digest"]:
        return []
    evidence, seen, seen_spans = [], set(), set()
    state = case.get("state") if isinstance(case.get("state"), dict) else {}
    story_contract = state.get("authoritative_story_contract")
    if isinstance(story_contract, dict) and story_contract:
        story_id = str(story_contract.get("id") or story_contract.get("title") or "")
        finding_story = str(review.get("story") or (review.get("finding") or {}).get("story") or "")
        if story_id and story_id == finding_story:
            state_digest = str(case.get("state_digest") or _digest(state))
            evidence_id = "qae-" + hashlib.sha256(
                f"story-contract:{state_digest}:{_canonical(story_contract)}".encode()).hexdigest()[:24]
            evidence.append({
                "evidence_id": evidence_id, "kind": "authoritative_story_contract",
                "story": story_id, "contract": story_contract, "state_digest": state_digest,
                "authority": str(state.get("contract_authority") or "")[:1000],
            })
            seen.add(evidence_id)
    for item in _sealed_fresh_browser_evidence(case, review, state):
        if len(evidence) >= MAX_EVIDENCE:
            break
        if item["evidence_id"] not in seen:
            evidence.append(item)
            seen.add(item["evidence_id"])
    ref = _provenance_ref(review) or {}
    try:
        manifest_path = Path(str(ref["manifest_path"]))
        if (not manifest_path.name.startswith("finding-repo-provenance-")
                or manifest_path.suffix != ".json" or not manifest_path.is_file()
                or manifest_path.stat().st_size > MAX_MANIFEST_BYTES):
            return evidence
        raw = manifest_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != str(ref["manifest_sha256"]):
            return evidence
        manifest = json.loads(raw)
        root = Path(case["repo"]).resolve(strict=True)
        if manifest.get("version") != 1 or Path(manifest.get("repo") or "").resolve() != root:
            return evidence
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            return evidence
    except Exception:
        return evidence
    verified_sources, seed_quotes = {}, {}
    citations = [citation for triage_review in ((review.get("triage") or {}).get("reviews") or [])
                 for citation in (triage_review.get("citations") or [])]
    for citation in citations[:MAX_EVIDENCE]:
        if len(evidence) >= MAX_EVIDENCE:
            break
        try:
            rel = str(citation.get("path") or "").strip().replace("\\", "/")
            if not rel or Path(rel).is_absolute():
                continue
            path = (root / rel).resolve(strict=True)
            path.relative_to(root)
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            content = path.read_bytes()
            file_digest = hashlib.sha256(content).hexdigest()
            canonical_rel = str(path.relative_to(root))
            if files.get(canonical_rel) != file_digest:
                continue
            start, end = int(citation["start_line"]), int(citation["end_line"])
            lines = content.decode("utf-8", errors="replace").splitlines()
            if start < 1 or end < start or end - start >= 40 or end > len(lines):
                continue
            quote = "\n".join(lines[start - 1:end])
            if quote != str(citation.get("quote") or "").replace("\r\n", "\n"):
                continue
            verified_sources[canonical_rel] = (lines, file_digest)
            seed_quotes.setdefault(canonical_rel, []).append(quote)
            evidence_id = "qae-" + hashlib.sha256(
                f"{ref['manifest_sha256']}:{canonical_rel}:{start}:{end}:{quote}".encode()).hexdigest()[:24]
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            seen_spans.add((canonical_rel, start, end))
            evidence.append({"evidence_id": evidence_id, "path": canonical_rel,
                             "start_line": start, "end_line": end, "quote": quote,
                             "file_sha256": file_digest,
                             "manifest_sha256": ref["manifest_sha256"]})
        except Exception:
            continue
    evidence.extend(_sealed_dependency_context(
        ref, verified_sources, seed_quotes, seen, seen_spans, MAX_EVIDENCE))
    return evidence[:MAX_EVIDENCE]


def _normalize_review(raw, evidence_ids) -> dict:
    raw = dict(raw or {}) if isinstance(raw, dict) else {}
    verdict = str(raw.get("verdict") or "uncertain").strip().lower()
    aliases = {"defect": "confirmed_defect", "false_positive": "verified_false_positive"}
    verdict = aliases.get(verdict, verdict)
    if verdict not in TERMINAL and verdict != "uncertain":
        verdict = "uncertain"
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    cited = []
    allowed = set(evidence_ids)
    for item in raw.get("evidence_ids") or []:
        item = str(item)
        if item in allowed and item not in cited:
            cited.append(item)
    authority = raw.get("external_authority") if isinstance(raw.get("external_authority"), dict) else {}
    authority_type = str(authority.get("type") or "").strip().lower()
    authority_name = str(authority.get("name") or "").strip()
    if (authority_type not in EXTERNAL_AUTHORITY_TYPES or not authority_name
            or authority_name.lower() in {"ceo", "founder", "human", "user"}):
        authority = {}
        if verdict == "needs_named_external_authority":
            verdict = "uncertain"
    else:
        authority = {"type": authority_type, "name": authority_name[:200],
                     "question": str(authority.get("question") or "")[:1000]}
    if verdict in {"confirmed_defect", "verified_false_positive"}:
        if confidence < MIN_CONFIDENCE or not cited:
            verdict = "uncertain"
    return {"verdict": verdict, "confidence": confidence,
            "rationale": str(raw.get("rationale") or raw.get("reason") or "")[:3000],
            "evidence_ids": cited, "external_authority": authority}


def _model_review(role, payload) -> dict:
    """Default reviewer is the read-only reviewer role and receives a sealed evidence bundle, not live state."""
    import factory
    prompt = f"""You are the {role} adjudicating a disputed QA finding. This is READ-ONLY evidence review.
Do not edit, run the product, install, restart, signal, browse, or ask the CEO. Use only the sealed evidence
bundle below. A finding's expected/detail text is a disputed allegation, not product authority. When an
authoritative_story_contract evidence item exists, use it to decide required scope: a finding cannot invent a
post-action invariant or sibling requirement absent from that story. The story contract alone can prove that
an observation is out of scope, but cannot prove that in-scope runtime behavior works; confirming a defect
still requires sealed behavioral/source evidence. Apply this causal interpretation: a broad phrase such as
"all panels become populated" requires each panel to
render and truthfully reflect the story actions, not every independent entity counter to become nonzero. A
zero approval/ticket/notification count is valid when no story step created or submitted that entity; only a
specific causal creation requirement makes its absence a defect. Return confirmed_defect or verified_false_positive only
with confidence >= {MIN_CONFIDENCE} and one or more listed evidence_ids. If evidence remains insufficient,
return uncertain. Only the senior QA
director may return needs_named_external_authority, and only for a concrete named authority with type one of:
{sorted(EXTERNAL_AUTHORITY_TYPES)}. The authority name cannot be generic human/user/CEO/founder.
Reply ONLY JSON with verdict, confidence, rationale, evidence_ids, external_authority.
INPUT:\n{_canonical(payload)[:24000]}"""
    result = factory.agent("reviewer", str(SCRIPTS.parent), prompt, spawner="qa-management",
                           timeout=120, retries=0)
    text = (result.get("out_full") or result.get("out") or "") if isinstance(result, dict) else str(result or "")
    start, end = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    except Exception:
        return {}


def _load_prior_reviews(tenant_id, case_id, state_digest) -> list[dict]:
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT tier,actor_role,review FROM qa_evidence_dispute_reviews
                       WHERE case_id=%s AND tenant_id=%s AND state_digest=%s ORDER BY tier""",
                    (case_id, str(tenant_id), state_digest))
        return [{"tier": row[0], "role": row[1], **row[2]} for row in cur.fetchall()]


def _assert_lease(cur, tenant_id, case_id, lease_token, state_digest):
    cur.execute("""SELECT status,lease_token,state_digest,lease_until>now()
                   FROM qa_evidence_disputes WHERE case_id=%s AND tenant_id=%s FOR UPDATE""",
                (case_id, str(tenant_id)))
    row = cur.fetchone()
    if (not row or row[0] != "leased" or row[1] != lease_token
            or row[2] != state_digest or not row[3]):
        raise LeaseLost(f"lease lost for QA evidence dispute {case_id}")


def _interrupt_review(tenant_id, case_id, lease_token, state_digest) -> dict:
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        _assert_lease(cur, tenant_id, case_id, lease_token, state_digest)
        cur.execute("""UPDATE qa_evidence_disputes SET status='pending',next_review_at=now(),
                       lease_owner=NULL,lease_token=NULL,lease_until=NULL,updated_at=now()
                       WHERE case_id=%s AND tenant_id=%s""", (case_id, str(tenant_id)))
    return {"case_id": case_id, "status": "pending", "disposition": None,
            "interrupted": True, "checkpoint_required": True}


def adjudicate(tenant_id, case_id, lease_token, *, review_fn=None, retry_after_s=300,
               after_review=None, should_stop=None) -> dict:
    """Run/replay the three-tier read-only review under a lease; only the senior may terminate the case."""
    _ensure()
    case = _case(tenant_id, case_id)
    if case["status"] in ("resolved", "external_authority"):
        outcome = case["outcome"] or {}
        terminal_state = {"trigger": "terminal_adjudication_replay", "status": case["status"],
                          "case_id": case_id, "review_id": case["review_id"],
                          "outcome": outcome}
        wake = _wake_runtime(tenant_id, case_id, terminal_state)
        return {"case_id": case_id, "status": case["status"],
                **outcome, "duplicate": True, **wake}
    if case["status"] != "leased" or case["lease_token"] != lease_token:
        raise LeaseLost(f"lease lost for QA evidence dispute {case_id}")
    evidence = _sealed_evidence(case)
    prompt_evidence = _prompt_evidence(evidence)
    evidence_ids = [item["evidence_id"] for item in prompt_evidence]
    evidence_digest = _digest(evidence)
    fn = review_fn or _model_review
    prior = _load_prior_reviews(tenant_id, case_id, case["state_digest"])
    prior_by_tier = {item["tier"]: item for item in prior}
    start = max(0, min(2, int(case["current_tier"] or 0)))
    senior = None
    for tier in range(start, len(ROLES)):
        if callable(should_stop) and should_stop():
            return _interrupt_review(tenant_id, case_id, lease_token, case["state_digest"])
        role = ROLES[tier]
        if tier in prior_by_tier:
            review = {key: value for key, value in prior_by_tier[tier].items()
                      if key not in {"tier", "role"}}
        else:
            payload = {"finding": _review_finding_summary(case["internal_review"]),
                       "dispute_reason": _behavior_text(case["internal_review"].get("reason"), 3000),
                       "state": _review_state_summary(case["state"]),
                       "sealed_evidence": prompt_evidence,
                       "prior_management_reviews": prior, "tier": tier, "role": role}
            try:
                raw_review = fn(role, payload)
            except Exception as exc:
                raw_review = {"verdict": "uncertain", "confidence": 0.0,
                              "rationale": f"read-only reviewer unavailable: {exc}"}
            if callable(should_stop) and should_stop():
                return _interrupt_review(tenant_id, case_id, lease_token, case["state_digest"])
            review = _normalize_review(raw_review, evidence_ids)
            with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
                _assert_lease(cur, tenant_id, case_id, lease_token, case["state_digest"])
                cur.execute("""INSERT INTO qa_evidence_dispute_reviews
                    (case_id,tenant_id,state_digest,tier,actor_role,review,evidence_digest)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(case_id,state_digest,tier) DO NOTHING""",
                    (case_id, str(tenant_id), case["state_digest"], tier, role,
                     json.dumps(review), evidence_digest))
                cur.execute("""UPDATE qa_evidence_disputes SET current_tier=%s,updated_at=now()
                               WHERE case_id=%s AND tenant_id=%s""",
                            (min(2, tier + 1), case_id, str(tenant_id)))
            if after_review:
                after_review(tier, review)
            prior.append({"tier": tier, "role": role, **review})
        if tier == 2:
            senior = review

    senior = senior or {"verdict": "uncertain", "confidence": 0.0, "rationale": "senior review missing",
                        "evidence_ids": [], "external_authority": {}}
    verdict = senior["verdict"]
    terminal = verdict in TERMINAL
    if verdict == "needs_named_external_authority" and not senior.get("external_authority"):
        terminal = False
    if terminal:
        outcome = {"disposition": verdict, "decided_by": ROLES[-1],
                   "confidence": senior["confidence"], "rationale": senior["rationale"],
                   "evidence_ids": senior["evidence_ids"],
                   "external_authority": senior.get("external_authority") or None,
                   "state_generation": case["state_generation"]}
        status = "external_authority" if verdict == "needs_named_external_authority" else "resolved"
        with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
            _assert_lease(cur, tenant_id, case_id, lease_token, case["state_digest"])
            cur.execute("""UPDATE qa_evidence_disputes SET status=%s,outcome=%s,resolved_at=now(),
                           lease_owner=NULL,lease_token=NULL,lease_until=NULL,updated_at=now()
                           WHERE case_id=%s AND tenant_id=%s""",
                        (status, json.dumps(outcome), case_id, str(tenant_id)))
        # A terminal evidence decision is itself the semantic state change the parked QA owner was awaiting.
        # Previously only nonterminal manager replies called the wake path, so a successful senior decision
        # could leave TESTQA permanently parked even though its dispute row was already resolved.
        terminal_state = {"trigger": "terminal_adjudication", "status": status,
                          "case_id": case_id, "review_id": case["review_id"],
                          "outcome": outcome}
        wake = _wake_runtime(tenant_id, case_id, terminal_state)
        return {"case_id": case_id, "status": status, **outcome, **wake}

    # Evidence uncertainty is internal work. Requeue senior management rather than manufacturing a CEO gate
    # or returning a terminal "unknown" outcome. A state-change notification can make it runnable sooner.
    retry_after_s = max(30, min(3600, int(retry_after_s)))
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        _assert_lease(cur, tenant_id, case_id, lease_token, case["state_digest"])
        cur.execute("""UPDATE qa_evidence_disputes SET status='manager_review',current_tier=2,
                       next_review_at=now()+(%s*interval '1 second'),lease_owner=NULL,lease_token=NULL,
                       lease_until=NULL,updated_at=now() WHERE case_id=%s AND tenant_id=%s""",
                    (retry_after_s, case_id, str(tenant_id)))
    return {"case_id": case_id, "status": "manager_review", "disposition": None,
            "reason": "senior evidence uncertainty remains queued inside QA management",
            "next_review_s": retry_after_s, "state_generation": case["state_generation"]}


def get(tenant_id, case_id) -> dict:
    """Read-only integration view; callers act only on a non-null terminal disposition."""
    case = _case(tenant_id, case_id)
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute("""SELECT run_id,coordinator_actor_id,thread_id,authority_decision_id
                       FROM qa_evidence_disputes WHERE case_id=%s AND tenant_id=%s""",
                    (case_id, str(tenant_id)))
        route = cur.fetchone() or (None, None, None, None)
    finding = dict((case.get("internal_review") or {}).get("finding") or {})
    return {"case_id": case_id, "review_id": case["review_id"], "status": case["status"],
            "state_generation": case["state_generation"], "outcome": case["outcome"],
            "finding_id": finding.get("finding_id"),
            "story": (case.get("internal_review") or {}).get("story") or finding.get("story"),
            "run_id": route[0], "coordinator_actor_id": route[1], "thread_id": route[2],
            "authority_decision_id": route[3]}


def attention_evidence(limit=100) -> list[dict]:
    """Bounded unresolved cases for the independent duty-manager sweep."""
    _ensure()
    with connection() as c, c.cursor() as cur:
        cur.execute("""SELECT case_id,tenant_id,review_id,thread_id,work_ref,status,state_generation,
                              attempts,updated_at,EXTRACT(EPOCH FROM now()-updated_at)::INT
                       FROM qa_evidence_disputes
                       WHERE status IN ('manager_review','external_authority')
                         AND NOT (status='manager_review'
                                  AND state->>'trigger'='evidence_collection_started'
                                  AND updated_at > now()-(%s*interval '1 second'))
                       ORDER BY updated_at LIMIT %s""",
                    (EVIDENCE_COLLECTION_REVIEW_S, max(1, min(500, int(limit)))))
        rows = cur.fetchall()
    keys = ("case_id","tenant_id","review_id","thread_id","work_ref","status",
            "state_generation","attempts","updated_at","age_s")
    return [dict(zip(keys, row)) for row in rows]
