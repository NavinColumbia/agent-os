# Public deployment

This profile exposes only the tenant-authenticated console through Caddy. PostgreSQL, Cerbos, ntfy,
the execution daemon, metrics, and internal dashboards stay on loopback. Caddy obtains and renews TLS
certificates automatically after the hostname's DNS records point to the host.

## Inputs required once

1. A Linux host with Docker Engine + Compose, Python 3.12+, systemd, and inbound TCP 80/443. A host Caddy
   installation is optional: the installer uses the pinned Docker Caddy edge when the binary is absent.
2. `agent-os` and `control-plane` checked out beside each other (or set
   `AOS_CONTROL_PLANE_ROOT`).
3. A DNS hostname in `AOS_PUBLIC_HOST`.
4. SMTP or SendGrid credentials for verification/reset messages.
5. Stripe live secret, webhook secret, and price IDs for `pro` and `enterprise`.
   To collect the founding $500 Release Assurance audit directly, also create a Stripe Payment Link and set
   `AOS_ASSURANCE_PAYMENT_URL`; the application accepts only `https://buy.stripe.com/...` links.
6. Model-provider credentials can be connected per tenant in onboarding; a platform-wide provider is
   optional.
7. A mounted off-host backup directory in `AOSNAP_OFFSITE_DIR`; keep `AOSNAP_PASS` separately from the
   host. The installer refuses a same-host-only public launch.

Copy `deploy/public.env.example` to `.env.local`, replace every public-launch placeholder, point Stripe's webhook at
`https://<host>/api/stripe/webhook`, then run:

```bash
cp deploy/public.env.example .env.local
# edit .env.local
bash deploy/install-public.sh
```

The installer is fail-closed: it stops on active durable work before applying catalog changes, or on a failed
container, ordered migration, tenant-isolation gate, manifest validation, self-test, credential preflight,
Caddy validation, or console health check. It does not create DNS records, Stripe products, email accounts,
or provider accounts.

Useful checks:

```bash
.venv/bin/python deploy/preflight.py
systemctl status agentos-supervisor
# Host-Caddy installation:
systemctl status caddy
# Docker-Caddy fallback:
docker compose --env-file .env.local -f deploy/docker-compose.public.yml ps caddy
journalctl -u agentos-supervisor -f
curl -fsS https://$AOS_PUBLIC_HOST/health
curl -fsS https://$AOS_PUBLIC_HOST/assurance
```

Rollback is ordinary service control: stop host Caddy, or run
`docker compose --env-file .env.local -f deploy/docker-compose.public.yml stop caddy`, to remove public reach
while leaving durable workflows and data intact. The scheduler creates a daily encrypted `.aosnap` containing PostgreSQL, secrets/signing
keys, and product source repositories (dependency/build caches excluded), retains 14 copies, and atomically
copies each artifact to `AOSNAP_OFFSITE_DIR`. Verify a real archive—including an isolated database restore—via:

```bash
.venv/bin/python platform/snapshot.py verify backups/agent-os-YYYYMMDD-HHMMSS.aosnap --restore-drill
```

Restore remains an explicit downtime operation:

```bash
sudo systemctl stop caddy agentos-supervisor  # omit caddy when using the Docker edge
.venv/bin/python platform/snapshot.py import /path/to/agent-os-....aosnap --apply
bash scripts/selftest.sh
sudo systemctl start agentos-supervisor caddy # omit caddy when using the Docker edge
```
