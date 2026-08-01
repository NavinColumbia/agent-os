# Deep end-to-end build runbook (full CEO pipeline, live)

Drives the FULL loopcontroller pipeline unattended via demo_e2e.py: DISCOVER → multi-agent RESEARCH →
OPTIONS → DEEP_DESIGN → PROTOTYPE → IMPLEMENT (project.build_complex = multi-component, multi-engineer,
recursive DAG) → TESTQA (coverage-driven browser QA + dev-fix loop) → DELIVER.

## Preconditions (learned the hard way)
The demo tenant MUST be a fully registered tenant, not just consent+provider:
1. Row in `tenants` (else factory's quota gate fails: "no such tenant"). Use a generous plan
   (enterprise = 100M tokens) for a deep build.
2. `consent.record(tenant, 'anthropic')` — AI-processing consent on file.
3. `tenantproviders.connect(tenant, 'anthropic', mode='subscription')` — runs on the host CLI login (no key).
Verify: `billing.quota(tenant)` returns a dict (not "no such tenant") and `tenantproviders.resolve(tenant)`
shows auth_mode='subscription'.

## High-limit env (real-user-session-grade depth)
AOS_BUDGET_USD=60 (→ 30 agents, depth 3, exploration 2), AOS_MAX_AGENTS=30, AOS_MAX_DEPTH=3,
AOS_FLEET_WORKERS=5, AOS_QA_PARALLEL=4, AOS_QA_MAX_STORIES=0 (unlimited), AOS_STORY_SATURATE_MAX=60,
AOS_QA_MAX_STEPS=400, AOS_QA_STALL_STEPS=60, AOS_QA_DECIDE_LIGHT=0 (full-model QA judgment),
AOS_QA_MAX_ROUNDS=12, AOS_DISPATCH_PARK=1.

## Gotchas
- Don't kill agents by grepping the product topic — research/QA agent PROMPTS contain the topic (e.g.
  "kanban"), so `pkill -f kanban` kills your own run's agents. Target by PID or by role uniquely.
- The scheduler's resume-sweep auto-resumes any factory build that finished BUILD without a terminal
  verdict — so a killed mid-QA build gets relaunched. Mark it done (`ProductComplete` audit) or let it finish.
- A parked worker survives its parent (dispatch-and-park) — killing the launcher does NOT stop the fleet;
  kill the parked run_job worker too.
