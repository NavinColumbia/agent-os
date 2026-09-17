from importlib.metadata import version

from ag_ui.core import CustomEvent

from agent_os.api.ag_ui import (
    AG_UI_CURSOR_EVENT_NAME,
    AG_UI_EVENT_NAME,
    AG_UI_PROTOCOL_VERSION,
    AG_UI_RESET_EVENT_NAME,
    experience_event_to_ag_ui,
    stream_control_to_ag_ui,
)


def test_experience_adapter_is_pinned_valid_and_disclosure_narrowing():
    assert version("ag-ui-protocol") == AG_UI_PROTOCOL_VERSION == "0.1.22"
    source = {
        "tenant_id": "secret-tenant",
        "tenant_sequence": 17,
        "event_id": "experience-17",
        "resource_type": "mission",
        "resource_id": "mission-one",
        "projection_revision": 4,
        "kind": "mission.evidence.added",
        "audience_ids": ["human:ceo", "secret-reviewer"],
        "safe_summary": "Mission evidence recorded.",
        "trace_id": "trace-safe",
        "occurred_at": "2026-09-17T12:00:00+00:00",
        "private_payload": "must never cross the adapter",
    }

    encoded = experience_event_to_ag_ui(source)
    parsed = CustomEvent.model_validate(encoded)
    assert parsed.name == AG_UI_EVENT_NAME
    assert encoded == {
        "metadata": {
            "agent-os": {
                "schemaVersion": 1,
                "sourceProtocol": "experience-event",
            },
        },
        "type": "CUSTOM",
        "name": "agent_os.experience.v1",
        "value": {
            "eventId": "experience-17",
            "sequence": 17,
            "occurredAt": "2026-09-17T12:00:00+00:00",
            "resource": {"type": "mission", "id": "mission-one", "revision": 4},
            "kind": "mission.evidence.added",
            "summary": "Mission evidence recorded.",
            "traceId": "trace-safe",
        },
    }
    assert "secret-tenant" not in str(encoded)
    assert "secret-reviewer" not in str(encoded)
    assert "private_payload" not in str(encoded)


def test_stream_control_events_use_the_same_official_custom_event_contract():
    reset = stream_control_to_ag_ui(
        AG_UI_RESET_EVENT_NAME, cursor="opaque-reset", reset_required=True,
    )
    cursor = stream_control_to_ag_ui(
        AG_UI_CURSOR_EVENT_NAME, cursor="opaque-cursor",
    )
    assert CustomEvent.model_validate(reset).value == {
        "cursor": "opaque-reset", "resetRequired": True,
    }
    assert CustomEvent.model_validate(cursor).value == {"cursor": "opaque-cursor"}
