# agent-os Platform — Product Lifecycle (the part *after* "build")

Building is one stage. A product *lives*: it gets pitched, marketed, sold, measured, improved, protected
(IP), and explained (whitepaper). This blueprint extension covers the **full lifecycle** — for **(A) the
customers' products** the factory ships, and **(B) our own product** (the factory app we publish) — plus
**(C) external-integration research** (some products need third-party APIs / cloud / domain knowledge), and
**(D) the visible task board** so "what was asked + its status" is never ambiguous.

## The lifecycle stages
`IDEATE → BUILD → VERIFY → LAUNCH → MARKET → SELL → MEASURE → IMPROVE`  ⟲ (loop)
with two cross-cutting **assets** produced along the way: **IP/Patent** and **Whitepaper/positioning**.
The factory already covers IDEATE→BUILD→VERIFY→IMPROVE (charter → recursive build → scalable verify →
`improve.py` safe-deploy loop). This doc is the rest.

---

## (D) Task Board — *implemented now* (`taskboard.py`)
Every request becomes a card: **asked → in_progress → blocked → done**, queryable, audited. Operator gets a
platform board; each tenant gets their own ("Requests" view). Fixes the "I thought we were just discussing
it" gap. *Views:* board (kanban by status), list, card detail (history/notes), per-tenant filter.

---

## (A) Lifecycle for the CUSTOMERS' products (what users see/do for things they build)
| Stage | Feature / view | Status |
|---|---|---|
| Launch | **Launch kit** (landing page, README, install/run, store-listing copy) | ✅ `launch_kit.py` (marketing-as-code) |
| Market | **Marketing generator** — landing pages, ad copy, SEO pages, social posts, email sequences (publish stays human-gated) | 🟡 partial (launch_kit) → extend |
| Market | **Pitch/demo generator** — one-pager, deck, demo script, Show-HN/Product-Hunt copy | ❌ new |
| Sell | **Self-serve storefront / Stripe** for the user's product (Stripe Connect) + pricing page generator | 🟡 (integrations) → wire |
| Feedback | **Feedback loops** — in-product feedback widget, NPS, surveys, support inbox for *their* users; routes back as improvement tasks | ❌ new |
| Measure | **Product analytics** — visitors, funnel, retention, Web Vitals, per-feature usage (the "how is it doing" view) | 🟡 (app-analytics screen) → build |
| Improve | **Continuous-improvement loop** — metrics → eval-gated improvement → safe deploy | ✅ `improve.py` |
| Grow | **Growth experiments** — A/B tests, SEO content engine, referral mechanics | ❌ new |
| Protect | **IP helper** — flags potentially-novel mechanisms, drafts a provisional-patent outline | 🟡 (ip-patent-advisor role) → surface |

## (B) Lifecycle for OUR OWN product (the factory app we publish)
The blueprint's marketing site + admin back-office cover much of this; the gaps are the *go-to-market &
business-asset* views:
| Need | View / capability | Status |
|---|---|---|
| **"How is OUR app doing"** | platform analytics + revenue dashboard (MRR/ARR, activation funnel signup→BYO-key→first-LAUNCH, churn, cohort) | 🟡 blueprint Area-16 + `portfolio.py` → build |
| **Sales / pipeline** | lightweight CRM (leads, demos, trials→paid, enterprise pipeline) | ❌ new |
| **Marketing site** | landing/pricing/features/docs/blog/case-studies | ✅ blueprint Area-1 (to build) |
| **Pitch / investor** | deck, one-pager, metrics sheet, data-room | ❌ new |
| **Whitepaper / positioning** | the "governed autonomous factory" technical whitepaper + positioning vs Lovable/Replit | ❌ new (the research dogfood feeds this) |
| **Patent / IP** | IP register: the patentable mechanisms (governed line, approval-gate, recursive verify), provisional-patent tracker | ❌ new (`ip-patent-advisor` role exists) |
| **Feedback / NPS** | user feedback inbox, NPS, feature-request board (public roadmap) | ❌ new |
| **Competitive intel** | market/competitor tracking | ✅ `intel.py` (advisory) |
| **Founder digest** | weekly state + next steps | ✅ `digest.py` |

**Product features/descriptions we can ADD (selling points):** a **"Lifecycle Suite"** — *"the factory
doesn't just build your app, it launches, markets, measures and improves it"* — is a strong differentiator
vs. pure code-gen tools (Lovable/Replit stop at "built"). That's a real wedge to put in the product
description + pricing (a paid tier unlock).

---

## (C) External-integration research capability
Some products need third-party systems (Stripe, Twilio, **Samsara** for fleet mgmt, Salesforce, etc.),
cloud/hardware, or domain knowledge. The capability:
- **Agents already have web tools** (`WebSearch`/`WebFetch` in `factory.AGENT_TOOLS`) — so a builder/role
  agent *can* research an external API's docs before integrating. ✅ exists.
- **To make it first-class:** add an **integration-research step** — when the architect marks a component
  as needing an external system, a research-growth agent first reads that system's API docs (web) and
  writes an `integration-notes.md` (auth, endpoints, rate limits, SDK) that the builder codes against.
  Pairs with **OSS-assembly** (`reuse` + per-product venv) so the official SDK can be installed. 🟡 to wire.
- **Gated by approval:** real credentials/spend/cloud stay human-approved (the existing approval gate).
- **Hardware/cloud/GPU:** provisioned with the user's creds via the integrations + deploy flows (blueprint
  02); the factory researches + writes the IaC, the human approves the spend.

---

## Build order (extends the master roadmap)
1. **Task board** ✅ (`taskboard.py`) — done.
2. **Lifecycle suite for users' products**: marketing/pitch generators (extend `launch_kit.py`), feedback
   widget + analytics, growth experiments. *(highest leverage — it's a sellable differentiator)*
3. **Our-own go-to-market views**: revenue/funnel analytics, lightweight CRM, pitch + whitepaper + IP
   register, NPS/feedback. *(needed to run the business + the visa/recognition evidence)*
4. **External-integration research step** (wire the integration-notes agent + OSS-assembly).

*Each is buildable by the factory itself (dogfooding). The research dogfood
([`RESEARCH-launch-onboarding.md`](RESEARCH-launch-onboarding.md) — 8-way parallel fleet, 89 cited sources)
backs the launch/publishing/onboarding/notification/observability/resilience specifics. Its top finding is
load-bearing: **the stores forbid central publishing of user-generated apps — each user publishes under
their own account (or via web/PWA)**, and a named AI-consent screen is mandatory cross-platform.*
