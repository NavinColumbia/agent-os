"""PydanticAI runtime for executable nodes in customer-designed graphs."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import Callable, Mapping, Sequence, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models import Model

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.ports import ArtifactStore, GraphNodeRuntime, UsageMeter
from agent_os.domain.organization import Organization
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowNode
from agent_os.domain.workflow_runtime import TokenStatus, WorkflowAction, WorkflowRunState
from agent_os.infrastructure.proposed_artifacts import (
    ProposedArtifact,
    persist_and_validate_artifacts,
)
from agent_os.infrastructure.pydantic_agents import (
    HiringRequest,
    ProposedDecision,
    ProposedMessage,
    ProposedWork,
    model_usage_record,
)


class GraphNodeDisposition(str, Enum):
    COMPLETE = "complete"
    WAIT = "wait"
    FAIL = "fail"


class GraphAgentNodeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    disposition: GraphNodeDisposition
    satisfied_conditions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    artifacts: list[ProposedArtifact] = Field(default_factory=list, max_length=16)
    output: dict[str, Any] = Field(default_factory=dict)
    recipient_ids: list[str] = Field(default_factory=list)
    correlation_id: str | None = None
    reason: str | None = None
    retryable: bool = False
    observations: list[str] = Field(default_factory=list, max_length=100)
    risks: list[str] = Field(default_factory=list, max_length=100)
    messages: list[ProposedMessage] = Field(default_factory=list, max_length=100)
    proposed_work: list[ProposedWork] = Field(default_factory=list, max_length=100)
    hiring_requests: list[HiringRequest] = Field(default_factory=list, max_length=32)
    decisions: list[ProposedDecision] = Field(default_factory=list, max_length=100)
    next_actions: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_disposition(self) -> "GraphAgentNodeOutput":
        if self.disposition is GraphNodeDisposition.COMPLETE and not (
            self.evidence_ids or self.artifacts
        ):
            raise ValueError("graph node completion requires evidence")
        if self.disposition is GraphNodeDisposition.WAIT:
            if not self.recipient_ids or not self.correlation_id or not self.reason:
                raise ValueError("graph node wait requires recipients, correlation, and reason")
        if self.disposition is GraphNodeDisposition.FAIL and not self.reason:
            raise ValueError("graph node failure requires a reason")
        return self


GraphNodeHandler = Callable[
    [str, str, WorkflowDefinition, WorkflowRunState, WorkflowAction, WorkflowNode],
    Mapping[str, Any],
]


_INSTRUCTIONS = """
You are the accountable owner of one node inside a durable, non-linear company workflow.
Complete only this node. Use authoritative context, expose uncertainty, and choose only listed outgoing
conditions. Never report completion without durable evidence IDs. If human authority or missing facts are
required, return a correlated wait. If work cannot proceed, fail honestly and state whether retry is useful.
Create new evidence through the bounded artifacts field. Cite an evidence ID only when it appears in the
authoritative prior-token context; never invent one. Source code uses a source-bundle artifact with a files map.
Proactively report risks, decisions, messages, delegations, missing specialists, and next actions. A hiring or
external message is a proposal until the organization authority applies it. Your structured output is a
proposal; deterministic workflow policy commits the transition.
""".strip()


class PydanticGraphNodeRuntime(GraphNodeRuntime):
    """Execute safe structural nodes and provider-backed agent/decision nodes."""

    def __init__(
        self,
        model: Model | str,
        *,
        tools: Sequence[Any] = (),
        handlers: Mapping[NodeKind, GraphNodeHandler] | None = None,
        artifact_store: ArtifactStore | None = None,
        request_limit: int = 12,
        output_tokens_limit: int = 8_000,
        request_timeout_seconds: float = 120,
        max_turn_budget_cents: int = 100,
        context_character_limit: int = 64_000,
        organization_loader: Callable[[str], Organization] | None = None,
        usage_meter: UsageMeter | None = None,
        model_name: str | None = None,
    ) -> None:
        if (
            request_limit < 1
            or output_tokens_limit < 1
            or request_timeout_seconds <= 0
            or max_turn_budget_cents < 1
            or context_character_limit < 1_000
        ):
            raise ValueError("graph node runtime limits must be positive")
        self._model = model
        self._tools = tuple(tools)
        self._handlers = dict(handlers or {})
        self._artifact_store = artifact_store
        self._request_limit = request_limit
        self._output_tokens_limit = output_tokens_limit
        self._request_timeout_seconds = request_timeout_seconds
        self._max_turn_budget_cents = max_turn_budget_cents
        self._context_character_limit = context_character_limit
        self._organization_loader = organization_loader
        self._usage_meter = usage_meter
        self._model_name = (model_name or str(model)).strip()
        if usage_meter is not None and not self._model_name:
            raise ValueError("a metered graph runtime requires a model name")

    @staticmethod
    def _node(definition: WorkflowDefinition, node_id: str) -> WorkflowNode:
        return next(node for node in definition.nodes if node.node_id == node_id)

    def execute_node(
        self,
        *,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        if not action.node_id or not action.token_id:
            raise FatalCommandError("graph execution requires node and token identity")
        node = self._node(definition, action.node_id)
        token = state.token(action.token_id)
        if token.status is not TokenStatus.RUNNING:
            raise FatalCommandError("graph node runtime requires a running token")

        handler = self._handlers.get(node.kind)
        if handler is not None:
            return dict(handler(tenant_id, run_id, definition, state, action, node))
        if node.kind is NodeKind.HUMAN:
            response = token.output.get("human_response")
            if response is not None:
                if not isinstance(response, Mapping):
                    raise FatalCommandError("durable human response must be an object")
                conditions = [edge.condition for edge in definition.outgoing(node.node_id)
                              if edge.condition != "always"]
                configured_condition = node.configuration.get("response_condition")
                if configured_condition is not None:
                    selected = [str(configured_condition)]
                elif len(set(conditions)) == 1:
                    selected = [conditions[0]]
                elif conditions:
                    raise FatalCommandError(
                        "human node with multiple response paths requires response_condition"
                    )
                else:
                    selected = []
                evidence_material = json.dumps(
                    dict(response), allow_nan=False, separators=(",", ":"), sort_keys=True,
                )
                evidence_id = "human-response-" + hashlib.sha256(
                    f"{tenant_id}:{run_id}:{token.token_id}:{evidence_material}".encode()
                ).hexdigest()
                return {
                    "disposition": "complete",
                    "satisfied_conditions": selected,
                    "evidence_ids": [evidence_id],
                    "output": {"human_response": dict(response)},
                }
            recipients = node.configuration.get("recipient_ids", ["human:ceo"])
            if not isinstance(recipients, (list, tuple)) or not recipients:
                raise FatalCommandError("human node requires configured recipient_ids")
            correlation = str(node.configuration.get("correlation_id") or (
                "graph-question-" + hashlib.sha256(action.action_id.encode()).hexdigest()
            ))
            return {
                "disposition": "wait",
                "recipient_ids": [str(item) for item in recipients],
                "correlation_id": correlation,
                "reason": node.purpose,
            }
        if node.kind is NodeKind.TERMINAL:
            evidence = tuple(dict.fromkeys(
                evidence_id
                for prior in state.tokens
                if prior.token_id != token.token_id and prior.status is TokenStatus.SUCCEEDED
                for evidence_id in prior.evidence_ids
            ))
            if not evidence:
                raise FatalCommandError("terminal node cannot accept a run without upstream evidence")
            return {
                "disposition": "complete",
                "satisfied_conditions": [],
                "evidence_ids": list(evidence),
                "output": {"accepted_upstream_evidence": list(evidence)},
            }
        if node.kind not in {NodeKind.AGENT, NodeKind.DECISION}:
            raise FatalCommandError(
                f"node kind {node.kind.value} requires an explicitly registered, idempotent handler"
            )

        outgoing = definition.outgoing(node.node_id)
        available_conditions = sorted({edge.condition for edge in outgoing if edge.condition != "always"})
        instructions = (
            f"{_INSTRUCTIONS}\n\nAssigned role: {node.owner_role or 'decision-owner'}. "
            f"Node: {node.node_id}. Purpose: {node.purpose}. "
            f"Allowed conditional paths: {available_conditions}."
        )
        node_requirements = node.configuration.get("agent_context", {})
        if not isinstance(node_requirements, Mapping):
            raise FatalCommandError("agent node configuration agent_context must be an object")
        authoritative = {
            "run_context": dict(state.context),
            "action": dict(action.payload),
            "node_requirements": dict(node_requirements),
            "prior_tokens": [{
                "node_id": prior.node_id,
                "status": prior.status.value,
                "iteration": prior.iteration,
                "evidence_ids": list(prior.evidence_ids),
                "output": dict(prior.output),
            } for prior in state.tokens if prior.token_id != token.token_id],
        }
        if self._organization_loader is not None:
            organization = self._organization_loader(tenant_id)
            active_agents = sorted(
                (item for item in organization.agents.values() if item.status.value == "active"),
                key=lambda item: item.agent_id,
            )
            visible_agents = active_agents[:128]
            authoritative["standing_organization"] = {
                "organization_id": organization.organization_id,
                "teams": [{
                    "team_id": item.team_id,
                    "purpose": item.purpose,
                    "manager_id": item.manager_id,
                } for item in organization.teams.values()],
                "agents": [{
                    "agent_id": item.agent_id,
                    "role": item.role,
                    "team_id": item.team_id,
                    "manager_id": item.manager_id,
                    "capabilities": sorted(item.capabilities),
                    "tool_grants": sorted(item.tool_grants),
                    "hiring_authority": item.hiring_authority,
                    "spending_limit_cents": item.spending_limit_cents,
                } for item in visible_agents],
                "active_agent_count": len(active_agents),
                "directory_truncated": len(visible_agents) != len(active_agents),
            }
        context_text = json.dumps(
            authoritative, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
        if len(context_text) > self._context_character_limit:
            raise FatalCommandError(
                "authoritative graph context exceeds the configured model-context boundary; "
                "a compaction node is required"
            )
        agent = Agent(
            self._model,
            output_type=GraphAgentNodeOutput,
            instructions=instructions,
            tools=self._tools,
            retries=2,
            name="agent-os-graph-node",
        )
        if self._usage_meter is not None:
            self._usage_meter.reserve_model_turn(
                tenant_id=tenant_id,
                source_id=idempotency_key,
                run_id=run_id,
                category="graph_agent",
                model=self._model_name,
                maximum_cost_cents=self._max_turn_budget_cents,
            )
        result = agent.run_sync(
            f"Execute this node using the authoritative context below:\n{context_text}",
            run_id=idempotency_key,
            metadata={
                "tenant.id": tenant_id,
                "agent_os.run_id": run_id,
                "agent_os.workflow_id": definition.workflow_id,
                "agent_os.node_id": node.node_id,
            },
            model_settings={"timeout": self._request_timeout_seconds},
            usage_limits=UsageLimits(
                cost_limit=Decimal(self._max_turn_budget_cents) / Decimal(100),
                request_limit=self._request_limit,
                output_tokens_limit=self._output_tokens_limit,
            ),
        )
        output = result.output
        usage = model_usage_record(result.usage)
        if self._usage_meter is not None:
            self._usage_meter.settle_model_turn(
                tenant_id=tenant_id,
                source_id=idempotency_key,
                usage=usage,
            )
        unknown = set(output.satisfied_conditions) - set(available_conditions)
        if unknown:
            raise FatalCommandError(f"graph agent selected unknown conditions: {sorted(unknown)}")
        prior_evidence = {
            evidence_id
            for prior in state.tokens
            if prior.token_id != token.token_id
            for evidence_id in prior.evidence_ids
        }
        raw = dict(persist_and_validate_artifacts(
            store=self._artifact_store,
            organization_id=tenant_id,
            idempotency_key=idempotency_key,
            output=output.model_dump(mode="json"),
            allowed_evidence_ids=prior_evidence,
        ))
        artifact_records = raw.get("artifacts", ())
        artifact_ids = {
            str(record["label"]): str(record["artifact_id"])
            for record in artifact_records
            if isinstance(record, Mapping) and record.get("label") and record.get("artifact_id")
        }
        raw["output"] = {
            **raw["output"],
            "summary": output.summary,
            "artifacts": list(artifact_records),
            "artifact_ids": artifact_ids,
            "organization_actions": {
                "observations": list(output.observations),
                "risks": list(output.risks),
                "messages": [item.model_dump(mode="json") for item in output.messages],
                "proposed_work": [item.model_dump(mode="json") for item in output.proposed_work],
                "hiring_requests": [item.model_dump(mode="json") for item in output.hiring_requests],
                "decisions": [item.model_dump(mode="json") for item in output.decisions],
                "next_actions": list(output.next_actions),
            },
            "usage": usage,
        }
        return raw
