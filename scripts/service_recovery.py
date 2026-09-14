#!/usr/bin/env python3
"""Exact-identity, readiness-gated recovery for Agent OS host services.

This module never discovers services by command-line substring.  New processes inherit a kernel lifetime
lock from singleton_exec; readiness is either a service-specific HTTP response or an identity-bound ready
record written only after the daemon's real loop/connection succeeds.  A healthy or exact legacy process is
left untouched and reported as deferred adoption, so deploying this code cannot duplicate or kill it.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

import process_assurance
import singleton_exec

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "bin" / "python")
SERVICE_LOG_DIR = ROOT / "logs" / "services"


@dataclass(frozen=True)
class Service:
    name: str
    command: tuple[str, ...]
    log: str
    ready_kind: str
    ready_target: str = ""
    ready_marker: str = ""
    cwd: str = str(ROOT)
    timeout_s: float = 8.0
    ready_max_age_s: float | None = None


def _specs() -> dict[str, Service]:
    home = Path.home()
    dist = home / "projects" / "products" / "noupload" / "dist"
    cockpit = home / "projects" / "products" / "1-ceo-cockpit"
    def py(name, *args):
        return (PY, str(ROOT / "scripts" / name), *map(str, args))
    def log(name):
        return str(SERVICE_LOG_DIR / f"{name}.log")
    return {
        "reply-listener": Service("reply-listener", (PY, "-u", str(ROOT / "scripts" / "reply_listener.py")),
            str(ROOT / "bridge" / "listener.log"), "record", ready_max_age_s=120),
        "noupload-static": Service("noupload-static",
            (sys.executable, "-m", "http.server", "5000", "--bind", "127.0.0.1"),
            log("noupload-static"), "http-header", "http://127.0.0.1:5000/", "SimpleHTTP/",
            str(dist)),
        "api": Service("api", py("api.py", "serve", "8090"), log("api"), "http",
            "http://127.0.0.1:8090/health", '"service": "agent-os"'),
        "dashboard": Service("dashboard", py("dashboard.py", "serve", "8092"), log("dashboard"),
            "http", "http://127.0.0.1:8092/health", '"service": "agent-os-dashboard"'),
        "jobd": Service("jobd", py("jobd.py", "serve", "15"), log("jobd"), "record",
            ready_max_age_s=60),
        "evidence-publisher": Service("evidence-publisher", py("evidencepublisher.py", "serve", "5"),
            log("evidence-publisher"), "record", ready_max_age_s=60),
        "frontdoor": Service("frontdoor", py("frontdoor.py", "serve", "8093"), log("frontdoor"),
            "http", "http://127.0.0.1:8093/health", '"service": "agent-os-frontdoor"'),
        "console": Service("console", py("console.py", "serve", "8099"), log("console"), "http",
            "http://127.0.0.1:8099/health", '"service": "agent-os-console"'),
        "assurance": Service("assurance", py("assurance_public.py", "serve", "8100"), log("assurance"),
            "http", "http://127.0.0.1:8100/health", '"service": "release-assurance"'),
        "statuspage": Service("statuspage", py("statuspage.py", "serve", "8097"), log("statuspage"),
            "http", "http://127.0.0.1:8097/health", '"verdict"'),
        "metrics": Service("metrics", py("metricsexport.py", "serve", "9101"), log("metrics"),
            "http", "http://127.0.0.1:9101/metrics", "# TYPE agentos_"),
        "ticker": Service("ticker", ("bash", str(ROOT / "scripts" / "ticker.sh")),
            log("ticker"), "record", timeout_s=65, ready_max_age_s=180),
        "watchdog": Service("watchdog", ("bash", str(ROOT / "scripts" / "watchdog.sh")),
            log("watchdog"), "record", timeout_s=65, ready_max_age_s=180),
        "dispatcher": Service("dispatcher", ("bash", str(ROOT / "scripts" / "dispatcher.sh")),
            log("dispatcher"), "record", timeout_s=15, ready_max_age_s=360),
        "replybridge": Service("replybridge", py("replybridge.py", "serve"),
            log("replybridge"), "record", timeout_s=15, ready_max_age_s=30),
        "cockpit-api": Service("cockpit-api",
            (PY, str(cockpit / "realapi" / "server.py"), "8766"),
            log("cockpit-api"), "http", "http://127.0.0.1:8766/health", '"ok": true',
            str(cockpit), timeout_s=15),
        "cockpit-web": Service("cockpit-web",
            (sys.executable, "-m", "http.server", "8871", "--bind", "127.0.0.1"),
            log("cockpit-web"), "http-header", "http://127.0.0.1:8871/", "SimpleHTTP/",
            str(cockpit), timeout_s=15),
    }


def _http_ready(service: Service) -> bool:
    if service.ready_kind not in {"http", "http-header"}:
        return False
    try:
        with urllib.request.urlopen(service.ready_target, timeout=1.5) as response:
            if response.status != 200:
                return False
            if service.ready_kind == "http-header":
                return response.headers.get("Server", "").startswith(service.ready_marker)
            return service.ready_marker.encode() in response.read(262144)
    except Exception:
        return False


def _resolve_arg(token: str, cwd: Path) -> Path:
    path = Path(token)
    return (path if path.is_absolute() else cwd / path).resolve()


def _process_cwd(pid: int, proc_root: Path | str = "/proc") -> Path:
    return (Path(proc_root) / str(int(pid)) / "cwd").resolve(strict=True)


def _argv_matches(actual: list[str], expected: list[str], actual_cwd: Path, expected_cwd: Path) -> bool:
    if len(actual) != len(expected):
        return False
    actual_exe, expected_exe = Path(actual[0]).name, Path(expected[0]).name
    if expected_exe.startswith("python"):
        if not actual_exe.startswith("python"):
            return False
    elif actual_exe != expected_exe:
        return False
    for got, want in zip(actual[1:], expected[1:]):
        if want.endswith((".py", ".sh")) or "/" in want:
            if _resolve_arg(got, actual_cwd) != _resolve_arg(want, expected_cwd):
                return False
        elif got != want:
            return False
    return True


def _legacy_identities(service: Service, proc_root: Path | str = "/proc") -> list[dict]:
    """Match exact executable/script/module argv and cwd, never a substring."""
    root = Path(proc_root)
    expected = list(service.command)
    matches = []
    for pid, snap in process_assurance.scan_snapshots(root).items():
        argv = singleton_exec._argv(pid, root)
        if len(argv) != len(expected):
            continue
        try:
            cwd = _process_cwd(pid, root)
        except OSError:
            continue
        argv_ok = _argv_matches(argv, expected, cwd, Path(service.cwd).resolve())
        if expected[1:3] == ["-m", "http.server"]:
            argv_ok = argv_ok and cwd == Path(service.cwd).resolve()
        if argv_ok:
            matches.append({"pid": pid, "start_ticks": snap.identity.start_ticks,
                            "boot_id": snap.identity.boot_id})
    return sorted(matches, key=lambda row: (row["start_ticks"], row["pid"]))


def recovery_decision(*, owned: bool, ready: bool, external_ready: bool, legacy: list) -> str:
    if owned:
        return "healthy" if ready else "owned_not_ready"
    if legacy:
        return "legacy_adoption_deferred"
    if external_ready:
        return "unowned_readiness_deferred"
    return "start"


def status(name: str, *, proc_root: Path | str = "/proc") -> dict:
    service = _specs()[name]
    state = singleton_exec.inspect(name, require_ready=service.ready_kind == "record",
                                   max_ready_age_s=service.ready_max_age_s, proc_root=proc_root)
    external = _http_ready(service)
    ready = bool(state.get("ready")) if service.ready_kind == "record" else bool(state.get("owned") and external)
    legacy = [] if state.get("owned") else _legacy_identities(service, proc_root)
    decision = recovery_decision(owned=bool(state.get("owned")), ready=ready,
                                 external_ready=external, legacy=legacy)
    return {"name": name, "state": decision, "owned": bool(state.get("owned")), "ready": ready,
            "ownership_reason": state.get("reason"), "legacy": legacy}


def ensure(name: str) -> dict:
    """Start only when neither exact ownership, service readiness, nor an exact legacy process exists."""
    service = _specs()[name]
    before = status(name)
    if before["state"] != "start":
        return before
    if not Path(service.cwd).is_dir():
        return {**before, "state": "unavailable", "reason": f"cwd missing: {service.cwd}"}
    Path(service.log).parent.mkdir(parents=True, exist_ok=True)
    with open(service.log, "ab", buffering=0) as log:
        subprocess.Popen([PY, str(ROOT / "scripts" / "singleton_exec.py"), "run", name, *service.command],
                         cwd=service.cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)
    deadline = time.monotonic() + service.timeout_s
    last = before
    deferred_since = None
    while time.monotonic() < deadline:
        time.sleep(0.2)
        last = status(name)
        if last["state"] == "healthy":
            return {**last, "started": True}
        if last["state"] in {"legacy_adoption_deferred", "unowned_readiness_deferred"}:
            # A concurrent supervisor can expose the target argv milliseconds before its singleton record is
            # observable. Do not misreport that start race as a permanent legacy process; require a stable
            # deferral before yielding. The pre-launch check above still leaves pre-existing legacy untouched.
            deferred_since = deferred_since or time.monotonic()
            if time.monotonic() - deferred_since >= 1.0:
                return {**last, "start_race_settled": True}
        else:
            deferred_since = None
    return {**last, "state": "owned_not_ready", "reason": "readiness deadline elapsed"}


def _wait_gone(expected: process_assurance.ProcessIdentity, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if not process_assurance.same_process(
                expected, process_assurance.read_snapshot(expected.pid)):
            return True
        time.sleep(0.05)
    return not process_assurance.same_process(
        expected, process_assurance.read_snapshot(expected.pid))


def _signal_exact(identity: process_assurance.ProcessIdentity, sig: int) -> bool:
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_signal is None:
        return False
    try:
        fd = pidfd_open(identity.pid, 0)
    except (OSError, ValueError):
        return False
    try:
        if not process_assurance.same_process(identity,
                                              process_assurance.read_snapshot(identity.pid)):
            return False
        pidfd_signal(fd, sig)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _cleanup_exact_descendants(plan: list[process_assurance.ProcessIdentity]) -> list[str]:
    """Bounded, birth-fenced cleanup used only if the daemon root ignored graceful TERM."""
    signaled = [identity for identity in plan if _signal_exact(identity, signal.SIGTERM)]
    deadline = time.monotonic() + 1.0
    while signaled and time.monotonic() < deadline:
        signaled = [identity for identity in signaled
                    if process_assurance.same_process(
                        identity, process_assurance.read_snapshot(identity.pid))]
        if signaled:
            time.sleep(0.05)
    for identity in signaled:
        _signal_exact(identity, signal.SIGKILL)
    return [identity.token() for identity in plan]


def _stop_generation(name: str, expected: process_assurance.ProcessIdentity) -> dict:
    """Signal and verify one already-authorized exact process generation."""
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_signal is None:
        return {"name": name, "stopped": False, "reason": "race_safe_signal_unavailable"}
    try:
        pidfd = pidfd_open(expected.pid, 0)
    except (OSError, ValueError) as exc:
        return {"name": name, "stopped": False,
                "reason": f"pidfd_open_failed:{getattr(exc, 'errno', None)}"}
    try:
        # Opening first binds the handle to one kernel process generation. Revalidate after that bind, then
        # signal the handle—not the reusable numeric PID.
        if not process_assurance.same_process(expected, process_assurance.read_snapshot(expected.pid)):
            return {"name": name, "stopped": False, "reason": "identity_changed_before_signal"}
        descendants = process_assurance.cleanup_plan(
            expected, process_assurance.scan_snapshots())
        try:
            pidfd_signal(pidfd, signal.SIGTERM)
        except OSError as exc:
            return {"name": name, "stopped": False, "reason": f"pidfd_signal_failed:{exc.errno}"}
        if _wait_gone(expected, 5.0):
            return {"name": name, "stopped": True, "identity": expected.token(), "forced": False}
        # A shell can defer a TERM trap while waiting on a foreground child. The pidfd still binds this
        # exact generation, so bounded escalation cannot hit a reused numeric PID.
        if not process_assurance.same_process(expected,
                                              process_assurance.read_snapshot(expected.pid)):
            return {"name": name, "stopped": True, "identity": expected.token(), "forced": False}
        try:
            pidfd_signal(pidfd, signal.SIGKILL)
        except OSError as exc:
            return {"name": name, "stopped": False,
                    "reason": f"pidfd_kill_failed:{exc.errno}", "identity": expected.token()}
        if not _wait_gone(expected, 2.0):
            return {"name": name, "stopped": False,
                    "reason": "exact_generation_did_not_exit", "identity": expected.token()}
        cleaned = _cleanup_exact_descendants(descendants)
        return {"name": name, "stopped": True, "identity": expected.token(), "forced": True,
                "descendants_cleaned": cleaned}
    finally:
        os.close(pidfd)


def stop(name: str) -> dict:
    """Stop one owned exact generation; legacy/unowned processes are never signaled here."""
    state = singleton_exec.inspect(name)
    if not state.get("owned"):
        return {"name": name, "stopped": False, "reason": state.get("reason")}
    record = state["record"]
    expected = process_assurance.ProcessIdentity(record["pid"], record["start_ticks"], record["boot_id"])
    return _stop_generation(name, expected)


def replace(name: str) -> dict:
    """Replace owned or one exact legacy generation, then require generation-bound readiness.

    This is reserved for a proven stale service. Exact argv + cwd + boot/PID/start identity is the authority
    for legacy adoption; ambiguous or merely externally-ready processes remain untouched.
    """
    before = status(name)
    stopped = None
    if before.get("owned"):
        stopped = stop(name)
    elif before.get("state") == "legacy_adoption_deferred":
        legacy = before.get("legacy") or []
        if len(legacy) != 1:
            return {"name": name, "state": "replacement_refused",
                    "reason": "ambiguous_exact_legacy_generations", "legacy": legacy}
        row = legacy[0]
        expected = process_assurance.ProcessIdentity(
            int(row["pid"]), int(row["start_ticks"]), str(row["boot_id"]))
        # Re-discover immediately before granting destructive authority. A stale caller snapshot or process
        # replacement cannot turn an argv substring into kill authority.
        current = _legacy_identities(_specs()[name], "/proc")
        if len(current) != 1 or current[0] != row:
            return {"name": name, "state": "replacement_refused",
                    "reason": "legacy_identity_changed", "legacy": current}
        stopped = _stop_generation(name, expected)
    elif before.get("state") != "start":
        return {**before, "state": "replacement_refused",
                "reason": "service_ownership_not_proven"}
    if stopped is not None and not stopped.get("stopped"):
        return {"name": name, "state": "replacement_failed", "stop": stopped}
    started = ensure(name)
    return {**started, "replaced": stopped is not None, "stop": stopped}


def repair(name: str) -> dict:
    """Converge a service without disrupting a healthy or merely legacy generation."""
    current = status(name)
    if current.get("state") == "healthy":
        return current
    if current.get("state") == "start":
        return ensure(name)
    if current.get("state") == "owned_not_ready":
        return replace(name)
    return current


def supervised_names() -> list[str]:
    """Services required by this installation, with optional surfaces discovered by responder."""
    import responder
    desired = list(dict.fromkeys(list(responder.DAEMONS.values()) + ["watchdog"]))
    specs = _specs()
    return [name for name in desired if name in specs]


def supervise(interval_s: float = 10.0) -> int:
    """Continuously converge the exact service set for a systemd/public deployment.

    Child processes remain identity-fenced by ``singleton_exec``.  This loop owns only convergence: a healthy
    generation is untouched, an exited generation is restarted, and ambiguous legacy/unowned listeners are
    never signalled.  systemd owns this supervisor and its cgroup, so host reboot and operator stop semantics
    remain conventional while the existing durable service recovery stays authoritative.
    """
    try:
        interval_s = min(300.0, max(2.0, float(interval_s)))
    except (TypeError, ValueError):
        interval_s = 10.0
    stopping = threading.Event()

    def _stop(_signum, _frame):
        stopping.set()

    prior = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _stop)
    names = supervised_names()
    print(json.dumps({"service_supervisor": "starting", "services": names}), flush=True)
    while not stopping.is_set():
        healthy = True
        for name in names:
            if stopping.is_set():
                break
            try:
                result = repair(name)
            except Exception as exc:
                result = {"name": name, "state": "error", "reason": str(exc)[:300]}
            state = str(result.get("state") or "unknown")
            healthy = healthy and state in {
                "healthy", "legacy_adoption_deferred", "unowned_readiness_deferred"}
            fingerprint = (state, str(result.get("reason") or ""))
            if prior.get(name) != fingerprint:
                print(json.dumps({"service": name, "state": state,
                                  "reason": result.get("reason")}, default=str), flush=True)
                prior[name] = fingerprint
        if healthy and not stopping.is_set():
            try:
                singleton_exec.mark_ready("service-supervisor", {"services": names})
            except Exception:
                pass
        stopping.wait(interval_s)
    try:
        singleton_exec.clear_ready("service-supervisor")
    except Exception:
        pass
    return 0


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    if argv and argv[0] == "serve":
        return supervise(argv[1] if len(argv) > 1 else os.environ.get(
            "AOS_SERVICE_SUPERVISOR_INTERVAL", "10"))
    commands = {"ensure": ensure, "status": status, "stop": stop, "replace": replace,
                "repair": repair}
    if len(argv) != 2 or argv[0] not in commands or argv[1] not in _specs():
        raise SystemExit("usage: service_recovery.py serve [interval_s] | "
                         "ensure|status|stop|replace|repair SERVICE")
    result = commands[argv[0]](argv[1])
    print(json.dumps(result, sort_keys=True))
    if argv[0] in {"ensure", "replace", "repair"}:
        return 0 if result["state"] in {"healthy", "legacy_adoption_deferred",
                                        "unowned_readiness_deferred"} else 1
    return 0 if result.get("ready") or result.get("stopped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
