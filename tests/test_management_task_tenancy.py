"""Concurrency and isolation regressions for semantic management and tenant tasks.

These integration cases require migration 69. They never apply it themselves and
clean up only UUID-qualified fixture rows.
"""

from concurrent.futures import ThreadPoolExecutor
import sys
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import directory  # noqa: E402
import management  # noqa: E402
import orchestrate  # noqa: E402


def _schema_ready():
    with management._conn() as c, c.cursor() as cur:
        cur.execute("""SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name='tasks' AND column_name='tenant_id'), EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_name='management_cases' AND column_name='semantic_generation'), EXISTS (
            SELECT 1 FROM pg_class WHERE oid='tasks'::regclass
             AND relrowsecurity AND relforcerowsecurity), EXISTS (
            SELECT 1 FROM pg_indexes WHERE indexname='tasks_tenant_idempotency_uidx')""")
        return all(cur.fetchone())


@pytest.fixture
def migration_69_required(monkeypatch):
    if not _schema_ready():
        pytest.skip("migration 69 is not installed; tests never mutate the live schema")
    # The migration owns DDL. Focus these tests on runtime transactions and avoid
    # taking redundant catalog locks in a shared developer database.
    monkeypatch.setattr(management, "_ensure", lambda: None)
    monkeypatch.setattr(orchestrate, "_ensure", lambda: None)


def _delete_management(case_ids, tenant, cursor_source=None):
    with management._conn() as c, c.cursor() as cur:
        cur.execute("SELECT case_id FROM management_cases WHERE tenant_id=%s", (tenant,))
        case_ids = list({*case_ids, *(row[0] for row in cur.fetchall())})
        cur.execute("DELETE FROM management_dispatches WHERE tenant_id=%s", (tenant,))
        if case_ids:
            cur.execute("DELETE FROM management_questions WHERE case_id=ANY(%s)", (case_ids,))
            cur.execute("DELETE FROM management_decisions WHERE case_id=ANY(%s)", (case_ids,))
            cur.execute("DELETE FROM management_events WHERE case_id=ANY(%s)", (case_ids,))
            cur.execute("DELETE FROM management_cases WHERE case_id=ANY(%s)", (case_ids,))
        if cursor_source:
            cur.execute("DELETE FROM management_duty_cursors WHERE source=%s", (cursor_source,))


def test_concurrent_volatile_observations_emit_one_semantic_event(migration_69_required):
    tenant = f"mg-sem-{uuid.uuid4().hex}"
    dedupe = f"duty:actor:{uuid.uuid4().hex}"
    case_id = None
    try:
        def observe(i):
            return management.signal(
                dedupe, "worker silent", "actor_heartbeat_silent",
                {"actor_id": "actor-1", "run_id": "run-1", "assignment": "ship",
                 "status_evidence": {"silent_s": 601 + i, "threshold_s": 600}},
                tenant_id=tenant)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(observe, range(24)))
        case_id = results[0]["case_id"]
        assert {row["semantic_generation"] for row in results} == {1}
        with management._conn() as c, c.cursor() as cur:
            cur.execute("""SELECT semantic_generation,last_event_generation,reviewed_generation
                           FROM management_cases WHERE tenant_id=%s AND case_id=%s""",
                        (tenant, case_id))
            generation, event_generation, reviewed = cur.fetchone()
            cur.execute("SELECT count(*) FROM management_events WHERE case_id=%s", (case_id,))
            events = cur.fetchone()[0]
        assert (generation, event_generation, reviewed, events) == (1, 1, 0, 1)
    finally:
        _delete_management([case_id] if case_id else [], tenant)


def test_semantic_not_exists_skips_large_fixed_prefix_and_reaches_tail(migration_69_required):
    tenant = f"mg-tail-{uuid.uuid4().hex}"
    source = f"large-tail-{uuid.uuid4().hex}"
    case_ids = []
    candidates = []
    try:
        for i in range(128):
            dedupe = f"{source}:prefix:{i:04d}"
            state = {"status": "blocked", "owner": f"worker-{i}", "overdue_s": i + 10}
            case = management.signal(dedupe, f"prefix {i}", "work_attention", state,
                                     tenant_id=tenant)
            case_ids.append(case["case_id"])
            candidates.append({"dedupe_key": dedupe, "subject": f"prefix {i}",
                               "trigger": "work_attention", "state": {**state, "overdue_s": 9999},
                               "tenant_id": tenant, "cursor_key": f"{i:04d}"})
        tail_dedupe = f"{source}:tail"
        candidates.append({"dedupe_key": tail_dedupe, "subject": "changed tail",
                           "trigger": "work_attention", "state": {"status": "new_failure"},
                           "tenant_id": tenant, "cursor_key": "zzzz-tail"})

        selected = management._duty_candidates({"remaining": 1}, source, candidates)
        assert selected == 1
        with management._conn() as c, c.cursor() as cur:
            cur.execute("""SELECT case_id FROM management_cases
                           WHERE tenant_id=%s AND dedupe_key=%s""", (tenant, tail_dedupe))
            tail = cur.fetchone()
        assert tail is not None
        case_ids.append(tail[0])
    finally:
        if case_ids:
            _delete_management(case_ids, tenant, source)


def test_task_idempotency_and_cross_tenant_complete_are_enforced(migration_69_required):
    tenant_a = f"task-a-{uuid.uuid4().hex}"
    tenant_b = f"task-b-{uuid.uuid4().hex}"
    key = f"idem-{uuid.uuid4().hex}"
    try:
        def enqueue_once(_):
            return orchestrate.enqueue("operator@fixture", "same work", tenant_id=tenant_a,
                                       idempotency_key=key)

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(enqueue_once, range(24)))
        assert len(set(ids)) == 1
        with pytest.raises(PermissionError):
            orchestrate.complete(ids[0], tenant_id=tenant_b)
        assert orchestrate.queue_of("operator@fixture", tenant_id=tenant_b) == []
        orchestrate.complete(ids[0], tenant_id=tenant_a)
        with management._conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM tasks WHERE tenant_id=%s AND idempotency_key=%s",
                        (tenant_a, key))
            assert cur.fetchone()[0] == 1
    finally:
        with management._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE tenant_id=ANY(%s)", ([tenant_a, tenant_b],))


def test_same_management_generation_routes_one_task_and_one_message(monkeypatch,
                                                                    migration_69_required):
    tenant = f"mg-route-{uuid.uuid4().hex}"
    assignee = f"operator@{uuid.uuid4().hex}"
    case = {"tenant_id": tenant, "case_id": f"mc-{uuid.uuid4().hex}",
            "semantic_generation": 7, "manager_role": "team-lead"}
    directory.register(assignee, "operator", None, "available", [], tenant_id=tenant)
    monkeypatch.setattr(orchestrate.governance, "may", lambda *_: True)
    try:
        def dispatch(_):
            return management._dispatch_staffing(case, "reassign", "operator", "recover this work")

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(dispatch, range(12)))
        key = management._dispatch_key(case, "reassign", "operator")
        with management._conn() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM tasks WHERE tenant_id=%s AND idempotency_key=%s",
                        (tenant, key))
            tasks = cur.fetchone()[0]
            cur.execute("""SELECT count(*) FROM conversations
                           WHERE tenant_id=%s AND idempotency_key=%s""", (tenant, f"{key}:message"))
            messages = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM management_dispatches WHERE dispatch_key=%s", (key,))
            dispatches = cur.fetchone()[0]
        assert (tasks, messages, dispatches) == (1, 1, 1)
    finally:
        with management._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM inbox WHERE tenant_id=%s", (tenant,))
            cur.execute("DELETE FROM conversations WHERE tenant_id=%s", (tenant,))
            cur.execute("DELETE FROM tasks WHERE tenant_id=%s", (tenant,))
            cur.execute("DELETE FROM management_dispatches WHERE tenant_id=%s", (tenant,))
            cur.execute("DELETE FROM directory WHERE tenant_id=%s", (tenant,))


def test_continue_instruction_reaches_worker_exactly_once(migration_69_required):
    tenant = f"mg-instruction-{uuid.uuid4().hex}"
    worker = f"qa-worker-{uuid.uuid4().hex}"
    case = {"tenant_id": tenant, "case_id": f"mc-{uuid.uuid4().hex}",
            "semantic_generation": 3, "manager_role": "team-lead", "worker": worker}
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda _: management._send_worker_instruction(
                    case, "continue", "Install the pinned dependency and retry."),
                range(16)))
        assert len({row["message_id"] for row in results}) == 1
        key = management._dispatch_key(case, "continue_instruction", worker)
        with management._conn() as c, c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM conversations
                           WHERE tenant_id=%s AND idempotency_key=%s""",
                        (tenant, f"{key}:message"))
            messages = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM inbox WHERE tenant_id=%s AND subscriber=%s",
                        (tenant, worker))
            inbox = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM management_dispatches WHERE dispatch_key=%s", (key,))
            dispatches = cur.fetchone()[0]
        assert (messages, inbox, dispatches) == (1, 1, 1)
    finally:
        with management._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM inbox WHERE tenant_id=%s", (tenant,))
            cur.execute("DELETE FROM conversations WHERE tenant_id=%s", (tenant,))
            cur.execute("DELETE FROM management_dispatches WHERE tenant_id=%s", (tenant,))


def test_manager_escalation_is_an_immediately_claimable_exact_handoff(migration_69_required):
    """Changing the manager must create work for that manager, not mark their turn already reviewed."""
    tenant = f"mg-handoff-{uuid.uuid4().hex}"
    case_id = None
    try:
        opened = management.signal(
            f"handoff:{uuid.uuid4().hex}", "cross-team acceptance conflict", "worker_state_changed",
            {"status": "blocked", "authority_boundary": None}, tenant_id=tenant,
            worker=f"qa-worker-{uuid.uuid4().hex}", manager_role="team-lead")
        case_id = opened["case_id"]
        owner = f"first-manager-{uuid.uuid4().hex}"
        first = management._claim_one(case_id, owner)
        assert first is not None
        first["lease_owner"] = owner
        result = management._apply(first, {
            "action": "escalate_manager", "rationale": "cross-department priority",
            "confidence": .98, "review_in_s": 300})
        assert result["manager_role"] == "department-head"

        def claim(i):
            return management._claim_one(case_id, f"department-head-{i}-{uuid.uuid4().hex}")

        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = [row for row in pool.map(claim, range(12)) if row]
        assert len(claims) == 1
        assert claims[0]["manager_role"] == "department-head"
        assert claims[0]["semantic_generation"] == 2
        assert claims[0]["reviewed_generation"] == 1
        with management._conn() as c, c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM management_events
                            WHERE case_id=%s AND event_type='manager_escalated'
                              AND semantic_generation=2""", (case_id,))
            assert cur.fetchone()[0] == 1
    finally:
        _delete_management([case_id] if case_id else [], tenant)


def test_internal_question_reaches_unregistered_worker_exactly_once(migration_69_required):
    """A durable orchestra worker needs no directory row to receive and answer a manager question."""
    tenant = f"mg-question-{uuid.uuid4().hex}"
    worker = f"orchestra-worker-{uuid.uuid4().hex}"
    case_id = None
    try:
        opened = management.signal(
            f"question:{uuid.uuid4().hex}", "clarify retry evidence", "worker_state_changed",
            {"status": "needs_rebrief"}, tenant_id=tenant, worker=worker)
        case_id = opened["case_id"]
        qids = [management.ask_internal(
            case_id, "team-lead", worker, "Retry once with the pinned dependency and report evidence.")
            for _ in range(8)]
        assert len(set(qids)) == 1
        with management._conn() as c, c.cursor() as cur:
            cur.execute("SELECT tenant_id,recipient,status FROM management_questions WHERE question_id=%s",
                        (qids[0],))
            assert cur.fetchone() == (tenant, worker, "open")
            cur.execute("SELECT count(*) FROM conversations WHERE tenant_id=%s", (tenant,))
            messages = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM inbox WHERE tenant_id=%s AND subscriber=%s",
                        (tenant, worker))
            inbox = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM management_dispatches WHERE tenant_id=%s", (tenant,))
            dispatches = cur.fetchone()[0]
        assert (messages, inbox, dispatches) == (1, 1, 1)
        assert management.answer(qids[0], "Pinned retry passed.", responder=worker)
    finally:
        with management._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM inbox WHERE tenant_id=%s", (tenant,))
            cur.execute("DELETE FROM conversations WHERE tenant_id=%s", (tenant,))
        _delete_management([case_id] if case_id else [], tenant)


def test_same_management_generation_files_one_hire(monkeypatch, migration_69_required):
    tenant = f"mg-hire-{uuid.uuid4().hex}"
    case = {"tenant_id": tenant, "case_id": f"mc-{uuid.uuid4().hex}",
            "semantic_generation": 11, "manager_role": "team-lead"}
    monkeypatch.setattr(orchestrate.directory, "find", lambda **_: [])
    monkeypatch.setattr(orchestrate, "known_roles", lambda: {"operator"})
    monkeypatch.setattr(orchestrate.governance, "may", lambda *_: True)
    try:
        def dispatch(_):
            return management._dispatch_staffing(case, "add_help", "operator", "join recovery")

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(dispatch, range(12)))
        hire_ids = {result["hire_id"] for result in results}
        assert len(hire_ids) == 1
        key = management._dispatch_key(case, "add_help", "operator")
        with management._conn() as c, c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM hire_requests
                           WHERE tenant_id=%s AND idempotency_key=%s""", (tenant, key))
            assert cur.fetchone()[0] == 1
    finally:
        with management._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM management_dispatches WHERE tenant_id=%s", (tenant,))
            cur.execute("DELETE FROM hire_requests WHERE tenant_id=%s", (tenant,))


def test_migration_declares_tenant_rls_and_idempotency_contracts():
    sql = (ROOT / "postgres" / "initdb" / "69-management-task-tenancy.sql").read_text()
    assert "UPDATE tasks SET tenant_id='_platform'" in sql
    assert "tasks_tenant_idempotency_uidx" in sql
    assert "ON tasks(tenant_id" in sql
    assert "ALTER TABLE %I FORCE ROW LEVEL SECURITY" in sql
    assert "management_dispatches" in sql and "management_duty_cursors" in sql
