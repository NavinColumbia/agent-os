# Dogfood Backlog — Prioritized

_Source: dogfood findings from a first-run founder journey (signup → create company → connect AI → describe product → research/build loop). Synthesized and prioritized by Head of Product._

> Note on scope: the brief referenced 49 findings; 19 complete findings survived in the payload (the JSON was truncated mid-record). This backlog covers those 19. If the full 49 exist, re-run against the complete set — the themes below will hold but counts will grow.

---

## Executive Summary

**The honest verdict:** Not yet something a stranger would pay for. The *core promise* — "be the CEO of a fleet of AI agents that build your software" — is undermined at the two moments that matter most: **getting in** (onboarding is a scavenger hunt, not a guided flow) and **waiting** (the async build/research loop gives no ETA, no live progress, no ping, no cancel, and at least once **lies that work is done**). The underlying engine may be fine; the product *narrates itself badly*. Fixable, but today the first 10 minutes would lose most non-technical founders.

**The top 8 things hurting the product most** (lead with MISSING capabilities + async ETA/ping/SLA + latency):

| # | Severity | Theme | Problem | One-line fix |
|---|----------|-------|---------|--------------|
| 1 | **blocker** | Speed | Every Assistant turn — even "hi" — blocks ~30s with no streaming; root cause is a clarifying chat turn spawning a cold `claude -p` Opus subprocess with the full ~2000-word role charter as system prompt | Don't route clarifying chat through `factory.agent`; use a fast, small-prompt path and **stream tokens** |
| 2 | **high** | Async UX | The assistant claims **"Done — research written to…"** while the job is still running (hallucinated completion on the `awaiting=='fleet'` fall-through) | When `awaiting=='fleet'`, never free-form LLM answer; reply with real job status only |
| 3 | **high** | Async UX | **No ETA/cost** when a build/research kicks off — `estimate.py`/`/api/estimate` exist but are never called from the controller | Call `estimate.estimate(kind)` at RESEARCH/IMPLEMENT dispatch and fold into the report |
| 4 | **high** | Async UX | **No live progress** — a 9+ min run shows only a static "give me a little time" bubble | Render a live status + elapsed timer bubble from the existing 5s poll |
| 5 | **high** | Async UX | **No ping when awaited results land** — options-ready and prototype-ready fire `urgent=False`, so closing the tab = no notification (the exact "it said it'd come back and left me hanging" complaint) | Send a push on options-ready and prototype-ready, not just failures/final ship |
| 6 | **high** | Async UX | **No Stop/Cancel** anywhere — once you hit Send you cannot abort the ~30s turn or the multi-minute job | Add a Stop button + `/api/controller/cancel` that parks the thread on a feedback gate |
| 7 | **high** | Onboarding | First action (typing the product idea) is **rejected** behind two reactive setup gates (provider banner, then consent) with no guided sequence; the promised guided first-run never runs | One guided checklist (company → connect AI → approve → describe); preserve typed input and auto-resume after consent |
| 8 | **high** | Provider/Naming | Providers page is written **for a server operator** ("real OAuth via host CLI", "self-hosted single-operator box"); and three nouns — company / org / organization — name one concept | Plain-language "Connect an AI model" + one button; pick ONE noun ("company") everywhere |

**Pattern:** the product's weakest layer is the **conversation that wraps the engine** — latency, status honesty, expectation-setting, and notification. Five of the top eight are async-loop trust issues. Fix the loop's narration and the product roughly triples in perceived quality without touching the agents themselves.

**Counts**
- By severity: **blocker 1, high 11, med 4, low 3** (total 19)
- By theme: Core build + async UX 7, Onboarding 5, Speed/latency 2, Provider/billing/account 1, Fleet/orgs (naming) 1, Edge/error/mobile 3
- By kind: missing-capability 3, product-judgment 5, latency 3, edge-journey 2, copy 2, broken-promise 1, accessibility 1, other 2

---

## 1. Speed / Latency

### 1.1 — BLOCKER — Every turn blocks ~30s with no streaming
- **Finding:** Every Assistant turn — even "hi" — blocks ~29–32s before anything appears (measured: "hi"=29.2s, "track gym members"=32.3s, "yes that sounds good"=32.3s). No streaming: first and last token arrive together; only a static "… thinking" bubble. Root cause confirmed from the live process list: a clarifying chat turn routes through `factory.agent("research-growth", …)` → spawns `claude -p --model claude-opus-4-8`, prepending the entire ~2000-word role charter as system prompt just to ask one clarifying question. CLI cold-start + Opus + giant prompt = 30s for a sub-second job.
- **Why it matters:** This is the *core loop* of the product — a multi-turn scoping chat. Best-in-class (ChatGPT/Claude) render the first token in <2s and stream. A 30s frozen turn, every turn, reads as broken.
- **Fix:** Do not route clarifying chat through `factory.agent`/the heavy role charter. Use a fast path: small model or trimmed system prompt, warm process, and **stream tokens (SSE)** into the bubble.
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py` (dispatch) + `/home/swami/projects/agent-os/scripts/console.py` (rendering)

### 1.2 — MED — Clarifying turns feel frozen (no streaming)
- **Finding:** Each DISCOVER clarifying turn took ~30s end-to-end with only a static "… thinking" bubble — no token streaming. Across a multi-turn scoping conversation this repeatedly feels frozen/broken.
- **Why it matters:** ChatGPT/Claude stream; perceived latency is a fraction of agent-os's blocking turn.
- **Fix:** Stream the controller's LLM replies token-by-token (SSE) instead of a blocking 90s POST that reveals the whole message at once. (Same root cause as 1.1.)
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

---

## 2. Core Build + Async UX

### 2.1 — HIGH — Assistant hallucinates "Done" while the job is still running
- **Finding:** Typing anything while a job runs (`awaiting=='fleet'`) falls through to the generic branch `_report(_llm(..., 'Answer the CEO briefly.'))` at `loopcontroller.py:325`. This produced a confident, **hallucinated** message: "Done — research written to docs/research/invoicing-competitive-landscape.md … every price fetched live today" — while the real run was still 'running' and never advanced to OPTIONS. The founder reads "Done", expects a plan, gets nothing.
- **Why it matters:** Claude/ChatGPT never claim a tool result that hasn't returned. A false "Done" erodes all trust in everything the assistant says.
- **Fix:** When `awaiting=='fleet'`, do NOT run a free-form LLM answer. Reply with the real job status ("Still researching — ~N min in; I'll post options here and ping you"). Optionally suppress duplicate identical user messages with "I'm already on it."
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py`

### 2.2 — HIGH — No time/cost estimate when work kicks off
- **Finding:** When the assistant starts research/build it says only "Give me a little time — I'll research this and come back with a few directions" (`loopcontroller.py:254`). No time estimate, cost, or range. `estimate.py` already computes minutes/$ per build kind and `/api/estimate` is wired — but it is **never** called from the controller conversation (only the standalone Build form via `showEst`).
- **Why it matters:** Linear/GitHub Actions show an ETA the instant a long job starts. The most important async moment gives zero expectation-setting.
- **Fix:** At the RESEARCH and IMPLEMENT dispatch points in `advance()`, call `estimate.estimate(kind)` and fold the result into `_report` ("Researching now — usually ~8–12 min. I'll post options here and ping you the moment they're ready."). Show the same estimate as a chip in the assistant header.
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py`

### 2.3 — HIGH — No live progress during a multi-minute run
- **Finding:** During a research run exceeding 9 minutes, the only progress signal is a tiny gray header line "Research · waiting on your agents to finish". No spinner in-thread, no step/percent, no elapsed timer. The chat just shows the last "give me a little time" bubble. A user cannot tell if it is working, stuck, or dead.
- **Why it matters:** ChatGPT streams "Searching… / Reading…" step chips with a running spinner; Claude shows a live activity log so a multi-minute task feels alive.
- **Fix:** The 5s CTLPOLL already runs — have it render a **progress bubble** (live status + elapsed timer) inside the thread while `awaiting=='fleet'` (e.g. "Researching: 3/5 sources analyzed · 6m elapsed") instead of just replacing history.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 2.4 — HIGH — No ping when the awaited result lands (broken promise)
- **Finding:** Push notifications (`urgent=True → push.send`) fire only for consent/provider gates, job FAILURES, qa-fail, and the FINAL DELIVER message. The two intermediate results a founder actually waits on — **research OPTIONS ready** (`loopcontroller.py:382`) and **PROTOTYPE ready** (`loopcontroller.py:387`) — report with `urgent=False`, so NO ping. If the founder closes the tab during a 10+ min run, nothing notifies them when options land. This is exactly the owner's complaint: it says "I'll come back" and leaves you hanging.
- **Why it matters:** CI tools (Vercel/GitHub) push a notification on every milestone, not just the last one.
- **Fix:** Add `urgent=True` (or a normal-priority push) to the options-ready and prototype-ready `_report` calls so the tenant's ntfy/feed lights up when an awaited result arrives.
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py`

### 2.5 — HIGH — No Stop / Cancel anywhere
- **Finding:** `ctlSend` (`console.py:777`) just disables Send and shows "… thinking" with a 90s client abort; a dispatched research/build `controller_jobs` row has no cancel endpoint. Once you hit Send you cannot abort the ~30s LLM turn or the multi-minute job — only wait or close the tab (orphaning you from the result).
- **Why it matters:** ChatGPT/Claude show a prominent Stop button the entire time a response generates — the single most-expected control in an AI chat.
- **Fix:** Add a Stop button in the composer that aborts the in-flight fetch, and a `/api/controller/cancel` that marks the active `controller_jobs` row cancelled and parks the thread on a `user_feedback` gate. Surface "Stopped — say retry to resume."
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 2.6 — MED — Stalled runs recover, but with no user-visible SLA
- **Finding:** Stall recovery (`resume_stalled`, `loopcontroller.py:526`) only triggers after `RUNNING_TIMEOUT_MIN=30` from a backend scheduler — no user-visible SLA. If a run hangs, the founder sees the same "give me a little time" for up to 30 minutes with no "taking longer than expected" notice and no manual retry/cancel.
- **Why it matters:** Vercel/GitHub flag "running longer than expected" and offer cancel; users are never left guessing whether a job is dead.
- **Fix:** Add a user-facing watchdog: if a fleet job exceeds its estimate, post "This is taking longer than usual — still running (Nm elapsed). Retry or cancel?" with buttons, well before the 30-min reaper.
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py`

### 2.7 — LOW — Suggestion chips are hardcoded and stay up mid-task
- **Finding:** The quick-suggestion chips above the composer are hardcoded generic — "Build a competitor to YouTube", "An internal tool for my team", "A booking page for my salon" (`console.py:632`) — and stay visible unchanged mid-research and after the product is fully scoped, where they're irrelevant clutter.
- **Why it matters:** ChatGPT/Claude chips adapt to conversation state and disappear once you're mid-task.
- **Fix:** Make chips context-aware: hide them once a build is in flight, and after scoping swap them for next-step suggestions tied to the current phase (the OPTIONS/next_steps meta already carries dynamic suggestions elsewhere).
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

---

## 3. Onboarding

### 3.1 — HIGH — First action is rejected; no guided first-run
- **Finding:** The founder's very first action — typing their product idea — is rejected. After creating a company you hit TWO reactive setup gates: a "Connect a model provider" banner, then (only after you submit your idea) a controller reply "accept the AI-processing consent in Settings → Privacy … then say ready". There is no guided sequence tying signup → company → provider → consent → first build; the Help page even lists an "onboarding: guided first-run setup" topic that never runs. The user is sent on two scavenger hunts.
- **Why it matters:** Stripe/Linear show a persistent "X of N steps" checklist and walk you through connect BEFORE the first real action; Linear preserves your first typed input across the setup step.
- **Fix:** Add a real first-run checklist on the Assistant ("1 Create company ✓, 2 Connect AI, 3 Approve AI use, 4 Describe your product") and collapse provider+consent into ONE guided step up front. Preserve the typed description and auto-resume the build after consent instead of making them re-state "ready".
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 3.2 — MED — Full 13-item nav shown to a 0-company user
- **Finding:** The brand-new 0-company user sees the full 13-item nav (Cockpit, Projects, Design, Approvals, Activity, Agents, Agentic features, Templates, Portfolio, Billing, Providers, Integrations + Assistant) before they have anything to put in it. Most land on empty/confusing screens (e.g. Design → "No org selected"). Overwhelming, and it buries the one action that matters.
- **Why it matters:** Linear/Notion progressively reveal navigation and keep onboarding on a single next action.
- **Fix:** During the 0-company first-run state, reduce/de-emphasize the nav to essentials (Assistant + create-company) and progressively reveal the rest once a company exists.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 3.3 — MED — "Create company" CTA dumps the user on a list page with a second empty state
- **Finding:** The prominent "Create your first company" CTA navigates to the full "My orgs" list page, which shows its own redundant header plus a second "No orgs yet / Create your first organization above" empty state and an inline form. Two stacked empty states and a jarring context switch for what should be a single focused action.
- **Why it matters:** Linear/Notion open an inline focused "Name your workspace" step rather than routing to a separate list screen.
- **Fix:** Make the welcome CTA open a focused inline create form or modal ("Name your company" + optional one-line vision) with the name field auto-focused, instead of dumping the user on the list page.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 3.4 — MED — Re-asked for consent already accepted
- **Finding:** Consent was accepted in Settings (`/api/settings/consent` returned `accepted:true`) and the `say()` consent gate passed — yet the assistant LLM still replied "I can't start researching yet … Have you accepted the AI-processing consent in Settings → Privacy? Please confirm with 'yes, accepted'." It re-asks because the stale `consent_required` message sits in chat history and the model isn't told consent is now on file. The user is gated on something they already did.
- **Why it matters:** Stripe/Linear never re-prompt for a setting you've completed; satisfied prerequisites disappear from the flow.
- **Fix:** When consent+provider are satisfied, inject a system note into the controller prompt ("AI-processing consent is on file; provider connected — proceed") and/or drop resolved gate messages from the model context so it stops re-asking.
- **File:** `/home/swami/projects/agent-os/scripts/loopcontroller.py`

### 3.5 — LOW — Thin landing page / weak value prop before signup
- **Finding:** The root drops you onto a bare signup card with one sentence of value prop ("Be the CEO of a company of AI agents that build & ship your software."). No "how it works", no example output, no social proof or screenshots to convince a first-time founder before handing over name/email/password.
- **Why it matters:** Linear/Vercel/Notion lead with a concise hero + example before asking you to create an account.
- **Fix:** Add a brief value-prop hero (what it does + a 3-step "how it works" + one example output) above/beside the signup card. Acceptable to keep minimal for a self-host console, but the current first impression is thin for a "product".
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

---

## 4. Provider / Billing / Account

### 4.1 — HIGH — Providers page is written for a server operator, not a founder
- **Finding:** The Providers page reads like ops docs: "real OAuth via the host CLI", "claude auth login / codex login", "subscription OAuth is first-party-CLI-only — no per-tenant token", "suits a self-hosted single-operator box", "No provider → platform default". The Assistant's banner repeats the jargon: "use the Claude / ChatGPT account already signed in on this machine (no key) or an API key." A non-technical founder has nothing "signed in on this machine", doesn't know what an API key is, and cannot parse host-CLI/self-host internals.
- **Why it matters:** Stripe Connect and Vercel integrations show a logo + one plain sentence + a single "Connect" button, hiding all protocol detail.
- **Fix:** Rewrite provider copy in plain language ("Connect an AI model to let your agents run") with a single Connect button per provider and a logo; move host-CLI / self-host / OAuth-vs-key internals behind a collapsed "Technical details" disclosure.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

---

## 5. Fleet / Orgs (Naming)

### 5.1 — HIGH — Three different nouns for the same concept
- **Finding:** Three nouns are used for one concept across the first run: welcome card says "Create your first company", left nav says "My orgs", top switcher says "All orgs (home)", and the orgs page says "Each org is its own company" + "No orgs yet" + "Create your first organization above." A non-technical founder cannot tell whether company, org, and organization are the same thing.
- **Why it matters:** Notion/Slack/Linear each pick a single noun ("workspace") and use it on every surface, so the mental model never wobbles.
- **Fix:** Pick ONE user-facing noun (recommend "company") and use it everywhere. Rename nav "My orgs"→"My companies", switcher "All orgs (home)"→"All companies (home)", orgs page header/empty-state "org"/"organization"→"company".
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

---

## 6. Edge / Error / Mobile

### 6.1 — LOW — Not usable on mobile (icon-only rail, no drawer)
- **Finding:** At phone width (~390px) the signed-in shell renders as a cramped icon-only left rail (labels disappear) with main content squeezed beside it; no hamburger/drawer collapse.
- **Why it matters:** ChatGPT/Linear collapse the sidebar into a drawer on mobile and give content the full viewport.
- **Fix:** Below ~700px, collapse the sidebar into a top hamburger drawer and give the main column full width.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 6.2 — LOW — Consent gate returns HTTP 400 (trips error monitoring)
- **Finding:** Submitting the first product description while AI-consent is pending returns HTTP 400 Bad Request (visible in network/console). The UI handles the gate gracefully, but an expected consent-gate state should not be a 400 — it trips error monitoring and looks like a real failure.
- **Why it matters:** Stripe/Linear return 200 with a structured "action required" payload for expected gated states, reserving 4xx for genuine client errors.
- **Fix:** Return HTTP 200 with a `{blocked:'consent_required'}` payload for the gate path instead of 400.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

### 6.3 — LOW — Nav selection and rendered view drift out of sync
- **Finding:** On initial load and after some navigations the top bar title showed "Assistant" while the body rendered Cockpit/onboarding (`CUR` defaults to 'cockpit'; clicking Assistant didn't always re-render the controller view — had to call `go('controller')` programmatically to get the composer). A real user clicking "Assistant" sees Cockpit instead.
- **Why it matters:** Linear/Stripe keep nav selection and content always in sync.
- **Fix:** Ensure `go('controller')` always renders the controller view and that the default landing view matches the highlighted nav item; reconcile `CUR` with the rendered view on load.
- **File:** `/home/swami/projects/agent-os/scripts/console.py`

---

## Suggested sequencing

1. **Ship the async-loop trust fixes first** (2.1 status-not-hallucination, 2.2 ETA, 2.3 live progress, 2.4 pings, 2.5 stop) — these are the cheapest path to "feels trustworthy" and reuse code that already exists (`estimate.py`, the 5s poll, `push.send`).
2. **Then latency** (1.1/1.2) — the streaming + fast-path rework is bigger but uplifts every single turn.
3. **Then guided onboarding + naming** (3.1, 5.1, 4.1) — converts the people who currently bounce in the first 10 minutes.
4. **Polish** (3.2–3.5, 6.x) as fast-follows.
