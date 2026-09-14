from __future__ import annotations

import pytest

from pydantic_ai.models.test import TestModel

from agent_os.infrastructure.pydantic_agents import ModelSelection, PydanticAgentRuntime


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


def test_agent_turn_can_delegate_hire_message_decide_and_raise_risk_without_network():
    output = {
        "summary": "Split the mission and escalated a regulatory dependency.",
        "disposition": "delegate",
        "progress_percent": 15,
        "evidence_ids": [],
        "observations": ["Two independent research tracks can run in parallel."],
        "risks": ["Trading activity may require licensed human/legal review."],
        "messages": [{
            "audience": "human",
            "kind": "request",
            "recipient_ids": ["human:ceo"],
            "subject": "Jurisdiction",
            "body": "Confirm intended operating jurisdictions.",
            "requires_response": True,
            "correlation_id": "question-jurisdiction",
        }],
        "proposed_work": [{
            "objective": "Research market microstructure",
            "owner_role": "research-lead",
            "specialist_roles": ["quant-researcher"],
            "dependency_ids": [],
            "acceptance_criteria": ["Evidence-backed report"],
            "urgency": 80,
        }],
        "hiring_requests": [{
            "role": "regulatory-counsel",
            "reason": "Review trading constraints",
            "capabilities": ["financial-regulation"],
            "requested_count": 1,
            "estimated_budget_cents": 5000,
        }],
        "decisions": [{
            "intent": "Choose initial validation mode",
            "considered_options": ["paper", "live"],
            "chosen_option": "paper",
            "rationale": "Avoid capital risk before evidence.",
            "evidence_ids": ["risk-policy-1"],
            "confidence": 0.98,
            "reversible": True,
            "needs_human_approval": False,
        }],
        "next_actions": ["Launch both research tracks"],
    }
    meter = FakeUsageMeter()
    runtime = PydanticAgentRuntime(
        TestModel(custom_output_args=output), usage_meter=meter, model_name="test:model",
    )

    result = runtime.run_agent(
        organization_id="tenant-1",
        run_id="run-1",
        role="chief-of-staff",
        prompt="Organize this mission",
        idempotency_key="turn-1",
        budget_cents=1,
    )

    turn = result["output"]
    assert turn["disposition"] == "delegate"
    assert turn["proposed_work"][0]["specialist_roles"] == ["quant-researcher"]
    assert turn["hiring_requests"][0]["role"] == "regulatory-counsel"
    assert turn["messages"][0]["correlation_id"] == "question-jurisdiction"
    assert turn["decisions"][0]["chosen_option"] == "paper"
    assert result["idempotency_key"] == "turn-1"
    assert meter.reservations[0]["maximum_cost_cents"] == 1
    assert meter.reservations[0]["source_id"] == "turn-1"
    assert meter.settlements[0]["usage"]["total_tokens"] == result["usage"]["total_tokens"]
    assert "provider_cost_usd_micros" in result["usage"]


def test_agent_runtime_rejects_unbounded_or_zero_limits():
    with pytest.raises(ValueError, match="limits must be positive"):
        PydanticAgentRuntime(TestModel(), request_timeout_seconds=0)


def test_agent_runtime_selects_and_meters_the_model_for_each_tenant():
    selected_tenants = []
    meter = FakeUsageMeter()
    output = {
        "summary": "Completed with tenant model.", "disposition": "complete",
        "progress_percent": 100, "evidence_ids": ["evidence-1"],
    }

    def select(tenant_id):
        selected_tenants.append(tenant_id)
        return ModelSelection(TestModel(custom_output_args=output), "test:tenant-model")

    runtime = PydanticAgentRuntime(
        TestModel(), usage_meter=meter, model_name="test:fallback", model_selector=select,
    )
    runtime.run_agent(
        organization_id="tenant-special", run_id="run-1", role="engineer",
        prompt="Complete", idempotency_key="tenant-model-turn", budget_cents=1,
    )

    assert selected_tenants == ["tenant-special"]
    assert meter.reservations[0]["model"] == "test:tenant-model"
