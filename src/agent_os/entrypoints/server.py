"""Composition root for the V2 HTTP service."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path

from fastapi import FastAPI

from agent_os.api.app import create_app
from agent_os.api.auth import Authenticator, HMACTokenIdentity, OIDCTokenIdentity
from agent_os.infrastructure.dbos_lifecycle import DBOSLifecycleEngine
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore
from agent_os.infrastructure.sql_company_directory import SQLCompanyDirectory
from agent_os.infrastructure.sql_notifications import SQLNotificationStore
from agent_os.infrastructure.sql_preview_deployments import SQLStaticPreviewDeployer
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine


@dataclass(frozen=True)
class ServerSettings:
    environment: str
    system_database_url: str
    application_database_url: str
    identity_mode: str
    auth_secret: str
    capability_secret: str
    oidc_issuer: str
    oidc_audience: str
    oidc_jwks_url: str
    oidc_organization_claim: str
    oidc_roles_claim: str
    oidc_algorithms: tuple[str, ...]
    oidc_leeway_seconds: int
    oidc_maximum_token_lifetime_seconds: int
    application_version: str
    public_base_url: str
    preview_ttl_seconds: int
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
        identity_mode = os.getenv(
            "AOS_V2_IDENTITY_MODE", "oidc" if environment == "production" else "hmac"
        ).strip().lower()
        if identity_mode not in {"hmac", "oidc"}:
            raise ValueError("AOS_V2_IDENTITY_MODE must be hmac or oidc")
        auth_secret = os.getenv("AOS_V2_AUTH_SECRET", "")
        capability_secret = os.getenv("AOS_V2_CAPABILITY_SECRET", "")
        oidc_issuer = os.getenv("AOS_V2_OIDC_ISSUER", "").strip().rstrip("/")
        oidc_audience = os.getenv("AOS_V2_OIDC_AUDIENCE", "").strip()
        oidc_jwks_url = os.getenv("AOS_V2_OIDC_JWKS_URL", "").strip()
        oidc_organization_claim = os.getenv("AOS_V2_OIDC_ORGANIZATION_CLAIM", "org_id").strip()
        oidc_roles_claim = os.getenv("AOS_V2_OIDC_ROLES_CLAIM", "roles").strip()
        oidc_algorithms = tuple(
            item.strip().upper()
            for item in os.getenv("AOS_V2_OIDC_ALGORITHMS", "RS256,ES256").split(",")
            if item.strip()
        )
        oidc_leeway_seconds = int(os.getenv("AOS_V2_OIDC_LEEWAY_SECONDS", "60"))
        oidc_maximum_token_lifetime_seconds = int(
            os.getenv("AOS_V2_OIDC_MAXIMUM_TOKEN_LIFETIME_SECONDS", "86400")
        )
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
            if environment == "production" and identity_mode != "oidc":
                raise ValueError("production requires AOS_V2_IDENTITY_MODE=oidc")
            if identity_mode == "hmac" and len(auth_secret.encode()) < 32:
                raise ValueError("staging HMAC identity requires AOS_V2_AUTH_SECRET of at least 32 bytes")
        elif not auth_secret:
            auth_secret = "agent-os-development-secret-change-me"
        if not capability_secret:
            capability_secret = auth_secret
        if len(capability_secret.encode()) < 32:
            raise ValueError("AOS_V2_CAPABILITY_SECRET must be at least 32 bytes")
        if environment == "production" and capability_secret == auth_secret and auth_secret:
            raise ValueError("production capability and local-auth secrets must be independent")
        if identity_mode == "oidc":
            # Construct once during settings validation so bad URLs, algorithms,
            # and bounds fail before the process starts accepting traffic.
            OIDCTokenIdentity(
                issuer=oidc_issuer,
                audience=oidc_audience,
                jwks_url=oidc_jwks_url,
                organization_claim=oidc_organization_claim,
                roles_claim=oidc_roles_claim,
                algorithms=oidc_algorithms,
                leeway_seconds=oidc_leeway_seconds,
                maximum_token_lifetime_seconds=oidc_maximum_token_lifetime_seconds,
            )
        port = int(os.getenv("PORT", os.getenv("AOS_V2_PORT", "8080")))
        public_base_url = os.getenv(
            "AOS_V2_PUBLIC_BASE_URL", f"http://127.0.0.1:{port}",
        ).rstrip("/")
        if environment == "production" and not public_base_url.startswith("https://"):
            raise ValueError("production AOS_V2_PUBLIC_BASE_URL must use HTTPS")
        preview_ttl_seconds = int(os.getenv("AOS_V2_PREVIEW_TTL_SECONDS", "604800"))
        if not 60 <= preview_ttl_seconds <= 30 * 24 * 60 * 60:
            raise ValueError("AOS_V2_PREVIEW_TTL_SECONDS must be between 60 and 2592000")
        return cls(
            environment=environment,
            system_database_url=system_database_url,
            application_database_url=application_database_url,
            identity_mode=identity_mode,
            auth_secret=auth_secret,
            capability_secret=capability_secret,
            oidc_issuer=oidc_issuer,
            oidc_audience=oidc_audience,
            oidc_jwks_url=oidc_jwks_url,
            oidc_organization_claim=oidc_organization_claim,
            oidc_roles_claim=oidc_roles_claim,
            oidc_algorithms=oidc_algorithms,
            oidc_leeway_seconds=oidc_leeway_seconds,
            oidc_maximum_token_lifetime_seconds=oidc_maximum_token_lifetime_seconds,
            application_version=os.getenv("AOS_V2_APPLICATION_VERSION", "v2-dev"),
            public_base_url=public_base_url,
            preview_ttl_seconds=preview_ttl_seconds,
            host=os.getenv("AOS_V2_HOST", "127.0.0.1"),
            port=port,
            create_schema=create_schema,
        )


def build_identity(settings: ServerSettings) -> Authenticator:
    if settings.identity_mode == "oidc":
        return OIDCTokenIdentity(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_url=settings.oidc_jwks_url,
            organization_claim=settings.oidc_organization_claim,
            roles_claim=settings.oidc_roles_claim,
            algorithms=settings.oidc_algorithms,
            leeway_seconds=settings.oidc_leeway_seconds,
            maximum_token_lifetime_seconds=settings.oidc_maximum_token_lifetime_seconds,
        )
    return HMACTokenIdentity(settings.auth_secret)


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
        company_directory = SQLCompanyDirectory(
            settings.application_database_url,
            create_schema=settings.create_schema,
        )
        resources.callback(company_directory.close)
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
        preview_deployments = SQLStaticPreviewDeployer(
            settings.application_database_url,
            artifact_store,
            public_base_url=settings.public_base_url,
            capability_secret=settings.capability_secret,
            ttl_seconds=settings.preview_ttl_seconds,
            create_schema=settings.create_schema,
        )
        resources.callback(preview_deployments.close)
        app = create_app(
            engine=engine,
            identity=build_identity(settings),
            graph_engine=graph_engine,
            notification_store=notification_store,
            artifact_store=artifact_store,
            preview_deployments=preview_deployments,
            company_directory=company_directory,
            shutdown=resources.close,
        )
    except Exception:
        resources.close()
        raise
    app.state.workflow_engine = engine
    app.state.graph_workflow_engine = graph_engine
    app.state.notification_store = notification_store
    app.state.artifact_store = artifact_store
    app.state.preview_deployments = preview_deployments
    app.state.company_directory = company_directory
    app.state.settings = settings
    return app
