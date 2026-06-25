# agent-os — Master Product Blueprint: Monetization, Finance, Legal/Risk, Personas

**Scope:** Productizing agent-os (a governed autonomous AI software factory) into a multi-tenant enterprise SaaS where the customer is "CEO" and an orchestrator of AI agents autonomously builds & ships their apps/businesses/websites.

**Date:** 2026-06. Grounded in real 2025–2026 pricing/economics from Vercel/v0, Replit, Lovable, Cursor, Bolt.new, Retool, Supabase, Anthropic/OpenAI API pricing, AWS/E2B compute, Stripe, and analyst market data. Sources are cited inline.

> **Not legal advice.** §3 is research synthesis; final contract language needs counsel (esp. EU AI Act + DSA exposure).

---

## 0. The strategic thesis (read first)

Four facts from the research drive every decision below:

1. **Inference is the dominant COGS line, not compute.** Opus-class output at **$25/MTok** dwarfs sandbox/CPU cost (E2B ~$0.05/vCPU-hr, Fargate ~$0.04/vCPU-hr). AI-native gross margins run **50–65%**, not classic SaaS 80%+ (ICONIQ avg 52%, improving; Bessemer LLM-native ~65%; inference alone ≈23% of revenue). **→ Margin is won on the model bill: caching, model routing, batching, and BYOK.**

2. **Speed is commoditized; trust is the moat.** Every tool gets a user to 70% of an app fast. Developer trust in AI accuracy *fell to 29%* (2025); the "70/30 wall" (last 30% of bugs/auth/edge-cases) is the #1 churn driver. **→ The "CEO orchestrates agents" pitch only works if agents close the loop (test, review, security-scan, explain).**

3. **Credit/effort billing is the industry's biggest friction point.** Cursor, Replit, Lovable, Retool all moved to it in 2025 and all generated billing-surprise backlash (Cursor publicly apologized + refunded). **→ Predictable/capped pricing is a genuine differentiation lever for non-technical/SMB.**

4. **Two opposite GTM motions.** Non-technical/SMB wants flat price + guardrails (high churn, low ACV). Technical/startup/agency wants *code ownership + export* (non-exportable = Series A red flag). Regulated verticals (health/fintech/edu) are an enterprise-tier game gated by BAA/SOC2/VPC, not an entry-tier game.

**Opinionated north star:** Hybrid pricing (seat + metered agent-work) with a **hard per-org spend cap on by default**, **BYO-key as the margin-saver and risk-shifter**, an **exportable real stack** (React/Postgres) to unlock high-value segments, and **microVM-per-tenant isolation** because we run hostile code.

---

# 1. MONETIZATION & PACKAGING

## 1.1 Billing model: hybrid, decisively

Seat-only underprices heavy agent users and caps revenue. Usage-only terrifies non-technical buyers (the universal 2025 backlash). **Use hybrid:**

- **Platform/seat fee** = predictable floor revenue + gates collaboration/governance features (this is what Vercel, Cursor Teams, Retool all do).
- **Metered "agent-work"** = covers variable inference/compute COGS, billed against an **included monthly allowance** bundled into the seat price, with **overage only after a hard cap the customer must explicitly raise.**

This mirrors the winning shape: Cursor ($20 seat + $20 credit pool), Replit (sub + credit pool), Supabase (base + usage), Vercel (seat + Active-CPU usage).

## 1.2 The meter: what we charge for and how

We meter a single, legible unit — the **Build Credit** — abstracting away tokens/compute the way Lovable/Replit do, because non-technical users cannot reason about tokens. One credit maps to a blended cost basis internally. We meter against these cost drivers (never expose all of them to the user):

| Cost driver | Internal cost basis (2026) | Notes |
|---|---|---|
| **Agent inference (tokens)** | Opus 4.8 $5/$25 per MTok; Sonnet 4.6 $3/$15; Haiku 4.5 $1/$5 (Anthropic) | **The dominant line.** Caching = 90% read discount; batch = 50% off async work |
| **Sandbox/build compute** | E2B ~$0.05/vCPU-hr; Fargate $0.04048/vCPU-hr + $0.004445/GB-hr | Rounding error vs tokens unless GPU |
| **GPU (optional, ML workloads)** | H100 $1.99–4.29/hr; A100 80GB $1.07–1.99/hr (RunPod/Lambda/Modal) | Gate to paid tiers; bill at cost+margin or BYO |
| **Deploy/hosting** | Egress, function invocations, storage (pass Supabase/Vercel-style rates) | Egress $0.09–0.15/GB; storage ~$0.02/GB |
| **Stripe** | 2.9%+30¢ processing + 0.7% Billing (~3.6% all-in) + Connect fees | Built into pricing, not a line item |

**Credit = effort-weighted, not message-count.** Following Lovable's mid-2025 shift: a small edit costs a fraction of a credit; a full app scaffold costs several. This is honest and aligns price to cost — but we pair it with a **hard cap** to avoid the Cursor/Replit backlash.

## 1.3 BYO-key vs platform-key economics (the central margin lever)

| | **Platform-key (we mark up)** | **BYO-key (customer pays inference)** |
|---|---|---|
| Who pays inference | We do, then bill customer | Customer's own Anthropic/OpenAI account |
| Our gross margin on inference | **Markup margin** (we buy at $25/MTok, meter at a credit price implying ~2–3x) | **~100%** on inference — we only charge the platform/orchestration fee |
| Customer perception | "Simple, one bill" | "I control cost + my data + my rate limits" |
| Risk | We carry denial-of-wallet + the model vendor relationship | Shifts inference liability + a kill-switch to customer (see §3) |
| Best for | Non-technical, Free/Pro | Technical, Team/Enterprise, cost-sensitive heavy users |

**Strategy:** Offer **both**, like Replit and Cursor. Default new/non-technical users to platform-key (simpler, we capture markup). Make BYO-key a **first-class Team/Enterprise feature** — it dramatically improves *our* margin (we stop carrying the biggest COGS line) AND shifts inference-cost and some liability to the customer. This is the single best structural margin move: **BYO-key converts a 52% AI-native gross margin into a near-SaaS-grade margin on the orchestration layer.**

**Markup math (platform-key):** If Opus output is $25/MTok and a credit is priced to imply ~$60–75/MTok blended (2.4–3x), gross margin on platform-key inference is ~58–67% *before* the caching/routing wins below. With aggressive caching (90% read discount on repeated context) and routing 60–70% of agent steps to Sonnet/Haiku, effective COGS drops further — realistic blended **65–75% margin on platform-key**, and **~90%+ on BYO-key** (orchestration + compute only).

## 1.4 Credits, allowances, overage, hard caps

- Each paid tier bundles a **monthly credit allowance** (resets monthly; one-month rollover on paid, à la Replit/Bolt — generous enough to feel fair, not so generous it's gameable).
- **Overage:** top-up packs (Lovable model: e.g. $20 / N credits, priced above the bundled rate so heavy users trend toward a higher tier). Purchased credits expire in 12 months.
- **HARD SPEND CAP ON BY DEFAULT** (Supabase Pro model). The org cannot exceed its allowance + an explicitly-set overage budget. This is both a UX win (no billing-surprise churn) AND a critical **denial-of-wallet control** (§3.5). Alerts at 75%/90%; auto-pause at cap.

## 1.5 Free-trial design

- **Free tier (not time-boxed):** daily-refreshed credit allowance (Replit Starter model), 1 published project, platform-key only, our cheapest model routing (Haiku/Sonnet), watermark/subdomain hosting. Anti-abuse: **card + $1 hold or phone verification at signup** (§3.5) — gating the free tier is non-negotiable on a code-execution platform.
- **Pro trial:** 14-day full-feature trial (Opus access, no watermark) with a credit grant, card required, hard-capped so a runaway agent can't generate a surprise bill.

## 1.6 What gets gated Free vs Paid

| Capability | Free | Pro | Team | Enterprise |
|---|---|---|---|---|
| Best model (Opus-class) | ✗ (Haiku/Sonnet) | ✓ | ✓ | ✓ |
| BYO API key | ✗ | optional | ✓ | ✓ |
| Code export / own-the-source | partial | ✓ | ✓ | ✓ |
| Custom domain / no watermark | ✗ | ✓ | ✓ | ✓ |
| Multiple orgs / workspaces | 1 | 1 | many | many |
| Collaborators | 0–1 | up to ~3 | per-seat | unlimited seats |
| White-label / reseller | ✗ | ✗ | add-on | ✓ |
| SSO/SAML, SCIM | ✗ | ✗ | ✗ | ✓ (base, not paywalled-upsell) |
| Audit logs (immutable) | ✗ | basic | extended | full + export |
| RBAC | ✗ | ✗ | ✓ | granular |
| VPC / self-host / on-prem | ✗ | ✗ | ✗ | ✓ |
| BAA / HIPAA, data residency | ✗ | ✗ | ✗ | ✓ |
| Human-in-the-loop deploy gates | basic | ✓ | ✓ | ✓ custom |
| Hard spend cap | ✓ forced | ✓ | ✓ | configurable |

## 1.7 The full tier structure (concrete price points)

Anchored to the 2025–2026 market: **$20–25 entry is the accepted norm**; non-technical users balk when effective spend hits $80–200; technical users pay $60–200 willingly (framed vs a $5K/mo dev). Enterprise is custom.

### FREE — "$0 / Founder"
- For: trying it, hobby, validation. Acquisition funnel.
- Daily-refreshed Build Credits (small), 1 org, 1 published project, Sonnet/Haiku routing, platform-key only, subdomain + watermark, community support, hard cap forced. Card/phone verification required.

### PRO — "$25/mo" (annual ~$20/mo)
- For: solo founder, indie hacker, freelancer.
- Monthly credit allowance (~$25 equiv, 1-mo rollover), Opus access, full code export, 1 custom domain, no watermark, optional BYO-key, basic audit log, basic HITL deploy gates, email support, hard cap on (raisable). Overage top-ups available.
- *Rationale:* hits the proven $25 entry point (Lovable/Bolt/Replit-Core/Supabase-Pro all cluster here).

### TEAM — "$40/user/mo" (annual ~$32) + bundled per-seat credits
- For: startups, agencies, dev shops, SMB teams.
- Everything in Pro per seat, multiple orgs/workspaces, BYO-key first-class, RBAC, extended audit logs, shared credit pool, client workspaces, **white-label add-on**, priority support, deploy approval workflows.
- *Rationale:* matches Cursor Teams $40, Retool Business builder economics, Vercel Pro per-seat shape.

### ENTERPRISE — "Custom" (land at ~$2.5K–10K+/mo floor; reference OutSystems $36K+/yr floors)
- For: enterprise internal-tools teams, regulated verticals.
- SSO/SAML + SCIM **in the base enterprise tier (not an upsell paywall — this is a deliberate differentiator)**, granular RBAC, immutable audit-log export, VPC/self-host/on-prem, **BAA + data residency (US/EU/APAC)**, named subprocessors in DPA, custom HITL governance, SLA, dedicated support, security review support (SOC 2 Type II report), custom contracts/indemnity. BYO-key standard.
- *Rationale:* InfoSec gates the deal more than price; SSO/audit/residency/BAA must be present or the deal dies.

### Feature-by-tier matrix (consolidated)

| Feature | Free | Pro $25 | Team $40/seat | Enterprise (custom) |
|---|---|---|---|---|
| Price | $0 | $25/mo | $40/user/mo | custom |
| Model access | Haiku/Sonnet | + Opus | + Opus | + Opus, custom |
| Build credits | daily refill | monthly allowance | pooled per-seat | custom/committed |
| BYO API key | ✗ | optional | ✓ | ✓ standard |
| Code export | partial | full | full | full |
| Orgs / workspaces | 1 | 1 | many | many |
| Seats / collaborators | 0–1 | ~3 | per-seat | unlimited |
| Custom domain / no watermark | ✗ | ✓ | ✓ | ✓ |
| White-label / reseller | ✗ | ✗ | add-on | ✓ |
| SSO/SAML + SCIM | ✗ | ✗ | ✗ | ✓ (base) |
| RBAC | ✗ | ✗ | ✓ | granular |
| Audit logs | ✗ | basic | extended | immutable + export |
| HITL deploy gates | basic | ✓ | workflows | custom governance |
| VPC / self-host | ✗ | ✗ | ✗ | ✓ |
| BAA / HIPAA / residency | ✗ | ✗ | ✗ | ✓ |
| Hard spend cap | forced | on (raisable) | on (raisable) | configurable |
| Support | community | email | priority | SLA + dedicated |

*Pricing anchors: Cursor $20/$40-seat/$200 Ultra; Replit Core $20/Pro $100; Lovable Pro $25/Business $50; Supabase Pro $25/Team $599; Vercel Pro $20/seat; Retool Business builder $50. Sources: vercel.com/pricing, replit.com/pricing, lovable.dev/pricing, cursor.com/pricing, bolt.new/pricing, retool.com/pricing, supabase.com/pricing.*

---

# 2. FINANCE / UNIT ECONOMICS

## 2.1 Cost structure (COGS lines, ranked by weight)

1. **Model inference (dominant).** Anthropic 2026: Opus 4.8 $5/$25 per MTok, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5; prompt-cache read ≈90% discount; batch ≈50% off. OpenAI alt: GPT-4o $2.50/$10, GPT-4.1 $2/$8. **This is ≈23% of revenue at industry benchmark and the single number to beat.**
2. **Build/sandbox compute.** E2B Firecracker ~$0.05/vCPU-hr; Fargate $0.04048/vCPU-hr + $0.004445/GB-hr. Cheap vs inference. (We need microVM isolation anyway for §3.1 — E2B/Firecracker doubles as the security boundary.)
3. **GPU (only if we offer ML workloads).** H100 $1.99–4.29/hr; A100 $1.07–1.99/hr. Gate to paid; prefer serverless (Modal/Replicate per-second) to avoid idle billing.
4. **Hosting/egress/storage** for deployed tenant apps. Egress $0.09–0.15/GB; storage ~$0.02/GB; function invocations ~$0.60–2/M.
5. **Payments.** ~3.6% all-in (2.9%+30¢ + 0.7% Stripe Billing) + Connect ($2/active account, 0.25%+25¢/payout) if marketplace.
6. **Infra/platform** (DB, orchestration, observability, secrets, control plane) — fixed-ish, amortized.
7. **Support, compliance, S&M, R&D** — opex.

## 2.2 Margin model: BYO-key vs platform-key

| | Platform-key | BYO-key |
|---|---|---|
| Revenue components | Seat + metered credits (incl. marked-up inference) | Seat + metered orchestration/compute only |
| Largest COGS (inference) | **On us** (~23% of rev benchmark) | **On customer** |
| Realistic gross margin | **65–75%** (with caching + routing + batch) | **~88–92%** (orchestration + compute + Stripe only) |
| Strategic role | Simplicity for non-technical; we capture markup | Margin saver + risk shifter for heavy/technical/enterprise |

**Blended target:** If ~40% of revenue runs BYO-key (skewed to Team/Enterprise where the dollars are) and 60% platform-key, blended gross margin lands **~72–78%** — meaningfully above the AI-native 52% average because BYO-key removes our biggest cost line on the highest-ACV accounts.

**The three margin multipliers on platform-key (apply all):**
1. **Prompt caching** — 90% read discount on repeated context (system prompts, codebase context re-sent each step). Break-even at 2 requests. Massive for agentic loops that re-read the same repo.
2. **Model routing** — send 60–70% of agent steps (planning, classification, small edits) to Sonnet/Haiku; reserve Opus for hard reasoning. Haiku is 5x cheaper output than Opus.
3. **Batch API** — 50% off all async/non-interactive work (background builds, test generation, bulk refactors).

## 2.3 Multi-tenant billing architecture

- **Stripe Billing (metered/usage)** as the spine: emit usage events (credits consumed → tokens/compute/agent-tasks) into Stripe's 2026 AI-usage metering. Subscriptions for seat fees; metered for overage; +0.7% Billing fee covers it.
- **Hard caps enforced in our control plane**, not just Stripe alerts (Stripe "budgets" are alerts, not hard stops — §3.5). We gate at the orchestrator: when an org hits its cap, agents pause.
- **Stripe Connect** — only needed **when our customers take payments through apps we host for them** (e.g., a tenant's e-commerce store). Then: choose Connect model deliberately. Critically, **Stripe is NOT merchant of record — we or the connected account are**, carrying chargebacks/refunds/tax (§3b). For most internal-tools/website use cases, Connect is *not* required and we should avoid taking on MoR risk.
- **BYO-key billing**: customer's inference billed directly by Anthropic/OpenAI to their account; we bill only platform/orchestration. Cleaner books, lower COGS, less denial-of-wallet exposure.

## 2.4 Gross-margin targets & path to high MRR

- **Target:** 72–78% blended GM (above AI-native 52% avg via BYO-key mix + caching/routing/batch). Floor acceptable: 60%. Below 50% = restructure pricing or push BYO-key harder.
- **Path to high MRR (illustrative ladder):**
  - Land on **Internal B2B tools + E-commerce ops** beachheads (proven WTP, low compliance friction).
  - **PLG funnel:** Free → Pro $25 (solo founders/indies) drives volume + word-of-mouth.
  - **Expansion engine:** Team $40/seat (agencies/startups/SMB) — net revenue retention via seats + credit overage; white-label add-on for agencies (open whitespace — Lovable et al. are affiliate-only).
  - **ACV anchor:** Enterprise custom ($30K–250K/yr range, OutSystems-style floors) once SOC 2 Type II + SSO/audit/VPC/BAA exist.
  - Comparable trajectories: Lovable $0→$100M ARR in 8 months; Cursor ~$2B ARR; Replit ~$253M ARR. The category supports rapid ARR if PLG + verification-trust land.

## 2.5 Key financial risks

1. **Margin compression from inference** — the structural AI-native risk (every agent step re-runs a model). Mitigate: caching/routing/batch + push BYO-key. **This is the #1 financial risk.**
2. **Denial-of-wallet** — a runaway agent or abusive tenant burns inference/GPU on platform-key. Mitigate: hard caps default-on, per-org budget enforcement at the orchestrator, BYO-key for heavy users.
3. **Credit-billing churn** — the universal 2025 backlash (Cursor refunds). Mitigate: predictable allowances + forced caps + transparent estimator.
4. **Free-tier abuse cost** — crypto miners / resource abuse on free compute (§3.5). Mitigate: signup friction, cheap-model routing, tight free compute quotas.
5. **Model-vendor price/policy shifts** — our COGS is hostage to Anthropic/OpenAI pricing. Mitigate: multi-model routing, BYO-key, negotiate committed-use discounts at scale.
6. **Stripe Connect liability** if we become MoR for tenant revenue (chargebacks/negative balance/tax) — avoid unless the business case is strong (§3b).
7. **Enterprise sales cost** — long procurement, compliance spend (SOC 2 $10K–150K, 6–15 mo) before ACV lands.

---

# 3. LEGAL / COMPLIANCE / RISK

*(Synthesized from the full legal research; see `legal-compliance-risk-research.md` for primary-source detail. Not legal advice.)*

**Why this is critical:** we stack three risk classes at once — (1) AI-output risk (wrong/insecure/infringing code), (2) code-execution/hosting risk (we run arbitrary, often hostile, customer-directed code on shared infra), (3) autonomy risk (agents act and ship without per-action human review). A generic SaaS legal stack does not bound this.

## 3.1 Multi-tenant data isolation & liability model

- **microVM-per-tenant execution is the defensible default.** Plain containers are **not** a security boundary for hostile code (NIST SP 800-190; repeated 2024–25 runc/kernel escape CVEs). Use **Firecracker/Kata microVMs** per tenant execution (the E2B/AWS-Lambda/Fly.io approach; Modal uses gVisor; Cloudflare uses V8 isolates). This *also* gives us the §2.1 sandbox cost basis.
- Layer **Postgres RLS + per-tenant KMS keys** on pooled storage. Use `FORCE ROW LEVEL SECURITY`, non-owner roles, `SET LOCAL` in transactions (pooler leakage), and `tenant_id` as leading index column. Follow AWS **Bridge model**: pool stateless services, **silo anything that executes tenant code or holds tenant data**.
- **Liability map:** the isolation boundary is *our* liability; weaker isolation = larger all-tenant blast radius. **BYO-key is the one lever that genuinely shifts risk to the tenant** (they hold the key + a kill-switch).

## 3.2 Essential legal docs

- **ToS:** disclaim AI output **"AS IS"**, require human verification before relying/deploying, **cap liability at ~12 months' fees**, make the **customer indemnify us**, reserve the right to suspend for AUP breach.
- **AUP:** name the prohibited categories explicitly — **lead with crypto mining**, malware/malicious code, CSAM, security attacks, resource abuse, phishing/fraud site generation, plus IP infringement, spam, and illegal content. (Mirror Vercel/Cloudflare/AWS AUPs.)
- **DPA:** hit all of **GDPR Art 28(3)(a)–(h)**; list **named subprocessors** (regulated buyers disqualify vendors who won't name them); define data residency.
- **Responsible-AI policy:** mirror Anthropic's tiered structure; mandate **human-in-the-loop before autonomous deploy** to regulated contexts.
- **Shared-responsibility line (the Vercel model):** *we* own infra + compute isolation; *customer* owns code content, secrets, and deploy behavior.

## 3.3 Liability for what agents build/deploy + IP ownership

- **Template the Vercel disclaimer model** (cleanest zero-exposure): disclaim output, refuse to indemnify the customer for what agents generate, make them indemnify us.
- **IP:** purely AI-generated code is **almost certainly uncopyrightable in the US** (USCO Jan 2025 report; *Thaler*, cert denied 2026). **There is nothing to assign** — protect via **trade secret + contract**, not copyright assignment. Grant the customer broad ownership/use rights contractually; don't promise copyright we can't confer.
- **Copyright shields:** vendor shields (MS/Google/Anthropic/OpenAI) are paid-tier-only with carve-outs and require keeping filters on + not modifying output. We can only **pass one through** under those exact conditions; otherwise don't offer an indemnity we can't back.

## 3.4 Compliance (the enterprise sales gate)

- **SOC 2 Type II first** for US buyers — ~**$10K–150K** all-in year one, **6–15 month** timeline (3–12 mo observation window). **ISO 27001** for international/gov (65–75% control overlap).
- Build to **CPRA** as the strictest US baseline (20+ state privacy laws). **GDPR** for EU.
- **Data residency is a revenue gate** — 65% of CIOs have rejected a vendor over it; offer US/EU/APAC. Watch the **residency ≠ sovereignty** CLOUD Act gotcha.
- **HIPAA + signed BAA** is the hard wall for healthcare (Enterprise tier only). Common pattern: keep the builder *out of* PHI (frontend-only; separate HIPAA backend).

## 3.5 Abuse / fraud (code-execution-specific)

- **Indirect prompt injection is the #1 threat** for an autonomous codegen agent (real CVEs: Copilot RCE CVE-2025-53773; Claude Code Action token exfil). Avoid the "lethal trifecta" (untrusted input + secrets + exfil path); use architectural defenses + **HITL for deploy/secret actions**; pin CI to commit SHAs.
- **Denial-of-wallet:** usage-based pricing without a **HARD cap** is a financial liability — most vendor "budgets" are alerts, not caps. Enforce hard caps at the orchestrator (ties to §1.4/§2.5).
- **Signup friction:** card + $1 hold / phone verification to gate free tier; **name crypto mining explicitly in AUP**; monitor compute fingerprints (mining, port scans, mass outbound).
- **Outbound controls** on tenant sandboxes (egress filtering, rate limits) to prevent phishing/spam/attack staging from our IPs.

## 3.6 Content moderation (what tenants build & host on our infra)

- **Section 230** covers most tort exposure **but NOT** copyright, federal crimes, or sex trafficking.
- **Mandatory hygiene:** register a **DMCA agent** (3-yr renewal) + **repeat-infringer policy** (missing it forfeits safe harbor entirely).
- **EU DSA:** legal rep + **Art 16 notice-and-action** + **Art 17 statements of reasons**.
- **CSAM → NCMEC reporting** mandatory on actual knowledge (penalties to $1M).
- Practical: automated scanning of deployed content for known-bad categories + a takedown/abuse pipeline.

## 3b. KYC / payments compliance (Stripe Connect)

- **Only relevant if tenants take payments through apps we host.** Then:
  - **Stripe is NOT merchant of record — we or the connected accounts are.** We carry tax remittance + chargebacks + refunds + our own negative balance **regardless of Connect config**.
  - **SSA §1.2(a)(ix) "enable to benefit"** makes us responsible for **enforcing the prohibited-business list on our tenants** (KYC/AML on connected accounts via Stripe's onboarding/identity verification).
  - If the pitch is "no tax/dispute headache," only a **true MoR (Paddle/Lemon Squeezy)** offloads it — Stripe Connect does not.
- **Recommendation:** for v1, **do not** become a payments marketplace. Let tenants connect their *own* Stripe accounts to *their* deployed apps (they're MoR for their own revenue), and keep our billing relationship strictly to the platform subscription. Revisit Connect only with a clear, high-value marketplace case and counsel.

## 3.7 Risk-bounding cheat-sheet

| Risk | Primary lever |
|---|---|
| Cross-tenant breach | microVM-per-tenant + RLS + per-tenant KMS; offer BYOK |
| AI-output / deployed-code liability | Disclaim "AS IS"; customer verifies + indemnifies us; pass through vendor shield only if conditions met |
| IP exposure | Treat AI code as uncopyrightable; protect via trade secret/contract |
| Enterprise gate | SOC 2 Type II → ISO 27001; DPA + pen test + Trust Center |
| Compute/hosting abuse | Signup friction + HARD caps + name crypto mining + treat prompt injection as primary threat + egress controls |
| Illegal tenant content | DMCA agent + repeat-infringer policy + DSA + NCMEC |
| Payments / KYC | Don't be MoR in v1; tenants use their own Stripe; if Connect, enforce prohibited-business list + carry chargebacks |

---

# 4. INDUSTRY USE-CASES & PERSONAS

## 4.1 Market context

- Low-code dev tech market: **$44.5B (2026)** → **$58.2B (2029)**, 14.1% CAGR (Gartner). AI code-tools → **$26B by 2030** (27.1% CAGR, Grand View).
- **70%** of new enterprise apps will use low/no-code by 2026; **≥80%** of low-code users will be outside formal IT (Gartner). 84% of developers use/plan AI coding tools (Stack Overflow 2025).
- **The trust gap:** AI-accuracy trust fell to **29%** (2025); #1 frustration is "almost right, not quite" (66%); only 32.5% feel confident deploying vibe-coded apps for mission-critical use. **The 70/30 wall is the dominant churn driver.**
- Traction proof: Lovable $0→$100M ARR/8mo, $6.6B valuation; Cursor ~$2B ARR, $29.3B; Replit ~$253M ARR; Base44 acquired by Wix.
- *Caveat: standalone "vibe coding market size" dollar figures are unreliable (SEO research shops); treat the trend as real and large, the specific dollars as soft.*

## 4.2 Persona × Needs matrix

| Persona | Tech ability | WTP (real anchors) | Values most | Top churn/barrier | Must-haves |
|---|---|---|---|---|---|
| **Non-technical solo founder** | None–low | $20–25 norm; balks at $80–200 effective | Speed (idea→MVP in hrs), zero code, handled infra | 70/30 wall; unpredictable credit billing; security blind spots | Full-stack scaffolding, **predictable pricing**, auto-security guardrails, "get unstuck" help |
| **Technical indie hacker** | High | $20 base; $60–200 willingly (vs $5K/mo dev) | **Code ownership/control**, codebase-aware editing, no black box | Distrust of AI output; **vendor lock-in** | **Source export**, git/IDE integration, diff review, model choice, no lock-in |
| **Agency / dev shop** | Med–high | Projects $1.8–40K+; retainers $2–7.2K/mo | **White-label + reseller margin**, client handoff | AI builders **lack productized white-label** | **True white-label**, client workspaces/billing, reseller pricing, clean export |
| **SMB** | Low–none | $10–50/user/mo; churns on overages | Low price, speed, **measurable ROI** | Credit surprises, no staff to maintain, tool sprawl | Templates, Zapier-class integrations, **flat pricing**, simple maintenance |
| **Startup** | Bimodal | $20–100/mo pre-revenue; **export > price** | Speed-to-validate; portability for scale | Outgrows no-code in 6–12 mo; **non-exportable = Series A red flag** | **Real source export**, standard stack (React/Postgres), scalable backend, migration path |
| **Enterprise internal-tools team** | High (IT owns buy) | $144–240/user/yr → $36K+/yr floors | **Governance + deploy model**; passing security review | **Compliance paywall**; InfoSec blocks on residency; long procurement | SSO/SAML, **immutable audit logs**, RBAC, **VPC/on-prem**, SOC 2 Type II, named subprocessors |

**Price ladder:** SMB ($10–50/seat, hates surprises) → Startup ($20–100, export>price) → Enterprise ($144/user/yr–$36K+/yr, compliance gates over price).

## 4.3 Industry × Needs matrix

| Industry | What they build | Compliance gate | WTP | Key features | Top barrier |
|---|---|---|---|---|---|
| **E-commerce** | Storefronts, ops/inventory/CRM dashboards | PCI-DSS (mostly inherited from Shopify; v4.0 checkout-script liability) | High volume, lower per-tool | Shopify/Stripe/Twilio integrations, ops dashboards, transparent pricing | Lowest friction; checkout-script governance |
| **Fintech** | Internal ops: KYC/AML, loan approval, dispute consoles (never raw card data) | **SOC 2 + PCI** (keep builder out of CDE) | Material; pays for SSO add-ons | **Immutable audit logs**, SSO base tier, RBAC, residency, VPC, named subprocessors | Governance/docs; **won't-name-subprocessors = disqualified** |
| **Healthcare** | Internal admin, intake, scheduling, clinical-ops (PHI minimized) | **HIPAA + signed BAA** (hard wall) | Gated to Enterprise | **BAA on file**, encryption, audit logs, RBAC, VPC | **BAA availability** (Bubble/Glide won't sign; Retool Enterprise will). Pattern: builder never touches PHI |
| **Internal B2B tools** | Internal apps, workflows, dashboards, APIs (replacing bought SaaS) | SOC 2 Type II; HIPAA (Ent) | Strongest market; Retool ~$1.4K→$7.8K→custom | **50+ connectors**, SSO/RBAC/audit, self-host | SSO/RBAC/audit **paywalled**; security #2 barrier |
| **Marketing / agencies** | Landing pages, microsites, client sites | Light (GDPR/cookie/pixels) | Delivery $2–35K/project; $10–35/seat | **White-label portals + billing**, CMS, A/B, HubSpot/SF/GA4, SEO | Per-site cost at scale; **white-label is the wedge** |
| **Gaming** | Marketing/community sites, throwaway prototypes | Light (loot-box/PEGI, COPPA) | **Low** for horizontal | Templates, community tooling | **Thinnest niche**; engineering-heavy; LiveOps owned by vertical SaaS |
| **Education** | Registration/attendance admin, quiz/assessment, LMS adjuncts | **FERPA + COPPA + WCAG 2.1 AA/508** | **Most price-sensitive**; site/school licenses | DPA, **VPAT/accessibility**, SSO, SOC 2, audit | Privacy review #1 friction; no-code UI may **fail WCAG AA** |
| **Real estate / proptech** | IDX/listing sites, CRM, lead-gen, tenant portals | Fair-housing (follows brokerage); NAR AI/MLS standards | Real spend (24% of agents >$500/mo) | **Native MLS/IDX (RESO Web API)**, CRM sync, eSign, white-label | Generic no-code **lacks native MLS** → middleware; fair-housing liability |

**Compliance gate ranking (hardest first):** Education ≈ Healthcare > Fintech > Real Estate > Internal B2B > Marketing > Gaming. Education/Healthcare/Fintech can **disqualify a vendor outright** regardless of product quality.

**Universal regulated-buyer checklist:** immutable audit logs · SSO/SAML *in base enterprise tier* (paywalling = red flag) · granular RBAC · contractual residency (US/EU/APAC) · encryption at rest+transit · self-host/VPC · BAA (healthcare) · named subprocessors in DPA.

## 4.4 Go-to-market implications

1. **Beachheads:** **Internal B2B tools** (largest, most mature, proven ROI) + **E-commerce ops** (compliance inherited). Real estate attractive on spend but needs native MLS/IDX.
2. **Two GTM motions:** flat-price + guardrails for non-technical/SMB (high churn, low ACV) vs **code-export + no-lock-in** for technical/startup/agency (lower churn, higher value). Build on a **real exportable stack** to unlock the latter.
3. **White-label for agencies is open whitespace** — productize reseller workspaces + billing + handoff; AI builders are still affiliate-only.
4. **Regulated verticals = enterprise tier**, post-SOC2/SSO/VPC/BAA. **Put SSO/audit/RBAC in the base enterprise tier** (not a paywall upsell) to beat the universally-disliked "compliance paywall."
5. **Trust/verification is the moat** — the "CEO orchestrates agents" framing only works if agents close the loop (test, review, security-scan, explain). That directly attacks the 70/30 wall and the 29% trust number, which are the real churn drivers.

---

## Appendix: source map

- **Pricing:** vercel.com/pricing, v0.app/pricing, replit.com/pricing, lovable.dev/pricing + docs.lovable.dev, cursor.com/pricing, bolt.new/pricing, retool.com/pricing, supabase.com/pricing.
- **API/compute:** platform.claude.com/docs/pricing (Anthropic), openai.com/api/pricing, aws.amazon.com/fargate/pricing, e2b.dev/pricing, runpod.io/pricing, Spheron/Modal GPU trackers.
- **Stripe:** stripe.com/pricing, /connect/pricing, /billing/pricing; PYMNTS AI-usage billing.
- **Margins:** ICONIQ State of AI 2026 (52%), Bessemer State of AI 2025 (~65%), Tanay Jaipuria, The SaaS CFO.
- **Legal:** AWS SaaS Lens / Tenant Isolation whitepapers, NIST SP 800-190, USCO Jan 2025 AI report + Thaler, OWASP/CVE-2025-53773, Section 230 / DMCA / EU DSA / NCMEC, Stripe SSA §1.2(a)(ix). Full detail in `legal-compliance-risk-research.md`.
- **Market/personas:** Gartner low-code forecasts, Stack Overflow Dev Survey 2025, Bubble State of Visual Development 2025, Retool AI build-vs-buy 2026, vendor ARR via TechCrunch/CNBC/Sacra.
- *Soft-confidence flags: standalone "vibe coding market size" dollars; some vendor ARR figures (press-relayed); a few JS-rendered upper-tier pricing rows cross-checked via secondary sources.*
