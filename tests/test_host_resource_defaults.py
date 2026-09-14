import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_resource_audit as hra
import watchdog


def test_repo_has_container_pid_memory_cpu_and_database_limits():
    assert hra.audit_repo(ROOT) == []


def test_host_snapshot_parser_is_deterministic(tmp_path):
    (tmp_path / "meminfo").write_text(
        "MemTotal: 16384000 kB\nMemAvailable: 8192000 kB\nSwapTotal: 4194304 kB\nSwapFree: 3145728 kB\n")
    (tmp_path / "cpuinfo").write_text("processor\t: 0\nprocessor\t: 1\n")
    (tmp_path / "loadavg").write_text("1.0 2.0 3.0 4/500 99\n")
    assert hra.host_snapshot(tmp_path) == {
        "memory_total_mb": 16000, "memory_available_mb": 8000,
        "swap_total_mb": 4096, "swap_used_mb": 1024,
        "logical_cpus": 2, "tasks": 500}


def test_pressure_policy_detects_swap_psi_pid_and_connections():
    issues = watchdog._pressure_issues(
        3_000_000, 16_000_000, 0, 0, swap_used_kb=3_500_000,
        swap_total_kb=4_000_000, host_pids=5000, memory_psi_avg10=25,
        cpu_psi_avg10=60)
    assert {i["sig"] for i in issues} >= {
        "host:swap", "host:pids", "host:memory-psi", "host:cpu-psi"}
    db = watchdog._db_connection_issues(92, 100, reserved=3, waiting=2)
    assert {i["sig"] for i in db} == {"postgres:connections", "postgres:connection-waits"}
    assert next(i for i in db if i["sig"] == "postgres:connections")["level"] == "crit"


def test_healthy_resource_snapshot_has_no_pressure_alerts():
    assert watchdog._pressure_issues(
        12_000_000, 16_000_000, 0, 0, swap_used_kb=0,
        swap_total_kb=4_000_000, host_pids=500,
        memory_psi_avg10=0, cpu_psi_avg10=0) == []
    assert watchdog._db_connection_issues(15, 100, reserved=3, waiting=0) == []


def test_wsl_external_state_reports_missing_config_without_writing(tmp_path):
    users = tmp_path / "Users"
    (users / "alex").mkdir(parents=True)
    assert hra.wsl_external_state(users)["explicit_config_present"] is False
    (users / "alex" / ".wslconfig").write_text("[wsl2]\nmemory=16GB\n")
    state = hra.wsl_external_state(users)
    assert state == {"explicit_config_present": True, "config_count": 1,
                     "requires_wsl_shutdown_to_apply": True}
