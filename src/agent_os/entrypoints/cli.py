"""One CLI for local evaluation and the hosted-service process."""

from __future__ import annotations

import json
import os
from threading import Event

import click

from agent_os.api.auth import HMACTokenIdentity
from agent_os.entrypoints.server import ServerSettings, build_app
from agent_os.entrypoints.static_site_router import (
    StaticSiteRouterSettings,
    build_static_site_router,
)
from agent_os.entrypoints.worker import WorkerSettings, install_shutdown_handlers, run_worker
from agent_os.infrastructure.http_connector_tools import (
    FileConnectorSecretResolver,
    GCPConnectorSecretResolver,
)
from agent_os.infrastructure.deployment_operations import GCPDeploymentOperator


@click.group()
def main() -> None:
    """Operate the Agent OS V2 service."""


@main.command()
def serve() -> None:
    """Run the authenticated control API."""

    import uvicorn

    settings = ServerSettings.from_env()
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


@main.command("static-router")
def static_router() -> None:
    """Serve immutable generated applications from private object storage."""

    import uvicorn

    try:
        settings = StaticSiteRouterSettings.from_env()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    uvicorn.run(build_static_site_router(settings), host=settings.host, port=settings.port)


@main.command()
@click.option(
    "--organization",
    "organizations",
    multiple=True,
    help=(
        "Optional static tenant allowlist; repeat it, or set AOS_V2_WORKER_ORGANIZATIONS. "
        "Production workers discover ready tenants when no allowlist is set."
    ),
)
@click.option("--once", is_flag=True, help="Run one fair polling cycle and exit.")
def worker(organizations: tuple[str, ...], once: bool) -> None:
    """Run the durable, lease-renewing agent command worker."""

    try:
        settings = WorkerSettings.from_env(organization_ids=organizations)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    stop = Event()
    install_shutdown_handlers(stop)
    run_worker(settings, once=once, stop=stop)


@main.command("issue-local-token")
@click.option("--subject", default="founder", show_default=True)
@click.option("--organization", default="local-company", show_default=True)
@click.option("--role", "roles", multiple=True, default=("owner",), show_default=True)
@click.option("--ttl-seconds", default=86_400, type=click.IntRange(60, 2_592_000), show_default=True)
def issue_local_token(subject: str, organization: str, roles: tuple[str, ...], ttl_seconds: int) -> None:
    """Issue a signed token for local/BYOC evaluation (not hosted signup)."""

    settings = ServerSettings.from_env()
    if settings.environment not in {"development", "test", "staging"}:
        raise click.ClickException("local tokens are disabled in production")
    token = HMACTokenIdentity(settings.auth_secret).issue(
        subject_id=subject,
        organization_id=organization,
        roles=roles,
        ttl_seconds=ttl_seconds,
    )
    click.echo(json.dumps({"token": token, "organization_id": organization, "roles": roles}))


@main.command("connector-secret-name")
@click.option("--organization", required=True)
@click.option("--credential-ref", required=True)
@click.option("--backend", type=click.Choice(("file", "gcp")), default="file", show_default=True)
def connector_secret_name(organization: str, credential_ref: str, backend: str) -> None:
    """Print the secret locator for a connector credential, never its value."""

    if not organization.strip() or not credential_ref.strip():
        raise click.ClickException("organization and credential-ref are required")
    if backend == "gcp":
        locator = GCPConnectorSecretResolver.secret_name(organization, credential_ref)
    else:
        locator = (
            FileConnectorSecretResolver.tenant_directory(organization)
            + "/" + credential_ref
        )
    click.echo(json.dumps({"backend": backend, "locator": locator}))


@main.command("deployment-control")
@click.option("--kind", type=click.Choice(("static", "service")), required=True)
@click.option("--action", type=click.Choice(("suspend", "resume")), required=True)
@click.option("--target", required=True, help="Opaque route ID or generated Cloud Run service name.")
@click.option("--reason", default="", help="Required for suspension; recorded in the operator result.")
@click.option("--actor", required=True, help="Incident operator identity or ticket reference.")
def deployment_control(kind: str, action: str, target: str, reason: str, actor: str) -> None:
    """Suspend or restore one generated application using ADC break-glass authority."""

    try:
        operator = GCPDeploymentOperator(
            published_bucket=os.getenv("AOS_V2_PUBLISHED_APP_BUCKET", "").strip(),
            app_project_id=os.getenv("AOS_V2_APP_PROJECT_ID", "").strip(),
            region=os.getenv("AOS_V2_APP_REGION", "").strip(),
        )
        try:
            if kind == "static":
                result = operator.set_static_route(
                    target, suspended=action == "suspend", reason=reason, actor=actor,
                )
            else:
                result = operator.set_service(
                    target, suspended=action == "suspend", reason=reason, actor=actor,
                )
        finally:
            operator.close()
    except (ConnectionError, LookupError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, sort_keys=True))
