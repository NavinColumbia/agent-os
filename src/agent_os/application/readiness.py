"""Honest, derived first-mission readiness over replaceable application ports."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Mapping

from agent_os.application.ports import (
    CompanyDirectory,
    ConnectorRegistry,
    NotificationStore,
    TenantModelStore,
    UsageMeter,
    WorkflowEngine,
)


def project_tenant_readiness(
    *,
    tenant_id: str,
    subject_id: str,
    capabilities: Collection[str],
    engine: WorkflowEngine,
    company_directory: CompanyDirectory | None,
    connector_registry: ConnectorRegistry | None,
    notification_store: NotificationStore | None,
    tenant_model_store: TenantModelStore | None,
    usage_meter: UsageMeter | None,
    billing_enabled: bool,
    execution_plane: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Return facts and explicit unknowns; never infer worker-only credential health."""

    steps: list[dict[str, Any]] = [{
        "id": "identity",
        "title": "Secure company access",
        "status": "complete",
        "required": True,
        "detail": f"Signed in as {subject_id} for this organization.",
        "action_view": None,
    }]
    try:
        runtime_ready = bool(engine.health().get("ok"))
    except Exception:
        runtime_ready = False
    steps.append({
        "id": "control_plane",
        "title": "Durable control plane",
        "status": "complete" if runtime_ready else "blocked",
        "required": True,
        "detail": (
            "Mission state and durable command admission are reachable."
            if runtime_ready else
            "The durable control plane is unavailable; starting work would be unsafe."
        ),
        "action_view": None,
    })

    if execution_plane is not None:
        execution_ready = bool(execution_plane.get("ok"))
        execution_reason = str(
            execution_plane.get("reason") or "worker_unavailable"
        )
        reason_detail = {
            "release_unconfigured": (
                "No execution release is active yet. An operator must complete "
                "release activation before new missions can start."
            ),
            "worker_unavailable": (
                "No current execution worker is available. Existing mission state is "
                "safe, but new work is paused until a worker recovers."
            ),
            "probe_stale": (
                "The execution worker is no longer proving queue access. New work is "
                "paused until its health proof recovers."
            ),
            "queue_stalled": (
                "Ready work is not advancing within the operating window. New mission "
                "admission is paused while operations investigates."
            ),
            "worker_stalled": (
                "An execution worker stopped reporting meaningful progress. Long-running "
                "work is not cancelled, but new missions are paused."
            ),
            "discovery_failing": (
                "The worker cannot reliably discover runnable work. New missions are "
                "paused until discovery recovers."
            ),
            "health_probe_failed": (
                "Execution availability could not be verified. The platform fails closed "
                "for new missions while existing state remains accessible."
            ),
        }.get(
            execution_reason,
            "Execution availability is not currently verified; new mission admission is paused.",
        )
        steps.append({
            "id": "execution_plane",
            "title": "Autonomous execution",
            "status": "complete" if execution_ready else "blocked",
            "required": True,
            "detail": (
                "A release-fenced worker is healthy and accepting durable work."
                if execution_ready else reason_detail
            ),
            "action_view": None,
            "reason": execution_reason,
            "retryable": not execution_ready,
        })

    organization = None
    if company_directory is not None:
        try:
            organization = company_directory.get_organization(tenant_id)
        except Exception:
            organization = None
    steps.append({
        "id": "organization",
        "title": "Standing team",
        "status": "complete" if organization is not None else "blocked",
        "required": True,
        "detail": (
            f"{len(organization.teams)} teams and "
            f"{sum(1 for item in organization.agents.values() if item.status.value == 'active')} "
            "active agents are available."
            if organization is not None else
            "The organization projection is unavailable."
        ),
        "action_view": "company",
    })

    usage = None
    if usage_meter is not None:
        try:
            usage = usage_meter.usage_summary(tenant_id)
        except Exception:
            usage = None
    spend_ready = bool(usage and int(usage.get("remaining_cents") or 0) > 0)
    steps.append({
        "id": "spend_guard",
        "title": "Hard model-spend guard",
        "status": "complete" if spend_ready else "blocked",
        "required": True,
        "detail": (
            f"${int(usage['remaining_cents']) / 100:,.2f} remains inside the current monthly ceiling."
            if usage is not None else
            "No enforceable tenant model-spend meter is available."
        ),
        "action_view": "billing" if billing_enabled else None,
    })

    model_setting = None
    model_policy_available = tenant_model_store is not None
    if tenant_model_store is not None:
        try:
            model_setting = tenant_model_store.get_model_setting(tenant_id)
        except Exception:
            model_policy_available = False
    model_runtime_proven = False
    if usage_meter is not None:
        try:
            model_runtime_proven = any(
                item.get("status") == "settled" and int(item.get("requests") or 0) > 0
                for item in usage_meter.list_usage_events(tenant_id, limit=20)
            )
        except Exception:
            model_runtime_proven = False
    if not model_policy_available:
        model_status = "blocked"
        model_detail = "The tenant model-policy projection is unavailable."
    elif model_runtime_proven:
        model_status = "complete"
        model_detail = (
            "At least one provider-backed, metered agent turn completed successfully. "
            + (
                f"Current policy is {model_setting['provider']}:{model_setting['model_name']}."
                if model_setting is not None else
                "The platform-managed model policy remains active."
            )
        )
    elif model_setting is None:
        model_status = "verify_on_first_use"
        model_detail = (
            "The platform model policy is inherited. Provider access is proved by the first "
            "metered agent turn; this API does not pretend to observe worker-only credentials."
        )
    else:
        model_status = "verify_on_first_use"
        model_detail = (
            f"New turns use {model_setting['provider']}:{model_setting['model_name']} "
            f"with {model_setting['credential_source']} credentials. Provider access is still "
            "verified only by a successful metered agent turn."
        )
    steps.append({
        "id": "model_runtime",
        "title": "AI model policy",
        "status": model_status,
        "required": True,
        "detail": model_detail,
        "action_view": "company",
    })

    attention_ready = False
    external_route_count = 0
    if notification_store is not None:
        try:
            notification_store.list_notifications(
                tenant_id, recipient_id=subject_id, limit=1,
            )
            attention_ready = True
            external_route_count = sum(
                1 for item in notification_store.list_notification_routes(tenant_id)
                if item.get("active")
            )
        except Exception:
            external_route_count = 0
    steps.append({
        "id": "attention",
        "title": "Human decision inbox",
        "status": "complete" if attention_ready else "blocked",
        "required": True,
        "detail": (
            "Durable in-app decisions are active"
            + (
                f" with {external_route_count} external route(s)."
                if external_route_count else
                "; external phone/chat routing is optional."
            )
            if attention_ready else
            "Human waits cannot be routed durably in this deployment."
        ),
        "action_view": "inbox",
    })

    connector_count = 0
    connector_projection_available = connector_registry is not None
    if connector_registry is not None:
        try:
            connector_count = sum(
                1 for item in connector_registry.list_connectors(tenant_id)
                if item.get("active")
            )
        except Exception:
            connector_projection_available = False
    steps.append({
        "id": "integrations",
        "title": "External tools",
        "status": (
            "unavailable" if not connector_projection_available else
            ("complete" if connector_count else "optional")
        ),
        "required": False,
        "detail": (
            f"{connector_count} governed connector(s) are active."
            if connector_count else
            (
                "No external tools are connected. Add only what a mission actually needs."
                if connector_projection_available else
                "The optional connector catalog is unavailable."
            )
        ),
        "action_view": "integrations" if connector_registry is not None else None,
    })

    try:
        has_mission = bool(engine.list_runs(tenant_id, limit=1))
    except Exception:
        has_mission = False
    steps.append({
        "id": "first_mission",
        "title": "First governed mission",
        "status": "complete" if has_mission else "action",
        "required": False,
        "detail": (
            "At least one mission is retained in durable history."
            if has_mission else
            "Describe an outcome, constraints, budget, and evidence of done."
        ),
        "action_view": "missions",
    })
    blockers = [
        str(item["id"]) for item in steps
        if item["required"] and item["status"] == "blocked"
    ]
    can_create = "mission.create" in capabilities
    model_verification_pending = model_status == "verify_on_first_use"
    return {
        "overall": (
            "blocked" if blockers else
            (
                "verification_pending" if has_mission and model_verification_pending else
                ("active" if has_mission else "ready_for_first_mission")
            )
        ),
        "can_start_mission": can_create and not blockers,
        "show_onboarding": can_create and (
            not has_mission or bool(blockers) or model_verification_pending
        ),
        "blockers": blockers,
        "steps": steps,
    }
