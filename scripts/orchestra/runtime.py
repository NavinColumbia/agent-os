#!/usr/bin/env python3
"""runtime.py — THE durable execution engine of the agent-org (REBUILD-PLAN A1).

The arch-review verdict this fixes: "the actual AI org engine sits in a demo folder with zero
production callers" / "agents are stateless subprocess invocations with no identity, memory, or
tenure". The PROVEN in-memory semantics of the old bus.py/actor.py/supervisor.py reactor now run
on Postgres via store.py — actors are rows (identity, tenure, assignment, memory), events are
SKIP-LOCKED-claimable rows, and every decide-loop step is a DURABLE unit:

    claim pending events (FOR UPDATE SKIP LOCKED, lease-reclaimable)
      -> AI decision (factory.agent — Opus default, retries, Codex failover;
         the actor's role + memory + assignment are in the prompt)
      -> persist new state/events/result (store.update_actor / store.emit)
      -> heartbeat, then mark the claimed events processed

Process death between (or during) steps loses NOTHING: state is only advanced by persisted
writes, and an event claimed by a dead worker is lease-reclaimed, so a restarted
runtime.run_org() resumes every non-terminal actor exactly where its rows say it stopped.
Delivery is at-least-once (events are completed LAST), so a crash mid-step can re-run one step —
never skip one.

  run_org(run_id, tenant_id, workers=N)  — a THREAD POOL (like the factory fleet): each worker
      repeatedly picks ANY actor with pending events (SKIP LOCKED — N workers never collide on
      one event) and executes ONE step. Real parallelism across actors; interrupt semantics are
      preserved: a supervisor step triggered by a child's `blocked` runs while siblings work.

  Actor semantics (ported 1:1 from the in-memory reactor):
      worker      — decide-loop via one AI call per step: continue (self-emits `next` to keep its
                    own loop alive), emit blocked/finding/question/need_agent/need_context
                    mid-work, or finish. `blocked` PARKS the actor (status row); a `resolve` /
                    `context_update` event resumes it from its persisted memory.
      supervisor  — decomposes via AI -> hires children (recursive orgs: a child spec can itself
                    be a supervisor); INTERRUPT-DRIVEN on child events: resolve locally (unblock /
                    rebrief / broadcast a correction to all siblings / hand next / spawn a helper,
                    org_decider.should_expand gating expansion) or ESCALATE up; aggregates via AI
                    once every child is terminal, emitting `done` up the tree.
      controller  — the root, top escalation tier: plans the org (org_decider.plan_org), resolves
                    escalations (or consults the human hook), finishes the run on final aggregate.

  FACTORY GATES per actor step:
      killswitch.is_halted — checked before EVERY step's AI work (plus store refuses new
          events/hires while halted); a halted run stops cleanly and is resumable.
      governance spawn gate — factory's exact semantics on HIRING: a manifest role without
          can_spawn may NOT hire; its decomposition becomes a hire REQUEST (`need_agent`) that
          routes up to the controller (can_spawn: true), which performs the hire on its behalf —
          "only the controller spawns", per the hire_requests doctrine in 17-orchestrate.sql.
          A missing manifest fails OPEN with an audit note, exactly like factory.agent.
      budget — factory.agent enforces the USD + token caps itself; a budget `blocker` from it
          surfaces as a `blocked`/`escalate` event, never a crash.

    scripts/orchestra/runtime.py selftest    # OFFLINE (factory.agent stubbed), REAL local
                                             # Postgres, 2-worker pool, crash-resume proven
Run with the agent-os venv python. Library + selftest only — binds no server.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent          # scripts/
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import factory      # noqa: E402  — factory.agent is THE llm call (retries/failover/gates inside)
import killswitch   # noqa: E402  — runtime-level per-step halt gate
import governance   # noqa: E402  — spawn gate on hiring
import store        # noqa: E402  — the durable substrate (rows for actors/events/runs)
import org_decider  # noqa: E402  — plan_org / should_expand (the org-shape AI)
import resourcepressure  # noqa: E402  — pure host/DB admission policy
from qa import campaign_checkpoint  # noqa: E402  — full-manifest pagination + product revision fence
from dbpool import connection  # noqa: E402

try:                # audit is best-effort; never let logging brick the org
    import audit    # noqa: E402
except Exception:   # pragma: no cover
    audit = None

TERMINAL = {"done", "dead"}          # actor statuses that end its decide-loop
MAX_ACTOR_STEPS = 64                 # runaway guard per actor (matches the old Actor default)
_MAX_RETEST = int(os.environ.get("AOS_QA_MAX_RETEST", "3"))   # per-slice safety envelope, not a human gate:
_MAX_GAPFILL = int(os.environ.get("AOS_QA_MAX_GAPFILL", "3")) # consecutive no-progress rotations; advancement resets it
_QA_NO_PROGRESS_REVIEW = int(os.environ.get("AOS_QA_NO_PROGRESS_REVIEW", "3"))
_QA_PERFORMANCE_MANAGEMENT_TRIGGER = "qa_performance_stalled"


def _qa_internal_review_route(record):
    """Route process-health stalls to management and product-evidence disputes to adjudication."""
    finding = (record or {}).get("finding") or {}
    return ("performance_management" if finding.get("kind") == "qa_performance_stall"
            else "evidence_review")


def _qa_authority_correlation(tenant_id, review_id, state_generation) -> str:
    """Idempotency is per semantic evidence generation, never per dispute lifetime."""
    try:
        generation = max(1, int(state_generation or 1))
    except (TypeError, ValueError):
        generation = 1
    return f"qa-dispute:{tenant_id}:{review_id}:g{generation}"


def _qa_needs_gapfill(stop_reason):
    """Stop reasons that mean "no bug proven, but coverage is still incomplete." The coordinator should not
    aggregate these as clean while the story ledger still has untested aspects."""
    stop = (stop_reason or "").lower()
    return any(x in stop for x in (
        "incomplete", "stalled", "stuck", "cap", "deadline", "repeated-action", "control-not-found",
        "target-not-found", "no-progress", "missed-target", "exhausted",
    ))


def _qa_finding_needs_fix(finding):
    """Every confirmed, unresolved product defect enters the fix/retest loop.

    ``blocking`` describes whether exploration can continue and ``severity``
    controls priority; neither is permission to ship a known defect.  Earlier
    code silently called medium/low findings clean, which made the aggregate
    verdict contradict the explorer's evidence.  Findings that a later settled
    state explicitly resolved are the only bug reports that can bypass repair.
    """
    item = finding or {}
    return bool(item.get("kind") == "bug" and not item.get("resolved"))


def _qa_pending_priority(finding):
    """Order deferred product defects by release risk, without losing queue durability."""
    item = finding or {}
    severity = {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(
        str(item.get("severity") or "").lower(), 0)
    adjudication = item.get("_qa_adjudication") or {}
    mutation_ready = int(
        isinstance(adjudication, dict)
        and adjudication.get("disposition") == "confirmed_defect"
        and bool(adjudication.get("case_id"))
        and bool(adjudication.get("review_id"))
    )
    # Risk remains the primary order. Among equally release-significant findings, use the one whose durable
    # senior-QA case has already authorized mutation; it can skip duplicate stochastic triage and reach a
    # diff/restart/retest immediately instead of sitting behind another multi-minute evidence review.
    return severity, int(bool(item.get("blocking"))), mutation_ready


_QA_FINDING_STOPWORDS = {
    "about", "after", "again", "also", "and", "app", "application", "before", "but", "does",
    "each", "from", "have", "into", "only", "product", "reports", "settled", "should", "shows",
    "that", "the", "their", "then", "this", "through", "visible", "when", "where", "while", "with",
    "workflow",
}


def _qa_finding_terms(finding):
    item = finding or {}
    # ``expected`` is usually the entire story acceptance paragraph. Including it makes two distinct defects
    # in one story look artificially similar while diluting short, highly specific observation text.
    text = " ".join(str(item.get(key) or "") for key in ("title", "bug", "detail")).lower()
    return {term for term in re.findall(r"[a-z0-9_:-]{3,}", text)
            if term not in _QA_FINDING_STOPWORDS}


def _qa_same_observation(left, right):
    """Conservatively identify paraphrases of one same-story observation.

    Exact finding IDs still remain independently auditable. This predicate only prevents ten scroll/click
    descriptions of the same missing inventory from driving ten repair chains. Claim-specific failures stay
    separate when they name different claim IDs.
    """
    if str((left or {}).get("story") or "") != str((right or {}).get("story") or ""):
        return False
    a, b = _qa_finding_terms(left), _qa_finding_terms(right)
    if not a or not b:
        return False
    claim_a = {term for term in a if term.startswith("claim_")}
    claim_b = {term for term in b if term.startswith("claim_")}
    if claim_a and claim_b and claim_a != claim_b:
        return False
    shared = a & b
    containment = len(shared) / max(1, min(len(a), len(b)))
    jaccard = len(shared) / max(1, len(a | b))
    return len(shared) >= 8 and containment >= 0.45 and jaccard >= 0.28


def _qa_select_pending_finding(pending):
    """Select the riskiest repair and include bounded same-story context.

    Explorers correctly preserve every observation, but one root cause can surface
    on several actions (for example click, reload, then keyboard activation).  A
    fixer should see that cluster in one handoff.  Only the selected finding is
    removed here: if the fresh story retest is not completely clean, the remaining
    observations stay durable and can still drive another repair.
    """
    queue = [dict(item) for item in (pending or []) if isinstance(item, dict)]
    if not queue:
        return None, []
    selected_index = max(range(len(queue)), key=lambda index: _qa_pending_priority(queue[index]))
    selected = queue.pop(selected_index)
    # Cluster transitively. Repeated browser actions often phrase A close to B and B close to C even when A
    # and C choose different nouns. Keeping only direct matches left long chains of the same defect in the
    # repair queue. Every member remains auditable in bounded duplicate context below.
    duplicates, cluster = [], [selected]
    changed = True
    while changed:
        changed = False
        distinct = []
        for item in queue:
            if any(_qa_same_observation(member, item) for member in cluster):
                duplicates.append(item)
                cluster.append(item)
                changed = True
            else:
                distinct.append(item)
        queue = distinct
    if duplicates:
        selected["duplicate_findings"] = [{
            "finding_id": item.get("finding_id"),
            "title": str(item.get("title") or item.get("bug") or "")[:240],
            "detail": str(item.get("detail") or item.get("bug") or "")[:360],
            "severity": item.get("severity"),
            "blocking": bool(item.get("blocking")),
        } for item in duplicates[:12]]
        selected["duplicate_finding_count"] = len(duplicates)
    story = str(selected.get("story") or "")
    related = []
    for item in queue:
        if not story or str(item.get("story") or "") != story:
            continue
        related.append({
            "finding_id": item.get("finding_id"),
            "title": str(item.get("title") or item.get("bug") or "")[:240],
            "detail": str(item.get("detail") or item.get("bug") or "")[:360],
            "severity": item.get("severity"),
            "blocking": bool(item.get("blocking")),
        })
        if len(related) >= 6:
            break
    if related:
        selected["related_findings"] = related
        selected["related_finding_count"] = sum(
            1 for item in queue if story and str(item.get("story") or "") == story)
    return selected, queue


def _qa_compact_pending_findings(pending):
    """Compact a durable legacy queue into risk-ordered semantic repair clusters.

    This is safe to run on every coordinator generation: it drops no evidence, embeds bounded details and IDs
    for every clustered observation, and leaves genuinely distinct same-story failures as separate repairs.
    Fresh story retesting remains the only authority that can clear the resulting defect status.
    """
    queue = [dict(item) for item in (pending or []) if isinstance(item, dict)]
    compacted = []
    while queue:
        selected, queue = _qa_select_pending_finding(queue)
        if selected:
            compacted.append(selected)
    return compacted


def _qa_mutation_may_start(children):
    """A product mutation may start only after the current QA slice is quiescent.

    Browser evidence gathered while a fixer edits the same repository is not a
    coherent release observation.  Queue fixes until every explorer in the
    current slice is terminal; the fix then receives its own fresh retest.
    """
    return not any((child or {}).get("role") == "qa-explorer" and
                   (child or {}).get("status") not in TERMINAL
                   for child in (children or {}).values())


def _qa_story_review_inflight(children, story):
    """Whether an authoritative evidence reviewer is still deciding this story's fresh proof."""
    story_id = str(story or "").strip()
    if not story_id:
        return False
    for child in (children or {}).values():
        if ((child or {}).get("role") != "qa-evidence-reviewer"
                or (child or {}).get("status") in TERMINAL):
            continue
        args = ((((child or {}).get("memory") or {}).get("context") or {}).get("tool_args") or {})
        record = args.get("internal_review") or {}
        review_story = record.get("story") or (record.get("finding") or {}).get("story")
        if str(review_story or "") == story_id:
            return True
    return False


def _qa_fix_queue_preempts_continuation(memory, *, active_dev=False):
    """Whether an ordinary evidence continuation must yield to already-grounded repair work."""
    return bool((memory or {}).get("pending_dev_findings") or active_dev)


def _qa_continuation_story(record):
    """Return the release-story identity for any managed continuation shape."""
    item = record or {}
    return str(item.get("story") or (item.get("finding") or {}).get("story") or "").strip()


def _qa_compact_story_continuations(pending, story_status=None):
    """Keep at most one full-story continuation per unfinished story.

    Gap-fill, post-review, harness-recovery, and performance-recovery records all schedule the same
    authoritative story object through ``_schedule_story_continuation``.  Keeping one row for each reason
    replayed the identical browser journey several times after one revision.  Coalesce those obligations while
    retaining their ids as audit metadata; a clean current-revision story retires its queued obligation.

    Records without a story cannot be proven equivalent, so only exact duplicate review ids are collapsed.
    """
    statuses = {str(key): value for key, value in (story_status or {}).items()}
    compacted = []
    story_slots = {}
    anonymous_ids = set()
    for raw in pending or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        story_id = _qa_continuation_story(item)
        review_id = str(item.get("review_id") or "").strip()
        if story_id and statuses.get(story_id) == "clean":
            continue
        if not story_id:
            if review_id and review_id in anonymous_ids:
                continue
            if review_id:
                anonymous_ids.add(review_id)
            compacted.append(item)
            continue
        if story_id not in story_slots:
            story_slots[story_id] = len(compacted)
            compacted.append(item)
            continue

        slot = story_slots[story_id]
        prior = dict(compacted[slot])
        prior_id = str(prior.get("review_id") or "").strip()
        merged_ids = list(prior.get("coalesced_review_ids") or [])
        for candidate in (prior_id, review_id, *(item.get("coalesced_review_ids") or [])):
            candidate = str(candidate or "").strip()
            if candidate and candidate != prior_id and candidate not in merged_ids:
                merged_ids.append(candidate)
        if merged_ids:
            prior["coalesced_review_ids"] = merged_ids
        prior["coalesced_continuation_count"] = 1 + len(merged_ids)
        compacted[slot] = prior
    return compacted


_QA_TERMINAL_REVIEW_DISPOSITIONS = frozenset({
    "confirmed_defect",
    "verified_false_positive",
    "superseded_by_current_revision",
    "superseded_by_fresh_evidence",
})


def _qa_compact_resolved_internal_reviews(reviews, states, resolutions):
    """Retire adjudication work whose exact durable review already has a terminal disposition."""
    terminal_ids = {
        str(item.get("review_id")) for item in (resolutions or [])
        if isinstance(item, dict) and item.get("review_id")
        and item.get("disposition") in _QA_TERMINAL_REVIEW_DISPOSITIONS
    }
    compacted_reviews = [
        item for item in (reviews or [])
        if not isinstance(item, dict) or str(item.get("review_id") or "") not in terminal_ids
    ]
    compacted_states = {
        key: value for key, value in (states or {}).items() if str(key) not in terminal_ids
    }
    return compacted_reviews, compacted_states


def _qa_repair_orphan_internal_review_statuses(memory):
    """Resume stories whose terminal review was compacted but status remained parked.

    Review rows are correctly retired after a verified-false-positive or revision-supersession decision, but
    older reducers did not move the owning story back from ``internal_review``. With no review, fixer, or
    continuation left, that status is an orphan and can never receive another event. Reopen only stories whose
    latest durable resolution is non-defect terminal work; confirmed defects and genuinely unresolved reviews
    remain parked behind their normal owners.
    """
    memory = memory if isinstance(memory, dict) else {}
    statuses = dict(memory.get("story_status") or {})
    unresolved = {
        str(item.get("story") or (item.get("finding") or {}).get("story") or "")
        for item in (memory.get("internal_reviews") or []) if isinstance(item, dict)
    }
    owned = {
        str((item or {}).get("story") or "")
        for item in list(memory.get("pending_story_continuations") or [])
        + list(memory.get("pending_dev_findings") or []) if isinstance(item, dict)
    }
    latest = {}
    for item in memory.get("finding_resolutions") or []:
        if not isinstance(item, dict):
            continue
        story = str(item.get("story") or (item.get("finding") or {}).get("story") or "")
        if story:
            latest[story] = item
    resumable = {"verified_false_positive", "superseded_by_current_revision",
                 "superseded_by_fresh_evidence", "superseded_by_invalid_recovery"}
    repaired = []
    pending = list(memory.get("pending_story_continuations") or [])
    for story, status in list(statuses.items()):
        resolution = latest.get(str(story)) or {}
        if (status != "internal_review" or str(story) in unresolved or str(story) in owned
                or resolution.get("disposition") not in resumable):
            continue
        statuses[str(story)] = "incomplete"
        review_id = str(resolution.get("review_id") or "terminal-review")
        pending.append({
            "review_id": f"orphan-review-resume:{story}:{review_id}",
            "story": str(story),
            "task": f"Continue story {story} after terminal QA review",
            "reason": "terminal review was compacted; resume retained evidence instead of parking",
        })
        repaired.append(str(story))
    if repaired:
        memory["story_status"] = statuses
        memory["pending_story_continuations"] = _qa_compact_story_continuations(pending, statuses)
        history = list(memory.get("resolution_status_repairs") or [])
        history.append({"stories": repaired, "action": "resume_after_terminal_review"})
        memory["resolution_status_repairs"] = history[-40:]
    return repaired


def _qa_story_continuation_may_start(children, story=None):
    """Fence a managed continuation against mutation and duplicate same-story exploration."""
    live = [child or {} for child in (children or {}).values()
            if (child or {}).get("status") not in TERMINAL]
    if any(child.get("role") == "dev-coordinator" for child in live):
        return False
    story_id = str(story or "").strip()
    if not story_id:
        return True
    for child in live:
        if child.get("role") != "qa-explorer":
            continue
        args = ((((child.get("memory") or {}).get("context") or {}).get("tool_args") or {}))
        child_story = args.get("story") or {}
        if isinstance(child_story, dict):
            child_story = child_story.get("id") or child_story.get("title")
        if str(child_story or "").strip() == story_id:
            return False
    return True


def _qa_story_status(done_payload):
    """Reduce explorer evidence to release status, not browser-continuation status."""
    payload = done_payload or {}
    result = payload.get("result") or {}
    bugs = result.get("bugs") or 0
    has_bugs = bool(len(bugs) if isinstance(bugs, (list, tuple, dict, set)) else bugs)
    if payload.get("blocking_found") or int(payload.get("findings_count") or 0) > 0 or has_bugs:
        return "blocking"
    # ``coverage-complete`` is a worker control-flow outcome, not release evidence.  Every covered assertion
    # must still resolve to a retained successful receipt (or a narrowly recognized mechanical proof).
    return "clean" if campaign_checkpoint.result_evidence_complete(result) else "incomplete"


def _qa_has_grounded_actionable_browser_receipt(result):
    """True when another identical focused reproduction cannot add decision-relevant evidence.

    This does not impose a time or attempt ceiling on genuine exploration. It recognizes a semantic terminal
    for *evidence collection*: the browser already returned an actionable finding with an exact grounded
    behavior receipt. Any remaining uncertainty belongs to adjudication/architecture, not another replay of
    the same user journey.
    """
    result = result if isinstance(result, dict) else {}
    try:
        bug_count = int(result.get("bugs") or 0)
    except (TypeError, ValueError):
        bug_count = 0
    if bug_count <= 0:
        return False
    for row in result.get("steps_detail") or []:
        if not isinstance(row, dict) or row.get("coverage_grounded") is not True:
            continue
        if not list(row.get("covers") or []) or not str(row.get("actual") or "").strip():
            continue
        verdict = row.get("verdict")
        mismatch = (not bool(verdict.get("matches_expected")) if isinstance(verdict, dict)
                    and "matches_expected" in verdict else
                    str(verdict or "").strip().lower() in {"mismatch", "fail", "failed", "defect"})
        if mismatch or row.get("bug"):
            return True
    return False


def _qa_grounded_actionable_marker(result, product_revision):
    """Return a compact durable marker for a decision-complete focused reproduction."""
    if not _qa_has_grounded_actionable_browser_receipt(result):
        return None
    revision = str(product_revision or "").strip()
    if not revision:
        # Without a revision fence we cannot distinguish an identical replay from a legitimate post-fix
        # verification. Keep the full receipt in review state, but do not make it a cross-generation stop.
        return None
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()
    return {"product_revision": revision, "receipt_digest": digest}


def _qa_grounded_actionable_marker_is_current(marker, product_revision):
    """True only while the product bytes match the grounded reproduction's revision fence."""
    marker = marker if isinstance(marker, dict) else {}
    marker_revision = str(marker.get("product_revision") or "").strip()
    current_revision = str(product_revision or "").strip()
    return bool(marker_revision and current_revision and marker_revision == current_revision)


def _qa_gapfill_candidates(story_status, planned_story_ids, gapfills, max_attempts):
    """Return known-incomplete/missing stories that still fit this slice's recovery envelope.

    This is also used at the aggregate join.  A child-done event normally schedules its own
    continuation, but a crash, an older runtime version, or a provider failure with no story in
    its result can leave every child terminal while planned work is absent from story_status.
    Such work must be re-staffed, not silently converted into a final QA verdict.
    """
    status = {str(k): v for k, v in (story_status or {}).items()}
    attempts = {str(k): int(v or 0) for k, v in (gapfills or {}).items()}
    candidates = []
    for story_id in map(str, planned_story_ids or []):
        if status.get(story_id) not in (None, "incomplete"):
            continue
        if int(max_attempts or 0) <= 0 or attempts.get(story_id, 0) < int(max_attempts):
            candidates.append(story_id)
    return candidates


def _qa_story_progress(memory, story, result):
    """Track semantic evidence advancement across subordinate rotations.

    A fresh browser run starts its local step counter at zero. Comparing only row counts let a longer replay of
    the same clicks reset the coordinator's no-progress streak, which could rotate explorers forever. Progress
    is now a union of exact covered ledger aspects and stable semantic step signatures; repeated actions, new
    screenshots, and volatile timestamps cannot manufacture advancement.
    """
    story_id = str(story)
    coverage = [item for item in ((result or {}).get("coverage") or []) if isinstance(item, dict)]
    progress = dict((memory or {}).get("story_progress") or {})
    prior = dict(progress.get(story_id) or {})
    prior_aspects = {str(item) for item in (prior.get("covered_aspects") or []) if str(item).strip()}
    current_aspects = {str(item.get("aspect")) for item in coverage
                       if item.get("covered") and str(item.get("aspect") or "").strip()}
    # Every explorer result carries its complete authoritative ledger (including inherited covered flags).
    # Replace obsolete labels when that ledger is present instead of unioning paraphrases forever. The old
    # behavior could report impossible progress such as 7/4 after a rolling upgrade changed aspect wording.
    # If a failure produced no ledger at all, retain the prior checkpoint unchanged.
    covered_aspects = current_aspects if coverage else prior_aspects

    prior_evidence = {str(item) for item in (prior.get("evidence_signatures") or []) if str(item).strip()}
    current_evidence = set()
    for item in (result or {}).get("steps_detail") or []:
        if not isinstance(item, dict):
            continue
        raw_action = item.get("action")
        if isinstance(raw_action, dict):
            # Browser element indexes are observation-local and model-written expectation prose is
            # non-deterministic.  Neither is evidence that a fresh continuation reached somewhere new.
            # Retain only the stable user intent and action parameters.
            action = {key: raw_action.get(key) for key in (
                "cmd", "target_text", "role", "selector", "value", "count", "interval_ms",
                "duration_ms") if raw_action.get(key) not in (None, "", [])}
        else:
            action = raw_action
        raw_verdict = item.get("verdict")
        if isinstance(raw_verdict, dict):
            verdict = {key: raw_verdict.get(key) for key in (
                "verdict", "matches_expected", "blocking", "severity")
                if raw_verdict.get(key) not in (None, "")}
        else:
            verdict = raw_verdict
        actual = item.get("actual") if isinstance(item.get("actual"), dict) else {}
        before = item.get("state") if isinstance(item.get("state"), dict) else {}
        semantic = {
            "action": action,
            "route": actual.get("url") or before.get("url"),
            "verdict": verdict,
            "covers": sorted(str(value) for value in (item.get("covers") or []) if str(value).strip()),
            "demonstrated": sorted(str(value) for value in (item.get("demonstrated") or [])
                                   if str(value).strip()),
        }
        semantic = {key: value for key, value in semantic.items()
                    if value not in (None, "", [], {})}
        if not semantic:
            continue
        current_evidence.add(hashlib.sha256(
            json.dumps(semantic, sort_keys=True, default=str).encode()).hexdigest()[:24])
    evidence = prior_evidence | current_evidence
    advanced = bool((current_aspects - prior_aspects) or (current_evidence - prior_evidence))
    no_progress = 0 if advanced else int(prior.get("no_progress") or 0) + 1
    current = {"covered": len(covered_aspects),
               "covered_aspects": sorted(covered_aspects),
               "coverage_total": (len(coverage) if coverage else int(prior.get("coverage_total") or 0)),
               "steps": len(evidence), "evidence_signatures": sorted(evidence)[-240:],
               "no_progress": no_progress, "advanced": advanced,
               "stop_reason": (result or {}).get("stop_reason"),
               "slow_phases": list((result or {}).get("slow_phases") or [])[-8:]}
    progress[story_id] = current
    memory["story_progress"] = progress
    return current


def _qa_record_performance_observations(memory, story, progress_state):
    """Make subordinate self-observations durable and return only newly observed slow phases."""
    existing = list((memory or {}).get("performance_events") or [])
    seen = {item.get("key") for item in existing if isinstance(item, dict)}
    added = []
    for raw in (progress_state or {}).get("slow_phases") or []:
        if not isinstance(raw, dict):
            continue
        event = {"story": str(story), "phase": str(raw.get("phase") or "unknown"),
                 "step": raw.get("step"), "elapsed_s": raw.get("elapsed_s"),
                 "status": raw.get("status")}
        event["key"] = (f"{event['story']}:{event['phase']}:{event['step']}:"
                        f"{event['elapsed_s']}:{event['status']}")
        if event["key"] in seen:
            continue
        seen.add(event["key"])
        existing.append(event)
        added.append(event)
    memory["performance_events"] = existing[-100:]
    return added


def _qa_apply_performance_recovery(memory, recovery):
    """Apply one manager-authored process recovery to the exact stalled review.

    This is deliberately a story retry, never a QA pass: prior semantic evidence is retained, only the
    consecutive no-progress streak is cleared, and the story remains incomplete until a fresh explorer
    genuinely exhausts its ledger.
    """
    recovery = dict(recovery or {})
    review_id = str(recovery.get("review_id") or "")
    reviews = list((memory or {}).get("internal_reviews") or [])
    record = next((item for item in reviews if str(item.get("review_id") or "") == review_id), None)
    if not record:
        return None
    story_id = str(recovery.get("story") or record.get("story")
                   or (record.get("finding") or {}).get("story") or "")
    memory["internal_reviews"] = [item for item in reviews
                                  if str(item.get("review_id") or "") != review_id]
    states = dict(memory.get("internal_review_states") or {})
    state = dict(states.get(review_id) or {})
    state.update({"status": "management_recovery", "management_action": recovery.get("action"),
                  "management_rationale": str(recovery.get("rationale") or "")[:1000]})
    states[review_id] = state
    memory["internal_review_states"] = states
    if story_id:
        statuses = dict(memory.get("story_status") or {})
        statuses[story_id] = "incomplete"
        memory["story_status"] = statuses
        gapfills = dict(memory.get("gapfills") or {})
        gapfills[story_id] = 0
        memory["gapfills"] = gapfills
        progress = dict(memory.get("story_progress") or {})
        prior = dict(progress.get(story_id) or {})
        prior["no_progress"] = 0
        prior["advanced"] = False
        progress[story_id] = prior
        memory["story_progress"] = progress
    return {**record, "story": story_id or record.get("story")}


def _qa_clear_restored_capability_reviews(memory, story, result):
    """Retire stale capability cases once a fresh worker actually exercised that capability.

    Capability admission failures are process state, not immutable product findings. A later same-story run
    that completed at least one browser step without reporting a missing capability proves provisioning is
    restored even if semantic coverage is still incomplete for another reason.
    """
    result = dict(result or {})
    if (list(result.get("missing_capabilities") or [])
            or str(result.get("stop_reason") or "") == "capability-unavailable"
            or int(result.get("steps") or 0) <= 0):
        return []
    story_id = str(story or "")
    reviews = list((memory or {}).get("internal_reviews") or [])
    cleared = [item for item in reviews
               if str(item.get("story") or (item.get("finding") or {}).get("story") or "") == story_id
               and (str(item.get("review_id") or "").startswith("qa-capability-")
                    or item.get("route") == "qa-capability-management")]
    if not cleared:
        return []
    cleared_ids = {str(item.get("review_id") or "") for item in cleared}
    memory["internal_reviews"] = [item for item in reviews
                                  if str(item.get("review_id") or "") not in cleared_ids]
    states = dict(memory.get("internal_review_states") or {})
    for review_id in cleared_ids:
        state = dict(states.get(review_id) or {})
        state.update({"status": "capability_restored", "story": story_id,
                      "restored_by_steps": int(result.get("steps") or 0)})
        states[review_id] = state
    memory["internal_review_states"] = states
    return sorted(cleared_ids)


def _qa_clear_proven_fixed(pending, story, result):
    """Remove deferred defects superseded by a complete, clean retest of their story.

    Findings may queue while another fixer owns the repository. Replaying those old
    observations after stronger clean evidence creates an endless fix/retest loop.
    Incomplete or non-clean results deliberately preserve the queue.
    """
    result = result or {}
    if not campaign_checkpoint.result_evidence_complete(result):
        return list(pending or [])
    story_id = str(story)
    return [bug for bug in (pending or [])
            if str((bug or {}).get("story")) != story_id]


def _qa_dev_completion_receipt(root_actor_id, actors, child_result=None):
    """Recover the exact changed-file receipt from a completed dev subtree.

    A rolling handoff can checkpoint a fixer after it writes product bytes, then resume its parent. The
    resumed fixer may correctly report ``fixed=false`` because the workspace is already fixed, while its
    durable nested result still carries the prior process's files. Looking only at the parent aggregate loses
    that receipt and falsely turns a bounded mutation into a full-manifest invalidation. Only receipt-shaped
    fields in the named subtree are accepted; finding provenance is not mutation authority.
    """
    by_id = {int(actor.get("actor_id") or 0): actor for actor in (actors or [])}
    root = int(root_actor_id or 0)
    subtree = {root}
    advanced = True
    while advanced:
        advanced = False
        for actor_id, actor in by_id.items():
            if actor_id not in subtree and int(actor.get("supervisor_id") or 0) in subtree:
                subtree.add(actor_id)
                advanced = True

    files, summaries, change_diffs = [], [], []

    def collect(value, depth=0):
        if not isinstance(value, dict) or depth > 4:
            return
        direct = value.get("files")
        if isinstance(direct, list):
            files.extend(str(path).strip() for path in direct if str(path).strip())
        direct_diff = value.get("change_diff")
        if isinstance(direct_diff, str) and direct_diff.strip():
            change_diffs.append(direct_diff.strip()[:14000])
        plan = value.get("plan")
        if isinstance(plan, dict) and plan.get("rationale"):
            summaries.append(str(plan["rationale"])[:2000])
        for key in ("result", "partial_result"):
            nested = value.get(key)
            if isinstance(nested, dict):
                collect(nested, depth + 1)
            elif key == "result" and isinstance(nested, str) and nested.strip():
                summaries.append(nested.strip()[:2000])

    collect(child_result or {})
    for actor_id in sorted(subtree):
        actor = by_id.get(actor_id) or {}
        collect(actor.get("result") or {})
        context = dict((actor.get("memory") or {}).get("context") or {})
        tool_args = dict(context.get("tool_args") or {})
        resumed = tool_args.get("resume_changed_files")
        if isinstance(resumed, list):
            files.extend(str(path).strip() for path in resumed if str(path).strip())
        resumed_diff = tool_args.get("resume_change_diff")
        if isinstance(resumed_diff, str) and resumed_diff.strip():
            change_diffs.append(resumed_diff.strip()[:14000])
    return {"files": list(dict.fromkeys(files))[:200],
            "summaries": list(dict.fromkeys(summaries))[:12],
            "change_diffs": list(dict.fromkeys(change_diffs))[:6],
            "actor_ids": sorted(subtree)}


def _qa_revision_impact_scope(stories, changed_files, triggering_story, bug=None, *, repo=".", reviewer=None,
                              change_summary=None):
    """Conservatively identify which end-to-end stories a bounded product change can affect.

    Two independent read-only reviews establish possible effects. When their concrete story sets disagree, a
    third bounded adjudicator resolves only the disputed IDs; malformed/unavailable adjudication falls back
    to the conservative union. Unknown/shared runtime changes fail closed to a full regression. This only
    changes scheduling: the triggering story is always re-tested, product tests still run in the fixer, and
    the final strong fix judge is unchanged.
    """
    contracts = []
    for story in stories or []:
        sid = str((story or {}).get("id") or (story or {}).get("title") or "").strip()
        if sid:
            contracts.append({"id": sid, "title": (story or {}).get("title"),
                              "steps": (story or {}).get("steps"),
                              "expected": ((story or {}).get("expected")
                                           or (story or {}).get("expected_outcome"))})
    all_ids = {item["id"] for item in contracts}
    trigger = str(triggering_story or "").strip()
    paths = sorted({str(path).replace("\\", "/").lstrip("./")
                    for path in changed_files or [] if str(path)})
    executable = [path for path in paths if not path.startswith(("tests/", "test/", "docs/"))]
    # Dependency, schema, and foundational state-contract changes are inherently cross-cutting. A shared
    # HTML shell, composition module, or stylesheet is only *potentially* cross-cutting: a narrow CEO-panel
    # focus fix can touch those files without changing eleven unrelated journeys. Send those surfaces through
    # two independent impact reviews instead of hard-coding a global rerun.
    hard_full_markers = (
        "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
        "src/local_state", "src/domain_contract", "/schema", "/migration",
    )
    review_shared_markers = ("index.html", "src/browser_app/")
    if not all_ids or trigger not in all_ids or not executable:
        return {"full_regression": True, "impacted_story_ids": sorted(all_ids),
                "reason": "no bounded executable change set was available", "changed_files": paths}
    if any(path in hard_full_markers
           or any(marker in path for marker in hard_full_markers if marker.startswith("src/"))
           or path.endswith(".sql") for path in executable):
        return {"full_regression": True, "impacted_story_ids": sorted(all_ids),
                "reason": "persistence, contract, dependency, or schema bytes changed",
                "changed_files": paths}
    potentially_shared = [path for path in executable
                          if path in review_shared_markers
                          or any(marker in path for marker in review_shared_markers
                                 if marker.endswith("/"))
                          or path.endswith(".css")]

    raw_summary = dict(change_summary or {})
    raw_diffs = raw_summary.pop("durable_change_diffs", [])
    if isinstance(raw_diffs, str):
        raw_diffs = [raw_diffs]
    exact_diffs = [str(value).strip() for value in (raw_diffs or [])
                   if str(value).strip()]
    exact_diff = "\n\n".join(dict.fromkeys(exact_diffs))[:14000]
    summary = {key: value for key, value in raw_summary.items()
               if value not in (None, "", [], {})}
    # Test names are executable impact evidence. They are cheap to extract and substantially more precise than
    # asking reviewers to infer the behavior of a bounded fix from a module filename alone. Keep this dossier
    # deliberately small; reviewers still operate read-only in an empty cwd.
    test_signals = {}
    root = Path(repo or ".")
    for path in paths:
        if not path.startswith(("tests/", "test/")):
            continue
        try:
            raw = (root / path).read_text(errors="replace")
        except OSError:
            continue
        lines = [line.strip() for line in raw.splitlines()
                 if re.search(r"\b(?:US[-_ ]?\d+|test\s*\(|describe\s*\()", line, re.I)]
        if lines:
            test_signals[path] = lines[-24:]

    # A file-list receipt proves *where* bytes changed but a feature module or namespaced stylesheet can still
    # look globally shared from its path alone. Supply bounded current-source windows selected by the finding's
    # own vocabulary. This is orientation for blast-radius analysis, not a substitute diff or a fix verdict;
    # it lets reviewers distinguish `.pe-field-error` from an unscoped global input rule without recursively
    # scanning the checkout. Exact paths remain the authoritative mutation receipt.
    query_terms = {
        term for term in re.findall(
            r"[a-z0-9_-]{4,}",
            (str(trigger) + " " + json.dumps(bug or {}, default=str)).casefold())
        if term not in {"actual", "after", "before", "behavior", "blocking", "detail", "expected",
                        "finding", "product", "reported", "should", "story", "their", "there", "these",
                        "this", "through", "title", "value", "while", "without"}
    }
    source_signals = {}
    for rel in executable[:12]:
        path = root / rel
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        ranked = []
        for line_no, line in enumerate(lines, 1):
            lowered = line.casefold()
            score = sum(1 for term in query_terms if term in lowered)
            if score:
                ranked.append((score, line_no))
        definitions = [line_no for line_no, line in enumerate(lines, 1)
                       if re.search(r"^\s*(?:export\b|(?:async\s+)?function\b|class\b|"
                                    r"(?:const|let|var)\s+[A-Za-z_$]|[.#][A-Za-z_-][\w-]*\s*[{,])", line)]
        anchors = list(dict.fromkeys(
            [line for _score, line in sorted(ranked, key=lambda row: (-row[0], row[1]))[:8]]
            + definitions[:4]))
        windows = []
        for anchor in sorted(anchors):
            start, end = max(1, anchor - 2), min(len(lines), anchor + 2)
            if windows and start <= windows[-1][1] + 1:
                windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            else:
                windows.append((start, end))
        records = [{"start_line": start, "end_line": end,
                    "text": "\n".join(lines[start - 1:end])}
                   for start, end in windows[:5]]
        if records:
            source_signals[rel] = records

    base = (
        "Determine the conservative end-to-end regression impact of a completed product fix. This is not a "
        "bug verdict. Include every story whose user-observable behavior, shared state, security boundary, or "
        "downstream rendering could plausibly change because of the specific completed change. Do not include "
        "unrelated stories merely because they share a page or broad module name. Require an explicit conservative "
        "causal path from the changed bytes or event to behavior exercised by every proposed story. A story is "
        "not affected merely because it starts in the same feature or later consumes records produced by an "
        "unchanged core submit/commit path. For an input-event adapter change, include downstream stories only "
        "when their contract exercises that event or supplied evidence shows the shared core path also changed. "
        "For a non-foundational feature-module change, the exact file receipt plus bounded source and test "
        "signals is sufficient to propose an explicit conservative set; absence of a historical diff alone "
        "does not justify full_regression. "
        "Prefer an explicit set of affected story IDs; set full_regression=true only when the change is genuinely cross-cutting or "
        "the supplied mutation receipt is too ambiguous to bound safely. The triggering story must be included. "
        "Reply ONLY JSON: "
        '{"impacted_story_ids":["exact IDs"],"full_regression":false,"reason":"brief evidence"}.\n\n'
        f"TRIGGERING STORY: {trigger}\n"
        f"FIXED FINDING: {json.dumps(bug or {}, default=str)[:2400]}\n"
        f"AUTHORITATIVE MUTATION RECEIPT — EXACT FILES ACTUALLY CHANGED: {json.dumps(paths)}\n"
        "The receipt is present and must not be described as withheld merely because a prose summary is absent.\n"
        f"EXACT MUTATION DIFF FROM THE FENCED WRITER (authoritative when present; do not attribute any "
        f"current-file content absent from this diff to the repair):\n{exact_diff or '(legacy handoff: unavailable)'}\n"
        f"POTENTIALLY SHARED SURFACES REQUIRING EXPLICIT BLAST-RADIUS REVIEW: "
        f"{json.dumps(potentially_shared)}\n"
        f"FIXER CHANGE SUMMARY: {json.dumps(summary, default=str)[:5000]}\n"
        f"CHANGED TEST CONTRACT SIGNALS: {json.dumps(test_signals, default=str)[:5000]}\n"
        f"BOUNDED CURRENT CHANGED-FILE BEHAVIOR SIGNALS (orientation, not a diff): "
        f"{json.dumps(source_signals, default=str)[:7000]}\n"
        f"STORY CONTRACTS: {json.dumps(contracts, default=str)[:14000]}\n")

    def decide(lens):
        if reviewer is not None:
            return reviewer(lens, base)
        # The bounded dossier is complete. An empty enforced cwd prevents an agentic reviewer from recursively
        # reading an exported product tree and turning a small impact decision into another multi-minute scan.
        with tempfile.TemporaryDirectory(prefix="aos-qa-impact-") as decision_cwd:
            return _ai_json(
                "qa-change-impact-reviewer", decision_cwd, base + "\nINDEPENDENT LENS: " + lens,
                compact=True,
                codex_model=os.environ.get("AOS_QA_IMPACT_CODEX_MODEL", "gpt-5.6-terra"),
                reasoning_effort=os.environ.get("AOS_QA_IMPACT_REASONING_EFFORT", "high"))

    lenses = (
        "dependency, persistence, and security-boundary propagation",
        "user journey, UI surface, and regression behavior propagation",
    )
    # Both reviewers receive one immutable bounded dossier and share no state. Serial calls doubled every
    # post-fix transition without adding independence; run them concurrently and restore lens order before
    # applying the unchanged conservative-union policy.
    by_index = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="qa-impact") as pool:
        futures = {pool.submit(decide, lens): index for index, lens in enumerate(lenses)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                by_index[index] = future.result()
            except Exception as exc:
                by_index[index] = {"_blocker": f"impact reviewer unavailable: {exc}"}
    reviews = [by_index[index] for index in range(len(lenses))]
    impacted = {trigger} if trigger in all_ids else set()
    reasons = []
    review_scopes = []
    for review in reviews:
        if (not isinstance(review, dict) or review.get("_blocker")
                or not isinstance(review.get("impacted_story_ids"), list)
                or not isinstance(review.get("full_regression"), bool)):
            return {"full_regression": True, "impacted_story_ids": sorted(all_ids),
                    "reason": "an independent impact review was unavailable or malformed",
                    "changed_files": paths}
        proposed = {str(item) for item in review["impacted_story_ids"]}
        if not proposed.issubset(all_ids):
            return {"full_regression": True, "impacted_story_ids": sorted(all_ids),
                    "reason": "an impact review escaped the known story manifest",
                    "changed_files": paths}
        # One conservative reviewer must not unilaterally turn a bounded feature repair into a full-manifest
        # browser replay. Treat its full-regression claim as an all-story scope proposal; the independent lens
        # can disagree and the existing third adjudicator then resolves only that disagreement. Two agreeing
        # full scopes, or an unavailable/malformed adjudicator, still fail closed to the complete manifest.
        if review["full_regression"]:
            proposed = set(all_ids)
        review_scopes.append(proposed)
        reasons.append(("full-scope proposal: " if review["full_regression"] else "")
                       + str(review.get("reason") or "")[:500])
    union_scope = set().union(*review_scopes) if review_scopes else set()
    if len({tuple(sorted(scope)) for scope in review_scopes}) > 1:
        common_scope = set.intersection(*review_scopes) if review_scopes else set()
        arbitration_lens = (
            "ADJUDICATE ONLY THIS IMPACT-SCOPE DISAGREEMENT. Common IDs are mandatory. For each disputed "
            "ID, include it only when the immutable dossier gives a concrete causal path from the changed "
            "bytes/event to behavior explicitly exercised by that story. Do not broaden based only on a later "
            "workflow consuming records from an unchanged core path. Return the same JSON schema. "
            f"COMMON_IDS={sorted(common_scope)} DISPUTED_IDS={sorted(union_scope - common_scope)} "
            f"REVIEW_SCOPES={[sorted(scope) for scope in review_scopes]}"
        )
        try:
            arbitration = decide(arbitration_lens)
        except Exception as exc:
            arbitration = {"_blocker": f"impact adjudicator unavailable: {exc}"}
        if (not isinstance(arbitration, dict) or arbitration.get("_blocker")
                or not isinstance(arbitration.get("impacted_story_ids"), list)
                or not isinstance(arbitration.get("full_regression"), bool)):
            impacted.update(union_scope)
            reasons.append("scope adjudication unavailable; retained conservative union")
        else:
            adjudicated = {str(item) for item in arbitration["impacted_story_ids"]}
            if (arbitration["full_regression"] or not adjudicated.issubset(union_scope)
                    or not common_scope.issubset(adjudicated)):
                impacted.update(union_scope)
                reasons.append("scope adjudication escaped its fence; retained conservative union")
            else:
                impacted.update(adjudicated)
                reasons.append("disagreement adjudicated: "
                               + str(arbitration.get("reason") or "")[:500])
    else:
        impacted.update(union_scope)
    full_regression = bool(all_ids and all_ids.issubset(impacted))
    return {"full_regression": full_regression, "impacted_story_ids": sorted(impacted),
            "preserved_story_ids": sorted(all_ids - impacted),
            "reason": "independent impact review: " + " | ".join(reasons),
            "changed_files": paths}


def _qa_invalidate_revision(memory, current_revision, *, impacted_story_ids=None, impact=None,
                            tenant_id=None):
    """Invalidate affected verdicts, with the former full-manifest behavior as the fail-closed default.

    Selectively preserved verdicts receive an explicit revision-equivalence audit. Stale story attempt
    counters are reset; fix/retest safety limits remain independent.
    """
    if not current_revision:
        return []
    previous = ((memory or {}).get("coverage_revision")
                or (((memory or {}).get("context") or {}).get("product_revision")))
    if not previous:
        memory["coverage_revision"] = current_revision
        return []
    if previous == current_revision:
        return []
    statuses = dict(memory.get("story_status") or {})
    known = set(map(str, statuses))
    stale = sorted(known if impacted_story_ids is None else
                   known.intersection({str(item) for item in impacted_story_ids}))
    stale_set = set(stale)
    preserved = sorted(known - stale_set)
    generation = int(memory.get("revision_generation") or 0) + 1
    history = list(memory.get("revision_invalidations") or [])[-19:]
    history.append({"from": previous, "to": current_revision, "stale_story_ids": stale,
                    "preserved_story_ids": preserved, "generation": generation,
                    "stale_story_status": {key: statuses[key] for key in stale if key in statuses},
                    "changed_files": list((impact or {}).get("changed_files") or []),
                    "full_regression": impacted_story_ids is None,
                    "reason": str((impact or {}).get("reason") or (
                        "QA fixer changed product bytes; full-manifest regression required"
                        if impacted_story_ids is None else
                        "independent change-impact reviews established bounded revision equivalence"))[:1500]})
    memory["story_status"] = {key: value for key, value in statuses.items()
                              if str(key) not in stale_set}
    memory["gapfills"] = {key: value for key, value in dict(memory.get("gapfills") or {}).items()
                          if str(key) not in stale_set}
    memory["coverage_revision"] = current_revision
    memory["revision_generation"] = generation
    memory["revision_invalidations"] = history
    memory["revision_reconciliation"] = {
        "state": "selective_invalidation" if impacted_story_ids is not None else "full_invalidation",
        "from": previous, "to": current_revision,
        "changed_files": list((impact or {}).get("changed_files") or []),
    }
    if stale_set:
        reviews = list(memory.get("internal_reviews") or [])
        stale_reviews = [item for item in reviews if _qa_continuation_story(item) in stale_set]
        stale_review_ids = {str(item.get("review_id") or "") for item in stale_reviews
                            if item.get("review_id")}
        memory["internal_reviews"] = [item for item in reviews
                                      if str(item.get("review_id") or "") not in stale_review_ids]
        states = dict(memory.get("internal_review_states") or {})
        resolutions = list(memory.get("finding_resolutions") or [])
        for review in stale_reviews:
            review_id = str(review.get("review_id") or "")
            prior_state = dict(states.get(review_id) or {})
            case_id = prior_state.get("case_id") or review.get("case_id")
            prior_state.update({
                "status": "superseded_by_revision",
                "product_revision": current_revision,
                "reason": "the finding belongs to product bytes invalidated by a newer revision",
            })
            states[review_id] = prior_state
            if not any(str(item.get("review_id") or "") == review_id
                       and item.get("disposition") == "superseded_by_current_revision"
                       for item in resolutions):
                finding = review.get("finding") or {}
                resolutions.append({
                    "review_id": review_id,
                    "case_id": case_id,
                    "finding_id": finding.get("finding_id"),
                    "story": _qa_continuation_story(review),
                    "title": finding.get("title") or finding.get("bug"),
                    "disposition": "superseded_by_current_revision",
                    "reason": "product bytes changed; current-revision story evidence is required",
                })
            if tenant_id and case_id:
                try:
                    import qareview
                    qareview.supersede_by_revision(
                        str(tenant_id), str(case_id), current_revision,
                        reason="runtime revision invalidation superseded the finding-time product bytes")
                except Exception:
                    # The coordinator memory remains the release authority. A missing/legacy dispute row must
                    # not turn a safe revision fence into a failed product update.
                    pass
        memory["internal_review_states"] = states
        memory["finding_resolutions"] = resolutions[-200:]
        memory["pending_dev_findings"] = [
            finding for finding in (memory.get("pending_dev_findings") or [])
            if str((finding or {}).get("story") or "") not in stale_set
        ]
    return stale


def _qa_invalidate_recovery_sources(memory, story_ids, actor_ids, *, finding_ids=None, reason=None):
    """Discard verdicts derived from browser checkpoints whose evidence provenance was invalid.

    This is distinct from a product revision change: the current product hash is still correct, but a newly
    hired explorer may have inherited old-revision localStorage/coverage through a legacy continuation. Keep
    the bad actor and finding evidence for audit, fence them from future recovery, and re-open only affected
    stories from a fresh browser. Unaffected story evidence remains authoritative.
    """
    resolution_reason = str(
        reason or "browser recovery provenance did not match current product bytes")[:1500]
    stories = {str(item) for item in (story_ids or []) if str(item)}
    actors = {str(item) for item in (actor_ids or []) if str(item)}
    findings = {str(item) for item in (finding_ids or []) if str(item)}
    if not stories and not actors:
        return []
    statuses = dict(memory.get("story_status") or {})
    stale = sorted(stories.intersection(map(str, statuses)))
    memory["story_status"] = {key: value for key, value in statuses.items()
                              if str(key) not in stories}
    memory["gapfills"] = {key: value for key, value in dict(memory.get("gapfills") or {}).items()
                          if str(key) not in stories}
    memory["invalid_recovery_actor_ids"] = list(dict.fromkeys(
        [str(item) for item in (memory.get("invalid_recovery_actor_ids") or [])] + sorted(actors)
    ))[-500:]
    history = list(memory.get("recovery_invalidations") or [])[-49:]
    history.append({
        "story_ids": sorted(stories), "actor_ids": sorted(actors),
        "finding_ids": sorted(findings), "product_revision": memory.get("coverage_revision"),
        "reason": resolution_reason,
    })
    memory["recovery_invalidations"] = history

    reviews = list(memory.get("internal_reviews") or [])
    stale_reviews = [item for item in reviews if _qa_continuation_story(item) in stories]
    stale_review_ids = {str(item.get("review_id") or "") for item in stale_reviews}
    memory["internal_reviews"] = [item for item in reviews
                                  if str(item.get("review_id") or "") not in stale_review_ids]
    states = dict(memory.get("internal_review_states") or {})
    resolutions = list(memory.get("finding_resolutions") or [])
    for review in stale_reviews:
        review_id = str(review.get("review_id") or "")
        state = dict(states.get(review_id) or {})
        state.update({"status": "superseded_by_invalid_recovery", "reason": resolution_reason})
        states[review_id] = state
    candidates = list(memory.get("pending_dev_findings") or []) + list(memory.get("qa_findings") or [])
    for finding in candidates:
        if not isinstance(finding, dict):
            continue
        finding_id = str(finding.get("finding_id") or "")
        if finding_id not in findings:
            continue
        if not any(str(item.get("finding_id") or "") == finding_id
                   and item.get("disposition") == "superseded_by_invalid_recovery"
                   for item in resolutions):
            resolutions.append({
                "finding_id": finding_id, "story": finding.get("story"),
                "title": finding.get("title") or finding.get("bug"),
                "disposition": "superseded_by_invalid_recovery",
                "reason": resolution_reason,
            })
    for finding_id in sorted(findings):
        if not any(str(item.get("finding_id") or "") == finding_id
                   and item.get("disposition") == "superseded_by_invalid_recovery"
                   for item in resolutions):
            # A finding already handed to a dev coordinator may have left both pending queues. Its immutable
            # event still exists and report reconciliation keys by finding_id, so retain a minimal terminal
            # disposition instead of letting that contaminated observation block the final release forever.
            resolutions.append({
                "finding_id": finding_id,
                "disposition": "superseded_by_invalid_recovery",
                "reason": resolution_reason,
            })
    memory["internal_review_states"] = states
    memory["finding_resolutions"] = resolutions[-200:]
    memory["pending_dev_findings"] = [
        finding for finding in (memory.get("pending_dev_findings") or [])
        if str((finding or {}).get("finding_id") or "") not in findings
        and str((finding or {}).get("story") or "") not in stories
    ]
    return stale


def _qa_record_internal_review(memory, payload) -> dict:
    """Idempotently persist a disputed finding in the internal QA-management queue.

    Event delivery is at-least-once, so the stable review id is the crash/replay fence. This queue is an
    internal terminal release state: it is never translated to ``disagree``, ``resource_request``, or another
    event kind that the controller could turn into a CEO/human gate.
    """
    item = dict(payload or {})
    finding = item.get("finding") or {}
    identity = {"story": item.get("story") or finding.get("story"),
                "title": finding.get("title") or finding.get("bug") or finding.get("detail"),
                "provenance": finding.get("evidence_provenance")}
    queue = list((memory or {}).get("internal_reviews") or [])
    semantic_existing = next((entry for entry in queue
                              if _qa_continuation_story(entry) == str(identity["story"] or "")
                              and _qa_same_observation(entry.get("finding") or {}, finding)), None)
    if semantic_existing:
        return semantic_existing
    review_id = "qa-review-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:16]
    existing = next((entry for entry in queue if entry.get("review_id") == review_id), None)
    if existing:
        return existing
    record = {"review_id": review_id, "state": "pending_internal_management",
              "route": "qa-internal-management", "story": identity["story"],
              "finding": finding, "triage": item.get("triage"), "reason": item.get("reason")}
    queue.append(record)
    memory["internal_reviews"] = queue
    return record


def _qa_record_capability_review(memory, story, capabilities, *, case_id=None) -> dict:
    """Persist one stable internal-management case for an unavailable QA capability.

    Capability absence is neither a product defect nor another browser gap-fill. Keeping it in the same
    unresolved-management spine used by evidence disputes lets qa_agentic checkpoint immediately while a
    duty manager provisions the tool, changes the evidence contract, or names a truly external authority.
    """
    normalized = []
    for raw in capabilities or []:
        if not isinstance(raw, dict) or not raw.get("capability"):
            continue
        normalized.append({
            "capability": str(raw["capability"]),
            "aspects": sorted({str(item) for item in (raw.get("aspects") or []) if str(item)}),
            "reason": str(raw.get("reason") or "")[:1000],
        })
    normalized.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    identity = {"story": str(story), "capabilities": normalized}
    review_id = "qa-capability-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:16]
    queue = list((memory or {}).get("internal_reviews") or [])
    record = next((item for item in queue if item.get("review_id") == review_id), None)
    if record is None:
        record = {"review_id": review_id, "state": "pending_internal_management",
                  "route": "qa-capability-management", "story": str(story),
                  "capabilities": normalized,
                  "reason": "required QA execution capability is unavailable"}
        queue.append(record)
    if case_id:
        record["case_id"] = case_id
    memory["internal_reviews"] = queue
    states = dict((memory or {}).get("internal_review_states") or {})
    states[review_id] = {"status": "manager_attention", "case_id": case_id,
                         "capabilities": normalized,
                         "reason": "required QA execution capability is unavailable"}
    memory["internal_review_states"] = states
    return record


def _qa_verification_peer(children, internal_reviews, target_record, *, exclude_review_id=None):
    """Return one live browser verification already proving the same semantic observation, if any."""
    records = {str(item.get("review_id")): item for item in (internal_reviews or [])
               if isinstance(item, dict) and item.get("review_id")}
    target_record = target_record or {}
    story = str(target_record.get("story") or (target_record.get("finding") or {}).get("story") or "")
    target_finding = target_record.get("finding") or {}
    for child_id, child in sorted(dict(children or {}).items()):
        if child.get("role") != "qa-explorer" or child.get("status") in TERMINAL:
            continue
        args = ((((child.get("memory") or {}).get("context") or {}).get("tool_args") or {}))
        peer_id = str(args.get("_qa_review_id") or "")
        record = records.get(peer_id) or {}
        peer_story = str(record.get("story") or (record.get("finding") or {}).get("story") or "")
        # A focused verifier is admissible for a follower only when both cases describe the same observed
        # symptom. Same-story alone is insufficient: one US-010 run about a leaked token cannot adjudicate an
        # unrelated label or blocker-target dispute merely because both share the release story identifier.
        same_observation = _qa_same_observation(record.get("finding") or {}, target_finding)
        if (peer_id and peer_id != str(exclude_review_id or "")
                and peer_story == story and same_observation):
            return {"actor_id": child_id, "review_id": peer_id}
    return None


def _qa_prior_coverage(results, story_id):
    """Coverage is resumable only with the exact portable browser state from the same latest result."""
    recovery = _qa_prior_recovery(results, story_id)
    return recovery.get("resume_covered", [])


def _qa_revision_lineage_preserves_story(source_revision, product_revision, story_id,
                                         revision_invalidations=None):
    """True when every audited revision edge explicitly preserves this story.

    Browser state remains fenced to its original bytes.  A conservative selective-impact decision may,
    however, prove that an unrelated change does not affect a story.  Walking only explicit ``from`` -> ``to``
    edges whose review says ``full_regression: false`` and names the story in ``preserved_story_ids`` lets that
    story keep its exact checkpoint without turning one selective decision into a blanket cross-revision pass.
    """
    source = str(source_revision or "").strip()
    wanted = str(product_revision or "").strip()
    story = str(story_id or "").strip()
    if not source or not wanted:
        return not wanted or source == wanted
    if source == wanted:
        return True
    edges = {}
    for raw in revision_invalidations or []:
        record = raw if isinstance(raw, dict) else {}
        frm, to = str(record.get("from") or "").strip(), str(record.get("to") or "").strip()
        preserved = {str(item) for item in (record.get("preserved_story_ids") or [])}
        if (not frm or not to or record.get("full_regression") is not False
                or story not in preserved):
            continue
        edges.setdefault(frm, set()).add(to)
    frontier, seen = [source], {source}
    while frontier:
        current = frontier.pop(0)
        for nxt in edges.get(current, set()):
            if nxt == wanted:
                return True
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return False


def _qa_prior_recovery(results, story_id, *, children=None, product_revision=None,
                       invalid_actor_ids=None, revision_invalidations=None,
                       allow_actionable_checkpoint=True):
    wanted_revision = str(product_revision or "")
    invalid_ids = {str(item) for item in (invalid_actor_ids or [])}
    child_map = dict(children or {})

    def child_for(actor_id):
        return child_map.get(actor_id) or child_map.get(str(actor_id)) or (
            child_map.get(int(actor_id)) if str(actor_id).isdigit() else None)

    for actor_id, payload in reversed(list((results or {}).items())):
        if str(actor_id) in invalid_ids:
            continue
        source_revision = ""
        if child_map:
            child = child_for(actor_id) or {}
            args = (((child.get("memory") or {}).get("context") or {}).get("tool_args") or {})
            # Once a continuation inherited browser state, provenance follows the original source bytes rather
            # than being rewritten to the continuation's current target revision.
            source_revision = str(args.get("_qa_recovery_source_revision")
                                  or args.get("product_revision") or "")
            if (wanted_revision and source_revision != wanted_revision
                    and not _qa_revision_lineage_preserves_story(
                        source_revision, wanted_revision, story_id, revision_invalidations)):
                continue
        if str((payload or {}).get("story")) != str(story_id):
            continue
        result = (payload or {}).get("result") or {}
        raw_bugs = result.get("bugs") or 0
        has_bugs = bool(len(raw_bugs) if isinstance(raw_bugs, (list, tuple, dict, set)) else raw_bugs)
        if has_bugs and not allow_actionable_checkpoint:
            # A dismissed semantic finding does not rewind the browser action that produced it. Its terminal
            # page may be after a consumed queue item, submitted form, or one-shot approval. A skeptical
            # full-story retest must start a coherent journey instead of manufacturing a second finding from
            # an already-consumed transition.
            continue
        coverage = [dict(item) for item in result.get("coverage") or [] if isinstance(item, dict)]
        explicit_text = " ".join(
            str(item.get("aspect") or "").lower() for item in coverage if item.get("explicit"))
        focused_legacy_markers = (
            "minimum valid product state and prerequisites needed for this exact finding",
            "reproduce this exact reported behavior",
            "exercise the reported action (or its intent-equivalent control sequence)",
            "needed to decide this finding",
        )
        # A focused evidence verifier deliberately owns a different, four-step contract from the full story.
        # Its portable browser state remains useful evidence for adjudication, but its coverage ledger must
        # never be grafted onto a normal story continuation. ``recovery_scope`` is authoritative for new
        # results; the title/template markers quarantine already-durable results from older generations and
        # the one historical continuation that inherited such a ledger before this fence existed.
        if (str(result.get("recovery_scope") or "").lower() == "focused"
                or str(result.get("title") or "").lower().startswith("focused reproduction")
                or any(marker in explicit_text for marker in focused_legacy_markers)):
            continue
        state_path = result.get("resume_state_path")
        if not state_path or not Path(state_path).is_file():
            continue
        steps_detail = campaign_checkpoint.compact_evidence_records(result.get("steps_detail") or [])
        coverage, _reopened = campaign_checkpoint.reopen_unproven_coverage(coverage, steps_detail)
        covered = []
        for item in coverage:
            aspect = item.get("aspect") if isinstance(item, dict) else None
            if aspect and item.get("covered") and aspect not in covered:
                covered.append(aspect)
        # Carry the exact ledger as well as its covered subset. Replanning on every process rotation changes
        # aspect wording/cardinality and makes completed evidence appear missing even though portable browser
        # state survived. Explorer validates that the state path exists before trusting either value.
        recovery = {"resume_covered": covered, "resume_coverage": coverage,
                    "resume_state_path": state_path}
        if source_revision or wanted_revision:
            recovery["_qa_recovery_source_revision"] = source_revision or wanted_revision
        # The browser state and coverage ledger say *where* a continuation is, but the sealed receipts say
        # *why* an item is already proved (and let a batch judge combine earlier matrix cases with the next
        # process generation). Without these records, a newly hired continuation sees covered labels without
        # their evidence and replays the same cases. Keep a bounded tail; the explorer performs an additional
        # prompt-size/deduplication pass before presenting it to a model.
        if steps_detail:
            recovery["resume_steps_detail"] = steps_detail
        return recovery
    return {}


def _qa_review_recovery(children, results, review_id, product_revision=None):
    """Return the strongest portable checkpoint for one focused evidence review.

    Review verification is intentionally a smaller contract than normal story QA, so it cannot use
    ``_qa_prior_recovery``.  It still needs the same inseparable browser-state + evidence-ledger handoff:
    without it, every inconclusive review generation restarts at step one and can loop despite having
    already proved most of its focused contract.  Select by retained proven coverage rather than recency so
    a later weak restart cannot overwrite a 3/4 checkpoint with a 1/4 checkpoint.
    """
    wanted_review = str(review_id or "")
    wanted_revision = str(product_revision or "")
    best = None
    for child in dict(children or {}).values():
        if child.get("role") != "qa-explorer":
            continue
        context = ((child.get("memory") or {}).get("context") or {})
        args = context.get("tool_args") or {}
        if str(args.get("_qa_review_id") or "") != wanted_review:
            continue
        # Browser state is evidence about exact product bytes. A checkpoint from before a mutation may help
        # navigation but must not carry assertions into current-revision adjudication.
        if wanted_revision and str(args.get("product_revision") or "") != wanted_revision:
            continue
        payload = (results or {}).get(str(child.get("actor_id")))
        if not isinstance(payload, dict):
            payload = child.get("result") or {}
        result = payload.get("result") if isinstance(payload, dict) else {}
        if not isinstance(result, dict):
            continue
        if str(result.get("recovery_scope") or "").lower() != "focused":
            continue
        state_path = result.get("resume_state_path")
        if not state_path or not Path(state_path).is_file():
            continue
        steps_detail = campaign_checkpoint.compact_evidence_records(result.get("steps_detail") or [])
        coverage = [dict(item) for item in (result.get("coverage") or [])
                    if isinstance(item, dict) and item.get("aspect")]
        if not _qa_seeded_checkpoint_state_is_current(args.get("story") or {}, steps_detail):
            # The portable state is captured at the end of the result. If the result reset storage after its
            # last fixture seed, that state is empty even though earlier receipts legitimately proved setup.
            # Reusing the earlier ledger with the later empty state manufactures "No records" findings.
            continue
        coverage, _reopened = campaign_checkpoint.reopen_unproven_coverage(coverage, steps_detail)
        covered = list(dict.fromkeys(
            str(item.get("aspect")) for item in coverage
            if item.get("covered") and str(item.get("aspect") or "").strip()))
        if not coverage:
            continue
        recovery = {"resume_state_path": state_path, "resume_covered": covered,
                    "resume_coverage": coverage}
        if steps_detail:
            recovery["resume_steps_detail"] = steps_detail
        raw_bugs = result.get("bugs") or 0
        has_bugs = bool(len(raw_bugs) if isinstance(raw_bugs, (list, tuple, dict, set)) else raw_bugs)
        # At equal progress, continue from a state that did not already terminate on a product observation;
        # that finding has its own immutable review path and its post-finding page may be deliberately fenced.
        score = (len(covered), int(not has_bugs), int(child.get("actor_id") or 0),
                 len(steps_detail))
        if best is None or score > best[0]:
            best = (score, recovery)
    return best[1] if best else {}


def _qa_seeded_checkpoint_state_is_current(story, steps_detail):
    """Whether a seeded journey's final browser state occurs after its latest reset and seed.

    Evidence from before a reset remains historical evidence, but it cannot describe the portable browser
    state saved after that reset. The handoff scheduler therefore rejects only that mismatched optimization;
    it does not erase the sealed receipts or convert the story to clean.
    """
    steps = [str(item or "").strip().lower() for item in ((story or {}).get("steps") or [])]
    if not any(re.match(r"^(?:seed|load)\b", step) for step in steps[:3]):
        return True
    last_reset = -1
    last_seed = -1
    for index, row in enumerate(steps_detail or []):
        if not isinstance(row, dict):
            continue
        action = row.get("action")
        if isinstance(action, dict):
            action_text = " ".join(str(action.get(key) or "")
                                   for key in ("cmd", "target_text", "value", "selector"))
        else:
            action_text = str(action or "")
        actual = str(row.get("actual") or "")
        if re.search(r"\breset[_ ]?storage\b", action_text, re.I):
            last_reset = index
        if (re.search(r"\b(?:load|seed)\b", action_text, re.I)
                or re.search(r"\btarget\s*=\s*['\"](?:load|seed)\b", actual, re.I)):
            last_seed = index
    return last_reset < 0 or last_seed > last_reset


# ------------------------------------------------------------------------------ small helpers
def _audit(actor_name, action, decision="executed", payload=None):
    if audit is None:
        return
    try:
        audit.append(actor=f"orchestra:{actor_name}", action=action, resource="runtime",
                     decision=decision, payload=payload or {})
    except Exception:
        pass


def _halted():
    """The per-step kill-switch gate (same scope the store gates creation on)."""
    try:
        h = killswitch.is_halted("orchestra")
        return h if h.get("halted") else None
    except Exception:
        return None


def _extract_json(text):
    """First JSON object out of a model reply; tolerant of ```json fences and prose."""
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        seg = t.split("```")[1]
        t = seg[4:] if seg.lower().startswith("json") else seg
    s, e = t.find("{"), t.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        obj = json.loads(t[s:e + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _ai_json(role, repo, prompt, spawner=None, *, compact=False, codex_model=None,
             reasoning_effort=None):
    """ONE AI decision via factory.agent, parsed to a dict. A hard factory blocker
    (budget exhausted / halted / spawn denied / consent) carries through as {"_blocker": ...}
    so the caller surfaces it as a blocked/escalate event instead of crashing the step."""
    try:
        # These are short structured management decisions, not implementation turns. A provider/tool stall must
        # hand control back to the durable hierarchy promptly instead of pinning the entire meeting for the
        # factory's much larger coding timeout.
        decision_timeout = int(os.environ.get("AOS_ORCHESTRA_DECISION_TIMEOUT_S", "120"))
        r = factory.agent(role, repo, prompt, spawner=spawner, timeout=decision_timeout,
                          compact=compact, codex_model=codex_model,
                          reasoning_effort=reasoning_effort)
    except Exception as e:
        return {"_blocker": f"AI call failed: {e}"}
    if isinstance(r, dict) and r.get("blocker"):
        return {"_blocker": r["blocker"]}
    txt = (r.get("out_full") or r.get("out") or "") if isinstance(r, dict) else str(r)
    return _extract_json(txt) or {}


def _spawn_gate(role):
    """factory.agent's exact spawn-gate semantics, applied to HIRING rows: manifest present ->
    can_spawn must be true (deny reason string returned otherwise); manifest missing/unreadable ->
    fail OPEN with an audit note (infra hiccup must not brick the org, same trade factory makes)."""
    try:
        m = governance.load_manifest(role)
        if not m:
            _audit(role, "SpawnGate", "failopen", {"reason": "manifest missing/unreadable"})
            return None
        if governance.may(role, "spawn"):
            return None
        return (f"role '{role}' is not permitted to spawn sub-agents "
                f"(can_spawn is false in its manifest)")
    except Exception as e:
        _audit(role, "SpawnGate", "failopen", {"error": str(e)[:200]})
        return None


def _merge_context(context, payload):
    """Fold an event payload's context into an actor's memory-context (dict merge; strings noted)."""
    g = payload.get("context", payload)
    if isinstance(g, dict):
        context.update({k: v for k, v in g.items() if not k.startswith("_ev")})
    elif g is not None:
        context.setdefault("notes", []).append(str(g)[:400])


# ================================================================================ org bootstrap
def create_org(tenant_id, vision, repo=".", root_name="Controller", root_role="controller"):
    """Open a run and HIRE its root controller actor (a durable row), handing it the vision as
    its first `task` event. Returns {"run_id", "root_id"} or {"error"}. Kill-switch-gated by the
    store on both the run and the hire."""
    r = store.start_run(tenant_id, vision)
    if r.get("error"):
        return r
    root = store.spawn_actor(r["run_id"], tenant_id, root_name, root_role, kind="controller",
                             assignment=vision, memory={"repo": repo})
    if root.get("error"):
        store.finish_run(r["run_id"], "failed", {"error": root["error"]}, tenant_id=tenant_id)
        return root
    store.emit(r["run_id"], tenant_id, None, root["actor_id"], "task", {"task": vision},
               corr_id=f"run-{r['run_id']}")
    _audit(root_name, "CreateOrg", "executed", {"run_id": r["run_id"], "vision": vision[:160]})
    return {"run_id": r["run_id"], "root_id": root["actor_id"]}


# ================================================================================ one durable step
class _Step:
    """Accumulates one step's outputs so they are persisted in a strict, crash-safe order:
    update actor -> hires -> emits -> only THEN complete the claimed events (at-least-once)."""

    def __init__(self):
        self.emits = []          # (frm, to, kind, payload, corr_id)
        self.status = None       # new actor status (None = unchanged)
        self.result = None       # terminal result dict (set only with a terminal status)
        self.assignment = None   # new assignment text (None = unchanged)
        self.memory = {}         # memory keys to merge
        self.finish_status = None  # root-only run transition, committed with this decide-step


class _StepCheckpoint(RuntimeError):
    """Cooperative stop after decision but before durable commit; the inbox is immediately re-opened."""


def _persist(ctx, a, step, evs):
    # ATOMIC: the actor update, every emit, and the completion of the events we just handled land in ONE
    # transaction (store.persist_step). This is load-bearing for crash-safety: a partial persist here (e.g.
    # actor='done' committed but its 'done' emit not) would strand the supervisor forever. One transaction
    # means a crash before commit persists nothing — the claimed events reappear after their lease and the
    # step re-runs cleanly, with no double-delivery. last_active is bumped inside that same UPDATE.
    live_deadline = (ctx.current_deadline() if callable(getattr(ctx, "current_deadline", None))
                     else getattr(ctx, "deadline", None))
    if (getattr(ctx, "stop", None) is not None and ctx.stop.is_set()) or (
            live_deadline is not None and time.time() >= live_deadline):
        raise _StepCheckpoint("runtime shift ended before durable step commit")
    saved = store.persist_step(ctx.run_id, ctx.tenant, a["actor_id"],
                               status=step.status, assignment=step.assignment,
                               memory=step.memory, result=step.result,
                               emits=step.emits, complete_ids=[ev["id"] for ev in evs],
                               claimed_by=(ctx.claimant() if callable(getattr(ctx, "claimant", None)) else None),
                               finish_status=step.finish_status,
                               stop_requested=(ctx.stop.is_set if getattr(ctx, "stop", None) is not None else None))
    if not saved or saved.get("error"):
        # Never tell the pool a step succeeded when the atomic commit was refused/failed. Its claimed events stay
        # unprocessed and are lease-reclaimable, which is the only lossless outcome (especially on a halt race).
        if (saved or {}).get("checkpoint"):
            raise _StepCheckpoint(saved.get("error"))
        raise RuntimeError(f"durable step persist failed: {(saved or {}).get('error', 'no result')}")
    # NOTE: the fleet is surfaced in the unified pulse view by READING orchestra_actors.last_active
    # (pulse.live() aggregates it) — NOT by a write here. A synchronous pulse write on this hot per-step
    # path added latency that perturbed the timing-sensitive supervisor/sibling race.


def _task_contract(spec):
    """Item 5: every hire carries an explicit task CONTRACT — {objective, output_format, allowed_tools,
    boundaries} — the thing Anthropic found subagents need or they 'duplicate work, leave gaps, or spawn
    excessively'. FAIL-OPEN: a field the coordinator didn't specify is filled with a sensible default (so the
    contract is ALWAYS structurally present and a hire never dies on a missing field); the defaulted fields are
    returned so the caller can JOURNAL under-specification without blocking."""
    given = spec.get("contract") if isinstance(spec.get("contract"), dict) else {}
    task = spec.get("task") or ""
    tool = spec.get("tool")
    defaults = {
        "objective": task or f"deliver the '{spec.get('role', 'assigned')}' work",
        "output_format": "report the concrete result up via a 'done' event (no vague status)",
        "allowed_tools": (tool if tool else "only what your role needs; request more via need_agent"),
        "boundaries": "stay within THIS task; don't do a sibling's job; escalate/disagree rather than drift",
    }
    contract = {k: (given.get(k) or defaults[k]) for k in defaults}
    missing = [k for k in defaults if not given.get(k)]
    return contract, missing


def _hire_key(supervisor_id, corr, spec, index=0):
    """Stable identity for a hire created by a replayable parent event."""
    raw = json.dumps({"supervisor": supervisor_id, "corr": corr or "uncorrelated",
                      "index": int(index), "spec": spec}, sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _hire(ctx, supervisor_id, spec, hire_key=None):
    """One governance-passed hire: a durable child row + its kickoff `task` event (carrying the task CONTRACT).
    Returns the new actor_id (or None on a store refusal, which is journaled)."""
    kind = "supervisor" if spec.get("kind") == "supervisor" else "worker"
    mem = {"repo": ctx.repo}
    contract, missing = _task_contract(spec)
    cblob = dict(spec.get("context") or {})   # a context blob (e.g. a bug handed to a dev-coordinator)
    cblob["contract"] = contract              # the worker reads its boundaries/output-format from context
    if spec.get("tool"):                      # a TOOL-worker: tool + args ride in memory.context so its
        cblob["tool"] = spec["tool"]          # step dispatch-and-parks
        cblob["tool_args"] = spec.get("tool_args") or {}
    mem["context"] = cblob
    spawn = getattr(store, "spawn_actor_once", None)
    if spawn and hire_key:
        child = spawn(ctx.run_id, ctx.tenant, hire_key,
                      spec.get("name") or spec.get("role") or "agent",
                      spec.get("role") or "engineer", kind=kind,
                      supervisor_id=supervisor_id, assignment=spec.get("task"), memory=mem)
    else:
        child = store.spawn_actor(ctx.run_id, ctx.tenant,
                                  spec.get("name") or spec.get("role") or "agent",
                                  spec.get("role") or "engineer", kind=kind,
                                  supervisor_id=supervisor_id, assignment=spec.get("task"), memory=mem)
    if child.get("error"):
        _audit(f"actor:{supervisor_id}", "HireFailed", "error", {"spec": spec, "err": child["error"]})
        return None
    if missing:                               # under-specified hand-off — journaled, NOT blocked (fail-open)
        _audit(f"actor:{supervisor_id}", "TaskContractDefaulted", "warn",
               {"child": child["actor_id"], "role": child["role"], "missing": missing})
    emit = store.emit_once if hire_key and hasattr(store, "emit_once") else store.emit
    emit(ctx.run_id, ctx.tenant, supervisor_id, child["actor_id"], "task",
         {"task": spec.get("task"), "contract": contract},
         corr_id=f"hire-{hire_key}-task" if hire_key else f"spawn-{child['actor_id']}")
    _audit(child["name"], "Hired", "executed",
           {"actor_id": child["actor_id"], "role": child["role"], "kind": kind,
            "supervisor": supervisor_id})
    return child["actor_id"]


# -------------------------------------------------------------------------------- worker step
_WORKER_PROMPT = """You are {name} — an autonomous AI employee in a durable agent org.
IDENTITY: role={role}, actor_id={aid}. TENURE: hired {hired}, decide-step {step} of {maxs}.
ASSIGNMENT: {assignment}
MEMORY/CONTEXT: {context}
RECENT PROGRESS: {progress}

Decide your NEXT action and reply with ONE JSON object:
  {{"action":"continue","note":"what you did this step"}}
  {{"action":"emit","kind":"blocked|finding|question|need_agent|resource_request|process_change|need_context|disagree","payload":{{...}},"note":"..."}}
  {{"action":"finish","result":"the final result","note":"..."}}
Emit 'blocked' the MOMENT you hit a wall you cannot pass alone (e.g. missing API creds); your
supervisor will resolve it and you will resume. Emit 'finding' to propagate a correction. Emit
'need_agent' (payload {{"role","task"}}) if the work needs another hire. Emit 'resource_request'
(payload {{"resource","reason","urgency"}}) if you need a real external capability, credential, infra,
permission, budget, or service provisioned. Emit 'process_change' (payload {{"proposal","reason","impact"}})
if the workflow/SDLC/org process itself should change. Emit 'disagree' (payload {{"reason":"..."}}) if you
professionally believe the ASSIGNMENT ITSELF is wrong, unwise, or harmful — you don't just execute a bad
directive; you object and it goes UP to the CEO to rule on. Reply with JSON only."""


def _worker_step(ctx, a, evs):
    """One durable decide-loop step of an IC. Inbox first (a resolution/correction may unpark or
    re-task it), then at most ONE work AI call, then persist. Ported from actor.py's decide-loop."""
    mem = dict(a["memory"] or {})
    context = dict(mem.get("context") or {})
    progress = list(mem.get("progress") or [])
    steps = int(mem.get("steps") or 0)
    assignment = a["assignment"] or ""
    me, sup = a["actor_id"], a["supervisor_id"]
    step = _Step()
    work = False
    tool_result = None                          # set when our dispatched tool finished (jobrunner emitted it)

    for ev in evs:
        k, p, corr = ev["kind"], (ev["payload"] or {}), ev["corr_id"]
        if k == "task":
            if p.get("task"):
                assignment = p["task"]
                step.assignment = assignment
            work = True
        elif k == "tool_result":                # our dispatched tool finished -> report it up + finish
            tool_result = p
            work = True
        elif k == "next":                       # own continuation token — keep the loop alive
            work = True
        elif k == "resolve":                    # supervisor cleared our blocker -> RESUME
            _merge_context(context, p)
            context["_resolved"] = True
            work = True
        elif k == "context_update":             # propagated correction; resumes a parked actor
            _merge_context(context, p)
            if a["status"] in ("blocked", "parked"):
                work = True
        elif k in ("question", "need_context"):  # a peer asks US -> AI answer back on the corr
            d = _ai_json(a["role"], ctx.repo,
                         f"You are {a['name']} ({a['role']}). A peer asks: {json.dumps(p)[:600]}\n"
                         f"Your context: {json.dumps(context)[:1200]}\n"
                         'Reply ONLY JSON: {"answer":"..."}', spawner=a["role"])
            step.emits.append((me, ev["frm"], "context_update",
                               {"context": {"answer": d.get("answer") or ""}}, corr))
        # other kinds addressed to a worker are informational — completing them records receipt.

    # TOOL-WORKER (agentic-org phase 3b): a worker whose memory carries a `tool` runs REAL long work (a
    # browser QA explore, a dev fix) that must NOT block a lease-bound decide-step. DISPATCH-AND-PARK: hand
    # the job to jobrunner (runs off-loop, browser lives there) and park (blocked). The job emits done+finding
    # to our supervisor and flips us terminal on completion; a crashed job is re-dispatched by reconcile.
    tool = (context or {}).get("tool")
    if work and a["status"] not in TERMINAL and tool_result is not None:
        # A QA slice deadline is a checkpoint, not completed work.  Older behavior marked queued explorers
        # (and partially explored stories) done when cancel_run's cooperative stop returned a tool_result;
        # the resumed campaign then silently skipped those stories.  Keep the durable worker parked with its
        # dispatch handle so the next process can reconcile and run it again.
        _tr = tool_result.get("result") or {}
        _cancel_text = " ".join(str(x or "") for x in (
            _tr.get("stop_reason"), _tr.get("error"), tool_result.get("status"))).lower()
        _retryable_cancel = (tool == "qa_explore" and
                             ("cancel" in _cancel_text or "safety deadline" in _cancel_text))
        # Capacity is an internal scheduling state, never a terminal story outcome.  Keeping the same actor
        # parked prevents the supervisor's incomplete-story recovery path from spawning one gap-fill per
        # queued story when browser/host admission is temporarily full.
        _retryable_checkpoint = (tool in ("qa_explore", "dev_fix", "qa_review")
                                 and bool(_tr.get("checkpoint_required")))
        if _retryable_cancel or _retryable_checkpoint:
            # Preserve partial evidence and the tested ledger before parking. Otherwise every 15-minute hand-off
            # silently threw away bugs already found and restarted the same story from aspect one.
            for f in (tool_result.get("findings") or []):
                if sup:
                    step.emits.append((me, sup, "finding", f, None))
            covered = [c.get("aspect") for c in (_tr.get("coverage") or [])
                       if c.get("covered") and c.get("aspect")]
            partial_steps = list(_tr.get("steps_detail") or [])
            if (_retryable_cancel or tool == "dev_fix") and (
                    covered or partial_steps or _tr.get("resume_state_path")):
                tool_args = dict(context.get("tool_args") or {})
                state_path = _tr.get("resume_state_path")
                if state_path and Path(state_path).is_file():
                    tool_args["resume_state_path"] = state_path
                    if covered:
                        tool_args["resume_covered"] = list(dict.fromkeys(
                            list(tool_args.get("resume_covered") or []) + covered))
                    if _tr.get("coverage"):
                        # Keep the exact ledger across process generations. Replanning can split/merge labels
                        # and turn 70%-complete work back into zero, even though the paired browser state and
                        # evidence remain valid. The explorer independently fences the ledger to this state.
                        tool_args["resume_coverage"] = [dict(item) for item in _tr["coverage"]
                                                        if isinstance(item, dict) and item.get("aspect")]
                else:
                    # Never carry assertions into an empty browser. Losing an optimization is safe; claiming
                    # a fixture still exists when its localStorage did not survive is a false QA result.
                    tool_args.pop("resume_covered", None)
                    tool_args.pop("resume_state_path", None)
                    tool_args.pop("resume_coverage", None)
                tool_args["resume_steps_detail"] = campaign_checkpoint.compact_evidence_records(
                    list(tool_args.get("resume_steps_detail") or []) + partial_steps)
                context["tool_args"] = tool_args
            if tool == "dev_fix" and _tr.get("files"):
                # Preserve the writer's mutation receipt across a verification checkpoint. A successor can
                # prove the live behavior but cannot recreate the predecessor's before-snapshot; dropping this
                # list made revision-impact selection lose the exact blast radius after a safe handoff.
                tool_args = dict(context.get("tool_args") or {})
                carried = [str(item) for item in (tool_args.get("resume_changed_files") or []) if item]
                carried.extend(str(item) for item in (_tr.get("files") or []) if item)
                tool_args["resume_changed_files"] = list(dict.fromkeys(carried))[:200]
                context["tool_args"] = tool_args
            if tool == "dev_fix" and _tr.get("change_diff"):
                # Preserve the exact before/after bytes alongside the file-list fence. Without this, a
                # successor impact reviewer can mistake unrelated pre-existing controls in a shared file for
                # behavior introduced by the repair and schedule unnecessary story replays.
                tool_args = dict(context.get("tool_args") or {})
                tool_args["resume_change_diff"] = str(_tr["change_diff"])[:14000]
                context["tool_args"] = tool_args
            if tool == "dev_fix" and isinstance(_tr.get("resume_triage_finding"), dict):
                # The focused browser already sealed this exact current-revision residual. Resume its
                # independent repository adjudication directly; replaying the browser adds no evidence.
                tool_args = dict(context.get("tool_args") or {})
                tool_args["resume_triage_finding"] = dict(_tr["resume_triage_finding"])
                context["tool_args"] = tool_args
            if (tool == "dev_fix" and isinstance(_tr.get("triage"), dict)
                    and _tr["triage"].get("disposition") == "confirmed_defect"
                    and (_tr.get("files") or (context.get("tool_args") or {}).get(
                        "resume_changed_files"))):
                # The two sealed finding-time reviewers already authorized the writer. Bind their complete
                # receipt to the exact finding so later partial browser checkpoints can resume read-only
                # recovery verification without paying for the same review again. A fresh residual still
                # crosses its own independent gate inside dev_loop.
                tool_args = dict(context.get("tool_args") or {})
                tool_args["resume_triage_receipt"] = dict(_tr["triage"])
                context["tool_args"] = tool_args
            context["tool_attempt"] = int(context.get("tool_attempt") or 0) + 1
            step.status = "blocked"
            checkpoint_reason = ((_tr.get("verdict") or {}).get("reason") or _cancel_text)[:240]
            step.result = {"checkpointed": True, "reason": checkpoint_reason,
                           "partial_result": _tr}
            step.memory = {"context": context, "progress": progress, "steps": steps}
            # This branch used to return before _persist(), so the cancellation event was never acknowledged
            # and the actor update was never committed.  Every fresh QA slice reclaimed the same leased events,
            # producing an endless 7/14-looking campaign with no runnable work.  Persisting is the hand-off:
            # event consumed once, actor remains resumable/blocked, reconcile can dispatch it on the next slice.
            _persist(ctx, a, step, evs)
            return step
        # the dispatched tool finished: report its findings + a done up to the supervisor, then FINISH. Only
        # the pool (here) writes this actor's row — the job thread merely emitted the tool_result event.
        _findings = tool_result.get("findings") or []
        for f in _findings:
            if sup:
                step.emits.append((me, sup, "finding", f, None))
        if sup and tool == "dev_fix" and _tr.get("internal_review_required"):
            original_bug = ((context.get("tool_args") or {}).get("bug") or {})
            # A confirmed finding may already have a terminal management disposition when recovery QA raises
            # a *different* residual observation. Reusing the original finding here reuses its review ID; the
            # coordinator then (correctly) compacts that terminal case and can strand the story in
            # ``internal_review`` forever. Route the newest residual as its own immutable evidence question.
            residuals = [item for item in (_tr.get("residual") or []) if isinstance(item, dict)]
            review_finding = dict(residuals[-1] if residuals else original_bug)
            # Focused recovery stories often replace ``story`` with a generated title. Management ownership
            # and manifest lookup must stay on the canonical source ID carried by the adjudicated finding.
            if original_bug.get("story"):
                review_finding["story"] = original_bug.get("story")
            if residuals:
                review_finding["_qa_parent_finding_id"] = original_bug.get("finding_id")
                review_finding["_qa_parent_adjudication"] = original_bug.get("_qa_adjudication")
            step.emits.append((me, sup, "internal_review_required", {
                "route": "qa-internal-management", "state": "pending",
                "story": review_finding.get("story"),
                "finding": review_finding,
                "triage": _tr.get("triage"),
                "reason": (_tr.get("verdict") or {}).get("reason"),
            }, None))
        if sup:
            # the done carries the story + whether a BLOCKING bug was found, so the qa-coordinator can track
            # per-story status (and thus give an honest verdict + drive the re-test loop) from the done alone.
            step.emits.append((me, sup, "done", {"task": assignment, "tool": tool_result.get("tool"),
                               "status": tool_result.get("status"), "result": tool_result.get("result"),
                               "story": (tool_result.get("result") or {}).get("story"),
                               "findings_count": len(_findings),
                               "blocking_found": any(f.get("blocking") for f in _findings)}, None))
        step.status = "done"
        step.result = {"tool": tool_result.get("tool"), "status": tool_result.get("status"),
                       "result": tool_result.get("result")}
    elif work and a["status"] not in TERMINAL and tool and not context.get("tool_dispatched"):
        try:
            import jobrunner
            jobrunner.dispatch({"run_id": ctx.run_id, "tenant": ctx.tenant, "actor_id": me,
                                "supervisor_id": sup, "actor_name": a["name"], "tool": tool,
                                "args": context.get("tool_args") or {}, "assignment": assignment,
                                "attempt": int(context.get("tool_attempt") or 0)}, store)
            context["tool_dispatched"] = True
            step.status = "blocked"                 # PARK; memory (tool + tool_dispatched) is the resume handle
            _audit(a["name"], "ToolDispatched", "executed", {"tool": tool})
        except Exception as e:                      # dispatch failed -> escalate, don't silently hang
            step.status = "blocked"
            if sup:
                step.emits.append((me, sup, "blocked", {"reason": f"tool dispatch failed: {e}"}, None))
    elif work and a["status"] not in TERMINAL and tool and context.get("tool_dispatched"):
        pass                                        # dispatched; waiting on the job to emit done (no work call)
    elif work and a["status"] not in TERMINAL:
        steps += 1
        if steps > MAX_ACTOR_STEPS:
            step.status, step.result = "dead", {"failed": True, "reason": "max steps exhausted"}
            if sup:
                step.emits.append((me, sup, "done", {"failed": True, "reason": "max steps"}, None))
        else:
            d = _ai_json(a["role"], ctx.repo, _WORKER_PROMPT.format(
                name=a["name"], role=a["role"], aid=me, hired=a["hired_at"], step=steps,
                maxs=MAX_ACTOR_STEPS, assignment=assignment,
                context=json.dumps(context)[:1800], progress="; ".join(progress[-6:]) or "(none)"),
                spawner=ctx.spawner_role(a))
            if d.get("_blocker"):               # a factory gate refused (budget/halt/…) -> park
                d = {"action": "emit", "kind": "blocked", "payload": {"reason": d["_blocker"]}}
            action = (d.get("action") or "continue").lower()
            if action == "finish":
                res = d.get("result") or d.get("note") or "done"
                step.status, step.result = "done", {"result": res}
                if sup:
                    step.emits.append((me, sup, "done", {"task": assignment, "result": res}, None))
            elif action == "emit":
                kind = d.get("kind") if d.get("kind") in store.KINDS else "finding"
                payload = dict(d.get("payload") or {})
                if d.get("note") and "note" not in payload:
                    payload["note"] = d["note"]
                if sup:
                    step.emits.append((me, sup, kind, payload, f"ev-{me}-{steps}"))
                if kind in ("blocked", "disagree"):
                    step.status = "blocked"     # PARK; memory is the resume handle. A disagreement parks the
                    _audit(a["name"], "WorkerBlocked" if kind == "blocked" else "WorkerDisagree",
                           "blocked", payload)   # agent until the CEO rules (proceed / revise the directive).
                else:
                    step.emits.append((me, me, "next", {}, None))   # keep working after a finding
                    step.status = "working"
            else:                               # continue
                progress.append(d.get("note") or f"step {steps}")
                step.emits.append((me, me, "next", {}, None))
                step.status = "working"

    step.memory = {"context": context, "progress": progress[-30:], "steps": steps}
    _persist(ctx, a, step, evs)


# ----------------------------------------------------------------------------- supervisor step
_DECOMPOSE_PROMPT = (
    "You are a LEAD decomposing a task for your team in a recursive, elastic agent-org. Decide "
    "(a) the INDEPENDENT subtasks, (b) HOW MANY children to staff, and (c) for each child whether "
    "it is a single IC ('worker') or, if its subtask is itself broad enough to need its OWN team, "
    "a sub-lead ('supervisor'). Bias toward EXPANDING structure when the scope is large (cost is "
    "not a constraint). Reply ONLY JSON:\n"
    '{{"org_note":"...", "children":[{{"role":"<role>","kind":"worker|supervisor","task":"..."}}]}}\n'
    "TASK:\n{task}")

_DECIDE_PROMPT = (
    "You are an interrupt-driven LEAD. A child just emitted an event. Decide ONE action and reply "
    'ONLY JSON. RESOLVE locally when you can: "unblock"/"rebrief" (send guidance to the child; '
    'include "message"), "spawn_helper" (staff a new agent; include "spec":{{"role","task"}}), '
    '"hand_next" (give the child its next task; include "task"), "broadcast" (a correction/context '
    'EVERY sibling must get; include "message"), "ack" (note it, no action). ESCALATE '
    '("action":"escalate", include "reason") ONLY when the blocker is beyond your capability '
    "(needs a capability enabled, a sub-fleet restart, or a human). "
    "EVENT: kind={kind} from={frm} payload={payload}")

_AGGREGATE_PROMPT = (
    "You are the LEAD reporting UP to your boss (ultimately the CEO). Your team's results are below. Write a "
    "SUBSTANTIVE executive synthesis a CEO can act on — NOT a status line. Lead with the bottom line / "
    "recommendation, then the 3-6 key findings that support it, then any risks or unresolved items. Be "
    "concrete: pull the actual numbers, names, and conclusions from your team's work; never reply with an "
    "empty or vague result. 4-10 sentences.\n"
    'Reply ONLY JSON: {{"result":"<the executive synthesis>", "ok":true}}. '
    "TEAM RESULTS: {results}\nUNRESOLVED ESCALATIONS: {escalations}")

_CONTROLLER_PROMPT = (
    "You are the CONTROLLER — the TOP escalation tier of a recursive agent-org. A supervisor "
    "escalated a blocker beyond its capability. Decide how to CLEAR it: enable a capability / "
    "grant creds / restart a sub-fleet, or CONSULT THE HUMAN if it truly needs a person. Reply "
    'ONLY JSON: {{"action":"resolve"|"consult_human","grant":"<capability or creds to hand back>",'
    '"message":"<what to tell the team / ask the human>"}}. ESCALATION: {payload}')


def _coordinator_specs(ctx, a, task, role):
    """QA/dev COORDINATORS spawn TOOL-workers DETERMINISTICALLY (not an AI decompose): one qa-explorer per
    story, or one dev-fixer per bug. The run params (vision/target_url/token/org/stories/bug) ride in the
    coordinator's memory.context, set by the agentic entrypoint (qa_run agentic=True). Returns [] if there is
    nothing to spawn (e.g. dev-coordinator with no bug yet) so the caller falls back to the generic path."""
    c = dict((a.get("memory") or {}).get("context") or {})
    if role == "qa-coordinator":
        stories = list(c.get("stories") or [])
        batch_size = max(0, int(c.get("story_batch_size") or 0))
        active_stories = campaign_checkpoint.story_window(
            stories, batch_size, (a.get("memory") or {}).get("story_status"))["active_stories"]
        base = {"target_url": c.get("target_url"), "vision": c.get("vision") or task,
                "token": c.get("token"), "org": c.get("org", "0"), "artifact_dir": c.get("artifact_dir"),
                "max_steps": c.get("max_steps"), "product": c.get("product"), "repo": c.get("repo"),
                "product_revision": c.get("product_revision")}
        return [{"name": f"{a['name']}.explorer{i}", "role": "qa-explorer", "kind": "worker",
                 "task": f"QA-explore story: {(s.get('title') or s.get('id') or i)}",
                 "tool": "qa_explore", "tool_args": {**base, "story": s}}
                for i, s in enumerate(active_stories)]
    if role == "dev-coordinator":
        bug = c.get("bug")
        if not bug:
            return []
        return [{"name": f"{a['name']}.fixer", "role": "dev-fixer", "kind": "worker",
                 "task": f"Fix: {bug.get('title') or bug.get('bug') or 'defect'}",
                 "tool": "dev_fix", "tool_args": {"bug": bug, "vision": c.get("vision"), "repo": c.get("repo"),
                    "product": c.get("product"),
                    "target_url": c.get("target_url"), "stories": c.get("stories"),
                    "max_steps": c.get("max_steps"),
                    "restart_cmd": c.get("restart_cmd"), "health_url": c.get("health_url"),
                    "token": c.get("token"), "org": c.get("org")}}]
    if role == "research-coordinator" and c.get("question") and not c.get("items"):
        # RESEARCH-as-a-durable-org path ONLY (context carries a `question`, no pre-set `items`). A generic
        # tool-team coordinator that happens to be named "research-coordinator" (context {tool, items}) must
        # fall through to the generic branch below — don't hijack it into research decomposition.
        # RESEARCH as a durable org: the coordinator DECOMPOSES the question (research_fleet's proven splitter)
        # and spawns one research_subq tool-worker per sub-question. Each is dispatch-and-parked (crash-
        # reclaimable) and writes the CONTRACT finding (findings/NN.md); run_research_via_org synthesizes
        # REPORT.md after the org completes. Mirrors the qa-coordinator's deterministic tool-team spawn.
        import research_fleet
        repo = c.get("repo") or ctx.repo
        question = c.get("question") or task
        subqs = research_fleet.decompose(Path(repo), question) or [question]   # >=1 -> honest empty never fabricated
        return [{"name": f"{a['name']}.r{i}", "role": "research-growth", "kind": "worker", "task": sq,
                 "tool": "research_subq",
                 "tool_args": {"idx": i, "subq": sq, "repo": str(repo),
                               "tenant": c.get("tenant"), "org": c.get("org")}}
                for i, sq in enumerate(subqs)]
    # COMPANY / CEO coordinator: context.functions = [{role, tool, items, worker_role, task}] -> spawn one
    # SUPERVISOR (a FUNCTION coordinator) per function, each carrying its own tool-team context. This is the
    # top of a full CEO-directed org: CEO-coordinator -> function coordinators -> tool-workers -> reports up.
    funcs = c.get("functions")
    if isinstance(funcs, list) and funcs:
        return [{"name": f.get("role") or f"function-{i}", "role": f.get("role") or f"function-{i}",
                 "kind": "supervisor",
                 "task": f.get("task") or f"Deliver the '{f.get('role', 'function')}' function toward: {task}",
                 "context": {"tool": f.get("tool"), "items": f.get("items") or [],
                             "worker_role": f.get("worker_role")}}
                for i, f in enumerate(funcs)]

    # GENERIC TOOL-TEAM: any coordinator whose context declares a `tool` + a list of `items` spawns one
    # tool-worker per item — this is how the org staffs ANY function (research, finance, legal, data, …) with
    # REAL work, reusing the proven tool-worker/dispatch-and-park pattern instead of a per-role branch.
    tool, items = c.get("tool"), c.get("items")
    if tool and isinstance(items, list) and items:
        worker_role = c.get("worker_role") or (role.replace("-coordinator", "").replace("-lead", "") or "worker")
        return [{"name": f"{a['name']}.w{i}", "role": worker_role, "kind": "worker",
                 "task": (it.get("task") or it.get("topic") or str(it)) if isinstance(it, dict) else str(it),
                 "tool": tool, "tool_args": (dict(it) if isinstance(it, dict) else {"task": str(it)})}
                for i, it in enumerate(items)]
    return []


def _decompose_specs(ctx, a, task):
    """The org-shape AI call. The root controller plans DOMAINS via org_decider.plan_org (one
    supervisor per domain — recursive from there); QA/dev coordinators spawn tool-workers deterministically;
    any other lead splits its task via AI into worker / sub-supervisor child specs. A QA coordinator may
    return [] only when its carried manifest already has a durable status for every story; that is a report
    join, not a request for generic decomposition. Other parse misses degrade to a single IC."""
    if a["kind"] == "controller":
        plan = org_decider.plan_org(task)
        return [{"name": f"{d['name']}-lead", "role": d.get("supervisor") or "supervisor",
                 "kind": "supervisor",
                 "task": f"Deliver the '{d['name']}' domain toward the vision: {task}"}
                for d in plan["root"]["children"]]
    specs = _coordinator_specs(ctx, a, task, (a.get("role") or "").lower())
    if specs:
        return specs
    if (a.get("role") or "").lower() == "qa-coordinator":
        memory = dict(a.get("memory") or {})
        context = dict(memory.get("context") or {})
        planned = {
            str(story.get("id") or story.get("title")) for story in (context.get("stories") or [])
            if story.get("id") or story.get("title")}
        settled = {str(key) for key in (memory.get("story_status") or {})}
        if planned and planned.issubset(settled):
            return []
    d = _ai_json(a["role"], ctx.repo, _DECOMPOSE_PROMPT.format(task=task), spawner=a["role"])
    specs = []
    for i, c in enumerate(d.get("children") or []):
        if isinstance(c, dict) and c.get("task"):
            specs.append({"name": f"{a['name']}.c{i}", "role": c.get("role") or a["role"],
                          "kind": "supervisor" if c.get("kind") == "supervisor" else "worker",
                          "task": c["task"]})
    return specs or [{"name": f"{a['name']}.c0", "role": a["role"], "kind": "worker", "task": task}]


# Fan-out ≈ 15× the tokens of a single call (Anthropic; token use explains ~80% of perf variance). Item 7:
# fanning out is a COST decision, only justified for breadth-first, independently-parallelizable work. We don't
# hard-cap (that would kill legitimate breadth-first fan-out; governance + MAX_ACTOR_STEPS are the hard
# backstops) — we make a WIDE fan-out VISIBLE (nothing fails invisibly) so a runaway can't happen silently.
_FANOUT_WARN = int(os.environ.get("AOS_FANOUT_WARN", "8"))


def _fanout_gate(a, specs):
    """Cost/value VISIBILITY gate on a fan-out: journal width + the ~15× token-cost signal when a single hire
    batch is unusually wide, so an over-eager coordinator is observable. Returns the width. Never blocks."""
    width = len(specs)
    if width >= _FANOUT_WARN:
        _audit(a["name"], "WideFanout", "warn",
               {"width": width, "est_cost_x": "~15x/agent vs single call",
                "guidance": "justified only for breadth-first parallelizable work; single-thread decision-coupled work"})
    return width


def _hire_or_request(ctx, a, specs, step, corr=None):
    """Governance-gated hiring. Allowed -> hire directly. Denied (manifest role without
    can_spawn) -> file a hire REQUEST up the tree as `need_agent` (only the controller spawns).
    Returns 'hired' | 'requested' | 'denied' (top-of-tree denial = hard failure)."""
    _fanout_gate(a, specs)
    deny = _spawn_gate(a["role"])
    if deny is None:
        for index, spec in enumerate(specs):
            _hire(ctx, a["actor_id"], spec,
                  hire_key=_hire_key(a["actor_id"], corr, spec, index))
        return "hired"
    _audit(a["name"], "SpawnDenied", "denied", {"reason": deny, "specs": len(specs)})
    if a["supervisor_id"]:
        step.emits.append((a["actor_id"], a["supervisor_id"], "need_agent",
                           {"specs": specs, "for": a["actor_id"], "reason": deny}, corr))
        return "requested"
    return "denied"


def _supervisor_step(ctx, a, evs):
    """One durable step of a lead/controller: interrupt-driven handling of whatever landed in
    its inbox, then the aggregate check. Ported from supervisor.Supervisor + orchestra.Controller."""
    mem = dict(a["memory"] or {})
    phase = mem.get("phase") or "new"
    results = dict(mem.get("results") or {})
    if (a.get("role") or "").lower() == "qa-coordinator" and mem.get("pending_dev_findings"):
        mem["pending_dev_findings"] = _qa_compact_pending_findings(mem["pending_dev_findings"])
    # Reconcile process-only capability reviews from durable prior results on every resumed controller
    # generation. This closes the gap where the restoring child finished just before a rolling handoff.
    for prior_payload in results.values():
        if isinstance(prior_payload, dict) and prior_payload.get("story") is not None:
            _qa_clear_restored_capability_reviews(
                mem, prior_payload.get("story"), prior_payload.get("result") or {})
    handled = list(mem.get("handled") or [])
    escalations = list(mem.get("escalations") or [])
    review_states = dict(mem.get("internal_review_states") or {})
    review_observations = dict(mem.get("review_observations") or {})
    review_followers = {str(key): list(value or []) for key, value in
                        dict(mem.get("review_verification_followers") or {}).items()}
    finding_resolutions = list(mem.get("finding_resolutions") or [])
    if (a.get("role") or "").lower() == "qa-coordinator":
        mem["pending_story_continuations"] = _qa_compact_story_continuations(
            mem.get("pending_story_continuations"), mem.get("story_status"))
        mem["internal_reviews"], review_states = _qa_compact_resolved_internal_reviews(
            mem.get("internal_reviews"), review_states, finding_resolutions)
        _qa_repair_orphan_internal_review_statuses(mem)
    blocked_child = mem.get("blocked_child")
    me, tid = a["actor_id"], ctx.tenant
    is_top = a["supervisor_id"] is None
    step = _Step()

    children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                if c["supervisor_id"] == me}

    def _live_children():
        return [cid for cid, c in children.items() if c["status"] not in TERMINAL]

    def _thread_context():
        """Best-effort chat/request routing context carried by coordinators. Older callers may not pass it;
        in that case agent_request still creates a durable tenant-level ask, just not attached to a chat."""
        c = dict(mem.get("context") or {})
        return c.get("thread_id") or c.get("controller_thread_id"), c.get("org") or a.get("org_id")

    def _open_human_request(kind, frm, payload):
        """Top-tier resource/process escalation: create a durable human-visible request instead of fabricating
        a text 'grant'. The child remains parked until an external answer/resolution path resumes it."""
        try:
            import agent_request
            thread_id, org_id = _thread_context()
            title = "Resource needed" if kind == "resource_request" else "Process change proposed"
            body = payload.get("reason") or payload.get("proposal") or payload.get("resource") or payload
            question = (f"{title} from actor {frm}: {json.dumps(body, default=str)[:900]}\n\n"
                        f"Full payload: {json.dumps(payload, default=str)[:1600]}")
            req_kind = "resource" if kind == "resource_request" else "process_change"
            req = agent_request.ask(tid, question, kind=req_kind, org_id=org_id, thread_id=thread_id)
            escalations.append({"frm": frm, "kind": kind, "request_id": req.get("request_id"),
                                "status": "waiting_for_human"})
            handled.append({"frm": frm, "kind": kind, "action": "human_request_opened",
                            "request_id": req.get("request_id"), "outstanding": _live_children()})
            _audit(a["name"], "HumanRequestOpened", "blocked",
                   {"kind": kind, "from": frm, "request_id": req.get("request_id")})
            return True
        except Exception as e:
            escalations.append({"frm": frm, "kind": kind, "error": f"human request failed: {e}"})
            handled.append({"frm": frm, "kind": kind, "action": "human_request_failed",
                            "error": str(e)[:200], "outstanding": _live_children()})
            _audit(a["name"], "HumanRequestFailed", "error",
                   {"kind": kind, "from": frm, "error": str(e)[:200]})
            return False

    def _broadcast(payload, note, corr_id=None):
        """Correct EVERY live child (the parked one last, so its resume lands after siblings)."""
        order = sorted(_live_children(), key=lambda cid: cid == blocked_child)
        for cid in order:
            step.emits.append((me, cid, "context_update", {"context": payload, "note": note}, corr_id))

    def _active_dev_coordinators():
        return [c for c in children.values()
                if c.get("role") == "dev-coordinator" and c.get("status") not in TERMINAL]

    def _review_record(review_id):
        return next((r for r in (mem.get("internal_reviews") or [])
                     if r.get("review_id") == review_id), None)

    def _child_tool_args(child):
        return ((((child or {}).get("memory") or {}).get("context") or {}).get("tool_args") or {})

    def _active_review_child(review_id, role):
        """Return the one live/unreduced child already working this immutable review case."""
        for child in children.values():
            if child.get("role") != role:
                continue
            args = _child_tool_args(child)
            child_review_id = (args.get("_qa_review_id") if role == "qa-explorer"
                               else (args.get("internal_review") or {}).get("review_id"))
            if str(child_review_id or "") != str(review_id or ""):
                continue
            # A terminal child whose done event has not yet been reduced is still authoritative work in
            # flight. Scheduling its successor first races the result reducer and duplicates the browser or
            # steals the review lease.
            if child.get("status") not in TERMINAL or str(child.get("actor_id")) not in results:
                return child
        return None

    def _prior_clean_review_result(review_id):
        """Return the newest complete bug-free focused result already reduced for this exact review."""
        for child in reversed(list(children.values())):
            args = _child_tool_args(child)
            if (child.get("role") != "qa-explorer"
                    or str(args.get("_qa_review_id") or "") != str(review_id or "")):
                continue
            payload = results.get(str(child.get("actor_id"))) or child.get("result") or {}
            candidate = payload.get("result") if isinstance(payload, dict) else {}
            candidate = candidate if isinstance(candidate, dict) else {}
            if campaign_checkpoint.result_evidence_complete(candidate):
                return candidate
        return None

    def _record_finding_resolution(record, disposition, reason, *, case_id=None):
        item = {"review_id": record.get("review_id"), "case_id": case_id,
                "finding_id": (record.get("finding") or {}).get("finding_id"),
                "story": record.get("story") or (record.get("finding") or {}).get("story"),
                "title": ((record.get("finding") or {}).get("title")
                          or (record.get("finding") or {}).get("bug")),
                "disposition": disposition, "reason": str(reason or "")[:1000]}
        key = (item["review_id"], item["disposition"])
        if not any((r.get("review_id"), r.get("disposition")) == key for r in finding_resolutions):
            finding_resolutions.append(item)
            step.emits.append((me, me, "finding_resolution", item,
                               f"qa-resolution:{item['review_id']}:{disposition}"))
        return item

    def _remove_internal_review(review_id):
        mem["internal_reviews"] = [r for r in (mem.get("internal_reviews") or [])
                                   if r.get("review_id") != review_id]

    def _cancel_review_verification(review_id, disposition):
        """Stop only focused browsers made redundant by this exact terminal review."""
        cancelled = []
        try:
            import jobrunner
            for child in children.values():
                args = _child_tool_args(child)
                if (child.get("role") == "qa-explorer"
                        and child.get("status") not in TERMINAL
                        and str(args.get("_qa_review_id") or "") == str(review_id)):
                    count = jobrunner.cancel_actor_job(
                        ctx.run_id, tid, int(child["actor_id"]),
                        reason=f"QA review {review_id} reached {disposition}")
                    if count:
                        cancelled.append(int(child["actor_id"]))
        except Exception:
            pass
        return cancelled

    def _schedule_qa_review(record, *, state=None, corr_id=None):
        """Hire one crash-idempotent read-only management chain for a stable review/state generation."""
        nonlocal children
        rid = record["review_id"]
        rs = dict(review_states.get(rid) or {})
        finding = record.get("finding") or {}
        if _qa_internal_review_route(record) == "performance_management":
            # This is the coordinator's own process-health observation, not a historical product defect.
            # Sending it to the evidence-dispute reviewer asks for finding-time repository provenance that
            # cannot exist, then burns two focused-browser attempts before reaching the manager that can
            # actually rebrief/repair the stalled lane. Escalate the durable progress ledger directly to the
            # senior QA manager; its recovery message preserves evidence and queues a continuation.
            try:
                import management
                import qareview
                evidence = {
                    "review_id": rid,
                    "status": "manager_review",
                    "run_id": ctx.run_id,
                    "coordinator_actor_id": me,
                    "story": record.get("story") or finding.get("story"),
                    "finding": finding,
                    "progress": ((state or {}).get("progress") if isinstance(state, dict) else None)
                                or record.get("triage") or {},
                }
                signaled = management.signal(
                    qareview.management_case_key(tid, rid),
                    f"QA subordinate performance needs management action: {rid}",
                    _QA_PERFORMANCE_MANAGEMENT_TRIGGER,
                    evidence,
                    tenant_id=tid, work_id=f"orchestra:{ctx.run_id}:{rid}",
                    worker="qa-manager", manager_role="senior-qa-director")
                rs.update({"status": "manager_attention",
                           "case_id": (signaled or {}).get("case_id"),
                           "state": state or rs.get("state")})
                review_states[rid] = rs
                return True
            except Exception as exc:
                rs.update({"status": "schedule_denied", "state": state or rs.get("state"),
                           "reason": f"performance management signal failed: {str(exc)[:300]}"})
                review_states[rid] = rs
                _audit(a["name"], "QaPerformanceManagement", "signal_failed",
                       {"review_id": rid, "error": str(exc)[:300]})
                return False
        active = _active_review_child(rid, "qa-evidence-reviewer")
        if active:
            if state is not None:
                # Coalesce repeated manager updates to the newest state. The active lease holder finishes (or
                # fences out), then its reducer starts exactly one reviewer for this pending generation.
                rs["pending_review_state"] = state
            rs.update({"status": "review_inflight", "review_actor_id": active.get("actor_id")})
            review_states[rid] = rs
            return True
        rs["review_attempts"] = int(rs.get("review_attempts") or 0) + 1
        if state is not None:
            rs["state"] = state
        cc = dict((a.get("memory") or {}).get("context") or {})
        story_id = str(record.get("story") or (record.get("finding") or {}).get("story") or "")
        source_story = next((item for item in (cc.get("stories") or [])
                             if str(item.get("id") or item.get("title")) == story_id), None)
        if source_story:
            # A QA finding's model-written ``expected`` is an allegation, not permission to expand the
            # acceptance contract. Seal the original story into this review generation so internal managers
            # can distinguish a real mismatch from tester overreach without asking the CEO or mutating code.
            review_state = dict(rs.get("state") or {})
            review_state["authoritative_story_contract"] = {
                key: source_story.get(key) for key in (
                    "id", "title", "goal", "persona", "category", "steps", "expected",
                    "expected_outcome", "acceptance_criteria")
                if source_story.get(key) is not None
            }
            review_state["contract_authority"] = (
                "The source story defines required scope. Finding expected/detail fields are disputed "
                "observations and cannot add requirements absent from this source story.")
            rs["state"] = review_state
        review_states[rid] = rs
        suffix = hashlib.sha256(json.dumps(rs.get("state") or {}, sort_keys=True,
                                          default=str).encode()).hexdigest()[:8]
        outcome = _hire_or_request(ctx, a, [{
            "name": f"{a['name']}.evidence-review-{rid[-8:]}-{suffix}",
            "role": "qa-evidence-reviewer", "kind": "worker",
            "task": f"Adjudicate disputed QA evidence for {record.get('story') or rid}",
            "tool": "qa_review", "tool_args": {
                "internal_review": record, "state": rs.get("state"),
                "repo": cc.get("repo") or ctx.repo, "thread_id": cc.get("thread_id"),
                "product": cc.get("product"),
                "coordinator_actor_id": me,
                "org": cc.get("org"), "work_ref": f"orchestra:{ctx.run_id}:{rid}",
            }}], step, f"qa-review:{rid}:{suffix}")
        rs["status"] = "review_scheduled" if outcome in ("hired", "requested") else "schedule_denied"
        children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                    if c["supervisor_id"] == me}
        return outcome in ("hired", "requested")

    def _schedule_review_verification(record, *, case_id=None, corr_id=None,
                                      manager_authorized=False):
        """Collect fresh independent browser evidence instead of replaying an unchanged uncertain meeting."""
        nonlocal children
        rid = record["review_id"]
        rs = dict(review_states.get(rid) or {})
        prior_clean = _prior_clean_review_result(rid)
        if prior_clean:
            # Management recovery may arrive after a newer verifier merely lost browser/AT capacity. The
            # immutable review already owns stronger complete evidence; reduce it now instead of launching a
            # weaker retry that can overwrite the review state and loop forever.
            _complete_review_verification(rid, prior_clean, corr_id=corr_id)
            return True
        active = _active_review_child(rid, "qa-explorer")
        if active:
            rs.update({"status": "verification_inflight", "case_id": case_id or rs.get("case_id"),
                       "verification_actor_id": active.get("actor_id")})
            review_states[rid] = rs
            return True
        prior_browser_result = ((rs.get("state") or {}).get("fresh_result")
                                if isinstance(rs.get("state"), dict) else None)
        durable_grounded = _qa_grounded_actionable_marker_is_current(
            rs.get("grounded_actionable_verification"), mem.get("coverage_revision"))
        state_grounded = _qa_has_grounded_actionable_browser_receipt(prior_browser_result)
        if durable_grounded or state_grounded:
            # A manager may extend a genuinely incomplete/failed collection regardless of elapsed time. But
            # replaying an already-grounded mismatch cannot answer an adjudicator that failed to consume its
            # receipt; it only spends another browser/model shift and creates another volatile state digest.
            # This applies to ordinary reviewer requests too: uncertainty after this semantic terminal belongs
            # to adjudication/repair. A changed product revision removes the fence and permits post-fix proof.
            rs.update({"status": "manager_attention", "case_id": case_id or rs.get("case_id"),
                       "reason": ("grounded actionable browser evidence already exists; repair or re-run "
                                  "adjudication instead of reproducing the same observation")})
            review_states[rid] = rs
            _audit(a["name"], "QaEvidenceRecovery", "duplicate_reproduction_suppressed", {
                "review_id": rid, "case_id": case_id,
                "verification_attempts": int(rs.get("verification_attempts") or 0)})
            return False
        attempt = int(rs.get("verification_attempts") or 0) + 1

        def signal_management():
            import management
            import qareview
            finding = record.get("finding") or {}
            performance_stall = finding.get("kind") == "qa_performance_stall"
            evidence = {"review_id": rid, "case_id": case_id, "status": "manager_review",
                        "state_generation": ((rs.get("last_outcome") or {}).get("state_generation") or 1)}
            if performance_stall:
                evidence.update({"run_id": ctx.run_id, "coordinator_actor_id": me,
                                 "story": record.get("story") or finding.get("story"),
                                 "verification_attempts": int(rs.get("verification_attempts") or 0),
                                 "last_verification": (rs.get("state") or {}).get("fresh_result") or {},
                                 "finding": finding})
            if case_id:
                try:
                    evidence.update(qareview.get(tid, case_id))
                except Exception:
                    pass
            management.signal(
                qareview.management_case_key(tid, rid),
                f"QA evidence dispute needs management action: {rid}",
                (_QA_PERFORMANCE_MANAGEMENT_TRIGGER if performance_stall
                 else qareview.MANAGEMENT_TRIGGER),
                (evidence if performance_stall else qareview.management_case_state(evidence)),
                tenant_id=tid, work_id=f"orchestra:{ctx.run_id}:{rid}", worker="qa-manager",
                manager_role="senior-qa-director")

        if attempt > 2 and not manager_authorized:
            rs.update({"status": "manager_attention", "case_id": case_id})
            review_states[rid] = rs
            try:
                signal_management()
            except Exception:
                pass
            return False
        cc = dict((a.get("memory") or {}).get("context") or {})
        story_id = str(record.get("story") or (record.get("finding") or {}).get("story") or "")
        sobj = next((s for s in (cc.get("stories") or [])
                     if str(s.get("id") or s.get("title")) == story_id), None)
        if sobj is None:
            rs.update({"status": "manager_attention", "case_id": case_id,
                       "reason": "originating story unavailable for fresh verification"})
            review_states[rid] = rs
            try:
                signal_management()
            except Exception:
                pass
            return False
        # This lane adjudicates one exact disputed observation. Replaying the entire release story duplicated
        # normal QA coverage and could take dozens of model/browser steps before reaching the relevant action.
        # The full story remains owned by the post-review continuation; here we retain only minimum setup,
        # exact finding/action, expected result, and the persistence/audit facts needed for adjudication.
        review_story = sobj
        review_resume = _qa_review_recovery(
            children, results, rid, mem.get("coverage_revision"))
        try:
            from qa import dev_loop
            review_story = dev_loop._focused_repro_stories(
                [sobj], record.get("finding") or {})[0]
            state_path = dev_loop._finding_browser_state_path(record.get("finding") or {})
            if state_path and not review_resume.get("resume_state_path"):
                review_resume["resume_state_path"] = state_path
        except Exception:
            pass
        rs.update({"status": "verification_scheduled", "case_id": case_id,
                   "verification_attempts": attempt})
        review_states[rid] = rs
        review_observations[rid] = []
        # Paraphrases of the same observation can enter management as distinct immutable cases. One focused
        # journey can ground that semantic cluster; unrelated findings from the same story must not share it.
        peer = _qa_verification_peer(
            children, mem.get("internal_reviews") or [], record, exclude_review_id=rid)
        if peer:
            followers = list(review_followers.get(str(peer["review_id"])) or [])
            if rid not in followers:
                followers.append(rid)
            review_followers[str(peer["review_id"])] = followers
            rs.update({"status": "verification_shared", "shared_review_id": peer["review_id"],
                       "shared_actor_id": peer["actor_id"]})
            review_states[rid] = rs
            return True
        outcome = _hire_or_request(ctx, a, [{
            "name": f"{a['name']}.evidence-verification-{rid[-8:]}-{attempt}",
            "role": "qa-explorer", "kind": "worker",
            "task": f"Independently reproduce disputed evidence for story {story_id}",
            "tool": "qa_explore", "tool_args": {
                "target_url": cc.get("target_url"), "vision": cc.get("vision"),
                "token": cc.get("token"), "org": cc.get("org", "0"), "story": review_story,
                "max_steps": cc.get("max_steps"), "product": cc.get("product"),
                "repo": cc.get("repo"), "product_revision": mem.get("coverage_revision"),
                "_qa_review_id": rid, "_qa_review_case_id": case_id,
                **review_resume,
            }}], step, f"qa-verify:{rid}:{attempt}")
        children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                    if c["supervisor_id"] == me}
        return outcome in ("hired", "requested")

    def _complete_review_verification(review_id, fresh_result, corr_id=None):
        """Apply one browser result to its review case; callers may fan it out to related-story followers."""
        record = _review_record(review_id)
        observed = list(review_observations.get(review_id) or [])
        if not record:
            return
        provenance = (record.get("finding") or {}).get("evidence_provenance")
        rs = dict(review_states.get(review_id) or {})
        grounded_marker = _qa_grounded_actionable_marker(
            fresh_result, mem.get("coverage_revision"))
        if grounded_marker:
            # Keep this outside ``state``. Reviewer/manager generations intentionally replace state, which
            # previously erased the only memory that this exact browser mismatch was already grounded.
            rs["grounded_actionable_verification"] = grounded_marker
            review_states[review_id] = rs
        effective_clean_result = fresh_result
        fresh_clean = campaign_checkpoint.result_evidence_complete(fresh_result)
        if not fresh_clean:
            effective_clean_result = _prior_clean_review_result(review_id) or fresh_result
            fresh_clean = campaign_checkpoint.result_evidence_complete(effective_clean_result)
        if not provenance and observed:
            _record_finding_resolution(record, "superseded_by_fresh_evidence",
                                       "legacy observation replaced by a fresh sealed reproduction")
            _remove_internal_review(review_id)
            fresh_record = _qa_record_internal_review(mem, {
                "story": record.get("story"), "finding": observed[0],
                "reason": "fresh independent reproduction of a legacy unsealed finding"})
            _schedule_qa_review(fresh_record,
                                state={"fresh_result": fresh_result,
                                       "fresh_findings": observed}, corr_id=corr_id)
        elif (not provenance and not observed
              and campaign_checkpoint.result_evidence_complete(fresh_result)
              and int(rs.get("verification_attempts") or 0) >= 2):
            _remove_internal_review(review_id)
            _record_finding_resolution(record, "verified_false_positive",
                                       "two independent fresh coverage-complete runs did not reproduce")
            ss = dict(mem.get("story_status") or {})
            if record.get("story") is not None:
                ss[str(record["story"])] = "clean"
            mem["story_status"] = ss
        elif not provenance:
            _schedule_review_verification(record, corr_id=corr_id)
        elif fresh_clean:
            # Historical sealed evidence can be internally valid for its old bytes while an exact current-
            # revision replay is now completely clean. Sending that contradiction through the same reviewer
            # indefinitely yields "uncertain" forever because neither artifact disproves the other. The
            # release decision is temporal: supersede this finding for the current revision, retain the
            # historical resolution reason, and require the ordinary full-story continuation for qa_ok.
            _remove_internal_review(review_id)
            _record_finding_resolution(
                record, "superseded_by_current_revision",
                "fresh exact focused coverage completed without a current-revision defect")
            ss = dict(mem.get("story_status") or {})
            if record.get("story") is not None:
                ss[str(record["story"])] = "incomplete"
            mem["story_status"] = ss
            _queue_story_continuation(record)
        else:
            state = {"trigger": "fresh_independent_verification",
                     "fresh_result": fresh_result, "fresh_findings": observed,
                     "verification_attempt": rs.get("verification_attempts"),
                     # Bind the browser receipt to the exact product bytes it exercised. qareview seals this
                     # state generation and exposes only redacted evidence IDs to its read-only reviewers.
                     "product_revision": mem.get("coverage_revision")}
            _schedule_qa_review(record, state=state, corr_id=corr_id)

    def _schedule_story_continuation(record, *, corr_id=None):
        """After a false-positive disposition, finish the story normally; adjudication never grants qa_ok."""
        nonlocal children
        cc = dict((a.get("memory") or {}).get("context") or {})
        story_id = str(record.get("story") or (record.get("finding") or {}).get("story") or "")
        if _qa_fix_queue_preempts_continuation(
                mem, active_dev=bool(_active_dev_coordinators())):
            return False
        if not _qa_story_continuation_may_start(children, story_id):
            return False
        sobj = next((s for s in (cc.get("stories") or [])
                     if str(s.get("id") or s.get("title")) == story_id), None)
        if sobj is None:
            return False
        task = record.get("task") or f"Complete story {story_id} after its disputed finding was resolved"
        outcome = _hire_or_request(ctx, a, [{
            "name": f"{a['name']}.post-review-{record['review_id'][-8:]}",
            "role": "qa-explorer", "kind": "worker",
            "task": task,
            "tool": "qa_explore", "tool_args": {
                "target_url": cc.get("target_url"), "vision": cc.get("vision"),
                "token": cc.get("token"), "org": cc.get("org", "0"), "story": sobj,
                "max_steps": cc.get("max_steps"), "product": cc.get("product"),
                "repo": cc.get("repo"), "product_revision": mem.get("coverage_revision"),
                **_qa_prior_recovery(
                    results, story_id, children=children,
                    product_revision=mem.get("coverage_revision"),
                    invalid_actor_ids=mem.get("invalid_recovery_actor_ids"),
                    revision_invalidations=mem.get("revision_invalidations")),
            }}], step, corr_id or f"qa-post-review:{record['review_id']}")
        children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                    if c["supervisor_id"] == me}
        return outcome in ("hired", "requested")

    def _handoff_qa_bug(bug, corr_id=None):
        """Start one repository-mutating fix chain. Callers serialize these through pending_dev_findings."""
        nonlocal children
        cc = dict((a.get("memory") or {}).get("context") or {})
        _hire_or_request(ctx, a, [{
            "name": f"{a['name']}.dev{len(children)}", "role": "dev-coordinator", "kind": "supervisor",
            "task": f"Fix release-significant QA defect: {bug.get('title') or bug.get('bug') or 'defect'}",
            "context": {"bug": bug, "vision": cc.get("vision"), "repo": cc.get("repo"),
                        "product": cc.get("product"),
                        "target_url": cc.get("target_url"), "stories": cc.get("stories"),
                        "token": cc.get("token"), "org": cc.get("org"),
                        "restart_cmd": cc.get("restart_cmd"), "health_url": cc.get("health_url")}}],
            step, corr_id)
        children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                    if c["supervisor_id"] == me}
        _audit(a["name"], "QaDevHandoff", "executed",
               {"bug": bug.get("title") or bug.get("bug")})

    def _flush_pending_qa_bug(corr_id=None):
        """Launch one fixer only after prior mutation and current QA slice are durably terminal."""
        if _active_dev_coordinators() or not _qa_mutation_may_start(children):
            return False
        pending = list(mem.get("pending_dev_findings") or [])
        if not pending:
            return False
        bug, remaining = _qa_select_pending_finding(pending)
        if not bug:
            return False
        # A fresh verifier may have just completed while its authoritative reviewer is still reducing the
        # result. Starting a same-story sibling fixer in that narrow window duplicates work and can mutate the
        # revision before the clean proof clears the cluster. Keep the queue unchanged until review resolves.
        if _qa_story_review_inflight(children, bug.get("story")):
            return False
        mem["pending_dev_findings"] = remaining
        _handoff_qa_bug(bug, corr_id)
        return True

    def _queue_story_continuation(record):
        pending = list(mem.get("pending_story_continuations") or [])
        review_id = str((record or {}).get("review_id") or "")
        if not any(str((item or {}).get("review_id") or "") == review_id for item in pending):
            pending.append(record)
        mem["pending_story_continuations"] = _qa_compact_story_continuations(
            pending, mem.get("story_status"))

    def _flush_pending_story_continuation(corr_id=None):
        """Resume one managed evidence story only when no repository mutation is active."""
        pending = list(mem.get("pending_story_continuations") or [])
        if not pending:
            return False
        # Known release-significant defects have already earned the right to mutate the product. Running an
        # ordinary gap-fill first only gathers evidence against a revision that is about to change, then forces
        # that work to be invalidated and repeated. Drain the serialized fixer queue first; the continuation is
        # durable and will be resumed after the mutation/retest chain reaches a safe boundary.
        if _qa_fix_queue_preempts_continuation(
                mem, active_dev=bool(_active_dev_coordinators())):
            return False
        record = pending[0]
        story_id = str(record.get("story") or (record.get("finding") or {}).get("story") or "")
        if not _qa_story_continuation_may_start(children, story_id):
            return False
        if not _schedule_story_continuation(record, corr_id=corr_id):
            return False
        mem["pending_story_continuations"] = pending[1:]
        return True

    for ev in evs:
        k, p, corr, frm = ev["kind"], (ev["payload"] or {}), ev["corr_id"], ev["frm"]

        # ---- kickoff: decompose + hire ---------------------------------------------------
        if k == "task" and phase == "new":
            task = p.get("task") or a["assignment"] or ""
            specs = _decompose_specs(ctx, a, task)
            outcome = _hire_or_request(ctx, a, specs, step, corr)
            if outcome == "hired":
                phase = "delegating"
                children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                            if c["supervisor_id"] == me}
            elif outcome == "requested":
                phase = "hiring"
                mem["pending_specs"] = specs
            else:                                # top of tree and still denied -> hard stop
                step.status = "dead"
                step.result = {"failed": True, "reason": "spawn denied by governance at the root"}
                step.finish_status = "failed"
            handled.append({"frm": frm, "kind": k, "action": f"decompose:{outcome}",
                            "children": len(specs)})
            continue

        if (k == "finding_resolution" and (a.get("role") or "").lower() == "qa-coordinator"):
            key = (p.get("review_id"), p.get("disposition"))
            if not any((r.get("review_id"), r.get("disposition")) == key for r in finding_resolutions):
                finding_resolutions.append(dict(p))
            handled.append({"frm": frm, "kind": k, "action": "resolution_recorded",
                            "review_id": p.get("review_id"), "outstanding": _live_children()})
            continue

        if (k == "context_update" and (a.get("role") or "").lower() == "qa-coordinator"
                and isinstance(p.get("qa_performance_recovery"), dict)):
            recovery = p["qa_performance_recovery"]
            record = _qa_apply_performance_recovery(mem, recovery)
            if record:
                _queue_story_continuation(record)
                recovery_action = "queued_for_batch_priority"
                _audit(a["name"], "QaPerformanceRecovery", "scheduled", {
                    "review_id": recovery.get("review_id"), "story": record.get("story"),
                    "action": recovery.get("action"), "scheduling": recovery_action})
            handled.append({"frm": frm, "kind": k, "action": "qa_performance_recovery",
                            "review_id": recovery.get("review_id"), "outstanding": _live_children()})
            continue

        if (k == "context_update" and (a.get("role") or "").lower() == "qa-coordinator"
                and isinstance(p.get("qa_capability_recovery"), dict)):
            recovery = p["qa_capability_recovery"]
            record = _qa_apply_performance_recovery(mem, recovery)
            if record:
                _queue_story_continuation(record)
                recovery_action = "queued_for_batch_priority"
                _audit(a["name"], "QaCapabilityRecovery", "scheduled", {
                    "review_id": recovery.get("review_id"), "story": record.get("story"),
                    "action": recovery.get("action"), "scheduling": recovery_action})
            handled.append({"frm": frm, "kind": k, "action": "qa_capability_recovery",
                            "review_id": recovery.get("review_id"), "outstanding": _live_children()})
            continue

        if (k == "context_update" and (a.get("role") or "").lower() == "qa-coordinator"
                and isinstance(p.get("qa_evidence_recovery"), dict)):
            recovery = p["qa_evidence_recovery"]
            record = _review_record(recovery.get("review_id"))
            scheduled = False
            if record:
                scheduled = _schedule_review_verification(
                    record, case_id=recovery.get("case_id"), corr_id=corr,
                    manager_authorized=True)
            _audit(a["name"], "QaEvidenceRecovery",
                   "scheduled" if scheduled else "not_scheduled", {
                       "review_id": recovery.get("review_id"),
                       "case_id": recovery.get("case_id"),
                       "action": recovery.get("action")})
            handled.append({"frm": frm, "kind": k, "action": "qa_evidence_recovery",
                            "review_id": recovery.get("review_id"), "scheduled": scheduled,
                            "outstanding": _live_children()})
            continue

        if (k == "context_update" and (a.get("role") or "").lower() == "qa-coordinator"
                and isinstance(p.get("qa_review_state_changed"), dict)):
            changed = p["qa_review_state_changed"]
            record = _review_record(changed.get("review_id"))
            if record:
                _schedule_qa_review(record, state=changed.get("state") or {}, corr_id=corr)
            handled.append({"frm": frm, "kind": k, "action": "qa_review_state_changed",
                            "review_id": changed.get("review_id"), "outstanding": _live_children()})
            continue

        # ---- a hire REQUEST from a governance-denied lead below --------------------------
        if k == "need_agent" and p.get("specs") and p.get("for"):
            deny = _spawn_gate(a["role"])
            if deny is None:
                hired = [_hire(ctx, p["for"], s,
                               hire_key=_hire_key(p["for"], corr, s, index))
                         for index, s in enumerate(p["specs"])]
                step.emits.append((me, frm, "resolve",
                                   {"hired": [h for h in hired if h], "note": "hired on your behalf"},
                                   corr))
                act = "hire_on_behalf"
            elif a["supervisor_id"]:            # can't hire either -> keep escalating up
                step.emits.append((me, a["supervisor_id"], "need_agent", p, corr))
                act = "escalate_hire"
            else:
                escalations.append({"frm": frm, "reason": deny, "specs": p["specs"]})
                act = "hire_denied"
            handled.append({"frm": frm, "kind": k, "action": act,
                            "outstanding": _live_children()})
            continue

        # ---- a resolution coming DOWN from our own supervisor -----------------------------
        if k == "resolve" and frm == a["supervisor_id"]:
            if p.get("hired"):                  # our hire request was fulfilled by the tier above
                phase = "delegating"
                mem.pop("pending_specs", None)
                children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                            if c["supervisor_id"] == me}
            else:                               # an escalation grant -> team-wide broadcast
                grant = p.get("grant", p)
                _broadcast({"grant": grant}, "resolution from above", corr)
                blocked_child = None
            handled.append({"frm": frm, "kind": k, "action": "apply_resolution",
                            "outstanding": _live_children()})
            continue

        # ---- child events: the interrupt-driven core --------------------------------------
        if frm in children:
            if k == "internal_review_required":
                prior_review_ids = {r.get("review_id") for r in (mem.get("internal_reviews") or [])}
                record = _qa_record_internal_review(mem, p)
                role = (a.get("role") or "").lower()
                if (role == "dev-coordinator" and a["supervisor_id"]
                        and record["review_id"] not in prior_review_ids):
                    # Route one tier to QA management, never through the generic escalation/CEO machinery.
                    step.emits.append((me, a["supervisor_id"], "internal_review_required", record, corr))
                elif role == "qa-coordinator":
                    ss = dict(mem.get("story_status") or {})
                    if record.get("story") is not None:
                        ss[str(record["story"])] = "internal_review"
                    mem["story_status"] = ss
                    if record["review_id"] not in prior_review_ids:
                        _schedule_qa_review(record, corr_id=corr)
                _audit(a["name"], "QaInternalReview", "queued",
                       {"review_id": record["review_id"], "story": record.get("story"), "route": record["route"]})
                handled.append({"frm": frm, "kind": k, "action": "queue_internal_management",
                                "review_id": record["review_id"], "outstanding": _live_children()})
                continue
            if k == "done":
                results[str(frm)] = p
                children[frm] = store.actor(frm, tid) or children[frm]
                if (a.get("role") or "").lower() == "qa-coordinator":
                    childrole = (children.get(frm) or {}).get("role")
                    child_args = _child_tool_args(children.get(frm))
                    review_id = child_args.get("_qa_review_id")
                    if childrole == "qa-evidence-reviewer":
                        outcome = p.get("result") or {}
                        review_id = outcome.get("review_id") or child_args.get("internal_review", {}).get("review_id")
                        record = _review_record(review_id)
                        if record:
                            case_id = outcome.get("case_id")
                            disposition = outcome.get("disposition")
                            rs = dict(review_states.get(review_id) or {})
                            rs.update({"status": outcome.get("status"), "case_id": case_id,
                                       "last_outcome": outcome})
                            pending_review_state = rs.pop("pending_review_state", None)
                            review_states[review_id] = rs
                            story_id = str(record.get("story") or "")
                            if disposition in ("confirmed_defect", "verified_false_positive",
                                               "needs_named_external_authority"):
                                cancelled = _cancel_review_verification(review_id, disposition)
                                if cancelled:
                                    _audit(a["name"], "QaFocusedVerification", "superseded", {
                                        "review_id": review_id, "disposition": disposition,
                                        "actor_ids": cancelled})
                            if pending_review_state is not None and not disposition:
                                _schedule_qa_review(
                                    record, state=pending_review_state,
                                    corr_id=f"qa-review-pending:{review_id}")
                                handled.append({"frm": frm, "kind": k,
                                                "action": "qa_review_pending_state_rescheduled",
                                                "review_id": review_id,
                                                "outstanding": _live_children()})
                                continue
                            if disposition == "confirmed_defect":
                                _remove_internal_review(review_id)
                                _record_finding_resolution(record, disposition, outcome.get("rationale"),
                                                           case_id=case_id)
                                ss = dict(mem.get("story_status") or {})
                                if story_id:
                                    ss[story_id] = "blocking"
                                mem["story_status"] = ss
                                pending = list(mem.get("pending_dev_findings") or [])
                                bug = dict(record.get("finding") or {})
                                # A senior QA disposition is durable authority for this exact finding. Carry
                                # the fenced case reference into the fixer so it does not re-open the same
                                # evidence dispute merely because unrelated repository bytes changed later.
                                bug["_qa_adjudication"] = {
                                    "case_id": case_id, "review_id": review_id,
                                    "disposition": disposition,
                                    "state_generation": outcome.get("state_generation"),
                                    "confidence": outcome.get("confidence"),
                                    "rationale": outcome.get("rationale"),
                                }
                                key = (bug.get("story"), bug.get("title") or bug.get("bug"))
                                if not any((b.get("story"), b.get("title") or b.get("bug")) == key
                                           or _qa_same_observation(b, bug) for b in pending):
                                    pending.append(bug)
                                mem["pending_dev_findings"] = pending
                            elif disposition == "verified_false_positive":
                                _remove_internal_review(review_id)
                                _record_finding_resolution(record, disposition, outcome.get("rationale"),
                                                           case_id=case_id)
                                fresh = (rs.get("state") or {}).get("fresh_result") or {}
                                clean_fresh = campaign_checkpoint.result_evidence_complete(fresh)
                                ss = dict(mem.get("story_status") or {})
                                if story_id:
                                    ss[story_id] = "clean" if clean_fresh else "incomplete"
                                mem["story_status"] = ss
                                if not clean_fresh:
                                    _queue_story_continuation(record)
                            elif disposition == "needs_named_external_authority":
                                try:
                                    import authority
                                    ext = outcome.get("external_authority") or {}
                                    kind = "legal" if ext.get("type") == "legal_counsel" else "business"
                                    proposal = {"question": ext.get("question"),
                                                "reason": outcome.get("rationale"), "risk": "high",
                                                "reversible": True, "management_exhausted": True,
                                                "requires_ceo_business_judgment": kind == "business",
                                                "named_authority": ext}
                                    decision = authority.open_decision(
                                        tid, f"orchestra:{ctx.run_id}:{review_id}", kind, proposal,
                                        # An answered authority request is resolved and immutable. A later
                                        # semantic evidence generation must never reattach that old request and
                                        # wait forever for an answer it already received.
                                        correlation_id=_qa_authority_correlation(
                                            tid, review_id, outcome.get("state_generation")),
                                        org_id=((a.get("memory") or {}).get("context") or {}).get("org"),
                                        thread_id=((a.get("memory") or {}).get("context") or {}).get("thread_id"),
                                        owner_role="senior-qa-director")
                                    rs["authority_decision_id"] = decision.get("id")
                                    rs["status"] = decision.get("status")
                                    review_states[review_id] = rs
                                    if case_id and decision.get("id"):
                                        import qareview
                                        qareview.attach_authority(tid, case_id, decision["id"])
                                except Exception as exc:
                                    rs["authority_error"] = str(exc)[:300]
                                    review_states[review_id] = rs
                            else:
                                _schedule_review_verification(record, case_id=case_id, corr_id=corr)
                        handled.append({"frm": frm, "kind": k, "action": "qa_review_result",
                                        "review_id": review_id, "outstanding": _live_children()})
                        continue
                    if childrole == "qa-explorer" and review_id:
                        fresh_result = p.get("result") or {}
                        related_reviews = [review_id] + list(review_followers.pop(str(review_id), []) or [])
                        for related_id in related_reviews:
                            if related_id != review_id:
                                review_observations[related_id] = list(
                                    review_observations.get(review_id) or [])
                            _complete_review_verification(related_id, fresh_result, corr_id=corr)
                        handled.append({"frm": frm, "kind": k, "action": "review_verification_recorded",
                                        "review_id": review_id, "shared_reviews": related_reviews[1:],
                                        "outstanding": _live_children()})
                        continue
                    if childrole == "qa-explorer" and p.get("story") is not None:
                        # Authoritative LATEST status for this story. "No blocking bug" is not enough to pass:
                        # if the explorer still has untested ledger items, the story remains incomplete until a
                        # gap-fill/re-test actually covers it.
                        ss = dict(mem.get("story_status") or {})
                        # GAP-FILL: an explorer that stopped with INCOMPLETE coverage (not a bug) gets another
                        # qa-explorer hired to CONTINUE that story — bounded per story so it always terminates.
                        explorer_result = p.get("result") or {}
                        stop = explorer_result.get("stop_reason")
                        missing_capabilities = list(explorer_result.get("missing_capabilities") or [])
                        restored_reviews = _qa_clear_restored_capability_reviews(
                            mem, p["story"], explorer_result)
                        if restored_reviews:
                            _audit(a["name"], "QaCapabilityRestored", "resolved", {
                                "story": p["story"], "review_ids": restored_reviews,
                                "steps": explorer_result.get("steps")})
                        story_outcome = _qa_story_status(p)
                        incomplete = story_outcome == "incomplete"
                        progress_state = _qa_story_progress(mem, p["story"], explorer_result)
                        performance_observations = _qa_record_performance_observations(
                            mem, p["story"], progress_state)
                        if performance_observations:
                            _audit(a["name"], "QaSubordinatePerformanceObserved", "recorded", {
                                "story": p["story"], "observations": performance_observations,
                                "progress": {key: progress_state.get(key) for key in (
                                    "covered", "coverage_total", "steps", "no_progress", "advanced")}})
                        performance_stalled = bool(
                            incomplete and int(progress_state.get("no_progress") or 0)
                            >= max(1, _QA_NO_PROGRESS_REVIEW))
                        ss[str(p["story"])] = ("internal_review" if missing_capabilities or performance_stalled
                                                else story_outcome)
                        mem["story_status"] = ss
                        if ss[str(p["story"])] == "clean":
                            mem["pending_dev_findings"] = _qa_clear_proven_fixed(
                                mem.get("pending_dev_findings"), p["story"], p.get("result"))
                            # Any queued gap-fill/post-review/harness reasons for this story are now all
                            # satisfied by the same authoritative current-revision full-story proof.
                            mem["pending_story_continuations"] = _qa_compact_story_continuations(
                                mem.get("pending_story_continuations"), ss)
                        if missing_capabilities:
                            record = _qa_record_capability_review(
                                mem, p["story"], missing_capabilities)
                            case_id = None
                            try:
                                import management
                                cc = dict((a.get("memory") or {}).get("context") or {})
                                signaled = management.signal(
                                    f"qa-capability:{tid}:{record['review_id']}",
                                    f"QA capability unavailable for story {p['story']}",
                                    "qa_capability_unavailable",
                                    {"status": "manager_attention", "run_id": ctx.run_id,
                                     "coordinator_actor_id": me, "story": str(p["story"]),
                                     "review_id": record["review_id"],
                                     "capabilities": missing_capabilities},
                                    tenant_id=tid, product=cc.get("product"),
                                    work_id=f"orchestra:{ctx.run_id}:{record['review_id']}",
                                    worker="qa-manager", manager_role="senior-qa-director")
                                case_id = signaled.get("case_id")
                            except Exception as exc:
                                _audit(a["name"], "QaCapabilityManagement", "signal_failed",
                                       {"review_id": record["review_id"], "error": str(exc)[:300]})
                            record = _qa_record_capability_review(
                                mem, p["story"], missing_capabilities, case_id=case_id)
                            _audit(a["name"], "QaCapabilityManagement", "queued", {
                                "review_id": record["review_id"], "case_id": case_id,
                                "story": p["story"],
                                "capabilities": [item.get("capability")
                                                 for item in missing_capabilities]})
                        if performance_stalled and not missing_capabilities:
                            record = _qa_record_internal_review(mem, {
                                "story": p["story"],
                                "finding": {"kind": "qa_performance_stall",
                                            "story": p["story"],
                                            "title": "QA subordinate made no new evidence progress",
                                            "detail": (f"{progress_state.get('no_progress')} consecutive "
                                                       "continuations added no covered aspect or durable step"),
                                            "blocking": False},
                                "reason": "QA coordinator detected repeated subordinate no-progress",
                                "triage": progress_state})
                            _schedule_qa_review(
                                record, state={"trigger": "subordinate_no_progress",
                                               "progress": progress_state}, corr_id=corr)
                            _audit(a["name"], "QaPerformanceManagement", "internal_review", {
                                "review_id": record["review_id"], "story": p["story"],
                                "no_progress": progress_state.get("no_progress"),
                                "slow_phases": progress_state.get("slow_phases")})
                        gf = dict(mem.get("gapfills") or {})
                        if progress_state.get("advanced"):
                            # This is a no-progress streak, not a lifetime attempt counter. A story that moves
                            # from 20% to 40% to 70% keeps its team and is never cut off at an arbitrary attempt.
                            gf[str(p["story"])] = 0
                        if (incomplete and not missing_capabilities and not p.get("blocking_found")
                                and not performance_stalled
                                and (_MAX_GAPFILL <= 0
                                     or gf.get(str(p["story"]), 0) < _MAX_GAPFILL)):
                            gf[str(p["story"])] = gf.get(str(p["story"]), 0) + 1
                            mem["gapfills"] = gf
                            cc = dict((a.get("memory") or {}).get("context") or {})
                            sobj = next((s for s in (cc.get("stories") or [])
                                         if str(s.get("id") or s.get("title")) == str(p["story"])), None)
                            if sobj is not None:
                                _queue_story_continuation({
                                    "review_id": f"gapfill:{p['story']}:{gf[str(p['story'])]}",
                                    "story": str(p["story"]),
                                    "task": f"GAP-FILL untested aspects of story {p['story']}",
                                    "reason": "queued until the complete event batch is prioritized",
                                })
                                _audit(a["name"], "QaGapFill", "queued_for_batch_priority",
                                       {"story": p["story"], "attempt": gf[str(p["story"])]})
                    elif childrole == "dev-coordinator":
                        # RE-TEST LOOP (closed loop): a fix finished -> hire a FRESH qa-explorer to re-verify
                        # the fixed story, bounded per story so a bad fix can't cycle find<->fix forever.
                        cc = dict((a.get("memory") or {}).get("context") or {})
                        bug = ((children[frm].get("memory") or {}).get("context") or {}).get("bug") or {}
                        story = bug.get("story")
                        child_result = p.get("result") or {}
                        # A dev-fixer can independently prove that the explorer observation was a false
                        # positive.  That is a real QA lifecycle transition, not merely prose for the next
                        # manager to summarize.  Preserve it as a durable finding resolution.  The story still
                        # needs a clean explorer completion, but when no bytes changed the continuation must
                        # inherit its exact portable browser state and already-proven ledger rather than replay
                        # the whole story.  ``_qa_prior_recovery`` is revision-fenced, so a real repair still
                        # receives a genuinely fresh retest.
                        if child_result.get("resolved_without_mutation"):
                            finding_id = bug.get("finding_id")
                            if finding_id:
                                review_id = f"dev-triage:{finding_id}"
                            else:
                                stable_bug = json.dumps({
                                    "story": story,
                                    "title": bug.get("title") or bug.get("bug"),
                                    "detail": bug.get("detail"),
                                }, sort_keys=True, default=str)
                                review_id = "dev-triage:" + hashlib.sha256(
                                    stable_bug.encode()).hexdigest()[:24]
                            verdict = child_result.get("verdict") or {}
                            reason = (verdict.get("reason") if isinstance(verdict, dict) else verdict)
                            _record_finding_resolution(
                                {"review_id": review_id, "finding": bug, "story": story},
                                "verified_false_positive",
                                reason or "two independent repository reviewers dismissed the observation")
                            _audit(a["name"], "QaFindingResolvedWithoutMutation", "verified_false_positive", {
                                "review_id": review_id, "finding_id": finding_id, "story": story})
                        current_revision = campaign_checkpoint.repo_revision(cc.get("repo"))
                        impact = None
                        impacted_ids = None
                        completion_receipt = _qa_dev_completion_receipt(
                            frm, store.actors(ctx.run_id, tid), child_result)
                        changed_files = list(completion_receipt.get("files") or [])
                        previous_revision = (mem.get("coverage_revision")
                                             or (mem.get("context") or {}).get("product_revision"))
                        if (current_revision and current_revision != previous_revision
                                and changed_files):
                            impact = _qa_revision_impact_scope(
                                cc.get("stories") or [], changed_files, story, bug,
                                repo=cc.get("repo") or ctx.repo,
                                change_summary={
                                    "result": child_result.get("result"),
                                    "verdict": child_result.get("verdict"),
                                    "plan": child_result.get("plan"),
                                    "durable_completion_summaries": completion_receipt.get("summaries"),
                                    "durable_change_diffs": completion_receipt.get("change_diffs"),
                                    "receipt_actor_ids": completion_receipt.get("actor_ids"),
                                })
                            if not impact.get("full_regression"):
                                impacted_ids = impact.get("impacted_story_ids") or [str(story)]
                        stale = _qa_invalidate_revision(
                            mem, current_revision, impacted_story_ids=impacted_ids, impact=impact,
                            tenant_id=tid)
                        if stale:
                            _audit(a["name"], "QaCoverageRevisionInvalidated", "scheduled_regression", {
                                "stale_stories": stale, "count": len(stale),
                                "preserved_stories": list((impact or {}).get("preserved_story_ids") or []),
                                "full_regression": impacted_ids is None,
                                "changed_files": changed_files,
                                "revision_generation": mem.get("revision_generation"),
                                "product_revision": current_revision})
                        if child_result.get("internal_review_required"):
                            handled.append({"frm": frm, "kind": k,
                                            "action": "internal_review_no_retest",
                                            "outstanding": _live_children()})
                            continue
                        sobj = next((s for s in (cc.get("stories") or [])
                                     if str(s.get("id") or s.get("title")) == str(story)), None)
                        rt_ = dict(mem.get("retests") or {})
                        if sobj is not None and rt_.get(str(story), 0) < _MAX_RETEST:
                            rt_[str(story)] = rt_.get(str(story), 0) + 1
                            mem["retests"] = rt_
                            _hire_or_request(ctx, a, [{
                                "name": f"{a['name']}.retest-{story}-{rt_[str(story)]}", "role": "qa-explorer",
                                "kind": "worker", "task": f"RE-TEST story {story} after fix",
                                "tool": "qa_explore", "tool_args": {"target_url": cc.get("target_url"),
                                    "vision": cc.get("vision"), "token": cc.get("token"),
                                    "org": cc.get("org", "0"), "story": sobj,
                                    "max_steps": cc.get("max_steps"),
                                    "product": cc.get("product"), "repo": cc.get("repo"),
                                    "product_revision": mem.get("coverage_revision"),
                                    **_qa_prior_recovery(
                                        results, story, children=children,
                                        product_revision=mem.get("coverage_revision"),
                                        invalid_actor_ids=mem.get("invalid_recovery_actor_ids"),
                                        revision_invalidations=mem.get("revision_invalidations"),
                                        allow_actionable_checkpoint=not bool(
                                            child_result.get("resolved_without_mutation")))}}],
                                step, corr)
                            children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                                        if c["supervisor_id"] == me}
                            _audit(a["name"], "QaRetest", "executed", {"story": story, "attempt": rt_[str(story)]})
                handled.append({"frm": frm, "kind": k, "action": "record",
                                "outstanding": _live_children()})
                continue
            if k == "need_agent":               # a child wants a helper -> org_decider gates it
                dec = org_decider.should_expand(
                    p, {"children": [{"id": cid, "role": c["role"], "status": c["status"]}
                                     for cid, c in children.items()]},
                    {"event": "need_agent", "from": frm})
                if dec.get("expand"):
                    spec = {"name": f"{a['name']}.h{len(children)}",
                            "role": p.get("role") or a["role"], "kind": "worker",
                            "task": p.get("task") or dec.get("detail") or "assist the team"}
                    _hire_or_request(ctx, a, [spec], step, corr)
                    act = "spawn_helper"
                else:
                    step.emits.append((me, frm, "context_update",
                                       {"note": f"expansion declined: {dec.get('rationale')}"}, corr))
                    act = "declined"
                handled.append({"frm": frm, "kind": k, "action": act,
                                "outstanding": _live_children()})
                continue

            # DISAGREEMENT: a child professionally objects to the directive. NEVER a local supervisor decide —
            # it always goes UP to the CEO's tier to rule on (proceed / revise); the child parks until the
            # ruling flows back (via resolve -> broadcast). This is the human-pattern "an agent won't just
            # execute a bad directive; it pushes back and the boss decides".
            if k == "disagree":
                blocked_child = frm
                reason = p.get("reason") or p.get("note") or "objects to the assignment"
                if is_top:                          # CEO tier -> consult the human, then resolve the ruling down
                    ruling = (ctx.human_hook(ev, {"disagreement": reason})
                              if callable(getattr(ctx, "human_hook", None)) else "proceed as directed")
                    step.emits.append((me, frm, "resolve",
                                       {"note": f"CEO ruling on the objection: {ruling}",
                                        "grant": {"ruling": ruling}, "context": {"ruling": ruling}}, corr))
                    escalations.append({"frm": frm, "kind": "disagree", "resolved_with": str(ruling)[:200]})
                elif a["supervisor_id"]:            # route the objection further up toward the CEO
                    step.emits.append((me, a["supervisor_id"], "disagree",
                                       {"reason": reason, "from": frm, "payload": p}, corr))
                _audit(a["name"], "Disagreement", "escalated", {"frm": frm, "reason": str(reason)[:160]})
                handled.append({"frm": frm, "kind": k, "action": "disagreement_up",
                                "outstanding": _live_children()})
                continue

            # RESOURCE / PROCESS ESCALATION: these are not ordinary "please advise" messages. A real org
            # needs a durable ask/proposal with a human-visible lifecycle; if this is the top tier, open one
            # through agent_request and keep the child parked. Non-top supervisors route it upward.
            if k in ("resource_request", "process_change"):
                blocked_child = frm
                if is_top:
                    _open_human_request(k, frm, p)
                elif a["supervisor_id"]:
                    step.emits.append((me, a["supervisor_id"], k,
                                       {"from": frm, "payload": p, "reason": p.get("reason")}, corr))
                    handled.append({"frm": frm, "kind": k, "action": "escalate_up",
                                    "outstanding": _live_children()})
                _audit(a["name"], "ResourceOrProcessEscalation", "escalated",
                       {"kind": k, "frm": frm, "payload": json.dumps(p, default=str)[:300]})
                continue

            child_review_id = (_child_tool_args(children.get(frm)).get("_qa_review_id")
                               if frm in children else None)
            if (k == "finding" and child_review_id
                    and (a.get("role") or "").lower() == "qa-coordinator"):
                targets = [child_review_id] + list(review_followers.get(str(child_review_id)) or [])
                finding_id = p.get("finding_id")
                for target_review_id in targets:
                    observed = list(review_observations.get(target_review_id) or [])
                    if not any((finding_id and f.get("finding_id") == finding_id)
                               or (f.get("story"), f.get("title")) == (p.get("story"), p.get("title"))
                               for f in observed):
                        observed.append(dict(p))
                    review_observations[target_review_id] = observed
                handled.append({"frm": frm, "kind": k, "action": "review_evidence_recorded",
                                "review_id": child_review_id, "shared_reviews": targets[1:],
                                "outstanding": _live_children()})
                continue

            # QA-COORDINATOR records every finding (for its honest aggregate verdict), then hands blocking ones off.
            if k == "finding" and (a.get("role") or "").lower() == "qa-coordinator":
                mem.setdefault("qa_findings", []).append(
                    {"title": p.get("title") or p.get("bug"), "story": p.get("story"),
                     "blocking": bool(p.get("blocking"))})

            # QA-COORDINATOR dev-handoff (phase 4b): a release-significant finding from an explorer is handed to a
            # dev-coordinator — the coordinator-to-coordinator conversation. Deterministic: hire a
            # dev-coordinator with the bug + repo/vision in its context; it spawns a dev-fixer, fixes on the
            # real git diff, and emits `done` back up, which the qa-coordinator aggregates. (Re-test of the
            # fixed story after the fix is a further refinement — see HANDOFF phase 4b.)
            if (k == "finding" and (a.get("role") or "").lower() == "qa-coordinator"
                    and _qa_finding_needs_fix(p)):
                pending = list(mem.get("pending_dev_findings") or [])
                key = (p.get("story"), p.get("title") or p.get("bug"))
                if not any((b.get("story"), b.get("title") or b.get("bug")) == key
                           or _qa_same_observation(b, p) for b in pending):
                    pending.append(p)
                mem["pending_dev_findings"] = pending
                # Reduce the complete claimed inbox before choosing work. Otherwise the first finding in a
                # batch can launch a medium repair before a later critical finding, or a continuation before
                # an adjudicated defect. The post-loop scheduler below preserves same-turn latency.
                action = "dev_handoff_queued_for_batch_priority"
                _audit(a["name"], "QaDevHandoff", "deferred",
                       {"bug": p.get("title") or p.get("bug"), "queue_depth": len(pending)})
                handled.append({"frm": frm, "kind": k, "action": action,
                                "outstanding": _live_children()})
                continue

            # blocked / escalate / question / finding / need_context / next
            if is_top:                          # TOP TIER: resolve it or consult the human
                d = _ai_json(a["role"], ctx.repo,
                             _CONTROLLER_PROMPT.format(payload=json.dumps(p, default=str)[:800]),
                             spawner=a["role"])
                action = (d.get("action") or "resolve").lower()
                if action in ("consult_human", "ask_human", "human") and callable(ctx.human_hook):
                    grant = ctx.human_hook(ev, d)
                else:
                    grant = d.get("grant") or d.get("message") or "capability granted"
                step.emits.append((me, frm, "resolve", {"grant": grant, "for": p}, corr))
                escalations.append({"frm": frm, "kind": k, "resolved_with": str(grant)[:200]})
                handled.append({"frm": frm, "kind": k, "action": "resolve",
                                "outstanding": _live_children()})
                _audit(a["name"], "ControllerResolve", "executed", {"frm": frm, "grant": str(grant)[:160]})
                continue

            d = _ai_json(a["role"], ctx.repo, _DECIDE_PROMPT.format(
                kind=k, frm=frm, payload=json.dumps(p, default=str)[:800]), spawner=a["role"])
            action = "escalate" if d.get("_blocker") else (d.get("action") or "ack").lower()
            if action == "escalate":
                blocked_child = frm             # keep the child PARKED (it resumes on the grant)
                reason = d.get("reason") or d.get("_blocker") or "beyond supervisor capability"
                escalations.append({"frm": frm, "kind": k, "reason": reason})
                step.emits.append((me, a["supervisor_id"], "escalate",
                                   {"reason": reason, "from": frm, "payload": p}, corr))
                _audit(a["name"], "Escalate", "escalated", {"frm": frm, "reason": reason})
            elif action == "broadcast":
                _broadcast({"correction": d.get("message") or d.get("context")}, "team correction", corr)
            elif action == "spawn_helper":
                spec = dict(d.get("spec") or {"role": a["role"], "task": d.get("task") or "assist"})
                spec.setdefault("name", f"{a['name']}.h{len(children)}")
                _hire_or_request(ctx, a, [spec], step, corr)
            elif action == "hand_next":
                step.emits.append((me, frm, "task", {"task": d.get("task") or "continue"}, corr))
            elif action in ("unblock", "rebrief"):
                step.emits.append((me, frm, "resolve",
                                   {"note": d.get("message") or "proceed", "context": {}}, corr))
            # 'ack' -> recorded only
            handled.append({"frm": frm, "kind": k, "action": action,
                            "outstanding": _live_children()})
            continue

        handled.append({"frm": frm, "kind": k, "action": "ignored"})

    # Choose the next QA action only after the entire claimed event batch has updated durable state. This is
    # still an event-driven same-turn decision, but it is independent of row/arrival order: confirmed and
    # higher-risk repairs preempt ordinary evidence continuations, and active retests retain repository safety.
    if phase == "delegating" and (a.get("role") or "").lower() == "qa-coordinator":
        if not _flush_pending_qa_bug():
            _flush_pending_story_continuation()

    # ---- aggregate: the ONLY join point, reached by events (never a barrier) --------------
    qa_no_child_join = False
    if phase == "delegating" and (a.get("role") or "").lower() == "qa-coordinator" and not children:
        join_context = dict(mem.get("context") or {})
        join_planned = {
            str(story.get("id") or story.get("title"))
            for story in (join_context.get("stories") or [])
            if story.get("id") or story.get("title")}
        qa_no_child_join = bool(
            join_planned and join_planned.issubset(
                {str(key) for key in (mem.get("story_status") or {})}))
    if (phase == "delegating" and (children or qa_no_child_join) and not mem.get("aggregated")
            and all(c["status"] in TERMINAL for c in children.values())):
        if (a.get("role") or "").lower() == "qa-coordinator":
            # HONEST agentic QA verdict — deterministic, not an AI aggregate. A run is only 'passed' with NO
            # blocking bugs; blocking bugs handed to dev report the fix as CLAIMED (re-verification after fix
            # is the phase-4b refinement, so we say so plainly rather than pretend it's confirmed).
            qf = mem.get("qa_findings") or []
            ss = mem.get("story_status") or {}          # LATEST status per story (re-tested-fixed -> clean)
            blocking_stories = [s for s, st in ss.items() if st == "blocking"]
            incomplete_stories = [s for s, st in ss.items() if st == "incomplete"]
            internal_reviews = list(mem.get("internal_reviews") or [])
            internal_review_stories = sorted({str(r.get("story")) for r in internal_reviews
                                               if r.get("story") is not None})
            cc = dict((a.get("memory") or {}).get("context") or {})
            planned_order = list(dict.fromkeys(
                str(s.get("id") or s.get("title")) for s in (cc.get("stories") or [])
                if s.get("id") or s.get("title")))
            planned = set(planned_order)
            # A browser/provider failure used to yield a terminal explorer with no story in its result. It then
            # vanished from story_status and the coordinator could call 0/12 tested stories "ALL CLEAR". Missing
            # planned stories are explicit incomplete work and can never pass.
            missing_stories = sorted(planned - set(map(str, ss)))
            incomplete_stories = list(dict.fromkeys(incomplete_stories + missing_stories))
            devs = [c for c in children.values() if c.get("role") == "dev-coordinator"]
            # Recovery at the join is also the durable pagination scheduler. The initial coordinator admission
            # is bounded; after its children settle, admit only the next bounded unseen/incomplete window. The
            # browser/process gates remain the concurrency authority, while story_status is the crash-safe cursor.
            retryable = _qa_gapfill_candidates(
                ss, planned_order, mem.get("gapfills"), _MAX_GAPFILL)
            recovery_scheduled = False
            if retryable:
                batch_size = max(0, int(cc.get("story_batch_size") or 0))
                selected = retryable if batch_size <= 0 else retryable[:max(1, batch_size)]
                story_by_id = {str(s.get("id") or s.get("title")): s for s in (cc.get("stories") or [])
                               if s.get("id") or s.get("title")}
                gf = dict(mem.get("gapfills") or {})
                specs = []
                for story_id in selected:
                    sobj = story_by_id.get(story_id)
                    if sobj is None:
                        continue
                    gf[story_id] = int(gf.get(story_id, 0) or 0) + 1
                    specs.append({
                        "name": f"{a['name']}.aggregate-gapfill-{story_id}-{gf[story_id]}",
                        "role": "qa-explorer", "kind": "worker",
                        "task": f"RECOVER missing or incomplete coverage for story {story_id}",
                        "tool": "qa_explore", "tool_args": {"target_url": cc.get("target_url"),
                        "vision": cc.get("vision"), "token": cc.get("token"),
                        "org": cc.get("org", "0"), "story": sobj,
                        "max_steps": cc.get("max_steps"), "product": cc.get("product"),
                        "repo": cc.get("repo"), "product_revision": mem.get("coverage_revision"),
                            **_qa_prior_recovery(
                                results, story_id, children=children,
                                product_revision=mem.get("coverage_revision"),
                                invalid_actor_ids=mem.get("invalid_recovery_actor_ids"),
                                revision_invalidations=mem.get("revision_invalidations"))}})
                if specs:
                    mem["gapfills"] = gf
                    outcome = _hire_or_request(ctx, a, specs, step)
                    recovery_scheduled = outcome in ("hired", "requested")
                    children = {c["actor_id"]: c for c in store.actors(ctx.run_id, tid)
                                if c["supervisor_id"] == me}
                    _audit(a["name"], "QaAggregateRecovery", outcome,
                           {"stories": selected, "attempts": {sid: gf[sid] for sid in selected},
                            "remaining": len(retryable) - len(selected),
                            "batch_size": batch_size})
            if not recovery_scheduled and internal_reviews:
                # A real internal management case is still active. Completing the run here would silently drop
                # its owner and make the next controller shift start over. Keep the coordinator durable and let
                # the fresh-evidence/review worker or duty manager deliver the next state-change event.
                agg = None
            elif not recovery_scheduled:
                passed = not blocking_stories and not incomplete_stories and not internal_reviews
                verdict = (f"AGENTIC QA — COVERAGE COMPLETE: {len(ss)} stories, no blocking stories and no incomplete coverage "
                           f"(after {len(devs)} fix hand-off(s) + re-test)" if passed else
                           f"AGENTIC QA — {len(blocking_stories)} blocking story(ies), "
                           f"{len(incomplete_stories)} incomplete story(ies), "
                           f"{len(internal_reviews)} internal review(s) after {len(devs)} fix hand-off(s): "
                           f"blocking={', '.join(map(str, blocking_stories)) or 'none'}; "
                           f"incomplete={', '.join(map(str, incomplete_stories)) or 'none'}; "
                           f"internal_review={', '.join(internal_review_stories) or 'none'}")
                agg = {"result": verdict, "ok": True, "passed": passed, "stories": len(ss),
                       "bugs": len(qf), "blocking_stories": blocking_stories,
                       "incomplete_stories": incomplete_stories, "missing_stories": missing_stories,
                       "planned_stories": len(planned), "handed_to_dev": len(devs),
                       "recovery_exhausted": bool(incomplete_stories),
                       "internal_review_required": bool(internal_reviews),
                       "internal_reviews": internal_reviews,
                       "finding_resolutions": finding_resolutions,
                       "story_progress": dict(mem.get("story_progress") or {}),
                       "performance_events": list(mem.get("performance_events") or []),
                       "review_route": "qa-internal-management" if internal_reviews else None}
                _audit(a["name"], "QaVerdict", "executed", agg)
            else:
                agg = None
        elif (a.get("role") or "").lower() == "dev-coordinator":
            if mem.get("internal_reviews"):
                internal_reviews = list(mem["internal_reviews"])
                agg = {"result": "fix mutation withheld pending evidence dispute review",
                       "ok": True, "fixed": False, "internal_review_required": True,
                       "internal_reviews": internal_reviews, "review_route": "qa-internal-management"}
                _audit(a["name"], "DevInternalReviewHandoff", "queued",
                       {"reviews": [r["review_id"] for r in internal_reviews]})
            else:
                # A dev coordinator has exactly one deterministic dev-fixer.  Do not run its structured
                # verdict through a free-form executive-summary model: doing so previously erased
                # `resolved_without_mutation`, leaving a unanimously dismissed false positive permanently
                # open and blocking the next skeptical-jury iteration.
                fixer = next((c for c in children.values() if c.get("role") == "dev-fixer"), None)
                child_payload = results.get(str((fixer or {}).get("actor_id"))) or {}
                outcome = child_payload.get("result") if isinstance(child_payload, dict) else None
                outcome = dict(outcome) if isinstance(outcome, dict) else {}
                context = dict(mem.get("context") or {})
                bug = dict(context.get("bug") or {})
                verdict = outcome.get("verdict") or {}
                reason = verdict.get("reason") if isinstance(verdict, dict) else verdict
                agg = {
                    "result": str(reason or (
                        "independent reviewers resolved the observation without mutation"
                        if outcome.get("resolved_without_mutation") else
                        "dev fixer completed and returned structured evidence")),
                    "ok": bool(child_payload) and child_payload.get("status") != "failed",
                    "fixed": bool(outcome.get("fixed")),
                    "resolved": bool(outcome.get("resolved")),
                    "resolved_without_mutation": bool(outcome.get("resolved_without_mutation")),
                    "internal_review_required": bool(outcome.get("internal_review_required")),
                    "review_route": outcome.get("review_route"),
                    "verdict": verdict,
                    "triage": outcome.get("triage"),
                    "files": list(outcome.get("files") or []),
                    "finding_id": bug.get("finding_id"),
                    "story": bug.get("story"),
                }
                _audit(a["name"], "DevStructuredVerdict", "executed", {
                    "fixed": agg["fixed"],
                    "resolved_without_mutation": agg["resolved_without_mutation"],
                    "internal_review_required": agg["internal_review_required"],
                    "finding_id": agg["finding_id"], "story": agg["story"]})
        else:
            d = _ai_json(a["role"], ctx.repo, _AGGREGATE_PROMPT.format(
                results=json.dumps(results)[:8000], escalations=json.dumps(escalations)[:800]),
                spawner=a["role"])
            agg = {"result": d.get("result", ""), "ok": bool(d.get("ok", True)),
                   "children": len(children), "escalations": len(escalations)}
            _audit(a["name"], "Aggregate", "executed", agg)
        if agg is not None:
            mem["aggregated"] = True
            step.status, step.result = "done", agg
            if a["supervisor_id"]:
                step.emits.append((me, a["supervisor_id"], "done",
                                   {"task": a["assignment"], "result": agg}, None))
            else:                               # the root finished -> the RUN finishes
                step.finish_status = "done" if agg["ok"] else "failed"

    step.memory = {"phase": phase, "results": results, "handled": handled[-60:],
                   "escalations": escalations, "blocked_child": blocked_child,
                   "aggregated": mem.get("aggregated", False),
                   "qa_findings": mem.get("qa_findings", []),   # qa-coordinator's honest-verdict tally
                   "story_status": mem.get("story_status", {}),  # latest pass/blocking per story
                   "retests": mem.get("retests", {}),           # re-test attempts per story (bounded loop)
                   "gapfills": mem.get("gapfills", {}),         # gap-fill attempts per story (bounded loop)
                   "story_progress": mem.get("story_progress", {}),  # subordinate advancement/no-progress
                   "performance_events": mem.get("performance_events", []),  # slow phase observations
                   "coverage_revision": mem.get("coverage_revision"),
                   "revision_generation": int(mem.get("revision_generation") or 0),
                   "revision_invalidations": mem.get("revision_invalidations", []),
                   # Event handling can append several legacy/replayed finding receipts after the entry-time
                   # compaction above.  Compact again at the persistence boundary so the durable queue itself
                   # stays bounded; otherwise the next generation has to load and reason over every duplicate
                   # before it gets a chance to normalize them.
                   "pending_dev_findings": (_qa_compact_pending_findings(mem.get("pending_dev_findings", []))
                                            if (a.get("role") or "").lower() == "qa-coordinator"
                                            else mem.get("pending_dev_findings", [])),
                   "pending_story_continuations": (_qa_compact_story_continuations(
                       mem.get("pending_story_continuations", []), mem.get("story_status"))
                       if (a.get("role") or "").lower() == "qa-coordinator"
                       else mem.get("pending_story_continuations", [])),
                   "internal_reviews": mem.get("internal_reviews", []),          # internal, never CEO-gated
                   "internal_review_states": review_states,
                   "review_observations": review_observations,
                   "review_verification_followers": review_followers,
                   "finding_resolutions": finding_resolutions,
                   "resolution_status_repairs": mem.get("resolution_status_repairs", [])}
    if mem.get("pending_specs") and phase == "hiring":
        step.memory["pending_specs"] = mem["pending_specs"]
    if step.status is None and phase != "new" and a["status"] == "idle":
        step.status = "working"
    _persist(ctx, a, step, evs)


# ================================================================================ the worker pool
class _Ctx:
    def __init__(self, run_id, tenant, repo, human_hook, lease_s, max_steps, deadline=None):
        self.run_id, self.tenant, self.repo = run_id, tenant, repo
        self.human_hook = human_hook
        self.lease_s = lease_s
        self.max_steps = max_steps
        self.deadline = deadline
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.steps_done = 0
        self.active = 0
        self.max_concurrency = 0
        self.last_progress = time.time()
        self.halted = False
        self.errors = []
        self._roles = {}
        self._actor_locks = {}
        self.instance_id = f"{os.getpid()}-{uuid.uuid4().hex}"
        self._claimant = threading.local()

    def current_deadline(self):
        if self.deadline is None:
            return None
        try:
            import jobrunner
            return jobrunner.run_deadline(self.run_id, self.tenant, self.deadline)
        except Exception:
            return self.deadline

    def worker_name(self, index):
        return f"{self.instance_id}:orgw-{index}"

    def set_claimant(self, value):
        self._claimant.value = value

    def claimant(self):
        return getattr(self._claimant, "value", None)

    def spawner_role(self, actor):
        """The supervisor's role, for factory.agent's spawner= threading (cached per parent)."""
        sup = actor.get("supervisor_id")
        if sup is None:
            return None
        if sup not in self._roles:
            p = store.actor(sup, self.tenant)
            self._roles[sup] = p["role"] if p else None
        return self._roles[sup]

    def actor_lock(self, actor_id):
        """One actor may have many pending inbox events, but its memory/result update is a single serialized
        decide-loop. Event-level SKIP LOCKED prevents duplicate event handling; this prevents concurrent
        steps for the same actor from overwriting each other's memory snapshot inside one runtime process."""
        with self.lock:
            lk = self._actor_locks.get(actor_id)
            if lk is None:
                lk = threading.Lock()
                self._actor_locks[actor_id] = lk
            return lk


def _execute_step(ctx, a, evs, wname):
    """ONE durable unit: gates -> decide (AI) -> persist -> heartbeat. Any exception leaves the
    events unprocessed and immediately releases this claimant's inbox lease; the long lease remains the
    process-crash backstop, while ordinary contention/cancellation can retry promptly."""
    try:
        if a["kind"] in ("supervisor", "controller"):
            _supervisor_step(ctx, a, evs)
        else:
            _worker_step(ctx, a, evs)
    except _StepCheckpoint as exc:
        try:
            store.release_event_claims([ev["id"] for ev in evs], ctx.tenant, claimed_by=wname)
        except Exception:
            pass
        _audit(a.get("name", "?"), "StepCheckpoint", "deferred",
               {"worker": wname, "reason": str(exc)[:240]})
    except Exception:
        try:
            store.release_event_claims([ev["id"] for ev in evs], ctx.tenant, claimed_by=wname)
        except Exception:
            pass
        ctx.errors.append(traceback.format_exc())
        _audit(a.get("name", "?"), "StepError", "error", {"worker": wname,
                                                          "trace": traceback.format_exc()[-800:]})


def _release_step_lock(ctx, actor_id, wname):
    try:
        store.release_actor_step(actor_id, ctx.tenant, claimed_by=wname)
    except Exception:
        pass


def _pool_loop(ctx, wname, poll_s, stall_s):
    while not ctx.stop.is_set():
        deadline = ctx.current_deadline()
        if deadline is not None and time.time() >= deadline:
            ctx.stop.set()
            return
        r = store.run(ctx.run_id, ctx.tenant)
        if not r or r["status"] != "running":
            ctx.stop.set()
            return
        if _halted():                            # KILLSWITCH: no step starts while halted
            ctx.halted = True
            ctx.stop.set()
            return
        progressed = False
        try:
            # Dispatch from the indexed durable inbox, not the run's complete employee history. A mature QA
            # campaign may retain hundreds of terminal actors; probing a step lease + empty inbox for each one
            # made a completed tool_result wait tens of seconds before its actor saw it.
            actor_rows = store.actors_with_pending_events(ctx.run_id, ctx.tenant)
        except Exception:
            ctx.errors.append(traceback.format_exc())
            time.sleep(min(1.0, max(0.05, poll_s * 2)))
            continue
        for a in actor_rows:
            if ctx.stop.is_set():
                return
            with ctx.lock:                       # step-budget gate (crash-sim / test knob)
                if ctx.max_steps is not None and ctx.steps_done >= ctx.max_steps:
                    ctx.stop.set()
                    return
            actor_lk = ctx.actor_lock(a["actor_id"])
            if not actor_lk.acquire(blocking=False):
                continue
            step_claimed = False
            try:
                step_claimed = store.claim_actor_step(a["actor_id"], ctx.tenant, claimed_by=wname,
                                                      lease_s=ctx.lease_s)
                if not step_claimed:
                    actor_lk.release()
                    continue
                evs = store.claim_events(a["actor_id"], ctx.tenant, claimed_by=wname,
                                         lease_s=ctx.lease_s)
            except Exception:
                if step_claimed:
                    try:
                        store.release_actor_step(a["actor_id"], ctx.tenant, claimed_by=wname)
                    except Exception:
                        pass
                actor_lk.release()
                ctx.errors.append(traceback.format_exc())
                time.sleep(min(0.5, max(0.02, poll_s)))
                continue
            if not evs:
                _release_step_lock(ctx, a["actor_id"], wname)
                actor_lk.release()
                continue
            if a["status"] in TERMINAL:          # stale mail to a finished actor -> drain
                try:
                    for ev in evs:
                        store.complete_event(ev["id"], ctx.tenant)
                finally:
                    _release_step_lock(ctx, a["actor_id"], wname)
                    actor_lk.release()
                continue
            with ctx.lock:
                ctx.steps_done += 1
                ctx.active += 1
                ctx.max_concurrency = max(ctx.max_concurrency, ctx.active)
            try:
                fresh = store.actor(a["actor_id"], ctx.tenant) or a
                ctx.set_claimant(wname)
                _execute_step(ctx, fresh, evs, wname)
            finally:
                ctx.set_claimant(None)
                with ctx.lock:
                    ctx.active -= 1
                ctx.last_progress = time.time()
                _release_step_lock(ctx, a["actor_id"], wname)
                actor_lk.release()
            progressed = True
        if not progressed:
            with ctx.lock:
                active = ctx.active
            if active == 0 and time.time() - ctx.last_progress > stall_s:
                ctx.stop.set()                   # stalled (no claimable work) -> stop, resumable
                return
            time.sleep(poll_s)


def run_org(run_id, tenant_id, repo=".", workers=2, human_hook=None, max_steps=None,
            lease_s=store.CLAIM_LEASE_S, poll_s=0.15, stall_s=20.0, deadline=None,
            stop_join_s=None):
    """THE engine entry: a pool of N worker threads each repeatedly claiming ANY actor's pending
    events (SKIP LOCKED — no two workers ever process the same event) and executing ONE durable
    step. Returns {"run", "steps", "max_concurrency", "halted", "errors"}. Idempotent + resumable:
    call it again after ANY interruption (crash, kill-switch, max_steps) and it picks up every
    non-terminal actor from its persisted rows; events claimed by dead workers reappear after
    `lease_s`."""
    store.ensure()
    # CRASH-RESUME for tool jobs: re-dispatch any tool-worker that's parked with a dispatched tool but whose
    # background job died with a previous process. Idempotent; fail-open (never blocks the engine start).
    try:
        import jobrunner
        jobrunner.reconcile_parked(store, run_id, tenant_id)
    except Exception:
        pass
    # Free any event a prior pool claimed but never processed, so a multi-level org doesn't stall ~15 minutes.
    # The actor-step owner fence prevents a bounded-shutdown survivor and a successor from committing together.
    try:
        store.release_stale_claims(run_id, tenant_id, older_than_s=max(3, int(stall_s)))
    except Exception:
        pass
    requested_workers = workers
    try:
        db_pool_max = int(os.environ.get("AOS_DB_POOL_MAX", "16"))
    except ValueError:
        db_pool_max = 16
    try:
        worker_ceiling = int(os.environ.get("AOS_ORCHESTRA_WORKER_MAX", "8"))
    except ValueError:
        worker_ceiling = 8
    workers = resourcepressure.runtime_worker_limit(
        workers, cpu_count=os.cpu_count() or 1, db_pool_max=db_pool_max,
        hard_ceiling=worker_ceiling)
    ctx = _Ctx(run_id, tenant_id, repo, human_hook, lease_s, max_steps, deadline=deadline)
    threads = [threading.Thread(target=_pool_loop, args=(ctx, ctx.worker_name(i), poll_s, stall_s),
                                daemon=True, name=f"orgw-{i}")
               for i in range(max(1, int(workers)))]
    for t in threads:
        t.start()
    deadline_exceeded = False
    while any(t.is_alive() for t in threads):
        live_deadline = ctx.current_deadline()
        if live_deadline is not None and time.time() >= live_deadline:
            ctx.stop.set()
            deadline_exceeded = True
            break
        wait_s = 1.0 if live_deadline is None else min(1.0, max(0.0, live_deadline - time.time()))
        for t in threads:
            if t.is_alive():
                t.join(timeout=wait_s)
                break
    # A deadline first stops admission, then gives cooperative workers a small bounded unwind window. Python
    # cannot forcibly kill a thread that is inside a provider or database call, so any survivor is returned as
    # explicit cleanup telemetry and may not be hidden by the caller. The threads are daemon-contained; the
    # durable run fence rejects their commits after the run is halted.
    if deadline_exceeded:
        if stop_join_s is None:
            try:
                stop_join_s = max(0.0, float(os.environ.get("AOS_ORCHESTRA_STOP_JOIN_S", "0.1")))
            except (TypeError, ValueError):
                stop_join_s = 0.1
        else:
            try:
                stop_join_s = max(0.0, float(stop_join_s))
            except (TypeError, ValueError):
                stop_join_s = 0.1
        join_deadline = time.monotonic() + float(stop_join_s)
        for t in threads:
            if t.is_alive():
                t.join(timeout=max(0.0, join_deadline - time.monotonic()))
    threads_alive = sum(1 for t in threads if t.is_alive())
    return {"run": store.run(run_id, tenant_id), "steps": ctx.steps_done,
            "max_concurrency": ctx.max_concurrency, "halted": ctx.halted, "errors": ctx.errors,
            "deadline_exceeded": bool(deadline_exceeded),
            "threads_alive": threads_alive, "workers_requested": requested_workers,
            "workers_admitted": workers, "stop_join_s": float(stop_join_s or 0.0)}


# ============================================================================================
# OFFLINE SELFTEST — factory.agent stubbed (no model/network/spend); REAL local Postgres.
# Proves, all through Postgres rows: 2-worker pool / supervisor + 2 children / blocked ->
# escalate -> resolve -> resume -> aggregate; then a CRASH mid-run (step budget + a dead
# worker's stranded claim) resumed to completion by a fresh run_org; then the kill-switch and
# governance hire-request gates. Touches only its own throwaway tenant's rows; cleans up.
# ============================================================================================
GRANT = "serviceX-cred=LIVE-OK (use v3 endpoint)"


def make_offline_agent():
    """Deterministic offline stand-in for factory.agent driving the canonical storyline:
    payments domain -> lead -> charge worker (blocks on serviceX creds) + refunds worker."""
    def agent(role, repo, task, **kw):
        t = task or ""
        if t.startswith("You are the ORG ARCHITECT"):
            sup = "tech-lead" if "hire-request" in t else "eng-director"
            tree = {"vision": "payments", "root": {"kind": "controller", "title": "Controller",
                    "children": [{"kind": "domain", "name": "payments", "supervisor": sup,
                                  "teams": []}]}}
            return {"rc": 0, "out_full": json.dumps(tree)}
        if t.startswith("You are a LEAD decomposing"):
            kids = [{"role": "backend-engineer", "kind": "worker",
                     "task": "Build the charge endpoint (needs serviceX API creds)"},
                    {"role": "backend-engineer", "kind": "worker",
                     "task": "Build the refunds endpoint"}]
            if "hire-request" in t:
                kids = kids[1:]                  # governance scenario: one simple worker
            return {"rc": 0, "out_full": json.dumps({"org_note": "split", "children": kids})}
        if t.startswith("You are an interrupt-driven LEAD"):
            if "kind=blocked" in t:
                return {"rc": 0, "out_full": json.dumps(
                    {"action": "escalate",
                     "reason": "no API creds for serviceX — needs the controller to provision"})}
            return {"rc": 0, "out_full": json.dumps({"action": "ack"})}
        if t.startswith("You are the CONTROLLER"):
            return {"rc": 0, "out_full": json.dumps(
                {"action": "resolve", "grant": GRANT, "message": "serviceX creds provisioned"})}
        if "A peer asks:" in t:
            return {"rc": 0, "out_full": json.dumps({"answer": "Use us-west-2 with the tenant BYO key."})}
        if t.startswith("You are the LEAD reporting UP"):
            return {"rc": 0, "out_full": json.dumps(
                {"result": "charge + refunds endpoints integrated", "ok": True})}
        if "autonomous AI employee" in t:
            if "serviceX" in t:
                if GRANT in t:                   # the resolution grant reached our memory-context
                    return {"rc": 0, "out_full": json.dumps(
                        {"action": "finish", "result": "charge endpoint built (serviceX v3 OK)"})}
                return {"rc": 0, "out_full": json.dumps(
                    {"action": "emit", "kind": "blocked",
                     "payload": {"reason": "no API creds for serviceX"}})}
            # The SIBLING is deliberately slow (ONE long step, then finishes) so it is provably LIVE while the
            # lead processes the escalate->resolve chain and broadcasts the correction — the broadcast (a
            # context_update event) is created while the sibling is a live child. 2.0s comfortably exceeds the
            # ~1s escalation chain, so the interrupt-broadcast-to-sibling assertion can't flake; and because it
            # FINISHES (not loops), standalone scenarios with no broadcast stay fast.
            time.sleep(2.0)
            return {"rc": 0, "out_full": json.dumps(
                {"action": "finish", "result": "refunds endpoint built"})}
        return {"rc": 0, "out_full": json.dumps({"action": "finish", "result": f"done: {t[:40]}"})}
    return agent


def _selftest():
    import types
    import uuid

    tid = f"orchestra-runtime-selftest-{uuid.uuid4().hex[:8]}"
    real_agent = factory.agent
    ok = True

    def check(cond, label):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + f": {label}")
        ok = ok and bool(cond)

    factory.agent = make_offline_agent()
    try:
        # ================= scenario 1: the full chain, 2-worker pool, all Postgres ==========
        org = create_org(tid, "A payments service: charge + refund endpoints", repo="/tmp/x")
        res = run_org(org["run_id"], tid, repo="/tmp/x", workers=2, stall_s=10)
        r = res["run"]
        check(not res["errors"], f"no step errors ({res['errors'][:1]})")
        check(r and r["status"] == "done" and (r["result"] or {}).get("ok"),
              f"run finished done+ok through Postgres (result={r and r['result']})")

        acts = store.actors(org["run_id"], tid)
        by_kind = {}
        for a in acts:
            by_kind.setdefault(a["kind"], []).append(a)
        lead = by_kind.get("supervisor", [None])[0]
        wrk = by_kind.get("worker", [])
        check(len(acts) == 4 and lead and len(wrk) == 2
              and all(a["status"] == "done" for a in acts),
              f"org tree rows: controller + supervisor + 2 children, all done "
              f"({[(a['name'], a['status']) for a in acts]})")
        tree = store.org_tree(org["run_id"], tid)
        check(len(tree["tree"]) == 1 and len(tree["tree"][0]["reports"]) == 1
              and len(tree["tree"][0]["reports"][0]["reports"]) == 2,
              "org chart nests controller -> lead -> 2 workers from supervisor_id rows")

        evs = store.events(org["run_id"], tid)
        kinds = [e["kind"] for e in evs]
        chain = all(k in kinds for k in ("task", "blocked", "escalate", "resolve",
                                         "context_update", "done"))
        check(chain, f"durable event chain blocked->escalate->resolve->context_update->done "
                     f"(kinds={sorted(set(kinds))})")
        check(all(e["processed_at"] for e in evs), f"every one of {len(evs)} events processed")
        claimers = {e["claimed_by"] for e in evs if e["claimed_by"]}
        check(len(claimers) >= 2 and res["max_concurrency"] >= 2,
              f"REAL parallelism: {len(claimers)} pool workers claimed steps "
              f"({sorted(claimers)}), max in-flight={res['max_concurrency']}")

        blocked_w = next(w for w in wrk if "serviceX" in (w["assignment"] or ""))
        sibling = next(w for w in wrk if w is not blocked_w)
        check(blocked_w["memory"].get("steps", 0) >= 2
              and GRANT in json.dumps(blocked_w["memory"].get("context")),
              f"parked child RESUMED from persisted memory with the grant "
              f"(steps={blocked_w['memory'].get('steps')})")
        blk = next((h for h in lead["memory"]["handled"] if h["kind"] == "blocked"), None)
        check(bool(blk) and blk["action"] == "escalate"
              and sibling["actor_id"] in blk.get("outstanding", []),
              f"interrupt semantics: lead escalated the block WHILE the sibling was still "
              f"working (outstanding={blk and blk['outstanding']})")
        sib_upd = [e for e in evs if e["kind"] == "context_update"
                   and e["to_actor"] == sibling["actor_id"]]
        check(len(sib_upd) >= 1, "the correction was broadcast to the sibling too")
        check(lead["result"] and lead["result"]["ok"] and lead["result"]["children"] == 2,
              "supervisor aggregated only after every child was terminal")

        # ================= scenario 2: CRASH mid-run -> fresh run_org resumes ================
        org2 = create_org(tid, "crash-sim: payments service again", repo="/tmp/x")
        res_a = run_org(org2["run_id"], tid, repo="/tmp/x", workers=2, max_steps=3, stall_s=10)
        r2 = store.run(org2["run_id"], tid)
        acts2 = store.actors(org2["run_id"], tid)
        nonterm = [a for a in acts2 if a["status"] not in TERMINAL]
        check(r2["status"] == "running" and nonterm,
              f"stopped mid-run after {res_a['steps']} steps: run still 'running', "
              f"{len(nonterm)} actors non-terminal")
        # a DEAD worker's stranded claim: claim events, never complete, backdate the lease
        doomed = 0
        for a in acts2:
            doomed += len(store.claim_events(a["actor_id"], tid, claimed_by="doomed-worker"))
        with connection() as c, c.cursor() as cur:
            cur.execute("""UPDATE orchestra_events SET claimed_at = claimed_at - interval '2 hours'
                           WHERE claimed_by='doomed-worker' AND tenant_id=%s""", (tid,))
        check(doomed >= 1, f"simulated crash: {doomed} in-flight events stranded by a dead worker")
        res_b = run_org(org2["run_id"], tid, repo="/tmp/x", workers=2, stall_s=10)
        r2 = store.run(org2["run_id"], tid)
        acts2 = store.actors(org2["run_id"], tid)
        check(r2["status"] == "done" and (r2["result"] or {}).get("ok")
              and all(a["status"] == "done" for a in acts2)
              and all(e["processed_at"] for e in store.events(org2["run_id"], tid)),
              f"fresh run_org picked the org up and FINISHED it (lost nothing; "
              f"{res_b['steps']} resumed steps)")
        w2 = next(a for a in acts2 if "serviceX" in (a["assignment"] or ""))
        check(w2["memory"].get("steps", 0) >= 2 and GRANT in json.dumps(w2["memory"]),
              "the blocked child still went through escalate->resolve->resume after the crash")

        # ================= scenario 3: kill-switch gates every step ==========================
        org3 = create_org(tid, "killswitch-sim: tiny org", repo="/tmp/x")
        fake_ks = types.SimpleNamespace(
            is_halted=lambda scope="global": {"halted": True, "scope": scope, "reason": "test"})
        real_ks_rt, real_ks_store = globals()["killswitch"], store.killswitch
        globals()["killswitch"], store.killswitch = fake_ks, fake_ks
        try:
            res3 = run_org(org3["run_id"], tid, repo="/tmp/x", workers=2, stall_s=5)
        finally:
            globals()["killswitch"], store.killswitch = real_ks_rt, real_ks_store
        check(res3["halted"] and res3["steps"] == 0
              and store.run(org3["run_id"], tid)["status"] == "running",
              "HALT: zero steps execute while the kill-switch is down; the run stays resumable")
        res3b = run_org(org3["run_id"], tid, repo="/tmp/x", workers=2, stall_s=10)
        check(store.run(org3["run_id"], tid)["status"] == "done",
              f"after resume from HALT the same org completes ({res3b['steps']} steps)")

        # ================= scenario 4: governance spawn gate -> hire request =================
        # 'tech-lead' HAS a manifest with can_spawn false -> its decompose may NOT hire directly;
        # the runtime files need_agent up to the controller (can_spawn true), which hires for it.
        org4 = create_org(tid, "hire-request governance sim", repo="/tmp/x")
        res4 = run_org(org4["run_id"], tid, repo="/tmp/x", workers=2, stall_s=10)
        evs4 = store.events(org4["run_id"], tid)
        acts4 = store.actors(org4["run_id"], tid)
        lead4 = next((a for a in acts4 if a["kind"] == "supervisor"), None)
        req = [e for e in evs4 if e["kind"] == "need_agent"
               and (e["payload"] or {}).get("specs")]
        hired_w = [a for a in acts4 if a["kind"] == "worker"]
        check(lead4 and lead4["role"] == "tech-lead" and len(req) == 1
              and req[0]["frm"] == lead4["actor_id"]
              and len(hired_w) == 1 and hired_w[0]["supervisor_id"] == lead4["actor_id"]
              and store.run(org4["run_id"], tid)["status"] == "done",
              f"governance: can_spawn=false lead filed a hire request; the controller hired the "
              f"worker ON ITS BEHALF under the lead; run completed ({res4['steps']} steps)")

        # ================= scenario 5: coworker clarification + disagreement ===============
        # Build the rows directly so this test isolates the communication fabric. A peer can ask another
        # peer for context and receive an answer via a durable context_update. A professional objection
        # routes up to the CEO tier/human hook, then comes back down as a team-wide ruling broadcast.
        r5 = store.start_run(tid, "org-communication-contract")
        ctrl5 = store.spawn_actor(r5["run_id"], tid, "CEO", "controller", kind="controller")
        lead5 = store.spawn_actor(r5["run_id"], tid, "Product Lead", "product-manager", kind="supervisor",
                                  supervisor_id=ctrl5["actor_id"], assignment="coordinate launch")
        a5 = store.spawn_actor(r5["run_id"], tid, "Researcher", "researcher", kind="worker",
                               supervisor_id=lead5["actor_id"], assignment="choose region")
        b5 = store.spawn_actor(r5["run_id"], tid, "Engineer", "backend-engineer", kind="worker",
                               supervisor_id=lead5["actor_id"], assignment="wire deployment")
        store.emit(r5["run_id"], tid, a5["actor_id"], b5["actor_id"], "question",
                   {"question": "Which region should deployment use?"}, corr_id="peer-clarify")
        store.emit(r5["run_id"], tid, a5["actor_id"], lead5["actor_id"], "disagree",
                   {"reason": "The launch directive conflicts with data residency."}, corr_id="ceo-ruling")
        rulings = []

        def _human(ev, data):
            rulings.append({"event": ev["kind"], "data": data})
            return "Revise launch plan for data residency before proceeding."

        res5 = run_org(r5["run_id"], tid, repo="/tmp/x", workers=2, stall_s=2, human_hook=_human)
        evs5 = store.events(r5["run_id"], tid)
        peer_reply = [e for e in evs5 if e["corr_id"] == "peer-clarify"
                      and e["kind"] == "context_update" and e["to_actor"] == a5["actor_id"]]
        ruling = [e for e in evs5 if e["corr_id"] == "ceo-ruling"
                  and e["kind"] == "context_update" and e["to_actor"] in (a5["actor_id"], b5["actor_id"])]
        check(not res5["errors"] and peer_reply and "us-west-2" in json.dumps(peer_reply[0]["payload"]),
              "peer clarification is answered through a durable context_update conversation")
        check(rulings and ruling and "data residency" in json.dumps(ruling),
              "professional disagreement routes to the CEO/human tier and broadcasts the ruling")

    finally:
        factory.agent = real_agent
        with connection() as c, c.cursor() as cur:   # only THIS tenant's throwaway rows
            cur.execute("DELETE FROM orchestra_events WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orchestra_actors WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM orchestra_runs WHERE tenant_id=%s", (tid,))

    print("\n" + ("PASS: durable runtime — every decide-step a Postgres-backed unit; parallel "
                  "pool; escalate->resolve->resume->aggregate; crash-resume lossless; killswitch "
                  "+ governance gates + coworker communication enforced ✅" if ok else "FAIL"))
    return 0 if ok else 1


def _main(argv):
    if not argv or argv[0] == "selftest":
        return _selftest()
    print("usage: runtime.py selftest", file=sys.stderr)
    return 2


__all__ = ["create_org", "run_org", "make_offline_agent", "TERMINAL", "MAX_ACTOR_STEPS", "GRANT"]


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
