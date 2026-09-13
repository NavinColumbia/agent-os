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
    replay = directory.retire_agent(
        tenant_id="tenant-a", agent_id=agent_id,
        reason="Mission capacity is no longer needed",
        actor_id="human:ceo", idempotency_key="retire-security",
    )
    assert replay["duplicate"] is True
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


def test_approved_ai_staffing_proposal_atomically_promotes_bounded_standing_agents(directory):
    decided = directory.decide_hiring_proposal(
        tenant_id="tenant-a",
        proposal_id="proposal-security-team",
        approved=True,
        reason="The mission requires independent review capacity",
        role="security-specialist",
        requested_count=2,
        team_id="engineering",
        manager_id="agent:engineering-manager",
        capabilities=("security-review",),
        tool_grants=("artifact.read",),
        spending_limit_cents=125,
        actor_id="human:ceo",
    )
    replay = directory.decide_hiring_proposal(
        tenant_id="tenant-a",
        proposal_id="proposal-security-team",
        approved=True,
        reason="The mission requires independent review capacity",
        role="security-specialist",
        requested_count=2,
        team_id="engineering",
        manager_id="agent:engineering-manager",
        capabilities=("security-review",),
        tool_grants=("artifact.read",),
        spending_limit_cents=125,
        actor_id="human:ceo",
    )

    assert decided["kind"] == "hiring_proposal_decided"
    assert decided["payload"]["approved"] is True
    assert len(decided["payload"]["agents"]) == 2
    assert replay["duplicate"] is True
    organization = directory.get_organization("tenant-a")
    for raw in decided["payload"]["agents"]:
        assert organization.agents[raw["agent_id"]].role == "security-specialist"

    with pytest.raises(ValueError, match="different content"):
        directory.decide_hiring_proposal(
            tenant_id="tenant-a", proposal_id="proposal-security-team", approved=False,
            reason="Changed mind", role="security-specialist", requested_count=2,
            team_id=None, manager_id=None, capabilities=("security-review",),
            tool_grants=(), spending_limit_cents=0, actor_id="human:ceo",
        )


def test_rejected_staffing_proposal_records_decision_without_creating_agent(directory):
    before = len(directory.get_organization("tenant-a").agents)
    decision = directory.decide_hiring_proposal(
        tenant_id="tenant-a", proposal_id="proposal-vendor", approved=False,
        reason="Use an existing teammate", role="recruiter", requested_count=1,
        team_id=None, manager_id=None, capabilities=("sourcing",), tool_grants=(),
        spending_limit_cents=0, actor_id="human:ceo",
    )

    assert decision["payload"]["agents"] == []
    assert len(directory.get_organization("tenant-a").agents) == before


def test_human_staffing_waits_for_external_attestations_then_becomes_routable(directory):
    decision = directory.decide_hiring_proposal(
        tenant_id="tenant-a", proposal_id="proposal-human-designer", approved=True,
        participant_kind="human", reason="A human design lead is required",
        role="design-lead", requested_count=1, team_id="product",
        manager_id="agent:product-architect", capabilities=("design-review",),
        tool_grants=("artifact.read",), spending_limit_cents=0, actor_id="human:ceo",
    )
    onboarding = decision["payload"]["onboarding_cases"][0]
    onboarding_id = onboarding["onboarding_id"]
    assert onboarding["status"] == "awaiting_external_onboarding"
    assert "human:designer" not in directory.get_organization("tenant-a").humans

    with pytest.raises(ValueError, match="missing required attestations"):
        directory.confirm_external_onboarding(
            tenant_id="tenant-a", onboarding_id=onboarding_id,
            display_name="Design Lead", identity_subject="human:designer",
            response_sla_seconds=3_600, quality_criteria=("Review evidence",),
            attestations=("identity_verified",), actor_id="human:ceo",
            idempotency_key="confirm-human-designer",
        )
    confirmed = directory.confirm_external_onboarding(
        tenant_id="tenant-a", onboarding_id=onboarding_id,
        display_name="Design Lead", identity_subject="human:designer",
        response_sla_seconds=3_600, quality_criteria=("Review evidence",),
        attestations=("identity_verified", "terms_accepted", "access_approved"),
        actor_id="human:ceo", idempotency_key="confirm-human-designer",
    )
    replay = directory.confirm_external_onboarding(
        tenant_id="tenant-a", onboarding_id=onboarding_id,
        display_name="Design Lead", identity_subject="human:designer",
        response_sla_seconds=3_600, quality_criteria=("Review evidence",),
        attestations=("identity_verified", "terms_accepted", "access_approved"),
        actor_id="human:ceo", idempotency_key="confirm-human-designer",
    )

    assert confirmed["duplicate"] is False
    assert replay["duplicate"] is True
    case = directory.list_external_onboarding("tenant-a")[0]
    assert case["status"] == "active"
    human = directory.get_organization("tenant-a").humans["human:designer"]
    assert human.manager_id == "agent:product-architect"
    assert human.response_sla_seconds == 3_600


def test_vendor_staffing_requires_contract_attestation_and_projects_as_service(directory):
    decision = directory.decide_hiring_proposal(
        tenant_id="tenant-a", proposal_id="proposal-vendor-security", approved=True,
        participant_kind="vendor", reason="Independent penetration testing is required",
        role="penetration-test-vendor", requested_count=1, team_id="quality",
        manager_id="agent:quality-manager", capabilities=("penetration-testing",),
        tool_grants=(), spending_limit_cents=25_000, actor_id="human:ceo",
    )
    onboarding_id = decision["payload"]["onboarding_cases"][0]["onboarding_id"]
    confirmed = directory.confirm_external_onboarding(
        tenant_id="tenant-a", onboarding_id=onboarding_id,
        display_name="Independent Security Lab", identity_subject=None,
        response_sla_seconds=86_400, quality_criteria=("Signed findings report",),
        attestations=(
            "identity_verified", "terms_accepted", "access_approved",
            "vendor_contract_approved",
        ),
        actor_id="human:ceo", idempotency_key="confirm-security-vendor",
    )
    participant_id = confirmed["payload"]["participant_id"]
    service = directory.get_organization("tenant-a").services[participant_id]
    assert service.owner_id == "agent:quality-manager"
    assert service.capabilities == frozenset({"penetration-testing"})


def test_company_directory_migration_is_tenant_fenced_and_append_only():
    migration = (ROOT / "postgres/initdb/95-company-directory-v2.sql").read_text()
    extension = (ROOT / "postgres/initdb/96-company-proposal-decisions-v2.sql").read_text()
    onboarding = (ROOT / "postgres/initdb/99z-external-onboarding-v2.sql").read_text()

    assert migration.count("ENABLE ROW LEVEL SECURITY") == 2
    assert migration.count("FORCE ROW LEVEL SECURITY") == 2
    assert "UNIQUE (tenant_id, stream_version)" in migration
    assert "GRANT SELECT, INSERT ON TABLE public.aos_v2_company_events" in migration
    assert "UPDATE ON TABLE public.aos_v2_company_events" not in migration
    assert "DELETE ON TABLE public.aos_v2_company_events" not in migration
    assert "hiring_proposal_decided" in extension
    assert "external_participant_onboarded" in onboarding
