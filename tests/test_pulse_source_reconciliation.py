"""Source-of-truth reconciliation for tool-job liveness pulses."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts" / "orchestra", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import management
import pulse
import store
from dbpool import connection


def _cleanup(run_id: int, pulse_ids: list[str], case_ids: list[str]) -> None:
    with connection() as c, c.cursor() as cur:
        if case_ids:
            cur.execute("DELETE FROM management_questions WHERE case_id=ANY(%s)", (case_ids,))
            cur.execute("DELETE FROM management_decisions WHERE case_id=ANY(%s)", (case_ids,))
            cur.execute("DELETE FROM management_events WHERE case_id=ANY(%s)", (case_ids,))
            cur.execute("DELETE FROM management_dispatches WHERE case_id=ANY(%s)", (case_ids,))
            cur.execute("DELETE FROM management_cases WHERE case_id=ANY(%s)", (case_ids,))
        cur.execute("DELETE FROM agent_pulse WHERE work_id=ANY(%s)", (pulse_ids,))
        cur.execute("DELETE FROM orchestra_events WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM orchestra_actors WHERE run_id=%s", (run_id,))
        cur.execute("DELETE FROM orchestra_runs WHERE run_id=%s", (run_id,))


def test_terminal_tool_source_reaps_immediately_and_retires_manager_question():
    if not pulse.DB:
        pytest.skip("no DB")
    tenant = f"pulse-source-{uuid.uuid4().hex}"
    run = store.start_run(tenant, "pulse source reconciliation")
    actor = store.spawn_actor(run["run_id"], tenant, "qa-worker", "qa-explorer")
    work_id = f"{run['run_id']}:{actor['actor_id']}:qa_explore:0"
    case_ids: list[str] = []
    try:
        pulse.beat(work_id, kind="tool-job", tenant_id=tenant, stage="running",
                   progress="running qa_explore", expected_cadence_s=300)
        case = management.signal(
            f"pulse:{work_id}", "QA worker stopped reporting", "heartbeat_silent",
            {"stage": "running"}, tenant_id=tenant, work_id=work_id, worker=work_id)
        case_ids.append(case["case_id"])
        question_id = management.ask_internal(
            case["case_id"], "team-lead", work_id, "Send status", reply_within_s=30)

        # Fresh live source is retained, regardless of the management question.
        assert pulse.reap_orphans() == 0
        with connection() as c, c.cursor() as cur:
            cur.execute("UPDATE orchestra_runs SET status='halted',finished_at=now() WHERE run_id=%s",
                        (run["run_id"],))

        assert pulse.reap_orphans() == 1
        # Other already-terminal questions may be reconciled in the same global
        # sweep; our fixture's exact state below is the one-shot assertion.
        assert management._reconcile_terminal_questions() >= 1
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT status,result->>'reaped_reason' FROM agent_pulse WHERE work_id=%s",
                        (work_id,))
            status, reason = cur.fetchone()
            assert status == "reaped" and "durable tool owner" in reason
            cur.execute("SELECT status FROM management_questions WHERE question_id=%s", (question_id,))
            assert cur.fetchone() == ("cancelled",)
            cur.execute("SELECT status,trigger FROM management_cases WHERE case_id=%s",
                        (case["case_id"],))
            assert cur.fetchone() == ("resolved", "terminal_work_reconciled")
    finally:
        _cleanup(run["run_id"], [work_id], case_ids)


def test_missing_tool_source_reaps_without_waiting_for_silence():
    if not pulse.DB:
        pytest.skip("no DB")
    tenant = f"pulse-missing-{uuid.uuid4().hex}"
    work_id = f"999999991:999999992:qa_explore:0"
    try:
        pulse.beat(work_id, kind="tool-job", tenant_id=tenant, stage="queued",
                   progress="queued", expected_cadence_s=300)
        assert pulse.reap_orphans() == 1
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT status FROM agent_pulse WHERE work_id=%s", (work_id,))
            assert cur.fetchone() == ("reaped",)
    finally:
        with connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM agent_pulse WHERE work_id=%s", (work_id,))
