# Factory benchmark scorecard

Measured by `scripts/eval_factory.py` — the autonomous factory run against a fixed, diverse benchmark
(no human in the loop). This is the metric to put in front of technical due-diligence: the platform
either ships working software autonomously, or it doesn't.

## Latest run — 4 specs (2 libraries, 2 web apps)

| Metric | Result |
|---|---|
| **pass@1** (shipped, zero fix loops) | **4 / 4** |
| pass (shipped after bounded fix loop) | 4 / 4 |
| blocked / error | 0 / 4 |
| avg wall-clock per app | 272 s (~4.5 min) |

| Product | Kind | Result | Fix loops | Time |
|---|---|---|---|---|
| ev-tempconv | lib | LAUNCHED | 0 | 296 s |
| ev-csvstats | lib | LAUNCHED | 0 | 292 s |
| ev-colorkit | web | LAUNCHED | 0 | 260 s |
| ev-notes | web | LAUNCHED | 0 | 241 s |

**Independent re-verification** (sandboxed, no network): `ev-tempconv` 92 passed / 4 skipped ·
`ev-csvstats` 33 passed · both web apps render with HTTP 200 and zero console errors.

## How each was produced
Each app went through the governed line — SPEC (PM) → BUILD (builder) → QA (real pytest **or** real
headless-browser smoke) → REVIEW (reviewer) → LAUNCH (tech-lead) — with role-grounded agents, a
test-driven re-flow, untrusted code sandboxed, and every step in the tamper-evident audit log.

## Session total
Across this session the factory has shipped **9 apps** end-to-end (splitbill, romanint, pwstrength,
pomodoro, + the 4 above, + a SaaS demo), one of which (`pwstrength`) hit a genuine bug and the
test-driven fix loop self-healed it to green.

> Honest scope: these are small/medium apps (libraries, single-page web apps). The next eval tier is
> multi-file products with persistence and APIs — that's where the success rate gets stress-tested.
