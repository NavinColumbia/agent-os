"""One CLI for local evaluation and the hosted-service process."""

from __future__ import annotations

import json

import click

from agent_os.api.auth import HMACTokenIdentity
from agent_os.entrypoints.server import ServerSettings, build_app


@click.group()
def main() -> None:
    """Operate the Agent OS V2 service."""


@main.command()
def serve() -> None:
    """Run the authenticated control API."""

    import uvicorn

    settings = ServerSettings.from_env()
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


@main.command("issue-local-token")
@click.option("--subject", default="founder", show_default=True)
@click.option("--organization", default="local-company", show_default=True)
@click.option("--role", "roles", multiple=True, default=("owner",), show_default=True)
@click.option("--ttl-seconds", default=86_400, type=click.IntRange(60, 2_592_000), show_default=True)
def issue_local_token(subject: str, organization: str, roles: tuple[str, ...], ttl_seconds: int) -> None:
    """Issue a signed token for local/BYOC evaluation (not hosted signup)."""

    settings = ServerSettings.from_env()
    if settings.environment not in {"development", "test"}:
        raise click.ClickException("local tokens are disabled outside development/test")
    token = HMACTokenIdentity(settings.auth_secret).issue(
        subject_id=subject,
        organization_id=organization,
        roles=roles,
        ttl_seconds=ttl_seconds,
    )
    click.echo(json.dumps({"token": token, "organization_id": organization, "roles": roles}))
