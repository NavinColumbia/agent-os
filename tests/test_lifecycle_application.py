"""Application-boundary tests for lifecycle replay and command scheduling."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_os.application.lifecycle import LifecycleHistory  # noqa: E402
from agent_os.domain.lifecycle import (  # noqa: E402
    Event,
    EventKind,
    LifecyclePhase,
    LifecycleState,
    TransitionRejected,
)


def event(event_id: str, kind: EventKind, version: int, **payload) -> Event:
    return Event(event_id, kind, expected_version=version, payload=payload)


def test_command_identity_is_stable_across_full_history_replay():
    initial = LifecycleState(run_id="run-1", organization_id="org-1")
    history = LifecycleHistory(initial)
    events = [
        event("scope", EventKind.SCOPE_ACCEPTED, 0, brief_id="brief-1"),
        event("research", EventKind.RESEARCH_COMPLETED, 1, report_id="report-1"),
        event("spec", EventKind.SPECIFICATION_APPROVED, 2, specification_id="spec-1"),
    ]
    original_ids = [history.append(item).commands[0].command_id for item in events]

    replayed = LifecycleHistory(initial)
    replayed_ids = [replayed.append(item).commands[0].command_id for item in events]

    assert replayed.state == history.state
    assert replayed.events == tuple(events)
    assert replayed_ids == original_ids
    assert len(set(original_ids)) == len(original_ids)


def test_old_event_redelivery_is_suppressed_after_later_transitions():
    initial = LifecycleState(run_id="run-1", organization_id="org-1")
    history = LifecycleHistory(initial)
    first = event("scope", EventKind.SCOPE_ACCEPTED, 0)
    history.append(first)
    history.append(event("research", EventKind.RESEARCH_COMPLETED, 1))

    duplicate = history.append(first)

    assert duplicate.transition.duplicate is True
    assert duplicate.commands == ()
    assert history.state.phase is LifecyclePhase.SPECIFY
    assert history.state.version == 2
    assert len(history.events) == 2


def test_reusing_event_id_with_different_content_fails_closed():
    history = LifecycleHistory(LifecycleState(run_id="run-1", organization_id="org-1"))
    history.append(event("same", EventKind.SCOPE_ACCEPTED, 0, brief_id="one"))

    with pytest.raises(TransitionRejected, match="reused"):
        history.append(event("same", EventKind.SCOPE_ACCEPTED, 0, brief_id="two"))


def test_non_json_payload_is_rejected_before_it_reaches_workflow_history():
    history = LifecycleHistory(LifecycleState(run_id="run-1", organization_id="org-1"))

    with pytest.raises(TransitionRejected, match="JSON-compatible"):
        history.append(event("bad", EventKind.SCOPE_ACCEPTED, 0, object=object()))


def test_replay_rejects_out_of_order_or_stale_history():
    initial = LifecycleState(run_id="run-1", organization_id="org-1")
    events = [
        event("scope", EventKind.SCOPE_ACCEPTED, 0),
        event("stale", EventKind.RESEARCH_COMPLETED, 0),
    ]

    with pytest.raises(TransitionRejected, match="stale event version"):
        LifecycleHistory.replay(initial, events)


def test_application_package_has_no_runtime_or_infrastructure_imports():
    forbidden = {
        "dbos", "fastapi", "psycopg", "pydantic_ai", "sqlalchemy", "temporalio",
    }
    violations = []
    for path in (ROOT / "src" / "agent_os" / "application").glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module.split(".", 1)[0]]
            else:
                continue
            violations.extend(f"{path.name}:{module}" for module in modules if module in forbidden)
    assert violations == []
