"""Safe bootstrap organization used until a tenant customizes its team graph."""

from __future__ import annotations

from agent_os.domain.organization import AgentProfile, HumanParticipant, Organization, Team


def default_organization(tenant_id: str, _: str | None = None) -> Organization:
    """Return a complete, routable organization for the lifecycle roles.

    Tenant-specific organization events will eventually project over this
    bootstrap.  Keeping a real directory even at bootstrap means hallucinated
    recipients are rejected and agents receive authoritative participant IDs.
    """

    teams = {
        "executive": Team("executive", "Executive", "Own mission outcomes", "agent:mission-manager"),
        "research": Team("research", "Research", "Establish evidence and constraints", "agent:research-lead"),
        "product": Team("product", "Product", "Turn evidence into an executable specification", "agent:product-architect"),
        "engineering": Team("engineering", "Engineering", "Build and repair the product", "agent:engineering-manager"),
        "quality": Team("quality", "Quality", "Independently verify customer outcomes", "agent:quality-manager"),
        "release": Team("release", "Release", "Publish and operate accepted releases", "agent:release-manager"),
    }
    agents = {
        "agent:mission-manager": AgentProfile(
            "agent:mission-manager", "mission-manager", "executive",
            capabilities=frozenset({"coordination", "escalation", "resource-planning"}),
            hiring_authority=True,
        ),
        "agent:research-lead": AgentProfile(
            "agent:research-lead", "research-lead", "research", "agent:mission-manager",
            frozenset({"research", "requirements", "risk-discovery"}),
        ),
        "agent:product-architect": AgentProfile(
            "agent:product-architect", "product-architect", "product", "agent:mission-manager",
            frozenset({"product-design", "architecture", "acceptance-criteria"}),
        ),
        "agent:engineering-manager": AgentProfile(
            "agent:engineering-manager", "engineering-manager", "engineering", "agent:mission-manager",
            frozenset({"implementation", "delegation", "code-review"}),
        ),
        "agent:repair-lead": AgentProfile(
            "agent:repair-lead", "repair-lead", "engineering", "agent:engineering-manager",
            frozenset({"debugging", "remediation", "regression-prevention"}),
        ),
        "agent:quality-manager": AgentProfile(
            "agent:quality-manager", "quality-manager", "quality", "agent:mission-manager",
            frozenset({"verification", "evidence-review", "release-gating"}),
        ),
        "agent:release-manager": AgentProfile(
            "agent:release-manager", "release-manager", "release", "agent:mission-manager",
            frozenset({"deployment", "rollback", "operations"}),
        ),
    }
    humans = {
        "human:ceo": HumanParticipant(
            "human:ceo",
            "CEO",
            "executive",
            ("mission authority", "budget authority", "irreversible decisions"),
            response_sla_seconds=86_400,
        )
    }
    return Organization(tenant_id, tenant_id, "Customer organization", teams, agents, humans)
