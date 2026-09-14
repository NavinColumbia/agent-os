#!/usr/bin/env python3
"""Durable worker→manager→senior-manager decisions for reversible work.

This is the decision layer between a controller transition and ``authority``.
Uncertainty moves up the internal org; it never manufactures a CEO gate.  A
human request is possible only after a manager validates a typed authority
boundary.  Stable correlation IDs make every review and final outcome replay
safe across process death.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import audit  # noqa: E402
import authority  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402


ROLES = ("product-owner", "product-manager", "senior-product-director")
LOCAL_ACTIONS = frozenset({"select", "proceed", "revise", "retry", "reassign",
                           "continue", "experiment"})
BOUNDARIES = frozenset({"none", "credential", "legal", "spend", "irreversible", "business"})
_AUTHORITY_FACT_KEYS = (
    "amount_usd", "campaign_spent_usd", "budget_usd", "requested_cap_usd", "product",
)
_ensure_lock = threading.Lock()
_ensured = False


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        with connection() as c, c.cursor() as cur:
            cur.execute("""SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema='public' AND table_name='agentic_decisions'
                   AND column_name='applied_at')""")
            acknowledgement_already_installed = bool(cur.fetchone()[0])
            cur.execute("""CREATE TABLE IF NOT EXISTS agentic_decisions (
                id BIGSERIAL PRIMARY KEY,
                correlation_id TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                thread_id BIGINT,
                work_ref TEXT NOT NULL,
                decision_type TEXT NOT NULL,
                input JSONB NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'reviewing',
                current_tier INT NOT NULL DEFAULT 0,
                outcome JSONB,
                authority_decision_id BIGINT,
                applied_at TIMESTAMPTZ,
                apply_attempts INT NOT NULL DEFAULT 0,
                apply_error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                resolved_at TIMESTAMPTZ)""")
            cur.execute("ALTER TABLE agentic_decisions ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE agentic_decisions ADD COLUMN IF NOT EXISTS apply_attempts INT NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE agentic_decisions ADD COLUMN IF NOT EXISTS apply_error TEXT")
            if not acknowledgement_already_installed:
                cur.execute("""UPDATE agentic_decisions
                                  SET applied_at=COALESCE(applied_at,resolved_at,updated_at,now())
                                WHERE status='resolved' AND applied_at IS NULL""")
            cur.execute("""CREATE INDEX IF NOT EXISTS agentic_decisions_attention_idx
                ON agentic_decisions(status,updated_at) WHERE status IN ('reviewing','human_wait')""")
            cur.execute("""CREATE INDEX IF NOT EXISTS agentic_decisions_tenant_idx
                ON agentic_decisions(tenant_id,id DESC)""")
            cur.execute("""CREATE INDEX IF NOT EXISTS agentic_decisions_pending_apply_idx
                ON agentic_decisions(tenant_id,id)
                WHERE status='resolved' AND applied_at IS NULL""")
            cur.execute("""CREATE TABLE IF NOT EXISTS agentic_decision_reviews (
                id BIGSERIAL PRIMARY KEY,
                decision_id BIGINT NOT NULL,
                tenant_id TEXT NOT NULL,
                tier INT NOT NULL,
                actor_role TEXT NOT NULL,
                decision JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE(decision_id,tier))""")
            cur.execute("""CREATE INDEX IF NOT EXISTS agentic_decision_reviews_tenant_idx
                ON agentic_decision_reviews(tenant_id,decision_id,tier)""")
        _ensured = True


def ensure():
    _ensure()
    return True


def stable_correlation(thread_id, decision_type, revision):
    raw = f"{int(thread_id)}:{decision_type}:{revision}"
    return "dc-" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def _normalize(raw):
    if not isinstance(raw, dict):
        raw = {}
    action = str(raw.get("action") or "continue").strip().lower()
    if action not in LOCAL_ACTIONS and action != "request_human":
        action = "continue"
    boundary = str(raw.get("boundary") or "none").strip().lower()
    if boundary not in BOUNDARIES:
        boundary = "none"
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
    except Exception:
        confidence = 0.0
    return {**raw, "action": action, "boundary": boundary, "confidence": confidence,
            "rationale": str(raw.get("rationale") or "")[:2000]}


def _authority_proposal(payload, review, *, tier, boundary, exhausted=False):
    """Keep measured authority facts immutable while letting managers supply judgment."""
    payload, review = dict(payload or {}), dict(review or {})
    proposal = {
        "question": payload.get("question") or review.get("question") or review.get("rationale") or "",
        "reason": review.get("rationale") or "",
        "confidence": review.get("confidence", 0),
        "risk": review.get("risk", "medium"),
        "reversible": bool(review.get("reversible", False)),
        "management_exhausted": bool(exhausted),
        "requires_ceo_business_judgment": boundary == "business",
    }
    for key in _AUTHORITY_FACT_KEYS:
        if key in payload:
            proposal[key] = payload[key]
        elif key in review:
            proposal[key] = review[key]
    proposal.setdefault("amount_usd", 0)
    proposal.setdefault("campaign_spent_usd", 0)
    return proposal


def _model_decide(role, decision_type, state):
    import factory
    prompt = f"""You are the {role} in an autonomous product company. Decide {decision_type}.
Choose and act on reversible internal/product decisions yourself. If uncertain, return a low confidence so
your manager reviews it. Request a human only for a concrete missing credential, legal judgment, spend beyond
standing authority, irreversible external action, or business judgment genuinely reserved for the CEO.
Reply ONLY JSON with action, selection, confidence, rationale, boundary, question, risk, reversible,
amount_usd. action is select|proceed|revise|retry|reassign|continue|experiment|request_human;
boundary is none|credential|legal|spend|irreversible|business.
STATE:\n{json.dumps(state, default=str)[:12000]}"""
    out = factory.agent(role, str(SCRIPTS.parent), prompt, spawner="controller", light=True)
    text = (out.get("out_full") or out.get("out") or "") if isinstance(out, dict) else str(out or "")
    start, end = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    except Exception:
        return {}


def _row(decision_id, tenant_id):
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT id,correlation_id,status,current_tier,outcome,authority_decision_id
                       FROM agentic_decisions WHERE id=%s AND tenant_id=%s""",
                    (decision_id, tenant_id))
        row = cur.fetchone()
    if not row:
        raise KeyError(f"agentic decision {decision_id} not found")
    return {"id": row[0], "correlation_id": row[1], "status": row[2], "current_tier": row[3],
            "outcome": row[4], "authority_decision_id": row[5]}


def _finish(decision_id, tenant_id, status, outcome, authority_decision_id=None):
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""UPDATE agentic_decisions SET status=%s,outcome=%s,authority_decision_id=%s,
                       updated_at=now(),resolved_at=CASE WHEN %s='resolved' THEN now() ELSE NULL END
                       WHERE id=%s AND tenant_id=%s""",
                    (status, json.dumps(outcome), authority_decision_id, status, decision_id, tenant_id))
    return {"id": decision_id, "status": status, **outcome,
            "authority_decision_id": authority_decision_id}


def decide(tenant_id, thread_id, work_ref, decision_type, state, *, correlation_id,
           authority_kind="business", default=None, decide_fn=None, request_human=None,
           after_review=None):
    """Resolve one decision through the durable internal management chain.

    ``decide_fn(role, decision_type, state)`` is injectable for deterministic
    tests. ``after_review`` is a crash-injection hook called after a tier review
    commits; replay reuses that review rather than invoking the actor twice.
    """
    _ensure()
    corr = str(correlation_id or "").strip()
    if not corr:
        raise ValueError("correlation_id is required")
    payload = dict(state or {})
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"agentic-decision:{corr}",))
        cur.execute("""INSERT INTO agentic_decisions
            (correlation_id,tenant_id,thread_id,work_ref,decision_type,input)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(correlation_id) DO NOTHING RETURNING id""",
            (corr, tenant_id, thread_id, str(work_ref), decision_type, json.dumps(payload)))
        inserted = cur.fetchone()
        if inserted:
            decision_id = inserted[0]
        else:
            cur.execute("""SELECT id,tenant_id FROM agentic_decisions WHERE correlation_id=%s""", (corr,))
            existing = cur.fetchone()
            if not existing or existing[1] != tenant_id:
                raise PermissionError("correlation id belongs to another tenant")
            decision_id = existing[0]

    current = _row(decision_id, tenant_id)
    if current["status"] in ("resolved", "human_wait"):
        return {"id": decision_id, "status": current["status"],
                **(current["outcome"] or {}), "authority_decision_id": current["authority_decision_id"],
                "duplicate": True}

    fn = decide_fn or _model_decide
    threshold = authority.active_envelope(tenant_id)["policy"]["manager_review_below_confidence"]
    start_tier = max(0, min(len(ROLES) - 1, int(current["current_tier"] or 0)))
    for tier in range(start_tier, len(ROLES)):
        role = ROLES[tier]
        with tenant_connection(tenant_id) as c, c.cursor() as cur:
            cur.execute("""SELECT decision FROM agentic_decision_reviews
                           WHERE decision_id=%s AND tenant_id=%s AND tier=%s""",
                        (decision_id, tenant_id, tier))
            found = cur.fetchone()
        if found:
            review = _normalize(found[0])
        else:
            review = _normalize(fn(role, decision_type, {**payload, "tier": tier, "role": role}))
            with tenant_connection(tenant_id) as c, c.cursor() as cur:
                cur.execute("""INSERT INTO agentic_decision_reviews
                    (decision_id,tenant_id,tier,actor_role,decision) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT(decision_id,tier) DO NOTHING""",
                    (decision_id, tenant_id, tier, role, json.dumps(review)))
                cur.execute("""UPDATE agentic_decisions SET current_tier=%s,updated_at=now()
                               WHERE id=%s AND tenant_id=%s""", (tier, decision_id, tenant_id))
            if after_review:
                after_review(tier, review)

        boundary = review["boundary"]
        # A worker flags a possible boundary; management validates it. Unknown
        # or untyped requests remain internal at every tier.
        if (review["action"] == "request_human" and boundary != "none" and tier >= 1
                and (boundary != "business" or tier == len(ROLES) - 1)):
            kind = boundary
            proposal = _authority_proposal(
                payload, review, tier=tier, boundary=boundary,
                exhausted=tier == len(ROLES) - 1)
            routed = authority.open_decision(
                tenant_id, work_ref, kind, proposal,
                correlation_id=f"agentic:{corr}:{boundary}", thread_id=thread_id,
                owner_role=role, request_human=request_human)
            if routed["disposition"] == "human_required":
                outcome = {"action": "request_human", "selection": review.get("selection"),
                           "confidence": review["confidence"], "rationale": routed["reason"],
                           "boundary": boundary, "decided_by": role, "tier": tier}
                return _finish(decision_id, tenant_id, "human_wait", outcome, routed["id"])
            if routed["disposition"] == "delegated":
                outcome = {"action": "continue", "selection": review.get("selection"),
                           "confidence": review["confidence"], "rationale": routed["reason"],
                           "boundary": "none", "decided_by": role, "tier": tier}
                return _finish(decision_id, tenant_id, "resolved", outcome, routed["id"])

        proposal = _authority_proposal(payload, review, tier=tier, boundary="none")
        # Reversible internal work defaults to reversible when the reviewer omits the field. Human-boundary
        # proposals above deliberately default false; conflating the two would escalate every ordinary choice.
        proposal["reversible"] = bool(review.get("reversible", True))
        verdict = authority.authorize(tenant_id, authority_kind, proposal)
        if (review["action"] in LOCAL_ACTIONS and review["confidence"] >= threshold
                and verdict["disposition"] == "delegated"):
            outcome = {"action": review["action"], "selection": review.get("selection"),
                       "confidence": review["confidence"], "rationale": review["rationale"],
                       "boundary": "none", "decided_by": role, "tier": tier}
            result = _finish(decision_id, tenant_id, "resolved", outcome)
            audit.append(actor=role, action="AgenticDecision", resource=str(work_ref),
                         decision=review["action"], payload={"type": decision_type, "tier": tier},
                         tenant_id=tenant_id)
            return result

        with tenant_connection(tenant_id) as c, c.cursor() as cur:
            cur.execute("""UPDATE agentic_decisions SET current_tier=%s,updated_at=now()
                           WHERE id=%s AND tenant_id=%s""",
                        (min(tier + 1, len(ROLES) - 1), decision_id, tenant_id))

    # All three people were uncertain. The senior manager chooses the explicitly
    # supplied bounded/reversible experiment instead of paging the CEO for doubt.
    fallback = _normalize(default or {"action": "experiment", "confidence": 1.0,
                                      "rationale": "senior manager chose the bounded reversible default"})
    fallback["confidence"] = 1.0  # confidence that the fallback is reversible, not that it will win
    if fallback["action"] == "request_human" and fallback["boundary"] != "none":
        boundary = fallback["boundary"]
        routed = authority.open_decision(
            tenant_id, work_ref, boundary,
            _authority_proposal(payload, fallback, tier=len(ROLES) - 1,
                                boundary=boundary, exhausted=True),
            correlation_id=f"agentic:{corr}:{boundary}", thread_id=thread_id,
            owner_role=ROLES[-1], request_human=request_human)
        if routed["disposition"] == "human_required":
            outcome = {"action": "request_human", "selection": fallback.get("selection"),
                       "confidence": 1.0, "rationale": routed["reason"], "boundary": boundary,
                       "decided_by": ROLES[-1], "tier": len(ROLES) - 1, "fallback": True}
            return _finish(decision_id, tenant_id, "human_wait", outcome, routed["id"])
        fallback["action"] = "continue"
    fallback["boundary"] = "none"
    outcome = {"action": fallback["action"], "selection": fallback.get("selection"),
               "confidence": 1.0, "rationale": fallback["rationale"], "boundary": "none",
               "decided_by": ROLES[-1], "tier": len(ROLES) - 1, "fallback": True}
    return _finish(decision_id, tenant_id, "resolved", outcome)


def get(correlation_id, tenant_id):
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT id,status,current_tier,outcome,authority_decision_id
                       FROM agentic_decisions WHERE correlation_id=%s AND tenant_id=%s""",
                    (correlation_id, tenant_id))
        row = cur.fetchone()
    return None if not row else {"id": row[0], "status": row[1], "current_tier": row[2],
                                 "outcome": row[3], "authority_decision_id": row[4]}


def _explicit_human_answer(answer, state):
    """Apply an unambiguous human instruction without asking a model to overrule it.

    The authority boundary was already validated before the question was sent.
    A literal approval/decline is therefore an instruction, not another agentic
    judgment. For fixed spend requests, a stated amount must match one of the
    amounts in the durable proposal; a different amount remains parked for a
    fresh scoped decision instead of silently widening authority.
    """
    text = " ".join(str(answer or "").strip().lower().split())
    if not text:
        return None
    negative = (re.search(r"\b(?:decline|deny|reject|cancel)\b", text)
                or re.search(r"\b(?:do not|don't|not)\s+(?:approve|authorize|proceed)\b", text)
                or text in {"no", "nope"})
    if negative:
        return {"action": "cancel", "rationale": "human explicitly declined the authority request"}
    revise = re.search(r"\b(?:revise|change|different|lower|reduce|instead)\b", text)
    positive = re.search(
        r"\b(?:approve|approved|authorize|authorized|yes|proceed|go ahead|connected|provided|done)\b",
        text)
    if revise and not positive:
        return {"action": "revise", "rationale": "human explicitly requested a revision"}
    if not positive:
        return None

    requested = state.get("requested_cap_usd") if isinstance(state, dict) else None
    if requested is not None:
        allowed = []
        for value in (requested, state.get("amount_usd"),
                      (float(requested) - float(state.get("campaign_spent_usd")))
                      if state.get("campaign_spent_usd") is not None else None):
            try:
                allowed.append(float(value))
            except (TypeError, ValueError):
                pass
        stated = [float(x) for x in re.findall(r"(?:\$\s*|usd\s*)(\d+(?:\.\d+)?)", text,
                                               flags=re.IGNORECASE)]
        if stated and any(not any(abs(amount - expected) <= 0.02 for expected in allowed)
                          for amount in stated):
            return {"action": "revise",
                    "rationale": "human stated an amount outside the existing fixed-cap request"}
    return {"action": "proceed", "rationale": "human explicitly authorized the existing request"}


def _interpret_human(question, answer, state):
    """Classify the human's authoritative answer without inventing a new decision."""
    explicit = _explicit_human_answer(answer, state or {})
    if explicit is not None:
        return explicit
    import factory
    prompt = f"""A human answered a previously validated authority request. Follow their answer.
Reply ONLY JSON with action (proceed|revise|cancel), selection, rationale. Do not overrule or re-ask them.
QUESTION: {question}\nANSWER: {answer}\nWORK: {json.dumps(state, default=str)[:6000]}"""
    out = factory.agent("product-manager", str(SCRIPTS.parent), prompt,
                        spawner="controller", light=True)
    text = (out.get("out_full") or out.get("out") or "") if isinstance(out, dict) else str(out or "")
    start, end = text.find("{"), text.rfind("}")
    try:
        parsed = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    except Exception:
        parsed = {}
    action = str(parsed.get("action") or "revise").lower()
    if action not in ("proceed", "revise", "cancel"):
        action = "revise"
    return {**parsed, "action": action, "rationale": str(parsed.get("rationale") or "human answered")[:2000]}


def reconcile_human_answers(answer_fn=None, limit=1):
    """Resume agentic decisions whose correlated human request was answered.

    The authority ledger owns acknowledgement; this layer durably interprets
    the answer once (review tier 3) and returns controller transitions to wake.
    """
    _ensure()
    limit = max(1, min(20, int(limit or 1)))
    authority.reconcile_human_answers(limit=max(1, limit * 2))
    with connection() as c, c.cursor() as cur:
        cur.execute("""SELECT d.id,d.tenant_id,d.thread_id,d.decision_type,d.input,d.outcome,
                              r.question,r.answer
                       FROM agentic_decisions d
                       JOIN authority_decisions a ON a.id=d.authority_decision_id
                       LEFT JOIN agent_requests r ON r.id=a.agent_request_id
                       WHERE d.status IN ('human_wait','resolved') AND d.applied_at IS NULL
                         AND a.status='resolved'
                         AND r.status='answered' AND r.answer IS NOT NULL
                       ORDER BY d.id LIMIT %s""", (limit,))
        rows = cur.fetchall()
    resumed = []
    fn = answer_fn or _interpret_human
    for did, tid, thread_id, decision_type, state, old_outcome, question, answer in rows:
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("""SELECT decision FROM agentic_decision_reviews
                           WHERE decision_id=%s AND tenant_id=%s AND tier=3""", (did, tid))
            found = cur.fetchone()
        interpreted = found[0] if found else fn(question, answer, state or {})
        action = str((interpreted or {}).get("action") or "revise").lower()
        if action not in ("proceed", "revise", "cancel"):
            action = "revise"
        interpreted = {**(interpreted or {}), "action": action,
                       "human_answer": str(answer), "rationale": str(
                           (interpreted or {}).get("rationale") or "human answered")[:2000]}
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO agentic_decision_reviews
                (decision_id,tenant_id,tier,actor_role,decision) VALUES (%s,%s,3,'human-answer',%s)
                ON CONFLICT(decision_id,tier) DO NOTHING""", (did, tid, json.dumps(interpreted)))
            outcome = {**(old_outcome or {}), **interpreted, "answered": True,
                       "decided_by": "human", "boundary": "none"}
            cur.execute("""UPDATE agentic_decisions SET status='resolved',outcome=%s,
                           updated_at=now(),resolved_at=now()
                           WHERE id=%s AND tenant_id=%s AND status='human_wait'""",
                        (json.dumps(outcome), did, tid))
        # Return the answer even when this is a replay of an already-resolved
        # row.  Only mark_applied(), called after the controller side effect,
        # makes it disappear from this queue.
        resumed.append({"id": did, "tenant_id": tid, "thread_id": thread_id,
                        "decision_type": decision_type, "state": state or {}, "outcome": outcome})
    return resumed


def needs_application(decision_id, tenant_id):
    """Recheck after acquiring the per-thread drive lock to fence concurrent sweepers."""
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""SELECT 1 FROM agentic_decisions
                       WHERE id=%s AND tenant_id=%s AND status='resolved' AND applied_at IS NULL""",
                    (int(decision_id), tenant_id))
        return cur.fetchone() is not None


def mark_applied(decision_id, tenant_id):
    """Durably acknowledge that the controller applied the answered decision."""
    _ensure()
    with tenant_connection(tenant_id) as c, c.cursor() as cur:
        cur.execute("""UPDATE agentic_decisions
                          SET applied_at=COALESCE(applied_at,now()), apply_attempts=apply_attempts+1,
                              apply_error=NULL, updated_at=now()
                        WHERE id=%s AND tenant_id=%s AND status='resolved'
                          AND applied_at IS NULL
                        RETURNING applied_at""", (int(decision_id), tenant_id))
        return cur.fetchone() is not None


__all__ = ["ensure", "stable_correlation", "decide", "get", "reconcile_human_answers",
           "needs_application", "mark_applied", "ROLES"]
