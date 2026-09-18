# Production Readiness Gap Map

Last updated: 2026-09-17

## 2026-09-17 Authoritative Human Collaboration Closure

This increment removes notification fan-out as implicit human-decision authority. Migration 109 adds a
tenant-fenced human-request ledger with one authoritative recipient address and explicit advisory versus
workflow-blocking semantics. Advisory requests continue work; governed workflow decisions retain durable
response, failure, redrive, supersession, and terminal-closure state. Legacy notices remain readable but do
not silently invent authority.

- The CEO workspace can select an exact active mission participant, preserve the request draft, and create a
  non-blocking request. The addressed person answers from the personal inbox; requester and monitoring
  fallbacks can see status but cannot impersonate the recipient.
- Human waits are rejected before `NODE_WAITED` unless they have exactly one recipient. Terminal graph runs,
  cancelled/succeeded lifecycle runs, and nonrecoverable lifecycle failures close stale requests; recoverable
  failures keep requests open. Failed recorded responses can be redriven only while the request remains
  recoverable.
- Human-request content never leaves through webhook, chat, email, or push delivery, even when a route is
  configured for full payloads. External delivery is an opaque reopen-the-app hint.
- A real PostgreSQL proof applied migration 109 using the local migration owner after the runtime role was
  correctly denied DDL. Under `agentos_app`, tenant A saw its rollback-only proof row and tenant B saw zero;
  `agentos_worker` had neither SELECT nor INSERT while `agentos_app` retained governed UPDATE.
- The frozen repository suite passes **1,500 tests with 2 intentional skips**. The invariant security scan
  reports zero findings. The headless CEO/reviewer proof reports request creation, exact participant routing,
  inbox response, and idempotency all true.
- Both pinned production images build. The application image runs as UID/GID `10001:10001`, and its packaged
  `agentos-v2` CLI smoke passes. The migration image contains revision 109.

This closes the repository-owned human-request slice, not public activation. External cloud/OIDC/DNS/payment/
provider credentials and production monitoring destinations remain deployment inputs, and commercial revenue
remains $0 until a customer purchases and uses the service.

## 2026-09-14 V2 Production-Readiness Closure

This section supersedes every older readiness statement below. Historical sections remain for audit context,
not as the current launch verdict.

- A fresh one-prompt TrailPaws mission completed through the durable V2 graph: graph run
  `mission-run-a955d9cac6b4e9bea4669fc183234a22`, lifecycle
  `run-1ce373b8546200c7ce89557c1322bab8`, and workflow
  `mission-202d6c8f9cece7428c74096166caf5ef` all reached `succeeded`. The final accepted workflow was revision
  3 at state version 102.
- The released preview was fetched independently over HTTP with status 200. Its exact 7,068-byte HTML payload
  has SHA-256 `7b0b85a1766ae095ad59fef9df58ba613fd8725afcf7d094154e03f13c10aacf`; the acceptance graph retained the
  fetch evidence, publication receipt, and released artifact.
- Workflow revision corruption, lost-model-result recovery, evidence hydration, oversized QA context,
  screenshot/evidence overhead, failed-node recovery, stale lifecycle projection, and scheduler alert
  truncation now have bounded implementations and regressions. Preview publishing consumes canonical source
  bundles directly, while rejected graph revisions may use a bounded merge patch that is materialized and
  fully revalidated by the authority layer.
- A clean empty PostgreSQL replay exposed legacy tables that had depended on Python import order. The task
  board, controller state/jobs, organizations, tenant runtime tables, and hire-request tenant/RLS contract are
  now explicit ordered migrations. The same clean-state environment passes 1,361 tests with one intentional
  skip, the zero-finding security scan, production OpenTofu validation, and runtime/migration/sandbox container
  build and entrypoint smoke tests.
- Verified GitHub evidence for the pre-documentation release is CI run `34836881970` at commit `e2bac1e`; the
  current revision must retain the same green gates before release. The local founder rehearsal is healthy at
  `http://127.0.0.1:8088/app` with PostgreSQL and the subscription-backed worker running.
- Repository-owned production work is closed for the first-customer V2 cell. Public activation is not yet
  performed: the remaining boundary is external GCP projects/deploy identity, managed PostgreSQL principals,
  DNS names, OIDC application, Stripe live configuration, a model-provider credential, and a tested monitoring
  destination. See `docs/PRODUCTION-ACTIVATION.md` and `deploy/gcp/launch.env.example`.
- Commercial outcomes remain separate from software readiness: revenue is still $0, no customer adoption or
  investment return is guaranteed, and no investment capital has been traded.

## 2026-08-25 Completion-Audit Supersession

This update supersedes the older statements below that call the dog-app campaign incomplete or waiting on
additional spend authority.

- Durable source run `4017` completed all twelve browser stories. Report-only continuation `4184` reused the
  exact grounded evidence ledger with zero browser explorers, and product-owned gate `1138` reports 12/12
  passed, zero open bugs, and zero blocking findings at executable revision
  `3f6b4aa5bc82d6f33c8c874dc7954515966499656ef9a6e33246e426316a1a20`.
- The current dog-walking product suite passes 174/174. The final Agent OS suite passes 1,025 tests with one
  intentional skip and zero failures. The public Release Assurance build/typecheck and post-deployment route,
  security-header, evidence, health, and fail-closed intake verifier also pass.
- QA continuation no longer generic-decomposes a settled campaign, final reporting composes exact adjudicated
  findings instead of trusting stale immutable counters, and revision-fenced evidence can be reused without a
  browser replay only when the audited impact policy permits it.
- PostgreSQL sentinel recovery now uses `pg_stat_activity.state_change` for the idle-in-transaction threshold,
  so a backend that just completed a long valid query is not killed merely because `query_start` is old. Audit
  verification releases its statement snapshot before the CPU-bound HMAC walk.
- Public Sites version `3` at
  <https://agent-os-release-assurance.artmusicasia.chatgpt.site> publishes the completed 12/12 proof. The
  fail-closed revenue preflight reports no system-owned gaps. Turning lead capture into outbound delivery and
  checkout now requires only the founder-owned payment, mail, sender/inbox, legal-name, jurisdiction, and
  provider-contact configuration.
- Commercial state remains separate from engineering readiness: revenue is $0, outreach sent is 0, and paid
  customers are 0. No investment capital was traded.

This maps current evidence against the Founder Intent / North Star: a governed OS for AI companies where a
CEO can delegate real business outcomes to durable AI teams that communicate, recover, and refuse silent
failure.

## 2026-08-16 Completion-Audit Update

- The current complete platform suite passes **564/564** with restricted database roles enabled by default;
  the dog-walking product suite passes **159/159**.
- Tests now run with a process-wide no-external-notification invariant. A before/after live delivery-ledger
  check around the 564-test run remained exactly unchanged at 3,386 accepted operator pages (latest acceptance
  2026-08-16 13:57:54 UTC). A selftest pager-stub leak found by that full run was repaired and regression
  covered, so one in-process selftest cannot silently replace the real guarded transport for later callers.
- PostgreSQL tenant RLS is now installed and runtime-enforced: `tenant_connection()` defaults to
  `agentos_app`, audit appends default to `agentos_audit_writer`, and migration 77 grants every sequence owned
  by a tenant table (including non-`id` columns such as orchestra `run_id`/`actor_id` and conversation
  `turn`). Migration 78 additionally makes resolved human decisions replayable until controller side effects
  are durably acknowledged. `rls_readiness.py rollout-gate` passes all five migration, real-table,
  real-module, app-role, and static-path proofs. An explicit `AOS_DB_APP_ROLE=off` is break-glass only.
- Migration 79 canonicalizes `org_artifacts` before tenant writes, enforces non-null ownership and RLS, and
  reconciles 217 legacy rows whose parent organizations no longer exist into an owner-only forensic archive.
  The archive has no app-role exposure; its complete payload digest and reconciliation count are recorded in
  the tamper-evident audit chain.
- Release/selftest work is durably excluded from production controller and management queues. The release
  harness is globally no-spend, work contracts carry an execution scope, and scheduled management claims
  require a live tenant. Historical leakage was reconciled (55 orphan contracts, 60 cases, 69 questions); the
  next live management cadence completed model-free in 1.16 seconds with an empty decision set.
- The North-Star automatic scorecard passes **24/24**. Its approvals proof is now genuinely no-spend: selftest
  retry approval cannot launch a detached factory build and no longer uses broad `pkill` cleanup.
- Fifteen host services were exact-identity restarted after the rollout and all report healthy, including the
  phone reply bridge and both CEO-cockpit services. Watchdog remediation now routes exclusively through the
  exact service registry instead of raw command matching/spawn. Ticker,
  watchdog, and dispatcher now terminate their tracked child and exit immediately on TERM; a controlled live
  proof stopped each gracefully in 0.065–0.067 seconds and restarted every service under a new exact process
  generation. The read-only process assurance audit reports zero critical findings and zero warnings.
- Controller thread 2787 remains safely checkpointed at TESTQA with no active job or expensive child process.
  The immutable tracked model-cost estimate is $519.56 against its $500 cap; authority request 790 asks for a
  fixed $650 tracked-ledger cap. The campaign and subsequent clean one-shot proof remain incomplete until the
  CEO explicitly grants or declines that fixed spend authority.
- The halted QA campaign is now proven resumable without regenerating its paid story corpus: the v3 checkpoint
  validates tenant/product/target/thread/vision/repository/full-manifest identity, reuses the original 12-story
  coordinator manifest, revives only actors stopped by the exact automatic spend-safety reason, and never
  revives an explicit user cancellation. A dry resume simulation restores the seven interrupted explorers and
  durable remaining-story cursor; focused checkpoint/resilience/fault tests pass 37/37.
- Scheduled proactive communication now uses a production execution scope and a recent-controller/open-request
  eligibility lane instead of sweeping 1,754 historical synthetic tenants. The current sweep inspects the one
  real open authority request rather than paging test history. Alert migration 80 similarly preserves all 341
  historical rows as `legacy`; scheduled SLA claims are production-only and currently report zero production
  alerts due. Test-created alerts are durably tagged `test`, not merely filtered by naming convention.
- Migration 81 closes the equivalent human-request leak. It retained 179 unprovable historical open requests
  as `legacy`, inherited scope only through a durable linked controller, and left exactly one open production
  request: fixed-cap authority request 790. Scheduled priority and Approvals views now accept execution scope;
  the production view contains only request 790, while explicit history reads can still inspect legacy rows.

The older evidence below is retained as history. Where it says RLS was staged but not deployed, this update
supersedes it.

## Proved Offline

- North-star automatic scorecard: `scripts/northstar_accept.py report` passes `24/24` auto proofs, with
  live-only proofs still listed separately.
- Core regression suite: `python -m pytest tests/test_core.py -q` passes `111/111` (zero failures/skips,
  50.88s on the current WSL host).
- Bounded fast release gate: `AOS_FAST_GATE=1 SELFTEST_JOBS=2 CKP_TIMEOUT=180 bash scripts/selftest.sh`
  passes `146/146` with zero failures, without starting a live model/browser product run.
- Async trust/ping regression pass: `scripts/notifications.py selftest`, `scripts/loopcontroller.py selftest`,
  and `scripts/rls_readiness.py enforced-module-smoke` pass after urgent build notification fanout was wired
  through the notification taxonomy.
- Live research progress: `loopcontroller.live_status()` now reads durable orchestra actor rows for an active
  research run and returns real researcher counts, blocked/active state, current assignment, and sub-progress
  percent; the console progress bubble renders that detail and a stable progress bar. `loopcontroller.py
  selftest` proves `1/3 researchers done, 1 blocked, 1 active · payment providers` style narration without
  model spend. The path now accepts tenant context and is covered by `rls_readiness.py enforced-module-smoke`
  under `agentos_app`: tenant A sees its live research progress, tenant B cannot read tenant A's thread.
- Durable org runtime: actor/event rows, crash resume, kill switch, governance hire requests, and cross-process
  actor single-flight are covered by `scripts/orchestra/store.py selftest` and
  `scripts/orchestra/runtime.py selftest`.
- Silent-failure observer: `scripts/sentinel.py selftest` now covers stale workflows, hung agents,
  provider-burst, stale actor heartbeat, and stale actor step lease.
- Agentic QA default: `qa_run(..., agentic=None)` defaults to durable `qa_agentic.run_agentic_qa`; rollback is
  `AOS_QA_AGENTIC_DEFAULT=0` or `agentic=False`.
- Research default: `research_org.run_research()` defaults to crash-resumable `run_org`; rollback is
  `AOS_RESEARCH_RUNORG=0`.
- Provider path: platform default is Codex (`AOS_DEFAULT_ENGINE=codex`), frontdoor gates missing providers
  before build spend, and incident/provider bypasses route through governed `factory.agent`.
- RLS preflight: `scripts/rls_readiness.py selftest` passes, and `scripts/rls_readiness.py apply-indexes`
  has created/confirmed tenant indexes for all current direct tenant-scoped tables, including the legacy
  `task_board.tenant` alias.
- RLS tenant spine prep: `postgres/initdb/54-rls-tenant-spine.sql` adds nullable `tenant_id` columns,
  backfills, indexes, and triggers for parent-scoped product/org/evidence tables such as `traces`,
  `qa_runs`, `quality_runs`, `research_options`, `findings`, and `app_registry`. It has been applied
  locally without enabling RLS.
- RLS fabric spine prep: `postgres/initdb/55-rls-fabric-spine.sql` adds nullable `tenant_id` columns,
  backfills, indexes, and directory-derived triggers for legacy durable fabric tables: `conversations`,
  `inbox`, `orchestra_messages`, and `waits`. A live transaction sanity proof confirmed conversation,
  inbox, and wait rows inherit tenant ownership from `directory`.
- RLS pattern harness: `scripts/rls_readiness.py harness` proves the intended non-owner role +
  transaction-local `app.tenant_id` policy behavior on throwaway DB objects: no GUC sees zero rows, tenant A
  sees only A, tenant B sees only B, same-tenant writes pass, cross-tenant writes fail.
- RLS app primitive: `dbpool.tenant_connection(tenant_id)` sets `app.tenant_id` transaction-locally and
  `scripts/dbpool.py selftest` proves the setting does not leak to later pooled/direct borrowers. It also has
  an opt-in `app_role`/`AOS_DB_APP_ROLE` path that runs `SET LOCAL ROLE` before setting the tenant GUC, so
  tenant request paths can be exercised under a non-owner app role before the permanent RLS flip.
- RLS tenant-path conversion started: consent (`ai_consent`), provider registry (`tenant_providers`), durable
  agent questions (`agent_requests`), and the tenant-facing approvals inbox now use
  `dbpool.tenant_connection()` for tenant-known reads/writes. Their focused selftests pass.
- RLS tenant-path conversion continued on the approvals decision surface: tenant-owned dead-letter reads and
  retry/drop decisions now run under `dbpool.tenant_connection()`, and the approvals selftest setup/assertion/
  cleanup path uses the same helper for tenant-owned `hire_requests`, `tenant_products`, `audit_log`, and
  related decision rows. Platform kill-switch lookup remains an explicit operator/exempt path.
- RLS tenant-path conversion continued: settings (`notification_prefs`/tenant profile), push targets
  (`push_targets`), and integrations (`tenant_integrations`) now use `dbpool.tenant_connection()` for
  tenant-known reads/writes. Their focused selftests pass.
- RLS tenant-path conversion continued again: onboarding state (`onboarding_state`), tenant product ownership
  helpers (`tenant_products`), and account export/erasure tenant reads/deletes now use
  `dbpool.tenant_connection()` for tenant-known operations. Bootstrap signup/token lookup remains a separate
  auth/admin path because no tenant GUC exists before identity resolution.
- RLS tenant-path conversion continued on the memory spine: tenant-owned company memory and coordinator
  checkpoints (`company_memory`, `memory_checkpoints`) now use `dbpool.tenant_connection()` for tenant-known
  operations. Shared fleet lessons remain a special policy case because `role_lessons.tenant_id IS NULL`
  is intentional cross-tenant learning.
- RLS tenant-path conversion continued on the billing spine: tenant-known billing period reads, plan lookup,
  usage/invoice/quota, suspend/unsuspend, signup plan update, and settlement now use
  `dbpool.tenant_connection()`. Billing audit writes now pass explicit `tenant_id`, and the factory terminal
  `ProductComplete` audit writer best-effort tags rows from `tenant_products` so tenant-scoped billing can
  still count shipped builds after RLS is enforced.
- RLS tenant-path conversion continued on tenant custom agents: define/list/toggle/delete/run-now and run
  bookkeeping for `custom_agents`/`custom_agent_runs` now use `dbpool.tenant_connection()` when tenant identity
  is known. New recurring scheduler commands include both `tenant_id` and `agent_id`; legacy `run <id>` remains
  as an admin/back-compat path for existing schedules.
- RLS tenant-path conversion continued on tenant UI wrappers: billing plan changes and project list/detail
  reads now use `dbpool.tenant_connection()`. Project drill-in still verifies ownership from
  `tenant_products` before reading traces/audit status, preserving the "not your product" guard under the
  same tenant-scoped transaction.
- RLS tenant-path conversion continued on research state/options: research run creation, report/status
  updates, option-card inserts, run-state reads, option selection, and research audit rows now use
  `dbpool.tenant_connection()` when tenant identity is known. The `research_options` tenant spine derives
  tenant ownership from `research_runs`, so option cards remain tenant-readable after RLS.
- RLS tenant-path conversion continued on product versioning: ownership checks, next-version calculation,
  snapshot metadata writes, version listing, rollback lookup, and rollback audit rows now use
  `dbpool.tenant_connection()` for tenant-known `product_versions`/`tenant_products` paths. Filesystem
  snapshot/restore remains outside the DB transaction, with the existing owner guard and `.git` preservation
  selftest intact.
- RLS tenant-path conversion continued on per-project budgets: ownership checks, cap writes, cap reads,
  tenant product listing, spend reads, and budget audit rows now use `dbpool.tenant_connection()` for
  tenant-known `project_budget`/`tenant_products`/`traces` paths. The appguard policy registration remains
  the enforcement-layer side effect after the tenant-owned cap is stored.
- RLS tenant-path conversion continued on tenant recommendations: tenant product lookup, recent outcome
  reads, next-action inserts, live recommendation reads, and recommendation feedback now use
  `dbpool.tenant_connection()`. `feedback()` now requires `tenant_id` and refuses cross-tenant updates; the
  global `recommend_strategy(kind)` history read remains a platform aggregate path by design.
- RLS tenant-path conversion continued on CEO observability surfaces: cockpit main payload, product pause/resume
  control, tenant health, comms graph, and tenant trace listing now use `dbpool.tenant_connection()`.
  Operator-wide trace runs/errors/prune stay platform paths. `dbpool` now reads `DB` from `aoscfg` directly
  instead of importing the debugger module, eliminating a `dbpool` <-> `trace.py` import cycle.
- RLS tenant-path conversion continued on the CEO requirements spine: visionkeeper's `get`, `set_vision`,
  refine-success persistence, and refine parse-miss persistence now use `dbpool.tenant_connection()` for
  tenant-owned `ceo_vision` rows. The living requirements doc write remains a filesystem side effect after
  tenant state is persisted.
- RLS tenant-path conversion continued on design and quality review surfaces: design gallery/decision paths
  and quality verdict/summary paths now use `dbpool.tenant_connection()` for tenant-known
  `design_artifacts`/`tenant_products`/`traces` reads and writes. Design decisions audit with explicit
  `tenant_id`, and both modules now read `DATABASE_URL` through `aoscfg.DB` instead of ad hoc env parsing.
- RLS tenant-path conversion continued on user-interruption surfaces: notification preference/feed/write/read
  paths now use `dbpool.tenant_connection()` for tenant-known `notification_prefs`/`notifications`
  operations. The notification taxonomy now also makes urgent build pings match the documented contract:
  missing prefs default `build` push on for self-host milestone pings, urgent notifications queue tenant push
  asynchronously, explicit per-category `push=false` is respected, and urgent still operator-pages. The
  focused `notifications.py selftest` proves feed/badge/read behavior plus push/page fanout without network.
  Ask-user creation plus pending-list reads use `dbpool.tenant_connection()` for tenant-known
  `ask_user_requests` operations. Id-only answer/resume reads remain direct until those APIs carry or securely
  resolve tenant context.
- RLS tenant-path conversion continued on the tenant trace explorer: overview, run list, and replay reads now
  use `dbpool.tenant_connection()` for tenant-known `traces`/`tenant_products` observability paths while
  preserving ownership checks and secret redaction.
- RLS tenant-path conversion continued on org and cross-org CEO surfaces: org create/list/get/vision/stage/
  archive/context reads now use `dbpool.tenant_connection()` for tenant-owned `orgs`/`org_artifacts`/
  `tenant_products` paths, and cross-org portfolio/analytics/failure rollups use the same tenant GUC for
  `tenant_products`/`traces`/`audit_log`/`findings` reads. Cross-org terminal-build selftest audit rows now
  carry explicit `tenant_id`.
- RLS tenant-path conversion continued on live-status and frontdoor user paths: per-tenant live-status reads,
  frontdoor tenant-product ownership writes, build-list reads, status reads, and download ownership checks now
  use `dbpool.tenant_connection()` when a tenant token has resolved. Signup/bootstrap and operator-wide
  registry enumeration remain separate control-plane paths.
- RLS tenant-path conversion continued on the budget forecast path: tenant forecast reads and threshold state
  writes now use `dbpool.tenant_connection()` for `tenant_products`/`traces`/`budget_alert_state`, forecast
  alert audit rows carry explicit `tenant_id`, and schema/setup/sweep enumeration/selftest now use the shared
  pool instead of direct `psycopg.connect(DB)`. The predictive warning loop is now in
  `scheduler.DEFAULT_SCHEDULES` as `forecast-sweep` every 3600s, and local bootstrap removed the retired
  duplicate `budget-forecast-sweep` row so budget alert checks do not double-run.
- RLS/direct-connection cleanup continued on focused user-facing utility paths: `consent.py`, `push.py`, and
  `notifications.py` now use the shared pool for schema/setup and selftest cleanup while keeping tenant
  reads/writes on `dbpool.tenant_connection()`. `estimate.py` now uses the shared pool for its global
  historical pre-commit estimate reads instead of direct admin connections.
- RLS/direct-connection cleanup continued on tenant/provider identity, observability, requirements, billing,
  and spend-guard support paths: `tenancy.py`, `tenantproviders.py`, `traceview.py`, `visionkeeper.py`,
  `billingview.py`, `budget.py`, `fleet.py`, `incident.py`, and `appguard.py` now use the shared pool for
  control-plane setup, operator snapshots, and selftest cleanup while preserving tenant-scoped reads/writes
  where tenant context is known.
- RLS/direct-connection cleanup continued on tenant CEO surfaces and recommendation logic:
  `livestatus.py`, `onboarding.py`, `qualityview.py`, `integrationsview.py`, `projectsview.py`, and
  `recommend.py` now use the shared pool for setup/history/selftest fixture paths while keeping the
  tenant-owned reads and writes behind `dbpool.tenant_connection()`.
- RLS/direct-connection cleanup continued on central orchestration and multi-company surfaces:
  `console.py`, `orchestrator.py`, `research.py`, `orgview.py`, and `crossorgview.py` now use the shared
  pool for schema setup and selftest fixture paths while preserving tenant-scoped chat, research,
  org-chart, and cross-org reads/writes.
- RLS tenant-path conversion continued on identity and orchestrator chat paths: auth account creation after
  signup, tenant-token lookup after verification/reset/login, and auth audit rows now use explicit tenant
  context once tenant identity is known; email/code bootstrap remains a no-tenant identity path. Orchestrator
  thread creation/history, user/assistant chat writes, queued product visibility, and build-start audit rows
  now use `dbpool.tenant_connection()` for tenant-known `chat_threads`/`chat_messages`/`tenant_products`
  operations.
- Orchestrator selftest hardening: the no-spend chat selftest now stubs host CLI subscription auth and records
  consent again after connecting a different named provider, so it proves provider-switch consent behavior
  without relying on this machine's real Claude/Codex login state.
- RLS tenant-path conversion continued on live observability: tenant-tagged `pulse.start()`/`pulse.beat()`
  writes now use `dbpool.tenant_connection()` for `agent_pulse`, and `pulse.py` has one explicit platform
  connection boundary for operator-wide live/stalled/sweep reads. The org chart and live roster views now use
  `dbpool.tenant_connection()` for tenant-owned `tenant_products`/`directory` reads while durable orchestra
  tree reads remain delegated to `store`'s tenant-aware APIs.
- RLS tenant-path conversion continued on product phase contracts and explainability: `productregistry`
  registration and later product-only phase calls now resolve tenant ownership from `product_registry` and
  use `dbpool.tenant_connection()` when possible for `product_registry` reads/writes, preserving the old API
  while preparing phase-boundary checks for tenant policies. The console explainability endpoint now calls a
  tenant-scoped wrapper that joins `traces` through `tenant_products`, and a focused cross-tenant proof shows
  another tenant gets no trace steps for the owner's product.
- RLS tenant-path conversion continued on product financial guardrails: product-keyed `appguard` policy,
  economics, pause/resume, and audit rows now derive tenant ownership from `tenant_products` and use
  `dbpool.tenant_connection()` when possible while preserving the fleet-wide operator `guard()` sweep. The
  runtime `budget` governor uses the same product-to-tenant resolver for budget writes/status reads and
  tenant-tags budget audit rows. A focused tenant-owned smoke proof covered appguard economics plus budget
  allow/deny behavior under a real `tenant_products` row.
- RLS tenant-path conversion continued on the quality loop: `quality_runs`, `quality_measurements`, and
  `build_outcomes` get/keep tenant ownership columns during `_ensure`; product/run-scoped run creation,
  measurements, finish/outcome writes, resume reads, and quality audit rows now derive tenant from
  `tenant_products`/`quality_runs` and use `dbpool.tenant_connection()` when possible. Operator learning-store
  outcome listing remains a platform path.
- RLS tenant-path conversion continued on governed findings: product/org-scoped QA and audit findings now
  infer tenant ownership from `tenant_products`/`orgs`, persist `tenant_id` on `findings` and
  `finding_verifications`, mirror tenant-owned findings to the tenant task board, and tenant-tag file/
  verify/resolve/escalation audit rows. Operator backlog and platform findings remain platform-visible, and
  the strengthened `findings.py selftest` proves tenant-owned QA findings stay tenant-scoped.
- RLS tenant-path conversion continued on collaborator routing: `orchestrate.request_collaborator()` now
  accepts/infers tenant ownership from `role@product` requesters and product-scoped finding requesters, then
  carries that tenant into queue writes and `hire_requests`. `fulfill()` resolves tenant ownership from the
  hire row before closing and routing the queued task. The strengthened `orchestrate.py selftest` proves a
  tenant-owned requester files a tenant-scoped hire while the existing reuse/spawn/priority/uncovered-role
  semantics still pass.
- RLS tenant-path conversion continued on cross-org operations: propose/scope/request-approval/execute/list
  now use `dbpool.tenant_connection()` once the operation tenant is known, cross-org audit rows are tenant
  tagged, and `org_lineage` gets an explicit `tenant_id` during execution. The strengthened
  `crossorg.py selftest` proves tenant ownership on both the operation and lineage rows while preserving the
  existing owner guard, human confirmation gate, action catalog, and portfolio brief.
- Scheduler monitoring hardening: the platform scheduler now uses the shared DB pool for `schedules` state
  and records every due job in `scheduler_runs` with decision, rc, duration, and detail. `scheduler_runs` is
  explicitly present in init SQL and the RLS platform-exemption set. The strengthened `scheduler.py selftest`
  proves successful, timeout, and rejected jobs all advance and write telemetry, while disabled jobs stay
  skipped. The mission-control dashboard now reads `scheduler_runs` plus overdue enabled `schedules` and emits
  alerts for unrecovered repeated nonzero/timeout/error/rejected jobs or schedule rows left badly overdue; because
  `watchdog.check()` reuses dashboard alerts, broken recovery/sweep loops now page instead of remaining
  forensic-only telemetry, while a later successful run clears stale scheduler failures. `proactivecomms.py
  sweep-all` also now uses a DB-cursored tenant batch (`AOS_PROACTIVE_SWEEP_LIMIT`, default 150) so a large tenant
  table cannot make the five-minute proactive briefing job run into the scheduler's 120-second timeout. A live
  no-model sweep over the local DB checked 150 tenants in 11.18s after this change, and
  `tests/test_core.py::test_dashboard_surfaces_scheduler_failures_and_overdue_jobs` covers active-vs-recovered
  scheduler alerts.
- Dispatcher reliability/RLS prep: the activation loop now uses the shared DB pool for platform task queue
  mutations and tenant-tags `WakeAgent`, `TaskRetry`, `TaskDeadLettered`, and `SkipPausedApp` audit rows when
  assignee product ownership is known from `tenant_products`. The strengthened `dispatcher.py selftest` proves
  paused-app skips are requeued/no-spend and tenant-audited.
- Loop controller reliability/RLS prep: the CEO phase controller now uses the shared DB pool for controller
  state/job reads and writes, live status updates, resume/reap/watchdog sweeps, chat persistence, and selftest
  fixtures. The session-scoped advisory-lock path now holds a pooled connection context open for the caller's
  whole critical section, preserving lock lifetime without a raw `psycopg.connect(DB)` call. The
  `loopcontroller.py liveness` command and the full no-spend controller selftest still pass after the conversion.
- Final runtime direct-connection cleanup before policy staging: `settingsview.py`, `qualityloop.py`,
  `crossorg.py`, `frontdoor.py`, `orchestrate.py`, `portfolio.py`, `appregistry.py`, `findings.py`,
  `pulse.py`, `replybridge.py`, `taskboard.py`, `approvals.py`, and `loopcontroller.py` now use the shared
  DB pool for platform/setup/operator paths and `dbpool.tenant_connection()` where tenant context is known.
  Focused compile/selftest checks passed for the touched modules, including approvals decision isolation and
  loopcontroller heartbeat liveness.
- Durable org RLS prep: `scripts/orchestra/store.py` now uses the shared DB pool for schema/operator paths and
  `dbpool.tenant_connection()` for tenant-known durable org run, actor, heartbeat, event-claim, event-complete,
  and atomic `persist_step()` operations. Operator liveness sweeps remain pooled platform paths. The focused
  store selftest and the full durable runtime selftest still prove tenant scoping, SKIP-LOCKED event claiming,
  actor step single-flight, crash resume, kill switch, and governance behavior after the conversion.
- Silent-failure observer reliability prep: `scripts/sentinel.py` now uses the shared DB pool for sentinel
  state, provider/session/build-stuck queries, selftest fixtures, spend reporting, and autocommit DB hang health
  checks while preserving operator-wide monitoring semantics. `sentinel.py selftest` and `watchdog.py selftest`
  still prove stale workflow, hung-agent/provider-burst, stuck durable-org actor heartbeat, stale actor step
  lease, progress ping, and watchdog integration.
- Auth reliability/RLS prep: `scripts/auth.py` now uses the shared DB pool for bootstrap identity/code paths
  where tenant identity is not yet known, uses `dbpool.tenant_connection()` for tenant-known account/token reads
  and writes, and tenant-tags signup/verify/login/reset audit rows. Auth schema setup is guarded by a process
  lock and cached after the first successful ensure so repeated login/reset calls do not rerun DDL and contend
  on `accounts`/`email_codes`. `auth.py selftest` and the code-attempt lock regression still pass.
- Company memory RLS prep: `scripts/companymemory.py` now uses the shared DB pool for schema setup,
  operator/back-compat checkpoint reads, shared fleet lesson paths, and selftest cleanup while retaining
  `dbpool.tenant_connection()` for tenant-known company memory and memory checkpoint operations. Its schema
  setup is guarded and cached, and `companymemory.py selftest` still proves scoped recall, private/role-scoped
  fragments, provenance, latest-plan reads, phase-summary compaction, and fleet-learning lesson writes.
- Agent alert routing reliability prep: `scripts/alerts.py` now uses the shared DB pool for platform alert
  state, dedup/owner updates, open backlog reads, and selftest cleanup. Schema setup is guarded and cached.
  `alerts.py selftest` now proves monitor alert routing to an owner, open-signature dedupe, resolve clearing,
  and an SLA sweep that pages stale critical/high alerts once per cooldown. `scheduler.DEFAULT_SCHEDULES` now
  includes `alerts-sla` every 300 seconds, and the local scheduler row is enabled, so owned alerts do not rely
  on a human remembering to inspect `alerts.py open`.
- Billing reliability/RLS prep: `scripts/billing.py` now uses the shared DB pool for schema setup and platform
  MRR aggregation, caches guarded billing schema setup, and keeps tenant-known selftest fixture writes inside
  `dbpool.tenant_connection()`. `billing.py test` still proves period-scoped usage, metered invoice overage,
  quota denial, automatic suspension lift at rollover, and sticky admin suspension.
- Stripe billing bridge: `scripts/stripebilling.py` adds the real payment-processor boundary for C3 without
  spending in tests. Paid plan changes now create a Stripe Checkout Session when `STRIPE_SECRET_KEY` plus
  `AOS_STRIPE_PRICE_<PLAN>` are configured, and they do **not** mutate `tenants.plan` until a signed Stripe
  webhook confirms checkout/subscription state. Stripe events are stored idempotently in `stripe_events` as a
  platform webhook ledger, while tenant-visible subscription/dunning state lives in tenant-owned
  `billing_subscriptions`. Payment failures increment dunning state, notify the tenant, and suspend after
  repeated failures; later `invoice.paid` lifts only Stripe-dunning suspensions, not admin holds.
  `billingview.change_plan` now blocks paid upgrades when Stripe is unconfigured instead of flipping a column
  for free, and the console exposes authenticated checkout start plus a signature-verified `/api/stripe/webhook`
  endpoint. `stripebilling.py preflight <plan> --tenant <tenant>` is the no-spend live-proof gate for real
  card-capture testing. `stripebilling.py selftest`, `billingview.py selftest`, and focused core regressions
  cover checkout gating, live-proof preflight, signed webhook activation, duplicate event idempotency, dunning
  suspension/recovery, and admin-hold preservation with no network spend.
- CEO false-idle guard: `scripts/workstreamview.py` now exposes active `controller_state` workstreams to
  cockpit, projects, live-status, observability, and chief-of-staff brief paths, including the
  pre-`tenant_products` interval after a CEO has directed work but before a materialized product exists.
  Cockpit now reports unmaterialized fleet-gated controller work in `summary.building`,
  `summary.in_flight_workstreams`, `workstreams`, and queue `active` count; Projects shows a provisional
  workstream row instead of an empty portfolio; live-status marks the workstream as running without inventing
  a URL. `traceview.overview()` now injects active workstreams into `recent_activity` and exposes
  `in_flight_workstreams`, so the Activity/Observability screens have a real controller event to render before
  the first product row exists. Those activity rows also carry `can_cancel`/`cancel_action`, and the console
  renders Stop beside them so monitoring is actionable, not passive. Cached chief-of-staff briefs are no
  longer allowed to keep saying idle while a
  live workstream exists; the cached narrative is overlaid with current workstream status. The console
  cockpit/projects/activity/observability empty states render that active work. The shared workstream payload
  now marks running workstreams with `can_cancel=true`, `workstreamview.cancel_workstream()` provides a
  tenant/org/thread-scoped Stop path, and the console renders Stop buttons on Cockpit active workstreams,
  provisional Projects rows, Activity, and Observability, so the CEO can halt pre-product work from the
  monitoring surfaces where J4 sends them instead of needing to return to the Assistant.
  `tests/test_core.py::test_controller_workstream_prevents_false_idle_cockpit_and_projects` recreates dogfood
  `#515`'s zero-product/active-controller state and proves these user-facing payloads cannot claim idle or hide
  the Stop affordance.
- Evidence-backed external finding verification: `findings.record_verification()` now records deterministic
  browser/CI/staging evidence as an immutable `finding_verifications` row without marking the finding resolved;
  callers still must pass the returned id through `findings.resolve(..., verification_id=...)`, preserving the
  evidence gate. `tests/test_core.py::test_findings_external_verification_still_uses_gated_resolve` proves a
  failing external verification cannot close a finding and a passing one can. This was used to resolve product
  finding `#577` (`o-feedback-board-trust-com` public Trust route): `node --test tests/**/*.test.js` passed
  `580/580`, the focused guest Trust regression passed, and a live browser-bridge probe of
  `http://127.0.0.1:8885/#/trust` observed the public Trust page with no `/api/login`, no `/api/session`,
  guest controls only, and no board content. Verification record: `370`.
- Proactive comms RLS prep: `scripts/proactivecomms.py` now uses the shared DB pool for schema setup and
  active-tenant enumeration, and `dbpool.tenant_connection()` for tenant-owned dedupe state plus controller
  decision-gate reads. Its focused selftest still proves urgent/standard altitude mapping, standing-item
  dedupe, and overdue re-reminders; the core proactive regressions still prove controller gates surface to
  the CEO instead of silently parking a build.
- Proactive operating updates: `scripts/proactivecomms.py` now also reads the unified `pulse.live()` plane and
  emits one bounded progress briefing per tenant when long-running work has been active long enough that a CEO
  would expect a standup/status ping. It aggregates pulse-tracked QA/build work plus durable fleet actors,
  excludes other tenants, throttles progress reminders separately from blockers, keeps healthy updates passive,
  and raises stalled/blocked progress to higher severity. `proactivecomms.py selftest` and
  `tests/test_core.py::test_proactive_comms_sends_bounded_progress_update_from_pulse` cover the no-spend path.
- Debug trace reliability/RLS prep: `scripts/trace.py` now uses the shared DB pool for operator-wide run
  listings, product trace replay, retention pruning, error search, and bootstrap selftest setup/cleanup, while
  retaining `dbpool.tenant_connection()` for tenant-owned trace visibility. `trace.py selftest` still proves a
  tenant sees its own product trace and not an unrelated product trace.
- Watchdog monitoring reliability prep: `scripts/watchdog.py` now uses the shared DB pool for heartbeat writes,
  stale-heartbeat reads, build-stall audit reads, alert dedupe/recovery bookkeeping, and selftest cleanup. The
  deliberate raw `psycopg.connect(DB, connect_timeout=3)` remains only for the out-of-band Postgres outage
  probe so a broken pool/control plane cannot mute the pager. `watchdog.py selftest` and the DB-down regression
  still pass.
- Agent-request RLS prep: `scripts/agent_request.py` now uses the shared DB pool for schema setup and id-only
  admin/back-compat reads, keeps tenant-owned ask/open/answer paths under `dbpool.tenant_connection()`, and
  allows `get()` / `is_answered()` to run tenant-scoped when the caller already has tenant context. Its
  selftest now exercises tenant-scoped answer/read polling, and `scripts/test_tenant_isolation.py` still proves
  another tenant cannot answer the CEO's AI question. `scripts/dogfood.py verify-j5` is now a no-model focused
  proof for the J5 review/respond contract: it creates a real `agent_request`, verifies the tenant Approvals
  inbox exposes a readable `question` item, answers through `/api/requests/answer` when the console is live,
  confirms the durable row is `answered` with the exact CEO reply, and confirms the question disappears from
  Approvals. Live HTTP run `ebab20` passed against `http://127.0.0.1:8099` with all five checks true.
- Ask-user RLS prep: `scripts/askuser.py` now uses the shared DB pool for schema setup and id-only
  admin/back-compat reads, keeps tenant-owned ask/pending paths under `dbpool.tenant_connection()`, and allows
  `answer()` / `is_answered()` / `get_answer()` to run tenant-scoped. Tenant mismatches now raise instead of
  silently reporting a reply that leaves the loop paused, and the selftest exercises the scoped answer/poll
  path.
- Directory/fabric RLS prep: `scripts/directory.py` now uses the shared DB pool for schema setup, product-to-
  tenant and agent-to-tenant lookup, and platform/back-compat directory operations, with optional
  `dbpool.tenant_connection()` scoping for known-tenant roster/find/conflict/contact paths. Directory,
  conversations, and inbox rows now carry explicit `tenant_id` columns/indexes from local setup, and the
  strengthened selftest proves tenant-scoped roster reads plus tenant-tagged brokered contact delivery while
  preserving conflict detection and no-socket mailbox semantics.
- Factory RLS/reliability prep: `scripts/factory.py` now uses the shared DB pool for trace persistence,
  product-tenant lookup, stage checkpoint reads, durable controller/agent comm logging, and interrupted-build
  resume sweeps. Product-owned trace writes use `dbpool.tenant_connection()` when ownership is known, stage
  directory presence plus controller handoff/handback comm rows carry tenant context, and the offline factory
  selftest disables transient retry sleep in its fake 529 failover scenario so it remains bounded/no-spend.
  Targeted factory tests still prove crash-resume checkpoints and interrupted-build resume detection.
- QA evidence RLS prep: `scripts/qa/qa_run.py` now persists `qa_runs` through the shared pool and uses
  `dbpool.tenant_connection()` when product ownership or tenant context is known, adding/indexing
  `qa_runs.tenant_id` as part of local setup. The agentic QA entrypoint now passes tenant context into shared
  QA persistence and tenant-tags blocking findings it files, while both procedural and agentic offline
  selftests keep proving durable `qa_runs` rows plus `QA-VERDICT.json` launch artifacts.
- Platform catalog/monitoring RLS prep: `scripts/skills.py`, `scripts/accountability.py`, and
  `scripts/dashboard.py` now use the shared DB pool instead of direct connections. `skills.py` remains an
  exempt platform catalog; `accountability.py` and `dashboard.py` keep explicit operator-wide visibility over
  fleet conversations, waits, audit activity, QA verdicts, and org trees so dropped handoffs and stuck work are
  still visible/paged outside any single tenant scope. `accountability.sweep()` now routes dropped handoffs
  and overdue waits into `alerts.raise_alert(..., target_role='incident-commander')` with tenant/product
  attribution resolved from `conversations`/`waits` and the live `directory`, so coordination failures become
  scoped owned work items instead of vague platform pages. `accountability.py selftest` proves scoped dropped
  handoff and overdue-wait escalation. The handoff matcher now consumes a generic resolver once, so one later
  `done` from a recipient cannot falsely close multiple prior requests in the same conversation; explicit
  `in_reply_to` replies still close their target. `tests/test_core.py::test_accountability_consumes_generic_resolver_once`
  covers this false-green coordination case. `scheduler.DEFAULT_SCHEDULES` includes `accountability-sweep`,
  and the local scheduler row is enabled.
- Tenant custom-agent/design RLS prep: `scripts/customagents.py` now uses pooled admin paths only for schema
  setup, legacy id-only load, and selftest cleanup while keeping tenant-owned define/list/toggle/delete/run
  paths under `dbpool.tenant_connection()`. `scripts/design_fleet.py` now writes design artifacts under
  `dbpool.tenant_connection()`, carries tenant context into design agent traces, and tenant-tags prototype
  audit rows. Their selftests still prove tenant-owned custom-agent reporting and design gallery/approval.
- Durable actor bus and org communication prep: `scripts/orchestra/bus.py` now uses the shared DB pool for
  durable message table setup, event persistence/readback, and selftest cleanup while preserving journal
  fallback and immediate in-process delivery. `scripts/orchestra/runtime.py selftest` now also proves peer
  clarification and professional disagreement as durable Postgres conversations: a coworker question is
  answered via `context_update`, and a worker objection routes up to the CEO/human tier before the ruling is
  broadcast back down with the original conversation id preserved.
- Secrets/wait-driver RLS/monitoring prep: `scripts/vault.py` now uses the shared pool for schema setup and
  GDPR-style multi-scope cleanup, and `dbpool.tenant_connection()` for tenant-owned secret put/get paths.
  `scripts/deadlock.py`, `scripts/commfabric.py`, `scripts/approval_gate.py`, and `scripts/jobd.py` now use
  the shared pool. Approval gate wait rows now carry `reply_by`, so overdue-wait monitoring can detect stuck
  human approval gates instead of parking silently until timeout.
- CEO/org surface RLS and liveness hardening: `scripts/cockpit.py`, `scripts/projbudget.py`,
  `scripts/designview.py`, `scripts/explain.py`, `scripts/productregistry.py`, `scripts/osq.py`,
  `scripts/reap.py`, `scripts/versions.py`, `scripts/account.py`, `scripts/retention.py`, and
  `scripts/metrics.py` now use the shared DB pool for their remaining schema, operator, CLI, and selftest
  DB paths. Tenant-facing paths still use `dbpool.tenant_connection()` where tenant context exists.
  `directory._ensure()` now avoids lock-heavy no-op `ALTER` calls when tenant spine columns already exist and
  bounds schema lock waits, after `cockpit.py selftest` exposed a read-path hang there.
- Org context spine hardening: `orgs.record_artifact()` now records `org_artifacts.tenant_id` by resolving the
  org owner while preserving the old org-id-first call shape; known-tenant callers in cross-org execution and
  final delivery pass tenant context explicitly. `orgs.py selftest` now proves tenant-tagged artifacts appear
  in the org context brief.
- Chief-of-staff RLS prep: `scripts/chiefofstaff.py` now reads portfolio/spend facts and gets/puts
  `brief_cache` through `dbpool.tenant_connection()` when composing tenant CEO briefs. Active-tenant
  enumeration remains a platform sweep, and `chiefofstaff.py selftest` still proves grounded fallback briefs,
  model-composed briefs, structured cache persistence, scheduled daily pushes, and pending-decision
  visibility. The cache schema ensure now runs on the setup/platform pooled path, not inside the tenant app
  role transaction, after the enforced RLS smoke exposed that request-path DDL would fail under `agentos_app`.
- Connection-pool reliability hardening: `dbpool.connection()` now fail-opens only on pool acquisition
  failures. Exceptions raised by the caller's DB work propagate normally through the pooled transaction
  instead of being mistaken for a pool hiccup; `scripts/dbpool.py selftest` and `scripts/research.py selftest`
  cover this path.
- RLS app-path scanner: `scripts/rls_app_paths.py summary` provides a static review queue for FORCE RLS.
  Current baseline after the latest conversions: `3` files with direct DB connection refs near tenant-owned
  table names, `29` tenant tables with at least one direct-connect ref, out of `60` tenant-owned tables;
  aggregate refs are `10` direct, `184` tenant-connection, and `206` pooled. The remaining direct refs are
  confined to RLS tooling/harness files, not runtime app modules.
- RLS audit special-case proof: `scripts/rls_readiness.py audit-policy-sql` now generates the reviewed
  split-role policy shape for `audit_log`, and `scripts/rls_readiness.py audit-harness` proves it on
  throwaway DB objects: tenant app reads are tenant-scoped and insert-denied; the audit writer can see the
  global tail and insert append-only rows, but cannot update rows.
- RLS shared-memory special-case proof: `scripts/rls_readiness.py role-lessons-policy-sql` now generates the
  reviewed policy shape for `role_lessons`, and `scripts/rls_readiness.py role-lessons-harness` proves it on
  throwaway DB objects: tenant app sessions read shared fleet lessons plus their own tenant lessons, cannot
  read another tenant's lessons, can insert only current-tenant lessons, and can update only the `uses`
  counter for visible rows.
- RLS policy migration staging proof: `postgres/initdb/56-rls-policies.sql` is the reviewed migration shape for
  app-role/GUC tenant policies, the `audit_log` split writer, and shared `role_lessons` policies.
  `scripts/rls_readiness.py migration-dry-run` applies that migration inside one transaction, confirms
  readiness goes green (`missing_rls=[]`, `missing_force_rls=[]`, `missing_policy=[]`,
  `special_policy_needed={}`), then rolls it back so the current DB remains unchanged.
- RLS app-role runtime proof: `scripts/rls_readiness.py app-role-harness` creates a throwaway RLS table and
  NOLOGIN app role, warms the shared DB pool with autocommit borrows, then proves
  `dbpool.tenant_connection(..., app_role=...)` runs under the non-owner role with transaction-local
  `app.tenant_id`, scopes tenant A/B reads, blocks cross-tenant writes, and resets role/GUC state for the next
  pooled borrower. This also caught and fixed a real pool safety bug: pooled connections now reset
  `autocommit` both to `True` and back to `False` on every borrow.
- RLS enforced real-table smoke: `scripts/rls_readiness.py enforced-smoke` applies
  `postgres/initdb/56-rls-policies.sql` inside one rollback-only transaction, seeds representative real
  public tables (`tenants`, `tenant_products`, `ai_consent`, `audit_log`, `role_lessons`), and proves
  `agentos_app` sees only its tenant, same-tenant writes pass, cross-tenant writes fail, tenant app audit
  inserts are denied, `agentos_audit_writer` can append but not update, and `role_lessons` keeps shared+own
  reads with uses-only updates. The command rolls back the policy flip and fixture rows.
- RLS enforced module smoke: `scripts/rls_readiness.py enforced-module-smoke` applies the same migration inside
  one rollback-only transaction, patches the in-process DB pool so real Python module APIs use that transaction,
  sets `AOS_DB_APP_ROLE=agentos_app` and `AOS_DB_AUDIT_ROLE=agentos_audit_writer`, and proves actual
  `tenancy`, `consent`, and `notifications` tenant-facing APIs isolate products/ownership, consent state,
  audited consent revoke/re-record writes, notification feed reads, unread counts, and mark-read updates under
  enforced RLS. It also now proves the CEO-facing `chiefofstaff` facts/cache path: tenant A sees only its
  traced product spend and can write/read its `brief_cache` row under the app role, cached briefs still get
  the live active-workstream overlay, and tenant-scoped controller live research progress remains visible only
  to the owning tenant. `audit.append()` now uses the shared DB pool and can opt into the audit-writer role,
  so audited tenant module paths are covered by the same app-role/GUC primitive. This is still not a substitute
  for a staging DB, but it proves more real entrypoint code can run through the intended policy shape.
- RLS rollout gate: `scripts/rls_readiness.py rollout-gate` now runs the migration dry-run, enforced real-table
  smoke, enforced module smoke, dbpool app-role harness, and static app-path direct-connection review as one
  no-spend preflight. It passed locally and reports no runtime direct-connect files outside RLS tooling/harness
  files, so the policy shape is green for a staging apply attempt while still refusing to mutate the live DB.
- Live dogfood preflight: `scripts/dogfood.py preflight [persona]` now performs a no-spend readiness gate before
  browser/model spend. It checks persona validity, required journey coverage, acceptance standards, explorer
  availability, console health, QA/browser contention, budget bounds, last live run evidence, and open dogfood
  findings, then prints the exact run command, required artifacts, monitoring surfaces, and stop conditions.
  Known critical/high dogfood findings block a new spendful run by default; `AOS_DOGFOOD_ALLOW_OPEN_FINDINGS=1`
  is an explicit override for a deliberate verification run after reviewing the known blockers. `scripts/dogfood.py
  selftest` proves the preflight stays offline. A deliberate override verification attempt on `2026-08-13`
  produced evidence directory
  `/mnt/c/Users/navin/Documents/agent-os-qa-evidence/dogfood-first-run/20260813-070609`, but it was stopped
  before J4 because J1 advanced to 9 steps while only one coverage aspect was credited; this was recorded as
  failed verification `379` on `#515`, not a resolution. A first focused `scripts/dogfood.py verify-finding 515`
  pass then failed as verification `382`, proving the long-running console process was still serving stale code
  and could still render the false-idle state. After restarting `scripts/console.py serve 8099`, the same
  focused verifier passed against the real HTTP console/API as verification `383` and resolved `#515`: with
  zero product rows, Cockpit reported `building=1` / `in_flight_workstreams=1`, Projects showed a provisional
  building workstream, livestatus reported it running, Observability showed controller recent activity, and the
  cached chief-of-staff brief was overlaid with `Your team is working on 1 active workstream(s).` The verifier
  now also requires `can_cancel` on Cockpit and Projects, matching the J4 expectation that running work has a
  visible stop/cancel affordance. Live HTTP verification `399` passed against the restarted console with
  `cockpit_can_cancel=true` and `projects_can_cancel=true` for a zero-product running controller workstream.
  The fresh full critical dogfood attempt
  `/mnt/c/Users/navin/Documents/agent-os-qa-evidence/dogfood-first-run/20260813-094152` then passed J1 signup,
  J2 company creation, and J3 direct-build kickoff before being intentionally interrupted at J4 after repeated
  live probes showed Activity exposing controller `RESEARCH` status but not the required stop/cancel affordance.
  `traceview.overview()` now carries `can_cancel`/`cancel_action` on workstream activity rows and
  Activity/Observability render Stop beside running workstreams, so the monitoring surface is actionable instead
  of passive. A real
  `scripts/dogfood.py preflight first-run` is now green with `open_dogfood_findings.total=0`, while still
  surfacing the stale `2026-07-31` `4/5` full persona run. Dogfood now also treats explorer `*-incomplete`
  stop reasons as story status `incomplete` instead of `passed`, and defaults live dogfood stall detection to
  `AOS_DOGFOOD_STALL_STEPS=10` unless `AOS_QA_STALL_STEPS` is explicitly set, so future live runs should
  preserve evidence and move on rather than quietly false-greening an unfinished story. A later critical-path
  live attempt exposed a second verifier truthfulness bug: J1 could be marked `passed` with
  `coverage-complete-ai-judged` while the explicit coverage ledger still had an uncovered critical-path item.
  `qa_explorer` now treats `done=true` with remaining coverage as
  `ai-done-with-remaining-incomplete`, and `dogfood.py` independently marks any story with remaining coverage
  as `incomplete`; the dogfood selftest pins both `*-incomplete` stop reasons and this uncovered-ledger
  false-green case. `AOS_DOGFOOD_DEPTH=critical` is now the default live finish-line mode and supplies one
  explicit critical-path coverage item per mandated CEO journey; `AOS_DOGFOOD_DEPTH=deep` keeps the broad
  AI-generated edge-case checklist for nightly/audit exploration. The
  scorecard's `scripts/dogfood.py qa-explore first-run` command is real and guarded: it runs preflight first
  and refuses to launch browser/model work if a check is red. `AOS_DOGFOOD_BASE` can target another console URL
  for staging. After the `20260813-083037` run was interrupted during J4, `dogfood.py` was hardened so
  SIGINT/SIGTERM during a journey writes an explicit partial `summary.json`, emits an `interrupted` event, and
  writes `NN-<journey>/result.json` with the active story marked `interrupted` and later stories marked
  `skipped-interrupted`. Dogfood preflight now also prefers the latest real browser artifact over stale audit
  rows and fills missing per-story statuses as `interrupted` when browser evidence exists or `unproven` when no
  result exists. The dogfood selftest proves these no-model paths, so readiness tooling no longer has to infer
  partial progress from missing result files. `scripts/dogfood.py verify-critical` now bundles the no-model J4
  and J5 focused verifiers into a single pre-spend readiness check. Live HTTP run `verify-critical` passed in
  `2.48s` after the Activity fix, proving J4 status/cancel surfaces including `observability_can_cancel=true`
  and J5 question/answer persistence without launching browser/model exploration.
- One-shot live-build preflight: `scripts/ceo_run.py preflight "<prompt>" --tenant <tenant> --org <org>` now
  performs a no-spend readiness gate before the expensive one-prompt product proof. It refuses to start a
  controller thread unless the prompt is present, Postgres is reachable, the tenant exists, AI consent is on
  file, a model provider is resolved, the tenant's billing/quota state is allowed to start work, the one-build
  budget is configured, the mission-control dashboard has no critical ops alerts, `jobd` and phone-reply daemons
  are running, and global/tenant kill-switches are clear.
  The billing/quota check is a read-only snapshot rather than `billing.quota()` enforcement, so preflight does
  not auto-suspend or otherwise mutate the tenant. It emits the exact guarded command, expected artifact surfaces
  (`controller_state`, `controller_jobs`, `audit_log`, `traces`, notifications, `pulse.live()`), monitoring
  surfaces, and stop conditions. `scripts/ceo_run.py selftest` and
  `tests/test_core.py::test_ceo_run_preflight_blocks_before_live_submit_when_provider_missing` cover the
  no-spend path, including missing-provider, over-quota, and critical-ops-alert blocks. A real no-spend preflight
  against tenant `demo` passed locally with `billing_quota_ready=true`, `build_budget_usd=40.0`, and
  `no_critical_ops_alerts=true`.
- Live first-run dogfood finish line: full browser/model run
  `/mnt/c/Users/navin/Documents/agent-os-qa-evidence/dogfood-first-run/20260813-102810` completed all five
  critical CEO journeys with zero bugs: J1 signup/verify, J2 company creation, J3 direct-build kickoff, J4
  wait/watch status + stop affordance, and J5 review/respond. The run resolved dogfood finding `#615` with
  verification `411`. During this pass the CEO response path also exposed a broader notification/actionability
  gap: proactive controller-gate notifications could link to Approvals while the actual gate was only visible
  in Assistant. Approvals now includes live `ceo_decision` controller gates and can reply to the exact parked
  thread; `tests/test_core.py::test_approvals_surfaces_controller_gate_and_thread_reply_clears_notification`
  proves the gate appears, the response records a `decision_registered` receipt, and the matching notification
  clears.
- Runaway-live-QA containment: the incident on controller thread `2711`, product
  `2033-trust-proof-dog-walking-`, exposed three compounding hazards: a 40-story live QA corpus, repeated
  crash/recovery redispatch, and browser/model work continuing after its parent was cancelled. The current
  defaults now cap a QA run at 12 stories, 80 steps per story, three rounds, a 30-minute TESTQA worker ceiling,
  and zero transparent QA crash retries. `jobd` admits at most two active jobs/tick by default, automatically
  replays only fresh runnable transitions, and parks stale ambiguous transitions for user feedback. Browser
  admission fails closed through both a local semaphore and DB slots, automatically sizes to at most four
  sessions unless an operator explicitly raises it, and reclaims dead pid-tagged/legacy QA holders. Browser,
  ffmpeg, Codex, and Claude children run in process groups so cancel/reap can terminate the full descendant
  tree. Thread `2711` is deliberately parked at `TESTQA / user_feedback`; it has no active controller job and
  was not resumed during verification.
- Recursive-build containment: bounded live proof thread `2787` safely exposed scope amplification when a
  one-page request recursively decomposed an agent pipeline into another server-side project. The controller
  cancellation path reaped the complete worker/model tree with no leaked slots, locks, or transactions. Safe
  defaults now permit only root + one subsystem level, admit at most 16 planned components across the entire
  recursive tree, and cap an IMPLEMENT worker at 60 minutes. Larger projects require an explicit measured-node
  override instead of silently multiplying work on a WSL laptop.
- Event-claim deadlock/root cleanup: `orchestra.store.claim_events()` now follows the same actor-then-event
  lock order as atomic `persist_step()`, uses `FOR UPDATE SKIP LOCKED`, and retries only PostgreSQL deadlock or
  serialization aborts with bounded jitter. Stale event claims and terminal actor-step claims have explicit
  recovery paths. The focused durable-store concurrency proof passes a three-claimer/12-event race with no
  duplicate delivery, and the core suite covers abandoned-claim release plus atomic step persistence.
- Regression-run containment: the console action crawler now intercepts all mutating API requests and the two
  model-spending GET endpoints with fixtures, ignores hidden controls, has a hard 180-second process timeout,
  and cleans only its clearly prefixed fixture tenants. Factory, onboarding, QA, and action-coverage selftests
  are offline/stubbed by default. Selftest audit records remain in the tamper-evident chain but carry
  `_selftest=true`, so intentional deny-path probes no longer page as production policy pressure.
  Factory traces now also persist an explicit `test_run` marker; live provider-degradation, session-cap,
  token-burn, stuck-build, reporting, and interrupted-build recovery signals exclude those synthetic rows.
- Host pressure and recovery proof: watchdog observes available memory, generated dev-server count, and root
  Chromium/ffmpeg process count before WSL exhausts the host. The current bounded verification left zero
  running/pending controller jobs, zero Postgres lock waiters/open transactions, and all browser/model slots
  free. An encrypted snapshot/restore drill passed at
  `/home/swami/projects/agent-os/backups/agent-os-20260814-074116.aosnap`.

## Still Not Proven Prod-Ready

- **One-shot shipped product proof:** the audit ledger contains substantial historical pipeline activity, but
  older rows predate explicit selftest tagging and are therefore not treated as clean live-product evidence.
  There is still no fresh owner-approved, end-to-end live run after the current safety/default changes. Need
  one bounded `ceo_run.py` one-prompt build with trace/audit/QA/pulse artifacts, starting from a green
  `ceo_run.py preflight`.
- **Stripe C3 live capture:** the payment-processor bridge is now built and offline-proven, but real revenue
  collection is not live-proven until `scripts/stripebilling.py preflight pro --tenant <tenant>` is green with
  owner-provided `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `AOS_STRIPE_PRICE_PRO`, and
  `AOS_STRIPE_PRICE_ENTERPRISE`, followed by a real Checkout subscription and signed webhook proof. Current
  billing still has no live card-capture evidence in this environment.
- **Postgres RLS:** app-level tenant tests pass, but DB-enforced row-level security is not deployed. Correct
  rollout requires a non-owner app DB role, per-request `SET LOCAL app.tenant_id`, RLS policies on every
  direct tenant-scoped table, and migration tests that prove no tenant rows are visible without the GUC. The
  current live-schema `scripts/rls_readiness.py report` remains red because RLS/FORCE/policies are not applied
  to this DB, but the stronger no-spend `scripts/rls_readiness.py rollout-gate` is green: the migration dry-run,
  enforced real-table smoke, enforced module smoke, dbpool app-role harness, and static app-path review all pass.
  Tenant indexes are ready, there are no unknown unscoped tables, and there are no remaining indirect-scope
  tables. `audit_log` is explicitly special: a naive tenant-only policy would break the global hash-chain append
  because appends must see the previous global row. `role_lessons` is also special: `tenant_id NULL` rows are
  shared fleet lessons and need a policy that allows shared reads plus tenant-scoped rows. Do not apply the
  migration permanently until tenant-facing request paths are exercised in staging with policies actually
  enforced and the process/login role inherits `agentos_app`.
- **Rollback cleanup:** the legacy procedural QA path and legacy inline research path remain intentionally for
  rollback. Delete them only after live parity runs prove the new defaults under real spend.
- **Single-host disaster recovery / HA:** encrypted local backup and restore are proven, but the evidence is
  still on this workstation. Production needs automated off-box encrypted backup replication, restore drills
  from that remote copy, and a documented replacement-host/failover objective. A WSL or Windows host loss can
  currently interrupt service even though durable database state and process recovery handle ordinary daemon
  crashes.

## Next No-Spend Work

- Run `scripts/rls_readiness.py rollout-gate` against a staging clone, then grant the actual tenant-facing
  process/login role membership in `agentos_app`, set `AOS_DB_APP_ROLE=agentos_app`, apply
  `56-rls-policies.sql` in that staging DB, and run the broader tenant-facing module/selftest suite through
  real entrypoints with policies permanently enforced before touching the live DB.
- Keep converting any new tenant-facing request paths from direct admin connections to app-role/GUC-safe
  connections; `postgres/initdb/56-rls-policies.sql` is dry-run proven but should remain unapplied to the
  live DB until staging module/selftest runs pass with policies actually enforced.
- Use `scripts/rls_app_paths.py summary` to drive the remaining conversion queue. Current scanner baseline:
  `direct_connect_refs=10`, `files_with_direct_connect_refs=3`, `tables_with_direct_connect_refs=29`,
  `tenant_connection_refs=192`, `pooled_connection_refs=220`, `unknown_unscoped_tables=[]`. The remaining
  direct-reference review targets are RLS tooling/harness files only: `rls_readiness.py`,
  `rls_app_paths.py`, and `test_tenant_isolation.py`.
- Keep the emitted `ceo_run.py preflight` JSON with any live one-shot run evidence, then run the guarded
  command only while the checks are green; fix any QA/review/budget/provider blocker before claiming the
  one-shot product proof.
