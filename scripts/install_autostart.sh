#!/usr/bin/env bash
# install_autostart.sh — make agent-os auto-recover on every WSL boot.
# Installs the root boot script and registers it in /etc/wsl.conf [boot] command.
# Takes effect on the NEXT WSL start (after `wsl --shutdown` from Windows, or a Windows reboot).
set -eu
ROOT="$HOME/projects/agent-os"

sudo install -m 0755 "$ROOT/scripts/agentos-boot.sh" /usr/local/sbin/agentos-boot.sh
echo "installed /usr/local/sbin/agentos-boot.sh"

# Register [boot] command in /etc/wsl.conf without clobbering other sections.
if grep -q 'agentos-boot.sh' /etc/wsl.conf 2>/dev/null; then
  echo "/etc/wsl.conf already references agentos-boot.sh — leaving as is"
else
  python3 - <<'PY'
import re, pathlib
p = pathlib.Path("/etc/wsl.conf")
txt = p.read_text() if p.exists() else ""
line = "command = /usr/local/sbin/agentos-boot.sh"
if "[boot]" in txt:
    txt = re.sub(r"\[boot\]", "[boot]\n" + line, txt, count=1)
else:
    txt = (txt.rstrip() + "\n\n[boot]\n" + line + "\n").lstrip("\n")
import os, tempfile
tmp = tempfile.NamedTemporaryFile("w", delete=False); tmp.write(txt); tmp.close()
os.system(f"sudo cp {tmp.name} /etc/wsl.conf")
print("updated /etc/wsl.conf:\n" + txt)
PY
fi
echo "done. Auto-recovery active on next WSL boot. Test now without rebooting:"
echo "  sudo /usr/local/sbin/agentos-boot.sh"
