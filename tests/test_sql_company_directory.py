from __future__ import annotations

from pathlib import Path

import pytest

from agent_os.domain.organization import AgentStatus
from agent_os.infrastructure.sql_company_directory import SQLCompanyDirectory


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def directory(tmp_path):
    value = SQLCompanyDirectory(
        f"sqlite:///{tmp_path / 'company.sqlite3'}", create_schema=True,
    )
    try:
        yield value
    finally:
        value.close()


def hire(directory, *, role="security-specialist", key="hire-security"):
    return directory.hire_agent(
        tenant_id="tenant-a",
        role=role,
        team_id="engineering",
        manager_id="agent:engineering-manager",
        capabilities=("security-review", "threat-modeling"),
        tool_grants=("artifact.read",),
        hiring_authority=False,
        spending_limit_cents=250,
        actor_id="human:ceo",
        idempotency_key=key,
    )


def test_standing_agent_is_event_sourced_idempotent_and_tenant_scoped(directory):
    first = hire(directory)
    repeated = hire(directory)

    assert first["duplicate"] is False
    assert repeated["duplicate"] is True
    assert first["payload"]["agent_id"].startswith("agent:custom-")
    assert first["stream_version"] == 1
    company = directory.get_organization("tenant-a")
    agent = company.agents[first["payload"]["agent_id"]]
    assert agent.role == "security-specialist"
    assert agent.manager_id == "agent:engineering-manager"
    assert agent.capabilities == frozenset({"security-review", "threat-modeling"})
    assert agent.spending_limit_cents == 250
    assert first["payload"]["agent_id"] not in directory.get_organization("tenant-b").agents
    assert directory.list_company_events("tenant-a")[0]["event_id"] == first["event_id"]
    assert directory.list_company_events("tenant-b") == ()

    with pytest.raises(ValueError, match="different content"):
        hire(directory, role="different-role")


def test_retirement_preserves_history_and_management_invariants(directory):
    hired = hire(directory)
    agent_id = hired["payload"]["agent_id"]

    retired = directory.retire_agent(
        tenant_id="tenant-a",
        agent_id=agent_id,
        reason="Mission capacity is no longer needed",
        actor_id="human:ceo",
        idempotency_key="retire-security",
    )

    assert retired["stream_version"] == 2
    assert directory.get_organization("tenant-a").agents[agent_id].status is AgentStatus.RETIRED
    with pytest.raises(ValueError, match="already retired"):
        directory.retire_agent(
            tenant_id="tenant-a", agent_id=agent_id, reason="again",
            actor_id="human:ceo", idempotency_key="retire-again",
        )
    with pytest.raises(ValueError, match="mission manager cannot be retired"):
        directory.retire_agent(
            tenant_id="tenant-a", agent_id="agent:mission-manager", reason="unsafe",
            actor_id="human:ceo", idempotency_key="retire-chief",
        )


def test_hiring_rejects_unknown_authority_and_unbounded_capabilities(directory):
    with pytest.raises(ValueError, match="active standing agent"):
        directory.hire_agent(
            tenant_id="tenant-a", role="engineer", team_id="engineering",
            manager_id="agent:unknown", capabilities=(), tool_grants=(),
            hiring_authority=False, spending_limit_cents=0, actor_id="human:ceo",
            idempotency_key="bad-manager",
        )
    with pytest.raises(ValueError, match="bounded to 64"):
        directory.hire_agent(
            tenant_id="tenant-a", role="engineer", team_id="engineering",
            manager_id="agent:engineering-manager",
            capabilities=tuple(f"cap-{index}" for index in range(65)), tool_grants=(),
            hiring_authority=False, spending_limit_cents=0, actor_id="human:ceo",
            idempotency_key="too-many-caps",
        )


def test_company_directory_migration_is_tenant_fenced_and_append_only():
    migration = (ROOT / "postgres/initdb/95-company-directory-v2.sql").read_text()

    assert migration.count("ENABLE ROW LEVEL SECURITY") == 2
    assert migration.count("FORCE ROW LEVEL SECURITY") == 2
    assert "UNIQUE (tenant_id, stream_version)" in migration
    assert "GRANT SELECT, INSERT ON TABLE public.aos_v2_company_events" in migration
    assert "UPDATE ON TABLE public.aos_v2_company_events" not in migration
    assert "DELETE ON TABLE public.aos_v2_company_events" not in migration
