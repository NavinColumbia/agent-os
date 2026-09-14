# Local first-user rehearsal

This profile runs the production-shaped V2 CEO workspace on WSL loopback and uses the already authenticated local
Codex CLI through ChatGPT subscription access. It exists to let the founder evaluate the real user journey before
creating cloud, identity, billing, database, DNS, or model API accounts.

```bash
deploy/founder-local.sh up
```

Open the displayed `http://127.0.0.1:8088/app` address and paste the signed token from the displayed owner-only
invitation file. Requests enter through the same authenticated V2 directive API, PostgreSQL lifecycle/outbox,
durable worker, mission planner, recursive workflow graph, management monitor, human-decision mechanism, evidence
store, and CEO projections used by the hosted architecture. Local preview deployment and a networkless Docker
sandbox are enabled. The launcher builds and pins a local Playwright/Chromium sandbox image so rendered desktop
and mobile checks do not silently degrade to source inspection. OIDC, Stripe, external connectors, public ingress,
and cloud deployment remain disabled.

Rendered evidence remains bounded: the local profile permits at most 8 MiB from one sandbox run inside a 32 MiB
ephemeral workspace, backed by a 16 MiB per-artifact SQL limit. Oversized evidence still fails closed.

The subscription bridge is deliberately rejected when the environment is production, billing is enabled, identity
is not a signed local invitation, or the application URL is not loopback. It never copies the Codex login token into
the application or a container. Each model turn starts an ephemeral, schema-constrained, read-only Codex process in
an isolated temporary directory. The bridge uses medium reasoning effort plus deterministic validation and bounded
self-repair so the founder preview favors a responsive loop without bypassing correctness checks.
The local transport permits a long-running model call to use up to 15 minutes; workflow iteration and total-node
bounds, rather than a short wall-clock cutoff, prevent endless work.

```bash
deploy/founder-local.sh status
deploy/founder-local.sh logs
deploy/founder-local.sh restart-worker
deploy/founder-local.sh down
```
