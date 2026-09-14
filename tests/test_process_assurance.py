import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import process_assurance as pa


def ident(pid, start=100, boot="boot-a"):
    return pa.ProcessIdentity(pid, start, boot)


def snap(pid, ppid, *, start=100, pgid=None, boot="boot-a", cmd="worker"):
    return pa.ProcessSnapshot(ident(pid, start, boot), ppid, pgid or pid, 1000, cmd)


def stat_line(pid=42, comm="chrome helper (QA)", ppid=7, pgid=9, start=12345):
    # fields 3..22; parser must not split the parenthesized comm.
    fields = ["S", str(ppid), str(pgid)] + ["0"] * 16 + [str(start)]
    return f"{pid} ({comm}) " + " ".join(fields)


def test_proc_stat_parser_handles_spaces_parentheses_and_start_identity():
    row = pa.parse_proc_stat(stat_line())
    assert row == {"pid": 42, "comm": "chrome helper (QA)", "state": "S",
                   "ppid": 7, "pgid": 9, "start_ticks": 12345}
    with pytest.raises(ValueError, match="malformed"):
        pa.parse_proc_stat("42 broken")


def test_pid_reuse_and_wsl_reboot_are_not_process_liveness():
    expected = ident(42, start=100, boot="old-boot")
    assert pa.process_state(expected, snap(42, 1, start=101, boot="old-boot")) == "pid_reused"
    assert pa.process_state(expected, snap(42, 1, start=100, boot="new-boot")) == "pid_reused"
    assert pa.process_state(expected, None) == "dead"
    assert pa.process_state(None, snap(42, 1)) == "identity_unproven"


def test_cleanup_is_descendant_only_leaf_first_and_revalidated():
    root = ident(10)
    before = {10: snap(10, 1), 11: snap(11, 10), 12: snap(12, 11),
              13: snap(13, 10), 99: snap(99, 1, pgid=10, cmd="unrelated same-pgid")}
    plan = pa.cleanup_plan(root, before)
    assert [p.pid for p in plan] == [12, 11, 13]
    current = dict(before)
    current[11] = snap(11, 1, start=999)  # old child exited; PID was reused
    current.pop(13)                       # another child already vanished
    checked = pa.revalidate_cleanup(plan, current)
    assert [p.pid for p in checked["safe"]] == [12]
    assert [p.pid for p in checked["pid_reused"]] == [11]
    assert [p.pid for p in checked["vanished"]] == [13]


def test_controller_recovery_fences_dead_or_reused_worker_but_keeps_exact_live_one():
    expected = ident(44, 600, "boot")
    job = {"status": "running", "process_identity": expected, "heartbeat_age_s": 20}
    assert pa.job_recovery_decision(job, snap(44, 1, start=600, boot="boot"))["action"] == "keep"
    reused = pa.job_recovery_decision(job, snap(44, 1, start=601, boot="boot"))
    assert reused == {"action": "fence_and_recover", "reason": "pid_reused"}
    assert pa.job_recovery_decision(job, None)["action"] == "fence_and_recover"
    assert pa.job_recovery_decision({"status": "pending", "heartbeat_age_s": 999}, None) == {
        "action": "investigate", "reason": "process identity was not persisted"}


def test_internal_and_human_gates_never_fail_silently_or_get_conflated():
    assert pa.gate_recovery_decision({"awaiting": "fleet", "active_job": False})["action"] == "recover"
    assert pa.gate_recovery_decision({"awaiting": "fleet", "active_job": True})["action"] == "observe"
    assert pa.gate_recovery_decision({"awaiting": "user_approval"})["action"] == "escalate"
    durable = {"awaiting": "user_approval", "decision_request_id": "d-1",
               "notification_evidence": {"delivery": "accepted"}}
    assert pa.gate_recovery_decision(durable)["action"] == "wait"
    assert pa.gate_recovery_decision({"awaiting": None})["action"] == "drive"


def test_singleton_record_contains_birth_and_boot_identity_not_bare_pid():
    payload = pa.singleton_record(ident(22, 456, "boot-x"), "jobd.py serve")
    assert '"boot_id": "boot-x"' in payload
    assert '"start_ticks": 456' in payload
    assert "jobd.py serve" not in payload


def test_current_source_audit_keeps_unowned_gaps_red_and_proves_wired_fixes_green():
    findings = pa.audit_sources(ROOT)
    codes = {f.code for f in findings}
    assert not codes, [(f.code, f.detail) for f in findings]
    fixed = {"jobd_non_atomic_singleton", "controller_pid_reuse", "pending_job_no_reaper",
             "cleanup_pid_reuse", "cleanup_group_ownership", "clauded_unowned_age_reap",
             "scheduler_claim_loss", "orphan_age_reap_unfenced", "singleton_pid_reuse",
             "browser_slot_pid_reuse", "legacy_browser_global_ownership"}
    assert not (fixed & codes), sorted(fixed & codes)
    assert not ({"recovery_schedule_missing", "ticker_not_driving_scheduler"} & codes), (
        "the cadence chain itself is present; its ownership/recovery semantics are the gaps")
    assert all(f.remediation for f in findings)


def test_wsl_boot_audit_is_deterministic_without_touching_host(tmp_path):
    absent = pa.host_boot_audit(tmp_path / "missing.conf", tmp_path / "missing.sh")
    assert {f.code for f in absent} == {"wsl_boot_hook_missing", "wsl_boot_script_missing"}
    conf = tmp_path / "wsl.conf"
    hook = tmp_path / "agentos-boot.sh"
    conf.write_text("[boot]\ncommand=/usr/local/sbin/agentos-boot.sh\n")
    hook.write_text("#!/bin/sh\n")
    assert pa.host_boot_audit(conf, hook) == []
