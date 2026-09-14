#!/usr/bin/env python3
"""Bounded Orca/AT-SPI wrapper for the newline-JSON Playwright bridge.

The ordinary browser bridge exposes DOM mutation and Chromium Accessibility-domain evidence.  Those
are useful, but neither is a screen reader.  This wrapper is launched inside ``xvfb-run`` and
``dbus-run-session`` when a QA story explicitly requires observable assistive-technology output.  It
starts Orca, proxies the existing bridge protocol, and adds only utterances Orca itself emitted to
``state`` responses.

The outer Python BrowserBridge registers the wrapper root in the exact process-identity registry, so
parent-death cleanup owns the complete Xvfb -> DBus -> Orca -> Node -> Chromium tree.  This process also
performs bounded normal cleanup; it never relies on a daemon or a bare PID.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# A single cold Orca initializes in ~8-10s on this host, but two fully isolated
# sessions can spend >25s concurrently in AT-SPI event-manager startup. This is
# still a hard safety bound; it is long enough for legitimate contention and
# short enough for the outer browser/job deadlines to recover the exact tree.
_INIT_TIMEOUT_S = 45.0
_NODE_REPLY_TIMEOUT_S = 30.0
_AT_SETTLE_S = 1.2
_AT_EXPECTED_TIMEOUT_S = 6.0
_SPEECH_RE = re.compile(r"^(?P<ts>\d\d:\d\d:\d\d\.\d+) - SPEECH OUTPUT: (?P<body>.*)$")


def node_reply_timeout_s(request: dict | None) -> float:
    """Size the proxy ceiling to the declared browser workload, with a hard upper bound."""
    request = request if isinstance(request, dict) else {}
    cmd = str(request.get("cmd") or "")
    workload_s = _NODE_REPLY_TIMEOUT_S
    if cmd == "dwellLandmarks":
        targets = list(dict.fromkeys(str(item or "").strip()
                    for item in (request.get("targets") or []) if str(item or "").strip()))[:8]
        each_ms = request.get("duration_ms")
        if each_ms is None:
            each_ms = float(request.get("duration_s", 10) or 10) * 1000
        workload_s = max(workload_s, max(0.0, float(each_ms)) * max(1, len(targets)) / 1000 + 8)
    elif cmd == "tabTraverse":
        count = request.get("count")
        stops = min(120, max(2, int(count))) if isinstance(count, int) and count > 0 else 120
        pace_ms = min(1500, max(0, float(request.get("pace_ms", 35) or 35)))
        workload_s = max(workload_s, 15 + stops * max(0.25, pace_ms / 1000 + 0.075) + 5)
    elif cmd == "timedTransition":
        duration_ms = max(250, float(request.get("duration_ms", 10000) or 10000))
        grace_ms = max(0, float(request.get("completion_grace_ms", 5000) or 0))
        workload_s = max(workload_s, (duration_ms + grace_ms) / 1000 + 5)
    elif cmd in {"wait", "waitFor"}:
        workload_s = max(workload_s, max(0, float(request.get("timeout_ms", 0) or 0)) / 1000 + 5)
    return min(330.0, workload_s)


def availability() -> dict:
    """Return truthful local capability facts without launching anything."""
    required = ("xvfb-run", "dbus-run-session", "speech-dispatcher", "orca", "node")
    missing = [name for name in required if not shutil.which(name)]
    return {"available": not missing, "driver": "orca", "missing": missing}


def parse_speech_line(line: str) -> dict | None:
    """Parse one Orca debug utterance; reject generic debug text and malformed payloads."""
    match = _SPEECH_RE.match(str(line or "").strip())
    if not match:
        return None
    body = match.group("body")
    # Orca appends the resolved voice mapping after the repr() of the utterance.  Find the first mapping
    # boundary whose prefix is a valid string literal; rsplit() is wrong because the mapping is nested.
    utterance = None
    for index, char in enumerate(body):
        if char != "{" or index == 0 or not body[index - 1].isspace():
            continue
        try:
            candidate = ast.literal_eval(body[:index].strip())
        except (SyntaxError, ValueError):
            continue
        if isinstance(candidate, str):
            utterance = candidate
            break
    if utterance is None:
        return None
    if not isinstance(utterance, str) or not utterance.strip():
        return None
    return {
        "ts": match.group("ts"),
        "utterance": " ".join(utterance.split())[:2000],
        "source": "orca-at-spi",
    }


def _normalized_speech(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", str(value or "").casefold()).split())


def live_region_phrases(response: dict) -> list[str]:
    """Return exact visible phrases a real AT driver should eventually expose.

    These are expectations used only to decide how long to keep collecting Orca's real log.  They are never
    themselves emitted as AT evidence.  A failed/missing Orca utterance therefore remains visibly missing.
    """
    phrases = []
    for region in (response or {}).get("accessibilityRegions") or []:
        if not isinstance(region, dict):
            continue
        role = str(region.get("role") or "").casefold()
        if not (region.get("ariaLive") or role in ("status", "alert")
                or str(region.get("tag") or "").casefold() == "output"):
            continue
        label = region.get("ariaLabel") or region.get("labelText") or ""
        value = region.get("text") or ""
        phrase = " ".join(f"{label} {value}".split())
        if phrase:
            phrases.append(phrase[:1000])
    return list(dict.fromkeys(phrases))


def _utterance_matches(utterance: str, phrase: str) -> bool:
    heard, expected = _normalized_speech(utterance), _normalized_speech(phrase)
    if not heard or not expected:
        return False
    # Normal Orca output is the complete labelled live-region phrase.  Permit containment for harmless voice
    # prefixes/suffixes, but do not let a one-character counter value match unrelated speech.
    if len(expected) < 4:
        return heard == expected
    return expected in heard


class OrcaBridge:
    def __init__(self, bridge: Path):
        self.bridge = bridge
        self.work_dir = Path(tempfile.mkdtemp(prefix="aos-qa-orca-"))
        self.debug_path = self.work_dir / "orca-debug.log"
        self.speechd_log_path = self.work_dir / "speech-dispatcher.log"
        self.speechd: subprocess.Popen | None = None
        self.orca: subprocess.Popen | None = None
        self.node: subprocess.Popen | None = None
        self._debug_offset = 0
        self._events: list[dict] = []
        self._last_live_phrases: set[str] = set()
        self._closed = False
        raw_lock_fd = os.environ.get("AOS_QA_AT_SESSION_LOCK_FD")
        try:
            self._session_lock_fd = int(raw_lock_fd) if raw_lock_fd is not None else None
            if self._session_lock_fd is not None:
                os.fstat(self._session_lock_fd)
        except (TypeError, ValueError, OSError) as exc:
            raise RuntimeError("real AT driver requires a valid inherited workstation lease") from exc

    @staticmethod
    def _wait_for_text(path: Path, needle: str, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if needle in path.read_text(errors="replace"):
                    return True
            except OSError:
                pass
            time.sleep(0.2)
        return False

    @staticmethod
    def _signal_group(proc: subprocess.Popen | None, sig: int) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _read_node(self, timeout_s: float = _NODE_REPLY_TIMEOUT_S) -> dict:
        if self.node is None or self.node.poll() is not None:
            raise RuntimeError(f"browser bridge exited rc={None if self.node is None else self.node.poll()}")
        ready, _, _ = select.select([self.node.stdout], [], [], timeout_s)
        if not ready:
            raise TimeoutError(f"browser bridge reply exceeded {timeout_s:.1f}s")
        line = self.node.stdout.readline()
        if not line:
            raise RuntimeError(f"browser bridge closed stdout rc={self.node.poll()}")
        return json.loads(line)

    def _read_speech(self) -> list[dict]:
        try:
            with self.debug_path.open("r", errors="replace") as handle:
                handle.seek(self._debug_offset)
                lines = handle.readlines()
                self._debug_offset = handle.tell()
        except OSError:
            return []
        added = []
        for line in lines:
            event = parse_speech_line(line)
            if event is None:
                continue
            self._events.append(event)
            added.append(event)
        self._events = self._events[-100:]
        return added

    def _settle_speech(self, expected_phrases=None) -> bool:
        # Polite live-region announcements are queued.  Wait for a short bounded quiet period rather than
        # snapshotting immediately and falsely claiming Orca said nothing.  When the DOM shows a changed live
        # region, wait longer for that exact phrase to appear in Orca's log; the phrase is only a wait target,
        # never evidence.  The wait remains bounded so a broken AT stack cannot hang the workforce.
        pending = {_normalized_speech(p): str(p) for p in (expected_phrases or [])
                   if _normalized_speech(p)}
        deadline = time.monotonic() + (_AT_EXPECTED_TIMEOUT_S if pending else _AT_SETTLE_S)
        quiet_since = None
        while time.monotonic() < deadline:
            added = self._read_speech()
            if added:
                quiet_since = time.monotonic()
                for event in added:
                    for key, phrase in list(pending.items()):
                        if _utterance_matches(event.get("utterance") or "", phrase):
                            pending.pop(key, None)
                if not pending:
                    return True
            elif not pending and quiet_since is not None and time.monotonic() - quiet_since >= 0.25:
                return True
            time.sleep(0.1)
        self._read_speech()
        return not pending

    def start(self) -> dict:
        facts = availability()
        if not facts["available"]:
            raise RuntimeError("Orca driver unavailable: missing " + ", ".join(facts["missing"]))
        if not os.environ.get("DISPLAY") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            raise RuntimeError("Orca driver requires isolated DISPLAY and DBUS_SESSION_BUS_ADDRESS")

        env = {
            **os.environ,
            "NO_AT_BRIDGE": "0",
            "GTK_MODULES": "gail:atk-bridge",
            "ACCESSIBILITY_ENABLED": "1",
            "AOS_QA_AT_DRIVER": "orca",
            "AOS_ORCA_DEBUG_PATH": str(self.debug_path),
        }
        runtime_path = Path(__file__).with_name("orca_runtime")
        env["PYTHONPATH"] = str(runtime_path) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

        # Own one foreground speech-dispatcher inside this job's private runtime instead of relying on
        # speechd.SSIPClient autospawn. Autospawn can block inside server.communicate() during a rapid Orca
        # handoff and previously consumed the entire 45-second startup budget. The explicit server has a
        # private socket, exact parentage, the WSLg Pulse endpoint, and the same workstation-lock fd; it is
        # therefore both deterministic and cleaned with the registered AT tree.
        speech_dir = Path(env["XDG_RUNTIME_DIR"]) / "speech-dispatcher"
        speech_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        speech_socket = speech_dir / "speechd.sock"
        speech_kwargs = dict(env=env, start_new_session=True)
        if self._session_lock_fd is not None:
            speech_kwargs["pass_fds"] = (self._session_lock_fd,)
        with self.speechd_log_path.open("ab", buffering=0) as speech_log:
            self.speechd = subprocess.Popen(
                ["speech-dispatcher", "--run-single", "--communication-method", "unix_socket",
                 "--socket-path", str(speech_socket), "--pid-file", str(self.work_dir / "speechd.pid"),
                 "--timeout", "0"],
                stdout=speech_log, stderr=subprocess.STDOUT, **speech_kwargs)
        speech_deadline = time.monotonic() + 10.0
        while (not speech_socket.exists() and self.speechd.poll() is None
               and time.monotonic() < speech_deadline):
            time.sleep(0.05)
        if not speech_socket.exists() or self.speechd.poll() is not None:
            try:
                speech_tail = self.speechd_log_path.read_text(errors="replace")[-2000:]
            except OSError:
                speech_tail = "(speech-dispatcher log unavailable)"
            raise RuntimeError(
                f"private speech-dispatcher failed readiness rc={self.speechd.poll()}; "
                f"log_tail={speech_tail}")
        env["SPEECHD_ADDRESS"] = f"unix_socket:{speech_socket}"
        env["SPEECHD_SOCKET"] = str(speech_socket)
        orca_kwargs = dict(
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            start_new_session=True)
        if self._session_lock_fd is not None:
            # If the registered wrapper is killed abruptly, Orca itself keeps
            # admission fenced until the exact external tree has actually died.
            orca_kwargs["pass_fds"] = (self._session_lock_fd,)
        orca_command = ["orca", "--enable=speech", "--debug-file", str(self.debug_path)]
        self.orca = subprocess.Popen(orca_command, **orca_kwargs)
        if not self._wait_for_text(self.debug_path, "ORCA: Initialized.", _INIT_TIMEOUT_S):
            try:
                debug_tail = " ".join(
                    self.debug_path.read_text(errors="replace").splitlines()[-20:])[-3000:]
            except OSError:
                debug_tail = "(debug file unavailable)"
            raise TimeoutError(
                "Orca did not initialize within the bounded startup window; "
                f"debug_tail={debug_tail}")
        if self.orca.poll() is not None:
            raise RuntimeError(f"Orca exited during startup rc={self.orca.returncode}")

        self.node = subprocess.Popen(
            ["node", str(self.bridge)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        ready = self._read_node()
        if ready.get("ok") is not True or ready.get("cmd") != "ready":
            raise RuntimeError(f"browser bridge failed readiness: {ready}")
        self._read_speech()
        return {
            **ready,
            "actualAssistiveTechnologyAvailable": True,
            "actualAssistiveTechnologyDriver": "orca-at-spi",
        }

    def serve(self) -> int:
        try:
            ready = self.start()
        except Exception as exc:
            print(json.dumps({"cmd": "ready", "ok": False,
                              "error": f"actual AT driver startup failed: {str(exc)[:500]}"},
                             separators=(",", ":")), flush=True)
            return 2
        print(json.dumps(ready, separators=(",", ":")), flush=True)
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if self.node is None or self.node.poll() is not None:
                    raise RuntimeError("browser bridge is not running")
                self.node.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                self.node.stdin.flush()
                response = self._read_node(node_reply_timeout_s(request))
                if request.get("cmd") == "state" and response.get("ok") is True:
                    phrases = live_region_phrases(response)
                    changed_phrases = [phrase for phrase in phrases
                                       if _normalized_speech(phrase) not in self._last_live_phrases]
                    self._last_live_phrases = {_normalized_speech(phrase) for phrase in phrases}
                    self._settle_speech(changed_phrases)
                    healthy = self.orca is not None and self.orca.poll() is None
                    response["actualAssistiveTechnologyAvailable"] = healthy
                    response["actualAssistiveTechnologyDriver"] = "orca-at-spi"
                    response["actualAssistiveTechnologyEvents"] = list(self._events[-100:])
                print(json.dumps(response, separators=(",", ":"), default=str), flush=True)
                if request.get("cmd") == "close":
                    return 0
            except Exception as exc:
                response = {
                    "id": locals().get("request", {}).get("id"),
                    "cmd": locals().get("request", {}).get("cmd", "driver"),
                    "ok": False,
                    "error": f"actual AT driver failure: {str(exc)[:500]}",
                    "actualAssistiveTechnologyAvailable": False,
                }
                print(json.dumps(response, separators=(",", ":")), flush=True)
                return 2
        return 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for proc in (self.node, self.orca, self.speechd):
            self._signal_group(proc, signal.SIGTERM)
        deadline = time.monotonic() + 3.0
        for proc in (self.node, self.orca, self.speechd):
            if proc is None or proc.poll() is not None:
                continue
            try:
                proc.wait(timeout=max(0.05, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self._signal_group(proc, signal.SIGKILL)
        if self._session_lock_fd is not None:
            try:
                os.close(self._session_lock_fd)
            except OSError:
                pass
            self._session_lock_fd = None
        shutil.rmtree(self.work_dir, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", type=Path, required=True)
    args = parser.parse_args(argv)
    driver = OrcaBridge(args.bridge.resolve())

    def stop(_signum, _frame):
        # A protocol close can already be inside its bounded child waits when
        # the outer exact-tree supervisor's grace expires.  Re-entering close
        # is harmless, but raising SystemExit from that nested signal handler
        # used to interrupt the original close before its temp-tree removal.
        # Let the in-flight owner finish; the outer supervisor still has a
        # later SIGKILL bound if it genuinely wedges.
        if driver._closed:
            return
        driver.close()
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return driver.serve()
    finally:
        driver.close()


if __name__ == "__main__":
    raise SystemExit(main())
