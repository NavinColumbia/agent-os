"""PydanticAI implementation of the replaceable AgentRuntime port.

The output contract gives every role ways to act like a member of an
organization: progress work, delegate, request hires, challenge decisions,
message peers/managers/humans, raise risks, or wait for correlated input.  The
model proposes these actions; durable organization workflows and policy decide
which consequential actions commit.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models import Model

from agent_os.application.ports import AgentRuntime


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


class HiringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    reason: str
    capabilities: list[str] = Field(default_factory=list)
    requested_count: int = Field(default=1, ge=1, le=1000)
    estimated_budget_cents: int = Field(default=0, ge=0)


class ProposedDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str
    considered_options: list[str] = Field(min_length=1)
    chosen_option: str
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
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
    evidence_ids: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    messages: list[ProposedMessage] = Field(default_factory=list)
    proposed_work: list[ProposedWork] = Field(default_factory=list)
    hiring_requests: list[HiringRequest] = Field(default_factory=list)
    decisions: list[ProposedDecision] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def completion_has_evidence(self) -> "AgentTurnOutput":
        if self.disposition is TurnDisposition.COMPLETE and not self.evidence_ids:
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
Consequential proposals are reviewed by durable policy and approval layers after this turn.
""".strip()


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
    ) -> None:
        if request_limit < 1 or output_tokens_limit < 1 or request_timeout_seconds <= 0:
            raise ValueError("agent runtime limits must be positive")
        self._model = model
        self._tools = tuple(tools)
        self._request_limit = request_limit
        self._output_tokens_limit = output_tokens_limit
        self._request_timeout_seconds = request_timeout_seconds

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
    ) -> Mapping[str, Any]:
        if not organization_id or not run_id or not role or not prompt or not idempotency_key:
            raise ValueError("organization, run, role, prompt, and idempotency key are required")
        if budget_cents < 0:
            raise ValueError("budget_cents cannot be negative")
        context_text = "" if not context else f"\nAuthoritative work context:\n{dict(context)}"
        instructions = f"{_BASE_INSTRUCTIONS}\n\nYour assigned role is: {role}.{context_text}"
        agent = Agent(
            self._model,
            output_type=AgentTurnOutput,
            instructions=instructions,
            tools=self._tools,
            retries=2,
            name="agent-os-role",
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
        usage = result.usage
        return {
            "output": result.output.model_dump(mode="json"),
            "usage": {
                "requests": usage.requests,
                "tool_calls": usage.tool_calls,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
            },
            "idempotency_key": idempotency_key,
        }
