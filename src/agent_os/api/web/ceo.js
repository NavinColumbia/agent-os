"use strict";

const state = { token: "", config: null, view: "missions", selectedRun: null, timer: null };
const byId = (id) => document.getElementById(id);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
const shortId = (value) => value ? `${value.slice(0, 11)}…${value.slice(-5)}` : "—";
const label = (value) => String(value || "unknown").replaceAll("_", " ");

function setFlash(message, kind = "") {
  const node = byId("flash");
  node.textContent = message;
  node.className = `flash${kind ? ` ${kind}` : ""}`;
  window.setTimeout(() => node.classList.add("hidden"), 7000);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  if (response.status === 401) {
    disconnect("Your session expired. Connect again to continue.");
    throw new Error("Session expired");
  }
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = payload && typeof payload === "object" ? payload.detail : payload;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return payload;
}

function randomValue(bytes = 32) {
  const data = crypto.getRandomValues(new Uint8Array(bytes));
  return btoa(String.fromCharCode(...data)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function sha256(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return btoa(String.fromCharCode(...new Uint8Array(digest))).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function beginOidc() {
  const verifier = randomValue(48);
  const oauthState = randomValue();
  sessionStorage.setItem("aos.oauth.verifier", verifier);
  sessionStorage.setItem("aos.oauth.state", oauthState);
  const url = new URL(state.config.authorization_url);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", state.config.client_id);
  url.searchParams.set("redirect_uri", state.config.redirect_uri);
  url.searchParams.set("scope", state.config.scope);
  url.searchParams.set("state", oauthState);
  url.searchParams.set("code_challenge", await sha256(verifier));
  url.searchParams.set("code_challenge_method", "S256");
  if (state.config.authorization_audience_parameter) {
    url.searchParams.set(state.config.authorization_audience_parameter, state.config.audience);
  }
  window.location.assign(url.toString());
}

async function finishOidc() {
  const query = new URLSearchParams(window.location.search);
  if (query.get("error")) throw new Error(query.get("error_description") || "Secure sign-in was not completed");
  const code = query.get("code");
  if (!code) return false;
  const expectedState = sessionStorage.getItem("aos.oauth.state");
  const verifier = sessionStorage.getItem("aos.oauth.verifier");
  if (!expectedState || query.get("state") !== expectedState || !verifier) {
    throw new Error("Secure sign-in state did not match");
  }
  const body = new URLSearchParams({
    grant_type: "authorization_code", code, client_id: state.config.client_id,
    redirect_uri: state.config.redirect_uri, code_verifier: verifier,
  });
  const response = await fetch(state.config.token_url, {
    method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body,
  });
  const tokens = await response.json();
  if (!response.ok || !tokens.access_token) throw new Error(tokens.error_description || "Sign-in exchange failed");
  sessionStorage.removeItem("aos.oauth.state");
  sessionStorage.removeItem("aos.oauth.verifier");
  history.replaceState({}, document.title, "/app");
  state.token = tokens.access_token;
  return true;
}

async function connect(token) {
  state.token = token;
  try {
    await api("/v2/company/organization");
    byId("auth-gate").classList.add("hidden");
    byId("workspace").classList.remove("hidden");
    await refreshView();
    const billingNotice = sessionStorage.getItem("aos.billing.notice");
    if (billingNotice) {
      sessionStorage.removeItem("aos.billing.notice");
      selectView("billing");
      setFlash(billingNotice === "success" ? "Subscription received. Entitlements update after Stripe confirms it." : "Billing account refreshed.");
    }
    state.timer = window.setInterval(() => {
      if (!document.hidden && state.token) refreshView(true);
    }, 10000);
  } catch (error) {
    state.token = "";
    byId("auth-status").textContent = error.message;
    byId("auth-status").className = "status-line error";
  }
}

function disconnect(message = "Disconnected. No credential was stored.") {
  state.token = "";
  window.clearInterval(state.timer);
  state.timer = null;
  closeDrawer();
  byId("workspace").classList.add("hidden");
  byId("auth-gate").classList.remove("hidden");
  byId("token-input").value = "";
  byId("auth-status").textContent = message;
  byId("auth-status").className = "status-line";
}

function stat(name, value, tone = "") {
  const node = el("div", "stat");
  node.append(el("small", "", name), el("strong", tone, value));
  return node;
}

async function loadMissions() {
  const [payload, usage] = await Promise.all([
    api("/v2/runs?limit=100"), api("/v2/usage/summary").catch(() => null),
  ]);
  const items = payload.items || [];
  const counts = {
    active: items.filter((item) => item.status === "active").length,
    waiting: items.filter((item) => item.status === "waiting").length,
    attention: items.filter((item) => item.status === "failed").length,
    delivered: items.filter((item) => item.status === "succeeded").length,
  };
  const stats = byId("mission-stats");
  stats.replaceChildren(
    stat("Active", counts.active, "good"), stat("Waiting", counts.waiting, "warn"),
    stat("Needs attention", counts.attention, counts.attention ? "warn" : ""),
    stat("Delivered", counts.delivered),
    stat("Model budget committed", usage ? `$${(usage.committed_cents / 100).toFixed(2)}` : "—"),
  );
  const list = byId("missions-list");
  list.replaceChildren();
  if (!items.length) {
    list.append(el("div", "empty", "No missions yet. Give your company its first objective above."));
    return;
  }
  for (const item of items) {
    const button = el("button", "mission");
    button.type = "button";
    const copy = el("div");
    copy.append(
      el("h4", "", item.title || item.objective_preview || "Untitled mission"),
      el("p", "", item.objective_preview || shortId(item.run_id)),
    );
    button.append(copy, el("span", "phase", label(item.phase)), el("span", `health ${item.status}`, label(item.status)));
    button.addEventListener("click", () => openMission(item));
    list.append(button);
  }
}

async function loadCompany() {
  const org = await api("/v2/company/organization");
  const activeAgents = (org.agents || []).filter((item) => item.status === "active");
  byId("company-summary").replaceChildren(
    stat("AI agents", activeAgents.length, "good"), stat("Human teammates", (org.humans || []).length),
    stat("Teams", (org.teams || []).length), stat("Connected services", (org.services || []).length),
  );
  const content = byId("company-content");
  content.replaceChildren();
  for (const team of org.teams || []) {
    const block = el("section", "team-block");
    block.append(el("h4", "", team.name));
    const people = el("div", "people");
    for (const agent of activeAgents.filter((item) => item.team_id === team.team_id)) {
      const card = el("div", "person");
      card.append(el("strong", "", agent.role), el("small", "", `Reports to ${agent.manager_id} · ${(agent.capabilities || []).join(", ") || "general"}`));
      people.append(card);
    }
    for (const human of (org.humans || []).filter((item) => item.team_id === team.team_id)) {
      const card = el("div", "person");
      card.append(el("strong", "", human.display_name), el("small", "", `Human · ${(human.responsibilities || []).join(", ")}`));
      people.append(card);
    }
    if (!people.children.length) people.append(el("div", "empty", "No active members"));
    block.append(people);
    content.append(block);
  }
}

async function loadInbox() {
  const payload = await api("/v2/notifications?limit=100");
  const items = payload.items || [];
  const count = byId("inbox-count");
  count.textContent = String(items.length);
  count.classList.toggle("hidden", !items.length);
  const content = byId("inbox-content");
  content.replaceChildren();
  if (!items.length) return content.append(el("div", "empty", "Nothing needs your attention."));
  for (const item of items) {
    const node = el("article", "notice");
    node.append(
      el("strong", "", item.title || label(item.kind || item.category || "Company update")),
      el("small", "", item.message || item.body || item.reason || JSON.stringify(item.payload || {})),
    );
    if (item.category === "human_action_required" && item.actionable && item.correlation_id) {
      const actions = el("div", "human-actions");
      const answer = document.createElement("input");
      answer.type = "text"; answer.maxLength = 2000; answer.placeholder = "Optional decision context";
      answer.setAttribute("aria-label", "Decision context");
      const approve = el("button", "", "Approve");
      const decline = el("button", "", "Decline");
      const reply = el("button", "quiet", "Reply");
      approve.type = decline.type = reply.type = "button";
      approve.addEventListener("click", () => resolveHumanRequest(item, {
        approved: true, answer: answer.value.trim() || "Approved in the CEO workspace",
      }, actions));
      decline.addEventListener("click", () => resolveHumanRequest(item, {
        approved: false, answer: answer.value.trim() || "Declined in the CEO workspace",
      }, actions));
      reply.addEventListener("click", () => {
        const value = answer.value.trim();
        if (!value) return setFlash("Enter a response first.", "error");
        resolveHumanRequest(item, {answer: value}, actions);
      });
      actions.append(answer, approve, decline, reply);
      node.append(actions);
    } else if (item.category === "human_action_required") {
      node.append(el("small", "", "Resolved or no longer actionable"));
    }
    content.append(node);
  }
}

async function resolveHumanRequest(item, response, actions) {
  for (const control of actions.querySelectorAll("button,input")) control.disabled = true;
  try {
    const run = await api(`/v2/graph-runs/${encodeURIComponent(item.run_id)}`);
    await api(`/v2/graph-runs/${encodeURIComponent(item.run_id)}/events`, {
      method: "POST",
      body: JSON.stringify({
        event_id: `human-response-${crypto.randomUUID()}`,
        kind: "wait_resumed",
        expected_version: run.version,
        payload: {correlation_id: item.correlation_id, response},
      }),
    });
    setFlash("Your response was recorded and the team can continue.");
    await loadInbox();
  } catch (error) {
    for (const control of actions.querySelectorAll("button,input")) control.disabled = false;
    setFlash(error.message, "error");
  }
}

async function loadPreviews() {
  const payload = await api("/v2/deployments/previews?limit=100");
  const items = payload.items || [];
  const content = byId("previews-content");
  content.replaceChildren();
  if (!items.length) return content.append(el("div", "empty", "Released previews will appear here."));
  for (const item of items) {
    const node = el("article", "release");
    node.append(el("strong", "", item.deployment_id || "Preview"), el("small", "", `Status: ${label(item.status)} · Expires ${item.expires_at || "—"}`));
    if (item.public_url && item.status !== "revoked") {
      const link = el("a", "", "Open preview ↗");
      link.href = item.public_url; link.target = "_blank"; link.rel = "noopener noreferrer";
      node.append(link);
    }
    content.append(node);
  }
}

async function openBillingDestination(path, body) {
  const result = await api(path, {
    method: "POST", headers: { "Idempotency-Key": `billing-${crypto.randomUUID()}` },
    body: body ? JSON.stringify(body) : undefined,
  });
  sessionStorage.setItem("aos.billing.pending", "1");
  window.location.assign(result.url);
}

async function loadBilling() {
  const [account, usage] = await Promise.all([
    api("/v2/billing"), api("/v2/usage/summary"),
  ]);
  byId("billing-summary").replaceChildren(
    stat("Current plan", account.display_name || label(account.plan_id), "good"),
    stat("Subscription", label(account.subscription_status)),
    stat("Monthly model ceiling", `$${(account.monthly_model_budget_cents / 100).toFixed(2)}`),
    stat("Remaining this month", `$${(usage.remaining_cents / 100).toFixed(2)}`),
  );
  const content = byId("billing-content");
  content.replaceChildren();
  const current = el("article", "plan-card current-plan");
  current.append(el("p", "eyebrow", "CURRENT"), el("h4", "", account.display_name || label(account.plan_id)),
    el("p", "muted", `${label(account.subscription_status)} · $${(account.monthly_model_budget_cents / 100).toFixed(2)} monthly model ceiling`));
  if (account.stripe_customer_id) {
    const manage = el("button", "quiet", "Manage payment and invoices ↗");
    manage.type = "button";
    manage.addEventListener("click", () => openBillingDestination("/v2/billing/portal").catch((error) => setFlash(error.message, "error")));
    current.append(manage);
  }
  content.append(current);
  for (const plan of account.available_plans || []) {
    const card = el("article", "plan-card");
    card.append(el("p", "eyebrow", "PLAN"), el("h4", "", plan.display_name),
      el("p", "muted", `$${(plan.monthly_model_budget_cents / 100).toFixed(2)} monthly model ceiling`));
    const upgrade = el("button", "primary", account.plan_id === plan.plan_id ? "Current plan" : `Choose ${plan.display_name}`);
    upgrade.type = "button"; upgrade.disabled = account.plan_id === plan.plan_id;
    upgrade.addEventListener("click", () => openBillingDestination("/v2/billing/checkout", { plan_id: plan.plan_id }).catch((error) => setFlash(error.message, "error")));
    card.append(upgrade); content.append(card);
  }
}

async function refreshView(silent = false) {
  try {
    if (state.view === "missions") await loadMissions();
    if (state.view === "company") await loadCompany();
    if (state.view === "inbox") await loadInbox();
    if (state.view === "previews") await loadPreviews();
    if (state.view === "billing") await loadBilling();
    byId("last-refresh").textContent = `Updated ${new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}`;
    if (state.selectedRun) await loadMissionDetail(state.selectedRun, true);
  } catch (error) {
    if (!silent) setFlash(error.message, "error");
  }
}

function detailSection(title) {
  const section = el("section", "detail-card");
  section.append(el("h4", "", title));
  return section;
}

async function openMission(item) {
  state.selectedRun = item;
  byId("drawer-title").textContent = item.title || "Mission";
  byId("mission-drawer").classList.add("open");
  byId("mission-drawer").setAttribute("aria-hidden", "false");
  await loadMissionDetail(item);
}

function closeDrawer() {
  state.selectedRun = null;
  byId("mission-drawer").classList.remove("open");
  byId("mission-drawer").setAttribute("aria-hidden", "true");
}

async function loadMissionDetail(item, silent = false) {
  const content = byId("drawer-content");
  if (!silent) content.replaceChildren(el("div", "empty", "Reading durable mission state…"));
  try {
    const [run, missionResult, managementResult, company] = await Promise.all([
      api(`/v2/runs/${encodeURIComponent(item.run_id)}`),
      api(`/v2/runs/${encodeURIComponent(item.run_id)}/mission`).catch(() => null),
      api(`/v2/runs/${encodeURIComponent(item.run_id)}/management`).catch(() => null),
      api("/v2/company/organization"),
    ]);
    if (!state.selectedRun || state.selectedRun.run_id !== item.run_id) return;
    content.replaceChildren();
    const overview = detailSection("Current state");
    const grid = el("div", "detail-grid");
    for (const [name, value] of [["Phase", label(run.phase)], ["Status", label(run.status)], ["Revision", run.version]]) {
      const cell = el("div"); cell.append(el("small", "", name), el("strong", "", value)); grid.append(cell);
    }
    overview.append(grid);
    if (managementResult) {
      const ratio = Math.round(Number(managementResult.progress?.materialized_completion_ratio || 0) * 100);
      const track = el("div", "progress-track"); const bar = el("span"); bar.style.width = `${Math.min(100, ratio)}%`; track.append(bar);
      overview.append(track, el("small", "muted", `${ratio}% of currently materialized work complete · ${label(managementResult.health)}`));
    }
    content.append(overview);

    const program = managementResult?.program || missionResult?.program;
    const readiness = managementResult?.readiness;
    if (program) {
      const command = detailSection("Program command");
      const commandGrid = el("div", "detail-grid");
      for (const [name, value] of [
        ["Feasibility", label(program.feasibility?.verdict)],
        ["Readiness", label(readiness?.status || "admitted")],
        ["Program revision", readiness?.revision || program.revision || 1],
      ]) {
        const cell = el("div");
        cell.append(el("small", "", name), el("strong", "", value));
        commandGrid.append(cell);
      }
      command.append(commandGrid);
      if (program.feasibility?.rationale) {
        command.append(el("p", "program-rationale", program.feasibility.rationale));
      }
      const delivery = program.feasibility?.delivery_estimate;
      const cost = program.feasibility?.cost_estimate;
      if (delivery || cost) {
        const estimates = el("div", "work-row");
        if (delivery) estimates.append(el("p", "", `Delivery range: ${delivery.optimistic}–${delivery.pessimistic} ${delivery.unit}; likely ${delivery.likely}.`));
        if (cost) estimates.append(el("p", "", `Cost range: $${(Number(cost.optimistic || 0) / 100).toFixed(2)}–$${(Number(cost.pessimistic || 0) / 100).toFixed(2)}; likely $${(Number(cost.likely || 0) / 100).toFixed(2)}.`));
        command.append(estimates);
      }
      const gaps = [
        ["Questions", readiness?.unresolved_question_ids || []],
        ["Blocked workstreams", readiness?.blocked_workstream_ids || []],
        ["Resources being acquired", readiness?.outstanding_resource_ids || []],
        ["Capabilities being expanded", readiness?.outstanding_capability_ids || []],
      ];
      for (const [name, values] of gaps) {
        if (!values.length) continue;
        const row = el("div", "work-row");
        row.append(el("small", "", name), el("p", "", values.map(label).join(" · ")));
        command.append(row);
      }
      if (readiness?.independent_work_continues) {
        command.append(el("small", "good-note", "Independent work is continuing while human input is pending."));
      }
      content.append(command);
    }

    if (managementResult?.management_signals?.length) {
      const signals = detailSection("Manager signals");
      for (const signal of managementResult.management_signals) {
        const row = el("div", "work-row");
        row.append(el("span", `health ${signal.severity === "critical" ? "failed" : "degraded"}`, label(signal.signal)), el("p", "", signal.reason || signal.recommended_action));
        signals.append(row);
      }
      content.append(signals);
    }
    if (managementResult?.work_items?.length) {
      const work = detailSection("Team execution");
      for (const itemValue of managementResult.work_items) {
        const row = el("div", "work-row");
        row.append(el("span", `health ${itemValue.health}`, label(itemValue.health)), el("p", "", itemValue.objective), el("small", "", `${itemValue.owner_id || "system"} · attempt ${itemValue.attempt}`));
        work.append(row);
      }
      content.append(work);
    }
    if (managementResult?.hiring_requests?.length) {
      const proposals = detailSection("Staffing proposals");
      for (const proposal of managementResult.hiring_requests) {
        const row = el("div", "work-row");
        row.append(el("span", "phase", label(proposal.status)), el("p", "", `${proposal.requested_count || 1} × ${proposal.role}`));
        if (proposal.status === "pending" && (proposal.participant_kind || "agent") === "agent") {
          const actions = el("div", "proposal-actions");
          const team = el("select"); team.setAttribute("aria-label", "Team for approved agent");
          for (const value of company.teams || []) {
            const option = el("option", "", value.name); option.value = value.team_id; team.append(option);
          }
          const manager = el("select"); manager.setAttribute("aria-label", "Manager for approved agent");
          for (const value of (company.agents || []).filter((agent) => agent.status === "active")) {
            const option = el("option", "", value.role); option.value = value.agent_id; manager.append(option);
          }
          const missionManager = Array.from(manager.options).find((option) => option.value === "agent:mission-manager");
          if (missionManager) missionManager.selected = true;
          const approve = el("button", "", "Approve"); const reject = el("button", "", "Reject");
          approve.type = reject.type = "button";
          approve.addEventListener("click", () => decideHiring(item, proposal, true, team.value, manager.value));
          reject.addEventListener("click", () => decideHiring(item, proposal, false, null, manager.value));
          actions.append(team, manager, approve, reject); row.append(actions);
        }
        proposals.append(row);
      }
      content.append(proposals);
    }
    if (missionResult?.deliverables?.length) {
      const deliverables = detailSection("Deliverables");
      for (const result of missionResult.deliverables) {
        const link = el(
          "a", "", result.kind === "static_site" ? "Open production app ↗" : "Open published preview ↗",
        );
        link.href = result.public_url; link.target = "_blank"; link.rel = "noopener noreferrer";
        deliverables.append(link);
      }
      content.append(deliverables);
    }
    if (!["succeeded", "failed", "cancelled"].includes(run.status)) {
      const controls = detailSection("Mission controls");
      const cancel = el("button", "danger", "Cancel mission"); cancel.type = "button";
      cancel.addEventListener("click", () => cancelMission(item, run.version)); controls.append(cancel); content.append(controls);
    }
  } catch (error) {
    if (!silent) content.replaceChildren(el("div", "empty", error.message));
  }
}

async function decideHiring(item, proposal, approved, teamId, managerId) {
  if (!window.confirm(`${approved ? "Approve" : "Reject"} this AI staffing proposal?`)) return;
  try {
    await api(`/v2/runs/${encodeURIComponent(item.run_id)}/management/proposals/${encodeURIComponent(proposal.proposal_id)}/hiring-decision`, {
      method: "POST", body: JSON.stringify({
        approved, reason: approved ? "Approved by the CEO workspace" : "Rejected by the CEO workspace",
        team_id: approved ? teamId : null, manager_id: managerId || "agent:mission-manager",
        tool_grants: [], spending_limit_cents: 0,
      }),
    });
    setFlash(`Staffing proposal ${approved ? "approved" : "rejected"}.`);
    await loadMissionDetail(item);
  } catch (error) { setFlash(error.message, "error"); }
}

async function cancelMission(item, version) {
  if (!window.confirm("Cancel this mission? Healthy in-progress work will receive a durable cancellation signal.")) return;
  try {
    await api(`/v2/runs/${encodeURIComponent(item.run_id)}/cancel`, {
      method: "POST", body: JSON.stringify({ event_id: `ceo-cancel-${crypto.randomUUID()}`, expected_version: version, reason: "Cancelled by CEO" }),
    });
    setFlash("Cancellation accepted."); closeDrawer(); await loadMissions();
  } catch (error) { setFlash(error.message, "error"); }
}

function selectView(name) {
  state.view = name;
  document.querySelectorAll(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.view === name));
  document.querySelectorAll(".view").forEach((node) => node.classList.add("hidden"));
  byId(`${name}-view`).classList.remove("hidden");
  byId("view-title").textContent = { missions: "Missions", company: "Company", inbox: "Inbox", previews: "Releases", billing: "Billing" }[name];
  closeDrawer(); refreshView();
}

async function bootstrap() {
  try {
    state.config = await fetch("/v2/client-config", { cache: "no-store" }).then((response) => response.json());
    if (state.config.billing_mode !== "stripe") {
      const billingNav = document.querySelector('[data-view="billing"]');
      if (billingNav) billingNav.classList.add("hidden");
    }
    const billingReturn = new URLSearchParams(window.location.search).get("billing");
    if (billingReturn) sessionStorage.setItem("aos.billing.notice", billingReturn);
    if (state.config.identity_mode === "oidc") {
      byId("oidc-login").classList.remove("hidden");
      byId("auth-status").textContent = "Secure sign-in uses authorization code + PKCE.";
      if (await finishOidc()) await connect(state.token);
      else if (billingReturn) await beginOidc();
    } else {
      byId("token-form").classList.remove("hidden");
      byId("auth-status").textContent = "Connect to the local/BYOC control plane.";
    }
  } catch (error) {
    byId("auth-status").textContent = error.message;
    byId("auth-status").className = "status-line error";
  }
}

byId("oidc-login").addEventListener("click", beginOidc);
byId("token-form").addEventListener("submit", (event) => { event.preventDefault(); connect(byId("token-input").value.trim()); });
byId("disconnect").addEventListener("click", () => disconnect());
byId("refresh").addEventListener("click", () => refreshView());
byId("drawer-close").addEventListener("click", closeDrawer);
byId("directive").addEventListener("input", (event) => { byId("directive-count").textContent = `${event.target.value.length.toLocaleString()} / 50,000`; });
byId("directive-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter; button.disabled = true;
  try {
    const result = await api("/v2/runs", {
      method: "POST", headers: { "Idempotency-Key": `ceo-${crypto.randomUUID()}` },
      body: JSON.stringify({
        prompt: byId("directive").value.trim(),
        title: byId("directive-title").value.trim() || null,
        budget_limit_cents: Math.round(Number(byId("directive-budget").value || 0) * 100),
      }),
    });
    byId("directive").value = ""; byId("directive-title").value = ""; byId("directive-budget").value = "0"; byId("directive-count").textContent = "0 / 50,000";
    setFlash("Mission accepted. Your team is planning it now."); await loadMissions();
    openMission({ run_id: result.run_id, title: "New mission" });
  } catch (error) { setFlash(error.message, "error"); }
  finally { button.disabled = false; }
});
document.querySelectorAll(".nav-item").forEach((node) => node.addEventListener("click", () => selectView(node.dataset.view)));
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
bootstrap();
