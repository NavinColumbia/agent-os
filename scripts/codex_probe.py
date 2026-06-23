#!/usr/bin/env python3
"""codex_probe.py — behavior-test an agent CLI (Codex) so the OS knows its quirks before trusting it.

Different agent CLIs behave differently (Codex wants a git repo, has its own sandbox modes, streams
JSONL events, reports tokens not USD). This probes the actual runtime behavior — responsiveness, whether
it HANGS on a blocked action, whether it writes files non-interactively, whether we can capture tokens,
and whether it exits cleanly with no background residue — and prints a behavior profile + verdict. The
factory uses these facts to drive Codex safely (timeouts/retries already cover the worst case).

    codex_probe.py            # run the probes, print profile + verdict
Run with the agent-os venv python. Requires `codex` authenticated.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _exec(prompt, timeout, sandbox="workspace-write"):
    d = Path(tempfile.mkdtemp(prefix="codexprobe-"))
    subprocess.run(["git", "init", "-q"], cwd=str(d))
    out = d / "last.txt"
    t0 = time.time()
    timed_out, rc, events = False, -1, ""
    try:
        p = subprocess.run(["codex", "exec", "--skip-git-repo-check", "-s", sandbox, "--json",
                            "-o", str(out), prompt], cwd=str(d), capture_output=True, text=True, timeout=timeout)
        rc, events = p.returncode, p.stdout
    except subprocess.TimeoutExpired:
        timed_out = True
    elapsed = round(time.time() - t0, 1)
    tokens = 0
    for line in (events or "").splitlines():
        try:
            o = json.loads(line)
            if o.get("type") == "turn.completed":
                u = o.get("usage", {})
                tokens = int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
        except Exception:
            pass
    last = (out.read_text().strip() if out.exists() else "")
    wrote = (d / "probe.py").exists()
    shutil.rmtree(d, ignore_errors=True)
    return {"rc": rc, "elapsed": elapsed, "timed_out": timed_out, "tokens": tokens, "last": last[:120], "wrote_file": wrote}


def probe():
    prof = {}
    prof["responsive"] = _exec("Reply with exactly: PROBE_OK", 60)
    prof["writes_files"] = _exec("Create a file named probe.py containing exactly: VALUE = 42  — then stop.", 90)
    prof["no_hang_on_block"] = _exec("Try to run: curl -s https://example.com . If it fails or is blocked, reply BLOCKED and stop.", 90)
    lingering = subprocess.run(["pgrep", "-fc", "codex exec"], capture_output=True, text=True).stdout.strip()
    prof["lingering_procs"] = int(lingering or "0")
    return prof


def _main(a):
    p = probe()
    print("\n=== Codex behavior profile ===")
    print(f"  responsive:        rc={p['responsive']['rc']} {p['responsive']['elapsed']}s  ('{p['responsive']['last']}')")
    print(f"  writes files:      {p['writes_files']['wrote_file']}  ({p['writes_files']['elapsed']}s)")
    print(f"  hangs on block?:   {'YES (timed out!)' if p['no_hang_on_block']['timed_out'] else 'no — graceful'}  ({p['no_hang_on_block']['elapsed']}s)")
    print(f"  token capture:     {p['responsive']['tokens']} tokens (from turn.completed)")
    print(f"  background residue:{p['lingering_procs']} lingering codex procs")
    ok = (p["responsive"]["rc"] == 0 and p["writes_files"]["wrote_file"] and not p["no_hang_on_block"]["timed_out"]
          and p["responsive"]["tokens"] > 0 and p["lingering_procs"] == 0)
    print("\nPASS: Codex is well-behaved for orchestration (blocks, no hang, files, tokens, clean exit) ✅"
          if ok else "FAIL: Codex showed a problematic behavior — see profile")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
