# Minimal-Info, AI-Assisted, "Confirm-and-Go" Onboarding & Provisioning Flows

**For a multi-tenant SaaS where an AI orchestrator provisions services for customers**
Research date: June 2026. Prices/API shapes change — re-verify at provision time.

---

## TL;DR — The Operating Doctrine

Every flow below is the same machine with different connectors:

1. **Collect the absolute minimum** the user must type (often just "yes" + one identifier).
2. **Hand off to a provider-hosted flow** for anything regulated/sensitive (Stripe KYC, AWS console, OAuth) so you never touch the hard data.
3. **Verify out-of-band** via webhook / status poll / validation endpoint — don't trust the redirect-back.
4. **Gate consequential actions** behind a single confirmation that states *what*, *cost*, *reversibility*, and *blast radius*.
5. **Tier autonomy by cost & reversibility**: read-only and idle-$0 actions run autonomously; continuous-burn or irreversible actions require explicit approval and a teardown plan.

The unifying pattern across every agent framework (LangGraph, Claude tool use, Bedrock Agents, Vercel AI SDK, OpenAI Agents) is identical: **model proposes a tool call → policy decides if auto-approvable → if not, pause & surface a confirm → human approves/edits/rejects → resume.** They differ only in field names. See §5.

---

## 1. "I want Stripe — set it up"

### The minimal-friction default (2025-2026)
**Stripe-hosted onboarding via Account Links.** Stripe explicitly recommends hosted or embedded onboarding over API onboarding because they auto-update for changing KYC/regulatory requirements and auto-support new countries. ([docs.stripe.com/connect/onboarding](https://docs.stripe.com/connect/onboarding))

Use **controller properties** at account creation rather than the legacy `type` (Standard/Express/Custom) — Stripe now recommends this for new integrations. An "Express-equivalent" minimal-friction config:
```
controller[fees][payer]            = application
controller[losses][payments]       = application
controller[stripe_dashboard][type] = express
controller[requirement_collection] = stripe   # Stripe collects KYC
```
These are immutable after creation. ([migrate-to-controller-properties](https://docs.stripe.com/connect/migrate-to-controller-properties))

### Exact handoff (3 API calls + 1 webhook)

**1. Create the connected account** — `POST /v1/accounts` with controller props (or `type=standard`). Returns `acct_…`. Optionally prefill `business_profile[url]` / `[product_description]` to drop fields.

**2. Create the Account Link** — `POST /v1/account_links`:
```
account              = acct_…
return_url           = https://you.app/stripe/return   (HTTPS required in live)
refresh_url          = https://you.app/stripe/refresh   (HTTPS required in live)
type                 = account_onboarding   # enum: account_onboarding | account_update
collection_options[fields] = eventually_due  # collect everything up front, fewer return trips
                                             # (default currently_due = bare minimum)
```
Returns a `connect.stripe.com/setup/…` URL — **single-use, expires in minutes, must be served only inside your authenticated app** (never email/SMS it). *(Params verified against [API ref](https://docs.stripe.com/api/account_links/create).)*

**3. Redirect** the user to that URL once → Stripe-hosted KYC form.

**4. Handle the returns:**
- **`refresh_url`** (link expired/visited): create a *new* Account Link, redirect again.
- **`return_url`** (finished OR clicked "Save for later"): this only means "flow exited cleanly" — **it does NOT mean onboarding is complete.** Do not enable payments here.

### Verify it actually works — `account.updated` webhook
Listen for **`account.updated`** and inspect the Account object. ([handle-verification-updates](https://docs.stripe.com/connect/handle-verification-updates), [account object](https://docs.stripe.com/api/accounts/object))

**"Fully onboarded & usable" logic:**
```
charges_enabled == true
&& payouts_enabled == true
&& details_submitted == true
&& requirements.currently_due == []
&& requirements.past_due == []
&& requirements.disabled_reason == null
```
- Gate *charging* on `charges_enabled`; gate *payouts* on `payouts_enabled`.
- `requirements.pending_verification` non-empty → wait for Stripe's review, not the user.
- `requirements.eventually_due` → collect later as volume thresholds are hit.

### Test vs live
Mode is determined entirely by the API key (`sk_test_…` vs `sk_live_…`); objects are fully isolated. Test mode lets you use fake KYC data (SSN `000-00-0000`, fake DOB/address) to flip `charges_enabled`/`payouts_enabled` true without real verification. Test webhooks with `stripe listen` / `stripe trigger account.updated`. **Live requires HTTPS return/refresh URLs and Connect activation.**

### OAuth (connect an *existing* account) vs Account Links (create *new*)
- **OAuth** (Standard accounts, connect existing): redirect to `connect.stripe.com/oauth/authorize?response_type=code&client_id=ca_…&scope=read_write` → user authorizes → callback with `?code=…` → `POST /oauth/token` (`grant_type=authorization_code`) → get `stripe_user_id`. Account creation is folded into the flow, so it handles both "has account" and "needs one." ([oauth-standard-accounts](https://docs.stripe.com/connect/oauth-standard-accounts))
- **Account Links** (above): for provisioning *new* connected accounts you control.

### How real platforms do it & minimum user input
- **Shopify Payments** — powered by Stripe under the hood; merchant never sees "Stripe," Shopify owns the UX.
- **Whop** — started as a Stripe Connect wrapper; now its own KYC/payout rails, Connect-style split-charge model.
- **Lemon Squeezy — different model (contrast):** Merchant-of-Record. *They* are the legal seller and file/remit global VAT/GST/sales tax; seller carries zero tax liability, paying ~5%+$0.50 (vs Stripe ~2.9%+$0.30). With raw Connect, **you** own tax liability. (Stripe announced **Stripe Managed Payments**, its own MoR offering, at Sessions 2025 — competing here directly.)
- **Minimum user interaction** (hosted, Express-style): platform pre-creates the account, so the user only (1) clicks "Set up payments," (2) on Stripe's form enters email+phone(OTP), business type, legal name/DOB, address, last-4 SSN, bank account for payouts, (3) returns. MoR platforms need essentially nothing from the seller's customer beyond card details.

### Sample agent UX
> **You:** "I want Stripe — set it up."
> **Agent:** "I'll create a Stripe account for *Acme Co* and open Stripe's secure setup so you can verify your identity and add a payout bank account. Stripe handles the sensitive data — I never see your SSN or bank details.
> → **[Set up payments with Stripe]**  *(opens Stripe-hosted onboarding)*
> I'll switch payments on automatically once Stripe confirms your account is verified (usually instant to a few minutes)."
>
> *(later, on `account.updated` with charges_enabled && payouts_enabled)*
> **Agent:** "✅ Stripe is live. You can now accept payments and receive payouts."

---

## 2. "Bring your own API keys" (Anthropic / OpenAI)

### Validate-on-paste — cheapest endpoint = list models (no tokens spent)
**Anthropic — `GET https://api.anthropic.com/v1/models`** *(verified)*
```
Headers:  x-api-key: <KEY>
          anthropic-version: 2023-06-01
Optional: ?limit=1   (lightest possible)
200 → valid (returns model metadata, zero token cost)
401 (authentication_error) → invalid/revoked
```
**OpenAI — `GET https://api.openai.com/v1/models`**
```
Header:  Authorization: Bearer <KEY>
200 → valid
401 → invalid auth (revoked / wrong org-project / lacks permission)
403 → accepted but not allowed (region, or a Restricted/Read-Only key without scope)
```
> Nuance: a scoped/restricted key can pass `GET /v1/models` (read) but `403` on a write endpoint. To confirm *generation* capability, follow with a `max_tokens=1` message; for "is this key live?" the models endpoint is sufficient.

### Encryption at rest (per-tenant)
**Recommendation: envelope encryption.** Encrypt each key with a per-tenant **DEK (AES-256-GCM)**; wrap the DEK with a **KEK** in KMS/HSM. AES-GCM (authenticated) over CBC for tamper detection.
- **AWS pattern:** one customer-managed KMS key per tenant (cost-conscious) + **KMS Encryption Context** bound to each encrypt/decrypt so a decrypt request for tenant A can't decrypt tenant B's data. HashiCorp Vault Transit is the non-AWS equivalent.
- **Hygiene (always):** never log full keys; **display last-4 only** (`sk-…AbCd`); store a hash/fingerprint for lookup/de-dup so you never decrypt just to identify; decrypt in-memory at request time only; scope decrypt IAM tightly.
- App-level AES-256-GCM with an env master key (LibreChat/Dify pattern) is acceptable for smaller deployments; KMS-backed envelope is the stronger multi-tenant posture.

### Cost attribution — the big BYOK implication
| | **BYOK (customer key)** | **Platform key + markup** |
|---|---|---|
| Who pays provider | Customer, billed directly by Anthropic/OpenAI | Platform, consolidated |
| Meter tokens for billing? | **No** — provider bills the customer; meter only for analytics/quotas | **Yes** — meter every token to bill |
| Rate limits | Customer's own provider limits | Your shared account limits |
| Data/ZDR | Follows **customer's** provider terms (e.g. Cursor notes ZDR does *not* apply under BYOK) | Follows your platform terms |
| Monetize via | Seat/subscription fee or thin per-request fee | Token markup |

**Key takeaway:** true BYOK means you generally **stop metering tokens for billing** — monetize with a subscription/seat fee or a thin per-request passthrough (the OpenRouter model).

### Key scoping (tell customers to create least-privilege keys)
- **OpenAI:** project-scoped keys are the 2025 default. Permission tiers: **All / Read-Only / Restricted** (per-endpoint scopes). Ask customers for a **project-scoped, Restricted key** limited to the models/endpoints you use. Use **service accounts** for production.
- **Anthropic:** regular keys are **workspace-scoped** (can't spend across workspaces) — the natural BYOK isolation boundary. Tell customers to make a dedicated workspace + key. **Never ask for an Admin API key** (`sk-ant-admin…`, org-level) — a workspace key is correct and far lower blast-radius.

### How real tools do BYOK
- **Cursor:** Settings→Models, paste key, **Verify** button (explicit validate-on-paste). Keys **not stored on Cursor servers** — sent per request. Surfaces "ZDR does not apply under BYOK."
- **OpenRouter:** supply upstream provider keys, still route through OpenRouter; keys "securely encrypted." **Fee: 5% of normal cost, waived for first 1M BYOK requests/month.** Per-key model/app filters for isolation; unified cross-provider analytics.
- **LibreChat:** set `ANTHROPIC_API_KEY=user_provided` → users enter keys in WebUI; stored **AES-256-CBC encrypted** via `CREDS_KEY`/`CREDS_IV`; substituted at runtime.
- **Dify:** **System credentials** (billed via Dify) vs **Custom credentials** (BYOK, billed by provider). Custom keys stored encrypted, decrypted per-tenant at runtime; **validates credentials before enabling** the provider (validate-on-save).

### Sample agent UX
> **You:** "Use my own Anthropic key."
> **Agent:** "Paste your Anthropic API key. I'll verify it instantly and store it encrypted (I'll only ever show the last 4 digits). Tip: create a key in a dedicated workspace so it's scoped to just this app."
> *(on paste → `GET /v1/models`, 200)*
> **Agent:** "✅ Key verified (`sk-…AbCd`) and encrypted. Anthropic will bill this key directly — you won't see token charges from us."

---

## 3. "Can you deploy to AWS?"

Two camps: **platform-managed hosting** (Render-style; simplest) vs **BYOC — deploy into the customer's own AWS account/VPC** (Porter, Vercel BYOC, Nuon, most data-plane-in-customer SaaS). For BYOC the canonical handshake is a **cross-account IAM role + ExternalId assumed via `sts:AssumeRole`** — *never static access keys.*

### The cross-account handshake (step-by-step)
1. **You generate a unique ExternalId per customer** and give the customer (a) your 12-digit AWS account ID (or specific role ARN) and (b) that ExternalId. AWS is explicit: the ExternalId **must be generated by the vendor, not the customer**, and **need not be secret**. ([id_roles_common-scenarios_third-party](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_common-scenarios_third-party.html))
2. **Customer creates an IAM role** with a tight permission policy + a trust policy naming your account + the ExternalId condition.
3. **Customer hands back the role ARN** (ideally surfaced as a CloudFormation Output so nothing is pasted).
4. **You call `sts:AssumeRole`** with the role ARN **and** matching `ExternalId` → short-lived temp credentials.

**Trust policy shape:**
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::<VENDOR_ACCOUNT_ID>:role/assume-customer-role" },
    "Action": "sts:AssumeRole",
    "Condition": { "StringEquals": { "sts:ExternalId": "<UNIQUE_ID_VENDOR_ASSIGNED>" } }
  }]
}
```
Scope `Principal` to a specific vendor role, not `:root`. ExternalId: 2–1,224 chars.

### Why ExternalId — the confused-deputy problem
Without it, since your account is the trusted principal for *all* tenants, Customer A who learns Customer B's role ARN could trick your platform into assuming B's role. ExternalId binds each AssumeRole to a specific customer. **Real-world risk:** a Praetorian study of 90 vendors found **37% mis-implemented ExternalId** and 15% more never validated it — over half vulnerable. ([confused-deputy](https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html), [Praetorian](https://www.praetorian.com/blog/aws-iam-assume-role-vulnerabilities/))
**Defensive check:** when a customer gives you a role ARN, try to assume it *without* the ExternalId. If that succeeds, reject the config and refuse to store the ARN.

### Minimal customer setup — one-click CloudFormation "Launch Stack"
The dominant low-friction pattern: a **quick-create link** → customer lands on a pre-filled stack page in *their* console → ticks the IAM-capabilities checkbox → **Create stack**. The template creates the role; your ExternalId/account are baked in or passed as URL params. ([cfn-console-create-stacks-quick-create-links](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/cfn-console-create-stacks-quick-create-links.html))

**Launch Stack URL anatomy:**
```
https://<region>.console.aws.amazon.com/cloudformation/home?region=<region>#/stacks/create/review
  ?templateURL=<S3 URL of template, url-encoded>
  &stackName=<name>
  &param_ExternalId=<...>      # param_ prefix; name must match a template parameter
  &param_VendorAccountId=<...>
```
**Smallest possible customer action**, in order of preference: (1) click Launch Stack → ack IAM → Create, with the role ARN as a stack **Output** so nothing is pasted back (Porter's model); (2) Terraform module for IaC-native customers; (3) manual role creation for high-security customers.

### IaC tooling for provisioning into customer cloud
| Tool | Scope | Dry-run | Best for |
|---|---|---|---|
| **Terraform / OpenTofu** | Multi-cloud, declarative HCL | `terraform plan` (true read-only) | **Safe default for BYOC** — distributable modules, mature, customer-reviewable. OpenTofu sidesteps BSL licensing. |
| **AWS CDK** | AWS-only → synthesizes CloudFormation | `cdk diff` | Pairs naturally with the **Launch Stack** onboarding (compiles to CFN). |
| **Pulumi** | Multi-cloud, real languages | `pulumi preview` | When per-tenant provisioning logic is genuinely programmatic (loops/conditionals). |

**Plan-then-apply is the safety mechanism:** `terraform plan -out=tfplan` (reads state, diffs config, **no mutation API calls**) → customer/reviewer inspects exactly what will be created in *their* account → `terraform apply tfplan` executes precisely that locked set. Terraform **refuses a stale plan** if state drifted — guaranteeing what was reviewed is what's applied. This is also the canonical model for agent confirmations (§5).

### How real vendors do BYOC
- **Porter** — textbook one-click: select AWS → paste 12-digit account ID → **Grant Permissions** opens CloudFormation in a new tab provisioning a `porter-manager` role → ack IAM → Create. Porter then uses `sts:AssumeRole`; **customer pastes nothing back.** Deleting the role revokes Porter.
- **Nuon** — different architecture: deploys a **runner VM inside the customer's account** holding permissions locally, so **no cross-account access is required** by default. Install = applying a Nuon-generated CloudFormation/ARM/GDM stack. Cross-account delegation (`vendor_role_arn`) is opt-in for debugging only.
- **Vercel BYOC (AWS, private beta 2025)** — compute/build/data run in the customer's own VPC; Vercel runs the control plane. Source never leaves customer CI.
- **Massdriver** — BYOC-by-default; customer owns the IaC + state; bring-your-own Terraform/OpenTofu/Helm/Bicep bundles; can self-host the orchestrator.
- **Render — no BYOC** (managed-hosting contrast case): all workloads on Render's infra; Private Link only.
- **General BYOC SaaS** (ClickHouse, Aiven, Pinecone…): same shape — control plane with vendor, **data plane in customer VPC**, bootstrap via CFN/TF stack, vendor reaches in via scoped role + ExternalId.

### Sample agent UX (consequential → confirm + dry-run)
> **You:** "Can you deploy to AWS?"
> **Agent:** "Two options:
> **(A) We host it** — fastest, runs on our infra, nothing to set up.
> **(B) Your AWS account (BYOC)** — runs in your VPC, you keep full control/audit. One-time setup: you click a 'Launch Stack' link that creates a scoped IAM role granting us deploy access (no access keys shared). Which do you want?"
> *(user picks B, completes Launch Stack, pastes/auto-detects role ARN)*
> **Agent:** "Role verified ✅ (and I confirmed it correctly requires the ExternalId). Here's the deployment plan before I touch anything:
> ```
> + ECS service 'acme-api'        (2 tasks, Fargate)
> + Application Load Balancer
> + RDS Postgres db.t4g.medium
> ~ Security group (1 ingress rule)
> Est. cost: ~$140/month
> ```
> Apply this plan to **your** AWS account? **[Apply]** / **[Edit]** / **[Cancel]**"

---

## 4. "Rent me GPU instances"

### The autonomy decision table
| Platform | Model | Scale-to-zero? | Autonomous-safe? |
|---|---|---|---|
| **Modal** | Serverless, per-second | Yes (default) | ✅ Safe (no idle billing unless `min_containers>0`) |
| **Replicate** | Per-run public / dedicated deployments | Yes (`min_instances=0`) | ✅ public `run()`; ⚠️ confirm deployments with `min_instances>0` |
| **Fal** | Serverless, per-sec / per-output | Yes (`keep_alive`) | ✅ Safe (pay-per-use) |
| **RunPod Serverless (Flex)** | Serverless, per-second | Yes (`workersMin=0`) | ✅ Safe (Flex); 🛑 Active workers & Pods bill 24/7 |
| **RunPod Pods / AWS EC2 / Capacity Blocks** | Rent dedicated by hour / upfront block | **No** | 🛑 Always confirm + ensure teardown |

**Rule of thumb:** *serverless / scale-to-zero / pay-per-request* tiers (Modal functions, Replicate public models, Fal endpoints, RunPod **Flex**) are safe-ish to call autonomously — idle cost ≈ $0. *Dedicated-instance* tiers (RunPod Pods, Active workers, any `min_instances>0`, EC2 on-demand, Capacity Blocks) accrue cost continuously → **cost-confirm before provisioning and guarantee a teardown path.**

### Representative GPU prices (fetched ~June 2026 — re-verify at provision time)
| GPU | Modal $/hr eq. | Replicate $/hr eq. | RunPod Flex $/hr eq. | Fal |
|---|---|---|---|---|
| H100 | ~3.95 | ~5.49 | ~4.18 | ~1.89/hr |
| A100 80GB | ~2.50 | ~5.04 | ~2.72 | ~0.99/hr |
| L40S | ~1.95 | ~3.51 | ~1.90 | — |
| L4 | ~0.80 | — | ~0.69 | — |
| T4 | ~0.59 | ~0.81 | — | — |

(Modal also bills CPU/memory separately. RunPod Pods on-demand: H100 ~$2.89/hr, A100 80GB ~$1.39/hr. **AWS EC2 — not scale-to-zero, highest commitment**: p5.48xlarge (8×H100) ~$31–39/hr post-June-2025 cut; g5.2xlarge (1×A10G) ~$1.21/hr; **Capacity Blocks charge an upfront reservation fee** for a future window.)

### Cold starts & scale-to-zero
- **Modal:** scales to zero by default; cold start seconds→>1min; 2025 GPU memory snapshots claim up to 10× faster. Config: `min_containers`, `max_containers`, `scaledown_window` (renamed from `keep_warm`/`container_idle_timeout` in Modal 1.0).
- **Replicate:** popular public models stay warm; `min_instances` (default 0) / `max_instances` (caps spend). `min_instances=1` kills cold starts but bills ~$36.60/day for an H100.
- **Fal:** scales from zero; `keep_alive` (idle runner lifetime), `min_concurrency` (warm floor); never charged for queue wait or server errors.
- **RunPod:** **FlashBoot** cold starts as low as ~500ms; default idle timeout 5s; set `workersMin=0` for scale-to-zero.

### Provisioning APIs (agent shapes)
- **Modal:** `@app.function(gpu="H100:1")` + `app.deploy()` (persistent endpoint) or `fn.remote(args)` (ad-hoc).
- **Replicate:** `client.run("owner/model", input={…})` for public; `deployments.create({name, model, version, hardware, min_instances, max_instances})` for dedicated.
- **Fal:** `fal.App` + `@fal.endpoint` routes → `fal deploy` CLI → persistent URL; call hosted models via `fal_client`.
- **RunPod:** REST/GraphQL/Python SDK; create endpoint with `gpuTypeIds, workersMin, workersMax, idleTimeout, executionTimeoutMs`.
- **AWS:** `ec2:RunInstances` — heavy (VPC/AMI/IAM/quota); always confirm.

### Confirm-cost-before-provision (how the agent estimates)
1. **Look up price** from the platform's pricing page/API at request time (don't trust cached values).
2. **Estimate spend:** serverless = `price_per_sec(gpu) × expected_duration_sec`; dedicated = `hourly_rate × expected_hours`. Add cold-start/setup seconds for dedicated tiers; add CPU+mem for Modal.
3. **Classify blast radius:** serverless/scale-to-zero = idle ≈ $0 (low); dedicated/`min_instances>0`/Pods/EC2 = continuous burn (high → confirm + teardown plan).
4. **Present** total estimate, billing model, whether idle is charged, and worst-case ("if left idle on a Pod ≈ $Y/day").
> Note: only **Replicate's deployment-creation UI** surfaces a built-in real-time cost indicator. None expose a true pre-flight "quote" REST endpoint — the agent computes the estimate from published per-second/per-output rates.

### Sample agent UX (autonomous-where-safe vs confirm-where-costly)
> **You:** "Run this image model on 200 prompts."
> **Agent (serverless, idle≈$0 → just does it):** "Running on Fal serverless (scales to zero when idle). ~200 images × ~$0.03 ≈ **$6 total**. Starting now."
>
> **You:** "Rent me an H100 box for fine-tuning."
> **Agent (dedicated, continuous burn → confirm):** "Heads up — a dedicated H100 bills continuously until torn down:
> • RunPod Pod H100: ~**$2.89/hr** (~$69/day if left running)
> • Estimated job: ~6 hrs ≈ **$17**, but it keeps billing if idle.
> I'll auto-terminate when the job finishes. **Provision the H100?** [Yes, with auto-teardown] / [No]"

---

## 5. ⭐ The Confirmation / Approval Pattern (central theme)

**Design law:** *read-only and in-scope actions auto-run; consequential, costly, or irreversible actions pause for an approve / edit / reject decision — and the decision can be made sticky (allow-list) so the gate only fires where it earns its interruption.*

Every framework implements the same skeleton — they differ only in field names:

| Framework | "Mark for approval" | "Pause / surface" | "Resume with decision" | Sticky / allow-list |
|---|---|---|---|---|
| **LangGraph** | `interrupt_on` / `when` predicate | `interrupt()` | `Command(resume=…)` | config per tool |
| **Claude / Claude Code** | `permissions.ask` rules / mode | `tool_use` block + prompt | return `tool_result` | `permissions.allow`, "allow for session" |
| **Bedrock Agents** | per-action user confirmation | `returnControl` / `invocationInputs` | `confirmationState: CONFIRM\|DENY` | — |
| **Vercel AI SDK** | `needsApproval` (bool / fn of input) | `tool-approval-request` part | `addToolResult` / approval response | remember approved patterns |
| **OpenAI Agents** | `needsApproval` / `requireApproval` | `RunToolApprovalItem` + `interruptions` | `state.approve()/reject()` → `runner.run` | `alwaysApprove`/`alwaysReject` |

### Per-framework mechanism
- **LangGraph** — `interrupt(payload)` halts the graph at that line, persists via checkpointer, returns payload to caller; resume with `Command(resume=value)`. `HumanInTheLoopMiddleware(interrupt_on={…})` inspects tool calls post-generation/pre-execution; per tool: `True`/`False`/`InterruptOnConfig` with `allowed_decisions`, `description`, and a **`when` predicate gating on arguments** (e.g. only interrupt above a threshold). **Four decisions: approve / edit / reject / respond.** ([docs.langchain.com/oss/python/langchain/human-in-the-loop](https://docs.langchain.com/oss/python/langchain/human-in-the-loop))
- **Claude / Claude Code** — tool use *is* the gate: Claude returns `stop_reason:"tool_use"`; **your app decides whether to execute** (run / prompt human / refuse), then returns `tool_result`. (Server tools like `web_search` execute on Anthropic infra — no client gate.) Claude Code permission modes are the production tiered-autonomy ladder: `default` (reads only) → `acceptEdits` → `plan` (propose, no edits) → `auto` (a **separate classifier vets each action** pre-execution, blocking prod deploys/migrations/`terraform destroy`/IAM grants/force-push) → `dontAsk` → `bypassPermissions`. Deny/ask rules + protected paths (`.git`, `.claude`) apply in every mode. ([code.claude.com/docs/en/permission-modes](https://code.claude.com/docs/en/permission-modes), [claude-code-auto-mode](https://www.anthropic.com/engineering/claude-code-auto-mode))
- **Bedrock Agents** — **Return of Control** (`RETURN_CONTROL` customControl) returns the call in `invocationInputs` for your app to execute, results back via `sessionState.returnControlInvocationResults`. **User confirmation** (per action): agent emits a `returnControl` block; decision rides back on **`confirmationState`** — `CONFIRM` executes, `DENY` doesn't. Designed partly as a prompt-injection defense. ([agents-userconfirmation](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-userconfirmation.html))
- **Vercel AI SDK 6** — `needsApproval: true` on a `tool({…})`, or **`needsApproval: async ({input}) => boolean`** for conditional gating (e.g. only payments >$1000 require approval — auto-execute below). Returns `tool-approval-request` parts; client renders a confirm card; `useChat` surfaces `approval-requested`; client-execution variant uses **`addToolResult`** to continue. ([ai-sdk.dev/cookbook/next/human-in-the-loop](https://ai-sdk.dev/cookbook/next/human-in-the-loop), [vercel.com/blog/ai-sdk-6](https://vercel.com/blog/ai-sdk-6))
- **OpenAI Agents SDK** — `needsApproval: true` (or predicate); pending calls become `RunToolApprovalItem`s in the result's **`interruptions`** array; resolve via `state.approve()/reject()`, pass `{alwaysApprove:true}`/`{alwaysReject:true}` for sticky decisions; resume `runner.run(agent, state)`. MCP: `requireApproval: 'always'`. ([openai.github.io/openai-agents-js/guides/human-in-the-loop](https://openai.github.io/openai-agents-js/guides/human-in-the-loop/))

### Plan-then-apply / dry-run (the Terraform model)
`terraform plan` = read-only preview of exactly what would change (no mutation calls); `-out=tfplan` locks it; `apply tfplan` executes precisely that, refusing a stale plan. Agents borrow this directly: **show a structured plan → one approval → deterministic execution.** Claude Code's `plan` mode is the agent analog. ([terraform plan](https://developer.hashicorp.com/terraform/cli/commands/plan))

### Spend caps / tiered autonomy
- **Enforcement vs monitoring:** monitoring reads what happened (too late); **enforcement intercepts each call against a ceiling before it goes out** and terminates the session at the limit. Wrap calls at the infra layer.
- **Hard cap / kill switch:** OpenAI project-level monthly budget cap; Google Cloud **Spend Caps** (per-project automated boundaries for Agent Platform / Cloud Run).
- **Tiered autonomy by cost/risk/reversibility:** auto-approve under threshold, escalate above — exactly what Vercel/OpenAI `needsApproval(input)` and LangGraph's `when` predicate express in code.
- **Payment guardrails:** Ramp **Agent Cards** — single-use cards scoped to a specific merchant + dollar amount.
- **Why it matters (2025-26):** a 4-agent infinite loop ran 264 hrs to a **$47,000** bill because no agent had a budget ceiling. ([Ramp spending controls](https://ramp.com/blog/ai-agent-spending-controls), [Google Cloud Spend Caps](https://cloud.google.com/blog/topics/cost-management/introducing-spend-caps-ai-cost-visibility-next26))

### Confirmation UX best practices
- **Restate the action and its consequences** — "Delete account and all data," not "Are you sure?" Show cost, reversibility, blast radius.
- **Match friction to impact** — trivial/reversible → don't prompt; prefer **Undo** (execute + offer an Undo toast). Reserve hard confirms for irreversible/expensive/wide-blast actions.
- **Specific button labels, not Yes/No** — "Delete account" vs "Cancel" (rushed users read the button, not the body). Don't rely on color alone (accessibility).
- **Extra friction for critical/irreversible** — type-to-confirm (type the resource name).
- **Use sparingly** — overused dialogs become noise; this is *why* allow-lists / "remember this choice" / sticky `alwaysApprove` exist.
- **Anthropic's agent guidance:** build checkpoints where the agent pauses for human review — **especially before irreversible actions like approving financial transactions or deleting data** — plus sandboxed testing and surfacing the agent's own uncertainty. ([Building effective agents](https://www.anthropic.com/research/building-effective-agents))

### Applying it to the four scenarios
| Scenario | Autonomous where safe | Confirm where costly/irreversible |
|---|---|---|
| **Stripe** | Create account, create Account Link, poll webhooks | (User self-confirms inside Stripe's KYC; nothing irreversible on your side) |
| **BYO keys** | Validate key, encrypt, store, show last-4 | (Low stakes — no confirm gate needed) |
| **AWS deploy** | Read state, `terraform plan`, validate role (incl. no-ExternalId check) | **`apply`** — show the plan + est. cost, require approval; never auto-`destroy` |
| **GPU** | Serverless/scale-to-zero `run()` (idle≈$0) | **Provision dedicated instance** — show $/hr + worst-case idle cost, confirm, auto-teardown |

---

## Key Source URLs
**Stripe:** [onboarding config](https://docs.stripe.com/connect/onboarding) · [hosted onboarding](https://docs.stripe.com/connect/hosted-onboarding) · [Account Links API](https://docs.stripe.com/api/account_links/create) · [controller properties](https://docs.stripe.com/connect/migrate-to-controller-properties) · [Account object](https://docs.stripe.com/api/accounts/object) · [verification updates](https://docs.stripe.com/connect/handle-verification-updates) · [OAuth standard](https://docs.stripe.com/connect/oauth-standard-accounts)
**BYOK:** [Anthropic List Models](https://platform.claude.com/docs/en/api/models-list) · [Anthropic workspaces](https://platform.claude.com/docs/en/build-with-claude/workspaces) · [OpenAI error codes](https://developers.openai.com/api/docs/guides/error-codes) · [OpenAI key permissions](https://help.openai.com/en/articles/8867743-assign-api-key-permissions) · [AWS multi-tenant KMS](https://aws.amazon.com/blogs/architecture/simplify-multi-tenant-encryption-with-a-cost-conscious-aws-kms-key-strategy/) · [OpenRouter BYOK](https://openrouter.ai/docs/guides/overview/auth/byok) · [Cursor BYOK](https://cursor.com/help/models-and-usage/api-keys) · [Dify model providers](https://docs.dify.ai/en/use-dify/workspace/model-providers)
**AWS BYOC:** [third-party role access](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_common-scenarios_third-party.html) · [confused deputy](https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html) · [External ID (APN)](https://aws.amazon.com/blogs/apn/securely-using-external-id-for-accessing-aws-accounts-owned-by-others/) · [Praetorian ExternalId study](https://www.praetorian.com/blog/aws-iam-assume-role-vulnerabilities/) · [Launch Stack URLs](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/cfn-console-create-stacks-quick-create-links.html) · [terraform plan](https://developer.hashicorp.com/terraform/cli/commands/plan) · [Porter cloud connect](https://docs.porter.run/cloud-accounts/connecting-a-cloud-account) · [Nuon runner](https://nuon.co/blog/the-nuon-runner-architecture)
**GPU:** [Modal pricing](https://modal.com/pricing) · [Modal scaling](https://modal.com/docs/guide/scale) · [Replicate pricing](https://replicate.com/pricing) · [Replicate deployments](https://replicate.com/docs/topics/deployments) · [Fal pricing](https://fal.ai/pricing) · [RunPod serverless pricing](https://docs.runpod.io/serverless/pricing) · [AWS GPU price cut](https://aws.amazon.com/blogs/aws/announcing-up-to-45-price-reduction-for-amazon-ec2-nvidia-gpu-accelerated-instances/)
**HITL:** [LangChain HITL](https://docs.langchain.com/oss/python/langchain/human-in-the-loop) · [Claude tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview) · [Claude Code permission modes](https://code.claude.com/docs/en/permission-modes) · [Claude Code auto mode](https://www.anthropic.com/engineering/claude-code-auto-mode) · [Bedrock user confirmation](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-userconfirmation.html) · [Vercel AI SDK HITL](https://ai-sdk.dev/cookbook/next/human-in-the-loop) · [OpenAI Agents HITL](https://openai.github.io/openai-agents-js/guides/human-in-the-loop/) · [Building effective agents](https://www.anthropic.com/research/building-effective-agents) · [Ramp agent spending](https://ramp.com/blog/ai-agent-spending-controls)
