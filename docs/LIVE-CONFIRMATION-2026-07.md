# Live confirmation — the company org runs end-to-end, unattended

**2026-07-27** — a real CEO directive drove a durable multi-agent company to completion with
NO human intervention, proving the core vision live (not stubbed).

- Directive: "Assess whether to build an AI code-review SaaS … go/no-go with a 60-day plan."
- Org: CEO-coordinator → research + finance + synthesis + artifact coordinators → 9 worker teams
  (14 actors total). 8 agents concurrent, real web research.
- Result: run **done**, unattended. Deliverables written: INVESTMENT-MEMO, differentiation-strategy,
  market-research. CEO produced a board-ready executive synthesis (conditional GO, real unit economics,
  honest self-flagged limitations).

## Bugs the live runs found + fixed (that stubbed tests could not)
1. **run_org lease-stall** — a pool thread claimed then abandoned an event; the 900s lease hung the org
   ~15 min. Fixed:  at run_org startup (self-heals in seconds).
2. **Agent auth went fleet-wide-stale** —  symlinked host creds once; claude's
   atomic-rename-on-refresh left a week-old copy, so every agent failed "OAuth session expired". Fixed:
   sync host creds into the isolated dir whenever newer.
3. **Thin CEO synthesis** — the aggregate prompt returned empty. Fixed: demand a substantive executive
   synthesis + widen the results context.
Also proven live: durability + crash-resume (a mid-run driver kill resumed to completion) and billing attribution.
