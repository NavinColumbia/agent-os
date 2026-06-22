#!/usr/bin/env bash
# Runs as ROOT at WSL boot via /etc/wsl.conf [boot] command. Idempotent.
# Brings back docker + tailscaled; containers auto-start (restart:unless-stopped);
# starts the reply listener as swami. Logs to /var/log/agentos-boot.log.
exec >>/var/log/agentos-boot.log 2>&1
echo "=== agentos-boot $(date) ==="

# Docker daemon (no systemd in this WSL)
service docker start

# Tailscale daemon — reconnects + resumes 'serve' from saved state on its own
mkdir -p /var/run/tailscale /var/lib/tailscale
if ! pgrep -x tailscaled >/dev/null; then
  setsid tailscaled --state=/var/lib/tailscale/tailscaled.state \
    --socket=/var/run/tailscale/tailscaled.sock >/var/log/tailscaled.log 2>&1 </dev/null &
fi

# Reply listener as swami (containers come back by themselves once docker is up)
su - swami -c 'bash /home/swami/projects/agent-os/scripts/recover.sh' || true
echo "=== agentos-boot done $(date) ==="
