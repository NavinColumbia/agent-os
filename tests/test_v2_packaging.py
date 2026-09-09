from __future__ import annotations

import tomllib
from pathlib import Path

from click.testing import CliRunner

from agent_os.entrypoints.cli import main


ROOT = Path(__file__).resolve().parents[1]


def test_production_lock_excludes_unhashable_local_project_and_covers_direct_dependencies():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "pylock.toml").read_text())
    locked = {package["name"] for package in lock["packages"]}
    assert "agent-os" not in locked
    for requirement in pyproject["project"]["dependencies"]:
        normalized = requirement.split("[", 1)[0].split("=", 1)[0].lower().replace("_", "-")
        assert normalized in locked


def test_runtime_image_installs_locked_dependencies_before_local_package():
    dockerfile = (ROOT / "deploy" / "Dockerfile.v2").read_text()
    assert "pip install --no-cache-dir -r pylock.toml" in dockerfile
    assert "pip install --no-cache-dir --no-deps ." in dockerfile


def test_packaged_cli_exposes_api_and_worker_processes():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
    assert "worker" in result.output


def test_evaluation_compose_runs_api_and_worker_from_the_same_image():
    compose = (ROOT / "deploy" / "docker-compose.v2.yml").read_text()
    assert "dockerfile: deploy/Dockerfile.v2" in compose
    assert "command: [\"agentos-v2\", \"worker\"]" in compose
    assert "service_completed_successfully" in compose
    assert "88-workflow-action-leases-v2.sql" in compose
    assert "89-notifications-v2.sql" in compose
    assert "90-artifacts-v2.sql" in compose
    assert "91-ready-tenant-discovery-v2.sql" in compose
    assert "92-preview-deployments-v2.sql" in compose
    assert "93-preview-capability-lifecycle-v2.sql" in compose
    assert "94-management-watch-v2.sql" in compose
    assert "95-company-directory-v2.sql" in compose
    assert "96-company-proposal-decisions-v2.sql" in compose
    env_example = (ROOT / "deploy" / "v2.env.example").read_text()
    assert "AOS_V2_SANDBOX_BACKEND=disabled" in env_example
    assert "AOS_V2_PUBLIC_BASE_URL=http://localhost:8080" in env_example
    assert "AOS_V2_PREVIEW_TTL_SECONDS=604800" in env_example
    assert "@sha256:" in env_example
