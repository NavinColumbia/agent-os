#!/usr/bin/env python3
"""voice_to_agent.py — transcribe audio, then SAFELY route it to an agent.

Security model (matches the typed path's gating):
  * SAFE transcript  -> delivered to the agent as input (tmux send-keys or a FIFO),
                        and the exact delivered text is echoed back to you.
  * GATED/risky      -> NOT delivered. The transcript is echoed back to you (stdout +
                        phone notification) and parked in bridge/pending/<task>.voice.
                        You must confirm it through the normal approval path (e.g. reply
                        on the ntfy bridge), exactly like a typed gated command.

Voice is therefore an INPUT method, never an auto-executor of gated actions.

    voice_to_agent.py <audio> --tmux <session:win.pane>     # deliver to a tmux pane
    voice_to_agent.py <audio> --fifo <path>                 # deliver to a FIFO
    voice_to_agent.py <audio> --print                       # just show routing decision
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path.home() / "projects" / "agent-os"
sys.path.insert(0, str(ROOT / "scripts"))
from transcribe import transcribe_file          # noqa: E402
from voice_guard import classify                 # noqa: E402

PENDING = ROOT / "bridge" / "pending"
VENV_PY = ROOT / ".venv" / "bin" / "python"


def notify_phone(text, title="voice ❓", priority="high"):
    """Echo back to the phone via the Step 2 notify helper (best effort)."""
    try:
        subprocess.run([str(VENV_PY), str(ROOT / "scripts" / "notify.py"),
                        "--title", title, "--priority", priority, text],
                       check=False, timeout=15)
    except Exception:
        pass


def deliver_tmux(target, text):
    # send the line, then Enter, as if typed into the agent's prompt
    subprocess.run(["tmux", "send-keys", "-t", target, text], check=True)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)


def deliver_fifo(path, text):
    with open(path, "w") as f:
        f.write(text + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--tmux", help="tmux target, e.g. voice-demo:0.0")
    ap.add_argument("--fifo", help="path to a FIFO/file to write the transcript to")
    ap.add_argument("--task", default="voice", help="task name for parked gated transcripts")
    ap.add_argument("--print", action="store_true", dest="just_print")
    args = ap.parse_args()

    transcript = transcribe_file(args.audio)
    print(f"[transcript] {transcript!r}")

    gated, reason = classify(transcript)
    if gated:
        PENDING.mkdir(parents=True, exist_ok=True)
        parked = PENDING / f"{args.task}.voice"
        parked.write_text(transcript + "\n")
        msg = (f"⛔ GATED voice command (NOT executed) — reason: {reason}\n"
               f"heard: \"{transcript}\"\n"
               f"confirm via the normal approval path to proceed.")
        print(msg)
        notify_phone(f"Confirm? heard: \"{transcript}\" (gated: {reason})")
        print(f"[parked] {parked}  (awaiting your confirmation)")
        return

    # SAFE path
    if args.just_print or (not args.tmux and not args.fifo):
        print(f"[SAFE] would deliver to agent: {transcript!r}")
        return
    if args.tmux:
        deliver_tmux(args.tmux, transcript)
        print(f"[SAFE] delivered to tmux {args.tmux}; echo: {transcript!r}")
    if args.fifo:
        deliver_fifo(args.fifo, transcript)
        print(f"[SAFE] delivered to FIFO {args.fifo}; echo: {transcript!r}")


if __name__ == "__main__":
    main()
