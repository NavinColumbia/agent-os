from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "qa"))
sys.path.insert(0, str(ROOT / "scripts" / "orchestra"))


def test_process_stalls_route_directly_to_management_not_product_evidence_review():
    import runtime

    assert runtime._qa_internal_review_route({
        "finding": {"kind": "qa_performance_stall", "story": "US-12"},
    }) == "performance_management"
    assert runtime._qa_internal_review_route({
        "finding": {"kind": "bug", "story": "US-12"},
    }) == "evidence_review"


def test_checkpoint_detects_only_unresolved_internal_management():
    import qa_agentic

    coordinator = {"memory": {
        "internal_reviews": [{"review_id": "open-manager"}, {"review_id": "open-authority"}],
        "internal_review_states": {
            "resolved-history": {"status": "human_required", "case_id": "old"},
            "open-manager": {"status": "manager_attention", "case_id": "case-1"},
            "open-authority": {"status": "human_required", "case_id": "case-2",
                               "authority_decision_id": 77},
            "still-working": {"status": "verification_scheduled", "case_id": "case-3"},
        },
    }}

    checkpoint = qa_agentic._internal_management_checkpoint(coordinator)

    assert checkpoint["internal_review_ids"] == ["open-authority", "open-manager"]
    assert checkpoint["qa_review_case_ids"] == ["case-1", "case-2"]
    assert checkpoint["authority_decision_ids"] == [77]
    assert "resolved-history" not in checkpoint["internal_review_states"]


def test_completed_or_actively_working_reviews_do_not_checkpoint():
    import qa_agentic

    assert qa_agentic._internal_management_checkpoint({"memory": {
        "internal_reviews": [],
        "internal_review_states": {"old": {"status": "human_required"}},
    }}) is None
    assert qa_agentic._internal_management_checkpoint({"memory": {
        "internal_reviews": [{"review_id": "active"}],
        "internal_review_states": {"active": {"status": "review_scheduled"}},
    }}) is None


def test_performance_recovery_preserves_evidence_but_resets_only_streak():
    import runtime

    review = {"review_id": "perf-1", "story": "US-12",
              "finding": {"kind": "qa_performance_stall", "story": "US-12"}}
    memory = {
        "internal_reviews": [review],
        "internal_review_states": {"perf-1": {"status": "manager_attention"}},
        "story_status": {"US-12": "internal_review", "US-2": "clean"},
        "gapfills": {"US-12": 3, "US-2": 1},
        "story_progress": {"US-12": {"covered": 2, "covered_aspects": ["a", "b"],
                                        "evidence_signatures": ["proof"], "no_progress": 3}},
    }

    recovered = runtime._qa_apply_performance_recovery(memory, {
        "review_id": "perf-1", "story": "US-12", "action": "rebrief",
        "rationale": "use the repaired wait path and continue the ledger"})

    assert recovered["story"] == "US-12"
    assert memory["internal_reviews"] == []
    assert memory["story_status"] == {"US-12": "incomplete", "US-2": "clean"}
    assert memory["gapfills"] == {"US-12": 0, "US-2": 1}
    assert memory["story_progress"]["US-12"]["covered_aspects"] == ["a", "b"]
    assert memory["story_progress"]["US-12"]["evidence_signatures"] == ["proof"]
    assert memory["story_progress"]["US-12"]["no_progress"] == 0
    assert memory["internal_review_states"]["perf-1"]["status"] == "management_recovery"


def test_fresh_exercised_capability_retires_stale_process_review_only():
    import runtime

    capability = {"review_id": "qa-capability-at", "story": "US-12",
                  "route": "qa-capability-management"}
    evidence_dispute = {"review_id": "qa-review-labels", "story": "US-12",
                        "route": "qa-internal-management"}
    memory = {
        "internal_reviews": [capability, evidence_dispute],
        "internal_review_states": {"qa-capability-at": {"status": "manager_attention"}},
    }

    cleared = runtime._qa_clear_restored_capability_reviews(memory, "US-12", {
        "steps": 8, "stop_reason": "repeated-action-incomplete", "missing_capabilities": [],
    })

    assert cleared == ["qa-capability-at"]
    assert memory["internal_reviews"] == [evidence_dispute]
    assert memory["internal_review_states"]["qa-capability-at"] == {
        "status": "capability_restored", "story": "US-12", "restored_by_steps": 8}
    assert runtime._qa_clear_restored_capability_reviews(memory, "US-12", {
        "steps": 0, "stop_reason": "capability-unavailable",
        "missing_capabilities": [{"capability": "actual-assistive-technology"}],
    }) == []


def test_pending_repairs_prioritize_risk_and_supply_same_story_context():
    import runtime

    selected, remaining = runtime._qa_select_pending_finding([
        {"finding_id": "low-first", "story": "US-10", "severity": "low",
         "title": "punctuation was dropped", "detail": "minor field fidelity"},
        {"finding_id": "other", "story": "US-8", "severity": "high", "blocking": True,
         "title": "retry control absent", "detail": "dead-letter cannot recover"},
        {"finding_id": "critical", "story": "US-10", "severity": "critical", "blocking": True,
         "title": "secret published", "detail": "API token rendered publicly"},
        {"finding_id": "reload", "story": "US-10", "severity": "high", "blocking": True,
         "title": "secret survives reload", "detail": "published token persisted"},
    ])

    assert selected["finding_id"] == "critical"
    assert selected["related_finding_count"] == 2
    assert {item["finding_id"] for item in selected["related_findings"]} == {"low-first", "reload"}
    assert [item["finding_id"] for item in remaining] == ["low-first", "other", "reload"]


def test_pending_repairs_prefer_mutation_ready_adjudication_at_equal_risk():
    import runtime

    confirmed = {
        "finding_id": "confirmed", "story": "US-6", "severity": "high", "blocking": True,
        "title": "timeout path completes too early", "detail": "pending state never lasts ten seconds",
        "_qa_adjudication": {"disposition": "confirmed_defect", "case_id": "qad-1",
                             "review_id": "qa-review-1"},
    }
    selected, remaining = runtime._qa_select_pending_finding([
        {"finding_id": "unreviewed", "story": "US-9", "severity": "high", "blocking": True,
         "title": "approval action absent", "detail": "approval cannot be completed"},
        confirmed,
    ])

    assert selected["finding_id"] == "confirmed"
    assert [item["finding_id"] for item in remaining] == ["unreviewed"]


def test_grounded_fix_queue_preempts_ordinary_gapfill_continuation():
    import runtime

    assert runtime._qa_fix_queue_preempts_continuation({
        "pending_dev_findings": [{"story": "US-011", "severity": "high"}]}) is True
    assert runtime._qa_fix_queue_preempts_continuation(
        {"pending_dev_findings": []}, active_dev=True) is True
    assert runtime._qa_fix_queue_preempts_continuation(
        {"pending_dev_findings": []}, active_dev=False) is False


def test_story_continuations_coalesce_recheck_reasons_without_losing_story_coverage():
    import runtime

    compacted = runtime._qa_compact_story_continuations([
        {"review_id": "gapfill:US-012:1", "story": "US-012", "task": "finish ledger"},
        {"review_id": "qa-review-labels", "story": "US-012",
         "finding": {"finding_id": "labels", "story": "US-012"}},
        {"review_id": "qa-review-focus", "story": "US-012",
         "finding": {"finding_id": "focus", "story": "US-012"}},
        {"review_id": "gapfill:US-006:1", "story": "US-006"},
        {"review_id": "harness-prerequisite:timeout", "story": "US-006"},
    ])

    assert [item["story"] for item in compacted] == ["US-012", "US-006"]
    assert compacted[0]["review_id"] == "gapfill:US-012:1"
    assert compacted[0]["coalesced_review_ids"] == ["qa-review-labels", "qa-review-focus"]
    assert compacted[0]["coalesced_continuation_count"] == 3
    assert compacted[1]["coalesced_review_ids"] == ["harness-prerequisite:timeout"]


def test_clean_story_retires_queued_continuation_but_unfinished_and_unknown_work_survive():
    import runtime

    compacted = runtime._qa_compact_story_continuations([
        {"review_id": "gapfill:US-011:1", "story": "US-011"},
        {"review_id": "gapfill:US-010:1", "story": "US-010"},
        {"review_id": "unknown-a"},
        {"review_id": "unknown-a"},
        {"review_id": "unknown-b"},
    ], {"US-011": "clean", "US-010": "incomplete"})

    assert compacted == [
        {"review_id": "gapfill:US-010:1", "story": "US-010"},
        {"review_id": "unknown-a"},
        {"review_id": "unknown-b"},
    ]


def test_terminal_finding_resolutions_retire_only_exact_internal_review_work():
    import runtime

    reviews, states = runtime._qa_compact_resolved_internal_reviews(
        [{"review_id": "fixed-review"}, {"review_id": "false-positive"},
         {"review_id": "external-authority"}, {"review_id": "still-open"}],
        {"fixed-review": {"status": "manager_attention"},
         "false-positive": {"status": "review_scheduled"},
         "external-authority": {"status": "human_required"},
         "still-open": {"status": "verification_scheduled"}},
        [{"review_id": "fixed-review", "disposition": "confirmed_defect"},
         {"review_id": "false-positive", "disposition": "verified_false_positive"},
         {"review_id": "external-authority", "disposition": "needs_named_external_authority"}],
    )

    assert reviews == [{"review_id": "external-authority"}, {"review_id": "still-open"}]
    assert set(states) == {"external-authority", "still-open"}


def test_terminal_false_review_cannot_leave_story_orphaned_in_internal_review():
    import runtime

    memory = {
        "story_status": {"US-003": "internal_review", "US-008": "internal_review",
                         "US-009": "internal_review", "US-001": "clean"},
        "internal_reviews": [{"review_id": "still-open", "story": "US-009"}],
        "pending_story_continuations": [],
        "pending_dev_findings": [],
        "finding_resolutions": [
            {"review_id": "review-3", "story": "US-003",
             "disposition": "verified_false_positive"},
            {"review_id": "review-8", "story": "US-008",
             "disposition": "superseded_by_current_revision"},
            {"review_id": "review-9", "story": "US-009",
             "disposition": "verified_false_positive"},
        ],
    }

    repaired = runtime._qa_repair_orphan_internal_review_statuses(memory)

    assert repaired == ["US-003", "US-008"]
    assert memory["story_status"] == {
        "US-003": "incomplete", "US-008": "incomplete",
        "US-009": "internal_review", "US-001": "clean"}
    assert [item["story"] for item in memory["pending_story_continuations"]] == [
        "US-003", "US-008"]
    assert memory["resolution_status_repairs"][-1]["stories"] == ["US-003", "US-008"]


def test_confirmed_defect_status_is_not_reopened_as_evidence_work():
    import runtime

    memory = {
        "story_status": {"US-003": "internal_review"},
        "internal_reviews": [],
        "finding_resolutions": [{"review_id": "review-3", "story": "US-003",
                                  "disposition": "confirmed_defect"}],
    }

    assert runtime._qa_repair_orphan_internal_review_statuses(memory) == []
    assert memory["story_status"]["US-003"] == "internal_review"


def test_invalid_recovery_reopens_only_contaminated_stories_and_fences_sources():
    import runtime

    finding = {"finding_id": "qaf-stale", "story": "US-010", "title": "legacy state leak"}
    memory = {
        "coverage_revision": "rev-new",
        "story_status": {"US-001": "clean", "US-010": "blocking", "US-011": "clean"},
        "gapfills": {"US-010": 2, "US-011": 1},
        "pending_dev_findings": [finding],
        "qa_findings": [finding],
        "internal_reviews": [{"review_id": "review-stale", "story": "US-010"}],
        "internal_review_states": {"review-stale": {"status": "manager_review"}},
    }

    stale = runtime._qa_invalidate_recovery_sources(
        memory, ["US-010"], [17], finding_ids=["qaf-stale"],
        reason="test inherited old revision")

    assert stale == ["US-010"]
    assert memory["story_status"] == {"US-001": "clean", "US-011": "clean"}
    assert memory["gapfills"] == {"US-011": 1}
    assert memory["invalid_recovery_actor_ids"] == ["17"]
    assert memory["pending_dev_findings"] == []
    assert memory["internal_reviews"] == []
    assert memory["internal_review_states"]["review-stale"]["status"] == \
        "superseded_by_invalid_recovery"
    assert memory["finding_resolutions"][-1]["disposition"] == \
        "superseded_by_invalid_recovery"


def test_full_story_recovery_skips_focused_and_legacy_contaminated_ledgers(tmp_path):
    import runtime

    full_state = tmp_path / "full-state.json"
    focused_state = tmp_path / "focused-state.json"
    contaminated_state = tmp_path / "contaminated-state.json"
    for path in (full_state, focused_state, contaminated_state):
        path.write_text("{}")

    recovered = runtime._qa_prior_recovery({
        "1": {"story": "US-011", "result": {
            "title": "Operate CEO risk dashboard", "resume_state_path": str(full_state),
            "coverage": [{"aspect": "Open the CEO command view", "covered": True,
                          "explicit": True}],
            "steps_detail": [{"verdict": "match", "covers": ["Open the CEO command view"]}],
        }},
        "2": {"story": "US-011", "result": {
            "title": "Focused reproduction — Operate CEO risk dashboard",
            "recovery_scope": "focused", "resume_state_path": str(focused_state),
            "coverage": [{"aspect": "Reproduce this exact reported behavior", "covered": True,
                          "explicit": True}],
        }},
        # One pre-fence full-story result inherited a focused ledger. Its normal title cannot make that
        # incompatible contract safe, so the focused template markers must quarantine it too.
        "3": {"story": "US-011", "result": {
            "title": "Operate CEO risk dashboard", "resume_state_path": str(contaminated_state),
            "coverage": [{
                "aspect": "Story step 1: Establish only the minimum valid product state and prerequisites "
                          "needed for this exact finding",
                "covered": True, "explicit": True,
            }],
        }},
    }, "US-011")

    assert recovered == {
        "resume_covered": ["Open the CEO command view"],
        "resume_coverage": [{"aspect": "Open the CEO command view", "covered": True,
                             "explicit": True}],
        "resume_state_path": str(full_state),
        "resume_steps_detail": [{"verdict": "match", "covers": ["Open the CEO command view"]}],
    }


def test_false_positive_retest_does_not_resume_actionable_terminal_browser_state(tmp_path):
    import runtime

    actionable_state = tmp_path / "post-drain.json"
    clean_state = tmp_path / "pre-action.json"
    actionable_state.write_text("{}")
    clean_state.write_text("{}")
    aspect = "Drain one queued item and render the populated panels"
    results = {
        "1": {"story": "US-003", "result": {
            "bugs": 0, "resume_state_path": str(clean_state),
            "coverage": [{"aspect": "empty state is valid", "covered": True}],
            "steps_detail": [{"verdict": "pass", "covers": ["empty state is valid"]}],
        }},
        "2": {"story": "US-003", "result": {
            "bugs": 1, "resume_state_path": str(actionable_state),
            "coverage": [{"aspect": aspect, "covered": False}],
            "steps_detail": [{"verdict": "bug", "covers": [],
                              "action": "click Drain queue"}],
        }},
    }

    ordinary = runtime._qa_prior_recovery(results, "US-003")
    skeptical = runtime._qa_prior_recovery(
        results, "US-003", allow_actionable_checkpoint=False)

    assert ordinary["resume_state_path"] == str(actionable_state)
    assert skeptical["resume_state_path"] == str(clean_state)


def test_focused_review_recovery_keeps_best_same_revision_state_and_ledger(tmp_path):
    import runtime

    weak_state = tmp_path / "weak-state.json"
    strong_state = tmp_path / "strong-state.json"
    stale_state = tmp_path / "stale-state.json"
    for path in (weak_state, strong_state, stale_state):
        path.write_text("{}")

    def child(actor_id, revision, state, covered):
        coverage = [
            {"aspect": f"focused step {index}", "covered": index <= covered,
             "explicit": True,
             **({"proof": {"engine": "codex", "action_kind": "click"}}
                if index <= covered else {})}
            for index in range(1, 5)
        ]
        steps = [{"verdict": "match", "covers": [f"focused step {index}"]}
                 for index in range(1, covered + 1)]
        return {"actor_id": actor_id, "role": "qa-explorer", "status": "done",
                "memory": {"context": {"tool_args": {
                    "_qa_review_id": "review-a", "product_revision": revision}}},
                "result": {"status": "done", "result": {
                    "recovery_scope": "focused", "resume_state_path": str(state),
                    "coverage": coverage, "steps_detail": steps}}}

    children = {
        41: child(41, "revision-a", strong_state, 3),
        # This newer retry made less progress; it must not erase the stronger checkpoint.
        42: child(42, "revision-a", weak_state, 1),
        # More coverage against different bytes is not admissible.
        43: child(43, "revision-old", stale_state, 4),
        44: child(44, "revision-a", weak_state, 4),
    }
    children[44]["memory"]["context"]["tool_args"]["_qa_review_id"] = "review-b"

    recovered = runtime._qa_review_recovery(
        children, {}, "review-a", "revision-a")

    assert recovered["resume_state_path"] == str(strong_state)
    assert recovered["resume_covered"] == [
        "focused step 1", "focused step 2", "focused step 3"]
    assert len(recovered["resume_coverage"]) == 4
    assert len(recovered["resume_steps_detail"]) == 3


def test_focused_review_recovery_rejects_ledger_saved_after_unreseeded_reset(tmp_path):
    import runtime

    tainted_state = tmp_path / "tainted-state.json"
    current_state = tmp_path / "current-state.json"
    tainted_state.write_text("{}")
    current_state.write_text("{}")
    story = {"steps": ["Load the CEO risk fixture.", "Exercise the disputed control."]}

    def child(actor_id, state, steps, covered):
        return {"actor_id": actor_id, "role": "qa-explorer", "status": "done",
                "memory": {"context": {"tool_args": {
                    "_qa_review_id": "review-a", "product_revision": "revision-a",
                    "story": story}}},
                "result": {"status": "done", "result": {
                    "recovery_scope": "focused", "resume_state_path": str(state),
                    "coverage": [{"aspect": f"focused step {index}",
                                  "covered": index <= covered, "explicit": True,
                                  **({"proof": {"engine": "codex", "action_kind": "click"}}
                                     if index <= covered else {})}
                                 for index in range(1, 5)],
                    "steps_detail": steps}}}

    tainted = child(51, tainted_state, [
        {"action": "click idx=1", "actual": "target='Load CEO risk'", "verdict": "match",
         "covers": ["focused step 1"]},
        {"action": "reset_storage ='http://app'", "actual": "driver_ok=True", "verdict": "match"},
        {"action": "click idx=4", "actual": "target='Drain queue'", "verdict": "match",
         "covers": ["focused step 2", "focused step 3"]},
    ], 3)
    current = child(52, current_state, [
        {"action": "reset_storage ='http://app'", "actual": "driver_ok=True", "verdict": "match"},
        {"action": "click idx=1", "actual": "target='Load CEO risk'", "verdict": "match",
         "covers": ["focused step 1"]},
    ], 1)

    recovered = runtime._qa_review_recovery(
        {51: tainted, 52: current}, {}, "review-a", "revision-a")

    assert recovered["resume_state_path"] == str(current_state)
    assert recovered["resume_covered"] == ["focused step 1"]


def test_pending_repairs_collapse_strict_same_observation_paraphrases_not_claim_specific_failures():
    import runtime

    inventory_a = {
        "finding_id": "inventory-scroll", "story": "US-010", "severity": "medium",
        "title": "The settled staff workflow does not reveal all five seeded claims with each claim status",
        "detail": "Only aggregate metrics and audit IDs are shown, so staff cannot inspect public private visibility and evidence prerequisites.",
        "expected": "Five seeded claims expose status, visibility, and evidence prerequisites for inspection.",
    }
    inventory_b = {
        "finding_id": "inventory-click", "story": "US-010", "severity": "medium",
        "title": "Five claims loaded but staff cannot inspect every seeded claim status",
        "detail": "The staff workflow exposes aggregate metrics and audit IDs instead of public private visibility and evidence prerequisites for all five claims.",
        "expected": "Every seeded claim exposes its status, visibility, and evidence prerequisites for inspection.",
    }
    draft = {
        "finding_id": "draft", "story": "US-010", "severity": "medium",
        "title": "Publication blocker identifies the wrong claim",
        "detail": "Submitting claim_us010draft01 produced a blocker for claim_us010unverif.",
    }
    private = {
        "finding_id": "private", "story": "US-010", "severity": "medium",
        "title": "Publication blocker identifies the wrong claim",
        "detail": "Submitting claim_us010private produced a blocker for claim_us010unverif.",
    }

    assert runtime._qa_same_observation(inventory_a, inventory_b)
    assert not runtime._qa_same_observation(draft, private)
    selected, remaining = runtime._qa_select_pending_finding([inventory_a, inventory_b])
    assert selected["duplicate_finding_count"] == 1
    assert selected["duplicate_findings"][0]["finding_id"] == "inventory-click"
    assert remaining == []


def test_internal_review_reuses_active_semantic_observation_across_new_browser_provenance():
    import runtime

    first = {
        "story": "US-011",
        "finding": {
            "story": "US-011",
            "title": "Inbound leads omits per-status counts and aged lead totals",
            "detail": "The panel lists enquiries but has no new reviewing qualified waitlisted closed blocked counts or leads over three days.",
            "evidence_provenance": {"manifest_sha256": "first-browser"},
        },
    }
    replay = {
        "story": "US-011",
        "finding": {
            "story": "US-011",
            "title": "The settled Inbound leads panel does not expose status counts or aged-lead totals",
            "detail": "Enquiry rows are present but new reviewing qualified waitlisted closed blocked counts and leads over three days are absent.",
            "evidence_provenance": {"manifest_sha256": "second-browser"},
        },
    }
    memory = {}

    original = runtime._qa_record_internal_review(memory, first)
    duplicate = runtime._qa_record_internal_review(memory, replay)

    assert duplicate["review_id"] == original["review_id"]
    assert len(memory["internal_reviews"]) == 1


def test_legacy_pending_queue_compacts_transitive_repeated_browser_observations():
    import runtime

    focus_a = {
        "finding_id": "focus-a", "story": "US-012", "severity": "medium",
        "title": "Keyboard traversal loses the visible focus indicator on an internal Tab stop of the Start date control",
        "detail": "The active date input remains focused but outline becomes none during the trusted forward traversal.",
    }
    focus_b = {
        "finding_id": "focus-b", "story": "US-012", "severity": "medium",
        "title": "During forward keyboard traversal the Start date input received a Tab focus stop with no visible focus indicator",
        "detail": "The traversal receipt keeps the date control active while its focus outline is none.",
    }
    focus_c = {
        "finding_id": "focus-c", "story": "US-012", "severity": "medium",
        "title": "Keyboard traversal lost visible focus on the Start date input",
        "detail": "A trusted Tab step leaves the date control focused without an outline or visible focus indicator.",
    }
    scroll_jump = {
        "finding_id": "scroll", "story": "US-012", "severity": "medium",
        "title": "Tab from the consent checkbox causes a large backward scroll jump",
        "detail": "Focus reaches Claim reference but the mobile page jumps from the enquiry to the staff console.",
    }

    compacted = runtime._qa_compact_pending_findings([focus_a, scroll_jump, focus_b, focus_c])

    assert len(compacted) == 2
    focus_cluster = next(item for item in compacted if item["finding_id"].startswith("focus-"))
    assert focus_cluster["duplicate_finding_count"] == 2
    assert {item["finding_id"] for item in focus_cluster["duplicate_findings"]} == {
        "focus-a", "focus-b", "focus-c"} - {focus_cluster["finding_id"]}
    assert any(item["finding_id"] == "scroll" for item in compacted)


def test_agentic_driver_immediately_halts_a_quiescent_management_wait(monkeypatch, tmp_path):
    import qa_agentic

    state = {"status": "running"}
    coordinator = {"actor_id": 10, "role": "qa-coordinator", "status": "working", "result": None,
                   "memory": {
                       "internal_reviews": [{"review_id": "review-1", "story": "US-1"}],
                       "internal_review_states": {
                           "review-1": {"status": "manager_attention", "case_id": "case-1"}},
                       "story_status": {"US-1": "internal_review"},
                   }}
    store = types.ModuleType("store")
    store.start_run = lambda tenant, vision: {"run_id": 12}
    store.spawn_actor = lambda *args, **kwargs: {"actor_id": 10}
    store.emit = lambda *args, **kwargs: None
    store.run = lambda *args, **kwargs: {"run_id": 12, "status": state["status"]}
    store.actors = lambda *args, **kwargs: [coordinator]
    store.pending_count = lambda *args, **kwargs: 0
    store.events = lambda *args, **kwargs: []

    def finish_run(run_id, status, result, tenant_id=None):
        state.update({"status": status, "result": result})
        return {"run_id": run_id, "status": status}

    store.finish_run = finish_run
    runtime = types.ModuleType("runtime")
    runtime.calls = 0

    def run_org(*args, **kwargs):
        runtime.calls += 1

    runtime.run_org = run_org
    jobrunner = types.ModuleType("jobrunner")
    jobrunner.set_run_deadline = lambda *args, **kwargs: None
    jobrunner.reconcile_parked = lambda *args, **kwargs: 0
    jobrunner.active_for_run = lambda *args, **kwargs: 0
    jobrunner.cancel_run = lambda *args, **kwargs: 0
    jobrunner.clear_run_deadline = lambda *args, **kwargs: None
    pulse = types.ModuleType("pulse")
    pulse.start = pulse.beat = pulse.finish = lambda *args, **kwargs: None
    artifacts = types.ModuleType("artifacts")
    artifacts.run_dir = lambda product, started: tmp_path / "evidence"
    qa_run = types.ModuleType("qa_run")
    qa_run._persist_run = lambda *args, **kwargs: 1
    qa_run.write_verdict = lambda *args, **kwargs: str(tmp_path / "verdict.json")
    for name, module in (("store", store), ("runtime", runtime), ("jobrunner", jobrunner),
                         ("pulse", pulse), ("artifacts", artifacts), ("qa_run", qa_run)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(qa_agentic, "_terminate_owned_children", lambda *args, **kwargs: 0)

    out = qa_agentic.run_agentic_qa(
        "http://app", "vision", product="p", repo=str(tmp_path), tenant="tenant",
        stories=[{"id": "US-1"}], drive_budget_s=1200, file_findings=False)

    assert runtime.calls == 1
    assert state["status"] == "halted"
    assert state["result"]["internal_management_wait"] is True
    assert out["report"]["internal_review_ids"] == ["review-1"]
    assert out["report"]["verdict"].startswith("INTERNAL MANAGEMENT")


def test_factory_and_controller_gate_preserve_management_routing(monkeypatch, tmp_path):
    import factory
    import loopcontroller

    facts = {
        "passed": False, "total_stories": 3, "stories": 3, "blocking_open": 1, "open_bugs": 1,
        "verdict": "INTERNAL MANAGEMENT", "safety_limited": True, "timed_out": True,
        "internal_management_wait": True,
        "internal_review_states": {"review-1": {"status": "human_required"}},
        "internal_review_ids": ["review-1"], "qa_review_case_ids": ["case-1"],
        "authority_decision_ids": [91],
    }
    fake_qa_run = types.ModuleType("qa_run")
    fake_qa_run.qa_run = lambda *args, **kwargs: {"report": facts}
    monkeypatch.setitem(sys.modules, "qa_run", fake_qa_run)

    factory_result = factory.run_agentic_web_qa(
        str(tmp_path), "p", "vision", "summary", target_url="http://app")
    assert factory_result["internal_management_wait"] is True
    assert factory_result["internal_review_ids"] == ["review-1"]
    assert factory_result["qa_review_case_ids"] == ["case-1"]
    assert factory_result["authority_decision_ids"] == [91]

    monkeypatch.setattr(loopcontroller.factory, "run_grounded_qa", lambda *args, **kwargs: factory_result)
    monkeypatch.setitem(sys.modules, "devserve", types.SimpleNamespace(
        up=lambda product: {"url": "http://app"}))
    gate = loopcontroller.qa_gate("p", platform="web")
    assert gate["internal_management_wait"] is True
    assert gate["internal_review_states"] == facts["internal_review_states"]
    assert gate["authority_decision_ids"] == [91]


def test_controller_parks_management_checkpoint_without_rotating_or_generic_gate(monkeypatch):
    import loopcontroller as lc

    state = {"thread_id": 42, "tenant_id": "tenant", "phase": "TESTQA", "awaiting": None,
             "product": "product", "qa_checkpoint_count": 4, "qa_last_completed": 3,
             "qa_no_progress_count": 1}
    changes, reports, audits, cleared = [], [], [], []
    monkeypatch.setattr(lc, "_st", lambda thread_id: dict(state))
    monkeypatch.setattr(lc, "_set", lambda thread_id, **kwargs: changes.append(kwargs))
    monkeypatch.setattr(lc, "_job_clear", lambda thread_id: cleared.append(thread_id))
    monkeypatch.setattr(lc, "_report", lambda *args, **kwargs: reports.append((args, kwargs)))
    monkeypatch.setattr(lc.audit, "append", lambda **kwargs: audits.append(kwargs))
    monkeypatch.setattr(lc, "_qa_manager_decision", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("a waiting dispute must not rotate through the shift manager")))
    monkeypatch.setattr(lc, "_to", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("management wait must not change phase")))
    monkeypatch.setitem(sys.modules, "productregistry", types.SimpleNamespace(record_phase=lambda *a, **k: None))

    lc.advance(42, job_result={
        "qa_ok": False, "safety_limited": True, "internal_management_wait": True,
        "internal_review_ids": ["review-1"], "qa_review_case_ids": ["case-1"],
        "authority_decision_ids": [91],
        "internal_review_states": {"review-1": {"status": "human_required"}},
    })

    assert cleared == [42]
    assert changes == [{"awaiting": "internal_management"}]
    assert len(reports) == 1 and reports[0][1]["urgent"] is False
    assert reports[0][0][3]["kind"] == "qa_internal_management"
    assert audits[0]["decision"] == "internal_management"
