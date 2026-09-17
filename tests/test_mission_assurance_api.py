from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from fastapi.testclient import TestClient

from agent_os.api.app import create_app
from agent_os.application.assurance import AssuranceKernel
from agent_os.infrastructure.authzen_policy import baseline_effect_policy
from agent_os.infrastructure.memory import InMemoryWorkflowEngine
from agent_os.infrastructure.sql_mission_control import SQLMissionControl
from agent_os.infrastructure.sql_mission_participants import SQLMissionParticipantStore


class Identity:
    def authenticate(self, authorization: str | None, session: str | None) -> Mapping[str, Any]:
        if authorization == "Bearer owner":
            return {"sub": "human:ceo", "org": "tenant-a", "roles": ["owner"]}
        if authorization == "Bearer other":
            return {"sub": "human:other", "org": "tenant-b", "roles": ["owner"]}
        if authorization == "Bearer client":
            return {"sub": "client-a", "org": "tenant-a", "roles": ["client"]}
        raise ValueError("authentication required")


def test_http_mission_control_routes_effect_through_assurance_and_budget(tmp_path):
    store = SQLMissionControl(
        f"sqlite:///{tmp_path / 'mission-api.sqlite3'}",
        assurance_kernel=AssuranceKernel(baseline_effect_policy()),
        create_schema=True,
    )
    participants = SQLMissionParticipantStore(
        f"sqlite:///{tmp_path / 'mission-api-participants.sqlite3'}", create_schema=True,
    )
    def close_stores():
        participants.close()
        store.close()

    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=Identity(), mission_control=store,
        mission_participant_store=participants, shutdown=close_stores,
    ))
    headers = {"Authorization": "Bearer owner"}
    now = datetime.now(timezone.utc)
    with api:
        created = api.post("/v2/missions", headers=headers, json={
            "mission_id": "mission-api",
            "objective": "Publish a verified preview",
            "budget_limit_cents": 1_000,
            "success_measures": ["preview is independently reachable"],
            "prohibited_effects": ["credential.export"],
        })
        assert created.status_code == 201

        delegated = api.post(
            "/v2/missions/mission-api/authorities", headers=headers, json={
                "grant_id": "grant-api",
                "delegate_id": "agent:release",
                "allowed_effects": ["deploy.preview"],
                "allowed_resources": ["preview:mission-api"],
                "budget_limit_cents": 500,
                "valid_from": (now - timedelta(minutes=1)).isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "delegation_chain": ["human:ceo", "agent:release"],
            },
        )
        assert delegated.status_code == 201

        admitted = api.post("/v2/missions/mission-api/effects", headers=headers, json={
            "effect_id": "effect-api",
            "actor_id": "agent:release",
            "authority_grant_id": "grant-api",
            "action": "deploy.preview",
            "resource": "preview:mission-api",
            "risk": "reversible",
            "estimated_cost_cents": 250,
            "reversible": True,
            "idempotency_key": "effect-api-key",
            "input_sha256": "d" * 64,
        })
        assert admitted.status_code == 202
        assert admitted.json()["status"] == "admitted"

        participants.grant_participant(
            tenant_id="tenant-a", mission_id="mission-api", subject_id="client-a",
            participation_role="client", actor_id="human:ceo",
            idempotency_key="assign-client-mission-api",
        )

        view = api.get("/v2/missions/mission-api/control", headers=headers)
        assert view.status_code == 200
        assert view.json()["budget"]["reserved_cents"] == 250
        assert view.json()["effects"][0]["decision"]["disposition"] == "allowed"
        client_view = api.get(
            "/v2/missions/mission-api/control",
            headers={"Authorization": "Bearer client"},
        )
        assert client_view.status_code == 200
        assert client_view.json()["projection"] == "stakeholder"
        assert client_view.json()["mission"]["objective"] == "Publish a verified preview"
        assert "budget" not in client_view.json()
        assert "authorities" not in client_view.json()
        assert "effects" not in client_view.json()
        assert client_view.json()["evidence"] == []
        assert api.get(
            "/v2/missions/mission-api/control",
            headers={"Authorization": "Bearer other"},
        ).status_code == 404


def test_directive_creates_canonical_mission_contract_when_store_is_configured(tmp_path):
    store = SQLMissionControl(
        f"sqlite:///{tmp_path / 'directive-mission.sqlite3'}",
        assurance_kernel=AssuranceKernel(baseline_effect_policy()),
        create_schema=True,
    )
    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=Identity(), mission_control=store,
        shutdown=store.close,
    ))
    with api:
        response = api.post(
            "/v2/runs",
            headers={"Authorization": "Bearer owner", "Idempotency-Key": "mission-run-one"},
            json={"prompt": "Build a customer portal", "budget_limit_cents": 2_000},
        )
        assert response.status_code == 202
        mission_id = response.json()["run_id"]
        view = api.get(f"/v2/missions/{mission_id}/control", headers={
            "Authorization": "Bearer owner",
        })
        assert view.status_code == 200
        assert view.json()["mission"]["objective"] == "Build a customer portal"
        assert view.json()["budget"]["available_cents"] == 2_000
        assert view.json()["authorities"][0]["delegate_id"] == "agent:mission-runtime"

        revision_body = {
            "expected_revision": 1,
            "reason": "Clarified the customer outcome",
            "objective": "Build a customer portal with SSO",
            "accountable_owner_id": "human:ceo",
            "budget_limit_cents": 2_500,
            "success_measures": ["SSO portal is independently verified"],
        }
        revised = api.put(
            f"/v2/missions/{mission_id}",
            headers={"Authorization": "Bearer owner"},
            json=revision_body,
        )
        assert revised.status_code == 200
        assert revised.json()["revision"] == 2
        retried = api.put(
            f"/v2/missions/{mission_id}",
            headers={"Authorization": "Bearer owner"},
            json=revision_body,
        )
        assert retried.status_code == 200
        assert retried.json()["duplicate"] is True
        control = api.get(
            f"/v2/missions/{mission_id}/control",
            headers={"Authorization": "Bearer owner"},
        ).json()
        assert [item["revision"] for item in control["mission_revisions"]] == [1, 2]
        assert {item["mission_revision"] for item in control["authorities"]} == {1, 2}
