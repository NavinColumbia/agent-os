from __future__ import annotations

import copy

import pytest

from agent_os.application.command_worker import FatalCommandError
from agent_os.domain.workflow_runtime import (
    TokenStatus,
    WorkflowRunStatus,
    begin_node,
    complete_node,
    start_workflow,
    wait_node,
)
from agent_os.infrastructure.mission_programs import MISSION_PROGRAM_FORMAT
from agent_os.infrastructure.mission_workflows import materialize_mission_program


def mission_program() -> dict:
    return {
        "format": MISSION_PROGRAM_FORMAT,
        "revision": 1,
        "objective": "Build a governed market-analysis service without promising returns.",
        "authorized_budget_cents": 20_000,
        "success_measures": [{
            "measure_id": "release-ready",
            "description": "A tested service and an evidence-backed operating plan exist.",
        }],
        "feasibility": {
            "verdict": "viable_with_conditions",
            "rationale": "Software can be built; outcomes and licensed data remain external constraints.",
            "delivery_estimate": {
                "optimistic": 4, "likely": 8, "pessimistic": 16, "unit": "weeks",
                "basis": "A staged research, implementation, and paper-trading delivery.",
                "confidence": 0.62,
            },
            "cost_estimate": {
                "optimistic": 0, "likely": 20_000, "pessimistic": 100_000,
                "unit": "usd_cents", "basis": "Local-first development plus optional data.",
                "confidence": 0.55,
            },
            "assumptions": ["The first release remains paper-only."],
            "risks": ["Market data licensing may change the cost."],
            "excluded_outcomes": ["Guaranteed investment returns."],
        },
        "clarifications": [{
            "question_id": "risk-budget",
            "question": "What maximum paper portfolio drawdown should the verifier enforce?",
            "why_material": "The answer changes risk acceptance but not market research.",
            "requested_from": ["human:ceo"],
            "status": "open",
            "blocking_workstream_ids": ["implementation"],
            "human_node_id": "clarify-risk",
        }],
        "roles": [
            {
                "role_id": "mission-manager", "title": "Mission Manager",
                "participant_kind": "agent", "responsibilities": ["Own plan and replanning"],
                "authority": ["propose reversible plan changes"],
            },
            {
                "role_id": "researcher", "title": "Market Researcher",
                "participant_kind": "agent", "responsibilities": ["Research data and constraints"],
                "manager_role_id": "mission-manager",
            },
            {
                "role_id": "engineer", "title": "Engineer",
                "participant_kind": "agent", "responsibilities": ["Build and repair capability"],
                "manager_role_id": "mission-manager",
            },
            {
                "role_id": "reviewer", "title": "Independent Reviewer",
                "participant_kind": "agent", "responsibilities": ["Verify acceptance claims"],
                "manager_role_id": "mission-manager",
            },
        ],
        "resources": [{
            "resource_id": "risk-authority", "kind": "decision",
            "purpose": "CEO-approved risk boundary", "status": "missing",
            "owner_role_id": "mission-manager", "acquisition_mode": "request_human",
            "acquisition_node_ids": ["clarify-risk"], "needs_human_approval": True,
        }],
        "capabilities": [{
            "capability_id": "analysis-service", "purpose": "Analyze stored market observations",
            "status": "missing", "owner_role_id": "engineer",
            "expansion_mode": "build_capability", "expansion_node_ids": ["build"],
            "acceptance_checks": ["Automated tests pass in an isolated environment."],
        }],
        "workstreams": [
            {
                "workstream_id": "command", "objective": "Continuously command the mission",
                "accountable_role_id": "mission-manager",
                "workflow_node_ids": ["triage", "replan", "revise-program"],
                "acceptance_criteria": ["Every material change is reviewed."],
            },
            {
                "workstream_id": "discovery", "objective": "Research feasibility and constraints",
                "accountable_role_id": "researcher", "workflow_node_ids": ["research"],
                "acceptance_criteria": ["Sources and constraints are recorded."],
            },
            {
                "workstream_id": "implementation", "objective": "Build the analysis service",
                "accountable_role_id": "engineer", "workflow_node_ids": ["build"],
                "depends_on_workstream_ids": ["discovery"],
                "required_resource_ids": ["risk-authority"],
                "required_capability_ids": ["analysis-service"],
                "acceptance_criteria": ["Risk boundary and tests are enforced."],
            },
            {
                "workstream_id": "assurance", "objective": "Independently verify the result",
                "accountable_role_id": "reviewer", "workflow_node_ids": ["verify"],
                "depends_on_workstream_ids": ["implementation"],
                "acceptance_criteria": ["Claims have reproducible evidence."],
            },
        ],
        "verification": [{
            "claim_id": "release-ready", "claim": "The service enforces the accepted boundary.",
            "success_measure_ids": ["release-ready"],
            "reviewer_role_id": "reviewer", "verification_node_ids": ["verify"],
            "required_evidence": ["Test output", "risk-policy artifact"],
            "failure_routes_to_node_id": "build",
        }],
        "replanning": {
            "owner_role_id": "mission-manager", "review_cadence": "After every evidence gate",
            "triggers": ["assumption invalidated", "estimate range exceeded", "verification failed"],
            "replan_node_ids": ["replan"], "continue_condition": "stable",
            "replan_condition": "changed", "material_change_requires_new_revision": True,
            "max_revisions": 8,
            "notify_role_ids": ["mission-manager"],
        },
        "workflow": {
            "name": "Governed adaptive delivery",
            "entry_node_id": "triage",
            "nodes": [
                {"node_id": "triage", "kind": "decision", "purpose": "Triage work and assumptions.",
                 "owner_role": "mission-manager", "configuration": {"max_iterations": 3}},
                {"node_id": "research", "kind": "agent", "purpose": "Research constraints.",
                 "owner_role": "researcher", "configuration": {"max_iterations": 3}},
                {"node_id": "clarify-risk", "kind": "human", "purpose": "Set the risk boundary.",
                 "configuration": {"recipient_ids": ["human:ceo"],
                                   "response_condition": "answered", "max_iterations": 2}},
                {"node_id": "build", "kind": "agent", "purpose": "Build or repair the service.",
                 "owner_role": "engineer", "configuration": {"max_iterations": 4}},
                {"node_id": "verify", "kind": "agent", "purpose": "Verify claims independently.",
                 "owner_role": "reviewer", "configuration": {"max_iterations": 4}},
                {"node_id": "replan", "kind": "decision", "purpose": "Review changed facts and replan.",
                 "owner_role": "mission-manager", "configuration": {"max_iterations": 4}},
                {"node_id": "revise-program", "kind": "tool",
                 "purpose": "Atomically admit the newly proposed mission program.",
                 "configuration": {"tool": "workflow.revise",
                     "source": {"node_id": "replan",
                                "output_path": ["artifact_ids", "mission-program-revision"]},
                     "success_condition": "revised", "max_iterations": 2}},
                {"node_id": "done", "kind": "terminal", "purpose": "Accept verified evidence.",
                 "configuration": {"max_iterations": 2}},
            ],
            "edges": [
                {"source": "triage", "target": "research", "condition": "always"},
                {"source": "triage", "target": "clarify-risk", "condition": "always"},
                {"source": "research", "target": "replan", "condition": "researched"},
                {"source": "clarify-risk", "target": "build", "condition": "answered"},
                {"source": "build", "target": "verify", "condition": "built"},
                {"source": "verify", "target": "replan", "condition": "passed"},
                {"source": "verify", "target": "build", "condition": "repair"},
                {"source": "replan", "target": "done", "condition": "stable"},
                {"source": "replan", "target": "revise-program", "condition": "changed"},
                {"source": "revise-program", "target": "triage", "condition": "revised"},
            ],
        },
    }


def test_complete_program_contract_materializes_without_any_provider_key():
    program, definition = materialize_mission_program(
        mission_program(), tenant_id="tenant-a", planning_run_id="planning-a",
        artifact_id="artifact-program", allowed_tools=set(),
    )

    assert program.feasibility.delivery_estimate.pessimistic == 16
    assert program.clarifications[0].blocking_workstream_ids == ["implementation"]
    assert program.replanning.material_change_requires_new_revision is True
    assert definition.entry_node_id == "triage"
    clarification = next(node for node in definition.nodes if node.node_id == "clarify-risk")
    assert clarification.configuration["decision_brief"] == {
        "kind": "input",
        "request": "What maximum paper portfolio drawdown should the verifier enforce?",
        "requesting_role": "mission-manager",
        "alternatives": [],
        "consequences": ["The answer changes risk acceptance but not market research."],
        "reversibility": "unknown",
        "safe_default": "Keep dependent work paused while independent work continues.",
        "allow_request_changes": False,
    }


def test_scoped_human_question_does_not_stop_independent_work():
    _, definition = materialize_mission_program(
        mission_program(), tenant_id="tenant-a", planning_run_id="planning-a",
        artifact_id="artifact-program", allowed_tools=set(),
    )
    started = start_workflow(definition, run_id="execution-a")
    entry = started.state.tokens[0]
    running = begin_node(started.state, entry.token_id, expected_version=0)
    branched = complete_node(
        definition, running.state, entry.token_id, expected_version=1,
        satisfied_conditions=frozenset(), evidence_ids=("triage-evidence",),
    )
    research = next(token for token in branched.state.tokens if token.node_id == "research")
    clarify = next(token for token in branched.state.tokens if token.node_id == "clarify-risk")
    clarify_running = begin_node(branched.state, clarify.token_id, expected_version=2)
    waiting = wait_node(
        clarify_running.state, clarify.token_id, expected_version=3,
        correlation_id="risk-answer", reason="Need risk boundary",
        recipient_ids=("human:ceo",),
    )

    assert waiting.state.status is WorkflowRunStatus.ACTIVE
    assert waiting.state.token(research.token_id).status is TokenStatus.READY
    assert waiting.state.token(clarify.token_id).status is TokenStatus.WAITING


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda plan: plan["replanning"].update({"replan_node_ids": []}), "at least 1 item"),
        (lambda plan: plan["resources"][0].update({"acquisition_node_ids": []}),
         "require acquisition workflow nodes"),
        (lambda plan: plan["clarifications"][0].update({"human_node_id": "build"}),
         "must reference a human node"),
        (lambda plan: plan["clarifications"][0].update({"requested_from": ["human:cfo"]}),
         "omits a requested recipient"),
        (lambda plan: plan["workflow"]["nodes"][2]["configuration"].update({
            "decision_brief": {
                "kind": "approval", "request": "Approve", "consequences": [],
                "safe_default": "Do not proceed",
            },
        }), "decision_brief is invalid"),
        (lambda plan: plan["success_measures"].append({
            "measure_id": "uncovered", "description": "An uncovered success claim",
        }), "success measures without verification"),
        (lambda plan: plan["workstreams"][1].update({
            "depends_on_workstream_ids": ["implementation"],
        }), "workstream graph contains a dependency cycle"),
        (lambda plan: plan["workflow"]["edges"].__setitem__(
            slice(None), [edge for edge in plan["workflow"]["edges"]
                          if not (edge["source"] == "replan" and edge["condition"] == "changed")]
        ), "both declared replan paths"),
    ],
)
def test_incomplete_or_falsely_wired_program_is_rejected(mutation, message):
    proposal = copy.deepcopy(mission_program())
    mutation(proposal)

    with pytest.raises(FatalCommandError, match=message):
        materialize_mission_program(
            proposal, tenant_id="tenant-a", planning_run_id="planning-a",
            artifact_id="artifact-program", allowed_tools=set(),
        )


def test_program_cannot_claim_an_unavailable_runtime_tool_as_a_capability():
    proposal = mission_program()
    proposal["capabilities"][0].update({
        "status": "available_unverified",
        "expansion_mode": "verify",
        "required_tool_ids": ["broker.execute-live-order"],
    })

    with pytest.raises(FatalCommandError, match="claims unavailable runtime tools"):
        materialize_mission_program(
            proposal, tenant_id="tenant-a", planning_run_id="planning-a",
            artifact_id="artifact-program", allowed_tools=set(),
        )


def test_independent_verification_cannot_name_the_maker_as_checker():
    proposal = mission_program()
    proposal["verification"][0].update({
        "independence_required": True,
        "maker_role_ids": ["reviewer"],
    })

    with pytest.raises(FatalCommandError, match="reviewer cannot be a maker"):
        materialize_mission_program(
            proposal, tenant_id="tenant-a", planning_run_id="planning-a",
            artifact_id="artifact-program", allowed_tools=set(),
        )


def test_program_cannot_invent_budget_authority_or_hide_a_budget_gap():
    proposal = mission_program()
    with pytest.raises(FatalCommandError, match="CEO-authorized budget"):
        materialize_mission_program(
            proposal, tenant_id="tenant-a", planning_run_id="planning-a",
            artifact_id="artifact-program", allowed_tools=set(),
            authorized_budget_cents=10_000,
        )

    proposal["authorized_budget_cents"] = 0
    with pytest.raises(FatalCommandError, match="explicit budget acquisition gap"):
        materialize_mission_program(
            proposal, tenant_id="tenant-a", planning_run_id="planning-a",
            artifact_id="artifact-program", allowed_tools=set(),
        )


def test_recursive_child_programs_cannot_oversubscribe_parent_budget():
    proposal = mission_program()
    proposal["authorized_budget_cents"] = 0
    proposal["workflow"]["nodes"].append({
        "node_id": "child-team", "kind": "subworkflow",
        "purpose": "Delegate an independently governed child team.",
        "owner_role": "mission-manager", "configuration": {
            "source": {"node_id": "triage", "output_path": ["artifact_ids", "child-program"]},
            "budget_limit_cents": 1, "success_condition": "child_succeeded",
            "failure_condition": "child_failed", "max_iterations": 1,
        },
    })

    with pytest.raises(FatalCommandError, match="child-program budgets exceed"):
        materialize_mission_program(
            proposal, tenant_id="tenant-a", planning_run_id="planning-a",
            artifact_id="artifact-program", allowed_tools=set(),
        )


def test_parallel_agents_require_low_coupling_and_a_registered_measure():
    proposal = mission_program()
    proposal["workstreams"][1]["coordination"] = {
        "strategy": "parallel_agents",
        "rationale": "Search independent source families concurrently.",
        "coupling": "high",
        "parallelism": 2,
        "comparison_baseline": "single_agent",
        "expected_benefit": "Reduce research latency without reducing source coverage.",
        "measure_ids": ["release-ready"],
        "estimated_model_cost_cents": 500,
        "latency_budget_seconds": 900,
        "fallback": "Use one researcher sequentially.",
    }

    with pytest.raises(FatalCommandError, match="parallel agents require low-coupling work"):
        materialize_mission_program(
            proposal, tenant_id="tenant-a", planning_run_id="planning-a",
            artifact_id="artifact-program", allowed_tools=set(),
        )


def test_parallelism_must_be_backed_by_real_workers():
    proposal = mission_program()
    proposal["workstreams"][1]["coordination"] = {
        "strategy": "parallel_agents",
        "rationale": "Search independent source families concurrently.",
        "coupling": "low",
        "parallelism": 2,
        "comparison_baseline": "single_agent",
        "expected_benefit": "Reduce research latency without reducing source coverage.",
        "measure_ids": ["release-ready"],
        "estimated_model_cost_cents": 500,
        "latency_budget_seconds": 900,
        "fallback": "Use one researcher sequentially.",
    }

    with pytest.raises(FatalCommandError, match="fewer parallel workers than admitted"):
        materialize_mission_program(
            proposal, tenant_id="tenant-a", planning_run_id="planning-a",
            artifact_id="artifact-program", allowed_tools=set(),
        )


def test_parallel_agents_require_an_executable_fanout_and_accept_a_real_branch():
    proposal = mission_program()
    proposal["workflow"]["nodes"].append({
        "node_id": "research-alt", "kind": "agent",
        "purpose": "Research an independent source family.",
        "owner_role": "researcher", "configuration": {"max_iterations": 3},
    })
    proposal["workflow"]["edges"].extend((
        {"source": "triage", "target": "research-alt", "condition": "always"},
        {"source": "research-alt", "target": "replan", "condition": "researched"},
    ))
    proposal["workstreams"][1]["workflow_node_ids"].append("research-alt")
    proposal["workstreams"][1]["coordination"] = {
        "strategy": "parallel_agents",
        "rationale": "Search two independent source families concurrently.",
        "coupling": "low",
        "parallelism": 2,
        "comparison_baseline": "single_agent",
        "expected_benefit": "Reduce research latency without reducing source coverage.",
        "measure_ids": ["release-ready"],
        "estimated_model_cost_cents": 500,
        "latency_budget_seconds": 900,
        "fallback": "Use one researcher sequentially.",
    }

    program, _ = materialize_mission_program(
        proposal, tenant_id="tenant-a", planning_run_id="planning-a",
        artifact_id="artifact-program", allowed_tools=set(),
    )

    assert program.workstreams[1].coordination.strategy == "parallel_agents"


def test_parallel_workers_without_a_graph_branch_are_rejected():
    proposal = mission_program()
    proposal["workflow"]["nodes"].append({
        "node_id": "research-alt", "kind": "agent",
        "purpose": "Research another source family after the first.",
        "owner_role": "researcher", "configuration": {"max_iterations": 3},
    })
    proposal["workflow"]["edges"].extend((
        {"source": "research", "target": "research-alt", "condition": "researched"},
        {"source": "research-alt", "target": "replan", "condition": "researched"},
    ))
    proposal["workstreams"][1]["workflow_node_ids"].append("research-alt")
    proposal["workstreams"][1]["coordination"] = {
        "strategy": "parallel_agents",
        "rationale": "Claim two source searches are concurrent.",
        "coupling": "low",
        "parallelism": 2,
        "comparison_baseline": "single_agent",
        "expected_benefit": "Reduce research latency.",
        "measure_ids": ["release-ready"],
        "estimated_model_cost_cents": 500,
        "latency_budget_seconds": 900,
        "fallback": "Use one researcher sequentially.",
    }

    with pytest.raises(FatalCommandError, match="does not expose its admitted parallel branch"):
        materialize_mission_program(
            proposal, tenant_id="tenant-a", planning_run_id="planning-a",
            artifact_id="artifact-program", allowed_tools=set(),
        )


def test_coordination_estimates_cannot_hide_budget_oversubscription():
    proposal = mission_program()
    proposal["workstreams"][1]["coordination"] = {
        "strategy": "single_agent",
        "rationale": "Keep tightly coupled research in one context.",
        "coupling": "high",
        "parallelism": 1,
        "comparison_baseline": "direct_model",
        "expected_benefit": "Retain durable evidence and recovery.",
        "measure_ids": ["release-ready"],
        "estimated_model_cost_cents": 20_001,
        "latency_budget_seconds": 900,
        "fallback": "Return the direct-model research packet.",
    }

    with pytest.raises(FatalCommandError, match="coordination estimates exceed"):
        materialize_mission_program(
            proposal, tenant_id="tenant-a", planning_run_id="planning-a",
            artifact_id="artifact-program", allowed_tools=set(),
        )


def test_declared_subworkflow_requires_an_executable_subworkflow_node():
    proposal = mission_program()
    proposal["workstreams"][1]["coordination"] = {
        "strategy": "subworkflow",
        "rationale": "Delegate a separately governed specialist program.",
        "coupling": "medium",
        "parallelism": 1,
        "comparison_baseline": "single_agent",
        "expected_benefit": "Isolate specialist authority and evidence.",
        "measure_ids": ["release-ready"],
        "estimated_model_cost_cents": 500,
        "latency_budget_seconds": 900,
        "fallback": "Keep specialist work in the parent program.",
    }

    with pytest.raises(FatalCommandError, match="without a subworkflow node"):
        materialize_mission_program(
            proposal, tenant_id="tenant-a", planning_run_id="planning-a",
            artifact_id="artifact-program", allowed_tools=set(),
        )
