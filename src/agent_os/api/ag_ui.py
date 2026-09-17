"""Version-pinned AG-UI edge translation for safe experience events.

AG-UI is an interoperability contract, not Agent OS's durable state model.
Only already-redacted experience events cross this boundary. Authority,
approval, workflow, and evidence records stay behind their native APIs.
"""

from __future__ import annotations

from typing import Any, Mapping

from ag_ui.core import CustomEvent


AG_UI_PROTOCOL_PACKAGE = "ag-ui-protocol"
AG_UI_PROTOCOL_VERSION = "0.1.22"
AG_UI_EVENT_NAME = "agent_os.experience.v1"
AG_UI_RESET_EVENT_NAME = "agent_os.stream.reset.v1"
AG_UI_CURSOR_EVENT_NAME = "agent_os.stream.cursor.v1"


def _dump(event: CustomEvent) -> dict[str, Any]:
    return event.model_dump(by_alias=True, exclude_none=True, mode="json")


def experience_event_to_ag_ui(event: Mapping[str, Any]) -> dict[str, Any]:
    """Translate one audience-filtered event without widening its disclosure."""

    value = {
        "eventId": str(event["event_id"]),
        "sequence": int(event["tenant_sequence"]),
        "occurredAt": str(event["occurred_at"]),
        "resource": {
            "type": str(event["resource_type"]),
            "id": str(event["resource_id"]),
            "revision": int(event["projection_revision"]),
        },
        "kind": str(event["kind"]),
        "summary": str(event["safe_summary"]),
    }
    trace_id = event.get("trace_id")
    if trace_id is not None:
        value["traceId"] = str(trace_id)
    return _dump(CustomEvent(
        name=AG_UI_EVENT_NAME,
        value=value,
        metadata={
            "agent-os": {
                "schemaVersion": 1,
                "sourceProtocol": "experience-event",
            },
        },
    ))


def stream_control_to_ag_ui(
    name: str,
    *,
    cursor: str,
    reset_required: bool | None = None,
) -> dict[str, Any]:
    if name not in {AG_UI_RESET_EVENT_NAME, AG_UI_CURSOR_EVENT_NAME}:
        raise ValueError("unsupported AG-UI stream control event")
    value: dict[str, Any] = {"cursor": cursor}
    if reset_required is not None:
        value["resetRequired"] = reset_required
    return _dump(CustomEvent(
        name=name,
        value=value,
        metadata={"agent-os": {"schemaVersion": 1}},
    ))
