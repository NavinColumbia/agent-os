#!/usr/bin/env python3
"""qa_agentic.py — the AGENTIC QA entrypoint (agentic-org phase 5).

Runs QA as a real ORG instead of a procedural loop: a **qa-coordinator** supervisor actor is hired with the
run params in its memory.context; on its `task` it spawns one **qa-explorer** tool-worker per story
(dispatch-and-park → jobrunner runs the browser explore off-loop → emits `finding`/`done` back); the
coordinator's generic supervisor step reacts and aggregates. Durable + crash-resumable on the orchestra
runtime — the coordination is genuine agent conversation over the bus, not a Python for-loop.

    run_agentic_qa(target_url, vision, product=, token=, org=, stories=, ...) -> {run_id, status, findings, ...}

STATUS: the org spine (coordinator hires explorers, tool-workers dispatch-and-park, findings flow back,
run finishes) is wired and tested here end-to-end with a stubbed instant tool. Full PARITY with the
procedural qa_run (gap-fill hires, dev-coordinator hand-off, auditor sign-off at aggregate, honest verdict +
findings.py) is phase 4b — until then `qa_run.py` stays the default. See docs/AGENTIC-QA-ORG.md.
"""
import json
import hashlib
import os
import signal
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for p in (str(_HERE), str(_HERE.parent), str(_HERE.parent / "orchestra")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dbpool import connection  # noqa: E402
import process_assurance  # noqa: E402
import campaign_checkpoint  # noqa: E402


def _terminate_owned_children(grace_s=2.0):
    """Terminate only descendants of this QA worker, including children in their own browser sessions."""
    root = os.getpid()
    snapshots = process_assurance.scan_snapshots()
    root_snap = snapshots.get(root)
    if root_snap is None:
        return 0
    plan = process_assurance.cleanup_plan(root_snap.identity, snapshots)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for expected in plan:
            try:
                # A PID may be reused between discovery and cleanup. Re-read birth identity immediately
                # before every signal; broad process-group signals are intentionally forbidden here.
                if process_assurance.same_process(
                        expected, process_assurance.read_snapshot(expected.pid)):
                    os.kill(expected.pid, sig)
            except (ProcessLookupError, PermissionError): pass
        if sig == signal.SIGTERM:
            until = time.time() + float(grace_s)
            while time.time() < until and any(process_assurance.same_process(
                    p, process_assurance.read_snapshot(p.pid)) for p in plan):
                time.sleep(0.05)
    return sum(1 for p in plan if process_assurance.same_process(
        p, process_assurance.read_snapshot(p.pid)))


def _cleanup_facts(threads_alive, processes_alive, runtime_threads_alive=0):
    """Keep process-contained unwind lag distinct from OS work that can actually be orphaned."""
    threads = max(0, int(threads_alive or 0))
    runtime_threads = max(0, int(runtime_threads_alive or 0))
    processes = max(0, int(processes_alive or 0))
    return {"cleanup_threads_incomplete": threads + runtime_threads,
            "cleanup_runtime_threads_incomplete": runtime_threads,
            "cleanup_processes_incomplete": processes,
            "cleanup_process_contained": bool((threads or runtime_threads) and not processes),
            # Compatibility/telemetry total. Safety gates must use cleanup_processes_incomplete.
            "cleanup_incomplete": threads + runtime_threads + processes}


def _resumable_halted_result(result):
    """Return whether a halted org is an internal checkpoint rather than a user cancellation.

    Normal QA runway checkpoints carry ``timed_out``.  A controller-level financial circuit breaker can
    terminate the owning worker before qa_agentic writes that envelope, leaving the older, explicit
    ``automatic safety stop`` marker instead.  Both are safe to resume after their authority boundary clears;
    an ordinary user/operator cancellation is deliberately excluded.
    """
    result = dict(result or {})
    if result.get("timed_out") or result.get("resumable_checkpoint") is True:
        return True
    reason = str(result.get("reason") or "").strip().lower()
    return bool(result.get("cancelled") is True
                and reason.startswith("automatic safety stop:"))


def _checkpoint_stories_for_resume(path, store, *, tenant, product, target_url, vision, repo,
                                   thread_id=None):
    """Load the immutable manifest from a strictly matching durable campaign without calling a model.

    Story generation is itself paid and nondeterministic.  Generating again before looking at the checkpoint
    could change the signature and strand a valid run behind a brand-new campaign.  This reader fails closed
    on every ownership/context mismatch; the normal story generator remains the fallback for genuinely new
    work.
    """
    if not path or not Path(path).exists():
        return None
    try:
        checkpoint = json.loads(Path(path).read_text())
        if (checkpoint.get("schema") != campaign_checkpoint.SCHEMA
                or checkpoint.get("evidence_policy_revision")
                != campaign_checkpoint.EVIDENCE_POLICY_REVISION
                or str(checkpoint.get("tenant")) != str(tenant)
                or str(checkpoint.get("product")) != str(product)
                or str(checkpoint.get("target_url")) != str(target_url)
                or (str(checkpoint.get("thread_id")) if checkpoint.get("thread_id") is not None else None)
                != (str(thread_id) if thread_id is not None else None)):
            return None
        prior = store.run(int(checkpoint.get("run_id")), tenant)
        if not prior or prior.get("status") not in ("running", "halted"):
            return None
        if prior.get("status") == "halted" and not _resumable_halted_result(prior.get("result")):
            return None
        coordinator = next((actor for actor in store.actors(prior["run_id"], tenant)
                            if (actor.get("role") or "").lower() == "qa-coordinator"), None)
        context = dict(((coordinator or {}).get("memory") or {}).get("context") or {})
        stories = list(context.get("stories") or [])
        expected_repo = str(Path(repo).resolve()) if repo else None
        actual_repo = str(Path(context.get("repo")).resolve()) if context.get("repo") else None
        if (str(context.get("product")) != str(product)
                or str(context.get("target_url")) != str(target_url)
                or str(context.get("vision")) != str(vision)
                or actual_repo != expected_repo
                or (str(context.get("thread_id")) if context.get("thread_id") is not None else None)
                != (str(thread_id) if thread_id is not None else None)):
            return None
        campaign_checkpoint.canonical_manifest(stories)
        signature = campaign_checkpoint.campaign_signature(
            tenant=tenant, product=product, target_url=target_url, vision=vision, repo=repo,
            stories=stories, thread_id=thread_id)
        if not campaign_checkpoint.checkpoint_matches(
                checkpoint, signature=signature, tenant=tenant, product=product,
                target_url=target_url, thread_id=thread_id):
            return None
        return stories
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _reopen_automatic_safety_stop_actors(store, run_id, tenant):
    """Revive only actors terminalized by a proven automatic circuit-breaker checkpoint.

    The controller's emergency cancellation path marks every currently nonterminal actor ``dead`` so no late
    side effect can escape the stopped worker.  Once the authority boundary is cleared, those same durable
    actors—not newly hired replacements—must return to ``blocked`` so jobrunner can resume their fenced tool
    attempts and the coordinator can receive their eventual reports.
    """
    reopened = 0
    for actor in store.actors(run_id, tenant):
        result = dict(actor.get("result") or {})
        if actor.get("status") != "dead" or not _resumable_halted_result(result):
            continue
        store.update_actor(actor["actor_id"], tenant, status="blocked", result=None)
        reopened += 1
    return reopened


def _reopen_legacy_cancelled_explorers(store, run_id, tenant):
    """Repair checkpoints written before cancellation results stayed parked.

    A cooperative deadline used to turn ``cancelled-before-browser-start`` and
    ``cancelled-incomplete`` tool results into terminal actors.  Reopen only those explicit cancellation
    outcomes and remove their stale coordinator aggregation; genuine completed stories remain untouched.
    """
    actors = store.actors(run_id, tenant)
    reopen, story_ids = [], set()
    for actor in actors:
        if actor.get("role") != "qa-explorer" or actor.get("status") != "done":
            continue
        result = ((actor.get("result") or {}).get("result") or {})
        text = " ".join(str(x or "") for x in (result.get("stop_reason"), result.get("error"))).lower()
        if "cancel" not in text and "safety deadline" not in text:
            continue
        reopen.append(actor["actor_id"])
        story = result.get("story") or ((((actor.get("memory") or {}).get("context") or {})
                                           .get("tool_args") or {}).get("story") or {}).get("id")
        if story:
            story_ids.add(str(story))
        context = dict(((actor.get("memory") or {}).get("context") or {}))
        context["tool_attempt"] = int(context.get("tool_attempt") or 0) + 1
        store.update_actor(actor["actor_id"], tenant, status="blocked", result=None,
                           memory={"context": context})

    if reopen:
        coordinator = next((a for a in actors if a.get("role") == "qa-coordinator"), None)
        if coordinator:
            mem = dict(coordinator.get("memory") or {})
            results = {k: v for k, v in dict(mem.get("results") or {}).items()
                       if str(k) not in {str(x) for x in reopen}}
            handled = [h for h in list(mem.get("handled") or [])
                       if not (h.get("kind") == "done" and h.get("frm") in reopen)]
            story_status = {k: v for k, v in dict(mem.get("story_status") or {}).items()
                            if str(k) not in story_ids}
            store.update_actor(coordinator["actor_id"], tenant,
                               memory={"results": results, "handled": handled,
                                       "story_status": story_status})
    return len(reopen)


def _refresh_resumed_coordinator_context(store, run_id, tenant, context):
    """Apply the current execution envelope to a durable resumed campaign.

    Campaign state outlives Python processes, but safety defaults evolve. Keeping
    the coordinator's original context meant every newly hired retest/gap-fill
    inherited an obsolete step cap forever even after the caller removed it. The
    evidence/results remain durable; only current invocation parameters refresh.
    ``update_actor`` merges memory at the top level, preserving story progress.
    """
    updated = 0
    for actor in store.actors(run_id, tenant):
        if (actor.get("role") or "").lower() != "qa-coordinator":
            continue
        old_context = dict((actor.get("memory") or {}).get("context") or {})
        store.update_actor(actor["actor_id"], tenant,
                           memory={"context": {**old_context, **context}})
        updated += 1
    return updated


def _settled_story_ids(story_status):
    """Stories with a real current-revision verdict; incomplete infrastructure/coverage is not progress."""
    return {str(story) for story, status in dict(story_status or {}).items()
            if str(status) in ("clean", "blocking")}


def _pending_dev_mutation_receipts(store, run_id, tenant, actors, *, repo=None):
    """Return in-flight durable fixer receipts that explain a revision shift.

    A resumed QA process inspects the repository before it starts driving the durable actor graph.  When a
    fixer committed product bytes immediately before a checkpoint, its exact ``files`` receipt can still be
    travelling through the dev-fixer -> dev-coordinator -> QA-coordinator event chain.  Full invalidation at
    that point races the ordinary coordinator path, which is responsible for the conservative two-reviewer
    impact decision.  A rolling handoff can already have consumed the event and parked the exact receipt on a
    still-live dev actor, so both transport locations are authoritative. If the process died after bytes were
    written but before the result envelope was persisted, the finding's content-addressed pre-fix manifest
    reconstructs the exact delta. Historical terminal actor results are deliberately ignored: they must never
    make an unrelated external repository change look explained.
    """
    actors = list(actors or [])
    actors_by_id = {int(actor.get("actor_id") or 0): actor for actor in actors}
    roles = {actor_id: str(actor.get("role") or "").lower()
             for actor_id, actor in actors_by_id.items()}
    terminal = {"done", "dead"}
    active_dev_coordinators = {
        actor_id for actor_id, actor in actors_by_id.items()
        if roles.get(actor_id) == "dev-coordinator"
        and str(actor.get("status") or "").lower() not in terminal
    }
    receipts = []
    seen = set()

    def _files(value):
        if not isinstance(value, dict):
            return []
        candidates = [value.get("files")]
        for key in ("result", "partial_result"):
            nested = value.get(key)
            if isinstance(nested, dict):
                candidates.append(nested.get("files"))
        paths = []
        for candidate in candidates:
            if isinstance(candidate, list):
                paths.extend(str(path).strip() for path in candidate if str(path).strip())
        return list(dict.fromkeys(paths))[:200]

    def _record(*, files, source, actor_id=None, event_id=None, kind=None):
        if not files:
            return
        key = (source, actor_id, event_id, tuple(files))
        if key in seen:
            return
        seen.add(key)
        receipts.append({"source": source, "event_id": event_id,
                         "from_actor": actor_id, "kind": kind, "files": files})

    read_events = getattr(store, "events", None)
    events = []
    if callable(read_events):
        try:
            events = read_events(run_id, tenant)
        except Exception:
            events = []
    for event in events or []:
        if event.get("processed_at") is not None or event.get("kind") not in ("tool_result", "done"):
            continue
        payload = event.get("payload") or {}
        result = payload.get("result") or {}
        origin_id = int(event.get("frm") or 0)
        origin_role = roles.get(origin_id, "")
        is_dev_receipt = (payload.get("tool") == "dev_fix"
                          or origin_role in ("dev-fixer", "dev-coordinator"))
        if is_dev_receipt:
            _record(files=_files(result), source="event", actor_id=origin_id,
                    event_id=event.get("id"), kind=event.get("kind"))

    # Checkpoint processing can consume the event before the successor process starts.  In that state the
    # same exact receipt is fenced on the live dev-fixer (or live dev coordinator) until its chain reports the
    # final result to QA.  Requiring a nonterminal coordinator ancestry prevents stale completed fixes from
    # suppressing normal full invalidation forever.
    for actor_id, actor in actors_by_id.items():
        role = roles.get(actor_id, "")
        status = str(actor.get("status") or "").lower()
        if status in terminal or role not in ("dev-fixer", "dev-coordinator"):
            continue
        if role == "dev-coordinator":
            if actor_id not in active_dev_coordinators:
                continue
        else:
            supervisor_id = int(actor.get("supervisor_id") or 0)
            if supervisor_id not in active_dev_coordinators:
                continue
        actor_files = _files(actor.get("result") or {})
        context = dict((actor.get("memory") or {}).get("context") or {})
        tool_args = dict(context.get("tool_args") or {})
        carried = tool_args.get("resume_changed_files")
        if isinstance(carried, list):
            actor_files.extend(str(path).strip() for path in carried if str(path).strip())
        if not actor_files and repo:
            bug = context.get("bug") or tool_args.get("bug")
            if isinstance(bug, dict):
                try:
                    from qa import dev_loop
                    actor_files.extend(dev_loop._changed_files_since_finding(repo, bug))
                except Exception:
                    pass
        _record(files=list(dict.fromkeys(actor_files))[:200], source="actor_state",
                actor_id=actor_id, kind=status)
    return receipts


def _restore_revision_compensation(memory, current_revision, *, impact, prior_status,
                                   reason, recorded_at=None):
    """Restore only verdicts explicitly preserved by an audited selective-impact decision.

    This is the repair path for data already erased by the former startup ordering race.  It cannot invent a
    selective scope: callers must supply a non-full-regression impact record with an explicit preserved set,
    and only statuses named in that set are eligible.  A concurrently completed fresh retest always wins over
    the restored historical value.
    """
    impact = dict(impact or {})
    if impact.get("full_regression") is not False:
        return {}
    preserved = {str(story) for story in (impact.get("preserved_story_ids") or []) if story}
    if not preserved:
        return {}
    allowed_statuses = {"clean", "blocking", "incomplete", "internal_review"}
    candidates = {
        str(story): str(status) for story, status in dict(prior_status or {}).items()
        if str(story) in preserved and str(status) in allowed_statuses
    }
    if not candidates:
        return {}
    current = dict(memory.get("story_status") or {})
    restored = {story: status for story, status in candidates.items() if story not in current}
    merged = {**candidates, **current}
    history = list(memory.get("revision_compensations") or [])[-19:]
    record = {
        "revision": current_revision,
        "generation": int(memory.get("revision_generation") or 0),
        "reason": str(reason or "repaired startup revision reconciliation")[:1500],
        "impact": impact,
        "restored_story_status": restored,
        "recorded_at": float(recorded_at if recorded_at is not None else time.time()),
    }
    if not any(item.get("revision") == current_revision
               and item.get("reason") == record["reason"]
               and item.get("restored_story_status") == restored for item in history):
        history.append(record)
    memory["story_status"] = merged
    memory["revision_compensations"] = history
    memory["revision_reconciliation"] = {
        "state": "selective_compensation", "to": current_revision,
        "changed_files": list(impact.get("changed_files") or []),
        "preserved_story_ids": sorted(candidates),
        "reason": record["reason"],
    }
    return restored


def _reconcile_compensated_false_positive_statuses(memory):
    """Reopen, rather than preserve, stale blocking labels whose latest finding was dismissed.

    A false-positive review normally writes ``incomplete`` (or ``clean`` when its fresh browser bundle is
    complete). A crash-window full invalidation can capture the older ``blocking`` label first, and later
    selective compensation used to restore that label after the dismissal had already been recorded. Match
    only the latest retained finding for the story and a highly specific same-story resolution; then reopen
    it for focused completion. This never promotes a story to clean and never hides an unresolved finding.
    """
    statuses = dict(memory.get("story_status") or {})
    findings = [item for item in (memory.get("qa_findings") or []) if isinstance(item, dict)]
    resolutions = [item for item in (memory.get("finding_resolutions") or [])
                   if isinstance(item, dict)
                   and item.get("disposition") == "verified_false_positive"]

    def normalized(value):
        return " ".join(str(value or "").casefold().split())

    def same_finding(finding, resolution):
        finding_id = str(finding.get("finding_id") or "")
        resolution_id = str(resolution.get("finding_id") or "")
        if finding_id and resolution_id:
            return finding_id == resolution_id
        left = normalized(finding.get("title") or finding.get("bug"))
        right = normalized(resolution.get("title"))
        # Legacy coordinator summaries retained a 120-character title but dropped the finding id. A long
        # shared prefix within the same story is the stable identity available for those historical rows.
        return bool(len(left) >= 80 and len(right) >= 80 and left[:80] == right[:80])

    repaired = {}
    for story, status in list(statuses.items()):
        if status not in {"blocking", "internal_review"}:
            continue
        latest = next((item for item in reversed(findings)
                       if str(item.get("story") or "") == str(story)), None)
        if latest is None:
            continue
        dismissed = any(str(item.get("story") or "") == str(story)
                        and same_finding(latest, item) for item in resolutions)
        if not dismissed:
            continue
        statuses[story] = "incomplete"
        repaired[str(story)] = {"from": status, "to": "incomplete",
                                "reason": "latest retained finding has a verified false-positive resolution"}
    if repaired:
        history = list(memory.get("resolution_status_repairs") or [])[-19:]
        history.append({"recorded_at": time.time(), "stories": repaired})
        memory["story_status"] = statuses
        memory["resolution_status_repairs"] = history
    return repaired


def _reconcile_causal_coverage_statuses(memory):
    """Reopen clean stories whose durable receipts violate browser-action chronology.

    The explorer owns the detailed ledger, but the durable coordinator owns whether a story is considered
    settled. A rolling process may therefore resume after a newer explorer version learns that an old receipt
    was invalid. Re-run only the deterministic ledger migration against the latest result for each story and
    demote invalid clean verdicts to incomplete; never promote a story or discard the original evidence.
    """
    try:
        import qa_explorer
    except Exception:
        return {}
    results = dict(memory.get("results") or {})
    latest = {}
    for actor_id, envelope in results.items():
        if not isinstance(envelope, dict):
            continue
        result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
        story = str(envelope.get("story") or result.get("story") or "").strip()
        if story and isinstance(result.get("coverage"), list):
            latest[story] = (actor_id, envelope, result)

    statuses = dict(memory.get("story_status") or {})
    progress = dict(memory.get("story_progress") or {})
    gapfills = dict(memory.get("gapfills") or {})
    repaired = {}
    for story, (actor_id, envelope, result) in latest.items():
        if statuses.get(story) != "clean":
            continue
        before = [dict(item) for item in result.get("coverage") or [] if isinstance(item, dict)]
        after = qa_explorer._migrate_compound_coverage_ledger(before)
        before_by_aspect = {str(item.get("aspect")): item for item in before}
        invalid = [str(item.get("aspect")) for item in after
                   if before_by_aspect.get(str(item.get("aspect")), {}).get("covered")
                   and not item.get("covered")]
        if not invalid:
            continue
        updated_result = {**result, "coverage": after,
                          "stop_reason": "causal-evidence-reopened"}
        updated_envelope = {**envelope, "result": updated_result}
        results[actor_id] = updated_envelope
        statuses[story] = "incomplete"
        prior_progress = dict(progress.get(story) or {})
        covered_aspects = [str(item.get("aspect")) for item in after if item.get("covered")]
        progress[story] = {
            **prior_progress,
            "covered": len(covered_aspects), "coverage_total": len(after),
            "covered_aspects": covered_aspects, "stop_reason": "causal-evidence-reopened",
            "advanced": False, "no_progress": 0,
        }
        gapfills[story] = 0
        repaired[story] = {
            "from": "clean", "to": "incomplete", "invalid_aspects": invalid,
            "reason": "durable receipt predates its required business-state creation proof",
        }
    if repaired:
        history = list(memory.get("causal_status_repairs") or [])[-19:]
        history.append({"recorded_at": time.time(), "stories": repaired})
        memory.update({"results": results, "story_status": statuses,
                       "story_progress": progress, "gapfills": gapfills,
                       "causal_status_repairs": history})
    return repaired


def _invalidate_resumed_revision(store, run_id, tenant, current_revision, *, repo=None, runtime_mod=None):
    """Invalidate coverage captured against a different product revision before resuming tools.

    A worker can checkpoint after a fixer mutated the repo but before the coordinator consumed its result.
    The durable actor graph must survive, but prior clean story verdicts and portable browser state cannot be
    treated as proof of the new release.  Runtime performs the same invalidation immediately after ordinary
    fixer completion; this closes the process-crash window between mutation and that event.
    """
    if not current_revision:
        return 0
    actors = store.actors(run_id, tenant)

    # Apply terminal evidence decisions that may have raced a broad invalidation/compensation handoff. This
    # is a scheduling repair only: dismissed blockers become incomplete and must still earn focused browser
    # completion on the current revision before they can be clean.
    for actor in actors:
        if (actor.get("role") or "").lower() != "qa-coordinator":
            continue
        memory = dict(actor.get("memory") or {})
        causal_repaired = _reconcile_causal_coverage_statuses(memory)
        repaired = _reconcile_compensated_false_positive_statuses(memory)
        if causal_repaired or repaired:
            actor["memory"] = memory
            store.update_actor(actor["actor_id"], tenant, memory=memory)

    # Repair the exact rolling-handoff race where startup deferred to a live writer receipt, but an older
    # parent aggregate subsequently erased the ledger as a global mutation after losing the writer's nested
    # ``files`` list. The reconciliation record fences exact actor IDs/files and the invalidation audit keeps
    # the erased statuses. Re-run the normal two-reviewer impact decision and restore only explicitly
    # preserved stories; current fresh results always win in _restore_revision_compensation.
    if runtime_mod is not None and hasattr(runtime_mod, "_qa_revision_impact_scope"):
        actors_by_id = {int(item.get("actor_id") or 0): item for item in actors}
        recovered_receipts = _pending_dev_mutation_receipts(
            store, run_id, tenant, actors, repo=repo)
        for actor in actors:
            if (actor.get("role") or "").lower() != "qa-coordinator":
                continue
            memory = dict(actor.get("memory") or {})
            reconciliation = dict(memory.get("revision_reconciliation") or {})
            invalidation = next((dict(item) for item in reversed(
                list(memory.get("revision_invalidations") or []))
                if item.get("to") == current_revision
                # Legacy startup invalidations did not persist this flag; absence meant the same
                # fail-closed full invalidation. Only an explicit False is selective.
                and item.get("full_regression") is not False
                and item.get("stale_story_status")), None)
            deferred = (reconciliation.get("state") == "deferred_pending_dev_receipt"
                        and reconciliation.get("to") == current_revision)
            live_receipts = [item for item in recovered_receipts
                             if item.get("source") == "actor_state"]
            receipt_files = list(reconciliation.get("changed_files") or []) if deferred else []
            receipt_files.extend(path for item in live_receipts
                                 for path in item.get("files") or [])
            receipt_files = list(dict.fromkeys(receipt_files))[:200]
            receipt_ids = ([int(item) for item in (reconciliation.get("actor_ids") or [])
                            if str(item).isdigit()] if deferred else [])
            receipt_ids.extend(int(item.get("from_actor") or 0) for item in live_receipts
                               if int(item.get("from_actor") or 0))
            receipt_ids = list(dict.fromkeys(receipt_ids))
            receipt_actors = [actors_by_id[item] for item in receipt_ids if item in actors_by_id]
            if not invalidation or not receipt_files or not receipt_actors:
                continue
            source = next((candidate for candidate in receipt_actors
                           if isinstance(((candidate.get("memory") or {}).get("context") or {}).get("bug"), dict)
                           or isinstance((((candidate.get("memory") or {}).get("context") or {})
                                          .get("tool_args") or {}).get("bug"), dict)), receipt_actors[0])
            source_context = dict((source.get("memory") or {}).get("context") or {})
            source_args = dict(source_context.get("tool_args") or {})
            bug = dict(source_context.get("bug") or source_args.get("bug") or {})
            story = str(bug.get("story") or "").strip()
            stories = list((memory.get("context") or {}).get("stories") or [])
            if not story or not stories:
                continue
            receipt = (runtime_mod._qa_dev_completion_receipt(
                receipt_ids[0], actors, source.get("result") or {})
                if hasattr(runtime_mod, "_qa_dev_completion_receipt") else
                {"summaries": [], "actor_ids": receipt_ids})
            impact = runtime_mod._qa_revision_impact_scope(
                stories, receipt_files, story, bug, repo=repo or ".",
                change_summary={"deferred_handoff_recovery": True,
                                "durable_completion_summaries": receipt.get("summaries"),
                                "durable_change_diffs": receipt.get("change_diffs"),
                                "receipt_actor_ids": receipt.get("actor_ids")})
            if impact.get("full_regression") is not False:
                # Do not leave a completed impact decision looking perpetually pending. The original broad
                # invalidation remains fail-closed, but operators and later handoffs can now distinguish a
                # reviewed full scope from a lost dev receipt or a stuck reviewer.
                memory["revision_reconciliation"] = {
                    "state": "full_invalidation_confirmed", "from": reconciliation.get("from"),
                    "to": current_revision, "actor_ids": receipt_ids,
                    "changed_files": list(impact.get("changed_files") or receipt_files),
                    "impacted_story_ids": list(impact.get("impacted_story_ids") or []),
                    "reason": str(impact.get("reason") or "independent impact review required full regression")[:1500],
                    "reviewed_at": time.time(),
                }
                store.update_actor(actor["actor_id"], tenant, memory={
                    "revision_reconciliation": memory["revision_reconciliation"]})
                continue
            restored = _restore_revision_compensation(
                memory, current_revision, impact=impact,
                prior_status=invalidation.get("stale_story_status") or {},
                reason="recovered descendant mutation receipt after rolling handoff")
            if restored:
                store.update_actor(actor["actor_id"], tenant, memory={
                    "story_status": memory.get("story_status"),
                    "revision_compensations": memory.get("revision_compensations"),
                    "revision_reconciliation": memory.get("revision_reconciliation")})

    coordinator_shifts = []
    for actor in actors:
        if (actor.get("role") or "").lower() != "qa-coordinator":
            continue
        memory = dict(actor.get("memory") or {})
        context = dict(memory.get("context") or {})
        previous = memory.get("coverage_revision") or context.get("product_revision")
        if previous and previous != current_revision:
            coordinator_shifts.append((actor, memory, previous))
    pending_receipts = (_pending_dev_mutation_receipts(store, run_id, tenant, actors, repo=repo)
                        if coordinator_shifts else [])
    if pending_receipts:
        # Do not rewrite active explorer checkpoints either. The atomic durable event path will consume the
        # receipt, calculate selective impact, invalidate only affected stories, and schedule a fresh retest.
        for actor, memory, previous in coordinator_shifts:
            store.update_actor(actor["actor_id"], tenant, memory={
                "revision_reconciliation": {
                    "state": "deferred_pending_dev_receipt", "from": previous,
                    "to": current_revision,
                    "event_ids": [item["event_id"] for item in pending_receipts
                                  if item.get("event_id") is not None],
                    "actor_ids": sorted({item["from_actor"] for item in pending_receipts
                                         if item.get("source") == "actor_state"}),
                    "changed_files": sorted({path for item in pending_receipts
                                             for path in item["files"]}),
                }})
        return 0
    changed = 0
    for actor in actors:
        memory = dict(actor.get("memory") or {})
        if (actor.get("role") or "").lower() == "qa-coordinator":
            context = dict(memory.get("context") or {})
            previous = memory.get("coverage_revision") or context.get("product_revision")
            if previous and previous != current_revision:
                # Upgrade repair: older workers hashed PAUSED.html, a generated financial-control marker. If
                # re-including exactly that marker reproduces the prior digest, executable product bytes are
                # identical and prior evidence is still valid. Rebuild the story ledger from durable explorer
                # results because an older worker may already have erased it before this policy loaded.
                legacy_revisions = {}
                if repo:
                    legacy_revisions.setdefault(campaign_checkpoint.repo_revision(
                        repo, include_control_markers=True),
                        "generated PAUSED.html removed from product revision policy")
                    legacy_revisions.setdefault(campaign_checkpoint.repo_revision(
                        repo, include_evidence_files=True),
                        "non-runtime test/docs evidence removed from product revision policy")
                    legacy_revisions.setdefault(campaign_checkpoint.repo_revision(
                        repo, include_control_markers=True, include_evidence_files=True),
                        "generated control and non-runtime evidence files removed from product revision policy")
                    legacy_revisions.pop(None, None)
                equivalence_reason = legacy_revisions.get(previous)
                if equivalence_reason:
                    recovered = {}
                    for worker in sorted(actors, key=lambda item: int(item.get("actor_id") or 0)):
                        if worker.get("role") != "qa-explorer" or worker.get("status") != "done":
                            continue
                        worker_context = dict((worker.get("memory") or {}).get("context") or {})
                        tool_args = dict(worker_context.get("tool_args") or {})
                        worker_revision = tool_args.get("product_revision")
                        if worker_revision not in (previous, current_revision):
                            continue
                        payload = dict(worker.get("result") or {})
                        result = dict(payload.get("result") or {})
                        story = result.get("story") or ((tool_args.get("story") or {}).get("id"))
                        if not story:
                            continue
                        if runtime_mod is not None:
                            recovered[str(story)] = runtime_mod._qa_story_status(payload)
                        else:
                            stop = str(result.get("stop_reason") or "").lower()
                            recovered[str(story)] = (
                                "blocking" if payload.get("blocking_found") or result.get("bugs")
                                else "incomplete" if any(key in stop for key in (
                                    "incomplete", "deadline", "stalled", "capacity", "exhausted"))
                                else "clean")
                    # An older worker may already have performed the false full invalidation before this policy
                    # loaded. Its audit entry retains the exact prior ledger; restore that durable evidence.
                    for invalidation in reversed(list(memory.get("revision_invalidations") or [])):
                        if invalidation.get("to") == previous and invalidation.get("stale_story_status"):
                            recovered.update(dict(invalidation.get("stale_story_status") or {}))
                            break
                    recovered.update(dict(memory.get("story_status") or {}))
                    history = list(memory.get("revision_equivalences") or [])[-19:]
                    history.append({"from": previous, "to": current_revision,
                                    "reason": equivalence_reason})
                    store.update_actor(actor["actor_id"], tenant, memory={
                        "story_status": recovered, "coverage_revision": current_revision,
                        "revision_equivalences": history})
                    for worker in actors:
                        if (worker.get("role") != "qa-explorer"
                                or worker.get("status") in ("done", "dead")):
                            continue
                        wm = dict(worker.get("memory") or {})
                        wc = dict(wm.get("context") or {})
                        wa = dict(wc.get("tool_args") or {})
                        if wa.get("product_revision") in (previous, current_revision):
                            wa["product_revision"] = current_revision
                            wc["tool_args"] = wa
                            store.update_actor(worker["actor_id"], tenant, memory={"context": wc})
                    continue
                if runtime_mod is not None and hasattr(runtime_mod, "_qa_invalidate_revision"):
                    stale = runtime_mod._qa_invalidate_revision(
                        memory, current_revision,
                        impact={"reason": "product revision changed between QA worker shifts"},
                        tenant_id=tenant)
                    memory["revision_reconciliation"] = {
                        **dict(memory.get("revision_reconciliation") or {}),
                        "reason": "no pending durable mutation receipt",
                    }
                    store.update_actor(actor["actor_id"], tenant, memory=memory)
                else:
                    stale = sorted(map(str, dict(memory.get("story_status") or {})))
                    generation = int(memory.get("revision_generation") or 0) + 1
                    history = list(memory.get("revision_invalidations") or [])[-19:]
                    history.append({"from": previous, "to": current_revision,
                                    "stale_story_ids": stale, "generation": generation,
                                    "stale_story_status": dict(memory.get("story_status") or {}),
                                    "reason": "product revision changed between QA worker shifts"})
                    store.update_actor(actor["actor_id"], tenant, memory={
                        "story_status": {}, "gapfills": {}, "coverage_revision": current_revision,
                        "revision_generation": generation, "revision_invalidations": history,
                        "revision_reconciliation": {"state": "full_invalidation", "from": previous,
                                                    "to": current_revision,
                                                    "reason": "no pending durable mutation receipt"}})
                changed += len(stale) or 1
            elif not previous:
                store.update_actor(actor["actor_id"], tenant,
                                   memory={"coverage_revision": current_revision})
        elif ((actor.get("role") or "").lower() == "qa-explorer"
              and actor.get("status") not in ("done", "dead")):
            context = dict(memory.get("context") or {})
            tool_args = dict(context.get("tool_args") or {})
            prior_revision = tool_args.get("product_revision")
            if prior_revision and prior_revision != current_revision:
                for key in ("resume_covered", "resume_coverage", "resume_state_path",
                            "resume_steps_detail"):
                    tool_args.pop(key, None)
                tool_args["product_revision"] = current_revision
                context["tool_args"] = tool_args
                store.update_actor(actor["actor_id"], tenant, memory={"context": context})
    return changed


def _write_campaign_checkpoint(path, *, store, run_id, tenant, signature, product, target_url,
                               thread_id, stories, batch_size, status="running"):
    """Publish the process locator from durable coordinator state; safe to call after every drive pass."""
    if not path:
        return None
    coordinator = next((actor for actor in store.actors(run_id, tenant)
                        if (actor.get("role") or "").lower() == "qa-coordinator"), None)
    memory = dict((coordinator or {}).get("memory") or {})
    document = campaign_checkpoint.checkpoint_document(
        run_id=run_id, signature=signature, tenant=tenant, product=product,
        target_url=target_url, thread_id=thread_id, stories=stories,
        story_status=memory.get("story_status"), batch_size=batch_size, status=status)
    document["product_revision"] = memory.get("coverage_revision")
    document["revision_generation"] = int(memory.get("revision_generation") or 0)
    return campaign_checkpoint.write_checkpoint(path, document)


def _materialize_story_artifacts(per_story, evidence_dir):
    """Copy explorer-owned visual proof into the final auditor dossier.

    The explorer and aggregate report intentionally use different lifecycle directories.  Returning only
    absolute references made the final dossier non-portable: review.py inventories its own screenshots/videos
    folders and therefore saw zero evidence.  Copy only bounded regular image/video files rooted beneath the
    tool-created artifact directory and return a path remap for step records.
    """
    import shutil
    evidence_dir = Path(evidence_dir)
    shot_out, video_out = evidence_dir / "screenshots", evidence_dir / "videos"
    inspection_out = evidence_dir / "inspections"
    shot_out.mkdir(parents=True, exist_ok=True); video_out.mkdir(parents=True, exist_ok=True)
    inspection_out.mkdir(parents=True, exist_ok=True)
    remap = {}
    for sid, result in sorted(dict(per_story or {}).items()):
        root_value = (result or {}).get("artifact_dir")
        if not root_value:
            continue
        try:
            root = Path(root_value).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        safe_sid = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(sid))[:60] or "story"
        candidates = list((root / "screenshots").glob("*.png"))
        candidates += list((root / "videos").glob("*.mp4"))
        candidates += list((root / "videos").glob("*.webm"))
        candidates += list(root.glob("recorder-inspection*.json"))
        video = (result or {}).get("video")
        if video:
            candidates.append(Path(video))
        seen_sources = set()
        for source in candidates[:250]:
            try:
                if source.is_symlink() or not source.is_file():
                    continue
                resolved = source.resolve(strict=True)
                resolved.relative_to(root)
                if resolved in seen_sources:
                    continue
                seen_sources.add(resolved)
                size = resolved.stat().st_size
                suffix = resolved.suffix.lower()
                if suffix == ".png":
                    if size > 25 * 1024 * 1024:
                        continue
                    target_dir = shot_out
                elif suffix in {".mp4", ".webm"}:
                    if size > 500 * 1024 * 1024:
                        continue
                    try:
                        import artifacts
                        if artifacts.probe_media(resolved) is None:
                            continue
                    except Exception:
                        continue
                    target_dir = video_out
                elif suffix == ".json" and resolved.name.startswith("recorder-inspection"):
                    if size > 25 * 1024 * 1024:
                        continue
                    target_dir = inspection_out
                else:
                    continue
                destination = target_dir / f"{safe_sid}--{resolved.name}"
                shutil.copy2(resolved, destination)
                remap[str(source)] = str(destination)
                remap[str(resolved)] = str(destination)
            except (OSError, RuntimeError, ValueError):
                continue
    return remap


def _audit_gap_fingerprint(audit_verdict):
    items = []
    for key in ("skipped_flows", "unbacked_claims", "evidence_gaps"):
        for value in (audit_verdict or {}).get(key) or []:
            normalized = " ".join(str(value).lower().split())
            if normalized:
                items.append(normalized)
    return hashlib.sha256(json.dumps(sorted(set(items))).encode()).hexdigest() if items else None


def _audit_gap_stories(audit_verdict, vision, stories, *, repo=None, iteration=1):
    """Have the QA director turn jury feedback into executable browser work.

    Audit rejection is feedback to the team, not a reason to send the same evidence to a builder or CEO.  The
    director consolidates duplicate prose into a small story set while the deterministic fallback preserves
    every gap if the planning call is unavailable.
    """
    gaps = []
    for key in ("skipped_flows", "unbacked_claims", "evidence_gaps"):
        for value in (audit_verdict or {}).get(key) or []:
            value = " ".join(str(value).split())
            if value and value.lower() not in {item.lower() for item in gaps}:
                gaps.append(value)
    if not gaps:
        return []
    prompt = f"""You are the QA director. An independent jury rejected a browser-QA dossier. Convert its
feedback into 1-3 minimal, non-overlapping, executable browser stories. These are TEST gaps, not requests to
change product code. Consolidate duplicate wording. Stay inside the original vision. Every story must include
id, title, goal, steps, expected, and coverage (2-6 ATOMIC observable aspects). Never combine two different
gestures into one coverage item: Tab and Shift+Tab, Enter and Space, reload and re-entry, and pointer and
keyboard activation are separate facts. When requested, explicitly name the executable proof mechanism:
true refresh uses reload; rapid input uses one timestamped burst; reverse traversal uses Shift+Tab; an
accessibility-tree exposure requires a changed Chromium Accessibility-domain event in addition to DOM
live-region mutation. Never claim that a screen reader announced/spoke text unless an actual AT driver produced
that evidence; this browser harness proves the Chromium AX contract, not audio from NVDA/VoiceOver/Orca.
If the jury explicitly requires real screen-reader output, name that requirement literally as an
"actual assistive-technology driver" coverage item so capability admission routes it to management before any
browser action; never imitate it with clicks/keypresses. Do not invent a reset-versus-persistence requirement
that is absent from the original vision/stories: in that case verify the observed state plus post-reload and
post-re-entry operability without calling either reset or persistence a defect. Include keyboard/accessibility,
reload/re-entry, timing, or repeated-use only when the jury named it. Reply ONLY a JSON array.

ORIGINAL VISION: {vision}
ORIGINAL STORIES: {json.dumps(stories, default=str)[:7000]}
JURY GAPS: {json.dumps(gaps, default=str)[:10000]}"""
    raw_stories = []
    try:
        import factory
        import story_gen
        response = factory.agent("qa-security", repo or str(_HERE.parent), prompt,
                                 timeout=240, retries=0, light=True)
        raw_stories = story_gen._parse_stories(
            (response or {}).get("out_full") or (response or {}).get("out") or "")
    except Exception:
        raw_stories = []
    if not raw_stories:
        raw_stories = [{"title": "Close independent QA audit gaps", "goal": "Ground every jury gap",
                        "steps": gaps, "expected": "Every jury gap has inspectable browser evidence.",
                        "coverage": gaps}]
    planned = []
    for index, raw in enumerate(raw_stories[:3]):
        if not isinstance(raw, dict):
            continue
        steps = raw.get("steps") if isinstance(raw.get("steps"), list) else []
        coverage = raw.get("coverage") if isinstance(raw.get("coverage"), list) else []
        expected = raw.get("expected") or raw.get("expected_outcome") or ""
        if not steps and not expected:
            continue
        identity = hashlib.sha256(json.dumps(
            {"iteration": iteration, "title": raw.get("title"), "steps": steps,
             "expected": expected}, sort_keys=True, default=str).encode()).hexdigest()[:12]
        planned.append({
            "id": f"AUDIT-{iteration}-{identity}",
            "title": str(raw.get("title") or f"Audit gap verification {index + 1}")[:180],
            "goal": str(raw.get("goal") or "Close the independent QA jury's evidence gaps")[:1000],
            "steps": [str(item)[:1000] for item in steps if str(item).strip()],
            "expected": str(expected)[:2000],
            "expected_outcome": str(expected)[:2000],
            "coverage": [str(item)[:1000] for item in (coverage or steps or gaps)
                         if str(item).strip()],
            "category": "audit-regression", "source": "regression",
            "bug_ref": json.dumps({"audit": audit_verdict, "iteration": iteration}, default=str)[:4000],
        })
    return planned


def _should_run_audit(file_findings, timed_out, status, candidate_clean=True):
    """A terminal, complete QA dossier is auditable; a halted checkpoint is not.

    Paying a jury to judge partial evidence after an operator/safety stop wastes model work and can create
    misleading follow-up stories from a deliberately incomplete campaign.
    """
    return bool(file_findings and candidate_clean and not timed_out and status == "done")


def _should_file_governed_findings(file_findings, timed_out, status):
    """Only a terminal QA decision may fan observations into the company backlog.

    A halted/time-sliced campaign already has a durable coordinator, repair queue, evidence cases, and
    continuations. Filing its event history at every checkpoint creates a second owner for the same unfinished
    work and was the source of hundreds of duplicate finding/task rows in long campaigns.
    """
    return bool(file_findings and not timed_out and status == "done")


def _governed_finding_key(finding):
    """Stable identity for retry-safe filing of one compacted QA observation."""
    item = finding or {}
    if item.get("finding_id"):
        return str(item["finding_id"])
    identity = {
        "story": str(item.get("story") or ""),
        "title": " ".join(str(item.get("title") or item.get("bug") or "").lower().split()),
        "detail": " ".join(str(item.get("detail") or "").lower().split()),
    }
    return "semantic:" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def _should_expand_audit(audit_rejected, candidate_clean, status, timed_out, open_findings):
    """A jury rejection expands an otherwise-clean candidate into fresh QA work.

    ``report["clean"]`` is deliberately downgraded after a rejected audit.  Expansion must therefore use
    the pre-audit candidate state; consulting the downgraded report would suppress the exact follow-up the
    skeptical jury requested and strand a legitimate evidence gap.
    """
    return bool(audit_rejected and candidate_clean and status == "done"
                and not timed_out and not open_findings)


def _release_cleanliness_verdict(base_verdict, open_findings, clean):
    """Make the human-facing headline agree with the machine ship gate.

    The coordinator can truthfully finish every story ledger while unresolved defects remain in its durable
    repair queue. Calling that state ``ALL CLEAR`` caused operators and downstream managers to believe QA had
    passed even though ``passed`` was correctly false. Keep coverage completion and release cleanliness as
    separate facts, and summarize the unresolved risk without dumping the full evidence dossier into chat.
    """
    base = str(base_verdict or "agentic QA complete")
    pending = [item for item in (open_findings or []) if isinstance(item, dict)]
    if clean and not pending:
        return base.replace("COVERAGE COMPLETE", "ALL CLEAR", 1)
    if not pending:
        return base
    severity_order = ("critical", "high", "medium", "low")
    counts = {name: sum(str(item.get("severity") or "").lower() == name for item in pending)
              for name in severity_order}
    severity = ", ".join(f"{count} {name}" for name, count in counts.items() if count) or "unclassified"
    blocking = sum(bool(item.get("blocking")) for item in pending)
    return (f"AGENTIC QA — RELEASE NOT CLEAN: story coverage is complete, but {len(pending)} unresolved "
            f"finding(s) remain ({severity}; {blocking} marked blocking). Shipping and delivery remain held.")


_FINDING_PASS_DISPOSITIONS = frozenset({
    "verified_false_positive",
    "superseded_by_fresh_evidence",
    "superseded_by_current_revision",
    "superseded_by_invalid_recovery",
})

# Only an adjudication that says the explorer's observation itself was false may repair that explorer's
# otherwise-complete receipt.  The other pass dispositions retire a finding because different evidence or a
# newer revision superseded it; they must not make the original, potentially stale receipt authoritative.
_RESULT_FINDING_COMPENSATION_DISPOSITIONS = frozenset({"verified_false_positive"})


def _result_with_resolved_findings(result, actor_id, events, resolutions):
    """Return a copy whose bug count reflects exact, durable false-positive adjudications.

    Explorer results are immutable receipts, so a later ``finding_resolution`` cannot rewrite their original
    ``bugs`` field.  Release reduction nevertheless has to compose the two durable facts.  Compensation is
    intentionally fail-closed: every reported bug must have one exact finding event from this actor, and every
    finding id must have a matching ``verified_false_positive`` disposition for the same story.  Title-only
    matches and supersession dispositions are insufficient.
    """
    original = dict(result or {})
    raw_bugs = original.get("bugs") or 0
    bug_count = (len(raw_bugs) if isinstance(raw_bugs, (list, tuple, dict, set))
                 else int(raw_bugs) if isinstance(raw_bugs, (bool, int, float)) else 0)
    story = str(original.get("story") or "")
    actor_findings = []
    seen = set()
    for event in events or []:
        if event.get("kind") != "finding" or str(event.get("frm")) != str(actor_id):
            continue
        payload = event.get("payload") or {}
        finding_id = payload.get("finding_id")
        if (not finding_id or finding_id in seen
                or (payload.get("story") and str(payload.get("story")) != story)):
            continue
        seen.add(finding_id)
        actor_findings.append(finding_id)
    # Coordinator memory may already have reduced the immutable actor bug count to zero.  In that case the
    # exact actor finding events still have to be dispositioned before the receipt can be carried forward.
    if not actor_findings or (bug_count > 0 and len(actor_findings) != bug_count):
        return original

    admissible = set()
    for resolution in resolutions or []:
        if not isinstance(resolution, dict):
            continue
        if resolution.get("disposition") not in _RESULT_FINDING_COMPENSATION_DISPOSITIONS:
            continue
        if resolution.get("story") and str(resolution.get("story")) != story:
            continue
        if resolution.get("finding_id"):
            admissible.add(resolution["finding_id"])
    if not set(actor_findings).issubset(admissible):
        return original

    compensated = dict(original)
    compensated["bugs"] = [] if isinstance(raw_bugs, (list, tuple, dict, set)) else 0
    compensated["resolved_false_positive_findings"] = sorted(actor_findings)
    compensated["finding_compensation"] = "exact-durable-false-positive-adjudication"
    return compensated


def _reconcile_historical_findings(events, actors, resolutions, story_status, *, revision_matches):
    """Apply durable finding dispositions and strictly newer full-story proof.

    A QA run retains every historical ``finding`` event, including observations made before a fixer changed
    the product.  The release report used to retire only findings whose payload happened to say
    ``blocking=true``.  Medium/non-blocking observations therefore stayed open forever even after a later
    full-story run completed every ledger item without a bug.

    Supersession is deliberately narrow: the aggregate revision must still match the product, the later done
    event must belong to an ordinary (not focused-review) explorer, its coverage ledger must be non-empty and
    fully covered, it must report zero bugs/findings, and that same explorer must not have emitted a finding.
    Event ids provide the durable before/after fence.  The old observation remains in the dossier with the
    exact proof event that superseded it.
    """
    event_list = list(events or [])
    actor_by_id = {item.get("actor_id"): item for item in (actors or []) if isinstance(item, dict)}
    final_status = {str(key): value for key, value in (story_status or {}).items()}

    resolved_ids = {
        item.get("finding_id") for item in (resolutions or [])
        if isinstance(item, dict) and item.get("finding_id")
        and item.get("disposition") in _FINDING_PASS_DISPOSITIONS
    }
    resolved_keys = {
        (str(item.get("story")), item.get("title")) for item in (resolutions or [])
        if isinstance(item, dict) and item.get("disposition") in _FINDING_PASS_DISPOSITIONS
    }

    clean_proof_event = {}
    if revision_matches:
        finding_senders = {
            item.get("frm") for item in event_list if item.get("kind") == "finding"
        }
        for item in event_list:
            if item.get("kind") != "done":
                continue
            actor = actor_by_id.get(item.get("frm")) or {}
            if actor.get("role") != "qa-explorer":
                continue
            tool_args = ((((actor.get("memory") or {}).get("context") or {}).get("tool_args")) or {})
            if tool_args.get("_qa_review_id"):
                continue
            payload = item.get("payload") or {}
            result = _result_with_resolved_findings(
                payload.get("result") or {}, item.get("frm"), event_list, resolutions)
            story = str(payload.get("story") or result.get("story") or "").strip()
            if (not story or final_status.get(story) != "clean"
                    or int(payload.get("findings_count") or 0) != 0
                    and not result.get("finding_compensation")
                    or (item.get("frm") in finding_senders
                        and not result.get("finding_compensation"))
                    or not campaign_checkpoint.result_evidence_complete(result)):
                continue
            event_id = int(item.get("id") or 0)
            if event_id > int(clean_proof_event.get(story) or 0):
                clean_proof_event[story] = event_id

    findings = []
    for item in event_list:
        if item.get("kind") != "finding":
            continue
        finding = dict(item.get("payload") or {})
        if (finding.get("finding_id") in resolved_ids
                or (str(finding.get("story")), finding.get("title")) in resolved_keys):
            finding["resolved"] = True
            finding["resolution"] = "independent QA evidence adjudication resolved this observation"
        proof_event_id = int(clean_proof_event.get(str(finding.get("story"))) or 0)
        if proof_event_id > int(item.get("id") or 0):
            finding["fixed"] = True
            finding["resolution"] = (
                "superseded by a later complete, bug-free full-story proof on the final product revision")
            finding["resolution_proof_event_id"] = proof_event_id
        findings.append(finding)
    return findings, [item for item in findings if not (item.get("fixed") or item.get("resolved"))]


def _apply_audit_judgment(report, audit_verdict):
    """Apply one jury result without leaving contradictory human-facing status fields."""
    report["audit"] = audit_verdict
    if audit_verdict.get("passed_audit") is False or audit_verdict.get("close_call"):
        report["passed"] = report["clean"] = False
        tag = ("AUDIT CLOSE-CALL → ESCALATE" if audit_verdict.get("close_call")
               else "AUDIT REJECTED")
        message = (f"{tag} (score {audit_verdict.get('score', '?')}/10) — "
                   f"{(audit_verdict.get('summary') or '')[:160]}")
        report["verdict"] = report["summary"] = message
    return report


def _clean_audit_continuity(stories, story_status, per_story, *, prior_status=None, prior_results=None):
    """Carry only fully clean, inspectable story evidence into an audit-gap expansion.

    A jury rejection adds new coverage; it does not mutate the product and therefore must not force already
    proven stories through the browser again.  Status without a dossier result is insufficient (the next jury
    could not inspect it), and any non-clean state remains work.  Current results win over older generations.
    """
    planned = {campaign_checkpoint.story_key(story) for story in stories or []}
    statuses = dict(prior_status or {})
    statuses.update(dict(story_status or {}))
    results = dict(prior_results or {})
    results.update(dict(per_story or {}))
    clean_status = {str(key): "clean" for key, value in statuses.items()
                    if str(key) in planned and value == "clean" and str(key) in results
                    and campaign_checkpoint.result_evidence_complete(results[str(key)])}
    clean_results = {key: results[key] for key in clean_status}
    return clean_status, clean_results


def _durable_terminal_continuity(store, run_id, tenant, product_revision):
    """Recover exact-campaign, ordinary, grounded story proofs from a terminal orchestra run.

    A completed coordinator cannot be reopened safely: its events were reduced and its final result is
    immutable.  Starting from zero wastes valid browser work, however.  This reader treats the old run as a
    content-addressed evidence source for a new continuation run.  The coordinator's exact final coverage
    revision includes its audited selective-impact compensations, so a grounded proof from an earlier product
    revision remains reusable only when the final coordinator status still says that story is clean.  Focused
    workers, invalid recovery actors, unresolved finding senders, and covered-label-only results are excluded.
    """
    try:
        prior = store.run(int(run_id), tenant)
        actors = list(store.actors(int(run_id), tenant))
        events = list(store.events(int(run_id), tenant))
    except Exception:
        return None
    if not prior or prior.get("status") not in ("done", "failed", "halted"):
        return None
    coordinator = next((actor for actor in actors
                        if (actor.get("role") or "").lower() == "qa-coordinator"), None)
    memory = dict((coordinator or {}).get("memory") or {})
    context = dict(memory.get("context") or {})
    if not coordinator or str(memory.get("coverage_revision") or "") != str(product_revision or ""):
        return None
    resolutions = list(memory.get("finding_resolutions") or [])
    reduced_results = dict(memory.get("results") or {})
    final_story_status = {str(key): value for key, value in (memory.get("story_status") or {}).items()}
    invalid_recovery_actor_ids = {str(value) for value in (memory.get("invalid_recovery_actor_ids") or [])}
    finding_senders = {str(event.get("frm")) for event in events if event.get("kind") == "finding"}
    latest = {}
    for actor in sorted(actors, key=lambda item: int(item.get("actor_id") or 0)):
        if actor.get("role") != "qa-explorer":
            continue
        args = ((((actor.get("memory") or {}).get("context") or {}).get("tool_args") or {}))
        actor_id = actor.get("actor_id")
        stored = reduced_results.get(str(actor_id), reduced_results.get(actor_id)) or {}
        stored_result = stored.get("result") if isinstance(stored, dict) else None
        source_result = stored_result or ((actor.get("result") or {}).get("result") or {})
        if (args.get("_qa_review_id") or str(actor_id) in invalid_recovery_actor_ids
                or str(source_result.get("recovery_scope") or "story").lower() == "focused"):
            continue
        result = _result_with_resolved_findings(
            source_result, actor_id, events, resolutions)
        if (str(actor_id) in finding_senders
                and not result.get("finding_compensation")):
            continue
        story = str(result.get("story") or "").strip()
        if not story:
            continue
        latest[story] = result
    results = {story: result for story, result in latest.items()
               if final_story_status.get(story) == "clean"
               and campaign_checkpoint.result_evidence_complete(result)}
    return {
        "run_id": int(run_id), "context": context,
        "story_status": {story: "clean" for story in results},
        "story_results": results,
        "reusable_story_ids": sorted(results),
    }


_INTERNAL_MANAGEMENT_WAIT_STATES = {
    "manager_attention", "manager_review", "schedule_denied",
    "external_authority", "human_required", "human_wait",
}


def _internal_management_checkpoint(coordinator):
    """Return the unresolved QA-management work that should end this worker shift.

    ``internal_review_states`` is an audit history and deliberately retains terminal
    entries.  Only review ids still present in ``internal_reviews`` are unresolved;
    intersecting the two prevents a completed adjudication from parking QA forever.
    Active review/verification states are excluded because their actors and events
    still belong to this shift and must be allowed to finish normally.
    """
    memory = dict((coordinator or {}).get("memory") or {})
    unresolved = {
        str(item.get("review_id")): item for item in (memory.get("internal_reviews") or [])
        if isinstance(item, dict) and item.get("review_id")
    }
    states = dict(memory.get("internal_review_states") or {})
    waiting = {}
    for review_id in sorted(unresolved):
        state = states.get(review_id)
        if not isinstance(state, dict):
            continue
        status = str(state.get("status") or "").strip().lower()
        if status in _INTERNAL_MANAGEMENT_WAIT_STATES:
            waiting[review_id] = dict(state)
    if not waiting:
        return None
    return {
        "internal_review_states": waiting,
        "internal_review_ids": list(waiting),
        "qa_review_case_ids": sorted({str(s["case_id"]) for s in waiting.values() if s.get("case_id")}),
        "authority_decision_ids": sorted({int(s["authority_decision_id"]) for s in waiting.values()
                                          if s.get("authority_decision_id") is not None}),
    }


def run_agentic_qa(target_url, vision, *, product="app", token=None, org="0", summary="", repo=None,
                   stories=None, artifact_dir=None, restart_cmd=None, health_url=None,
                   tenant="agentic-qa", workers=None, drive_budget_s=None, stall_s=3.0, file_findings=True,
                   on_event=None, max_steps=None, thread_id=None, story_limit=None,
                   resume_from_run_id=None,
                   _audit_iteration=0, _audit_fingerprints=None,
                   _carried_story_status=None, _carried_story_results=None,
                   _carried_coverage_revision=None):
    """Create the QA org and drive it to completion. Returns the run id, terminal status, and the findings
    (bugs) the explorers reported over the bus. Drives run_org in a loop because tool jobs run ASYNC — a
    single run_org can return while a browser job is still going; we re-enter until the run is terminal or
    the wall-clock budget trips (a runaway guard, not a quality cap)."""
    import store
    import runtime as rt
    campaign_path = Path(repo) / "docs" / "QA-CHECKPOINT.json" if repo else None
    product_revision = campaign_checkpoint.repo_revision(repo)
    terminal_continuity = None
    if resume_from_run_id is not None:
        terminal_continuity = _durable_terminal_continuity(
            store, resume_from_run_id, tenant, product_revision)
        if not terminal_continuity:
            raise ValueError(
                f"run {resume_from_run_id} has no exact-revision terminal QA continuity for tenant {tenant}")
        prior_context = dict(terminal_continuity.get("context") or {})
        expected_repo = str(Path(repo).resolve()) if repo else None
        prior_repo = str(Path(prior_context.get("repo")).resolve()) if prior_context.get("repo") else None
        if (str(prior_context.get("product")) != str(product)
                or str(prior_context.get("target_url")) != str(target_url)
                or str(prior_context.get("vision")) != str(vision)
                or prior_repo != expected_repo
                or (str(prior_context.get("thread_id")) if prior_context.get("thread_id") is not None else None)
                != (str(thread_id) if thread_id is not None else None)):
            raise ValueError(f"run {resume_from_run_id} does not match this QA campaign context")
        if stories is None:
            stories = list(prior_context.get("stories") or [])
    if drive_budget_s is None:
        # The duty manager reviews substantive progress without killing healthy work. A composite staff triage
        # -> fixer -> adversarial browser verification routinely needs more than 20m because every real browser
        # action is independently judged. Leave five minutes for durable checkpoint/cleanup before the separate
        # controller runaway ceiling; concurrency, fencing, heartbeat and host pressure are the safety guards.
        drive_budget_s = int(os.environ.get("AOS_QA_DRIVE_BUDGET_S", "1500"))
    def emit(kind, data=None):
        if on_event:
            try:
                on_event(kind, data or {})
            except Exception:
                pass

    if stories is None:
        stories = _checkpoint_stories_for_resume(
            campaign_path, store, tenant=tenant, product=product, target_url=target_url,
            vision=vision, repo=repo, thread_id=thread_id)
        if stories is not None:
            emit("story_manifest_resumed", {"product": product, "stories": len(stories)})
        else:
            import story_gen
            emit("storygen_start", {"product": product, "saturated": True})
            try:
                stories = story_gen.saturate_stories(vision, summary, product=product, repo=repo)
            except Exception as e:
                emit("storygen_fallback", {"error": str(e)[:240]})
                stories = story_gen.generate_stories(vision, summary, repo=repo)
            emit("storygen_done", {"stories": len(stories or [])})
    stories = list(stories or [])
    # Fail closed before creating a durable org: duplicate/missing identities collapse coordinator
    # ``story_status`` keys and could otherwise make an untested tail look covered.
    campaign_checkpoint.canonical_manifest(stories)
    enumerated_count = len(stories)
    if story_limit is None:
        story_limit = int(os.environ.get("AOS_QA_MAX_STORIES", "12"))
    story_batch_size = max(0, int(story_limit or 0))
    initial_window = campaign_checkpoint.story_window(stories, story_batch_size)
    deferred_stories = list(initial_window["deferred_story_ids"])
    if deferred_stories:
        emit("safety_limit", {"kind": "stories", "planned": enumerated_count,
                              "running": len(initial_window["active_stories"]),
                              "deferred": len(deferred_stories),
                              "mode": "bounded_admission_batch"})
    workers = _planned_workers(len(initial_window["active_stories"]), workers)

    legacy_campaign_sig = hashlib.sha256(json.dumps([
        {"id": s.get("id"), "title": s.get("title"), "steps": s.get("steps"),
         "expected": s.get("expected") or s.get("expected_outcome")} for s in (stories or [])
    ], sort_keys=True, default=str).encode()).hexdigest()
    v2_campaign_sig = hashlib.sha256(json.dumps({
        "schema": 2, "product": product, "target_url": target_url, "vision": vision,
        "repo": str(Path(repo).resolve()) if repo else None,
        "stories": [{"id": s.get("id"), "title": s.get("title"), "steps": s.get("steps"),
                     "expected": s.get("expected") or s.get("expected_outcome")}
                    for s in (stories or [])],
    }, sort_keys=True, default=str).encode()).hexdigest()
    campaign_sig = campaign_checkpoint.campaign_signature(
        tenant=tenant, product=product, target_url=target_url, vision=vision, repo=repo,
        stories=stories, thread_id=thread_id)
    rid, resumed = None, False
    if campaign_path and campaign_path.exists():
        try:
            cp = json.loads(campaign_path.read_text())
            signature_matches = campaign_checkpoint.checkpoint_matches(
                cp, signature=campaign_sig, tenant=tenant, product=product,
                target_url=target_url, thread_id=thread_id)
            # Backward compatibility for an already-running v1 campaign (including the live recovery run). New
            # checkpoints bind URL/vision/repo too, so future same-story campaigns cannot attach to the wrong app.
            if (not signature_matches and cp.get("schema") == "aos.qa.checkpoint/2"
                    and cp.get("signature") == v2_campaign_sig and cp.get("product") == product
                    and cp.get("target_url") == target_url):
                signature_matches = True
            elif (not signature_matches and cp.get("schema") == "aos.qa.checkpoint/1"
                    and cp.get("signature") == legacy_campaign_sig and cp.get("product") == product):
                signature_matches = True
            prior = store.run(int(cp.get("run_id")), tenant) if signature_matches else None
            # A controller/WSL process can die after reopening the campaign but before the next safety timeout
            # writes another halted checkpoint.  The durable org is then still `running`; starting a brand-new
            # run repeats completed stories and abandons its workforce.  A matching running campaign is itself
            # the checkpoint—re-enter it and let reconcile_parked recover only unfinished tools.
            if prior and prior.get("status") == "running":
                rid, resumed = prior["run_id"], True
            elif (prior and prior.get("status") == "halted"
                  and _resumable_halted_result(prior.get("result"))):
                reopened = store.resume_run(prior["run_id"], tenant)
                if not reopened.get("error"):
                    rid, resumed = prior["run_id"], True
        except Exception:
            pass
    if rid is None:
        run = store.start_run(tenant, vision)
        rid = run["run_id"]
    pulse_work_id = f"qa-agentic:{product}:{rid}"
    try:
        import pulse
        pulse.start(pulse_work_id, "qa-run", label=f"Agentic QA: {product}", tenant_id=tenant,
                    stage="starting", progress=f"{len(stories or [])} stories planned",
                    expected_cadence_s=int(os.environ.get("AOS_QA_PULSE_CADENCE_S", "90")),
                    meta={"run_id": rid, "product": product, "target_url": target_url,
                          "workers": workers, "stories": len(stories or [])})
    except Exception:
        pulse = None
    emit("stories", {"stories": len(stories or []), "workers": workers})
    carry_valid = bool(
        repo and product_revision and _carried_coverage_revision
        and str(product_revision) == str(_carried_coverage_revision))
    carried_status, carried_results = _clean_audit_continuity(
        stories, _carried_story_status if carry_valid else {},
        _carried_story_results if carry_valid else {},
        prior_status=(terminal_continuity or {}).get("story_status"),
        prior_results=(terminal_continuity or {}).get("story_results"))
    context = {"vision": vision, "target_url": target_url, "token": token, "org": str(org),
               "product": product, "stories": stories, "repo": repo, "artifact_dir": artifact_dir,
               "restart_cmd": restart_cmd, "health_url": health_url, "max_steps": max_steps,
               "thread_id": thread_id, "story_batch_size": story_batch_size,
               "product_revision": product_revision}
    if not resumed:
        coord = store.spawn_actor(rid, tenant, "qa-coordinator", "qa-coordinator", kind="supervisor",
                                  memory={"context": context, "repo": repo or ".",
                                          "coverage_revision": product_revision,
                                          "revision_generation": 0,
                                          "story_status": carried_status})
        store.emit(rid, tenant, None, coord["actor_id"], "task",
                   {"task": f"QA the product '{product}' against its vision; report every bug."})
    else:
        emit("campaign_resumed", {"run_id": rid, "stories": len(stories or [])})
        safety_reopened = _reopen_automatic_safety_stop_actors(store, rid, tenant)
        if safety_reopened:
            emit("campaign_safety_stop_reopened", {"run_id": rid, "actors": safety_reopened})
        invalidated = _invalidate_resumed_revision(
            store, rid, tenant, product_revision, repo=repo, runtime_mod=rt)
        if invalidated:
            emit("campaign_revision_invalidated", {"run_id": rid, "stories": invalidated,
                                                     "product_revision": product_revision})
        refreshed = _refresh_resumed_coordinator_context(store, rid, tenant, context)
        if refreshed:
            emit("campaign_context_refreshed", {"run_id": rid, "coordinators": refreshed,
                                                 "max_steps": max_steps})
        repaired = _reopen_legacy_cancelled_explorers(store, rid, tenant)
        if repaired:
            emit("campaign_checkpoint_repaired", {"run_id": rid, "explorers": repaired})
    # Publish the locator before the first runtime step. A WSL/controller crash during the first batch must
    # reattach to this durable run instead of starting another run over the same prefix.
    try:
        _write_campaign_checkpoint(
            campaign_path, store=store, run_id=rid, tenant=tenant, signature=campaign_sig,
            product=product, target_url=target_url, thread_id=thread_id, stories=stories,
            batch_size=story_batch_size, status="running")
    except Exception as exc:
        emit("campaign_checkpoint_error", {"run_id": rid, "error": str(exc)[:240]})

    # DRIVE to completion. run_org self-stalls after ~stall_s of no claimable events (e.g. while an async
    # tool job runs); loop and re-enter until the run is terminal or the budget trips.
    started = time.time()
    deadline = started + drive_budget_s
    early_checkpoint = False
    management_checkpoint = None
    runtime_threads_alive = 0
    last_progress_payload = None
    last_stall_payload = None
    try:
        import jobrunner
        jobrunner.set_run_deadline(rid, tenant, deadline)
        def live_deadline():
            getter = getattr(jobrunner, "run_deadline", None)
            return getter(rid, tenant, deadline) if callable(getter) else deadline
    except Exception:
        jobrunner = None
        def live_deadline():
            return deadline
    while time.time() < live_deadline():
        runtime_result = rt.run_org(rid, tenant, repo=repo or ".", workers=workers, stall_s=stall_s,
                                    deadline=deadline)
        runtime_threads_alive = max(runtime_threads_alive,
                                    int((runtime_result or {}).get("threads_alive") or 0))
        if (runtime_result or {}).get("threads_alive"):
            # Never stack a second orchestra pool over a still-unwinding first pool. The durable run is
            # checkpointed below and its status fence rejects any survivor's post-halt commit.
            early_checkpoint = True
            emit("agentic_runtime_cleanup_handoff", {
                "run_id": rid, "threads_alive": int(runtime_result["threads_alive"]),
                "deadline_exceeded": bool(runtime_result.get("deadline_exceeded"))})
        r = store.run(rid, tenant)
        acts = store.actors(rid, tenant)
        counts = {}
        for a in acts:
            counts[a.get("status") or "unknown"] = counts.get(a.get("status") or "unknown", 0) + 1
        explorers_done = sum(1 for a in acts if a.get("role") == "qa-explorer" and a.get("status") == "done")
        explorers_total = sum(1 for a in acts if a.get("role") == "qa-explorer")
        coordinator = next((a for a in acts if a.get("role") == "qa-coordinator"), None)
        story_status = dict(((coordinator or {}).get("memory") or {}).get("story_status") or {})
        primary_done = len(_settled_story_ids(story_status).intersection(
            {str(s.get("id") or s.get("title")) for s in (stories or []) if s.get("id") or s.get("title")}))
        progress = (f"{primary_done}/{len(stories or [])} assigned stories have a latest verdict; "
                    f"{explorers_done}/{explorers_total} exploration passes done")
        try:
            if pulse:
                pulse.beat(pulse_work_id, stage=(r or {}).get("status") or "running", progress=progress,
                           meta={"actors": counts, "explorers_done": explorers_done,
                                 "explorers_total": explorers_total, "stories_done": primary_done,
                                 "stories_total": len(stories or [])})
        except Exception:
            pass
        progress_payload = {"run_id": rid, "status": (r or {}).get("status"),
                            "actors": counts, "explorers_done": explorers_done,
                            "explorers_total": explorers_total, "stories_done": primary_done,
                            "stories_total": len(stories or [])}
        if progress_payload != last_progress_payload:
            emit("agentic_progress", progress_payload)
            last_progress_payload = progress_payload
        try:
            _write_campaign_checkpoint(
                campaign_path, store=store, run_id=rid, tenant=tenant, signature=campaign_sig,
                product=product, target_url=target_url, thread_id=thread_id, stories=stories,
                batch_size=story_batch_size, status=(r or {}).get("status") or "running")
        except Exception as exc:
            emit("campaign_checkpoint_error", {"run_id": rid, "error": str(exc)[:240]})
        if (r or {}).get("status") in ("done", "failed", "halted"):
            break
        if runtime_threads_alive:
            break
        try:
            import jobrunner
            # run_org may just have consumed checkpoint/cancellation result events, changing actors from
            # "blocked with a pending result" to "blocked and ready to redispatch".  Its startup reconcile
            # necessarily ran BEFORE that transition.  Reconcile once more before deciding the org is settled;
            # otherwise the campaign exits in the tiny hand-off window with no active job and no queued event.
            jobrunner.reconcile_parked(store, rid, tenant)
            active = bool(jobrunner.active_for_run(rid, tenant))
        except Exception:
            active = False
        current_actors = store.actors(rid, tenant)
        pend = sum(store.pending_count(a["actor_id"], tenant) for a in current_actors)
        live_actors = [a for a in current_actors if a.get("status") not in ("done", "dead")]
        if not active and not pend and live_actors:
            # A nonterminal durable workforce with no local thread/event is a stall, not completion. Keep driving
            # and reconciling until it recovers or the bounded shift checkpoints it honestly at the deadline.
            stall_payload = {"run_id": rid, "live_actors": len(live_actors)}
            if stall_payload != last_stall_payload:
                emit("agentic_stall", stall_payload)
                last_stall_payload = stall_payload
            current_coordinator = next(
                (a for a in current_actors if a.get("role") == "qa-coordinator"), None)
            management_checkpoint = _internal_management_checkpoint(current_coordinator)
            if management_checkpoint:
                # No QA actor can make progress until the durable management case changes. Return control now;
                # idling to the generic runway threshold wastes a worker and can provoke a blind replacement.
                early_checkpoint = True
                emit("agentic_management_handoff", {"run_id": rid, **management_checkpoint})
                break
            minimum_runway = max(0.0, float(os.environ.get("AOS_TOOL_MIN_LAUNCH_RUNWAY_S", "600")))
            if live_deadline() - time.time() < minimum_runway:
                # A parked worker was deliberately not launched near the end of
                # this shift. Checkpoint now instead of idling until the hard
                # deadline; the controller can immediately grant a fresh shift.
                early_checkpoint = True
                emit("agentic_shift_handoff", {"run_id": rid,
                                                "remaining_s": max(0, int(live_deadline()-time.time())),
                                                "minimum_launch_runway_s": int(minimum_runway)})
                break
        elif not active and not pend:
            break
        time.sleep(0.2)

    timed_out = ((store.run(rid, tenant) or {}).get("status") not in ("done", "failed", "halted")
                 and (time.time() >= live_deadline() or early_checkpoint))
    cleanup = _cleanup_facts(0, 0, runtime_threads_alive)
    if timed_out:
        if jobrunner is not None:
            cleanup_threads_incomplete = jobrunner.cancel_run(
                rid, tenant, grace_s=float(os.environ.get("AOS_QA_CANCEL_GRACE_S", "20")))
        else:
            cleanup_threads_incomplete = 0
        cleanup = _cleanup_facts(cleanup_threads_incomplete, _terminate_owned_children(),
                                 runtime_threads_alive)
        terminal = {"safety_limited": True, "timed_out": True,
                    "internal_management_wait": bool(management_checkpoint),
                    **(management_checkpoint or {}),
                    **cleanup,
                    "reason": ("QA checkpointed while durable internal management resolves disputed evidence"
                               if management_checkpoint else
                               "QA checkpointed while orchestra runtime threads unwind"
                               if runtime_threads_alive else
                               "QA shift handed off before starting work without enough runway"
                               if early_checkpoint else
                               "QA progress lease expired after no durable subordinate advancement")}
        # Halt the run but preserve its blocked actor/job specs. A later controller retry re-opens THIS run
        # and reconcile_parked dispatches only unfinished tools; completed stories are never repeated.
        store.finish_run(rid, "halted", terminal, tenant_id=tenant)
        if campaign_path:
            try:
                _write_campaign_checkpoint(
                    campaign_path, store=store, run_id=rid, tenant=tenant, signature=campaign_sig,
                    product=product, target_url=target_url, thread_id=thread_id, stories=stories,
                    batch_size=story_batch_size, status="halted")
            except Exception as exc:
                emit("campaign_checkpoint_error", {"run_id": rid, "error": str(exc)[:240]})
    elif jobrunner is not None:
        jobrunner.clear_run_deadline(rid, tenant)

    evs = store.events(rid, tenant)
    acts = store.actors(rid, tenant)
    status = (store.run(rid, tenant) or {}).get("status")
    explorers_total = sum(1 for a in acts if a.get("role") == "qa-explorer")
    explorers_done = sum(1 for a in acts
                         if a.get("role") == "qa-explorer" and a.get("status") == "done")

    # PHASE 6 — durable, gate-consumable verdict: map the qa-coordinator's honest verdict onto the procedural
    # pipeline's report shape and reuse its persistence, so an AGENTIC run leaves the SAME artifacts a build's
    # LAUNCH gate binds to: a qa_runs row (durable history) + docs/QA-VERDICT.json (in the product repo).
    # Fail-open — persistence must never crash the run.
    coord = next((a for a in acts if a.get("role") == "qa-coordinator"), None)
    v = (coord or {}).get("result") or {}
    blocking = v.get("blocking_stories") or []
    coord_mem = (coord or {}).get("memory") or {}
    performance_events = list(coord_mem.get("performance_events") or v.get("performance_events") or [])
    for observation in performance_events[-100:]:
        emit("agentic_subordinate_slow", observation)
    latest_story_status = dict(coord_mem.get("story_status") or {})
    resolutions = list(coord_mem.get("finding_resolutions") or v.get("finding_resolutions") or [])
    coverage_revision = coord_mem.get("coverage_revision") or (
        (coord_mem.get("context") or {}).get("product_revision"))
    # Build the latest-result index before adjudicating history or computing release status.  Coordinator
    # labels are scheduling state; the retained receipts plus exact later adjudications are the release
    # authority.  False-positive resolutions compensate only their originating explorer receipt.
    per_story = dict(carried_results)
    reduced_results = dict(coord_mem.get("results") or {})
    invalid_recovery_actor_ids = {
        str(value) for value in (coord_mem.get("invalid_recovery_actor_ids") or [])}
    for act in sorted(acts, key=lambda item: int(item.get("actor_id") or 0)):
        if act.get("role") != "qa-explorer":
            continue
        actor_id = act.get("actor_id")
        stored = reduced_results.get(str(actor_id), reduced_results.get(actor_id)) or {}
        stored_result = stored.get("result") if isinstance(stored, dict) else None
        res = stored_result or ((act.get("result") or {}).get("result")) or {}
        tool_args = ((((act.get("memory") or {}).get("context") or {}).get("tool_args")) or {})
        if (tool_args.get("_qa_review_id")
                or str(actor_id) in invalid_recovery_actor_ids
                or str(res.get("recovery_scope") or "story").lower() == "focused"
                ):
            continue
        if res.get("story") is not None:
            per_story[str(res["story"])] = _result_with_resolved_findings(
                res, actor_id, evs, resolutions)
    planned_story_ids = {str(s.get("id") or s.get("title")) for s in (stories or [])
                         if s.get("id") or s.get("title")}
    evidence_diagnostics = {
        sid: campaign_checkpoint.evidence_diagnostics(per_story.get(sid) or {})
        for sid in sorted(planned_story_ids)
    }
    evidence_incomplete_stories = sorted(
        sid for sid, detail in evidence_diagnostics.items() if not detail.get("complete"))
    effective_story_status = dict(latest_story_status)
    for sid in evidence_incomplete_stories:
        # An unresolved product defect remains blocking even when its proof bundle is also incomplete.  Only
        # optimistic clean labels are downgraded to an automatic continuation.
        if effective_story_status.get(sid) == "clean":
            effective_story_status[sid] = "incomplete"
    final_product_revision = campaign_checkpoint.repo_revision(repo)
    revision_unavailable = bool(repo and not final_product_revision)
    revision_mismatch = bool(repo and final_product_revision
                             and coverage_revision != final_product_revision)
    revision_matches = bool(repo and final_product_revision and not revision_mismatch)
    findings, open_findings = _reconcile_historical_findings(
        evs, acts, resolutions, effective_story_status, revision_matches=revision_matches)

    settled_story_ids = _settled_story_ids(effective_story_status)
    deferred_stories = sorted(planned_story_ids - settled_story_ids)
    story_set_complete = planned_story_ids.issubset(settled_story_ids)
    safety_limited = bool(deferred_stories or timed_out or revision_unavailable or revision_mismatch)
    clean = bool(v.get("passed")) and story_set_complete and not open_findings and not safety_limited
    verdict_text = _release_cleanliness_verdict(
        v.get("result") or f"agentic QA: {status}", open_findings, clean)
    if management_checkpoint:
        verdict_text = ("INTERNAL MANAGEMENT — QA is checkpointed while disputed evidence or a named "
                        "authority request is resolved.")
    elif evidence_incomplete_stories:
        verdict_text = (f"EVIDENCE INCOMPLETE — {len(evidence_incomplete_stories)} story result(s) claim a "
                        "verdict without a complete durable proof chain; those stories remain owned for "
                        "automatic continuation.")
    elif deferred_stories:
        verdict_text = (f"BOUNDED QA CHECKPOINT — {enumerated_count-len(deferred_stories)} of "
                        f"{enumerated_count} stories have a current-revision verdict; "
                        f"{len(deferred_stories)} remain for automatic bounded admission.")
    elif revision_unavailable:
        verdict_text = "REVISION PROOF UNAVAILABLE — product coverage cannot be tied to exact source bytes."
    elif revision_mismatch:
        verdict_text = ("PRODUCT REVISION CHANGED — previously collected coverage was invalidated; "
                        "a fresh full-manifest regression sweep is required.")
    elif timed_out:
        verdict_text = ("PROGRESS LEASE EXPIRED — QA checkpointed unfinished work after no new durable "
                        "subordinate advancement; it did not convert incomplete coverage into a pass.")

    # EVIDENCE (parity with the procedural loop): a Windows-visible evidence dir with COVERAGE.md
    # (tested-vs-untested per story, from the explorers' coverage ledgers — latest per story) + run-final.json.
    import artifacts
    evidence_dir = artifacts.run_dir(product, started)
    cov_json, lines = [], [f"# QA Coverage (agentic) — {product}", "", f"Verdict: {verdict_text}", ""]
    artifact_remap = _materialize_story_artifacts(per_story, evidence_dir)
    for sid, res in per_story.items():
        cov = res.get("coverage") or []
        tested = [c["aspect"] for c in cov if c.get("covered")]
        untested = [c["aspect"] for c in cov if not c.get("covered")]
        cov_json.append({"story": sid, "stop_reason": res.get("stop_reason"),
                         "tested": tested, "yet_to_test": untested})
        lines += [f"## {sid}", f"- stop reason: **{res.get('stop_reason', '?')}**",
                  f"- tested ({len(tested)}): " + ("; ".join(tested) or "(none recorded)"),
                  f"- yet to test ({len(untested)}): " + ("; ".join(untested) or "(none)"), ""]
    # run-final.json in the review.py dossier shape (stories[] with per-step records) so the auditor can
    # scrutinise the AGENTIC run's actual work, not just its verdict.
    stories_report = []
    for sid, res in per_story.items():
        steps = []
        for raw_step in (res.get("steps_detail") or []):
            step = dict(raw_step)
            if step.get("screenshot") in artifact_remap:
                step["screenshot"] = artifact_remap[step["screenshot"]]
            steps.append(step)
        raw_video = res.get("video")
        story_video = artifact_remap.get(str(raw_video)) if raw_video else None
        if not story_video:
            story_video = next((destination for source, destination in artifact_remap.items()
                                if Path(str(source)).suffix.lower() in {".mp4", ".webm"}
                                and str(sid) in Path(destination).name), None)
        story_state = effective_story_status.get(str(sid))
        stories_report.append({"id": sid, "title": res.get("title") or sid,
                               "status": ("blocked" if story_state == "blocking" else
                                          "passed" if story_state == "clean" else "incomplete"),
                               "expected": "", "steps": steps, "coverage": res.get("coverage"),
                               "stop_reason": res.get("stop_reason"), "video": story_video,
                               "artifact_evidence": res.get("artifact_evidence") or [],
                               "timings": res.get("timings") or [],
                               "slow_phases": res.get("slow_phases") or []})
    try:
        (evidence_dir / "run-input.json").write_text(json.dumps(
            {"product": product, "vision": vision, "url": target_url, "stories": stories},
            indent=2, default=str))
        (evidence_dir / "COVERAGE.md").write_text("\n".join(lines))
        (evidence_dir / "coverage.json").write_text(json.dumps(cov_json, indent=2, default=str))
        (evidence_dir / "run-final.json").write_text(json.dumps(
            {"product": product, "vision": vision, "url": target_url, "verdict": verdict_text,
             "passed": clean, "stories": stories_report, "bugs": findings}, indent=2, default=str))
    except Exception:
        pass
    report = {"verdict": verdict_text, "summary": verdict_text,
              "passed": clean, "total_stories": v.get("stories") or len(stories or []),
              "total_bugs": len(findings), "open_bugs": len(open_findings),
              "blocking_open": sum(bool(item.get("blocking")) for item in open_findings),
              "clean": clean, "rounds": 1, "safety_limited": safety_limited,
              "deferred_stories": len(deferred_stories), "enumerated_stories": enumerated_count,
              "timed_out": timed_out, "evidence_dir": str(evidence_dir),
              "product_revision": final_product_revision,
              "coverage_revision": coverage_revision,
              "revision_generation": int(coord_mem.get("revision_generation") or 0),
              # Outer controller shift accounting must compare progress only inside one evidence/revision
              # campaign. A new orchestra run or a fixer invalidating prior coverage legitimately restarts
              # completed-story counts at zero and is not a stall.
              "qa_campaign_run_id": rid,
              "qa_campaign_key": (
                  f"orchestra:{rid}:policy:{campaign_checkpoint.EVIDENCE_POLICY_REVISION}:"
                  f"revision:{coverage_revision or final_product_revision or 'unavailable'}:"
                  f"generation:{int(coord_mem.get('revision_generation') or 0)}"
              ),
              "evidence_policy_revision": campaign_checkpoint.EVIDENCE_POLICY_REVISION,
              "continued_from_run_id": (terminal_continuity or {}).get("run_id"),
              "reused_story_ids": (terminal_continuity or {}).get("reusable_story_ids") or [],
              "revision_mismatch": revision_mismatch,
              "revision_unavailable": revision_unavailable,
              "internal_management_wait": bool(management_checkpoint),
              "evidence_incomplete_stories": evidence_incomplete_stories,
              "evidence_diagnostics": evidence_diagnostics,
              "story_progress": dict(coord_mem.get("story_progress") or v.get("story_progress") or {}),
              "performance_events": performance_events,
              **(management_checkpoint or {}),
              **cleanup,
              # Durable campaign progress consumed by the outer controller.  A worker time slice may end, but
              # management can now distinguish a productive shift hand-off from a repeatedly stalled one.
              "explorers_done": explorers_done, "explorers_total": explorers_total,
              "stories_done": len(planned_story_ids.intersection(settled_story_ids)),
              "stories_total": len(planned_story_ids), "story_set_complete": story_set_complete,
              "coverage_doc": str(evidence_dir / "COVERAGE.md"),
              "md": str(evidence_dir / "COVERAGE.md"), "json": str(evidence_dir / "run-final.json")}

    # AUDITOR SIGN-OFF (same skeptical gate as the procedural loop): review the agentic run's ACTUAL work
    # (per-step records + coverage in the evidence dir) — a rejected run can't be reported 'passed'. Runs
    # before the persist below so the downgrade reaches QA-VERDICT.json. Env-gated + fail-open.
    report["audit"] = None
    if (_should_run_audit(file_findings, timed_out, status, candidate_clean=clean)
            and os.environ.get("AOS_QA_AUDIT_GATE", "1").lower() not in ("0", "false", "no")):
        try:
            import review
            av = review.review(str(evidence_dir), write=True)
            _apply_audit_judgment(report, av)
        except Exception as e:
            report["audit_error"] = str(e)

    # SHIP BAR: only a genuinely terminal campaign fans unresolved semantic clusters into the governed
    # backlog. A checkpoint already has a durable repair owner and must never manufacture parallel work.
    if _should_file_governed_findings(file_findings, timed_out, status) and open_findings:
        try:
            import json as _j
            import findings as _findings
            blocking_set = set(map(str, blocking))
            for f in rt._qa_compact_pending_findings(open_findings):
                sev = "high" if f.get("blocking") or str(f.get("story")) in blocking_set else (f.get("severity") or "medium")
                pri = 2 if sev in ("high", "critical") else 3
                _findings.file(f"qa-agentic:{product}", "builder",
                               f"[{product}] {f.get('title') or f.get('bug') or 'QA defect'}",
                               _j.dumps(f, default=str), severity=sev, priority=pri, tenant_id=tenant,
                               dedupe_key=_governed_finding_key(f))
        except Exception:
            pass
    try:
        import qa_run
        report["qa_run_id"] = qa_run._persist_run(report, {
            "product": product, "url": target_url, "vision": vision, "stories": [], "bugs": [],
            "started_at": started, "finished_at": time.time(), "rounds": 1, "clean": report["passed"],
            "tenant_id": tenant})
        if repo:                                    # only write the gate artifact into a real product repo
            report["verdict_json"] = qa_run.write_verdict(repo, report, product=product,
                target_url=target_url, producer="qa_agentic", qa_run_id=report.get("qa_run_id"))
    except Exception as e:
        report["persist_error"] = str(e)

    # A skeptical rejection is a state change for QA itself.  Convert it into new owned stories and run a
    # fresh full-manifest regression campaign; never bounce a test-evidence gap to the builder or the CEO.
    # Semantic fingerprints and a generous shift bound prevent an adversarial jury from manufacturing an
    # infinite novelty loop.  Hitting either boundary is an internal-management checkpoint, not acceptance.
    audit_verdict = report.get("audit") or {}
    audit_rejected = bool(audit_verdict.get("passed_audit") is False
                          or audit_verdict.get("close_call"))
    if _should_expand_audit(audit_rejected, clean, status, timed_out, open_findings):
        fingerprint = _audit_gap_fingerprint(audit_verdict)
        seen = set(_audit_fingerprints or [])
        try:
            max_expansions = max(0, int(os.environ.get("AOS_QA_AUDIT_MAX_EXPANSIONS", "4")))
        except ValueError:
            max_expansions = 4
        if fingerprint and fingerprint not in seen and _audit_iteration < max_expansions:
            supplements = _audit_gap_stories(
                audit_verdict, vision, stories, repo=repo, iteration=_audit_iteration + 1)
            existing_ids = {str(item.get("id")) for item in stories}
            supplements = [item for item in supplements if str(item.get("id")) not in existing_ids]
            if supplements:
                expanded_stories = list(stories) + supplements
                continuity_status, continuity_results = _clean_audit_continuity(
                    expanded_stories, effective_story_status, per_story,
                    prior_status=carried_status, prior_results=carried_results)
                try:
                    import story_gen
                    story_gen.save_stories(product, supplements, source="regression")
                except Exception:
                    pass
                emit("audit_gap_handoff", {"run_id": rid, "iteration": _audit_iteration + 1,
                                            "stories": len(supplements), "fingerprint": fingerprint})
                try:
                    if pulse:
                        pulse.finish(pulse_work_id, status="incomplete",
                                     result={"verdict": report.get("verdict"),
                                             "audit_gap_handoff": True,
                                             "qa_run_id": report.get("qa_run_id")})
                except Exception:
                    pass
                followup = run_agentic_qa(
                    target_url, vision, product=product, token=token, org=org, summary=summary, repo=repo,
                    stories=expanded_stories, artifact_dir=artifact_dir,
                    restart_cmd=restart_cmd, health_url=health_url, tenant=tenant, workers=workers,
                    drive_budget_s=drive_budget_s, stall_s=stall_s, file_findings=file_findings,
                    on_event=on_event, max_steps=max_steps, thread_id=thread_id, story_limit=story_limit,
                    _audit_iteration=_audit_iteration + 1,
                    _audit_fingerprints=sorted(seen | {fingerprint}),
                    _carried_story_status=continuity_status,
                    _carried_story_results=continuity_results,
                    _carried_coverage_revision=final_product_revision)
                history = list(followup.get("audit_history") or [])
                history.insert(0, {"run_id": rid, "qa_run_id": report.get("qa_run_id"),
                                   "iteration": _audit_iteration, "audit": audit_verdict,
                                   "supplemental_story_ids": [item["id"] for item in supplements]})
                followup["audit_history"] = history
                followup["prior_run_ids"] = [rid] + list(followup.get("prior_run_ids") or [])
                return followup
        report["safety_limited"] = True
        report["audit_internal_management"] = True
        report["verdict"] = ("INTERNAL QA MANAGEMENT — the skeptical jury rejection did not produce new "
                             "executable coverage after bounded semantic expansion; the durable audit gaps "
                             "remain owned and the release stays blocked.")

    if campaign_path and status in ("done", "failed") and not timed_out:
        try:
            campaign_path.unlink(missing_ok=True)
        except Exception:
            pass

    try:
        if pulse:
            pulse.finish(pulse_work_id, status=("done" if report.get("passed") else "incomplete"),
                         result={"verdict": report.get("verdict"), "stories": report.get("total_stories"),
                                 "blocking_open": report.get("blocking_open"),
                                 "qa_run_id": report.get("qa_run_id")})
    except Exception:
        pass

    return {"run_id": rid, "status": status, "findings": findings, "actors": len(acts),
            "explorers": [a for a in acts if a.get("role") == "qa-explorer"],
            "verdict": v, "report": report}


def _planned_workers(n_stories, requested=None):
    if requested is not None:
        return max(1, int(requested))
    try:
        pinned = int(os.environ.get("AOS_QA_AGENTIC_WORKERS", "0"))
        if pinned > 0:
            return pinned
    except ValueError:
        pass
    try:
        cap = max(1, int(os.environ.get("AOS_QA_AGENTIC_WORKER_CAP", os.environ.get("AOS_FLEET_WORKERS", "8"))))
    except ValueError:
        cap = 8
    if n_stories <= 0:
        return 1
    return max(1, min(cap, n_stories, max(2, (n_stories + 9) // 10)))


def _selftest():
    """FULL async agentic drive, offline: stub BOTH seams — the tool (no browser) AND factory.agent (no real
    CLI, so the coordinator's decide/aggregate steps are instant) — then drive the real org to completion and
    assert it hired one qa-explorer per story, the tool-workers dispatched (dispatch-and-park), their findings
    flowed back over the bus (via `tool_result` -> the worker -> `finding`/`done` up), and the run finished."""
    import types
    import json as _json
    import store

    _real_factory = sys.modules.get("factory")
    fake = types.ModuleType("factory")

    def fake_agent(role, repo, task, **k):        # instant, deterministic coordinator AI (ack / aggregate)
        body = _json.dumps({"action": "ack", "result": "qa complete", "ok": True})
        return {"rc": 0, "out": body, "out_full": body}
    fake.agent = fake_agent
    fake.PRODUCTS = "/tmp"
    sys.modules["factory"] = fake

    import tools
    import jobrunner
    _orig_tool = tools.run_tool

    _seen = {}

    def fake_tool(name, args):
        if name == "dev_fix":                     # the dev-fixer tool-worker (spawned via the dev-handoff)
            return {"status": "done", "findings": [], "result": {"fixed": True, "files": ["src/x.js"]}}
        sid = (args.get("story") or {}).get("id")  # qa_explore: first test finds a bug; RE-TEST after fix is clean
        _seen[sid] = _seen.get(sid, 0) + 1
        findings = ([{"kind": "bug", "title": f"bug {sid}", "blocking": True, "story": sid}]
                    if _seen[sid] == 1 else [])
        return {"status": "done", "findings": findings,
                "result": {"story": sid, "stop_reason": "coverage-complete", "bugs": len(findings),
                           "coverage": [{"aspect": f"complete story {sid}", "covered": True}],
                           "steps_detail": [{"verdict": "match",
                                             "covers": [f"complete story {sid}"]}]}}
    tools.run_tool = fake_tool
    jobrunner._default_run_tool = lambda: fake_tool

    import tempfile
    tmprepo = tempfile.mkdtemp(prefix="agentic-qa-")   # a throwaway repo so write_verdict never clobbers ours
    _prev_ev = os.environ.get("AOS_QA_EVIDENCE_DIR")
    _prev_runway = os.environ.get("AOS_TOOL_MIN_LAUNCH_RUNWAY_S")
    os.environ["AOS_QA_EVIDENCE_DIR"] = tempfile.mkdtemp(prefix="agentic-ev-")   # evidence in a throwaway dir
    os.environ["AOS_QA_AUDIT_GATE"] = "0"          # offline test: skip the real auditor call (own test covers it)
    os.environ["AOS_TOOL_MIN_LAUNCH_RUNWAY_S"] = "1"  # 180s offline envelope; production default is 600s
    out = None
    try:
        out = run_agentic_qa("http://app.test", "A console the user signs in to and messages an assistant.",
                             product="agentic-selftest", stories=[{"id": "US1"}, {"id": "US2"}],
                             tenant="agentic-selftest", repo=tmprepo, workers=2, drive_budget_s=180, stall_s=1.0)
        roles = [a.get("role") for a in store.actors(out["run_id"], "agentic-selftest")]
        assert out["status"] == "done", f"the org run must finish; got {out['status']}"
        # the CLOSED LOOP: 2 initial explorers find bugs -> 2 dev-coordinators -> 2 dev-fixers fix them ->
        # qa-coordinator RE-TESTS each story (2 more explorers) -> re-test is CLEAN -> verdict PASSED.
        assert roles.count("dev-coordinator") == 2, f"a dev-coordinator per blocking bug; got {roles.count('dev-coordinator')}"
        assert roles.count("dev-fixer") == 2, f"a dev-fixer per dev-coordinator; got {roles.count('dev-fixer')}"
        assert roles.count("qa-explorer") == 4, f"2 initial + 2 re-test explorers; got {roles.count('qa-explorer')}"
        coord = next(a for a in store.actors(out["run_id"], "agentic-selftest") if a["role"] == "qa-coordinator")
        v = (coord.get("result") or {})
        assert v.get("passed") is True, f"re-test was clean -> verdict must PASS; got {v}"
        assert not v.get("blocking_stories"), v
        # phase 6: the run left a durable qa_runs row + a gate-consumable QA-VERDICT.json in the (temp) repo.
        rep = out.get("report") or {}
        assert rep.get("qa_run_id"), f"agentic run must persist a qa_runs row; got {rep}"
        assert (Path(tmprepo) / "docs" / "QA-VERDICT.json").exists(), "QA-VERDICT.json (the LAUNCH artifact) must be written"
        assert rep.get("coverage_doc") and Path(rep["coverage_doc"]).exists(), "COVERAGE.md evidence must be written"
        print(f"qa_agentic selftest: PASS (CLOSED LOOP find->fix->re-test->passed; "
              f"{roles.count('qa-explorer')} explorers, {roles.count('dev-fixer')} fixers; "
              f"durable verdict persisted qa_run_id={rep.get('qa_run_id')} + QA-VERDICT.json written)")
        return 0
    finally:
        tools.run_tool = _orig_tool
        jobrunner._default_run_tool = lambda: __import__("tools").run_tool
        if _real_factory is not None:
            sys.modules["factory"] = _real_factory
        else:
            sys.modules.pop("factory", None)
        _cleanup(out["run_id"] if isinstance(out, dict) else -1, "agentic-selftest")
        import shutil
        shutil.rmtree(tmprepo, ignore_errors=True)
        shutil.rmtree(os.environ.get("AOS_QA_EVIDENCE_DIR", ""), ignore_errors=True)
        if _prev_ev is None:
            os.environ.pop("AOS_QA_EVIDENCE_DIR", None)
        else:
            os.environ["AOS_QA_EVIDENCE_DIR"] = _prev_ev
        if _prev_runway is None:
            os.environ.pop("AOS_TOOL_MIN_LAUNCH_RUNWAY_S", None)
        else:
            os.environ["AOS_TOOL_MIN_LAUNCH_RUNWAY_S"] = _prev_runway
        if isinstance(out, dict) and (out.get("report") or {}).get("qa_run_id"):
            try:                                        # drop the selftest's qa_runs row
                with connection() as c, c.cursor() as cur:
                    cur.execute("DELETE FROM qa_runs WHERE id=%s", (out["report"]["qa_run_id"],))
            except Exception:
                pass


def _cleanup(run_id, tenant):
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM orchestra_events WHERE run_id=%s", (run_id,))
            cur.execute("DELETE FROM orchestra_actors WHERE run_id=%s", (run_id,))
            cur.execute("DELETE FROM orchestra_runs WHERE run_id=%s", (run_id,))
            cur.execute("DELETE FROM agent_pulse WHERE work_id LIKE %s OR work_id LIKE %s",
                        (f"{run_id}:%", f"qa-agentic:%:{run_id}"))
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(_selftest())
