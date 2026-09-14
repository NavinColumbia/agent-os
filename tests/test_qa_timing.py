import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "orchestra"))
import qatiming
import jobrunner
import runtime as orchestra_runtime
import loopcontroller


def test_resumed_dev_verification_combines_matching_inherited_receipts(monkeypatch, tmp_path):
    """A process rotation may add zero actions when inherited proof already closes the ledger."""
    from qa import dev_loop

    state = tmp_path / "storage-state.json"
    state.write_text('{"cookies":[],"origins":[]}')
    step = "Inspect the restored finding-time state."
    aspect = f"Story step 1.1: {step}"
    coverage = [{"aspect": aspect, "covered": True, "explicit": True}]
    receipt = {
        "step": 0,
        "action": {"cmd": "inspect_surfaces", "targets": ["Staff console"]},
        "verdict": {"verdict": "pass", "matches_expected": True},
        "covers": [aspect],
    }

    class ResumedExplorer:
        def __init__(self, *_args, **kwargs):
            self.resume_state_path = kwargs.get("resume_state_path")
            self.coverage = coverage
            self.stop_reason = "coverage-complete"
            self.infrastructure_error = None
            self.missing_capabilities = []
            self.bugs = []
            self.artifact_dir = None

        def explore(self, _story, **kwargs):
            assert kwargs["resume_coverage"] == coverage
            assert kwargs["resume_steps_detail"] == [receipt]
            return []  # no replay is necessary; the restored ledger is already complete

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "qa_explorer", types.SimpleNamespace(Explorer=ResumedExplorer))
    report = dev_loop.dev_self_qa(
        "http://app", "vision",
        [{"id": "US-003", "category": "focused-regression", "steps": [step]}],
        browser_state_path=str(state), resume_covered=[aspect], resume_coverage=coverage,
        resume_steps_detail=[receipt], return_report=True)

    assert report["complete"] is True
    assert report["stories"][0]["evidence_diagnostics"]["proven"] == 1
    assert report["steps_detail"] == [receipt]


def test_default_eta_matches_full_safe_qa_slice():
    assert qatiming.drive_budget_s({}) == 1500
    assert qatiming.slice_eta_min({}) == 25
    assert not 3 <= qatiming.slice_eta_min({}) <= 8


def test_eta_rounds_up_and_respects_configured_budget():
    assert qatiming.slice_eta_min({"AOS_QA_DRIVE_BUDGET_S": "1501"}) == 26
    assert qatiming.slice_eta_min({"AOS_QA_DRIVE_BUDGET_S": "600"}) == 10


def test_controller_and_console_publish_the_same_full_slice_eta():
    import console
    import loopcontroller
    expected = qatiming.slice_eta_min()
    assert expected >= 25
    assert loopcontroller._PHASE_ETA_DEFAULT["TESTQA"] == expected
    assert loopcontroller._estimate_runtime("TESTQA") == expected
    assert console._PHASE_ETA_MIN["TESTQA"] == expected
    assert console._job_eta_min("TESTQA", "qa") == expected
    assert loopcontroller._worker_eta_floor("qa") == expected
    assert loopcontroller._worker_eta_floor("build") is None
    assert loopcontroller._sla_pageable_phase("TESTQA") is False
    assert loopcontroller._sla_pageable_phase("IMPLEMENT") is True


def test_live_qa_progress_is_story_release_state_not_historical_session_count():
    snapshot = loopcontroller._qa_story_snapshot({
        "context": {"stories": [{"id": f"US-{index:03d}"} for index in range(1, 5)]},
        "story_status": {"US-001": "clean", "US-002": "blocking",
                         "US-003": "incomplete"},
    })

    assert snapshot["clean"] == ["US-001"]
    assert snapshot["blocking"] == ["US-002"]
    assert snapshot["incomplete"] == ["US-003", "US-004"]
    assert snapshot["recorded_incomplete"] == ["US-003"]
    assert snapshot["missing"] == ["US-004"]


def test_qa_headline_progress_cannot_saturate_from_elapsed_time():
    subprogress = {"qa_stories_total": 12, "subprogress_pct": 17}

    assert loopcontroller._headline_progress_pct("TESTQA", 99, subprogress) == 17
    assert loopcontroller._headline_progress_pct("IMPLEMENT", 99, subprogress) == 99
    assert loopcontroller._headline_progress_pct(
        "TESTQA", 99, {"qa_stories_total": 12, "subprogress_pct": 100}) == 99


def test_dev_fix_runway_resolves_the_live_progress_lease(monkeypatch):
    from qa import dev_loop

    now = 10_000.0
    monkeypatch.setattr(dev_loop.time, "time", lambda: now)
    live = {"deadline": now + 30}
    deadline = lambda: live["deadline"]

    assert dev_loop._external_stage_has_runway(deadline, required=60) is False
    live["deadline"] = now + 120
    assert dev_loop._external_stage_has_runway(deadline, required=60) is True


def test_new_subordinate_progress_renews_shift_but_duplicate_heartbeat_does_not(monkeypatch):
    run_id, tenant = 424242, "qa-progress-test"
    monkeypatch.setenv("AOS_QA_PROGRESS_LEASE_S", "900")
    jobrunner.set_run_deadline(run_id, tenant, 200.0)
    try:
        assert jobrunner.note_run_progress(
            run_id, tenant, signature="story:step-1", phase="explore", now=100.0) == 1000.0
        assert jobrunner.note_run_progress(
            run_id, tenant, signature="story:step-1", phase="explore", now=500.0) == 1000.0
        assert jobrunner.note_run_progress(
            run_id, tenant, signature="story:step-2", phase="explore", now=500.0) == 1400.0
        ctx = orchestra_runtime._Ctx(run_id, tenant, ".", None, 60, None, deadline=200.0)
        assert ctx.current_deadline() == 1400.0
    finally:
        jobrunner.clear_run_deadline(run_id, tenant)


def test_alternating_stalled_subordinates_cannot_renew_each_other(monkeypatch):
    run_id, tenant = 424243, "qa-progress-multi-test"
    monkeypatch.setenv("AOS_QA_PROGRESS_LEASE_S", "900")
    jobrunner.set_run_deadline(run_id, tenant, 200.0)
    try:
        assert jobrunner.note_run_progress(
            run_id, tenant, signature="step-1", phase="explore",
            details={"story": "US-1"}, now=100.0) == 1000.0
        assert jobrunner.note_run_progress(
            run_id, tenant, signature="step-1", phase="explore",
            details={"story": "US-2"}, now=200.0) == 1100.0
        assert jobrunner.note_run_progress(
            run_id, tenant, signature="step-1", phase="explore",
            details={"story": "US-1"}, now=800.0) == 1100.0
    finally:
        jobrunner.clear_run_deadline(run_id, tenant)


def test_replaying_an_older_signature_cannot_renew_a_story_lease(monkeypatch):
    run_id, tenant = 424245, "qa-progress-replay-test"
    monkeypatch.setenv("AOS_QA_PROGRESS_LEASE_S", "900")
    jobrunner.set_run_deadline(run_id, tenant, 200.0)
    try:
        assert jobrunner.note_run_progress(
            run_id, tenant, signature="scroll", phase="explore",
            details={"story": "US-9"}, now=100.0) == 1000.0
        assert jobrunner.note_run_progress(
            run_id, tenant, signature="inspect", phase="explore",
            details={"story": "US-9"}, now=200.0) == 1100.0
        assert jobrunner.note_run_progress(
            run_id, tenant, signature="scroll", phase="explore",
            details={"story": "US-9"}, now=800.0) == 1100.0
    finally:
        jobrunner.clear_run_deadline(run_id, tenant)


def test_new_progress_is_projected_to_outer_supervisor_once(monkeypatch):
    run_id, tenant = 424244, "qa-progress-projection-test"
    projected = []
    monkeypatch.setattr(jobrunner, "_persist_controller_progress",
                        lambda *args: projected.append(args))
    jobrunner.set_run_deadline(run_id, tenant, 1.0)
    try:
        jobrunner.note_run_progress(run_id, tenant, signature="US-1:step-1", phase="evaluate",
                                    details={"story": "US-1"})
        jobrunner.note_run_progress(run_id, tenant, signature="US-1:step-1", phase="evaluate",
                                    details={"story": "US-1"})
        assert len(projected) == 1, "duplicate heartbeats must not masquerade as meaningful progress"
        assert projected[0][0:3] == (run_id, tenant, "US-1:step-1")
    finally:
        jobrunner.clear_run_deadline(run_id, tenant)


def test_qa_reaper_uses_progress_silence_not_absolute_age(monkeypatch):
    import uuid
    import psycopg
    import loopcontroller

    loopcontroller._ensure()
    base = 980000 + uuid.uuid4().int % 10000
    fresh_id = stale_id = None
    monkeypatch.setattr(loopcontroller, "QA_HARD_CEILING_MIN", 30)
    monkeypatch.setattr(loopcontroller, "QA_PROGRESS_STALL_MIN", 30)
    try:
        with psycopg.connect(loopcontroller.DB) as c, c.cursor() as cur:
            for offset, progress in ((0, "now()"), (1, "now()-interval '2 hours'")):
                cur.execute(f"""INSERT INTO controller_jobs
                                  (thread_id,tenant_id,phase,kind,status,started_at,heartbeat_at,
                                   progress_at,execution_scope)
                               VALUES (%s,'qa-progress-reaper','TESTQA','qa','running',
                                       now()-interval '2 hours',now(),{progress},'test')
                               RETURNING id""", (base + offset,))
                if offset == 0:
                    fresh_id = cur.fetchone()[0]
                else:
                    stale_id = cur.fetchone()[0]
            c.commit()
        loopcontroller._reap_dead_jobs(thread_ids=[base, base + 1], execution_scope="test")
        with psycopg.connect(loopcontroller.DB) as c, c.cursor() as cur:
            cur.execute("SELECT id,status FROM controller_jobs WHERE id=ANY(%s)",
                        ([fresh_id, stale_id],))
            states = dict(cur.fetchall())
        assert states[fresh_id] == "running", "healthy progress may outlive the nominal QA ceiling"
        assert states[stale_id] == "failed", "old heartbeat alone cannot conceal a no-progress runaway"
    finally:
        with psycopg.connect(loopcontroller.DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM controller_jobs WHERE id=ANY(%s)",
                        ([x for x in (fresh_id, stale_id) if x is not None],))
            c.commit()


def test_story_recovery_is_progress_governed_not_attempt_governed():
    assert orchestra_runtime._qa_gapfill_candidates(
        {"US-1": "incomplete"}, ["US-1"], {"US-1": 500}, 0) == ["US-1"]
    memory = {}
    empty = {"coverage": [{"aspect": "a", "covered": False}], "steps_detail": []}
    first = orchestra_runtime._qa_story_progress(memory, "US-1", empty)
    assert first["advanced"] is False and first["no_progress"] == 1
    advanced = orchestra_runtime._qa_story_progress(
        memory, "US-1", {"coverage": [{"aspect": "a", "covered": True}],
                          "steps_detail": [{"action": "type", "expected": "field accepts text"}]})
    assert advanced["advanced"] is True and advanced["no_progress"] == 0
    stalled = orchestra_runtime._qa_story_progress(
        memory, "US-1", {"coverage": [{"aspect": "a", "covered": True}],
                          "steps_detail": [{"action": "type", "expected": "field accepts text"},
                                           {"action": "type", "expected": "field accepts text"}]})
    assert stalled["advanced"] is False and stalled["no_progress"] == 1


def test_story_progress_ignores_rephrased_expectations_and_ephemeral_element_indexes():
    memory = {}
    first = orchestra_runtime._qa_story_progress(memory, "US-9", {
        "coverage": [{"aspect": "deny send before approval", "covered": False}],
        "steps_detail": [{
            "action": {"cmd": "scroll", "idx": 55, "value": "600"},
            "expected": "Reveal the approval controls.",
            "actual": {"url": "http://app/"},
            "covers": ["deny send before approval"],
            "verdict": {"verdict": "pass", "matches_expected": True},
        }],
    })
    repeated = orchestra_runtime._qa_story_progress(memory, "US-9", {
        "coverage": [{"aspect": "deny send before approval", "covered": False}],
        "steps_detail": [{
            "action": {"cmd": "scroll", "idx": 83, "value": "600"},
            "expected": "The page should expose governance diagnostics below.",
            "actual": {"url": "http://app/"},
            "covers": ["deny send before approval"],
            "verdict": {"verdict": "pass", "matches_expected": True},
        }],
    })
    genuinely_new = orchestra_runtime._qa_story_progress(memory, "US-9", {
        "coverage": [{"aspect": "deny send before approval", "covered": False}],
        "steps_detail": [{
            "action": {"cmd": "click", "idx": 91, "target_text": "Attempt send", "role": "button"},
            "expected": "The unauthorised send is rejected.",
            "actual": {"url": "http://app/"},
            "covers": ["deny send before approval"],
            "verdict": {"verdict": "pass", "matches_expected": True},
        }],
    })

    assert first["advanced"] is True
    assert repeated["advanced"] is False and repeated["no_progress"] == 1
    assert genuinely_new["advanced"] is True and genuinely_new["no_progress"] == 0


def test_returned_full_ledger_replaces_obsolete_paraphrased_coverage_labels():
    memory = {"story_progress": {"US-9": {
        "covered": 3, "coverage_total": 2,
        "covered_aspects": ["approve then send", "approve and send", "old extra paraphrase"],
        "evidence_signatures": [], "no_progress": 0,
    }}}

    progress = orchestra_runtime._qa_story_progress(memory, "US-9", {
        "coverage": [
            {"aspect": "create pending approval", "covered": True},
            {"aspect": "deny send, approve, then send once", "covered": True},
        ],
        "steps_detail": [],
    })

    assert progress["covered"] == 2
    assert progress["coverage_total"] == 2
    assert progress["covered_aspects"] == [
        "create pending approval", "deny send, approve, then send once"]


def test_recovery_carries_exact_ledger_with_portable_browser_state(tmp_path):
    state = tmp_path / "browser-state.json"
    state.write_text("{}")
    coverage = [{"aspect": "keyboard reaches submit", "covered": True},
                {"aspect": "focus remains visible", "covered": False}]
    recovered = orchestra_runtime._qa_prior_recovery({"7": {
        "story": "US-12", "result": {"resume_state_path": str(state), "coverage": coverage,
                                        "steps_detail": [{"verdict": "match",
                                                          "covers": ["keyboard reaches submit"]}]}}},
        "US-12")
    assert recovered["resume_state_path"] == str(state)
    assert recovered["resume_covered"] == ["keyboard reaches submit"]
    assert recovered["resume_coverage"] == coverage


def test_recovery_carries_bounded_sealed_receipts_with_the_exact_browser_state(tmp_path):
    state = tmp_path / "browser-state.json"
    state.write_text("{}")
    receipts = [{"step": index, "action": {"cmd": "click", "target_text": f"case {index}"}}
                for index in range(700)]

    recovered = orchestra_runtime._qa_prior_recovery({"7": {
        "story": "US-10", "result": {
            "resume_state_path": str(state),
            "coverage": [{"aspect": "test the matrix", "covered": False}],
            "steps_detail": receipts,
        }}}, "US-10")

    assert len(recovered["resume_steps_detail"]) == 600
    assert recovered["resume_steps_detail"][0]["step"] == 100
    assert recovered["resume_steps_detail"][-1]["step"] == 699


def test_recovery_rejects_old_revision_and_invalidated_actor_provenance(tmp_path):
    state = tmp_path / "browser-state.json"
    state.write_text("{}")
    result = {"story": "US-10", "result": {
        "resume_state_path": str(state),
        "coverage": [{"aspect": "block sensitive content", "covered": True}],
        "steps_detail": [{"verdict": "match", "covers": ["block sensitive content"]}],
    }}
    children = {
        7: {"actor_id": 7, "memory": {"context": {"tool_args": {
            "product_revision": "new", "_qa_recovery_source_revision": "old"}}}},
    }

    assert orchestra_runtime._qa_prior_recovery(
        {"7": result}, "US-10", children=children, product_revision="new") == {}

    children[7]["memory"]["context"]["tool_args"].pop("_qa_recovery_source_revision")
    recovered = orchestra_runtime._qa_prior_recovery(
        {"7": result}, "US-10", children=children, product_revision="new")
    assert recovered["_qa_recovery_source_revision"] == "new"
    assert orchestra_runtime._qa_prior_recovery(
        {"7": result}, "US-10", children=children, product_revision="new",
        invalid_actor_ids=[7]) == {}


def test_recovery_crosses_only_explicit_selective_preservation_lineage(tmp_path):
    state = tmp_path / "browser-state.json"
    state.write_text("{}")
    result = {"story": "US-12", "result": {
        "resume_state_path": str(state),
        "coverage": [{"aspect": "mobile viewport", "covered": True}],
        "steps_detail": [{"verdict": "match", "covers": ["mobile viewport"]}],
    }}
    children = {7: {"actor_id": 7, "memory": {"context": {"tool_args": {
        "product_revision": "revision-a", "_qa_recovery_source_revision": "revision-a"}}}}}
    selective_lineage = [
        {"from": "revision-a", "to": "revision-b", "full_regression": False,
         "preserved_story_ids": ["US-12"]},
        {"from": "revision-b", "to": "revision-c", "full_regression": False,
         "preserved_story_ids": ["US-12", "US-7"]},
    ]

    recovered = orchestra_runtime._qa_prior_recovery(
        {"7": result}, "US-12", children=children, product_revision="revision-c",
        revision_invalidations=selective_lineage)
    assert recovered["resume_state_path"] == str(state)
    assert recovered["resume_covered"] == ["mobile viewport"]
    assert recovered["_qa_recovery_source_revision"] == "revision-a"

    assert orchestra_runtime._qa_prior_recovery(
        {"7": result}, "US-10", children=children, product_revision="revision-c",
        revision_invalidations=selective_lineage) == {}
    unsafe = [selective_lineage[0], {
        "from": "revision-b", "to": "revision-c", "full_regression": True,
        "preserved_story_ids": ["US-12"],
    }]
    assert orchestra_runtime._qa_prior_recovery(
        {"7": result}, "US-12", children=children, product_revision="revision-c",
        revision_invalidations=unsafe) == {}


def test_slow_subordinate_phases_are_durable_and_deduplicated():
    memory = {}
    progress = {"slow_phases": [
        {"phase": "decide", "step": 2, "elapsed_s": 45.1, "status": "ok"},
        {"phase": "evaluate", "step": 2, "elapsed_s": 97.0, "status": "ok"},
    ]}
    first = orchestra_runtime._qa_record_performance_observations(memory, "US-1", progress)
    second = orchestra_runtime._qa_record_performance_observations(memory, "US-1", progress)
    assert [item["phase"] for item in first] == ["decide", "evaluate"]
    assert second == []
    assert len(memory["performance_events"]) == 2
