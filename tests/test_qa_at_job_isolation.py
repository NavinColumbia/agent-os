"""Executable release policy for per-job headed Chromium + Orca/AT-SPI isolation.

The fast tests exercise ownership, environment, and cleanup behavior without a desktop.  The explicitly
enabled live test launches the production process chain and is mandatory release evidence on a worker that
permits local D-Bus sockets.  It is opt-in only so generic unit-only environments do not pretend a skip is
actual-AT proof; docs/STANDARDS-qa.md requires the explicit invocation at the release gate.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import io
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts" / "qa")]


class _ExitedProcess:
    """Small Popen stand-in for constructor/environment contract tests."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = 0
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class _GracefulProcess:
    """A protocol-close-aware process: wait lets its driver finish without a signal."""

    pid = 49001

    def __init__(self, events):
        self.events = events
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        self.returncode = 0
        return 0


def _install_fake_admission(monkeypatch):
    import browser_gate
    import clauded

    acquired = []
    released = []
    registered = []
    unregistered = []

    monkeypatch.setattr(browser_gate, "qa_holder", lambda label: f"qa-test:{label}")

    def acquire(holder, *args, **kwargs):
        token = {"kind": "qa-isolation-test", "holder": holder, "serial": len(acquired) + 1}
        acquired.append(token)
        return token

    monkeypatch.setattr(browser_gate, "acquire", acquire)
    monkeypatch.setattr(browser_gate, "release", lambda token: released.append(token))
    monkeypatch.setattr(browser_gate, "lease_lost", lambda _token: False)

    def register(pid, owner):
        record = Path(f"/tmp/qa-isolation-owned-{pid}")
        registered.append((pid, owner, record))
        return record

    monkeypatch.setattr(clauded, "register_owned", register)
    monkeypatch.setattr(clauded, "unregister_owned", lambda record: unregistered.append(record))
    return acquired, released, registered, unregistered


def test_concurrent_actual_at_jobs_get_private_child_environments_and_exact_cleanup(monkeypatch):
    import qa_explorer

    acquired, released, registered, unregistered = _install_fake_admission(monkeypatch)
    launches = []
    launch_lock = threading.Lock()

    class FakeSessionLock:
        def __init__(self, fd):
            self._fd = fd

        def fileno(self):
            return self._fd

    fake_locks = []
    released_locks = []
    monkeypatch.setattr(
        qa_explorer, "_acquire_at_session_lock",
        lambda _timeout: fake_locks.append(FakeSessionLock(700 + len(fake_locks))) or fake_locks[-1])
    monkeypatch.setattr(
        qa_explorer, "_release_at_session_lock", lambda handle: released_locks.append(handle))

    def popen(command, **kwargs):
        with launch_lock:
            launches.append((command, kwargs))
            return _ExitedProcess(48000 + len(launches))

    monkeypatch.setattr(qa_explorer.subprocess, "Popen", popen)
    monkeypatch.setattr(qa_explorer.BrowserBridge, "_await_ready", lambda _self: None)
    monkeypatch.setattr(qa_explorer.BrowserBridge, "goto", lambda _self, _url: {"ok": True})
    parent_runtime = os.environ.get("XDG_RUNTIME_DIR")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(
            qa_explorer.BrowserBridge,
            "data:text/html,alpha", actual_at=True, shot_dir="alpha/screenshots", scope_run_id=101,
            scope_tenant="tenant-a")
        second_future = pool.submit(
            qa_explorer.BrowserBridge,
            "data:text/html,beta", actual_at=True, shot_dir="beta/screenshots", scope_run_id=202,
            scope_tenant="tenant-b")
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)
    runtimes = [Path(first._at_runtime_dir), Path(second._at_runtime_dir)]
    try:
        assert runtimes[0] != runtimes[1]
        assert all(path.is_dir() for path in runtimes)
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in runtimes)
        assert os.environ.get("XDG_RUNTIME_DIR") == parent_runtime

        for command, kwargs in launches:
            assert command[:4] == ["xvfb-run", "-a", "dbus-run-session", "--"]
            assert command[-3:] == [str(qa_explorer.AT_DRIVER), "--bridge", str(qa_explorer.BRIDGE_JS)]
            assert kwargs["start_new_session"] is True
            assert kwargs["env"]["AOS_QA_AT_DRIVER"] == "orca"
            assert kwargs["env"]["AOS_QA_AT_SESSION_LOCK_FD"] == str(kwargs["pass_fds"][0])
            assert kwargs["env"].get("GSETTINGS_BACKEND") != "memory", (
                "a process-local settings backend hides Orca's accessibility enablement from Chromium")
            runtime = Path(kwargs["env"]["XDG_RUNTIME_DIR"])
            assert runtime in runtimes
        assert {Path(kwargs["env"]["XDG_RUNTIME_DIR"]) for _command, kwargs in launches} == set(runtimes)
        assert {kwargs["env"]["AOS_QA_SHOT_DIR"] for _command, kwargs in launches} == {
            "alpha/screenshots", "beta/screenshots"}
        assert {kwargs["env"]["AOS_QA_VIDEO_DIR"] for _command, kwargs in launches} == {
            "alpha/videos", "beta/videos"}
        assert {item[1] for item in registered} == {
            f"qa-browser:tenant-a:101:{os.getpid()}",
            f"qa-browser:tenant-b:202:{os.getpid()}",
        }
    finally:
        first.close()
        second.close()

    assert all(not path.exists() for path in runtimes)
    assert {item["serial"] for item in released} == {item["serial"] for item in acquired}
    assert set(unregistered) == {item[2] for item in registered}
    assert set(released_locks) == set(fake_locks)


def test_real_at_workstation_lock_is_full_lifetime_and_bounded():
    """A second real screen-reader job queues; it never starts a competing Orca."""
    import qa_explorer

    first = qa_explorer._acquire_at_session_lock(1)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="Orca workstation lease"):
            qa_explorer._acquire_at_session_lock(0.15)
        assert time.monotonic() - started < 0.75
    finally:
        qa_explorer._release_at_session_lock(first)
    successor = qa_explorer._acquire_at_session_lock(1)
    qa_explorer._release_at_session_lock(successor)


def test_real_at_driver_owns_private_speech_server_instead_of_autospawn():
    """Rapid workstation handoff must not depend on speechd's unbounded client autospawn."""
    import at_driver

    source = Path(at_driver.__file__).read_text()
    assert '"speech-dispatcher", "--run-single"' in source
    assert 'env["SPEECHD_ADDRESS"] = f"unix_socket:{speech_socket}"' in source
    assert "self.node, self.orca, self.speechd" in source


def test_actual_at_child_environment_does_not_inherit_unrelated_parent_values(monkeypatch):
    """Regression: the browser/AT tree must not receive worker credentials through ``os.environ``."""
    import qa_explorer

    _install_fake_admission(monkeypatch)
    launches = []
    monkeypatch.setenv("QA_ISOLATION_CANARY_SECRET", "must-not-reach-child")
    monkeypatch.setattr(
        qa_explorer.subprocess, "Popen",
        lambda command, **kwargs: launches.append((command, kwargs)) or _ExitedProcess(48500))
    monkeypatch.setattr(qa_explorer.BrowserBridge, "_await_ready", lambda _self: None)
    monkeypatch.setattr(qa_explorer.BrowserBridge, "goto", lambda _self, _url: {"ok": True})

    bridge = qa_explorer.BrowserBridge(
        "data:text/html,secret-canary", actual_at=True, scope_run_id="canary",
        scope_tenant="isolation")
    try:
        assert "QA_ISOLATION_CANARY_SECRET" not in launches[0][1]["env"], (
            "the per-job subprocess environment must be an allowlist, not a copy of the worker environment")
    finally:
        bridge.close()


def test_prelaunch_failure_releases_lease_and_private_runtime(monkeypatch, tmp_path):
    """A bad resume-state path fails before Popen but still owes every acquired-resource postcondition."""
    import qa_explorer

    acquired, released, _registered, _unregistered = _install_fake_admission(monkeypatch)
    runtime_dir = tmp_path / "private-runtime"

    def make_runtime(*_args, **_kwargs):
        runtime_dir.mkdir(mode=0o700)
        return str(runtime_dir)

    monkeypatch.setattr(qa_explorer.tempfile, "mkdtemp", make_runtime)
    with pytest.raises(RuntimeError, match="storage state is missing"):
        qa_explorer.BrowserBridge(
            "data:text/html,startup-failure", actual_at=True,
            storage_state_path=tmp_path / "missing-storage.json",
            scope_run_id="startup-failure", scope_tenant="isolation")

    assert released == acquired, "startup failure leaked its browser-capacity lease"
    assert not runtime_dir.exists(), "startup failure leaked its private XDG runtime directory"


def test_protocol_close_gets_bounded_grace_before_forced_tree_signal(monkeypatch):
    """Regression: signaling immediately after the close reply can preempt Orca's finally cleanup."""
    import qa_explorer

    bridge = qa_explorer.BrowserBridge("data:text/html,cleanup", autostart=False)
    events = []
    bridge.proc = _GracefulProcess(events)
    bridge._send = lambda request: events.append(("protocol", request["cmd"])) or {
        "ok": True, "closed": True}
    bridge._terminate_process_tree = lambda sig: events.append(("signal", sig))

    bridge.close()

    assert events[0] == ("protocol", "close")
    assert events[1][0] == "wait", (
        "after the driver acknowledges close, wait for its bounded finally cleanup before signaling")
    assert not any(kind == "signal" for kind, _value in events), (
        "a driver that exits inside the grace period must not be signaled")


def test_scoped_cancellation_closes_only_the_exact_tenant_run_pair():
    import qa_explorer

    class Bridge:
        def __init__(self, run_id, tenant):
            self.scope_run_id = run_id
            self.scope_tenant = tenant
            self.closed = False

        def close(self):
            self.closed = True

    target = Bridge("run-a", "tenant-a")
    same_run_other_tenant = Bridge("run-a", "tenant-b")
    same_tenant_other_run = Bridge("run-b", "tenant-a")
    with qa_explorer._LIVE_BRIDGES_LOCK:
        qa_explorer._LIVE_BRIDGES.update((target, same_run_other_tenant, same_tenant_other_run))
    try:
        assert qa_explorer.close_live_bridges(run_id="run-a", tenant="tenant-a") == 1
        assert target.closed is True
        assert same_run_other_tenant.closed is False
        assert same_tenant_other_run.closed is False
    finally:
        with qa_explorer._LIVE_BRIDGES_LOCK:
            qa_explorer._LIVE_BRIDGES.difference_update(
                (target, same_run_other_tenant, same_tenant_other_run))


def test_scoped_cancellation_rejects_partial_tenant_or_run_scope():
    """A single scope field is ambiguous and must never widen cancellation across a neighbor."""
    import qa_explorer

    class Bridge:
        scope_run_id = "run-a"
        scope_tenant = "tenant-a"
        closed = False

        def close(self):
            self.closed = True

    bridge = Bridge()
    with qa_explorer._LIVE_BRIDGES_LOCK:
        qa_explorer._LIVE_BRIDGES.add(bridge)
    try:
        with pytest.raises(ValueError, match="run_id.*tenant|tenant.*run_id"):
            qa_explorer.close_live_bridges(run_id="run-a")
        with pytest.raises(ValueError, match="run_id.*tenant|tenant.*run_id"):
            qa_explorer.close_live_bridges(tenant="tenant-a")
        assert bridge.closed is False
    finally:
        with qa_explorer._LIVE_BRIDGES_LOCK:
            qa_explorer._LIVE_BRIDGES.discard(bridge)


def _counter_url(label: str) -> str:
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{label} isolation proof</title></head>
<body><main><h1>{label} counter</h1>
<p id="count-label">{label} current count</p>
<output id="count" role="status" aria-live="polite" aria-atomic="true"
 aria-labelledby="count-label">0</output>
<button type="button" onclick="document.getElementById('count').textContent='1'">Increment {label}</button>
</main></body></html>"""
    return "data:text/html;charset=utf-8," + urllib.parse.quote(html)


def _process_tree_identities(root_pid: int):
    import process_assurance

    snapshots = process_assurance.scan_snapshots()
    root = snapshots[root_pid].identity
    pids = process_assurance.descendant_pids(root, snapshots) + [root_pid]
    return [snapshots[pid].identity for pid in pids if pid in snapshots]


def _environment(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    pairs = [item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item]
    return {key.decode(errors="replace"): value.decode(errors="replace") for key, value in pairs}


def _tree_isolation_facts(identities):
    import process_assurance

    facts = {key: set() for key in (
        "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "AOS_ORCA_DEBUG_PATH",
        "AOS_QA_STORAGE_STATE_PATH", "AOS_QA_SHOT_DIR", "AOS_QA_VIDEO_DIR")}
    facts["cmdlines"] = []
    for identity in identities:
        snapshot = process_assurance.read_snapshot(identity.pid)
        if not process_assurance.same_process(identity, snapshot):
            continue
        facts["cmdlines"].append(snapshot.cmdline)
        env = _environment(identity.pid)
        for key in (
                "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "AOS_ORCA_DEBUG_PATH",
                "AOS_QA_STORAGE_STATE_PATH", "AOS_QA_SHOT_DIR", "AOS_QA_VIDEO_DIR"):
            if env.get(key):
                facts[key].add(env[key])
    return facts


def _wait_identities_dead(identities, timeout_s=8.0):
    import process_assurance

    deadline = time.monotonic() + timeout_s
    remaining = list(identities)
    while remaining and time.monotonic() < deadline:
        remaining = [identity for identity in remaining if process_assurance.same_process(
            identity, process_assurance.read_snapshot(identity.pid))]
        if remaining:
            time.sleep(0.1)
    return remaining


def _close_finished_bridge_future(future, timeout_s=95.0):
    """Own a queued live bridge even when an earlier assertion aborts the test.

    The real-AT workstation is serialized, so the peer future can still be
    waiting when the first job is inspected.  A failed assertion must not let
    that queued job launch after the normal bridge cleanup callbacks have
    already unwound.
    """
    try:
        bridge = future.result(timeout=timeout_s)
    except Exception:
        return
    bridge.close()


def _assert_live_host_can_create_private_bus(runtime_dir: Path):
    runtime_dir.mkdir(mode=0o700)
    env = {**os.environ, "XDG_RUNTIME_DIR": str(runtime_dir)}
    result = subprocess.run(
        ["dbus-run-session", "--", "true"], text=True, capture_output=True, timeout=10, env=env)
    assert result.returncode == 0, (
        "live isolation proof requires local Unix-socket binding for a private D-Bus session; "
        f"rc={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}")


@pytest.mark.skipif(
    os.environ.get("AOS_RUN_LIVE_AT_ISOLATION") != "1",
    reason="run explicitly on the release worker; a skip is not release evidence",
)
def test_live_concurrent_jobs_are_isolated_and_failure_cleanup_is_complete(monkeypatch, tmp_path, request):
    """Launch two jobs together; prove one real AT workstation queues, hands off, and cleans exactly."""
    import at_driver
    import browser_gate
    import qa_explorer

    assert at_driver.availability() == {"available": True, "driver": "orca", "missing": []}
    assert Path(qa_explorer.NODE_PATH, "playwright").is_dir(), qa_explorer.NODE_PATH
    _assert_live_host_can_create_private_bus(tmp_path / "dbus-preflight-runtime")

    # A data: URL leaves real Orca focused on Chrome's address bar on this
    # desktop stack. Serve the same self-contained app over HTTP, as production
    # QA does, so the live-region assertion is an actual page/AT contract.
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class CounterHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            label = "Beta" if self.path.startswith("/beta") else "Alpha"
            body = urllib.parse.unquote(_counter_url(label).split(",", 1)[1]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), CounterHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    request.addfinalizer(server.server_close)
    request.addfinalizer(server.shutdown)
    targets = {name: f"http://127.0.0.1:{server.server_port}/{name.lower()}"
               for name in ("Alpha", "Beta")}

    lease_lock = threading.Lock()
    acquired = []
    released = []

    def acquire(holder, *args, **kwargs):
        with lease_lock:
            token = {"kind": "live-isolation", "holder": holder, "serial": len(acquired) + 1}
            acquired.append(token)
            return token

    monkeypatch.setattr(browser_gate, "acquire", acquire)
    monkeypatch.setattr(browser_gate, "release", lambda token: released.append(token))
    monkeypatch.setattr(browser_gate, "lease_lost", lambda _token: False)

    job_paths = {}
    for name in ("Alpha", "Beta"):
        job_dir = tmp_path / name.lower()
        job_dir.mkdir()
        storage_path = job_dir / "storage.json"
        storage_path.write_text('{"cookies":[],"origins":[]}')
        job_paths[name] = {
            "storage": storage_path,
            "screenshots": job_dir / "screenshots",
            "videos": job_dir / "videos",
        }

    bridges = {}
    identities = {}
    ownership_records = {}
    work_dirs = {}
    facts = {}

    def record_and_exercise(name, bridge):
        bridges[name] = bridge
        identities[name] = _process_tree_identities(bridge.proc.pid)
        ownership_records[name] = Path(bridge._ownership_record)
        assert ownership_records[name].is_file()
        facts[name] = _tree_isolation_facts(identities[name])
        assert Path(bridge._at_runtime_dir).is_dir()
        assert stat.S_IMODE(Path(bridge._at_runtime_dir).stat().st_mode) == 0o700
        assert str(bridge._at_runtime_dir) in facts[name]["XDG_RUNTIME_DIR"]
        assert facts[name]["DISPLAY"]
        assert facts[name]["DBUS_SESSION_BUS_ADDRESS"]
        assert facts[name]["AOS_ORCA_DEBUG_PATH"]
        assert facts[name]["AOS_QA_STORAGE_STATE_PATH"] == {str(job_paths[name]["storage"])}
        assert facts[name]["AOS_QA_SHOT_DIR"] == {str(job_paths[name]["screenshots"])}
        assert facts[name]["AOS_QA_VIDEO_DIR"] == {str(job_paths[name]["videos"])}
        work_dirs[name] = {str(Path(path).parent) for path in facts[name]["AOS_ORCA_DEBUG_PATH"]}
        assert any("at_driver.py" in line for line in facts[name]["cmdlines"])
        assert any("browser_bridge.js" in line for line in facts[name]["cmdlines"])
        assert any("orca" in line for line in facts[name]["cmdlines"])
        initial = bridge.state()
        assert initial.get("actualAssistiveTechnologyAvailable") is True, initial
        clicked = bridge.act({"cmd": "click", "target_text": f"Increment {name}", "role": "button"})
        assert clicked.get("ok") and clicked.get("clicked"), clicked
        changed = bridge.state()
        assert Path(changed["screenshot"]).is_relative_to(job_paths[name]["screenshots"]), changed
        utterances = [str(event.get("utterance") or "")
                      for event in changed.get("actualAssistiveTechnologyEvents") or []]
        assert any(f"{name} current count 1" in value for value in utterances), utterances
        other = "Beta" if name == "Alpha" else "Alpha"
        assert not any(f"{other} current count" in value for value in utterances), utterances

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool, contextlib.ExitStack() as cleanup:
        futures = {
            name: pool.submit(
                qa_explorer.BrowserBridge, targets[name], actual_at=True, timeout=50,
                storage_state_path=job_paths[name]["storage"],
                shot_dir=job_paths[name]["screenshots"],
                scope_run_id=f"run-{name.lower()}", scope_tenant="isolation-live")
            for name in ("Alpha", "Beta")
        }
        for future in futures.values():
            cleanup.callback(_close_finished_bridge_future, future)
        done, waiting = concurrent.futures.wait(
            futures.values(), timeout=75, return_when=concurrent.futures.FIRST_COMPLETED)
        assert len(done) == 1 and len(waiting) == 1, (
            "Orca is a single-user workstation: one job must run while its peer remains durably queued")
        first_name = next(name for name, future in futures.items() if future in done)
        second_name = "Beta" if first_name == "Alpha" else "Alpha"
        first_bridge = futures[first_name].result()
        cleanup.callback(first_bridge.close)
        record_and_exercise(first_name, first_bridge)

        first_runtime = Path(bridges[first_name]._at_runtime_dir)
        assert qa_explorer.close_live_bridges(
            run_id=f"run-{first_name.lower()}", tenant="isolation-live") == 1
        assert not _wait_identities_dead(identities[first_name]), "scoped cancellation leaked first children"
        assert not first_runtime.exists()
        assert all(not Path(path).exists() for path in work_dirs[first_name])
        assert not ownership_records[first_name].exists(), "scoped cancellation leaked first ownership record"

        second_bridge = futures[second_name].result(timeout=90)
        cleanup.callback(second_bridge.close)
        record_and_exercise(second_name, second_bridge)
        # Orca admission guarantees these workstation lifetimes do not overlap, and the exact first tree was
        # proved dead above. xvfb-run -a may therefore safely reuse the released numeric DISPLAY (for example
        # :104); treating sequential reuse as cross-job sharing made this live gate nondeterministic. The
        # actual per-job state/evidence channels must remain unique across both lifetimes.
        assert facts[first_name]["DISPLAY"] and facts[second_name]["DISPLAY"]
        for key in (
                "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "AOS_ORCA_DEBUG_PATH",
                "AOS_QA_STORAGE_STATE_PATH", "AOS_QA_SHOT_DIR", "AOS_QA_VIDEO_DIR"):
            assert facts[first_name][key].isdisjoint(facts[second_name][key]), f"shared {key}: {facts}"

        second_node = next(identity for identity in identities[second_name]
                         if "browser_bridge.js" in (_tree_isolation_facts([identity])["cmdlines"] or [""])[0])
        import process_assurance
        assert process_assurance.same_process(
            second_node, process_assurance.read_snapshot(second_node.pid)), (
            "refuse failure injection after the recorded browser birth identity changed")
        os.kill(second_node.pid, signal.SIGKILL)
        failed = bridges[second_name].state()
        assert failed.get("ok") is not True, failed
        second_runtime = Path(bridges[second_name]._at_runtime_dir)
        bridges[second_name].close()
        assert not _wait_identities_dead(identities[second_name]), "browser failure leaked second children"
        assert not second_runtime.exists()
        assert all(not Path(path).exists() for path in work_dirs[second_name])
        assert not ownership_records[second_name].exists(), "browser failure leaked second ownership record"
        assert sorted(token["serial"] for token in released) == [1, 2]
