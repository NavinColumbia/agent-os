"""Composition root for the V2 HTTP service."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path

from fastapi import FastAPI

from agent_os.api.app import create_app
from agent_os.api.auth import HMACTokenIdentity
from agent_os.infrastructure.dbos_lifecycle import DBOSLifecycleEngine
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore
from agent_os.infrastructure.sql_notifications import SQLNotificationStore
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine


@dataclass(frozen=True)
class ServerSettings:
    environment: str
    system_database_url: str
    application_database_url: str
    auth_secret: str
    application_version: str
    host: str
    port: int
    create_schema: bool

    @classmethod
    def from_env(cls) -> "ServerSettings":
        environment = os.getenv("AOS_ENVIRONMENT", "development").strip().lower()
        if environment not in {"development", "test", "staging", "production"}:
            raise ValueError("AOS_ENVIRONMENT must be development, test, staging, or production")
        runtime_dir = Path(os.getenv("AOS_V2_RUNTIME_DIR", ".runtime/v2")).resolve()
        if environment in {"development", "test"}:
            runtime_dir.mkdir(parents=True, exist_ok=True)
        default_system = f"sqlite:///{runtime_dir / 'dbos.sqlite3'}"
        default_application = f"sqlite:///{runtime_dir / 'application.sqlite3'}"
        system_database_url = os.getenv("AOS_V2_SYSTEM_DATABASE_URL", default_system)
        application_database_url = os.getenv(
            "AOS_V2_APPLICATION_DATABASE_URL",
            os.getenv("DATABASE_URL", default_application),
        )
        auth_secret = os.getenv("AOS_V2_AUTH_SECRET", "")
        create_schema = os.getenv(
            "AOS_V2_CREATE_SCHEMA", "1" if environment in {"development", "test"} else "0"
        ).lower() in {"1", "true", "yes", "on"}
        if environment in {"staging", "production"}:
            if not system_database_url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
                raise ValueError("staging/production DBOS system database must be PostgreSQL")
            if not application_database_url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
                raise ValueError("staging/production application database must be PostgreSQL")
            if create_schema:
                raise ValueError("staging/production requires explicit migrations, not create_schema")
            if len(auth_secret.encode()) < 32:
                raise ValueError("staging/production AOS_V2_AUTH_SECRET must be at least 32 bytes")
        elif not auth_secret:
            auth_secret = "agent-os-development-secret-change-me"
        return cls(
            environment=environment,
            system_database_url=system_database_url,
            application_database_url=application_database_url,
            auth_secret=auth_secret,
            application_version=os.getenv("AOS_V2_APPLICATION_VERSION", "v2-dev"),
            host=os.getenv("AOS_V2_HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", os.getenv("AOS_V2_PORT", "8080"))),
            create_schema=create_schema,
        )


def build_app(settings: ServerSettings | None = None) -> FastAPI:
    settings = settings or ServerSettings.from_env()
    resources = ExitStack()
    try:
        engine = DBOSLifecycleEngine(
            system_database_url=settings.system_database_url,
            application_database_url=settings.application_database_url,
            application_version=settings.application_version,
            create_schema=settings.create_schema,
        )
        resources.callback(engine.close)
        graph_engine = SQLGraphWorkflowEngine(
            settings.application_database_url,
            create_schema=settings.create_schema,
        )
        resources.callback(graph_engine.close)
        notification_store = SQLNotificationStore(
            settings.application_database_url,
            create_schema=settings.create_schema,
        )
        resources.callback(notification_store.close)
        artifact_store = SQLArtifactStore(
            settings.application_database_url,
            create_schema=settings.create_schema,
        )
        resources.callback(artifact_store.close)
        app = create_app(
            engine=engine,
            identity=HMACTokenIdentity(settings.auth_secret),
            graph_engine=graph_engine,
            notification_store=notification_store,
            artifact_store=artifact_store,
            shutdown=resources.close,
        )
    except Exception:
        resources.close()
        raise
    app.state.workflow_engine = engine
    app.state.graph_workflow_engine = graph_engine
    app.state.notification_store = notification_store
    app.state.artifact_store = artifact_store
    app.state.settings = settings
    return app
