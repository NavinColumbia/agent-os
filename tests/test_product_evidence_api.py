from __future__ import annotations

from typing import Any, Mapping

from fastapi.testclient import TestClient
import pytest

from agent_os.api.app import create_app
from agent_os.infrastructure.memory import InMemoryWorkflowEngine
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore
from agent_os.infrastructure.sql_product_evidence import SQLProductEvidenceStore


class Identity:
    def authenticate(self, authorization: str | None, session: str | None) -> Mapping[str, Any]:
        identities = {
            "Bearer owner-a": {"sub": "owner-a", "org": "tenant-a", "roles": ["owner"]},
            "Bearer agent-a": {"sub": "agent-a", "org": "tenant-a", "roles": ["agent"]},
            "Bearer viewer-a": {"sub": "viewer-a", "org": "tenant-a", "roles": ["viewer"]},
            "Bearer owner-b": {"sub": "owner-b", "org": "tenant-b", "roles": ["owner"]},
        }
        if authorization not in identities:
            raise ValueError("authentication required")
        return identities[authorization]


def headers(identity: str, key: str | None = None) -> dict[str, str]:
    result = {"Authorization": f"Bearer {identity}"}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def study() -> dict:
    return {
        "study_id": "navigation",
        "revision": 1,
        "hypothesis": "The candidate makes the next action clearer.",
        "baseline_variant_id": "baseline",
        "candidate_variant_id": "candidate",
        "canonical_task_ids": ["find-next-action"],
        "metrics": [{
            "metric_id": "success",
            "direction": "higher",
            "minimum_delta": 0.05,
            "hard_gate": True,
        }],
        "representative_segments": ["new-owner"],
        "synthetic_repetitions": 3,
        "require_human_or_production": True,
    }


def observation(evidence_id: str, *, kind: str = "synthetic") -> dict:
    return {
        "observation_id": f"observation-{kind}",
        "task_id": "find-next-action",
        "segment_id": "new-owner",
        "variant_id": "candidate",
        "evaluator_id": "evaluator-1",
        "evidence_kind": kind,
        "metrics": {"success": 0.9},
        "evidence_ids": [evidence_id],
        "repeat_index": 0,
        "presentation_position": 1,
        "blinded": True,
        "critical_failures": [],
    }


def system(system_id: str, evidence_id: str, *, candidate: bool) -> dict:
    return {
        "system_id": system_id,
        "task_count": 10,
        "success_rate": 0.9 if candidate else 0.8,
        "quality_score": 0.9 if candidate else 0.8,
        "reliability_rate": 0.95 if candidate else 0.9,
        "p95_latency_seconds": 25,
        "model_cost_cents": 120 if candidate else 100,
        "human_minutes": 20 if candidate else 60,
        "interventions": 1 if candidate else 4,
        "evidence_ids": [evidence_id],
    }


def test_api_enforces_tenant_rbac_grounding_and_value_receipts(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'evidence-api.sqlite3'}"
    artifacts = SQLArtifactStore(database_url, create_schema=True)
    evidence = SQLProductEvidenceStore(database_url, create_schema=True)

    def close() -> None:
        evidence.close()
        artifacts.close()

    api = TestClient(create_app(
        engine=InMemoryWorkflowEngine(), identity=Identity(),
        artifact_store=artifacts, product_evidence_store=evidence, shutdown=close,
    ))
    with api:
        created = api.post(
            "/v2/product-studies", headers=headers("owner-a", "create-study-01"),
            json=study(),
        )
        assert created.status_code == 201, created.text
        assert api.get(
            "/v2/product-studies", headers=headers("owner-b"),
        ).json()["items"] == []
        assert api.get(
            "/v2/product-studies", headers=headers("viewer-a"),
        ).status_code == 403

        missing = api.post(
            "/v2/product-studies/navigation/revisions/1/observations",
            headers=headers("agent-a", "missing-evidence-01"),
            json=observation("artifact-missing"),
        )
        assert missing.status_code == 409
        artifact_id = artifacts.put(
            organization_id="tenant-a", content=b'{"result":"measured"}',
            media_type="application/json", idempotency_key="raw-evidence-01",
        )
        accepted = api.post(
            "/v2/product-studies/navigation/revisions/1/observations",
            headers=headers("agent-a", "synthetic-observation-01"),
            json=observation(artifact_id),
        )
        assert accepted.status_code == 201, accepted.text
        calibration = api.get(
            "/v2/product-studies/navigation/revisions/1/calibration",
            headers=headers("owner-a"),
        )
        assert calibration.status_code == 200, calibration.text
        assert calibration.json()["paired_cells"] == 0
        assert calibration.json()["synthetic_protocol_valid"] is False
        assert api.get(
            "/v2/product-studies/navigation/revisions/1/calibration",
            headers=headers("owner-b"),
        ).status_code == 404
        assert api.get(
            "/v2/product-studies/navigation/revisions/1/calibration",
            headers=headers("viewer-a"),
        ).status_code == 403
        forbidden_human = api.post(
            "/v2/product-studies/navigation/revisions/1/observations",
            headers=headers("agent-a", "human-observation-01"),
            json=observation(artifact_id, kind="human"),
        )
        assert forbidden_human.status_code == 403
        assert api.post(
            "/v2/product-studies/navigation/revisions/1/evaluate",
            headers=headers("agent-a"),
        ).status_code == 403
        decision = api.post(
            "/v2/product-studies/navigation/revisions/1/evaluate",
            headers=headers("owner-a"),
        )
        assert decision.status_code == 200, decision.text
        assert decision.json()["disposition"] == "insufficient_evidence"

        receipt = api.post(
            "/v2/value-receipts", headers=headers("owner-a", "value-receipt-01"),
            json={
                "baseline": system("manual", artifact_id, candidate=False),
                "candidate": system("agent-os", artifact_id, candidate=True),
                "customer_price_cents": 100,
                "human_hourly_value_cents": 6_000,
                "maximum_latency_regression_seconds": 10,
            },
        )
        assert receipt.status_code == 201, receipt.text
        assert receipt.json()["dominates_baseline"] is True
        assert len(api.get(
            "/v2/value-receipts", headers=headers("owner-a"),
        ).json()["items"]) == 1
        assert api.get(
            "/v2/value-receipts", headers=headers("owner-b"),
        ).json()["items"] == []


def test_product_evidence_api_requires_an_artifact_store(tmp_path):
    evidence = SQLProductEvidenceStore(
        f"sqlite:///{tmp_path / 'orphan.sqlite3'}", create_schema=True,
    )
    with pytest.raises(ValueError, match="requires a durable artifact store"):
        create_app(
            engine=InMemoryWorkflowEngine(), identity=Identity(),
            product_evidence_store=evidence,
        )
    evidence.close()
