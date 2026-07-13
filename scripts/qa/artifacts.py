#!/usr/bin/env python3
"""QA artifact paths.

The QA loop must leave human-inspectable evidence: reports, screenshots, event logs, and verdicts.
Default to a Windows-visible folder when running under WSL so the owner can open the artifacts directly.
"""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


def _windows_visible_root() -> Path | None:
    users = Path("/mnt/c/Users")
    if not users.exists():
        return None
    preferred = os.environ.get("AOS_WINDOWS_USER")
    names = [preferred] if preferred else []
    names += ["navin", os.environ.get("USER", ""), "Public"]
    for name in names:
        if not name:
            continue
        home = users / name
        for base in ("Desktop", "Documents", "Downloads"):
            p = home / base
            if p.exists():
                return p / "agent-os-qa-evidence"
    return None


def root() -> Path:
    configured = os.environ.get("AOS_QA_EVIDENCE_DIR")
    if configured:
        return Path(configured).expanduser()
    return _windows_visible_root() or Path(os.environ.get("AOS_QA_DIR", "/tmp/aos-qa"))


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "app")).strip("-._")
    return (s or "app")[:80]


def run_dir(product: str = "app", ts: float | None = None) -> Path:
    ts = ts or time.time()
    d = root() / _slug(product) / time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
    d.mkdir(parents=True, exist_ok=True)
    (d / "screenshots").mkdir(parents=True, exist_ok=True)
    return d


# ── Video: Playwright records .webm (VP8/VP9) — many players (incl. Windows Media Player) can't open it and
# it scrubs badly. QA evidence must be reviewable like a real QA team's SESSION RECORDING, so we transcode to
# H.264 .mp4 with +faststart (seekable/scrollable everywhere). Fail-open everywhere: a video is EVIDENCE the
# CEO can watch, never a gate — a missing ffmpeg or a bad source must never fail a QA run.
def webm_to_mp4(webm, out_mp4):
    """Transcode one Playwright .webm to a widely-playable, seekable .mp4. Returns the Path on success,
    None if ffmpeg is missing / the source is empty / the encode fails."""
    webm, out_mp4 = Path(webm), Path(out_mp4)
    if not shutil.which("ffmpeg") or not webm.exists() or webm.stat().st_size == 0:
        return None
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
             # libx264+yuv420p needs even dimensions; pad odd sizes up by a pixel so any capture encodes.
             "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
             "-movflags", "+faststart", str(out_mp4)],
            check=True, timeout=240)
    except Exception:
        return None
    return out_mp4 if out_mp4.exists() and out_mp4.stat().st_size > 0 else None


def stitch_mp4s(mp4s, out_mp4):
    """Concatenate per-story .mp4 clips into ONE scrollable session recording (the whole QA run, end to
    end). Fail-open: returns None if there is nothing to stitch or ffmpeg is unavailable."""
    clips = [Path(m) for m in mp4s if m and Path(m).exists() and Path(m).stat().st_size > 0]
    if not clips or not shutil.which("ffmpeg"):
        return None
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    if len(clips) == 1:
        try:
            shutil.copyfile(clips[0], out_mp4)
            return out_mp4
        except Exception:
            return None
    listing = out_mp4.parent / "_concat.txt"
    try:
        listing.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips))
        # All clips share the 1280x800 recording size, so a stream copy concat works and is instant.
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                            "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(out_mp4)],
                           check=True, timeout=300)
        except Exception:                          # differing clips -> re-encode fallback
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                            "-i", str(listing), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_mp4)],
                           check=True, timeout=300)
    except Exception:
        return None
    finally:
        try:
            listing.unlink()
        except Exception:
            pass
    return out_mp4 if out_mp4.exists() and out_mp4.stat().st_size > 0 else None
