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
