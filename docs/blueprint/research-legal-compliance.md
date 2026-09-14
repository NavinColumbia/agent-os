# Legal, Compliance & Risk Frameworks for an Autonomous AI Codegen-and-Deploy SaaS Platform

**Scope:** A multi-tenant SaaS platform where external customers run *autonomous AI agents* that write **and deploy** code/apps on the platform's own infrastructure. This is a high-risk model because it stacks three risk classes at once:

1. **AI-output risk** — agents generate code that may be wrong, insecure, or infringing.
2. **Code-execution / hosting risk** — you run arbitrary, customer-directed (often hostile) code on shared infrastructure.
3. **Autonomy risk** — the agent acts and ships without per-action human review.

A generic SaaS legal/compliance stack does **not** bound this. Every section below is oriented to an operator trying to *bound* risk, with primary-source URLs throughout.

> **Not legal advice.** This is research synthesis. Have qualified counsel draft/review final contract language — especially given direct EU AI Act and DSA exposure for an autonomous-agent provider.

---

## Executive summary — the risk-bounding levers

| Risk | Primary lever | Where |
|---|---|---|
| Cross-tenant breach | microVM-per-tenant compute (not containers) + RLS + per-tenant KMS keys; offer BYOK to shift custody to tenant | §1 |
| AI-output / deployed-code liability | Disclaim AI output "AS IS", make tenant verify + indemnify you, do **not** indemnify tenant unless you can pass through an upstream model-vendor shield | §2, §3 |
| IP exposure | Treat purely AI-generated code as **uncopyrightable**; protect via trade secret/contract, not copyright assignment | §3 |
| Enterprise sales gate | SOC 2 Type II first (US), ISO 27001 for international; DPA + pen test + Trust Center | §4 |
| Compute/hosting abuse | Signup friction (card+$1 hold, phone), **hard** spend caps (not alerts), name crypto mining in AUP, treat indirect prompt injection as primary threat | §5 |
| Hosting illegal tenant content | DMCA agent + repeat-infringer policy, Section 230 limits, DSA notice-and-action + EU rep, mandatory NCMEC CSAM reporting | §6 |
| Payments / KYC | Pick Connect model deliberately; you are (or your tenants are) the **merchant of record**, not Stripe; you're always liable for chargebacks/refunds/your own negative balance | §7 |

---

## 1. Multi-Tenant Data Isolation Models & the Liability Map

### 1.1 Pool vs Silo vs Bridge (the AWS canonical models)

- **Silo** — tenants get *dedicated* resources (separate DB / full stack). Strongest isolation, smallest blast radius, highest cost. ([AWS SaaS Lens — Silo/Pool/Bridge](https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/silo-pool-and-bridge-models.html))
- **Pool** — tenants *share* scalable infrastructure. Cheapest/densest, but the **highest-risk** config: one app-layer or credential failure can expose **all** tenants at once.
- **Bridge** — mix per service: pool the cheap stateless services, silo anything that executes tenant code or holds tenant data. This is what most production SaaS actually does. ([AWS Tenant Isolation Strategies whitepaper](https://docs.aws.amazon.com/whitepapers/latest/saas-tenant-isolation-strategies/the-bridge-model.html); [PDF](https://docs.aws.amazon.com/pdfs/whitepapers/latest/saas-tenant-isolation-strategies/saas-tenant-isolation-strategies.pdf))

**Driver:** regulatory profile + noisy-neighbor → silo; cost/agility → pool.

### 1.2 Row-Level Security (Postgres RLS)

Acts as a DB-enforced automatic `WHERE tenant_id = current_setting('app.current_tenant')` — defends against the most common multi-tenant bug (a forgotten tenant filter). Secure-by-default: no context set → zero rows. ([AWS Database Blog — RLS](https://aws.amazon.com/blogs/database/multi-tenant-data-isolation-with-postgresql-row-level-security/))

**Failure modes that silently leak across tenants:**
- **Table-owner bypass** — owner ignores policies unless `FORCE ROW LEVEL SECURITY`; connect as a non-owner role.
- **Superuser / `BYPASSRLS`** roles ignore all policies.
- **Views/functions run as creator** — a view owned by a bypass role leaks everything.
- **Connection-pooler leakage** — session vars can carry one tenant's context into another's request via pgBouncer; use `SET LOCAL` in a transaction.
- **Performance cliff** without `tenant_id` as leading index column → tempts teams to disable it.
([AWS Database Blog](https://aws.amazon.com/blogs/database/multi-tenant-data-isolation-with-postgresql-row-level-security/); [Postgres RLS in Practice](https://queryplane.com/blog/postgres-row-level-security-in-practice/))

RLS is **defense-in-depth, not a sole boundary** — it's administered by your own DB roles and doesn't protect against a compromised privileged credential.

### 1.3 Per-tenant encryption / BYOK

Per-tenant keys "break the chain of shared fate" — a storage breach goes from *all tenants* to *one tenant*. ([WorkOS — cryptographic key isolation](https://workos.com/blog/cryptographic-key-isolation-multi-tenant-saas))

AWS-recommended cost-conscious pattern: **one KMS key per tenant** (not per service×tenant), isolated by IAM — per-tenant alias + `AssumeRole` session policy with `kms:RequestAlias` condition + tenant identity from JWT. Cost ~$1/key/month. ([AWS Architecture Blog, Aug 2025](https://aws.amazon.com/blogs/architecture/simplify-multi-tenant-encryption-with-a-cost-conscious-aws-kms-key-strategy/))

**BYOK / customer-managed keys** let the *tenant* hold the key in their own KMS and grant revocable use — the one lever that genuinely **shifts custody and a kill-switch to the tenant**. ([AWS Security Blog — BYOK](https://aws.amazon.com/blogs/security/demystifying-kms-keys-operations-bring-your-own-key-byok-custom-key-store-and-ciphertext-portability/)) Caveat: isolation is only as strong as the IAM scoping around `kms:Decrypt` — a broad app role that can decrypt any tenant undermines it.

### 1.4 Compute isolation for untrusted code — the central question

**Plain containers are NOT a security boundary** for hostile multi-tenant code — they share one host kernel (~40M LoC, 450+ syscalls). Recent production container escapes: CVE-2024-21626 (Leaky Vessels/runc), CVE-2025-23266 (NVIDIAScape, CVSS 9.0), CVE-2025-31133/52565 (runc). ([Your Container Is Not a Sandbox, 2026](https://emirb.github.io/blog/microvm-2026/); [NIST SP 800-190](https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-190.pdf)) For *autonomous agents* specifically, the argument is sharper: soft userspace controls live "in the same space the agent reasons in," whereas a microVM boundary is enforced by hardware below that layer.

| Tech | Boundary | Strength | Used by |
|---|---|---|---|
| Plain container | Shared host kernel (software) | Weakest — not a trust boundary | trusted code only |
| **gVisor** | userspace Sentry kernel (~53–68 host syscalls) | software, smaller surface; fast, good GPU | **Modal** |
| **Firecracker microVM** | hardware virt, separate guest kernel | strongest practical; escape needs rare hypervisor CVE | **AWS Lambda, E2B, Fly.io** |
| Kata Containers | VM per pod | VM-grade | K8s drop-in |
| V8 isolates | in-process software | density-first; defended by depth | **Cloudflare Workers** |

- **AWS Lambda** — Firecracker microVM per invocation + `jailer` second barrier. ([AWS](https://aws.amazon.com/blogs/aws/run-isolated-sandboxes-with-full-lifecycle-control-aws-lambda-introduces-microvms/))
- **E2B / Fly.io** — Firecracker microVMs. **Modal** — gVisor. ([Northflank: E2B vs Modal vs Fly.io](https://northflank.com/blog/e2b-vs-modal-vs-fly-io-sprites))
- **Cloudflare Workers** — V8 isolates (software boundary); "V8 itself cannot defend against Spectre," so they layer V8 sandbox + memory-protection keys + trust "cordons" (free never shares a process with enterprise). Residual: a V8 JIT bug "can collapse the software isolation boundary entirely." ([Cloudflare Workers security model](https://blog.cloudflare.com/mitigating-spectre-and-other-security-threats-the-cloudflare-workers-security-model/); [Safe in the sandbox](https://blog.cloudflare.com/safe-in-the-sandbox-security-hardening-for-cloudflare-workers/))

**Default for this platform:** **Firecracker/Kata microVM per tenant execution.** gVisor is an acceptable middle ground (GPU/fast-start). Plain containers are out of scope as a trust boundary for hostile code.

### 1.5 How isolation maps to liability

The **isolation boundary is the platform's responsibility** — so a cross-tenant leak from weak isolation lands on *you*, not the tenant. ([Frontegg — SaaS multitenancy](https://frontegg.com/blog/saas-multitenancy)) "A single application-level vulnerability, a compromised set of privileged credentials, or a malicious database administrator can result in a catastrophic breach, exposing the sensitive data of all tenants simultaneously." ([Complydog](https://complydog.com/blog/multi-tenant-saas-privacy-data-isolation-compliance-architecture))

| Choice | Blast radius | Who absorbs it |
|---|---|---|
| Pool compute (containers/shared isolate) | All tenants | Platform |
| Pool storage + RLS only | All tenants if privileged role/pooler bug | Platform |
| Per-tenant encryption (provider keys) | One tenant | Platform, but contained |
| **BYOK** | One tenant; tenant can revoke | **Shifts to tenant** |
| Silo / microVM / separate account | One tenant | Platform, contained |

**Bottom line:** every step toward silo/hardware-isolation/per-tenant-keys shrinks the worst case from "all tenants" to "one tenant," and **BYOK is the only lever that moves real risk onto the tenant** (hence it's an enterprise upsell).

---

## 2. Essential Legal Documents

### 2.1 Terms of Service — must-haves

([toslawyer.com — ToS for AI Products 2026](https://toslawyer.com/terms-of-service-for-ai-products-what-your-agreement-must-include-in-2026/); [Mayer Brown — contracting for agentic AI](https://www.mayerbrown.com/en/insights/publications/2026/02/contracting-for-agentic-ai-solutions-shifting-the-model-from-saas-to-services))

- **Warranty disclaimer for AI output** — "AS-IS, WITH ALL FAULTS"; *"AI-generated outputs are probabilistic and not guaranteed to be accurate, complete, or error-free."* Extend explicitly to generated code being insecure, non-functional, or infringing.
- **Human-verification requirement** — customer is responsible for reviewing/verifying output before relying on it; this is where you shift responsibility for what the agent ships.
- **Limitation of liability** — exclude consequential/indirect damages, cap at trailing 12 months' fees (industry standard across major AI providers).
- **Customer indemnification** — customer indemnifies you for claims arising from use/distribution/publication of their application outputs (the deployed code).
- **Termination/suspension at sole discretion** — Replit model: suspend/terminate for violations *"or any other actions that Replit deems as detrimental to the platform or its users."* Add immediate-suspension for resource abuse. ([Replit ToS](https://replit.com/terms-of-service))
- **Service-level disclaimers** — no guaranteed uptime outside a separate SLA. **Duplicate key AI disclaimers at the point of use**, not only buried in ToS (2025–26 enforcement weights point-of-use disclosures more heavily).
- **Age/eligibility** — 18+/age of majority + authority to bind entity.
- **AI-specific** — IP ownership of output (you **cannot warrant copyrightability** — see §3), training opt-out, third-party model-provider disclosure, regulatory-modification clause (~30 days' notice).

### 2.2 Acceptable Use Policy — the prohibited categories that matter

Real-platform sources: [Vercel AUP](https://vercel.com/legal/acceptable-use-policy) · [AWS AUP](https://aws.amazon.com/aup/) · [Cloudflare Website Terms](https://www.cloudflare.com/website-terms/) · [Replit ToS](https://replit.com/terms-of-service)

Consolidated **must-have** categories for a code-execution/hosting platform:

1. **Crypto/cryptocurrency mining** — *"Mining Bitcoin, other cryptocurrencies, or Cycles is prohibited"* (Replit) — most on-point for compute abuse.
2. **Malware / virus / Trojan** creation, hosting, distribution (all four platforms).
3. **Phishing & fraud** (all four).
4. **CSAM & illegal content** (all four — non-negotiable).
5. **Security violations** — unauthorized access, attacks on systems (AWS, Cloudflare, Vercel).
6. **Network abuse** — DDoS, undue burden, proxy/VPN, hot-linking (Vercel, Cloudflare).
7. **Resource/compute abuse** — circumventing rate limits, excessive consumption, spam bots (Vercel, Replit).
8. **Spam / unsolicited mass messaging** (all four).
9. **Scraping** (Cloudflare, Vercel).
10. **EU AI Act high-risk uses** — Vercel flatly prohibits *"any 'high risk' areas under the EU AI Act"* (facial recognition, biometric inference, clinical practice, weapons).
11. **Competing-model training / model-extraction attacks** (Vercel).

For an autonomous-agent platform, **add an explicit prohibition on directing agents to perform any of the above, and on using agents to attack/probe third-party systems.**

### 2.3 Data Processing Agreement — GDPR Art 28(3) checklist

([GDPR Art 28](https://gdpr-info.eu/art-28-gdpr/); real reference: [Vercel DPA](https://vercel.com/legal/dpa))

- (a) Process **only on documented instructions**.
- (b) **Confidentiality** commitments from authorized persons.
- (c) **Security (TOMs)** per Art 32 — e.g., AES-256 at rest, TLS 1.2+ in transit, pseudonymization (Vercel Schedule 2).
- (d) **Subprocessor controls** — prior authorization + flow-down; publish a **subprocessor list** (for you: the LLM provider, cloud host, storage/vector vendors) with change notice.
- (e) **Data-subject-rights assistance** (access/rectification/erasure).
- (f) **Compliance assistance** — security, breach notification, DPIAs.
- (g) **Deletion/return** of all personal data at termination.
- (h) **Audit rights**.
- **Transfers** — incorporate EU **SCCs** + UK Addendum/IDTA (your LLM subprocessor likely processes cross-border).
- **AI-specific** — fix controller/processor roles; default **"no training on customer data"**; flow LLM-provider terms through as a subprocessor obligation.

### 2.4 Responsible AI / Acceptable AI Use Policy

Template: [Anthropic Usage Policy](https://www.anthropic.com/legal/aup) — three tiers (Universal Standards / High-Risk Requirements / Use-Case Guidelines incl. **agents**).

- **Prohibited AI uses** — illegal activity, critical-infrastructure compromise, *"Unauthorized computer/network system access,"* weapons, CSAM/child safety, fraud/forgery, *"Platform abuse and jailbreaking attempts."* (The unauthorized-access and jailbreaking items are load-bearing for an autonomous codegen agent.)
- **Human oversight for high-risk domains** — Anthropic requires a *"qualified professional… review the content or decision prior to dissemination"* for legal/health/finance/employment/housing. Translate to: **agents must not autonomously deploy into regulated/high-risk production without qualified human review.**
- **Transparency** — disclose AI use at session start; make AI-authored code identifiable where it matters.
- **Agentic safeguards** — human-in-the-loop checkpoints for irreversible/destructive actions, scope limits on agent access/deploy, kill-switch, output disclaimers.
- **EU AI Act** — prohibit high-risk categories; note you may face **direct** AI Act obligations as an agent provider, not just pass-through.
- **Substantiation of on-device / "no-data-leaves-device" claims** — any product-, marketing-, or store-listing claim that data is processed **on-device** or that **no data leaves the device** must be backed by evidence *before publication* (FTC requires a reasonable basis for objective claims; unsubstantiated privacy claims draw enforcement, e.g. the FTC's actions on deceptive AI/privacy representations). Required proof: the actual data-flow diagram showing no personal data egress, the specific technical basis (e.g. Apple Foundation Models / Core ML on-device exemption where the model runs locally — see [[RESEARCH-launch-onboarding]] §disclosure exemptions), and a named owner who signs off. **Where our runtime executes server-side, an unqualified "on-device" claim is false and must not ship** — qualify it precisely (which processing is local vs server-side) or drop it. No such claim ships without a linked substantiation record and compliance sign-off.

### 2.5 Shared-responsibility framing

- **AWS** — *"security OF the cloud"* (AWS owns infra) vs *"security IN the cloud"* (customer owns guest OS, app, config, data, IAM); the split **varies by service abstraction level**. ([AWS Shared Responsibility Model](https://aws.amazon.com/compliance/shared-responsibility-model/))
- **Vercel** — the most directly applicable model: Customer / Shared / Vercel buckets. Vercel owns *"a compute environment… to ensure the secure execution of customer code… isolate customer applications"*; **customer** owns *"Source Code"* and costs of *"Malicious Traffic"* + spend caps; **shared** covers *"User Code & Environment Variables"* and incident response. ([Vercel Shared Responsibility](https://vercel.com/docs/security/shared-responsibility))

**Draw your line:** *you own infrastructure + compute isolation; the customer owns code content, secrets/env vars, and the behavior of what the agent deploys.* Publish a Vercel-style responsibility matrix in a Trust Center.

### 2.6 App Store Privacy Labeling Requirements

Where a tenant app (or our own first-party app) ships through **Apple's App Store** or **Google Play**, the store-mandated privacy disclosures are a **pre-publication gate**, not a post-launch formality. Both stores make the developer *attest* to the app's data practices, and both treat an inaccurate declaration as a policy violation and grounds for rejection or removal. This obligation is distinct from, and additive to, the on-device claim substantiation in §2.4.

- **Apple App Store Privacy "Nutrition" Labels** — the **App Privacy Details** declared in App Store Connect: for each data type, whether it is **Collected**, whether it is **Linked** to the user's identity, and whether it is **Used to Track**. Any data used for tracking additionally requires an **App Tracking Transparency (ATT)** consent prompt. ([Apple — App Privacy Details](https://developer.apple.com/app-store/app-privacy-details/); [Apple — App Tracking Transparency](https://developer.apple.com/documentation/apptrackingtransparency)) See also [[RESEARCH-launch-onboarding]] §1.1 (nutrition labels + Guideline 5.1.2(i) AI-provider disclosure).
- **Google Play Data Safety declarations** — the **Data safety** section in Play Console (mandatory even for a zero-data app): per data category, disclose whether data is **Collected** and/or **Shared**, the **purpose**, whether collection is **optional**, encryption-in-transit, and the user's ability to request deletion. ([Google — Provide information for Google Play's Data safety section](https://support.google.com/googleplay/android-developer/answer/10787469)); see [[RESEARCH-launch-onboarding]] §1.2.

- **Required disclosure (both stores).** Enumerate **every** data-collection and tracking category actually exercised by the shipped build **and its bundled SDKs/subprocessors** — including any data our runtime or the LLM subprocessor collects server-side on the app's behalf (cross-reference the subprocessor list in §2.3). A category triggered by an embedded SDK is the developer's to declare; "the SDK collected it" is **not** a defense.
- **Accuracy / consistency with actual behavior.** The declared label MUST match the app's observed runtime data flows. An **under-declared** label is a **misrepresentation** — independently actionable by the FTC as a deceptive privacy claim (same reasonable-basis standard as §2.4) *in addition to* store enforcement. Before every submission, reconcile the declared label against the app's actual data-flow diagram; any discrepancy blocks release.
- **Update cadence.** Re-verify and, where needed, re-file the labels **on every data-practice change** — a new SDK, a newly collected/shared category, a new purpose, or changed tracking behavior — *before* the changed build ships (this is the release-gate check in [[RESEARCH-launch-onboarding]]'s per-release privacy checklist). Absent a triggering change, run a **fixed quarterly review** so labels do not silently drift out of date.
- **Named owner (accountability).** A single **Privacy Label Owner** — the compliance sign-off role established in §2.4 — is accountable for keeping every store label current and consistent with actual behavior. That owner maintains the mapping of *app → declared categories → substantiation record*, and gives written sign-off on the label at each submission and at each quarterly review. No build ships to either store without that sign-off on record.

---

## 3. Liability Allocation & IP of AI-Generated Code

### 3.1 Who's liable for what agents generate/deploy — the universal pattern

Platforms disclaim all warranties ("AS IS"), warrant nothing about non-infringement, push responsibility for output onto the user, and make the **user indemnify the platform**. Indemnity *toward* the customer is the exception.

- **GitHub Copilot** — *"GitHub does not own Suggestions. You retain ownership of Your Code"*; *"you are solely responsible for any application or agent you create using… Generative AI Services."* ([Copilot PST](https://github.com/customer-terms/github-copilot-product-specific-terms); [GenAI Services Terms, eff. Mar 2026](https://github.com/customer-terms/github-generative-ai-services-terms)) GitHub is the **one vendor that defends the customer** on IP (via Microsoft CCC, paid tiers only).
- **Replit** — *"Code generated or suggested by our AI systems may be erroneous or incomplete"*; service "AS IS"; *"You agree to indemnify and hold Replit harmless."* ([Replit ToS](https://replit.com/terms-of-service))
- **Vercel** — *"You are responsible for evaluating and monitoring the actions and output of the AI Functionality… Vercel is not responsible…"*; AI terms are "AS IS" with no non-infringement warranty; and crucially *"Vercel does not provide any indemnity to you for… the AI Products and Services"* while you must *"defend, indemnify and hold harmless Vercel."* ([Vercel ToS](https://vercel.com/legal/terms); [AI Product Terms](https://vercel.com/legal/ai-product-terms)) **Vercel is the cleanest zero-IP-exposure model.**

### 3.2 Copyright status of AI-generated code (US, 2025–2026)

**Purely AI-generated code is almost certainly uncopyrightable — nothing to own, assign, or license; a competitor could copy it without infringing.**

- **US Copyright Office, "Copyright and AI Part 2: Copyrightability" (Jan 29, 2025)** — *"Copyright does not extend to purely AI-generated material… prompts do not alone provide sufficient control… Prompts essentially function as instructions that convey unprotectible ideas."* Protection attaches only to human-authored expression or creative selection/arrangement/modification of outputs — never the raw AI material. ([USCO Part 2 PDF](https://www.copyright.gov/ai/Copyright-and-Artificial-Intelligence-Part-2-Copyrightability-Report.pdf))
- **Thaler v. Perlmutter (D.C. Cir., Mar 18, 2025)** — the Copyright Act *"requires all eligible work to be authored in the first instance by a human being."* SCOTUS denied cert (2026), leaving it intact. ([opinion PDF](https://media.cadc.uscourts.gov/opinions/docs/2025/03/23-5233.pdf))
- **Zarya of the Dawn (USCO, 2023)** — Midjourney images unprotectable; human prompt + selection/arrangement protected, AI images not. ([PDF](https://www.copyright.gov/docs/zarya-of-the-dawn.pdf))

**Consequences for code:** nothing to assign/license; effectively public-domain-like ("all the liability, none of the protection"). Copyright **can** re-attach where a human substantially edits/selects/arranges — but only over the human contribution. **Protect what you must keep via trade secret + contract (NDA/confidentiality) + patent, not copyright assignment.** ([Foley](https://www.foley.com/insights/publications/2025/02/clarifying-copyrightability-ai-assisted-works/))

### 3.3 Vendor "copyright shields" — scope & common carve-outs

All four converge on: **paid/enterprise tiers only**, **no knowing infringement**, **must keep safety/citation filters on**, **unmodified output only**, **trademark-in-commerce excluded**.

- **Microsoft Customer Copyright Commitment** (Copilot, M365, Azure OpenAI) — defends + pays adverse judgments/settlements; requires built-in content filters on. For GitHub Copilot, **as of Apr 3, 2026 there are no additional required mitigations** (Duplicate Detection now optional). ([MS CCC](https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/openai/customer-copyright-commitment))
- **Google Cloud** — two-pronged: covers both **training-data** and **generated-output** infringement; voids on knowing infringement, ignoring citations/filters, or use after notice. ([Google Cloud](https://cloud.google.com/blog/products/ai-machine-learning/protecting-customers-with-generative-ai-indemnification))
- **Anthropic** — defends paid customers against claims that *"paid use of the Services… or Outputs… violates any third-party IP right"* (incl. training data); carve-outs for modifications, combinations, customer inputs, knowing violation, patent practice, trademark-in-commerce. ([Anthropic Commercial Terms](https://www.anthropic.com/legal/commercial-terms))
- **OpenAI Copyright Shield** — Enterprise + API only; excludes free/Plus; same carve-out family. ([OpenAI Service Terms](https://openai.com/policies/service-terms/))

### 3.4 How to allocate liability for code a tenant's agent generates **and deploys**

Market-standard structure (Vercel is the template):

1. **Disclaim AI output "AS IS,"** no non-infringement warranty — explicitly cover *agent actions and deployments*.
2. **Assign output to the tenant + place full responsibility on them** — frame as assignment of "any rights Vercel may have" (since there may be no copyright to assign) plus a responsibility allocation.
3. **Tenant indemnifies the platform** for third-party IP/regulatory claims from their agent's output and deployments — *the single most important lever for a deploy-capable platform.*
4. **Expressly state you do NOT indemnify the tenant** for AI output — unless you offer a shield as a paid differentiator.
5. **If you offer a shield**, copy the vendor carve-outs and only back-to-back it with your upstream model vendor's indemnity so the obligation flows through.
6. **MoR-style structural separation** — position as infrastructure/runtime provider; tenant is deployer-of-record; reinforce with "we don't review your code" disclaimers and takedown/suspension rights for unlawful deployments.

---

## 4. Enterprise Compliance: SOC 2, ISO 27001, GDPR, CCPA, Residency

### 4.1 SOC 2 Type II

- **What** — AICPA attestation (not pass/fail) by a CPA firm against the **Trust Services Criteria**: Security (mandatory) + Availability, Confidentiality, Processing Integrity, Privacy (optional). The default US enterprise trust artifact. ([Drata](https://drata.com/learn/soc-2/type-1-vs-type-2))
- **Type I vs II** — Type I = controls *designed* at a point in time; **Type II = designed AND operating effectively over an observation window** (what enterprises want).
- **Observation window** — 3 months (minimum), 6 months (recommended first-timer), 12 months (renewal standard).
- **Cost** — audit fee alone: Type II ~**$12k–$100k+** (SMB band $20k–$50k). All-in first-year program (readiness, risk assessment, pen test, prep, audit, maintenance): **$80k–$350k**; most smaller companies land **$10k–$150k**. Automation platform (Vanta/Drata/Secureframe) ~**$10k–$50k/yr**. ([Secureframe](https://secureframe.com/hub/soc-2/audit-cost))
- **Timeline** — Type I 3–6 months; **Type II 6–15 months** (1–3mo prep + 3–12mo window + ~2–5wk fieldwork + reporting).
- A **pen test within the audit period** is effectively expected for any real cloud infra. ([Blaze Infosec](https://www.blazeinfosec.com/post/soc-2-penetration-testing-requirements/))

### 4.2 ISO 27001:2022

- A genuine **certification** of an Information Security Management System (93 controls / 4 themes); certificate valid **3 years** with annual surveillance. ([secure.com](https://www.secure.com/blog/compliance/soc-2-vs-iso-27001))
- **~65–75% control overlap with SOC 2** → combined engagement saves ~20–30%. ([atlantsecurity](https://atlantsecurity.com/learn/iso-27001-vs-soc-2))
- **Cost** — small org $15k–$50k; medium $50k–$150k; large $150k–$500k+ (audits ~1.5–2× SOC 2). **Timeline** 6–10 months (up to 12–18 for complex). ([axipro](https://axipro.co/iso-27001-certification-cost/))
- **Rule:** US-only customers → SOC 2 first; international/government → ISO 27001.

### 4.3 GDPR (you're usually a processor, Art 28)

- Process only on controller's documented instructions; **signed DPA mandatory**; subprocessor list with locations.
- **Breach notification** — controller notifies authority within **72h**; processor must notify controller *"without undue delay"* (aim <24h). Non-notification fineable to €10M/2% turnover. ([gdpr-info Art 33](https://gdpr-info.eu/art-33-gdpr/))
- **Transfers** — adequacy / **SCCs** (+ TIA) / BCRs. **EU–US Data Privacy Framework survived legal challenge (EU General Court, Sept 3, 2025)** — currently valid (watch PCLOB quorum issue). Enforcement is real: **Uber fined €290M (Jan 2025)** for invalid US transfers. ([EU Commission](https://commission.europa.eu/law/law-topic/data-protection/international-dimension-data-protection/eu-us-data-transfers_en); [Epstein Becker Green](https://www.workforcebulletin.com/adequacy-of-the-eu-u-s-data-privacy-framework-survives-challenge))
- **EU AI Act timeline** (layers on top of GDPR): prohibited practices + AI literacy **in force Feb 2, 2025**; **GPAI model obligations in force Aug 2, 2025** (fines to €35M/7%); most high-risk requirements Aug 2, 2026 — but the **"Digital Omnibus" (May 2026) defers Annex III high-risk to Dec 2, 2027**. ([AI Act timeline](https://artificialintelligenceact.eu/implementation-timeline/); [DLA Piper](https://www.dlapiper.com/en-us/insights/publications/2025/08/latest-wave-of-obligations-under-the-eu-ai-act-take-effect))

### 4.4 CCPA/CPRA + US state patchwork

- **Thresholds** (any one): >~$26.6M revenue, OR 100k+ CA consumers/households/yr, OR ≥50% revenue from selling/sharing. The **100k-consumer threshold** is what most SaaS triggers. ([Secure Privacy](https://secureprivacy.ai/blog/ccpa-requirements-2026-complete-compliance-guide))
- **New for 2026** (CPPA regs effective Jan 1, 2026): **ADMT** notice + opt-out for automated decisions; pre-use **risk assessments**; **annual independent cybersecurity audits** for larger processors. ([Hinshaw](https://www.hinshawlaw.com/en/insights/privacy-cyber-and-ai-decoded-alert/2026-privacy-compliance-california-and-colorado-regulations))
- **20+ states** now have comprehensive privacy laws. **Strategy: build to California CPRA as the strictest baseline**, then layer state configs. ([njbusiness-attorney](https://www.njbusiness-attorney.com/us-state-privacy-laws-2026-saas-compliance-map/))

### 4.5 Data residency

- **A revenue gate:** a 2025 CIO survey found **65% rejected a preferred SaaS vendor that couldn't meet data-residency requirements.** ([Secure Privacy](https://secureprivacy.ai/blog/data-residency-requirements-eu-vs-us-explained))
- Big clouds offer EU-region deployments; modern pattern is **"selective residency"** (content + increasingly AI inference in-region; some control plane global). OpenAI's EU data residency is the canonical example. ([WorkOS](https://workos.com/blog/data-residency-for-enterprise-saas))
- **Residency ≠ sovereignty gotcha:** a US-incorporated provider is subject to the **US CLOUD Act even when data sits in Frankfurt** — so residency alone won't satisfy sovereignty-focused (often public-sector) buyers. ([StoneFly](https://stonefly.com/blog/data-sovereignty-vs-data-residency-compliance-guide/))

### 4.6 What enterprise buyers demand in vendor reviews

SOC 2 Type II report (they read the exceptions) or ISO 27001 cert · recent **pen test** · signed **DPA** + subprocessor list · completed **security questionnaire** (**CAIQ** for cloud, **SIG Lite ~150Q / SIG Full 1,000+Q** for financials) · GDPR/privacy program evidence. Reassessment: high-risk vendors annually + event-based. A **Trust Center** (self-serve SOC 2 / pen test / DPA / subprocessor list) shortens cycles. ([NMS Consulting](https://nmsconsulting.com/vendor-risk-management-checklist/))

**Suggested sequencing:** automation platform early → SOC 2 Type II first (US) → GDPR processor basics in parallel → CPRA baseline → ISO 27001 for international → EU residency when demand appears → Trust Center.

---

## 5. Abuse / Fraud Vectors on a Code-Execution Platform

### 5.1 Cryptojacking / crypto miners (free-compute & CI/CD abuse)

- **Sysdig PURPLEURCHIN** — freejacking across 30+ GitHub, 2,000 Heroku, 900 Buddy accounts; mining a single Monero cost the *provider* **>$100,000** while the actor netted ~$137 (cost fully externalized); 3–5 GitHub accounts/min, defeating CAPTCHAs. ([Sysdig](https://www.sysdig.com/blog/massive-cryptomining-operation-github-actions))
- **GitHub Actions vector** — fork public repo → add workflow → open PR → miner runs on GitHub runners with no merge. ([BleepingComputer](https://www.bleepingcomputer.com/news/security/github-actions-being-actively-abused-to-mine-cryptocurrency-on-github-servers/))
- **Three counters:** (1) **financial/identity friction** — GitLab requires a card verified by a **$1 auth hold** ([GitLab](https://about.gitlab.com/blog/prevent-crypto-mining-abuse/)); (2) **PR-approval gate** for first-time contributors (GitHub's fix); (3) **quota reduction / removing free tiers** — **Heroku killed all free plans (Nov 2022) citing "an extraordinary amount of effort to manage fraud and abuse"** ([TechCrunch](https://techcrunch.com/2022/08/25/heroku-announces-plans-to-eliminate-free-plans-blaming-fraud-and-abuse/)); Azure Pipelines removed free parallelism for new public projects.
- **Detection** — sustained CPU/GPU + **stratum protocol** traffic to mining pools (now obfuscated); Intel TDT + Defender; ML (~99% precision). Cloud cryptojacking has run victims **>$300,000** in compute. ([Microsoft](https://www.microsoft.com/en-us/security/blog/2023/07/25/cryptojacking-understanding-and-defending-against-cloud-compute-resource-abuse/))

### 5.2 Malicious code hosting / malware ("Living Off Trusted Sites")

Attackers use your trusted domain for phishing/C2/exfil because of inherited reputation + default TLS + free signup. ([LOTS project](https://lots-project.com/)) Documented: **Stargazers Ghost Network** (3,000+ accounts as malware DaaS) ([Check Point](https://research.checkpoint.com/2024/stargazers-ghost-network/)); **tj-actions/changed-files supply-chain (CVE-2025-30066)** dumped CI secrets across 23,000+ repos ([Wiz](https://www.wiz.io/blog/github-action-tj-actions-changed-files-supply-chain-attack-cve-2025-30066); [CISA](https://www.cisa.gov/news-events/alerts/2025/03/18/supply-chain-compromise-third-party-tj-actionschanged-files-cve-2025-30066-and-reviewdogaction)); **TryCloudflare tunnels** delivering RATs ([Proofpoint](https://www.proofpoint.com/us/blog/threat-insight/threat-actor-abuses-cloudflare-tunnels-deliver-rats)). Defenses: **AWS GuardDuty Malware Protection for S3** ([AWS](https://aws.amazon.com/blogs/aws/introducing-amazon-guardduty-malware-protection-for-amazon-s3/)); **Replit Package Firewall** blocks ~8,000 malicious npm/pip packages/day ([Replit](https://replit.com/blog/package-firewall)).

### 5.3 Prompt injection against the agent — the **primary** threat for autonomous codegen

- **OWASP LLM01:2025 Prompt Injection** (ranked #1) — direct, **indirect** (from websites/files the agent reads), and multimodal. Mitigations: constrain behavior, validate I/O, **least privilege**, **human approval for high-risk actions**, segregate untrusted content, adversarial testing. ([OWASP LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/))
- **Documented against coding agents:** **CVE-2025-53773 — GitHub Copilot RCE** via injected instructions flipping `chat.tools.autoApprove: true` ([embracethered](https://embracethered.com/blog/posts/2025/github-copilot-remote-code-execution-via-prompt-injection/)); **Claude Code GitHub Action** indirect injection via PR titles/issue bodies to exfiltrate OIDC/API tokens — fixed v1.0.94+, **pin to commit SHAs** ([CSA](https://labs.cloudsecurityalliance.org/research/csa-research-note-claude-code-github-action-prompt-injection/)).
- **The "lethal trifecta"** (Willison) — danger = private-data access + untrusted content + exfiltration ability **in one agent**; don't combine all three. ([Willison](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/))
- **Architectural defenses** (more robust than detection): Dual-LLM, CaMeL, plan-then-execute, least-privilege tools, egress allowlists, **human-in-the-loop for deploy and secret-touching actions**. Microsoft "spotlighting" cut attack success >50%→<2%. ([Willison — design patterns](https://simonwillison.net/2025/Jun/13/prompt-injection-design-patterns/); [Microsoft Research](https://www.microsoft.com/en-us/research/publication/defending-against-indirect-prompt-injection-attacks-with-spotlighting/))

### 5.4 Resource abuse / Denial-of-Wallet — **usage-based pricing without a hard cap is the liability**

DoW = mass invocation to drain the budget while service stays *available*. ([arXiv](https://arxiv.org/pdf/2104.08031)) Real surprise bills: **empty S3 bucket → $1,300+** from 100M unauthorized PUTs (AWS billed the 403s — since fixed: no charge for unauthorized 403s as of May 2024) ([Pocwierz](https://medium.com/@maciej.pocwierz/how-an-empty-s3-bucket-can-make-your-aws-bill-explode-934a383cb8b1); [AWS](https://aws.amazon.com/about-aws/whats-new/2024/05/amazon-s3-no-charge-http-error-codes/)); **Netlify ~$104,500** in 4 days; **Vercel $20→$23,000** from a DDoS; **Firebase ~$70,000/day**.

**Critical caveat: most vendor "budgets" are ALERTS, not caps.** Design hard stops:
- **AWS Budgets** can enforce via IAM/SCP budget actions; **Lambda reserved concurrency** caps blast radius. ([AWS](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-controls.html))
- **GCP/Firebase budgets do NOT cap spend** — hard cap is DIY (budget→Pub/Sub→disable billing). ([GCP](https://cloud.google.com/billing/docs/how-to/budgets))
- **Vercel** spend management "does not automatically stop usage" unless "Pause production deployment" is enabled. ([Vercel](https://vercel.com/docs/spend-management))
- **Cloudflare** structurally excludes DDoS traffic from billing. ([Cloudflare](https://developers.cloudflare.com/ddos-protection/about/))
- **Stripe Radar** predicts free-trial abuse "with 90% accuracy"; a valid card "ties that trial to a real financial identity." ([Stripe](https://docs.stripe.com/radar/free-trial-abuse))

### 5.5 Phishing site generation & hosting

Trusted subdomains (`*.pages.dev`, `*.vercel.app`, `*.netlify.app`, `*.web.app`) are trivially abused. **AI-generated phishing (2025–26):** Cofense documented **Vercel v0.dev weaponized** to mass-produce phishing login pages with Telegram exfil, GenAI regeneration defeating takedowns ([Cofense](https://cofense.com/blog/steal-smarter-not-harder-malicious-use-of-vercel-for-credential-phishing)). **Whack-a-mole problem:** most phishing damage occurs in the first minutes a site is live, while takedowns can take up to 72h. **Cloudflare's bar:** phishing auto-resolution went 37% (median 3.4 days) → **78%, median <1 hour**. ([Cloudflare](https://blog.cloudflare.com/how-cloudflare-is-using-automation-to-tackle-phishing/))

### 5.6 How the platforms detect & prevent abuse (comparison)

| | Vercel | Netlify | Cloudflare | AWS | Replit |
|---|---|---|---|---|---|
| Crypto mining named-banned | No | No | No | **Yes** | **Yes** |
| Proactive scanning | Yes (BotID) | Weak | **Strongest** (phishing ML, CSAM hash, AV) | GuardDuty (opt-in) | **Strong** (package firewall) |
| Signup gating | Email + payment | Email + payment | Disposable-email risk | **Phone + card + $1 hold** | Email only |
| Enforcement window | On notice | 48-hr grace | Sole discretion | **24-hr** | Sole discretion |

Sources: [Vercel transparency](https://vercel.com/legal/transparency) · [Netlify AUP](https://www.netlify.com/legal/acceptable-use-policy/) · [Cloudflare abuse approach](https://www.cloudflare.com/trust-hub/abuse-approach/) · [AWS registration FAQs](https://aws.amazon.com/free/registration-faqs/) · [Replit usage](https://docs.replit.com/legal-and-security-info/usage)

---

## 6. Content Moderation Obligations for Tenant-Deployed Content

### 6.1 US — Section 230 (47 U.S.C. § 230)

- **Protects:** you're generally not the "publisher/speaker" of tenant content (§230(c)(1)); and you can moderate/remove freely without losing immunity ("Good Samaritan," §230(c)(2)). ([Cornell LII](https://www.law.cornell.edu/uscode/text/47/230); [EFF](https://www.eff.org/issues/cda230))
- **Does NOT protect:** federal criminal law (incl. CSAM/obscenity), **intellectual property** (→ use DMCA), communications-privacy law, and **sex trafficking** (FOSTA-SESTA §230(e)(5)).

### 6.2 US — DMCA safe harbor (17 U.S.C. § 512) — mandatory hygiene

1. **Register a designated agent** with the Copyright Office + publish contact (**renew every 3 years**). ([copyright.gov/512](https://www.copyright.gov/512/); [agent directory](https://www.copyright.gov/dmca-directory/))
2. **Notice-and-takedown** — remove expeditiously on compliant notice; honor **counter-notice** (10–14 business days).
3. **Repeat-infringer termination policy** (§512(i)) — failing to reasonably implement **defeats the safe harbor entirely**.
4. Lose protection on actual/red-flag knowledge or financial-benefit-plus-control.

### 6.3 EU — Digital Services Act (Reg (EU) 2022/2065), fully applicable since Feb 17, 2024

Cumulative tiers. As a **hosting service** you need (Tier 1 + Tier 2):
- **EU legal representative** (Art 13) if no EU establishment; **points of contact** for authorities (Art 11) and users (Art 12); **T&Cs** stating moderation rules (Art 14); **annual transparency reports** (Art 15).
- **Notice-and-action mechanism** (Art 16) — failure to act on a substantiated notice gives "actual knowledge" and can lose the liability exemption; **statement of reasons** for every moderation action (Art 17); **notify authorities of suspected serious offences** threatening life/safety (Art 18).
- If tenant apps disseminate user content publicly, **online-platform** duties may attach (Arts 20–23: complaint-handling, ODR, **trusted flaggers**, anti-misuse). VLOP duties at ≥45M EU MAU. ([EU Commission DSA](https://digital-strategy.ec.europa.eu/en/policies/digital-services-act))
- **From July 1, 2025**, transparency reports must use the Commission's harmonized template. ([EU Commission](https://digital-strategy.ec.europa.eu/en/policies/dsa-brings-transparency))

### 6.4 CSAM reporting

- **US — 18 U.S.C. § 2258A** — mandatory **NCMEC CyberTipline** report *"as soon as reasonably possible after obtaining actual knowledge"*; **no affirmative monitoring duty**; preserve report 1 year. Penalties for knowing failure up to **$850k–$1M**. ([Cornell LII](https://www.law.cornell.edu/uscode/text/18/2258A))
- **EU** — scanning currently *voluntary* under interim Reg 2021/1232 (expiry extended toward 2028); permanent CSA Regulation still in trilogue. Plan for future mandatory risk assessment + EU Centre reporting. ([EDPS](https://www.edps.europa.eu/press-publications/press-news/press-releases/2026/extension-interim-rules-combat-child-sexual-abuse-online-must-address-shortcomings-and-prevent-indiscriminate-scanning_en))

### 6.5 Operational must-haves & vendor framing

Must have: abuse-reporting intake · registered DMCA agent · takedown/suspension + statement-of-reasons + counter-notice · repeat-infringer policy · CSAM→NCMEC pipeline · transparency reporting · AUP authorizing removal · EU legal rep + Art 16/17 mechanisms.

- **Cloudflare** — splits **host vs conduit**: removes content only from services it *hosts* (Pages, Workers, KV, Stream, Images); for pass-through CDN it forwards complaints to the actual host. ([Cloudflare abuse approach](https://www.cloudflare.com/trust-hub/abuse-approach/))
- **Vercel** — does proactive + reactive moderation; **2024 DSA transparency report**: ~10,959 actionable reports (phishing ~8,460, platform misuse 4,169, DMCA 1,015, CSAM 183). ([Vercel transparency](https://vercel.com/legal/transparency); [DMCA policy](https://vercel.com/legal/dmca-policy))

**Pattern:** draw a clear host-vs-conduit line; act only on content you host; standardize intake/workflows; keep an AUP that authorizes immediate suspension; publish a DSA-aligned transparency report.

---

## 7. KYC / AML / Payments via Stripe Connect

### 7.1 Connect models — KYC & liability differ

| | **Standard** | **Express** | **Custom** |
|---|---|---|---|
| Stripe relationship | Full, direct; own dashboard | Limited Express dashboard | None; platform owns everything |
| Who collects KYC | Account holder self-onboards (Stripe-hosted) | Stripe-hosted | **Platform** collects + submits via API |
| Who bears liability | Connected account; **platform NOT responsible** | **Platform** | **Platform** (incl. negative balances) |

*"A Platform is liable for the activity of all Express and Custom connected accounts."* Responsibility tracks the **onboarding method/config**, not strictly the legacy label. ([Standard](https://docs.stripe.com/connect/standard-accounts) · [Express](https://docs.stripe.com/connect/express-accounts) · [Custom](https://docs.stripe.com/connect/custom-accounts) · [identity verification](https://docs.stripe.com/connect/identity-verification))

### 7.2 KYC/KYB — and the key liability point

Stripe requires: legal entity (name, address, tax ID), **representative** (with significant control), **all beneficial owners (25%+ or executives)**, and documents when data can't be verified. **But:** *"Even after Stripe verifies a connected account, platforms still must monitor for and prevent fraud. Don't rely on Stripe's verification to meet any independent legal KYC or verification requirements."* SSA §11.1: *"User is solely responsible for evaluating and configuring the Services to comply with User's legal obligations."* ([required verification](https://docs.stripe.com/connect/required-verification-information) · [risk management](https://docs.stripe.com/connect/risk-management) · [SSA](https://stripe.com/legal/ssa))

### 7.3 Merchant of Record — **Stripe is NOT the MoR**

*"the business itself remains the MoR while Stripe acts as a payment processor."* The MoR (your platform or the connected account, by config) *"is liable for any disputes or refunds"* and *"handles any applicable regulations and liabilities, including sales taxes."* ([Stripe MoR](https://stripe.com/resources/more/merchant-of-record); [Connect MoR](https://docs.stripe.com/connect/merchant-of-record))

| Charge type | MoR / descriptor |
|---|---|
| Direct charges | Connected account |
| Destination/separate **with** `on_behalf_of` | Connected account (platform still covers losses if it goes negative) |
| Destination/separate **without** `on_behalf_of` | **Platform** |

**Tax:** Stripe Tax calculates/collects but **registration + remittance belong to the liable entity** (for software platforms, the connected accounts). Contrast a **true MoR (Paddle/Lemon Squeezy)** which becomes reseller-of-record and absorbs VAT/tax + chargeback liability — Paddle: *"Paddle acts as a reseller… is therefore the 'seller on record'… responsible for the collection and payment of VAT and tax instead of you."* ([Paddle](https://www.paddle.com/blog/what-is-merchant-of-record); [Lemon Squeezy](https://docs.lemonsqueezy.com/help/payments/merchant-of-record))

### 7.4 AML, fraud & negative balances — what's ALWAYS the platform's

You can assign some loss responsibility to Stripe (`losses_collector`), **but the platform is ALWAYS responsible for:** its own negative balances; **chargebacks + costs** for destination and separate charges; and **all refunds** (drawn from platform balance). If you're the loss-collector, Stripe pulls from a **platform reserve** after a negative balance persists 180 days. Card networks fine platforms that exceed dispute thresholds (>0.75% is "excessive"). Stripe's **Managed Risk** can absorb unrecoverable negatives, but only when Stripe is the responsible party. ([risk management](https://docs.stripe.com/connect/risk-management) · [account balances](https://docs.stripe.com/connect/account-balances) · [managed risk](https://docs.stripe.com/connect/risk-management/managed-risk))

**Monitoring duty:** verify accounts before activation; monitor for suspicious behavior; alert on dispute rate >0.75%, negative balances, volume swings, anomalous logins; reject/close suspected fraud.

### 7.5 Prohibited & restricted businesses you must enforce

Authoritative list: [stripe.com/legal/restricted-businesses](https://stripe.com/legal/restricted-businesses) (localized per country). **SSA §1.2(a)(ix)** prohibits transacting with **or *enabling* any entity** to benefit from a prohibited business — *this clause reaches your connected accounts.* *"content creator platforms are responsible for monitoring that their creators comply with… our Prohibited and Restricted Businesses list."*

Prohibited headline categories: illegal products, adult/sexual services, debt relief, certain financial services, gambling, IP infringement/counterfeits, marijuana/high-THC, **crypto mining/staking/ICOs**, MLM/pyramid/UDAAP. Restricted (pre-approval): crypto exchanges/wallets, lending/BNPL/money transmitters, firearms, pharma/telemedicine, tobacco/e-cig, NFTs, gift cards, third-party payment aggregation. ([restricted businesses](https://stripe.com/legal/restricted-businesses) · [SSA §1.2](https://stripe.com/legal/ssa))

### 7.6 Operator playbook

- **Minimal liability/KYC burden** → Standard accounts (sellers own KYC + fees + chargebacks; you're not responsible for their activity).
- **Branded embedded experience** → Express/Custom, but you own the liability and (Custom) must collect/submit KYC.
- **Assume you eat the losses** — chargebacks, refunds, your own negatives are always yours.
- **Monitor continuously**, enforce the prohibited/restricted list as an ongoing duty, expect a 90–180-day termination reserve, and know you **indemnify Stripe** (SSA §9.1(a)) for losses from your/your accounts' breaches.
- If your pitch is "no tax/dispute headache for tenants," recognize Connect does **not** make you a true MoR — consider Paddle/Lemon Squeezy only if offloading that liability beats the higher fees/less control.

---

## Sourcing caveats (flagged by the research)

- A few §1.4 platform-mechanism claims and the "2026 agent sandbox-bypass" anecdote lean on engineering blogs (Northflank, emirb.github.io) consistent with — but not identical to — each vendor's own docs; confirm a specific vendor's mechanism against its security docs before publishing it as fact.
- OpenAI policy pages and some security-vendor pages (Trend Micro, Fortra, Lemon Squeezy) block automated fetching; their figures/quotes were corroborated via reputable secondary outlets — re-confirm exact wording from the rendered pages before relying on it in a filing.
- A "$82,000 from a stolen Gemini key" DoW figure traced only to secondary blogs — treat as **unverified**.
- The exact clause text of Stripe's Connect Platform Services Terms subsection truncated on fetch; substance is corroborated by the SSA + Connected Account Agreement, but cite clause numbers from the rendered page. Note `stripe.com/legal/connect-platform` 404s — platform terms live at `stripe.com/legal/connect`.
- This document is research, not legal advice — have qualified counsel review, especially for EU AI Act / DSA exposure as an autonomous-agent provider.
