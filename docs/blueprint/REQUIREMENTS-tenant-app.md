# Tenant-App Requirements — exhaustive discovery + honest status

**The question this answers:** "Have you truly thought about *every* use case a CEO-of-an-AI-agent-company
would want, and built it?" Honest answer: I had **not** — so I ran real requirements discovery (four
parallel product-manager agents, each on a slice, each grounding findings in the actual code), producing
~150 distinct requirements mapped ✅ built / 🟡 partial / ❌ missing. This is the consolidated map.

## The honest headline
agent-os is a **strong build-factory + governance core**, not yet a complete **command console + business
operator**. The safety/governance layer is genuinely done (consent gate, kill-switch, approvals inbox,
tenant isolation, quota, audit/trace). The gaps cluster in three places:
1. **Command-console UX** — directing by conversation, refining mid-build, per-project budgets *(buildable)*.
2. **Predictive visibility** — the surfaces were descriptive (what *is/was*), not predictive *(buildable)*.
3. **Identity, money & go-live** — real auth, real payments, prod deploy, domains *(mostly GATED on your
   accounts — needs Stripe/cloud/email, not just code)*.

## The 4 discovery slices — top P0 gaps each found
**Onboarding & trust:** no durable accounts/login-recovery (token-only); no real payment entry; **no
pre-commit cost estimate / spend-cap UI**; no account deletion/export (GDPR); no guided first-run.

**Directing the fleet:** **no orchestrator chat / clarifying-question loop** (the defining interaction);
no mid-build refinement ("also add Y"); no per-project budget/priority; agent free-form blockers don't
reach the CEO; no durable versioning/rollback.

**Visibility & operations:** **no budget forecast / burn-rate** ("am I about to blow my budget"); no
pre-emptive 80/100% alerts (caps were deny-only); no plain-language **quality/trust verdict** in the
cockpit; founder-only failure RCAs (not tenant-facing); "what's blocked" only in the operator dashboard.

**Business lifecycle & growth:** no real **prod deploy + custom domains** (localhost only); no **storefront/
their-Stripe checkout** (monetize *their* product); no **their-app analytics**; no data export/offboarding;
no spend-cap alert thresholds.

## Built THIS session (closing the top buildable P0s)
- **Orchestrator chat** (`orchestrator.py`, console "Direct (chat)") — the CEO describes an idea in plain
  words; the orchestrator asks one clarifying question at a time, answers factory-state questions, and when
  the idea is concrete proposes a build the CEO approves with one tap — through the same governed gate
  (consent + quota + factory). *Verified live:* "track my freelance invoices" → it asked paid/unpaid/overdue
  before building. **Closes Directing-P0 #1.**
- **Budget forecast + pre-emptive alerts** (`forecast.py`, surfaced in the Cockpit) — burn-rate from the
  traces time-series → projected % of quota + ETA-to-cap, with a notification at 80% (standard) and 100%
  (urgent), idempotent, swept hourly. **Closes Visibility-P0 #1 & #2.**
- **Console robustness** — fixed the `undefined.map` crashes: a defensive fetch layer renders real
  auth/error/empty states instead of breaking. **Closes a cross-cutting trust/quality gap.**

## Consolidated roadmap (what's next, by buildability)
### P0 — buildable now (no external accounts)
1. **Plain-language quality/trust verdict** in the cockpit (surface `verify.py`/QA: "✅ tests pass, security
   clean, independently checked") — the CEO can't read code; the verdict is the point.
2. **Pre-commit cost estimate** ("this build ≈ $X / ~N min") before spend.
3. **Per-project budget + priority** (today budget is one global env var).
4. **Guided first-run wizard** (signup → key/credits → consent → first build) + the "you're the CEO" framing.
5. **Mid-build refinement / cancel-and-discard**; **durable versioning + rollback**.
6. **Agent free-form blocker → CEO** (free-form questions reach the chat/approvals, not just structured ones).
7. **Account data export / deletion** (GDPR/CPRA — also a no-lock-in selling point).

### P0 — GATED (needs your accounts/decisions, can't do autonomously)
- **Real auth + account recovery** (email/password or magic-link; token-only today).
- **Real payments** (Stripe for our plans; today plan-switch is in-app only).
- **Prod deploy to a public URL + custom domains** (Vercel/Netlify/cloud + DNS/TLS).
- **The tenant's own storefront / Stripe Connect** (monetize *their* product).
- **Their-app product analytics** (GA/PostHog connect).

### P1/P2 — depth
Team/RBAC & invites · integrations real OAuth + test-connection + secret rotation · their-app feedback/NPS ·
A/B & growth experiments · compliance-doc generation (ToS/AUP/DPA) · tenant-facing weekly digest ·
cost-anomaly detection · per-agent cost attribution · org-chart/hierarchy view · voice intake.

## What stands honestly
The agent fleet can decompose and build complex multi-module systems fast and at quality, under a real
governance layer. It is **not** a finished, 240-screen, pay-and-go SaaS: the end-user surface now spans all
16 areas and the two defining UX gaps (chat, forecast) are built and working, but real **identity, payment,
and go-live** remain — and those are gated on your accounts, not on more code from me.

*Method: 4 parallel requirements-discovery agents (jobs-to-be-done × must-have × edge/failure × priority ×
built-status) grounded in the repo, June 2026. This doc is the synthesis; it is the working backlog.*
