#!/usr/bin/env python3
"""QA artifact paths.

The QA loop must leave human-inspectable evidence: reports, screenshots, event logs, and verdicts.
Default to a Windows-visible folder when running under WSL so the owner can open the artifacts directly.
"""
import os
import re
import time
from pathlib import Path


def _windows_visible_root() -> Path | None:
    users = Path("/mnt/c/Users")
    if not users.exists():
        return None
    preferred = os.environ.get("AOS_WINDOWS_USER")
    names = [preferred] if preferred else []
    names += ["navin", os.environ.get("USER", ""), "Public"]
    for name in names:
        if not name:
            continue
        home = users / name
        for base in ("Desktop", "Documents", "Downloads"):
            p = home / base
            if p.exists():
                return p / "agent-os-qa-evidence"
    return None


def root() -> Path:
    configured = os.environ.get("AOS_QA_EVIDENCE_DIR")
    if configured:
        return Path(configured).expanduser()
    return _windows_visible_root() or Path(os.environ.get("AOS_QA_DIR", "/tmp/aos-qa"))


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "app")).strip("-._")
    return (s or "app")[:80]


def run_dir(product: str = "app", ts: float | None = None) -> Path:
    ts = ts or time.time()
    d = root() / _slug(product) / time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
    d.mkdir(parents=True, exist_ok=True)
    (d / "screenshots").mkdir(parents=True, exist_ok=True)
    return d
