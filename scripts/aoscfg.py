#!/usr/bin/env python3
"""aoscfg.py — the ONE place agent-os resolves its runtime config (DB URL, .env.local, venv python).

Historically ~85 modules each hardcoded `Path.home()/"projects"/"agent-os"/".env.local"` and the venv
python path. That location is CORRECT in production but wrong anywhere else — a CI checkout under
$GITHUB_WORKSPACE, or a dev clone in another directory — which is exactly what kept CI red and forced a
symlink band-aid. Centralizing resolution here kills that whole bug class:

  * DATABASE_URL from the real environment WINS (twelve-factor: CI/tests can inject it with no file);
  * else .env.local, located relative to THIS file (repo root) first, then the production home path;
  * VENV_PY resolves the same way (repo-relative .venv if present, else the home path).

Stdlib-only and imports NO agent-os module, so every module can `from aoscfg import ENV, DB` without
risking an import cycle.
"""
import os
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]   # scripts/aoscfg.py -> repo root


def _env_file() -> Path:
    override = os.environ.get("AOS_ENV_FILE")
    if override:
        return Path(override)
    local = _REPO / ".env.local"
    if local.exists():
        return local
    return Path.home() / "projects" / "agent-os" / ".env.local"


ENV = _env_file()


def _read_env() -> dict:
    out = {}
    try:
        for line in ENV.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


_CFG = _read_env()

# DATABASE_URL: the real environment overrides the file so CI/tests can inject it without writing one.
DB = os.environ.get("DATABASE_URL") or _CFG.get("DATABASE_URL")

# Portable runtime topology.  Defaults preserve the historical sibling layout
# while allowing a public installation to live anywhere on disk.
AOS_ROOT = Path(os.environ.get("AOS_ROOT") or _CFG.get("AOS_ROOT") or _REPO).expanduser().resolve()
PROJECTS_ROOT = Path(os.environ.get("AOS_PROJECTS_ROOT") or _CFG.get("AOS_PROJECTS_ROOT")
                     or AOS_ROOT.parent).expanduser().resolve()
CONTROL_PLANE_ROOT = Path(os.environ.get("AOS_CONTROL_PLANE_ROOT")
                          or _CFG.get("AOS_CONTROL_PLANE_ROOT")
                          or PROJECTS_ROOT / "control-plane").expanduser().resolve()
PRODUCTS_ROOT = Path(os.environ.get("AOS_PRODUCTS_ROOT") or _CFG.get("AOS_PRODUCTS_ROOT")
                     or PROJECTS_ROOT / "products").expanduser().resolve()

# venv python used to spawn sandboxed subprocesses — repo-relative if present, else the prod home path.
_venv = _REPO / ".venv" / "bin" / "python"
VENV_PY = str(_venv if _venv.exists()
              else Path.home() / "projects" / "agent-os" / ".venv" / "bin" / "python")


def get(key: str, default=None):
    """Any other key from .env.local (real env var wins), e.g. AUDIT_HMAC_KEY."""
    return os.environ.get(key) or _CFG.get(key, default)


if __name__ == "__main__":
    print(f"repo      = {_REPO}")
    print(f"env file  = {ENV}  (exists={ENV.exists()})")
    print(f"DATABASE_URL resolved = {bool(DB)}")
    print(f"VENV_PY   = {VENV_PY}")
    print(f"products  = {PRODUCTS_ROOT}")
    print(f"control   = {CONTROL_PLANE_ROOT}")
