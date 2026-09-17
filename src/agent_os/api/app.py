"""FastAPI control surface for the V2 product lifecycle."""

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import time
from typing import Any, Annotated, Mapping
from urllib.parse import urlparse

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from agent_os.api.auth import Authenticator, Principal
from agent_os.application.billing import BillingService
from agent_os.application.mission import mission_planning_run_id
from agent_os.application.mission_control import project_mission_control
from agent_os.application.readiness import project_tenant_readiness
from agent_os.application.runtime_effects import build_runtime_authority
from agent_os.application.ports import (
    ArtifactStore,
    CompanyDirectory,
    ConnectorRegistry,
    GraphWorkflowEngine,
    GraphRunInspector,
    MembershipStore,
    MissionControlStore,
    NotificationStore,
    OrganizationLedger,
    PreviewDeploymentStore,
    UsageMeter,
    TenantModelStore,
    WorkflowEngine,
    WorkflowReceipt,
)
from agent_os.domain.lifecycle import Event, EventKind, LifecycleState, TransitionRejected
from agent_os.domain.mission_model import (
    AuthorityGrant,
    Claim,
    ClaimStatus,
    EffectRequest,
    EffectRisk,
    EvidenceKind,
    EvidenceRef,
    Hazard,
    HazardSeverity,
    MissionSpec,
    SafeMode,
)
from agent_os.domain.notifications import (
    NotificationPreferenceMode,
    NotificationPreferences,
)
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import (
    TokenStatus,
    WorkflowEvent,
    WorkflowEventKind,
    WorkflowTransitionRejected,
)


_MAX_MISSION_SUBPROGRAMS = 256


def _encode_notification_cursor(item: Mapping[str, Any]) -> str:
    created_at = str(item.get("created_at") or "")
    notification_id = str(item.get("notification_id") or "")
    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None or not 1 <= len(notification_id) <= 128:
        raise ValueError("notification cannot form a stable cursor")
    raw = json.dumps(
        {"v": 1, "created_at": parsed.isoformat(), "notification_id": notification_id},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_notification_cursor(value: str) -> tuple[datetime, str]:
    if not 1 <= len(value) <= 1_024 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("notification cursor is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(decoded)
        if not isinstance(payload, Mapping) or payload.get("v") != 1:
            raise ValueError
        created_at = datetime.fromisoformat(
            str(payload.get("created_at") or "").replace("Z", "+00:00")
        )
        notification_id = str(payload.get("notification_id") or "")
    except (
        ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error,
    ) as exc:
        raise ValueError("notification cursor is invalid") from exc
    if (
        created_at.tzinfo is None
        or not 1 <= len(notification_id) <= 128
        or "\0" in notification_id
    ):
        raise ValueError("notification cursor is invalid")
    return created_at, notification_id


def _encode_experience_cursor(tenant_id: str, sequence: int) -> str:
    if not tenant_id.strip() or sequence < 0:
        raise ValueError("experience cursor is invalid")
    raw = json.dumps(
        {"v": 1, "tenant_id": tenant_id, "sequence": sequence},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_experience_cursor(value: str, tenant_id: str) -> int:
    if not 1 <= len(value) <= 1_024 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("experience cursor is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(decoded)
        if (
            not isinstance(payload, Mapping)
            or payload.get("v") != 1
            or payload.get("tenant_id") != tenant_id
            or not isinstance(payload.get("sequence"), int)
            or isinstance(payload.get("sequence"), bool)
        ):
            raise ValueError
        sequence = int(payload["sequence"])
    except (
        ValueError, TypeError, KeyError, UnicodeDecodeError,
        json.JSONDecodeError, binascii.Error,
    ) as exc:
        raise ValueError("experience cursor is invalid for this organization") from exc
    if sequence < 0:
        raise ValueError("experience cursor is invalid")
    return sequence


def _project_mission_subprograms(
    graph_engine: GraphWorkflowEngine,
    tenant_id: str,
    root_run_id: str | None,
) -> tuple[list[Mapping[str, Any]], bool]:
    """Return a bounded, tenant-fenced hierarchy of recursively launched missions."""

    if root_run_id is None:
        return [], False
    root = graph_engine.get_graph_run(tenant_id, root_run_id)
    if root is None:
        return [], False
    queue: list[tuple[str, int]] = [(root_run_id, 0)]
    seen = {root_run_id}
    projected: list[Mapping[str, Any]] = []
    truncated = False
    while queue:
        parent_run_id, parent_depth = queue.pop(0)
        parent = graph_engine.get_graph_run(tenant_id, parent_run_id)
        if parent is None:
            continue
        child_tokens = sorted(
            (
                (token.token_id, str(token.output["child_run_id"]))
                for token in parent.tokens
                if isinstance(token.output.get("child_run_id"), str)
                and token.output["child_run_id"]
            ),
            key=lambda item: (item[1], item[0]),
        )
        for parent_token_id, child_run_id in child_tokens:
            if child_run_id in seen:
                continue
            if len(projected) >= _MAX_MISSION_SUBPROGRAMS:
                truncated = True
                return projected, truncated
            seen.add(child_run_id)
            child = graph_engine.get_graph_run(tenant_id, child_run_id)
            if child is None:
                projected.append({
                    "run_id": child_run_id,
                    "parent_run_id": parent_run_id,
                    "parent_token_id": parent_token_id,
                    "depth": parent_depth + 1,
                    "status": "unavailable",
                })
                continue
            counts = {
                status.value: sum(1 for token in child.tokens if token.status is status)
                for status in TokenStatus
            }
            program = child.context.get("mission_program")
            projected.append({
                "run_id": child.run_id,
                "parent_run_id": parent_run_id,
                "parent_token_id": parent_token_id,
                "depth": parent_depth + 1,
                "workflow_id": child.workflow_id,
                "workflow_version": child.workflow_version,
                "state_version": child.version,
                "status": child.status.value,
                "objective": (
                    program.get("objective")
                    if isinstance(program, Mapping) else None
                ),
                "token_counts": counts,
                "failure": child.failure,
            })
            queue.append((child_run_id, parent_depth + 1))
    return projected, truncated


class DirectiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=50_000)
    title: str | None = Field(default=None, max_length=200)
    budget_limit_cents: int = Field(default=0, ge=0, le=100_000_000_000)
    human_involvement_mode: str = Field(
        default="balanced", pattern=r"^(autonomous|balanced|collaborative)$",
    )
    daily_interrupt_limit: int = Field(default=8, ge=0, le=100)


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=256)
    kind: EventKind
    expected_version: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class CancellationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=256)
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2_000)


class MutationResponse(BaseModel):
    workflow_id: str
    run_id: str
    accepted: bool
    duplicate: bool


class WorkflowNodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=256)
    kind: str
    purpose: str = Field(min_length=1, max_length=4_000)
    owner_role: str | None = Field(default=None, max_length=256)
    configuration: dict[str, Any] = Field(default_factory=dict)


class WorkflowEdgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=256)
    target: str = Field(min_length=1, max_length=256)
    condition: str = Field(default="always", min_length=1, max_length=256)
    priority: int = Field(default=0, ge=-1_000_000, le=1_000_000)


class WorkflowDefinitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=256)
    version: int = Field(ge=1)
    entry_node_id: str = Field(min_length=1, max_length=256)
    nodes: list[WorkflowNodeRequest] = Field(min_length=1, max_length=5_000)
    edges: list[WorkflowEdgeRequest] = Field(default_factory=list, max_length=20_000)
    supersedes_version: int | None = Field(default=None, ge=1)


class GraphRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_version: int = Field(ge=1)
    context: dict[str, Any] = Field(default_factory=dict)


class GraphEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=256)
    kind: WorkflowEventKind
    expected_version: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class ArtifactUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_base64: str = Field(max_length=3_000_000)
    media_type: str = Field(min_length=1, max_length=256)


class AgentHireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=128)
    team_id: str = Field(min_length=1, max_length=128)
    manager_id: str = Field(default="agent:mission-manager", min_length=1, max_length=256)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    tool_grants: list[str] = Field(default_factory=list, max_length=64)
    hiring_authority: bool = False
    spending_limit_cents: int = Field(default=0, ge=0, le=100_000_000)


class AgentRetireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2_000)


class HiringProposalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    reason: str = Field(min_length=1, max_length=2_000)
    team_id: str | None = Field(default=None, min_length=1, max_length=128)
    manager_id: str | None = Field(default="agent:mission-manager", min_length=1, max_length=256)
    tool_grants: list[str] = Field(default_factory=list, max_length=64)
    spending_limit_cents: int = Field(default=0, ge=0, le=100_000_000)


class ExternalOnboardingConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)
    identity_subject: str | None = Field(default=None, min_length=1, max_length=256)
    response_sla_seconds: int = Field(default=86_400, ge=60, le=2_678_400)
    quality_criteria: list[str] = Field(default_factory=list, max_length=32)
    attestations: list[str] = Field(min_length=3, max_length=16)


class ConnectorRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2_000)
    allowed_path_prefixes: list[str] = Field(min_length=1, max_length=32)
    allowed_methods: list[str] = Field(default_factory=lambda: ["GET"], max_length=6)
    auth_kind: str = Field(default="none", pattern=r"^(none|bearer|header)$")
    credential_ref: str | None = Field(default=None, max_length=128)
    auth_header: str | None = Field(default=None, max_length=128)
    idempotency_header: str | None = Field(default=None, max_length=128)
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_response_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=8 * 1024 * 1024)


class ConnectorDisableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2_000)


class NotificationRouteRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: str = Field(min_length=2, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    connector_id: str = Field(min_length=2, max_length=64)
    path: str = Field(min_length=1, max_length=2_000)
    categories: list[str] = Field(min_length=1, max_length=16)
    payload_format: str = Field(default="agent-os", pattern=r"^(agent-os|slack)$")
    destination: str | None = Field(default=None, min_length=1, max_length=256)
    recipient_ids: list[str] = Field(default_factory=lambda: ["human:ceo"], min_length=1, max_length=128)
    redaction_policy: str = Field(default="summary", pattern=r"^(summary|full)$")


class NotificationRouteDisableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2_000)


class NotificationStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(pattern=r"^(unread|read|dismissed|snoozed)$")
    snoozed_until: str | None = None


class NotificationPreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: NotificationPreferenceMode = NotificationPreferenceMode.BALANCED
    browser_notifications: bool = False
    quiet_hours_start: str | None = Field(default=None, pattern=r"^[0-2][0-9]:[0-5][0-9]$")
    quiet_hours_end: str | None = Field(default=None, pattern=r"^[0-2][0-9]:[0-5][0-9]$")
    timezone: str = Field(default="UTC", min_length=1, max_length=128)
    digest_interval_minutes: int = Field(default=60)


class DecisionResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: dict[str, Any] = Field(min_length=1, max_length=32)


class InvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: list[str] = Field(min_length=1, max_length=3)
    expires_in_seconds: int = Field(default=86_400, ge=300, le=2_592_000)


class InvitationClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=32, max_length=2_000)


class MembershipRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2_000)


class TenantModelSettingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(pattern=r"^(openai|anthropic|google)$")
    model_name: str = Field(min_length=1, max_length=256)
    credential_ref: str | None = Field(default=None, min_length=1, max_length=128)


class BillingCheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=64)


class MissionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=50_000)
    accountable_owner_id: str | None = Field(default=None, min_length=1, max_length=256)
    budget_limit_cents: int = Field(default=0, ge=0, le=100_000_000_000)
    success_measures: list[str] = Field(min_length=1, max_length=64)
    constraints: list[str] = Field(default_factory=list, max_length=64)
    prohibited_effects: list[str] = Field(default_factory=list, max_length=64)
    risk_tier: str = Field(default="moderate", pattern=r"^(low|moderate|high|critical)$")
    human_involvement_mode: str = Field(
        default="balanced", pattern=r"^(autonomous|balanced|collaborative)$",
    )
    daily_interrupt_limit: int = Field(default=8, ge=0, le=100)


class MissionRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2_000)
    objective: str = Field(min_length=1, max_length=50_000)
    accountable_owner_id: str = Field(min_length=1, max_length=256)
    budget_limit_cents: int = Field(ge=0, le=100_000_000_000)
    success_measures: list[str] = Field(min_length=1, max_length=64)
    constraints: list[str] = Field(default_factory=list, max_length=64)
    prohibited_effects: list[str] = Field(default_factory=list, max_length=64)
    risk_tier: str = Field(default="moderate", pattern=r"^(low|moderate|high|critical)$")
    human_involvement_mode: str = Field(
        default="balanced", pattern=r"^(autonomous|balanced|collaborative)$",
    )
    daily_interrupt_limit: int = Field(default=8, ge=0, le=100)


class MissionEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=256)
    kind: EvidenceKind
    artifact_ref: str = Field(min_length=1, max_length=2_000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: str
    recorded_at: str
    media_type: str = Field(default="application/octet-stream", min_length=1, max_length=256)
    contains_personal_data: bool = False
    retention_until: str | None = None
    source_uri: str | None = Field(default=None, max_length=2_000)


class MissionClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1, max_length=256)
    statement: str = Field(min_length=1, max_length=8_000)
    status: ClaimStatus
    valid_from: str
    recorded_at: str
    evidence_ids: list[str] = Field(default_factory=list, max_length=128)
    depends_on_claim_ids: list[str] = Field(default_factory=list, max_length=128)
    valid_to: str | None = None
    supersedes_claim_id: str | None = Field(default=None, max_length=256)


class MissionHazardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hazard_id: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=4_000)
    unacceptable_loss: str = Field(min_length=1, max_length=4_000)
    severity: HazardSeverity
    safety_constraints: list[str] = Field(min_length=1, max_length=64)
    unsafe_control_actions: list[str] = Field(min_length=1, max_length=64)
    fallback_mode: SafeMode
    requires_human_release: bool = False


class AuthorityGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: str = Field(min_length=1, max_length=256)
    delegate_id: str = Field(min_length=1, max_length=256)
    allowed_effects: list[str] = Field(min_length=1, max_length=64)
    allowed_resources: list[str] = Field(min_length=1, max_length=64)
    budget_limit_cents: int = Field(default=0, ge=0, le=100_000_000_000)
    valid_from: str
    expires_at: str
    delegation_chain: list[str] = Field(min_length=1, max_length=32)
    parent_grant_id: str | None = Field(default=None, max_length=256)
    human_approved: bool = False
    approval_binding_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    approval_evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    policy_version: str = Field(default="agent-os-baseline-policy-v1", min_length=1, max_length=256)


class MissionEffectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect_id: str = Field(min_length=1, max_length=256)
    actor_id: str = Field(min_length=1, max_length=256)
    authority_grant_id: str = Field(min_length=1, max_length=256)
    action: str = Field(min_length=1, max_length=256)
    resource: str = Field(min_length=1, max_length=2_000)
    risk: EffectRisk
    estimated_cost_cents: int = Field(default=0, ge=0, le=100_000_000_000)
    reversible: bool
    idempotency_key: str = Field(min_length=8, max_length=256)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hazard_ids: list[str] = Field(default_factory=list, max_length=64)
    purpose: str = Field(default="", max_length=4_000)
    requires_human_approval: bool = False


class AuthorityRevocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2_000)


class EvidenceErasureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2_000)


class MissionEffectSettlementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actual_cost_cents: int = Field(ge=0, le=100_000_000_000)
    succeeded: bool


_HUMAN_EVENTS = {
    EventKind.WAIT_RESOLVED,
    EventKind.RECOVERY_REQUESTED,
    EventKind.CANCEL_REQUESTED,
}
_INTERNAL_ROLES = {"agent", "operator", "system"}
_MAX_API_ARTIFACT_BYTES = 2 * 1024 * 1024
_MAX_STRIPE_WEBHOOK_BYTES = 1_000_000
_MAX_HUMAN_RESPONSE_BYTES = 16 * 1024
_DEPLOYMENT_RECEIPT_MEDIA_TYPES = (
    "application/vnd.agent-os.static-site-release+json",
    "application/vnd.agent-os.service-release+json",
    "application/vnd.agent-os.service-release-failure+json",
)
_WEB_ROOT = Path(__file__).with_name("web")
_PUBLIC_IDENTITY_KEYS = {
    "identity_mode", "authorization_url", "token_url", "client_id", "scope", "audience",
    "authorization_audience_parameter", "redirect_uri", "billing_mode",
}


def _browser_identity_config(raw: Mapping[str, str] | None) -> tuple[dict[str, str], str]:
    values = dict(raw or {"identity_mode": "manual"})
    if set(values) - _PUBLIC_IDENTITY_KEYS:
        raise ValueError("browser identity configuration contains an unsupported field")
    if any(not isinstance(value, str) or len(value) > 2_000 or any(
        character in value for character in "\r\n\0"
    ) for value in values.values()):
        raise ValueError("browser identity configuration contains an invalid value")
    mode = values.get("identity_mode", "manual")
    if mode not in {"manual", "hmac", "oidc"}:
        raise ValueError("browser identity mode must be manual, hmac, or oidc")
    if values.get("billing_mode", "disabled") not in {"disabled", "stripe"}:
        raise ValueError("browser billing mode must be disabled or stripe")
    if mode != "oidc":
        public = {"identity_mode": mode}
        if "billing_mode" in values:
            public["billing_mode"] = values["billing_mode"]
        return public, ""
    required = (
        "authorization_url", "token_url", "client_id", "scope", "audience", "redirect_uri",
    )
    if any(not values.get(key) for key in required):
        raise ValueError("OIDC browser identity configuration is incomplete")
    for key in ("authorization_url", "token_url"):
        parsed = urlparse(values[key])
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(f"OIDC browser {key} must be an HTTPS URL")
    parameter = values.get("authorization_audience_parameter", "")
    if parameter and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", parameter) is None:
        raise ValueError("OIDC authorization audience parameter is invalid")
    parsed_token = urlparse(values["token_url"])
    return {key: values[key] for key in _PUBLIC_IDENTITY_KEYS if key in values}, (
        f"{parsed_token.scheme}://{parsed_token.netloc}"
    )


def _run_id(organization_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"agent-os:directive:v2:{organization_id}:{idempotency_key}".encode()).hexdigest()
    return f"run-{digest[:32]}"


def _response(receipt: WorkflowReceipt, run_id: str) -> MutationResponse:
    return MutationResponse(
        workflow_id=receipt.workflow_id,
        run_id=run_id,
        accepted=receipt.accepted,
        duplicate=receipt.duplicate,
    )


def _principal_experience(principal: Principal) -> Mapping[str, Any]:
    """Project authorization into honest navigation hints for any client."""

    roles = set(principal.roles)
    owner = bool(roles & {"owner", "system"})
    operator = bool(roles & {"operator"})
    agent = bool(roles & {"agent"})
    if owner:
        persona = "executive"
    elif operator:
        persona = "operator"
    elif agent:
        persona = "builder"
    else:
        persona = "viewer"
    capabilities = {
        "mission.read",
        "notification.read",
        "release.read",
    }
    if owner or operator:
        capabilities.update({
            "mission.create", "mission.cancel", "mission.steer",
            "notification.respond", "decision.redrive", "company.read",
            "integration.manage",
        })
    if owner:
        capabilities.update({
            "membership.manage", "model.manage", "billing.manage",
            "authority.manage", "policy.manage",
        })
    if agent:
        capabilities.update({"work.read", "work.execute", "notification.respond"})
    return {
        "subject_id": principal.subject_id,
        "organization_id": principal.organization_id,
        "roles": sorted(principal.roles),
        "persona": persona,
        "capabilities": sorted(capabilities),
    }


def _notification_attention(category: str, payload: Mapping[str, Any]) -> Mapping[str, str]:
    disposition = str(payload.get("attention_disposition") or "")
    severity = str(payload.get("severity") or payload.get("risk") or "").lower()
    if disposition == "interrupt" or severity in {"critical", "irreversible"}:
        level = "time_sensitive"
    elif category in {"human_action_required", "operator_attention", "run_failed"}:
        level = "time_sensitive"
    elif category in {"run_succeeded", "run_cancelled", "work_recovered"}:
        level = "passive"
    else:
        level = "active"
    rationale = str(
        payload.get("attention_reason")
        or payload.get("reason")
        or {
            "human_action_required": "Work is waiting for a decision only you can make.",
            "operator_attention": "The system needs an operator to restore healthy progress.",
            "run_failed": "A mission stopped before its acceptance contract was satisfied.",
            "management_attention": "A manager detected material variance or stalled progress.",
            "run_succeeded": "A mission reported a verified completion outcome.",
            "run_cancelled": "A mission was cancelled and no further execution is expected.",
            "work_recovered": "Previously unhealthy work resumed or recovered.",
        }.get(category, "This update is part of the durable mission record.")
    )
    return {"level": level, "rationale": rationale[:2_000]}


def _organization_view(organization) -> Mapping[str, Any]:
    return {
        "tenant_id": organization.tenant_id,
        "organization_id": organization.organization_id,
        "name": organization.name,
        "teams": [{
            "team_id": item.team_id,
            "name": item.name,
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
            "status": item.status.value,
        } for item in organization.agents.values()],
        "humans": [{
            "participant_id": item.participant_id,
            "display_name": item.display_name,
            "team_id": item.team_id,
            "responsibilities": list(item.responsibilities),
            "manager_id": item.manager_id,
            "response_sla_seconds": item.response_sla_seconds,
            "quality_criteria": list(item.quality_criteria),
            "active": item.active,
        } for item in organization.humans.values()],
        "services": [{
            "participant_id": item.participant_id,
            "name": item.name,
            "capabilities": sorted(item.capabilities),
            "owner_id": item.owner_id,
            "active": item.active,
        } for item in organization.services.values()],
    }


def create_app(
    *,
    engine: WorkflowEngine,
    identity: Authenticator,
    graph_engine: GraphWorkflowEngine | None = None,
    notification_store: NotificationStore | None = None,
    artifact_store: ArtifactStore | None = None,
    preview_deployments: PreviewDeploymentStore | None = None,
    company_directory: CompanyDirectory | None = None,
    connector_registry: ConnectorRegistry | None = None,
    membership_store: MembershipStore | None = None,
    tenant_model_store: TenantModelStore | None = None,
    usage_meter: UsageMeter | None = None,
    mission_control: MissionControlStore | None = None,
    billing_service: BillingService | None = None,
    client_identity_config: Mapping[str, str] | None = None,
    experience_stream_seconds: float = 55.0,
    shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    if not 0.01 <= experience_stream_seconds <= 300:
        raise ValueError("experience stream lifetime must be between 0.01 and 300 seconds")
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if shutdown is not None:
                shutdown()

    app = FastAPI(
        title="Agent OS Control API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/v2/openapi.json",
        lifespan=lifespan,
    )
    public_identity, token_origin_value = _browser_identity_config(client_identity_config)
    token_origin = f" {token_origin_value}" if token_origin_value else ""
    workspace_headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"connect-src 'self'{token_origin}; font-src 'self'; base-uri 'none'; "
            "manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; "
            "form-action 'none'; object-src 'none'"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }

    @app.get("/", include_in_schema=False)
    def workspace_root() -> RedirectResponse:
        return RedirectResponse("/app", status_code=307, headers={"Cache-Control": "no-store"})

    @app.get("/app", include_in_schema=False)
    def ceo_workspace() -> FileResponse:
        return FileResponse(
            _WEB_ROOT / "ceo.html",
            media_type="text/html",
            headers={
                **workspace_headers,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )

    @app.get("/assets/ceo.css", include_in_schema=False)
    def ceo_styles() -> FileResponse:
        return FileResponse(
            _WEB_ROOT / "ceo.css", media_type="text/css",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/assets/ceo.js", include_in_schema=False)
    def ceo_script() -> FileResponse:
        return FileResponse(
            _WEB_ROOT / "ceo.js", media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/assets/app-icon.svg", include_in_schema=False)
    def app_icon() -> FileResponse:
        return FileResponse(
            _WEB_ROOT / "app-icon.svg", media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/app.webmanifest", include_in_schema=False)
    def app_manifest() -> JSONResponse:
        return JSONResponse({
            "name": "Agent OS Workspace",
            "short_name": "Agent OS",
            "description": "Govern missions, decisions, evidence, and AI company operations.",
            "start_url": "/app",
            "scope": "/",
            "display": "standalone",
            "background_color": "#080b0d",
            "theme_color": "#080b0d",
            "icons": [{
                "src": "/assets/app-icon.svg", "sizes": "any", "type": "image/svg+xml",
                "purpose": "any maskable",
            }],
        }, headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/service-worker.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(
            _WEB_ROOT / "service-worker.js", media_type="text/javascript",
            headers={
                "Cache-Control": "no-cache", "Service-Worker-Allowed": "/",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/v2/client-config", include_in_schema=False)
    def get_client_config() -> JSONResponse:
        return JSONResponse(
            public_identity,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    def base_principal(
        authorization: Annotated[str | None, Header()] = None,
        aos_session: Annotated[str | None, Cookie()] = None,
    ) -> Principal:
        try:
            return Principal.from_mapping(identity.authenticate(authorization, aos_session))
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    def current_principal(
        principal: Annotated[Principal, Depends(base_principal)],
        selected_organization: Annotated[
            str | None, Header(alias="X-Agent-OS-Organization", max_length=255)
        ] = None,
    ) -> Principal:
        organization_id = (selected_organization or "").strip()
        if not organization_id or organization_id == principal.organization_id:
            return principal
        if membership_store is None:
            raise HTTPException(status_code=403, detail="organization selection is unavailable")
        roles = membership_store.roles_for(organization_id, principal.subject_id)
        if not roles:
            raise HTTPException(status_code=403, detail="active organization membership is required")
        return Principal(principal.subject_id, organization_id, roles)

    @app.exception_handler(TransitionRejected)
    def transition_rejected(_: Request, exc: TransitionRejected) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(WorkflowTransitionRejected)
    def workflow_transition_rejected(_: Request, exc: WorkflowTransitionRejected) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/health")
    def health() -> Mapping[str, str]:
        return {"status": "ok", "service": "agent-os-v2"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        report = dict(engine.health())
        return JSONResponse(status_code=200 if report.get("ok") else 503, content=report)

    @app.get("/v2/me")
    def get_current_experience(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Mapping[str, Any]:
        return _principal_experience(principal)

    @app.get("/v2/readiness")
    def get_tenant_readiness(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Mapping[str, Any]:
        experience = _principal_experience(principal)
        return project_tenant_readiness(
            tenant_id=principal.organization_id,
            subject_id=principal.subject_id,
            capabilities=experience["capabilities"],
            engine=engine,
            company_directory=company_directory,
            connector_registry=connector_registry,
            notification_store=notification_store,
            tenant_model_store=tenant_model_store,
            usage_meter=usage_meter,
            billing_enabled=billing_service is not None,
        )

    if membership_store is not None:
        @app.get("/v2/organizations")
        def list_organizations(
            principal: Annotated[Principal, Depends(base_principal)],
        ) -> Mapping[str, Any]:
            memberships_by_id = {
                str(item["organization_id"]): dict(item)
                for item in membership_store.organizations_for(principal.subject_id)
            }
            base = memberships_by_id.get(principal.organization_id)
            if base is None:
                memberships_by_id[principal.organization_id] = {
                    "organization_id": principal.organization_id,
                    "roles": sorted(principal.roles),
                    "source": "identity_provider",
                    "joined_at": None,
                }
            else:
                base["roles"] = sorted(set(base.get("roles", ())) | set(principal.roles))
                base["source"] = "identity_provider_and_membership"
            return {
                "subject_id": principal.subject_id,
                "default_organization_id": principal.organization_id,
                "items": [memberships_by_id[key] for key in sorted(memberships_by_id)],
            }

        @app.post("/v2/invitations", status_code=201)
        def create_invitation(
            body: InvitationCreateRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "system"}):
                raise HTTPException(status_code=403, detail="invitations require owner authority")
            try:
                return membership_store.create_invitation(
                    tenant_id=principal.organization_id,
                    roles=tuple(body.roles),
                    actor_id=principal.subject_id,
                    expires_in_seconds=body.expires_in_seconds,
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.post("/v2/invitations/claim")
        def claim_invitation(
            body: InvitationClaimRequest,
            principal: Annotated[Principal, Depends(base_principal)],
        ) -> Mapping[str, Any]:
            try:
                return membership_store.claim_invitation(
                    token=body.token,
                    subject_id=principal.subject_id,
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.get("/v2/memberships")
        def list_memberships(
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="membership inventory requires operator authority")
            return {"items": list(membership_store.list_members(principal.organization_id))}

        @app.delete("/v2/memberships/{subject_id}")
        def revoke_membership(
            subject_id: str,
            body: MembershipRevokeRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "system"}):
                raise HTTPException(status_code=403, detail="membership revocation requires owner authority")
            try:
                result = membership_store.revoke_member(
                    tenant_id=principal.organization_id,
                    subject_id=subject_id,
                    actor_id=principal.subject_id,
                    reason=body.reason,
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if result is None:
                raise HTTPException(status_code=404, detail="membership does not exist")
            return result

    if tenant_model_store is not None:
        @app.get("/v2/settings/model")
        def get_tenant_model_setting(
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            configured = tenant_model_store.get_model_setting(principal.organization_id)
            return {
                "configured": configured is not None,
                "setting": configured,
            }

        @app.put("/v2/settings/model")
        def set_tenant_model_setting(
            body: TenantModelSettingRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "system"}):
                raise HTTPException(status_code=403, detail="model settings require owner authority")
            try:
                return tenant_model_store.set_model_setting(
                    tenant_id=principal.organization_id,
                    provider=body.provider,
                    model_name=body.model_name,
                    credential_ref=body.credential_ref,
                    actor_id=principal.subject_id,
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    if billing_service is not None:
        @app.post("/v2/billing/webhooks/stripe", include_in_schema=False)
        async def stripe_billing_webhook(
            request: Request,
            stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
        ) -> Mapping[str, Any]:
            if not stripe_signature:
                raise HTTPException(status_code=400, detail="Stripe-Signature is required")
            payload_buffer = bytearray()
            async for chunk in request.stream():
                payload_buffer.extend(chunk)
                if len(payload_buffer) > _MAX_STRIPE_WEBHOOK_BYTES:
                    raise HTTPException(status_code=413, detail="Stripe webhook body is too large")
            try:
                result = await run_in_threadpool(
                    billing_service.webhook, bytes(payload_buffer), stripe_signature,
                )
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"received": True, **result}

        @app.get("/v2/billing")
        def get_billing_account(
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            return billing_service.account(principal.organization_id)

        @app.post("/v2/billing/checkout", status_code=201)
        def create_billing_checkout(
            body: BillingCheckoutRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, str]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="billing changes require owner authority")
            try:
                return billing_service.checkout(
                    principal.organization_id, body.plan_id, idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except ConnectionError as exc:
                raise HTTPException(status_code=502, detail="payment provider unavailable") from exc

        @app.post("/v2/billing/portal", status_code=201)
        def create_billing_portal(
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, str]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="billing changes require owner authority")
            try:
                return billing_service.portal(principal.organization_id, idempotency_key)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ConnectionError as exc:
                raise HTTPException(status_code=502, detail="payment provider unavailable") from exc

    if company_directory is not None:
        @app.get("/v2/company/organization")
        def get_company_organization(
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            return _organization_view(
                company_directory.get_organization(principal.organization_id)
            )

        @app.get("/v2/company/activity")
        def get_company_activity(
            principal: Annotated[Principal, Depends(current_principal)],
            after_version: Annotated[int, Query(ge=0)] = 0,
            limit: Annotated[int, Query(ge=1, le=500)] = 200,
        ) -> Mapping[str, Any]:
            events = company_directory.list_company_events(
                principal.organization_id,
                after_version=after_version,
                limit=limit,
            )
            next_version = after_version if not events else int(events[-1]["stream_version"])
            return {"items": list(events), "next_version": next_version}

        @app.get("/v2/company/external-onboarding")
        def get_external_onboarding(
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(
                    status_code=403,
                    detail="external onboarding inventory requires owner authority",
                )
            return {
                "items": list(company_directory.list_external_onboarding(
                    principal.organization_id,
                )),
            }

        @app.post("/v2/company/external-onboarding/{onboarding_id}/confirm")
        def confirm_external_onboarding(
            onboarding_id: str,
            body: ExternalOnboardingConfirmationRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(
                    status_code=403,
                    detail="external onboarding confirmation requires owner authority",
                )
            try:
                return company_directory.confirm_external_onboarding(
                    tenant_id=principal.organization_id,
                    onboarding_id=onboarding_id,
                    display_name=body.display_name,
                    identity_subject=body.identity_subject,
                    response_sla_seconds=body.response_sla_seconds,
                    quality_criteria=tuple(body.quality_criteria),
                    attestations=tuple(body.attestations),
                    actor_id=principal.subject_id,
                    idempotency_key=idempotency_key,
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail="external onboarding not found") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.post("/v2/company/agents", status_code=201)
        def hire_company_agent(
            body: AgentHireRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="standing agent creation requires owner authority")
            try:
                event = company_directory.hire_agent(
                    tenant_id=principal.organization_id,
                    role=body.role,
                    team_id=body.team_id,
                    manager_id=body.manager_id,
                    capabilities=tuple(body.capabilities),
                    tool_grants=tuple(body.tool_grants),
                    hiring_authority=body.hiring_authority,
                    spending_limit_cents=body.spending_limit_cents,
                    actor_id=principal.subject_id,
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

            return event

        @app.post("/v2/company/agents/{agent_id}/retire")
        def retire_company_agent(
            agent_id: str,
            body: AgentRetireRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="standing agent retirement requires owner authority")
            try:
                return company_directory.retire_agent(
                    tenant_id=principal.organization_id,
                    agent_id=agent_id,
                    reason=body.reason,
                    actor_id=principal.subject_id,
                    idempotency_key=idempotency_key,
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail="standing agent not found") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    if connector_registry is not None:
        @app.get("/v2/connectors")
        def list_connectors(
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            return {"items": list(connector_registry.list_connectors(
                principal.organization_id,
            ))}

        @app.post("/v2/connectors", status_code=201)
        def register_connector(
            body: ConnectorRegistrationRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="connector creation requires owner authority")
            try:
                return connector_registry.register_connector(
                    tenant_id=principal.organization_id,
                    definition=body.model_dump(),
                    actor_id=principal.subject_id,
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.delete("/v2/connectors/{connector_id}")
        def disable_connector(
            connector_id: str,
            body: ConnectorDisableRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="connector removal requires owner authority")
            try:
                result = connector_registry.disable_connector(
                    tenant_id=principal.organization_id,
                    connector_id=connector_id,
                    actor_id=principal.subject_id,
                    reason=body.reason,
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if result is None:
                raise HTTPException(status_code=404, detail="connector not found")
            return result

    if usage_meter is not None:
        @app.get("/v2/usage/summary")
        def get_usage_summary(
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            return usage_meter.usage_summary(principal.organization_id)

        @app.get("/v2/usage/events")
        def get_usage_events(
            principal: Annotated[Principal, Depends(current_principal)],
            limit: Annotated[int, Query(ge=1, le=500)] = 100,
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="usage event detail requires owner authority")
            return {"items": list(usage_meter.list_usage_events(
                principal.organization_id, limit=limit,
            ))}

    if preview_deployments is not None:
        if artifact_store is None:
            raise ValueError("public previews require an artifact store")

        @app.get("/v2/deployments/previews")
        def list_previews(
            principal: Annotated[Principal, Depends(current_principal)],
            limit: Annotated[int, Query(ge=1, le=500)] = 100,
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(
                    status_code=403, detail="preview inventory requires owner/operator authority",
                )
            return {
                "items": list(preview_deployments.list_previews(
                    principal.organization_id, limit=limit,
                ))
            }

        @app.delete("/v2/deployments/previews/{deployment_id}")
        def revoke_preview(
            deployment_id: str,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(
                    status_code=403, detail="preview revocation requires owner/operator authority",
                )
            try:
                record = preview_deployments.revoke(
                    organization_id=principal.organization_id,
                    deployment_id=deployment_id,
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if record is None:
                raise HTTPException(status_code=404, detail="preview not found")
            return record

        @app.get("/v2/public/previews/{tenant_slug}/{public_id}")
        def public_preview(tenant_slug: str, public_id: str) -> Response:
            record = preview_deployments.resolve_public(tenant_slug, public_id)
            if record is None:
                raise HTTPException(status_code=404, detail="preview not found")
            tenant_id = str(record.get("tenant_id") or "")
            artifact_id = str(record.get("artifact_id") or "")
            content = artifact_store.get(tenant_id, artifact_id)
            if not tenant_id or not artifact_id or content is None:
                raise HTTPException(status_code=404, detail="preview not found")
            return Response(
                content=content,
                media_type="text/html; charset=utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": "inline",
                    "Content-Security-Policy": (
                        "sandbox allow-scripts; default-src 'none'; "
                        "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                        "img-src data: blob:; font-src data:; connect-src 'none'; "
                        "form-action 'none'; base-uri 'none'"
                    ),
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                },
            )

    if mission_control is not None:
        @app.post("/v2/missions", status_code=201)
        def create_mission_contract(
            body: MissionCreateRequest,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            if not principal.roles & {"owner", "operator", "system"}:
                raise HTTPException(status_code=403, detail="mission creation requires owner authority")
            try:
                return mission_control.create_mission(MissionSpec(
                    mission_id=body.mission_id,
                    tenant_id=principal.organization_id,
                    objective=body.objective,
                    principal_id=principal.subject_id,
                    accountable_owner_id=body.accountable_owner_id or principal.subject_id,
                    budget_limit_cents=body.budget_limit_cents,
                    success_measures=tuple(body.success_measures),
                    constraints=tuple(body.constraints),
                    prohibited_effects=tuple(body.prohibited_effects),
                    risk_tier=body.risk_tier,
                    human_involvement_mode=body.human_involvement_mode,
                    daily_interrupt_limit=body.daily_interrupt_limit,
                ))
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.get("/v2/missions/{mission_id}/control")
        def get_mission_contract(
            mission_id: str,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            view = mission_control.control_view(principal.organization_id, mission_id)
            if view is None:
                raise HTTPException(status_code=404, detail="mission not found")
            return view

        @app.put("/v2/missions/{mission_id}")
        def revise_mission_contract(
            mission_id: str,
            body: MissionRevisionRequest,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            if not principal.roles & {"owner", "operator", "system"}:
                raise HTTPException(status_code=403, detail="mission revision requires owner authority")
            current = mission_control.get_mission(principal.organization_id, mission_id)
            if current is None:
                raise HTTPException(status_code=404, detail="mission not found")
            retrying_committed_revision = current.revision == body.expected_revision + 1
            revised = MissionSpec(
                mission_id=mission_id,
                tenant_id=principal.organization_id,
                objective=body.objective,
                principal_id=current.principal_id,
                accountable_owner_id=body.accountable_owner_id,
                budget_limit_cents=body.budget_limit_cents,
                success_measures=tuple(body.success_measures),
                constraints=tuple(body.constraints),
                prohibited_effects=tuple(body.prohibited_effects),
                risk_tier=body.risk_tier,
                human_involvement_mode=body.human_involvement_mode,
                daily_interrupt_limit=body.daily_interrupt_limit,
                revision=body.expected_revision + 1,
                created_at=current.created_at,
                revised_at=(
                    current.revised_at
                    if retrying_committed_revision
                    else datetime.now(timezone.utc).isoformat()
                ),
            )
            try:
                result = mission_control.revise_mission(
                    revised,
                    expected_revision=body.expected_revision,
                    revised_by=principal.subject_id,
                    reason=body.reason,
                )
                # Grant creation is deterministic and idempotent. Repeating it
                # closes the crash window where the revision committed but the
                # HTTP process stopped before materializing runtime authority.
                mission_control.grant_authority(
                    build_runtime_authority(MissionSpec.from_dict(result))
                )
                return result
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.post("/v2/missions/{mission_id}/evidence", status_code=201)
        def add_mission_evidence(
            mission_id: str,
            body: MissionEvidenceRequest,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            try:
                created = mission_control.add_evidence(
                    principal.organization_id,
                    EvidenceRef(
                        evidence_id=body.evidence_id, mission_id=mission_id, kind=body.kind,
                        artifact_ref=body.artifact_ref, sha256=body.sha256,
                        produced_by=principal.subject_id, observed_at=body.observed_at,
                        recorded_at=body.recorded_at, media_type=body.media_type,
                        contains_personal_data=body.contains_personal_data,
                        retention_until=body.retention_until, source_uri=body.source_uri,
                    ),
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"created": created, "evidence_id": body.evidence_id}

        @app.post("/v2/missions/{mission_id}/claims", status_code=201)
        def add_mission_claim(
            mission_id: str,
            body: MissionClaimRequest,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            try:
                created = mission_control.add_claim(
                    principal.organization_id,
                    Claim(
                        claim_id=body.claim_id, mission_id=mission_id,
                        statement=body.statement, status=body.status,
                        asserted_by=principal.subject_id, valid_from=body.valid_from,
                        recorded_at=body.recorded_at,
                        evidence_ids=tuple(body.evidence_ids),
                        depends_on_claim_ids=tuple(body.depends_on_claim_ids),
                        valid_to=body.valid_to,
                        supersedes_claim_id=body.supersedes_claim_id,
                    ),
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"created": created, "claim_id": body.claim_id}

        @app.delete("/v2/missions/{mission_id}/evidence/{evidence_id}")
        def erase_mission_evidence_reference(
            mission_id: str,
            evidence_id: str,
            body: EvidenceErasureRequest,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            if not principal.roles & {"owner", "operator", "system"}:
                raise HTTPException(status_code=403, detail="evidence erasure requires owner authority")
            try:
                return mission_control.tombstone_evidence(
                    tenant_id=principal.organization_id, mission_id=mission_id,
                    evidence_id=evidence_id, erased_by=principal.subject_id,
                    reason=body.reason,
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.post("/v2/missions/{mission_id}/hazards", status_code=201)
        def add_mission_hazard(
            mission_id: str,
            body: MissionHazardRequest,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            if not principal.roles & {"owner", "operator", "system", "agent"}:
                raise HTTPException(status_code=403, detail="hazard registration requires mission authority")
            try:
                created = mission_control.add_hazard(
                    principal.organization_id,
                    Hazard(
                        hazard_id=body.hazard_id, mission_id=mission_id,
                        description=body.description, unacceptable_loss=body.unacceptable_loss,
                        severity=body.severity,
                        safety_constraints=tuple(body.safety_constraints),
                        unsafe_control_actions=tuple(body.unsafe_control_actions),
                        fallback_mode=body.fallback_mode,
                        requires_human_release=body.requires_human_release,
                    ),
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"created": created, "hazard_id": body.hazard_id}

        @app.post("/v2/missions/{mission_id}/authorities", status_code=201)
        def grant_mission_authority(
            mission_id: str,
            body: AuthorityGrantRequest,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            if not principal.roles & {"owner", "operator", "system"}:
                raise HTTPException(status_code=403, detail="authority delegation requires owner authority")
            mission = mission_control.get_mission(principal.organization_id, mission_id)
            if mission is None:
                raise HTTPException(status_code=404, detail="mission not found")
            try:
                created = mission_control.grant_authority(AuthorityGrant(
                    grant_id=body.grant_id, tenant_id=principal.organization_id,
                    mission_id=mission_id, principal_id=mission.principal_id,
                    delegate_id=body.delegate_id,
                    allowed_effects=tuple(body.allowed_effects),
                    allowed_resources=tuple(body.allowed_resources),
                    budget_limit_cents=body.budget_limit_cents,
                    valid_from=body.valid_from, expires_at=body.expires_at,
                    delegation_chain=tuple(body.delegation_chain),
                    mission_revision=mission.revision,
                    parent_grant_id=body.parent_grant_id,
                    human_approved=body.human_approved,
                    approval_binding_sha256=body.approval_binding_sha256,
                    approval_evidence_ids=tuple(body.approval_evidence_ids),
                    policy_version=body.policy_version,
                ))
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"created": created, "grant_id": body.grant_id}

        @app.post("/v2/missions/{mission_id}/effects", status_code=202)
        def propose_mission_effect(
            mission_id: str,
            body: MissionEffectRequest,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            if body.actor_id != principal.subject_id and not principal.roles & {
                "owner", "operator", "system",
            }:
                raise HTTPException(status_code=403, detail="agents may propose only their own effects")
            try:
                return mission_control.admit_effect(EffectRequest(
                    effect_id=body.effect_id, tenant_id=principal.organization_id,
                    mission_id=mission_id, actor_id=body.actor_id,
                    authority_grant_id=body.authority_grant_id, action=body.action,
                    resource=body.resource, risk=body.risk,
                    estimated_cost_cents=body.estimated_cost_cents,
                    reversible=body.reversible, idempotency_key=body.idempotency_key,
                    requested_at=datetime.now(timezone.utc).isoformat(),
                    input_sha256=body.input_sha256,
                    hazard_ids=tuple(body.hazard_ids), purpose=body.purpose,
                    requires_human_approval=body.requires_human_approval,
                ))
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.delete("/v2/missions/{mission_id}/authorities/{grant_id}")
        def revoke_mission_authority(
            mission_id: str,
            grant_id: str,
            body: AuthorityRevocationRequest,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            if not principal.roles & {"owner", "operator", "system"}:
                raise HTTPException(status_code=403, detail="authority revocation requires owner authority")
            try:
                return mission_control.revoke_authority(
                    tenant_id=principal.organization_id, mission_id=mission_id,
                    grant_id=grant_id, revoked_by=principal.subject_id, reason=body.reason,
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.post("/v2/missions/{mission_id}/effects/{effect_id}/settle")
        def settle_mission_effect(
            mission_id: str,
            effect_id: str,
            body: MissionEffectSettlementRequest,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            if not principal.roles & {"owner", "operator", "system"}:
                raise HTTPException(status_code=403, detail="effect settlement requires operator authority")
            try:
                return mission_control.settle_effect(
                    tenant_id=principal.organization_id, mission_id=mission_id,
                    effect_id=effect_id, actual_cost_cents=body.actual_cost_cents,
                    succeeded=body.succeeded,
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v2/runs", response_model=MutationResponse, status_code=202)
    def create_run(
        body: DirectiveRequest,
        principal: Annotated[Principal, Depends(current_principal)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    ) -> MutationResponse:
        if not (principal.roles & {"owner", "operator", "system"}):
            raise HTTPException(status_code=403, detail="mission creation requires owner/operator authority")
        run_id = _run_id(principal.organization_id, idempotency_key)
        if mission_control is not None:
            try:
                mission_spec = MissionSpec(
                    mission_id=run_id,
                    tenant_id=principal.organization_id,
                    objective=body.prompt,
                    principal_id=principal.subject_id,
                    accountable_owner_id=principal.subject_id,
                    budget_limit_cents=body.budget_limit_cents,
                    success_measures=(
                        "all admitted mission-program verification claims pass with retained evidence",
                    ),
                    constraints=(
                        "all material external effects pass deterministic assurance admission",
                    ),
                    prohibited_effects=("credential.export", "secret.export"),
                    human_involvement_mode=body.human_involvement_mode,
                    daily_interrupt_limit=body.daily_interrupt_limit,
                )
                mission_control.create_mission(mission_spec)
                mission_control.grant_authority(build_runtime_authority(mission_spec))
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        initial = LifecycleState(run_id=run_id, organization_id=principal.organization_id)
        event = Event(
            event_id=f"directive-{hashlib.sha256(idempotency_key.encode()).hexdigest()}",
            kind=EventKind.SCOPE_ACCEPTED,
            expected_version=0,
            payload={
                "prompt": body.prompt,
                "title": body.title,
                "requested_by": principal.subject_id,
                "budget_limit_cents": body.budget_limit_cents,
                "human_involvement_mode": body.human_involvement_mode,
                "daily_interrupt_limit": body.daily_interrupt_limit,
            },
        )
        try:
            receipt = engine.start_run(initial, event)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _response(receipt, run_id)

    @app.get("/v2/runs")
    def list_runs(
        principal: Annotated[Principal, Depends(current_principal)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> Mapping[str, Any]:
        return {
            "items": [{
                "run_id": state_value.run_id,
                "title": state_value.title,
                "objective_preview": None if state_value.objective is None else (
                    state_value.objective[:280]
                    + ("…" if len(state_value.objective) > 280 else "")
                ),
                "phase": state_value.phase.value,
                "status": state_value.status.value,
                "version": state_value.version,
                "verification_cycle": state_value.verification_cycle,
                "artifact_revision": state_value.artifact_revision,
            } for state_value in engine.list_runs(principal.organization_id, limit=limit)]
        }

    @app.get("/v2/runs/{run_id}")
    def get_run(
        run_id: str,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Mapping[str, Any]:
        state_value = engine.get_run(principal.organization_id, run_id)
        if state_value is None:
            # A different tenant's run is intentionally indistinguishable from
            # a nonexistent run.
            raise HTTPException(status_code=404, detail="run not found")
        return state_value.to_dict()

    @app.get("/v2/runs/{run_id}/activity")
    def get_run_activity(
        run_id: str,
        principal: Annotated[Principal, Depends(current_principal)],
        after_version: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ) -> Mapping[str, Any]:
        # Check the tenant-bound lifecycle first so another organization's run
        # is indistinguishable from a nonexistent run.
        if engine.get_run(principal.organization_id, run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        if not isinstance(engine, OrganizationLedger):
            return {"items": [], "next_version": after_version}
        items = engine.load_organization_events(
            principal.organization_id,
            run_id,
            after_version=after_version,
            limit=limit,
        )
        next_version = after_version if not items else int(items[-1]["stream_version"])
        return {"items": list(items), "next_version": next_version}

    @app.post("/v2/runs/{run_id}/events", response_model=MutationResponse, status_code=202)
    def submit_event(
        run_id: str,
        body: EventRequest,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> MutationResponse:
        if body.kind in _HUMAN_EVENTS and not (
            principal.roles & {"owner", "operator", "system"}
        ):
            raise HTTPException(status_code=403, detail="human lifecycle events require owner authority")
        if body.kind not in _HUMAN_EVENTS and not (principal.roles & _INTERNAL_ROLES):
            raise HTTPException(status_code=403, detail="this event is restricted to the internal agent runtime")
        try:
            receipt = engine.submit_event(
                principal.organization_id,
                run_id,
                Event(body.event_id, body.kind, body.expected_version, body.payload),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        return _response(receipt, run_id)

    @app.post("/v2/runs/{run_id}/cancel", response_model=MutationResponse, status_code=202)
    def cancel_run(
        run_id: str,
        body: CancellationRequest,
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> MutationResponse:
        if not (principal.roles & {"owner", "operator", "system"}):
            raise HTTPException(status_code=403, detail="mission cancellation requires owner authority")
        try:
            receipt = engine.cancel_run(
                principal.organization_id,
                run_id,
                reason=body.reason,
                expected_version=body.expected_version,
                event_id=body.event_id,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        return _response(receipt, run_id)

    if notification_store is not None:
        def notification_visible_to(
            raw: Mapping[str, Any], principal: Principal,
        ) -> bool:
            recipients = {str(item) for item in raw.get("recipient_ids", ())}
            if not recipients or "system" in principal.roles:
                return True
            admitted = {principal.subject_id}
            if "owner" in principal.roles:
                admitted.update({"human:ceo", "role:owner", "role:executive"})
            if "operator" in principal.roles:
                admitted.update({"human:operator", "operator:on-call", "role:operator"})
            if "agent" in principal.roles:
                admitted.add(f"agent:{principal.subject_id}")
            return bool(recipients & admitted)

        experience_lister = getattr(
            notification_store, "list_experience_events", None,
        )
        if experience_lister is not None:
            def experience_audience_ids(
                principal: Principal,
            ) -> tuple[str, ...] | None:
                if principal.roles & {"owner", "operator", "system"}:
                    return None
                admitted = {
                    principal.subject_id,
                    "tenant:members",
                    *(f"role:{role}" for role in principal.roles),
                }
                if "agent" in principal.roles:
                    admitted.add(f"agent:{principal.subject_id}")
                return tuple(sorted(admitted))

            @app.get("/v2/events")
            def list_experience_events(
                principal: Annotated[Principal, Depends(current_principal)],
                limit: Annotated[int, Query(ge=1, le=500)] = 100,
                cursor: Annotated[str | None, Query(max_length=1_024)] = None,
            ) -> Mapping[str, Any]:
                try:
                    after_sequence = (
                        0 if cursor is None else _decode_experience_cursor(
                            cursor, principal.organization_id,
                        )
                    )
                    page = experience_lister(
                        principal.organization_id,
                        after_sequence=after_sequence,
                        audience_ids=experience_audience_ids(principal),
                        limit=limit,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                return {
                    "items": list(page.events),
                    "cursor": _encode_experience_cursor(
                        principal.organization_id, page.cursor_sequence,
                    ),
                    "minimum_cursor": _encode_experience_cursor(
                        principal.organization_id,
                        max(0, page.minimum_sequence - 1),
                    ),
                    "latest_cursor": _encode_experience_cursor(
                        principal.organization_id, page.latest_sequence,
                    ),
                    "has_more": page.has_more,
                    "reset_required": page.reset_required,
                }

            @app.get("/v2/events/stream")
            async def stream_experience_events(
                request: Request,
                principal: Annotated[Principal, Depends(current_principal)],
                cursor: Annotated[str | None, Query(max_length=1_024)] = None,
                last_event_id: Annotated[
                    str | None, Header(alias="Last-Event-ID", max_length=1_024)
                ] = None,
            ) -> StreamingResponse:
                if cursor is not None and last_event_id is not None and cursor != last_event_id:
                    raise HTTPException(
                        status_code=400,
                        detail="cursor and Last-Event-ID must identify the same position",
                    )
                selected_cursor = cursor or last_event_id
                try:
                    after_sequence = (
                        0 if selected_cursor is None else _decode_experience_cursor(
                            selected_cursor, principal.organization_id,
                        )
                    )
                    initial_page = await run_in_threadpool(
                        lambda: experience_lister(
                            principal.organization_id,
                            after_sequence=after_sequence,
                            audience_ids=experience_audience_ids(principal),
                            limit=100,
                        )
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

                async def event_stream() -> AsyncIterator[str]:
                    current_sequence = after_sequence
                    page = initial_page
                    deadline = time.monotonic() + experience_stream_seconds
                    last_heartbeat = time.monotonic()
                    yield "retry: 3000\n\n"
                    while True:
                        if page.reset_required:
                            reset_cursor = _encode_experience_cursor(
                                principal.organization_id, page.cursor_sequence,
                            )
                            yield (
                                f"id: {reset_cursor}\n"
                                "event: reset\n"
                                f"data: {json.dumps({'reset_required': True, 'cursor': reset_cursor}, separators=(',', ':'))}\n\n"
                            )
                            return
                        for item in page.events:
                            item_sequence = int(item["tenant_sequence"])
                            item_cursor = _encode_experience_cursor(
                                principal.organization_id, item_sequence,
                            )
                            payload = json.dumps(
                                {"cursor": item_cursor, "event": item},
                                allow_nan=False, ensure_ascii=False,
                                separators=(",", ":"), sort_keys=True,
                            )
                            yield f"id: {item_cursor}\nevent: experience\ndata: {payload}\n\n"
                        previous_sequence = current_sequence
                        emitted_sequence = (
                            int(page.events[-1]["tenant_sequence"])
                            if page.events else previous_sequence
                        )
                        current_sequence = page.cursor_sequence
                        if (
                            not page.has_more
                            and current_sequence > emitted_sequence
                        ):
                            cursor_value = _encode_experience_cursor(
                                principal.organization_id, current_sequence,
                            )
                            yield (
                                f"id: {cursor_value}\n"
                                "event: cursor\n"
                                f"data: {json.dumps({'cursor': cursor_value}, separators=(',', ':'))}\n\n"
                            )
                        if await request.is_disconnected():
                            return
                        if time.monotonic() >= deadline:
                            return
                        if page.has_more:
                            await asyncio.sleep(0)
                        else:
                            await asyncio.sleep(min(1.0, max(0.01, deadline - time.monotonic())))
                            if time.monotonic() - last_heartbeat >= 15:
                                yield ": keep-alive\n\n"
                                last_heartbeat = time.monotonic()
                        try:
                            page = await run_in_threadpool(
                                lambda: experience_lister(
                                    principal.organization_id,
                                    after_sequence=current_sequence,
                                    audience_ids=experience_audience_ids(principal),
                                    limit=100,
                                )
                            )
                        except ValueError:
                            # A retention sweep can move the floor while the
                            # stream is open. Force the browser through its
                            # authenticated reconnect/snapshot path.
                            return

                return StreamingResponse(
                    event_stream(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-store, no-transform",
                        "X-Accel-Buffering": "no",
                        "X-Content-Type-Options": "nosniff",
                    },
                )

        @app.get("/v2/notification-preferences")
        def get_notification_preferences(
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            reader = getattr(notification_store, "get_notification_preferences", None)
            if reader is None:
                raise HTTPException(status_code=503, detail="notification preferences are unavailable")
            return reader(principal.organization_id, subject_id=principal.subject_id)

        @app.put("/v2/notification-preferences")
        def set_notification_preferences(
            body: NotificationPreferencesRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            writer = getattr(notification_store, "set_notification_preferences", None)
            if writer is None:
                raise HTTPException(status_code=503, detail="notification preferences are unavailable")
            try:
                preferences = NotificationPreferences(
                    tenant_id=principal.organization_id,
                    subject_id=principal.subject_id,
                    mode=body.mode,
                    browser_notifications=body.browser_notifications,
                    quiet_hours_start=body.quiet_hours_start,
                    quiet_hours_end=body.quiet_hours_end,
                    timezone_name=body.timezone,
                    digest_interval_minutes=body.digest_interval_minutes,
                )
                return writer(
                    preferences, actor_id=principal.subject_id,
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.put("/v2/notifications/{notification_id}/state")
        def set_notification_state(
            notification_id: str,
            body: NotificationStateRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            reader = getattr(notification_store, "get_notification", None)
            writer = getattr(notification_store, "set_notification_state", None)
            if reader is None or writer is None:
                raise HTTPException(status_code=503, detail="notification state is unavailable")
            raw = reader(principal.organization_id, notification_id)
            if raw is None or not notification_visible_to(raw, principal):
                raise HTTPException(status_code=404, detail="notification not found")
            try:
                return writer(
                    tenant_id=principal.organization_id,
                    subject_id=principal.subject_id,
                    notification_id=notification_id,
                    status=body.status,
                    snoozed_until=body.snoozed_until,
                    actor_id=principal.subject_id,
                    idempotency_key=idempotency_key,
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        if graph_engine is not None:
            @app.post("/v2/decisions/{notification_id}/responses", status_code=202)
            def submit_decision_response(
                notification_id: str,
                body: DecisionResponseRequest,
                principal: Annotated[Principal, Depends(current_principal)],
                idempotency_key: Annotated[
                    str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
                ],
            ) -> Mapping[str, Any]:
                reader = getattr(notification_store, "get_notification", None)
                existing_reader = getattr(notification_store, "get_decision_response", None)
                writer = getattr(notification_store, "admit_decision_response", None)
                if reader is None or existing_reader is None or writer is None:
                    raise HTTPException(status_code=503, detail="structured decisions are unavailable")
                raw = reader(principal.organization_id, notification_id)
                if (
                    raw is None
                    or not notification_visible_to(raw, principal)
                    or raw.get("category") != "human_action_required"
                ):
                    raise HTTPException(status_code=404, detail="decision not found")
                run_id = str(raw.get("run_id") or "")
                correlation_id = str(raw.get("correlation_id") or "")
                existing = existing_reader(
                    principal.organization_id, notification_id=notification_id,
                )
                if existing is None:
                    state_value = graph_engine.get_graph_run(
                        principal.organization_id, run_id,
                    )
                    waiting = [] if state_value is None else [
                        token for token in state_value.tokens
                        if token.status is TokenStatus.WAITING
                        and token.wait_correlation_id == correlation_id
                    ]
                    if len(waiting) != 1:
                        raise HTTPException(
                            status_code=409, detail="decision is no longer actionable",
                        )
                    expected_version = state_value.version
                else:
                    expected_version = int(existing["expected_version"])
                try:
                    admitted = writer(
                        tenant_id=principal.organization_id,
                        notification_id=notification_id,
                        run_id=run_id,
                        correlation_id=correlation_id,
                        response=body.response,
                        expected_version=expected_version,
                        actor_id=principal.subject_id,
                        idempotency_key=idempotency_key,
                    )
                except LookupError as exc:
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                if admitted["status"] == "failed":
                    raise HTTPException(
                        status_code=409,
                        detail="the recorded response requires operator recovery",
                    )
                if admitted["status"] == "superseded":
                    raise HTTPException(
                        status_code=409,
                        detail="the recorded decision was superseded by another resolution",
                    )
                return {
                    "response_id": admitted["response_id"],
                    "notification_id": admitted["notification_id"],
                    "status": admitted["status"],
                    "actor_id": admitted["actor_id"],
                    "accepted": admitted["status"] in {"pending", "executing", "applied"},
                    "duplicate": admitted["duplicate"],
                }

            @app.post("/v2/decisions/{notification_id}/redrive", status_code=202)
            def redrive_decision_response(
                notification_id: str,
                principal: Annotated[Principal, Depends(current_principal)],
                idempotency_key: Annotated[
                    str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
                ],
            ) -> Mapping[str, Any]:
                if not principal.roles & {"owner", "operator", "system"}:
                    raise HTTPException(
                        status_code=403,
                        detail="decision recovery requires owner or operator authority",
                    )
                redriver = getattr(notification_store, "redrive_decision_response", None)
                if redriver is None:
                    raise HTTPException(status_code=503, detail="decision recovery is unavailable")
                try:
                    result = redriver(
                        tenant_id=principal.organization_id,
                        notification_id=notification_id,
                        actor_id=principal.subject_id,
                        idempotency_key=idempotency_key,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                if result is None:
                    raise HTTPException(status_code=404, detail="decision response not found")
                return {
                    "response_id": result["response_id"],
                    "notification_id": result["notification_id"],
                    "status": result["status"],
                    "attempts": result["attempts"],
                    "total_attempts": result["total_attempts"],
                    "redrive_count": result["redrive_count"],
                    "redriven_by": result["redriven_by"],
                    "duplicate": result["duplicate"],
                }

        @app.get("/v2/notifications")
        def list_notifications(
            principal: Annotated[Principal, Depends(current_principal)],
            run_id: Annotated[str | None, Query(max_length=256)] = None,
            limit: Annotated[int, Query(ge=1, le=500)] = 100,
            cursor: Annotated[str | None, Query(max_length=1_024)] = None,
        ) -> Mapping[str, Any]:
            privileged = bool(principal.roles & {"owner", "operator", "system"})
            try:
                before = None if cursor is None else _decode_notification_cursor(cursor)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            list_args = {
                "run_id": run_id,
                "recipient_id": None if privileged else principal.subject_id,
                "limit": limit + 1,
            }
            if before is not None:
                list_args["before"] = before
            raw_items = notification_store.list_notifications(
                principal.organization_id, **list_args,
            )
            has_more = len(raw_items) > limit
            page_items = raw_items[:limit]
            next_cursor = None
            if has_more and page_items:
                try:
                    next_cursor = _encode_notification_cursor(page_items[-1])
                except ValueError as exc:
                    raise HTTPException(
                        status_code=503, detail="notification pagination is unavailable",
                    ) from exc
            items = tuple(
                item for item in page_items if notification_visible_to(item, principal)
            )
            state_reader = getattr(notification_store, "list_notification_states", None)
            states = {} if state_reader is None else state_reader(
                principal.organization_id,
                subject_id=principal.subject_id,
                notification_ids=tuple(str(item.get("notification_id") or "") for item in items),
            )
            decision_reader = getattr(notification_store, "list_decision_responses", None)
            decisions = {} if decision_reader is None else decision_reader(
                principal.organization_id,
                notification_ids=tuple(str(item.get("notification_id") or "") for item in items),
            )
            preference_reader = getattr(notification_store, "get_notification_preferences", None)
            preferences = (
                NotificationPreferences(
                    principal.organization_id, principal.subject_id,
                ).to_dict()
                if preference_reader is None else preference_reader(
                    principal.organization_id, subject_id=principal.subject_id,
                )
            )
            preference_mode = str(preferences.get("mode") or "balanced")
            focused_categories = {
                "human_action_required", "operator_attention", "run_failed",
                "management_attention",
            }
            graph_states: dict[str, Any] = {}
            rendered = []
            for raw in items:
                item = dict(raw)
                if item.get("category") == "human_action_required":
                    run_key = str(item.get("run_id") or "")
                    correlation = str(item.get("correlation_id") or "")
                    if graph_engine is not None and run_key and correlation:
                        if run_key not in graph_states:
                            graph_states[run_key] = graph_engine.get_graph_run(
                                principal.organization_id, run_key,
                            )
                        graph_state = graph_states[run_key]
                        item["actionable"] = bool(
                            graph_state is not None
                            and any(
                                token.status is TokenStatus.WAITING
                                and token.wait_correlation_id == correlation
                                for token in graph_state.tokens
                            )
                        )
                    else:
                        item["actionable"] = False
                else:
                    item["actionable"] = False
                decision = decisions.get(str(item.get("notification_id") or ""))
                if decision is not None:
                    item["decision_response"] = {
                        "response_id": decision["response_id"],
                        "status": decision["status"],
                        "actor_id": decision["actor_id"],
                        "attempts": decision["attempts"],
                        "total_attempts": decision["total_attempts"],
                        "redrive_count": decision["redrive_count"],
                        "redriven_by": decision["redriven_by"],
                        "last_error": decision["last_error"],
                        "completed_at": decision["completed_at"],
                    }
                    if decision["status"] in {"pending", "executing", "applied", "superseded", "failed"}:
                        item["actionable"] = False
                item_state = dict(states.get(str(item.get("notification_id") or ""), {
                    "status": "unread", "snoozed_until": None, "version": 0,
                }))
                snoozed_until = item_state.get("snoozed_until")
                if item_state.get("status") == "snoozed" and snoozed_until:
                    try:
                        if datetime.fromisoformat(str(snoozed_until).replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                            item_state["status"] = "unread"
                    except ValueError:
                        item_state["status"] = "unread"
                category = str(item.get("category") or "")
                payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
                attention = _notification_attention(category, payload)
                promoted = (
                    attention["level"] == "time_sensitive"
                    or preference_mode == "all"
                    or category in focused_categories
                    or (preference_mode == "balanced" and attention["level"] != "passive")
                )
                disposition = "interrupt" if (
                    item["actionable"] or attention["level"] == "time_sensitive"
                ) else ("feed" if promoted else "muted")
                if item_state.get("status") in {"dismissed", "snoozed", "resolved"}:
                    disposition = "hidden"
                item["user_state"] = item_state
                item["presentation"] = {
                    **attention,
                    "disposition": disposition,
                    "preference_mode": preference_mode,
                }
                rendered.append(item)
            return {
                "items": rendered,
                "preferences": preferences,
                "next_cursor": next_cursor,
            }

        @app.get("/v2/notification-routes")
        def list_notification_routes(
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="notification routes require owner authority")
            return {"items": list(notification_store.list_notification_routes(
                principal.organization_id,
            ))}

        @app.post("/v2/notification-routes", status_code=201)
        def register_notification_route(
            body: NotificationRouteRegistrationRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="notification route creation requires owner authority")
            if connector_registry is None:
                raise HTTPException(status_code=503, detail="connector registry is unavailable")
            connector = connector_registry.get_connector(
                principal.organization_id, body.connector_id,
            )
            prefixes = () if connector is None else connector.get("allowed_path_prefixes", ())
            path_allowed = isinstance(prefixes, list) and any(
                body.path == str(prefix)
                or body.path.startswith(
                    str(prefix) if str(prefix).endswith("/") else str(prefix) + "/"
                )
                for prefix in prefixes
            )
            if (
                connector is None or not connector.get("active")
                or "POST" not in connector.get("allowed_methods", ()) or not path_allowed
            ):
                raise HTTPException(
                    status_code=409,
                    detail="route requires an active POST connector admitting the exact path",
                )
            try:
                return notification_store.register_notification_route(
                    tenant_id=principal.organization_id,
                    definition=body.model_dump(),
                    actor_id=principal.subject_id,
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.delete("/v2/notification-routes/{route_id}")
        def disable_notification_route(
            route_id: str,
            body: NotificationRouteDisableRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="notification route removal requires owner authority")
            try:
                result = notification_store.disable_notification_route(
                    tenant_id=principal.organization_id,
                    route_id=route_id,
                    actor_id=principal.subject_id,
                    reason=body.reason,
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if result is None:
                raise HTTPException(status_code=404, detail="notification route not found")
            return result

        @app.get("/v2/notification-deliveries")
        def list_notification_deliveries(
            principal: Annotated[Principal, Depends(current_principal)],
            limit: Annotated[int, Query(ge=1, le=500)] = 100,
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="notification delivery audit requires owner authority")
            return {"items": list(notification_store.list_notification_deliveries(
                principal.organization_id, limit=limit,
            ))}

        @app.post("/v2/notification-deliveries/{delivery_id}/redrive")
        def redrive_notification_delivery(
            delivery_id: str,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="notification redrive requires owner authority")
            try:
                result = notification_store.redrive_notification_delivery(
                    tenant_id=principal.organization_id,
                    delivery_id=delivery_id,
                    actor_id=principal.subject_id,
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if result is None:
                raise HTTPException(status_code=404, detail="notification delivery not found")
            return result

    if artifact_store is not None:
        @app.get("/v2/deployments")
        def list_deployments(
            principal: Annotated[Principal, Depends(current_principal)],
            limit: Annotated[int, Query(ge=1, le=500)] = 100,
        ) -> Mapping[str, Any]:
            items: list[dict[str, Any]] = []
            for record in artifact_store.list_artifacts(
                principal.organization_id,
                media_types=_DEPLOYMENT_RECEIPT_MEDIA_TYPES,
                limit=limit,
            ):
                artifact_id = str(record.get("artifact_id") or "")
                content = artifact_store.get(principal.organization_id, artifact_id)
                try:
                    receipt = json.loads(content) if content is not None else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    receipt = None
                if not isinstance(receipt, Mapping):
                    items.append({
                        "kind": "deployment_receipt",
                        "status": (
                            "receipt_expired" if content is None else "corrupt"
                        ),
                        "receipt_artifact_id": artifact_id,
                        "created_at": record.get("created_at"),
                    })
                    continue
                item = dict(receipt)
                item.update({
                    "status": (
                        "failed" if item.get("kind") == "cloud_run_service_failure"
                        else "active"
                    ),
                    "receipt_artifact_id": artifact_id,
                    "created_at": record.get("created_at"),
                })
                items.append(item)
            if preview_deployments is not None:
                items.extend({"kind": "preview", **dict(item)} for item in (
                    preview_deployments.list_previews(principal.organization_id, limit=limit)
                ))
            items.sort(
                key=lambda item: (str(item.get("created_at") or ""), str(item.get("deployment_id") or "")),
                reverse=True,
            )
            return {"items": items[:limit]}

        @app.post("/v2/artifacts", status_code=201)
        def upload_artifact(
            body: ArtifactUploadRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "agent", "system"}):
                raise HTTPException(status_code=403, detail="artifact publication requires write authority")
            try:
                content = base64.b64decode(body.content_base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise HTTPException(status_code=422, detail="content_base64 is invalid") from exc
            if len(content) > _MAX_API_ARTIFACT_BYTES:
                raise HTTPException(status_code=413, detail="artifact exceeds the API byte limit")
            try:
                artifact_id = artifact_store.put(
                    organization_id=principal.organization_id,
                    content=content,
                    media_type=body.media_type,
                    idempotency_key=idempotency_key,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            record = artifact_store.describe(principal.organization_id, artifact_id)
            if record is None:
                raise HTTPException(status_code=500, detail="artifact publication was not readable")
            return record

        @app.get("/v2/artifacts/{artifact_id}")
        def describe_artifact(
            artifact_id: str,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            record = artifact_store.describe(principal.organization_id, artifact_id)
            if record is None:
                raise HTTPException(status_code=404, detail="artifact not found")
            return record

        @app.get("/v2/artifacts/{artifact_id}/content")
        def download_artifact(
            artifact_id: str,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Response:
            record = artifact_store.describe(principal.organization_id, artifact_id)
            content = artifact_store.get(principal.organization_id, artifact_id)
            if record is None or content is None:
                raise HTTPException(status_code=404, detail="artifact not found")
            return Response(
                content=content,
                media_type=str(record["media_type"]),
                headers={
                    "ETag": f'"{record["digest"]}"',
                    "Content-Disposition": f'attachment; filename="{record["artifact_id"]}"',
                    "Content-Security-Policy": "sandbox; default-src 'none'",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    if graph_engine is not None:
        @app.get("/v2/runs/{run_id}/management")
        def get_mission_management(
            run_id: str,
            principal: Annotated[Principal, Depends(current_principal)],
            slow_after_seconds: Annotated[int, Query(ge=30, le=86_400)] = 300,
        ) -> Mapping[str, Any]:
            lifecycle = engine.get_run(principal.organization_id, run_id)
            if lifecycle is None:
                raise HTTPException(status_code=404, detail="run not found")
            planning_run_id = mission_planning_run_id(run_id)
            planning = graph_engine.get_graph_run(principal.organization_id, planning_run_id)
            if planning is None:
                raise HTTPException(status_code=404, detail="mission planning has not started")
            child_run_ids = {
                str(token.output["child_run_id"])
                for token in planning.tokens
                if token.output.get("child_run_id")
            }
            if len(child_run_ids) > 1:
                raise HTTPException(status_code=500, detail="mission has conflicting execution runs")
            execution_run_id = next(iter(child_run_ids), None)
            execution = None if execution_run_id is None else graph_engine.get_graph_run(
                principal.organization_id, execution_run_id,
            )
            selected = execution or planning
            definition = graph_engine.get_workflow_definition(
                principal.organization_id,
                selected.workflow_id,
                selected.workflow_version,
            )
            if definition is None:
                raise HTTPException(status_code=500, detail="mission workflow definition is missing")
            observation = None
            if isinstance(graph_engine, GraphRunInspector):
                observation = graph_engine.inspect_graph_run(
                    principal.organization_id, selected.run_id,
                )
            organization_events: tuple[Mapping[str, Any], ...] = ()
            if isinstance(engine, OrganizationLedger):
                organization_events = engine.load_organization_events(
                    principal.organization_id, run_id, limit=5_000,
                )
            company_events: tuple[Mapping[str, Any], ...] = ()
            if company_directory is not None:
                company_events = company_directory.list_company_events(
                    principal.organization_id, limit=5_000,
                )
            projection = dict(project_mission_control(
                lifecycle_run_id=run_id,
                planning_state=planning,
                execution_state=execution,
                definition=definition,
                observation=observation,
                organization_events=organization_events,
                company_events=company_events,
                slow_after_seconds=slow_after_seconds,
            ))
            if observation is not None and principal.roles & {"owner", "operator", "system"}:
                action_rows = observation.get("actions", ())
                action_rows = action_rows if isinstance(action_rows, list) else []
                bounded_actions = action_rows[:200]
                timeline = []
                for row in bounded_actions:
                    if not isinstance(row, Mapping):
                        continue
                    action = row.get("action")
                    action_value = action if isinstance(action, Mapping) else {}
                    error = row.get("last_error")
                    error_value = error if isinstance(error, Mapping) else {}
                    timeline.append({
                        "action_id": row.get("action_id"),
                        "state_version": row.get("state_version"),
                        "kind": action_value.get("kind"),
                        "node_id": action_value.get("node_id"),
                        "token_id": action_value.get("token_id"),
                        "status": row.get("status"),
                        "attempts": row.get("attempts"),
                        "available_at": row.get("available_at"),
                        "created_at": row.get("created_at"),
                        "completed_at": row.get("completed_at"),
                        "error": None if not error_value else {
                            "type": error_value.get("type"),
                            "message": str(error_value.get("message") or "")[:2_000],
                            "retryable": error_value.get("retryable"),
                        },
                    })
                projection["execution_timeline"] = timeline
                projection["execution_timeline_truncated"] = len(action_rows) > len(timeline)
            subprograms, truncated = _project_mission_subprograms(
                graph_engine, principal.organization_id, execution_run_id,
            )
            projection["subprograms"] = subprograms
            projection["subprograms_truncated"] = truncated
            projection["assurance"] = (
                None if mission_control is None else mission_control.control_view(
                    principal.organization_id, run_id,
                )
            )
            return projection

        if company_directory is not None:
            @app.post("/v2/runs/{run_id}/management/proposals/{proposal_id}/hiring-decision")
            def decide_mission_hiring_proposal(
                run_id: str,
                proposal_id: str,
                body: HiringProposalDecisionRequest,
                principal: Annotated[Principal, Depends(current_principal)],
            ) -> Mapping[str, Any]:
                if not (principal.roles & {"owner", "operator", "system"}):
                    raise HTTPException(
                        status_code=403, detail="staffing proposal decisions require owner authority",
                    )
                projection = get_mission_management(run_id, principal, 300)
                proposal = next((
                    item for item in projection["hiring_requests"]
                    if item.get("proposal_id") == proposal_id
                ), None)
                if proposal is None:
                    raise HTTPException(status_code=404, detail="staffing proposal not found")
                participant_kind = str(proposal.get("participant_kind") or "agent")
                role = str(proposal.get("role") or "").strip()
                capabilities = proposal.get("capabilities", ())
                requested_count = proposal.get("requested_count", 1)
                if (
                    not role
                    or not isinstance(capabilities, list)
                    or isinstance(requested_count, bool)
                    or not isinstance(requested_count, int)
                ):
                    raise HTTPException(status_code=409, detail="staffing proposal is malformed")
                try:
                    return company_directory.decide_hiring_proposal(
                        tenant_id=principal.organization_id,
                        proposal_id=proposal_id,
                        approved=body.approved,
                        participant_kind=participant_kind,
                        reason=body.reason,
                        role=role,
                        requested_count=requested_count,
                        team_id=body.team_id,
                        manager_id=body.manager_id,
                        capabilities=tuple(str(item) for item in capabilities),
                        tool_grants=tuple(body.tool_grants),
                        spending_limit_cents=body.spending_limit_cents,
                        actor_id=principal.subject_id,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.get("/v2/runs/{run_id}/mission")
        def get_mission_execution(
            run_id: str,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            lifecycle = engine.get_run(principal.organization_id, run_id)
            if lifecycle is None:
                raise HTTPException(status_code=404, detail="run not found")
            planning_run_id = mission_planning_run_id(run_id)
            planning = graph_engine.get_graph_run(
                principal.organization_id, planning_run_id,
            )
            child_run_ids = set()
            if planning is not None:
                child_run_ids = {
                    str(token.output["child_run_id"])
                    for token in planning.tokens
                    if token.output.get("child_run_id")
                }
            if len(child_run_ids) > 1:
                raise HTTPException(status_code=500, detail="mission has conflicting execution runs")
            execution_run_id = next(iter(child_run_ids), None)
            execution = (
                None
                if execution_run_id is None
                else graph_engine.get_graph_run(principal.organization_id, execution_run_id)
            )
            deliverables = []
            if execution is not None:
                definition = graph_engine.get_workflow_definition(
                    principal.organization_id,
                    execution.workflow_id,
                    execution.workflow_version,
                )
                deployment_nodes: dict[str, str] = {}
                if definition is not None:
                    deployment_nodes = {
                        node.node_id: str(node.configuration.get("tool"))
                        for node in definition.nodes
                        if node.kind is NodeKind.TOOL
                        and node.configuration.get("tool") in {
                            "deploy.preview", "deploy.service", "deploy.static",
                        }
                    }
                for token in execution.tokens:
                    output = token.output
                    if token.node_id not in deployment_nodes or token.status is not TokenStatus.SUCCEEDED:
                        continue
                    public_url = output.get("public_url")
                    deployment_id = output.get("deployment_id")
                    receipt_artifact_id = output.get("receipt_artifact_id")
                    if not all(isinstance(item, str) and item for item in (
                        public_url, deployment_id, receipt_artifact_id,
                    )):
                        continue
                    deliverable = {
                        "kind": (
                            "static_site"
                            if deployment_nodes[token.node_id] == "deploy.static"
                            else (
                                "cloud_run_service"
                                if deployment_nodes[token.node_id] == "deploy.service"
                                else "static_preview"
                            )
                        ),
                        "node_id": token.node_id,
                        "deployment_id": deployment_id,
                        "public_url": public_url,
                        "receipt_artifact_id": receipt_artifact_id,
                    }
                    if deployment_nodes[token.node_id] == "deploy.preview":
                        deliverable["expires_at"] = output.get("expires_at")
                    deliverables.append(deliverable)
            subprograms, truncated = _project_mission_subprograms(
                graph_engine, principal.organization_id, execution_run_id,
            )
            return {
                "lifecycle": lifecycle.to_dict(),
                "planning_run_id": planning_run_id,
                "planning": None if planning is None else planning.to_dict(),
                "execution_run_id": execution_run_id,
                "execution": None if execution is None else execution.to_dict(),
                "program": None if execution is None else execution.context.get("mission_program"),
                "deliverables": deliverables,
                "subprograms": subprograms,
                "subprograms_truncated": truncated,
                "assurance": (
                    None if mission_control is None else mission_control.control_view(
                        principal.organization_id, run_id,
                    )
                ),
            }

        @app.post("/v2/workflows", status_code=201)
        def register_workflow(
            body: WorkflowDefinitionRequest,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            if not (principal.roles & {"owner", "operator", "system"}):
                raise HTTPException(status_code=403, detail="workflow design requires owner/operator authority")
            try:
                definition = WorkflowDefinition(
                    workflow_id=body.workflow_id,
                    tenant_id=principal.organization_id,
                    name=body.name,
                    version=body.version,
                    entry_node_id=body.entry_node_id,
                    nodes=tuple(WorkflowNode.from_dict(item.model_dump()) for item in body.nodes),
                    edges=tuple(WorkflowEdge.from_dict(item.model_dump()) for item in body.edges),
                    created_by=principal.subject_id,
                    supersedes_version=body.supersedes_version,
                )
                created = graph_engine.register_workflow(definition)
            except (ValueError, TypeError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {
                "workflow_id": definition.workflow_id,
                "version": definition.version,
                "created": created,
            }

        @app.post("/v2/workflows/{workflow_id}/runs", status_code=202)
        def start_graph_run_api(
            workflow_id: str,
            body: GraphRunRequest,
            principal: Annotated[Principal, Depends(current_principal)],
            idempotency_key: Annotated[
                str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
            ],
        ) -> Mapping[str, Any]:
            graph_run_id = "graph-" + hashlib.sha256(
                f"agent-os:graph-run:v1:{principal.organization_id}:{workflow_id}:{idempotency_key}".encode()
            ).hexdigest()[:32]
            try:
                receipt = graph_engine.start_graph_run(
                    principal.organization_id,
                    workflow_id,
                    body.workflow_version,
                    run_id=graph_run_id,
                    request_id="start-" + hashlib.sha256(idempotency_key.encode()).hexdigest(),
                    context=body.context,
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {
                "run_id": graph_run_id,
                "duplicate": receipt.duplicate,
                "state": receipt.state.to_dict(),
                "actions": [action.to_dict() for action in receipt.actions],
            }

        @app.get("/v2/graph-runs/{run_id}")
        def get_graph_run_api(
            run_id: str,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            state_value = graph_engine.get_graph_run(principal.organization_id, run_id)
            if state_value is None:
                raise HTTPException(status_code=404, detail="graph run not found")
            return state_value.to_dict()

        @app.post("/v2/graph-runs/{run_id}/events", status_code=202)
        def submit_graph_event_api(
            run_id: str,
            body: GraphEventRequest,
            principal: Annotated[Principal, Depends(current_principal)],
        ) -> Mapping[str, Any]:
            state_value = graph_engine.get_graph_run(principal.organization_id, run_id)
            if state_value is None:
                raise HTTPException(status_code=404, detail="graph run not found")
            owner_authority = bool(principal.roles & {"owner", "operator", "system"})
            if body.kind in {
                WorkflowEventKind.RUN_REVISED,
                WorkflowEventKind.CHILD_WAITED,
                WorkflowEventKind.CHILD_COMPLETED,
            }:
                # Revision is a compound authority: validate a complete mission
                # program, register its immutable definition, then atomically
                # advance the run.  The in-process workflow.revise handler owns
                # that sequence; accepting raw revision events here could point
                # a run at an unregistered or policy-bypassing definition.
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "workflow revisions require the internal program-revision authority"
                        if body.kind is WorkflowEventKind.RUN_REVISED
                        else "child workflow coordination requires an internal compound authority"
                    ),
                )
            if body.kind is WorkflowEventKind.NODE_RETRY_REQUESTED:
                if not owner_authority:
                    raise HTTPException(
                        status_code=403,
                        detail="failed-node recovery requires owner authority",
                    )
                token_id = body.payload.get("token_id")
                reason = body.payload.get("reason")
                if (
                    not isinstance(token_id, str)
                    or not 1 <= len(token_id) <= 256
                    or not isinstance(reason, str)
                    or not 1 <= len(reason.strip()) <= 2_000
                ):
                    raise HTTPException(status_code=422, detail="node recovery request is invalid")
            if body.kind is WorkflowEventKind.RUN_CANCELLED and not owner_authority:
                raise HTTPException(status_code=403, detail="graph cancellation requires owner authority")
            if body.kind is WorkflowEventKind.WAIT_RESUMED:
                if (
                    notification_store is not None
                    and getattr(notification_store, "admit_decision_response", None) is not None
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "human responses must use the structured decision endpoint "
                            "so intent remains recoverable"
                        ),
                    )
                correlation_id = body.payload.get("correlation_id")
                response = body.payload.get("response")
                if (
                    not isinstance(correlation_id, str)
                    or not 1 <= len(correlation_id) <= 256
                    or not isinstance(response, Mapping)
                    or not 1 <= len(response) <= 32
                    or any(not isinstance(key, str) or not 1 <= len(key) <= 128 for key in response)
                ):
                    raise HTTPException(status_code=422, detail="human response is invalid")
                try:
                    response_size = len(json.dumps(
                        response,
                        allow_nan=False,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode())
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=422, detail="human response is invalid") from exc
                if response_size > _MAX_HUMAN_RESPONSE_BYTES:
                    raise HTTPException(status_code=413, detail="human response is too large")
                waiting = [
                    token for token in state_value.tokens
                    if token.status is TokenStatus.WAITING
                    and token.wait_correlation_id == correlation_id
                ]
                if len(waiting) != 1 and not owner_authority:
                    raise HTTPException(status_code=409, detail="human request is no longer actionable")
                if waiting:
                    definition = graph_engine.get_workflow_definition(
                        principal.organization_id,
                        state_value.workflow_id,
                        state_value.workflow_version,
                    )
                    target_node = None if definition is None else next(
                        (node for node in definition.nodes if node.node_id == waiting[0].node_id),
                        None,
                    )
                    recipients = (
                        ()
                        if target_node is None
                        else target_node.configuration.get("recipient_ids", ["human:ceo"])
                    )
                    recipient_authority = (
                        isinstance(recipients, (list, tuple))
                        and principal.subject_id in recipients
                    )
                    if not owner_authority and not recipient_authority:
                        raise HTTPException(
                            status_code=403,
                            detail="human response requires owner or named-recipient authority",
                        )
            elif body.kind not in {
                WorkflowEventKind.RUN_CANCELLED,
                WorkflowEventKind.NODE_RETRY_REQUESTED,
            } and not (
                principal.roles & _INTERNAL_ROLES
            ):
                raise HTTPException(status_code=403, detail="node execution events require an agent/operator")
            try:
                receipt = graph_engine.submit_graph_event(
                    principal.organization_id,
                    run_id,
                    WorkflowEvent(body.event_id, body.kind, body.expected_version, body.payload),
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail="graph run not found") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {
                "run_id": run_id,
                "duplicate": receipt.duplicate,
                "state": receipt.state.to_dict(),
                "actions": [action.to_dict() for action in receipt.actions],
            }

    return app
