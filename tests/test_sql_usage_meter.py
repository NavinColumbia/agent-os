from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_os.infrastructure.sql_usage_meter import SQLUsageMeter, UsageQuotaExceeded


def usage(*, cost_micros: int | None = 12_345) -> dict[str, int | None]:
    return {
        "requests": 2,
        "tool_calls": 1,
        "input_tokens": 300,
        "output_tokens": 100,
        "total_tokens": 400,
        "provider_cost_usd_micros": cost_micros,
    }


def test_usage_reservation_settlement_and_summary_are_idempotent_and_tenant_scoped(tmp_path):
    meter = SQLUsageMeter(
        f"sqlite:///{tmp_path / 'usage.sqlite3'}", monthly_budget_cents=100,
        create_schema=True,
    )
    try:
        reservation = meter.reserve_model_turn(
            tenant_id="tenant-a", source_id="action-1", run_id="run-1",
            category="graph_agent", model="provider:model", maximum_cost_cents=25,
        )
        duplicate = meter.reserve_model_turn(
            tenant_id="tenant-a", source_id="action-1", run_id="run-1",
            category="graph_agent", model="provider:model", maximum_cost_cents=25,
        )
        assert reservation["duplicate"] is False
        assert duplicate["duplicate"] is True

        settled = meter.settle_model_turn(
            tenant_id="tenant-a", source_id="action-1", usage=usage(),
        )
        replayed = meter.settle_model_turn(
            tenant_id="tenant-a", source_id="action-1", usage=usage(),
        )
        assert settled["charged_cost_cents"] == 2
        assert replayed["duplicate"] is True
        assert meter.usage_summary("tenant-a") == {
            "billing_period": datetime.now(timezone.utc).strftime("%Y-%m"),
            "monthly_budget_cents": 100,
            "committed_cents": 2,
            "remaining_cents": 98,
            "reserved_ceiling_cents": 0,
            "settled_charged_cents": 2,
            "known_provider_cost_usd_micros": 12_345,
            "unknown_cost_events": 0,
            "events": 1,
            "input_tokens": 300,
            "output_tokens": 100,
            "total_tokens": 400,
        }
        assert meter.usage_summary("tenant-b")["events"] == 0
        assert meter.list_usage_events("tenant-b") == ()
        assert meter.list_usage_events("tenant-a")[0]["source_id"] == "action-1"
    finally:
        meter.close()


def test_unknown_cost_keeps_reserved_ceiling_and_quota_blocks_before_provider_work(tmp_path):
    meter = SQLUsageMeter(
        f"sqlite:///{tmp_path / 'quota.sqlite3'}", monthly_budget_cents=30,
        create_schema=True,
    )
    try:
        meter.reserve_model_turn(
            tenant_id="tenant-a", source_id="action-1", run_id="run-1",
            category="graph_agent", model="new-model", maximum_cost_cents=20,
        )
        with pytest.raises(UsageQuotaExceeded, match="budget exhausted"):
            meter.reserve_model_turn(
                tenant_id="tenant-a", source_id="action-2", run_id="run-1",
                category="graph_agent", model="new-model", maximum_cost_cents=20,
            )
        meter.settle_model_turn(
            tenant_id="tenant-a", source_id="action-1", usage=usage(cost_micros=None),
        )
        summary = meter.usage_summary("tenant-a")
        assert summary["committed_cents"] == 20
        assert summary["unknown_cost_events"] == 1

        with pytest.raises(ValueError, match="different reservation"):
            meter.reserve_model_turn(
                tenant_id="tenant-a", source_id="action-1", run_id="different-run",
                category="graph_agent", model="new-model", maximum_cost_cents=20,
            )
        with pytest.raises(ValueError, match="total_tokens"):
            meter.settle_model_turn(
                tenant_id="tenant-a", source_id="action-1",
                usage={**usage(), "total_tokens": 401},
            )
    finally:
        meter.close()
