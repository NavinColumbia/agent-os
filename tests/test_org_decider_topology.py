from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "orchestra"))

import org_decider  # noqa: E402


def test_unparseable_org_decision_cannot_create_paid_work():
    decision = org_decider._normalize_decision(None)

    assert decision["expand"] is False
    assert decision["how"] == "none"
    assert decision["ambiguous"] is True


def test_string_boolean_cannot_bypass_the_structured_decision_contract():
    decision = org_decider._normalize_decision({
        "expand": "true",
        "how": "more_agents",
        "detail": "Add two agents",
        "rationale": "The backlog is large",
    })

    assert decision["expand"] is False
    assert decision["how"] == "none"
    assert decision["ambiguous"] is True


def test_incomplete_or_inconsistent_expansion_fails_closed():
    missing_detail = org_decider._normalize_decision({
        "expand": True,
        "how": "more_agents",
        "rationale": "The tasks are independent",
    })
    contradictory = org_decider._normalize_decision({
        "expand": False,
        "how": "new_supervisor",
        "detail": "Payments lead",
        "rationale": "Separate authority",
    })

    assert missing_detail["expand"] is False
    assert missing_detail["ambiguous"] is True
    assert contradictory["expand"] is False
    assert contradictory["how"] == "none"
    assert contradictory["ambiguous"] is True


def test_coherent_expansion_proposal_remains_available():
    decision = org_decider._normalize_decision({
        "expand": True,
        "how": "new_supervisor",
        "detail": "Add an independent security assurance lead",
        "rationale": "The maker cannot independently verify the release claim",
    })

    assert decision == {
        "expand": True,
        "how": "new_supervisor",
        "detail": "Add an independent security assurance lead",
        "rationale": "The maker cannot independently verify the release claim",
        "ambiguous": False,
    }


def test_org_decider_prompt_selects_the_smallest_sufficient_shape():
    assert "SMALLEST sufficient organization" in org_decider._EXPAND_PROMPT
    assert "never speculate by adding staff" in org_decider._EXPAND_PROMPT
