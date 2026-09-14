"""PydanticAI implementation of the replaceable AgentRuntime port.

The output contract gives every role ways to act like a member of an
organization: progress work, delegate, request hires, challenge decisions,
message peers/managers/humans, raise risks, or wait for correlated input.  The
model proposes these actions; durable organization workflows and policy decide
which consequential actions commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from enum import Enum
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models import Model

from agent_os.application.ports import AgentRuntime, UsageMeter
from agent_os.infrastructure.proposed_artifacts import ProposedArtifact


class TurnDisposition(str, Enum):
    CONTINUE = "continue"
    DELEGATE = "delegate"
    WAIT_FOR_AGENT = "wait_for_agent"
    WAIT_FOR_HUMAN = "wait_for_human"
    COMPLETE = "complete"
    FAIL = "fail"


class ProposedMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audience: str
    kind: str
    recipient_ids: list[str] = Field(default_factory=list)
    subject: str
    body: str
    requires_response: bool = False
    correlation_id: str | None = None

    @model_validator(mode="after")
    def response_is_correlated(self) -> "ProposedMessage":
        if self.requires_response and not self.correlation_id:
            raise ValueError("messages requesting a response require a correlation_id")
        return self


class ProposedWork(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str
    owner_role: str
    specialist_roles: list[str] = Field(default_factory=list)
    dependency_ids: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    urgency: int = Field(default=50, ge=0, le=100)


class HiringParticipantKind(str, Enum):
    AGENT = "agent"
    HUMAN = "human"
    VENDOR = "vendor"


class HiringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    reason: str
    participant_kind: HiringParticipantKind = HiringParticipantKind.AGENT
    capabilities: list[str] = Field(default_factory=list)
    requested_count: int = Field(default=1, ge=1, le=1000)
    estimated_budget_cents: int = Field(default=0, ge=0)


class ProposedDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str
    considered_options: list[str] = Field(min_length=1)
    chosen_option: str
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    confidence: float = Field(ge=0, le=1)
    reversible: bool
    needs_human_approval: bool = False

    @model_validator(mode="after")
    def selected_option_was_considered(self) -> "ProposedDecision":
        if self.chosen_option not in self.considered_options:
            raise ValueError("chosen_option must be one of considered_options")
        return self


class AgentTurnOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    disposition: TurnDisposition
    progress_percent: int = Field(ge=0, le=100)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    artifacts: list[ProposedArtifact] = Field(default_factory=list, max_length=16)
    observations: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    messages: list[ProposedMessage] = Field(default_factory=list)
    proposed_work: list[ProposedWork] = Field(default_factory=list)
    hiring_requests: list[HiringRequest] = Field(default_factory=list)
    decisions: list[ProposedDecision] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def completion_has_evidence(self) -> "AgentTurnOutput":
        if self.disposition is TurnDisposition.COMPLETE and not (
            self.evidence_ids or self.artifacts
        ):
            raise ValueError("completion requires evidence")
        if self.disposition is TurnDisposition.WAIT_FOR_HUMAN:
            if not any(message.requires_response for message in self.messages):
                raise ValueError("waiting for a human requires a correlated response request")
        return self


_BASE_INSTRUCTIONS = """
You are one accountable member of a persistent AI company, not a chat assistant and not the whole company.
Exercise judgment within your role. Inspect evidence, identify uncertainty, and make the best next decision.
You may propose parallel work, specialists, hiring, peer/upward/human messages, risks, challenges, and decisions.
Never claim completion without durable evidence. Do not conceal blockers or wait silently. Request human input
only when judgment, authority, credentials, physical action, or genuinely missing information requires it.
Create new evidence through the bounded artifacts field. Use evidence_ids only for durable evidence IDs supplied
in authoritative context; never invent an ID. Source code must be a source-bundle artifact with a files map.
Consequential proposals are reviewed by durable policy and approval layers after this turn.
""".strip()


@dataclass(frozen=True)
class ModelSelection:
    """One tenant's resolved model instance and non-secret billing identity."""

    model: Model | str
    name: str

    def __post_init__(self) -> None:
        if not self.name.strip() or len(self.name) > 512:
            raise ValueError("resolved model name must be bounded and nonempty")


def default_model_name(model: Model | str, configured: str | None) -> str:
    if configured is not None:
        return configured.strip()
    if isinstance(model, str):
        return model.strip()
    value = str(getattr(model, "model_name", "") or type(model).__name__).strip()
    return value[:512]


def model_usage_record(usage: Any) -> dict[str, int | None]:
    provider_cost_usd_micros = None
    if usage.cost is not None:
        provider_cost_usd_micros = int(
            (usage.cost * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING)
        )
    return {
        "requests": usage.requests,
        "tool_calls": usage.tool_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "provider_cost_usd_micros": provider_cost_usd_micros,
    }


class PydanticAgentRuntime(AgentRuntime):
    """Provider-portable structured role execution with hard per-turn limits."""

    def __init__(
        self,
        model: Model | str,
        *,
        tools: Sequence[Any] = (),
        request_limit: int = 12,
        output_tokens_limit: int = 8_000,
        request_timeout_seconds: float = 120,
        usage_meter: UsageMeter | None = None,
        model_name: str | None = None,
        model_selector: Callable[[str], ModelSelection] | None = None,
    ) -> None:
        if request_limit < 1 or output_tokens_limit < 1 or request_timeout_seconds <= 0:
            raise ValueError("agent runtime limits must be positive")
        self._model = model
        self._tools = tuple(tools)
        self._request_limit = request_limit
        self._output_tokens_limit = output_tokens_limit
        self._request_timeout_seconds = request_timeout_seconds
        self._usage_meter = usage_meter
        self._model_name = default_model_name(model, model_name)
        self._model_selector = model_selector
        if usage_meter is not None and not self._model_name:
            raise ValueError("a metered agent runtime requires a model name")

    def _selection(self, organization_id: str) -> ModelSelection:
        if self._model_selector is None:
            return ModelSelection(self._model, self._model_name)
        selected = self._model_selector(organization_id)
        if not isinstance(selected, ModelSelection):
            raise TypeError("model selector must return ModelSelection")
        return selected

    def run_agent(
        self,
        *,
        organization_id: str,
        run_id: str,
        role: str,
        prompt: str,
        idempotency_key: str,
        budget_cents: int = 100,
        context: Mapping[str, Any] | None = None,
        usage_category: str = "lifecycle_agent",
    ) -> Mapping[str, Any]:
        if not organization_id or not run_id or not role or not prompt or not idempotency_key:
            raise ValueError("organization, run, role, prompt, and idempotency key are required")
        if budget_cents < 0:
            raise ValueError("budget_cents cannot be negative")
        if not usage_category.strip() or len(usage_category) > 64:
            raise ValueError("usage_category must contain 1 to 64 characters")
        context_text = "" if not context else f"\nAuthoritative work context:\n{dict(context)}"
        instructions = f"{_BASE_INSTRUCTIONS}\n\nYour assigned role is: {role}.{context_text}"
        selection = self._selection(organization_id)
        agent = Agent(
            selection.model,
            output_type=AgentTurnOutput,
            instructions=instructions,
            tools=self._tools,
            retries=2,
            name="agent-os-role",
        )
        if self._usage_meter is not None:
            self._usage_meter.reserve_model_turn(
                tenant_id=organization_id,
                source_id=idempotency_key,
                run_id=run_id,
                category=usage_category,
                model=selection.name,
                maximum_cost_cents=max(1, budget_cents),
            )
        result = agent.run_sync(
            prompt,
            run_id=idempotency_key,
            metadata={
                "tenant.id": organization_id,
                "agent_os.run_id": run_id,
                "agent_os.role": role,
            },
            # Bound each provider request while allowing the durable mission to
            # take as long as useful work genuinely requires. The command lease
            # is renewed independently and transient call failures are retried
            # from durable state by the worker.
            model_settings={"timeout": self._request_timeout_seconds},
            usage_limits=UsageLimits(
                cost_limit=Decimal(budget_cents) / Decimal(100),
                request_limit=self._request_limit,
                output_tokens_limit=self._output_tokens_limit,
            ),
        )
        usage = model_usage_record(result.usage)
        if self._usage_meter is not None:
            self._usage_meter.settle_model_turn(
                tenant_id=organization_id,
                source_id=idempotency_key,
                usage=usage,
            )
        return {
            "output": result.output.model_dump(mode="json"),
            "usage": usage,
            "idempotency_key": idempotency_key,
        }
