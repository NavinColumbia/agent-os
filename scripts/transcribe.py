#!/usr/bin/env python3
"""transcribe.py — local speech-to-text with faster-whisper (CPU, small.en).

Pure transcription: takes an audio file, returns text. Does NOT execute anything.
Model is the smallest English CPU model (small.en), int8 — downloaded once to
~/.cache and cached thereafter. Everything stays on this host (no cloud STT).

    transcribe.py <audio-file>            # prints transcript to stdout
    from transcribe import transcribe_file
"""
import sys
from functools import lru_cache

from faster_whisper import WhisperModel

MODEL = "small.en"


@lru_cache(maxsize=1)
def _model():
    # int8 on CPU = smallest/fastest; good enough for short commands.
    return WhisperModel(MODEL, device="cpu", compute_type="int8")


def transcribe_file(path):
    segments, _info = _model().transcribe(path, beam_size=1, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: transcribe.py <audio-file>")
    print(transcribe_file(sys.argv[1]))
