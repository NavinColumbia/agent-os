#!/usr/bin/env python3
"""clauded.py — the central owned-agent subprocess reaper.

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
import json
import shlex
from pathlib import Path

import process_assurance

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
REGISTRY = Path(os.environ.get("AOS_CLAUDE_OWNERS_DIR", "/tmp/agentos-owned-claude"))


class OwnershipUnavailable(RuntimeError):
    """A batch child was stopped because exact durable ownership could not be recorded."""


def register_owned(pid, owner):
    """Persist an exact child identity so a dead parent leaves safe cleanup authority behind."""
    try:
        snap = process_assurance.read_snapshot(int(pid))
        if snap is None:
            return None
        owner_snap = process_assurance.read_snapshot(os.getpid())
        REGISTRY.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = REGISTRY / f"{snap.identity.boot_id}-{pid}-{snap.identity.start_ticks}.json"
        payload = {"pid": pid, "start_ticks": snap.identity.start_ticks,
                   "boot_id": snap.identity.boot_id, "owner": str(owner)[:160]}
        if owner_snap is not None:
            payload.update({"owner_pid": owner_snap.identity.pid,
                            "owner_start_ticks": owner_snap.identity.start_ticks,
                            "owner_boot_id": owner_snap.identity.boot_id})
        tmp = path.with_suffix(f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(payload, sort_keys=True)); os.chmod(tmp, 0o600); tmp.replace(path)
        return path
    except OSError:
        # Missing registry evidence means the reaper will leave this child alone; execution itself continues.
        return None


def unregister_owned(record):
    if record:
        try:
            Path(record).unlink(missing_ok=True)
        except OSError:
            pass


def _owned_records():
    records = {}
    try:
        paths = list(REGISTRY.glob("*.json"))
    except OSError:
        return records
    for path in paths:
        try:
            data = json.loads(path.read_text())
            identity = process_assurance.ProcessIdentity(
                int(data["pid"]), int(data["start_ticks"]), str(data["boot_id"]))
            observed = process_assurance.read_snapshot(identity.pid)
            if observed is None or not process_assurance.same_process(identity, observed):
                path.unlink(missing_ok=True)
                continue
            records[identity.pid] = (identity, path, data.get("owner"))
        except Exception:
            continue
    return records


def _registered_identity(record):
    """Read the exact process identity from one registry record, or fail closed."""
    try:
        data = json.loads(Path(record).read_text())
        return process_assurance.ProcessIdentity(
            int(data["pid"]), int(data["start_ticks"]), str(data["boot_id"]))
    except Exception:
        return None


def _signal_registered_descendants(record, sig=9):
    """Signal only descendants of the still-exact registered process, leaf first.

    A CLI can have MCP/shell children. Killing only the CLI root can reparent those
    children and turn a bounded timeout into host pressure that no owner can later
    prove. The root itself remains for the caller to wait/reap normally.
    """
    root = _registered_identity(record)
    if root is None:
        return []
    snapshots = process_assurance.scan_snapshots()
    killed = []
    for expected in process_assurance.cleanup_plan(root, snapshots):
        if not process_assurance.same_process(
                expected, process_assurance.read_snapshot(expected.pid)):
            continue
        try:
            os.kill(expected.pid, sig)
            killed.append(expected.pid)
        except (ProcessLookupError, PermissionError):
            pass
    return killed


def _signal_registered_tree(identity, sig=9):
    """Signal an exact registered process tree, descendants first, with PID-reuse fencing."""
    snapshots = process_assurance.scan_snapshots()
    killed = []
    for expected in process_assurance.cleanup_plan(identity, snapshots) + [identity]:
        if not process_assurance.same_process(
                expected, process_assurance.read_snapshot(expected.pid)):
            continue
        try:
            os.kill(expected.pid, sig)
            killed.append(expected.pid)
        except (ProcessLookupError, PermissionError):
            pass
    return killed


def _registered_owner_gone(record, root_snapshot) -> bool:
    """Prove the spawning parent generation is gone; old records fall back to orphan PPID evidence."""
    try:
        data = json.loads(Path(record).read_text())
        if all(data.get(key) is not None for key in
               ("owner_pid", "owner_start_ticks", "owner_boot_id")):
            owner = process_assurance.ProcessIdentity(
                int(data["owner_pid"]), int(data["owner_start_ticks"]), str(data["owner_boot_id"]))
            observed = process_assurance.read_snapshot(owner.pid)
            if observed is None:
                # A transient /proc read error is not proof of death.  Only an absent PID directory is.
                try:
                    return not (Path("/proc") / str(owner.pid)).exists()
                except OSError:
                    return False
            return not process_assurance.same_process(owner, observed)
    except Exception:
        return False
    return bool(root_snapshot is not None and root_snapshot.ppid == 1)


def reap_owned_orphans(owner_prefix, *, sig=9, dry=False):
    """Reap exact registered trees whose spawning parent generation is provably gone.

    This is used for QA browser bridges: Chromium and ffmpeg remain children of the Node bridge, so matching
    only browser roots by PPID misses the whole young tree after its Python worker dies. Registry identity plus
    ancestry is kill authority; an unregistered lookalike, same-PGID peer, or reused PID is never signalled.
    """
    roots, signaled = [], []
    for pid, (identity, record, owner) in _owned_records().items():
        if not str(owner or "").startswith(str(owner_prefix)):
            continue
        observed = process_assurance.read_snapshot(pid)
        if (not process_assurance.same_process(identity, observed)
                or not _registered_owner_gone(record, observed)):
            continue
        roots.append(pid)
        if dry:
            continue
        killed = _signal_registered_tree(identity, sig)
        if pid in killed:
            signaled.extend(killed)
            unregister_owned(record)
    return {"reaped": len([pid for pid in roots if dry or pid in signaled]),
            "pids": roots, "signaled_pids": signaled, "dry_run": bool(dry)}


def run_owned(args, *, owner, timeout=None, lease=None, **kwargs):
    """Run a Claude or Codex child under exact ownership and descendant cleanup."""
    input_data = kwargs.pop("input", None)
    if input_data is not None:
        kwargs.setdefault("stdin", subprocess.PIPE)
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE; kwargs["stderr"] = subprocess.PIPE
    if lease is not None:
        import claude_gate
        record = None
        def registered(proc):
            nonlocal record
            record = register_owned(proc.pid, owner)
            if record is None:
                # An unregistered child cannot be reaped safely after parent death. Stop it before the
                # provider call proceeds; a command-line substring is observation, never kill authority.
                proc.kill()
                proc.wait()
                raise OwnershipUnavailable(f"could not register exact ownership for child {proc.pid}")
        try:
            return claude_gate.run_fenced(args, lease=lease, timeout=timeout,
                                          input=input_data, on_spawn=registered,
                                          on_terminate=lambda _proc: _signal_registered_descendants(record),
                                          **kwargs)
        finally:
            unregister_owned(record)
    proc = subprocess.Popen(args, **kwargs)
    record = register_owned(proc.pid, owner)
    if record is None:
        proc.kill()
        proc.wait()
        raise OwnershipUnavailable(f"could not register exact ownership for child {proc.pid}")
    try:
        try:
            stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _signal_registered_descendants(record)
            proc.kill()
            stdout, stderr = proc.communicate()
            exc.stdout, exc.stderr = stdout, stderr
            raise
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    finally:
        unregister_owned(record)


def _is_headless_agent(args: str) -> bool:
    """True only for a factory HEADLESS agent call (`claude -p … --output-format json …`), NOT an interactive
    Claude Code / `claude --continue` session. CRITICAL: the reaper must never kill a human's live session, only
    the fire-and-forget agent subprocesses. The `--output-format` flag is unique to the headless SDK path."""
    return ("claude" in args) and (" -p " in f" {args} ") and ("--output-format" in args)


def _is_headless_codex(args: str) -> bool:
    """Recognize a Codex batch worker while excluding interactive/app-server sessions."""
    try:
        tokens = shlex.split(str(args or ""))
    except ValueError:
        tokens = str(args or "").split()
    for index, token in enumerate(tokens[:-1]):
        if Path(token).name == "codex" and tokens[index + 1] == "exec":
            return True
    return False


def claude_procs():
    """Observable Claude/Codex batch workers; observation alone is never kill authority."""
    try:
        out = subprocess.run(["ps", "-eo", "pid,etimes,args"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    procs = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) >= 3 and (_is_headless_agent(parts[2]) or _is_headless_codex(parts[2])):
            try:
                procs.append((int(parts[0]), int(parts[1])))
            except ValueError:
                continue
    return procs


def stale(procs, max_age_s=MAX_AGE_S):
    """Pure: which pids are past the ceiling. Isolated so it's unit-testable without touching real processes."""
    return [pid for pid, age in procs if age > max_age_s]


# A LIVE off-switch. Two reasons it is a FILE and not an env var: jobd reaps in-process and runs for days,
# so an env var only takes effect on restart (the exact trap that made the 900s ceiling outlive its fix);
# and during a long, expensive build an operator needs to stop the reaper NOW without bouncing the daemon
# that is driving the build. Checked on every call.
#   disable:  touch <repo>/.reaper-off      re-enable:  rm <repo>/.reaper-off
DISABLE_FLAG = Path(__file__).resolve().parents[1] / ".reaper-off"


def disabled():
    """True while the operator has parked the reaper. Fail-SAFE: if the check itself errors we do NOT reap,
    because wrongly killing live work is far more expensive than briefly leaking an orphan."""
    try:
        return DISABLE_FLAG.exists()
    except Exception:
        return True


def _ceiling():
    """Read the ceiling AT CALL TIME, not at import.

    jobd imports this module once and then runs for days — it had been up 7 days holding MAX_AGE_S=900
    from a stale import, so raising the constant in the file changed nothing for the daemon that does most
    of the reaping. It kept killing live build agents and audited them as max_age_s:900 while the file on
    disk said 2400. A long-lived daemon must re-read a limit that ops can change, or the config is a lie."""
    try:
        return int(os.environ.get("AOS_CLAUDE_MAX_S") or MAX_AGE_S)
    except ValueError:
        return MAX_AGE_S


def reap(max_age_s=None):
    """Kill every old, exactly registered batch-agent tree; never kill from a name match alone."""
    if disabled():
        return {"reaped": 0, "disabled": True, "flag": str(DISABLE_FLAG)}
    max_age_s = _ceiling() if max_age_s is None else max_age_s
    observed = claude_procs()
    owned = _owned_records()
    victims = [pid for pid in stale(observed, max_age_s) if pid in owned]
    unowned_stale = [pid for pid in stale(observed, max_age_s) if pid not in owned]
    killed = []
    for pid in victims:
        try:
            identity, record, _owner = owned[pid]
            if process_assurance.same_process(identity, process_assurance.read_snapshot(pid)):
                signaled = _signal_registered_tree(identity, 9)
                if pid in signaled:
                    killed.append(pid)
            unregister_owned(record)
        except ProcessLookupError:
            pass
        except Exception:
            pass
    browser_orphans = reap_owned_orphans("qa-browser:")
    if killed:
        try:
            import audit
            audit.append(actor="clauded", action="ReapHungClaude", resource="claude-cli",
                         decision="killed", payload={"pids": killed, "max_age_s": max_age_s})
        except Exception:
            pass
    all_roots = killed + [pid for pid in browser_orphans["pids"] if pid not in killed]
    return {"reaped": len(killed) + int(browser_orphans["reaped"]), "pids": all_roots,
            "checked": len(observed), "unowned_stale": unowned_stale,
            "browser_trees_reaped": int(browser_orphans["reaped"])}


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
