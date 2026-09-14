#!/usr/bin/env python3
"""Read-only host/process recovery assurance and pure recovery decisions.

Nothing in this module sends a signal, starts a daemon, changes a lease, or writes
the database.  It exists so destructive recovery code can share testable identity
and ownership rules before those rules are wired into live paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_ticks: int
    boot_id: str

    def token(self) -> str:
        return f"{self.boot_id}:{self.pid}:{self.start_ticks}"


@dataclass(frozen=True)
class ProcessSnapshot:
    identity: ProcessIdentity
    ppid: int
    pgid: int
    uid: int
    cmdline: str
    state: str = "S"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    component: str
    detail: str
    remediation: str


def parse_proc_stat(text: str) -> dict:
    """Parse Linux /proc/<pid>/stat even when the parenthesized comm has spaces."""
    left = text.find("(")
    right = text.rfind(")")
    if left <= 0 or right <= left:
        raise ValueError("malformed proc stat")
    try:
        pid = int(text[:left].strip())
        tail = text[right + 1:].strip().split()
        # tail begins at field 3 (state): ppid=4, pgrp=5, starttime=22.
        return {"pid": pid, "comm": text[left + 1:right], "state": tail[0],
                "ppid": int(tail[1]), "pgid": int(tail[2]),
                "start_ticks": int(tail[19])}
    except (IndexError, ValueError) as exc:
        raise ValueError("malformed proc stat") from exc


def _boot_id(proc_root: Path) -> str:
    candidates = [proc_root / "sys/kernel/random/boot_id", Path("/proc/sys/kernel/random/boot_id")]
    for path in candidates:
        try:
            value = path.read_text().strip()
            if value:
                return value
        except OSError:
            continue
    raise FileNotFoundError("kernel boot_id unavailable")


def read_snapshot(pid: int, proc_root: Path | str = "/proc") -> ProcessSnapshot | None:
    """Return one identity-bound process snapshot; None means it vanished mid-read."""
    root = Path(proc_root)
    base = root / str(int(pid))
    try:
        before = parse_proc_stat((base / "stat").read_text())
        cmdline = (base / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        uid = (base / "status").stat().st_uid
        after = parse_proc_stat((base / "stat").read_text())
        if before["start_ticks"] != after["start_ticks"]:
            return None
        identity = ProcessIdentity(int(pid), after["start_ticks"], _boot_id(root))
        return ProcessSnapshot(identity, after["ppid"], after["pgid"], uid, cmdline, after["state"])
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError:
        return None


def scan_snapshots(proc_root: Path | str = "/proc") -> dict[int, ProcessSnapshot]:
    root = Path(proc_root)
    out = {}
    try:
        pids = [int(p.name) for p in root.iterdir() if p.name.isdigit()]
    except OSError:
        return out
    for pid in pids:
        snap = read_snapshot(pid, root)
        if snap is not None:
            out[pid] = snap
    return out


def same_process(expected: ProcessIdentity, observed: ProcessSnapshot | None) -> bool:
    return observed is not None and observed.state != "Z" and observed.identity == expected


def process_state(expected: ProcessIdentity | None, observed: ProcessSnapshot | None) -> str:
    """Distinguish dead, live, PID-reused, and legacy/unverifiable ownership."""
    if observed is None:
        return "dead"
    if expected is None:
        return "identity_unproven"
    return "same" if same_process(expected, observed) else "pid_reused"


def descendant_pids(root: ProcessIdentity, snapshots: Mapping[int, ProcessSnapshot]) -> list[int]:
    """Return identity-bound descendants deepest-first; excludes unrelated PGID peers."""
    root_now = snapshots.get(root.pid)
    if not same_process(root, root_now):
        return []
    children: dict[int, list[int]] = {}
    for pid, snap in snapshots.items():
        children.setdefault(snap.ppid, []).append(pid)
    ordered: list[int] = []

    def visit(parent: int, ancestry: frozenset[int]):
        for pid in sorted(children.get(parent, [])):
            if pid in ancestry:
                continue
            visit(pid, ancestry | {pid})
            ordered.append(pid)

    visit(root.pid, frozenset({root.pid}))
    return ordered


def cleanup_plan(root: ProcessIdentity, snapshots: Mapping[int, ProcessSnapshot]) -> list[ProcessIdentity]:
    """Capture only descendants, leaf-first. Callers must revalidate before each future signal."""
    return [snapshots[pid].identity for pid in descendant_pids(root, snapshots)]


def revalidate_cleanup(plan: Iterable[ProcessIdentity],
                       current: Mapping[int, ProcessSnapshot]) -> dict:
    """Partition a prior cleanup plan without signaling anything."""
    safe, vanished, reused = [], [], []
    for expected in plan:
        observed = current.get(expected.pid)
        state = process_state(expected, observed)
        if state == "same":
            safe.append(expected)
        elif state == "dead":
            vanished.append(expected)
        else:
            reused.append(expected)
    return {"safe": safe, "vanished": vanished, "pid_reused": reused}


def job_recovery_decision(job: Mapping, observed: ProcessSnapshot | None, *,
                          heartbeat_timeout_s: int = 180) -> dict:
    """Pure fail-closed decision for a controller job after crash/restart."""
    status = str(job.get("status") or "")
    expected = job.get("process_identity")
    beat_age = float(job.get("heartbeat_age_s") or 0)
    if status not in {"running", "pending"}:
        return {"action": "none", "reason": "terminal"}
    if expected is None:
        return {"action": "investigate", "reason": "process identity was not persisted"}
    state = process_state(expected, observed)
    if state in {"dead", "pid_reused"}:
        return {"action": "fence_and_recover", "reason": state}
    if beat_age > heartbeat_timeout_s:
        return {"action": "fence_and_recover", "reason": "heartbeat_lapsed"}
    return {"action": "keep", "reason": "identity_and_heartbeat_current"}


def gate_recovery_decision(state: Mapping) -> dict:
    """Make silent internal gates recoverable without bypassing real human authority."""
    awaiting = state.get("awaiting")
    if awaiting is None:
        return {"action": "drive", "reason": "ungated durable transition"}
    if awaiting == "fleet":
        if state.get("active_job"):
            return {"action": "observe", "reason": "internal work is durably represented"}
        return {"action": "recover", "reason": "fleet gate has no active or terminal job"}
    if awaiting in {"user_feedback", "user_approval", "consent", "payment"}:
        if state.get("decision_request_id") and state.get("notification_evidence"):
            return {"action": "wait", "reason": "human decision is durable and notified"}
        return {"action": "escalate", "reason": "human gate lacks durable request or delivery evidence"}
    return {"action": "investigate", "reason": "unknown gate kind"}


def singleton_record(identity: ProcessIdentity, command: str) -> str:
    """Portable pidfile payload for a future flock-held singleton implementation."""
    digest = hashlib.sha256(command.encode()).hexdigest()
    return json.dumps({"pid": identity.pid, "start_ticks": identity.start_ticks,
                       "boot_id": identity.boot_id, "command_sha256": digest}, sort_keys=True)


def audit_sources(root: Path | str = ROOT) -> list[Finding]:
    """Deterministically report recovery invariants missing from current source. Read-only."""
    root = Path(root)
    findings: list[Finding] = []

    def read(rel):
        try:
            return (root / rel).read_text(errors="replace")
        except OSError:
            return ""

    for rel, name in (("scripts/watchdog.sh", "watchdog"),
                      ("scripts/dispatcher.sh", "dispatcher")):
        src = read(rel)
        if "kill -0" in src and "start_ticks" not in src and "flock" not in src:
            findings.append(Finding("singleton_pid_reuse", "critical", name,
                "pidfile singleton accepts any live process with the recorded PID",
                "hold flock for daemon lifetime and persist boot_id+start_ticks for diagnosis"))
    recover = read("scripts/recover.sh")
    jobd = read("scripts/jobd.py")
    if "pgrep -f \"jobd.py serve\"" in recover and "singleton_exec.require(\"jobd\")" not in jobd:
        findings.append(Finding("jobd_non_atomic_singleton", "critical", "jobd",
            "recovery uses substring pgrep; concurrent boots can both start jobd",
            "make jobd acquire its own lifetime flock and report exact identity"))
    bridge = read("scripts/bridge.sh")
    if "pgrep -f" in recover or "pkill -f" in recover or "pgrep -f" in bridge or "pkill -f" in bridge:
        findings.append(Finding("recovery_substring_identity", "warning", "recover",
            "service recovery or listener management trusts command-line substrings",
            "use service-owned flock/identity records and readiness probes"))
    recovery = read("scripts/service_recovery.py")
    listener = read("scripts/reply_listener.py")
    required_recovery = ("singleton_exec.inspect", "_http_ready", "legacy_adoption_deferred",
                         "identity_changed_before_signal", "start_new_session=True", "ready_max_age_s")
    if any(token not in recovery for token in required_recovery) or "clear_ready" not in listener:
        findings.append(Finding("recovery_identity_or_readiness_incomplete", "warning", "recover",
            "host recovery lacks exact singleton identity, readiness, legacy deferral, or signal revalidation",
            "route host services through the identity-bound service recovery registry"))

    controller = read("scripts/loopcontroller.py")
    if "worker_pid" in controller and "worker_start_ticks" not in controller:
        findings.append(Finding("controller_pid_reuse", "critical", "controller_jobs",
            "parked jobs persist PID but not process birth identity",
            "persist boot_id+start_ticks and require an exact match before keep/cancel/reap"))
    if "status IN ('running','pending')" in controller and "_reap_dead_jobs" in controller:
        # The reaper SQL itself is narrowed to cj.status='running'; pending can survive a restart indefinitely.
        reaper = controller[controller.find("def _reap_dead_jobs"):controller.find("def liveness_selftest")]
        if "cj.status='pending'" not in reaper and "status='pending'" not in reaper:
            findings.append(Finding("pending_job_no_reaper", "critical", "controller_jobs",
                "pending jobs are counted as active but dead-job recovery only reaps running rows",
                "define pending ownership/heartbeat semantics and reconcile stale pending rows"))

    qa = read("scripts/qa/qa_agentic.py")
    if "os.kill(pid" in qa and "process_assurance.same_process" not in qa:
        findings.append(Finding("cleanup_pid_reuse", "critical", "agentic_qa",
            "descendant cleanup signals snapshot PIDs without birth-identity revalidation",
            "capture descendants by identity and re-read start_ticks immediately before each signal"))
    if "os.killpg" in qa:
        findings.append(Finding("cleanup_group_ownership", "critical", "agentic_qa",
            "cleanup signals process groups without proving every current member is owned",
            "signal identity-validated descendants leaf-first; avoid broad PGID signaling"))

    browser = read("scripts/browser_gate.py")
    if "qa:pid=" in browser and "start_ticks" not in browser:
        findings.append(Finding("browser_slot_pid_reuse", "warning", "browser_gate",
            "browser lease holders encode bare PID, so reuse delays orphan reclaim",
            "encode boot_id+start_ticks in holder metadata"))
    if "_any_browser_bridge_alive" in browser:
        findings.append(Finding("legacy_browser_global_ownership", "warning", "browser_gate",
            "any browser bridge suppresses reclaim of every legacy QA slot",
            "associate each slot with a specific owner identity/session"))

    reaper = read("scripts/clauded.py")
    if "os.kill(pid, 9)" in reaper and "start_ticks" not in reaper:
        findings.append(Finding("clauded_unowned_age_reap", "critical", "clauded",
            "all matching old headless CLIs are killed without run ownership or birth revalidation",
            "register child identity+run owner at spawn and reap only exact owned identities"))
    reap = read("scripts/reap.py")
    if "ppid == 1" in reap and "process_assurance.same_process" not in reap:
        findings.append(Finding("orphan_age_reap_unfenced", "critical", "reap",
            "agent/browser/ffmpeg cleanup uses PPID or age and signals bare PID",
            "persist owner identity, discover descendants, and revalidate process birth before signal"))

    scheduler = read("scripts/scheduler.py")
    if "next_run=now()" in scheduler and "claim_token" not in scheduler:
        findings.append(Finding("scheduler_claim_loss", "critical", "scheduler",
            "due time advances before execution with no durable running claim; ticker death can lose an occurrence",
            "persist leased scheduler run claims and recover expired claims after restart"))

    ticker = read("scripts/ticker.sh")
    expected = {"controller-resume", "tasksweep", "reap-orphans", "management-control"}
    missing = sorted(name for name in expected if f'"{name}"' not in scheduler)
    if missing:
        findings.append(Finding("recovery_schedule_missing", "critical", "scheduler",
            f"required recovery schedules absent: {', '.join(missing)}",
            "bootstrap every recovery loop idempotently"))
    if "scheduler.py\" tick" not in ticker:
        findings.append(Finding("ticker_not_driving_scheduler", "critical", "ticker",
            "ticker does not invoke scheduler tick", "wire scheduler tick into the singleton ticker"))
    return findings


def host_boot_audit(wsl_conf: Path | str = "/etc/wsl.conf",
                    boot_script: Path | str = "/usr/local/sbin/agentos-boot.sh") -> list[Finding]:
    """Read-only WSL restart-chain audit; absence is reported, never repaired."""
    findings = []
    try:
        conf = Path(wsl_conf).read_text(errors="replace")
    except OSError:
        conf = ""
    script = Path(boot_script)
    if "[boot]" not in conf or "agentos-boot.sh" not in conf:
        findings.append(Finding("wsl_boot_hook_missing", "critical", "wsl",
            "WSL boot command is not configured", "install the audited agentos boot hook"))
    if not script.is_file():
        findings.append(Finding("wsl_boot_script_missing", "critical", "wsl",
            "configured recovery script is absent", "install the versioned boot script"))
    return findings


def report(root: Path | str = ROOT, include_host=False) -> dict:
    findings = audit_sources(root)
    if include_host:
        findings.extend(host_boot_audit())
    counts = {level: sum(1 for f in findings if f.severity == level)
              for level in ("critical", "warning")}
    return {"mode": "read_only", "root": str(Path(root).resolve()), "counts": counts,
            "findings": [asdict(f) for f in findings]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--include-host", action="store_true",
                        help="also read /etc/wsl.conf and installed boot hook")
    args = parser.parse_args(argv)
    result = report(args.root, args.include_host)
    print(json.dumps(result, indent=2))
    return 1 if result["counts"]["critical"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
