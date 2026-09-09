# GCP production cell

This is the low-fixed-cost, first-customer deployment path. It creates a scale-to-zero Cloud Run API, one manually
scaled Cloud Run worker pool, a one-shot migration Job, private Artifact Registry and Cloud Storage repositories,
separate least-privilege service accounts, Secret Manager containers, and optional repository-ID-bound GitHub OIDC.
It deliberately does **not** create GKE, Cloud SQL, public artifact buckets, permanent sandbox capacity, or secret
values in OpenTofu state.

Generated source, build output, and QA evidence use the private artifact bucket through Application Default
Credentials; no storage key is created. Artifact bytes are immutable generation-guarded GCS objects. Tenant-scoped
identity, media type, digest, idempotency, and object generation remain in PostgreSQL under RLS. The authenticated
API and worker can create and read objects, but neither runtime identity can overwrite or delete them.

The API also enables zero-touch personal-company onboarding. If a valid OIDC token has no organization claim, a
stable isolated tenant ID is derived from its verified issuer/subject with a server-only HMAC secret. That secret is
generated into Secret Manager on first deploy; no extra key or provider-specific organization feature is required.
Provider-supplied roles are ignored on this path and the sole verified subject becomes that personal company's owner.
The derivation secret is deletion-protected and intentionally excluded from bulk secret rotation because changing it
would change tenant identities; disaster recovery must restore the existing secret version.

The transactional database is an external managed PostgreSQL service for the bootstrap cell. Its migration URL
uses a table-owner/admin principal and is readable only by the one-shot migration identity. The system/application
URLs use runtime principals; the application login is validated as non-superuser/non-`BYPASSRLS` and receives only
membership in `agentos_app` and `agentos_worker`. Use TLS and point-in-time backups. The first provider can be Neon or another PostgreSQL
service that passes the V2 migration/RLS proof. Move to Cloud SQL only when its fixed floor fits the revenue rule.

## One-time inputs

Install `gcloud` and OpenTofu 1.12.6, authenticate to a billed GCP project, and export the following. Credentials
remain in the local process long enough to create Secret Manager versions; they are never written to a tfvars file
or OpenTofu state.

```bash
export GCP_PROJECT_ID=your-project
export GCP_REGION=us-central1
export AOS_V2_PUBLIC_BASE_URL=https://your-domain.example

export AOS_V2_OIDC_ISSUER=https://your-provider.example
export AOS_V2_OIDC_AUDIENCE=agent-os-api
export AOS_V2_OIDC_JWKS_URL=https://your-provider.example/.well-known/jwks.json
export AOS_V2_OIDC_AUTHORIZATION_URL=https://your-provider.example/authorize
export AOS_V2_OIDC_TOKEN_URL=https://your-provider.example/oauth/token
export AOS_V2_OIDC_CLIENT_ID=your-public-pkce-client

export AOS_V2_STRIPE_STARTER_PRICE_ID=price_live_starter
export AOS_V2_STRIPE_GROWTH_PRICE_ID=price_live_growth
export AOS_V2_STRIPE_SECRET_KEY=sk_live_replace
export AOS_V2_STRIPE_WEBHOOK_SECRET=whsec_replace

export AOS_V2_MIGRATION_DATABASE_URL='postgresql://migration-user:password@host/database?sslmode=require'
export AOS_V2_DATABASE_RUNTIME_ROLE=agentos_runtime
export AOS_V2_SYSTEM_DATABASE_URL='postgresql://runtime-user:password@host/database?sslmode=require'
export AOS_V2_APPLICATION_DATABASE_URL='postgresql://runtime-user:password@host/database?sslmode=require'
export AOS_V2_MODEL=openai:gpt-5-mini
export AOS_V2_MODEL_PROVIDER_SECRET_ENVIRONMENT=OPENAI_API_KEY
export AOS_V2_MODEL_PROVIDER_KEY=replace

# Optional, repeatable; create the email/Slack/PagerDuty channel in Monitoring first.
export TF_VAR_alert_notification_channels='["projects/your-project/notificationChannels/123456"]'
```

Then run `deploy/gcp/deploy.sh`. On a new cell it creates/version-enables the remote state bucket and bootstraps
foundation resources without a runtime. On both first and repeat releases it adds only missing secret versions,
builds both images in Cloud Build, resolves immutable digests, updates only the migration Job, executes migrations,
then rolls the API/worker and checks `/ready`. An existing serving plane is never reconciled against the inactive
bootstrap shape. Set `AOS_ROTATE_SECRETS=1` only for an intentional rotation; the tenant-derivation secret is
permanently excluded.

The active cell also creates a one-minute HTTPS uptime check against the customer-facing `/ready` endpoint from
USA, Europe, and Asia-Pacific, logs failed probes, and opens a critical alert after multiple regions fail for two
minutes. Pass one or more existing Monitoring notification-channel resource names to
`TF_VAR_alert_notification_channels`; the policy is still created without a destination so missing paging wiring is
visible in infrastructure state rather than silently invented. Test every configured channel before launch.

The script defaults GitHub repository ID to this repository's immutable ID (`1276674620`), not its reusable name.
After the first apply, set these GitHub repository/environment variables from the OpenTofu outputs:

- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_DEPLOY_SERVICE_ACCOUNT`
- `GCP_PROJECT_ID`, `GCP_REGION`, and `GCP_STATE_BUCKET`
- all non-secret `AOS_V2_OIDC_*`, Stripe price, public URL, and model values used above
- `AOS_ALERT_NOTIFICATION_CHANNELS`, a JSON list of full Monitoring notification-channel resource names (or `[]`)

Runtime credentials stay in GCP Secret Manager and are never copied into GitHub. The worker identity cannot read
Stripe secrets; the API identity cannot read the model-provider key. GitHub deployment can update Cloud Run and
submit builds but cannot read secret payloads.

`github-actions-deploy.yml` is the reviewed deployment-workflow template. Install it as
`.github/workflows/deploy-gcp.yml` when enabling the production GitHub environment. It refuses a commit unless its
existing `test` and `container` checks are green, then repeats OpenTofu validation, migration-first release, and the
deployed readiness check.

## Ordered release contract

Every image is deployed by digest. A release uses an OpenTofu targeted apply to update only the idempotent migration
Job, executes it, and only then applies the new API and worker revisions. Schema changes must remain expand/contract
compatible with the prior revision. The API
scales from zero to a bounded maximum; the worker pool defaults to one instance and can be set to zero to halt spend
without discarding durable work.

## Serving-plane rollback

Use `deploy/gcp/rollback.sh` from the same authenticated deployment environment. Keep all non-secret exports from
the normal deployment, then identify a known-good application release (or its exact image digest):

```bash
export AOS_ROLLBACK_RELEASE_ID=known-good-git-sha
deploy/gcp/rollback.sh

# Equivalent when an immutable reference is already known:
export AOS_ROLLBACK_APPLICATION_IMAGE='us-central1-docker.pkg.dev/project/repository/agent-os@sha256:...'
deploy/gcp/rollback.sh
```

Rollback updates only the API and worker to the prior digest and verifies `/ready`. It deliberately neither changes
nor executes the migration Job. Database revisions must therefore follow the documented expand/contract contract;
destructive schema reversal is an incident-specific, reviewed recovery action rather than an automated rollback.

The GCP plane does not make the current product fully launch-ready by itself. A real domain, OIDC organization
tenant, Stripe products/webhook, managed PostgreSQL, model key, hosted untrusted sandbox, artifact garbage collector,
production generated-app deployer, backup/restore drills, notification-channel delivery, and an external smoke test
must still be configured or proven.
