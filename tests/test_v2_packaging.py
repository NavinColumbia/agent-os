from __future__ import annotations

import tomllib
from pathlib import Path


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
