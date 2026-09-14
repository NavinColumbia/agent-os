#!/usr/bin/env python3
"""QA artifact paths.

The QA loop must leave human-inspectable evidence: reports, screenshots, event logs, and verdicts.
Default to a Windows-visible folder when running under WSL so the owner can open the artifacts directly.
"""
import os
import re
import signal
import shutil
import sys
import time
import json
from contextlib import contextmanager
from pathlib import Path

_TRANSCODE_TIMEOUT_S = int(os.environ.get("AOS_QA_TRANSCODE_TIMEOUT_S", "180"))
_STITCH_TIMEOUT_S = int(os.environ.get("AOS_QA_STITCH_TIMEOUT_S", "60"))
_MEDIA_PROBE_TIMEOUT_S = int(os.environ.get("AOS_QA_MEDIA_PROBE_TIMEOUT_S", "10"))
_MEDIA_DECODE_TIMEOUT_S = int(os.environ.get("AOS_QA_MEDIA_DECODE_TIMEOUT_S", "90"))
_ENCODER_WAIT_S = max(0, int(os.environ.get("AOS_QA_ENCODER_WAIT_S", "15")))


@contextmanager
def _media_admission(label):
    """Share cross-process browser/media capacity; missing evidence remains fail-soft."""
    slot = None
    gate = None
    try:
        import browser_gate
        gate = browser_gate
        slot = gate.acquire(gate.qa_holder(f"media:{label}"), wait_s=_ENCODER_WAIT_S,
                            resource_kind="media")
    except Exception:
        gate = None
        slot = None
    try:
        yield slot
    finally:
        if gate is not None and slot is not None:
            try:
                gate.release(slot)
            except Exception:
                pass


def _run_media_owned(args, admission, *, timeout):
    """Run one encoder under the same fenced generation as its weighted admission."""
    import browser_gate
    import clauded
    lease = browser_gate.fencing_lease(admission)
    return clauded.run_owned(
        args, owner=f"qa-media:{os.getpid()}:{Path(args[-1]).name}", lease=lease,
        check=True, timeout=timeout, **_ENCODER_CHILD_KWARGS)


def _parent_death_signal():
    """Make an encoder die with its QA worker instead of being reparented to WSL init.

    The fenced runner cleans up when *it* times out, but cannot do so when the
    entire worker is terminated by the outer QA slice deadline.  Linux PR_SET_PDEATHSIG closes
    that hole.  The parent check handles the small fork-to-prctl race.
    """
    if not sys.platform.startswith("linux"):
        return
    import ctypes
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() != parent:
        os.kill(os.getpid(), signal.SIGTERM)


_ENCODER_CHILD_KWARGS = {"preexec_fn": _parent_death_signal} if sys.platform.startswith("linux") else {}


def probe_media(path):
    """Return bounded, machine-verifiable media metadata, or ``None`` for a corrupt/partial file."""
    path = Path(path)
    if not shutil.which("ffprobe") or not path.is_file() or path.stat().st_size <= 0:
        return None
    try:
        import clauded
        completed = clauded.run_owned(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration,format_name,size:stream=codec_name,width,height",
             "-of", "json", str(path)], owner=f"qa-media-probe:{os.getpid()}:{path.name}",
            timeout=max(1, _MEDIA_PROBE_TIMEOUT_S), capture_output=True, text=True,
            **_ENCODER_CHILD_KWARGS)
        if completed.returncode != 0:
            return None
        payload = json.loads(completed.stdout or "{}")
        duration = float((payload.get("format") or {}).get("duration") or 0)
        streams = list(payload.get("streams") or [])
        if duration <= 0 or not streams:
            return None
        return {
            "path": str(path), "bytes": path.stat().st_size, "duration_s": round(duration, 3),
            "format": (payload.get("format") or {}).get("format_name"), "streams": streams,
        }
    except Exception:
        return None


def validate_media(path, *, decode=True):
    """Prove a recording is structurally readable and, by default, decodes from start to finish."""
    facts = probe_media(path)
    if facts is None:
        return None
    facts["decode_verified"] = False
    if not decode:
        return facts
    if not shutil.which("ffmpeg"):
        return None
    try:
        with _media_admission(f"verify:{Path(path).name}") as admitted:
            if not admitted:
                return None
            _run_media_owned(
                ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], admitted,
                timeout=max(1, _MEDIA_DECODE_TIMEOUT_S))
        facts["decode_verified"] = True
        return facts
    except Exception:
        return None


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
    # Record screenshots/video/checkpoints on native Linux storage. Direct writes to /mnt/c traverse WSL's
    # Plan9 bridge; live proof repeatedly left the parent in uninterruptible p9_client_rpc waits and kept an
    # explorer slot occupied minutes after browser work ended. A separate evidence publisher can copy the
    # final, closed bundle to the Windows-visible root once.
    return Path(os.environ.get("AOS_QA_STAGING_DIR", os.environ.get("AOS_QA_DIR", "/tmp/aos-qa-staging")))


def search_roots():
    """Current staging root plus the legacy/public root for crash-resume compatibility."""
    roots = [root()]
    public = _windows_visible_root()
    if public and public not in roots:
        roots.append(public)
    return roots


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
def webm_to_mp4_result(webm, out_mp4):
    """Return a reasoned encode outcome so background publishers can distinguish capacity from corruption."""
    webm, out_mp4 = Path(webm), Path(out_mp4)
    if os.environ.get("AOS_QA_DISABLE_TRANSCODE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return {"status": "disabled", "reason": "transcoding disabled"}
    if not shutil.which("ffmpeg"):
        return {"status": "failed", "reason": "ffmpeg unavailable"}
    if not webm.exists() or webm.stat().st_size == 0:
        return {"status": "failed", "reason": "raw WebM is missing or empty"}
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_mp4.unlink(missing_ok=True)
    except OSError:
        return {"status": "failed", "reason": "output path is not writable"}
    try:
        with _media_admission(webm.name) as admitted:
            if not admitted:
                return {"status": "deferred", "reason": "media capacity unavailable"}
            _run_media_owned(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm),
                 # Keep evidence encoding from consuming every WSL CPU while the live QA workers are active.
                 "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p",
                 # libx264+yuv420p needs even dimensions; pad odd sizes up by a pixel so any capture encodes.
                 "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                 "-movflags", "+faststart", str(out_mp4)],
                admitted, timeout=_TRANSCODE_TIMEOUT_S)
    except Exception as exc:
        try:
            out_mp4.unlink(missing_ok=True)
        except OSError:
            pass
        return {"status": "failed", "reason": f"encoder failed: {str(exc)[:300]}"}
    facts = probe_media(out_mp4)
    if facts is None:
        try:
            out_mp4.unlink(missing_ok=True)
        except OSError:
            pass
        return {"status": "failed", "reason": "encoded MP4 failed media probe"}
    return {"status": "done", "path": out_mp4, "result": facts}


def webm_to_mp4(webm, out_mp4):
    """Compatibility wrapper returning the Path on success and ``None`` otherwise."""
    result = webm_to_mp4_result(webm, out_mp4)
    return result.get("path") if result.get("status") == "done" else None


def stitch_mp4s(mp4s, out_mp4):
    """Concatenate per-story .mp4 clips into ONE scrollable session recording (the whole QA run, end to
    end). Fail-open: returns None if there is nothing to stitch or ffmpeg is unavailable."""
    if os.environ.get("AOS_QA_DISABLE_TRANSCODE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return None
    clips = [Path(m) for m in mp4s if m and probe_media(m) is not None]
    if not clips or not shutil.which("ffmpeg"):
        return None
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    if len(clips) == 1:
        try:
            shutil.copyfile(clips[0], out_mp4)
            if probe_media(out_mp4) is not None:
                return out_mp4
            out_mp4.unlink(missing_ok=True)
            return None
        except Exception:
            return None
    listing = out_mp4.parent / "_concat.txt"
    try:
        with _media_admission("stitch") as admitted:
            if not admitted:
                return None
            listing.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips))
            # All clips share the 1280x800 recording size, so a stream copy concat works and is instant.
            try:
                _run_media_owned(
                    ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                     "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(out_mp4)],
                    admitted, timeout=_STITCH_TIMEOUT_S)
            except Exception:                          # differing clips -> re-encode fallback
                _run_media_owned(
                    ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                     "-i", str(listing), "-c:v", "libx264", "-threads", "1",
                     "-preset", "veryfast", "-crf", "23",
                     "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_mp4)],
                    admitted, timeout=_STITCH_TIMEOUT_S)
    except Exception:
        try:
            out_mp4.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    finally:
        try:
            listing.unlink()
        except Exception:
            pass
    if probe_media(out_mp4) is None:
        try:
            out_mp4.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return out_mp4
