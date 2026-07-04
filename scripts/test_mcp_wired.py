#!/usr/bin/env python3
"""test_mcp_wired.py — guard: the A4 MCP tier stays WIRED, not just declared.

governance ALREADY gates mcp__* tools (mcp__slack__post_message, mcp__deploy__release, ...) in
approval_required_for — but for years factory never passed --mcp-config, so no MCP server was ever spawned
and those tools didn't exist for any agent (a declared-not-wired hole). This test FAILS if that wiring
regresses: the catalog, the per-role config builder, or the _run_once consumer that actually hands
--mcp-config to `claude -p`. Source-probe (no heavy factory import) per the STANDARDS-verification pattern.

    python test_mcp_wired.py     # prints PASS / FAIL
Run with the agent-os venv python.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
FACTORY = (SCRIPTS / "factory.py").read_text()
GOV = (SCRIPTS / "governance.py").read_text()

ok = True


def chk(cond, label):
    global ok
    print(("PASS" if cond else "FAIL") + f": {label}")
    ok = ok and bool(cond)


# 1) the DECLARATION side still exists: governance gates mcp__* tools (the reason wiring must exist).
chk("mcp__" in GOV, "governance still gates mcp__* tools (the capability that needs a live server)")

# 2) the catalog of vetted MCP servers exists and has at least the filesystem server (verified e2e).
chk("_MCP_CATALOG" in FACTORY and "server-filesystem" in FACTORY,
    "factory._MCP_CATALOG exists with a vetted server (filesystem)")

# 3) the per-role config builder exists and reads the manifest's mcp_servers.
chk("def _role_mcp_config" in FACTORY and "mcp_servers" in FACTORY,
    "_role_mcp_config builds --mcp-config from a role manifest's mcp_servers")

# 4) THE CONSUMER: _run_once actually calls _role_mcp_config AND hands --mcp-config to claude -p. Without
#    this the whole tier is dead again. Probe the _run_once body specifically.
run_once = FACTORY.split("def _run_once(", 1)[-1].split("\ndef ", 1)[0]
chk("_role_mcp_config(" in run_once, "_run_once CONSUMES _role_mcp_config (not just defines it)")
chk('"--mcp-config"' in run_once or "'--mcp-config'" in run_once,
    "_run_once passes --mcp-config to the agent (the MCP servers actually get spawned)")

# 5) fail-safe: the temp config is cleaned up (no /tmp leak — we built a browser/scratch reaper, don't add leaks).
chk("os.unlink" in run_once and ("finally" in run_once),
    "the temp --mcp-config file is cleaned up in a finally (no /tmp leak)")

print("PASS: A4 MCP tier is wired end-to-end (catalog -> per-role config -> _run_once hands --mcp-config)"
      if ok else "FAIL: MCP tier wiring regressed")
sys.exit(0 if ok else 1)
