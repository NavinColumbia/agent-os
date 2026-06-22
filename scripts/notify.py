#!/usr/bin/env python3
"""notify.py — send a phone notification (agent -> you) via self-hosted ntfy.

Usage:
  notify.py "message"
  notify.py --title "Build failed" --priority high --tags warning "details..."

Reads NTFY_BASE_URL and NTFY_TOPIC from env or ~/projects/agent-os/.env.local.
Refuses to SEND anything that looks like a secret (so a key never leaves the host
via the bridge). Binds to nothing — it's an outbound client to localhost ntfy.
"""
import argparse
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secret_filter import looks_like_secret  # noqa: E402

ENV_LOCAL = Path.home() / "projects" / "agent-os" / ".env.local"


def load_env():
    cfg = {}
    if ENV_LOCAL.exists():
        for line in ENV_LOCAL.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    cfg.setdefault("NTFY_BASE_URL", os.environ.get("NTFY_BASE_URL", "http://localhost:8080"))
    cfg.setdefault("NTFY_TOPIC", os.environ.get("NTFY_TOPIC", ""))
    # env overrides file
    if os.environ.get("NTFY_BASE_URL"):
        cfg["NTFY_BASE_URL"] = os.environ["NTFY_BASE_URL"]
    if os.environ.get("NTFY_TOPIC"):
        cfg["NTFY_TOPIC"] = os.environ["NTFY_TOPIC"]
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("message")
    ap.add_argument("--title", default="agent-os")
    ap.add_argument("--priority", default="default",
                    choices=["min", "low", "default", "high", "urgent"])
    ap.add_argument("--tags", default="")
    ap.add_argument("--topic", default="")
    args = ap.parse_args()

    cfg = load_env()
    topic = args.topic or cfg["NTFY_TOPIC"]
    if not topic or "CHANGE-ME" in topic:
        sys.exit("ERROR: NTFY_TOPIC not set. Fill it in ~/projects/agent-os/.env.local")

    sec, why = looks_like_secret(args.message)
    if sec:
        sys.exit(f"REFUSED: message looks like a secret ({why}); not sending over the bridge.")

    url = f"{cfg['NTFY_BASE_URL'].rstrip('/')}/{topic}"
    headers = {"Title": args.title, "Priority": args.priority}
    if args.tags:
        headers["Tags"] = args.tags
    r = requests.post(url, data=args.message.encode(), headers=headers, timeout=10)
    r.raise_for_status()
    print(f"sent -> {url}  (priority={args.priority})")


if __name__ == "__main__":
    main()
