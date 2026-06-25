# Identity & Multi-Tenancy for Enterprise B2B SaaS — Research Blueprint (2025–2026)

A research-backed reference for designing the identity, tenancy, authorization, data-isolation, and audit/session layers of a B2B SaaS that needs to scale from startup to enterprise. Every major claim is cited; a full Sources section is at the end. Vendor-published microbenchmarks and marketing figures are flagged as directional.

> **TL;DR recommended stack** (full rationale in the last section):
> - **Auth/SSO:** WorkOS AuthKit (or Clerk) for managed SAML+OIDC+SCIM behind one integration; include SSO in mid-tier, not just "Enterprise."
> - **Tenancy:** `Organization` as the top-level tenant, `User`↔`Membership`↔`Organization` join model, optional `Team`/`Project` mid-layer.
> - **Authorization:** Start RBAC (small fixed role set + isolated billing role), add ReBAC (OpenFGA or SpiceDB) for resource-level sharing as you scale; layer ABAC for context.
> - **Isolation:** Shared Postgres + RLS with a `tenant_id`/`organization_id` column (pool), with a documented graduation path to **bridge** (schema-per-tenant) and **silo** (DB-per-tenant) for regulated/large tenants.
> - **Audit/session:** Append-only hash-chained audit log to WORM storage; short-lived JWT access tokens in `__Host-` cookies + opaque rotating refresh tokens; passkeys/WebAuthn for phishing-resistant MFA; envelope encryption via KMS/Vault for BYO-API-keys.

---

## 1. Auth & SSO

### 1.1 Signup / login flows
- **Email-first, not usernames.** Use email as the primary identifier and collect minimal data at signup, with progressive profiling afterward. Cutting form fields (e.g., 9→6) is reported to lift signups ~25%; ~88% of users won't return after a poor login experience. [Authgear]
- **Offer multiple login options side-by-side; never social-only.** Present email/password OR "Continue with Google" OR magic link together, and always keep email/password as a backup for users without social accounts or who distrust social login. [Scalekit/Clerk]
- **Passwordless reduces friction.** Magic links, email/SMS OTP, and passkeys lower friction; ~10% of active users hit password reset monthly and ~75% abandon it — a structural driver toward passwordless. [Authgear]

### 1.2 OAuth social login (Google, GitHub, Microsoft)
- **Always use Authorization Code flow + PKCE** (the redirect flow for web). PKCE prevents CSRF/code interception; request only minimal scopes and escalate later. [Clerk/Scalekit]
- **Match the provider to the audience:** Google is the consumer default; GitHub suits developer platforms; Microsoft (Entra/Azure AD) is the trusted business choice and bridges into corporate SSO. [Scalekit]
- **Don't hand-roll OAuth.** Use managed libraries/services (Auth.js/NextAuth, Clerk, Supabase Auth) that handle state/PKCE/account-linking edge cases. [Vibe Coder/Clerk]

### 1.3 Enterprise SSO — SAML & OIDC
- **Three protocols dominate:** SAML 2.0 (XML, signed assertions, 20-yr track record, universal IdP support), OAuth 2.0, and OIDC (JWT over REST, better for SPA/mobile/API). SAML embeds authorization claims but needs strict XML validation + manual cert rotation and performs poorly on native mobile. [Gupta Deepak]
- **SP-initiated vs IdP-initiated:** SP-initiated starts at your app (issues an `AuthnRequest`, correlates via `InResponseTo`/RequestID, supports RelayState, better replay protection). IdP-initiated starts at the IdP portal with *unsolicited* assertions (no RequestID) — more convenient, more vulnerable to replay/injection. Most enterprises expect you to support both. [WorkOS]
- **OIDC is the forward default; SAML still has the broadest coverage.** Essentially every enterprise/government IdP speaks SAML 2.0 (ADFS, Shibboleth, on-prem Ping are SAML-only); modern IdPs (Okta, Entra, Google) do both. OIDC is required for mobile (RFC 8252/AppAuth) and preferred for SPAs. **Pragmatic guidance: pick a provider that supports both behind one integration.** [Clerk]
- **Home Realm Discovery (HRD) by email domain is core B2B architecture.** Register email domains against each enterprise connection so login auto-routes to the right IdP ("Home Realm Discovery" in Auth0/Entra, "IdP Discovery" in Okta). [Microsoft Entra/Auth0]
- **SAML security baseline:** require signed responses *and* assertions, never accept unsigned SAML, use SHA-2 certs, reject self-signed certs, encrypt assertions where possible, enforce assertion-lifetime/replay checks. [WorkOS/SSOJet]

### 1.4 SCIM provisioning / deprovisioning
- **SCIM (RFC 7643/7644) = automated user lifecycle** between an IdP (Okta, Entra, JumpCloud) and your app. SSO authenticates at login; SCIM handles create/update/disable *after* the account exists. [Stytch/Microsoft]
- **Deprovisioning is the security-critical half.** Every account left active after offboarding is a backdoor. SOC 2 doesn't mandate SCIM but does require a documented, logged offboarding process; SCIM also stops you paying for unused seats. [WorkOS/PropelAuth]

### 1.5 When enterprises *require* SAML/SCIM, and the "enterprise gate"
- **SAML SSO is "item one" on the security questionnaire at ~$30–50K ACV** — the most common feature gating contracts above ~$50K/year. Plan for it before you need it; retrofitting on the wrong platform is a multi-week project. [Gupta Deepak]
- **SCIM becomes mandatory around ~1,000-seat customers.** Below ~100 enterprise customers it's nice-to-have; at ~1,000 seats per customer manual management is unacceptable. [Gupta Deepak]
- **Model the Organization as a first-class entity.** The org is the unit of contract; SSO config, MFA policy, audit retention, and access controls must be **org-scoped**, not global. [Gupta Deepak]

### 1.6 The "SSO tax" debate
- **The SSO tax = gating SSO behind enterprise tiers at markups far above cost** (sso.tax "Wall of Shame"). Documented examples: Railway ~9,900%, Appsmith ~16,567%, Mixpanel ~4,065%, Coursera 2,400% ($1,995→$49,875/yr), GitHub 425%, Figma ~275%, Notion ~88%, Slack ~72%. [sso.tax]
- **The debate:** vendors cite real support cost for "homebuilt" enterprise IdPs and use SSO as a segmentation lever; critics note "if your SSO pricing is 3x your base, are 2/3 of your costs just keeping SAML going?" CISA's "Secure by Design/Demand" guidance frames paywalled SSO as a security anti-pattern, and the ssotax.org "Friends of SSO" list catalogs vendors that include it at no surcharge. [1Password/Sastrify]

### 1.7 Real platforms compared

| Provider | Pricing model | SSO | SCIM | Notable |
|---|---|---|---|---|
| **WorkOS (AuthKit)** | Per-connection | $125 (1–15) → $50 (101–200) per connection | Directory Sync per-connection | AuthKit free to **1M MAU**; cheapest for a handful of SSO customers; not self-hostable. Audit Logs $99/mo per 1M events. [WorkOS/Clerk] |
| **Auth0 (Okta CIC)** | Per-MAU | Gated to higher tiers; B2B Essentials from $150/mo (3 conns), Pro from $800/mo (5) | Inbound SCIM free on free tier | Per-MAU spikes unpredictably with large-seat customers; Okta-owned. [Clerk/WorkOS] |
| **Clerk** | Per-MAU + per-conn | 1 conn included, then $75 (2–15) → $15 (500+) | **Bundled free on every connection** | SSO+SCIM in base paid tier; predictable. [Clerk] |
| **Stytch (Twilio)** | Per-MAU + per-conn | 5 SSO/SCIM conns included free, then $125 each | Included with the 5 | Twilio acquisition Nov 2025 → repricing risk. [Clerk] |
| **Supabase Auth** | Per-MAU | SAML/OIDC free for first 50 SSO MAU then $0.015/MAU | **No SCIM** | Cheap app-level auth; SOC 2 report gated to Team plan ($599/mo). [Supabase] |
| **Ory (Kratos+Hydra)** | OSS / Enterprise License | SAML & org-login **not in OSS** (Enterprise License or Ory Network) | Not in OSS | Self-hostable, avoids per-MAU explosions at scale; you own operations. Hydra is OIDC-certified (used by ChatGPT login). [Ory] |

- **The single biggest cost decision is per-connection (WorkOS, Clerk, base Stytch) vs per-MAU (Auth0, Supabase, Firebase)** — per-MAU spikes unpredictably when you land a large-seat enterprise customer. Building SSO in-house is estimated at **$110K–$1.1M+ over three years**. [Clerk]

---

## 2. Tenancy Hierarchy

### 2.1 How real platforms model it (actual entity names)

| Platform | Top-level tenant | Mid-level | Leaf / work unit | Billing attaches to | Identity model |
|---|---|---|---|---|---|
| **Vercel** | Organization (Enterprise) | Team | Project | A designated "billing team" (rolled-up Enterprise invoice) | Roles at team level |
| **Supabase** | Organization | — (no team layer) | Project (= a dedicated Postgres instance) | Organization (1 plan per org) | Members at org level |
| **Linear** | Workspace | Team → Sub-team (≤5 levels) | Issue (Projects/Initiatives cut across teams) | Workspace | One account ↔ many workspaces (hybrid) |
| **GitHub** | Enterprise account | Organization → Teams (nested) | Repository | Cost centers / org | One personal account across orgs |
| **Slack** | Enterprise organization (org) | Workspace | Channel | Org | Shared org-wide identity (`U`/`W` id) |
| **Notion** | Workspace | Teamspace | Page → sub-page | Workspace | Workspace member + teamspace member |
| **Retool** | Organization (split via Spaces) | Folder (Permission Groups for access) | App / Workflow / Resource | Organization | Permission-group membership |

Selected specifics:
- **Vercel:** API exposes `organizationId` (`org_…`), `teamId` (`team_…`), `ownerId` (`user_…`); child teams carry a `parentId`. Teams take a `billingPlan` of `platform` (many limited Pro teams) or `enterprise` (full perms, max 100 per org). [Vercel docs]
- **Supabase:** Two levels only — Org → Project; each project is a dedicated Postgres instance. One subscription/plan per org; plans can't be mixed (create separate orgs to mix). Projects can be transferred between orgs. [Supabase docs]
- **Linear:** Workspace → Team → **Sub-team (≤5 levels)**; Issues belong to one team (`ENG-123`), while Projects and Initiatives span teams. [Linear docs]
- **GitHub:** Enterprise → Orgs → **nested Teams** (parent access cascades to child teams) → Repositories; billing via **cost centers**. [GitHub docs]
- **Slack Enterprise Grid:** Org → Workspaces → Channels; one org-wide identity; centralized vs distributed workspace design models. [Slack]
- **Notion:** Workspace → Teamspace → Page; teamspace access types **Open / Closed / Private**; "inheritance with override" from workspace down to page sharing. [Notion]
- **Retool:** No native team/workspace nesting — **Permission Groups** are the access layer (Use → Edit → Own); **Spaces** split one org into fully isolated sub-orgs. [Retool]

### 2.2 General modeling guidance
- **Name the top-level tenant "Organization."** Preferred over "Account" (confusing), "Team" (limits hierarchy), "Company" (assumes commercialism), or "Workspace" (conflicts with nested structures). [FlightControl]
- **Core schema: `User` ↔ `Membership` (join table, carries role) ↔ `Organization`** — never a direct user→org FK, so users can belong to many orgs. [FlightControl]
- **Three identity models** — *GitHub* (one account across orgs), *Google* (a separate account per org), *Linear* (one account → many orgs **and** a person can hold multiple accounts; "ideal for most B2B startups"). [FlightControl]
- **Tenancy is a first-class dimension:** every record belongs to exactly one tenant; every request carries tenant context; every read/write/authz path enforces it. Default to a shared DB with an `organization_id` column unless you have an exceptional requirement. [FlightControl/WorkOS]
- **Cross-platform permission principles (Slack/Notion/Linear):** keep global roles simple and push customization to org boundaries; sensible defaults + opt-in complexity; IdP group mapping (SCIM/SSO) for assignment at scale; use structural controls (private teams, workspace scoping) alongside permission flags. [WorkOS]

**Converging pattern:** most platforms use a **3-tier model** — a billing-bearing top tenant, a mid-level grouping, and a work unit. "Organization" and "Workspace" dominate as top-tenant names. Billing almost always attaches to exactly one top entity with usage rolling up. Roles trend toward **two tiers**: a small global role set + a delegated mid-level (team/teamspace) owner, with IdP/SCIM mapping at scale.

---

## 3. RBAC / Authorization

### 3.1 Role models on real platforms
- **GitHub:** Owner / Member / **Billing Manager** (the billing role *does not consume a paid seat*), plus predefined granular roles (Security Manager, App Manager, CI/CD). [GitHub]
- **Vercel:** Team-level Owner / Member / Developer / Billing / Viewer (+ Security on Enterprise); separate project-level roles via **Contributors**. [Vercel]
- **Linear:** A deliberately minimal Admin / Member / Guest (+ Owner on Enterprise) and a delegated **team owner** role (2025). [Linear]
- **Notion:** Workspace Owner / Membership Admin / Member / Guest with **inheritance-with-override** down to page-level sharing (Full Access → Can Edit → Can Comment → Can View). [Notion]
- **Slack:** Workspace Primary Owner/Owners/Admins/Members/Guests (admins **cannot** access Billing) + org-tier roles on Grid; **workspace-scoped system roles** (e.g., Channels Admin in Engineering only). [Slack]
- **Stripe:** Owner / Administrator / Developer / Analyst / Support Specialist / View Only / IAM Admin — **no custom roles on any plan**; users can hold multiple roles (permissions combine). [Stripe]

**Pattern:** keep the global role set small (3–6 fixed roles), push customization to *scoped* roles, and **isolate billing as a narrow, often seat-free role.**

### 3.2 RBAC vs ABAC vs ReBAC
- **RBAC** — permissions via roles (Admin/Manager/Employee). Fastest for simple checks (≈O(1) string lookup); suffers **role explosion**.
- **ABAC** — access via attributes of user/resource/environment evaluated against policies. Fits dynamic, contextual rules (time, location, risk, "assigned_physician"); can suffer **attribute explosion**.
- **ReBAC** — access via relationships between entities (user *is owner of* doc, doc *is in* folder). Best for resource-level, hierarchical, sharing (the Google Drive model); most flexible but heaviest on query/graph traversal. [Pangea/Permit.io]
- **Recommended evolution:** start **RBAC** → add **ReBAC** for resource-level permissions as you scale → layer **ABAC** for contextual refinement. Most 2026 B2B SaaS ends up hybrid (sometimes called **PBAC**, policy-based access control). [Permit.io/dev.to]

### 3.3 Google Zanzibar (the reference architecture)
- **Relation tuples:** `<namespace>:<objectid>#<relation>@<user>` (e.g., `doc:readme#viewer@user:alice`); `<user>` can be a **userset** (object#relation), enabling nested groups. Production: 1,500+ namespaces. [AuthZed/USENIX]
- **Userset rewrite rules:** set algebra (union/intersection/exclusion) + `_this`, `computed_userset` (editors are also viewers), `tuple_to_userset` (a doc inherits its folder's viewers). [AuthZed]
- **New Enemy Problem:** must respect causal ordering so stale ACLs aren't applied to new content (remove Bob → add new docs → Bob must not see them). **Zookies** (opaque timestamp tokens) bound staleness on reads and solve this without full global consistency. **Leopard indexing** denormalizes deeply nested groups. [AuthZed/WorkOS]
- **Scale benchmark:** 10M+ QPS, 2T+ tuples (~100 TB), p95 ~11 ms / p99 ~20 ms, >99.999% availability for 3 consecutive years. [AuthZed/USENIX]

### 3.4 Authorization engines — when each fits

| Engine | Type | Consistency story | Best fit |
|---|---|---|---|
| **SpiceDB (AuthZed)** | Zanzibar ReBAC, stores relations | Full **ZedToken** (zookie) model | Zanzibar-purist ReBAC needing the new-enemy guarantee |
| **OpenFGA (CNCF/Auth0)** | Zanzibar ReBAC, stores relations | **No zookie** — weaker consistency, hot-object caching | Auth0 ecosystem, broad language support |
| **Permify** | Zanzibar ReBAC | Has a zookie analog (**Snap Token**) | DX-focused ReBAC, closer to SpiceDB on consistency |
| **Ory Keto** | Zanzibar ReBAC | Native Zanzibar | Teams already on the Ory stack |
| **Cerbos** | Stateless PDP, YAML policies | App owns data (sidesteps new-enemy) | Microservices wanting centralized policy + low-latency local decisions; ~17× faster than OPA after custom engine |
| **Oso** | Embedded-in-app | App owns data | Bespoke authz logic needing tight app integration |
| **Permit.io** | Hybrid over OPA+OPAL | Distributes policy/data to PDPs | Evaluating **ReBAC + ABAC in one call**; Rego/Cedar |
| **AWS Verified Permissions / Cedar** | Managed PARC engine | App passes context | RBAC+ABAC(+ReBAC) in one policy; **Cedar is formally verified** (Lean theorem prover) |

### 3.5 PDP architecture
- **Centralized PDP** adds a network hop per check (latency + availability bottleneck). **Embedded/sidecar PDPs** (the OPA pattern, one sidecar per pod) remove the hop. [AWS Prescriptive Guidance/Cerbos]
- **Dominant production pattern is hybrid:** **centralize policy authoring/distribution, distribute evaluation** to sidecars/libraries with caching + HA (OPA canonical; Permit.io's OPAL syncs policy/data). [AWS/devsecopsnow/Permit.io]
- **The core fork:** *store relationships?* → Zanzibar-style engines (consistency/zookie question dominates; only SpiceDB/Permify/Keto have a zookie equivalent). *Stateless evaluation?* → Cerbos/Oso/Cedar-AVP (app owns the data, sidesteps distributed consistency, but you build the relationship graph). [sph.sh/Cerbos/AuthZed]

---

## 4. Data Isolation Models

### 4.1 The pool / silo / bridge taxonomy (AWS SaaS Lens)
- **Silo** = dedicated resources per tenant (separate stack/DB); **Pool** = shared scalable infra (table indexed by tenant id); **Bridge** = mixed (some services siloed, some pooled). Even silo deployments share identity, onboarding, metering, and ops — that's what makes it SaaS. [AWS SaaS Lens]
- **Isolation vs cost is an inverse spectrum, not binary.** Silo = strongest isolation, most cost/complexity; pool = least isolation, least cost. Microsoft frames it as a continuum and notes you can place different tiers at different points (shared web tier + isolated DBs). [AWS/Microsoft]

### 4.2 The three patterns and their tradeoffs

| Dimension | **Pool** (shared DB + RLS / `tenant_id`) | **Bridge** (schema-per-tenant) | **Silo** (DB-per-tenant) |
|---|---|---|---|
| Isolation strength | Weakest (logical, bug-prone) | Medium (namespace) | Strongest (physical) |
| Postgres scale ceiling | 100k–millions of tenants | ~hundreds–few thousand (catalog bloat at ~1–2k schemas) | ~10s–few hundred (`max_connections`) |
| Cost efficiency | Best | Medium | Worst (≈N× per tenant) |
| Noisy neighbor | High risk | Reduced | None |
| Blast radius | All tenants | Schema-scoped | Single tenant |
| Migrations | One migration, all tenants | Per-schema (linear effort) | Per-DB (worst) |
| Cross-tenant analytics | Easy | Cross-schema joins | Effectively impossible in Postgres |
| Compliance fit | Needs RLS + audit logs + isolation tests | Better | Best (HIPAA/data residency) |
| Primary failure mode | Data-leak bug / missing `WHERE tenant_id` | `pg_catalog` bloat | Connection exhaustion |

Key numbers and gotchas:
- **Silo cons scale poorly:** "20 siloed accounts might be manageable… a thousand tenants would impact operational efficiency." DB-per-tenant dies first on **connection pooling** (PgBouncer pools are per-database, quickly exceeding `max_connections`), not storage; each new DB also copies the template (~8 MB). [AWS/PlanetScale]
- **Bridge breaks on catalog bloat:** hundreds of schemas → millions of `pg_catalog` rows → slow planner; practical degradation ~1,000–2,000 schemas; migrations must run per-schema (slower, harder to roll back). [PlanetScale/Crunchy]
- **Pool's core risk:** with shared compute you "can't lean on networking/IAM constructs" — a missing `WHERE tenant_id` or RLS bug leaks data; isolation must be enforced **in-band** (RLS, scoped credentials). [AWS]

### 4.3 Postgres Row-Level Security (RLS) in practice — the Supabase model
- **RLS acts as an implicit `WHERE` clause tied to Postgres roles** (`anon`, `authenticated`); policies use `auth.uid()`/`auth.jwt()`, with `USING` checking existing rows and `WITH CHECK` validating writes. [Supabase]
- **Five performance gotchas (vendor microbenchmarks — directional):**
  1. **Wrap functions in a subquery** so the optimizer caches via initPlan: `(select auth.uid())` → 179ms→9ms (~95%); `is_admin()` → 11,000ms→10ms (~1,100×).
  2. **Index policy columns** (`tenant_id`, `user_id`) or you get a seq scan: 171ms→<0.1ms (~99.94%).
  3. **Add explicit client-side filters** duplicating the predicate (`.eq('user_id', userId)`): 171ms→9ms (~19×).
  4. **Always specify `TO authenticated`** to short-circuit irrelevant roles: 170ms→<0.1ms (~99.78%).
  5. **Push membership joins into `SECURITY DEFINER` functions / restructure `IN` clauses:** 178,000ms→12ms (~14,800×). [Supabase GitHub #14576]
- **RLS satisfies most SOC 2 auditors only when paired with audit logging + automated isolation tests** — necessary but not sufficient. [Clerk]

### 4.4 Sharding by tenant_id (Citus)
- **Put `tenant_id` on every table** so Citus co-locates a tenant's rows on one node → single-step queries with **full SQL support**, no network shuffle on joins. [Citus]
- **Citus 12 (2023) added schema-based sharding** (`citus.enable_schema_based_sharding`, `citus_schema_distribute()`): schema-per-tenant with **no query changes** (just per-tenant `search_path`), and it **fixed the PgBouncer pooling problem** that previously made schema-per-tenant impractical. [Citus/Crunchy]

### 4.5 When to graduate, and the hybrid endpoint
- **The dominant mature pattern is hybrid:** small/free tenants share a schema; enterprise/regulated tenants get their own schema or database; route in middleware by tenant tier. Microsoft formalizes this as **vertical** (mix single+multitenant by tier) and **horizontal partitioning** (shared app tier + per-tenant DBs to "mitigate a noisy neighbor problem"), plus bin-packing tenants into stamps. [PlanetScale/Microsoft]
- **Graduation rule of thumb:** start shared, move only when **compliance, scalability/noisy-neighbor, or deep per-tenant customization** forces it. The decision is "cheap to make, expensive to undo" — migrating live tenant data means downtime customers notice. [PlanetScale/Bytebase]
- **Silo's strongest justification:** blast-radius elimination, instant per-tenant restore, and direct satisfaction of **data residency / HIPAA (per-customer BAA)** — at the cost of needing a **control plane** to automate provisioning. [Neon]

---

## 5. Audit & Session Security

### 5.1 Audit log design for SOC 2
- **Capture who/what/when/where, immutably.** Per-entry fields: `timestamp`; actor (`id`, `email`, `ip_address`, `session_id`); `action`; resource (`type`, `id`, `changes` with old/new); `result`, `method`, `user_agent`. OWASP groups these as When/Where/Who/What. [AuditPath/OWASP]
- **Tamper-evidence cryptographically:** append-only DB + hash-chaining each record (or ledger/WORM object storage). AWS CloudTrail signs each file (log file validation). [hoop.dev/AuditKit]
- **Store logs separately from the systems that generate them** — dedicated logging account, S3 Object Lock in **compliance mode** (WORM), minimal write principals. [AuditPath]
- **Retention:** SOC 2 sets no fixed number; auditor expectation is **≥12 months**, with **~90 days hot/searchable** and the rest in cold storage (Glacier/Archive). [AuditPath/Konfirmity]
- **Never log secrets:** no passwords, session IDs/tokens, keys, connection strings, or cardholder data; treat IPs as PII; synchronize time via NTP for cross-service correlation. [OWASP]
- **OWASP A09:2025** reframes the category as "Security Logging **and Alerting** Failures" — alert on failed auth (brute-force, credential stuffing, password spraying), not just log it. [OWASP Top 10:2025]
- **For B2B, audit logs are a sales deliverable:** tenant-scoped queryable UI for customer admins; coverage of SSO logins, SCIM events, MFA changes, role/admin changes; durable-queue SIEM export (commit to DB first, decoupled from SIEM availability). [SSOJet/AuditKit]

### 5.2 Session management
- **Session IDs:** ≥64 bits of entropy from a CSPRNG, opaque/meaningless (all meaning held server-side). [OWASP]
- **Cookie attributes:** `Secure` + `HttpOnly` + `SameSite=Strict`(or Lax) + `__Host-` prefix + restrictive `Path`; **do not set `Domain`** (keeps cookie origin-bound). [OWASP]
- **Three timeouts, all enforced server-side:** idle (2–5 min high-value, 15–30 min low-risk), absolute (4–8 h), and renewal (periodic session-ID regeneration). **Regenerate the session ID on login and every privilege change** (anti-fixation). [OWASP]
- **Session vs token tradeoff:** JWTs scale and avoid backend lookups but have "no straightforward way to revoke." Make session length / token duration / inactivity timeout **admin-configurable per organization** for enterprise customers. [WorkOS]

### 5.3 Refresh tokens & revocation
- **Lifetimes:** access tokens **5–30 min** (ideally minutes), refresh tokens **days to weeks** (7 days common). Short access tokens propagate session changes fast and limit leak blast radius. [WorkOS/env.dev]
- **Rotation + reuse detection:** every refresh exchange issues a new access *and* refresh token and invalidates the old one. Auth0's **token-family** model revokes the entire grant if a rotated-out token is replayed (catches theft), with a configurable **rotation overlap/grace period** to avoid false positives from retries, plus **IP-diversity** monitoring per family. [Auth0]
- **Use opaque refresh tokens, not JWTs** (server-side lookup enables real revocation); reserve JWTs for access tokens. [WorkOS/env.dev]
- **Token storage:** HttpOnly secure cookie (web), iOS Keychain / Android Keystore (mobile), DB/cache (backend) — **never localStorage**. [WorkOS]
- **JWT revocation:** combine short-lived tokens + a `jti` denylist in Redis (TTL = token expiry) + a **per-user token-version counter** to "revoke all" in one DB update (breach case). [SuperTokens/Medium]

### 5.4 MFA / 2FA
- **TOTP is the baseline but NOT phishing-resistant** — a real-time AiTM proxy can relay codes. **WebAuthn/passkeys (FIDO2)** are phishing-resistant via origin binding (browser checks the challenge origin against registration). [FedResources/Hideez]
- **NIST SP 800-63-4 (2025, final):** AAL2 must offer a phishing-resistant option; AAL3 requires a phishing-resistant authenticator with **non-exportable private key + explicit user intent**. Cyber insurers (AIG, Beazley) now price premiums on phishing-resistant MFA for privileged/remote access. [wwpass/FedResources]
- **Step-up / adaptive MFA:** trigger extra factors only on elevated risk (new device, unusual location) or sensitive actions (exports, large transfers). Example: Salesforce step-up before report exports. [Entrust/Salesforce]

### 5.5 Secret / BYO-API-key storage
- **Envelope encryption is the core pattern:** encrypt data with a **DEK**, encrypt the DEK with a **KEK/root key** that never leaves the HSM. Re-encrypt only the small DEK to rotate; **never store a plaintext DEK** (discard from memory after use). [AWS/GCP KMS]
- **AWS KMS:** root keys in **FIPS 140-3 Level 3 HSMs**, AES-256-GCM. Flow for storing a customer API key: `GenerateDataKey` → encrypt the key locally with the plaintext DEK → store ciphertext + wrapped DEK → zero the plaintext DEK; `Decrypt` the wrapped DEK to read. Only the tiny DEK crosses the network. **BYOK** (import key material wrapped with RSAES-OAEP) lets regulated customers prove control. [AWS KMS]
- **GCP Secret Manager** uses the same DEK/KEK envelope (optionally **CMEK** with your Cloud KMS key). [GCP]
- **HashiCorp Vault Transit = "encryption as a service":** app sends plaintext, Vault returns ciphertext, key material never leaves Vault; supports versioned key rotation (keyring + `min_decryption_version`), convergent encryption (equality queries on ciphertext), and **dynamic secrets** (short-lived per-app credentials with auto-revocation). [HashiCorp]

---

## 6. Opinionated Recommendation — a startup→enterprise B2B stack

**Identity & SSO.** Adopt a managed auth provider that exposes **SAML + OIDC + SCIM behind one integration** and uses **per-connection** pricing — **WorkOS AuthKit** (free to 1M MAU; cheapest with a handful of SSO customers) or **Clerk** (SSO + SCIM in the base paid tier, predictable). Avoid betting your enterprise motion on per-MAU pricing (Auth0/Supabase) — it spikes exactly when you land a big-seat customer. Don't paywall SSO behind a top "Enterprise" tier alone; include it mid-tier to avoid the sso.tax reputational/regulatory liability. If you must self-host for cost/control at large scale, **Ory (Kratos + Hydra)** is the OSS path — but SAML/SCIM/org-login require the Enterprise License and you own operations. **Build the org-first data model from day one**, since SAML lands at ~$30–50K ACV and SCIM at ~1,000 seats.

**Tenancy.** Make **`Organization` the top-level tenant** and the unit of billing/contract. Use a **`User` ↔ `Membership` ↔ `Organization`** join model (Linear identity model: one account → many orgs). Add an optional mid-layer (`Team` and/or `Project`) only when product structure needs it — most platforms converge on a 3-tier billing-tenant → grouping → work-unit shape. Scope SSO config, MFA policy, audit retention, and roles to the org. Carry `organization_id` on every row.

**Authorization.** Start with **RBAC**: a small fixed role set (`owner`, `admin`, `member`, `viewer`) plus an **isolated, seat-free `billing` role** (as GitHub/Vercel/Slack do). Keep global roles small and push customization to scoped roles (team/project owners). As resource-level sharing appears, add **ReBAC** — **OpenFGA** if you want CNCF/Auth0-ecosystem and broad language support, **SpiceDB** if you need the full Zanzibar zookie/new-enemy consistency guarantee. Layer **ABAC** for contextual rules later. Run authorization as **centralized policy + embedded/sidecar evaluation** for latency. If you're all-in on AWS, **Cedar / Verified Permissions** (formally verified, RBAC+ABAC+ReBAC in one policy) is a strong managed alternative.

**Data isolation — recommended path.** Start **pooled: a single Postgres with RLS and an `organization_id` column on every table.** It scales to 100k–millions of tenants, gives one-migration-all-tenants operations, and makes cross-tenant analytics trivial. Enforce isolation in-band (RLS + scoped credentials) and **prove it with automated isolation tests + audit logging** (required for SOC 2). Apply the five RLS performance disciplines (subquery-wrap auth functions, index policy columns, explicit filters, `TO authenticated`, `SECURITY DEFINER` for membership joins). Plan a **graduation path to a hybrid model**: keep small/free tenants pooled; move regulated, residency-bound, or noisy large tenants to **schema-per-tenant** (now viable at scale with **Citus 12** schema-based sharding + PgBouncer fix) and, for the heaviest HIPAA/data-residency cases, **database-per-tenant** behind a provisioning **control plane** (Neon-style). Route by tenant tier in middleware. Don't start siloed — the decision is cheap to make and expensive to undo, and DB-per-tenant dies on `max_connections` long before storage.

**Audit & session security.** Ship an **append-only, hash-chained audit log** (who/what/when/where) to a **separate WORM-locked store** (S3 Object Lock compliance mode), 12-month retention with 90 days hot, never logging secrets, exposed as a **tenant-scoped query UI** with durable-queue SIEM export. Use **short-lived JWT access tokens (5–15 min)** in `__Host-` HttpOnly/Secure/SameSite cookies + **opaque rotating refresh tokens** with token-family reuse detection and a grace window; add a **per-user token-version** counter for "revoke all." Enforce idle (15–30 min) and absolute (4–8 h) timeouts server-side; make these org-configurable for enterprise. Offer **WebAuthn/passkeys** for phishing-resistant MFA (NIST 800-63-4 AAL2/AAL3, insurer-driven) with TOTP as fallback and **step-up** on sensitive actions. For **BYO-API-keys/secrets**, use **envelope encryption** with a KMS/Vault-held KEK (AWS KMS or Vault Transit), discard plaintext DEKs immediately, and offer **CMEK/BYOK** to regulated customers as a control they can demonstrate.

---

## Sources

**Auth & SSO**
- https://www.authgear.com/post/login-signup-ux-guide/
- https://www.scalekit.com/social-authentication • https://www.scalekit.com/blog/comparing-social-login-providers
- https://clerk.com/articles/how-do-i-implement-social-login-for-my-web-app
- https://clerk.com/articles/oidc-vs-saml-for-enterprise-sso-a-2026-decision-guide
- https://clerk.com/articles/the-real-cost-of-enterprise-sso-per-connection-vs-per-mau-pricing
- https://guptadeepak.com/sso-deep-dive-saml-oauth-and-scim-in-enterprise-identity-management/
- https://guptadeepak.com/ciam-compass/guides/b2b-saas-identity/
- https://workos.com/blog/sp-initiated-sso-vs-idp-authentication
- https://workos.com/blog/the-best-saml-providers-for-b2b-saas-in-2025
- https://workos.com/blog/auth0-pricing-how-it-works-and-compares-to-workos • https://workos.com/blog/how-scim-deprovisioning-works • https://workos.com/compare/auth0
- https://stytch.com/blog/scim-protocol-explained/
- https://supabase.com/docs/guides/auth/enterprise-sso/auth-sso-saml • https://supabase.com/pricing
- https://www.ory.com/open-source • https://github.com/ory/kratos • https://github.com/ory/hydra
- https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/home-realm-discovery-policy
- https://sso.tax/ • https://ssotax.org/ • https://1password.com/blog/explaining-the-backlash-to-the-sso-tax • https://www.sastrify.com/blog/sso-a-basic-security-need-or-an-enterprise-level-luxury

**Tenancy hierarchy**
- https://vercel.com/docs/organizations • https://vercel.com/docs/rbac/access-roles/team-level-roles • https://vercel.com/docs/accounts/team-members-and-roles
- https://supabase.com/docs/guides/platform/billing-on-supabase • https://supabase.com/docs/guides/platform/billing-faq • https://supabase.com/blog/organization-based-billing • https://supabase.com/docs/guides/platform/project-transfer
- https://linear.app/docs/conceptual-model • https://linear.app/docs/teams • https://linear.app/docs/sub-teams • https://linear.app/docs/members-roles • https://linear.app/changelog/2025-03-06-sub-teams
- https://docs.github.com/en/enterprise-cloud@latest/admin/overview/about-enterprise-accounts • https://docs.github.com/en/organizations/organizing-members-into-teams/about-teams • https://github.blog/enterprise-software/devops/best-practices-for-organizations-and-teams-using-github-enterprise-cloud/
- https://slack.com/help/articles/115005474583-Enterprise-Grid-structure-and-design • https://slack.engineering/unified-grid-how-we-re-architected-slack-for-our-largest-customers/ • https://docs.slack.dev/enterprise/
- https://www.notion.com/help/intro-to-teamspaces • https://www.notion.com/help/intro-to-workspaces
- https://docs.retool.com/org-users/concepts/permission-groups • https://docs.retool.com/org-users/tutorials/spaces • https://docs.retool.com/permissions/guides/configure-permission-groups
- https://www.flightcontrol.dev/blog/ultimate-guide-to-multi-tenant-saas-data-modeling
- https://workos.com/blog/multi-tenant-permissions-slack-notion-linear • https://workos.com/blog/developers-guide-saas-multi-tenant-architecture

**RBAC / authorization**
- https://docs.github.com/en/organizations/managing-peoples-access-to-your-organization-with-roles/roles-in-an-organization • https://docs.github.com/en/organizations/managing-peoples-access-to-your-organization-with-roles/permissions-of-predefined-organization-roles
- https://vercel.com/docs/rbac/access-roles/project-level-roles • https://vercel.com/changelog/increased-security-with-the-developer-and-billing-roles
- https://linear.app/changelog/2025-12-17-team-owners
- https://www.notion.com/help/whos-who-in-a-workspace
- https://slack.com/help/articles/360018112273-Types-of-roles-in-Slack • https://slack.com/help/articles/201314026-Permissions-by-role-in-Slack
- https://docs.stripe.com/get-started/account/teams/roles • https://stripe.com/blog/new-roles-and-permissions-in-the-dashboard
- https://pangea.cloud/blog/rbac-vs-rebac-vs-abac/ • https://www.permit.io/blog/rbac-vs-abac-vs-rebac • https://www.permit.io/blog/abac-vs-rebac • https://www.permit.io/blog/conditions-vs-relationships-choosing-between-abac-and-rebac • https://www.permit.io/blog/top-open-source-authorization-tools-for-enterprises-in-2026 • https://www.permit.io/blog/rebac-in-practice-permitio-vs-openfga
- https://dev.to/kanywst/rbac-vs-abac-vs-rebac-how-to-choose-and-implement-access-control-models-3i2d
- https://authzed.com/zanzibar • https://www.usenix.org/system/files/atc19-pang.pdf • https://workos.com/guide/google-zanzibar
- https://authzed.com/learn/openfga-alternatives • https://sph.sh/en/posts/spicedb-vs-auth0-fga/ • https://www.pkgpulse.com/guides/openfga-vs-permify-vs-spicedb-zanzibar-authorization-2026
- https://www.cerbos.dev/blog/cerbos-vs-opa • https://www.cerbos.dev/news/rise-of-embeddable-pdps-in-architectures • https://www.osohq.com/learn/cerbos-alternatives-for-authorization
- https://docs.permit.io/concepts/pdp/overview/
- https://developer-friendly.blog/blog/2024/07/01/ory-keto-authorization-and-access-control-as-a-service/
- https://aws.amazon.com/verified-permissions/ • https://docs.aws.amazon.com/verifiedpermissions/latest/userguide/terminology.html • https://aws.amazon.com/about-aws/whats-new/2025/08/amazon-verified-permissions-cedar-4-5/ • https://arxiv.org/pdf/2407.01688 • https://arxiv.org/pdf/2403.04651 • https://github.com/cedar-policy/cedar
- https://docs.aws.amazon.com/prescriptive-guidance/latest/saas-multitenant-api-access-authorization/centralized-pdp.html • https://docs.aws.amazon.com/prescriptive-guidance/latest/saas-multitenant-api-access-authorization/using-opa.html • https://www.devsecopsnow.com/opa/

**Data isolation**
- https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/silo-pool-and-bridge-models.html • https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/silo-isolation.html • https://docs.aws.amazon.com/whitepapers/latest/saas-tenant-isolation-strategies/pool-isolation.html
- https://learn.microsoft.com/en-us/azure/architecture/guide/multitenant/considerations/tenancy-models • https://learn.microsoft.com/en-us/azure/azure-sql/database/saas-tenancy-app-design-patterns
- https://supabase.com/docs/guides/database/postgres/row-level-security • https://github.com/orgs/supabase/discussions/14576
- https://planetscale.com/blog/approaches-to-tenancy-in-postgres • https://www.crunchydata.com/blog/designing-your-postgres-database-for-multi-tenancy
- https://neon.com/blog/multi-tenancy-and-database-per-user-design-in-postgres • https://neon.com/blog/hipaa-multitenancy-b2b-saas
- https://www.citusdata.com/blog/2016/08/10/sharding-for-a-multi-tenant-app-with-postgres/ • https://docs.citusdata.com/en/stable/articles/sharding_mt_app.html • https://www.citusdata.com/blog/2023/07/18/citus-12-schema-based-sharding-for-postgres/
- https://dohost.us/index.php/2026/06/12/designing-for-multi-tenancy-scalable-data-isolation-patterns-in-postgresql/ • https://www.bytebase.com/blog/multi-tenant-database-architecture-patterns-explained/ • https://clerk.com/blog/what-are-the-risks-and-challenges-of-multi-tenancy • https://dev.to/aloknecessary/designing-multi-tenant-saas-systems-isolation-models-data-strategies-and-failure-domains-261

**Audit & session security**
- https://www.auditpath.io/blog/soc2-audit-log-requirements • https://www.konfirmity.com/blog/soc-2-data-retention-guide • https://hoop.dev/blog/immutable-audit-logs-the-key-to-soc-2-compliance-and-trust • https://auditkit.dev/blog/soc-2-audit-log-requirements • https://auditkit.dev/blog/siem-integration-audit-logs • https://pangea.cloud/blog/audit-logs-what-why-and-how/
- https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html • https://cheatsheetseries.owasp.org/cheatsheets/Logging_Vocabulary_Cheat_Sheet.html • https://owasp.org/Top10/2025/A09_2025-Security_Logging_and_Alerting_Failures/ • https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- https://ssojet.com/blog/critical-audit-log-events-b2b-saas-enterprise • https://ormaos.com/blog/dont-lose-enterprise-deals-audit-logs • https://securityboulevard.com/2026/04/10-critical-audit-log-events-every-b2b-saas-app-should-track-for-enterprise-buyers/ • https://workos.com/blog/enterprise-readiness-checklist-2026
- https://workos.com/docs/authkit/sessions • https://workos.com/blog/why-your-app-needs-refresh-tokens-and-how-they-work
- https://auth0.com/docs/secure/tokens/refresh-tokens/configure-refresh-token-rotation • https://auth0.com/blog/refresh-token-security-detecting-hijacking-and-misuse-with-auth0/ • https://env.dev/guides/jwt-best-practices
- https://supertokens.com/blog/revoking-access-with-a-jwt-blacklist • https://medium.com/@ahmedosamaft/understanding-jwt-revocation-strategies-allowlist-denylist-and-jti-matcher-9d298893f8a1 • https://www.michal-drozd.com/en/blog/jwt-revocation-strategies/
- https://fedresources.com/from-totp-to-phishing-resistant-passkeys-a-guide-to-multi-factor-authentication/ • https://www.wwpass.com/blog/phishing-resistant-mfa-in-2025-buyer-s-guide-to-nist-sp-800-63-4-omb-m-22-09/ • https://hideez.com/blogs/news/phishing-resistant-mfa • https://www.entrust.com/resources/learn/step-up-authentication • https://www.paloaltonetworks.com/cyberpedia/what-is-adaptive-mfa
- https://docs.aws.amazon.com/kms/latest/developerguide/kms-cryptography.html • https://aws.amazon.com/blogs/security/demystifying-kms-keys-operations-bring-your-own-key-byok-custom-key-store-and-ciphertext-portability/ • https://cloud.google.com/kms/docs/envelope-encryption • https://cloud.google.com/secret-manager/docs/cmek • https://developer.hashicorp.com/vault/docs/secrets/transit

---

*Caveat: pricing tiers, markup percentages, conversion-lift figures, and the Supabase RLS microbenchmarks come from vendor/marketing sources and should be treated as directional. Postgres schema/DB ceilings are practitioner-reported degradation points, not hard engine limits. Core structural facts (protocol behavior, entity names, isolation tradeoffs, OWASP/NIST/KMS mechanics) are cross-corroborated.*
