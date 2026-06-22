#!/usr/bin/env python3
"""agent_worker.py — invoke a REAL agent (headless Claude Code) to perform a task, GOVERNED by the
product repo's capability manifest (the enforce_manifest PreToolUse hook + sandbox) (ADR 0004).

This is the bridge from "Controller orchestrates stubs" to "Controller dispatches real work":
a stage hands a prompt to a real agent running in the product repo, where every tool call the
agent makes is gated by that repo's builder manifest and logged to the audit chain.

    agent_worker.py <product_repo> "<task prompt>"
Returns the agent's final text. Uses --permission-mode acceptEdits so the CC permission layer
auto-accepts edits while the enforce_manifest HOOK remains the real allow/deny gate.
Run with the agent-os venv python (or any python3).
"""
import subprocess
import sys
from pathlib import Path


def run_agent(repo: str, prompt: str, timeout: int = 240) -> dict:
    repo = str(Path(repo).expanduser())
    proc = subprocess.run(
        ["claude", "-p", prompt, "--permission-mode", "acceptEdits"],
        cwd=repo, capture_output=True, text=True, timeout=timeout,
    )
    return {"rc": proc.returncode, "out": (proc.stdout or "").strip(), "err": (proc.stderr or "").strip()[:500]}


def main(argv):
    if len(argv) < 2:
        sys.exit('usage: agent_worker.py <product_repo> "<task prompt>"')
    repo, prompt = argv[0], argv[1]
    res = run_agent(repo, prompt)
    print(f"[agent_worker] rc={res['rc']}")
    print(res["out"][-1500:])
    if res["err"]:
        print("[stderr]", res["err"][-300:], file=sys.stderr)
    sys.exit(res["rc"])


if __name__ == "__main__":
    main(sys.argv[1:])
