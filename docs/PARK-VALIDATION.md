# Dispatch-and-park — live validation runbook

Dispatch-and-park (overhaul Step 3) runs each phase in a **detached worker process** that survives the
driver's death (retires G1). It ships behind `AOS_DISPATCH_PARK` (default **off**). The mechanics are proven
offline; this runbook validates it on a **real build** before you make it the default and unlock Steps 4–5.

## 0. Mechanics check (no spend, ~10s)
```
.venv/bin/python scripts/loopcontroller.py parkcrash
```
Expect `park_crash_selftest: PASS` — a driver is SIGKILL'd mid-phase and the detached worker still finishes.
This proves the machinery without touching claude. (Also runs in CI:
`test_g1_retired_parked_worker_survives_driver_crash`.)

## 1. Turn park mode on
Set it in the environment the fleet actually runs in (the daemon's env / your shell before starting jobd):
```
export AOS_DISPATCH_PARK=1
```
Everything is backward-compatible: off = the proven in-process daemon path; on = detached workers. Launch
failure auto-falls-back to in-process, so turning it on can't strand a build.

## 2. Start a small real build
Drive the normal flow (controller `start` → `say "<a tiny product>"` → approve the plan). Keep it small so
the spend is minimal. Once it reaches BUILD/QA you have a live parked worker.

## 3. Watch the parked work
```
watch -n2 '.venv/bin/python scripts/loopcontroller.py parkstatus'
```
You should see the in-flight job with `"parked": true`, a `"worker_pid"`, `"worker_alive": true`, and a
fresh `"heartbeat_age_s"`. This is the observability that makes park mode safe to run — parked work is never
invisible.

## 4. The real test — crash the driver, not the worker
Find the **driver** process (the `jobd`/scheduler that dispatched — NOT the `worker_pid` shown above) and
kill it hard:
```
kill -9 <driver_pid>     # do NOT kill worker_pid
```
Then keep watching `parkstatus`. **Pass criteria:** `worker_alive` stays `true`, the heartbeat keeps
advancing, and the job reaches `done` — the build completes even though its driver died. Restart jobd; it
picks up the completed job and advances the phase (poller advances under the drive lock).

## 5. Verify billing landed on the tenant
The one thing only a live run confirms: a worker in a fresh process rebuilds `factory._ctx` from the tenant id
(`_rebuild_ctx` → resolved provider engine+key). Confirm the spend/tokens for this build are attributed to the
**tenant's** provider (check the audit log / billing), not the platform default.

## 6. Worker-death recovery (optional)
Kill the `worker_pid` mid-build. Its heartbeat stops → the reaper marks the job `crashed=true` → `advance`
transparently re-runs the phase (builds resume from their `_stage_done` checkpoint), bounded by
`CRASH_RETRY_MAX`. Confirm the build resumes rather than restarting from scratch, and that the CEO is **not**
paged (crash-resume is silent to the user).

## 7. Promote
If 3–6 pass: set `AOS_DISPATCH_PARK=1` permanently (in `.env.local` / the daemon env). That is the gate for
overhaul **Steps 4–5** (consolidate onto the orchestra engine; retire the hand-rolled loopcontroller
durability) — do NOT delete the in-process path until park mode has run clean builds in production.

## Rollback
`unset AOS_DISPATCH_PARK` (or remove it from the env) and restart the fleet — instantly back to the proven
in-process path, no data migration.
