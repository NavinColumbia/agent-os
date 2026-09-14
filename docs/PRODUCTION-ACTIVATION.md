# Production activation boundary

This is the handoff for the first-customer Agent OS V2 cell. The repository-side launch path is reproducible and
credential-independent: the runtime, migrations, control API and CEO workspace, durable workers, tenant isolation,
mission-program admission, recursive subprograms, human decisions, management monitoring, usage limits, billing,
connectors, artifact retention, sandboxing, generated-app deployment, rollback, emergency suspension, monitoring,
and CI contracts are implemented and exercised without using a real customer or cloud credential.

The production image contains only `src/agent_os`; the overlapping legacy `scripts/` runtime is not copied into or
started by the GCP serving plane. This gives launch-critical traffic one durable implementation rather than two
competing state machines.

## The remaining activation inputs

These are external resources, not unfinished repository logic:

- three distinct billed GCP projects (control, untrusted sandbox, generated applications) and an authenticated
  deploy principal;
- a managed PostgreSQL database with separate migration-owner and constrained runtime logins;
- two DNS names, with either a Cloud DNS zone or the ability to add the emitted A records;
- an OIDC issuer, public PKCE client, API audience, endpoints, and callback registration;
- two live Stripe prices, a live API key, and the webhook signing secret;
- one supported platform model and key: OpenAI, Anthropic, or Google/Gemini;
- at least one tested Cloud Monitoring notification channel;
- a named on-call principal to impersonate the generated-app incident service account;
- founder/company facts that software cannot invent: legal entity/name, jurisdiction, customer terms/privacy
  approval, support owner, risk/budget authority, and any domain-specific licensed data, capital, specialists, or
  physical operations a customer mission requires.

No service-account key needs to be downloaded. Runtime secrets go directly to Secret Manager and never enter
OpenTofu state or GitHub.

## One-command activation path

```bash
mkdir -p .runtime
cp deploy/gcp/launch.env.example .runtime/gcp-launch.env
chmod 600 .runtime/gcp-launch.env
# Replace every CHANGE_ME using the external inputs above.
deploy/gcp/deploy-from-env.sh
```

The wrapper runs the redacting offline preflight before any cloud mutation. The deployment then bootstraps remote
state, builds the exact Git commit into digest-addressed images, migrates before traffic activation, applies the
three-project cell, waits for DNS/TLS, and checks the public API and generated-app health endpoints. Repeat releases
can remove secret values from the local file and use:

```bash
deploy/gcp/deploy-from-env.sh --reuse-existing-secrets
```

For a no-mutation inventory of precisely what is still missing on a machine, run:

```bash
.venv/bin/python deploy/gcp/launch_preflight.py \
  --env-file .runtime/gcp-launch.env --require-bootstrap-secrets
```

## Capacity boundary

The bootstrap cell is intentionally economical for the first tens of customers: Cloud Run API and generated-app
router scale from zero, one durable worker pool coordinates through PostgreSQL leases, and every service has a hard
instance ceiling. The architecture is horizontally partitionable, but “millions of users” is a measured scaling
stage—not a launch slogan. Before raising those ceilings, load-test observed traffic, allocate sandbox/generated-app
projects by risk tier, add database replicas/partitioning when measurements require them, and run restore and
regional-failure drills in the real cloud account. Those are demand- and credential-dependent operations; they do
not require replacing the lifecycle, graph, policy, artifact, or adapter contracts.

The platform can plan and drive arbitrary goals by discovering missing resources/capabilities and routing typed
human or external boundaries. It cannot guarantee outcomes outside anyone's control. What it does guarantee at the
software boundary is durable ownership, bounded authority, evidence-backed completion, explicit blockers, safe
retry/replanning, and no silent conversion of a missing credential, person, approval, dataset, or budget into a
false success.
