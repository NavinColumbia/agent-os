# platform/ — rebuild anywhere · snapshot · migrate to cloud

This directory is the **single source of truth for deploying agent-os** — on a fresh laptop, a buyer's
machine, or the cloud. Nothing here is laptop-specific; everything is config + code.

```
platform/
  inventory.yaml     ← THE component registry (local form, cloud target, what a snapshot captures).
                       selftest.sh asserts it covers every running container — it can't go stale.
  rebuild.sh         ← one command: fresh box → full stack up → migrated → seeded → self-tested green.
  migrate.sh         ← apply every Postgres migration idempotently (ordered, safe to re-run).
  snapshot.py        ← ONE encrypted .aosnap file = your whole brain (DB + secrets + keys + provenance
                       + repo SHAs). Carry it to any box and restore.
  terraform/         ← cloud IaC skeleton (RDS + S3 + Secrets Manager). plan-only; apply when you scale.
```

## Move to a new laptop (few steps)
On the **old** box:
```bash
AOSNAP_PASS='a-strong-passphrase' ~/projects/agent-os/.venv/bin/python platform/snapshot.py export
#  -> backups/agent-os-<ts>.aosnap   (copy this file + remember the passphrase, stored separately)
```
On the **new** box:
```bash
git clone <agent-os repo> ~/projects/agent-os
git clone <control-plane repo> ~/projects/control-plane
cd ~/projects/agent-os && bash platform/rebuild.sh          # stack up + proven (fresh, empty data)
AOSNAP_PASS='...' .venv/bin/python platform/snapshot.py import <file.aosnap> --apply   # restore real data
bash scripts/selftest.sh && .venv/bin/python scripts/provenance.py verify
```
That's it: same governance, same audit chain, same memory/skills, same signing keys.

## Sell it / set up on a buyer's machine
Ship the two git repos (no secrets in them) + `platform/rebuild.sh`. The buyer runs `rebuild.sh` and
gets a clean, self-tested install with **their own** freshly generated secrets — no data of yours
travels unless you hand them an `.aosnap` and its passphrase.

## Scale to cloud (when one box isn't enough)
1. `cd platform/terraform && terraform init && terraform plan` — review RDS + S3 + Secrets Manager.
2. `terraform apply`; take the outputs.
3. Point `.env.local` at them: `DATABASE_URL` → RDS, `OBJSTORE_BACKEND=s3` + bucket, secrets ARN.
4. `bash platform/migrate.sh` against the managed DB; containerize controller/api (same code).
See `../docs/CLOUD-MIGRATION.md` for the per-component mapping (this README is the *how*, that doc is
the *what maps to what*).

## Backups (routine, not just migration)
`snapshot.py export` is also your backup. Schedule it (`scripts/scheduler.py`) and copy each `.aosnap`
offline. Each file is self-describing (MANIFEST.json inside) and pinned to exact git commits, so a
restore reproduces the *code* state too, not just the data.

## Rules that still hold everywhere
Never bind `0.0.0.0` (localhost + tailnet only), never commit `.env*`/`keys/`, secrets travel only
inside an encrypted `.aosnap`. These survive the move — they're enforced by the manifests + gitignore,
which come with the repos.
