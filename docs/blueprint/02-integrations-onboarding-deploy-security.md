# agent-os SaaS — Master Design: Integrations, Minimal-Info Onboarding, Deploy/Infra, Multi-Tenant Security

**Purpose:** Concrete, exhaustive design feeding the master product blueprint for productizing agent-os into a multi-tenant enterprise SaaS where the customer is "CEO" and an AI orchestrator ships their apps/businesses. The signature requirement — **minimal-info, AI-assisted, confirm-and-go** — is the spine of every flow below.

**Date:** 2026-06-25. API shapes/prices drift; re-verify the flagged items at implementation time.

**How this connects to what already exists in agent-os** (do not rebuild; extend):
- Per-tenant **vault** (envelope-encrypted, frontdoor stores BYO LLM key) — Section D formalizes it.
- **BYO-key per-tenant routing** (`factory.agent` runs `claude -p` with tenant `ANTHROPIC_API_KEY`) — Section B.2 generalizes to multi-provider + cost attribution.
- **`appguard.py`** spend_cap + loss_limit auto-pause — Section B.5 makes this the universal spend-cap enforcement layer for ALL provisioning.
- **Approval-gated actions** in `responder.py` (spend/deploy/secrets/data ESCALATE, not act) — Section B.5 is the confirmation pattern that governs them.
- **Plan-only Terraform** skeleton in `platform/terraform/` + **1:1 local→cloud via config** goal (CLOUD-MIGRATION.md) — Section C builds the deploy automation on top.
- **`sanitize.py`** (prompt-injection untrusted-data wrapping) + **`redact.py`** (secret scrubbing at trace-write) + **`srt` sandbox** (network-denied untrusted code) — Section D.4 threat model extends these to the integration surface.
- **`frontdoor.py`** self-serve front door — becomes the host for the onboarding flows in Section B.

---

## The One Doctrine (applies to every integration and flow)

Every provisioning flow is the **same state machine** with different connectors:

1. **Collect the absolute minimum** the user must type — often just "yes" + one identifier (a domain, an email, a company name).
2. **Hand off to a provider-hosted flow** for anything regulated/sensitive (Stripe KYC, OAuth consent, cloud console) so the platform **never touches the hard data**.
3. **Verify out-of-band** — webhook / status poll / validation endpoint. **Never trust the redirect-back** as proof of success.
4. **Gate consequential actions** behind a single confirmation stating *what · cost · reversibility · blast radius*.
5. **Tier autonomy by cost & reversibility** — read-only and idle-≈$0 actions run autonomously; continuous-burn or irreversible actions require explicit approval and a teardown plan.

Two cross-cutting facts that shape the whole catalog:

- **The modern credential shape is universal:** *customer creates the identity → grants a scoped role/consent → the SaaS exchanges its OWN identity for short-lived credentials.* Never long-lived shared secrets where avoidable. Build the catalog abstraction around the tuple **`(credential_type, validation_endpoint, scope, human_gate)`**.
- **The human gate is always consent/KYC, never plumbing.** Stripe identity verification, Google sensitive-scope verification, Azure admin consent, GitHub install repo-selection. The agent drives everything *up to* that gate, then hands off a hosted flow. Architect for this — don't try to automate the gate away.

---

# SECTION A — INTEGRATIONS CATALOG & CONFIG

Each entry: **what it's for · minimal info to collect · how to validate · auth/security model · agent autonomy boundary.** The catalog is data-driven: one row per integration in a registry table, each tagged with the tuple above, so the orchestrator handles a new integration by adding a row, not code.

## A.1 Stripe + Stripe Connect (payments for the platform AND for customers' shipped apps)

**For:** (a) the platform billing its own customers; (b) **Stripe Connect** so each customer's *shipped product* can take payments and receive payouts — this is the high-value one.

**Account model (2025–2026):** the legacy `type` param (`standard`/`express`/`custom`) is **deprecated** in favor of explicit **controller properties** on `POST /v1/accounts`:
- `controller[fees][payer]` = `application` (platform pays Stripe fees, recoups from customer) or `account`
- `controller[losses][payments]` = `stripe` | `application`
- `controller[stripe_dashboard][type]` = `express` (recommended) | `full` | `none` (white-label)
- `controller[requirement_collection]` = `stripe` (**Stripe runs KYC — PII never touches our servers**) | `application`

**Minimal info to collect:** `country` (required), `email`, business name (prefill `business_profile[url]`/`[product_description]` to drop fields). Everything else (DOB, SSN last-4, beneficial owners, bank account, ToS acceptance) is collected **inside Stripe-hosted onboarding** — we never see it.

**Provisioning (3 API calls + 1 webhook):**
1. `POST /v1/accounts` with controller props + `capabilities[card_payments][requested]=true`, `capabilities[transfers][requested]=true` → returns `acct_…`.
2. `POST /v1/account_links` (`account`, `type=account_onboarding`, `return_url`, `refresh_url` all required, `collection_options[fields]=eventually_due` to minimize return trips). Returns a `connect.stripe.com/setup/…` URL — **single-use, expires in ~5 min, serve only inside our authenticated app, never email/SMS it.**
3. Redirect user once → Stripe-hosted KYC. (Embedded alternative: `POST /v1/account_sessions` + `@stripe/connect-js` `account-onboarding` component for in-app UX.)
4. **Validate** on the `account.updated` webhook — the `return_url` only means "flow exited cleanly," NOT done.

**Validation — "fully onboarded & usable":**
```
charges_enabled == true && payouts_enabled == true && details_submitted == true
&& requirements.currently_due == [] && requirements.past_due == []
&& requirements.disabled_reason == null
```
Gate *charging* on `charges_enabled`, *payouts* on `payouts_enabled`. `requirements.pending_verification` non-empty → wait for Stripe review, not the user.

**Security model:** restricted keys **`rk_`** (per-resource None/Read/Write + a separate Connect permission) as the default, NOT full `sk_`. Act on a connected account via the **`Stripe-Account: acct_…`** header. Mode is the key (`sk_test_`/`sk_live_`); test mode flips `charges_enabled` true with fake KYC (SSN `000-00-0000`) for end-to-end testing. Live requires HTTPS return/refresh URLs + Connect activation.

**Connect an EXISTING account** (vs create new): OAuth — redirect to `connect.stripe.com/oauth/authorize?response_type=code&client_id=ca_…&scope=read_write` → callback `?code=` → `POST /oauth/token` → `stripe_user_id`. Handles both "has account" and "needs one."

**Agent autonomy:** create account, prefill, mint links, poll, handle webhooks = autonomous. **Cannot automate:** human KYC — push entirely to hosted/embedded onboarding.

**MoR contrast (product decision):** raw Connect = the *customer* owns global tax/VAT liability (~2.9%+$0.30). Merchant-of-Record (Lemon Squeezy ~5%+$0.50; **Stripe Managed Payments**, Stripe's own MoR launched at Sessions 2025) makes the platform the legal seller and remits tax. For a "ship my business for me" product, **offer MoR as the default** so customers carry zero tax liability, with raw Connect as an advanced option.

## A.2 OAuth Providers — Google / GitHub / Microsoft (platform login AND app credentials)

Two uses: (1) **platform auth** (CEO logs into agent-os); (2) **credentials the built apps need** (a shipped app's "Sign in with Google"). Bottom line: **GitHub and Microsoft support programmatic OAuth-app creation; Google does not — plan a manual fallback for Google.**

**GitHub** — prefer **GitHub Apps** over OAuth Apps (fine-grained, short-lived, repo-scoped). Programmatic creation via **manifest flow**: form-POST to `github.com/settings/apps/new` with `manifest` → redirect `?code=` → `POST /app-manifests/{code}/conversions` returns `client_id`, `client_secret`, `pem` (private key), `webhook_secret`. Auth chain: RS256 JWT (`iss`=client id, `exp`≤10min) → `POST /app/installations/{id}/access_tokens` (scope down with `repositories`/`permissions`) → token expires in **1 hour**. **Validate:** `GET /user`, or `POST /applications/{client_id}/token` (Basic) → 404 if invalid.

**Google** — **no public API to create OAuth client IDs**; manual via Google Cloud Console. **2025 change:** client secret viewable **only once at creation**. Sensitive/restricted scopes require **verification** (domain ownership + demo video + security assessment); unverified apps capped at **100 lifetime users**. **Validate:** `https://oauth2.googleapis.com/tokeninfo`, `https://openidconnect.googleapis.com/v1/userinfo`.

**Microsoft / Entra** — fully programmatic via Graph: `POST /applications` (`displayName`, `signInAudience`, `web.redirectUris`, `requiredResourceAccess`), `POST /applications/{id}/addPassword` (`secretText` once), `POST /servicePrincipals`. Delegated consent automatable via `POST /oauth2PermissionGrants`; **application permissions require human admin consent**. **Validate:** `GET /me` (delegated) or `GET /oidc/userinfo`.

**Security model:** store `client_secret`/`pem` in the per-tenant vault (Section D). For GitHub Apps, store the **installation** reference and mint 1-hour installation tokens just-in-time — never persist a long-lived token. For Google, the manual fallback is a guided flow: agent generates the exact redirect URIs + scopes, surfaces them, the user pastes back the one-time secret which is immediately vaulted.

## A.3 GitHub / GitLab — repo create + push

**For:** the factory needs to create a repo and push the generated app (today's factory writes to `~/projects/products/<p>/`; SaaS needs per-tenant remote repos).

**GitHub create:** `POST /user/repos` or `POST /orgs/{org}/repos` (`name` required, `private`, `auto_init`). **Push a scaffold (atomic):** Git Data API — `POST .../git/blobs` per file (`encoding:base64` for binaries) → `POST .../git/trees` → `POST .../git/commits` → `PATCH .../git/refs/{ref}`. Simple single-file alt: `PUT /repos/{owner}/{repo}/contents/{path}`.

**Token model — use GitHub App installation tokens** (repo+permission scoped, ~1hr, uninstall = instant revoke, 12,500/hr rate limit) over PATs. **Validate:** `GET /installation/repositories`.

**GitLab create:** `POST /projects` (`name`/`path`, `namespace_id`, `visibility`, `initialize_with_readme`). **Push atomically:** `POST /projects/:id/repository/commits` with `actions[]` of `{action:create, file_path, content, encoding}`. Tokens: project/group access tokens (`POST /projects/:id/access_tokens`, scopes `api`/`read_repository`/`write_repository`). **Validate:** `GET /personal_access_tokens/self`.

**Security model:** GitHub secret **push protection** + the **secret-scanning partner program** (AWS, Stripe, OpenAI, Anthropic are all named partners that auto-revoke leaked keys) is a free safety net — enable it on every tenant repo so generated code that accidentally embeds a secret is blocked at push (mitigates the supply-chain risk in D.4).

## A.4 Cloud providers — AWS / GCP / Azure (deploy into the CUSTOMER's cloud)

**For:** BYOC — deploy the shipped app into the customer's own cloud for data residency/control. The credential model is the security crux (full detail in D.3).

**AWS — cross-account IAM role + ExternalId (gold standard).** Customer creates a role; trust policy names the SaaS account as `Principal` + `Condition.StringEquals."sts:ExternalId"`. SaaS calls `sts:AssumeRole` (`RoleArn`, `RoleSessionName`, `ExternalId`; optional `Policy`/`PolicyArns` to intersect-scope; `DurationSeconds` 900–43200) → temporary creds. **ExternalId is SaaS-generated, unique per tenant, NOT secret** — it prevents the confused-deputy attack. **Minimal customer setup:** a CloudFormation **quick-create "Launch Stack" URL** (`templateURL` + `param_ExternalId` + `param_VendorAccountId`) → customer ticks IAM checkbox → Create → role ARN surfaced as a stack **Output** so nothing is pasted back (Porter's model). **Validate:** `sts:GetCallerIdentity` + a **negative test** (assume *without* the ExternalId must FAIL; 37% of vendors get this wrong — if it succeeds, refuse to store the ARN).

**GCP — Workload Identity Federation (keyless, preferred; SA keys explicitly discouraged).** Pool + provider → token exchange at `sts.googleapis.com` → `iamcredentials…/{SA}:generateAccessToken`. Two grants: `roles/iam.workloadIdentityUser` (federation) + `roles/iam.serviceAccountTokenCreator` (impersonation). **Validate:** `oauth2.googleapis.com/tokeninfo` or a project read.

**Azure — multi-tenant app + admin consent + RBAC.** One app object → SP materialized in customer tenant via admin consent → `az role assignment create --role Reader --scope /subscriptions/.../resourceGroups/...`. Prefer **federated credentials/certs over client secrets**. **Validate:** Graph `GET /me` + ARM subscription read.

**Security model:** never accept long-lived cloud keys; mint short-lived STS/federated tokens just-in-time; scope each session with a session policy (intersection semantics — can only narrow). Do **not** request `ReadOnlyAccess` (over-grants S3 data + SSM secrets) — hand-write the minimal policy.

## A.5 Domains / DNS / SSL

**For:** give the shipped app a real domain with HTTPS. **Closing the DNS loop is the single biggest autonomy unlock** — if the agent owns the registrar/DNS API, domain verification (email/SSL) and wildcard ACME become fully unattended.

**Cloudflare** (`api.cloudflare.com/client/v4`): scoped **API Token** (Bearer; "Edit zone DNS" = Zone.DNS Edit) over Global Key. **Validate:** `GET /user/tokens/verify` (`status:active`). Zones `POST /zones`; records `POST /zones/{zone_id}/dns_records` (`type`, `name`, `content`, `ttl`, `proxied`). Registrar API (2025 beta, TLD-limited): `domain-search`/`domain-check`/`registrations`.

**Route 53:** `CreateHostedZone` → `DelegationSet.NameServers`; `ChangeResourceRecordSets` (`UPSERT`, transactional). `route53domains` (us-east-1 only): `RegisterDomain`, `CheckDomainAvailability`.

**Porkbun** (JSON POST, `apikey`+`secretapikey`, **per-domain API toggle required**) / **Namecheap** (XML, **mandatory IP allowlist + eligibility floor**, `setHosts` is a full-replace → read-merge-write). Porkbun is the friendlier programmatic registrar for an agent.

**SSL — ACME (RFC 8555):** **DNS-01** (TXT at `_acme-challenge`) is the **only challenge that issues wildcards**; HTTP-01 for single hosts. LE prod `acme-v02.api.letsencrypt.org/directory` (rate limit 50 certs/domain/week; staging for tests). Tools: certbot/lego/acme.sh with DNS plugins close the loop via the registrar API.

**Security model:** registrar/DNS tokens are high-blast-radius (can hijack a domain) — vault them with the tightest scope (single-zone Cloudflare tokens, not global keys), and treat domain-transfer/nameserver-change as an irreversible action requiring confirmation.

## A.6 Transactional / email

**For:** the shipped app sends signup/receipt/notification emails.

**Resend** (`Bearer re_…`): send `POST /emails`; `POST /domains` returns a **`records[]`** array (SPF/DKIM/tracking); poll `GET /domains/{id}` (`pending`→`verified`); `POST /domains/{id}/verify`. **Validate key:** `GET /api-keys`.

**Postmark** (PascalCase, **two tokens**: `X-Postmark-Server-Token` send, `X-Postmark-Account-Token` for Domains/Senders): send `POST /email`; `POST /domains` + `PUT /domains/{id}/verifyDkim`+`verifyReturnPath`.

**AWS SES v2:** `GetAccount` → `ProductionAccessEnabled`; **sandbox = 200 msgs/24h, verified recipients only** (production = manual AWS review). `CreateEmailIdentity` → `DkimAttributes.Tokens[]` (3 Easy-DKIM CNAMEs). **Validate:** `GetEmailIdentity` (`VerificationStatus`).

**Closing the loop:** all three verify via SPF (TXT) + DKIM (CNAME/TXT) + DMARC (TXT). If the agent owns DNS (A.5), it adds records and polls the verify endpoint → **fully autonomous**. Otherwise it surfaces the exact records.

## A.7 Notifications — Slack / Discord / ntfy

**For:** ops alerts to the customer; the shipped app's own notifications. (agent-os already uses **ntfy** as the phone bridge — this generalizes it per-tenant.)

**Slack:** Incoming Webhook `POST hooks.slack.com/services/…` (channel-locked, send-only) via app + `incoming-webhook` scope; or bot token `xoxb-` → `chat.postMessage` (`chat:write`) for multi-channel/edit. **Validate:** `auth.test`.

**Discord:** Execute webhook `POST discord.com/api/webhooks/{id}/{token}` (`content`≤2000, `embeds`≤10). Create via `POST /channels/{id}/webhooks` (Bot token + MANAGE_WEBHOOKS). Rate ~30/min.

**ntfy:** the **topic is the only credential**. `POST ntfy.sh/{topic}` (body=message; headers `X-Title`/`X-Priority`/`X-Tags`/`X-Actions`; optional `Authorization: Bearer tk_…`). Self-hostable, no OAuth — the lowest-friction option and what agent-os already runs.

## A.8 Analytics

**PostHog:** `phc_` project key (public, client ingestion) → `/i/v0/e/`; `phx_` personal key (Bearer) for management (`POST /api/organizations/{org}/projects/`). Regions us/eu.
**Plausible:** `<script data-domain>`; Sites Provisioning API `POST /api/v1/sites` (Bearer scoped `sites:provision:*`, Enterprise on cloud).
**Segment:** Write Key (HTTP Basic) → `/v1/track`; source creation via Public API `POST api.segmentapis.com/sources` (Team/Business tier).

**Agent autonomy:** project/site creation is autonomous once a management key exists; injecting the tracking snippet into the generated app is autonomous (it's code-gen).

## A.9 Databases (programmatic provisioning)

**Neon** (`Bearer`): `POST /projects` → `connection_uris` + generated role password; branches/databases/roles. **Copy-on-write branching** (O(1), <1s) is the standout for per-PR preview parity. **Validate:** `GET /projects`.
**Supabase** (`Bearer sbp_…`): `POST /v1/projects` (`name`, `organization_id`, `db_pass`, `region`); poll `GET /v1/projects/{ref}/health`. **2025:** `sb_publishable_`/`sb_secret_` replace `anon`/`service_role`.
**PlanetScale** (auth `<TOKEN_ID>:<TOKEN>`, **not Bearer**): `POST /organizations/{org}/databases` (`kind` mysql|postgresql) → branches → passwords (`plain_text` once). **No free tier** ($5/mo Postgres entry). Safe migrations via deploy requests (shadow tables, non-blocking cutover).

**Connects to agent-os:** the live stack already uses **Postgres+pgvector**; for SaaS, each tenant's app gets its own Neon/Supabase project (full isolation) or a schema in a shared cluster (cheaper) — the bridge model.

## A.10 Vector stores

**Pinecone:** control plane `api.pinecone.io` (`Api-Key`), data plane per-index `host`. `POST /indexes` (`dimension`, `metric`, `spec.serverless.{cloud,region}`). 2025 integrated embedding: `create-for-model`/`/embed`/`/rerank`.
**Weaviate:** Bearer API key or OIDC; `POST /v1/schema` (`vectorizer`, `vectorIndexType`); GraphQL search.
**Qdrant:** header `api-key`; `PUT /collections/{name}` (`vectors.{size,distance}`); unified `points/query` (fusion `rrf`/`dbsf`).
**pgvector:** no separate credential — `CREATE EXTENSION vector; vector(N)` (≤2000 indexable dims), `USING hnsw (… vector_cosine_ops)`. **Ties directly to agent-os's existing pgvector** — the default for tenant apps that need vectors, no new integration.

## A.11 MCP tool servers

**For:** giving agents (and the shipped apps' agents) governed tool access. agent-os is Claude-first; MCP is how external tools attach.

**Spec:** latest **`2025-06-18`** (server = OAuth 2.1 Resource Server, mandatory RFC 9728 Protected Resource Metadata + RFC 8707 Resource Indicators, **token passthrough forbidden**). Sent as `MCP-Protocol-Version` header. Transports: stdio (local, creds via env), **Streamable HTTP** (single endpoint, `Mcp-Session-Id`, resumable SSE), legacy HTTP+SSE (deprecated).
**Config (`mcpServers`/`.mcp.json`):** stdio = `command`/`args[]`/`env`; remote = `type:"http"`/`url`/`headers` (`Authorization: Bearer ${API_KEY}`), variable expansion `${VAR:-default}`.
**Auth:** unauthenticated → 401 + `WWW-Authenticate` → `/.well-known/oauth-protected-resource` → `/.well-known/oauth-authorization-server` → optional dynamic client registration → auth-code + PKCE + **`resource` param (RFC 8707 audience binding)**. Simpler: API keys via env/headers.
**Catalog registration (`server.json`):** reverse-DNS `name`, `packages[]`/`remotes[]`, `environmentVariables[].isSecret` — **secrets are *declared*, not embedded.**

**Security model (critical — see D.4):** **never pass a client token through to an upstream API** (audience-confusion confused deputy, CVE-2025-6514 in `mcp-remote`); enforce per-client consent; tool **allowlists**; beware **tool-poisoning** (malicious instructions hidden in MCP tool descriptions, read by the model as trusted).

---

# SECTION B — MINIMAL-INFO, AI-ASSISTED ONBOARDING & PROVISIONING FLOWS

All flows run on the **frontdoor** host. Each is the One Doctrine state machine. The **confirmation/approval pattern (B.5)** governs every consequential step.

## B.1 "I want Stripe — set it up"

```
User: "I want Stripe — set it up."

Agent (autonomous up to the gate):
  1. POST /v1/accounts  (controller props, country inferred from tenant profile, email prefilled)
  2. POST /v1/account_links (type=account_onboarding, our return/refresh URLs)
  3. Render a single button → opens Stripe-hosted onboarding.

Agent → user:
  "I'll create a Stripe account for {Company} and open Stripe's secure setup so you can
   verify your identity and add a payout bank account. Stripe handles the sensitive data —
   I never see your SSN or bank details.
   → [Set up payments with Stripe]
   I'll switch payments on automatically once Stripe confirms verification."

[User completes KYC inside Stripe. Returns to our app — we do NOT trust the return.]

Agent (on account.updated webhook, charges_enabled && payouts_enabled && currently_due==[]):
  "✅ Stripe is live. You can now accept payments and receive payouts."

If requirements.currently_due re-populates later (volume thresholds):
  Agent mints a new account_update link and pings: "Stripe needs one more detail to keep
   payouts flowing → [Update Stripe]."
```
**Minimal info from user:** company name (already known) + "yes." Test vs live handled by which key set the tenant is in; we run an end-to-end test charge in test mode before flipping live.

## B.2 "Bring your own API keys" (Anthropic / OpenAI / others)

**This generalizes agent-os's existing BYO-key routing to multi-provider with validate-on-paste and cost attribution.**

```
User: "Use my own Anthropic key."

Agent:
  "Paste your Anthropic API key. I'll verify it instantly and store it encrypted
   (I'll only ever show the last 4 digits). Tip: create it in a dedicated workspace so
   it's scoped to just this app — and never give me an Admin key."

[On paste → validate cheaply, ZERO token cost:]
  Anthropic: GET https://api.anthropic.com/v1/models
             headers x-api-key:<KEY>, anthropic-version:2023-06-01   (200=valid, 401=invalid)
  OpenAI:    GET https://api.openai.com/v1/models  Authorization: Bearer <KEY>
             (200=valid, 401=invalid auth, 403=scoped/region-blocked)

[On 200:]
  - Envelope-encrypt (Section D), store fingerprint/hash for lookup + last-4 for display.
  - Mark provider routing for this tenant.

Agent → user:
  "✅ Key verified (sk-…AbCd) and encrypted. Anthropic will bill this key directly —
   you won't see token charges from us."
```
**Cost implication (the big one):** true BYOK means we **stop metering tokens for billing** — the provider bills the customer. Monetize via **seat/subscription fee** (or a thin per-request passthrough, the OpenRouter model: 5% fee, waived first 1M req/mo). Keep metering tokens for **analytics/quotas/`appguard`**, not invoicing. This cleanly resolves the agent-os honest gap ("token-cost not in build cost") for BYOK tenants: it's the customer's cost, surfaced for transparency, not ours.

**Scoping guidance to give customers:** OpenAI → project-scoped **Restricted** key (service account in prod); Anthropic → dedicated **workspace** key (workspace-scoped is the natural isolation boundary), **never an Admin key** (`sk-ant-admin…`).

**Provider-agnostic loop:** to close the OpenAI/DeepSeek BYO gap noted in memory, the factory's agent loop needs the **codex provider adapter** (already scoped as next work) so `factory.agent` can run `claude` OR `codex` with the tenant's chosen provider key.

## B.3 "Can you deploy to AWS?" (or platform-managed)

```
User: "Can you deploy to AWS?"

Agent offers two paths:
  (A) We host it — fastest, runs on our managed infra, nothing to set up.
  (B) Your AWS account (BYOC) — runs in your VPC, you keep full control/audit.
      One-time setup: click a 'Launch Stack' link that creates a scoped IAM role
      granting deploy access (no access keys shared).

[User picks B:]
Agent generates a per-tenant ExternalId + a CloudFormation quick-create URL:
  https://<region>.console.aws.amazon.com/cloudformation/home#/stacks/create/review
    ?templateURL=<our role template>&param_ExternalId=<id>&param_VendorAccountId=<acct>
  "Click → tick the IAM checkbox → Create. The role ARN comes back automatically."

[User completes. Agent validates:]
  - sts:GetCallerIdentity via AssumeRole (with ExternalId) → success.
  - NEGATIVE TEST: AssumeRole WITHOUT ExternalId → must fail. If it succeeds, REFUSE.

Agent (plan-then-apply — the consequential gate):
  "Role verified ✅ (and I confirmed it correctly requires the ExternalId).
   Here's the deployment plan before I touch anything:
     + ECS service 'acme-api' (2 tasks, Fargate)
     + Application Load Balancer
     + RDS Postgres db.t4g.medium
     ~ Security group (1 ingress rule)
     Est. cost: ~$140/month
   Apply this plan to YOUR AWS account?  [Apply] / [Edit] / [Cancel]"

[On Apply → terraform apply tfplan (the exact reviewed plan; stale plan is refused).]
```
**IaC:** **Terraform/OpenTofu** is the safe default (distributable modules, true read-only `plan`, customer-reviewable; OpenTofu sidesteps BSL). AWS CDK pairs naturally with Launch-Stack onboarding (compiles to CFN). Pulumi when per-tenant logic is genuinely programmatic. **`terraform plan -out=tfplan` → review → `apply tfplan`** is the canonical agent confirmation primitive. **This extends agent-os's existing plan-only `platform/terraform/` skeleton** into the live deploy path.

**Never auto-`destroy`.** Teardown is always confirmed.

## B.4 "Rent me GPU instances"

**Autonomy splits cleanly by billing model:**

| Tier | Examples | Idle cost | Agent behavior |
|---|---|---|---|
| **Serverless / scale-to-zero** | Modal functions, Replicate public `run()`, Fal endpoints, RunPod **Flex** | ≈ $0 | **Autonomous** — just run it, report the per-run estimate |
| **Dedicated / continuous-burn** | RunPod Pods, Replicate `min_instances>0`, RunPod Active workers, EC2, Capacity Blocks | $$/hr 24/7 | **Confirm + auto-teardown plan** |

```
User: "Run this image model on 200 prompts."
Agent (serverless, idle≈$0 → just does it):
  "Running on Fal serverless (scales to zero when idle). ~200 × ~$0.03 ≈ $6 total. Starting."

User: "Rent me an H100 box for fine-tuning."
Agent (dedicated, continuous burn → CONFIRM):
  "Heads up — a dedicated H100 bills continuously until torn down:
   • RunPod Pod H100: ~$2.89/hr (~$69/day if left running)
   • Estimated job: ~6 hrs ≈ $17, but it keeps billing if idle.
   I'll auto-terminate when the job finishes.
   Provision the H100?  [Yes, with auto-teardown] / [No]"
```
**How the agent estimates cost** (no provider exposes a true pre-flight quote API): look up the published per-sec/hr rate at request time (don't trust cache) → `serverless: price_per_sec × expected_duration`; `dedicated: hourly_rate × expected_hours` → classify blast radius → present total + worst-case idle. **Auto-teardown is mandatory** for dedicated tiers (a 4-agent loop once ran 264 hrs to a **$47,000** bill for lack of a ceiling). **Connects to agent-os:** this is exactly the deferred GPU/paid capability in the skills registry — BYO hosted-API key (Replicate/Fal) is the autonomous path; cloud GPU is the confirm path. `appguard` spend caps (B.5) enforce the ceiling.

## B.5 ⭐ The Confirmation / Approval Pattern (the spine)

**Design law:** *read-only and in-scope actions auto-run; consequential, costly, or irreversible actions pause for an approve / edit / reject decision — and the decision can be made sticky (allow-list) so the gate only fires where it earns its interruption.*

Every major agent framework implements the **same skeleton** (propose → policy decides → pause if not auto-approvable → approve/edit/reject → resume), differing only in field names:

| Framework | Mark for approval | Pause / surface | Resume with decision | Sticky |
|---|---|---|---|---|
| LangGraph | `interrupt_on`/`when` predicate | `interrupt()` | `Command(resume=…)` | per-tool config |
| Claude / Claude Code | `permissions.ask` rules / mode | `tool_use` block | return `tool_result` | `permissions.allow`, "allow for session" |
| Bedrock Agents | per-action confirmation | `returnControl` | `confirmationState: CONFIRM\|DENY` | — |
| Vercel AI SDK 6 | `needsApproval` (bool/fn) | `tool-approval-request` | `addToolResult` | remember approved patterns |
| OpenAI Agents | `needsApproval`/`requireApproval` | `RunToolApprovalItem`/`interruptions` | `state.approve()/reject()` | `alwaysApprove/alwaysReject` |

**agent-os mapping:** this is **already partially built** — `responder.py` ESCALATES (vs acts) on spend/deploy/secrets/data, and the voice-in path has gated-action approval. Formalize it as a single **policy gate** every tool call passes through:

- **Tier by `(cost, reversibility, blast_radius)`**: auto-approve below a per-tenant threshold; escalate above. Expressed exactly as Vercel/OpenAI `needsApproval(input)` or LangGraph's `when` predicate.
- **Plan-then-apply** (the Terraform model, Claude Code `plan` mode): structured plan → one approval → deterministic execution; refuse a stale plan.
- **Spend caps = enforcement, not monitoring** — intercept each call against a ceiling *before* it goes out and terminate at the limit. **This IS `appguard.py`** (spend_cap/loss_limit auto-pause) — promote it from per-app to the universal pre-flight gate for ALL provisioning (GPU, cloud, paid APIs).
- **Confirmation UX:** restate the action + consequences ("Provision H100, ~$69/day if idle"), specific button labels (not Yes/No), match friction to impact (prefer **Undo** for reversible actions; type-to-confirm for irreversible), use sparingly (that's why sticky allow-lists exist). Anthropic's guidance: checkpoint **before irreversible actions** (financial, deletion) + surface the agent's own uncertainty.

**Per-scenario application:**

| Scenario | Autonomous where safe | Confirm where costly/irreversible |
|---|---|---|
| Stripe | create account, mint link, poll webhooks | (user self-confirms inside Stripe KYC) |
| BYO keys | validate, encrypt, store, show last-4 | (low stakes — no gate) |
| AWS deploy | read state, `plan`, validate role (+ no-ExternalId check) | **`apply`** (show plan+cost); never auto-`destroy` |
| GPU | serverless `run()` (idle≈$0) | **provision dedicated** (show $/hr + idle worst-case); auto-teardown |

---

# SECTION C — DEPLOY & INFRA AUTOMATION (build → live URL)

## C.1 The orchestrator's pipeline (every PaaS uses this)
**Detect** (scan marker files: `package.json`/`requirements.txt`/`next.config.js`/`Dockerfile`) → **Build** (immutable OCI image preferred) → **Release + URL** (push to runtime, auto subdomain, auto-TLS, optional custom domain) → **Wrap** (git-push-to-deploy, previews, secrets, autoscaling, observability, rollback).

## C.2 Build → artifact
Default to **Cloud Native Buildpacks** (CNB) → reproducible OCI image, no Dockerfile, SBOM at export. Use **Railpack** (Railway's 2025 Nixpacks replacement — Go + mise + BuildKit LLB, images 38–77% smaller; **lesson: pin/lock dep versions at build**) or Nixpacks for zero-config speed; detect/generate a Dockerfile as fallback (Fly's `dockerfile-node` model). **agent-os fit:** the factory already produces static web apps and multi-module HTTP+SQLite services — wrap each `products/<p>/` output in CNB build → OCI image as the portable artifact.

## C.3 Runtime — managed default vs BYOC
- **Managed default:** **Google Cloud Run** or **Fly Machines** — immutable revisions, **scale-to-zero**, request-based billing, **instant rollback by traffic reassignment**. Cloud Run wakes only on inbound HTTP; Fly `auto_stop="stop"` + `min_machines_running=0`.
- **BYOC:** **Northflank** (single control plane, cross-account links, broadest cloud + GPU + microVM sandboxes) to host customer apps; **Nuon** (zero-access egress-only Runner running Terraform+Helm inside the customer account) to ship *our product* into customer accounts. **Keep the control plane OUT of the request path** — that's the "genuine BYOC" test.
- **Control plane / data plane split:** UI/API/scheduler/pipeline in our cloud; VPC/workloads/DBs/secrets in the customer's. **Crossplane** is the best-fit reconciling control plane if we build BYOC orchestration as a platform.

## C.4 CI/CD
git-push-to-deploy via webhook → **GitHub Actions + OIDC** (keyless: exchange GitHub JWT for temp cloud creds via `AssumeRoleWithWebIdentity`, no static keys; trust policy scopes the `sub` claim to repo/branch/env). Immutable artifacts + **instant rollback by re-pointing traffic/domains** (no rebuild). **Argo Rollouts** (K8s, metric-driven auto-promote/rollback) or **Cloud Run traffic tags** / **ECS-native blue-green** (gained canary+linear Oct 2025, now AWS's recommended path) for progressive delivery. **agent-os fit:** the factory's LAUNCH stage (gated on green QA) becomes the trigger for the deploy pipeline; crash-resume trace checkpoint already gives idempotent re-runs.

## C.5 Preview environments
Per-PR ephemeral deploy (Render previews / Vercel / Cloudflare per-branch URLs GA July 2025 / **vcluster** for K8s) + **Neon copy-on-write DB branch** (O(1), <1s, auto-injected `DATABASE_URL` per preview — the standout for per-PR *data* parity). Auto-post preview URL to the PR; teardown on merge/close.

## C.6 Horizontal scaling
The agent-os **scale story already holds** (memory: work queue = Postgres `tasks` + `FOR UPDATE SKIP LOCKED` → N workers on N machines, same `DATABASE_URL`, no second broker). For the *shipped apps*: **Cloud Run**/**Knative** (request-based, scale-to-zero) for HTTP; **KEDA** (70+ scalers — Kafka/SQS/cron/Postgres) for non-HTTP/queue/cron work (the key differentiator: Cloud Run wakes only on HTTP). **Footgun:** never run HPA+VPA on the same dimension (oscillation death spiral) — HPA on CPU, VPA on memory. **Gateway API** supersedes Ingress (L4+L7).

## C.7 Secrets in deploy
**Inject at runtime, never bake at build** — except public `NEXT_PUBLIC_*`/`VITE_*` which are **frozen into the bundle at build** (the dominant footgun; promoting one image across envs requires runtime-config for these). Source of truth: **Infisical/Doppler** (dynamic DB creds, real-time sync) → runtime; **ESO + SOPS** on Kubernetes. Per-environment scoping. **agent-os fit:** the per-tenant vault (Section D) is the source of truth; deploy injects decrypted secrets as runtime env into the runtime, never into build logs/image layers (Render's model — AES at rest, never in build output).

## C.8 Observability hooks (wire automatically)
**OTel SDK → OTel Collector** (swap exporter per env, no re-instrumentation) + **Sentry DSN + CI source-map/release automation** + structured **JSON logs** (OTel Log Bridge to wrap existing loggers) + liveness/readiness/startup probes + **cause-based alerts**. Backend: Grafana LGTM (cost), Honeycomb (high-cardinality debugging), or Datadog (breadth). **agent-os fit:** the dashboard + watchdog + heartbeats + trace.py already ARE an observability plane for the *factory*; wire the same OTel pipeline into *shipped apps* so the customer-CEO sees their app's health in the same mission-control.

## C.9 The 1:1 local→cloud portability goal (swami cares a lot)
**This is already the agent-os thesis** (CLOUD-MIGRATION.md: local→cloud ≈ 1:1 config — `DATABASE_URL` + service URLs; only non-config steps = drop-in S3 objstore + cloud-KMS vault behind existing interfaces). Formalize with the industry bedrock:
- **Twelve-Factor:** config in env; backing services as **attached resources** (local Postgres ↔ RDS = a config-only swap); dev/prod parity.
- **Dev Containers** (`devcontainer.json`) + **Docker Compose** for local; **Dagger** for **identical local/CI pipelines** (same pipeline runs on laptop and in CI — directly kills "works on my machine").
- **Neon/PlanetScale branching** for DB parity per-PR.
- **Eliminate environment-specific code paths — everything varies by config only.** This is the portability invariant the `inventory.yaml` drift-guard already enforces; extend the drift-guard to assert no hard-coded env-specific values in shipped apps.

---

# SECTION D — SECRETS & SECURITY (multi-tenant)

## D.1 Per-tenant secret vault — envelope encryption (formalizes the existing vault)
Four layers:
1. **KMS envelope encryption.** `GenerateDataKey` (`KeyId=alias/customer-<tenant>`, `KeySpec=AES_256`, `EncryptionContext`) → plaintext DEK + wrapped DEK. AES-256-**GCM** the secret with the DEK; persist `{ciphertext, iv, auth_tag, wrapped_dek, kek_key_id, encryption_context}`; **zero the plaintext DEK immediately**. Read = `Decrypt` with identical `EncryptionContext` (mismatch → fails). **Never persist a plaintext DEK.**
2. **Isolation via the bridge model** — shared secrets table (pool) + **per-tenant KMS KEK** (silo at the key layer). AWS official rule: **one KMS key per tenant, shared across that tenant's services** (~$1–3/mo each; hard cap ~100k keys/Region) — NOT key-per-tenant-per-service. Single shared KEK + encryption context only when tenant count makes per-tenant keys impractical.
3. **Encryption context + ABAC** — bind ciphertext to `{tenant_id, purpose, provider}` (non-secret AAD, appears in CloudTrail for audit). Enforce `kms:EncryptionContext:tenant_id == aws:PrincipalTag/tenant_id` (tenant tag from STS session/JWT). A decrypt request for tenant A cannot decrypt tenant B's data.
4. **Rotation + dynamic secrets** — Secrets Manager `tenant/<id>/<provider>` + managed/Lambda rotation; prefer **Vault dynamic secrets** (lease-based, auto-revoke) + **Transit** (`derived=true`+`context` for per-tenant keys without storing data). Vault **namespaces** for silo-grade isolation.

**Non-AWS equivalents:** GCP **CMEK** + Cloud KMS (disabling the key = tenant kill-switch); **Vault Transit** (one master KEK deriving billions of per-tenant keys, ~0.5ms p50 — proven at Ariso.ai). For "**operator can't read the key**": AWS **XKS/HYOK** (key material never leaves the customer HSM) or Snowflake-style **Tri-Secret** (customer CMK revocation makes data undecryptable).

**agent-os fit:** the existing frontdoor vault is the right shape; this defines the production backend (per-tenant KEK + encryption context). The **redact.py** scrubber (already masks `sk-`/`aos_`/`AKIA`/bearer/DB-url at trace-write) is the logging-side complement — keep it mandatory.

## D.2 BYO-key handling
Wrap each tenant key with its per-tenant KEK; **decrypt only in-memory at point of use, in isolated workers** (Portkey pattern). **Display last-4 only; store a fingerprint/hash** so you never decrypt just to identify. **Never log full keys** (OWASP). **The quiet leak:** AI gateways (LiteLLM, Cloudflare AI Gateway) **persist request/response payloads by default** — disable payload logging (`disable_spend_logs`/`disable_error_logs`) or it leaks. Audit secret access with **HMAC'd accessor IDs** (Vault model — who/what/when/which-secret, without leaking the secret).

## D.3 Least-privilege cross-account roles
- **AWS:** cross-account role + **server-generated, unpredictable, per-customer ExternalId** (confused-deputy prevention; it is **NOT secret** — security comes from trust-policy enforcement + unpredictability). **Backend-validate:** assume-without-ExternalId must FAIL or refuse the config (37% of vendors get this wrong). Scope down with **session policies** (intersection — can only narrow), short-lived **STS** (15min–12hr; role chaining caps at 1hr). **Hand-write the minimal policy — never `ReadOnlyAccess`** (over-grants S3 data + SSM secrets).
- **GCP:** **Workload Identity Federation** (keyless; SA keys discouraged) + attribute conditions (CEL).
- **Azure:** **Federated Identity Credentials** / **Lighthouse** (cross-tenant management without shared creds; customer revokes anytime); prefer certs/federation over client secrets.

## D.4 Threat model — what goes wrong & mitigations (agent-specific emphasis)
Anchor on **OWASP LLM Top 10 (2025)** + **OWASP Agentic Top 10 (ASI01–10, Dec 2025)**. **The dominant real-world failure is the agent itself** — prompt injection turning a credential-holding agent into an exfiltration/destruction tool is now CVE-tracked.

**The single most useful heuristic — the "lethal trifecta"** (Simon Willison): danger = (1) access to private data + (2) exposure to untrusted content + (3) external communication. An agent with all three can be tricked into exfiltrating secrets. **Meta's "Rule of Two":** an unsupervised agent may satisfy **at most two of the three**; all three requires a human in the loop. Prompt injection is **not fully solvable today** — manage it architecturally (least-agency + isolation + HITL), not with a filter.

| Threat | Concrete mitigation | agent-os hook |
|---|---|---|
| Cross-tenant data leakage | Per-tenant DEK/KEK; encryption context + ABAC; Vault namespaces; SCP-governed tags | D.1 vault |
| Confused deputy (cross-account) | Server-gen unpredictable per-customer ExternalId; backend negative-test; never accept IAM users | A.4/B.3/D.3 |
| Confused deputy (MCP/agent) | **No token passthrough**; audience-check OAuth tokens; per-client consent; one resource per session | A.11 |
| **Prompt injection → secret exfil** | Lethal-trifecta budget (≤2 of 3); strip secrets from context/tool-responses; **egress allowlist**; block markdown image/link exfil; sandbox | **`sanitize.py`** (already wraps untrusted charter as DATA, proven no key leak) |
| Excessive agency / destructive action | Least privilege per tool; **HITL for high-impact**; read-only defaults; dev/prod separation; planning-only mode | **`responder.py`** ESCALATE + B.5 gate |
| SSRF / metadata theft (`169.254.169.254`) | IMDSv2 required + hop-limit 2; deny-egress-by-default; post-DNS CIDR blocklist; **block IPv6 metadata too** | `srt` sandbox (network DENIED) — extend to egress allowlist for tools that DO need net |
| Supply chain (gen code w/ secrets, bad packages) | **GitHub push protection + secret scanning** (AWS/Stripe/OpenAI/Anthropic auto-revoke); pin/verify packages; treat agent skills/PRs as privileged code; scoped CI tokens | A.3 + `security_scan.py` |
| Key leakage in logs/LLM context | Redaction denylists; disable gateway payload logging; HMAC accessor IDs; ZDR with providers | **`redact.py`** (proven masked in storage) |
| Over-privileged cloud role | Session policies; permission boundaries; minimal hand-written policy; short STS | D.3 |
| Secret sprawl / stale creds | Dynamic/short-lived secrets; auto-rotation; leaked-key auto-revocation | D.5 |

**Real 2025 incidents (proof it's not theoretical):** EchoLeak (CVE-2025-32711, zero-click M365 Copilot exfil via markdown image); Claude Code DNS exfil (CVE-2025-55284, `.env` out via DNS — fixed by tightening the command allowlist); GitHub MCP toxic-flow (public issue → private-repo leak → **one repo per agent session**); Amazon Q wiper (over-scoped CI token → destructive PR shipped to ~1M devs); Replit prod-DB deletion during a freeze (→ hard dev/prod separation + immutable backups + HITL on destructive ops).

**Sandboxing tiers:** standard Docker/runc shares the host kernel and is **insufficient for untrusted agent code** — minimum production isolation is a **Firecracker/Kata microVM** (own kernel, ~125ms boot), with **gVisor** as a lighter middle ground. **Claude Code's own model** (bubblewrap/Seatbelt + a unix-socket network proxy enforcing a domain allowlist, covering bash subprocesses) is the reference. **agent-os already runs untrusted generated code under `srt` with network DENIED** — for SaaS scale, graduate to microVM-per-tenant-build + egress allowlist proxy.

## D.5 Rotation & lifecycle
Prefer **short-lived over long-lived** (a 15-min token vs a 90-day key ≈ 8,640× smaller exposure window; AWS Well-Architected rates skipping this **High** risk). Auto-rotation: Secrets Manager managed/Lambda (as often as every 4hr), GCP Pub/Sub `SECRET_ROTATE`, **Vault dynamic** (per-request, auto-revoke at lease expiry — eliminates sprawl). **Leaked-key auto-revocation:** GitHub **push protection** + the **secret-scanning partner program** (AWS, Stripe, OpenAI, Anthropic all auto-revoke detected keys). Context: ~29M secrets leaked to public GitHub in 2025 (+34% YoY), AI-service creds +81%, **64% of 2022-leaked secrets still active** — most leaked creds are never rotated, so detection + short-lived creds are the real defense.

## D.6 Compliance hooks (enterprise)
SOC 2 **CC6** (logical access — encryption + key management; "no plaintext secrets in repos/.env; use a secrets manager") and **CC7** (audit logging of secret access). Auditors increasingly require **distinct attributable identities for AI agents**. Data residency: KMS keys / Secrets Manager / CMEK are Region-scoped; Vault namespaces / per-region clusters isolate by geography. **agent-os fit:** the per-tenant trace isolation + retention controls already flagged as "remaining for a sellable debugger" are exactly the CC7 evidence; the snapshot/provenance system (`.aosnap`) is acquisition-grade audit material.

---

# CONSOLIDATED BUILD PRIORITIES (what to build next, grounded in current agent-os state)

1. **Catalog abstraction** — one registry table keyed on `(credential_type, validation_endpoint, scope, human_gate)`; each integration = a row, the orchestrator handles new ones by config. (Mirrors the pre-built-org philosophy: selection, not creation.)
2. **Universal policy gate** — promote `appguard.py` + `responder.py`-ESCALATE into ONE pre-flight confirmation gate every consequential tool call passes through, tiered by `(cost, reversibility, blast_radius)`, with sticky allow-lists. This is the literal embodiment of "confirm-and-go."
3. **Stripe Connect onboarding flow** — highest commercial value (lets customers' shipped apps take money); MoR default.
4. **Multi-provider BYO-key** — finish the codex provider adapter so BYOK covers OpenAI/DeepSeek, not just Anthropic; stop metering tokens for BYOK billing (seat fee instead).
5. **Deploy pipeline** — CNB → OCI → Cloud Run (managed) / Northflank (BYOC); GitHub Actions + OIDC; extend the plan-only Terraform skeleton into live `plan→confirm→apply`; never auto-destroy.
6. **Production vault backend** — per-tenant KEK + encryption-context ABAC behind the existing vault interface; keep `redact.py` mandatory; HMAC'd secret-access audit log (CC7).
7. **Sandbox graduation** — microVM-per-build + egress-allowlist proxy for tools that need network, extending the existing `srt` network-denied default; enforce the lethal-trifecta Rule-of-Two on agent tool grants.
8. **DNS-loop ownership** — own a registrar/DNS API (Cloudflare token per tenant) → domain + email verification + wildcard ACME become fully unattended (the biggest single autonomy unlock).

---

*Source research (full citations inline in each):*
- `research/deploy-infra-automation-2026.md`
- `research-onboarding-provisioning-flows.md`
- `secrets-security-multitenant-agent-saas-research.md`
- (integrations catalog — captured in this document, Section A)
