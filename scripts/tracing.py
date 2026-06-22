#!/usr/bin/env python3
"""tracing.py — OpenTelemetry GenAI-convention tracing for agent/LLM calls (ADR 0004 K6).

Emits spans using the emerging OTel GenAI semantic conventions (gen_ai.*). Exports to console
here; in production point the OTLP exporter at a local Arize Phoenix (self-hosted, Postgres-backed).
Wrap any agent LLM/tool call in `agent_span(...)` to get cost/latency/token traces.

    tracing.py demo
Run with the agent-os venv python.
"""
import sys
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter

_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(_provider)
_tracer = trace.get_tracer("agent-os")


@contextmanager
def agent_span(operation, model, agent_name, tool=None):
    """Span with OTel GenAI attributes. Set tokens via the yielded span."""
    name = f"{operation} {agent_name}"
    with _tracer.start_as_current_span(name) as span:
        span.set_attribute("gen_ai.operation.name", operation)   # e.g. chat | invoke_agent | execute_tool
        span.set_attribute("gen_ai.system", "anthropic")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.agent.name", agent_name)
        if tool:
            span.set_attribute("gen_ai.tool.name", tool)
        yield span


def _demo():
    with agent_span("invoke_agent", "claude-opus-4-8", "builder") as parent:
        with agent_span("execute_tool", "claude-opus-4-8", "builder", tool="Edit") as t:
            t.set_attribute("gen_ai.tool.call.arguments", '{"path":"src/app.py"}')
        with agent_span("chat", "claude-opus-4-8", "builder") as c:
            c.set_attribute("gen_ai.usage.input_tokens", 1200)
            c.set_attribute("gen_ai.usage.output_tokens", 340)
        parent.set_attribute("gen_ai.usage.input_tokens", 1200)
    print("\nPASS: GenAI-convention spans emitted (gen_ai.* attrs above) ✅")


if __name__ == "__main__":
    _demo() if (len(sys.argv) > 1 and sys.argv[1] == "demo") else _demo()
