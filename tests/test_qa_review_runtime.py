import os
import json
import shutil
import sys
import tempfile
import threading
import uuid
from contextlib import nullcontext
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "qa"))
sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))


def test_failed_video_encode_removes_partial_file(monkeypatch, tmp_path):
    import artifacts

    source = tmp_path / "source.webm"
    output = tmp_path / "partial.mp4"
    source.write_bytes(b"fixture-source")
    monkeypatch.setattr(artifacts.shutil, "which", lambda _name: "/usr/bin/fake")
    monkeypatch.setattr(artifacts, "_media_admission", lambda _label: nullcontext(object()))

    def fail_after_partial(args, _admission, **_kwargs):
        Path(args[-1]).write_bytes(b"unfinished-moov-less-file")
        raise TimeoutError("encoder deadline")

    monkeypatch.setattr(artifacts, "_run_media_owned", fail_after_partial)
    assert artifacts.webm_to_mp4(source, output) is None
    assert not output.exists(), "a timed-out encoder must not leave an artifact that reports as playable"


def test_terminal_review_cancels_only_its_exact_focused_actor():
    import jobrunner

    exact_event, sibling_event = threading.Event(), threading.Event()
    original = dict(jobrunner._JOBS)
    try:
        jobrunner._JOBS.clear()
        jobrunner._JOBS.update({
            "exact": {"state": "running", "job": {
                "run_id": 7, "tenant": "tenant-a", "actor_id": 41},
                "cancel_event": exact_event},
            "sibling": {"state": "running", "job": {
                "run_id": 7, "tenant": "tenant-a", "actor_id": 42},
                "cancel_event": sibling_event},
        })

        assert jobrunner.cancel_actor_job(
            7, "tenant-a", 41, reason="review resolved") == 1

        assert exact_event.is_set() is True and sibling_event.is_set() is False
        assert jobrunner._JOBS["exact"]["terminal_cancel"] == "review resolved"
        assert "terminal_cancel" not in jobrunner._JOBS["sibling"]
    finally:
        jobrunner._JOBS.clear()
        jobrunner._JOBS.update(original)


def test_audit_gap_fingerprint_is_semantic_and_order_independent():
    import qa_agentic

    first = {"skipped_flows": ["  Keyboard   activation ", "reload persistence"],
             "unbacked_claims": ["COUNT increments"], "evidence_gaps": []}
    replay = {"skipped_flows": ["reload persistence", "keyboard activation"],
              "unbacked_claims": ["count INCREMENTS", "count increments"],
              "evidence_gaps": []}
    changed = dict(replay, evidence_gaps=["screen-reader announcement"])
    assert qa_agentic._audit_gap_fingerprint(first) == qa_agentic._audit_gap_fingerprint(replay)
    assert qa_agentic._audit_gap_fingerprint(changed) != qa_agentic._audit_gap_fingerprint(first)
    assert qa_agentic._audit_gap_fingerprint({}) is None
    assert qa_agentic._should_run_audit(True, False, "done") is True
    assert qa_agentic._should_run_audit(True, False, "halted") is False
    assert qa_agentic._should_run_audit(True, True, "done") is False
    assert qa_agentic._should_run_audit(True, False, "done", candidate_clean=False) is False
    assert qa_agentic._should_file_governed_findings(True, False, "done") is True
    assert qa_agentic._should_file_governed_findings(True, True, "halted") is False
    assert qa_agentic._should_file_governed_findings(True, False, "running") is False
    assert qa_agentic._should_file_governed_findings(False, False, "done") is False
    # The jury changes report.clean to false when it rejects. Expansion must use the clean candidate that
    # existed immediately before that downgrade, or a valid rejection silently ends the QA campaign.
    assert qa_agentic._should_expand_audit(True, True, "done", False, []) is True
    assert qa_agentic._should_expand_audit(True, False, "done", False, []) is False
    assert qa_agentic._should_expand_audit(True, True, "halted", False, []) is False
    assert qa_agentic._should_expand_audit(True, True, "done", True, []) is False
    assert qa_agentic._should_expand_audit(True, True, "done", False, [{"title": "real bug"}]) is False

    report = {"passed": True, "clean": True, "verdict": "ALL CLEAR", "summary": "ALL CLEAR"}
    qa_agentic._apply_audit_judgment(
        report, {"passed_audit": False, "score": 3, "summary": "missing repeated-use proof"})
    assert report["passed"] is False and report["clean"] is False
    assert report["summary"] == report["verdict"]
    assert "AUDIT REJECTED" in report["summary"] and "ALL CLEAR" not in report["summary"]


def test_governed_finding_key_prefers_immutable_finding_id():
    import qa_agentic

    assert qa_agentic._governed_finding_key(
        {"finding_id": "qaf-stable", "title": "old"}) == "qaf-stable"
    first = qa_agentic._governed_finding_key({
        "story": "US-1", "title": "  SAME   observation ", "detail": "Exact detail"})
    replay = qa_agentic._governed_finding_key({
        "story": "US-1", "title": "same observation", "detail": "exact   detail"})
    assert first == replay and first.startswith("semantic:")


def test_audit_expansion_carries_only_clean_stories_with_inspectable_results():
    import qa_agentic

    stories = [{"id": "old"}, {"id": "new"}, {"id": "missing-dossier"}]
    clean_result = {"story": "old", "stop_reason": "coverage-complete", "bugs": 0,
                    "coverage": [{"aspect": "flow", "covered": True}],
                    "steps_detail": [{"action": "click", "verdict": "match",
                                      "covers": ["flow"]}]}
    status, results = qa_agentic._clean_audit_continuity(
        stories,
        {"old": "clean", "new": "incomplete", "missing-dossier": "clean"},
        {"old": clean_result,
         "new": {"story": "new"}},
        prior_status={"old": "blocking", "foreign": "clean"},
        prior_results={"old": {"story": "stale"}, "foreign": {"story": "foreign"}},
    )
    assert status == {"old": "clean"}
    assert results == {"old": clean_result}


def test_audit_expansion_staffs_only_new_gap_stories_from_carried_clean_status():
    import runtime

    actor = {
        "name": "qa-coordinator",
        "memory": {
            "story_status": {"old": "clean"},
            "context": {
                "stories": [
                    {"id": "old", "title": "Already proven"},
                    {"id": "gap", "title": "New jury gap"},
                ],
                "story_batch_size": 12,
                "target_url": "http://app",
                "vision": "vision",
                "repo": "/tmp/repo",
            },
        },
    }
    specs = runtime._coordinator_specs(None, actor, "QA", "qa-coordinator")
    assert [spec["tool_args"]["story"]["id"] for spec in specs] == ["gap"]


def test_paid_qa_director_output_becomes_executable_regression_stories(monkeypatch, tmp_path):
    import factory
    import qa_agentic

    response = [{"id": "ignored-model-id", "title": "Keyboard and reload proof",
                 "goal": "Prove non-pointer use and state durability",
                 "steps": ["Focus Increment and press Enter", "Reload the page"],
                 "expected": "The count increments from the keyboard and remains coherent after reload.",
                 "coverage": ["keyboard activation", "focus visibility", "reload behavior"]}]
    calls = []

    def fake_agent(role, repo, prompt, **kwargs):
        calls.append((role, repo, prompt, kwargs))
        return {"out_full": json.dumps(response)}

    monkeypatch.setattr(factory, "agent", fake_agent)
    audit = {"passed_audit": False,
             "skipped_flows": ["Keyboard activation was never exercised", "Reload was skipped"],
             "unbacked_claims": [], "evidence_gaps": ["No focus evidence"]}
    planned = qa_agentic._audit_gap_stories(
        audit, "An accessible counter", [{"id": "US-1", "title": "Increment"}],
        repo=str(tmp_path), iteration=2)

    assert len(planned) == 1 and planned[0]["id"].startswith("AUDIT-2-")
    assert planned[0]["category"] == "audit-regression"
    assert planned[0]["steps"] == response[0]["steps"]
    assert planned[0]["coverage"] == response[0]["coverage"]
    assert calls and calls[0][0] == "qa-security" and calls[0][3]["light"] is True
    assert "TEST gaps" in calls[0][2] and "change product code" in calls[0][2]


def test_aggregate_dossier_materializes_visuals_and_before_after_facts(tmp_path, monkeypatch):
    import qa_agentic
    import review
    import tools
    import artifacts

    monkeypatch.setattr(artifacts, "probe_media", lambda path: {
        "path": str(path), "bytes": Path(path).stat().st_size, "duration_s": 1.0,
        "format": "fixture", "streams": [{"codec_name": "fixture"}],
    })

    story_artifacts = tmp_path / "story-artifacts"
    (story_artifacts / "screenshots").mkdir(parents=True)
    before = story_artifacts / "screenshots" / "before.png"
    after = story_artifacts / "screenshots" / "after.png"
    video = story_artifacts / "story.mp4"
    before.write_bytes(b"png-before"); after.write_bytes(b"png-after"); video.write_bytes(b"mp4")
    dossier = tmp_path / "dossier"
    mapping = qa_agentic._materialize_story_artifacts({"US-1": {
        "artifact_dir": str(story_artifacts), "video": str(video)}}, dossier)
    assert Path(mapping[str(before)]).is_file() and Path(mapping[str(after)]).is_file()
    assert Path(mapping[str(video)]).is_file()
    assert len(list((dossier / "screenshots").glob("*.png"))) == 2
    assert len(list((dossier / "videos").glob("*.mp4"))) == 1

    actual = tools._step_actual({
        "state": {"url": "http://app", "bodyText": "Current count 0 Increment",
                  "perception": {"firstAt": 10}},
        "actual": {"url": "http://app", "bodyText": "Current count 1 Increment",
                   "perception": {"firstAt": 10}, "console_errors": [],
                   "accessibilityRegions": [{"tag": "output", "role": "status",
                                               "ariaLive": "polite", "text": "1"}],
                   "accessibilityTree": "- main:\n  - status: 1",
                   "accessibilityEvents": [{"mutationType": "characterData", "role": "status",
                                             "ariaLive": "polite", "text": "1"}],
                   "accessibilityPlatformEvents": [{"source": "Accessibility.getFullAXTree:changed",
                                                      "role": "status", "name": "1", "live": "polite"}],
                   "actualAssistiveTechnologyAvailable": True,
                   "actualAssistiveTechnologyEvents": [
                       {"ts": "11:39:53.584710", "utterance": "Current count 1",
                        "source": "orca-at-spi"}],
                   "recent_requests": [{"method": "GET", "url": "http://app/api", "status": None,
                                        "failed": "net::ERR_FAILED"}],
                   "activeElement": {"tag": "button", "text": "Increment",
                                     "focusVisible": True,
                                     "focusStyle": {"outline": "solid 3px rgb(0, 0, 0)"}}},
        "targeting": {"driver_ok": True, "effect_registered": True, "action_kind": "burst",
                      "burst": {"burst": True, "count": 5, "interval_ms": 25,
                                "timestamps": [10, 35, 60, 85, 110], "elapsed_ms": 103},
                      "targeted_label": "Increment"}})
    assert "Current count 0" in actual and "Current count 1" in actual
    assert "full_reload=False" in actual and "target='Increment'" in actual
    assert '"role": "status"' in actual and "status: 1" in actual
    assert "live_events=1:" in actual and '"mutationType":"characterData"' in actual
    assert "ax_events=1:" in actual
    assert "Accessibility.getFullAXTree:changed" in actual
    assert "real_at=True" in actual and "Current count 1" in actual
    assert "network_failures=" in actual and "ERR_FAILED" in actual
    assert '"action_kind":"burst"' in actual and '"elapsed_ms":103' in actual
    assert "active_after=" in actual and '"focusVisible":true' in actual
    assert 'input_receipt={"pointer_types":[]' in actual
    assert '"burst_count":5' in actual and '"burst_elapsed_ms":103' in actual
    assert 'input_events={"burst_timestamps":[10,35,60,85,110]}' in actual
    # All decision-critical facts must survive review.dossier's bounded ACTUAL cell clip.
    critical = actual[:1200]
    assert '"burst_timestamps":[10,35,60,85,110]' in critical
    assert "real_at_events=1:" in critical and "Current count 1" in critical
    assert "network_failures=" in critical and "live_events=1:" in critical and "ax_events=1:" in critical

    recorder = tools._recorder_step_details(
        [{"state": {"url": "http://app", "statusText": "Current count 0", "bodyText": "Count 0"},
          "actual": {"url": "http://app", "statusText": "Current count 1", "bodyText": "Count 1",
                     "screenshot": str(after), "console_errors": [],
                     "recent_requests": [{"ts": 15, "method": "GET", "url": "http://app", "status": 200}]},
          "action_started_at": 14.0, "action_completed_at": 14.1, "recorded_at": 16.0,
          "act_result": {"clicked": True}, "verdict": {"matches_expected": True},
          "demonstrated": ["count increments"]}],
        [{"aspect": "start trace", "stage": "start", "timestamp": 10,
          "artifact_dir": str(story_artifacts)},
         {"aspect": "inspect complete record", "stage": "end", "timestamp": 20,
          "capture_started_at": 10, "artifact_dir": str(story_artifacts)}],
        video=video, recorder_trace={"capture_started_at": 10, "capture_ended_at": 20,
                                     "clear_receipt": {"cleared_at": 10},
                                     "console_errors": [],
                                     "network_requests": [{"ts": 15, "method": "GET",
                                                           "url": "http://app", "status": 200}]})
    assert [row["action"] for row in recorder] == ["recorder start", "recorder end"]
    assert recorder[1]["covers"] == ["inspect complete record"]
    assert '"timestamp": 20' in recorder[1]["actual"]
    assert '"capture_ended_at": 20' in recorder[1]["actual"]
    assert str(video) in recorder[1]["actual"]
    inspection = story_artifacts / "recorder-inspection.json"
    assert inspection.is_file()
    inspection_data = json.loads(inspection.read_text())
    assert inspection_data["network_requests"][0]["status"] == 200
    assert inspection_data["actions"][0]["observed_before"]["status_text"] == "Current count 0"
    assert inspection_data["actions"][0]["observed_after"]["status_text"] == "Current count 1"
    (story_artifacts / "recorder-inspection-review.json").write_text(json.dumps({
        "accepted": True, "source_sha256": "abc", "checked": {"chronology": True}, "issues": [],
        "summary": "Independent inspection accepted the timestamped trace.",
        "source_summary": {"capture_started_at": 10, "capture_ended_at": 20,
                           "actions": 1, "requests": 1, "console_errors": 0,
                           "media": {"decode_verified": True}},
    }))
    qa_agentic._materialize_story_artifacts({"US-1": {
        "artifact_dir": str(story_artifacts), "video": str(video)}}, dossier)

    (dossier / "run-input.json").write_text(json.dumps({"vision": "accessible counter"}))
    (dossier / "coverage.json").write_text("[]")
    (dossier / "run-final.json").write_text(json.dumps({"stories": [{
        "id": "US-1", "title": "Counter", "status": "passed", "steps": recorder,
        "video": mapping[str(video)], "artifact_evidence": [{"stage": "start"}, {"stage": "end"}],
    }]}))
    rendered = review.dossier(dossier)["md"]
    assert "Recorded story videos:** 1" in rendered
    assert f"Story video: {mapping[str(video)]}" in rendered
    assert "Explicit recorder-contract records: 2" in rendered
    assert "Immutable recorder inspections" in rendered
    assert "capture_ended_at" in rendered and "Independent inspection accepted" in rendered


def test_missing_qa_capability_is_one_idempotent_internal_management_case():
    import runtime

    memory = {}
    capabilities = [{"capability": "actual-assistive-technology",
                     "aspects": ["updated announcement", "initial announcement"],
                     "reason": "No real AT driver is attached."}]
    first = runtime._qa_record_capability_review(memory, "US-AT", capabilities, case_id="mc-at")
    replay = runtime._qa_record_capability_review(memory, "US-AT", list(reversed(capabilities)),
                                                  case_id="mc-at")
    assert first["review_id"] == replay["review_id"]
    assert len(memory["internal_reviews"]) == 1
    state = memory["internal_review_states"][first["review_id"]]
    assert state["status"] == "manager_attention" and state["case_id"] == "mc-at"
    assert runtime._qa_gapfill_candidates(
        {"US-AT": "internal_review"}, ["US-AT"], {}, 3) == []


def test_grounded_actionable_receipt_stops_duplicate_reproduction_not_incomplete_work():
    import runtime

    grounded_defect = {"bugs": 1, "steps_detail": [{
        "actual": "publish was allowed", "verdict": "mismatch",
        "covers": ["reject pasted credential"], "coverage_grounded": True,
        "bug": {"title": "credential was published"},
    }]}
    ungrounded_allegation = {"bugs": 1, "steps_detail": [{
        "actual": "model assertion", "verdict": "mismatch",
        "covers": ["reject pasted credential"], "coverage_grounded": False,
        "bug": {"title": "credential was published"},
    }]}
    infrastructure_incomplete = {"bugs": 0, "steps_detail": [],
                                 "infrastructure_error": "browser unavailable"}

    assert runtime._qa_has_grounded_actionable_browser_receipt(grounded_defect) is True
    assert runtime._qa_has_grounded_actionable_browser_receipt(ungrounded_allegation) is False
    assert runtime._qa_has_grounded_actionable_browser_receipt(infrastructure_incomplete) is False

    marker = runtime._qa_grounded_actionable_marker(grounded_defect, "rev-before-fix")
    assert marker["product_revision"] == "rev-before-fix"
    assert len(marker["receipt_digest"]) == 64
    assert runtime._qa_grounded_actionable_marker_is_current(marker, "rev-before-fix") is True
    assert runtime._qa_grounded_actionable_marker_is_current(marker, "rev-after-fix") is False
    assert runtime._qa_grounded_actionable_marker(ungrounded_allegation, "rev-before-fix") is None
    assert runtime._qa_grounded_actionable_marker(grounded_defect, None) is None


def test_related_review_reuses_only_live_same_observation_browser_verification():
    import runtime

    reviews = [
        {"review_id": "review-a", "story": "US-10", "finding": {
            "finding_id": "qaf-a", "story": "US-10",
            "title": "Secret token remains visible after reload in governed public content",
            "detail": "The published API token remains visible after a settled browser reload."}},
        {"review_id": "review-b", "story": "US-10", "finding": {
            "finding_id": "qaf-b", "story": "US-10",
            "title": "Governed public content still shows secret token after reload",
            "detail": "After a settled reload the published API token remains visible publicly."}},
        {"review_id": "review-c", "story": "US-10", "finding": {
            "finding_id": "qaf-c", "story": "US-10",
            "title": "Private claim blocker names the wrong record",
            "detail": "The staff blocker targets another claim rather than the private claim."}},
    ]
    children = {
        41: {"role": "qa-explorer", "status": "blocked", "memory": {"context": {"tool_args": {
            "_qa_review_id": "review-a"}}}},
        42: {"role": "qa-explorer", "status": "done", "memory": {"context": {"tool_args": {
            "_qa_review_id": "review-b"}}}},
    }

    assert runtime._qa_verification_peer(
        children, reviews, reviews[1], exclude_review_id="review-b") == {
            "actor_id": 41, "review_id": "review-a"}
    assert runtime._qa_verification_peer(
        children, reviews, reviews[2], exclude_review_id="review-c") is None


def test_external_authority_correlation_is_stable_per_generation_and_rotates_after_answer():
    import runtime

    first = runtime._qa_authority_correlation("tenant-a", "review-a", 1)
    replay = runtime._qa_authority_correlation("tenant-a", "review-a", 1)
    resumed = runtime._qa_authority_correlation("tenant-a", "review-a", 2)
    assert first == replay == "qa-dispute:tenant-a:review-a:g1"
    assert resumed == "qa-dispute:tenant-a:review-a:g2" and resumed != first


def test_independent_recorder_inspector_requires_timestamped_trusted_pointer_and_persists_receipt(
        monkeypatch, tmp_path):
    import qa_explorer
    import tools

    inspection = tmp_path / "recorder-inspection.json"
    inspection.write_text(json.dumps({
        "capture_started_at": 100.0,
        "capture_ended_at": 120.0,
        "clear_receipt": {"cleared_at": 100.0, "console_before": 1, "requests_before": 2},
        "console_errors": [],
        "network_requests": [
            {"ts": 104.0, "method": "GET", "url": "http://app/away", "status": 200},
        ],
        "actions": [{
            "step": 1, "recorded_at": 110.0, "action_started_at": 109.7,
            "action_completed_at": 110.0, "action": {"cmd": "click"},
            "url": "http://app", "targeting": {"pointer_evidence": [
                {"ts": 109.8, "type": "pointerdown", "isTrusted": True,
                 "pointerType": "mouse", "clientX": 20, "clientY": 30},
                {"ts": 109.9, "type": "pointerup", "isTrusted": True,
                 "pointerType": "mouse", "clientX": 20, "clientY": 30},
                {"ts": 110.0, "type": "click", "isTrusted": True,
                 "pointerType": "mouse", "clientX": 20, "clientY": 30},
            ]},
        }],
        "screenshots": [str(tmp_path / "after.png")],
        "video": {"decode_verified": True, "bytes": 2048, "duration_s": 21.0,
                  "path": str(tmp_path / "story.mp4")},
    }))
    rows = [{"action": "recorder end", "actual": json.dumps({
        "inspection_artifact": str(inspection),
    })}]
    calls = []

    def reviewer(role, repo, prompt, light=False):
        calls.append((role, repo, prompt, light))
        return {"rc": 0, "model": "gpt-5.6-sol", "engine": "codex", "out_full": json.dumps({
            "accepted": True,
            "checked": {key: True for key in tools._RECORDER_REVIEW_CHECKS},
            "issues": [], "summary": "The immutable trace proves the requested pointer journey.",
        })}

    monkeypatch.setattr(qa_explorer, "_call_agent", reviewer)
    review = tools._independent_recorder_review(
        rows, story={"steps": ["activate with the pointer"]}, repo=str(tmp_path))

    assert review["accepted"] is True and review["mechanical_checks"]["pointer"] is True
    assert calls and calls[0][0] == "reviewer" and "SOURCE_SHA256" in calls[0][2]
    receipt = tmp_path / "recorder-inspection-review.json"
    assert receipt.is_file() and json.loads(receipt.read_text())["accepted"] is True
    assert review["review_sha256"]


def test_independent_recorder_inspector_cannot_accept_missing_pointer_receipts(monkeypatch, tmp_path):
    import qa_explorer
    import tools

    inspection = tmp_path / "recorder-inspection.json"
    inspection.write_text(json.dumps({
        "capture_started_at": 100.0, "capture_ended_at": 110.0,
        "clear_receipt": {"cleared_at": 100.0}, "console_errors": [],
        "network_requests": [{"ts": 101.0, "method": "GET", "url": "http://app", "status": 200}],
        "actions": [{"step": 1, "recorded_at": 105.0, "action_started_at": 104.8,
                     "action_completed_at": 105.0, "action": {"cmd": "click"},
                     "targeting": {"pointer_evidence": []}}],
        "video": {"decode_verified": True, "bytes": 1024, "duration_s": 11.0},
    }))
    rows = [{"action": "recorder end", "actual": json.dumps({
        "inspection_artifact": str(inspection),
    })}]
    monkeypatch.setattr(qa_explorer, "_call_agent", lambda *_a, **_k: {
        "rc": 0, "out_full": json.dumps({"accepted": True,
            "checked": {key: True for key in tools._RECORDER_REVIEW_CHECKS}, "issues": []})})

    review = tools._independent_recorder_review(
        rows, story={"steps": ["activate with the pointer"]}, repo=str(tmp_path))

    assert review["accepted"] is False and review["mechanical_checks"]["pointer"] is False
    assert any("mechanical evidence floor failed: pointer" in issue for issue in review["issues"])


def test_disputed_finding_is_managed_then_coverage_completes_without_a_human_gate(monkeypatch, tmp_path):
    import jobrunner
    import qa_agentic
    import store
    import tools

    tenant = f"qa-review-runtime-{uuid.uuid4().hex}"
    product = f"qa-review-runtime-{uuid.uuid4().hex[:10]}"
    calls = {"qa_explore": 0, "dev_fix": 0, "qa_review": 0}

    def fake_tool(name, args):
        calls[name] += 1
        story = (args.get("story") or {}).get("id")
        if name == "qa_explore":
            if calls[name] == 1:
                finding = {"kind": "bug", "finding_id": "qaf-original", "story": story,
                           "title": "disputed behavior", "detail": "observed mismatch",
                           "severity": "high", "blocking": True,
                           "evidence_provenance": {"manifest_path": str(tmp_path / "sealed.json"),
                                                   "manifest_sha256": "a" * 64}}
                return {"status": "done", "findings": [finding],
                        "result": {"story": story, "stop_reason": "blocking-bug", "bugs": 1}}
            return {"status": "done", "findings": [],
                    "result": {"story": story, "stop_reason": "coverage-complete", "bugs": 0,
                               "coverage": [{"aspect": "contract", "covered": True}],
                               "steps_detail": [{"verdict": "match", "covers": ["contract"]}]}}
        if name == "dev_fix":
            return {"status": "internal_review", "findings": [], "result": {
                "fixed": False, "internal_review_required": True,
                "triage": {"reviews": [{"verdict": "uncertain"}]},
                "verdict": {"reason": "independent reviewers disagreed"}}}
        if name == "qa_review":
            record = args["internal_review"]
            return {"status": "done", "findings": [], "result": {
                "review_id": record["review_id"], "case_id": "qad-test", "status": "resolved",
                "disposition": "verified_false_positive", "confidence": 0.96,
                "rationale": "sealed contract and independent review reject the observation"}}
        raise AssertionError(name)

    monkeypatch.setattr(tools, "run_tool", fake_tool)
    monkeypatch.setattr(jobrunner, "_default_run_tool", lambda: fake_tool)
    monkeypatch.setattr(qa_agentic.campaign_checkpoint, "repo_revision", lambda _repo: "revision-a")
    monkeypatch.setenv("AOS_QA_EVIDENCE_DIR", tempfile.mkdtemp(prefix="qa-review-runtime-evidence-"))
    monkeypatch.setenv("AOS_QA_AUDIT_GATE", "0")
    monkeypatch.setenv("AOS_TOOL_MIN_LAUNCH_RUNWAY_S", "1")
    out = None
    try:
        out = qa_agentic.run_agentic_qa(
            "http://app", "vision", product=product, repo=str(tmp_path), tenant=tenant,
            stories=[{"id": "US-1", "title": "One story"}], workers=2,
            drive_budget_s=30, stall_s=0.3, file_findings=False)
        assert out["status"] == "done"
        assert out["verdict"]["passed"] is True
        assert out["report"]["passed"] is True and out["report"]["open_bugs"] == 0
        assert calls == {"qa_explore": 2, "dev_fix": 1, "qa_review": 1}
        roles = [a["role"] for a in store.actors(out["run_id"], tenant)]
        assert roles.count("qa-evidence-reviewer") == 1
        assert out["findings"][0]["resolved"] is True
    finally:
        if out:
            qa_agentic._cleanup(out["run_id"], tenant)
            qrid = (out.get("report") or {}).get("qa_run_id")
            if qrid:
                with qa_agentic.connection() as c, c.cursor() as cur:
                    cur.execute("DELETE FROM qa_runs WHERE id=%s", (qrid,))
        shutil.rmtree(os.environ.get("AOS_QA_EVIDENCE_DIR", ""), ignore_errors=True)


def test_dev_triage_false_positive_is_durably_resolved_then_retested(monkeypatch, tmp_path):
    """A unanimous no-mutation verdict must survive the dev-manager handoff as structured state.

    The paid canary proved the old free-form aggregate erased this field: the re-test passed but the original
    observation remained open, so the skeptical jury could not schedule its next evidence iteration.
    """
    import jobrunner
    import qa_agentic
    import store
    import tools

    tenant = f"qa-false-positive-{uuid.uuid4().hex}"
    product = f"qa-false-positive-{uuid.uuid4().hex[:10]}"
    calls = {"qa_explore": 0, "dev_fix": 0}
    qa_args = []
    resume_state = None

    def fake_tool(name, args):
        if name in calls:
            calls[name] += 1
        story = (args.get("story") or {}).get("id")
        if name == "qa_explore":
            qa_args.append(dict(args))
            if calls[name] == 1:
                finding = {"kind": "bug", "finding_id": "qaf-dismissed", "story": story,
                           "title": "test harness expected a nonexistent second route",
                           "detail": "same single-page app appeared at its canonical root",
                           "severity": "high", "blocking": True}
                return {"status": "done", "findings": [finding],
                        "result": {"story": story, "stop_reason": "blocking-bug", "bugs": 1,
                                   "resume_state_path": str(resume_state),
                                   "coverage": [
                                       {"aspect": "canonical route is reachable", "covered": True},
                                       {"aspect": "true Back navigation", "covered": False}],
                                   "steps_detail": [{"verdict": "match",
                                                     "covers": ["canonical route is reachable"]}]}}
            return {"status": "done", "findings": [],
                    "result": {"story": story, "stop_reason": "coverage-complete", "bugs": 0,
                               "coverage": [{"aspect": "true Back navigation", "covered": True}],
                               "steps_detail": [{"verdict": "match",
                                                 "covers": ["true Back navigation"]}]}}
        if name == "dev_fix":
            return {"status": "done", "findings": [], "result": {
                "fixed": True, "resolved": True, "resolved_without_mutation": True, "files": [],
                "triage": {"reviews": [{"reviewer": 1, "verdict": "false_positive"},
                                        {"reviewer": 2, "verdict": "false_positive"}]},
                "verdict": {"fixed": True, "confidence": 0.99,
                            "resolved_without_mutation": True,
                            "reason": "two independent reviewers proved the route was not in contract"}}}
        raise AssertionError(name)

    monkeypatch.setattr(tools, "run_tool", fake_tool)
    monkeypatch.setattr(jobrunner, "_default_run_tool", lambda: fake_tool)
    monkeypatch.setattr(qa_agentic.campaign_checkpoint, "repo_revision", lambda _repo: "revision-a")
    evidence = tempfile.mkdtemp(prefix="qa-false-positive-evidence-")
    resume_state = Path(evidence) / "false-positive-browser-state.json"
    resume_state.write_text("{}")
    monkeypatch.setenv("AOS_QA_EVIDENCE_DIR", evidence)
    monkeypatch.setenv("AOS_QA_AUDIT_GATE", "0")
    monkeypatch.setenv("AOS_TOOL_MIN_LAUNCH_RUNWAY_S", "1")
    out = None
    try:
        out = qa_agentic.run_agentic_qa(
            "http://app", "vision", product=product, repo=str(tmp_path), tenant=tenant,
            stories=[{"id": "US-1", "title": "One story"}], workers=2,
            drive_budget_s=30, stall_s=0.3, file_findings=False)
        assert out["status"] == "done" and out["report"]["passed"] is True, json.dumps(out, default=str, indent=2)
        assert out["report"]["open_bugs"] == 0
        assert calls == {"qa_explore": 2, "dev_fix": 1}
        # A false-positive verdict resolves the semantic observation but cannot rewind the action that
        # produced its terminal browser state. The skeptical retest starts a coherent journey so a consumed
        # queue item/form/navigation transition cannot manufacture a second finding.
        assert "resume_state_path" not in qa_args[1]
        assert "resume_covered" not in qa_args[1]
        assert "_qa_recovery_source_revision" not in qa_args[1]
        assert out["findings"][0]["resolved"] is True
        coord = next(a for a in store.actors(out["run_id"], tenant)
                     if a.get("role") == "qa-coordinator")
        resolutions = (coord.get("memory") or {}).get("finding_resolutions") or []
        assert any(r.get("finding_id") == "qaf-dismissed"
                   and r.get("disposition") == "verified_false_positive" for r in resolutions)
    finally:
        if out:
            qa_agentic._cleanup(out["run_id"], tenant)
            qrid = (out.get("report") or {}).get("qa_run_id")
            if qrid:
                with qa_agentic.connection() as c, c.cursor() as cur:
                    cur.execute("DELETE FROM qa_runs WHERE id=%s", (qrid,))
        shutil.rmtree(evidence, ignore_errors=True)


def test_legacy_unsealed_dispute_gets_fresh_evidence_then_exactly_one_fenced_repair(monkeypatch, tmp_path):
    import jobrunner
    import qa_agentic
    import store
    import tools

    tenant = f"qa-review-legacy-{uuid.uuid4().hex}"
    product = f"qa-review-legacy-{uuid.uuid4().hex[:10]}"
    calls = {"qa_explore": 0, "dev_fix": 0, "qa_review": 0}

    def finding(story, *, sealed):
        item = {"kind": "bug", "finding_id": f"qaf-{'sealed' if sealed else 'legacy'}",
                "story": story, "title": "legacy disputed behavior", "detail": "mismatch",
                "severity": "high", "blocking": True}
        if sealed:
            item["evidence_provenance"] = {"manifest_path": str(tmp_path / "fresh-sealed.json"),
                                           "manifest_sha256": "b" * 64}
        return item

    def fake_tool(name, args):
        calls[name] += 1
        story = (args.get("story") or {}).get("id")
        if name == "qa_explore":
            if calls[name] <= 2:
                bug = finding(story, sealed=calls[name] == 2)
                return {"status": "done", "findings": [bug],
                        "result": {"story": story, "stop_reason": "blocking-bug", "bugs": 1}}
            return {"status": "done", "findings": [],
                    "result": {"story": story, "stop_reason": "coverage-complete", "bugs": 0,
                               "coverage": [{"aspect": "complete story", "covered": True}],
                               "steps_detail": [{"verdict": "match", "covers": ["complete story"]}]}}
        if name == "dev_fix":
            if calls[name] == 1:
                return {"status": "internal_review", "findings": [], "result": {
                    "fixed": False, "internal_review_required": True,
                    "triage": {"reviews": [{"verdict": "uncertain"}]},
                    "verdict": {"reason": "legacy observation has no sealed boundary"}}}
            return {"status": "done", "findings": [], "result": {"fixed": True, "files": ["app.js"]}}
        if name == "qa_review":
            record = args["internal_review"]
            if calls[name] == 1:
                return {"status": "done", "findings": [], "result": {
                    "review_id": record["review_id"], "status": "fresh_evidence_required",
                    "disposition": None, "fresh_evidence_required": True}}
            return {"status": "done", "findings": [], "result": {
                "review_id": record["review_id"], "case_id": "qad-fresh", "status": "resolved",
                "disposition": "confirmed_defect", "confidence": 0.97,
                "rationale": "fresh sealed reproduction confirms the defect"}}
        raise AssertionError(name)

    monkeypatch.setattr(tools, "run_tool", fake_tool)
    monkeypatch.setattr(jobrunner, "_default_run_tool", lambda: fake_tool)
    monkeypatch.setattr(qa_agentic.campaign_checkpoint, "repo_revision", lambda _repo: "revision-a")
    evidence = tempfile.mkdtemp(prefix="qa-review-legacy-evidence-")
    monkeypatch.setenv("AOS_QA_EVIDENCE_DIR", evidence)
    monkeypatch.setenv("AOS_QA_AUDIT_GATE", "0")
    monkeypatch.setenv("AOS_TOOL_MIN_LAUNCH_RUNWAY_S", "1")
    out = None
    try:
        out = qa_agentic.run_agentic_qa(
            "http://app", "vision", product=product, repo=str(tmp_path), tenant=tenant,
            stories=[{"id": "US-1", "title": "One story"}], workers=2,
            drive_budget_s=30, stall_s=0.3, file_findings=False)
        assert out["status"] == "done" and out["report"]["passed"] is True
        assert calls == {"qa_explore": 3, "dev_fix": 2, "qa_review": 2}
        roles = [a["role"] for a in store.actors(out["run_id"], tenant)]
        assert roles.count("qa-evidence-reviewer") == 2
        assert roles.count("dev-fixer") == 2, "confirmed evidence must create exactly one new repair handoff"
        assert out["findings"][0]["fixed"] is True
    finally:
        if out:
            qa_agentic._cleanup(out["run_id"], tenant)
            qrid = (out.get("report") or {}).get("qa_run_id")
            if qrid:
                with qa_agentic.connection() as c, c.cursor() as cur:
                    cur.execute("DELETE FROM qa_runs WHERE id=%s", (qrid,))
        shutil.rmtree(evidence, ignore_errors=True)
