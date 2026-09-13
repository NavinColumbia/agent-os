"""Composition root for the V2 HTTP service."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI

from agent_os.api.app import create_app
from agent_os.api.auth import Authenticator, HMACTokenIdentity, OIDCTokenIdentity
from agent_os.application.billing import BillingCatalog, BillingPlan, BillingService
from agent_os.infrastructure.dbos_lifecycle import DBOSLifecycleEngine
from agent_os.infrastructure.gcs_artifacts import build_artifact_store
from agent_os.infrastructure.sql_company_directory import SQLCompanyDirectory
from agent_os.infrastructure.sql_connectors import SQLConnectorRegistry
from agent_os.infrastructure.sql_memberships import SQLMembershipStore
from agent_os.infrastructure.sql_notifications import SQLNotificationStore
from agent_os.infrastructure.sql_preview_deployments import SQLStaticPreviewDeployer
from agent_os.infrastructure.sql_billing import SQLBillingStore
from agent_os.infrastructure.sql_usage_meter import SQLUsageMeter
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine
from agent_os.infrastructure.stripe_billing import StripeBillingGateway


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
    oidc_authorization_url: str
    oidc_token_url: str
    oidc_client_id: str
    oidc_scope: str
    oidc_authorization_audience_parameter: str
    oidc_personal_tenants: bool
    tenant_derivation_secret: str
    tenant_monthly_model_budget_cents: int
    billing_mode: str
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_starter_price_id: str
    stripe_growth_price_id: str
    stripe_starter_model_budget_cents: int
    stripe_growth_model_budget_cents: int
    stripe_api_version: str
    application_version: str
    public_base_url: str
    preview_ttl_seconds: int
    artifact_backend: str
    artifact_bucket: str
    artifact_max_content_bytes: int
    host: str
    port: int
    create_schema: bool

    @classmethod
    def from_env(
        cls, *, require_billing: bool = True, require_identity: bool = True,
    ) -> "ServerSettings":
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
        oidc_authorization_url = os.getenv("AOS_V2_OIDC_AUTHORIZATION_URL", "").strip()
        oidc_token_url = os.getenv("AOS_V2_OIDC_TOKEN_URL", "").strip()
        oidc_client_id = os.getenv("AOS_V2_OIDC_CLIENT_ID", "").strip()
        oidc_scope = os.getenv("AOS_V2_OIDC_SCOPE", "openid profile email").strip()
        oidc_authorization_audience_parameter = os.getenv(
            "AOS_V2_OIDC_AUTHORIZATION_AUDIENCE_PARAMETER", ""
        ).strip()
        oidc_personal_tenants = os.getenv(
            "AOS_V2_OIDC_PERSONAL_TENANTS", "0",
        ).strip().lower() in {"1", "true", "yes", "on"}
        tenant_derivation_secret = os.getenv("AOS_V2_TENANT_DERIVATION_SECRET", "")
        tenant_monthly_model_budget_cents = int(
            os.getenv("AOS_V2_TENANT_MONTHLY_MODEL_BUDGET_CENTS", "10000")
        )
        if not 1 <= tenant_monthly_model_budget_cents <= 1_000_000_000:
            raise ValueError(
                "AOS_V2_TENANT_MONTHLY_MODEL_BUDGET_CENTS must be between 1 and 1000000000"
            )
        billing_mode = os.getenv(
            "AOS_V2_BILLING_MODE", "stripe" if environment == "production" else "disabled",
        ).strip().lower()
        if billing_mode not in {"disabled", "stripe"}:
            raise ValueError("AOS_V2_BILLING_MODE must be disabled or stripe")
        stripe_secret_key = os.getenv("AOS_V2_STRIPE_SECRET_KEY", "").strip()
        stripe_webhook_secret = os.getenv("AOS_V2_STRIPE_WEBHOOK_SECRET", "").strip()
        stripe_starter_price_id = os.getenv("AOS_V2_STRIPE_STARTER_PRICE_ID", "").strip()
        stripe_growth_price_id = os.getenv("AOS_V2_STRIPE_GROWTH_PRICE_ID", "").strip()
        stripe_starter_model_budget_cents = int(
            os.getenv("AOS_V2_STRIPE_STARTER_MODEL_BUDGET_CENTS", "50000")
        )
        stripe_growth_model_budget_cents = int(
            os.getenv("AOS_V2_STRIPE_GROWTH_MODEL_BUDGET_CENTS", "250000")
        )
        stripe_api_version = os.getenv(
            "AOS_V2_STRIPE_API_VERSION", "2025-06-30.basil",
        ).strip()
        for name, value in (
            ("AOS_V2_STRIPE_STARTER_MODEL_BUDGET_CENTS", stripe_starter_model_budget_cents),
            ("AOS_V2_STRIPE_GROWTH_MODEL_BUDGET_CENTS", stripe_growth_model_budget_cents),
        ):
            if not 1 <= value <= 1_000_000_000:
                raise ValueError(f"{name} must be between 1 and 1000000000")
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
        if identity_mode == "oidc" and require_identity:
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
                personal_tenant_secret=(
                    tenant_derivation_secret if oidc_personal_tenants else None
                ),
            )
            if environment == "production":
                for label, value in (
                    ("authorization URL", oidc_authorization_url),
                    ("token URL", oidc_token_url),
                ):
                    parsed = urlparse(value)
                    if (
                        parsed.scheme != "https" or not parsed.netloc
                        or parsed.username or parsed.password or parsed.fragment
                    ):
                        raise ValueError(
                            f"production OIDC {label} must be HTTPS without credentials or a fragment"
                        )
                if not oidc_client_id or len(oidc_client_id) > 256:
                    raise ValueError("production AOS_V2_OIDC_CLIENT_ID is required")
                if not oidc_scope or len(oidc_scope) > 1_000:
                    raise ValueError("production AOS_V2_OIDC_SCOPE is required")
                if len(oidc_authorization_audience_parameter) > 64:
                    raise ValueError("OIDC authorization audience parameter is too long")
        port = int(os.getenv("PORT", os.getenv("AOS_V2_PORT", "8080")))
        public_base_url = os.getenv(
            "AOS_V2_PUBLIC_BASE_URL", f"http://127.0.0.1:{port}",
        ).rstrip("/")
        if environment == "production" and not public_base_url.startswith("https://"):
            raise ValueError("production AOS_V2_PUBLIC_BASE_URL must use HTTPS")
        if require_billing and environment == "production" and billing_mode != "stripe":
            raise ValueError("production requires AOS_V2_BILLING_MODE=stripe")
        if require_billing and billing_mode == "stripe":
            if not public_base_url.startswith("https://"):
                raise ValueError("Stripe billing requires an HTTPS AOS_V2_PUBLIC_BASE_URL")
            if not stripe_secret_key.startswith(("sk_test_", "sk_live_")):
                raise ValueError("Stripe billing requires AOS_V2_STRIPE_SECRET_KEY")
            if environment == "production" and not stripe_secret_key.startswith("sk_live_"):
                raise ValueError("production Stripe billing requires an sk_live_ secret key")
            if not stripe_webhook_secret.startswith("whsec_") or len(stripe_webhook_secret) < 16:
                raise ValueError("Stripe billing requires AOS_V2_STRIPE_WEBHOOK_SECRET")
            for name, value in (
                ("AOS_V2_STRIPE_STARTER_PRICE_ID", stripe_starter_price_id),
                ("AOS_V2_STRIPE_GROWTH_PRICE_ID", stripe_growth_price_id),
            ):
                if not value.startswith("price_"):
                    raise ValueError(f"Stripe billing requires {name}")
            if stripe_starter_price_id == stripe_growth_price_id:
                raise ValueError("Stripe billing price IDs must be unique")
            if stripe_starter_model_budget_cents <= tenant_monthly_model_budget_cents:
                raise ValueError("the Stripe starter model budget must exceed the free budget")
            if stripe_growth_model_budget_cents <= stripe_starter_model_budget_cents:
                raise ValueError("the Stripe growth model budget must exceed the starter budget")
        preview_ttl_seconds = int(os.getenv("AOS_V2_PREVIEW_TTL_SECONDS", "604800"))
        if not 60 <= preview_ttl_seconds <= 30 * 24 * 60 * 60:
            raise ValueError("AOS_V2_PREVIEW_TTL_SECONDS must be between 60 and 2592000")
        artifact_backend = os.getenv(
            "AOS_V2_ARTIFACT_BACKEND", "gcs" if environment == "production" else "sql",
        ).strip().lower()
        if artifact_backend not in {"sql", "gcs"}:
            raise ValueError("AOS_V2_ARTIFACT_BACKEND must be sql or gcs")
        if environment == "production" and artifact_backend != "gcs":
            raise ValueError("production requires AOS_V2_ARTIFACT_BACKEND=gcs")
        artifact_bucket = os.getenv("AOS_V2_ARTIFACT_BUCKET", "").strip()
        if artifact_backend == "gcs" and not artifact_bucket:
            raise ValueError("GCS artifacts require AOS_V2_ARTIFACT_BUCKET")
        default_artifact_limit = 64 * 1024 * 1024 if artifact_backend == "gcs" else 2 * 1024 * 1024
        artifact_max_content_bytes = int(os.getenv(
            "AOS_V2_ARTIFACT_MAX_CONTENT_BYTES", str(default_artifact_limit),
        ))
        if not 1 <= artifact_max_content_bytes <= 1024 * 1024 * 1024:
            raise ValueError("AOS_V2_ARTIFACT_MAX_CONTENT_BYTES must be between 1 and 1073741824")
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
            oidc_authorization_url=oidc_authorization_url,
            oidc_token_url=oidc_token_url,
            oidc_client_id=oidc_client_id,
            oidc_scope=oidc_scope,
            oidc_authorization_audience_parameter=oidc_authorization_audience_parameter,
            oidc_personal_tenants=oidc_personal_tenants,
            tenant_derivation_secret=tenant_derivation_secret,
            tenant_monthly_model_budget_cents=tenant_monthly_model_budget_cents,
            billing_mode=billing_mode,
            stripe_secret_key=stripe_secret_key,
            stripe_webhook_secret=stripe_webhook_secret,
            stripe_starter_price_id=stripe_starter_price_id,
            stripe_growth_price_id=stripe_growth_price_id,
            stripe_starter_model_budget_cents=stripe_starter_model_budget_cents,
            stripe_growth_model_budget_cents=stripe_growth_model_budget_cents,
            stripe_api_version=stripe_api_version,
            application_version=os.getenv("AOS_V2_APPLICATION_VERSION", "v2-dev"),
            public_base_url=public_base_url,
            preview_ttl_seconds=preview_ttl_seconds,
            artifact_backend=artifact_backend,
            artifact_bucket=artifact_bucket,
            artifact_max_content_bytes=artifact_max_content_bytes,
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
            personal_tenant_secret=(
                settings.tenant_derivation_secret if settings.oidc_personal_tenants else None
            ),
        )
    return HMACTokenIdentity(settings.auth_secret)


def browser_identity_config(settings: ServerSettings) -> dict[str, str]:
    if settings.identity_mode != "oidc":
        return {"identity_mode": "hmac", "billing_mode": settings.billing_mode}
    return {
        "identity_mode": "oidc",
        "billing_mode": settings.billing_mode,
        "authorization_url": settings.oidc_authorization_url,
        "token_url": settings.oidc_token_url,
        "client_id": settings.oidc_client_id,
        "scope": settings.oidc_scope,
        "audience": settings.oidc_audience,
        "authorization_audience_parameter": settings.oidc_authorization_audience_parameter,
        "redirect_uri": f"{settings.public_base_url}/app",
    }


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
        connector_registry = SQLConnectorRegistry(
            settings.application_database_url,
            create_schema=settings.create_schema,
        )
        resources.callback(connector_registry.close)
        membership_store = SQLMembershipStore(
            settings.application_database_url,
            signing_secret=settings.capability_secret,
            create_schema=settings.create_schema,
        )
        resources.callback(membership_store.close)
        notification_store = SQLNotificationStore(
            settings.application_database_url,
            create_schema=settings.create_schema,
        )
        resources.callback(notification_store.close)
        artifact_store = build_artifact_store(
            settings.application_database_url,
            backend=settings.artifact_backend,
            bucket_name=settings.artifact_bucket,
            create_schema=settings.create_schema,
            max_content_bytes=settings.artifact_max_content_bytes,
        )
        resources.callback(artifact_store.close)
        usage_meter = SQLUsageMeter(
            settings.application_database_url,
            monthly_budget_cents=settings.tenant_monthly_model_budget_cents,
            create_schema=settings.create_schema,
        )
        resources.callback(usage_meter.close)
        billing_service = None
        billing_store = None
        if settings.billing_mode == "stripe":
            billing_catalog = BillingCatalog(
                BillingPlan(
                    "free", "Free", settings.tenant_monthly_model_budget_cents,
                ),
                (
                    BillingPlan(
                        "starter", "Starter", settings.stripe_starter_model_budget_cents,
                        settings.stripe_starter_price_id,
                    ),
                    BillingPlan(
                        "growth", "Growth", settings.stripe_growth_model_budget_cents,
                        settings.stripe_growth_price_id,
                    ),
                ),
            )
            billing_store = SQLBillingStore(
                settings.application_database_url,
                free_monthly_model_budget_cents=settings.tenant_monthly_model_budget_cents,
                create_schema=settings.create_schema,
            )
            resources.callback(billing_store.close)
            billing_gateway = StripeBillingGateway(
                secret_key=settings.stripe_secret_key,
                webhook_secret=settings.stripe_webhook_secret,
                public_base_url=settings.public_base_url,
                api_version=settings.stripe_api_version,
            )
            resources.callback(billing_gateway.close)
            billing_service = BillingService(
                catalog=billing_catalog,
                accounts=billing_store,
                gateway=billing_gateway,
                usage_meter=usage_meter,
            )
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
            connector_registry=connector_registry,
            membership_store=membership_store,
            usage_meter=usage_meter,
            billing_service=billing_service,
            client_identity_config=browser_identity_config(settings),
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
    app.state.connector_registry = connector_registry
    app.state.membership_store = membership_store
    app.state.usage_meter = usage_meter
    app.state.billing_store = billing_store
    app.state.billing_service = billing_service
    app.state.settings = settings
    return app
