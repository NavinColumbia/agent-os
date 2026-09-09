"""FastAPI control surface for the V2 product lifecycle."""

import base64
import binascii
import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
import re
from typing import Any, Annotated, Mapping
from urllib.parse import urlparse

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from agent_os.api.auth import Authenticator, Principal
from agent_os.application.mission import mission_planning_run_id
from agent_os.application.mission_control import project_mission_control
from agent_os.application.ports import (
    ArtifactStore,
    CompanyDirectory,
    GraphWorkflowEngine,
    GraphRunInspector,
    NotificationStore,
    OrganizationLedger,
    PreviewDeploymentStore,
    WorkflowEngine,
    WorkflowReceipt,
)
from agent_os.domain.lifecycle import Event, EventKind, LifecycleState, TransitionRejected
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import (
    TokenStatus,
    WorkflowEvent,
    WorkflowEventKind,
    WorkflowTransitionRejected,
)


class DirectiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=50_000)
    title: str | None = Field(default=None, max_length=200)


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


_HUMAN_EVENTS = {EventKind.WAIT_RESOLVED, EventKind.CANCEL_REQUESTED}
_INTERNAL_ROLES = {"agent", "operator", "system"}
_MAX_API_ARTIFACT_BYTES = 2 * 1024 * 1024
_WEB_ROOT = Path(__file__).with_name("web")
_PUBLIC_IDENTITY_KEYS = {
    "identity_mode", "authorization_url", "token_url", "client_id", "scope", "audience",
    "authorization_audience_parameter", "redirect_uri",
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
    if mode != "oidc":
        return {"identity_mode": mode}, ""
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
    return {key: values.get(key, "") for key in _PUBLIC_IDENTITY_KEYS}, (
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
    client_identity_config: Mapping[str, str] | None = None,
    shutdown: Callable[[], None] | None = None,
) -> FastAPI:
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
            "frame-ancestors 'none'; form-action 'none'; object-src 'none'"
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

    def current_principal(
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

    @app.post("/v2/runs", response_model=MutationResponse, status_code=202)
    def create_run(
        body: DirectiveRequest,
        principal: Annotated[Principal, Depends(current_principal)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    ) -> MutationResponse:
        run_id = _run_id(principal.organization_id, idempotency_key)
        initial = LifecycleState(run_id=run_id, organization_id=principal.organization_id)
        event = Event(
            event_id=f"directive-{hashlib.sha256(idempotency_key.encode()).hexdigest()}",
            kind=EventKind.SCOPE_ACCEPTED,
            expected_version=0,
            payload={
                "prompt": body.prompt,
                "title": body.title,
                "requested_by": principal.subject_id,
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
        @app.get("/v2/notifications")
        def list_notifications(
            principal: Annotated[Principal, Depends(current_principal)],
            run_id: Annotated[str | None, Query(max_length=256)] = None,
            limit: Annotated[int, Query(ge=1, le=500)] = 100,
        ) -> Mapping[str, Any]:
            privileged = bool(principal.roles & {"owner", "operator", "system"})
            items = notification_store.list_notifications(
                principal.organization_id,
                run_id=run_id,
                recipient_id=None if privileged else principal.subject_id,
                limit=limit,
            )
            return {"items": list(items)}

    if artifact_store is not None:
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
            return project_mission_control(
                lifecycle_run_id=run_id,
                planning_state=planning,
                execution_state=execution,
                definition=definition,
                observation=observation,
                organization_events=organization_events,
                company_events=company_events,
                slow_after_seconds=slow_after_seconds,
            )

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
                if body.approved and participant_kind != "agent":
                    raise HTTPException(
                        status_code=409,
                        detail="human/vendor staffing requires its configured legal and onboarding workflow",
                    )
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
                deployment_nodes = set()
                if definition is not None:
                    deployment_nodes = {
                        node.node_id for node in definition.nodes
                        if node.kind is NodeKind.TOOL
                        and node.configuration.get("tool") == "deploy.preview"
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
                    deliverables.append({
                        "kind": "static_preview",
                        "node_id": token.node_id,
                        "deployment_id": deployment_id,
                        "public_url": public_url,
                        "receipt_artifact_id": receipt_artifact_id,
                        "expires_at": output.get("expires_at"),
                    })
            return {
                "lifecycle": lifecycle.to_dict(),
                "planning_run_id": planning_run_id,
                "planning": None if planning is None else planning.to_dict(),
                "execution_run_id": execution_run_id,
                "execution": None if execution is None else execution.to_dict(),
                "deliverables": deliverables,
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
            human_allowed = {WorkflowEventKind.WAIT_RESUMED, WorkflowEventKind.RUN_CANCELLED}
            if body.kind not in human_allowed and not (principal.roles & _INTERNAL_ROLES):
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
