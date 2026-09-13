# Zero-fixed-cost laptop pilot

This profile publishes the V2 CEO workspace from one Windows/WSL laptop for a small, invite-only evaluation. It
uses Docker Compose for the application cell and the existing Tailscale Funnel for a stable public HTTPS URL. The
tunnel is outbound, so the router needs no port forward, public IP, DNS record, or Windows inbound firewall rule.

It is intentionally a **pilot**, not the paying-customer production cell:

- The public API binds only to WSL loopback. Funnel is the only public ingress.
- Access requires a signed, expiring invitation. Tokens stay in browser memory and are not persisted.
- Billing is disabled. The UI cannot claim a paid entitlement.
- PostgreSQL data and artifacts live on the laptop. Container restart policies recover after WSL starts, but sleep,
  power loss, internet loss, Windows Update, disk failure, or theft causes an outage or data loss.
- The worker can run generated tests in networkless, resource-capped Docker containers. Its narrowly used Docker
  socket is still a larger host boundary than the separate GCP sandbox project and must not be treated as hostile
  multi-tenant isolation.
- Local releases are revocable, seven-day preview URLs. Durable static/backend production publication remains
  disabled, and the mission planner is told only about tools that actually exist in this cell.
- Gemini free-tier prompts may be used by Google to improve its products. Use synthetic/non-confidential prompts
  until a paid provider/data-processing posture is configured.

## Start

```bash
deploy/local-pilot.sh init
# Put a restricted Gemini auth key in .runtime/local-pilot/pilot.env.
deploy/local-pilot.sh preflight
deploy/local-pilot.sh up
```

`up` builds the pinned images, applies V2 migrations, starts the API/worker, verifies local and public readiness,
enables Funnel on port 10000, and writes a 24-hour founder invitation to an owner-only file. It never prints the
token. Open the reported `/app` URL and paste the token from that file into the connection form.

Create another short-lived invitation without sharing the founder credential:

```bash
deploy/local-pilot.sh invite alice acme-pilot 86400
```

Each organization ID is a separate tenant. The worker discovers its queued work through the durable database.

Operate or stop the cell:

```bash
deploy/local-pilot.sh status
deploy/local-pilot.sh logs
deploy/local-pilot.sh down
```

`down` removes public reach and stops the containers while preserving the PostgreSQL volume. Keep Windows awake,
plugged in, and connected during an evaluation. Move real customers to the GCP cell before accepting confidential
data, payments, uptime commitments, or arbitrary public signup.
