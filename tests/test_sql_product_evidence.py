from __future__ import annotations

from pathlib import Path

import pytest

from agent_os.infrastructure.sql_product_evidence import SQLProductEvidenceStore


ROOT = Path(__file__).resolve().parents[1]


def study(*, require_grounding: bool = True) -> dict:
    return {
        "study_id": "workspace-navigation",
        "revision": 1,
        "hypothesis": "The candidate reduces time-to-correct-action.",
        "baseline_variant_id": "baseline",
        "candidate_variant_id": "candidate",
        "canonical_task_ids": ["find-blocker"],
        "metrics": [{
            "metric_id": "success",
            "direction": "higher",
            "minimum_delta": 0.05,
            "hard_gate": True,
        }],
        "representative_segments": ["new-owner"],
        "synthetic_repetitions": 3,
        "require_human_or_production": require_grounding,
    }


def observation(observation_id: str, variant: str, value: float, kind: str) -> dict:
    return {
        "observation_id": observation_id,
        "study_id": "workspace-navigation",
        "study_revision": 1,
        "task_id": "find-blocker",
        "segment_id": "new-owner",
        "variant_id": variant,
        "evaluator_id": "evaluator-1",
        "evidence_kind": kind,
        "metrics": {"success": value},
        "evidence_ids": [f"artifact-{observation_id}"],
        "repeat_index": 0,
        "presentation_position": None,
        "blinded": False,
        "critical_failures": [],
    }


def outcome(system_id: str, evidence_id: str, **overrides) -> dict:
    value = {
        "system_id": system_id,
        "task_count": 10,
        "success_rate": 0.8,
        "quality_score": 0.8,
        "reliability_rate": 0.9,
        "p95_latency_seconds": 30,
        "model_cost_cents": 100,
        "human_minutes": 60,
        "interventions": 4,
        "evidence_ids": [evidence_id],
    }
    value.update(overrides)
    return value


def test_store_is_append_only_tenant_scoped_and_binds_each_decision(tmp_path):
    store = SQLProductEvidenceStore(
        f"sqlite:///{tmp_path / 'evidence.sqlite3'}", create_schema=True,
    )
    created = store.create_study(
        tenant_id="tenant-a", study=study(), created_by="owner-a",
        idempotency_key="create-study-001",
    )
    assert created["duplicate"] is False
    assert store.create_study(
        tenant_id="tenant-a", study=study(), created_by="owner-a",
        idempotency_key="create-study-001",
    )["duplicate"] is True
    assert store.list_studies("tenant-b") == ()
    with pytest.raises(ValueError, match="reused"):
        changed = {**study(), "hypothesis": "Mutated after registration"}
        store.create_study(
            tenant_id="tenant-a", study=changed, created_by="owner-a",
            idempotency_key="create-study-001",
        )

    for index, (variant, score) in enumerate((("baseline", 0.6), ("candidate", 0.9))):
        store.append_observation(
            tenant_id="tenant-a",
            observation=observation(f"det-{index}", variant, score, "deterministic"),
            recorded_by="reviewer-a", idempotency_key=f"observation-det-{index}",
        )
    first = store.evaluate_study(
        tenant_id="tenant-a", study_id="workspace-navigation", revision=1,
        decided_by="manager-a",
    )
    assert first is not None
    assert first["disposition"] == "human_validation_required"

    for index, (variant, score) in enumerate((("baseline", 0.6), ("candidate", 0.9))):
        store.append_observation(
            tenant_id="tenant-a",
            observation=observation(f"prod-{index}", variant, score, "production"),
            recorded_by="manager-a", idempotency_key=f"observation-prod-{index}",
        )
    second = store.evaluate_study(
        tenant_id="tenant-a", study_id="workspace-navigation", revision=1,
        decided_by="manager-a",
    )
    assert second is not None
    assert second["disposition"] == "adopt"
    assert second["observation_set_sha256"] != first["observation_set_sha256"]
    complete = store.get_study("tenant-a", "workspace-navigation", 1)
    assert complete is not None
    assert len(complete["observations"]) == 4
    assert len(complete["decisions"]) == 2
    store.close()


def test_store_rejects_unregistered_observations_and_persists_value_receipts(tmp_path):
    store = SQLProductEvidenceStore(
        f"sqlite:///{tmp_path / 'evidence.sqlite3'}", create_schema=True,
    )
    store.create_study(
        tenant_id="tenant-a", study=study(), created_by="owner-a",
        idempotency_key="create-study-001",
    )
    invalid = observation("wrong-task", "candidate", 0.9, "deterministic")
    invalid["task_id"] = "unregistered"
    with pytest.raises(ValueError, match="task is not registered"):
        store.append_observation(
            tenant_id="tenant-a", observation=invalid, recorded_by="reviewer-a",
            idempotency_key="wrong-task-observation",
        )

    receipt = store.record_value_receipt(
        tenant_id="tenant-a",
        baseline=outcome("manual", "artifact-manual"),
        candidate=outcome(
            "agent-os", "artifact-agent-os", success_rate=0.9,
            quality_score=0.9, reliability_rate=0.95, model_cost_cents=120,
            human_minutes=20, interventions=1,
        ),
        customer_price_cents=100,
        human_hourly_value_cents=6_000,
        maximum_latency_regression_seconds=10,
        created_by="owner-a",
        idempotency_key="value-receipt-001",
    )
    assert receipt["dominates_baseline"] is True
    assert receipt["duplicate"] is False
    duplicate = store.record_value_receipt(
        tenant_id="tenant-a",
        baseline=outcome("manual", "artifact-manual"),
        candidate=outcome(
            "agent-os", "artifact-agent-os", success_rate=0.9,
            quality_score=0.9, reliability_rate=0.95, model_cost_cents=120,
            human_minutes=20, interventions=1,
        ),
        customer_price_cents=100,
        human_hourly_value_cents=6_000,
        maximum_latency_regression_seconds=10,
        created_by="owner-a",
        idempotency_key="value-receipt-001",
    )
    assert duplicate["duplicate"] is True
    assert len(store.list_value_receipts("tenant-a")) == 1
    assert store.list_value_receipts("tenant-b") == ()
    store.close()


def test_product_evidence_migration_is_rls_fenced_and_immutable():
    migration = (ROOT / "postgres/initdb/110-product-evidence-v2.sql").read_text()
    for table in (
        "aos_v2_product_studies",
        "aos_v2_product_observations",
        "aos_v2_product_decisions",
        "aos_v2_product_value_receipts",
    ):
        assert table in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "current_setting(''app.tenant_id'', true)" in migration
    assert "GRANT SELECT, INSERT ON TABLE" in migration
    assert "GRANT SELECT, INSERT, UPDATE" not in migration
    assert "GRANT SELECT, INSERT, DELETE" not in migration
