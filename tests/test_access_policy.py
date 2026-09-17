from __future__ import annotations

import pytest

from agent_os.domain.access import (
    HUMAN_MEMBERSHIP_ROLES,
    capabilities_for_roles,
    persona_for_roles,
    roles_have_capability,
)


@pytest.mark.parametrize(
    ("role", "persona", "allowed", "denied"),
    [
        ("owner", "executive", "ownership.manage", None),
        ("admin", "administrator", "membership.manage", "mission.create"),
        ("manager", "manager", "mission.create", "billing.manage"),
        ("operator", "operator", "operations.recover", "billing.manage"),
        ("builder", "builder", "artifact.publish", "mission.create"),
        ("reviewer", "reviewer", "review.read", "artifact.publish"),
        ("billing", "billing", "billing.manage", "mission.create"),
        ("client", "client", "review.read", "company.read"),
        ("viewer", "viewer", "company.read", "mission.create"),
    ],
)
def test_human_roles_project_distinct_personas_and_least_privilege(
    role: str, persona: str, allowed: str, denied: str | None,
):
    assert role in HUMAN_MEMBERSHIP_ROLES
    assert persona_for_roles((role,)) == persona
    assert roles_have_capability((role,), allowed)
    if denied is not None:
        assert not roles_have_capability((role,), denied)


def test_multi_role_membership_unions_capabilities_without_changing_priority():
    capabilities = capabilities_for_roles(("billing", "reviewer"))

    assert {"billing.manage", "review.read", "notification.respond"} <= capabilities
    assert "mission.create" not in capabilities
    assert persona_for_roles(("billing", "reviewer")) == "reviewer"
    assert not roles_have_capability(("admin",), "ownership.manage")


def test_unknown_identity_provider_role_fails_to_viewer_equivalent_baseline():
    capabilities = capabilities_for_roles(("unexpected-provider-role",))

    assert capabilities == capabilities_for_roles(())
    assert "mission.create" not in capabilities
    assert persona_for_roles(("unexpected-provider-role",)) == "viewer"
