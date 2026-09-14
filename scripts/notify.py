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

from aoscfg import ENV as ENV_LOCAL


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


def send(message, title="agent-os", priority="default", tags="", topic=""):
    """Importable notifier (agent -> your phone). Best-effort: returns True/False, never raises —
    so a down ntfy never breaks the caller. Reuses the secret-guard so we never leak over the bridge."""
    try:
        if os.environ.get("AOS_DISABLE_EXTERNAL_NOTIFICATIONS", "").strip().lower() in {
                "1", "true", "yes", "on"}:
            return False
        cfg = load_env()
        topic = topic or cfg.get("NTFY_TOPIC", "")
        if not topic or "CHANGE-ME" in topic:
            return False
        sec, _ = looks_like_secret(message)
        if sec:
            return False
        prio = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}.get(priority, 3)
        payload = {"topic": topic, "message": message, "title": title, "priority": prio}
        if tags:
            payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        r = requests.post(cfg["NTFY_BASE_URL"].rstrip("/"), json=payload, timeout=10)
        return r.status_code < 400
    except Exception:
        return False


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

    # Publish via JSON API: UTF-8 body, so emoji/unicode in title/message are safe
    # (HTTP headers are latin-1-only and would crash on emoji).
    prio = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}[args.priority]
    payload = {"topic": topic, "message": args.message, "title": args.title, "priority": prio}
    if args.tags:
        payload["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    base = cfg["NTFY_BASE_URL"].rstrip("/")
    r = requests.post(base, json=payload, timeout=10)
    r.raise_for_status()
    print(f"sent -> {base}/{topic}  (priority={args.priority})")


if __name__ == "__main__":
    main()
