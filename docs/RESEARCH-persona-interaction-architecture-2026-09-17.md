# Persona and Interaction Architecture Research — 2026-09-17

## Decision summary

Agent OS must present a familiar operating system for work, not expose multi-agent machinery as its
primary product model. Every persona should enter through the same durable mission truth, projected into
the outcomes, owned work, decisions, evidence, and controls they need. Agent topology, traces, model calls,
and raw logs remain available through progressive disclosure for diagnosis.

This materially changes the UI priority order:

1. mission home: outcome, owner, status, next milestone, cost, risk, and what needs a person;
2. personal decision inbox: exact action, recommendation, alternatives, consequences, evidence, and deadline;
3. work: human-accountable ownership with an agent shown as delegate/executor;
4. conversation: steer now, queue next, clarify, revise, pause, resume, stop, and replan;
5. review/evidence: intent, changes, checks, claims, evidence, residual risk, and release decision;
6. control center: identity, authority, tools, data policy, budgets, audit, retention, and incidents.

Chat is one interaction channel, not the database, task manager, approval protocol, or evidence system.

## Research basis

The conclusions converged across current primary product guidance, empirical research, and standards:

- Microsoft HAX's 18 human-AI guidelines require expectation-setting, efficient correction and dismissal,
  contextual explanation, uncertainty-aware scope, and learning from user behavior. The HAX Playbook also
  recommends proactively designing recovery from predictable AI interaction failures.
  [HAX Guidelines](https://www.microsoft.com/en-us/haxtoolkit/ai-guidelines/),
  [HAX Playbook](https://www.microsoft.com/en-us/haxtoolkit/playbook/)
- Google's People + AI Guidebook centers user needs, mental models, feedback/control, explainability, and
  graceful failure; its control audit explicitly treats automation as a spectrum.
  [PAIR Guidebook](https://pair.withgoogle.com/old-gb/)
- Microsoft research on AI-assisted product managers and developers finds that desired delegation varies
  with accountability, task, experience, identity, and risk. People retain ownership of consequential
  specifications and outcomes even when agents perform lower-accountability work.
  [PM study](https://www.maraulloa.com/pms_genai.pdf),
  [developer autonomy study](https://www.microsoft.com/en-us/research/publication/you-shall-not-pass-where-and-why-developers-draw-the-line-on-ai-autonomy/)
- Linear preserves a human assignee while showing the delegated agent, avoiding the fiction that an agent
  eliminated accountability. GitHub separates plan, interactive, and autonomous modes and distinguishes
  immediate steering from queued follow-up.
  [Linear assignment](https://linear.app/docs/assigning-issues),
  [GitHub agent sessions](https://docs.github.com/en/copilot/how-tos/github-copilot-app/agent-sessions),
  [GitHub steering and queueing](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/steering-and-queueing)
- Apple classifies interruption as passive, active, time-sensitive, or critical and requires accurate
  urgency, consent, and in-app controls. Slack adds per-device preferences, schedules, focus, and activity
  filtering. These establish that routing is a user policy, not merely an event-to-webhook map.
  [Apple notification management](https://developer.apple.com/design/human-interface-guidelines/managing-notifications),
  [Slack notification controls](https://slack.com/help/articles/201355156-Configure-your-Slack-notifications)
- Jira permits approvals where people already work, including email, Slack, Teams, and its portal. The
  approval remains one structured lifecycle rather than becoming unrelated chat messages.
  [Jira approvals](https://support.atlassian.com/jira-service-management-cloud/docs/what-are-approvals/)
- GitHub's pull-request model and Playwright's trace viewer support evidence-rich review: purpose, changes,
  checks, comments, step-level runtime evidence, and an explicit disposition.
  [GitHub pull requests](https://docs.github.com/en/pull-requests/reference/pull-requests),
  [Playwright Trace Viewer](https://playwright.dev/docs/trace-viewer-intro)
- NIST treats AI risk management as a lifecycle spanning design, deployment, use, and evaluation. Its 2026
  agent evaluation work emphasizes structured, machine-readable trails joining claims and decisions to
  evidence. OWASP recommends least privilege, downstream execution in the user's context, and human approval
  for high-impact actions.
  [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework),
  [NIST agent evaluation probes](https://www.nist.gov/programs-projects/building-evaluation-probes-agentic-ai),
  [OWASP Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)
- WCAG 2.2 requires programmatically determinable controls and status updates that assistive technology can
  announce without forced focus changes. Accessibility applies to intake, progress, decisions, evidence, and
  recovery—not only final visual polish. [WCAG 2.2](https://www.w3.org/TR/WCAG22/)
- Google Cloud's current reliability guidance defines reliability in user-experience terms and emphasizes
  observability, horizontal scale, graceful degradation, recovery tests, and learning. The system should keep
  its durable modular-monolith/cell path rather than add distributed-system machinery without measured need.
  [Google Cloud reliability pillar](https://docs.cloud.google.com/architecture/framework/reliability)
- AG-UI's current event model separates lifecycle, activity, subagent, state snapshot/delta, and custom events;
  its state guidance uses a fresh snapshot to recover divergence. The protocol is valuable at the public edge,
  while still-evolving/draft event areas make it the wrong internal database schema.
  [AG-UI events](https://github.com/ag-ui-protocol/ag-ui/blob/main/docs/concepts/events.mdx),
  [AG-UI state](https://github.com/ag-ui-protocol/ag-ui/blob/main/docs/concepts/state.mdx)
- The HTML living standard defines SSE reconnect and `Last-Event-ID`; the W3C Push API instead defines
  service-worker-scoped subscriptions and push-service endpoints/key material. Foreground live updates and
  background mobile attention therefore need separate adapters and receipts.
  [WHATWG SSE](https://html.spec.whatwg.org/multipage/server-sent-events.html),
  [W3C Push API](https://www.w3.org/TR/push-api/)

## Persona defaults

| Persona | Default surface | Must answer immediately | Default detail |
|---|---|---|---|
| CEO/founder | Outcomes and decisions | What changed, what needs me, money/time at risk, what happens next? | Plain-language portfolio and daily digest |
| Product/engineering leader | Program and dependencies | What is blocked, who owns it, what changed, is quality improving? | Milestones, dependencies, variance, review load |
| Builder/engineer | My work | What do I own, what context/tools exist, how do I test and escalate? | Plan, branch/diff, tests, tool receipts, logs |
| QA/reviewer | Review queue | Which claim is made, what independent evidence supports it, what changed? | Acceptance matrix, replay, evidence provenance |
| Security/admin | People and policy | Who can do what, what data/tools are exposed, what must be revoked? | Identity, authority, retention, audit, hard budgets |
| Operator/SRE | Incidents and fleet | What is unhealthy, how old is it, what recovered, what is safe to redrive? | Queues, leases, retries, SLOs, incident lifecycle |
| External client | Request and milestones | What was agreed, what is ready, what decision is needed from me? | Privacy-safe previews, comments, approvals, invoices |
| Developer/integrator | API/CLI | What is the contract, cursor, idempotency rule, and failure mode? | Schemas, examples, traces, test environment |

These are presets, not data-access boundaries. A person can change information density without bypassing
authorization, and one person may hold several personas.

## Required journeys

### First use

1. Authenticate and select or create a company.
2. See a resumable readiness checklist: identity, model, budget, optional integrations, and sample mission.
3. Describe an outcome in ordinary language.
4. Choose a collaboration preset: handle routine choices, ask for important decisions, or work closely.
5. Review the interpreted charter, predictable prerequisites, range estimate, and material risks.
6. Start only after the accepted mission revision and authority envelope are visible.

### Long-running mission

1. Mission home shows last proven progress, next milestone, current owner, cost, risk, and ETA range.
2. A user can steer current work or queue a follow-up without discarding safe completed work.
3. New information creates a human-readable mission revision and fences stale authority.
4. Healthy long work checkpoints and reports; silence, lease loss, and non-convergence become signals.
5. A projection failure is shown as stale/unavailable, never as an empty success state.

### Human decision

1. Route an attention item to an exact recipient/delegate.
2. Show action, target, requesting actor, recommendation, alternatives, consequences, reversibility, cost,
   evidence, expiry, and safe default.
3. Permit approve, deny, request changes, delegate, snooze, or dismiss only when semantically valid.
4. Bind the response to the exact mission revision and effect parameters.
5. Remove resolution from the open count without deleting history.

### Review and release

1. Present intent and affected requirements before implementation detail.
2. Join acceptance measure → claim → independent evidence → artifact/version.
3. Show diffs/previews, test results, security findings, known limitations, and residual risk.
4. Permit approve, request changes, or reject with a durable reason.
5. Publish only the reviewed revision; retain release and rollback receipts.

## System requirements

### Identity and capability

- Server returns subject, active tenant, roles, named capabilities, and persona preset.
- Every mutation enforces capability server-side; UI only hides unusable controls for clarity.
- Tenant roles, platform operator roles, and service identities remain separate.
- Human accountability and agent delegation are distinct fields.

### Experience and live data

- Stable IDs update cards without destroying draft text, selection, focus, or scroll.
- Use durable cursor/SSE updates with reconnect and polling fallback; polling must not rebuild dirty forms.
- Primary views use bounded summaries; evidence, effects, and audit history require cursor pagination.
- Route-addressable mission, decision, evidence, and incident views support secure deep links.

### Attention and communication

- Immutable events remain separate from mutable per-recipient read, acknowledged, snoozed, resolved, and
  dismissed projections.
- Routing evaluates recipient, category, urgency, channel, quiet hours, digest cadence, escalation, redaction,
  mission scope, safety floor, and interruption budget.
- The frozen delivery decision records why a person was or was not interrupted.
- Critical safety events may bypass quiet hours; promotional or routine progress never may.
- In-app, browser/PWA, email, Slack, Teams, and webhook delivery share one decision/receipt lifecycle.

### Evidence, audit, and observability

- Audit answers who decided or changed what, under which identity/authority/revision, with which evidence.
- Telemetry separately answers where time/cost accrued and why health degraded.
- Tool parameters, policy results, budgets, retries, model usage, and external receipts remain correlated.
- Telemetry export failure cannot block business execution and metric labels cannot have unbounded cardinality.

### Accessibility and mobile

- Intake, status, approval, review, and recovery meet WCAG 2.2 AA keyboard/screen-reader expectations.
- Status is announced without stealing focus; drawers trap and restore focus.
- Mobile retains company switching, inbox, decisions, and sign-out with at least 44×44 CSS-pixel targets.
- Installable PWA is the first mobile channel; native applications remain evidence-driven future work.

## Priorities

### P0

- role/capability consistency and role-aware shell;
- non-destructive refresh;
- personal decision state and notification preferences;
- structured decision response boundary;
- mission collaboration controls and revision/steering;
- review packet with linked claims/evidence;
- WCAG 2.2 AA core journeys;
- tenant isolation, least privilege, exact approvals, hard spend stops.

### P1

- persona-specific landing projections;
- SSE/cursor event plane and bounded pagination;
- digest/escalation/delegation and audience-aware external routing;
- guided onboarding and provider templates;
- installable PWA with provisioned background push;
- external client portal and existing-tool deep links;
- outcome/cycle-time/quality/intervention/cost dashboards.

### P2

- voice intake/status;
- adaptive information density with explicit override;
- reusable versioned organization/workflow templates;
- native mobile only if measured PWA limitations justify it.

## Implemented baseline — 2026-09-17

The first P0 slice now includes a role/capability projection, non-destructive ambient refresh, personal
notification state and preferences, recipient-aware/redacted external routes, mission collaboration controls,
linked claim/evidence rendering, core keyboard/dialog/mobile improvements, and an installable shell. Human
responses enter through a structured endpoint backed by durable intent, deterministic graph-event identity,
lease recovery, conflict rejection, and immutable notification truth. Remaining priorities above are retained
as product requirements rather than represented as already complete.

The operator projection now defaults to the decision inbox, keeps role-aware deep links, explains manager
signals, exposes bounded and payload-redacted execution diagnostics, and shows the durable queue timeline only
to owners/operators. A failed human response can be redriven explicitly without asking the person to repeat
their decision: the immutable intent and event identity remain fixed, a new bounded retry cycle starts, and
lifetime attempts plus the redriving actor remain auditable.

The attention feed now uses a stable opaque `(created_at, notification_id)` cursor rather than an unbounded
offset. The workspace can page backward without duplicates, keeps decision drafts across refresh/page renders,
and applies every cursor inside the authenticated tenant and recipient projection. The live SSE/cursor event plane
and the privacy-reduced Web Push adapter are now implemented. Live push still requires deployment-specific VAPID
provisioning and a real-device smoke; the durable inbox remains authoritative when a browser or provider cannot
deliver in the background. Push clicks now resolve an opaque, person-scoped delivery only after authentication,
recheck current item authorization, and focus the exact item even beyond the inbox's first page without putting
mission or notification content into the background payload.

First use now has a server-derived, resumable readiness projection covering identity, durable admission,
standing organization, hard spend guard, model policy, human-decision routing, optional connectors, and the
first mission. It deliberately labels an inherited platform model `verify_on_first_use`: API health cannot
observe worker-only provider credentials, so the first metered turn—not a green setup badge—is the proof.

Newly planned human waits now carry a validated, bounded decision brief through durable graph state into the
personal inbox. The card shows requester, recommendation, alternatives, consequences, reversibility, safe
default, material cost/deadline, and only the actions the workflow declares valid. API admission and graph
execution enforce the same action set, so a free-text response cannot silently become an approval and “request
changes” appears only when the rejection route is explicitly designed to accept it.

Mission drawers are now addressable by an opaque run ID in the workspace route. The client resolves the run
only after authentication and active-organization selection, and the existing tenant-fenced endpoint makes a
foreign and nonexistent mission indistinguishable. Opening, closing, reloading, and browser-back navigation
retain the familiar drawer interaction while producing a link a teammate can use inside the same authorized
company.

The role model now includes least-privilege human `builder` and `reviewer` memberships rather than representing
those personas only as UI labels. Builders receive assigned-work execution and artifact-publication capability,
without mission or administration authority. Reviewers receive review-read and decision-response capability,
without artifact or runtime mutation authority. Personal inbox queries resolve both subject and authenticated
role audiences on the server, so teams can route a review to `role:reviewer` without leaking it to builders or
viewers and without duplicating an item addressed to both the person and role.

Mission participation now supplies the missing resource boundary for builders, reviewers, clients, and viewers.
A tenant role describes allowable behavior, while a separately audited grant identifies the exact mission. The
portfolio, direct mission APIs, assurance and management projections, artifacts, releases, experience feed, and
legacy role-addressed inbox records all enforce that boundary. Client/viewer projections show outcomes,
progress, milestones, and approved deliverables without internal communications, staffing, effect/authority
ledgers, raw execution identifiers, or arbitrary artifacts. New role-addressed mission notifications are
expanded into exact participant subjects before publication; an empty audience escalates to accountable
management instead of silently broadcasting or dropping the question.

## Satisfaction and evolution loop

“Everyone satisfied” is not a one-time engineering state and cannot be established by synthetic personas alone.
Each release must maintain canonical persona tasks and recruit representative users. Measure:

- completion rate and time to understand current state;
- decision latency, reversals, and authorization errors;
- unnecessary interruptions, snoozes, dismissals, and channel opt-outs;
- review time, coverage, escaped defects, and rollback rate;
- clarification cycles and requirement revisions;
- accessibility completion with keyboard and assistive technology;
- trust calibration: appropriate reliance and override, not maximized trust.

Agent-based adversarial review broadens coverage. Real users remain the final acceptance evidence, and their
feedback becomes versioned requirements rather than silent UI drift.
