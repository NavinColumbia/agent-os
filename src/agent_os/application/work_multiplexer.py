"""Fairly alternate lifecycle and graph queues within each tenant shard."""

from __future__ import annotations

from agent_os.application.command_worker import CommandRunReport, CommandRunStatus, DurableCommandWorker
from agent_os.application.graph_action_worker import DurableGraphActionWorker


class TenantWorkMultiplexer:
    """Expose two durable queues as one fair tenant worker to the supervisor."""

    def __init__(
        self,
        *,
        lifecycle_worker: DurableCommandWorker,
        graph_worker: DurableGraphActionWorker,
    ) -> None:
        self._lifecycle = lifecycle_worker
        self._graph = graph_worker
        self._prefer_graph: dict[str, bool] = {}

    def run_one(self, tenant_id: str) -> CommandRunReport:
        prefer_graph = self._prefer_graph.get(tenant_id, False)
        self._prefer_graph[tenant_id] = not prefer_graph
        if prefer_graph:
            graph = self._graph.run_one(tenant_id)
            if graph.status is not CommandRunStatus.IDLE:
                return CommandRunReport(
                    graph.status, graph.action_id, graph.attempt,
                    graph.retry_after_seconds, graph.error_type,
                )
            return self._lifecycle.run_one(tenant_id)
        lifecycle = self._lifecycle.run_one(tenant_id)
        if lifecycle.status is not CommandRunStatus.IDLE:
            return lifecycle
        graph = self._graph.run_one(tenant_id)
        return CommandRunReport(
            graph.status, graph.action_id, graph.attempt,
            graph.retry_after_seconds, graph.error_type,
        )
