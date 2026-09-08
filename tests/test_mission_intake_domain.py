from __future__ import annotations

from agent_os.domain.mission import (
    GapKind,
    GapStatus,
    IntakeSnapshot,
    MissionCharter,
    OrganizationChangeProposal,
    PersonalCopilotGrant,
    PrerequisiteGap,
    ResourceInventory,
)


def test_chief_of_staff_asks_consolidated_blocking_first_adaptive_questions():
    charter = MissionCharter(
        "mission-1", "tenant-1", "Build a world-class game studio", "ceo", 1_000_000,
        ("validated playable product", "sustainable unit economics"),
    )
    gaps = (
        PrerequisiteGap("nice", GapKind.DATA, "Any visual references?", "Improve art direction", "ceo", False),
        PrerequisiteGap("team", GapKind.HUMAN_ROLE, "Who is already on your team?", "Avoid duplicate hiring", "ceo", True),
        PrerequisiteGap("law", GapKind.LEGAL, "Which launch jurisdictions?", "Plan compliance", "ceo", True),
    )
    snapshot = IntakeSnapshot(charter, ResourceInventory(available_budget_cents=1_000_000), gaps, 1)
    packet = snapshot.questions(limit=2)
    assert {gap.gap_id for gap in packet} == {"team", "law"}
    assert snapshot.can_execute is False


def test_confirmed_recruiter_can_be_proposed_for_reassignment_without_silent_mutation():
    proposal = OrganizationChangeProposal(
        "proposal-1", "chief-of-staff", "Assign recruiting program to Riley",
        ("assign work:hiring-plan to human:recruiter", "create assistant:recruiter-copilot"),
        "CEO confirmed Riley has joined and can own recruiting", 5000, True, True,
    )
    grant = PersonalCopilotGrant(
        "grant-1", "human:recruiter", "agent:recruiter-copilot",
        frozenset({"work.read:assigned", "work.update:assigned", "jira.write:recruiting"}), "human:recruiter",
    )
    assert proposal.needs_human_approval is True
    assert "jira.write:recruiting" in grant.scopes


def test_resolved_blockers_allow_execution_but_waiving_does_not_fake_resolution():
    charter = MissionCharter("m", "t", "Ship", "ceo", 10, ("public URL",))
    resolved = PrerequisiteGap("g", GapKind.CREDENTIAL, "Connect cloud?", "Deploy", "ceo", True,
                               GapStatus.RESOLVED, "credential-ref-1")
    assert IntakeSnapshot(charter, ResourceInventory(), (resolved,), 2).can_execute is True
    waived = PrerequisiteGap("g", GapKind.CREDENTIAL, "Connect cloud?", "Deploy", "ceo", True,
                             GapStatus.WAIVED)
    assert IntakeSnapshot(charter, ResourceInventory(), (waived,), 2).can_execute is False
