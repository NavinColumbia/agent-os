"""Transactional SQL adapter for arbitrary customer workflow graphs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    insert,
    select,
    text,
    update,
)

from agent_os.application.ports import GraphWorkflowEngine, GraphWorkflowReceipt
from agent_os.domain.workflow import WorkflowDefinition
from agent_os.domain.workflow_runtime import (
    WorkflowAction,
    WorkflowEvent,
    WorkflowRunState,
    evolve_workflow,
    start_workflow,
    workflow_event_fingerprint,
)
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


graph_metadata = MetaData()

workflow_definitions = Table(
    "aos_v2_workflow_definitions",
    graph_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("workflow_id", String(256), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("definition", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

workflow_runs = Table(
    "aos_v2_workflow_runs",
    graph_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("run_id", String(256), primary_key=True),
    Column("workflow_id", String(256), nullable=False),
    Column("workflow_version", Integer, nullable=False),
    Column("state_version", Integer, nullable=False),
    Column("start_fingerprint", String(64), nullable=False),
    Column("state", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "workflow_id", "workflow_version"],
        [
            "aos_v2_workflow_definitions.tenant_id",
            "aos_v2_workflow_definitions.workflow_id",
            "aos_v2_workflow_definitions.version",
        ],
    ),
)

workflow_events = Table(
    "aos_v2_workflow_events",
    graph_metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("run_id", String(256), primary_key=True),
    Column("event_id", String(256), primary_key=True),
    Column("state_version", Integer, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("event", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "run_id"],
        ["aos_v2_workflow_runs.tenant_id", "aos_v2_workflow_runs.run_id"],
        ondelete="CASCADE",
    ),
)

workflow_actions = Table(
    "aos_v2_workflow_actions",
    graph_metadata,
    Column("action_id", String(64), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("run_id", String(256), nullable=False),
    Column("source_event_id", String(256), nullable=False),
    Column("state_version", Integer, nullable=False),
    Column("position", Integer, nullable=False),
    Column("action", JSON, nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "run_id", "source_event_id"],
        [
            "aos_v2_workflow_events.tenant_id",
            "aos_v2_workflow_events.run_id",
            "aos_v2_workflow_events.event_id",
        ],
        ondelete="CASCADE",
    ),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(raw: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            raw, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("workflow data must contain JSON-compatible primitives") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()


class SQLGraphWorkflowEngine(GraphWorkflowEngine):
    def __init__(self, database_url: str, *, create_schema: bool = False) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)
        if create_schema:
            graph_metadata.create_all(self._engine)

    @contextmanager
    def _tenant_connection(self, tenant_id: str):
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_app"))
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": tenant_id},
                )
            yield connection

    def register_workflow(self, definition: WorkflowDefinition) -> bool:
        raw = definition.to_dict()
        fingerprint = _fingerprint(raw)
        with self._tenant_connection(definition.tenant_id) as connection:
            key = and_(
                workflow_definitions.c.tenant_id == definition.tenant_id,
                workflow_definitions.c.workflow_id == definition.workflow_id,
                workflow_definitions.c.version == definition.version,
            )
            prior = connection.execute(select(
                workflow_definitions.c.fingerprint,
            ).where(key)).scalar_one_or_none()
            if prior is not None:
                if prior != fingerprint:
                    raise ValueError("workflow version already exists with different content")
                return False
            if definition.version == 1:
                if definition.supersedes_version is not None:
                    raise ValueError("workflow version one cannot supersede another version")
            else:
                if definition.supersedes_version != definition.version - 1:
                    raise ValueError("workflow versions must explicitly supersede the prior version")
                prior_version = connection.execute(select(
                    workflow_definitions.c.version,
                ).where(and_(
                    workflow_definitions.c.tenant_id == definition.tenant_id,
                    workflow_definitions.c.workflow_id == definition.workflow_id,
                    workflow_definitions.c.version == definition.supersedes_version,
                ))).scalar_one_or_none()
                if prior_version is None:
                    raise ValueError("superseded workflow version does not exist")
            connection.execute(insert(workflow_definitions).values(
                tenant_id=definition.tenant_id,
                workflow_id=definition.workflow_id,
                version=definition.version,
                fingerprint=fingerprint,
                definition=raw,
                created_at=_now(),
            ))
        return True

    def _definition(self, connection, tenant_id: str, workflow_id: str, version: int):
        raw = connection.execute(select(
            workflow_definitions.c.definition,
        ).where(and_(
            workflow_definitions.c.tenant_id == tenant_id,
            workflow_definitions.c.workflow_id == workflow_id,
            workflow_definitions.c.version == version,
        ))).scalar_one_or_none()
        if raw is None:
            raise LookupError("workflow definition/version does not exist for this tenant")
        return WorkflowDefinition.from_dict(raw)

    @staticmethod
    def _insert_actions(connection, tenant_id, run_id, event_id, state_version, actions):
        if not actions:
            return
        now = _now()
        connection.execute(insert(workflow_actions), [{
            "action_id": action.action_id,
            "tenant_id": tenant_id,
            "run_id": run_id,
            "source_event_id": event_id,
            "state_version": state_version,
            "position": position,
            "action": action.to_dict(),
            "status": "pending",
            "created_at": now,
        } for position, action in enumerate(actions)])

    def start_graph_run(
        self,
        tenant_id: str,
        workflow_id: str,
        workflow_version: int,
        *,
        run_id: str,
        request_id: str,
        context: Mapping[str, Any] | None = None,
    ) -> GraphWorkflowReceipt:
        if not run_id.strip() or not request_id.strip():
            raise ValueError("run_id and request_id are required")
        request = {
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "workflow_version": workflow_version,
            "run_id": run_id,
            "request_id": request_id,
            "context": {} if context is None else dict(context),
        }
        fingerprint = _fingerprint(request)
        with self._tenant_connection(tenant_id) as connection:
            existing = connection.execute(select(
                workflow_runs.c.start_fingerprint,
                workflow_runs.c.state,
            ).where(and_(
                workflow_runs.c.tenant_id == tenant_id,
                workflow_runs.c.run_id == run_id,
            )).with_for_update()).mappings().first()
            if existing is not None:
                if existing["start_fingerprint"] != fingerprint:
                    raise ValueError("graph run identity already exists with a different request")
                actions = self._actions_for_event(connection, tenant_id, run_id, request_id)
                return GraphWorkflowReceipt(
                    WorkflowRunState.from_dict(existing["state"]), actions, duplicate=True,
                )
            definition = self._definition(connection, tenant_id, workflow_id, workflow_version)
            mutation = start_workflow(definition, run_id=run_id, context=context)
            now = _now()
            connection.execute(insert(workflow_runs).values(
                tenant_id=tenant_id,
                run_id=run_id,
                workflow_id=workflow_id,
                workflow_version=workflow_version,
                state_version=mutation.state.version,
                start_fingerprint=fingerprint,
                state=mutation.state.to_dict(),
                created_at=now,
                updated_at=now,
            ))
            start_event = {"event_id": request_id, "kind": "run_started", "request": request}
            connection.execute(insert(workflow_events).values(
                tenant_id=tenant_id,
                run_id=run_id,
                event_id=request_id,
                state_version=0,
                fingerprint=_fingerprint(start_event),
                event=start_event,
                created_at=now,
            ))
            self._insert_actions(
                connection, tenant_id, run_id, request_id, 0, mutation.actions,
            )
            return GraphWorkflowReceipt(mutation.state, mutation.actions)

    def _actions_for_event(self, connection, tenant_id, run_id, event_id):
        rows = connection.execute(select(
            workflow_actions.c.action,
        ).where(and_(
            workflow_actions.c.tenant_id == tenant_id,
            workflow_actions.c.run_id == run_id,
            workflow_actions.c.source_event_id == event_id,
        )).order_by(workflow_actions.c.position)).scalars().all()
        return tuple(WorkflowAction.from_dict(raw) for raw in rows)

    def submit_graph_event(
        self,
        tenant_id: str,
        run_id: str,
        event: WorkflowEvent,
    ) -> GraphWorkflowReceipt:
        fingerprint = workflow_event_fingerprint(event)
        with self._tenant_connection(tenant_id) as connection:
            event_key = and_(
                workflow_events.c.tenant_id == tenant_id,
                workflow_events.c.run_id == run_id,
                workflow_events.c.event_id == event.event_id,
            )
            prior = connection.execute(select(
                workflow_events.c.fingerprint,
            ).where(event_key)).scalar_one_or_none()
            run_key = and_(
                workflow_runs.c.tenant_id == tenant_id,
                workflow_runs.c.run_id == run_id,
            )
            if prior is not None:
                if prior != fingerprint:
                    raise ValueError("workflow event_id was reused with different content")
                state_raw = connection.execute(select(workflow_runs.c.state).where(run_key)).scalar_one()
                return GraphWorkflowReceipt(
                    WorkflowRunState.from_dict(state_raw),
                    self._actions_for_event(connection, tenant_id, run_id, event.event_id),
                    duplicate=True,
                )
            row = connection.execute(select(
                workflow_runs.c.workflow_id,
                workflow_runs.c.workflow_version,
                workflow_runs.c.state_version,
                workflow_runs.c.state,
            ).where(run_key).with_for_update()).mappings().first()
            if row is None:
                raise LookupError("workflow run does not exist for this tenant")
            definition = self._definition(
                connection, tenant_id, row["workflow_id"], int(row["workflow_version"]),
            )
            current = WorkflowRunState.from_dict(row["state"])
            mutation = evolve_workflow(definition, current, event)
            changed = connection.execute(update(workflow_runs).where(and_(
                run_key,
                workflow_runs.c.state_version == row["state_version"],
            )).values(
                state_version=mutation.state.version,
                state=mutation.state.to_dict(),
                updated_at=_now(),
            ))
            if changed.rowcount != 1:
                raise RuntimeError("concurrent graph writer lost its version fence")
            connection.execute(insert(workflow_events).values(
                tenant_id=tenant_id,
                run_id=run_id,
                event_id=event.event_id,
                state_version=mutation.state.version,
                fingerprint=fingerprint,
                event=event.to_dict(),
                created_at=_now(),
            ))
            self._insert_actions(
                connection, tenant_id, run_id, event.event_id,
                mutation.state.version, mutation.actions,
            )
            return GraphWorkflowReceipt(mutation.state, mutation.actions)

    def get_graph_run(self, tenant_id: str, run_id: str) -> WorkflowRunState | None:
        with self._tenant_connection(tenant_id) as connection:
            raw = connection.execute(select(workflow_runs.c.state).where(and_(
                workflow_runs.c.tenant_id == tenant_id,
                workflow_runs.c.run_id == run_id,
            ))).scalar_one_or_none()
        return None if raw is None else WorkflowRunState.from_dict(raw)

    def close(self) -> None:
        self._engine.dispose()
