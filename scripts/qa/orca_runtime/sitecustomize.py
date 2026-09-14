"""Make only Orca's configured debug stream line-buffered.

Orca opens ``--debug-file`` itself without an explicit buffering policy.  On a regular file that can delay a
real speech utterance for minutes, long after QA has judged the product.  This module is injected only into the
isolated Orca process and only alters the one exact owner-generated path supplied in the environment.
"""
from __future__ import annotations

import builtins
import os

_REAL_OPEN = builtins.open
_TARGET = os.path.abspath(os.environ.get("AOS_ORCA_DEBUG_PATH", "")) \
    if os.environ.get("AOS_ORCA_DEBUG_PATH") else ""


def _line_buffered_open(file, mode="r", buffering=-1, encoding=None, errors=None,
                        newline=None, closefd=True, opener=None):
    try:
        is_target = _TARGET and os.path.abspath(os.fspath(file)) == _TARGET
    except TypeError:
        is_target = False
    if is_target and "b" not in mode and any(flag in mode for flag in ("w", "a", "+")):
        buffering = 1
    return _REAL_OPEN(file, mode, buffering, encoding, errors, newline, closefd, opener)


builtins.open = _line_buffered_open
