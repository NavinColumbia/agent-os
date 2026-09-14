# Standard: Test Like a Human (Human-Paced + Idle-Dwell Verification)

Status: REQUIRED for every verification step on an interactive surface (factory QA/VERIFY stage,
loopcontroller verification, and any QA/test/review role: qa-security, reviewer, sdet). Applies to any
UI, form, or session-bearing screen a real user can sit on.

## Why this exists

Our automated QA acts at machine speed: it fills a form in milliseconds, submits instantly, and never
DWELLS on a screen. An entire CLASS of bugs is therefore structurally invisible to it — they only
manifest when a human pauses, reads, types slowly, or simply leaves a screen open.

The motivating example: on the **signed-out create-account screen**, a leaked polling timer bounced
the user back to the sign-in screen after ~6 seconds. A test that types-and-submits in under a second
never sees it; the bug reached the owner. "It renders, throws no console error, and the fast scripted
happy-path passes" said PASS while a real human got kicked off the page mid-typing.

This is a class, not a one-off. Members include:

- **Leaked timers / intervals** that fire after a delay and disrupt the screen.
- **Polling that disrupts input/focus/scroll** — a refetch that re-renders and blows away half-typed
  input, steals focus, or jumps the scroll position.
- **Session-expiry loops** — a token check that redirects or bounces to sign-in while the user is idle
  or still working.
- **Duplicate-interval / listener accumulation** — repeated login/logout (or remounts) stacking more
  and more timers, each firing and compounding the disruption.

All of these require TIME and STILLNESS to appear. Machine-speed testing guarantees they slip through.

## The principle

> "Renders without error" and "fast scripted happy-path passes" are NOT sufficient verification.

A surface is only verified once it has been driven at human pace AND left idle long enough for a leaked
timer, poll, or session check to fire — with assertions that nothing disrupted the user.

## What every interactive-surface verification MUST do

1. **TEST AT HUMAN PACE.** Type with realistic per-keystroke delays (do not paste a whole field
   instantly). Move between fields and controls with pauses, as a person reading the screen would.

2. **DWELL WITHOUT ACTING.** On each meaningful screen, sit idle for a realistic dwell of **8–12
   seconds** doing nothing — no typing, no clicking. This is the window in which a leaked timer,
   polling refetch, or session-expiry check fires. A test that never pauses cannot catch this class.

3. **ASSERT NO DISRUPTION DURING IDLE.** While dwelling (and after), assert that NONE of the following
   happened:
   - **No unexpected navigation / route change** (e.g. bounce back to sign-in).
   - **No form-input loss** — text typed before the dwell is still present after it.
   - **No focus loss** — the focused field is still focused; focus was not stolen.
   - **No scroll jump** — the scroll position is unchanged.
   - **No bounce-to-signin** or other forced redirect on an authenticated/working screen.

4. **EXERCISE THE SIGNED-OUT DWELL.** Sit on signed-out screens (sign-in, **create-account**, password
   reset) at human pace and idle through the dwell window. The create-account timer-leak lived
   precisely here — a signed-out screen no fast happy-path lingers on.

5. **EXERCISE SESSION-EXPIRY TRANSITIONS.** Drive the app across the session boundary: let a session
   approach/cross expiry while idle and assert the transition is intentional and non-disruptive (no
   silent bounce loop, no input loss, no redirect storm).

6. **EXERCISE REPEATED LOGIN/LOGOUT FOR LEAKED/DUPLICATED TIMERS.** Repeat login → logout (and screen
   mount → unmount) several times, then dwell. Assert timers/intervals/listeners are not accumulating —
   the disruption must not get worse with each cycle. A clean first pass can still leak on the third.

## How "done" is judged

A verification verdict on an interactive surface is incomplete — and must not be PASS — unless it
records:

- Human-paced typing and per-screen idle dwell (8–12s) were actually performed, with the screens
  named.
- The no-disruption assertions (navigation / input / focus / scroll / bounce-to-signin) held through
  idle, OR a failure is filed with the exact screen, dwell time, and what moved.
- Signed-out dwell, session-expiry transition, and repeated login/logout (timer-accumulation) checks
  were each run or explicitly named as not-covered — never silently skipped.

A green render and a sub-second scripted happy-path are evidence the code mounts, not evidence a human
can use it. The create-account timer-leak is the standing proof of the difference.

## Per-job headed-browser and assistive-technology isolation

Status: REQUIRED whenever a QA job launches a headed browser or an external assistive-technology driver
(currently Orca/AT-SPI). Browser-capacity admission limits resource use; it is not an isolation boundary.
Every admitted job gets its own complete desktop/AT session and its own exact cleanup authority.

### Required boundary

Each job MUST have all of the following, created for that job rather than inherited from the worker or
shared with another job. Orca is an explicit exception: upstream supports one screen-reader process per
Unix user, so real-AT jobs share one bounded workstation queue but never a live tenant browser session:

- an automatically allocated Xvfb display;
- a private D-Bus session and the AT-SPI bus it brokers;
- a private `XDG_RUNTIME_DIR`, mode `0700`;
- an explicitly owned foreground `speech-dispatcher` on a private Unix socket (never client autospawn);
- a private Orca work directory and debug stream;
- a private Playwright browser context, cookies/local storage, screenshots, video, and evidence ledger;
- a process root registered with exact PID birth identity and the job's `(tenant, run_id)` scope; and
- a browser-capacity lease held for the lifetime of that process root.

The child environment MUST be built from an explicit runtime allowlist; copying the worker's complete
environment into Xvfb, D-Bus, Orca, Node, or Chromium is forbidden because it forwards unrelated credentials
and provider tokens into a larger process tree. Job-specific values are passed only in that child environment.
A job MUST NOT modify the worker's global `DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`, `XDG_RUNTIME_DIR`, AT-SPI,
storage-state, or evidence variables.
Two concurrently submitted real-AT jobs MUST NOT run two Orca instances. One holds the full-lifetime
workstation lease and one waits without consuming browser capacity. After exact handoff, their recorded
display, bus, runtime-directory, debug-stream, browser-storage, and evidence identities must differ. An
utterance, cookie, screenshot, or state mutation from one job appearing in another is a release-blocking
isolation failure. Required non-secret WSLg runtime endpoints such as `PULSE_SERVER` may be allowlisted;
dropping them can deadlock Orca's event thread and is also release-blocking.

### Startup and cleanup contract

The boundary is fail closed:

1. For real AT, acquire the bounded full-lifetime Orca workstation lease; only then acquire browser capacity.
2. Create the private runtime directory with mode `0700`. From this point onward, every exception — including
   invalid resume/storage input before process launch — MUST unwind the directory and capacity lease.
3. Start `xvfb-run -a` and `dbus-run-session`, register the exact process identity, then await the
   Orca/browser readiness handshake.
4. Do not navigate, click, or claim actual-AT coverage unless readiness proves the real driver is live.
5. On normal close, send the protocol `close` request and give the driver a bounded grace period to reap
   Orca, Node, Chromium, ffmpeg, D-Bus, and Xvfb and remove its private work directory. Send `SIGTERM` only
   after that grace period expires; use `SIGKILL` only after a second bounded wait.
6. On startup failure, command failure, scoped cancellation, lease loss, or parent death, fence only the
   exact identity-bound tree for that `(tenant, run_id)`. Never use a process-name match, bare PID, global
   browser set, or broad `pkill`.
7. Revalidate that every recorded process identity is dead, remove the private runtime/work directories,
   unregister ownership, and release the exact capacity lease. Cleanup is not successful while any one of
   those postconditions is unproven.

`close_live_bridges(run_id=..., tenant=...)` MUST receive both scope fields for job cancellation and MUST
reject calls supplying only one field. Supplying neither field is reserved for whole-worker shutdown. Closing
one scope must leave concurrent neighbors responsive and must not delete or alter their evidence.

### Release-gate proof

The executable policy is `tests/test_qa_at_job_isolation.py`. A release gate must run both commands; a
skip or an unavailable local desktop bus is **not** a pass:

```bash
.venv/bin/python -m pytest -q tests/test_qa_at_job_isolation.py
AOS_RUN_LIVE_AT_ISOLATION=1 .venv/bin/python -m pytest -q -s \
  tests/test_qa_at_job_isolation.py::test_live_concurrent_jobs_are_isolated_and_failure_cleanup_is_complete
```

The live proof must submit two jobs concurrently; prove exactly one owns the real-AT workstation while its
peer remains queued without browser admission; inspect the first job's real identities and utterance; cancel
it by exact tenant/run scope; prove the queued job starts and records distinct identities/utterances; kill the
remaining browser child; and verify every recorded identity and private directory is gone. Record the exact
command output and what was not covered. If any assertion fails, BLOCK and notify platform/SRE plus the
controller with the failed postcondition; do not ask the CEO to diagnose an internal cleanup failure.
