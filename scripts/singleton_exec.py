#!/usr/bin/env python3
"""Process-birth-aware lifetime singleton ownership and readiness records."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from process_assurance import ProcessIdentity, read_snapshot, same_process, scan_snapshots

ROOT = Path(__file__).resolve().parents[1]
# A systemd service with PrivateTmp=true and an operator CLI otherwise see different /tmp trees, allowing
# both to believe they own the same daemon. Keep the kernel-lock registry in the repo runtime directory so
# every namespace and recovery entrypoint observes one exact process generation. Stale files across reboot
# are harmless: inspect() also validates the held flock, boot id, PID birth ticks, and exact argv.
LOCK_DIR = Path(os.environ.get("AOS_SINGLETON_DIR", str(ROOT / ".runtime" / "singletons")))
_HELD = {}


def _safe_name(name: str) -> str:
    safe = "".join(c for c in str(name) if c.isalnum() or c in "-_")
    if not safe or safe != name:
        raise ValueError("invalid singleton name")
    return safe


def _argv(pid: int, proc_root: Path | str = "/proc") -> list[str]:
    try:
        raw = (Path(proc_root) / str(int(pid)) / "cmdline").read_bytes()
        return [part.decode(errors="replace") for part in raw.split(b"\0") if part]
    except (OSError, ValueError):
        return []


def _cwd(pid: int, proc_root: Path | str = "/proc") -> Path:
    return (Path(proc_root) / str(int(pid)) / "cwd").resolve(strict=True)


def _command_digest(argv) -> str:
    raw = json.dumps(list(argv), separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def acquire(name: str, command=None):
    """Acquire a nonblocking lifetime flock and publish exact birth/command identity."""
    safe = _safe_name(name)
    LOCK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        LOCK_DIR.chmod(0o700)
    except OSError:
        pass
    path = LOCK_DIR / f"{safe}.lock"
    fh = path.open("a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    os.set_inheritable(fh.fileno(), True)
    snap = read_snapshot(os.getpid())
    argv = list(command) if command is not None else _argv(os.getpid())
    payload = {"pid": os.getpid(), "name": safe, "argv": argv,
               "command_sha256": _command_digest(argv)}
    if snap:
        payload.update({"start_ticks": snap.identity.start_ticks, "boot_id": snap.identity.boot_id})
    fh.seek(0); fh.truncate(); fh.write(json.dumps(payload, sort_keys=True)); fh.flush()
    _HELD[safe] = fh
    if snap:
        prefix = f"AOS_SINGLETON_{safe.upper().replace('-', '_')}"
        os.environ[f"{prefix}_TOKEN"] = snap.identity.token()
        # The descriptor must cross singleton_exec's one intended exec.  Its number lets the target seal it
        # against every subsequent child exec; shell loops use the same value in explicit child redirections.
        os.environ[f"{prefix}_FD"] = str(fh.fileno())
    return fh


def require(name: str, command=None):
    """Acquire ownership, or recognize ownership inherited across singleton_exec's exec()."""
    safe = _safe_name(name)
    snap = read_snapshot(os.getpid())
    inherited = os.environ.get(f"AOS_SINGLETON_{safe.upper().replace('-', '_')}_TOKEN")
    if snap is not None and inherited == snap.identity.token():
        state = inspect(safe)
        if state.get("owned") and int(state["record"]["pid"]) == os.getpid():
            fd_value = os.environ.get(f"AOS_SINGLETON_{safe.upper().replace('-', '_')}_FD", "")
            try:
                fd = int(fd_value)
                os.fstat(fd)
                os.set_inheritable(fd, False)
            except (OSError, ValueError):
                # A matching token without the inherited lock descriptor is not sufficient ownership.
                return False
            return True
    return acquire(safe, command=command) is not None


def inspect(name: str, *, require_ready=False, max_ready_age_s=None,
            proc_root: Path | str = "/proc") -> dict:
    """Validate kernel lock, exact process generation/argv, and generation-bound readiness."""
    safe = _safe_name(name)
    path = LOCK_DIR / f"{safe}.lock"
    try:
        record = json.loads(path.read_text())
        expected = ProcessIdentity(int(record["pid"]), int(record["start_ticks"]), str(record["boot_id"]))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {"owned": False, "ready": False, "reason": "missing_or_invalid_record"}
    try:
        probe = path.open("a+")
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            return {"owned": False, "ready": False, "reason": "lock_unheld", "record": record}
        except BlockingIOError:
            pass
        finally:
            probe.close()
    except OSError:
        return {"owned": False, "ready": False, "reason": "lock_unreadable", "record": record}
    observed = read_snapshot(expected.pid, proc_root)
    if not same_process(expected, observed):
        return {"owned": False, "ready": False, "reason": "identity_mismatch", "record": record}
    actual = _argv(expected.pid, proc_root)
    recorded = list(record.get("argv") or [])
    if not recorded or actual != recorded or record.get("command_sha256") != _command_digest(actual):
        return {"owned": False, "ready": False, "reason": "command_mismatch", "record": record}
    ready, reason = (True, "identity_owned") if not require_ready else (False, "readiness_missing")
    if require_ready:
        try:
            ready_record = json.loads((LOCK_DIR / f"{safe}.ready").read_text())
            ready = ready_record.get("identity") == expected.token()
            reason = "ready" if ready else "stale_readiness"
            if ready and max_ready_age_s is not None:
                # The ready record is already boot-generation-bound, so monotonic time avoids wall-clock
                # corrections making a stalled loop look fresh for longer than its contract.
                age = max(0.0, time.monotonic() - float(ready_record.get("ready_monotonic", 0)))
                if age > float(max_ready_age_s):
                    ready, reason = False, "readiness_expired"
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return {"owned": True, "ready": ready, "reason": reason, "record": record}


def mark_ready(name: str, detail=None) -> bool:
    """Publish readiness only for this exact process generation's held singleton."""
    safe = _safe_name(name)
    state = inspect(safe)
    if not state.get("owned"):
        return False
    record = state["record"]
    identity = f"{record['boot_id']}:{record['pid']}:{record['start_ticks']}"
    inherited = os.environ.get(f"AOS_SINGLETON_{safe.upper().replace('-', '_')}_TOKEN")
    # A daemon may call directly, while a shell daemon marks readiness from its just-completed tick child.
    # Both must carry the exact generation token inherited from singleton_exec; a random process cannot bless
    # a stale or somebody else's record.
    if inherited != identity:
        return False
    payload = {"identity": identity,
               "ready_at": time.time(), "ready_monotonic": time.monotonic(), "detail": detail}
    path = LOCK_DIR / f"{safe}.ready"
    tmp = LOCK_DIR / f".{safe}.ready.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, sort_keys=True))
    os.replace(tmp, path)
    return True


def clear_ready(name: str) -> bool:
    """Remove only this singleton generation's readiness marker (for disconnect/reconnect transitions)."""
    safe = _safe_name(name)
    state = inspect(safe)
    if not state.get("owned"):
        return False
    record = state["record"]
    identity = f"{record['boot_id']}:{record['pid']}:{record['start_ticks']}"
    inherited = os.environ.get(f"AOS_SINGLETON_{safe.upper().replace('-', '_')}_TOKEN")
    if inherited != identity:
        return False
    path = LOCK_DIR / f"{safe}.ready"
    try:
        current = json.loads(path.read_text())
        if current.get("identity") != identity:
            return False
        path.unlink()
        return True
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def exact_legacy(script_name: str, args=()) -> list[dict]:
    """Find exact interpreter/script argv matches and expose their birth identity for safe deferral."""
    matches = []
    expected_args = list(map(str, args))
    for pid, snap in scan_snapshots().items():
        argv = _argv(pid)
        if len(argv) != len(expected_args) + 2 or not Path(argv[0]).name.startswith("python"):
            continue
        if Path(argv[1]).name != Path(script_name).name or argv[2:] != expected_args:
            continue
        matches.append({"pid": pid, "start_ticks": snap.identity.start_ticks,
                        "boot_id": snap.identity.boot_id, "argv": argv})
    return sorted(matches, key=lambda item: (item["start_ticks"], item["pid"]))


def older_matching(command, cwd: Path | str, proc_root: Path | str = "/proc") -> list[int]:
    """Find only older processes with this exact argv and working directory."""
    current = read_snapshot(os.getpid())
    if current is None:
        return []
    expected_argv = list(map(str, command))
    expected_cwd = Path(cwd).resolve()
    matches = []
    for pid, snap in scan_snapshots(proc_root).items():
        if pid == os.getpid() or snap.identity.boot_id != current.identity.boot_id:
            continue
        argv = _argv(pid, proc_root)
        if argv != expected_argv or snap.identity.start_ticks >= current.identity.start_ticks:
            continue
        try:
            actual_cwd = _cwd(pid, proc_root)
        except OSError:
            continue
        if actual_cwd == expected_cwd:
            matches.append(pid)
    return sorted(matches)


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    if len(argv) >= 2 and argv[0] == "ready":
        return 0 if mark_ready(argv[1], " ".join(argv[2:]) or None) else 1
    if len(argv) < 3 or argv[0] != "run":
        raise SystemExit("usage: singleton_exec.py run NAME COMMAND [ARG ...] | ready NAME [DETAIL]")
    if not require(argv[1], command=argv[2:]):
        return 0
    os.execvp(argv[2], argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
