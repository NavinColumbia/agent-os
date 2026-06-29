#!/usr/bin/env python3
"""test_supervision_wired.py — standing guard for the RESILIENCE class.

A recurring failure this session was the console dying with nothing restarting it — because it was
never registered for supervision. The owner's rule: a recurring failure is an OS resilience defect to
fix at the source, not retry around. This guard makes that structural:

  every daemon the watchdog DETECTS as down (watchdog.EXPECTED) MUST have a responder RESTART action
  (responder.DAEMONS) — detection without remediation is a half-supervised daemon that pages forever;
  and the critical user-facing serving daemons MUST be supervised at all.

So you can't add a new serving daemon and forget to make it self-healing — the suite goes red.
Run with the project venv:  python scripts/test_supervision_wired.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import responder      # noqa: E402
import watchdog       # noqa: E402

# Daemons that bind a user-facing port — losing any of these silently is unacceptable.
CRITICAL = {"console", "dashboard", "api"}


def main() -> int:
    expected = set(watchdog.EXPECTED)        # what the watchdog flags as DOWN
    healable = set(responder.DAEMONS)        # what the responder can RESTART
    problems = []

    # every detected-down daemon must be auto-restartable (no detect-without-heal)
    detect_no_heal = expected - healable
    if detect_no_heal:
        problems.append(f"watchdog detects but responder can't restart: {sorted(detect_no_heal)}")

    # the critical serving daemons must be both detected and healable
    for d in sorted(CRITICAL):
        if d not in expected:
            problems.append(f"critical daemon '{d}' not in watchdog.EXPECTED (a death goes undetected)")
        if d not in healable:
            problems.append(f"critical daemon '{d}' not in responder.DAEMONS (a death isn't auto-restarted)")

    if problems:
        print("FAIL: daemon supervision has gaps (a daemon could die and stay dead):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"PASS: all {len(expected)} expected daemons are auto-restartable; "
          f"critical {sorted(CRITICAL)} supervised ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
