"""Fairly alternate durable work queues within each tenant shard."""

from __future__ import annotations

from agent_os.application.command_worker import CommandRunReport, CommandRunStatus, DurableCommandWorker
from agent_os.application.graph_action_worker import DurableGraphActionWorker
from agent_os.application.worker_loop import TenantWorker


class TenantWorkMultiplexer:
    """Expose durable execution/management/delivery queues through one fair worker."""

    def __init__(
        self,
        *,
        lifecycle_worker: DurableCommandWorker,
        graph_worker: DurableGraphActionWorker,
        management_worker: TenantWorker | None = None,
        notification_worker: TenantWorker | None = None,
        web_push_worker: TenantWorker | None = None,
        decision_worker: TenantWorker | None = None,
    ) -> None:
        self._lifecycle = lifecycle_worker
        self._graph = graph_worker
        self._management = management_worker
        self._notification = notification_worker
        self._web_push = web_push_worker
        self._decision = decision_worker
        self._next_queue: dict[str, int] = {}

    @staticmethod
    def _graph_report(graph) -> CommandRunReport:
        return CommandRunReport(
            graph.status, graph.action_id, graph.attempt,
            graph.retry_after_seconds, graph.error_type,
        )

    def run_one(self, tenant_id: str) -> CommandRunReport:
        queues = [
            lambda: self._lifecycle.run_one(tenant_id),
            lambda: self._graph_report(self._graph.run_one(tenant_id)),
        ]
        if self._management is not None:
            queues.append(lambda: self._management.run_one(tenant_id))
        if self._notification is not None:
            queues.append(lambda: self._notification.run_one(tenant_id))
        if self._web_push is not None:
            queues.append(lambda: self._web_push.run_one(tenant_id))
        if self._decision is not None:
            queues.append(lambda: self._decision.run_one(tenant_id))
        start = self._next_queue.get(tenant_id, 0) % len(queues)
        self._next_queue[tenant_id] = (start + 1) % len(queues)
        for offset in range(len(queues)):
            report = queues[(start + offset) % len(queues)]()
            if report.status is not CommandRunStatus.IDLE:
                return report
        return CommandRunReport(CommandRunStatus.IDLE)
