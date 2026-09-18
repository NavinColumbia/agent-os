from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_os.api.app import create_app
from agent_os.application.command_worker import CommandRunReport, CommandRunStatus
from agent_os.application.worker_loop import CommandWorkerLoop
from agent_os.infrastructure.memory import InMemoryWorkflowEngine
from agent_os.infrastructure.telemetry import build_otlp_telemetry


class Identity:
    def authenticate(self, authorization, session):
        return {"sub": "owner", "org": "tenant", "roles": ["owner"]}


def test_http_spans_use_route_templates_and_return_a_correlation_id():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=Identity(),
        request_tracer=provider.get_tracer("test"),
    ))
    response = api.get("/health?secret=must-not-appear")
    assert response.status_code == 200
    assert len(response.headers["X-Trace-ID"]) == 32
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "GET /health"
    assert spans[0].attributes["http.route"] == "/health"
    assert "secret" not in str(spans[0].attributes)
    provider.shutdown()


def test_otlp_runtime_is_explicit_and_production_transport_is_bounded():
    assert build_otlp_telemetry(
        endpoint="", service_name="api", service_version="v1",
        environment="development", sample_ratio=1,
    ) is None
    with pytest.raises(ValueError, match="requires HTTPS"):
        build_otlp_telemetry(
            endpoint="http://collector.example.test/v1/traces",
            service_name="api", service_version="v1",
            environment="production", sample_ratio=1,
        )
    runtime = build_otlp_telemetry(
        endpoint="http://127.0.0.1:4318/v1/traces",
        service_name="api", service_version="v1",
        environment="production", sample_ratio=0.1,
    )
    assert runtime is not None
    runtime.close()


def test_worker_spans_capture_outcome_without_tenant_or_prompt_data():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "worker-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class Worker:
        def run_one(self, organization_id):
            assert organization_id == "secret-tenant-name"
            return CommandRunReport(
                CommandRunStatus.RETRY_SCHEDULED, "command-secret", 2, 5, "ProviderThrottle",
            )

    loop = CommandWorkerLoop(
        worker=Worker(), organization_ids=("secret-tenant-name",),
        tracer=provider.get_tracer("worker-test"),
    )
    assert loop.run_cycle()[0].status is CommandRunStatus.RETRY_SCHEDULED
    span = exporter.get_finished_spans()[0]
    assert span.name == "agent_os.worker.tenant_cycle"
    assert span.attributes["agent_os.command.status"] == "retry_scheduled"
    assert span.attributes["error.type"] == "ProviderThrottle"
    assert "secret-tenant-name" not in str(span.attributes)
    assert "command-secret" not in str(span.attributes)
    provider.shutdown()
