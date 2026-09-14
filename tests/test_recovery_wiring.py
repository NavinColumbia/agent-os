import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import clauded
import process_assurance as pa
import singleton_exec
import service_recovery
import scheduler
import reap


def test_service_supervisor_uses_only_configured_known_services(monkeypatch):
    import responder

    monkeypatch.setattr(responder, "DAEMONS", {
        "console": "console", "worker": "jobd", "optional": "not-installed"})
    monkeypatch.setattr(service_recovery, "_specs", lambda: {
        "console": object(), "jobd": object(), "watchdog": object()})

    assert service_recovery.supervised_names() == ["console", "jobd", "watchdog"]


def test_deferred_evidence_publisher_is_supervised_and_cold_boot_recovered():
    import responder

    assert responder.DAEMONS["evidence-publisher"] == "evidence-publisher"
    service = service_recovery._specs()["evidence-publisher"]
    assert service.command[-2:] == ("serve", "5")
    assert service.ready_kind == "record" and service.ready_max_age_s == 60
    recover = (ROOT / "scripts" / "recover.sh").read_text()
    assert 'recover_service evidence-publisher "deferred QA evidence publisher"' in recover


def test_public_assurance_boundary_is_supervised_and_cold_boot_recovered():
    import responder

    assert responder.DAEMONS["assurance"] == "assurance"
    service = service_recovery._specs()["assurance"]
    assert Path(service.command[-3]).name == "assurance_public.py"
    assert service.command[-2:] == ("serve", "8100")
    assert service.ready_target == "http://127.0.0.1:8100/health"
    recover = (ROOT / "scripts" / "recover.sh").read_text()
    assert 'recover_service assurance "public Release Assurance intake"' in recover


def test_owned_agent_fails_closed_when_exact_registration_is_unavailable(monkeypatch):
    class Proc:
        pid = 8181
        killed = waited = False

        def kill(self):
            self.killed = True

        def wait(self):
            self.waited = True

    proc = Proc()
    monkeypatch.setattr(clauded.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(clauded, "register_owned", lambda *_args: None)
    with pytest.raises(clauded.OwnershipUnavailable):
        clauded.run_owned(["codex", "exec", "task"], owner="test")
    assert proc.killed is True and proc.waited is True


def test_streaming_agent_fails_closed_when_exact_registration_is_unavailable(monkeypatch, tmp_path):
    import factory

    class Proc:
        pid = 8282
        stdout = []
        killed = waited = False

        def kill(self):
            self.killed = True

        def wait(self):
            self.waited = True
            return -9

    proc = Proc()
    monkeypatch.setattr(factory.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(clauded, "register_owned", lambda *_args: None)
    with pytest.raises(clauded.OwnershipUnavailable):
        factory._run_once_stream("assistant", tmp_path, "hello", 30, {}, "model", lambda _t: None,
                                 light=True)
    assert proc.killed is True and proc.waited is True


def test_streaming_cancel_cleans_exact_descendants_before_root(monkeypatch, tmp_path):
    import factory

    events = []

    class Proc:
        pid = 8383
        stdout = ['{"type":"stream_event","event":{"type":"content_block_delta",'
                  '"delta":{"type":"text_delta","text":"hello"}}}\n']
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            events.append("kill-root")
            self.returncode = -9

        def wait(self):
            events.append("wait-root")
            return self.returncode or 0

    proc = Proc()
    record = tmp_path / "owned-stream.json"
    record.write_text("{}")
    monkeypatch.setattr(factory.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(clauded, "register_owned", lambda *_args: record)
    monkeypatch.setattr(clauded, "_signal_registered_descendants",
                        lambda _record: events.append("kill-descendants") or [8384])
    monkeypatch.setattr(clauded, "unregister_owned", lambda _record: events.append("unregister"))

    result = factory._run_once_stream(
        "assistant", tmp_path, "hello", 30, {}, "model", lambda _t: None,
        cancel=lambda: True, light=True)

    assert result[-1] is True
    assert events == ["kill-descendants", "kill-root", "wait-root", "unregister"]


def test_singleton_lock_is_atomic_and_identity_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setattr(singleton_exec, "LOCK_DIR", tmp_path)
    first = singleton_exec.acquire("test-daemon")
    assert first is not None
    assert singleton_exec.acquire("test-daemon") is None
    payload = json.loads((tmp_path / "test-daemon.lock").read_text())
    assert payload["pid"] == os.getpid()
    assert payload.get("boot_id") and payload.get("start_ticks")
    first.close()


def test_python_daemon_seals_intended_exec_lock_from_later_children(tmp_path, monkeypatch):
    monkeypatch.setattr(singleton_exec, "LOCK_DIR", tmp_path)
    lock = singleton_exec.acquire("seal-daemon")
    try:
        assert lock is not None and os.get_inheritable(lock.fileno()) is True
        assert singleton_exec.require("seal-daemon") is True
        assert os.get_inheritable(lock.fileno()) is False
    finally:
        lock.close()


def test_singleton_readiness_is_bound_to_lock_birth_and_exact_command(tmp_path, monkeypatch):
    monkeypatch.setattr(singleton_exec, "LOCK_DIR", tmp_path)
    current = pa.ProcessSnapshot(pa.ProcessIdentity(os.getpid(), 777, "boot-z"), 1, os.getpid(), 1000,
                                 "python daemon.py")
    monkeypatch.setattr(singleton_exec, "read_snapshot", lambda _pid, *a: current)
    monkeypatch.setattr(singleton_exec, "_argv", lambda _pid, *a: ["python", "daemon.py"])
    lock = singleton_exec.acquire("ready-daemon")
    try:
        assert singleton_exec.inspect("ready-daemon") ["owned"] is True
        assert singleton_exec.inspect("ready-daemon", require_ready=True)["ready"] is False
        assert singleton_exec.mark_ready("ready-daemon", {"probe": "passed"}) is True
        assert singleton_exec.inspect("ready-daemon", require_ready=True)["ready"] is True
        ready = json.loads((tmp_path / "ready-daemon.ready").read_text())
        ready["identity"] = "boot-z:999:777"
        (tmp_path / "ready-daemon.ready").write_text(json.dumps(ready))
        stale = singleton_exec.inspect("ready-daemon", require_ready=True)
        assert stale["owned"] is True and stale["ready"] is False and stale["reason"] == "stale_readiness"
        singleton_exec.mark_ready("ready-daemon")
        ready = json.loads((tmp_path / "ready-daemon.ready").read_text())
        ready["ready_monotonic"] = 1
        (tmp_path / "ready-daemon.ready").write_text(json.dumps(ready))
        assert singleton_exec.inspect("ready-daemon", require_ready=True,
                                      max_ready_age_s=1)["reason"] == "readiness_expired"
        singleton_exec.mark_ready("ready-daemon")
        assert singleton_exec.clear_ready("ready-daemon") is True
        assert singleton_exec.inspect("ready-daemon", require_ready=True)["reason"] == "readiness_missing"
        monkeypatch.setattr(singleton_exec, "_argv", lambda _pid, *a: ["python", "other.py"])
        assert singleton_exec.inspect("ready-daemon")["reason"] == "command_mismatch"
    finally:
        lock.close()


def test_recovery_decision_never_duplicates_owned_or_legacy_processes():
    decide = service_recovery.recovery_decision
    assert decide(owned=True, ready=True, external_ready=True, legacy=[]) == "healthy"
    assert decide(owned=True, ready=False, external_ready=False, legacy=[]) == "owned_not_ready"
    assert decide(owned=False, ready=False, external_ready=False,
                  legacy=[{"pid": 4, "start_ticks": 8, "boot_id": "b"}]) == "legacy_adoption_deferred"
    assert decide(owned=False, ready=False, external_ready=True, legacy=[]) == "unowned_readiness_deferred"
    assert decide(owned=False, ready=False, external_ready=False, legacy=[]) == "start"


def test_legacy_deferral_requires_exact_repo_script_and_argv(monkeypatch):
    service = service_recovery._specs()["api"]
    exact = pa.ProcessSnapshot(pa.ProcessIdentity(41, 10, "boot"), 1, 41, 1000, "ignored")
    lookalike = pa.ProcessSnapshot(pa.ProcessIdentity(42, 11, "boot"), 1, 42, 1000, "ignored")
    monkeypatch.setattr(service_recovery.process_assurance, "scan_snapshots",
                        lambda _root: {41: exact, 42: lookalike})
    monkeypatch.setattr(service_recovery.singleton_exec, "_argv", lambda pid, _root: {
        41: [service.command[0], service.command[1], "serve", "8090"],
        42: [service.command[0], "/tmp/api.py", "serve", "8090"],
    }[pid])
    monkeypatch.setattr(service_recovery, "_process_cwd", lambda pid, _root: ROOT if pid == 41 else Path("/tmp"))
    found = service_recovery._legacy_identities(service, "/fake-proc")
    assert found == [{"pid": 41, "start_ticks": 10, "boot_id": "boot"}]


def test_legacy_listener_allows_python_version_name_but_requires_exact_u_script_and_cwd():
    service = service_recovery._specs()["reply-listener"]
    assert service_recovery._argv_matches(
        ["python3", "-u", "scripts/reply_listener.py"], list(service.command), ROOT, ROOT)
    assert not service_recovery._argv_matches(
        ["python3", "-u", "/tmp/reply_listener.py"], list(service.command), Path("/tmp"), ROOT)
    assert not service_recovery._argv_matches(
        ["python3", "scripts/reply_listener.py"], list(service.command), ROOT, ROOT)


def test_recover_and_bridge_have_no_substring_or_broad_kill_management():
    recover = (ROOT / "scripts/recover.sh").read_text()
    bridge = (ROOT / "scripts/bridge.sh").read_text()
    for source in (recover, bridge):
        assert "pgrep -f" not in source
        assert "pkill -f" not in source
    assert "service_recovery.py" in recover and "service_recovery.py" in bridge
    assert "legacy/unowned but was left untouched" in recover


def test_private_tmp_supervisor_uses_shared_singleton_registry_and_service_logs():
    """Operator recovery and PrivateTmp systemd supervision must observe the same ownership generation."""
    singleton_source = (ROOT / "scripts/singleton_exec.py").read_text()
    unit = (ROOT / "deploy/agentos-supervisor.service.template").read_text()
    assert "PrivateTmp=true" in unit
    assert 'ROOT / ".runtime" / "singletons"' in singleton_source
    assert "/tmp/agentos-singletons" not in singleton_source
    for name in ("ticker", "watchdog", "dispatcher"):
        source = (ROOT / "scripts" / f"{name}.sh").read_text()
        assert 'LOCKFILE="$RUNTIME_DIR/' in source
        assert 'LOG_DIR="$ROOT/logs/services"' in source
    for service in service_recovery._specs().values():
        if service.name == "reply-listener":
            continue
        assert Path(service.log).is_relative_to(ROOT / "logs" / "services")


def test_singleton_legacy_detection_only_accepts_older_exact_argv(monkeypatch):
    current = pa.ProcessSnapshot(pa.ProcessIdentity(50, 500, "boot"), 1, 50, 1000,
                                 "python jobd.py serve")
    older = pa.ProcessSnapshot(pa.ProcessIdentity(40, 400, "boot"), 1, 40, 1000,
                               "python /repo/scripts/jobd.py serve 15")
    wrong_repo = pa.ProcessSnapshot(pa.ProcessIdentity(42, 350, "boot"), 1, 42, 1000,
                                    "python /other/scripts/jobd.py serve 15")
    newer = pa.ProcessSnapshot(pa.ProcessIdentity(60, 600, "boot"), 1, 60, 1000,
                               "python /repo/scripts/jobd.py serve 15")
    wrapper = pa.ProcessSnapshot(pa.ProcessIdentity(30, 300, "boot"), 1, 30, 1000,
                                 "bash -c echo jobd.py serve")
    monkeypatch.setattr(singleton_exec.os, "getpid", lambda: 50)
    monkeypatch.setattr(singleton_exec, "read_snapshot", lambda _pid: current)
    monkeypatch.setattr(singleton_exec, "scan_snapshots",
                        lambda _root: {30: wrapper, 40: older, 42: wrong_repo, 50: current, 60: newer})
    argv = {
        30: ["bash", "-c", "echo", "jobd.py", "serve"],
        40: ["python", "/repo/scripts/jobd.py", "serve", "15"],
        42: ["python", "/other/scripts/jobd.py", "serve", "15"],
        50: ["python", "jobd.py", "serve"],
        60: ["python", "/repo/scripts/jobd.py", "serve", "15"],
    }
    monkeypatch.setattr(singleton_exec, "_argv", lambda pid, *a: argv[pid])
    monkeypatch.setattr(singleton_exec, "_cwd", lambda pid, _root: Path("/repo") if pid != 42 else Path("/other"))
    assert singleton_exec.older_matching(argv[40], "/repo", "/fake-proc") == [40]


def test_shell_child_explicitly_drops_inherited_singleton_lock(tmp_path):
    """Old behavior kept this lock until sleep exited; the explicit child redirection releases it."""
    env = os.environ.copy()
    env["AOS_SINGLETON_DIR"] = str(tmp_path)
    wrapper = ROOT / "scripts" / "singleton_exec.py"
    py = ROOT / ".venv" / "bin" / "python"
    target = 'fd="$AOS_SINGLETON_FD_PROOF_FD"; sleep 2 9>&- {fd}>&- &'
    first = subprocess.run([str(py), str(wrapper), "run", "fd-proof", "bash", "-c", target],
                           env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=1)
    assert first.returncode == 0
    probe = subprocess.run([str(py), str(wrapper), "run", "fd-proof", "bash", "-c",
                            "echo PROBE_RAN"], env=env, capture_output=True, text=True, timeout=1)
    assert probe.stdout.strip() == "PROBE_RAN"
    for name in ("ticker", "watchdog", "dispatcher"):
        source = (ROOT / "scripts" / f"{name}.sh").read_text()
        assert '"$@" 9>&- {SINGLETON_FD}>&-' in source
        assert "run_child sleep" in source
        assert "CHILD_PID=$!" in source
        assert 'wait "$CHILD_PID"' in source


def test_shell_daemon_term_trap_exits_instead_of_only_cleaning_pidfile():
    """TERM must end the loop; a cleanup-only trap let stopped daemons continue forever."""
    for name in ("ticker", "watchdog", "dispatcher"):
        source = (ROOT / "scripts" / f"{name}.sh").read_text()
        assert 'kill -TERM "$pid"' in source
        assert "exit 0" in source
        assert "trap cleanup EXIT" in source
        assert "trap stop INT TERM" in source
        assert "trap cleanup EXIT INT TERM" not in source


def test_stop_binds_generation_with_pidfd_and_never_uses_numeric_kill(monkeypatch):
    identity = pa.ProcessIdentity(321, 44, "boot-x")
    monkeypatch.setattr(service_recovery.singleton_exec, "inspect", lambda _name: {
        "owned": True,
        "record": {"pid": identity.pid, "start_ticks": identity.start_ticks,
                   "boot_id": identity.boot_id},
    })
    monkeypatch.setattr(service_recovery.process_assurance, "read_snapshot", lambda _pid:
                        pa.ProcessSnapshot(identity, 1, identity.pid, 1000, "daemon"))
    monkeypatch.setattr(service_recovery.process_assurance, "scan_snapshots", lambda: {})
    monkeypatch.setattr(service_recovery.os, "pidfd_open", lambda pid, flags: 88)
    sent, closed = [], []
    monkeypatch.setattr(service_recovery.signal, "pidfd_send_signal",
                        lambda fd, sig: sent.append((fd, sig)))
    monkeypatch.setattr(service_recovery, "_wait_gone", lambda *_args: True)
    monkeypatch.setattr(service_recovery.os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(service_recovery.os, "kill",
                        lambda *_args: (_ for _ in ()).throw(AssertionError("numeric PID signal used")))
    result = service_recovery.stop("jobd")
    assert result["stopped"] is True
    assert sent == [(88, signal.SIGTERM)] and closed == [88]


def test_stop_escalates_same_pidfd_generation_when_term_is_ignored(monkeypatch):
    identity = pa.ProcessIdentity(654, 87, "boot-y")
    snapshot = pa.ProcessSnapshot(identity, 1, identity.pid, 1000, "daemon")
    monkeypatch.setattr(service_recovery.singleton_exec, "inspect", lambda _name: {
        "owned": True,
        "record": {"pid": identity.pid, "start_ticks": identity.start_ticks,
                   "boot_id": identity.boot_id},
    })
    monkeypatch.setattr(service_recovery.process_assurance, "read_snapshot", lambda _pid: snapshot)
    monkeypatch.setattr(service_recovery.process_assurance, "scan_snapshots", lambda: {})
    monkeypatch.setattr(service_recovery.os, "pidfd_open", lambda _pid, _flags: 99)
    monkeypatch.setattr(service_recovery.os, "close", lambda _fd: None)
    sent = []
    monkeypatch.setattr(service_recovery.signal, "pidfd_send_signal",
                        lambda fd, sig: sent.append((fd, sig)))
    waits = iter((False, True))
    monkeypatch.setattr(service_recovery, "_wait_gone", lambda *_args: next(waits))

    result = service_recovery.stop("ticker")

    assert result["stopped"] is True and result["forced"] is True
    assert sent == [(99, signal.SIGTERM), (99, signal.SIGKILL)]


def test_replace_adopts_only_one_revalidated_exact_legacy_generation(monkeypatch):
    legacy = {"pid": 701, "start_ticks": 91, "boot_id": "boot-r"}
    monkeypatch.setattr(service_recovery, "status", lambda _name: {
        "name": "ticker", "state": "legacy_adoption_deferred", "owned": False,
        "ready": False, "legacy": [legacy],
    })
    monkeypatch.setattr(service_recovery, "_legacy_identities", lambda *_args: [legacy])
    stopped = []
    monkeypatch.setattr(service_recovery, "_stop_generation", lambda name, identity: (
        stopped.append((name, identity.token())) or {"stopped": True, "forced": False}))
    monkeypatch.setattr(service_recovery, "ensure", lambda name: {
        "name": name, "state": "healthy", "owned": True, "ready": True,
    })

    result = service_recovery.replace("ticker")

    assert result["state"] == "healthy" and result["replaced"] is True
    assert stopped == [("ticker", "boot-r:701:91")]


def test_repair_replaces_only_an_owned_unready_generation(monkeypatch):
    monkeypatch.setattr(service_recovery, "status", lambda _name: {
        "name": "watchdog", "state": "owned_not_ready", "owned": True, "ready": False,
    })
    called = []
    monkeypatch.setattr(service_recovery, "replace", lambda name: (
        called.append(name) or {"name": name, "state": "healthy"}))
    assert service_recovery.repair("watchdog")["state"] == "healthy"
    assert called == ["watchdog"]


def test_ensure_waits_through_transient_concurrent_start_legacy_view(monkeypatch):
    states = iter([
        {"name": "ticker", "state": "start", "owned": False, "ready": False, "legacy": []},
        {"name": "ticker", "state": "legacy_adoption_deferred", "owned": False,
         "ready": False, "legacy": [{"pid": 4}]},
        {"name": "ticker", "state": "healthy", "owned": True, "ready": True, "legacy": []},
    ])
    monkeypatch.setattr(service_recovery, "status", lambda _name: next(states))
    monkeypatch.setattr(service_recovery.subprocess, "Popen", lambda *_a, **_k: object())
    result = service_recovery.ensure("ticker")
    assert result["state"] == "healthy" and result["started"] is True


def test_stop_fails_closed_without_pidfd_signal(monkeypatch):
    identity = pa.ProcessIdentity(321, 44, "boot-x")
    monkeypatch.setattr(service_recovery.singleton_exec, "inspect", lambda _name: {
        "owned": True,
        "record": {"pid": identity.pid, "start_ticks": identity.start_ticks,
                   "boot_id": identity.boot_id},
    })
    monkeypatch.delattr(service_recovery.signal, "pidfd_send_signal", raising=False)
    result = service_recovery.stop("jobd")
    assert result == {"name": "jobd", "stopped": False, "reason": "race_safe_signal_unavailable"}


def test_listener_readiness_and_boot_database_barrier_are_bounded():
    listener = service_recovery._specs()["reply-listener"]
    assert listener.ready_max_age_s == 120
    source = (ROOT / "scripts" / "reply_listener.py").read_text()
    refresh = source.index('singleton_exec.mark_ready("reply-listener", {"connected_url": url, "stream": "active"})')
    skip_blank = source.index("if not line:", refresh)
    assert refresh < skip_blank
    recover = (ROOT / "scripts" / "recover.sh").read_text()
    barrier = recover.index("POSTGRES_READY=0")
    first_db_service = recover.index('recover_service api "API on 127.0.0.1:8090"')
    assert barrier < first_db_service
    assert "DB-dependent host services were not started" in recover


def test_claude_reaper_never_kills_matching_but_unregistered_process(monkeypatch):
    owned_id = pa.ProcessIdentity(101, 5, "boot")
    owned_snap = pa.ProcessSnapshot(owned_id, 1, 101, 1000, "claude -p x --output-format json")
    record = Path("/tmp/nonexistent-owned-record")
    monkeypatch.setattr(clauded, "disabled", lambda: False)
    monkeypatch.setattr(clauded, "claude_procs", lambda: [(101, 9999), (202, 9999)])
    monkeypatch.setattr(clauded, "_owned_records", lambda: {101: (owned_id, record, "run-1")})
    monkeypatch.setattr(clauded.process_assurance, "read_snapshot",
                        lambda pid: owned_snap if pid == 101 else None)
    monkeypatch.setattr(clauded.process_assurance, "scan_snapshots", lambda: {101: owned_snap})
    killed = []
    monkeypatch.setattr(clauded.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    result = clauded.reap(max_age_s=10)
    assert [pid for pid, _ in killed] == [101]
    assert result["unowned_stale"] == [202]


def test_owned_codex_tree_is_reaped_leaf_first_without_touching_same_group_peer(
        monkeypatch, tmp_path):
    root_id = pa.ProcessIdentity(301, 10, "boot-a")
    child_id = pa.ProcessIdentity(302, 11, "boot-a")
    peer_id = pa.ProcessIdentity(399, 12, "boot-a")
    root = pa.ProcessSnapshot(root_id, 1, 301, 1000, "codex exec task")
    child = pa.ProcessSnapshot(child_id, 301, 301, 1000, "node mcp-server")
    peer = pa.ProcessSnapshot(peer_id, 1, 301, 1000, "unrelated same-pgid")
    snapshots = {301: root, 302: child, 399: peer}
    record = tmp_path / "owned.json"
    record.write_text(json.dumps({"pid": 301, "start_ticks": 10, "boot_id": "boot-a"}))
    monkeypatch.setattr(clauded, "disabled", lambda: False)
    monkeypatch.setattr(clauded, "claude_procs", lambda: [(301, 9999), (777, 9999)])
    monkeypatch.setattr(clauded, "_owned_records", lambda: {301: (root_id, record, "qa-codex")})
    monkeypatch.setattr(clauded.process_assurance, "scan_snapshots", lambda: snapshots)
    monkeypatch.setattr(clauded.process_assurance, "read_snapshot", lambda pid: snapshots.get(pid))
    killed = []
    monkeypatch.setattr(clauded.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    result = clauded.reap(max_age_s=10)

    assert [pid for pid, _sig in killed] == [302, 301]
    assert 399 not in [pid for pid, _sig in killed]
    assert result["pids"] == [301]
    assert result["unowned_stale"] == [777]


def test_dead_browser_parent_reaps_exact_registered_tree_and_preserves_peers(
        monkeypatch, tmp_path):
    # Use a PID outside the kernel's supported range so the real CI host can
    # never make the synthetic "dead" owner look alive by coincidence.
    owner_id = pa.ProcessIdentity(987654321, 5, "boot-a")
    root_id = pa.ProcessIdentity(411, 10, "boot-a")
    child_id = pa.ProcessIdentity(412, 11, "boot-a")
    peer_id = pa.ProcessIdentity(499, 12, "boot-a")
    root = pa.ProcessSnapshot(root_id, 1, 411, 1000, "node /repo/scripts/qa/browser_bridge.js")
    child = pa.ProcessSnapshot(child_id, 411, 411, 1000, "chromium --remote-debugging-pipe")
    peer = pa.ProcessSnapshot(peer_id, 1, 411, 1000, "unrelated same-pgid")
    snapshots = {411: root, 412: child, 499: peer}  # owner 410 is gone
    record = tmp_path / "owned-browser.json"
    record.write_text(json.dumps({"pid": 411, "start_ticks": 10, "boot_id": "boot-a",
                                  "owner": "qa-browser:tenant:run",
                                  "owner_pid": owner_id.pid,
                                  "owner_start_ticks": owner_id.start_ticks,
                                  "owner_boot_id": owner_id.boot_id}))
    monkeypatch.setattr(clauded, "_owned_records",
                        lambda: {411: (root_id, record, "qa-browser:tenant:run")})
    monkeypatch.setattr(clauded.process_assurance, "scan_snapshots", lambda: snapshots)
    monkeypatch.setattr(clauded.process_assurance, "read_snapshot", lambda pid: snapshots.get(pid))
    killed = []
    monkeypatch.setattr(clauded.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    result = clauded.reap_owned_orphans("qa-browser:")

    assert [pid for pid, _sig in killed] == [412, 411]
    assert 499 not in [pid for pid, _sig in killed]
    assert result["pids"] == [411] and result["reaped"] == 1
    assert not record.exists()


def test_browser_orphan_registry_preserves_reused_root_pid(monkeypatch, tmp_path):
    expected = pa.ProcessIdentity(511, 20, "boot-a")
    reused = pa.ProcessSnapshot(pa.ProcessIdentity(511, 21, "boot-a"), 1, 511, 1000,
                                "node /repo/scripts/qa/browser_bridge.js")
    record = tmp_path / "reused-browser.json"
    record.write_text(json.dumps({"pid": 511, "start_ticks": 20, "boot_id": "boot-a",
                                  "owner": "qa-browser:tenant:run"}))
    monkeypatch.setattr(clauded, "_owned_records",
                        lambda: {511: (expected, record, "qa-browser:tenant:run")})
    monkeypatch.setattr(clauded.process_assurance, "read_snapshot", lambda _pid: reused)
    monkeypatch.setattr(clauded.os, "kill", lambda *_args: pytest.fail("reused PID was signalled"))
    assert clauded.reap_owned_orphans("qa-browser:")["reaped"] == 0


def test_clauded_recognizes_batch_codex_but_not_interactive_or_app_server():
    assert clauded._is_headless_codex("/usr/local/bin/codex exec --json do-work")
    assert not clauded._is_headless_codex("codex")
    assert not clauded._is_headless_codex("codex app-server")


def test_migration_and_sources_wire_birth_identity_claims_and_no_group_kill():
    migration = (ROOT / "postgres/initdb/66-recovery-claims.sql").read_text()
    controller = (ROOT / "scripts/loopcontroller.py").read_text()
    qa = (ROOT / "scripts/qa/qa_agentic.py").read_text()
    scheduler = (ROOT / "scripts/scheduler.py").read_text()
    assert "worker_start_ticks" in migration and "worker_boot_id" in migration
    assert "scheduler_one_running_claim_idx" in migration
    assert "process_assurance.same_process" in controller
    assert "status='pending' AND phase<>'RESEARCH'" in controller
    assert "os.killpg" not in qa
    assert "claim_token" in scheduler and "lease_until" in scheduler


def test_scheduler_leases_only_immediately_runnable_priority_batch():
    rows = [("housekeeping", "h"), ("tasksweep", "t"),
            ("controller-resume", "r"), ("management-control", "m")]
    assert scheduler._claim_batch(rows, 2) == [
        ("controller-resume", "r"), ("management-control", "m")]
    assert scheduler._claim_batch(rows, 0) == []


def test_browser_cleanup_requires_agentos_marker_and_exact_birth_revalidation(monkeypatch):
    assert not reap._owned_browser_command("/usr/bin/google-chrome https://example.com")
    cmd = "/home/u/.cache/ms-playwright/chromium/chrome --headless --remote-debugging-pipe"
    assert reap._owned_browser_command(cmd)
    identity = pa.ProcessIdentity(77, 12, "boot")
    original = pa.ProcessSnapshot(identity, 1, 77, 1000, cmd)
    reused = pa.ProcessSnapshot(pa.ProcessIdentity(77, 13, "boot"), 1, 77, 1000, cmd)
    reads = iter([original, reused])
    monkeypatch.setattr(reap.process_assurance, "read_snapshot", lambda _pid: next(reads))
    killed = []
    monkeypatch.setattr(reap.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert not reap._identity_safe_signal(77, 1, cmd, 9)
    assert killed == []


def test_unregistered_browser_match_is_observation_only(monkeypatch):
    monkeypatch.setattr(reap, "_browser_procs", lambda: [(77, 1, reap.BROWSER_STALE_S + 1)])
    monkeypatch.setattr(reap.os, "kill",
                        lambda *_args: pytest.fail("unregistered browser was signalled"))

    assert reap._sweep_browsers(dry=False) == 1


def test_browser_owner_proc_read_failure_is_not_proof_of_death(monkeypatch, tmp_path):
    owner_id = pa.ProcessIdentity(os.getpid(), 123, "boot-a")
    root = pa.ProcessSnapshot(pa.ProcessIdentity(611, 10, "boot-a"), 1, 611, 1000,
                              "node /repo/scripts/qa/browser_bridge.js")
    record = tmp_path / "owned-browser.json"
    record.write_text(json.dumps({"pid": 611, "start_ticks": 10, "boot_id": "boot-a",
                                  "owner": "qa-browser:tenant:run",
                                  "owner_pid": owner_id.pid,
                                  "owner_start_ticks": owner_id.start_ticks,
                                  "owner_boot_id": owner_id.boot_id}))
    monkeypatch.setattr(clauded.process_assurance, "read_snapshot", lambda _pid: None)

    assert clauded._registered_owner_gone(record, root) is False
