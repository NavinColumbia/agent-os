#!/usr/bin/env python3
"""reply_listener.py — watch the ntfy REPLY topic (phone -> agent) and capture commands.

Subscribes to  <NTFY_BASE_URL>/<NTFY_TOPIC>-reply/json  (newline-delimited JSON stream).
For each message:
  * if it looks like a secret/API key -> REFUSE: write to bridge/rejected/ and notify back.
  * otherwise -> write the command to bridge/inbox/<task>.cmd

The task name comes from the ntfy message Title (or a `task:` prefix in the body);
defaults to "default". Body form:
    task: deploy-fix
    restart the worker
-> writes "restart the worker" to inbox/deploy-fix.cmd

Connects only to localhost ntfy. Reconnects on stream drop.
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secret_filter import looks_like_secret  # noqa: E402

ROOT = Path.home() / "projects" / "agent-os"
INBOX = ROOT / "bridge" / "inbox"
REJECTED = ROOT / "bridge" / "rejected"
ENV_LOCAL = ROOT / ".env.local"


def load_env():
    cfg = {"NTFY_BASE_URL": "http://localhost:8080", "NTFY_TOPIC": ""}
    if ENV_LOCAL.exists():
        for line in ENV_LOCAL.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    for k in ("NTFY_BASE_URL", "NTFY_TOPIC"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def safe_task_name(name):
    keep = [c for c in (name or "").strip() if c.isalnum() or c in "-_."]
    out = "".join(keep).strip("._") or "default"
    return out[:64]


def parse_task_and_body(title, message):
    """Resolve (task, command) from a phone reply.

    Accepts, in priority order:
      1) ntfy Title field = task name, message = command.
      2) body starting with 'task: NAME' on line 1, command on the rest.
      3) labeled body 'Title: NAME\\nMessage: CMD' (what's easy to type in the app).
    Falls back to task 'default' with the whole body as the command.
    """
    body = (message or "").strip()
    task = (title or "").strip()

    if body.lower().startswith("task:"):
        first, _, rest = body.partition("\n")
        return safe_task_name(first.split(":", 1)[1]), rest.strip()

    # labeled "Title:/Message:" form
    lines = body.splitlines()
    labeled_task, labeled_cmd, used = "", [], False
    for ln in lines:
        low = ln.lower()
        if low.startswith("title:") and not labeled_task:
            labeled_task = ln.split(":", 1)[1].strip(); used = True
        elif low.startswith("message:") or low.startswith("msg:") or low.startswith("cmd:"):
            labeled_cmd.append(ln.split(":", 1)[1].strip()); used = True
        elif used:
            labeled_cmd.append(ln)
    if used and (labeled_task or labeled_cmd):
        return safe_task_name(labeled_task or task), "\n".join(labeled_cmd).strip()

    return safe_task_name(task), body


def notify_back(cfg, text):
    try:
        requests.post(f"{cfg['NTFY_BASE_URL'].rstrip('/')}/{cfg['NTFY_TOPIC']}",
                      data=text.encode(), headers={"Title": "bridge", "Priority": "high"},
                      timeout=10)
    except Exception:
        pass


def handle(cfg, ev):
    if ev.get("event") != "message":
        return
    title = ev.get("title", "")
    message = ev.get("message", "")
    task, body = parse_task_and_body(title, message)
    sec, why = looks_like_secret(message)
    if sec:
        REJECTED.mkdir(parents=True, exist_ok=True)
        # store ONLY a redacted note, never the secret itself
        (REJECTED / f"{task}.rejected").write_text(
            f"REFUSED reply for task '{task}': looks like a secret ({why}). Not stored.\n")
        print(f"[REFUSED] task={task} reason={why} (secret not written)")
        notify_back(cfg, f"⛔ refused: that reply for '{task}' looked like a secret ({why}). "
                         f"Not stored. Send the command without the credential.")
        return
    INBOX.mkdir(parents=True, exist_ok=True)
    out = INBOX / f"{task}.cmd"
    out.write_text(body + "\n")
    print(f"[CAPTURED] task={task} -> {out}  body={body!r}")


def main():
    cfg = load_env()
    if not cfg.get("NTFY_TOPIC") or "CHANGE-ME" in cfg["NTFY_TOPIC"]:
        sys.exit("ERROR: NTFY_TOPIC not set in ~/projects/agent-os/.env.local")
    reply_topic = f"{cfg['NTFY_TOPIC']}-reply"
    url = f"{cfg['NTFY_BASE_URL'].rstrip('/')}/{reply_topic}/json"
    print(f"listening on {url}  (inbox: {INBOX})")
    while True:
        try:
            with requests.get(url, stream=True, timeout=(10, None)) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        handle(cfg, json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except KeyboardInterrupt:
            print("\nstopped.")
            return
        except Exception as e:
            print(f"stream dropped ({e}); reconnecting in 3s...")
            time.sleep(3)


if __name__ == "__main__":
    main()
