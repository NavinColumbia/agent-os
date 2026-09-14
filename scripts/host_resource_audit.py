#!/usr/bin/env python3
"""Read-only audit of host/container/database resource safety defaults."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LIMITS = {
    "postgres/docker-compose.yml": ("mem_limit:", "memswap_limit:", "cpus:", "pids_limit:", "init: true",
                                    "shm_size:",
                                    "max_connections=", "temp_file_limit="),
    "ntfy/docker-compose.yml": ("mem_limit:", "memswap_limit:", "cpus:", "pids_limit:", "init: true"),
    "cerbos/docker-compose.yml": ("mem_limit:", "memswap_limit:", "cpus:", "pids_limit:", "init: true"),
}


def parse_meminfo(text):
    values = {}
    for line in str(text).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].rstrip(":") in {
                "MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            values[parts[0].rstrip(":")] = int(parts[1])
    return values


def host_snapshot(proc_root="/proc"):
    root = Path(proc_root)
    try:
        values = parse_meminfo((root / "meminfo").read_text())
        cpu_count = sum(line.startswith("processor")
                        for line in (root / "cpuinfo").read_text().splitlines())
        tasks = int((root / "loadavg").read_text().split()[3].split("/")[1])
    except Exception:
        values, cpu_count, tasks = {}, 0, 0
    return {"memory_total_mb": values.get("MemTotal", 0)//1024,
            "memory_available_mb": values.get("MemAvailable", 0)//1024,
            "swap_total_mb": values.get("SwapTotal", 0)//1024,
            "swap_used_mb": (values.get("SwapTotal", 0)-values.get("SwapFree", 0))//1024,
            "logical_cpus": max(0, cpu_count), "tasks": tasks}


def audit_repo(root=ROOT):
    root = Path(root)
    findings = []
    for rel, required in LIMITS.items():
        try:
            source = (root / rel).read_text()
        except OSError:
            findings.append({"severity": "critical", "component": rel, "detail": "compose file missing"})
            continue
        for marker in required:
            if marker not in source:
                findings.append({"severity": "critical", "component": rel,
                                 "detail": f"resource default missing: {marker}"})
    dbpool = (root / "scripts/dbpool.py").read_text(errors="replace")
    if 'AOS_DB_POOL_MAX", 8' not in dbpool or 'AOS_DB_DIRECT_MAX", 1' not in dbpool:
        findings.append({"severity": "warning", "component": "dbpool",
                         "detail": "connection defaults exceed the single-host envelope"})
    watchdog = (root / "scripts/watchdog.py").read_text(errors="replace")
    for marker in ("_db_connection_issues", "memory_psi_avg10", "swap_used_kb", "host_pids"):
        if marker not in watchdog:
            findings.append({"severity": "warning", "component": "watchdog",
                             "detail": f"pressure signal is not monitored: {marker}"})
    if not (root / "deploy/wslconfig.example").is_file():
        findings.append({"severity": "warning", "component": "wsl",
                         "detail": "no explicit Windows-side WSL envelope example"})
    return findings


def compose_validate(root=ROOT):
    results = {}
    for rel in LIMITS:
        path = Path(root) / rel
        proc = subprocess.run(["docker", "compose", "-f", str(path), "config", "-q"],
                              capture_output=True, text=True, timeout=20)
        results[rel] = {"valid": proc.returncode == 0, "detail": (proc.stderr or "")[:300]}
    return results


def wsl_external_state(users_root="/mnt/c/Users"):
    try:
        configs = sorted(str(p) for p in Path(users_root).glob("*/.wslconfig") if p.is_file())
    except OSError:
        configs = []
    return {"explicit_config_present": bool(configs), "config_count": len(configs),
            "requires_wsl_shutdown_to_apply": True}


def docker_runtime_limits():
    """Inspect effective live limits without exposing container environment values."""
    result = {}
    for name in ("agentos-postgres", "agentos-ntfy", "agentos-cerbos"):
        try:
            proc = subprocess.run(
                ["docker", "inspect", name, "--format", "{{json .HostConfig}}"],
                capture_output=True, text=True, timeout=20)
            data = json.loads(proc.stdout) if proc.returncode == 0 else {}
            result[name] = {"memory_bytes": data.get("Memory"),
                            "memory_swap_bytes": data.get("MemorySwap"),
                            "nano_cpus": data.get("NanoCpus"),
                            "pids_limit": data.get("PidsLimit")}
        except Exception as exc:
            result[name] = {"error": str(exc)[:160]}
    return result


def postgres_runtime_settings():
    names = ("max_connections", "shared_buffers", "effective_cache_size", "work_mem",
             "maintenance_work_mem", "temp_file_limit")
    query = ("select name||'='||setting||coalesce(unit,'') from pg_settings where name in (" +
             ",".join("'%s'" % name for name in names) + ") order by name")
    try:
        proc = subprocess.run(["docker", "exec", "agentos-postgres", "psql", "-X", "-U",
                               "agentos", "-d", "agentos", "-At", "-c", query],
                              capture_output=True, text=True, timeout=20)
        return {line.split("=", 1)[0]: line.split("=", 1)[1]
                for line in proc.stdout.splitlines() if "=" in line} if proc.returncode == 0 else {
                    "error": (proc.stderr or "unavailable")[:160]}
    except Exception as exc:
        return {"error": str(exc)[:160]}


def report(root=ROOT, validate=False):
    findings = audit_repo(root)
    result = {"mode": "read_only", "host": host_snapshot(),
              "wsl_external_state": wsl_external_state(), "findings": findings,
              "external": ["Windows .wslconfig takes effect only after wsl --shutdown",
                           "Docker container limits take effect only after normal container recreation",
                           "Postgres startup defaults take effect only after normal container recreation"]}
    if validate:
        result["compose"] = compose_validate(root)
        result["live_container_limits"] = docker_runtime_limits()
        result["live_postgres_settings"] = postgres_runtime_settings()
    return result


if __name__ == "__main__":
    print(json.dumps(report(validate=True), indent=2))
