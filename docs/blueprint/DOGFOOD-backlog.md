# Dogfood Backlog — Prioritized

_Source: dogfood findings from first-run founder journeys (signup → create company → connect AI → describe product → research/build loop). Synthesized and prioritized by Head of Product. This is a **consolidated** backlog merging two dogfood runs; overlapping findings are deduplicated, distinct ones preserved._

> Scope note: the briefs referenced 39 + 49 findings; the payloads arrived truncated (JSON cut mid-record). **28 distinct, complete findings** survived across both runs and are captured here. The themes and priorities will hold if the full sets are recovered — only the counts will grow.

---

## Executive Summary

**The honest verdict:** Not yet something a stranger would pay for. The *engine is real* — a 6-agent research fleet actually runs, extracts good options, and the success-path notification fires — but the **cockpit that narrates it is not trustworthy yet.** The headline journey (describe → research → pick → build) currently has a **hard stop**: after a 13-minute research run the user is shown "pick a direction" with **zero selectable options**, even though 3 good ones sit in the DB. Around that sit a cluster of expectation-misses — a 3-min ETA that becomes 13, no push/ping when a job stalls or finishes, a 6–30s spinner before the first chat token, an "I'll stop" button that doesn't stop, and an onboarding gate that silently eats the founder's first typed idea. Each is individually fixable; together they read as "unfinished." **Roughly 2–3 weeks of focused UX-integrity work separates "impressive demo" from "I can leave the tab and trust it."**

**The top 8 things hurting the product most** (leading with MISSING capabilities + async ETA/ping/SLA + latency):

| # | Severity | Theme | Problem | One-line fix |
|---|----------|-------|---------|--------------|
| 1 | **blocker** | Core/async | Research completes but surfaces **empty options** — a 13-min run dead-ends with 3 good directions sitting unreachable in the DB (a `status='done'`-before-`_extract_options` race) | Commit `done` only AFTER options are extracted; re-pull on empty |
| 2 | **blocker** | Speed | First chat token takes **6–30s** with no streaming — every conversational turn cold-spawns a `claude -p` subprocess (Opus + 2000-word charter on the heavy path; haiku still pays cold-start) | Warm API/worker path for light turns + stream tokens |
| 3 | **high** | Async (missing) | **No real ping** — options-ready/prototype-ready report `urgent=False`, and self-host has no email + push defaults off; "I'll ping you" = a bell badge only if the tab is open | Push on every milestone; default build push:true for self-host |
| 4 | **high** | Async (missing) | **SLA watchdog warns but never pings** on overrun/stall — silence at the one moment you'd want a heads-up | `_ping(urgent)` from `sla_watchdog`, re-ping on continued overrun |
| 5 | **high** | Async | **ETA lies** — "~3 min" then 13; hardcoded constant, internally inconsistent (3 vs 10) | History-back the research ETA; show an honest range; raise on overrun |
| 6 | **high** | Async | **Stop is a false promise** — reply still lands ~10s after "Stopped"; no real cancel of the subprocess/job | Actually terminate the stream + a `cancel` endpoint that parks the thread |
| 7 | **high** | Async | Assistant **hallucinates "Done"** while the job is still running (generic LLM fall-through on `awaiting=='fleet'`) | On `awaiting=='fleet'`, reply with real job status only, never free-form |
| 8 | **high** | Onboarding | The founder's **first typed idea silently vanishes** on the connect-a-model gate (confirmation rendered then wiped by `go('providers')`) | Keep the idea inline / persist server-side; never destroy a confirmation you just rendered |

**Pattern:** the product's weakest layer is the **conversation that wraps the engine** — latency, status honesty, expectation-setting, notification, and the long-wait handoff. Six of the top eight are async-loop trust issues. Fix the loop's narration and the perceived quality roughly triples without touching the agents.

**Counts (28 distinct findings)**
- By severity: **blocker 2, high 11, med 7, low 8**
- By theme: Core build + async UX 9, Onboarding 10, Speed/latency 2, Provider/billing/account 3, Fleet/orgs (naming) 1, Edge/error/mobile 3
- By kind: product-judgment 7, missing-capability 4, latency 3, broken-promise 3, edge-journey 3, accessibility 3, copy 3, other 2

---

## 1. Speed / Latency

### 1.1 — BLOCKER — First token takes 6–30s, every turn, no streaming
- **Finding:** Every Assistant turn blocks before anything appears — measured ~29–32s on the heavy path ("hi"=29.2s, "track gym members"=32.3s) and ~5.8s on the light chat path (5.74s/5.85s first-token via SSE). No streaming: first and last token arrive together; only a static "… thinking" bubble. Root cause: every conversational turn cold-spawns a `claude -p --output-format stream-json` subprocess (`factory._run_once_stream`). On the heavy path it routes through `factory.agent("research-growth", …)` → Opus with the full ~2000-word role charter as system prompt just to ask one clarifying question. CLI cold-start (node boot + auth + MCP/tool/manifest load) dominates even when the model is haiku with thinking disabled. No warm connection or process reuse; even a tenant with a BYO API key still shells out to the CLI.
- **Why it matters:** This is the *core loop* — a multi-turn scoping chat. ChatGPT/Claude.ai stream the first token in ~300–800ms over a warm connection. A frozen turn, every turn, reads as broken.
- **Fix:** For light streaming turns with a tenant API key, call the Anthropic Messages API directly over a warm HTTP connection (sub-second first token). For subscription-mode tenants, keep a warm/persistent CLI worker pool so the spawn tax is paid once, not per message. Don't route clarifying chat through the heavy role charter; trim the chat path's startup work (skip allowedTools/manifest/governance it doesn't use). **Stream tokens (SSE)** into the bubble.
- **File:** `/home/swami/projects/agent-os/scripts/factory.py` (`_run_once_stream` ~678, `agent_stream` ~739); `/home/swami/projects/agent-os/scripts/loopcontroller.py` (`_llm` ~859); `/home/swami/projects/agent-os/scripts/console.py` (rendering)

### 1.2 — MED — Clarifying turns feel frozen (no token streaming)
- **Finding:** Each DISCOVER clarifying turn shows only a static "… thinking" bubble — no token streaming. Across a multi-turn scoping conversation this repeatedly feels frozen/broken.
- **Why it matters:** ChatGPT/Claude stream; perceived latency is a fraction of agent-os's blocking turn.
- **Fix:** Stream the controller's LLM replies token-by-token (SSE) instead of a blocking POST that reveals the whole message at once. (Same root cause as 1.1.)
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

---

## 2. Core Build + Async UX

### 2.1 — BLOCKER — Research finishes but offers ZERO options (core journey dead-ends)
- **Finding:** After a real 13-min research fleet, the Assistant posted "Here's what I found — pick a direction:" with an empty options list. The DB (research_runs id=143, thread 442) actually held 3 good extracted options ("EU compliance wedge" ★, "Free core, monetize adjacent", "All-in-one client ops"). Only generic chips show; saying/clicking them loops "Tap one of the options above to pick a direction" — and there are none. The founder is hard-stuck after a 13-min wait with good directions unreachable. **The core "research → bring options → build" journey breaks at the handoff.**
- **Why it matters:** ChatGPT/Claude deep research **atomically attach** the synthesized result to completion — "here's what I found" always has something to act on.
- **Fix:** Root cause is a race — `research.py:106-109` commits `status='done'` BEFORE `_extract_options()` (a separate 10–60s LLM call) runs; `loopcontroller.py:758-759` `resume_stalled` (fired by the 120s SLA scheduler and on every state read) reconciles in that window, reads `options=[]`, bakes the empty OPTIONS message (`loopcontroller.py:556-559`) and never re-reconciles. Set `status='done'` only AFTER extraction, or make `run_state` report done only once options exist, or have `resume_stalled` re-pull options when the surfaced set is empty.
- **File:** `/home/swami/projects/agent-os/scripts/research.py:106-109` (+ `/home/swami/projects/agent-os/scripts/loopcontroller.py:758-759`)

### 2.2 — HIGH — Assistant hallucinates "Done" while the job is still running
- **Finding:** Typing anything while a job runs (`awaiting=='fleet'`) falls through to the generic branch `_report(_llm(..., 'Answer the CEO briefly.'))`. This produced a confident, hallucinated "Done — research written to docs/research/invoicing-competitive-landscape.md … every price fetched live today" while the real run was still 'running' and never advanced to OPTIONS. The founder reads "Done", expects a plan, gets nothing.
- **Why it matters:** Claude/ChatGPT never claim a tool result that hasn't returned. A false "Done" erodes trust in everything the assistant says.
- **Fix:** When `awaiting=='fleet'`, do NOT run a free-form LLM answer. Reply with real job status ("Still researching — ~N min in; I'll post options here and ping you"). Suppress duplicate identical user messages with "I'm already on it."
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py`

### 2.3 — HIGH — ETA is missing on some paths and dishonest on others (3 min → 13)
- **Finding:** When research kicks off the Assistant says "On it — researching this now… (~3 min)" and the bubble shows "about 3 min left"; the real 6-agent fleet took ~13 min (4–5x). The ETA is a hardcoded constant that never adapts and is internally inconsistent: `loopcontroller.py:139` uses RESEARCH=3 (what the user saw) while `console.py:163` uses RESEARCH=10. On other dispatch paths no estimate is shown at all even though `estimate.py` / `/api/estimate` exist and history-back PROTOTYPE/IMPLEMENT — RESEARCH falls through to the bogus 3. Being told 3 then waiting 13 reads as "hung" → churn.
- **Why it matters:** ChatGPT/Claude deep research quote an honest wide range ("5–30 min") and never assert a precise small number they blow past 4x; Linear/GitHub Actions show an ETA the instant a long job starts.
- **Fix:** History-back the research ETA via `estimate.py` (extend `loopcontroller.py:134-152`). Reconcile the two constants, call `estimate.estimate(kind)` at the RESEARCH/IMPLEMENT dispatch points and fold an honest range into the report ("usually ~8–15 min"). When a job overruns, **recompute and raise** the displayed ETA instead of freezing at 3.
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py:139` (+ `console.py:163`, `estimate.py`)

### 2.4 — HIGH — No live sub-step progress during a multi-minute run
- **Finding:** For the full 13 min the live bubble showed a single static "Researching directions… Nm Ns elapsed" while 6 distinct agents ran in parallel (competitors, live pricing, payment providers, e-invoicing tech, GTM, positioning). No "searching… / reading X / 3 of 7 done / N sources" signal — and the only other progress hint is a tiny gray "Research · waiting on your agents to finish". A user cannot distinguish steady progress from a stall and gets anxious well before the SLA note.
- **Why it matters:** ChatGPT/Claude deep research stream a live activity log ("Reading freshbooks.com/pricing", running source count) so the run visibly advances.
- **Fix:** Surface fleet sub-progress in the `live_status` string (`loopcontroller.py:117-122`) — completed sub-questions vs total, or the current sub-question/source — and have the existing 5s poll render a progress bubble (live status + elapsed timer) in-thread while `awaiting=='fleet'`.
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py:117` (+ `console.py`)

### 2.5 — HIGH — No real ping when the awaited result lands ("I'll ping you" is bell-only)
- **Finding:** The completion ping is in-app-only in practice. Push (`urgent=True → push.send`) fires only for consent/provider gates, job failures, qa-fail, and the FINAL DELIVER message — the two intermediate results a founder actually waits on, **research OPTIONS ready** (`loopcontroller.py:382`) and **PROTOTYPE ready** (`loopcontroller.py:387`), report with `urgent=False`. Even where push is requested, self-host has no email service ("no email service configured" at signup), `push.send` needs ntfy, and default `notification_prefs` set `push:false` for build. So "I'll ping you the moment they're ready" means "a bell badge you'll only see if this tab is still open" — inadequate for a 13-min research, useless for a multi-hour build.
- **Why it matters:** ChatGPT emails/pushes you when deep research or a long task completes, so you can leave entirely and come back; CI tools push on every milestone.
- **Fix:** Add a push to the options-ready and prototype-ready `_report` calls. For self-host, default the build category to `push:true`, surface an ntfy/email setup nudge during onboarding, and make the "I'll ping you" copy honest about the channel.
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py:155-171` (+ `:382`, `:387`)

### 2.6 — HIGH — Stop is a false promise; no real cancel
- **Finding:** Stop does not stop. On a conversational turn, after clicking Stop the chat posts "⏹️ Stopped the DISCOVER step — nothing more will run until you say so" (16:11:59) but the full model reply ("I appreciate the enthusiasm, but let me pump the brakes…") still lands and is persisted ~10s later (16:12:09). More broadly, `ctlSend` only disables Send with a client-side abort, and a dispatched research/build `controller_jobs` row has no cancel endpoint — once you hit Send you cannot abort the turn or the multi-minute job, only close the tab (orphaning you from the result).
- **Why it matters:** ChatGPT/Claude show a prominent Stop that kills the stream the instant you click — the single most-expected control in an AI chat. A Stop that doesn't stop destroys trust in every control.
- **Fix:** Actually terminate the streaming subprocess on Stop and discard/suppress any late tokens (never persist a reply for a cancelled turn). Add a `/api/controller/cancel` that marks the active `controller_jobs` row cancelled and parks the thread on a `user_feedback` gate ("Stopped — say retry to resume").
- **File:** `/home/swami/projects/agent-os/scripts/factory.py` + `/home/swami/projects/agent-os/scripts/loopcontroller.py` / `console.py`

### 2.7 — HIGH — Overrun watchdog posts a warning but never pings (silence at the worst moment)
- **Finding:** Stall handling improved — `sla_watchdog` now posts "taking longer than usual (11m elapsed) — say retry/cancel" when a job overruns its ETA (good), and `resume_stalled` recovers truly hung runs. But the SLA warning is written only via `_report` (in-thread) and **never calls `_ping`** — notifications stayed unread=0 through the entire overrun window. The one moment you'd most want a heads-up (job overrunning / possibly stalling) produces no notification or push. A user who took "I'll ping you" at face value and closed the tab is left in silence — the owner's exact complaint.
- **Why it matters:** ChatGPT/Claude email/push you when a long background job needs attention or is taking unusually long, not only when it succeeds; Vercel/GitHub flag "running longer than expected" and offer cancel.
- **Fix:** Call `_ping(tid, …)` (urgent) from `sla_watchdog` when it posts the SLA warning, with retry/cancel buttons, and re-ping on continued overrun.
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py:836-840`

### 2.8 — MED — Mid-flight instructions are silently dropped
- **Finding:** While research was running the user sent "Pick the recommended option and start building it now." The controller replied "I'm already on it — researching directions…" and discarded the instruction — it was not queued, so when options landed the thread parked on `user_approval` instead of auto-proceeding to build as asked. A reasonable "start building when research is done" directive just vanished.
- **Why it matters:** Linear/Claude queue follow-up instructions sent during an in-progress action rather than dropping them.
- **Fix:** Capture mid-flight user messages as a queued intent and apply them at the next gate (auto-select the recommended option + start build when pre-authorized), or at minimum acknowledge "I'll do that as soon as research finishes."
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py`

### 2.9 — LOW — Suggestion chips are hardcoded and stay up mid-task
- **Finding:** The quick-suggestion chips above the composer are hardcoded generic — "Build a competitor to YouTube", "An internal tool for my team", "A booking page for my salon" — and stay visible unchanged while the controller is three messages deep scoping a houseplant app and after the product is fully scoped. Offering "build a YouTube competitor" mid-conversation feels un-tailored and is irrelevant clutter.
- **Why it matters:** ChatGPT/Claude chips adapt to conversation state and disappear once you're mid-task.
- **Fix:** Hide the generic starter chips once a scoping conversation/build is underway and swap in the context-aware `ctlNextChips` next-step suggestions as soon as the first idea is sent (not only after scoping completes).
- **File:** `/home/swami/projects/agent-os/scripts/console.py:956`

---

## 3. Onboarding

### 3.1 — HIGH — First action is rejected; no guided first-run
- **Finding:** The founder's very first action — typing their product idea — is rejected. After creating a company you hit TWO reactive setup gates: a "Connect a model provider" banner, then (only after you submit your idea) a controller reply "accept the AI-processing consent in Settings → Privacy … then say ready". There is no guided sequence tying signup → company → provider → consent → first build; the Help page even lists an "onboarding: guided first-run setup" topic that never runs.
- **Why it matters:** Stripe/Linear show a persistent "X of N steps" checklist and walk you through connect BEFORE the first real action; Linear preserves your first typed input across the setup step.
- **Fix:** Add a real first-run checklist on the Assistant ("1 Create company ✓, 2 Connect AI, 3 Approve AI use, 4 Describe your product") and collapse provider+consent into ONE guided step up front. Preserve the typed description and auto-resume the build after consent instead of making them re-state "ready".
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 3.2 — HIGH — Typed product idea silently vanishes on the model gate
- **Finding:** The describe box shows immediately, so a founder naturally types their idea and hits Send BEFORE connecting a model. Instead of acknowledging it, the app silently teleports to the Providers settings page and the typed idea visibly vanishes. The reassuring copy "Saved your idea ✓ — connect an AI model first and I'll start automatically" is written to `#cnote` then immediately wiped by `go('providers')` in the same function, so the user never sees it. From the founder's POV: I wrote a thoughtful sentence, the page jumped to a settings screen I didn't ask for, and my words are gone. Reproduced twice.
- **Why it matters:** ChatGPT/Linear never discard a typed message on a gate — they keep it in the composer and surface the blocker inline; Linear's command bar preserves draft text across modal transitions.
- **Fix:** Don't navigate away silently. Keep the user on the Assistant with the reassurance + an inline "Connect a model" CTA in place, or carry the message onto Providers (set a banner: "Your idea is saved — connect a model and we'll start building it automatically"). Never destroy the confirmation you just rendered.
- **File:** `/home/swami/projects/agent-os/scripts/console.py:981`

### 3.3 — MED — Gate dumps you on a dead-end Providers page with no way back
- **Finding:** When the gate drops you on Providers you lose all onboarding context: the 4-step wizard header isn't shown there, there's no breadcrumb explaining why you're here, and after you connect a model nothing tells you to return to the Assistant or that your idea is waiting. You must independently notice the left-nav "Assistant" link. The "I'll start automatically" promise only fires if `PEND_IDEA` survives in JS and BOTH provider+consent complete while you're back on the controller view — fragile, and a full reload wipes it.
- **Why it matters:** Notion/Linear onboarding auto-advances to the next step the instant the current one is satisfied, with a persistent "Step X of Y" rail so you never feel dropped on a dead-end settings page.
- **Fix:** When the user arrives at Providers via the gate, render the wizard step bar plus a primary "Continue → describe your product" button that appears the moment a model connects, and persist the pending idea **server-side** so it survives reloads.
- **File:** `/home/swami/projects/agent-os/scripts/console.py:922`

### 3.4 — MED — Full 13-item nav shown to a 0-company user
- **Finding:** The brand-new 0-company user sees the full 13-item nav (Cockpit, Projects, Design, Approvals, Activity, Agents, Agentic features, Templates, Portfolio, Billing, Providers, Integrations + Assistant) before they have anything to put in it. Most land on empty/confusing screens (e.g. Design → "No org selected"). Overwhelming, and it buries the one action that matters.
- **Why it matters:** Linear/Notion progressively reveal navigation and keep onboarding on a single next action.
- **Fix:** During the 0-company first-run state, reduce/de-emphasize the nav to essentials (Assistant + create-company) and progressively reveal the rest once a company exists.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 3.5 — MED — "Create company" CTA dumps the user on a list page with a second empty state
- **Finding:** The prominent "Create your first company" CTA navigates to the full "My orgs" list page, which shows its own redundant header plus a second "No orgs yet / Create your first organization above" empty state and an inline form. Two stacked empty states and a jarring context switch for what should be a single focused action.
- **Why it matters:** Linear/Notion open an inline focused "Name your workspace" step rather than routing to a separate list screen.
- **Fix:** Make the welcome CTA open a focused inline create form or modal ("Name your company" + optional one-line vision) with the name field auto-focused, instead of dumping the user on the list page.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 3.6 — MED — Re-asked for consent already accepted
- **Finding:** Consent was accepted in Settings (`/api/settings/consent` returned `accepted:true`) and the `say()` consent gate passed — yet the assistant LLM still replied "I can't start researching yet … Have you accepted the AI-processing consent in Settings → Privacy? Please confirm with 'yes, accepted'." It re-asks because the stale `consent_required` message sits in chat history and the model isn't told consent is now on file.
- **Why it matters:** Stripe/Linear never re-prompt for a setting you've completed; satisfied prerequisites disappear from the flow.
- **Fix:** When consent+provider are satisfied, inject a system note into the controller prompt ("AI-processing consent is on file; provider connected — proceed") and/or drop resolved gate messages from the model context so it stops re-asking.
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py`

### 3.7 — LOW — Two parallel onboarding state machines can disagree
- **Finding:** A client-side 4-step header (`firstRunSteps`, driven by PROVIDER_OK/CONSENT_OK) and a separate server `/api/onboarding` "Get set up" banner on the Cockpit (welcome/provider/consent/first_build) track progress independently and can show inconsistent "next step" guidance to the same first-run user.
- **Why it matters:** A first-run user should see one coherent checklist, not two that can disagree.
- **Fix:** Drive both surfaces from one source of truth (the server onboarding state), or remove the duplicate Cockpit banner.
- **File:** `/home/swami/projects/agent-os/scripts/console.py:864`

### 3.8 — LOW — Thin landing page / weak value prop before signup
- **Finding:** The root drops you onto a bare signup card with one sentence of value prop ("Be the CEO of a company of AI agents that build & ship your software."). No "how it works", example output, or social proof before handing over name/email/password.
- **Why it matters:** Linear/Vercel/Notion lead with a concise hero + example before asking you to create an account.
- **Fix:** Add a brief value-prop hero (what it does + a 3-step "how it works" + one example output) above/beside the signup card.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 3.9 — LOW — Email verify works but lacks OTP polish
- **Finding:** Verification is honest and solid — the 6-digit code is shown on-screen and clearly labeled "(self-hosted: no email service configured, so here's your code)", and verify returns a real token. But the code field doesn't auto-focus and you must click Verify even after typing all 6 digits.
- **Why it matters:** Stripe/Vercel OTP fields auto-focus, accept a pasted code, and submit automatically on the final digit.
- **Fix:** Auto-focus the code input when the verify panel appears; auto-submit once 6 digits are entered; accept paste of the whole code.
- **File:** `/home/swami/projects/agent-os/scripts/console.py:540`

### 3.10 — LOW — "Approve AI use" label/button name collision
- **Finding:** The Step-3 card uses the exact string "Approve AI use" for BOTH the bold descriptive label ("Step 3 — Approve AI use.") and the actionable button. During automated/ambiguous interaction a text-based click can land on the non-interactive label and silently do nothing (consent stayed false with no error). A screen-reader/automation footgun.
- **Why it matters:** Stripe/Linear give CTA buttons distinct, action-oriented labels that don't duplicate the surrounding descriptive text.
- **Fix:** Give the button a distinct label such as "Approve & continue".
- **File:** `/home/swami/projects/agent-os/scripts/console.py:806`

---

## 4. Provider / Billing / Account

### 4.1 — HIGH — Providers page is written for a server operator, not a founder
- **Finding:** The Providers page reads like ops docs: "real OAuth via the host CLI", "claude auth login / codex login", "subscription OAuth is first-party-CLI-only — no per-tenant token", "suits a self-hosted single-operator box", "No provider → platform default". The Assistant's banner repeats the jargon. A non-technical founder has nothing "signed in on this machine", doesn't know what an API key is, and cannot parse host-CLI/self-host internals.
- **Why it matters:** Stripe Connect and Vercel integrations show a logo + one plain sentence + a single "Connect" button, hiding all protocol detail.
- **Fix:** Rewrite provider copy in plain language ("Connect an AI model to let your agents run") with a single Connect button per provider and a logo; move host-CLI / self-host / OAuth-vs-key internals behind a collapsed "Technical details" disclosure.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 4.2 — HIGH — API-key entry uses a raw native prompt() (unmasked, looks phishy)
- **Finding:** "Use an API key" triggers a raw native browser `prompt()`: "Paste your anthropic API key (sk-ant-…):". It's unstyled, the key is shown in clear text (no masking), there's no paste-verify, no "where do I find this?" link, and in a headless/automation context it silently returns null and appears to do nothing. For a non-technical founder this looks broken and slightly phishy — and it's the ONLY non-CLI way in.
- **Why it matters:** Stripe/OpenAI key entry uses an inline masked field with validation and a direct link to where the key lives — never a native `prompt()`.
- **Fix:** Replace the `prompt()` with an inline form row on the provider card: a masked password-type input with paste support, a "Validating…" state, real inline error text, and a "Get your key" link to the provider's key page.
- **File:** `/home/swami/projects/agent-os/scripts/console.py:1092`

### 4.3 — MED — Connect gives no feedback; state flips before the server confirms (re-gates and re-discards the idea)
- **Finding:** Clicking Connect gives no progress or success feedback — `provSub`/`provAdd` navigate to the Providers list optimistically with no "Connecting…" or "Connected ✓" state, and the connection registers a few seconds later. Meanwhile `PROVIDER_OK` only refreshes on controller re-render or the 15s topbar poll, so there's a window where Providers already shows "connected" but the Assistant still insists "Step 2 — Connect an AI model" and re-gates Send — **discarding the idea a SECOND time**. Only worked after waiting 7s and reloading.
- **Why it matters:** Linear/Notion integration connects show an instant spinner then a green "Connected" toast, and the rest of the app reflects it synchronously.
- **Fix:** Show a "Connecting…" state, await real server confirmation before flipping the tile to connected, and set `PROVIDER_OK=true` immediately on a successful connect so the Assistant gate clears without waiting for a poll.
- **File:** `/home/swami/projects/agent-os/scripts/console.py:1096`

---

## 5. Fleet / Orgs (Naming)

### 5.1 — HIGH — Three different nouns for the same concept
- **Finding:** Three nouns name one concept across the first run: welcome card "Create your first company", left nav "My orgs", top switcher "All orgs (home)", orgs page "Each org is its own company" + "No orgs yet" + "Create your first organization above." A non-technical founder cannot tell whether company, org, and organization are the same thing.
- **Why it matters:** Notion/Slack/Linear each pick a single noun ("workspace") and use it on every surface, so the mental model never wobbles.
- **Fix:** Pick ONE user-facing noun (recommend "company") and use it everywhere — nav "My orgs"→"My companies", switcher "All orgs (home)"→"All companies (home)", orgs page header/empty-state "org"/"organization"→"company".
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

---

## 6. Edge / Error / Mobile

### 6.1 — LOW — Not usable on mobile (icon-only rail, no drawer)
- **Finding:** At phone width (~390px) the signed-in shell renders as a cramped icon-only left rail (labels disappear) with main content squeezed beside it; no hamburger/drawer collapse.
- **Why it matters:** ChatGPT/Linear collapse the sidebar into a drawer on mobile and give content the full viewport.
- **Fix:** Below ~700px, collapse the sidebar into a top hamburger drawer and give the main column full width.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 6.2 — LOW — Consent gate returns HTTP 400 (trips error monitoring)
- **Finding:** Submitting the first product description while AI-consent is pending returns HTTP 400 Bad Request. The UI handles the gate gracefully, but an expected consent-gate state should not be a 400 — it trips error monitoring and looks like a real failure.
- **Why it matters:** Stripe/Linear return 200 with a structured "action required" payload for expected gated states, reserving 4xx for genuine client errors.
- **Fix:** Return HTTP 200 with a `{blocked:'consent_required'}` payload for the gate path instead of 400.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 6.3 — LOW — Nav selection and rendered view drift out of sync
- **Finding:** On initial load and after some navigations the top bar title showed "Assistant" while the body rendered Cockpit/onboarding (`CUR` defaults to 'cockpit'; clicking Assistant didn't always re-render the controller view — had to call `go('controller')` programmatically). A real user clicking "Assistant" sees Cockpit instead.
- **Why it matters:** Linear/Stripe keep nav selection and content always in sync.
- **Fix:** Ensure `go('controller')` always renders the controller view and that the default landing view matches the highlighted nav item; reconcile `CUR` with the rendered view on load.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

---

## Suggested sequencing

1. **Unblock the core journey first** — 2.1 (empty-options race). Nothing else matters if research dead-ends.
2. **Ship the async-loop trust fixes** — 2.2 (no hallucinated "Done"), 2.3 (honest ETA), 2.4 (live progress), 2.5 (real pings + self-host push), 2.6 (Stop actually stops), 2.7 (ping on overrun). Cheapest path to "feels trustworthy"; reuses existing code (`estimate.py`, the 5s poll, `push.send`).
3. **Then latency** — 1.1/1.2. The warm-path + streaming rework is bigger but uplifts every single turn.
4. **Then stop eating the founder's input + guided onboarding + naming** — 3.2 (persist the idea), 3.1 (guided first-run), 4.2/4.3 (real key entry + connect feedback), 4.1, 5.1.
5. **Polish** — 2.8, 2.9, 3.3–3.10, 6.x as fast-follows.
