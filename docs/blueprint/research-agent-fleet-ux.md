# Agent-Interaction & Observability UX — Research & Design Report

**Context:** Designing the UX for a platform where a **non-technical "CEO" user** interacts with and monitors a **hierarchy of LLM agents** (orchestrator → role-specialized agents → sub-agents) that autonomously build and ship software.

**Method:** Heavy web research (2025–2026) across real products, docs, and engineering blogs, covering seven UX problems: (1) chatting with agents, (2) live activity view, (3) agent org-chart/hierarchy, (4) inter-agent communication feed, (5) monitoring dashboards, (6) incidents & on-call, (7) capacity/backpressure/idle agents.

---

## Executive Summary — the 12 highest-leverage design moves

These cut across all seven sections. Each is grounded in a real product (cited inline below in the detailed sections).

1. **Every agent gets its own addressable thread.** The CEO can DM the orchestrator *and* drop into any sub-agent's thread. (Devin's "each managed Devin has its own session link, so you can message it directly" is the gold standard. Counter-pattern: Manus, where sub-agents are invisible.)

2. **A left-rail thread/session list with unread badges + pinning,** rolled up under an initiative/"Project" container. (Devin's orange unread dot that clears on open; Vercel v0's Project→many-chats.)

3. **A clear addressing grammar:** mention-to-start, mention-in-thread to steer, a keyword to fork a new agent, "list my agents" to inventory. (Cursor's Slack grammar; Claude Code's `@agent-<name>` typeahead showing live status.)

4. **Outcome views beat transcripts for non-technical users.** Lead with a live checklist + sticky % progress bar + one-line "current step," plain-language activity cards, and action receipts ("what changed, where, when"). Raw tool calls/thinking/diffs live one click deeper. (HatchWorks "chat-first fails"; Claude Code TodoWrite; Magentic-UI plan steps.)

5. **Thinking collapsed by default, summarized when expanded.** Never stream raw chain-of-thought at a CEO. (Claude Code's Ctrl+O gating; ChatGPT's "Thought for 6s" capsule; ReTrace's provenance caveat.)

6. **A live org-map of agents** (React Flow node-graph: orchestrator on top, workers/sub-agents below) with **per-node status by color *and* motion** and an **animated edge only on the currently-active hand-off.** (ClawPort Org Map; Microsoft Conductor animated edges; n8n's negative lesson — authoring canvases don't auto-highlight live execution, you must build that.)

7. **A readable agent-to-agent feed** as a Slack-style labeled transcript (`Researcher → Writer: "handing off the draft"`), grouped by plan step (completed steps auto-collapse), with a one-line "why" on each hand-off. (AutoGen transcript; Magentic-UI ledger narration; decision-provenance research.)

8. **A fleet dashboard organized by Golden Signals + agent-specific cost/quality + an exec ROI strip:** Exec KPIs → Traffic → Saturation/Queues → Latency → Errors/Health → Cost/Tokens → Quality/ROI. Universal drill-down: aggregate KPI → run table → trace waterfall → raw I/O. (Datadog LLM Observability; Grafana RED; USE method.)

9. **Incidents on a Triggered → Acknowledged → Resolved lifecycle** with a visible escalation ladder + timers (Agent → senior agent → on-call human → team), where **acknowledgement halts escalation.** (PagerDuty; Opsgenie; Datadog On-Call.)

10. **Escalation-to-human is a first-class Approval Inbox,** not a buried log: each card shows which agent, what it wants to do, why it stopped (low confidence / high-risk / edge case), the evidence, and Approve / Reject / Modify — with **propose-then-commit** (the action is blocked until a human signs off). (AwaitHuman/AgentRQ; Rootly/Augment "AI SRE" maturity ladder; confidence-threshold escalation ~0.6.)

11. **Decide agent autonomy by reversibility, not capability.** Reversible actions (retry, scale, restart) auto-run; irreversible ones (touching money/customer data) gate on a human. Make each agent's autonomy level visible and dial-able (Suggest → Draft → Execute). (Rootly maturity ladder; HatchWorks autonomy slider.)

12. **Capacity as a traffic-light pool** (green busy / grey standby / yellow starting / red crashing) with one headline backlog number — **"oldest waiting task is N minutes old"** — and legible autoscaling events. (k9s pod colors; Sidekiq queue-latency / SQS ApproximateAgeOfOldestMessage; KEDA scaling feed; "Pending pods = saturation ceiling.")

---

## 1. Chat With Agents

### Devin (Cognition)
- **Threading:** chat panel + **left-rail session list**; **pin sessions**, **read/unread orange dot** (clears on open), filter-out scheduled sessions; a **Progress tab** unifies steps (clickable), with Shell/IDE/Browser views per session.
- **Orchestrator → sub-agents ("Devin can now manage Devins"):** a primary Devin scopes work, assigns pieces to managed child Devins (each a full Devin in its own VM), monitors, resolves conflicts, compiles results. **Each managed Devin has its own session link, so you can inspect or message it directly.** The coordinator can message child sessions mid-task, schedule messages to itself for checkpoints, monitor per-child compute (ACU), sleep or terminate children.
- **Slack:** tag `@Devin`; replies in-thread; inline control words `mute`/`unmute`, `(aside)`, `sleep`, `archive`, `EXIT`; mode prefixes `!fast`/`!lite`/`!ultra`/`!agent`/`!new`; per-run "Enable Slack notifications" for DM status.

### Claude Code
- **Three concepts:** subagents (own context window; a **subagent panel appears below the prompt**, full tree with `(+N)` descendant counts, color-coded), background agents (concurrent; **Ctrl+B** to background), agent teams.
- **Addressing escalates:** natural language ("use the test-runner subagent…"), **`@agent-<name>` typeahead** (guarantees that agent; **shows live status next to running agents**), or session-wide `claude --agent <name>`.
- **Background HITL (2026):** when a background subagent needs permission, the prompt **surfaces in the main session naming the asking subagent**; approve to continue or Esc to deny that one call without killing the agent. `/agents` opens a tabbed manager with a **Running tab** (open/stop live agents). `SendMessage` resumes a specific agent by ID/name with full context.
- **Warning:** a filed bug — background output bleeding into the foreground chat disrupts the active conversation → **visually separate background streams.**

### OpenAI — ChatGPT Agent Mode / Operator / AgentKit
- Invoke via `/agent`; watch it work in a replay-like interface. **Strong steering patterns:** pauses for clarification/confirmation, requests permission before consequential actions, you can **interrupt / type "stop" / correct and continue**, **"Take over browser"** for logins, **Watch Mode** (pauses if you click away during financial/email/personal-data actions). AgentKit = drag-and-drop **Agent Builder** + embeddable **ChatKit**. *(Note: OpenAI is winding down Agent Builder/Evals by 2026-11-30 — cite the patterns, not the product.)*

### Lindy AI
- Agent-to-agent via **"Agent Message Received" trigger** + **"Send Message" action** with a **"Target Lindy" dropdown** (explicit addressing, not @-mention). Receiver's **"Follow-up Message Behavior": Handle-in-same-task / Create-new-task / Ignore** — a clean model for how a sub-agent thread should treat repeated pings.

### Manus — Wide Research
- Orchestrator decomposes → spins up **hundreds of parallel sub-agents** (each own VM/context) → aggregates. **Sub-agents never talk to each other** (avoids context pollution) and, notably, **expose no per-sub-agent UI** — the "orchestrator-mediated, agents are invisible" counter-pattern. Telegram integration for DMing your agent.

### Cursor — Agents & Background/Cloud Agents
- **Agents Window**: multiple parallel agents (each own working set, model, approval policy, conversation tab) on git worktrees or cloud VMs, ~8 parallel ceiling; cloud agents "report results back to your IDE asynchronously."
- **Best real-world addressing grammar:** `@Cursor [prompt]` to launch; `@Cursor agent [prompt]` forces a *new* agent in an existing thread; `@Cursor in <repo>` / `@Cursor with opus` for targeting; follow-up adds instructions to *your* existing agent; `@Cursor list my agents` inventories; completion notification with "view PR in GitHub" / "Open in Cursor."

### Factory.ai (Droids), Replit Agent, Vercel v0
- **Factory:** Droids across Terminal/IDE/Browser/Slack/Jira; primary Droid delegates to custom sub-droids; **"Tag @Factory and move on"** fire-and-forget; in-thread answers in incident channels.
- **Replit:** single chat + **checkpoints** (auto-snapshot per major request, one-click rollback); cheap inline pause-and-ask gates (answering a paused question doesn't consume a checkpoint) — good for non-technical approval cadence.
- **Vercel v0:** **Projects** (container: GitHub/Vercel integration, env vars) vs **chats** (threads bound to a project) — directly analogous to "an initiative → many agent conversations"; modal on new chat asks continue-in-project or start-new.

### Cross-cutting patterns
- **Slack-as-agent-surface (Slack/Salesforce):** Slackbot routes to the right agent and can invoke multiple agents in one thread; agents respond **in-thread, not the main channel**; explicit @-mentions prevent bot-loops.
- **LangChain Agent Inbox (canonical async HITL):** queue of interrupt cards, each with title, markdown instructions, editable args, and **Accept / Edit / Respond / Ignore**; multiple agents = multiple "inboxes."
- **HatchWorks "chat-first fails":** chat-only breaks because work is async/multi-step (invisible actions, no pause/resume, no rollback). Recommends **Taskboard + Outcomes, Activity Timeline, Start/Stop/Pause/Resume, Autonomy slider (Suggest→Draft→Execute), Two-Phase Actions (plan→validate→execute+receipt), Action Receipts, Human Checkpoint Gates, Evidence Panel (citations not CoT dumps), Role Cards (per-agent scope/tools/permissions), Budget+Time Boxes.** Target **Level 2 "Guided agent"** for v1.

---

## 2. Live Activity View

### Three layout archetypes
1. **Multi-panel "agent computer" (Devin):** left = sessions; center = conversation; right = **Workspace** tabs (Progress, Shell/terminal, Browser screenshots, Editor with diffs + "Global Work View" of modified files, Planner to-do). A **"Following" toggle** auto-switches to wherever the agent is working, with the logo on the active tab. A **scrubber timeline** replays every command/edit/browser action; Progress steps are clickable. Intervene by chatting to redirect or **taking over the IDE/terminal/browser**, then handing back.
2. **Streamed log / TUI (Claude Code):** tool calls as color-coded, expandable entries (Read=blue/Write=green/Edit=amber/Bash=red convention); **sticky todo checklist + % progress bar**; **line-by-line syntax-highlighted diffs**; **thinking collapsed by default** (Ctrl+O reveals gray-italic streamed reasoning); **Ctrl+C** interrupts mid-stream. GUI wrappers (e.g., Cogpit) add SSE "no-refresh" streaming, status colors, per-turn token/cost bars, multi-agent kanban.
3. **Run-tree / waterfall trace (observability):** LangSmith (nested run tree + right-hand detail panel, token-by-token streaming), AgentOps (chronological **session waterfall** + selected-event detail + chat-history rendering + **time-travel replay**), Langfuse (hierarchical observations + **timeline view** for bottlenecks/parallelism), OpenAI Traces (typed spans: agent/generation/function/handoff/guardrail), Helicone (request logs + **Sessions** tree). **LangGraph Studio is the standout for *live graph nodes lighting up*** + state inspection + interrupt()-based HITL + time-travel.

### File-diff patterns (cross-product)
- **Inline vs side-by-side toggle**; **hierarchical accept/reject** (per-chunk Accept/Reject/Edit, per-line +/−, Accept-All/Reject-All); green/red/gray color coding; multi-file sidebar with `[+12/−3]` summaries. **Principle: separate reviewing from editing** so the human stays in decision (yes/no) mode.

### Reasoning-trace patterns
- **Collapsible "Chain of Thought" accordion** grouping steps + an aggregate count; **auto-open while streaming, auto-collapse when done**, with a duration label; **collapsed by default** everywhere. Shape-of-AI "stream of thought" = plan-before-acting / execution-log / compact-summary; mark each step queued/running/waiting/errored/completed. **ReTrace caveat:** summaries aid skimming but collapse provenance → keep raw trace on details-on-demand.

### Synthesis for a non-technical CEO
Checklist + status chip on top → stream of **plain-language activity cards** ("Reading your sales spreadsheet," "Editing 3 files") in the middle → collapsed thinking + raw diffs/traces one click deeper → **pause / redirect-in-plain-English / approve-before-X** always reachable. Stream via SSE (motion = "it's alive"). Offer a scrubbable replay timeline (Devin) for after-the-fact review. Progressive disclosure is the through-line: **summary → expandable detail → raw trace**, defaulting the CEO to layer 1.

---

## 3. Agent Org Chart / Hierarchy

### ClawPort "Org Map" — closest 1:1 reference
Open-source, built on Claude Code agent teams. **React Flow node-graph**: orchestrators on top, sub-agents below, edges = reporting/delegation. **Per-node real-time status (running / idle / error)**; click a node → profile panel (capabilities + the agent's `CLAUDE.md`). Companion views form a "command center": **Agent Chat** (steer one agent), **Kanban** (queued/in-progress/done), **Activity Console** (event log + floating live-stream widget, click row to expand raw JSON), **Cost dashboard** (daily chart, per-job, model distribution). **The winning layout = persistent node-graph "map" + swappable detail/feed pane.**

### Microsoft Conductor (2026)
Interactive **DAG with animated edges indicating execution flow** (active connection visibly flows). Node-click reveals prompt/model/tokens/cost/activity/output. **Human approval gates inline in the graph.**

### LangGraph Studio
Node-link graph; **solid (unconditional) vs branching (conditional) edges**; per-node execution status, **highlights the current/active node and the path taken as it runs**; hover for options, click to inspect state; **time-travel**; interrupted threads pinned at the triggering node. Developer-grade — leaves color semantics to you.

### Magentic-One / Magentic-UI — CEO-friendly alternative to a graph
Lead **Orchestrator** delegates to named workers (WebSurfer, Coder, FileSurfer, Terminal). **The plan is the primary structure**, not a graph: an ordered list of natural-language steps, each with **title + details + assigned agent name**; user presses **"Accept Plan"** to start. During execution each step is a **collapsible banner** (completed steps auto-collapse) + a **progress bar**; left panel = session navigator (each session shows whether user input is required); right panel = live browser with **upcoming actions animated as a preview**.

### CrewAI, AutoGen Studio, n8n, OpenAI Agents SDK
- **CrewAI hierarchical process** literally simulates an org chart (manager agent delegates to workers); ships its live picture as **trace trees** (CrewAI AMP / W&B Weave), not a spatial chart.
- **AutoGen Studio:** drag-and-drop **Team Builder** + **Playground** with live message streaming and a **"control transition graph"** (who-speaks-next). *(Now in maintenance mode; successor Microsoft Agent Framework is code-first.)*
- **n8n (negative lesson):** the canvas does **not** animate/highlight nodes step-by-step during a live run — authoring canvases are built for editing, not live monitoring. **Live status highlighting is a deliberate feature you must build.**
- **OpenAI Agents SDK:** static `draw_graph()` (yellow agent boxes, green tool ellipses, solid=handoff/dotted=tool/dashed=MCP) + the Traces dashboard for live/historical span trees.

### Recommendations
ClawPort "Org Map + detail pane" as the spine. **Status by color *and* motion** (idle=grey, running=blue/teal pulse, done=green check, error=red badge, waiting-on-human=amber) with a visible legend. **Animate only the active hand-off edge.** Click node → friendly profile (current task in plain English, reports-to, recent outputs, cost; prompts/tokens behind "advanced"). **Offer a Graph ⇄ Plan toggle** (Magentic-UI plan view is often more readable for a CEO). Always surface a **"needs your input" queue** regardless of which agent raised it.

---

## 4. Inter-Agent Communication Feed

### Patterns observed
- **A — Group-chat transcript (AutoGen):** agents post to one shared thread, **explicitly sender→recipient labeled** (`supervisor (to chat_manager): …` + `Next speaker: cloud`). Most directly human-readable.
- **B — Ledger / plan-progress narration (Magentic-UI):** Orchestrator emits a Progress Ledger each round: is-request-satisfied, are-we-looping, is-progress-being-made, **next speaker + the instruction to send them.** Reads as collapsible plan-step banners tagged by agent.
- **C — Animated edges (Conductor / React Flow):** hand-off shown spatially as the active edge flows; click reveals what was passed.
- **D — Span/trace tree (LangSmith/Langfuse/OpenAI):** delegation = span nesting; great for engineers, **too dense for a CEO** without a friendlier overlay.
- **E — Inbox/mailbox model (Stream0, A2A):** every agent has an email-style inbox; delegation = addressed messages with from/to + task subject. Scales to many agents better than one shared chat.
- **F — Provenance ("why did this agent do this"):** Agent Execution Record capturing intent/observation/inference per step, preserving causal order and even rejected alternatives. Attach a **one-line rationale** to each hand-off (not a CoT wall). (AgentLens is a notable academic UI.)

### Recommendations
Default to a **Slack-style labeled transcript** with avatars/timestamps and a clear "now handing to ___" marker. Render delegations as **first-class hand-off cards** (who → whom, task in one sentence, one-line "why"); clicking a card highlights the corresponding edge in the Org Map (**link the two views bidirectionally**). **Group the feed by plan step, collapsible** (completed auto-collapse). Add a per-message **"Why?"** affordance revealing rationale + evidence, CoT hidden by default. For 20+ agents, prefer an **inbox/threaded model** with filters (by agent / task / "needs-human").

---

## 5. Monitoring Dashboards

### Methodology framing
- **RED** (services): Rate, Errors, Duration — "a proxy for user experience."
- **USE** (resources): Utilization, Saturation, Errors.
- **Four Golden Signals** (Google SRE): Latency, Traffic, Errors, Saturation.

**Mapped to an agent fleet:** Rate→tasks started/min, sessions/day, tool-calls/sec; Errors→run failure rate, tool error rate, eval failures, guardrail blocks; Duration→end-to-end latency, **TTFT**, per-step/tool latency, P50/P95/P99; Saturation→**queue depth, concurrency, worker-pool utilization, 429 headroom**; agent-specific→**cost-per-task / token burn, success rate, human-escalation rate, retries/loops per run, steps per run.** Note: current LLM tools lean RED-heavy and **under-serve the Saturation/queue band** — your fleet dashboard must add it.

### Reference implementations
- **Datadog LLM Observability** (most complete): out-of-box **"Operational Insights" dashboard**; named metric catalog (`ml_obs.span.llm.input/output/total.tokens`, reasoning/cache tokens, `ml_obs.*.duration`, **cost in nanodollars**), faceted by env/app/model/provider/service/span_kind. **Cost view:** Total Cost, Cost Change, Total Tokens, Token Change + most-expensive-calls + breakdowns by token type/model/custom cost_tags. **Trace Cluster Map:** clusters trace I/O by topic, **color-coded by eval score or duration** to spot drift/failure clusters.
- **Widget vocabulary (Datadog/Grafana):** Timeseries (trends), Query-Value/Stat (current-state-at-a-glance KPI + sparkline), Top List (most errors / most expensive), Heatmap (latency distribution over time — the standard P-distribution rendering), Table, SLO widget (status + **remaining error budget** + target), State-timeline/Status-history (up/busy/idle over time).
- **New Relic AI Monitoring:** total requests, avg response time, token usage, feedback, error rate; per-agent latency/error/token panels; AI Responses table → **trace waterfall (errors red) + entity/agent map.**
- **LangSmith / Langfuse / Helicone / AgentOps / Phoenix:** trace+LLM-call counts, latency percentiles, token+cost by type/model/user/session/feature, top-5 tools by count/error/latency, curated **Cost/Latency/Usage** dashboards, session waterfalls, LLM-as-judge eval scores.
- **Exec/CFO KPI layer:** **cost-per-task** (benchmark ≥80% cheaper than human labor), task success rate, **human-intervention rate (>40% = inadequate ROI)**, cycle time, incidents per 1,000 runs, **agentic uptime** (orchestration-graph completion %, not API uptime), **blast-radius cap** ($ ceiling), **agent ROI per workflow** (labor $ saved − operating cost; red if negative).

### Proposed CEO-facing dashboard (7 bands)
- **Band 0 — Exec strip (Stat tiles + Δ):** Tasks completed today (& success %), Cost today + Δ vs budget, **Cost-per-task** vs human baseline, **Human-intervention rate** (red if >40%), Agentic uptime, Active agents now.
- **Band 1 — Traffic:** tasks started/min, sessions/day; **token burn rate** (in/out split); busiest agents/workflows.
- **Band 2 — Saturation/Queues:** **queue depth** gauge (threshold-colored), concurrency vs capacity (%), 429 headroom / retry-storm, per-worker state-timeline.
- **Band 3 — Latency:** **latency heatmap** + P50/P95/P99 timeseries; **TTFT**; per-step/tool slow-list.
- **Band 4 — Errors/Health:** run + tool-call failure-rate % by component; top error sources; **SLO widget + error budget**; incidents per 1,000 runs; guardrail blocks; trace-cluster map.
- **Band 5 — Cost/Tokens:** total cost + total tokens over time (cached vs non-cached); most-expensive runs/prompts/tools; cost attributed by agent/workflow/model/customer/sub-agent/external-API; cost-per-task-by-workflow vs human baseline.
- **Band 6 — Quality/ROI:** eval/quality scores (success/grounding/safety); agent ROI per workflow (red if negative); blast-radius cap.
- **Drill-down spine (every panel):** aggregate KPI → filtered run table (row = trace) → **trace/span waterfall (errors red) + agent map** → session replay of raw prompts/completions/tool calls.

---

## 6. Incidents & On-Call

### The shared lifecycle (copy this)
**Triggered** (active, unowned, escalation clock running) → **Acknowledged** (a human claimed it; **halts escalation**; reverts to Triggered if not resolved before an ack timeout) → **Resolved** (closeable/reopenable). Used identically by PagerDuty, Datadog On-Call, Opsgenie.

**PagerDuty's three independent dials:** **Priority** (resolution order) vs **Urgency** (notification intensity — high pages aggressively, low just records) vs **Severity** (impact, on the alert). Don't conflate them.

### Reference UIs
- **PagerDuty:** **"On Call Now"** widget (who's on call at each escalation level right now) + **"My On-Call Shifts"** (current + next shift). Escalation policy = **layered ladder** with **escalation timeout (default 30 min)**; if nobody is on call it **won't even create the incident** (coverage enforced). Incident page: status badge, assignee, Acknowledge/Resolve, **Timeline tab** of every action. **Intelligent Alert Grouping** (~91% noise reduction) so a human sees one grouped incident.
- **Opsgenie:** escalation as a **timed, repeating ladder** (e.g., page on-call → +5 min next person → +10 min whole team), with a **"repeating" toggle** that restarts if never acknowledged — never silently gives up.
- **Datadog On-Call:** monitor-driven pages set **urgency dynamically** (WARN→low, ALERT→high) — *signal severity auto-sets how hard a human gets paged*; "Next Steps" block (Acknowledge/Reassign/Resolve/**Declare Incident**); declaring auto-creates a Slack war-room + auto-timeline + **Incident Commander** role.
- **incident.io / FireHydrant / Rootly:** Slack-native; the incident *is* a channel; **role-centric** (Incident Lead/Commander/Comms/Ops — role = responsibility, not skill); **Runbooks auto-assign roles + auto-escalate severity by impact/duration**; **auto-generated timestamped timelines**; severities can be role-restricted.

### Escalation-to-human (the agent-fleet frontier)
- **AI-SRE maturity ladder (Rootly/Augment):** **Read-only → Advised (proposes + rationale + confidence) → Approved (executes only after human OK) → Autonomous (bounded, reversible).** **Risk + reversibility (not capability) decide what's automated**; guardrails = approval workflows, rollback criteria, **blast-radius limits**, audit trails.
- **Confidence-threshold escalation (~0.6):** below threshold, auto-route to a human **with full transcript + metadata** so they never start cold; agent states hypotheses as probabilities ("Payment latency likely caused by Catalog deploy at 14:03, confidence 0.74").
- **HITL inbox infra (AwaitHuman/AgentRQ/AG-UI):** a queue of **paused** executions; each item shows which agent, the decision point, the reasoning trace, tools already run, the **escalation reason**, and **Approve/Reject/Modify** — with **propose-then-commit** (the tool call is *blocked* until approval; approval before side effects). **HITL vs HOTL:** in-the-loop (pause + wait, high-risk) vs on-the-loop (act + monitor, low-risk) — pick per action class. Multi-channel fan-out (Push/Email/Slack/SMS).

### Recommendations (CEO-facing)
Treat the fleet like a 24/7 team that pages a human when stuck. **Three states always visible** (Triggered red+clock / Acknowledged amber / Resolved green). A persistent **"Who's on call now"** panel for *agents and humans* (on-call agent, standby human, time to next handoff). **Escalation ladder with timers** (Agent → senior agent → on-call human → team), repeating so nothing drops. **Escalation-to-human = first-class Approval Inbox** (which agent / what it wants / why it stopped + confidence / evidence / Approve-Reject-Modify, action blocked until sign-off). **Tie paging intensity to severity automatically.** **Automate by reversibility**, with each agent's autonomy level visible/dial-able. Auto-built timeline + auto-assigned owner from second one.

---

## 7. Capacity, Backpressure & Idle Agents

### Reference monitors
- **Celery Flower:** Workers tab (status online/offline, **active vs reserved tasks** → idle is active=0), Tasks tab (SUCCESS/FAILURE/RETRY/REVOKED + runtime), Broker tab (**queue name + message count (depth) + consuming workers**).
- **Sidekiq:** **Busy** (threads processing) vs idle (total − busy), Enqueued/Scheduled/Retries/Dead. Headline metric: **queue latency = age of the oldest job** ("oldest waiting job is 4 min old") — a brilliant single-number backpressure signal.
- **RabbitMQ:** four-number model — **Ready** (queue depth: growing = consumers too slow/few), **Unacked** (in-flight: growing while Ready flat = consumers stuck), consumer count, rates. **Backpressure controls:** publisher confirms throttle upstream; `max-length` caps the queue.
- **AWS SQS/CloudWatch:** MessagesVisible (depth), MessagesNotVisible (**in-flight**), MessagesDelayed, **ApproximateAgeOfOldestMessage** (the best "falling behind?" alarm).
- **Kubernetes:** **k9s color-codes state** (green=Running, **yellow=Pending**, red=Error, purple=Terminating) — a literal traffic light. **Capacity-exhaustion tell:** when HPA/KEDA wants to scale but there's no node capacity, **new pods sit Pending** (a wall of yellow = "want more workers, nowhere to put them"). `CrashLoopBackOff` = stuck restart loop (exponential backoff). **KEDA/HPA** dashboards show scaling events, active vs desired replicas, the triggering metric; KEDA can **scale to zero** (dashboards must show "0 is intentional").

### Recommendations (CEO-facing)
Agents = workers, tasks = a line of waiting customers. **One traffic-light pool view** (green busy / grey standby / yellow starting / red crashing) — "12 working, 5 standby, 2 stuck." **Show idle positively** as "standby capacity/headroom" (not "wasted"); show **busy ÷ total = utilization** as the saturation gauge. **One headline backlog number: "oldest waiting task is N minutes old"** (Sidekiq/SQS pattern) — far more intuitive than raw counts. **Distinguish waiting vs stuck** (RabbitMQ Ready vs Unacked) as separate bars — different problems, different fixes. **Backpressure as a visible state** ("Backpressure: throttling intake") so it reads as self-protection, not failure. **Make autoscaling legible** (event feed: "2:14 — backlog hit 500, scaling 8→14"; "2:40 — cleared, scaling 14→8") and **raise the saturation ceiling explicitly** ("Want 6 more agents, none available") — the moment a CEO needs to know capacity is maxed.

---

## Sources

### 1. Chat With Agents
- Devin Slack — https://docs.devin.ai/integrations/slack
- Devin "manage Devins" — https://cognition.com/blog/devin-can-now-manage-devins
- Devin session tools — https://docs.devin.ai/work-with-devin/devin-session-tools
- Devin GA — https://cognition.com/blog/devin-generally-available
- Claude Code subagents — https://code.claude.com/docs/en/sub-agents
- Claude Code autonomy — https://www.anthropic.com/news/enabling-claude-code-to-work-more-autonomously
- Claude Code bg-stream bug — https://github.com/anthropics/claude-code/issues/64651
- OpenAI AgentKit — https://openai.com/index/introducing-agentkit/
- OpenAI ChatGPT agent — https://openai.com/index/introducing-chatgpt-agent/
- ChatGPT agent help — https://help.openai.com/en/articles/11752874-chatgpt-agent
- Lindy talk-with-other-Lindy — https://docs.lindy.ai/skills/by-lindy/talk-with-other-lindy
- Manus Wide Research — https://manus.im/blog/introducing-wide-research
- Manus Wide Research docs — https://manus.im/docs/features/wide-research
- Manus Telegram — https://manus.im/blog/manus-agents-telegram
- Cursor Slack — https://cursor.com/docs/integrations/slack
- Cursor changelog 1.1 — https://cursor.com/changelog/1-1
- Cursor background agents guide — https://ameany.io/cursor-background-agents/
- Factory Slack — https://factory.ai/product/slack
- Factory droids guide — https://sidbharath.com/blog/factory-ai-guide/
- Replit Agent docs — https://docs.replit.com/replitai/agent
- v0 projects vs chats — https://community.vercel.com/t/confused-by-projects-chats-in-v0/29558
- Slack agent orchestration — https://slack.com/blog/news/agent-orchestration
- Slack developing agents — https://docs.slack.dev/ai/developing-agents/
- LangChain Agent Inbox — https://github.com/langchain-ai/agent-inbox
- HatchWorks agent UX patterns — https://hatchworks.com/blog/ai-agents/agent-ux-patterns/
- Agentic Design chat patterns — https://agentic-design.ai/patterns/ui-ux-patterns/chat-interface-patterns
- Mastra multi-channel agents — https://mastra.ai/blog/building-multi-user-multi-channel-agents

### 2. Live Activity View
- Devin intro — https://docs.devin.ai/get-started/devin-intro
- Devin 2025 release notes — https://docs.devin.ai/release-notes/2025
- Devin product analysis — https://ppaolo.substack.com/p/in-depth-product-analysis-devin-cognition-labs
- Claude Code todo tracking — https://code.claude.com/docs/en/agent-sdk/todo-tracking
- Claude Code verbose/thinking — https://wmedia.es/en/tips/claude-code-verbose-output-see-thinking
- Claude Code + Cogpit — https://dev.to/gentritbiba/claude-code-is-my-favorite-dev-tool-i-was-flying-blind-until-i-found-cogpit-1edn
- Per-hunk diff issue — https://github.com/anthropics/claude-code/issues/31395
- LangSmith observability — https://docs.langchain.com/langsmith/observability
- LangSmith/LangGraph Studio — https://docs.langchain.com/langsmith/studio
- LangGraph Studio debugging — https://mem0.ai/blog/visual-ai-agent-debugging-langgraph-studio
- AgentOps intro/waterfall — https://docs.agentops.ai/v2/introduction
- Langfuse data model — https://langfuse.com/docs/observability/data-model
- OpenAI Agents tracing — https://openai.github.io/openai-agents-python/tracing/
- Helicone sessions — https://docs.helicone.ai/features/sessions
- Windsurf vs Cursor — https://www.datacamp.com/blog/windsurf-vs-cursor
- Antigravity diff view — https://antigravitylab.net/en/articles/editor/antigravity-diff-view-advanced-guide
- assistant-ui chain-of-thought — https://www.assistant-ui.com/docs/guides/chain-of-thought
- Shape of AI stream-of-thought — https://www.shapeof.ai/patterns/stream-of-thought
- ReTrace (arXiv) — https://arxiv.org/html/2511.11187v1
- Magentic-UI (arXiv) — https://arxiv.org/pdf/2507.22358

### 3 & 4. Org Chart & Inter-Agent Feed
- ClawPort — https://www.clawport.dev/
- ClawPort UI (GitHub) — https://github.com/JohnRiceML/clawport-ui
- Conductor (MS) — https://opensource.microsoft.com/blog/2026/05/14/conductor-deterministic-orchestration-for-multi-agent-ai-workflows/
- LangGraph Studio viz (DeepWiki) — https://deepwiki.com/langchain-ai/langgraph-studio/5.2-graph-visualization
- LangGraph interrupts — https://docs.langchain.com/oss/python/langgraph/interrupts
- CrewAI hierarchical — https://docs.crewai.com/en/learn/hierarchical-process
- CrewAI AMP — https://crewai.com/amp
- CrewAI W&B Weave tracing — https://wandb.ai/onlineinference/genai-research/reports/Tracing-your-CrewAI-application--VmlldzoxMzQ5MDcwNA
- AutoGen Studio user guide — https://microsoft.github.io/autogen/dev//user-guide/autogenstudio-user-guide/index.html
- AutoGen group chat — https://microsoft.github.io/autogen/0.2/docs/Use-Cases/agent_chat/
- Magentic-UI blog — https://www.microsoft.com/en-us/research/blog/magentic-ui-an-experimental-human-centered-web-agent/
- Magentic orchestration (Learn) — https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/magentic
- n8n execution preview — https://docs.n8n.io/courses/level-one/chapter-5/chapter-5.8/
- n8n real-time flow issue — https://github.com/n8n-io/n8n/issues/22385
- OpenAI Agents viz — https://openai.github.io/openai-agents-python/visualization/
- React Flow animating edges — https://reactflow.dev/examples/edges/animating-edges
- Langfuse agent graph — https://langfuse.com/integrations/frameworks/openai-agents
- Agent-to-agent inbox model — https://medium.com/@yingjunwu/agent-to-agent-communication-is-broken-why-an-email-like-inbox-model-works-3ac15cfe7085
- Google A2UI — https://developers.googleblog.com/introducing-a2ui-an-open-project-for-agent-driven-interfaces/
- Microsoft AG-UI — https://techcommunity.microsoft.com/blog/appsonazureblog/ag-ui-the-future-of-agent-driven-user-interfaces/4515769
- Decision provenance — https://tianpan.co/blog/2026-04-19-decision-provenance-agentic-systems
- AgentLens (arXiv) — https://arxiv.org/pdf/2402.08995

### 5. Monitoring Dashboards
- Datadog LLM Observability — https://docs.datadoghq.com/llm_observability/
- Datadog LLM metrics — https://docs.datadoghq.com/llm_observability/monitoring/metrics/
- Datadog LLM cost — https://docs.datadoghq.com/llm_observability/monitoring/cost/
- Datadog dashboards blog — https://www.datadoghq.com/blog/llm-observability-at-datadog-dashboards/
- Datadog dashboards docs — https://docs.datadoghq.com/dashboards/
- Datadog SLO best practices — https://www.datadoghq.com/blog/define-and-manage-slos/
- Grafana RED method — https://grafana.com/blog/the-red-method-how-to-instrument-your-services/
- Grafana visualizations — https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/visualizations/
- RED/USE metrics — https://betterstack.com/community/guides/monitoring/red-use-metrics/
- USE vs RED vs golden — https://faun.pub/use-vs-red-vs-the-four-golden-signals-50655e93fad7
- New Relic AI agents — https://docs.newrelic.com/docs/ai-monitoring/explore-ai-data/view-ai-agents/
- New Relic AI monitoring blog — https://newrelic.com/blog/apm/ai-monitoring
- LangSmith dashboards — https://docs.langchain.com/langsmith/dashboards
- Langfuse custom dashboards — https://langfuse.com/docs/metrics/features/custom-dashboards
- Helicone cost tracking — https://docs.helicone.ai/guides/cookbooks/cost-tracking
- AgentOps (GitHub) — https://github.com/agentops-ai/agentops
- Arize Phoenix — https://arize.com/phoenix/
- groundcover AI agent observability — https://www.groundcover.com/learn/observability/ai-agent-observability
- Braintrust agent observability 2026 — https://www.braintrust.dev/articles/best-ai-agent-observability-tools-2026
- AI Agent KPIs (CFO) — https://agileleadershipdayindia.org/blogs/ai-agent-orchestration-production-deployment-playbook/ai-agent-kpis-agile-team.html
- Agentic AI ROI — https://shawnkanungo.com/blog/agentic-ai-roi-how-to-measure-real-business-value-from-ai-agents-in-2026

### 6 & 7. Incidents/On-Call & Capacity
- PagerDuty incidents — https://support.pagerduty.com/main/docs/incidents
- PagerDuty escalation policies — https://support.pagerduty.com/main/docs/escalation-policies-and-schedules
- PagerDuty my on-call shifts — https://support.pagerduty.com/main/docs/my-on-call-shifts
- PagerDuty intelligent alert grouping — https://support.pagerduty.com/main/docs/intelligent-alert-grouping
- Opsgenie escalations — https://support.atlassian.com/opsgenie/docs/how-do-escalations-work-in-opsgenie/
- Datadog On-Call pages — https://docs.datadoghq.com/incident_response/on-call/pages/
- Datadog incident response — https://www.datadoghq.com/product/incident-response/
- incident.io run-from-Slack — https://incident.io/incident-response-slack
- incident.io AI SRE — https://incident.io/ai-sre
- FireHydrant runbooks — https://firehydrant.com/runbooks/
- FireHydrant incident roles — https://firehydrant.com/docs/managing-incidents/incident-roles
- Rootly AI SRE guide — https://rootly.com/ai-sre-guide
- Augment Code AI SRE — https://www.augmentcode.com/guides/ai-sre-incident-management
- arXiv multi-agent incident response — https://arxiv.org/pdf/2511.15755
- AwaitHuman — https://www.awaithuman.dev/
- AgentRQ — https://agentrq.com/
- MS Learn HITL with AG-UI — https://learn.microsoft.com/en-us/agent-framework/integrations/ag-ui/human-in-the-loop
- Permit.io HITL best practices — https://www.permit.io/blog/human-in-the-loop-for-ai-agents-best-practices-frameworks-use-cases-and-demo
- Celery monitoring — https://docs.celeryq.dev/en/stable/userguide/monitoring.html
- Flower (GitHub) — https://github.com/mher/flower
- Sidekiq scaling — https://sidekiq.org/wiki/Scaling
- Cronitor monitoring Sidekiq — https://cronitor.io/guides/monitoring-sidekiq
- RabbitMQ queues — https://www.rabbitmq.com/docs/queues
- OneUptime RabbitMQ depth — https://oneuptime.com/blog/post/2026-02-06-rabbitmq-queue-depth-consumer-metrics/view
- AWS SQS CloudWatch metrics — https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html
- enix.io k9s — https://enix.io/en/blog/k9s/
- Sysdig CrashLoopBackOff — https://www.sysdig.com/blog/debug-kubernetes-crashloopbackoff
- KEDA — https://keda.sh/
- Dash0 observable KEDA — https://www.dash0.com/blog/observable-event-driven-autoscaling-with-keda-opentelemetry-and-dash0
