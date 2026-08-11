#!/usr/bin/env python3
"""clauded.py — the central CLAUDE-SUBPROCESS REAPER (fixes F12: hung/orphaned claude calls).

Why this exists: every agent call shells out to the `claude` CLI, guarded by a per-call subprocess timeout.
But that timeout is only enforced by the PARENT python process — when the parent dies (the G1 problem: a driver
reaped, a session torn down), its `claude` child is ORPHANED and runs FOREVER (we saw one hung 56 minutes). A
pile of orphaned/hung claude procs (a) never finish, (b) hold subscription capacity so NEW calls throttle and
hang too, and (c) have to be killed by hand. This is the central, always-on process that kills them.

A legitimate `claude` call — even Opus on a hard prompt — finishes in a few minutes. So any `claude` process
older than a hard ceiling (AOS_CLAUDE_MAX_S, default 15 min) is dead weight: reap it. Runs from jobd's tick
(central daemon) and as a standalone command.

    clauded.py reap [max_age_s]     kill claude procs older than the ceiling (default 900s/15min); prints {reaped, pids}
    clauded.py list                 show claude procs + ages
    clauded.py selftest             offline (pure age-filter logic)
"""
import os
import subprocess
import sys

# The ceiling must sit ABOVE the longest budget factory actually hands an agent, or this reaper kills real
# work. It was 900s under the belief that "a real claude call never legitimately runs this long" — but
# factory._fast_estimate budgets a HEAVY agent (build/engineer/research/architect/qa) up to 25 MINUTES, and
# component builds genuinely take 6-15 min. So every build in the 15-25 min band was killed by design.
# Measured on 2026-08-11 in one run: BUILD:retention_engine died at 907s and BUILD:web_ui_dom_charts at
# 912s — both matching a ReapHungClaude audit entry to the SECOND — throwing away ~15 min of finished work
# each, then paying to rebuild them. Two of 82 build-minutes lost to a guard meant to protect throughput.
# 2400s matches reap.py's MAX_RUNTIME_S so the two reapers no longer disagree, and clears factory's 25-min
# heavy budget with margin. Hung calls are still caught — 40 min is far beyond any real agent.
MAX_AGE_S = int(os.environ.get("AOS_CLAUDE_MAX_S", "2400"))


def _is_headless_agent(args: str) -> bool:
    """True only for a factory HEADLESS agent call (`claude -p … --output-format json …`), NOT an interactive
    Claude Code / `claude --continue` session. CRITICAL: the reaper must never kill a human's live session, only
    the fire-and-forget agent subprocesses. The `--output-format` flag is unique to the headless SDK path."""
    return ("claude" in args) and (" -p " in f" {args} ") and ("--output-format" in args)


def claude_procs():
    """[(pid, age_seconds)] for every live HEADLESS `claude` agent call (excludes interactive sessions)."""
    try:
        out = subprocess.run(["ps", "-eo", "pid,etimes,args"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    procs = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) >= 3 and _is_headless_agent(parts[2]):
            try:
                procs.append((int(parts[0]), int(parts[1])))
            except ValueError:
                continue
    return procs


def stale(procs, max_age_s=MAX_AGE_S):
    """Pure: which pids are past the ceiling. Isolated so it's unit-testable without touching real processes."""
    return [pid for pid, age in procs if age > max_age_s]


def reap(max_age_s=MAX_AGE_S):
    """Kill every `claude` process older than the ceiling (orphaned/hung). Kills the pid directly (NOT the
    process group — the group may contain a live parent driver we must not touch). Best-effort. Returns a
    summary. This frees subscription capacity so healthy calls stop throttling."""
    victims = stale(claude_procs(), max_age_s)
    killed = []
    for pid in victims:
        try:
            os.kill(pid, 9)
            killed.append(pid)
        except ProcessLookupError:
            pass
        except Exception:
            pass
    if killed:
        try:
            import audit
            audit.append(actor="clauded", action="ReapHungClaude", resource="claude-cli",
                         decision="killed", payload={"pids": killed, "max_age_s": max_age_s})
        except Exception:
            pass
    return {"reaped": len(killed), "pids": killed, "checked": len(claude_procs())}


def _budget_ceiling_ok():
    """The reaper ceiling MUST exceed the longest budget factory hands an agent, or this reaper kills real
    work mid-flight. They drifted apart once (reaper 900s vs a 25-minute heavy budget) and it silently
    killed two finished component builds, so the invariant is asserted rather than trusted to a comment."""
    try:
        import factory
        # the estimator's heaviest bucket: a long prompt to a build/engineer role
        mins, _rets = factory._heuristic_estimate("backend-engineer", "x" * 4000)
        return MAX_AGE_S > mins * 60, mins
    except Exception as e:
        # A guard that silently passes is worse than no guard: if the budget function is gone or renamed,
        # SAY so rather than reporting green on an invariant that was never evaluated.
        return False, ("uncheckable: " + str(e)[:80])


def _selftest():
    # pure age-filter: only procs strictly older than the ceiling are stale.
    procs = [(101, 30), (102, 599), (103, 600), (104, 601), (105, 3600)]
    s = set(stale(procs, 600))
    assert s == {104, 105}, s                         # 601s and 3600s are stale; 600s is NOT ( > , not >= )
    assert stale([], 600) == []
    assert set(stale(procs, 20)) == {101, 102, 103, 104, 105}   # a tiny ceiling reaps all
    # SAFETY: only headless agent calls are targeted — an interactive session is NEVER reaped.
    assert _is_headless_agent("claude -p 'do x' --output-format json --model claude-opus-4-8")
    assert not _is_headless_agent("claude"), "bare interactive session must be safe"
    assert not _is_headless_agent("claude --continue"), "a --continue session must be safe"
    assert not _is_headless_agent("node /path/claude-code/cli.js"), "the Claude Code wrapper must be safe"
    # claude_procs returns well-formed tuples (may be empty here — that's fine)
    for pid, age in claude_procs():
        assert isinstance(pid, int) and isinstance(age, int)
    ok, mins = _budget_ceiling_ok()
    assert ok, (f"reaper ceiling {MAX_AGE_S}s must exceed factory's heaviest agent budget "
                f"({mins} min) — otherwise it kills builds that are still working")
    print("clauded selftest: PASS (reaps only stale HEADLESS agent calls; interactive sessions never touched)")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else "list"
    if cmd == "selftest":
        sys.exit(_selftest())
    elif cmd == "reap":
        import json
        print(json.dumps(reap(int(a[1]) if len(a) > 1 else MAX_AGE_S)))
    else:
        for pid, age in sorted(claude_procs(), key=lambda x: -x[1]):
            flag = "  <-- STALE (reap)" if age > MAX_AGE_S else ""
            print(f"pid {pid}  {age}s old{flag}")
