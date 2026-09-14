"""Focused integration regressions for management recovery truth."""

import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import management
import qareview


def test_orphaned_internal_management_checkpoint_is_owned_and_resumed():
    thread_id = 991_000_000 + int(uuid.uuid4().hex[:6], 16)
    tenant = f"management-internal-{uuid.uuid4().hex}"
    case_id = None
    job_id = None
    try:
        with management._conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                              (thread_id,tenant_id,org_id,phase,brief,awaiting,updated_at,execution_scope,
                               qa_checkpoint_count,qa_last_completed,qa_no_progress_count)
                           VALUES (%s,%s,0,'TESTQA','{}'::jsonb,'internal_management',now(),'test',34,16,2)""",
                        (thread_id, tenant))
            cur.execute("""INSERT INTO controller_jobs
                              (thread_id,tenant_id,phase,kind,status,result,started_at,finished_at,
                               execution_scope)
                           VALUES (%s,%s,'TESTQA','qa','failed',%s,now(),now(),'test') RETURNING id""",
                        (thread_id, tenant, '{"error":"handoff lost","engineering_hold":true}'))
            job_id = cur.fetchone()[0]

        assert management.discover_orphaned_internal_management(
            {"remaining": 1}, thread_ids=[thread_id]) == 1
        with management._conn() as c, c.cursor() as cur:
            cur.execute("""SELECT case_id FROM management_cases
                            WHERE tenant_id=%s AND dedupe_key=%s""",
                        (tenant, f"duty:controller-internal:{thread_id}"))
            case_id = cur.fetchone()[0]

        decision = management.review_now(
            case_id,
            decide_fn=lambda *_a, **_k: {"action": "retry", "rationale": "handoff failed before commit",
                                          "confidence": 0.99, "review_in_s": 30},
            send_fn=lambda *_a, **_k: {"sent": True})
        assert decision["status"] == "resolved" and decision["action"] == "retry"
        with management._conn() as c, c.cursor() as cur:
            cur.execute("""SELECT awaiting,qa_checkpoint_count,qa_last_completed,qa_no_progress_count
                           FROM controller_state WHERE thread_id=%s""", (thread_id,))
            assert cur.fetchone() == (None, 0, None, 0)
            cur.execute("""SELECT status,state FROM management_cases WHERE case_id=%s""", (case_id,))
            status, state = cur.fetchone()
            assert status == "resolved" and state["controller_resumed"] is True
            cur.execute("""SELECT count(*) FROM management_events
                            WHERE case_id=%s AND event_type='controller_resumed'""", (case_id,))
            assert cur.fetchone()[0] == 1
    finally:
        with management._conn() as c, c.cursor() as cur:
            if case_id:
                cur.execute("DELETE FROM management_questions WHERE case_id=%s", (case_id,))
                cur.execute("DELETE FROM management_decisions WHERE case_id=%s", (case_id,))
                cur.execute("DELETE FROM management_events WHERE case_id=%s", (case_id,))
                cur.execute("DELETE FROM management_cases WHERE case_id=%s", (case_id,))
            if job_id:
                cur.execute("DELETE FROM controller_jobs WHERE id=%s", (job_id,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread_id,))


def test_newer_controller_job_resolves_old_worker_failure_case():
    thread_id = 990_000_000 + int(uuid.uuid4().hex[:6], 16)
    tenant = f"management-recovery-{uuid.uuid4().hex}"
    case_id = None
    job_id = None
    try:
        with management._conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO controller_state
                              (thread_id,tenant_id,org_id,phase,brief,awaiting,updated_at,execution_scope)
                           VALUES (%s,%s,0,'TESTQA','{}'::jsonb,NULL,now(),'test')""",
                        (thread_id, tenant))
        case = management.signal(
            f"controller:{thread_id}:TESTQA", "TESTQA worker repeatedly failed",
            "worker_state_changed", {"thread_id": thread_id, "phase": "TESTQA",
                                     "error": "old worker died", "failure_count": 2},
            tenant_id=tenant, work_id=f"controller:{thread_id}:TESTQA")
        case_id = case["case_id"]
        with management._conn() as c, c.cursor() as cur:
            # Make the ordering unambiguous even on a fast database clock.
            cur.execute("UPDATE management_cases SET last_progress_at=now()-interval '1 minute' WHERE case_id=%s",
                        (case_id,))
            cur.execute("""INSERT INTO controller_jobs
                              (thread_id,tenant_id,phase,kind,status,started_at,heartbeat_at,execution_scope)
                           VALUES (%s,%s,'TESTQA','qa','running',now(),now(),'test') RETURNING id""",
                        (thread_id, tenant))
            job_id = cur.fetchone()[0]

        recovered = management._reconcile_recovered_controller_cases()
        with management._conn() as c, c.cursor() as cur:
            cur.execute("SELECT status,trigger,state FROM management_cases WHERE case_id=%s", (case_id,))
            status, trigger, state = cur.fetchone()
            cur.execute("SELECT count(*) FROM management_events WHERE case_id=%s AND event_type='control_recovered'",
                        (case_id,))
            recovery_events = cur.fetchone()[0]
        # Other genuinely recovered live cases may be reconciled in the same
        # set-based sweep; the fixture itself must be among them.
        assert recovered >= 1
        assert status == "resolved" and trigger == "worker_recovered"
        assert state["recovered"] is True and recovery_events == 1
    finally:
        with management._conn() as c, c.cursor() as cur:
            if case_id:
                cur.execute("DELETE FROM management_questions WHERE case_id=%s", (case_id,))
                cur.execute("DELETE FROM management_decisions WHERE case_id=%s", (case_id,))
                cur.execute("DELETE FROM management_events WHERE case_id=%s", (case_id,))
                cur.execute("DELETE FROM management_cases WHERE case_id=%s", (case_id,))
            if job_id:
                cur.execute("DELETE FROM controller_jobs WHERE id=%s", (job_id,))
            cur.execute("DELETE FROM controller_state WHERE thread_id=%s", (thread_id,))


def test_runtime_and_duty_qa_signals_converge_and_wake_the_exact_dispute(monkeypatch):
    tenant = f"management-qa-{uuid.uuid4().hex}"
    review_id = f"review-{uuid.uuid4().hex}"
    qa_case_id = f"qad-{uuid.uuid4().hex}"
    dedupe = qareview.management_case_key(tenant, review_id)
    management_case_id = None
    wakes = []
    try:
        runtime_signal = management.signal(
            dedupe, f"QA dispute: {review_id}", qareview.MANAGEMENT_TRIGGER,
            qareview.management_case_state({"review_id": review_id, "case_id": qa_case_id,
                                            "status": "manager_review", "state_generation": 3,
                                            "source": "runtime"}),
            tenant_id=tenant, worker="qa-manager", manager_role="senior-qa-director")
        duty_signal = management.signal(
            qareview.management_case_key(tenant, review_id), f"QA dispute: {review_id}",
            qareview.MANAGEMENT_TRIGGER,
            qareview.management_case_state({"review_id": review_id, "case_id": qa_case_id,
                                            "status": "manager_review", "state_generation": 3,
                                            "source": "duty", "age_s": 999, "updated_at": "later"}),
            tenant_id=tenant, worker="qa-manager", manager_role="senior-qa-director")
        management_case_id = runtime_signal["case_id"]
        assert duty_signal["case_id"] == management_case_id
        with management._conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*),min(trigger) FROM management_cases WHERE dedupe_key=%s", (dedupe,))
            count, trigger = cur.fetchone()
        assert count == 1 and trigger == qareview.MANAGEMENT_TRIGGER

        monkeypatch.setattr(
            management, "_qa_process_owner",
            lambda _case, _rid: (2416, 9280, {"story": "US-012"}))
        monkeypatch.setattr(
            qareview, "begin_evidence_collection",
            lambda tid, cid, state, **kwargs: (
                wakes.append((tid, cid, state, kwargs))
                or {"status": "manager_review", "state_generation": 4,
                    "evidence_collection": True}))
        emitted = []
        monkeypatch.setattr(
            management, "_emit_qa_process_recovery",
            lambda case, run, actor, payload, kind: emitted.append(
                (case, run, actor, payload, kind)) or True)
        case = {"case_id": management_case_id, "trigger": qareview.MANAGEMENT_TRIGGER,
                "tenant_id": tenant, "manager_role": "senior-qa-director",
                "work_id": f"orchestra:2416:{review_id}", "semantic_generation": 3,
                "state": {"case_id": qa_case_id, "review_id": review_id,
                          "status": "manager_review"}}
        assert management._resume_qa_dispute(case, "continue", "collect changed evidence", 0.9) is True
        assert len(wakes) == 1 and wakes[0][0:2] == (tenant, qa_case_id)
        assert emitted[0][3]["qa_evidence_recovery"]["state_generation"] == 4
        monkeypatch.setattr(
            qareview, "begin_evidence_collection",
            lambda *_args, **_kwargs: {"status": "external_authority"})
        assert management._resume_qa_dispute(case, "continue", "do not bypass authority", 0.9) is False
        assert len(emitted) == 1
    finally:
        if management_case_id:
            with management._conn() as c, c.cursor() as cur:
                cur.execute("DELETE FROM management_questions WHERE case_id=%s", (management_case_id,))
                cur.execute("DELETE FROM management_decisions WHERE case_id=%s", (management_case_id,))
                cur.execute("DELETE FROM management_events WHERE case_id=%s", (management_case_id,))
                cur.execute("DELETE FROM management_cases WHERE case_id=%s", (management_case_id,))


def test_terminal_qa_dispute_retires_stale_manager_questions():
    tenant = f"management-qa-terminal-{uuid.uuid4().hex}"
    review_id = f"review-{uuid.uuid4().hex}"
    qa_case_id = f"qad-{uuid.uuid4().hex}"
    management_case_id = None
    try:
        with management._conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO qa_evidence_disputes
                              (case_id,tenant_id,review_id,work_ref,repo,internal_review,
                               internal_review_digest,state,state_digest,status,resolved_at)
                           VALUES (%s,%s,%s,%s,'/tmp/repo','{}','digest','{}','state',
                                   'resolved',now())""",
                        (qa_case_id, tenant, review_id, f"orchestra:test:{review_id}"))
        case = management.signal(
            qareview.management_case_key(tenant, review_id), f"QA dispute: {review_id}",
            qareview.MANAGEMENT_TRIGGER,
            qareview.management_case_state({
                "review_id": review_id, "case_id": qa_case_id,
                "status": "manager_review", "state_generation": 1,
            }), tenant_id=tenant, worker="qa-manager", manager_role="senior-qa-director")
        management_case_id = case["case_id"]
        with management._conn() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO management_questions
                              (question_id,case_id,asker,recipient,question,status,reply_by)
                           VALUES (%s,%s,'director','qa-manager','send status','open',
                                   now()-interval '1 minute')""",
                        (f"mq-{uuid.uuid4().hex}", management_case_id))

        assert management._reconcile_terminal_qa_dispute_cases() >= 1
        with management._conn() as c, c.cursor() as cur:
            cur.execute("SELECT status,trigger,state FROM management_cases WHERE case_id=%s",
                        (management_case_id,))
            status, trigger, state = cur.fetchone()
            cur.execute("SELECT status FROM management_questions WHERE case_id=%s",
                        (management_case_id,))
            question_status = cur.fetchone()[0]
            cur.execute("""SELECT count(*) FROM management_events
                            WHERE case_id=%s AND event_type='terminal_qa_dispute_reconciled'""",
                        (management_case_id,))
            events = cur.fetchone()[0]
        assert status == "resolved" and trigger == "terminal_qa_dispute_reconciled"
        assert state["terminal_qa_dispute"] is True and state["terminal_qa_case_id"] == qa_case_id
        assert question_status == "cancelled" and events == 1
    finally:
        with management._conn() as c, c.cursor() as cur:
            if management_case_id:
                cur.execute("DELETE FROM management_questions WHERE case_id=%s", (management_case_id,))
                cur.execute("DELETE FROM management_decisions WHERE case_id=%s", (management_case_id,))
                cur.execute("DELETE FROM management_events WHERE case_id=%s", (management_case_id,))
                cur.execute("DELETE FROM management_cases WHERE case_id=%s", (management_case_id,))
            cur.execute("DELETE FROM qa_evidence_dispute_reviews WHERE case_id=%s", (qa_case_id,))
            cur.execute("DELETE FROM qa_evidence_disputes WHERE case_id=%s", (qa_case_id,))


def test_qa_performance_manager_rebriefs_coordinator_instead_of_asking_ghost(monkeypatch):
    tenant = f"management-qa-performance-{uuid.uuid4().hex}"
    review_id = f"review-{uuid.uuid4().hex}"
    case_id = None
    resumed = []
    try:
        case = management.signal(
            f"qa-performance:{tenant}:{review_id}", f"QA performance stall: {review_id}",
            management.QA_PERFORMANCE_TRIGGER,
            {"review_id": review_id, "run_id": 99, "coordinator_actor_id": 42,
             "story": "US-12", "status": "manager_review", "verification_attempts": 2},
            tenant_id=tenant, work_id=f"orchestra:99:{review_id}",
            worker="qa-manager", manager_role="senior-qa-director")
        case_id = case["case_id"]
        monkeypatch.setattr(
            management, "_resume_qa_performance",
            lambda claimed, action, rationale, confidence: resumed.append(
                (claimed, action, rationale, confidence)) or True)

        decision = management.review_now(
            case_id,
            decide_fn=lambda *_a, **_k: {
                "action": "request_status", "rationale": "need a useful evidence report",
                "confidence": 0.92, "review_in_s": 30},
            send_fn=lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("a symbolic qa-manager mailbox must not be queried")))

        assert decision["action"] == "rebrief" and decision["status"] == "resolved"
        assert resumed and resumed[0][1] == "rebrief"
        with management._conn() as c, c.cursor() as cur:
            cur.execute("SELECT status FROM management_cases WHERE case_id=%s", (case_id,))
            assert cur.fetchone()[0] == "resolved"
            cur.execute("SELECT count(*) FROM management_questions WHERE case_id=%s", (case_id,))
            assert cur.fetchone()[0] == 0
    finally:
        if case_id:
            with management._conn() as c, c.cursor() as cur:
                cur.execute("DELETE FROM management_questions WHERE case_id=%s", (case_id,))
                cur.execute("DELETE FROM management_decisions WHERE case_id=%s", (case_id,))
                cur.execute("DELETE FROM management_events WHERE case_id=%s", (case_id,))
                cur.execute("DELETE FROM management_cases WHERE case_id=%s", (case_id,))


def test_qa_performance_resume_targets_exact_durable_coordinator(monkeypatch):
    import types

    emitted = []
    fake_store = types.SimpleNamespace(
        actor=lambda actor_id, tenant: {
            "actor_id": actor_id, "tenant_id": tenant, "run_id": 99,
            "role": "qa-coordinator"},
        resume_run=lambda *_args, **_kwargs: None,
        emit_once=lambda *args, **kwargs: emitted.append((args, kwargs)) or {"id": 1})
    monkeypatch.setitem(sys.modules, "store", fake_store)
    case = {
        "case_id": "mc-performance", "tenant_id": "tenant",
        "trigger": management.QA_PERFORMANCE_TRIGGER, "semantic_generation": 3,
        "state": {"run_id": 99, "coordinator_actor_id": 42,
                  "review_id": "review-1", "story": "US-12"},
    }

    assert management._resume_qa_performance(
        case, "rebrief", "continue from exact evidence", 0.95) is True
    args, kwargs = emitted[0]
    assert args[:5] == (99, "tenant", None, 42, "context_update")
    assert args[5]["qa_performance_recovery"]["review_id"] == "review-1"
    assert kwargs["corr_id"] == "management-qa-performance:mc-performance:3"


def test_overdue_qa_process_cases_route_to_coordinator_not_symbolic_mailbox(monkeypatch):
    import types

    emitted = []
    coordinator = {
        "actor_id": 42, "tenant_id": "tenant", "run_id": 99, "role": "qa-coordinator",
        "memory": {"internal_reviews": [
            {"review_id": "qa-review-stall", "story": "US-11",
             "finding": {"kind": "qa_performance_stall"}},
            {"review_id": "qa-capability-at", "story": "US-12",
             "finding": {"kind": "qa_capability"}},
        ]},
    }
    fake_store = types.SimpleNamespace(
        actor=lambda actor_id, tenant: coordinator if actor_id == 42 else None,
        actors=lambda run_id, tenant: [coordinator],
        resume_run=lambda *_args, **_kwargs: None,
        emit_once=lambda *args, **kwargs: emitted.append((args, kwargs)) or {"id": 1})
    monkeypatch.setitem(sys.modules, "store", fake_store)

    performance = {
        "case_id": "mc-overdue-performance", "tenant_id": "tenant",
        "trigger": "internal_reply_overdue", "semantic_generation": 8,
        "work_id": "orchestra:99:qa-review-stall",
        "state": {"review_id": "qa-review-stall", "case_id": None,
                  "communication_evidence": {"status": "overdue"}},
    }
    assert management._is_qa_process_case(performance, "performance") is True
    assert management._resume_qa_performance(
        performance, "rebrief", "use exact landmarks", 0.94) is True
    assert emitted[-1][0][5]["qa_performance_recovery"]["story"] == "US-11"

    capability = {
        "case_id": "mc-overdue-capability", "tenant_id": "tenant",
        "trigger": "internal_reply_overdue", "semantic_generation": 9,
        "work_id": "orchestra:99:qa-capability-at",
        "state": {"run_id": 99, "coordinator_actor_id": 42,
                  "review_id": "qa-capability-at", "story": "US-12",
                  "capabilities": [{"capability": "actual-assistive-technology"}]},
    }
    assert management._is_qa_process_case(capability, "capability") is True
    assert management._resume_qa_capability(
        capability, "retry", "the AT workstation lease is available", 0.97) is True
    assert emitted[-1][0][5]["qa_capability_recovery"]["story"] == "US-12"
