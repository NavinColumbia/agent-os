"""Stable identities shared by mission intake, workers, and projections."""

from __future__ import annotations

import hashlib


def mission_planning_run_id(lifecycle_run_id: str) -> str:
    if not lifecycle_run_id.strip():
        raise ValueError("lifecycle_run_id is required")
    digest = hashlib.sha256(f"agent-os:mission-plan:v1:{lifecycle_run_id}".encode()).hexdigest()
    return f"plan-{digest[:32]}"
