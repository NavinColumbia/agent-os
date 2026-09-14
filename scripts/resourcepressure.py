#!/usr/bin/env python3
"""Pure admission math for bounded host and queue pressure.

The live controllers call these functions, but this module has no database,
process, browser, or network side effects.  That makes saturation policy
deterministic and lets assurance exercise overload without touching live work.
"""
from __future__ import annotations

import math


def browser_capacity(available_mb, cpu_count, *, reserve_mb=2048,
                     mb_per_session=550, cores_per_session=0.8,
                     auto_ceiling=2, override=None):
    """Return safe simultaneous browser sessions; zero means do not launch.

    An operator override is a ceiling, never permission to ignore current RAM/CPU
    pressure.  In particular, a stale high override must not turn a pressure drop
    into new Chromium/model launches.
    """
    try:
        available_mb = max(0, int(available_mb))
        cpu_count = max(0.0, float(cpu_count))
        reserve_mb = max(0, int(reserve_mb))
        mb_per_session = float(mb_per_session)
        cores_per_session = float(cores_per_session)
        auto_ceiling = max(0, min(64, int(auto_ceiling)))
        if override not in (None, ""):
            try:
                # Dedicated nodes may raise the configured ceiling, but it remains
                # subordinate to the live RAM and CPU dimensions below.
                auto_ceiling = max(0, min(64, int(override)))
            except (TypeError, ValueError):
                pass
        if mb_per_session <= 0 or cores_per_session <= 0:
            return 0
        ram_cap = max(0, int((available_mb - reserve_mb) // mb_per_session))
        cpu_cap = max(0, int(cpu_count // cores_per_session))
        return min(auto_ceiling, ram_cap, cpu_cap)
    except (TypeError, ValueError, OverflowError):
        return 0


def weighted_host_admission(*, reserved_memory_mb, reserved_cpu_millis,
                            request_memory_mb, request_cpu_millis,
                            memory_envelope_mb, cpu_envelope_millis,
                            available_memory_mb, emergency_floor_mb,
                            available_cpu_millis=None,
                            emergency_cpu_floor_millis=0):
    """Return ``(allowed, reason)`` for one atomically serialized host lease.

    The durable ledger protects against concurrent processes observing the same
    free memory before any of their children materialize.  The live-memory floor
    independently protects the host from Windows/WSL or unrelated-process
    pressure, and therefore cannot be bypassed by a configured capacity override.
    """
    try:
        reserved_memory_mb = max(0, int(reserved_memory_mb))
        reserved_cpu_millis = max(0, int(reserved_cpu_millis))
        request_memory_mb = max(0, int(request_memory_mb))
        request_cpu_millis = max(0, int(request_cpu_millis))
        memory_envelope_mb = max(0, int(memory_envelope_mb))
        cpu_envelope_millis = max(0, int(cpu_envelope_millis))
        available_memory_mb = max(0, int(available_memory_mb))
        emergency_floor_mb = max(0, int(emergency_floor_mb))
        if available_cpu_millis is not None:
            available_cpu_millis = max(0, int(available_cpu_millis))
        emergency_cpu_floor_millis = max(0, int(emergency_cpu_floor_millis))
    except (TypeError, ValueError, OverflowError):
        return False, "invalid-resource-measurement"
    if available_memory_mb - request_memory_mb < emergency_floor_mb:
        return False, "emergency-memory-floor"
    if (available_cpu_millis is not None
            and available_cpu_millis - request_cpu_millis < emergency_cpu_floor_millis):
        return False, "emergency-cpu-floor"
    if reserved_memory_mb + request_memory_mb > memory_envelope_mb:
        return False, "memory-envelope"
    if reserved_cpu_millis + request_cpu_millis > cpu_envelope_millis:
        return False, "cpu-envelope"
    return True, "admitted"


def runtime_worker_limit(requested, *, cpu_count, db_pool_max,
                         hard_ceiling=8, reserve_db_connections=2):
    """Clamp generic org workers to CPU, DB, and an explicit hard ceiling."""
    try:
        requested = int(requested)
    except (TypeError, ValueError):
        raise ValueError("workers must be an integer") from None
    if requested < 1:
        raise ValueError("workers must be positive")
    cpu_cap = max(1, int(cpu_count or 1))
    db_cap = max(1, int(db_pool_max or 1) - max(0, int(reserve_db_connections)))
    hard_cap = max(1, int(hard_ceiling or 1))
    return min(requested, cpu_cap, db_cap, hard_cap)


def tool_worker_capacity(requested, browser_capacity):
    """Heavy QA/fixer threads may never outnumber admissible browser sessions."""
    try:
        requested = max(0, int(requested))
        browser_capacity = max(0, int(browser_capacity))
    except (TypeError, ValueError):
        return 0
    return min(requested, browser_capacity)


def agent_process_capacity(available_mb, cpu_count, *, reserve_mb=4096,
                           mb_per_agent=1200, cores_per_agent=1,
                           ceiling=8, override=None):
    """Host-sized cap for the combined Claude/Codex subprocess population."""
    return browser_capacity(
        available_mb, cpu_count, reserve_mb=reserve_mb,
        mb_per_session=mb_per_agent, cores_per_session=cores_per_agent,
        auto_ceiling=ceiling, override=override)


def effective_priority(priority, wait_seconds, *, aging_seconds=300):
    """Age waiting work toward priority 1 so a hot high-priority stream cannot starve it."""
    priority = max(1, int(priority))
    aging_seconds = max(1, int(aging_seconds))
    boosts = max(0, int(float(wait_seconds) // aging_seconds))
    return max(1, priority - boosts)


def fair_order(tasks, *, now, aging_seconds=300):
    """Deterministic reference order matching dispatcher admission SQL."""
    def key(task):
        created = float(task["created_at"])
        waited = max(0.0, float(now) - created)
        return (effective_priority(task.get("priority", 5), waited,
                                   aging_seconds=aging_seconds),
                created, int(task.get("id", 0)))
    return sorted(tasks, key=key)


def pressure_snapshot(*, browsers_active, browser_capacity, encoders_active,
                      encoder_capacity, db_in_use, db_capacity, workers_active,
                      worker_capacity, queued=0):
    """Neutral measurable pressure facts for health/management review."""
    resources = {}
    for name, active, capacity in (
        ("browser", browsers_active, browser_capacity),
        ("encoder", encoders_active, encoder_capacity),
        ("database", db_in_use, db_capacity),
        ("worker", workers_active, worker_capacity),
    ):
        active, capacity = max(0, int(active)), max(0, int(capacity))
        resources[name] = {
            "active": active, "capacity": capacity,
            "available": capacity - active,
            "saturated": active >= capacity,
            "oversubscribed_by": max(0, active - capacity),
            "utilization": (active / capacity if capacity else (0.0 if not active else math.inf)),
        }
    return {"resources": resources, "queued": max(0, int(queued)),
            "admit_new": all(r["available"] > 0 for r in resources.values())}
