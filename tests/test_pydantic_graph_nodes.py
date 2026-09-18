from __future__ import annotations

import json

import pytest
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.messages import ModelResponse, ToolCallPart

from agent_os.application.command_worker import FatalCommandError
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import begin_node, complete_node, resume_wait, start_workflow, wait_node
from agent_os.infrastructure.pydantic_graph_nodes import PydanticGraphNodeRuntime
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore


class FakeUsageMeter:
    def __init__(self):
        self.reservations = []
        self.settlements = []

    def reserve_model_turn(self, **values):
        self.reservations.append(values)
        return values

    def settle_model_turn(self, **values):
        self.settlements.append(values)
        return values


def agent_graph() -> WorkflowDefinition:
    return WorkflowDefinition(
        "graph", "tenant-a", "Graph", 1, "agent",
        (
            WorkflowNode("agent", NodeKind.AGENT, "Choose the verified path", "engineer"),
            WorkflowNode("done", NodeKind.TERMINAL, "Accept upstream evidence"),
        ),
        (WorkflowEdge("agent", "done", "verified"),),
        "architect",
    )


def test_agent_node_uses_structured_output_and_only_declared_conditions():
    definition = agent_graph()
    started = start_workflow(definition, run_id="run-agent")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    output = {
        "summary": "Verified the path.",
        "disposition": "complete",
        "satisfied_conditions": ["verified"],
        "evidence_ids": ["evidence-1"],
        "output": {"decision": "ship"},
        "recipient_ids": [],
        "correlation_id": None,
        "reason": None,
        "retryable": False,
    }
    meter = FakeUsageMeter()
    runtime = PydanticGraphNodeRuntime(
        TestModel(custom_output_args=output), max_turn_budget_cents=1,
        usage_meter=meter, model_name="test:model",
    )

    result = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-agent", definition=definition,
        state=running, action=action, idempotency_key=action.action_id,
    )

    assert result["disposition"] == "complete"
    assert result["satisfied_conditions"] == ["verified"]
    assert result["output"]["summary"] == "Verified the path."
    assert meter.reservations[0]["category"] == "graph_agent"
    assert meter.reservations[0]["source_id"] == action.action_id
    assert meter.settlements[0]["usage"] == result["output"]["usage"]


def test_agent_node_persists_bounded_management_proposals_in_durable_output():
    definition = agent_graph()
    started = start_workflow(definition, run_id="run-management")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    output = {
        "summary": "Found a missing reviewer and delegated follow-up.",
        "disposition": "complete",
        "satisfied_conditions": ["verified"],
        "evidence_ids": ["evidence-1"],
        "output": {},
        "risks": ["No independent security review"],
        "messages": [{
            "audience": "human", "kind": "update", "recipient_ids": ["human:ceo"],
            "subject": "Review gap", "body": "Security review is still missing.",
        }],
        "proposed_work": [{
            "objective": "Review authentication", "owner_role": "security-specialist",
            "acceptance_criteria": ["Threats documented"],
        }],
        "hiring_requests": [{
            "role": "security-specialist", "reason": "No reviewer is assigned",
            "capabilities": ["security"],
        }],
        "decisions": [{
            "intent": "Gate launch", "considered_options": ["launch", "review"],
            "chosen_option": "review", "rationale": "Reduce risk", "confidence": 0.9,
            "reversible": True,
        }],
        "next_actions": ["Assign the review"],
    }
    runtime = PydanticGraphNodeRuntime(TestModel(custom_output_args=output))

    result = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-management", definition=definition,
        state=running, action=action, idempotency_key=action.action_id,
    )

    actions = result["output"]["organization_actions"]
    assert actions["risks"] == ["No independent security review"]
    assert actions["messages"][0]["recipient_ids"] == ["human:ceo"]
    assert actions["proposed_work"][0]["owner_role"] == "security-specialist"
    assert actions["hiring_requests"][0]["requested_count"] == 1
    assert actions["decisions"][0]["chosen_option"] == "review"
    assert actions["next_actions"] == ["Assign the review"]


def test_agent_node_rejects_a_hallucinated_branch():
    definition = agent_graph()
    started = start_workflow(definition, run_id="run-branch")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    output = {
        "summary": "Invented a path.",
        "disposition": "complete",
        "satisfied_conditions": ["secret-shortcut"],
        "evidence_ids": ["evidence-1"],
        "output": {},
        "recipient_ids": [],
        "correlation_id": None,
        "reason": None,
        "retryable": False,
    }
    runtime = PydanticGraphNodeRuntime(TestModel(custom_output_args=output))

    with pytest.raises(FatalCommandError, match="unknown conditions"):
        runtime.execute_node(
            tenant_id="tenant-a", run_id="run-branch", definition=definition,
            state=running, action=action, idempotency_key=action.action_id,
        )


def test_human_node_creates_a_deterministic_correlated_wait_without_model_spend():
    definition = WorkflowDefinition(
        "human", "tenant-a", "Human", 1, "approval",
        (
            WorkflowNode(
                "approval", NodeKind.HUMAN, "Approve the irreversible deployment",
                configuration={"recipient_ids": ["human:ceo"]},
            ),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
        ),
        (WorkflowEdge("approval", "done", "always"),),
        "architect",
    )
    started = start_workflow(definition, run_id="run-human")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    runtime = PydanticGraphNodeRuntime(TestModel())

    first = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-human", definition=definition,
        state=running, action=action, idempotency_key=action.action_id,
    )
    repeated = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-human", definition=definition,
        state=running, action=action, idempotency_key=action.action_id,
    )

    assert first == repeated
    assert first["disposition"] == "wait"
    assert first["recipient_ids"] == ["human:ceo"]
    assert first["correlation_id"].startswith("graph-question-")

    waiting = wait_node(
        running,
        action.token_id,
        expected_version=1,
        correlation_id=first["correlation_id"],
        reason=first["reason"],
        recipient_ids=("human:ceo",),
    )
    resumed = resume_wait(
        waiting.state,
        expected_version=2,
        correlation_id=first["correlation_id"],
        response={"approved": True},
    )
    resumed_action = resumed.actions[0]
    resumed_running = begin_node(
        resumed.state, resumed_action.token_id, expected_version=3,
    ).state
    completed = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-human", definition=definition,
        state=resumed_running, action=resumed_action, idempotency_key=resumed_action.action_id,
    )
    assert completed["disposition"] == "complete"
    assert completed["output"]["human_response"] == {"approved": True}
    assert completed["evidence_ids"][0].startswith("human-response-")


def test_human_rejection_selects_only_an_explicit_rejection_path():
    definition = WorkflowDefinition(
        "human-decision", "tenant-a", "Human decision", 1, "approval",
        (
            WorkflowNode(
                "approval", NodeKind.HUMAN, "Approve release",
                configuration={
                    "response_condition": "approved",
                    "rejection_condition": "rejected",
                    "decision_brief": {
                        "kind": "approval",
                        "request": "Approve the production release?",
                        "recommendation": "Approve after reviewing the evidence packet.",
                        "alternatives": ["Keep the current release live"],
                        "consequences": ["The new revision becomes public."],
                        "reversibility": "reversible",
                        "safe_default": "Keep the current release live.",
                        "allow_request_changes": True,
                    },
                },
            ),
            WorkflowNode("ship", NodeKind.TERMINAL, "Ship"),
            WorkflowNode("repair", NodeKind.TERMINAL, "Repair"),
        ),
        (
            WorkflowEdge("approval", "ship", "approved"),
            WorkflowEdge("approval", "repair", "rejected"),
        ),
        "architect",
    )
    started = start_workflow(definition, run_id="run-reject")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    runtime = PydanticGraphNodeRuntime(TestModel())
    prompt = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-reject", definition=definition,
        state=running, action=action, idempotency_key=action.action_id,
    )
    assert prompt["decision_context"]["allowed_actions"] == [
        "approve", "decline", "request_changes",
    ]
    assert prompt["decision_context"]["recommendation"].startswith("Approve")
    assert prompt["decision_context"]["evidence_ids"] == []
    waiting = wait_node(
        running, action.token_id, expected_version=1,
        correlation_id="decision", reason="Approve release", recipient_ids=("human:ceo",),
        decision_context=prompt["decision_context"],
    )
    assert waiting.actions[0].payload["decision_context"]["safe_default"] == (
        "Keep the current release live."
    )
    resumed = resume_wait(
        waiting.state, expected_version=2,
        correlation_id="decision", response={
            "action": "decline", "approved": False, "answer": "Keep the current release.",
        },
    )
    resumed_action = resumed.actions[0]
    resumed_running = begin_node(
        resumed.state, resumed_action.token_id, expected_version=3,
    ).state

    result = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-reject", definition=definition,
        state=resumed_running, action=resumed_action,
        idempotency_key=resumed_action.action_id,
    )

    assert result["satisfied_conditions"] == ["rejected"]


def test_terminal_node_aggregates_real_upstream_evidence():
    definition = agent_graph()
    started = start_workflow(definition, run_id="run-terminal")
    agent_action = started.actions[0]
    running = begin_node(started.state, agent_action.token_id, expected_version=0).state
    advanced = complete_node(
        definition,
        running,
        agent_action.token_id,
        expected_version=1,
        satisfied_conditions=frozenset({"verified"}),
        evidence_ids=("verified-build",),
    )
    terminal_action = advanced.actions[0]
    terminal_running = begin_node(
        advanced.state, terminal_action.token_id, expected_version=2,
    ).state
    runtime = PydanticGraphNodeRuntime(TestModel())

    result = runtime.execute_node(
        tenant_id="tenant-a", run_id="run-terminal", definition=definition,
        state=terminal_running, action=terminal_action,
        idempotency_key=terminal_action.action_id,
    )

    assert result["evidence_ids"] == ["verified-build"]
    assert result["output"]["accepted_upstream_evidence"] == ["verified-build"]


def test_graph_agent_persists_new_evidence_and_rejects_invented_ids(tmp_path):
    definition = agent_graph()
    started = start_workflow(definition, run_id="run-evidence")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'graph-artifacts.sqlite3'}", create_schema=True,
    )
    proposed = {
        "summary": "Built a small application.",
        "disposition": "complete",
        "satisfied_conditions": ["verified"],
        "evidence_ids": [],
        "artifacts": [{
            "label": "application-source",
            "media_type": "application/vnd.agent-os.source-bundle+json",
            "files": {"index.html": "<h1>Built</h1>"},
        }],
        "output": {"decision": "test"},
        "recipient_ids": [],
        "correlation_id": None,
        "reason": None,
        "retryable": False,
    }
    try:
        runtime = PydanticGraphNodeRuntime(
            TestModel(custom_output_args=proposed),
            artifact_store=artifacts,
        )
        result = runtime.execute_node(
            tenant_id="tenant-a", run_id="run-evidence", definition=definition,
            state=running, action=action, idempotency_key=action.action_id,
        )
        artifact_id = result["evidence_ids"][0]
        assert artifacts.describe("tenant-a", artifact_id)["media_type"].endswith(
            "source-bundle+json"
        )
        assert result["artifacts"][0]["artifact_id"] == artifact_id
        assert result["output"]["artifact_ids"] == {"application-source": artifact_id}

        def model_must_not_run(messages, info):
            raise AssertionError("durable node result replay called the model")

        replayed = PydanticGraphNodeRuntime(
            FunctionModel(model_must_not_run), artifact_store=artifacts,
        ).execute_node(
            tenant_id="tenant-a", run_id="run-evidence", definition=definition,
            state=running, action=action, idempotency_key=action.action_id,
        )
        assert replayed == result

        typo_with_artifact = {
            **proposed,
            "evidence_ids": ["mistyped-prior-id"],
        }
        normalized = PydanticGraphNodeRuntime(
            TestModel(custom_output_args=typo_with_artifact), artifact_store=artifacts,
        ).execute_node(
            tenant_id="tenant-a", run_id="run-evidence", definition=definition,
            state=running, action=action, idempotency_key="typo-with-artifact",
        )
        assert normalized["output"]["rejected_evidence_ids"] == ["mistyped-prior-id"]
        assert "mistyped-prior-id" not in normalized["evidence_ids"]
        assert normalized["evidence_ids"]

        hallucinated = {**proposed, "evidence_ids": ["invented"], "artifacts": []}
        runtime = PydanticGraphNodeRuntime(
            TestModel(custom_output_args=hallucinated),
            artifact_store=artifacts,
        )
        with pytest.raises(FatalCommandError, match="completion requires durable evidence"):
            runtime.execute_node(
                tenant_id="tenant-a", run_id="run-evidence", definition=definition,
                state=running, action=action, idempotency_key="different-action",
            )
    finally:
        artifacts.close()


def test_successor_agent_receives_bounded_prior_artifact_contents(tmp_path):
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'handoff-artifacts.sqlite3'}", create_schema=True,
    )
    try:
        source_id = artifacts.put(
            organization_id="tenant-a",
            content=b'{"format":"agent-os.source-bundle.v1","files":{"index.html":{"encoding":"utf-8","content":"<h1>Handoff</h1>"}}}',
            media_type="application/vnd.agent-os.source-bundle+json",
            idempotency_key="handoff-source",
        )
        definition = WorkflowDefinition(
            "handoff", "tenant-a", "Handoff", 1, "build",
            (
                WorkflowNode("build", NodeKind.AGENT, "Build", "engineer"),
                WorkflowNode("review", NodeKind.AGENT, "Review", "reviewer"),
                WorkflowNode("done", NodeKind.TERMINAL, "Done"),
            ),
            (
                WorkflowEdge("build", "review", "built"),
                WorkflowEdge("review", "done", "reviewed"),
            ),
            "architect",
        )
        started = start_workflow(definition, run_id="run-handoff")
        build = started.actions[0]
        running = begin_node(started.state, build.token_id, expected_version=0).state
        advanced = complete_node(
            definition, running, build.token_id, expected_version=1,
            satisfied_conditions=frozenset({"built"}), evidence_ids=(source_id,),
            output={"artifact_ids": {"site-source": source_id}},
        )
        review = advanced.actions[0]
        review_running = begin_node(
            advanced.state, review.token_id, expected_version=2,
        ).state
        runtime = PydanticGraphNodeRuntime(
            TestModel(), artifact_store=artifacts, context_character_limit=8_000,
        )

        hydrated = runtime._prior_artifact_context(
            "tenant-a", review_running, current_token_id=review.token_id,
        )

        assert hydrated[0]["artifact_id"] == source_id
        assert "<h1>Handoff</h1>" in hydrated[0]["source_bundle_text_files"][
            "index.html"
        ]
        assert hydrated[0]["producer_node_id"] == "build"
    finally:
        artifacts.close()


def test_handoff_preserves_original_labels_and_follows_source_dependencies(tmp_path):
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'dependency-handoff.sqlite3'}", create_schema=True,
    )
    try:
        original_id = artifacts.put(
            organization_id="tenant-a",
            content=(
                b'{"format":"agent-os.source-bundle.v1","files":'
                b'{"index.html":{"encoding":"utf-8","content":"<h1>Original</h1>"}}}'
            ),
            media_type="application/vnd.agent-os.source-bundle+json",
            idempotency_key="original-source",
        )
        repair_id = artifacts.put(
            organization_id="tenant-a",
            content=json.dumps({
                "format": "agent-os.source-bundle.v1",
                "files": {"REPAIR.md": {
                    "encoding": "utf-8",
                    "content": f"Apply this overlay to {original_id}.",
                }},
            }, separators=(",", ":")).encode(),
            media_type="application/vnd.agent-os.source-bundle+json",
            idempotency_key="repair-source",
        )
        brief_id = artifacts.put(
            organization_id="tenant-a", content=b'{"requirements":"unchanged"}',
            media_type="application/json", idempotency_key="brief",
        )
        definition = WorkflowDefinition(
            "dependency", "tenant-a", "Dependency", 1, "source",
            (
                WorkflowNode("source", NodeKind.AGENT, "Source", "engineer"),
                WorkflowNode("repair", NodeKind.AGENT, "Repair", "engineer"),
                WorkflowNode("spec", NodeKind.AGENT, "Specify", "architect"),
                WorkflowNode("build", NodeKind.AGENT, "Build", "engineer"),
                WorkflowNode("done", NodeKind.TERMINAL, "Done"),
            ),
            (
                WorkflowEdge("source", "repair", "next"),
                WorkflowEdge("repair", "spec", "next"),
                WorkflowEdge("spec", "build", "next"),
                WorkflowEdge("build", "done", "next"),
            ),
            "architect",
        )
        started = start_workflow(definition, run_id="dependency-run")
        source = begin_node(
            started.state, started.actions[0].token_id, expected_version=0,
        ).state
        source_done = complete_node(
            definition, source, started.actions[0].token_id, expected_version=1,
            satisfied_conditions=frozenset({"next"}), evidence_ids=(original_id,),
            output={"artifact_ids": {"site-source": original_id}},
        )
        repair_action = source_done.actions[0]
        repair = begin_node(
            source_done.state, repair_action.token_id, expected_version=2,
        ).state
        repair_done = complete_node(
            definition, repair, repair_action.token_id, expected_version=3,
            satisfied_conditions=frozenset({"next"}), evidence_ids=(repair_id,),
            output={"artifact_ids": {"repair-source": repair_id}},
        )
        spec_action = repair_done.actions[0]
        spec = begin_node(
            repair_done.state, spec_action.token_id, expected_version=4,
        ).state
        spec_done = complete_node(
            definition, spec, spec_action.token_id, expected_version=5,
            satisfied_conditions=frozenset({"next"}), evidence_ids=(repair_id, brief_id),
            output={"latest_repair_artifact_id": repair_id},
        )
        build_action = spec_done.actions[0]
        build = begin_node(
            spec_done.state, build_action.token_id, expected_version=6,
        ).state

        hydrated = PydanticGraphNodeRuntime(
            TestModel(), artifact_store=artifacts,
        )._prior_artifact_context(
            "tenant-a", build, current_token_id=build_action.token_id,
            preferred_artifact_ids=frozenset({repair_id}),
        )

        assert [item["artifact_id"] for item in hydrated[:2]] == [repair_id, original_id]
        assert hydrated[0]["labels"] == ["repair-source"]
        assert hydrated[0]["producer_node_id"] == "repair"
        compact_context = PydanticGraphNodeRuntime._compact_run_context({
            "prompt": "Build the product",
            "mission_program": {
                "format": "agent-os.mission-program.v1",
                "revision": 2,
                "objective": {"summary": "Build"},
                "authorized_budget_cents": 0,
                "success_measures": ["Verified"],
                "workstreams": [{
                    "workstream_id": "build",
                    "objective": "Build the admitted product",
                    "accountable_role_id": "engineer",
                    "workflow_node_ids": ["large", "duplicated", "executable", "graph"],
                    "acceptance_criteria": ["Verified"],
                    "coordination": {
                        "strategy": "single_agent",
                        "comparison_baseline": "direct_model",
                        "expected_benefit": "Durable recovery",
                    },
                }],
                "roles": [{"role": "engineer"}],
            },
        })
        assert compact_context["mission_program"]["objective"] == {"summary": "Build"}
        assert "workstreams" not in compact_context["mission_program"]
        assert compact_context["mission_program_omitted_sections"] == ["roles", "workstreams"]
        assert compact_context["mission_coordination"] == [{
            "workstream_id": "build",
            "objective": "Build the admitted product",
            "accountable_role_id": "engineer",
            "acceptance_criteria": ["Verified"],
            "coordination": {
                "strategy": "single_agent",
                "comparison_baseline": "direct_model",
                "expected_benefit": "Durable recovery",
            },
        }]
    finally:
        artifacts.close()


def test_direct_revision_artifact_precedes_its_large_source_dependencies(tmp_path):
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'revision-handoff.sqlite3'}", create_schema=True,
        max_content_bytes=2 * 1024 * 1024,
    )
    try:
        source_id = artifacts.put(
            organization_id="tenant-a",
            content=(
                b'{"format":"agent-os.source-bundle.v1","files":'
                b'{"index.html":{"encoding":"utf-8","content":"<h1>Built</h1>"}}}'
            ),
            media_type="application/vnd.agent-os.source-bundle+json",
            idempotency_key="revision-source",
        )
        program_id = artifacts.put(
            organization_id="tenant-a",
            content=json.dumps({
                "format": "agent-os.mission-program.v1",
                "revision": 3,
                "source_artifact_id": source_id,
            }, separators=(",", ":")).encode(),
            media_type="application/json",
            idempotency_key="rejected-revision",
        )
        definition = WorkflowDefinition(
            "revision", "tenant-a", "Revision", 1, "checkpoint",
            (
                WorkflowNode("checkpoint", NodeKind.AGENT, "Revise", "manager"),
                WorkflowNode("revise", NodeKind.TOOL, "Validate"),
                WorkflowNode("done", NodeKind.TERMINAL, "Done"),
            ),
            (
                WorkflowEdge("checkpoint", "revise", "proposed"),
                WorkflowEdge("revise", "checkpoint", "repair"),
                WorkflowEdge("revise", "done", "accepted"),
            ),
            "architect",
        )
        started = start_workflow(definition, run_id="run-revision")
        checkpoint_action = started.actions[0]
        checkpoint = begin_node(
            started.state, checkpoint_action.token_id, expected_version=0,
        ).state
        proposed = complete_node(
            definition, checkpoint, checkpoint_action.token_id, expected_version=1,
            satisfied_conditions=frozenset({"proposed"}), evidence_ids=(program_id, source_id),
            output={"artifact_ids": {"replacement-mission-program": program_id}},
        )
        revise_action = proposed.actions[0]
        revising = begin_node(
            proposed.state, revise_action.token_id, expected_version=2,
        ).state
        rejected = complete_node(
            definition, revising, revise_action.token_id, expected_version=3,
            satisfied_conditions=frozenset({"repair"}), evidence_ids=(program_id,),
            output={
                "rejected_artifact_id": program_id,
                "validation_error": "owner mismatch",
                "repair_required": True,
            },
        )
        retry_action = rejected.actions[0]
        retrying = begin_node(
            rejected.state, retry_action.token_id, expected_version=4,
        ).state

        hydrated = PydanticGraphNodeRuntime(
            TestModel(), artifact_store=artifacts,
        )._prior_artifact_context(
            "tenant-a", retrying, current_token_id=retry_action.token_id,
            preferred_artifact_labels=frozenset({"replacement-mission-program"}),
            preferred_artifact_ids=frozenset({program_id}),
        )

        assert [item["artifact_id"] for item in hydrated[:2]] == [program_id, source_id]
        assert hydrated[0]["content"].startswith('{"format":"agent-os.mission-program.v1"')
    finally:
        artifacts.close()


def test_node_contract_artifact_reference_takes_precedence_over_router_output(tmp_path):
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'contract-handoff.sqlite3'}", create_schema=True,
    )
    try:
        source_id = artifacts.put(
            organization_id="tenant-a",
            content=(
                b'{"format":"agent-os.source-bundle.v1","files":'
                b'{"index.html":{"encoding":"utf-8","content":"<h1>Approved</h1>"}}}'
            ),
            media_type="application/vnd.agent-os.source-bundle+json",
            idempotency_key="approved-source",
        )
        routing_id = artifacts.put(
            organization_id="tenant-a",
            content=b'{"format":"agent-os.mission-program.v1","revision":3}',
            media_type="application/json",
            idempotency_key="routing-program",
        )
        requirements = {
            "instructions": f"Publish exact source {source_id}; do not reconstruct it.",
        }

        assert PydanticGraphNodeRuntime._artifact_references(requirements) == {source_id}

        # The contract-derived ID is passed as the direct preference by
        # execute_node; verify that it stays ahead of a routing artifact.
        definition = WorkflowDefinition(
            "release-handoff", "tenant-a", "Release", 1, "route",
            (
                WorkflowNode("route", NodeKind.AGENT, "Route", "manager"),
                WorkflowNode("release", NodeKind.AGENT, "Release", "releaser", {
                    "agent_context": requirements,
                }),
                WorkflowNode("done", NodeKind.TERMINAL, "Done"),
            ),
            (
                WorkflowEdge("route", "release", "next"),
                WorkflowEdge("release", "done", "published"),
            ),
            "architect",
        )
        started = start_workflow(definition, run_id="release-handoff")
        route_action = started.actions[0]
        routing = begin_node(
            started.state, route_action.token_id, expected_version=0,
        ).state
        routed = complete_node(
            definition, routing, route_action.token_id, expected_version=1,
            satisfied_conditions=frozenset({"next"}),
            evidence_ids=(routing_id, source_id),
            output={"artifact_ids": {"replacement-mission-program": routing_id}},
        )
        release_action = routed.actions[0]
        releasing = begin_node(
            routed.state, release_action.token_id, expected_version=2,
        ).state
        hydrated = PydanticGraphNodeRuntime(
            TestModel(), artifact_store=artifacts,
        )._prior_artifact_context(
            "tenant-a", releasing, current_token_id=release_action.token_id,
            preferred_artifact_ids=frozenset({source_id}),
        )

        assert hydrated[0]["artifact_id"] == source_id
        assert "Approved" in hydrated[0]["source_bundle_text_files"]["index.html"]
    finally:
        artifacts.close()


def test_node_contract_file_path_is_hydrated_before_other_large_source_files():
    bundle = json.dumps({
        "format": "agent-os.source-bundle.v1",
        "files": {
            "verify.py": {"encoding": "utf-8", "content": "v" * 22_000},
            "index.html": {"encoding": "utf-8", "content": "<main>complete</main>"},
        },
    }, separators=(",", ":")).encode()

    requested = PydanticGraphNodeRuntime._source_path_references({
        "instructions": "Publish exact index.html; never reconstruct it.",
    })
    hydrated = PydanticGraphNodeRuntime._source_bundle_text_context(
        bundle, preferred_paths=frozenset(requested),
    )

    assert requested == {"index.html"}
    assert hydrated is not None
    assert hydrated["source_bundle_text_files"]["index.html"] == "<main>complete</main>"
    assert len(hydrated["source_bundle_text_files"]["verify.py"]) == 22_000


def test_large_sandbox_bundle_hydrates_results_and_attaches_page_screenshots(tmp_path):
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'visual-handoff.sqlite3'}", create_schema=True,
        max_content_bytes=2 * 1024 * 1024,
    )
    try:
        from agent_os.infrastructure.docker_sandbox import encode_source_bundle

        bundle_id = artifacts.put(
            organization_id="tenant-a",
            content=encode_source_bundle({
                "index.html": "x" * 30_000,
                "verification/results.json": b'{"status":"fail","check":"overflow"}',
                "verification/page-360.png": b"small-mobile-image",
                "verification/page-1280.png": b"small-desktop-image",
                "verification/focus-360.png": b"focus-image",
                "verification/join-360.png": b"join-image",
            }),
            media_type="application/vnd.agent-os.source-bundle+json",
            idempotency_key="visual-output",
        )
        definition = WorkflowDefinition(
            "visual", "tenant-a", "Visual", 1, "test",
            (
                WorkflowNode("test", NodeKind.TOOL, "Test"),
                WorkflowNode("qa", NodeKind.AGENT, "Review", "reviewer"),
                WorkflowNode("done", NodeKind.TERMINAL, "Done"),
            ),
            (
                WorkflowEdge("test", "qa", "tested"),
                WorkflowEdge("qa", "done", "reviewed"),
            ),
            "architect",
        )
        started = start_workflow(definition, run_id="run-visual")
        test_action = started.actions[0]
        running = begin_node(started.state, test_action.token_id, expected_version=0).state
        advanced = complete_node(
            definition, running, test_action.token_id, expected_version=1,
            satisfied_conditions=frozenset({"tested"}), evidence_ids=(bundle_id,),
        )
        qa_action = advanced.actions[0]
        qa_running = begin_node(
            advanced.state, qa_action.token_id, expected_version=2,
        ).state
        runtime = PydanticGraphNodeRuntime(
            TestModel(), artifact_store=artifacts, context_character_limit=64_000,
        )

        hydrated = runtime._prior_artifact_context(
            "tenant-a", qa_running, current_token_id=qa_action.token_id,
        )
        metadata, images = runtime._prior_image_context(
            "tenant-a", qa_running, current_token_id=qa_action.token_id,
        )

        assert "overflow" in hydrated[0]["source_bundle_text_files"][
            "verification/results.json"
        ]
        assert len(images) == 3
        assert metadata[0]["source_path"].startswith("verification/page-")
        assert metadata[1]["source_path"].startswith("verification/page-")
        assert all(len(image.data) < 512 * 1024 for image in images)
    finally:
        artifacts.close()


def test_visual_evidence_is_attached_for_review_not_screenshot_production():
    build = WorkflowNode(
        "build", NodeKind.AGENT, "Build the verified source", "engineering-manager",
    )
    qa = WorkflowNode(
        "qa", NodeKind.AGENT, "Independently review complete evidence", "quality-manager",
    )

    assert PydanticGraphNodeRuntime._needs_visual_evidence(
        build, {"instructions": "Run checks and save screenshots at three widths."},
    ) is False
    assert PydanticGraphNodeRuntime._needs_visual_evidence(
        qa, {"instructions": "Inspect actual results and screenshots."},
    ) is True


def test_resumed_agent_receives_the_durable_wait_response():
    definition = agent_graph()
    started = start_workflow(definition, run_id="run-agent-resume")
    action = started.actions[0]
    running = begin_node(started.state, action.token_id, expected_version=0).state
    waiting = wait_node(
        running, action.token_id, expected_version=1,
        correlation_id="missing-contract", reason="Need exact limits",
        recipient_ids=("human:ceo",),
    )
    resumed = resume_wait(
        waiting.state, expected_version=2, correlation_id="missing-contract",
        response={"answer": "The output limit is 8388608 bytes."},
    )
    resumed_action = resumed.actions[0]
    resumed_running = begin_node(
        resumed.state, resumed_action.token_id, expected_version=3,
    ).state
    observed: dict[str, str] = {}

    async def respond(messages, info):
        prompt = str(messages[-1].parts[0].content)
        observed["prompt"] = prompt
        return ModelResponse(parts=[ToolCallPart(
            info.output_tools[0].name,
            {
                "summary": "Used the supplied limit.",
                "disposition": "complete",
                "satisfied_conditions": ["verified"],
                "evidence_ids": ["evidence-limit"],
            },
        )])

    result = PydanticGraphNodeRuntime(FunctionModel(respond)).execute_node(
        tenant_id="tenant-a", run_id="run-agent-resume", definition=definition,
        state=resumed_running, action=resumed_action,
        idempotency_key=resumed_action.action_id,
    )

    assert result["disposition"] == "complete"
    assert "8388608 bytes" in observed["prompt"]
