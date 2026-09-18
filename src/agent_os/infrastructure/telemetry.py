"""Vendor-neutral OTLP tracing with bounded, privacy-safe HTTP correlation."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased


@dataclass
class TelemetryRuntime:
    provider: TracerProvider
    tracer: object

    def close(self) -> None:
        self.provider.force_flush(timeout_millis=5_000)
        self.provider.shutdown()


def build_otlp_telemetry(
    *,
    endpoint: str,
    service_name: str,
    service_version: str,
    environment: str,
    sample_ratio: float,
) -> TelemetryRuntime | None:
    """Build an isolated provider; an empty endpoint explicitly disables export."""

    endpoint = endpoint.strip()
    if not endpoint:
        return None
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("OTLP traces endpoint must be an absolute HTTP(S) URL")
    if environment in {"staging", "production"} and parsed.scheme != "https" and parsed.hostname not in {
        "127.0.0.1", "localhost", "::1",
    }:
        raise ValueError("remote staging/production OTLP export requires HTTPS")
    if not 0 < sample_ratio <= 1:
        raise ValueError("OTLP trace sample ratio must be greater than zero and at most one")
    if not service_name.strip() or not service_version.strip():
        raise ValueError("telemetry service name and version are required")

    # Import only when export is enabled so offline/local use has no exporter side effects.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(
        resource=Resource.create({
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment.name": environment,
        }),
        sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    return TelemetryRuntime(
        provider=provider,
        tracer=provider.get_tracer(service_name, service_version),
    )
