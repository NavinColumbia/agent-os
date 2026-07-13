#!/usr/bin/env python3
"""otel.py — map agent-os observability onto OpenTelemetry GenAI semantic conventions (IMPROVEMENTS-PLAN item 12).

Our `pulse` plane + `trace`/`audit` rows are bespoke. OTel's GenAI semantic conventions standardise agent
telemetry — operation types `create_agent` / `invoke_agent` / `invoke_workflow`, the `gen_ai.*` attribute
namespace, and a CLIENT (remote agent call) vs INTERNAL (local framework) span-kind distinction — so traces
become PORTABLE to any OTel backend (Grafana Tempo, Jaeger, Honeycomb) instead of proprietary. This module is a
pure, dependency-free MAPPER: given a pulse row / work descriptor it returns the standard operation name, span
kind, and attribute dict an exporter would emit. No hot-path change — exporters call it; the runtime doesn't.

    span_attrs(kind, name, ...) -> {"operation", "span_kind", "attributes": {gen_ai.*: ...}}
    pulse_to_span(row)          -> the same, derived from an agent_pulse row dict

Ref: OpenTelemetry GenAI semantic conventions (create_agent/invoke_agent/invoke_workflow; gen_ai.operation.name,
gen_ai.agent.name, gen_ai.provider.name, gen_ai.request.model, gen_ai.usage.*). Field names track the conventions
as of v1.4x; treat as the stable subset (a follow-up can widen coverage).
"""

# our internal work KIND -> the OTel GenAI operation it maps to
KIND_TO_OP = {
    "orchestra-run": "invoke_workflow", "company-run": "invoke_workflow", "qa-run": "invoke_workflow",
    "controller": "invoke_workflow", "loopcontroller": "invoke_workflow",
    "factory-build": "invoke_agent", "dev-fix": "invoke_agent", "agent": "invoke_agent",
    "tool": "invoke_agent", "qa-explore": "invoke_agent", "auditor": "invoke_agent",
    "spawn": "create_agent", "hire": "create_agent",
}
_VALID_OPS = {"create_agent", "invoke_agent", "invoke_workflow", "chat", "execute_tool"}


def operation_for(kind: str) -> str:
    """The OTel operation name for one of our work kinds (default invoke_agent — a generic agent activity)."""
    return KIND_TO_OP.get((kind or "").lower(), "invoke_agent")


def span_attrs(kind, name, *, model=None, provider="anthropic", tokens_in=None, tokens_out=None,
               tenant=None, remote=False, operation=None, extra=None) -> dict:
    """Build the OTel GenAI span descriptor for a unit of agentic work.
    span_kind: CLIENT for a call to a REMOTE agent/model, INTERNAL for local framework orchestration."""
    op = operation or operation_for(kind)
    if op not in _VALID_OPS:
        op = "invoke_agent"
    attrs = {"gen_ai.operation.name": op, "gen_ai.agent.name": name}
    if provider:
        attrs["gen_ai.provider.name"] = provider
    if model:
        attrs["gen_ai.request.model"] = model
    if tokens_in is not None:
        attrs["gen_ai.usage.input_tokens"] = int(tokens_in)
    if tokens_out is not None:
        attrs["gen_ai.usage.output_tokens"] = int(tokens_out)
    if tenant:
        attrs["gen_ai.agent.id"] = str(tenant)      # tenant as the owning-agent id dimension
    if extra:
        attrs.update(extra)
    return {"operation": op, "span_kind": "CLIENT" if remote else "INTERNAL", "attributes": attrs}


def pulse_to_span(row: dict) -> dict:
    """Map an agent_pulse row (dict with kind/work_id/label/tenant_id/meta/...) to an OTel span descriptor."""
    meta = row.get("meta") or {}
    return span_attrs(row.get("kind"), row.get("label") or row.get("work_id") or "work",
                      model=meta.get("model"), tenant=row.get("tenant_id"),
                      remote=bool(meta.get("remote")),
                      extra={"gen_ai.agent.id": str(row.get("work_id"))} if row.get("work_id") else None)


def active_spans():
    """Every in-flight unit of agentic work as a portable OTel GenAI span descriptor (from the pulse plane).
    An OTel exporter/bridge can emit these directly. Best-effort: returns [] if the pulse plane is unreachable."""
    try:
        import pulse
        return [pulse_to_span(r) for r in pulse._rows()]
    except Exception:
        return []


def _selftest():
    wf = span_attrs("orchestra-run", "CEO-Coordinator")
    assert wf["operation"] == "invoke_workflow" and wf["span_kind"] == "INTERNAL", wf
    ag = span_attrs("factory-build", "backend-engineer", model="claude-opus-4-8", tokens_in=100, tokens_out=50,
                    remote=True)
    assert ag["operation"] == "invoke_agent" and ag["span_kind"] == "CLIENT", ag
    assert ag["attributes"]["gen_ai.request.model"] == "claude-opus-4-8", ag
    assert ag["attributes"]["gen_ai.usage.input_tokens"] == 100 and ag["attributes"]["gen_ai.provider.name"] == "anthropic"
    sp = span_attrs("spawn", "researcher")
    assert sp["operation"] == "create_agent", sp
    ps = pulse_to_span({"kind": "qa-run", "work_id": "qa:demo", "label": "QA demo", "tenant_id": "t1",
                        "meta": {"model": "claude-sonnet-4-6"}})
    assert ps["operation"] == "invoke_workflow" and ps["attributes"]["gen_ai.request.model"] == "claude-sonnet-4-6"
    assert ps["attributes"]["gen_ai.operation.name"] == "invoke_workflow", ps
    # unknown kind falls back to a valid generic op
    assert span_attrs("weird-thing", "x")["operation"] == "invoke_agent"
    print("otel selftest: PASS (kind->operation mapping; gen_ai.* attributes; CLIENT/INTERNAL span kind; "
          "pulse row -> portable span)")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "active":     # live: dump in-flight work as OTel spans
        import json
        print(json.dumps(active_spans(), indent=2))
        sys.exit(0)
    sys.exit(_selftest())
