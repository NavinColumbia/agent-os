#!/usr/bin/env python3
# Claude Code Notification/Stop hook -> ntfy push. Reads hook JSON on stdin.
import sys, json, subprocess, os
root = os.path.expanduser("~/projects/agent-os")
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
msg = d.get("message") or "Claude needs your attention — open the session."
subprocess.run([os.path.join(root, ".venv/bin/python"),
                os.path.join(root, "scripts/notify.py"),
                "--title", "Claude 🔔", "--priority", "high", msg],
               check=False)
