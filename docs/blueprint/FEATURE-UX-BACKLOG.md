# Feature + UX Backlog — Prioritized, Grouped BY FILE

_Owner: Head of Product + UX. Source: consolidated dogfood findings (first-run founder + async-wait +
latency + buyer + edge + feature-completeness personas) benchmarked vs ChatGPT/Stripe/Linear/Vercel,
plus the CRAFT completeness standard (docs/STANDARDS-product-quality.md) and a live read of the code._

**Why grouped by file:** most UI lives in `scripts/console.py`, auth in `scripts/auth.py`, the async
loop in `scripts/loopcontroller.py`, the model runner in `scripts/factory.py`. Grouping keeps two devs
off the same file. Take a whole file-section as one work unit.

---

## PART A — INCOMPLETE FEATURES (build these first)

Half-built or missing capabilities. A gated flow with no path, a stub note, a dead button, a promise
the backend can't keep. **Password reset is #1.** In priority order:

1. **Password reset / forgot-password — NOT BUILT.** `console.py:659 forgotPw()` only prints
   "not available yet — email support@…"; `auth.py` has no reset function at all. A sign-in screen with
   a Forgot-password link that leads nowhere is an incomplete auth surface. **Build:** `auth.py`
   `issue_reset(email)` + `verify_reset(email, code, new_password)` (hashed one-time code, ~15 min
   expiry, self-host shows the code on-screen exactly like email-verify does today) + a real reset
   screen in `console.py`. Files: `scripts/auth.py`, `scripts/console.py`.
2. **Real notification delivery on milestones (self-host).** "I'll ping you" is a bell badge only if
   the tab is open — options-ready/prototype-ready fire `urgent=False`, self-host has no email, push
   defaults off. **Build:** push on every milestone; default build category `push:true` for self-host;
   ntfy/email setup nudge in onboarding. Files: `scripts/loopcontroller.py` (`:155-171,:382,:387`),
   `scripts/auth.py`/`scripts/console.py` (channel setup).
3. **Real Stop/Cancel.** `console.py` `_ctl_cancel` endpoint exists but the streaming subprocess is not
   actually terminated — a late reply still lands ~10s after "Stopped". **Build:** kill the
   `claude -p` process on cancel and discard late tokens (never persist a cancelled turn's reply). Files:
   `scripts/factory.py`, `scripts/loopcontroller.py`.
4. **SLA overrun ping.** `sla_watchdog` posts an in-thread warning but never calls `_ping` — silence at
   the exact moment a user who left the tab wants a heads-up. **Build:** `_ping(urgent)` from
   `sla_watchdog` with retry/cancel, re-ping on continued overrun. File: `scripts/loopcontroller.py:836`.
5. **Guided first-run onboarding.** The Help page lists an "Onboarding: guided first-run setup" topic
   that never actually runs; setup is a reactive scavenger hunt (provider banner, then consent). **Build:**
   a real "1 company ✓ 2 connect AI 3 approve 4 describe" checklist driven by server onboarding state.
   File: `scripts/console.py`.
6. **Honest ETA wired from estimate.py.** `estimate.py`/`/api/estimate` exist but the controller never
   calls them for RESEARCH; the ETA is a hardcoded constant (`loopcontroller.py:139`=3) inconsistent
   with `console.py:163`=10. **Build:** call `estimate.estimate(kind)` at every dispatch point, show an
   honest range, recompute+raise on overrun. Files: `scripts/loopcontroller.py:139`, `scripts/estimate.py`,
   `scripts/console.py:163`.
7. **Live sub-step progress.** A 13-min run shows one static bubble; the 6 parallel agents surface no
   "3 of 7 done / reading X". **Build:** surface fleet sub-progress in `live_status` and render a
   progress+elapsed bubble from the existing 5s poll. Files: `scripts/loopcontroller.py:117`,
   `scripts/console.py`.
8. **Warm/streaming light-chat path.** Every turn cold-spawns a CLI subprocess; there is no warm
   Anthropic Messages API path for BYO-key tenants and no persistent CLI worker pool. **Build:** direct
   warm API call for light turns with a tenant key; warm CLI worker pool for subscription tenants; keep
   token streaming. Files: `scripts/factory.py` (`_run_once_stream ~678`, `agent_stream ~739`),
   `scripts/console.py`.

---

## PART B — TOP FRICTION / CHURN MOMENTS

The moments a real founder abandons. Fix in this order:

1. **BLOCKER — Research finishes but shows ZERO options.** 13-min run dead-ends; 3 good directions sit
   unreachable in the DB (`status='done'` committed before `_extract_options`, a race with
   `resume_stalled`). The core journey breaks at the handoff. Fix: `scripts/research.py:106-109`.
2. **BLOCKER — 6–30s first-token latency, every turn.** Cold `claude -p` spawn per message reads as
   broken on the core scoping loop. Fix: `scripts/factory.py`.
3. **CHURN — First typed idea silently vanishes on the model gate.** The reassurance is rendered then
   wiped by `go('providers')` in the same function; the founder's thoughtful sentence disappears and
   the page teleports to settings. Fix: `scripts/console.py:981`.
4. **CHURN — Assistant hallucinates "Done" while the job still runs.** False completion destroys trust
   in everything the assistant says. Fix: `scripts/loopcontroller.py`.
5. **CHURN — ETA lies (~3 min → 13).** Told 3, waits 13, reads as hung. Fix: `scripts/loopcontroller.py:139`.
6. **CHURN — Stop is a false promise.** Reply lands after "Stopped". Fix: `scripts/factory.py` +
   `scripts/loopcontroller.py`.
7. **CHURN — No ping when results land / on overrun.** Take "I'll ping you" at face value, close the
   tab, get silence. Fix: `scripts/loopcontroller.py:155-171,:836`.
8. **CHURN — Provider setup reads like ops docs + raw `prompt()` key entry.** Non-technical founder
   can't parse "host-CLI / self-host OAuth" and the only key path is an unmasked native `prompt()` that
   looks phishy. Fix: `scripts/console.py` (copy + `:1092` key form).

---

## PART C — FULL BACKLOG, GROUPED BY FILE

### `scripts/auth.py`
| Sev | Finding | Fix |
|-----|---------|-----|
| high | No password-reset backend — `forgot` link leads nowhere | Add `issue_reset`/`verify_reset` (hashed code, ~15 min expiry, self-host shows code like email-verify) |
| high | Self-host has no email/notification channel — verification codes + pings can't reach a user off-tab | Wire an optional email/ntfy sender; keep the honest on-screen fallback; surface a setup nudge |

### `scripts/console.py`
| Sev | Finding | Fix |
|-----|---------|-----|
| high | `forgotPw()` (`:659`) is a dead-end stub note, not a reset flow | Add a reset screen (request code → enter code → new password + confirm, show/hide, strength) calling the new `auth.py` endpoints |
| high | Typed product idea silently vanishes on the model gate (`:981`) — confirmation rendered then wiped by `go('providers')` | Keep the user on the Assistant with the idea + inline "Connect a model" CTA, or carry it to Providers; persist server-side; never destroy a confirmation just rendered |
| high | No guided first-run; setup is reactive gates; the Help "guided first-run" topic never runs | Add a "company → connect AI → approve → describe" checklist; collapse provider+consent into one step; auto-resume build after consent |
| high | Providers page written for a server operator ("real OAuth via host CLI", "self-hosted single-operator box") | Plain-language "Connect an AI model" + one Connect button + logo; move CLI/OAuth internals behind a "Technical details" disclosure |
| high | API-key entry uses a raw native `prompt()` (`:1092`) — unmasked, no validation, looks phishy | Inline masked field with paste, "Validating…" state, inline errors, and a "Get your key" link |
| high | Three nouns for one concept — company / org / organization across first run | Pick ONE user-facing noun ("company") on every surface (nav, switcher, orgs page) |
| med | Gate dumps user on a dead-end Providers page with no wizard header or way back (`:922`) | Render the step bar + a "Continue → describe your product" button on connect; persist the pending idea server-side |
| med | Full 13-item nav shown to a 0-company user; most screens are empty/confusing | Reduce/de-emphasize nav to Assistant + create-company during first-run; progressively reveal the rest |
| med | "Create company" CTA dumps user on the orgs list page with a second empty state | Open a focused inline create form/modal ("Name your company") with the field auto-focused |
| med | Connect gives no feedback; `PROVIDER_OK` flips before server confirms and re-gates/re-discards the idea (`:1096`) | Show "Connecting…", await server confirm before flipping to connected, set `PROVIDER_OK=true` on success so the gate clears without a poll |
| med | Clarifying turns feel frozen — static "… thinking" bubble, no token streaming | Stream controller LLM replies token-by-token (SSE) — same root cause as the latency blocker |
| low | Suggestion chips hardcoded + stay up mid-task (`:956`) | Hide starter chips once scoping/build is underway; swap in context-aware `ctlNextChips` |
| low | Two onboarding state machines (client `firstRunSteps` + server Cockpit banner) can disagree (`:864`) | Drive both from one server onboarding source of truth (or drop the Cockpit banner) |
| low | Thin landing page — bare signup card, one sentence of value prop | Add a value-prop hero: what it does + 3-step "how it works" + one example output |
| low | Email-verify OTP lacks polish (`:540`) — no auto-focus, no auto-submit, manual Verify click | Auto-focus the code field, auto-submit on the 6th digit, accept a pasted code |
| low | "Approve AI use" label and button share the exact string (`:806`) — click can land on the inert label | Give the button a distinct action label ("Approve & continue") |
| low | Not usable on mobile (~390px) — icon-only rail, no drawer | Below ~700px collapse the sidebar into a hamburger drawer; full-width content |
| low | Consent gate returns HTTP 400 for an expected gated state — trips error monitoring | Return HTTP 200 with `{blocked:'consent_required'}` |
| low | Nav selection and rendered view drift (Assistant highlighted, Cockpit rendered; `CUR` defaults to 'cockpit') | Ensure `go('controller')` always renders the controller view; reconcile `CUR` with the rendered view on load |

### `scripts/loopcontroller.py`
| Sev | Finding | Fix |
|-----|---------|-----|
| high | Hallucinates "Done" while the job runs — generic LLM fall-through on `awaiting=='fleet'` | On `awaiting=='fleet'` reply with real job status only, never free-form; suppress duplicate user messages |
| high | ETA lies / hardcoded (`:139`=3 vs `console.py:163`=10); never history-backed | Reconcile the constants, call `estimate.estimate(kind)` at RESEARCH/IMPLEMENT dispatch, show an honest range, raise on overrun |
| high | No live sub-step progress during multi-minute runs (`:117`) | Surface fleet sub-progress in `live_status`; render a progress+elapsed bubble via the 5s poll |
| high | No real ping when awaited results land — options-ready/prototype-ready fire `urgent=False` (`:155-171,:382,:387`) | Push on options-ready + prototype-ready; default build `push:true` self-host; honest channel copy |
| high | SLA watchdog warns in-thread but never pings on overrun/stall (`:836`) | Call `_ping(urgent)` from `sla_watchdog` with retry/cancel; re-ping on continued overrun |
| med | Mid-flight instructions silently dropped ("start building when research is done" vanished) | Queue mid-flight intents and apply at the next gate, or at minimum acknowledge "I'll do that when research finishes" |
| med | Re-asks for consent already accepted — stale gate message left in model context | Inject a system note ("consent on file; provider connected — proceed") and/or drop resolved gate messages from context |

### `scripts/factory.py`
| Sev | Finding | Fix |
|-----|---------|-----|
| blocker | First token 6–30s, every turn — cold `claude -p` spawn; heavy path routes Opus + ~2000-word charter for one clarifying question (`_run_once_stream ~678`, `agent_stream ~739`) | Warm Anthropic Messages API for light BYO-key turns; warm CLI worker pool for subscription tenants; trim the chat path (skip allowedTools/manifest/governance it doesn't use); keep SSE streaming |
| high | Stop doesn't terminate the streaming subprocess — late tokens still persist | Actually kill the process on cancel and discard/suppress late tokens; never persist a cancelled turn |

### `scripts/research.py`
| Sev | Finding | Fix |
|-----|---------|-----|
| blocker | `status='done'` committed before `_extract_options` (`:106-109`) → `resume_stalled` reads `options=[]` and bakes an empty OPTIONS message; core journey dead-ends | Set `status='done'` only AFTER extraction (or report done only once options exist / re-pull on empty) |

### `scripts/estimate.py`
| Sev | Finding | Fix |
|-----|---------|-----|
| high | ETA source exists but is never called from the controller for RESEARCH | Expose/confirm `estimate.estimate(kind)` and wire it at every controller dispatch point (paired with the loopcontroller ETA fix) |

---

## Suggested sequencing

1. **Password reset** (`auth.py` + `console.py`) — the top incomplete feature; a visible link to nowhere.
2. **Unblock the core journey** — empty-options race (`research.py`).
3. **Async-loop trust** — real pings + SLA ping + honest ETA + live progress + no hallucinated "Done" +
   real Stop (`loopcontroller.py`, `factory.py`, `estimate.py`).
4. **Latency** — warm/streaming light-chat path (`factory.py`, `console.py`).
5. **Stop eating input + guided onboarding + provider UX + naming** (`console.py`).
6. **Polish** — chips, mobile, OTP, consent 400, nav drift, landing page (`console.py`).
