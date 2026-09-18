"""One CLI for local evaluation and the hosted-service process."""

from __future__ import annotations

import json
import os
from threading import Event

import click

from agent_os.api.auth import HMACTokenIdentity
from agent_os.domain.benchmark import (
    evaluate_benchmark,
    parse_benchmark_manifest,
    parse_benchmark_trial,
)
from agent_os.domain.resilience import (
    evaluate_fault_campaign,
    parse_fault_campaign,
    parse_fault_observation,
)
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
from agent_os.infrastructure.sql_execution_health import SQLExecutionReleaseGate


@click.group()
def main() -> None:
    """Operate the Agent OS V2 service."""


@main.command("benchmark-report")
@click.option(
    "--manifest", "manifest_file", required=True,
    type=click.File("r", encoding="utf-8"),
    help="Frozen JSON benchmark manifest.",
)
@click.option(
    "--trials", "trials_file", required=True,
    type=click.File("r", encoding="utf-8"),
    help="JSON array, or object with a trials array, containing measured outcomes.",
)
@click.option("--customer-price-cents", type=click.FloatRange(min=0), default=0.0)
@click.option(
    "--human-hourly-value-cents", type=click.FloatRange(min=0),
    default=6000.0, show_default=True,
)
def benchmark_report(
    manifest_file, trials_file, customer_price_cents: float,
    human_hourly_value_cents: float,
) -> None:
    """Validate a matched benchmark and emit an immutable value-report payload."""

    try:
        manifest_raw = json.load(manifest_file)
        trials_raw = json.load(trials_file)
        if isinstance(trials_raw, dict):
            trials_raw = trials_raw.get("trials")
        if not isinstance(manifest_raw, dict) or not isinstance(trials_raw, list):
            raise ValueError("manifest must be an object and trials must be an array")
        report = evaluate_benchmark(
            parse_benchmark_manifest(manifest_raw),
            (parse_benchmark_trial(item) for item in trials_raw),
            customer_price_cents=customer_price_cents,
            human_hourly_value_cents=human_hourly_value_cents,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))


@main.command("resilience-report")
@click.option(
    "--campaign", "campaign_file", required=True,
    type=click.File("r", encoding="utf-8"),
    help="Frozen JSON fault-campaign contract.",
)
@click.option(
    "--observations", "observations_file", required=True,
    type=click.File("r", encoding="utf-8"),
    help="JSON array, or object with an observations array, of measured fault outcomes.",
)
def resilience_report(campaign_file, observations_file) -> None:
    """Validate a six-class recovery campaign without upgrading local evidence."""

    try:
        campaign_raw = json.load(campaign_file)
        observations_raw = json.load(observations_file)
        if isinstance(observations_raw, dict):
            observations_raw = observations_raw.get("observations")
        if not isinstance(campaign_raw, dict) or not isinstance(observations_raw, list):
            raise ValueError("campaign must be an object and observations must be an array")
        report = evaluate_fault_campaign(
            parse_fault_campaign(campaign_raw),
            (parse_fault_observation(item) for item in observations_raw),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))


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


@main.command("activate-release")
def activate_release() -> None:
    """Atomically permit this exact application release to claim new work."""

    database_url = os.getenv("AOS_V2_APPLICATION_DATABASE_URL", "").strip()
    cell_id = os.getenv("AOS_V2_EXECUTION_CELL_ID", "").strip()
    application_version = os.getenv("AOS_V2_APPLICATION_VERSION", "").strip()
    try:
        gate = SQLExecutionReleaseGate(
            database_url,
            cell_id=cell_id,
            application_version=application_version,
            statement_timeout_seconds=86_400,
        )
        try:
            generation = gate.activate()
        finally:
            gate.close()
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({
        "event": "execution_release_activated",
        "execution_cell_id": cell_id,
        "application_version": application_version,
        "activation_generation": generation,
    }, sort_keys=True))


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
