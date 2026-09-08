from __future__ import annotations

import pytest

from agent_os.domain.organization import (
    AgentProfile,
    Audience,
    EscalationAction,
    EscalationLevel,
    EscalationPolicy,
    HumanParticipant,
    Message,
    MessageKind,
    Organization,
    ReviewRecord,
    ServiceParticipant,
    Team,
    WorkContract,
    WorkGraph,
    WorkStatus,
)


def organization() -> Organization:
    agents = {
        "ceo": AgentProfile("ceo", "CEO", "exec", hiring_authority=True),
        "eng-manager": AgentProfile("eng-manager", "Engineering Manager", "eng", manager_id="ceo", hiring_authority=True),
        "builder": AgentProfile("builder", "Builder", "eng", manager_id="eng-manager"),
        "qa": AgentProfile("qa", "QA", "quality", manager_id="ceo"),
    }
    teams = {
        "exec": Team("exec", "Executive", "Direct the company", "ceo"),
        "eng": Team("eng", "Engineering", "Build the product", "eng-manager"),
        "quality": Team("quality", "Quality", "Independently verify", "qa"),
    }
    humans = {
        "recruiter": HumanParticipant(
            "recruiter", "Riley", "exec", ("recruit specialists",), manager_id="ceo",
            response_sla_seconds=3600, quality_criteria=("candidate meets role rubric",),
        )
    }
    services = {
        "jira": ServiceParticipant("jira", "Customer Jira", frozenset({"ticket.read", "ticket.write"}), "recruiter")
    }
    return Organization("tenant-1", "org-1", "Acme AI", teams, agents, humans, services)


def test_agents_can_communicate_peer_upward_cross_team_and_to_humans():
    org = organization()
    base = dict(
        message_id="m1", conversation_id="c1", sender_id="builder",
        subject="Release risk", body="The vendor contract changed", created_at="now",
        kind=MessageKind.RISK,
    )
    assert org.route(Message(**base, audience=Audience.DIRECT, recipient_ids=("qa", "ceo"))) == ("qa", "ceo")
    assert org.route(Message(**base, audience=Audience.HUMAN, recipient_ids=("recruiter",))) == ("recruiter",)
    assert set(org.route(Message(**base, audience=Audience.ORGANIZATION, recipient_ids=()))) == set(org.agents)


def test_managers_can_monitor_reports_and_authorized_agents_can_hire():
    org = organization()
    assert [agent.agent_id for agent in org.direct_reports("eng-manager")] == ["builder"]
    assert org.can_hire("eng-manager") is True
    assert org.can_hire("builder") is False


def test_work_is_a_parallel_dependency_graph_with_truthful_progress():
    research = WorkContract("research", "Research APIs", "ceo", "eng-manager", ("builder",), status=WorkStatus.READY)
    security = WorkContract("security", "Threat model", "ceo", "qa", ("qa",), status=WorkStatus.READY)
    build = WorkContract(
        "build", "Implement", "eng-manager", "eng-manager", ("builder",),
        dependency_ids=frozenset({"research", "security"}),
    )
    graph = WorkGraph({item.work_id: item for item in (research, security, build)})
    assert {item.work_id for item in graph.ready()} == {"research", "security"}
    progressed = research.report_progress(percent=70, observed_at="t1", next_update_at="t2")
    assert progressed.progress_percent == 70 and progressed.revision == 1
    with pytest.raises(ValueError, match="backwards"):
        progressed.report_progress(percent=60, observed_at="t2", next_update_at="t3")


def test_management_and_work_dependency_cycles_fail_closed():
    teams = {"team": Team("team", "Team", "Work")}
    with pytest.raises(ValueError, match="management hierarchy contains a cycle"):
        Organization("tenant", "org", "Bad", teams, {
            "a": AgentProfile("a", "A", "team", manager_id="b"),
            "b": AgentProfile("b", "B", "team", manager_id="a"),
        })
    a = WorkContract("a", "A", "owner", "one", ("one",), dependency_ids=frozenset({"b"}))
    b = WorkContract("b", "B", "owner", "one", ("one",), dependency_ids=frozenset({"a"}))
    with pytest.raises(ValueError, match="dependency graph contains a cycle"):
        WorkGraph({"a": a, "b": b})


def test_humans_and_services_can_receive_work_while_an_agent_remains_accountable():
    org = organization()
    contract = WorkContract(
        "hire-quant", "Recruit a quant researcher", "ceo", "eng-manager",
        ("recruiter", "jira"), acceptance_criteria=("accepted candidate",),
    )
    org.validate_work_participants(contract)
    with pytest.raises(ValueError, match="accountability"):
        org.validate_work_participants(WorkContract(
            "bad", "Recruit", "ceo", "recruiter", ("recruiter",)
        ))


def test_quality_review_and_mission_specific_escalation_are_explicit():
    accepted = ReviewRecord("review-1", "hire-quant", "qa", 0.9, True, (), ("interview-rubric",))
    assert accepted.accepted is True
    policy = EscalationPolicy("human-work", (
        EscalationLevel(1, 300, EscalationAction.FOLLOW_UP, ("recruiter",)),
        EscalationLevel(2, 3600, EscalationAction.NOTIFY_MANAGER, ("eng-manager",)),
        EscalationLevel(3, 7200, EscalationAction.REASSIGN, ("ceo",)),
    ))
    assert policy.due(elapsed_seconds=400, completed_levels=frozenset()).action is EscalationAction.FOLLOW_UP
    assert policy.due(elapsed_seconds=4000, completed_levels=frozenset({1})).action is EscalationAction.NOTIFY_MANAGER
    assert policy.due(elapsed_seconds=8000, completed_levels=frozenset({1, 2})).action is EscalationAction.REASSIGN
