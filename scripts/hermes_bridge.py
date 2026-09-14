#!/usr/bin/env python3
"""Governed optional bridge from Agent OS to a local Hermes Agent installation.

Hermes is useful as a low-cost, session-capable specialist at the edge of Agent OS.  It is deliberately not a
drop-in replacement for the factory core: the local Hermes profile owns platform credentials, and its broad
file tool does not expose a read-only mode.  This bridge therefore admits only platform-internal,
self-contained judgment/research roles with minimal toolsets.  Tenant and code-mutation work fail closed.

Before routing work, an operator runs ``hermes_bridge.py canary``.  The durable, short-lived canary prevents a
nominally installed CLI with expired auth, empty credits, or a broken provider from consuming orchestration
shifts.  Agent OS falls back to its normal engine when readiness is false.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANARY_PATH = ROOT / ".runtime" / "hermes-canary.json"
CANARY_MAX_AGE_S = max(60, min(604800, int(os.environ.get(
    "AOS_HERMES_CANARY_MAX_AGE_S", "86400"))))
DEFAULT_TIMEOUT_S = max(15, min(1800, int(os.environ.get("AOS_HERMES_TIMEOUT_S", "240"))))
DEFAULT_MAX_TURNS = max(1, min(20, int(os.environ.get("AOS_HERMES_MAX_TURNS", "6"))))

_READ_ONLY_ROLES = frozenset({
    "market-analyst", "qa-evidence-reviewer", "qa-impact-analyst", "qa-manager",
    "researcher", "reviewer", "senior-qa-director",
})
_WEB_ROLES = frozenset({"market-analyst", "researcher"})
_ALLOWED_TOOLSETS = frozenset({"clarify", "web"})
_CREDIT_FAILURES = (
    "insufficient credits", "out of credits", "usage limit", "usage-credits", "http 402", "code: 402",
)
_AUTH_FAILURES = (
    "authentication", "invalid api key", "not logged in", "login required", "unauthorized", "http 401",
)


def _enabled():
    return os.environ.get("AOS_HERMES_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _hermes_binary():
    override = os.environ.get("AOS_HERMES_BIN", "").strip()
    return override or shutil.which("hermes")


def _read_canary(now=None):
    try:
        record = json.loads(CANARY_PATH.read_text())
    except (OSError, TypeError, ValueError):
        return None
    current = time.time() if now is None else float(now)
    checked_at = float(record.get("checked_at") or 0)
    record["fresh"] = bool(record.get("ok") and checked_at > 0
                           and current - checked_at <= CANARY_MAX_AGE_S)
    return record


def status(*, require_canary=True, now=None):
    binary = _hermes_binary()
    result = {"enabled": _enabled(), "installed": bool(binary), "binary": binary,
              "canary": _read_canary(now)}
    if not binary:
        return {**result, "ready": False, "reason": "Hermes CLI is not installed"}
    try:
        check = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=8)
        version_text = ((check.stdout or "") + "\n" + (check.stderr or "")).strip()
        result["version"] = version_text[:500]
        if check.returncode != 0:
            return {**result, "ready": False, "reason": "Hermes version check failed"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {**result, "ready": False, "reason": str(exc)[:500]}
    if not result["enabled"]:
        return {**result, "ready": False, "reason": "AOS_HERMES_ENABLED is not enabled"}
    if require_canary and not ((result.get("canary") or {}).get("fresh")):
        reason = ((result.get("canary") or {}).get("reason")
                  or "no fresh successful Hermes provider canary")
        return {**result, "ready": False, "reason": reason}
    return {**result, "ready": True, "reason": None}


def _role_toolsets(role, requested=None):
    if str(role or "") not in _READ_ONLY_ROLES:
        return None
    defaults = ["clarify", "web"] if role in _WEB_ROLES else ["clarify"]
    values = defaults if requested is None else [str(item).strip() for item in requested]
    values = [item for item in values if item]
    if not values or any(item not in _ALLOWED_TOOLSETS for item in values):
        return None
    return list(dict.fromkeys(values))


def _parse_session_id(text):
    matches = re.findall(r"(?m)^session_id:\s*([A-Za-z0-9_-]+)\s*$", str(text or ""))
    return matches[-1] if matches else None


def _final_text(stdout):
    lines = [line for line in str(stdout or "").splitlines()
             if not re.fullmatch(r"session_id:\s*[A-Za-z0-9_-]+\s*", line)]
    return "\n".join(lines).strip()


def _session_usage(binary, session_id, env):
    if not session_id:
        return {}
    try:
        exported = subprocess.run(
            [binary, "sessions", "export", "-", "--session-id", session_id],
            capture_output=True, text=True, timeout=20, env=env)
        if exported.returncode != 0:
            return {}
        record = json.loads(exported.stdout)
        return {
            "tokens_in": int(record.get("input_tokens") or 0),
            "tokens_out": int(record.get("output_tokens") or 0),
            "cost_usd": float(record.get("actual_cost_usd")
                              if record.get("actual_cost_usd") is not None
                              else record.get("estimated_cost_usd") or 0.0),
            "model": record.get("model"),
            "provider": record.get("billing_provider"),
        }
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return {}


def _invoke(role, repo, prompt, *, timeout=None, model=None, toolsets=None, max_turns=None):
    binary = _hermes_binary()
    if not binary:
        return {"rc": 127, "failed": True, "reason": "Hermes CLI is not installed",
                "provider_unavailable": True, "engine": "hermes"}
    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        return {"rc": 2, "failed": True, "reason": f"repository does not exist: {repo_path}",
                "provider_unavailable": False, "engine": "hermes"}
    selected_tools = _role_toolsets(role, toolsets)
    if selected_tools is None:
        return {"rc": 2, "failed": True,
                "reason": f"Hermes role/toolset is not read-only approved: {role}",
                "provider_unavailable": False, "engine": "hermes"}
    selected_model = str(model or os.environ.get("AOS_HERMES_MODEL", "")).strip()
    cmd = [binary, "chat", "-Q", "--source", "tool", "--ignore-rules",
           "--max-turns", str(max_turns or DEFAULT_MAX_TURNS),
           "-t", ",".join(selected_tools)]
    if selected_model:
        cmd += ["-m", selected_model]
    cmd += ["-q", str(prompt)]
    env = {**os.environ}
    timeout_s = DEFAULT_TIMEOUT_S if timeout is None else max(1, int(timeout))
    tmp = Path(tempfile.mkdtemp(prefix="aos-hermes-"))
    stdout_path, stderr_path = tmp / "stdout.txt", tmp / "stderr.txt"
    started = time.monotonic()
    try:
        import claude_gate
        import clauded
        with claude_gate.agent_slot(
                claude_gate.process_holder("hermes", role), lease_s=max(60, timeout_s + 60)) as lease:
            with stdout_path.open("w", encoding="utf-8") as stdout_stream, \
                    stderr_path.open("w", encoding="utf-8") as stderr_stream:
                proc = clauded.run_owned(
                    cmd, owner=f"factory-hermes:{role}:{os.getpid()}", lease=lease,
                    cwd=str(repo_path), stdout=stdout_stream, stderr=stderr_stream,
                    text=True, timeout=timeout_s, env=env)
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        session_id = _parse_session_id(stdout) or _parse_session_id(stderr)
        usage = _session_usage(binary, session_id, env)
        answer = _final_text(stdout)
        combined = (answer or stderr or stdout).strip()[:20000]
        lower = (stdout + "\n" + stderr).lower()
        exhausted = any(token in lower for token in _CREDIT_FAILURES)
        auth_failed = any(token in lower for token in _AUTH_FAILURES)
        succeeded = proc.returncode == 0 and bool(answer) and not (exhausted or auth_failed)
        reason = None
        if not succeeded:
            reason = ("Hermes provider credits exhausted" if exhausted else
                      "Hermes provider authentication unavailable" if auth_failed else
                      "Hermes produced no final response" if not answer else "Hermes execution failed")
        return {
            "rc": 0 if succeeded else (proc.returncode or 1),
            "out": answer[-1500:] if succeeded else combined[-1500:],
            "out_full": answer if succeeded else combined,
            "failed": not succeeded,
            "reason": reason,
            "provider_unavailable": bool(exhausted or auth_failed),
            "provider_exhausted": exhausted,
            "provider_auth_unavailable": auth_failed,
            "engine": "hermes", "session_id": session_id,
            "elapsed_s": time.monotonic() - started,
            "tokens_in": usage.get("tokens_in", 0), "tokens_out": usage.get("tokens_out", 0),
            "cost_usd": usage.get("cost_usd", 0.0),
            "model": usage.get("model") or selected_model or "hermes-default",
            "provider": usage.get("provider"), "toolsets": selected_tools,
        }
    except subprocess.TimeoutExpired:
        return {"rc": 124, "failed": True, "reason": f"Hermes timed out after {timeout_s}s",
                "provider_unavailable": True, "engine": "hermes",
                "elapsed_s": time.monotonic() - started, "model": selected_model or "hermes-default"}
    except Exception as exc:
        return {"rc": 1, "failed": True, "reason": str(exc)[:1000],
                "provider_unavailable": True, "engine": "hermes",
                "elapsed_s": time.monotonic() - started, "model": selected_model or "hermes-default"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(role, repo, prompt, *, tenant_id=None, timeout=None, model=None, toolsets=None,
        require_canary=True):
    if tenant_id:
        return {"rc": 2, "failed": True,
                "reason": "Hermes host credentials are not isolated for tenant work",
                "provider_unavailable": False, "engine": "hermes"}
    readiness = status(require_canary=require_canary)
    if not readiness.get("ready"):
        return {"rc": 1, "failed": True, "reason": readiness.get("reason"),
                "provider_unavailable": True, "engine": "hermes", "readiness": readiness}
    return _invoke(role, repo, prompt, timeout=timeout, model=model, toolsets=toolsets)


def canary(*, repo=ROOT, model=None):
    # Canary intentionally bypasses the enable/readiness gate: it is what establishes readiness.
    result = _invoke("qa-evidence-reviewer", repo, "Return exactly HERMES_OK and nothing else.",
                     timeout=60, model=model, toolsets=["clarify"], max_turns=1)
    ok = result.get("rc") == 0 and result.get("out_full", "").strip() == "HERMES_OK"
    record = {"ok": ok, "checked_at": time.time(), "model": result.get("model"),
              "provider": result.get("provider"), "session_id": result.get("session_id"),
              "elapsed_s": result.get("elapsed_s"),
              "reason": None if ok else result.get("reason"), "rc": result.get("rc")}
    CANARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CANARY_PATH.with_name(f".{CANARY_PATH.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True))
    tmp.replace(CANARY_PATH)
    return {**record, "result": result}


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    command = argv[0] if argv else "status"
    if command == "status":
        print(json.dumps(status(), indent=2, default=str))
        return 0
    if command == "canary":
        result = canary(model=argv[1] if len(argv) > 1 else None)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1
    raise SystemExit("usage: hermes_bridge.py status|canary [model]")


if __name__ == "__main__":
    raise SystemExit(main())
