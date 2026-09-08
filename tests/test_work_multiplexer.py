from __future__ import annotations

from agent_os.application.command_worker import CommandRunReport, CommandRunStatus
from agent_os.application.graph_action_worker import GraphActionRunReport
from agent_os.application.work_multiplexer import TenantWorkMultiplexer


class LifecycleWorker:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def run_one(self, tenant_id):
        self.calls += 1
        status = self.statuses.pop(0)
        return CommandRunReport(status, "lifecycle" if status is not CommandRunStatus.IDLE else None)


class GraphWorker:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def run_one(self, tenant_id):
        self.calls += 1
        status = self.statuses.pop(0)
        return GraphActionRunReport(status, "graph" if status is not CommandRunStatus.IDLE else None)


def test_multiplexer_alternates_queue_preference_and_never_starves_graph_work():
    lifecycle = LifecycleWorker([CommandRunStatus.SUCCEEDED, CommandRunStatus.SUCCEEDED])
    graph = GraphWorker([CommandRunStatus.SUCCEEDED])
    worker = TenantWorkMultiplexer(  # type: ignore[arg-type]
        lifecycle_worker=lifecycle,
        graph_worker=graph,
    )

    first = worker.run_one("tenant-a")
    second = worker.run_one("tenant-a")

    assert first.command_id == "lifecycle"
    assert second.command_id == "graph"
    assert lifecycle.calls == 1
    assert graph.calls == 1


def test_multiplexer_falls_through_when_preferred_queue_is_idle():
    lifecycle = LifecycleWorker([CommandRunStatus.IDLE])
    graph = GraphWorker([CommandRunStatus.SUCCEEDED])
    worker = TenantWorkMultiplexer(  # type: ignore[arg-type]
        lifecycle_worker=lifecycle,
        graph_worker=graph,
    )

    report = worker.run_one("tenant-a")

    assert report.status is CommandRunStatus.SUCCEEDED
    assert report.command_id == "graph"
