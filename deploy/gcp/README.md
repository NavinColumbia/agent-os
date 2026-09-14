# GCP production cell

This is the low-fixed-cost, first-customer deployment path. It creates a scale-to-zero Cloud Run API, a separate
scale-to-zero generated-app router, one manually scaled Cloud Run worker pool, a one-shot migration Job, private
Artifact Registry and Cloud Storage repositories,
separate least-privilege service accounts, Secret Manager containers, and optional repository-ID-bound GitHub OIDC.
The two customer-facing origins terminate TLS at one global external Application Load Balancer with a reserved IP,
Google-managed certificate, HTTP-to-HTTPS redirect, full request logging, and separate serverless backends. Direct
public access to both control-plane Cloud Run URLs is disabled so the edge cannot be bypassed.
It also creates a scale-to-zero, secretless Cloud Run sandbox Job in a second GCP project and a generated-service
build/serving plane in a third GCP project. It deliberately does
**not** create GKE, Cloud SQL, public artifact buckets, permanent sandbox capacity, or secret values in OpenTofu
state.

Production static releases use a second private bucket. The worker can create/get immutable release objects and
create/get/update route pointers through prefix-conditioned custom roles, but cannot delete either. A separate
service account can get exact objects but cannot list the bucket. Its public Cloud Run router receives no database,
model, OIDC, Stripe, or control-plane secret. The control API and generated apps must use different HTTPS origins.
`deploy.static` additionally requires durable `approved: true` evidence from a preceding Human workflow node.

Verified backend applications use `deploy.service`. The worker stages a bounded, secret-scanned source archive,
reconciles Cloud Builds by a deterministic tag after crashes, and accepts only the matching output image digest.
The build uses an explicit least-privilege identity; the deployed service uses a different identity with no project
roles, receives no control-plane secrets, scales from zero to a bounded maximum, and is promoted through Cloud Run's
idempotent create-or-update API. Source Dockerfiles and the trusted build-step image must pin every base by digest.
An independently reachable health check must pass before the release receipt is committed. Production promotion
still requires exact durable human approval from the workflow.

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
export GCP_SANDBOX_PROJECT_ID=your-separate-sandbox-project
export GCP_APP_PROJECT_ID=your-separate-generated-app-project
export GCP_REGION=us-central1
export AOS_V2_PUBLIC_BASE_URL=https://your-domain.example
export AOS_V2_APPS_BASE_URL=https://apps.your-domain.example

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

# Optional reviewed override; the repository already carries an immutable default.
export AOS_V2_APP_BUILDER_IMAGE='gcr.io/cloud-builders/docker@sha256:3d00b6c1a9b862621c30fc74d4f2abfc62bcbdee631ed3febd31e7edbdf6252c'

# Optional: an existing Cloud DNS zone name. Leave unset for any other DNS provider.
export GCP_DNS_MANAGED_ZONE=your-cloud-dns-zone

# Required for paying-customer launch; create and test an email/Slack/PagerDuty channel first.
export AOS_ALERT_NOTIFICATION_CHANNELS='["projects/your-project/notificationChannels/123456"]'
```

Run `.venv/bin/python deploy/gcp/launch_preflight.py --require-bootstrap-secrets` before the first activation. It
is offline, never prints a credential value, and names every missing or malformed input. Repeat deployments use the
same non-secret check automatically but can reuse existing Secret Manager versions without re-exporting secret
payloads.

For the simplest secret-safe path, copy `deploy/gcp/launch.env.example` to `.runtime/gcp-launch.env`, replace the
placeholders, and set mode `600`. Run `deploy/gcp/deploy-from-env.sh`; it verifies file ownership/permissions and the
redacting bootstrap preflight before sourcing it. After the first successful secret injection, remove secret values
from that file and use `deploy/gcp/deploy-from-env.sh --reuse-existing-secrets` for later releases. The two
server-generated tenant/capability secrets never need founder input.

Configure the OIDC SPA callback/logout/web origin as `${AOS_V2_PUBLIC_BASE_URL}/app`. Configure the Stripe webhook
as `${AOS_V2_PUBLIC_BASE_URL}/v2/billing/webhooks/stripe`; checkout redirects alone never grant an entitlement.

For an authenticated tenant connector, derive its secret name without revealing either tenant or credential label:

```bash
DERIVED_NAME="$(agentos-v2 connector-secret-name \
  --backend gcp --organization "$TENANT_ID" --credential-ref "$CREDENTIAL_REF" | jq -r .locator)"
gcloud secrets create "$DERIVED_NAME" --replication-policy=automatic
gcloud secrets versions add "$DERIVED_NAME" --data-file=-
gcloud secrets add-iam-policy-binding "$DERIVED_NAME" \
  --member="serviceAccount:${WORKER_SERVICE_ACCOUNT}" \
  --role=roles/secretmanager.secretAccessor
```

Grant the worker on that individual secret only; it deliberately has no project-wide Secret Manager reader role.
The credential value and its raw logical reference never enter OpenTofu state, PostgreSQL, model context, or
connector receipts. Register the matching non-secret connector envelope through `POST /v2/connectors`.

Then run `deploy/gcp/deploy.sh`. On a new cell it creates/version-enables the remote state bucket and bootstraps
foundation resources without a runtime. On both first and repeat releases it adds only missing secret versions,
builds all three images in Cloud Build, resolves immutable digests, updates only the migration Job, executes migrations,
then rolls the API/worker and verifies both public origins through DNS, valid TLS, `/ready`, and `/health`. An
existing serving plane is never reconciled against the inactive
bootstrap shape. Set `AOS_ROTATE_SECRETS=1` only for an intentional rotation; the tenant-derivation secret is
permanently excluded.

The bootstrap reserves the edge IP before building an application. With `GCP_DNS_MANAGED_ZONE`, the same bootstrap
creates both A records automatically. With another DNS provider, the first run stops before secret injection or
image spend and prints the two exact A records; add them and rerun the same command. The readiness gate then follows
observable DNS, TLS, `/ready`, and `/health` state for up to an hour (configurable with
`AOS_V2_EDGE_READY_TIMEOUT_SECONDS`) so normal managed-certificate propagation is not mistaken for an app failure.
The global load balancer and reserved IP have non-zero fixed cost even while Cloud Run scales to zero.

Release builds archive the exact 40-character Git commit and read `cloudbuild.yaml` from that same commit. Local
dirty and untracked files are explicitly excluded instead of being uploaded by `gcloud builds submit .`. All six
build/push steps use the same reviewed digest-pinned Docker builder; changing it is an auditable code/config update.

`GCP_SANDBOX_PROJECT_ID` and `GCP_APP_PROJECT_ID` must be existing billed projects, all three project IDs must be
different, and each is a separate failure/security plane. The
module enables only the APIs it needs there, creates a dedicated VPC/subnet, sends all Job egress through that VPC,
allows HTTPS only to Google's restricted API ranges, and denies every other IPv4 destination. Private DNS maps
`*.googleapis.com` to `restricted.googleapis.com`. The sandbox runtime service account receives no project role and
no database, model, Stripe, or application secret. The worker passes one input and one output object through
short-lived V4 signed URLs; its self-signing permission cannot add storage authority it does not already possess.
The Job uses a bounded in-memory workspace, a digest-pinned minimal image, a non-root tenant child process, direct
argv, resource/time/log/output ceilings, and immutable result evidence. Temporary transfer objects expire under the
bucket's 30-day cleanup rule. A future project-pool allocator will tighten the boundary from one separate untrusted
execution project to Google's recommended one-project-per-paying-tenant model.

The generated-app project contains only a seven-day source-staging bucket, a customer-image registry, the minimal
build and zero-role runtime identities, and dynamically created Cloud Run services. The control worker can
create/get/list builds and create/get/update services but cannot delete them. It can act as only the two generated-app
identities; the build identity cannot deploy, and generated code cannot read its source bucket or push images.

The active cell also creates one-minute HTTPS uptime checks against the customer-facing `/ready` endpoint and the
separate generated-app `/health` endpoint from
USA, Europe, and Asia-Pacific, logs failed probes, and opens a critical alert after multiple regions fail for two
minutes. Pass one or more existing Monitoring notification-channel resource names to
`TF_VAR_alert_notification_channels`; the policy is still created without a destination so missing paging wiring is
visible in infrastructure state rather than silently invented. Test every configured channel before launch.

## Generated-application emergency control

The cell creates a dedicated incident-operator service account with no Secret Manager, database, build, artifact
deletion, or control-plane authority. It can atomically change only static route pointers and can get/update (but
not create or delete) generated Cloud Run services. Grant a named on-call principal
`roles/iam.serviceAccountTokenCreator` on that one service account after the external identity is known; do not
download a service-account key.

During a declared incident, authenticate with Application Default Credentials by impersonating the
`incident_operator_service_account` output, retain the incident ticket in `--reason`, and fence only the opaque
route or deterministic service named by the deployment receipt:

```bash
export AOS_V2_PUBLISHED_APP_BUCKET="$(tofu -chdir=deploy/gcp output -raw published_app_bucket)"
export AOS_V2_APP_PROJECT_ID="$GCP_APP_PROJECT_ID"
export AOS_V2_APP_REGION="$GCP_REGION"
INCIDENT_SA="$(tofu -chdir=deploy/gcp output -raw incident_operator_service_account)"
gcloud auth application-default login --impersonate-service-account="$INCIDENT_SA"

agentos-v2 deployment-control --kind static --action suspend \
  --target "$OPAQUE_ROUTE_ID" --reason "INC-123 containment" --actor "$ON_CALL_IDENTITY"
agentos-v2 deployment-control --kind service --action suspend \
  --target "$GENERATED_SERVICE_NAME" --reason "INC-123 containment" --actor "$ON_CALL_IDENTITY"

# Restore only after the incident decision is recorded.
agentos-v2 deployment-control --kind static --action resume \
  --target "$OPAQUE_ROUTE_ID" --actor "$ON_CALL_IDENTITY"
```

Static suspension is stored in the generation-guarded route pointer and fences both the stable URL and fresh
requests to every immutable revision while retaining release objects for forensics. Cloud Run suspension restores
IAM enforcement by setting `invokerIamDisabled=false`; resumption reverses that exact field. Both operations are
idempotent, bounded, and appear under the assumed identity in Cloud Audit Logs. Save the command's JSON result with
the incident record.

The script defaults GitHub repository ID to this repository's immutable ID (`1276674620`), not its reusable name.
After the first apply, set these GitHub repository/environment variables from the OpenTofu outputs:

- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_DEPLOY_SERVICE_ACCOUNT`
- `GCP_PROJECT_ID`, `GCP_SANDBOX_PROJECT_ID`, `GCP_APP_PROJECT_ID`, `GCP_REGION`, and `GCP_STATE_BUCKET`
- `GCP_DNS_MANAGED_ZONE` when the two public hostnames are managed by Cloud DNS; otherwise leave it empty
- all non-secret `AOS_V2_OIDC_*`, Stripe price, control/app public URLs, and model values used above
- optional `AOS_V2_APP_BUILDER_IMAGE` only when intentionally replacing the repository's reviewed pinned default
- `AOS_ALERT_NOTIFICATION_CHANNELS`, a JSON list of full Monitoring notification-channel resource names (or `[]`)

Runtime credentials stay in GCP Secret Manager and are never copied into GitHub. The worker identity cannot read
Stripe secrets; the API identity cannot read the model-provider key. GitHub deployment can update Cloud Run and
submit builds but cannot read secret payloads.

`.github/workflows/deploy-gcp.yml` is the installed, manual-only production workflow. GitHub's `production`
environment is the deployment authority boundary. The workflow refuses a commit unless its existing `test` and
`container` checks are green, then repeats the offline launch preflight, OpenTofu validation, exact-commit build,
migration-first release, and deployed DNS/TLS/readiness check.

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

Rollback updates only the API, worker, and public app router to the prior digest and verifies both origins. It deliberately neither changes
nor executes the migration Job. Database revisions must therefore follow the documented expand/contract contract;
destructive schema reversal is an incident-specific, reviewed recovery action rather than an automated rollback.

The GCP plane does not make the current product fully launch-ready by itself. A real domain, OIDC organization
tenant, Stripe products/webhook, managed PostgreSQL, model key, per-paying-tenant generated-app project allocator,
artifact/image garbage collector,
backup/restore drills, notification-channel delivery, and an external smoke test
must still be configured or proven.
