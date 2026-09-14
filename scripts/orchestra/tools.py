#!/usr/bin/env python3
"""tools.py — the TOOL LAYER for the agentic QA/dev org (see docs/AGENTIC-QA-ORG.md, phase 1).

A tool-worker actor never runs long work inline (that would blow the runtime's 900s event lease and pin a
live browser across decide-steps). Instead it DISPATCHES one of these tools as a tracked background job and
PARKS. Each tool wraps proven code — the coverage-driven QA explorer, the git-diff-judged dev fixer — behind
ONE uniform, JSON-serialisable contract, so the runtime hook stays tiny and each tool is unit-testable in
isolation (this file's selftest stubs the heavy deps, no browser / no API):

    run_tool(name, args) -> {"status": "done"|"failed"|"checkpoint"|"internal_review",
                            "findings": [...], "result": {...}}

Tools:
  qa_explore  {story, target_url, vision, token?, org?, artifact_dir?, max_steps?}
              -> explore ONE story (coverage-driven, checkpointed, video). findings = the bugs it found;
                 result = {coverage ledger, stop_reason, video, steps}.
  dev_fix     {bug, code_context, vision, repo?, target_url?, stories?, restart_cmd?, health_url?, token?, org?}
              -> plan+spawn fixers, judge on the REAL git diff + a fresh observation. result = the fix dict.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

_QA = Path(__file__).resolve().parent.parent / "qa"
_SCRIPTS = Path(__file__).resolve().parent.parent          # for pulse, factory, etc.
for _p in (str(_QA), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def effect_record(action: str, resource: str, *, content=None, artifact=None, tenant=None,
                  actor="tool-worker", extra=None) -> dict:
    """PROVABLE side-effect (IMPROVEMENTS-PLAN item 13). Every REAL external action — a file written, an egress
    fetched, a commit — records a tamper-evident EFFECT into the audit chain with a content HASH + an
    IDEMPOTENCY key, so the org can PROVE what actually happened (not just that an agent said it did) and a
    retried step is recognisable. Returns {hash, idempotency_key, effect_id}. Best-effort: never raises — an
    observability write must not break the work it observes."""
    import hashlib
    body = content
    if body is None and artifact:
        try:
            body = Path(artifact).read_bytes()
        except Exception:
            body = None
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    digest = hashlib.sha256(body).hexdigest() if body is not None else None
    idem = hashlib.sha256(f"{action}|{resource}|{digest}".encode()).hexdigest()[:16]
    eid = None
    try:
        import audit
        payload = {"artifact": str(resource), "sha256": digest, "idempotency_key": idem,
                   "bytes": len(body) if body is not None else None}
        if extra:
            payload.update(extra)
        eid = audit.append(actor=actor, action=f"Effect:{action}", resource=str(resource),
                           payload=payload, tenant_id=tenant)[0]
    except Exception:
        pass
    return {"hash": digest, "idempotency_key": idem, "effect_id": eid}


def _release_blocking(bug: dict) -> bool:
    """Translate explorer semantics into the release gate.

    Explorer ``blocking`` means "this defect prevents further exploration". It does not mean a serious defect
    is safe to ship. High/critical grounded bugs block release and receive a dev handoff even when QA could
    continue testing the rest of the story.
    """
    return bool(bug.get("blocking") or str(bug.get("severity") or "").lower() in ("high", "critical"))


def qa_explore(args: dict) -> dict:
    """Explore ONE story to coverage-completion. The browser lives only for this call (the dispatch-and-park
    job), never across decide-steps. Bugs become findings; the coverage ledger + stop reason + video go in
    result so the qa-coordinator can decide gap-fill / hand-off / accept."""
    _apply_tenant_ctx(args.get("tenant"), args.get("org"), product=args.get("product"))
    import qa_explorer
    import campaign_checkpoint
    import artifacts
    deadline_provider = args.get("_deadline_provider")
    current_deadline = (deadline_provider() if callable(deadline_provider) else args.get("_deadline"))
    if ((args.get("_cancel_event") is not None and args["_cancel_event"].is_set())
            or (current_deadline is not None and time.time() >= current_deadline)):
        return {"status": "failed", "findings": [],
                "result": {"stop_reason": "cancelled-before-browser-start"}}
    story = args.get("story") or {}
    # Focused review tasks are durable and can outlive the controller generation that serialized them. Apply
    # narrowly-scoped contract migrations at execution time so a rolling fix takes effect on resumed actors
    # instead of replaying a known-invalid assertion indefinitely.
    if str(story.get("category") or "") == "focused-regression":
        try:
            import dev_loop
            story = dev_loop._normalize_focused_repro_contract(story)
        except Exception:
            pass
    sid = _safe_slug(story.get("id") or story.get("title") or "story")
    product = _safe_slug(args.get("product") or "product")
    # A process can die after the browser checkpoint is durable but before its
    # cancellation result updates actor memory. On a proven resumed generation,
    # recover the latest revision-fenced state from the product-scoped evidence
    # root rather than replaying a long setup flow from an empty localStorage.
    if args.get("_tool_resumed") and (
            not args.get("resume_state_path") or not args.get("resume_steps_detail")):
        try:
            import dev_loop
            repo = Path.home() / "projects" / "products" / str(args.get("product") or "")
            resume = dev_loop._latest_resume_checkpoint(
                story, repo, product=str(args.get("product") or ""))
            if resume:
                args = dict(args)
                current_covered = sum(bool(item.get("covered")) for item in (
                    args.get("resume_coverage") or []) if isinstance(item, dict))
                recovered_covered = sum(bool(item.get("covered")) for item in (
                    resume.get("coverage") or []) if isinstance(item, dict))
                # The checkpoint triplet is inseparable. Prefer it when actor memory has no portable state,
                # or when its sealed evidence proves at least as much as the actor's ledger. This repairs the
                # crash window where storage/labels landed but the matching result event did not.
                if (not args.get("resume_state_path")
                        or (resume.get("steps_detail") and recovered_covered >= current_covered)):
                    args["resume_state_path"] = resume["resume_state_path"]
                    args["resume_coverage"] = list(resume.get("coverage") or [])
                    args["resume_covered"] = list(resume.get("covered") or [])
                if resume.get("steps_detail"):
                    args["resume_steps_detail"] = campaign_checkpoint.compact_evidence_records(
                        resume["steps_detail"])
        except Exception:
            pass
    base_artifact = args.get("artifact_dir")
    if base_artifact:
        artifact_dir = Path(base_artifact) / f"{sid}-{os.getpid()}-{int(time.time() * 1000)}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    else:
        artifact_dir = artifacts.run_dir(f"qa-explorer-{product}-{sid}-{os.getpid()}-{int(time.time() * 1000)}")
    ex = qa_explorer.Explorer(args["target_url"], args.get("vision", ""),
                              token=args.get("token"), org=str(args.get("org", "0")),
                              artifact_dir=artifact_dir,
                              resume_state_path=args.get("resume_state_path"),
                              scope_run_id=args.get("_run_id"), scope_tenant=args.get("tenant"))
    pulse_work_id = (f"qa-explore:{args.get('product') or 'product'}:"
                     f"{story.get('id') or story.get('title') or 'story'}:{os.getpid()}")
    bugs = []
    try:
        ex.pulse_work_id = pulse_work_id
        try:
            import pulse
            pulse.start(pulse_work_id, "qa-story", tenant_id=args.get("tenant"),
                        label=f"QA story: {story.get('id') or story.get('title') or 'story'}",
                        stage="starting", progress="starting browser exploration",
                        expected_cadence_s=int(os.environ.get("AOS_QA_STORY_PULSE_CADENCE_S", "120")),
                        meta={"story": story.get("id") or story.get("title"),
                              "target_url": args.get("target_url")})
        except Exception:
            pass
        explore_args = {
            "max_steps": args.get("max_steps"),
            "on_bug": bugs.append,
            # A confirmed release-blocking defect needs a fixer before the remaining ledger can become clean.
            # Checkpoint and hand it back immediately, then resume this same story after repair instead of
            # spending the rest of the browser shift rediscovering the defect on every later action.
            "stop_on_actionable_bug": bool(args.get("stop_on_actionable_bug", True)),
        }
        # Coverage without the exact portable browser state is unsafe: a fresh localStorage context may not
        # contain the enquiry/draft/approval that the old coverage proved.
        if args.get("resume_covered") and args.get("resume_state_path"):
            explore_args["resume_covered"] = list(args["resume_covered"])
        if args.get("resume_coverage") and args.get("resume_state_path"):
            explore_args["resume_coverage"] = list(args["resume_coverage"])
        if args.get("resume_steps_detail") and args.get("resume_state_path"):
            # Prior individually judged action receipts let a resumed worker close a composite matrix instead
            # of forgetting its first cases and cycling. They are evidence context only; the current browser
            # state still gates whether any coverage can be reused.
            explore_args["resume_steps_detail"] = campaign_checkpoint.compact_evidence_records(
                args["resume_steps_detail"])
        if callable(deadline_provider):
            explore_args["deadline"] = deadline_provider
        elif current_deadline is not None:
            explore_args["deadline"] = current_deadline
        if args.get("_cancel_event") is not None:
            explore_args["cancel_event"] = args["_cancel_event"]
        records = ex.explore(story, **explore_args)
    finally:
        try:
            ex.close()
        except Exception:
            pass
        try:
            import pulse
            open_bugs = [b for b in bugs if not b.get("resolved")]
            pulse.finish(pulse_work_id, status=("done" if not open_bugs else "failed"),
                         result={"story": story.get("id") or story.get("title"),
                                 "bugs": len(open_bugs), "resolved_bugs": len(bugs) - len(open_bugs),
                                 "stop_reason": getattr(ex, "stop_reason", None)})
        except Exception:
            pass
    open_bugs = [b for b in bugs if not b.get("resolved")]
    provenance = None
    try:
        import dev_loop
        repo = args.get("repo") or (Path.home() / "projects" / "products" /
                                    str(args.get("product") or ""))
        provenance = dev_loop.capture_finding_provenance(repo, artifact_dir)
    except Exception:
        pass
    findings = []
    for b in open_bugs:
        raw_detail = b.get("bug") or b.get("detail") or b.get("title") or "defect"
        # Structured evaluators may return a nested defect object.  Slicing that mapping as if it were text
        # raises ``KeyError: slice(...)`` after the browser has already completed, losing the whole focused
        # verification. Normalize at the tool boundary so every downstream finding has a stable text contract.
        detail = (raw_detail if isinstance(raw_detail, str)
                  else json.dumps(raw_detail, sort_keys=True, default=str))
        raw_title = b.get("title") or detail or "defect"
        title = (raw_title if isinstance(raw_title, str)
                 else json.dumps(raw_title, sort_keys=True, default=str))
        item = {"kind": "bug", "title": title[:120], "detail": detail,
                "severity": b.get("severity", "medium"), "blocking": _release_blocking(b),
                "exploration_blocking": bool(b.get("blocking")),
                "story": story.get("id") or story.get("title"),
                "screenshot": b.get("shot") or b.get("screenshot"), "url": b.get("url"),
                "expected": b.get("expected"), "action": b.get("action"),
                "evidence_provenance": provenance}
        identity = {key: item.get(key) for key in ("story", "title", "detail", "url", "expected", "action")}
        identity["manifest_sha256"] = (provenance or {}).get("manifest_sha256")
        item["finding_id"] = "qaf-" + __import__("hashlib").sha256(
            json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:24]
        findings.append(item)
    artifact_evidence = list(getattr(ex, "artifact_evidence", None) or [])
    recorder_aspects = {str(item.get("aspect")) for item in artifact_evidence if item.get("aspect")}
    recorder_stage = getattr(qa_explorer, "_recorder_requirement_stage", None)
    certified_media_required = (not callable(recorder_stage) or any(
        recorder_stage(item.get("aspect")) == "end"
        for item in (getattr(ex, "coverage", None) or [])))
    video_mp4 = getattr(ex, "video_mp4", None)
    # Never advertise a partial encoder output. Prefer a fully decodable MP4, then the original fully decodable
    # Playwright WebM; a missing video is more truthful than a path to a corrupt file.
    candidates = ([Path(video_mp4)] if video_mp4 else [])
    candidates += sorted((artifact_dir / "videos").glob("*.mp4"))
    candidates += sorted((artifact_dir / "videos").glob("*.webm"))
    selected_video = None
    selected_media_facts = None
    for candidate in dict.fromkeys(candidates):
        try:
            media_facts = (artifacts.validate_media(candidate) if certified_media_required
                           else artifacts.probe_media(candidate))
            if media_facts:
                selected_video = candidate
                selected_media_facts = media_facts
                break
        except Exception:
            continue
    # Compact per-step record (the same shape qa_run._story_report emits) so the AGENTIC evidence is
    # auditable by review.py — reasoning + action + expected + ACTUAL + verdict + screenshot per step.
    steps_detail = list(args.get("resume_steps_detail") or []) + [
        {"step": r.get("step"), "recorded_at": r.get("recorded_at"),
         "action": _fmt_action(r.get("action")), "reasoning": r.get("reasoning", ""),
         "expected": r.get("expected", ""), "actual": _step_actual(r),
         "verdict": "match" if (r.get("verdict") or {}).get("matches_expected") else "mismatch",
         # Only evaluator-confirmed or mechanically proved coverage belongs in the release dossier.  Falling
         # back to the decider's optimistic intent made an uncredited action look tested to the paid jury.
         "covers": [aspect for aspect in (r.get("demonstrated") or [])
                    if str(aspect) not in recorder_aspects],
         # The exact list has already crossed Explorer's browser-grounding filter.  Preserve that distinction
         # when a broader action expectation was inconclusive, instead of making the compact dossier look like
         # an ungrounded model claim or dropping the only receipt during a later process rotation.
         "coverage_grounded": bool(r.get("demonstrated")),
         "targeting": {key: (r.get("targeting") or {}).get(key) for key in (
             "action_kind", "intended", "targeted_label", "label_matched",
             "effect_registered", "driver_ok", "external_handoff_url",
             "traversal_summary", "landmark_dwell_summary", "scenario_matrix_summary",
             "timed_transition_summary")
             if isinstance(r.get("targeting"), dict)
             and (r.get("targeting") or {}).get(key) not in (None, "")},
         "bug": r.get("bug") or ((r.get("verdict") or {}).get("bug")
                                  if isinstance(r.get("verdict"), dict) else None),
         "screenshot": (r.get("actual") or {}).get("screenshot")} for r in (records or [])]
    recorder_rows = _recorder_step_details(
        records, artifact_evidence, video=selected_video,
        recorder_trace=getattr(ex, "recorder_trace", None),
        media_validation=selected_media_facts)
    steps_detail += recorder_rows
    recorder_review = _independent_recorder_review(
        recorder_rows, story=story, repo=args.get("repo") or str(Path.home() / "projects" / "products" /
                                                                 str(args.get("product") or "")),
        deadline=(deadline_provider() if callable(deadline_provider) else current_deadline))
    if recorder_review:
        steps_detail.append(_recorder_review_row(recorder_review))
        artifact_evidence.append({
            "aspect": "independent recorder inspection",
            "stage": "inspection",
            "timestamp": recorder_review.get("reviewed_at"),
            "artifact_dir": str(artifact_dir),
            "accepted": bool(recorder_review.get("accepted")),
            "review_artifact": recorder_review.get("review_artifact"),
            "review_sha256": recorder_review.get("review_sha256"),
        })
        if not recorder_review.get("accepted"):
            # The browser worker may assemble the evidence package, but it cannot certify its own final
            # inspection. A rejected/unavailable independent review reopens only recorder-end coverage and
            # leaves the full browser checkpoint intact for internal diagnosis/resume. It is neither a product
            # bug nor a pass.
            for item in getattr(ex, "coverage", None) or []:
                if qa_explorer._recorder_requirement_stage(item.get("aspect")) == "end":
                    item["covered"] = False
            for row in recorder_rows:
                if row.get("action") == "recorder end":
                    row["verdict"] = "mismatch"
                    row["covers"] = []
            artifact_evidence[:] = [item for item in artifact_evidence
                                    if item.get("stage") not in ("end", "inspection")]
            ex.stop_reason = "independent-inspection-incomplete"
            if recorder_review.get("model_unavailable"):
                ex.infrastructure_error = "independent recorder reviewer unavailable"
    steps_detail = campaign_checkpoint.compact_evidence_records(steps_detail)
    return {"status": "done", "findings": findings,
            "result": {"story": story.get("id") or story.get("title"),
                       "title": story.get("title") or story.get("id"),
                       # Full-story recovery must never consume the deliberately narrower evidence ledger
                       # produced for an adjudication verifier. Runtime also recognizes legacy focused titles,
                       # while this explicit scope makes the durable contract unambiguous going forward.
                       "recovery_scope": ("focused" if story.get("category") == "focused-regression"
                                          else "story"),
                       "artifact_dir": str(artifact_dir),
                       "coverage": getattr(ex, "coverage", None),
                       "infrastructure_error": getattr(ex, "infrastructure_error", None),
                       "missing_capabilities": getattr(ex, "missing_capabilities", None),
                       "resume_state_path": getattr(ex, "resume_state_path", None),
                       "stop_reason": getattr(ex, "stop_reason", None),
                       "video": str(selected_video) if selected_video else None,
                       "artifact_evidence": artifact_evidence,
                       "recorder_review": recorder_review,
                       "timings": list(getattr(ex, "timings", None) or []),
                       "timings_path": str(artifact_dir / "timings.jsonl"),
                       "slow_phases": list(getattr(ex, "slow_phases", None) or []),
                       "steps": len(records or []), "steps_detail": steps_detail,
                       "bugs": len(open_bugs), "resolved_bugs": len(bugs) - len(open_bugs)}}


def _safe_slug(value) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "item")).strip("-._")[:80] or "item"


def _fmt_action(action) -> str:
    if not isinstance(action, dict):
        return str(action)
    tgt = action.get("selector") or (f"idx={action['idx']}" if action.get("idx") is not None else "")
    val = action.get("value")
    return f"{action.get('cmd', '?')} {tgt}{(' =' + repr(val)[:40]) if val else ''}".strip()


def _step_actual(record) -> str:
    """Portable before/after facts for the aggregate release dossier."""
    before, after = (record or {}).get("state") or {}, (record or {}).get("actual") or {}
    targeting = (record or {}).get("targeting") or {}
    compact = lambda value: json.dumps(value, default=str, separators=(",", ":"))
    bp, ap = before.get("perception") or {}, after.get("perception") or {}
    reloaded = (bool(bp.get("firstAt") and ap.get("firstAt"))
                and bp.get("firstAt") != ap.get("firstAt"))
    before_text = " ".join(str(before.get("bodyText") or "").split())[:300]
    after_text = " ".join(str(after.get("bodyText") or "").split())[:300]
    a11y = json.dumps((after.get("accessibilityRegions") or [])[:4], default=str)[:900]
    a11y_tree = " ".join(str(after.get("accessibilityTree") or "").split())[:800]
    a11y_events_all = list(after.get("accessibilityEvents") or [])
    a11y_events = [{k: event.get(k) for k in
                    ("mutationType", "role", "ariaLive", "ariaAtomic", "labelText", "text")
                    if event.get(k) not in (None, "")}
                   for event in a11y_events_all[-2:]]
    platform_all = list(after.get("accessibilityPlatformEvents") or [])
    platform_events = [{k: event.get(k) for k in
                        ("source", "role", "name", "value", "descendantText", "live", "atomic")
                        if event.get(k) not in (None, "")}
                       for event in platform_all[-2:]]
    actual_at_all = list(after.get("actualAssistiveTechnologyEvents") or [])
    actual_at_events = [{k: event.get(k) for k in ("utterance", "source")
                         if event.get(k) not in (None, "")}
                        for event in actual_at_all[-2:]]
    active_raw = after.get("activeElement") or {}
    active = {k: active_raw.get(k) for k in ("tag", "text", "role", "focusVisible")
              if active_raw.get(k) not in (None, "")}
    requests = list(after.get("recent_requests") or [])
    network_failures = [r for r in requests if r.get("failed") or r.get("status") is None]
    network_failure_facts = [{k: item.get(k) for k in ("ts", "method", "url", "status", "failed")
                              if item.get(k) not in (None, "")}
                             for item in network_failures[-2:]]
    network_recent_facts = [{k: item.get(k) for k in ("ts", "method", "url", "status", "failed")
                             if item.get(k) not in (None, "")}
                            for item in requests[-4:]]
    targeting_facts = {k: targeting.get(k) for k in
        ("action_kind", "action_key", "reloaded", "history_direction",
         "trusted_pointer", "trusted_pointer_types", "trusted_keyboard")
        if targeting.get(k) not in (None, "", False)}
    if targeting.get("keyboard_matrix_summary"):
        targeting_facts["keyboard_matrix"] = targeting.get("keyboard_matrix_summary")
    if targeting.get("traversal_summary"):
        targeting_facts["traversal"] = targeting.get("traversal_summary")
    if targeting.get("burst"):
        burst = targeting.get("burst") or {}
        targeting_facts["burst"] = {key: burst.get(key) for key in
                                    ("count", "interval_ms", "timestamps", "elapsed_ms")
                                    if burst.get(key) is not None}
    pointer_events = [event for event in (targeting.get("pointer_evidence") or [])
                      if isinstance(event, dict) and event.get("isTrusted") is True]
    if pointer_events:
        targeting_facts["pointer_receipts"] = [
            {key: event.get(key) for key in
             ("ts", "type", "pointerType", "clientX", "clientY", "target")
             if event.get(key) not in (None, "")}
            for event in pointer_events[-6:]
        ]
    keyboard_events = [event for event in (targeting.get("keyboard_evidence") or [])
                       if isinstance(event, dict) and event.get("isTrusted") is True]
    if keyboard_events:
        targeting_facts["keyboard_receipts"] = [
            {key: event.get(key) for key in
             ("ts", "type", "key", "code", "repeat", "detail", "target")
             if event.get(key) not in (None, "")}
            for event in keyboard_events[-8:]
        ]
    input_summary = {
        "pointer_types": sorted({str(event.get("pointerType")) for event in pointer_events
                                  if event.get("pointerType")}),
        "pointer_events": len(pointer_events),
        "keyboard_events": len(keyboard_events),
        "keyboard_repeat_downs": sum(
            1 for event in keyboard_events
            if event.get("type") == "keydown" and event.get("repeat") is True),
        "keyboard_clicks": sum(1 for event in keyboard_events if event.get("type") == "click"),
    }
    if targeting.get("burst"):
        input_summary["burst_count"] = (targeting.get("burst") or {}).get("count")
        input_summary["burst_elapsed_ms"] = (targeting.get("burst") or {}).get("elapsed_ms")
    # The release jury reads a bounded table cell. Preserve the exact compact
    # action sequence near the front instead of making it chase a later,
    # potentially clipped ``action_proof`` blob. Counts alone cannot prove a
    # five-click burst or a trusted held-key repeat sequence.
    input_events = {}
    burst_timestamps = list((targeting.get("burst") or {}).get("timestamps") or [])
    if burst_timestamps:
        input_events["burst_timestamps"] = burst_timestamps[:20]
    if pointer_events:
        input_events["pointer"] = [
            {key: event.get(key) for key in ("type", "pointerType", "ts")
             if event.get(key) not in (None, "")}
            for event in pointer_events[-6:]
        ]
    if keyboard_events:
        input_events["keyboard"] = [
            {key: event.get(key) for key in ("type", "key", "repeat", "detail", "ts")
             if event.get(key) not in (None, "")}
            for event in keyboard_events[-8:]
        ]
    # review.dossier clips each table cell. Put the decision-critical truth first and summarize event payloads
    # instead of letting long visible text hide `network_failures=[]` or the Chromium AX delta after an ellipsis.
    # Put release-blocking facts before the bounded action trace. review.dossier clips cells; a large burst or
    # pointer receipt must never hide final URL/console/network truth or the visible before->after value.
    critical = (f"driver_ok={targeting.get('driver_ok', True)}; "
                f"effect_registered={targeting.get('effect_registered', False)}; "
                f"target={targeting.get('targeted_label') or targeting.get('intended')!r}; "
                f"visible_status={before.get('statusText')!r}->{after.get('statusText')!r}; "
                f"input_receipt={compact(input_summary)}; "
                f"input_events={compact(input_events)}; "
                f"real_at={bool(after.get('actualAssistiveTechnologyAvailable'))}; "
                f"real_at_events={len(actual_at_all)}:{compact(actual_at_events)}; "
                f"url={before.get('url')!r}->{after.get('url')!r}; full_reload={reloaded}; "
                f"active_after={compact(active)}; "
                f"console_errors={list(after.get('console_errors') or [])!r}; "
                f"network_failures={len(network_failures)}:{compact(network_failure_facts)}; "
                f"live_events={len(a11y_events_all)}:{compact(a11y_events)}; "
                f"ax_events={len(platform_all)}:{compact(platform_events)}; "
                f"action_proof={compact(targeting_facts)}")
    return (critical + "; "
            f"network_recent={compact(network_recent_facts)}; "
            f"before visible={before_text!r}; after visible={after_text!r}; "
            f"console_errors={list(after.get('console_errors') or [])!r}; "
            f"a11y_regions={a11y}; a11y_tree={a11y_tree!r}; "
            f"settled={targeting.get('settled', False)}")


def _recorder_step_details(records, evidence, video=None, recorder_trace=None, media_validation=None):
    """Render recorder-owned proof as explicit audit rows instead of attributing it to the final click."""
    grouped = {"start": [], "end": []}
    for item in evidence or []:
        stage = str((item or {}).get("stage") or "")
        if stage in grouped:
            grouped[stage].append(dict(item))
    if not any(grouped.values()):
        return []
    actuals = [(record or {}).get("actual") or {} for record in records or []]
    console_errors = [error for actual in actuals for error in (actual.get("console_errors") or [])]
    request_facts = {}
    for actual in actuals:
        for request in actual.get("recent_requests") or []:
            key = (request.get("method"), request.get("url"), request.get("status"), request.get("failed"))
            request_facts[key] = request
    failed = []
    for request in request_facts.values():
        try:
            bad_status = request.get("status") is None or int(request.get("status")) >= 400
        except (TypeError, ValueError):
            bad_status = True
        if request.get("failed") or bad_status:
            failed.append(request)
    recorder_trace = dict(recorder_trace or {})
    if not recorder_trace:
        recorder_trace = {"console_errors": console_errors,
                          "network_requests": list(request_facts.values())}
    trace_requests = list(recorder_trace.get("network_requests") or request_facts.values())
    failed = []
    for request in trace_requests:
        try:
            bad_status = request.get("status") is None or int(request.get("status")) >= 400
        except (AttributeError, TypeError, ValueError):
            bad_status = True
        if not isinstance(request, dict) or request.get("failed") or bad_status:
            failed.append(request)
    media_facts = dict(media_validation or {}) or None
    if video and media_facts is None:
        try:
            import artifacts
            media_facts = artifacts.probe_media(video)
        except Exception:
            media_facts = None
    inspection_path = None
    inspection_sha256 = None
    artifact_dir = next((item.get("artifact_dir") for item in (evidence or [])
                         if item.get("artifact_dir")), None)
    if artifact_dir:
        try:
            import hashlib
            def observed(state):
                state = state or {}
                return {
                    "url": state.get("url"),
                    "title": state.get("title"),
                    "status_text": state.get("statusText"),
                    "visible_text": " ".join(str(state.get("bodyText") or "").split())[:1000],
                    "console_errors": list(state.get("console_errors") or []),
                    "network_requests": list(state.get("recent_requests") or []),
                    "active_element": state.get("activeElement"),
                }
            inspection = {
                "capture_started_at": recorder_trace.get("capture_started_at"),
                "capture_ended_at": recorder_trace.get("capture_ended_at"),
                "clear_receipt": recorder_trace.get("clear_receipt"),
                "console_errors": list(recorder_trace.get("console_errors") or []),
                "network_requests": list(recorder_trace.get("network_requests") or []),
                "actions": [{"step": record.get("step"), "recorded_at": record.get("recorded_at"),
                             "action_started_at": record.get("action_started_at"),
                             "action_completed_at": record.get("action_completed_at"),
                             "action": record.get("action"),
                             "expected": record.get("expected"),
                             "url": ((record.get("actual") or {}).get("url")),
                             "observed_before": observed(record.get("state")),
                             "observed_after": observed(record.get("actual")),
                             "targeting": record.get("targeting"),
                             "driver_result": record.get("act_result"),
                             "verdict": record.get("verdict"),
                             "demonstrated": list(record.get("demonstrated") or []),
                             "mechanically_proven": list(record.get("mechanically_proven") or [])}
                            for record in (records or [])],
                "screenshots": [((record.get("actual") or {}).get("screenshot"))
                                for record in (records or [])
                                if (record.get("actual") or {}).get("screenshot")],
                "video": media_facts,
            }
            encoded = json.dumps(inspection, indent=2, sort_keys=True, default=str).encode()
            inspection_path = Path(artifact_dir) / "recorder-inspection.json"
            tmp = inspection_path.with_suffix(f".tmp-{os.getpid()}")
            tmp.write_bytes(encoded)
            os.replace(tmp, inspection_path)
            inspection_sha256 = hashlib.sha256(encoded).hexdigest()
        except Exception:
            inspection_path = None
            inspection_sha256 = None
    rows = []
    for stage in ("start", "end"):
        items = grouped[stage]
        if not items:
            continue
        timestamps = [item.get("timestamp") for item in items if item.get("timestamp") is not None]
        facts = {
            "recorder_stage": stage,
            "timestamp": min(timestamps) if stage == "start" else max(timestamps),
            "capture_started_at": next((item.get("capture_started_at") for item in items
                                        if item.get("capture_started_at") is not None),
                                       min(timestamps) if timestamps else None),
            "artifact_dir": next((item.get("artifact_dir") for item in items
                                  if item.get("artifact_dir")), None),
            "clear_receipt": recorder_trace.get("clear_receipt"),
        }
        if stage == "end":
            facts.update({
                "capture_ended_at": recorder_trace.get("capture_ended_at"),
                "browser_steps": len(records or []),
                "console_errors": console_errors,
                "network_requests": trace_requests,
                "network_failures_or_4xx": failed,
                "video": str(video) if video else None,
                "video_validation": media_facts,
                "inspection_artifact": str(inspection_path) if inspection_path else None,
                "inspection_sha256": inspection_sha256,
            })
        rows.append({
            "action": f"recorder {stage}",
            "reasoning": "Recorder-owned lifecycle evidence captured independently of browser decisions.",
            "expected": "; ".join(item.get("aspect", "") for item in items),
            "actual": json.dumps(facts, sort_keys=True, default=str),
            "verdict": "match" if not console_errors and not failed else "mismatch",
            "covers": [item.get("aspect") for item in items if item.get("aspect")],
            "screenshot": ((actuals[0] if stage == "start" else actuals[-1]).get("screenshot")
                           if actuals else None),
        })
    return rows


_RECORDER_REVIEW_CHECKS = (
    "clear_boundary", "chronology", "network", "console", "action_trace", "pointer", "media",
)


def _recorder_inspection_path(rows):
    for row in reversed(rows or []):
        if row.get("action") != "recorder end":
            continue
        try:
            actual = json.loads(row.get("actual") or "{}")
        except (TypeError, ValueError):
            continue
        candidate = actual.get("inspection_artifact")
        if candidate:
            return Path(candidate)
    return None


def _recorder_mechanical_checks(inspection, *, pointer_required=False):
    """Non-probabilistic floor beneath the independent agent's semantic inspection."""
    start, end = inspection.get("capture_started_at"), inspection.get("capture_ended_at")
    clear = inspection.get("clear_receipt") or {}
    actions = [item for item in (inspection.get("actions") or []) if isinstance(item, dict)]
    requests = [item for item in (inspection.get("network_requests") or []) if isinstance(item, dict)]
    try:
        chronological = bool(float(end) > float(start))
    except (TypeError, ValueError):
        chronological = False
    action_times = [item.get("action_started_at") for item in actions]
    try:
        completed_times = [item.get("action_completed_at") for item in actions]
        action_trace = (bool(actions) and all(value is not None for value in action_times + completed_times)
                        and action_times == sorted(action_times)
                        and all(float(start) <= float(value) <= float(end) + 1 for value in action_times)
                        and all(float(started) <= float(completed) <= float(end) + 1
                                for started, completed in zip(action_times, completed_times)))
    except (TypeError, ValueError):
        action_trace = False
    network_ok = bool(requests)
    for request in requests:
        try:
            network_ok = (network_ok and request.get("ts") is not None
                          and 200 <= int(request.get("status")) < 400 and not request.get("failed"))
        except (TypeError, ValueError):
            network_ok = False
    pointer_events = [event for action in actions
                      for event in ((action.get("targeting") or {}).get("pointer_evidence") or [])
                      if isinstance(event, dict) and event.get("isTrusted") is True]
    pointer_types = {str(event.get("type") or "") for event in pointer_events}
    pointer_ok = (not pointer_required or
                  ({"pointerdown", "pointerup", "click"} <= pointer_types
                   and all(event.get("clientX") is not None and event.get("clientY") is not None
                           for event in pointer_events)))
    media = inspection.get("video") or {}
    try:
        media_ok = (media.get("decode_verified") is True and int(media.get("bytes") or 0) > 0
                    and float(media.get("duration_s") or 0) >= max(0.1, float(end) - float(start) - 3))
    except (TypeError, ValueError):
        media_ok = False
    try:
        clear_ok = bool(clear.get("cleared_at") and
                        abs(float(clear.get("cleared_at")) - float(start)) <= 0.01)
    except (TypeError, ValueError):
        clear_ok = False
    return {
        "clear_boundary": clear_ok,
        "chronology": chronological,
        "network": network_ok,
        "console": not list(inspection.get("console_errors") or []),
        "action_trace": action_trace,
        "pointer": pointer_ok,
        "media": media_ok,
    }


def _independent_recorder_review(rows, *, story, repo, deadline=None):
    """Ask a distinct governed QA agent to inspect the immutable cumulative trace before it is credited."""
    inspection_path = _recorder_inspection_path(rows)
    if inspection_path is None:
        return None
    reviewed_at = time.time()
    try:
        raw = inspection_path.read_bytes()
        inspection = json.loads(raw)
    except Exception as exc:
        return {"accepted": False, "reviewed_at": reviewed_at,
                "issues": [f"inspection artifact unreadable: {str(exc)[:200]}"],
                "checked": {key: False for key in _RECORDER_REVIEW_CHECKS},
                "model_unavailable": True}
    import hashlib
    source_sha256 = hashlib.sha256(raw).hexdigest()
    story_text = json.dumps(story or {}, sort_keys=True, default=str)
    pointer_required = "pointer" in story_text.lower()
    mechanical = _recorder_mechanical_checks(inspection, pointer_required=pointer_required)
    if deadline is not None and time.time() + 60 >= float(deadline):
        response = {"accepted": False, "checked": mechanical,
                    "issues": ["insufficient lease runway for an independent paid inspection"]}
        model_unavailable = True
        model_meta = {}
    else:
        prompt = f"""You are a separate senior QA evidence inspector. You did not drive this browser session.
Read the immutable recorder package below skeptically and decide whether it proves the supplied story's
complete cumulative trace. This source package IS the attached inspection artifact; your independently
persisted verdict will be the separate inspection receipt, so do not demand a pre-existing copy of your own
future verdict. Do not edit files, run the product, or infer missing facts. Reject if timestamps
are absent/out of order; the explicit clear receipt is missing; any request lacks timestamp/status or failed;
console errors exist; pointer use is required but lacks trusted pointerdown/pointerup/click with coordinates;
the action sequence is incomplete; or the media is not full-decode-verified and long enough to span the
capture boundaries. Mechanical checks are a floor, not a reason to rubber-stamp weak semantic evidence.

STORY:
{story_text}

SOURCE_SHA256: {source_sha256}
MECHANICAL_CHECKS: {json.dumps(mechanical, sort_keys=True)}
IMMUTABLE_RECORDER_PACKAGE:
{raw.decode('utf-8', 'replace')[:50000]}

Return ONLY JSON with exactly this shape:
{{"accepted":true|false,"checked":{{"clear_boundary":true|false,"chronology":true|false,
"network":true|false,"console":true|false,"action_trace":true|false,"pointer":true|false,
"media":true|false}},"issues":["specific evidence gap, or empty when accepted"],
"summary":"one concise evidence-based conclusion"}}"""
        try:
            import qa_explorer
            review_timeout = qa_explorer._EVALUATE_TIMEOUT_S
            if deadline is not None:
                review_timeout = max(1, min(review_timeout, int(float(deadline) - time.time() - 5)))
            try:
                model_result = qa_explorer._call_agent(
                    "reviewer", str(repo), prompt, timeout=review_timeout, retries=0)
            except TypeError:
                model_result = qa_explorer._call_agent("reviewer", str(repo), prompt)
            response = qa_explorer._extract_json(
                (model_result or {}).get("out_full") or (model_result or {}).get("out") or "")
            model_unavailable = int((model_result or {}).get("rc", 1)) != 0 or not response
            model_meta = {key: (model_result or {}).get(key) for key in
                          ("model", "engine", "tokens_in", "tokens_out", "cost_usd")
                          if (model_result or {}).get(key) is not None}
        except Exception as exc:
            response = {"accepted": False, "checked": {}, "issues": [str(exc)[:300]]}
            model_unavailable = True
            model_meta = {}
    checked = response.get("checked") if isinstance(response, dict) else {}
    checked = checked if isinstance(checked, dict) else {}
    all_mechanical = all(mechanical.get(key) is True for key in _RECORDER_REVIEW_CHECKS)
    all_agent = all(checked.get(key) is True for key in _RECORDER_REVIEW_CHECKS)
    accepted = bool(response.get("accepted") is True and all_mechanical and all_agent and not model_unavailable)
    issues = [str(item)[:500] for item in (response.get("issues") or []) if str(item).strip()]
    if not all_mechanical:
        issues.append("mechanical evidence floor failed: " + ", ".join(
            key for key in _RECORDER_REVIEW_CHECKS if mechanical.get(key) is not True))
    if not all_agent and not model_unavailable:
        issues.append("independent inspector did not affirm: " + ", ".join(
            key for key in _RECORDER_REVIEW_CHECKS if checked.get(key) is not True))
    receipt = {
        "accepted": accepted,
        "reviewed_at": reviewed_at,
        "source_artifact": str(inspection_path),
        "source_sha256": source_sha256,
        "mechanical_checks": mechanical,
        "checked": {key: checked.get(key) is True for key in _RECORDER_REVIEW_CHECKS},
        "issues": list(dict.fromkeys(issues)),
        "summary": str(response.get("summary") or "")[:1000],
        "model_unavailable": model_unavailable,
        "reviewer": {"role": "reviewer", **model_meta},
        "source_summary": {
            "capture_started_at": inspection.get("capture_started_at"),
            "capture_ended_at": inspection.get("capture_ended_at"),
            "actions": len(inspection.get("actions") or []),
            "requests": len(inspection.get("network_requests") or []),
            "console_errors": len(inspection.get("console_errors") or []),
            "trusted_pointer_types": sorted({
                str(event.get("pointerType"))
                for action in (inspection.get("actions") or [])
                for event in (((action.get("targeting") or {}).get("pointer_evidence")) or [])
                if isinstance(event, dict) and event.get("isTrusted") is True and event.get("pointerType")
            }),
            "media": inspection.get("video"),
        },
    }
    encoded = json.dumps(receipt, indent=2, sort_keys=True, default=str).encode()
    review_path = inspection_path.with_name("recorder-inspection-review.json")
    tmp = review_path.with_suffix(f".tmp-{os.getpid()}")
    try:
        tmp.write_bytes(encoded)
        os.replace(tmp, review_path)
        receipt["review_artifact"] = str(review_path)
        receipt["review_sha256"] = hashlib.sha256(encoded).hexdigest()
    except Exception:
        receipt["accepted"] = False
        receipt["issues"].append("independent review receipt could not be persisted atomically")
    return receipt


def _recorder_review_row(review):
    return {
        "action": "independent recorder inspection",
        "reasoning": "A separately spawned governed QA agent inspected the immutable cumulative record.",
        "expected": "Independently verify trace boundaries, raw requests, console, actions, pointer proof, and media.",
        "actual": json.dumps(review, sort_keys=True, default=str),
        "verdict": "match" if review.get("accepted") else "mismatch",
        "covers": ["independent recorder inspection"] if review.get("accepted") else [],
        "screenshot": None,
    }


def dev_fix(args: dict) -> dict:
    """Fix one bug: the proven dev-fix loop (AI plans #agents -> spawns them -> judges FIXED on the real git
    diff + a fresh live observation). A safety-runway checkpoint is resumable; an evidence dispute routes to
    internal QA management. Neither is collapsed into an ordinary failure/retry cycle."""
    _apply_tenant_ctx(args.get("tenant"), args.get("org"), product=args.get("product"))
    import dev_loop
    bug = args["bug"]
    adjudication = None
    reference = bug.get("_qa_adjudication") if isinstance(bug, dict) else None
    if isinstance(reference, dict) and reference.get("case_id") and reference.get("review_id"):
        # Never trust an agent-supplied bypass flag. Resolve it through the tenant-scoped durable case and
        # require the exact case/review/finding identity plus a terminal senior disposition.
        try:
            import qareview
            case = qareview.get(str(args.get("tenant") or ""), str(reference["case_id"]))
            outcome = dict(case.get("outcome") or {})
            finding_id = bug.get("finding_id")
            if (case.get("review_id") == reference.get("review_id")
                    and case.get("status") == "resolved"
                    and outcome.get("disposition") == "confirmed_defect"
                    and (not finding_id or not case.get("finding_id")
                         or case.get("finding_id") == finding_id)):
                adjudication = {**outcome, "case_id": case["case_id"],
                                "review_id": case["review_id"],
                                "finding_id": case.get("finding_id")}
        except Exception:
            adjudication = None
    stories = list(args.get("stories") or [])
    story_id = bug.get("story") if isinstance(bug, dict) else None
    if story_id:
        matched = [s for s in stories if isinstance(s, dict)
                   and (s.get("id") == story_id or s.get("title") == story_id)]
        if matched:
            stories = matched
    # A single defect must never launch a second full-corpus QA campaign inside its fixer.  If an old
    # finding has no usable story identity, one representative story is the safest bounded fallback.
    if len(stories) > 1:
        stories = stories[:1]
    live_deadline = (args.get("_deadline_provider")
                     if callable(args.get("_deadline_provider")) else args.get("_deadline"))
    fix = dev_loop.fix_bug(bug, args.get("code_context") or {}, args.get("vision", ""),
                           repo=args.get("repo"), target_url=args.get("target_url"),
                           stories=stories, restart_cmd=args.get("restart_cmd"),
                           health_url=args.get("health_url"), token=args.get("token"), org=args.get("org"),
                           max_steps=args.get("max_steps"), deadline=live_deadline,
                           cancel_event=args.get("_cancel_event"),
                           scope_run_id=args.get("_run_id"), scope_tenant=args.get("tenant"),
                           adjudication=adjudication,
                           resume_triage_finding=args.get("resume_triage_finding"),
                           resume_triage_receipt=args.get("resume_triage_receipt"),
                           resume_changed_files=args.get("resume_changed_files"),
                           resume_change_diff=args.get("resume_change_diff"),
                           resume_state_path=args.get("resume_state_path"),
                           resume_covered=args.get("resume_covered"),
                           resume_coverage=args.get("resume_coverage"),
                           resume_steps_detail=args.get("resume_steps_detail"),
                           resume_existing=(bool(args.get("_tool_resumed"))
                                            or int(args.get("_tool_attempt") or 0) > 0))

    if args.get("resume_change_diff") and not fix.get("change_diff"):
        # Early recovery exits predate the fresh-writer final return. Never drop the predecessor's exact
        # mutation receipt merely because the current generation completed at a read-only gate.
        fix = dict(fix)
        fix["change_diff"] = str(args["resume_change_diff"])[:dev_loop.DIFF_LIMIT]

    status = ("done" if fix.get("fixed") else
              ("checkpoint" if fix.get("checkpoint_required") else
               ("internal_review" if fix.get("internal_review_required") else "failed")))
    return {"status": status, "findings": [], "result": fix}


def qa_review(args: dict) -> dict:
    """Run one durable, read-only QA evidence management chain.

    Missing legacy provenance is not guessed around: the coordinator receives a concrete instruction to
    collect a fresh browser observation. A valid case is stable across retries, lease fenced, and reviews are
    reused after a crash. No product mutation or generic human escalation is possible in this tool.
    """
    import qareview

    tenant = str(args.get("tenant") or "")
    record = dict(args.get("internal_review") or {})
    review_id = record.get("review_id")
    story = record.get("story") or (record.get("finding") or {}).get("story")
    base = {"review_id": review_id, "story": story, "finding": record.get("finding")}
    _apply_tenant_ctx(tenant, args.get("org"), product=args.get("product"))
    try:
        submitted = qareview.submit(
            tenant, record, repo=args.get("repo") or ".", thread_id=args.get("thread_id"),
            run_id=args.get("_run_id"), coordinator_actor_id=args.get("coordinator_actor_id"),
            work_ref=args.get("work_ref") or f"orchestra:{args.get('_run_id')}:{review_id}")
    except ValueError as exc:
        if "provenance" in str(exc).lower():
            return {"status": "done", "findings": [], "result": {
                **base, "status": "fresh_evidence_required", "disposition": None,
                "reason": str(exc), "fresh_evidence_required": True}}
        raise
    case_id = submitted["case_id"]
    if isinstance(args.get("state"), dict):
        qareview.state_changed(tenant, case_id, args["state"], actor="qa-coordinator")
    current = qareview.get(tenant, case_id)
    if current.get("outcome"):
        return {"status": "done", "findings": [], "result": {**base, **current,
                **(current.get("outcome") or {})}}
    lease = qareview.claim_case(
        tenant, case_id, f"qa-review:{os.getpid()}:{args.get('_run_id')}", lease_s=480)
    if not lease:
        return {"status": "done", "findings": [], "result": {
            **base, **current, "disposition": None, "waiting_for_state_change": True}}
    cancel = args.get("_cancel_event")
    deadline = args.get("_deadline")
    should_stop = lambda: bool((cancel is not None and cancel.is_set())
                               or (deadline is not None and time.time() >= float(deadline)))
    outcome = qareview.adjudicate(tenant, case_id, lease["lease_token"], should_stop=should_stop)
    if outcome.get("checkpoint_required"):
        return {"status": "checkpoint", "findings": [], "result": {**base, **outcome}}
    return {"status": "done", "findings": [], "result": {**base, **outcome}}


def _agent_tool(role: str, prompt: str, args: dict) -> dict:
    """Shared shape for knowledge-work tools: run ONE role-specialized factory agent (web on by default, so
    research/intel/finance agents reach live data) and return its report as the result. The tool-worker
    dispatch-and-parks it, so a long web-research or analysis call never blocks a decide-step."""
    import factory
    _apply_tenant_ctx(args.get("tenant"), args.get("org"), product=args.get("product"))  # billing/authority
    #        runs in a jobrunner thread with no inherited factory._ctx, so without this the whole company org's
    #        knowledge work (research/finance/legal/data/artifact/design) would spend on the PLATFORM default.
    res = factory.agent(role, args.get("repo") or str(getattr(factory, "PRODUCTS", "/tmp")), prompt)
    out = (res.get("out_full") or res.get("out") or "") if isinstance(res, dict) else str(res)
    ok = isinstance(res, dict) and res.get("rc", 0) == 0 and bool(out.strip())
    return {"status": "done" if ok else "failed", "findings": [], "result": {"report": out, "role": role}}


def research(args: dict) -> dict:
    """A researcher's real work: thorough, web-grounded research on a topic → a concise, cited report.
    (Same tool-worker pattern as QA — this is how a 'researcher' role DOES work instead of guessing.)"""
    topic = args.get("topic") or args.get("task") or args.get("question") or ""
    r = _agent_tool("researcher", "Research this THOROUGHLY using live web sources. Cross-check claims across "
                    "independent sources; be concrete and skeptical. Produce a concise report with the key "
                    "findings, the evidence, and CITATIONS (urls).\n\nTOPIC:\n" + topic, args)
    r["result"]["topic"] = topic
    return r


def _apply_tenant_ctx(tenant, org, product=None):
    """BILLING CORRECTNESS for a tool running in a jobrunner background thread — which does NOT inherit the
    caller's thread-local factory._ctx. Resolve THIS tenant's connected provider and wire engine/keys into
    _ctx so model spend lands on THEIR account, never the platform default. Returns the resolved CLAUDE
    api_key (or None for codex/subscription/platform) to hand to research_one. tenant None/'platform' -> the
    host subscription CLI (no key). Mirrors loopcontroller._apply_provider_ctx so both engines agree.
    FAIL-OPEN: a ctx-rebuild hiccup (or a stubbed factory with no _ctx) must never break the tool."""
    import factory
    ctx = getattr(factory, "_ctx", None)
    if ctx is None:
        return None
    tid = tenant if tenant not in (None, "", "platform") else None
    ctx.tenant, ctx.org = tid, org
    if product:
        ctx.product, ctx.stage = str(product), "QA"
    # A pooled tool thread may retain a previous tenant's provider, so always clear
    # credentials.  Platform/internal work must then use the factory's configured
    # host engine (Codex-first today), not silently force Claude.  Forcing Claude
    # here made a healthy authenticated Codex host repeatedly invoke an
    # unauthenticated Claude CLI; the explorer subsequently saw empty model output.
    ctx.engine = str(getattr(factory, "DEFAULT_ENGINE", "codex") or "codex").lower()
    ctx.api_key, ctx.codex_key = None, None
    if not tid:
        return None
    # A tenant is provider-explicit.  Start from the fail-closed Claude shape so a
    # missing resolver record reaches factory.agent's provider guard; a resolved
    # Codex tenant switches this below.
    ctx.engine = "claude"
    try:
        import tenantproviders
        r = tenantproviders.resolve(tid) or {}
    except Exception:
        r = {}
    if r.get("engine") == "codex":
        ctx.engine, ctx.codex_key = "codex", r.get("key")
        return None
    ctx.api_key = r.get("key")
    return r.get("key")


def research_subq(args: dict) -> dict:
    """One researcher's real work IN THE run_org ENGINE: answer ONE sub-question via
    research_fleet.research_one so it writes the CONTRACT finding (findings/NN.md) that
    research_fleet.synthesize later reads — this is what lets research run as a crash-resumable durable org
    (each subq is a dispatch-and-parked, lease-reclaimable tool job) WITHOUT changing the console output.
    Rebuilds factory._ctx from the tenant id in args (never a persisted api_key) so spend is billed to the
    right account. args: {idx, subq|task, repo, tenant?, org?}."""
    import research_fleet
    idx = int(args.get("idx") or 0)
    subq = args.get("subq") or args.get("task") or ""
    repo = args.get("repo")
    if not (subq and repo):
        return {"status": "failed", "findings": [], "result": {"error": "research_subq needs {subq, repo}"}}
    key = _apply_tenant_ctx(args.get("tenant"), args.get("org"))
    res = research_fleet.research_one(Path(repo), idx, subq, api_key=key)
    return {"status": "done" if res.get("ok") else "failed", "findings": [], "result": res}


def finance_report(args: dict) -> dict:
    """A finance function's real work: assemble a CEO-facing financial report from the data provided (or the
    platform's own metrics/billing if present) — spend, revenue, burn, runway, unit economics — honestly."""
    data = args.get("data")
    if data is None:                              # pull the platform's real numbers when no data is passed
        try:
            import billing
            data = billing.summary() if hasattr(billing, "summary") else None
        except Exception:
            data = None
    prompt = ("You are the finance function reporting to the CEO. From the DATA below, produce a crisp, honest "
              "financial report: spend, revenue, burn, runway, unit economics, and the ONE number the CEO "
              "should watch. State assumptions; never invent figures not supported by the data.\n\nDATA:\n"
              + json.dumps(data, default=str)[:4000])
    r = _agent_tool("finance-cost-controller", prompt, args)
    r["result"]["kind"] = "finance-report"
    return r


_LEGAL_RISKS = [(r"(?i)\bunlimited\s+liability\b", "unlimited liability", "high"),
                (r"\b\d{3}-\d{2}-\d{4}\b", "possible SSN / PII in the document", "high"),
                (r"(?i)\bperpetual\b.*\b(?:license|right)s?\b", "perpetual grant — review", "medium"),
                (r"(?i)\bindemnif", "indemnification clause — review scope", "medium"),
                (r"(?i)\bauto[- ]?renew", "auto-renewal — confirm notice period", "low")]


def legal_scan(args: dict) -> dict:
    """A legal/compliance function's REAL action: scan a document against policy. Deterministic first (required
    clauses that are MISSING = blocking; risky/PII patterns = flagged), then an AI compliance review. Findings
    flow to the coordinator exactly like QA bugs. `doc`/`text` inline or `path` to a file; `policy` = required
    clauses/keywords."""
    doc = args.get("doc") or args.get("text") or ""
    if not doc and args.get("path"):
        try:
            doc = Path(args["path"]).read_text(errors="ignore")[:40000]
        except Exception:
            doc = ""
    policy = args.get("policy") or []
    findings = []
    for req in policy:                            # required clause missing -> a BLOCKING finding
        if str(req).lower() not in doc.lower():
            findings.append({"kind": "missing-clause", "title": f"policy requires '{req}' — not present",
                             "severity": "high", "blocking": True})
    for pat, label, sev in _LEGAL_RISKS:
        if re.search(pat, doc):
            findings.append({"kind": "risk", "title": label, "severity": sev, "blocking": False})
    review = _agent_tool("legal-compliance-checklist",
                         "Review this document for legal/compliance risk: missing protections, risky terms, "
                         f"and PII. Policy requirements: {policy}. Be specific.\n\nDOC:\n{doc[:6000]}", args)
    blocking = any(f["blocking"] for f in findings)
    return {"status": "failed" if blocking else "done", "findings": findings,
            "result": {"issues": len(findings), "blocking": blocking,
                       "review": (review.get("result") or {}).get("report")}}


def connector_ingest(args: dict) -> dict:
    """Reach a LIVE external source through the GOVERNED connector (DNS-pinned, allowlisted egress — the same
    egress policy the platform enforces), then summarise it. This is how research/data/intel agents pull real
    external data safely. Denied/blocked egress is a failed result, never a crash."""
    url = args.get("url") or ""
    try:
        import connectors
        content = connectors.ingest(url, args.get("product", "platform"),
                                    role=args.get("role", "data-engineer"))
    except Exception as e:
        return {"status": "failed", "findings": [], "result": {"error": f"connector denied/failed: {e}"}}
    eff = effect_record("connector_ingest", url, content=str(content),
                        tenant=args.get("org") or args.get("tenant"),
                        actor=f"tool:{args.get('role', 'data-engineer')}", extra={"egress": True})
    s = _agent_tool(args.get("role", "data-engineer"),
                    f"Summarise this content fetched from {url} for the CEO (2-4 honest lines):\n"
                    + str(content)[:5000], args)
    return {"status": "done", "findings": [],
            "result": {"url": url, "summary": (s.get("result") or {}).get("report"), "effect": eff}}


def data_query(args: dict) -> dict:
    """A data function's REAL external action: run a READ-ONLY query against the platform DB and summarise the
    result for the CEO. Refuses anything but a single SELECT (no writes, no semicolons) — a data agent reads,
    it never mutates. This is the template for external-action tools (connectors, live systems)."""
    sql = (args.get("sql") or "").strip().rstrip(";")
    if not sql.lower().startswith("select") or ";" in sql:
        return {"status": "failed", "findings": [], "result": {"error": "data_query runs ONE read-only SELECT"}}
    try:
        import psycopg
        import pulse
        with psycopg.connect(pulse.DB) as c, c.cursor() as cur:
            cur.execute(sql + ("" if "limit" in sql.lower() else " LIMIT 200"))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        return {"status": "failed", "findings": [], "result": {"error": f"query failed: {e}"}}
    s = _agent_tool("data-analyst", "Summarise this query result for the CEO in 2-3 honest lines (call out "
                    f"anything notable).\nQUERY: {sql}\nROWS ({len(rows)}): {json.dumps(rows, default=str)[:3000]}",
                    args)
    return {"status": "done", "findings": [],
            "result": {"sql": sql, "row_count": len(rows), "rows": rows[:20],
                       "summary": (s.get("result") or {}).get("report")}}


def produce_artifact(args: dict) -> dict:
    """A function that OUTPUTS a real deliverable FILE — marketing copy, a landing page, a spec doc, a design
    mockup, a report. The role agent generates the content; we WRITE it to a Windows-visible artifacts dir and
    return the path (so the CEO gets a real file, not just chat). `role`, `task`, `filename` (its extension
    picks the format), optional `context`. This is the output counterpart to the read tools (research/data)."""
    import artifacts
    role = args.get("role") or args.get("worker_role") or "specialist"
    filename = Path(args.get("filename") or "artifact.md").name
    fmt = (Path(filename).suffix.lstrip(".") or "md")
    prompt = (f"You are the {role}. Produce the deliverable below as a COMPLETE, ready-to-use {fmt} file — "
              f"output ONLY the file content, no preamble or code fences.\n\nDELIVERABLE:\n{args.get('task', '')}"
              + (f"\n\nCONTEXT:\n{json.dumps(args.get('context'), default=str)[:3000]}" if args.get("context") else ""))
    r = _agent_tool(role, prompt, args)
    content = (r.get("result") or {}).get("report") or ""
    if r["status"] != "done" or not content.strip():
        return {"status": "failed", "findings": [], "result": {"error": "agent produced no content"}}
    try:
        path = artifacts.run_dir(args.get("product") or role) / filename
        path.write_text(content)
    except Exception as e:
        return {"status": "failed", "findings": [], "result": {"error": f"write failed: {e}"}}
    eff = effect_record("produce_artifact", str(path), content=content,
                        tenant=args.get("org") or args.get("tenant"), actor=f"tool:{role}")
    return {"status": "done", "findings": [],
            "result": {"artifact": str(path), "role": role, "format": fmt, "bytes": len(content), "effect": eff}}


def design_asset(args: dict) -> dict:
    """A design function's real artifact: generate a self-contained SVG (or HTML) mockup/asset the CEO can open.
    The designer agent outputs valid, self-contained markup; we save it as a viewable file."""
    fmt = (args.get("format") or "svg").lower()
    spec = args.get("spec") or args.get("task") or ""
    return produce_artifact({**args, "role": args.get("role") or "brand-designer",
                             "task": f"Design a clean, on-brand {fmt.upper()} asset for: {spec}. Output valid, "
                                     f"SELF-CONTAINED {fmt} markup only (no external refs).",
                             "filename": args.get("filename") or f"asset.{fmt}"})


def knowledge_work(args: dict) -> dict:
    """The catch-all for any KNOWLEDGE-WORK function — a product-manager writing a spec, a legal-compliance
    review of a provided doc, a strategy memo, an analysis. Runs the given role agent on the task and returns
    its deliverable. This is how MOST of the 92 role charters do real work (no external tool needed) — pair it
    with a coordinator's tool-team context (tool='knowledge_work', worker_role='<role>')."""
    role = args.get("role") or args.get("worker_role") or "specialist"
    task = args.get("task") or args.get("prompt") or args.get("topic") or ""
    ctx = args.get("context")
    prompt = (f"You are the {role}. Do this task to an elite, CEO-grade standard and return your deliverable "
              f"(be concrete and honest; state assumptions).\n\nTASK:\n{task}"
              + (f"\n\nCONTEXT:\n{json.dumps(ctx, default=str)[:3000]}" if ctx else ""))
    return _agent_tool(role, prompt, args)


_TOOLS = {"qa_explore": qa_explore, "dev_fix": dev_fix, "qa_review": qa_review,
          "research": research, "research_subq": research_subq,
          "finance_report": finance_report, "knowledge_work": knowledge_work, "data_query": data_query,
          "legal_scan": legal_scan, "connector_ingest": connector_ingest,
          "produce_artifact": produce_artifact, "design_asset": design_asset}


def run_tool(name: str, args: dict) -> dict:
    """The single uniform entry the tool-worker calls. Never raises — a tool error is a failed result the
    coordinator can react to (park/retry/escalate), never a crash of the org."""
    fn = _TOOLS.get(name)
    if not fn:
        return {"status": "failed", "findings": [], "result": {"error": f"unknown tool {name!r}"}}
    try:
        out = fn(args or {})
    except Exception as e:
        return {"status": "failed", "findings": [], "result": {"error": f"{type(e).__name__}: {e}"}}
    # normalise the contract so the runtime hook can trust the shape
    out.setdefault("status", "done")
    out.setdefault("findings", [])
    out.setdefault("result", {})
    return out


def _selftest():
    import types
    # 1) qa_explore routes to the Explorer, shapes bugs->findings + coverage into result. Stub the Explorer.
    fake_qx = types.ModuleType("qa_explorer")

    class _StubEx:
        def __init__(self, *a, **k):
            self.coverage = [{"aspect": "open panel", "covered": True},
                             {"aspect": "empty submit", "covered": False}]
            self.stop_reason = "coverage-complete"
            self.video_mp4 = "/tmp/x/qa-session.mp4"

        def explore(self, story, max_steps=None, on_bug=None):
            on_bug({"bug": "panel rendered blank", "severity": "high", "blocking": True,
                    "shot": "/tmp/x/s1.png", "url": "http://app/#/x"})
            return [{"step": 0}, {"step": 1}]

        def close(self):
            pass

    fake_qx.Explorer = _StubEx
    sys.modules["qa_explorer"] = fake_qx
    import artifacts as _selftest_artifacts
    _real_validate_media = _selftest_artifacts.validate_media
    _selftest_artifacts.validate_media = lambda path: {
        "path": str(path), "duration_s": 1.0, "decode_verified": True}
    try:
        r = run_tool("qa_explore", {"target_url": "http://app", "vision": "v",
                                    "story": {"id": "US1", "title": "open"}})
    finally:
        _selftest_artifacts.validate_media = _real_validate_media
    assert r["status"] == "done", r
    assert len(r["findings"]) == 1 and r["findings"][0]["blocking"] is True, r
    assert r["findings"][0]["title"] == "panel rendered blank" and r["findings"][0]["story"] == "US1"
    assert r["result"]["stop_reason"] == "coverage-complete" and r["result"]["steps"] == 2
    assert r["result"]["video"].endswith(".mp4")
    assert _release_blocking({"severity": "high", "blocking": False}) is True
    assert _release_blocking({"severity": "medium", "blocking": False}) is False

    # 2) dev_fix routes to dev_loop.fix_bug; status reflects the JUDGE's verdict, not the agent's claim.
    fake_dl = types.ModuleType("dev_loop")
    fix_call = {}
    def _fake_fix(bug, ctx, vision, **kwargs):
        fix_call.update(kwargs)
        return {"fixed": True, "files": ["src/app.js"], "judged": True}
    fake_dl.fix_bug = _fake_fix
    sys.modules["dev_loop"] = fake_dl
    r2 = run_tool("dev_fix", {"bug": {"bug": "500 on pay", "story": "US2"}, "vision": "v",
                              "repo": "/tmp/r", "stories": [{"id": "US1"}, {"id": "US2"}],
                              "max_steps": 4, "_deadline": 123.0})
    assert r2["status"] == "done" and r2["result"]["files"] == ["src/app.js"], r2
    assert fix_call["stories"] == [{"id": "US2"}] and fix_call["max_steps"] == 4 \
        and fix_call["deadline"] == 123.0, fix_call
    fake_dl.fix_bug = lambda *a, **k: {"fixed": False, "error": "judge said not fixed"}
    r3 = run_tool("dev_fix", {"bug": {"bug": "x"}, "vision": "v"})
    assert r3["status"] == "failed", r3

    # 3) knowledge-work tools (research, finance_report) run a role agent and return its report. Stub factory.
    fake_f = types.ModuleType("factory")
    fake_f.PRODUCTS = "/tmp"
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": f"[{role}] report: " + task[:30]}
    sys.modules["factory"] = fake_f
    rr = run_tool("research", {"topic": "the market for AI QA tools"})
    assert rr["status"] == "done" and rr["result"]["role"] == "researcher" and "report" in rr["result"], rr
    assert rr["result"]["topic"] == "the market for AI QA tools"
    rf = run_tool("finance_report", {"data": {"spend": 100, "revenue": 250}})
    assert rf["status"] == "done" and rf["result"]["role"] == "finance-cost-controller", rf
    # knowledge_work: any role (product-manager, legal-compliance, strategist, …) does a deliverable.
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": f"[{role}] deliverable"}
    kw = run_tool("knowledge_work", {"role": "product-manager", "task": "write the launch spec"})
    assert kw["status"] == "done" and kw["result"]["role"] == "product-manager", kw
    lg = run_tool("knowledge_work", {"role": "legal-compliance-checklist", "task": "review the ToS", "context": {"doc": "..."}})
    assert lg["status"] == "done" and "deliverable" in lg["result"]["report"], lg
    # data_query: a REAL read-only DB query (external action) + write-refusal.
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": "summary"}
    import pulse as _pulse
    if _pulse.DB:                                 # a real SELECT against the platform DB
        dq = run_tool("data_query", {"sql": "SELECT 1 AS one"})
        assert dq["status"] == "done" and dq["result"]["row_count"] == 1, dq
    assert run_tool("data_query", {"sql": "DELETE FROM agent_pulse"})["status"] == "failed", "writes refused"
    assert run_tool("data_query", {"sql": "SELECT 1; DROP TABLE x"})["status"] == "failed", "multi-stmt refused"

    # legal_scan: a MISSING required clause = blocking finding; risky patterns (PII, unlimited liability) flagged.
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": "legal review"}
    ls = run_tool("legal_scan", {"doc": "This has unlimited liability and SSN 123-45-6789.",
                                 "policy": ["limitation of liability", "governing law"]})
    assert ls["status"] == "failed", ls          # required clauses missing -> blocking
    ts = " ".join(f["title"] for f in ls["findings"])
    assert "limitation of liability" in ts and "liability" in ts.lower() and ("PII" in ts or "SSN" in ts), ts
    lok = run_tool("legal_scan", {"doc": "limitation of liability applies; governing law is X.",
                                  "policy": ["limitation of liability", "governing law"]})
    assert lok["status"] == "done" and not any(f["blocking"] for f in lok["findings"]), lok

    # connector_ingest: governed external fetch (stub connectors) + summary; a denial -> failed, never a crash.
    fake_c = types.ModuleType("connectors")
    fake_c.ingest = lambda url, product, role="data-engineer", **k: "fetched: " + url
    sys.modules["connectors"] = fake_c
    ci = run_tool("connector_ingest", {"url": "https://example.com/x"})
    assert ci["status"] == "done" and ci["result"]["url"] == "https://example.com/x", ci
    fake_c.ingest = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("egress denied"))
    assert run_tool("connector_ingest", {"url": "http://evil"})["status"] == "failed"

    # artifact-output tools: the role agent's content is WRITTEN to a real file the CEO can open.
    import tempfile
    _prev_ev = os.environ.get("AOS_QA_EVIDENCE_DIR")
    os.environ["AOS_QA_EVIDENCE_DIR"] = tempfile.mkdtemp(prefix="artifact-test-")
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": "# Launch copy\nBuy our thing."}
    pa = run_tool("produce_artifact", {"role": "content-marketer", "task": "landing page copy",
                                       "filename": "landing.md", "product": "demo"})
    assert pa["status"] == "done" and Path(pa["result"]["artifact"]).exists(), pa
    assert Path(pa["result"]["artifact"]).read_text().startswith("# Launch copy"), "content written to the file"
    # item 13: a provable EFFECT record — content hash + idempotency key — accompanies the real file write.
    eff = pa["result"].get("effect") or {}
    assert eff.get("hash") and len(eff["hash"]) == 64 and eff.get("idempotency_key"), f"effect record: {eff}"
    import hashlib as _h
    assert eff["hash"] == _h.sha256(b"# Launch copy\nBuy our thing.").hexdigest(), "effect hash must bind the real bytes"
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": "<svg xmlns='http://www.w3.org/2000/svg'/>"}
    da = run_tool("design_asset", {"spec": "a logo", "product": "demo"})
    assert da["status"] == "done" and da["result"]["artifact"].endswith(".svg") and Path(da["result"]["artifact"]).exists(), da
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": "   "}   # empty content -> failed, no file
    assert run_tool("produce_artifact", {"role": "x", "task": "y", "filename": "z.md"})["status"] == "failed"
    import shutil as _sh
    _sh.rmtree(os.environ["AOS_QA_EVIDENCE_DIR"], ignore_errors=True)
    if _prev_ev is None:
        os.environ.pop("AOS_QA_EVIDENCE_DIR", None)
    else:
        os.environ["AOS_QA_EVIDENCE_DIR"] = _prev_ev

    fake_f.agent = lambda role, repo, task, **k: {"rc": 1, "out_full": ""}   # a failed agent -> failed tool
    assert run_tool("research", {"topic": "x"})["status"] == "failed"

    # 4) unknown tool + a raising tool are FAILED results, never exceptions (org never crashes on a tool).
    assert run_tool("nope", {})["status"] == "failed"

    def _boom(a):
        raise RuntimeError("browser died")
    _TOOLS["boom"] = _boom
    assert run_tool("boom", {})["status"] == "failed" and "browser died" in run_tool("boom", {})["result"]["error"]
    del _TOOLS["boom"]

    print("tools selftest: PASS (qa_explore, dev_fix, research, finance_report, knowledge_work, data_query, "
          "legal_scan, connector_ingest, produce_artifact, design_asset — read + OUTPUT work; errors fail-soft)")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
