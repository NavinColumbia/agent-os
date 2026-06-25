# agent-os Platform — Exhaustive Screen / View Inventory

Every page/view/screen across **web + mobile + admin**. ~240 screens across 16 areas — intentionally overkill.
Roles: `Visitor` · `CEO` (tenant owner directing the fleet) · `Member` (teammate) · `Org-Admin` · `Operator` (us, platform back-office) · AI `Agents` (first-class data consumers).
Tiers: `Free` · `Pro` · `Team` · `Enterprise`. BYO-key on all tiers; platform-key is metered.

**Design-efficiency note:** four primitives compose ~70% of these screens — a **list view** (search/filter/saved-views/bulk), a **detail view** (header + tabs + activity timeline + quick actions), a **config/editor form**, and a **log/audit/trace stream**. Build those component systems well and the surface collapses.

---

## AREA 0 — Cross-cutting shell (every authenticated screen)
App Shell / Global Nav · Command Palette (⌘K) · Global Notifications/Inbox · Global Search · Org Audit Feed · Upgrade/Quota-Gate Modal · Empty States · Error/403/404/Maintenance · In-app Changelog.

## AREA 1 — Marketing / public site (Visitor)
- **Landing & product:** Home/Landing · Interactive "Describe your business" hero · Platform Overview · Feature deep-dives (CEO Cockpit, Agent Fleet, Governed Factory, Sandbox/Safety, Observability, BYO-key, Self-healing, App Store) · Solutions by use-case · Solutions by persona · Enterprise.
- **Commercial:** Pricing · Usage/Cost Calculator · Full Plan-comparison matrix.
- **Proof/docs/content:** Customers/Case-studies · Showcase gallery · Docs portal (home/article/quickstart) · API/SDK/CLI reference · Interactive API playground · Changelog/Roadmap · Blog · Public templates/blueprints gallery · Community/forum · Glossary · "vs"/migration pages.
- **Trust/company/legal:** Security/Trust Center · Public Status Page (+incidents/postmortems) · About/Careers/Press/Brand · Contact/Sales + Book-a-demo · Partners/Affiliate/Referral · Legal suite (ToS, Privacy, DPA, Subprocessors, SLA, AUP, Responsible-AI/training-opt-out, Cookie consent).

## AREA 2 — Auth & onboarding
- **Account entry/security:** Sign-up (email+OAuth) · Log-in · SSO/SAML login · Magic-link request/sent · Email verification · OAuth consent/authorize · Forgot/reset password · MFA enroll/challenge/backup-codes/passkeys · New-device verification.
- **Org/workspace:** Create Organization (name/slug/industry/region) · Workspace type (solo vs team) · Invite team / invite acceptance / domain-join · Org switcher.
- **Guided onboarding wizard (signature):** Welcome · Profile setup · **"Describe your business"** (core) · Goal/intent routing (MVP / internal tool / full SaaS / migrate) · **BYO-key setup** (validate-on-paste, vault-encrypted) · Connect integrations (optional) · Plan selection / trial · Billing/payment setup · First charter → build kickoff · Generation-in-progress (live) · Getting-started checklist · Product tour + sample project · Onboarding success · Verify domain/DNS + SSO·SCIM config (Enterprise).

## AREA 3 — CEO Cockpit / Orchestrator chat (the core surface)
CEO Cockpit Home (portfolio KPIs, "needs your decision", active builds, fleet health) · **Orchestrator Chat** (direct the whole company; streaming; proposed-action cards with inline approve; cost meter) · Directive → Charter Composer (editable charter, budget/spend caps, injection-sanitized) · Decision/Approvals Inbox · Risk Register · Weekly Founder Digest · Portfolio/Business view (per-product economics + platform finance) · Market/Competitor Intel (Pro/Ent).

## AREA 4 — Projects / products workspace
Projects List · Project Overview · **Pipeline View (SPEC→BUILD→QA→REVIEW→LAUNCH)** (stage status/timing, QA green-gate, fix-loop counter, crash-resume indicator, promote/rollback) · Stage Detail · Artifacts Browser · Live Preview Pane · Code/Diff Viewer · QA/Test Report · Launch Kit (marketing-as-code; publish stays human-gated) · Per-app Circuit-breaker/Profit-guard (spend/loss caps, auto-pause) · Project Settings · App-store listing (per generated product).

## AREA 5 — Agent fleet
Fleet Overview/Live status · Org chart / role hierarchy (~90 governed roles) · Agent Comms Graph (hub-and-spoke) · Per-agent Live Activity (streamed work, claimed paths, queue, cost) · **Chat with an agent** (durable mailbox) · Agent Directory (presence/skills) · Conflicts Panel (overlapping resource claims) · Standby/Idle agents · Hire-requests / orchestration queue (reuse-vs-spawn) · Comms feed/message queue · Skills/Capabilities registry.

## AREA 6 — Monitoring / observability
System Health Dashboard (postgres/ntfy/cerbos/daemons, self-heal status) · Cost & Usage Dashboard (real $ from traces, BYO-vs-platform split) · Throughput/Factory metrics (apps shipped, pass@1, fix-loop rate, latency, queue) · Logs Explorer (per-tenant isolated) · **Trace Explorer + Trace Detail** (replay a build; secrets redacted at write-time) · Metrics Explorer / custom dashboards (Pro/Ent) · Web/app analytics for shipped products (Pro/Ent).

## AREA 7 — Incidents & alerts
Alerts/Watchdog rules (daemon down, build stall, component down, SLA breach, deadlock, disk/backup, denial spike) · Incidents List · **Incident Detail / War Room** (responder auto-fix ≤3 before paging; incident-commander RCA) · Self-healing/Responder activity · Postmortem/RCA · Public Status Page admin.

## AREA 8 — Approvals / decisions inbox (governance core)
Approvals Queue (spend / deploy / secrets / data / public-post / hire / resume-paused-app) · Approval Detail (diff, cost/impact, agent rationale, approve/reject/request-changes, MFA re-auth for sensitive) · Approval confirmation / reason capture · Access requests.

## AREA 9 — Integrations marketplace + per-integration config
Integrations Marketplace · Integration Detail · Installed Integrations/Connections (status, reauth, health) · Per-integration config — **GitHub** (repo/branch/push), **Stripe** (Connect for shipped products' billing), **OAuth providers**, **Cloud/AWS** (creds/region/RDS+S3+SecretsMgr), **Domains/DNS/SSL**, **Email**, **Slack/ntfy** · Outgoing Webhooks + delivery logs · OAuth/Developer apps · CLI / local-dev pairing.

## AREA 10 — Deployments & infrastructure
Deployments List/History · Deployment Detail + build logs · Deploy targets ("where deployed"; managed vs BYO-cloud vs static) · **Deploy-to-cloud wizard** (AWS-first; RDS+S3+SecretsManager; Terraform plan→apply) · Compute/scaling config (N workers, same DATABASE_URL, SKIP-LOCKED) · **GPU/creative-compute provisioning** (Replicate/Fal/cloud GPU; cost confirm; auto-teardown) · Environments manager (prod/staging/preview) · Domains/DNS/SSL · Cron/scheduled jobs · Backups/Snapshots/Migration (encrypted `.aosnap`) · Service/resource health.

## AREA 11 — Billing & plans
Billing Overview · Usage & metering (from real builds+tokens) · **BYO-key vs platform-key center** (per-provider keys, routing preview, cost impact) · Cost breakdown/attribution (by product/agent/stage) · Invoices/history · Payment methods + billing details (VAT/Tax-ID) · Plan upgrade/downgrade (proration) · Spend limits / budget controls (hard caps + auto-suspend) · Credits & balance · Seats/license management.

## AREA 12 — Team & org management
Members/People list · Invitations/pending · **Roles & Permissions (RBAC, Cerbos-backed)** · Teams/Groups (Ent) · SSO/SAML + OIDC config (Ent) · SCIM/directory provisioning (Ent) · Domain verification · **Audit Log** (immutable, SIEM export) · Service accounts/machine users (incl. agents) · Org settings / danger zone.

## AREA 13 — Settings (user & account)
Profile · Security/password/2FA · Sessions & devices · Connected accounts/authorized apps · API keys/tokens · Notification preferences (channel matrix, quiet hours) · Appearance/theme · Language & region · Data export (GDPR) · Data deletion/close account · Privacy & consent (training opt-out, DSAR) · Referrals / keyboard shortcuts.

## AREA 14 — Knowledge, templates/blueprints, app store
Knowledge/Memory library (tenant-scoped, pgvector, per-tenant isolated) · Blueprints/Templates library (build-from / save-as) · App Store of generated products (preview, versions, remix/clone, submit-to-public-showcase) · Eval/Benchmark scorecard (pass@1, fix-loop, chaos — Ent).

## AREA 15 — Mobile app (CEO companion: direct / approve / monitor on the go)
- **Entry/nav/home:** Splash + onboarding carousel · Mobile sign-up/login + biometric/app-lock · Bottom tab bar + drawer + org switcher · Mobile Cockpit/home feed · Activity/events feed.
- **Chat/projects/monitoring:** Mobile Orchestrator Chat (proposed-action approve) · Voice/dictation mode (gated-action approval on voice) · Project list + detail · Live preview (webview) · Monitoring dashboard · Live logs/trace · Build/deploy trigger (biometric gate for prod).
- **Approvals/alerts/notifications (companion core):** Approvals inbox + detail + confirmation (swipe approve/reject, biometric re-auth) · Code/diff review · Alerts/incidents list + detail · Notification inbox/center · **Push notifications (ntfy bridge) with inline Approve/Reject actions** · Notification settings + DND/quiet hours.
- **States/system:** Offline/sync/skeleton/error · Search + quick-action sheet · Settings/profile/theme · Widgets / Live Activities / Watch (pending-approvals, build progress) · Force-update / share / permission priming / app-rating.

## AREA 16 — Admin / back-office (Operator = us)
*The biggest differentiator and most under-spec'd area. Same 4 primitives recur — build as internal tooling first.*
Tenant/account list · **Tenant 360 detail** (subscription/usage/users/builds/invoices/BYO-key/flags/support; quick actions) · Tenant users/sub-accounts · **Impersonation/login-as** (reason, read-only toggle, banner, full audit) · Platform usage analytics (activation funnel signup→BYO-key→first-LAUNCH) · Revenue/business metrics (MRR/ARR/churn) · Subscription/billing admin + dunning · **Abuse/Fraud/Trust-&-Safety review** (prompt-injection charters, sandbox-escape attempts, denial spikes, crypto-mining) · Content/charter moderation queue · Support/helpdesk console + ticket detail (customer-360 sidebar) · KB/Help-center admin · **Feature flags / entitlements** (plan→feature/quota matrix, per-tenant override) · System config/platform settings (maintenance mode, model-routing defaults, kill switches) · Internal audit/admin-action log · Staff/internal RBAC · Jobs/queue/worker monitor (tasks + SKIP-LOCKED) · Fleet-wide self-heal / chaos console · Email/notification log · Data-pipeline/ingestion health · Announcements/broadcast · Tenant provisioning (manual) · Compliance/Legal (DSAR) console · Eval/benchmark fleet (cross-tenant quality).

---

## Tiering logic (embedded above)
- **Free:** single-tenant basics + BYO-key + limited builds.
- **Pro:** team/collab, deeper observability/debugger, marketing-kit, custom dashboards.
- **Team:** seats, shared blueprints, role management.
- **Enterprise:** SSO/SCIM/RBAC, audit/SIEM, postmortems, deploy-to-own-cloud, eval scorecard, data residency.

## Signature screens to design first (the differentiators)
CEO Cockpit + Orchestrator Chat · SPEC→BUILD→QA→REVIEW→LAUNCH Pipeline View (green QA gate + crash-resume) · Agent Comms Graph + Directory + Conflicts · Approvals/Decisions Inbox (the governance core that makes it safe for strangers — the actual sellable value) · Trace Explorer debugger (redacted, per-tenant isolated) · BYO-key vs Platform-key Center · Self-healing / Incident-commander surface.
