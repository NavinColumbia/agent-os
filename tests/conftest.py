"""Hermetic defaults for the repository test suite.

Tests may replace the low-level model runners with deterministic fakes, but a
missed mock must never spend money or leave a real Claude/Codex child behind.
An explicitly live test has to opt back in for its own scope.
"""

import sys

import pytest


@pytest.fixture(autouse=True)
def _hermetic_agent_defaults(monkeypatch):
    monkeypatch.setenv("AOS_SELFTEST", "1")
    monkeypatch.setenv("AOS_DISABLE_EXTERNAL_MODEL_EXEC", "1")
    # Repository tests use the configured local database for focused durability checks. Even if a test misses
    # a notifier mock, it must never turn a synthetic urgent row into a real phone page.
    monkeypatch.setenv("AOS_DISABLE_EXTERNAL_NOTIFICATIONS", "1")
    monkeypatch.setenv("AOS_EXPLORATION", "1")
    monkeypatch.setenv("AOS_RIGOR", "1")
    monkeypatch.setenv("AOS_MAX_DEPTH", "1")
    monkeypatch.setenv("AOS_MAX_TOTAL_COMPONENTS", "16")
    # scale.apply() intentionally adjusts process-wide production knobs. A test
    # that exercises it must not leak those module constants into later tests.
    project = sys.modules.get("project")
    if project is not None:
        monkeypatch.setattr(project, "MAX_DEPTH", 1)
        monkeypatch.setattr(project, "MAX_TOTAL_COMPONENTS", 16)
