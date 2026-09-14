#!/usr/bin/env python3
"""Adopt the durable organization APIs in live workstream lifecycles.

The work-contract, objective-portfolio, and assurance modules deliberately keep
their own invariants small.  This module is the integration seam used by live
controllers: it gives a workstream stable identities, records real lifecycle
movement, and turns an already-independent QA verdict into durable acceptance.

All public operations are idempotent.  Callers may retry after a crash without
creating a second commitment/review, and an integration outage never changes the
underlying execution decision (in particular it never restarts or cancels QA).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import assurance_learning  # noqa: E402
import objectiveportfolio  # noqa: E402
import workcontracts  # noqa: E402
from dbpool import tenant_connection  # noqa: E402


def identities(workstream_id, tenant_id=None):
    """Stable, opaque IDs shared by every retry/process handling a workstream."""
    # The backing primary keys are global even though reads are tenant-scoped.
    # Include tenant identity so two installations/tenants may both use "run:1".
    digest = hashlib.sha256(f"{tenant_id or ''}\0{workstream_id}".encode()).hexdigest()[:28]
    return {
        "contract_id": f"wc-live-{digest}",
        "portfolio_id": f"op-live-{digest}",
        "objective_id": f"so-live-{digest}",
        "review_id": f"ar-live-{digest}",
        "evidence_id": f"ae-live-{digest}",
    }


def _one(tenant_id, query, args):
    with tenant_connection(str(tenant_id)) as c, c.cursor() as cur:
        cur.execute(query, args)
        return cur.fetchone()


def ensure_workstream(tenant_id, workstream_id, objective, acceptance_contract, *,
                      accountable_owner, manager_owner, created_by="workstream-spine",
                      org_id=None, backup_owner=None, priority=3, risk="medium",
                      update_cadence_s=900, execution_scope=None):
    """Ensure one accepted operational commitment and one strategic objective.

    Deterministic primary keys make the operation retry-safe.  A partial prior
    attempt is repaired in dependency order rather than replaced.
    """
    ids = identities(workstream_id, tenant_id)
    tid = str(tenant_id)
    acceptance = dict(acceptance_contract or {})
    if not acceptance:
        raise ValueError("acceptance_contract is required")
    scope = str(execution_scope or
                ("test" if os.environ.get("AOS_SELFTEST", "").strip().lower()
                 in {"1", "true", "yes", "on"} else "production"))
    if scope not in {"production", "test"}:
        raise ValueError("execution_scope must be 'production' or 'test'")

    workcontracts.ensure()
    objectiveportfolio.ensure()
    contract = _one(tid, """SELECT contract_id,accountable_owner,manager_owner,status
                              FROM work_contracts WHERE tenant_id=%s AND contract_id=%s""",
                    (tid, ids["contract_id"]))
    if not contract:
        try:
            workcontracts.create(
                tid, objective, acceptance, accountable_owner, manager_owner,
                created_by=created_by, org_id=str(org_id) if org_id is not None else None,
                backup_owner=backup_owner, priority=priority, risk=risk,
                update_cadence_s=update_cadence_s, contract_id=ids["contract_id"],
                constraints={"execution_scope": scope})
        except Exception:
            # A concurrent retry may have won the deterministic insert.  Only
            # suppress the error when the intended record now exists.
            if not _one(tid, "SELECT 1 FROM work_contracts WHERE tenant_id=%s AND contract_id=%s",
                        (tid, ids["contract_id"])):
                raise

    portfolio = _one(tid, "SELECT 1 FROM objective_portfolios WHERE tenant_id=%s AND portfolio_id=%s",
                     (tid, ids["portfolio_id"]))
    if not portfolio:
        try:
            objectiveportfolio.create_portfolio(
                tid, f"Workstream {workstream_id}", str(objective),
                accountable_owner, manager_owner, created_by=created_by,
                org_id=str(org_id) if org_id is not None else None,
                portfolio_id=ids["portfolio_id"])
        except Exception:
            if not _one(tid, "SELECT 1 FROM objective_portfolios WHERE tenant_id=%s AND portfolio_id=%s",
                        (tid, ids["portfolio_id"])):
                raise

    obj = _one(tid, """SELECT state,version FROM strategic_objectives
                         WHERE tenant_id=%s AND objective_id=%s""",
               (tid, ids["objective_id"]))
    if not obj:
        try:
            objectiveportfolio.add_objective(
                tid, ids["portfolio_id"], ids["contract_id"],
                f"Deliver {objective}", str(objective), created_by=created_by,
                priority=priority, confidence=0.5, objective_id=ids["objective_id"])
        except Exception:
            if not _one(tid, "SELECT 1 FROM strategic_objectives WHERE tenant_id=%s AND objective_id=%s",
                        (tid, ids["objective_id"])):
                raise
        obj = _one(tid, """SELECT state,version FROM strategic_objectives
                             WHERE tenant_id=%s AND objective_id=%s""",
                   (tid, ids["objective_id"]))
    if obj and obj[0] == "proposed":
        try:
            objectiveportfolio.change_state(
                tid, ids["objective_id"], "active", "execution workstream opened",
                {"workstream_id": str(workstream_id), "contract_id": ids["contract_id"]},
                changed_by=created_by, expected_version=obj[1])
        except ValueError:
            # Optimistic retry raced another process; the desired state is enough.
            current = _one(tid, "SELECT state FROM strategic_objectives WHERE tenant_id=%s AND objective_id=%s",
                           (tid, ids["objective_id"]))
            if not current or current[0] != "active":
                raise
    return ids


def record_progress(tenant_id, workstream_id, kind, evidence, *, actor,
                    substantive=True):
    """Append grounded progress for a previously seeded live workstream."""
    ids = identities(workstream_id, tenant_id)
    if not _one(str(tenant_id),
                "SELECT 1 FROM work_contracts WHERE tenant_id=%s AND contract_id=%s",
                (str(tenant_id), ids["contract_id"])):
        return {**ids, "recorded": False, "reason": "workstream_not_seeded"}
    out = workcontracts.update(str(tenant_id), ids["contract_id"], actor, str(kind),
                               dict(evidence or {}), substantive=bool(substantive))
    return {**ids, **out, "recorded": True}


def record_assurance(tenant_id, workstream_id, qa_result, *,
                     executor_id="build-team", manager_id="controller",
                     reviewer_id="qa-gate-auditor", submitted_by="loopcontroller",
                     subject_id=None):
    """Persist an independent QA verdict and complete the contract when it passed.

    This consumes the existing gate result; it does not run QA, rotate a worker,
    or introduce a new approval.  The verdict is stable under duplicate callbacks.
    """
    tid = str(tenant_id)
    ids = identities(workstream_id, tenant_id)
    contract = _one(tid, """SELECT objective,acceptance_contract,accountable_owner,manager_owner
                              FROM work_contracts WHERE tenant_id=%s AND contract_id=%s""",
                    (tid, ids["contract_id"]))
    if not contract:
        return {**ids, "recorded": False, "reason": "workstream_not_seeded"}

    assurance_learning.ensure()
    review = _one(tid, "SELECT status FROM assurance_reviews WHERE tenant_id=%s AND review_id=%s",
                  (tid, ids["review_id"]))
    if not review:
        try:
            assurance_learning.open_review(
                tid, "workstream_delivery", str(subject_id or workstream_id),
                executor_id, manager_id, reviewer_id, contract[1], submitted_by=submitted_by,
                independence_basis={"cross_functional_mandate": True},
                work_contract_id=ids["contract_id"], review_id=ids["review_id"])
        except Exception:
            if not _one(tid, "SELECT 1 FROM assurance_reviews WHERE tenant_id=%s AND review_id=%s",
                        (tid, ids["review_id"])):
                raise

    facts = dict(qa_result or {})
    canonical = json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    if not _one(tid, "SELECT 1 FROM assurance_evidence WHERE tenant_id=%s AND evidence_id=%s",
                (tid, ids["evidence_id"])):
        try:
            assurance_learning.add_evidence(
                tid, ids["review_id"], "qa_verdict", f"workstream://{workstream_id}/qa",
                digest, {"qa_ok": facts.get("qa_ok"), "stories": facts.get("stories"),
                         "blocking_open": facts.get("blocking_open")},
                submitted_by=submitted_by, metadata={"verdict": facts.get("verdict")},
                evidence_id=ids["evidence_id"])
        except Exception:
            if not _one(tid, "SELECT 1 FROM assurance_evidence WHERE tenant_id=%s AND evidence_id=%s",
                        (tid, ids["evidence_id"])):
                raise

    review = _one(tid, "SELECT status FROM assurance_reviews WHERE tenant_id=%s AND review_id=%s",
                  (tid, ids["review_id"]))
    if review and review[0] in {"accepted", "rejected", "changes_requested"}:
        if review[0] == "accepted":
            _mark_objective_achieved(tid, ids, reviewer_id)
        return {**ids, "recorded": True, "verdict": review[0], "idempotent": True}

    def count(value):
        return len(value) if isinstance(value, (list, tuple, set, dict)) else int(value or 0)

    passed = (facts.get("qa_ok") is True and count(facts.get("blocking_open")) == 0
              and count(facts.get("stories")) > 0)
    criteria = {
        "grounded_qa_passed": facts.get("qa_ok") is True,
        "no_blocking_findings": count(facts.get("blocking_open")) == 0,
        "nonempty_test_coverage": count(facts.get("stories")) > 0,
    }
    verdict = "accepted" if passed else "changes_requested"
    assurance_learning.decide(
        tid, ids["review_id"], reviewer_id, verdict, [ids["evidence_id"]],
        "Independent QA evidence satisfies delivery acceptance." if passed
        else "Delivery evidence does not yet satisfy every acceptance criterion.",
        criteria)

    if passed:
        _mark_objective_achieved(tid, ids, reviewer_id)
    return {**ids, "recorded": True, "verdict": verdict, "criteria": criteria}


def _mark_objective_achieved(tenant_id, ids, reviewer_id):
    """Finish a possibly-partial accepted-review transaction on any retry."""
    obj = _one(tenant_id,
               "SELECT state,version FROM strategic_objectives WHERE tenant_id=%s AND objective_id=%s",
               (tenant_id, ids["objective_id"]))
    if not obj or obj[0] == "achieved":
        return
    if obj[0] not in {"active", "at_risk", "blocked"}:
        return
    try:
        objectiveportfolio.change_state(
            tenant_id, ids["objective_id"], "achieved", "independent assurance accepted delivery",
            {"review_id": ids["review_id"], "evidence_id": ids["evidence_id"]},
            changed_by=reviewer_id, expected_version=obj[1])
    except ValueError:
        current = _one(tenant_id,
                       "SELECT state FROM strategic_objectives WHERE tenant_id=%s AND objective_id=%s",
                       (tenant_id, ids["objective_id"]))
        if not current or current[0] != "achieved":
            raise
