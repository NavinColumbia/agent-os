import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


QA = Path(__file__).resolve().parents[1] / "scripts" / "qa"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ORCHESTRA = SCRIPTS / "orchestra"
for item in (str(SCRIPTS), str(ORCHESTRA)):
    if item not in sys.path:
        sys.path.insert(0, item)
if str(QA) not in sys.path:
    sys.path.insert(0, str(QA))

import campaign_checkpoint as campaign
import qa_agentic
import runtime
from qa import dev_loop


def _stories(count):
    return [{"id": f"US{i:03d}", "title": f"Journey {i}", "steps": [f"do {i}"],
             "expected_outcome": f"result {i}"} for i in range(1, count + 1)]


def test_signature_covers_deferred_tail_but_ignores_harmless_reordering(tmp_path):
    stories = _stories(14)
    kwargs = {"tenant": "tenant-a", "product": "product-a", "target_url": "http://app",
              "vision": "complete vision", "repo": tmp_path, "thread_id": 44}
    signature = campaign.campaign_signature(stories=stories, **kwargs)

    assert campaign.campaign_signature(stories=list(reversed(stories)), **kwargs) == signature
    changed_tail = [dict(item) for item in stories]
    changed_tail[-1]["steps"] = ["different tail contract"]
    assert campaign.campaign_signature(stories=changed_tail, **kwargs) != signature


def test_story_windows_advance_through_every_story_without_repeating_prefix():
    stories = _stories(25)
    first = campaign.story_window(stories, 12)
    second = campaign.story_window(stories, 12, first["active_story_ids"])
    third = campaign.story_window(
        stories, 12, first["active_story_ids"] + second["active_story_ids"])

    assert first["active_story_ids"] == [f"US{i:03d}" for i in range(1, 13)]
    assert second["active_story_ids"] == [f"US{i:03d}" for i in range(13, 25)]
    assert third["active_story_ids"] == ["US025"]
    assert not (set(first["active_story_ids"]) & set(second["active_story_ids"]))
    assert third["deferred_story_ids"] == []


def test_story_identity_collisions_fail_closed():
    with pytest.raises(ValueError, match="duplicate QA story identity"):
        campaign.story_window([{"id": "US1"}, {"id": "US1", "title": "other"}], 1)
    with pytest.raises(ValueError, match="non-empty id or title"):
        campaign.story_window([{"steps": ["unaddressable"]}], 1)


def test_agentic_report_exposes_campaign_identity_for_outer_shift_accounting():
    source = (QA / "qa_agentic.py").read_text()
    assert '\"qa_campaign_run_id\": rid' in source
    assert "campaign_checkpoint.EVIDENCE_POLICY_REVISION" in source
    assert '\"qa_campaign_key\"' in source


def test_v3_checkpoint_is_full_campaign_tenant_fenced_and_atomic(tmp_path):
    stories = _stories(14)
    signature = campaign.campaign_signature(
        tenant="tenant-a", product="product-a", target_url="http://app", vision="vision",
        repo=tmp_path, stories=stories, thread_id=44)
    document = campaign.checkpoint_document(
        run_id=912, signature=signature, tenant="tenant-a", product="product-a",
        target_url="http://app", thread_id=44, stories=stories,
        story_status={f"US{i:03d}": "clean" for i in range(1, 13)}, batch_size=12)
    path = campaign.write_checkpoint(tmp_path / "docs" / "QA-CHECKPOINT.json", document)
    saved = json.loads(path.read_text())

    assert saved["schema"] == "aos.qa.checkpoint/3"
    assert saved["evidence_policy_revision"] == campaign.EVIDENCE_POLICY_REVISION
    assert saved["total_stories"] == 14
    assert saved["next_index"] == 12
    assert saved["remaining_story_ids"] == ["US013", "US014"]
    assert campaign.checkpoint_matches(
        saved, signature=signature, tenant="tenant-a", product="product-a",
        target_url="http://app", thread_id=44)
    assert not campaign.checkpoint_matches(
        saved, signature=signature, tenant="tenant-b", product="product-a",
        target_url="http://app", thread_id=44)
    stale_policy = dict(saved)
    stale_policy.pop("evidence_policy_revision")
    assert not campaign.checkpoint_matches(
        stale_policy, signature=signature, tenant="tenant-a", product="product-a",
        target_url="http://app", thread_id=44)
    assert not list(path.parent.glob(".QA-CHECKPOINT.json.*.tmp"))


def test_matching_safety_checkpoint_reuses_durable_manifest_without_story_generation(tmp_path):
    stories = _stories(3)
    signature = campaign.campaign_signature(
        tenant="tenant-a", product="product-a", target_url="http://app", vision="vision",
        repo=tmp_path, stories=stories, thread_id=44)
    path = campaign.write_checkpoint(
        tmp_path / "docs" / "QA-CHECKPOINT.json",
        campaign.checkpoint_document(
            run_id=912, signature=signature, tenant="tenant-a", product="product-a",
            target_url="http://app", thread_id=44, stories=stories,
            story_status={"US001": "clean"}, batch_size=2, status="halted"))
    coordinator = {"role": "qa-coordinator", "memory": {"context": {
        "tenant": "tenant-a", "product": "product-a", "target_url": "http://app",
        "vision": "vision", "repo": str(tmp_path), "thread_id": 44, "stories": stories,
    }}}
    store = SimpleNamespace(
        run=lambda run_id, tenant: {
            "run_id": run_id, "status": "halted",
            "result": {"cancelled": True,
                       "reason": "automatic safety stop: product spend exceeded the standing limit"}},
        actors=lambda run_id, tenant: [coordinator])

    loaded = qa_agentic._checkpoint_stories_for_resume(
        path, store, tenant="tenant-a", product="product-a", target_url="http://app",
        vision="vision", repo=tmp_path, thread_id=44)

    assert loaded == stories
    store.run = lambda *_: {"run_id": 912, "status": "halted",
                            "result": {"cancelled": True, "reason": "stopped by user"}}
    assert qa_agentic._checkpoint_stories_for_resume(
        path, store, tenant="tenant-a", product="product-a", target_url="http://app",
        vision="vision", repo=tmp_path, thread_id=44) is None


def test_automatic_safety_stop_revives_exact_actors_but_user_cancel_stays_dead():
    actors = [
        {"actor_id": 1, "role": "qa-coordinator", "status": "dead",
         "result": {"cancelled": True, "reason": "automatic safety stop: spend cap"}},
        {"actor_id": 2, "role": "qa-explorer", "status": "dead",
         "result": {"cancelled": True, "reason": "automatic safety stop: spend cap"}},
        {"actor_id": 3, "role": "qa-explorer", "status": "dead",
         "result": {"cancelled": True, "reason": "stopped by user"}},
        {"actor_id": 4, "role": "qa-explorer", "status": "done",
         "result": {"result": {"stop_reason": "coverage-complete"}}},
    ]
    updates = []
    store = SimpleNamespace(
        actors=lambda *_: actors,
        update_actor=lambda *args, **kwargs: updates.append((args, kwargs)))

    assert qa_agentic._reopen_automatic_safety_stop_actors(store, 912, "tenant-a") == 2
    assert [item[0][0] for item in updates] == [1, 2]
    assert all(item[1] == {"status": "blocked", "result": None} for item in updates)
    assert qa_agentic._resumable_halted_result({"timed_out": True}) is True
    assert qa_agentic._resumable_halted_result(
        {"cancelled": True, "reason": "stopped by user"}) is False


def test_product_revision_tracks_source_but_not_generated_qa_gate_files(tmp_path):
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "app.js"
    source.write_text("export const value = 1;\n")
    before = campaign.repo_revision(tmp_path)

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "QA-CHECKPOINT.json").write_text('{"cursor": 12}\n')
    (tmp_path / "docs" / "QA-VERDICT.json").write_text('{"passed": false}\n')
    (tmp_path / "PAUSED.html").write_text("temporarily paused by the control plane\n")
    assert campaign.repo_revision(tmp_path) == before
    assert campaign.repo_revision(tmp_path, include_control_markers=True) != before

    (tmp_path / "tests").mkdir()
    regression = tmp_path / "tests" / "app.test.js"
    regression.write_text("assert executable behavior\n")
    assert campaign.repo_revision(tmp_path) == before
    legacy_with_evidence = campaign.repo_revision(tmp_path, include_evidence_files=True)
    regression.write_text("assert executable behavior more rigorously\n")
    assert campaign.repo_revision(tmp_path) == before
    assert campaign.repo_revision(tmp_path, include_evidence_files=True) != legacy_with_evidence

    source.write_text("export const value = 2;\n")
    assert campaign.repo_revision(tmp_path) != before


def test_covered_labels_require_exact_durable_evidence():
    label_only = {
        "stop_reason": "coverage-complete", "bugs": 0,
        "coverage": [{"aspect": "Submit and retain the enquiry", "covered": True}],
    }
    assert campaign.result_evidence_complete(label_only) is False
    assert campaign.evidence_diagnostics(label_only)["unproven_aspects"] == [
        "Submit and retain the enquiry"]

    proved = {**label_only, "steps_detail": [
        {"action": "submit", "verdict": "match", "covers": ["Submit and retain the enquiry"]},
    ]}
    assert campaign.result_evidence_complete(proved) is True

    broader = {**proved, "coverage": [{
        "aspect": "Submit and retain the enquiry, then reload and verify every field", "covered": True,
    }]}
    assert campaign.result_evidence_complete(broader) is False


def test_atomic_mechanical_receipt_survives_compaction_and_label_only_rows_reopen():
    ledger = [
        {"aspect": "semantic journey", "covered": True},
        {"aspect": "dwell ten seconds", "covered": True, "proof": {
            "engine": "mechanical-atomic-dwell-proof", "action_kind": "dwell_surfaces",
            "recorded_at": 123.5,
        }},
    ]
    repaired, reopened = campaign.reopen_unproven_coverage(ledger, [])
    assert reopened == {"semantic journey"}
    assert repaired[0]["covered"] is False
    assert repaired[1]["covered"] is True

    rows = [{"action": f"noise-{index}", "verdict": "mismatch", "covers": []}
            for index in range(700)]
    rows.insert(2, {"action": "proof", "verdict": "match", "covers": ["semantic journey"]})
    compact = campaign.compact_evidence_records(rows, max_records=30, tail=5)
    assert any(item.get("action") == "proof" for item in compact)
    assert len(compact) <= 30


def test_terminal_run_continuation_reuses_only_current_grounded_ordinary_proofs():
    clean = {"story": "US-1", "stop_reason": "coverage-complete", "bugs": 0,
             "coverage": [{"aspect": "journey", "covered": True}],
             "steps_detail": [{"verdict": "match", "covers": ["journey"]}]}
    actors = [
        {"actor_id": 1, "role": "qa-coordinator", "memory": {
            "coverage_revision": "rev-b", "story_status": {"US-1": "clean"},
            "context": {"product": "app", "stories": [{"id": "US-1"}]}},
        },
        {"actor_id": 2, "role": "qa-explorer", "memory": {"context": {"tool_args": {
            "product_revision": "rev-b"}}}, "result": {"result": clean}},
        {"actor_id": 3, "role": "qa-explorer", "memory": {"context": {"tool_args": {
            "product_revision": "rev-a"}}}, "result": {"result": {**clean, "story": "US-stale"}}},
        {"actor_id": 4, "role": "qa-explorer", "memory": {"context": {"tool_args": {
            "product_revision": "rev-b"}}}, "result": {"result": {
                "story": "US-label", "stop_reason": "coverage-complete", "bugs": 0,
                "coverage": [{"aspect": "label", "covered": True}]}}},
        {"actor_id": 5, "role": "qa-explorer", "memory": {"context": {"tool_args": {
            "product_revision": "rev-b"}}}, "result": {"result": {**clean, "story": "US-finding"}}},
        {"actor_id": 6, "role": "qa-explorer", "memory": {"context": {"tool_args": {
            "product_revision": "rev-b", "_qa_review_id": "review"}}},
         "result": {"result": {**clean, "story": "US-focused"}}},
    ]
    fake = SimpleNamespace(
        run=lambda *_args: {"status": "done"},
        actors=lambda *_args: actors,
        events=lambda *_args: [{"kind": "finding", "frm": 5}],
    )

    recovered = qa_agentic._durable_terminal_continuity(fake, 77, "tenant", "rev-b")

    assert recovered["run_id"] == 77
    assert recovered["reusable_story_ids"] == ["US-1"]
    assert recovered["story_status"] == {"US-1": "clean"}
    assert recovered["story_results"] == {"US-1": clean}


def test_terminal_continuation_reuses_complete_receipt_after_exact_false_positive_resolution():
    result = {
        "story": "US-1", "stop_reason": "coverage-complete", "bugs": 1,
        "coverage": [{"aspect": "journey", "covered": True}],
        "steps_detail": [{"verdict": "match", "covers": ["journey"]}],
    }
    actors = [
        {"actor_id": 1, "role": "qa-coordinator", "memory": {
            "coverage_revision": "rev-b",
            "story_status": {"US-1": "clean"},
            "context": {"product": "app", "stories": [{"id": "US-1"}]},
            "finding_resolutions": [{
                "finding_id": "finding-1", "story": "US-1",
                "disposition": "verified_false_positive",
            }],
        }},
        {"actor_id": 2, "role": "qa-explorer", "memory": {"context": {"tool_args": {
            "product_revision": "rev-b"}}}, "result": {"result": result}},
    ]
    fake = SimpleNamespace(
        run=lambda *_args: {"status": "done"},
        actors=lambda *_args: actors,
        events=lambda *_args: [{
            "kind": "finding", "frm": 2,
            "payload": {"finding_id": "finding-1", "story": "US-1"},
        }],
    )

    recovered = qa_agentic._durable_terminal_continuity(fake, 77, "tenant", "rev-b")

    assert recovered["reusable_story_ids"] == ["US-1"]
    assert recovered["story_results"]["US-1"]["bugs"] == 0
    assert recovered["story_results"]["US-1"]["resolved_false_positive_findings"] == [
        "finding-1"]


def test_evidence_hash_policy_upgrade_restores_ledger_erased_by_false_invalidation(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.js").write_text("export const value = 1;\n")
    (tmp_path / "tests" / "app.test.js").write_text("assert value one\n")
    current = campaign.repo_revision(tmp_path)
    legacy = campaign.repo_revision(tmp_path, include_evidence_files=True)
    assert current != legacy
    actors = [{
        "actor_id": 1, "role": "qa-coordinator", "status": "working", "memory": {
            "coverage_revision": legacy,
            "story_status": {},
            "revision_invalidations": [{
                "from": "older-revision", "to": legacy,
                "stale_story_status": {"US-1": "clean", "US-2": "incomplete"},
            }],
        },
    }]

    class FakeStore:
        @staticmethod
        def actors(_run_id, _tenant):
            return actors

        @staticmethod
        def update_actor(actor_id, _tenant, **changes):
            actor = next(item for item in actors if item["actor_id"] == actor_id)
            actor["memory"].update(changes.get("memory") or {})

    changed = qa_agentic._invalidate_resumed_revision(
        FakeStore(), 9, "tenant", current, repo=tmp_path, runtime_mod=runtime)

    assert changed == 0
    assert actors[0]["memory"]["story_status"] == {"US-1": "clean", "US-2": "incomplete"}
    assert actors[0]["memory"]["coverage_revision"] == current
    assert "non-runtime test/docs evidence" in actors[0]["memory"]["revision_equivalences"][-1]["reason"]


def test_pause_marker_policy_upgrade_restores_durable_story_evidence(tmp_path):
    """Removing a generated control marker from the hash must repair, not erase, the live campaign."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.js").write_text("export const value = 1;\n")
    (tmp_path / "PAUSED.html").write_text("control-plane state\n")
    current = campaign.repo_revision(tmp_path)
    legacy = campaign.repo_revision(tmp_path, include_control_markers=True)
    assert current != legacy

    actors = [
        {"actor_id": 1, "role": "qa-coordinator", "status": "working", "memory": {
            "coverage_revision": legacy, "story_status": {"US-4": "incomplete"}}},
        {"actor_id": 2, "role": "qa-explorer", "status": "done", "memory": {"context": {
            "tool_args": {"product_revision": current, "story": {"id": "US-1"}}}},
         "result": {"result": {"story": "US-1", "bugs": 0,
                                 "stop_reason": "coverage-complete",
                                 "coverage": [{"aspect": "journey", "covered": True}],
                                 "steps_detail": [{"verdict": "match", "covers": ["journey"]}]}}},
        {"actor_id": 3, "role": "qa-explorer", "status": "done", "memory": {"context": {
            "tool_args": {"product_revision": legacy, "story": {"id": "US-2"}}}},
         "result": {"blocking_found": True,
                    "result": {"story": "US-2", "bugs": 1, "stop_reason": "coverage-complete"}}},
        {"actor_id": 4, "role": "qa-explorer", "status": "blocked", "memory": {"context": {
            "tool_args": {"product_revision": legacy, "story": {"id": "US-3"},
                          "resume_state_path": "/tmp/portable-state"}}}},
    ]

    class FakeStore:
        @staticmethod
        def actors(_run_id, _tenant):
            return actors

        @staticmethod
        def update_actor(actor_id, _tenant, **changes):
            actor = next(item for item in actors if item["actor_id"] == actor_id)
            if "memory" in changes:
                actor["memory"].update(changes["memory"])

    changed = qa_agentic._invalidate_resumed_revision(
        FakeStore(), 9, "tenant", current, repo=tmp_path, runtime_mod=runtime)

    assert changed == 0
    coordinator = actors[0]["memory"]
    assert coordinator["coverage_revision"] == current
    assert coordinator["story_status"] == {
        "US-1": "clean", "US-2": "blocking", "US-4": "incomplete"}
    active_args = actors[3]["memory"]["context"]["tool_args"]
    assert active_args["product_revision"] == current
    assert active_args["resume_state_path"] == "/tmp/portable-state"


def test_runtime_admits_only_one_story_batch_but_keeps_full_manifest_in_context():
    stories = _stories(25)
    actor = {"name": "qa", "memory": {"context": {
        "stories": stories, "story_batch_size": 12, "product_revision": "rev-a",
    }}}
    specs = runtime._coordinator_specs(SimpleNamespace(), actor, "test all stories", "qa-coordinator")

    assert len(specs) == 12
    assert [spec["tool_args"]["story"]["id"] for spec in specs] == [
        f"US{i:03d}" for i in range(1, 13)]
    assert all(spec["tool_args"]["product_revision"] == "rev-a" for spec in specs)
    assert actor["memory"]["context"]["stories"] == stories


def test_fixer_revision_change_invalidates_all_prior_coverage_for_final_regression():
    memory = {"context": {"product_revision": "rev-a"}, "coverage_revision": "rev-a",
              "story_status": {"US001": "clean", "US002": "blocking"},
              "gapfills": {"US001": 2, "US002": 1}}

    assert runtime._qa_invalidate_revision(memory, "rev-b") == ["US001", "US002"]
    assert memory["story_status"] == {}
    assert memory["gapfills"] == {}
    assert memory["coverage_revision"] == "rev-b"
    assert memory["revision_generation"] == 1
    assert runtime._qa_invalidate_revision(memory, "rev-b") == []


def test_fixer_revision_change_can_preserve_independently_proven_unaffected_stories():
    memory = {"coverage_revision": "rev-a",
              "story_status": {"US001": "clean", "US002": "blocking", "US003": "clean"},
              "gapfills": {"US001": 2, "US002": 1, "US003": 3}}
    impact = {"reason": "two-reviewer conservative union", "changed_files": ["src/trust_policy/index.js"]}

    stale = runtime._qa_invalidate_revision(
        memory, "rev-b", impacted_story_ids=["US002"], impact=impact)

    assert stale == ["US002"]
    assert memory["story_status"] == {"US001": "clean", "US003": "clean"}
    assert memory["gapfills"] == {"US001": 2, "US003": 3}
    event = memory["revision_invalidations"][-1]
    assert event["preserved_story_ids"] == ["US001", "US003"]
    assert event["full_regression"] is False
    assert event["changed_files"] == ["src/trust_policy/index.js"]


def test_revision_invalidation_retires_stale_disputes_and_old_fix_queue():
    review = {"review_id": "qa-review-old", "story": "US002",
              "finding": {"finding_id": "finding-old", "story": "US002",
                          "title": "old revision observation"}}
    memory = {
        "coverage_revision": "rev-a",
        "story_status": {"US001": "clean", "US002": "internal_review"},
        "internal_reviews": [review],
        "internal_review_states": {"qa-review-old": {"status": "manager_attention",
                                                        "case_id": "qad-old"}},
        "pending_dev_findings": [{"story": "US002", "title": "old revision observation"}],
    }

    stale = runtime._qa_invalidate_revision(
        memory, "rev-b", impacted_story_ids=["US002"],
        impact={"reason": "bounded UI change", "changed_files": ["src/staff.js"]})

    assert stale == ["US002"]
    assert memory["story_status"] == {"US001": "clean"}
    assert memory["internal_reviews"] == []
    assert memory["pending_dev_findings"] == []
    assert memory["internal_review_states"]["qa-review-old"]["status"] == "superseded_by_revision"
    assert memory["finding_resolutions"][-1]["disposition"] == "superseded_by_current_revision"


def test_change_impact_adjudicates_disagreement_and_always_includes_trigger_story():
    stories = [{"id": f"US00{i}", "title": f"story {i}", "steps": [f"flow {i}"],
                "expected_outcome": f"outcome {i}"} for i in range(1, 5)]
    answers = iter([
        {"impacted_story_ids": ["US002"], "full_regression": False, "reason": "shared policy"},
        {"impacted_story_ids": ["US003"], "full_regression": False, "reason": "rendered risk"},
        {"impacted_story_ids": ["US002"], "full_regression": False,
         "reason": "only US002 has a concrete changed-policy dependency"},
    ])

    impact = runtime._qa_revision_impact_scope(
        stories, ["src/trust_policy/index.js", "tests/trust_policy.test.js"], "US001",
        {"detail": "policy leak"}, reviewer=lambda *_: next(answers))

    assert impact["full_regression"] is False
    assert impact["impacted_story_ids"] == ["US001", "US002"]
    assert impact["preserved_story_ids"] == ["US003", "US004"]


def test_change_impact_adjudication_failure_retains_conservative_union():
    stories = [{"id": "US001", "steps": ["Enter from Name"]},
               {"id": "US002", "steps": ["inspect later records"]},
               {"id": "US003", "steps": ["keyboard traversal"]}]
    answers = iter([
        {"impacted_story_ids": ["US001", "US003"], "full_regression": False, "reason": "event path"},
        {"impacted_story_ids": ["US001", "US002"], "full_regression": False, "reason": "downstream"},
        {},
    ])

    impact = runtime._qa_revision_impact_scope(
        stories, ["src/public_form.js"], "US001",
        {"detail": "native Enter adapter"}, reviewer=lambda *_: next(answers))

    assert impact["impacted_story_ids"] == ["US001", "US002", "US003"]
    assert "retained conservative union" in impact["reason"]


def test_one_full_scope_reviewer_cannot_force_manifest_replay_when_adjudicator_bounds_change():
    stories = [{"id": "US001", "steps": ["inspect staff audit"]},
               {"id": "US002", "steps": ["resume staff audit"]},
               {"id": "US003", "steps": ["submit empty first run"]}]
    answers = iter([
        {"impacted_story_ids": ["US001", "US002", "US003"], "full_regression": True,
         "reason": "shared staff module looked broad"},
        {"impacted_story_ids": ["US001", "US002"], "full_regression": False,
         "reason": "only audit rendering is changed"},
        {"impacted_story_ids": ["US001", "US002"], "full_regression": False,
         "reason": "US003 has no causal dependency on audit rendering"},
    ])

    impact = runtime._qa_revision_impact_scope(
        stories, ["src/staff_console.js", "tests/staff_console.test.js"], "US002",
        {"detail": "retained audit history was filtered"}, reviewer=lambda *_: next(answers))

    assert impact["full_regression"] is False
    assert impact["impacted_story_ids"] == ["US001", "US002"]
    assert impact["preserved_story_ids"] == ["US003"]


def test_two_full_scope_reviewers_still_fail_closed_to_manifest_replay():
    stories = [{"id": "US001", "steps": ["one"]}, {"id": "US002", "steps": ["two"]}]
    impact = runtime._qa_revision_impact_scope(
        stories, ["src/feature.js"], "US001", reviewer=lambda *_: {
            "impacted_story_ids": ["US001", "US002"], "full_regression": True,
            "reason": "both journeys share the changed behavior"})

    assert impact["full_regression"] is True
    assert impact["impacted_story_ids"] == ["US001", "US002"]
    assert impact["preserved_story_ids"] == []


def test_change_impact_gives_reviewers_specific_fixer_and_test_contract_evidence(tmp_path):
    test_path = tmp_path / "tests" / "trust_policy.test.js"
    test_path.parent.mkdir(parents=True)
    test_path.write_text('test("US-010 blocks bare provider tokens", () => {});\n')
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "trust_policy.js").write_text("export const validate = () => false;\n")
    prompts = []

    def review(lens, prompt):
        prompts.append((lens, prompt))
        return {"impacted_story_ids": ["US010"], "full_regression": False,
                "reason": "bounded detector branch"}

    impact = runtime._qa_revision_impact_scope(
        [{"id": "US010", "steps": ["publish"], "expected_outcome": "blocked"},
         {"id": "US011", "steps": ["dashboard"], "expected_outcome": "renders"}],
        ["src/trust_policy.js", "tests/trust_policy.test.js"], "US010",
        {"detail": "bare token escaped"}, repo=tmp_path, reviewer=review,
        change_summary={
            "result": "recognize sk_live token shape",
            "durable_change_diffs": [
                "@@ trust policy\n-export const validate = () => true;\n"
                "+export const validate = () => false;\n"],
        })

    assert impact["impacted_story_ids"] == ["US010"]
    assert len(prompts) == 2
    assert all("recognize sk_live token shape" in prompt for _, prompt in prompts)
    assert all("US-010 blocks bare provider tokens" in prompt for _, prompt in prompts)
    assert all("AUTHORITATIVE MUTATION RECEIPT" in prompt for _, prompt in prompts)
    assert all("EXACT MUTATION DIFF FROM THE FENCED WRITER" in prompt for _, prompt in prompts)
    assert all("-export const validate = () => true" in prompt for _, prompt in prompts)
    assert all("do not attribute any current-file content absent from this diff" in prompt
               for _, prompt in prompts)
    assert all("export const validate" in prompt for _, prompt in prompts)


def test_change_impact_independent_reviews_share_one_wall_clock_interval(tmp_path):
    stories = [{"id": "US001", "steps": ["public form"]},
               {"id": "US002", "steps": ["CEO view"]}]
    barrier = threading.Barrier(2)

    def review(_lens, _prompt):
        barrier.wait(timeout=1)
        time.sleep(0.05)
        return {"impacted_story_ids": ["US001"], "full_regression": False,
                "reason": "bounded public form"}

    started = time.monotonic()
    impact = runtime._qa_revision_impact_scope(
        stories, ["src/public_form.js"], "US001",
        {"detail": "blank name validation"}, repo=tmp_path, reviewer=review)

    assert time.monotonic() - started < 0.5
    assert impact["full_regression"] is False
    assert impact["preserved_story_ids"] == ["US002"]


def test_change_impact_fails_closed_for_shared_or_malformed_change_reviews():
    stories = [{"id": "US001", "steps": ["open"]}, {"id": "US002", "steps": ["submit"]}]
    called = []
    shared = runtime._qa_revision_impact_scope(
        stories, ["src/local_state/index.js"], "US001", reviewer=lambda *_: called.append(True))
    assert shared["full_regression"] is True
    assert shared["impacted_story_ids"] == ["US001", "US002"]
    assert called == []

    malformed = runtime._qa_revision_impact_scope(
        stories, ["src/bounded_policy/index.js"], "US001", reviewer=lambda *_: {})
    assert malformed["full_regression"] is True


def test_shared_ui_surface_uses_independent_blast_radius_review_instead_of_forcing_every_story():
    stories = [{"id": "US001", "steps": ["public form"]},
               {"id": "US011", "steps": ["CEO dashboard"]},
               {"id": "US012", "steps": ["keyboard traversal"]}]
    prompts = []

    def review(lens, prompt):
        prompts.append((lens, prompt))
        return {"impacted_story_ids": ["US011", "US012"], "full_regression": False,
                "reason": "bounded CEO focus surface"}

    impact = runtime._qa_revision_impact_scope(
        stories, ["index.html", "src/browser_app/index.js", "src/ceo/styles.css"],
        "US011", reviewer=review,
        change_summary={"result": "preserve CEO acknowledgement after reload"})

    assert impact["full_regression"] is False
    assert impact["impacted_story_ids"] == ["US011", "US012"]
    assert impact["preserved_story_ids"] == ["US001"]
    assert len(prompts) == 2
    assert all("POTENTIALLY SHARED SURFACES" in prompt for _, prompt in prompts)


def test_dev_completion_receipt_recovers_nested_fixer_files_after_handoff():
    actors = [
        {"actor_id": 10, "supervisor_id": 1, "role": "dev-coordinator",
         "result": {"fixed": False, "result": "already fixed"}},
        {"actor_id": 11, "supervisor_id": 10, "role": "dev-fixer",
         "result": {"tool": "dev_fix", "result": {
             "fixed": False, "files": ["src/ceo.js", "tests/ceo.test.js"],
             "change_diff": "@@ ceo.js\n-old\n+new\n",
             "plan": {"rationale": "workspace already contains the checkpointed repair"}}},
         "memory": {"context": {"tool_args": {
             "resume_changed_files": ["src/ceo.css"],
             "resume_change_diff": "@@ ceo.css\n-old css\n+new css\n"}}}},
    ]

    receipt = runtime._qa_dev_completion_receipt(
        10, actors, {"fixed": False, "result": "parent resumed"})

    assert receipt["files"] == ["src/ceo.js", "tests/ceo.test.js", "src/ceo.css"]
    assert receipt["actor_ids"] == [10, 11]
    assert receipt["change_diffs"] == [
        "@@ ceo.js\n-old\n+new", "@@ ceo.css\n-old css\n+new css"]
    assert "workspace already contains" in " ".join(receipt["summaries"])


def test_resume_revision_change_clears_stale_story_status_and_portable_browser_state():
    actors = [
        {"actor_id": 1, "role": "qa-coordinator", "status": "working", "memory": {
            "context": {"product_revision": "rev-a"}, "coverage_revision": "rev-a",
            "story_status": {"US001": "clean"}, "gapfills": {"US001": 2}}},
        {"actor_id": 2, "role": "qa-explorer", "status": "blocked", "memory": {"context": {
            "tool_args": {"story": {"id": "US002"}, "product_revision": "rev-a",
                          "resume_covered": ["setup"], "resume_state_path": "/old/state.json",
                          "resume_coverage": [{"aspect": "setup", "covered": True}],
                          "resume_steps_detail": [{"action": "open"}]}}}},
    ]

    class Store:
        def __init__(self):
            self.updates = {}

        def actors(self, _run_id, _tenant):
            return actors

        def update_actor(self, actor_id, _tenant, **values):
            self.updates[actor_id] = values

    store = Store()
    assert qa_agentic._invalidate_resumed_revision(store, 9, "tenant-a", "rev-b") == 1
    assert store.updates[1]["memory"]["story_status"] == {}
    tool_args = store.updates[2]["memory"]["context"]["tool_args"]
    assert tool_args["product_revision"] == "rev-b"
    assert "resume_state_path" not in tool_args
    assert "resume_covered" not in tool_args
    assert "resume_coverage" not in tool_args


def test_resume_revision_change_defers_to_unprocessed_durable_dev_mutation_receipt():
    actors = [
        {"actor_id": 1, "role": "qa-coordinator", "status": "blocked", "memory": {
            "coverage_revision": "rev-a", "story_status": {"US001": "clean", "US002": "blocking"}}},
        {"actor_id": 2, "role": "dev-coordinator", "status": "done", "memory": {}},
        {"actor_id": 3, "role": "qa-explorer", "status": "blocked", "memory": {"context": {
            "tool_args": {"story": {"id": "US002"}, "product_revision": "rev-a",
                          "resume_covered": ["setup"], "resume_state_path": "/portable/state"}}}},
    ]

    class Store:
        def __init__(self):
            self.updates = {}

        def actors(self, _run_id, _tenant):
            return actors

        def events(self, _run_id, _tenant):
            return [{"id": 91, "frm": 2, "kind": "done", "processed_at": None,
                     "payload": {"result": {"fixed": True,
                                             "files": ["src/trust_policy/index.js"]}}}]

        def update_actor(self, actor_id, _tenant, **values):
            self.updates[actor_id] = values

    store = Store()
    assert qa_agentic._invalidate_resumed_revision(store, 9, "tenant-a", "rev-b") == 0
    assert store.updates[1]["memory"]["revision_reconciliation"]["state"] == (
        "deferred_pending_dev_receipt")
    assert store.updates[1]["memory"]["revision_reconciliation"]["event_ids"] == [91]
    # The pending coordinator event remains the sole authority to invalidate and retest; portable browser
    # state must not be rewritten against the new revision before that decision lands.
    assert 3 not in store.updates


def test_resume_revision_change_defers_to_receipt_parked_on_live_dev_chain():
    actors = [
        {"actor_id": 1, "role": "qa-coordinator", "status": "blocked", "memory": {
            "coverage_revision": "rev-a", "story_status": {
                "US001": "clean", "US002": "blocking"}}},
        {"actor_id": 2, "role": "dev-coordinator", "status": "working", "memory": {}},
        {"actor_id": 3, "supervisor_id": 2, "role": "dev-fixer", "status": "blocked",
         "result": {"checkpointed": True, "partial_result": {
             "files": ["src/trust_policy/index.js"]}},
         "memory": {"context": {"tool": "dev_fix", "tool_args": {
             "resume_changed_files": ["tests/trust_policy.test.js"]}}}},
        {"actor_id": 4, "role": "qa-explorer", "status": "blocked", "memory": {"context": {
            "tool_args": {"story": {"id": "US002"}, "product_revision": "rev-a",
                          "resume_state_path": "/portable/state"}}}},
    ]

    class Store:
        def __init__(self):
            self.updates = {}

        def actors(self, _run_id, _tenant):
            return actors

        def events(self, _run_id, _tenant):
            # The checkpoint event has already been consumed; actor state is now the durable authority.
            return []

        def update_actor(self, actor_id, _tenant, **values):
            self.updates[actor_id] = values

    store = Store()
    assert qa_agentic._invalidate_resumed_revision(store, 9, "tenant-a", "rev-b") == 0
    reconciliation = store.updates[1]["memory"]["revision_reconciliation"]
    assert reconciliation["state"] == "deferred_pending_dev_receipt"
    assert reconciliation["event_ids"] == []
    assert reconciliation["actor_ids"] == [3]
    assert reconciliation["changed_files"] == [
        "src/trust_policy/index.js", "tests/trust_policy.test.js"]
    assert 4 not in store.updates


def test_live_fixer_recovers_lost_mutation_receipt_from_sealed_finding_manifest(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "artifacts").mkdir()
    source = tmp_path / "src" / "trust_policy.js"
    source.write_text("export const blocksToken = false;\n")
    provenance = dev_loop.capture_finding_provenance(tmp_path, tmp_path / "artifacts")
    source.write_text("export const blocksToken = true;\n")
    (tmp_path / "tests" / "trust_policy.test.js").write_text("test token blocker\n")
    bug = {"story": "US010", "detail": "bearer token escaped",
           "evidence_provenance": provenance}
    actors = [
        {"actor_id": 2, "role": "dev-coordinator", "status": "working",
         "memory": {"context": {"bug": bug}}},
        {"actor_id": 3, "supervisor_id": 2, "role": "dev-fixer", "status": "blocked",
         "result": None, "memory": {"context": {"tool_args": {"bug": bug}}}},
    ]

    store = SimpleNamespace(events=lambda *_args: [])
    receipts = qa_agentic._pending_dev_mutation_receipts(
        store, 9, "tenant-a", actors, repo=tmp_path)

    assert receipts
    assert sorted({path for item in receipts for path in item["files"]}) == [
        "src/trust_policy.js", "tests/trust_policy.test.js"]
    assert {item["source"] for item in receipts} == {"actor_state"}


def test_resume_repairs_false_global_invalidation_from_sealed_finding_delta(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "artifacts").mkdir()
    source = tmp_path / "src" / "trust_policy.js"
    source.write_text("export const blocksToken = false;\n")
    provenance = dev_loop.capture_finding_provenance(tmp_path, tmp_path / "artifacts")
    source.write_text("export const blocksToken = true;\n")
    (tmp_path / "tests" / "trust_policy.test.js").write_text("test token blocker\n")
    bug = {"story": "US010", "detail": "bearer token escaped",
           "evidence_provenance": provenance}
    actors = [
        {"actor_id": 1, "role": "qa-coordinator", "status": "working", "memory": {
            "context": {"stories": [{"id": "US001"}, {"id": "US010"}]},
            "coverage_revision": "rev-b", "story_status": {"US010": "incomplete"},
            "revision_invalidations": [{
                # Older startup invalidations omitted full_regression; absence was the full-regression form.
                "from": "rev-a", "to": "rev-b",
                "stale_story_status": {"US001": "clean", "US010": "blocking"},
            }],
            "revision_reconciliation": {"state": "full_invalidation", "to": "rev-b"},
        }},
        {"actor_id": 2, "role": "dev-coordinator", "status": "working",
         "memory": {"context": {"bug": bug}}},
        {"actor_id": 3, "supervisor_id": 2, "role": "dev-fixer", "status": "blocked",
         "result": None, "memory": {"context": {"tool_args": {"bug": bug}}}},
    ]

    class Store:
        def actors(self, _run_id, _tenant):
            return actors

        def events(self, _run_id, _tenant):
            return []

        def update_actor(self, actor_id, _tenant, **values):
            actor = next(item for item in actors if item["actor_id"] == actor_id)
            actor.setdefault("memory", {}).update(values.get("memory") or {})

    fake_runtime = SimpleNamespace(
        _qa_dev_completion_receipt=lambda *_args, **_kwargs: {
            "summaries": [], "actor_ids": [2, 3]},
        _qa_revision_impact_scope=lambda *_args, **_kwargs: {
            "full_regression": False, "impacted_story_ids": ["US010"],
            "preserved_story_ids": ["US001"],
            "changed_files": ["src/trust_policy.js", "tests/trust_policy.test.js"],
            "reason": "sealed manifest bounded the interrupted security fix"},
    )

    changed = qa_agentic._invalidate_resumed_revision(
        Store(), 9, "tenant-a", "rev-b", repo=tmp_path, runtime_mod=fake_runtime)

    assert changed == 0
    memory = actors[0]["memory"]
    assert memory["story_status"] == {"US001": "clean", "US010": "incomplete"}
    assert memory["revision_reconciliation"]["state"] == "selective_compensation"
    assert memory["revision_compensations"][-1]["restored_story_status"] == {"US001": "clean"}


def test_resume_repairs_false_global_invalidation_from_deferred_descendant_receipt():
    actors = [
        {"actor_id": 1, "role": "qa-coordinator", "status": "working", "memory": {
            "context": {"stories": [{"id": "US001"}, {"id": "US002"}]},
            "coverage_revision": "rev-b", "story_status": {"US002": "incomplete"},
            "revision_invalidations": [{
                "from": "rev-a", "to": "rev-b", "full_regression": True,
                "stale_story_status": {"US001": "clean", "US002": "blocking"},
            }],
            "revision_reconciliation": {
                "state": "deferred_pending_dev_receipt", "from": "rev-a", "to": "rev-b",
                "actor_ids": [2], "changed_files": ["src/ceo_view.js"],
            }}},
        {"actor_id": 2, "supervisor_id": 9, "role": "dev-fixer", "status": "done",
         "result": {"result": {"files": ["src/ceo_view.js"], "plan": {
             "rationale": "bounded US002 repair already exists"}}},
         "memory": {"context": {"tool_args": {
             "bug": {"story": "US002", "detail": "CEO state reload"}}}}},
    ]

    class Store:
        def actors(self, _run_id, _tenant):
            return actors

        def events(self, _run_id, _tenant):
            return []

        def update_actor(self, actor_id, _tenant, **values):
            actor = next(item for item in actors if item["actor_id"] == actor_id)
            actor.setdefault("memory", {}).update(values.get("memory") or {})

    fake_runtime = SimpleNamespace(
        _qa_dev_completion_receipt=lambda *_args, **_kwargs: {
            "summaries": ["bounded US002 repair already exists"], "actor_ids": [2]},
        _qa_revision_impact_scope=lambda *_args, **_kwargs: {
            "full_regression": False, "impacted_story_ids": ["US002"],
            "preserved_story_ids": ["US001"], "changed_files": ["src/ceo_view.js"],
            "reason": "two-reviewer bounded impact"},
    )

    changed = qa_agentic._invalidate_resumed_revision(
        Store(), 9, "tenant-a", "rev-b", runtime_mod=fake_runtime)

    assert changed == 0
    memory = actors[0]["memory"]
    assert memory["story_status"] == {"US001": "clean", "US002": "incomplete"}
    assert memory["revision_reconciliation"]["state"] == "selective_compensation"
    assert memory["revision_compensations"][-1]["restored_story_status"] == {"US001": "clean"}


def test_resume_records_reviewed_full_scope_instead_of_leaving_receipt_pending():
    actors = [
        {"actor_id": 1, "role": "qa-coordinator", "status": "working", "memory": {
            "context": {"stories": [{"id": "US001"}, {"id": "US002"}]},
            "coverage_revision": "rev-b", "story_status": {},
            "revision_invalidations": [{
                "from": "rev-a", "to": "rev-b", "full_regression": True,
                "stale_story_status": {"US001": "clean", "US002": "clean"},
            }],
            "revision_reconciliation": {
                "state": "deferred_pending_dev_receipt", "from": "rev-a", "to": "rev-b",
                "actor_ids": [2], "changed_files": ["src/shared.js"],
            }}},
        {"actor_id": 2, "role": "dev-fixer", "status": "done",
         "result": {"result": {"files": ["src/shared.js"]}},
         "memory": {"context": {"tool_args": {"bug": {
             "story": "US001", "detail": "shared contract changed"}}}}},
    ]

    class Store:
        def actors(self, _run_id, _tenant):
            return actors

        def events(self, _run_id, _tenant):
            return []

        def update_actor(self, actor_id, _tenant, **values):
            actor = next(item for item in actors if item["actor_id"] == actor_id)
            actor.setdefault("memory", {}).update(values.get("memory") or {})

    fake_runtime = SimpleNamespace(
        _qa_dev_completion_receipt=lambda *_args, **_kwargs: {
            "summaries": ["shared contract changed"], "actor_ids": [2]},
        _qa_revision_impact_scope=lambda *_args, **_kwargs: {
            "full_regression": True, "impacted_story_ids": ["US001", "US002"],
            "changed_files": ["src/shared.js"], "reason": "two reviewers confirmed cross-cutting scope"},
    )

    qa_agentic._invalidate_resumed_revision(
        Store(), 9, "tenant-a", "rev-b", runtime_mod=fake_runtime)

    reconciliation = actors[0]["memory"]["revision_reconciliation"]
    assert reconciliation["state"] == "full_invalidation_confirmed"
    assert reconciliation["impacted_story_ids"] == ["US001", "US002"]
    assert reconciliation["reason"] == "two reviewers confirmed cross-cutting scope"


def test_historical_done_dev_actor_does_not_hide_unexplained_revision_change():
    actors = [
        {"actor_id": 1, "role": "qa-coordinator", "status": "working", "memory": {
            "coverage_revision": "rev-a", "story_status": {"US001": "clean"}}},
        {"actor_id": 2, "role": "dev-coordinator", "status": "done", "result": {
            "result": {"files": ["src/historical.js"]}}, "memory": {}},
        {"actor_id": 3, "supervisor_id": 2, "role": "dev-fixer", "status": "done",
         "result": {"partial_result": {"files": ["src/historical.js"]}},
         "memory": {"context": {"tool_args": {
             "resume_changed_files": ["tests/historical.test.js"]}}}},
    ]

    class Store:
        def actors(self, _run_id, _tenant):
            return actors

        def events(self, _run_id, _tenant):
            return []

        def update_actor(self, actor_id, _tenant, **values):
            actor = next(item for item in actors if item["actor_id"] == actor_id)
            actor.setdefault("memory", {}).update(values.get("memory") or {})

    assert qa_agentic._invalidate_resumed_revision(Store(), 9, "tenant-a", "rev-b") == 1
    assert actors[0]["memory"]["revision_reconciliation"]["state"] == "full_invalidation"
    assert actors[0]["memory"]["story_status"] == {}


def test_revision_compensation_restores_only_audited_preserved_statuses_and_keeps_fresh_results():
    memory = {
        "story_status": {"US010": "clean"},
        "revision_generation": 6,
        "revision_compensations": [],
    }
    impact = {
        "full_regression": False,
        "impacted_story_ids": ["US005", "US010"],
        "preserved_story_ids": ["US001", "US002", "US003"],
        "changed_files": ["src/trust_policy.js"],
    }

    restored = qa_agentic._restore_revision_compensation(
        memory, "rev-current", impact=impact,
        prior_status={"US001": "clean", "US002": "blocking", "US003": "incomplete",
                      "US005": "clean", "US010": "blocking", "US999": "mystery"},
        reason="repair consumed receipt race", recorded_at=123.0)

    assert restored == {"US001": "clean", "US002": "blocking", "US003": "incomplete"}
    assert memory["story_status"] == {
        "US001": "clean", "US002": "blocking", "US003": "incomplete", "US010": "clean"}
    assert memory["revision_reconciliation"]["state"] == "selective_compensation"
    assert memory["revision_compensations"][-1]["recorded_at"] == 123.0


def test_revision_compensation_refuses_unreviewed_or_full_regression_scope():
    memory = {"story_status": {}}
    assert qa_agentic._restore_revision_compensation(
        memory, "rev", impact={"full_regression": True, "preserved_story_ids": ["US001"]},
        prior_status={"US001": "clean"}, reason="unsafe") == {}
    assert qa_agentic._restore_revision_compensation(
        memory, "rev", impact={"full_regression": False},
        prior_status={"US001": "clean"}, reason="missing scope") == {}
    assert memory == {"story_status": {}}


def test_compensated_dismissed_latest_finding_reopens_stale_blocker_without_promoting_clean():
    memory = {
        "story_status": {"US009": "internal_review", "US011": "blocking", "US012": "clean"},
        "qa_findings": [
            {"story": "US009", "title": "Governance audit history contains three approval.required events "
             "for the same follow-up draft instead of exactly one; two entries are visible", "blocking": True},
            {"story": "US011", "title": "The settled product loads five US-011 risks but exposes no reachable "
             "control or workflow to acknowledge a blocker, so mouse operation is unavailable", "blocking": True},
        ],
        "finding_resolutions": [
            {"story": "US009", "title": "Governance audit history contains three approval.required events "
             "for the same follow-up draft instead of exactly one; the contract requires tickets",
             "disposition": "verified_false_positive"},
            {"story": "US011", "title": "The settled product loads five US-011 risks but exposes no reachable "
             "control or workflow to acknowledge a blocker, so mouse and keyboard are unavailable",
             "disposition": "verified_false_positive"},
        ],
    }

    repaired = qa_agentic._reconcile_compensated_false_positive_statuses(memory)

    assert set(repaired) == {"US009", "US011"}
    assert memory["story_status"] == {
        "US009": "incomplete", "US011": "incomplete", "US012": "clean"}
    assert memory["resolution_status_repairs"][-1]["stories"]["US011"]["from"] == "blocking"


def test_compensated_status_repair_does_not_touch_unresolved_or_nonlatest_findings():
    memory = {
        "story_status": {"US001": "blocking", "US002": "blocking"},
        "qa_findings": [
            {"story": "US001", "title": "A historical detailed finding whose exact visible symptom was "
             "dismissed after a complete review and has enough identity bytes"},
            {"story": "US001", "title": "A newer detailed finding whose exact visible symptom remains "
             "unresolved and has enough different identity bytes to remain blocking"},
            {"story": "US002", "title": "Another detailed finding that remains unresolved and has enough "
             "identity bytes to stay blocking without any resolution"},
        ],
        "finding_resolutions": [{
            "story": "US001", "title": "A historical detailed finding whose exact visible symptom was "
            "dismissed after a complete review and has enough identity bytes",
            "disposition": "verified_false_positive"}],
    }

    assert qa_agentic._reconcile_compensated_false_positive_statuses(memory) == {}
    assert memory["story_status"] == {"US001": "blocking", "US002": "blocking"}


def test_resume_reopens_clean_story_when_persistence_receipt_predates_creation():
    create = "Submit one valid enquiry and verify the completed job."
    persist = "Refresh the page and verify the created enquiry and agent job persist."
    memory = {
        "story_status": {"US-002": "clean", "US-003": "clean"},
        "gapfills": {"US-002": 2},
        "story_progress": {"US-002": {"covered": 2, "coverage_total": 2}},
        "results": {
            "41": {"story": "US-002", "result": {
                "story": "US-002", "stop_reason": "coverage-complete",
                "coverage": [
                    {"aspect": create, "covered": True,
                     "proof": {"action_kind": "diagnose_cumulative_progress", "recorded_at": 30}},
                    {"aspect": persist, "covered": True,
                     "proof": {"action_kind": "reload", "recorded_at": 20}},
                ],
            }},
            "42": {"story": "US-003", "result": {
                "story": "US-003", "stop_reason": "coverage-complete",
                "coverage": [{"aspect": "Inspect empty state", "covered": True,
                              "proof": {"action_kind": "inspect", "recorded_at": 10}}],
            }},
        },
    }

    repaired = qa_agentic._reconcile_causal_coverage_statuses(memory)

    assert set(repaired) == {"US-002"}
    assert memory["story_status"] == {"US-002": "incomplete", "US-003": "clean"}
    assert memory["gapfills"]["US-002"] == 0
    assert memory["story_progress"]["US-002"]["covered"] == 1
    result = memory["results"]["41"]["result"]
    assert result["stop_reason"] == "causal-evidence-reopened"
    assert result["coverage"][1]["covered"] is False
    assert memory["causal_status_repairs"][-1]["stories"]["US-002"]["from"] == "clean"


def test_resume_revision_change_does_not_defer_for_processed_or_non_dev_receipts():
    actors = [{"actor_id": 1, "role": "qa-coordinator", "status": "working", "memory": {
        "coverage_revision": "rev-a", "story_status": {"US001": "clean"}}}]

    class Store:
        def actors(self, _run_id, _tenant):
            return actors

        def events(self, _run_id, _tenant):
            return [
                {"id": 1, "frm": 2, "kind": "done", "processed_at": None,
                 "payload": {"result": {"files": ["src/external.js"]}}},
                {"id": 2, "frm": 3, "kind": "tool_result", "processed_at": "already",
                 "payload": {"tool": "dev_fix", "result": {"files": ["src/fixed.js"]}}},
            ]

        def update_actor(self, actor_id, _tenant, **values):
            actors[0]["memory"].update(values.get("memory") or {})

    assert qa_agentic._invalidate_resumed_revision(Store(), 9, "tenant-a", "rev-b") == 1
    event = actors[0]["memory"]["revision_invalidations"][-1]
    assert event["stale_story_status"] == {"US001": "clean"}


def test_runtime_aggregate_admits_next_unseen_batch_from_durable_story_status(monkeypatch):
    stories = _stories(5)
    coordinator = {
        "actor_id": 1, "name": "qa", "role": "qa-coordinator", "kind": "supervisor",
        "supervisor_id": None, "status": "working", "assignment": "QA all stories",
        "memory": {"phase": "delegating", "context": {
            "stories": stories, "story_batch_size": 2, "product_revision": "rev-a",
        }, "coverage_revision": "rev-a",
                   "story_status": {"US001": "clean", "US002": "clean"}},
    }
    children = [
        {"actor_id": 10, "supervisor_id": 1, "role": "qa-explorer", "status": "done"},
        {"actor_id": 11, "supervisor_id": 1, "role": "qa-explorer", "status": "done"},
    ]
    admitted = []
    persisted = []
    monkeypatch.setattr(runtime.store, "actors", lambda *_args, **_kwargs: children)
    monkeypatch.setattr(runtime, "_hire_or_request",
                        lambda _ctx, _actor, specs, _step, *_args: admitted.extend(specs) or "hired")
    monkeypatch.setattr(runtime, "_persist",
                        lambda _ctx, _actor, step, _events: persisted.append(step))
    monkeypatch.setattr(runtime, "_audit", lambda *_args, **_kwargs: None)

    runtime._supervisor_step(
        SimpleNamespace(run_id=99, tenant="tenant-a", repo="/tmp"), coordinator, [])

    assert [spec["tool_args"]["story"]["id"] for spec in admitted] == ["US003", "US004"]
    assert persisted[0].status is None
    assert persisted[0].result is None
    assert persisted[0].memory["gapfills"] == {"US003": 1, "US004": 1}


def test_runtime_report_only_continuation_joins_carried_clean_manifest_without_generic_hires(
        monkeypatch):
    stories = _stories(3)
    coordinator = {
        "actor_id": 1, "name": "qa", "role": "qa-coordinator", "kind": "supervisor",
        "supervisor_id": None, "status": "working", "assignment": "QA all stories",
        "memory": {
            "phase": "new",
            "context": {"stories": stories, "story_batch_size": 3},
            "story_status": {"US001": "clean", "US002": "clean", "US003": "clean"},
        },
    }
    hired_specs = []
    persisted = []
    monkeypatch.setattr(runtime.store, "actors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(runtime, "_hire_or_request",
                        lambda _ctx, _actor, specs, _step, *_args: hired_specs.extend(specs) or "hired")
    monkeypatch.setattr(runtime, "_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_persist",
                        lambda _ctx, _actor, step, _events: persisted.append(step))

    runtime._supervisor_step(
        SimpleNamespace(run_id=99, tenant="tenant-a", repo="/tmp"), coordinator,
        [{"kind": "task", "payload": {"task": "QA all stories"}, "corr_id": "kickoff",
          "frm": None}])

    step = persisted[0]
    assert hired_specs == []
    assert step.status == "done"
    assert step.finish_status == "done"
    assert step.result["passed"] is True
    assert step.result["stories"] == 3
