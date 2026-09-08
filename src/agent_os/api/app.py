"""FastAPI control surface for the V2 product lifecycle."""

import base64
import binascii
import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Annotated, Mapping

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from agent_os.api.auth import Authenticator, Principal
from agent_os.application.ports import (
    ArtifactStore,
    GraphWorkflowEngine,
    NotificationStore,
    OrganizationLedger,
    WorkflowEngine,
    WorkflowReceipt,
)
from agent_os.domain.lifecycle import Event, EventKind, LifecycleState, TransitionRejected
from agent_os.domain.workflow import WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import WorkflowEvent, WorkflowEventKind, WorkflowTransitionRejected


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


_HUMAN_EVENTS = {EventKind.WAIT_RESOLVED, EventKind.CANCEL_REQUESTED}
_INTERNAL_ROLES = {"agent", "operator", "system"}
_MAX_API_ARTIFACT_BYTES = 2 * 1024 * 1024


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


def create_app(
    *,
    engine: WorkflowEngine,
    identity: Authenticator,
    graph_engine: GraphWorkflowEngine | None = None,
    notification_store: NotificationStore | None = None,
    artifact_store: ArtifactStore | None = None,
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
