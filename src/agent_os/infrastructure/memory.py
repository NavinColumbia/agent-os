"""Synchronous in-memory adapters for bounded API and contract tests."""

from __future__ import annotations

import hashlib
from threading import RLock

from agent_os.application.lifecycle import LifecycleHistory, event_fingerprint
from agent_os.application.ports import WorkflowEngine, WorkflowReceipt
from agent_os.domain.lifecycle import Event, EventKind, LifecycleState


class InMemoryWorkflowEngine(WorkflowEngine):
    def __init__(self) -> None:
        self._runs: dict[tuple[str, str], LifecycleHistory] = {}
        self._requests: set[str] = set()
        self._lock = RLock()

    @staticmethod
    def _receipt(kind: str, organization_id: str, run_id: str, suffix: str) -> str:
        raw = f"memory:{kind}:{organization_id}:{run_id}:{suffix}"
        return "memory-" + hashlib.sha256(raw.encode()).hexdigest()

    def start_run(
        self,
        initial_state: LifecycleState,
        initial_event: Event | None = None,
    ) -> WorkflowReceipt:
        suffix = "none" if initial_event is None else event_fingerprint(initial_event)
        workflow_id = self._receipt(
            "start", initial_state.organization_id, initial_state.run_id, suffix
        )
        key = (initial_state.organization_id, initial_state.run_id)
        with self._lock:
            duplicate = workflow_id in self._requests
            if not duplicate:
                existing = self._runs.get(key)
                if existing is None:
                    existing = LifecycleHistory(initial_state)
                    self._runs[key] = existing
                elif existing.initial_state != initial_state:
                    raise ValueError("run identity already exists with different initial state")
                if initial_event is not None:
                    existing.append(initial_event)
                self._requests.add(workflow_id)
        return WorkflowReceipt(workflow_id, duplicate=duplicate)

    def submit_event(
        self,
        organization_id: str,
        run_id: str,
        event: Event,
    ) -> WorkflowReceipt:
        workflow_id = self._receipt("event", organization_id, run_id, event_fingerprint(event))
        with self._lock:
            history = self._runs.get((organization_id, run_id))
            if history is None:
                raise LookupError("lifecycle run does not exist for this organization")
            duplicate = workflow_id in self._requests
            if not duplicate:
                history.append(event)
                self._requests.add(workflow_id)
        return WorkflowReceipt(workflow_id, duplicate=duplicate)

    def get_run(self, organization_id: str, run_id: str) -> LifecycleState | None:
        with self._lock:
            history = self._runs.get((organization_id, run_id))
            return None if history is None else history.state

    def cancel_run(
        self,
        organization_id: str,
        run_id: str,
        *,
        reason: str,
        expected_version: int,
        event_id: str,
    ) -> WorkflowReceipt:
        return self.submit_event(
            organization_id,
            run_id,
            Event(event_id, EventKind.CANCEL_REQUESTED, expected_version, {"reason": reason}),
        )

    def health(self):
        return {"ok": True, "workflow_engine": "memory", "database": "memory"}
