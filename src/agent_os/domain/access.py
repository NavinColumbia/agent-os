"""Auditable tenant-role capability policy shared by APIs and experience projections."""

from __future__ import annotations

from collections.abc import Iterable


HUMAN_MEMBERSHIP_ROLES = frozenset({
    "owner",
    "admin",
    "manager",
    "operator",
    "builder",
    "reviewer",
    "billing",
    "client",
    "viewer",
})

BASE_CAPABILITIES = frozenset({
    "artifact.read",
    "mission.read",
    "notification.read",
    "notification.respond",
    "release.read",
})

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "viewer": frozenset({"company.read"}),
    "client": frozenset({"review.read"}),
    "billing": frozenset({"billing.read", "billing.manage", "usage.read"}),
    "reviewer": frozenset({
        "artifact.read", "company.read", "hazard.report", "review.read", "work.read",
    }),
    "builder": frozenset({
        "artifact.publish", "artifact.read", "company.read", "effect.request",
        "hazard.report", "work.execute", "work.read",
    }),
    "manager": frozenset({
        "artifact.publish", "artifact.read", "authority.manage", "company.read",
        "decision.redrive", "effect.request", "effect.settle", "hazard.report",
        "mission.cancel", "mission.create", "mission.steer", "release.manage",
        "review.read", "usage.read", "work.assign", "work.execute", "work.read",
        "workflow.manage", "workforce.manage",
    }),
    "operator": frozenset({
        "artifact.publish", "artifact.read", "authority.manage", "company.read",
        "decision.redrive", "effect.request", "effect.settle", "evidence.erase",
        "hazard.report", "integration.manage", "membership.read", "mission.cancel",
        "mission.create", "mission.steer", "model.read", "notification.read.all",
        "operations.read",
        "operations.recover", "release.manage", "review.read", "usage.read",
        "work.assign", "work.execute", "work.read", "workflow.manage",
        "workforce.manage",
    }),
    "admin": frozenset({
        "company.read", "integration.manage", "membership.manage", "membership.read",
        "model.manage", "model.read", "policy.manage", "usage.read",
    }),
    # Runtime agents use the builder projection but remain non-invitable service identities.
    "agent": frozenset({
        "artifact.publish", "artifact.read", "company.read", "effect.request",
        "hazard.report", "work.execute", "work.read",
    }),
}

ALL_CAPABILITIES = frozenset().union(
    BASE_CAPABILITIES, {"ownership.manage"}, *ROLE_CAPABILITIES.values(),
)
ROLE_CAPABILITIES["owner"] = ALL_CAPABILITIES
ROLE_CAPABILITIES["system"] = ALL_CAPABILITIES

_PERSONA_PRIORITY = (
    (frozenset({"owner", "system"}), "executive"),
    (frozenset({"admin"}), "administrator"),
    (frozenset({"manager"}), "manager"),
    (frozenset({"operator"}), "operator"),
    (frozenset({"builder", "agent"}), "builder"),
    (frozenset({"reviewer"}), "reviewer"),
    (frozenset({"billing"}), "billing"),
    (frozenset({"client"}), "client"),
)


def capabilities_for_roles(roles: Iterable[str]) -> frozenset[str]:
    """Return the union of bounded capabilities for authenticated role assertions."""

    normalized = frozenset(str(role).strip() for role in roles if str(role).strip())
    capabilities = set(BASE_CAPABILITIES)
    for role in normalized:
        capabilities.update(ROLE_CAPABILITIES.get(role, ()))
    return frozenset(capabilities)


def persona_for_roles(roles: Iterable[str]) -> str:
    """Choose a presentation preset without turning the preset into authorization."""

    normalized = frozenset(str(role).strip() for role in roles if str(role).strip())
    for candidates, persona in _PERSONA_PRIORITY:
        if normalized & candidates:
            return persona
    return "viewer"


def roles_have_capability(roles: Iterable[str], capability: str) -> bool:
    return capability in capabilities_for_roles(roles)
