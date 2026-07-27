# Live confirmation — the company org runs end-to-end, unattended

**2026-07-27** — a real CEO directive drove a durable multi-agent company to completion with **no human
intervention**, proving the core vision *live* (not stubbed).

- **Directive:** "Assess whether to build an AI code-review SaaS for small teams … go/no-go with a 60-day plan."
- **Org:** CEO-coordinator → research + finance + synthesis + artifact coordinators → 9 worker teams
  (14 actors total), 8 agents concurrent, real web research.
- **Result:** run **done**, fully unattended. Deliverables written to disk: an investment memo, a
  differentiation strategy, and market research. The CEO produced a board-ready executive synthesis
  (Conditional GO; real unit economics — ~72% margin typical vs ~−230% if a heavy user hits Opus; and
  **honest self-flagged limitations**: contradictory vendor benchmarks, no cost ledger for burn/runway,
  single-pass memo to verify before capital).

## Bugs the live runs found + fixed (that stubbed tests could not)
1. **run_org lease-stall** — a pool thread claimed then abandoned an event; the 900s claim lease hung the
   org ~15 min. Fixed: `store.release_stale_claims` at `run_org` startup (self-heals in seconds).
2. **Agent auth went fleet-wide stale** — `_agent_config_dir` symlinked host creds only once; claude's
   atomic-rename-on-refresh left a week-old copy, so every spawned agent failed "OAuth session expired."
   Fixed: copy the host creds into the isolated dir whenever they are newer.
3. **Thin CEO synthesis** — the aggregate returned an empty result. Fixed: the aggregate prompt now demands a
   substantive, CEO-actionable executive synthesis, with a wider results context.

Also proven live across the two runs: **durability + crash-resume** (a mid-run driver kill was resumed to
completion by a fresh process) and **billing attribution** on real agents.

## What this means
The core of the North Star — *a fleet of elite AI agents that, from one CEO prompt, self-organize into a
company and deliver real work end-to-end* — is demonstrated live, clean, and unattended. Remaining work is
enterprise-scale hardening (Postgres RLS for storage-enforced multi-tenant isolation; audit-key to KMS;
billing ±1% accuracy) — larger infra efforts tracked in `SYSTEM-AUDIT-2026-07.md`, best done with oversight.
