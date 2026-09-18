"use strict";

const state = {
  token: "", organization: "", config: null, view: "missions", selectedRun: null,
  timer: null, session: null, inboxFilter: "open", inboxItems: [], notificationPreferences: null,
  inboxCursor: null, decisionDrafts: {}, browserAlertBaseline: null, drawerReturnFocus: null,
  eventCursor: null, eventAbort: null, eventRefreshTimer: null,
  pushSubscriptionId: null,
  focusedNotificationId: null, focusInboxItemPending: false,
  missionMessageDrafts: {}, humanRequestDrafts: {},
  readiness: null, readinessCheckedAt: 0, missionAdmissionBlocked: true,
  missionSubmission: null,
};
const byId = (id) => document.getElementById(id);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
const shortId = (value) => value ? `${value.slice(0, 11)}…${value.slice(-5)}` : "—";
const label = (value) => String(value || "unknown").replaceAll("_", " ");

function routeFromHash() {
  const raw = window.location.hash.startsWith("#") ? window.location.hash.slice(1) : "";
  const parameters = new URLSearchParams(raw);
  return {
    view: parameters.get("view") || "",
    pushDeliveryId: parameters.get("push") || "",
    runId: parameters.get("run") || "",
  };
}

function workspaceRoute(view = state.view, runId = "") {
  const parameters = new URLSearchParams({view});
  if (runId) parameters.set("run", runId);
  return `#${parameters}`;
}

function setFlash(message, kind = "") {
  const node = byId("flash");
  node.textContent = message;
  node.className = `flash${kind ? ` ${kind}` : ""}`;
  window.setTimeout(() => node.classList.add("hidden"), 7000);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${state.token}`);
  if (state.organization) headers.set("X-Agent-OS-Organization", state.organization);
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
    const message = detail && typeof detail === "object"
      ? (detail.message || detail.code) : detail;
    const error = new Error(message || `Request failed (${response.status})`);
    error.status = response.status;
    if (detail && typeof detail === "object") {
      error.code = detail.code || "";
      error.reason = detail.reason || "";
    }
    throw error;
  }
  return payload;
}

async function downloadMissionArtifact(runId, artifactId) {
  const headers = new Headers({Authorization: `Bearer ${state.token}`});
  if (state.organization) headers.set("X-Agent-OS-Organization", state.organization);
  const path = `/v2/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}/content`;
  const response = await fetch(path, {headers, cache: "no-store"});
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `Evidence download failed (${response.status})`);
  }
  const objectUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = objectUrl; link.download = artifactId; link.hidden = true;
  document.body.append(link); link.click(); link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

function setLiveStatus(message) {
  const node = byId("live-status");
  if (node) node.textContent = message;
}

function stopLiveEvents() {
  if (state.eventAbort) state.eventAbort.abort();
  state.eventAbort = null;
  window.clearTimeout(state.eventRefreshTimer);
  state.eventRefreshTimer = null;
}

function scheduleLiveRefresh() {
  if (state.eventRefreshTimer) return;
  state.eventRefreshTimer = window.setTimeout(async () => {
    state.eventRefreshTimer = null;
    if (!state.token || document.hidden) return;
    await refreshAmbient();
  }, 200);
}

function consumeSseFrame(frame) {
  let eventName = "message";
  let eventId = "";
  const data = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    if (line.startsWith("id:")) eventId = line.slice(3).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (eventId) state.eventCursor = eventId;
  if (!["experience", "cursor", "reset"].includes(eventName)) return;
  if (data.length) {
    try {
      const payload = JSON.parse(data.join("\n"));
      if (payload.cursor) state.eventCursor = payload.cursor;
    } catch (_) {
      return;
    }
  }
  scheduleLiveRefresh();
}

async function startLiveEvents() {
  if (!state.token || document.hidden || state.eventAbort) return;
  const controller = new AbortController();
  state.eventAbort = controller;
  try {
    while (state.token && !document.hidden && !controller.signal.aborted) {
      const query = state.eventCursor
        ? `?cursor=${encodeURIComponent(state.eventCursor)}` : "";
      const headers = new Headers({
        "Accept": "text/event-stream",
        "Authorization": `Bearer ${state.token}`,
      });
      if (state.organization) headers.set("X-Agent-OS-Organization", state.organization);
      const response = await fetch(`/v2/events/stream${query}`, {
        headers, cache: "no-store", signal: controller.signal,
      });
      if (response.status === 401) {
        disconnect("Your session expired. Connect again to continue.");
        return;
      }
      if (!response.ok || !response.body) throw new Error(`Live updates unavailable (${response.status})`);
      setLiveStatus("Live updates connected");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (!controller.signal.aborted) {
        const {value, done} = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
        buffer = buffer.replaceAll("\r\n", "\n");
        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
          consumeSseFrame(buffer.slice(0, boundary));
          buffer = buffer.slice(boundary + 2);
          boundary = buffer.indexOf("\n\n");
        }
        if (done) break;
      }
      if (!controller.signal.aborted) await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
  } catch (error) {
    if (!controller.signal.aborted) {
      setLiveStatus("Polling fallback active");
      window.setTimeout(() => {
        if (state.eventAbort === controller) state.eventAbort = null;
        startLiveEvents();
      }, 3000);
    }
  } finally {
    if (state.eventAbort === controller && controller.signal.aborted) state.eventAbort = null;
  }
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
  state.organization = sessionStorage.getItem("aos.organization") || "";
  state.browserAlertBaseline = null;
  state.eventCursor = null;
  try {
    await loadOrganizations();
    if (can("company.read")) await api("/v2/company/organization");
    byId("auth-gate").classList.add("hidden");
    byId("workspace").classList.remove("hidden");
    const requestedRoute = routeFromHash();
    const roleDefault = personaDefaultView();
    await selectView(
      requestedRoute.view && viewAllowed(requestedRoute.view)
        ? requestedRoute.view : roleDefault,
    );
    if (requestedRoute.pushDeliveryId) await openPushDelivery(requestedRoute.pushDeliveryId);
    else if (requestedRoute.runId) await openMissionDeepLink(requestedRoute.runId);
    const billingNotice = sessionStorage.getItem("aos.billing.notice");
    if (billingNotice) {
      sessionStorage.removeItem("aos.billing.notice");
      selectView("billing");
      setFlash(billingNotice === "success" ? "Subscription received. Entitlements update after Stripe confirms it." : "Billing account refreshed.");
    }
    await refreshInboxBadge();
    reconcileBackgroundPush().catch(() => null);
    startLiveEvents();
    state.timer = window.setInterval(() => {
      if (!document.hidden && state.token) refreshAmbient();
    }, 60000);
  } catch (error) {
    state.token = "";
    byId("auth-status").textContent = error.message;
    byId("auth-status").className = "status-line error";
  }
}

function disconnect(message = "Disconnected. No credential was stored.") {
  stopLiveEvents();
  state.token = "";
  state.organization = "";
  sessionStorage.removeItem("aos.organization");
  window.clearInterval(state.timer);
  state.timer = null;
  state.session = null;
  state.inboxItems = [];
  state.inboxCursor = null;
  state.decisionDrafts = {};
  state.notificationPreferences = null;
  state.focusedNotificationId = null;
  state.focusInboxItemPending = false;
  state.browserAlertBaseline = null;
  state.eventCursor = null;
  state.pushSubscriptionId = null;
  state.missionMessageDrafts = {};
  state.humanRequestDrafts = {};
  state.readiness = null;
  state.readinessCheckedAt = 0;
  state.missionAdmissionBlocked = true;
  state.missionSubmission = null;
  renderExecutionReadiness(null);
  closeDrawer({updateRoute: false});
  history.replaceState({}, document.title, "/app");
  byId("workspace").classList.add("hidden");
  byId("auth-gate").classList.remove("hidden");
  byId("token-input").value = "";
  byId("auth-status").textContent = message;
  byId("auth-status").className = "status-line";
}

async function loadOrganizations(preferred = state.organization) {
  const payload = await api("/v2/organizations");
  const items = payload.items || [];
  const available = new Set(items.map((item) => item.organization_id));
  state.organization = available.has(preferred) ? preferred : payload.default_organization_id;
  sessionStorage.setItem("aos.organization", state.organization);
  const picker = byId("organization-select");
  picker.replaceChildren();
  for (const item of items) {
    const option = el("option", "", `${item.organization_id} · ${(item.roles || []).join(", ")}`);
    option.value = item.organization_id;
    option.selected = item.organization_id === state.organization;
    picker.append(option);
  }
  state.session = await api("/v2/me");
  applyRoleExperience();
  return payload;
}

function can(capability) {
  return Boolean(state.session && (state.session.capabilities || []).includes(capability));
}

function personaDefaultView() {
  const persona = state.session?.persona;
  if (["operator", "reviewer"].includes(persona)) return "inbox";
  if (persona === "builder") return "work";
  if (persona === "administrator") return "company";
  if (persona === "billing" && state.config?.billing_mode === "stripe") return "billing";
  return "missions";
}

function viewAllowed(name) {
  if (!["missions", "work", "company", "inbox", "integrations", "previews", "billing"].includes(name)) return false;
  if (name === "work") return can("work.read");
  if (name === "company") return can("company.read");
  if (name === "integrations") return can("integration.manage");
  if (name === "previews") return can("release.read");
  if (name === "billing") return can("billing.manage") && state.config?.billing_mode === "stripe";
  return true;
}

function applyRoleExperience() {
  const persona = state.session?.persona || "viewer";
  const labels = {
    executive: "Executive", operator: "Operations", builder: "Builder",
    reviewer: "Reviewer", administrator: "Administrator", manager: "Manager",
    billing: "Billing", client: "Client", viewer: "Viewer",
  };
  byId("workspace-role-label").textContent = `${labels[persona] || "Member"} workspace`;
  byId("identity-avatar").textContent = {
    executive: "CEO", operator: "OPS", builder: "BUILD", reviewer: "REVIEW",
    administrator: "ADMIN", manager: "MGR", billing: "BILL", client: "CLIENT",
    viewer: "VIEW",
  }[persona] || "USER";
  document.querySelectorAll(".requires-mission-create").forEach((node) => node.classList.toggle("hidden", !can("mission.create")));
  document.querySelectorAll(".requires-work-read").forEach((node) => node.classList.toggle("hidden", !can("work.read")));
  document.querySelectorAll(".requires-company-read").forEach((node) => node.classList.toggle("hidden", !can("company.read")));
  document.querySelectorAll(".requires-release-read").forEach((node) => node.classList.toggle("hidden", !can("release.read")));
  document.querySelectorAll(".requires-integration-manage").forEach((node) => node.classList.toggle("hidden", !can("integration.manage")));
  const billingAvailable = state.config?.billing_mode === "stripe";
  document.querySelectorAll(".requires-billing-manage").forEach((node) => node.classList.toggle("hidden", !can("billing.manage") || !billingAvailable));
}

function userIsEditing() {
  const active = document.activeElement;
  return Boolean(active && active.matches("input, textarea, select, [contenteditable='true']"));
}

function stat(name, value, tone = "") {
  const node = el("div", "stat");
  node.append(el("small", "", name), el("strong", tone, value));
  return node;
}

async function loadMissions() {
  const [payload, usage, readiness] = await Promise.all([
    api("/v2/runs?limit=100"), api("/v2/usage/summary").catch(() => null),
    api("/v2/readiness").catch(() => null),
  ]);
  state.readiness = readiness;
  state.readinessCheckedAt = Date.now();
  renderExecutionReadiness(readiness);
  renderReadiness(readiness);
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
    list.append(el(
      "div", "empty",
      can("mission.create")
        ? "No missions yet. Give your company its first objective above."
        : "No missions are assigned to you yet.",
    ));
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

async function loadMyWork() {
  const content = byId("work-content");
  try {
    const payload = await api("/v2/me/work?limit=100");
    const items = payload.items || [];
    content.replaceChildren();
    if (!items.length) {
      content.append(el("div", "empty", "No open work is assigned to you. Assigned mission access remains available under Missions."));
      return;
    }
    for (const item of items) {
      const work = item.work || {};
      const card = el("article", "panel notice-card");
      card.append(
        el("p", "eyebrow", `${label(item.duty)} · ${label(item.status)}`),
        el("h4", "", work.objective || `Work details unavailable · ${shortId(item.work_id)}`),
        el("p", "muted", item.mission_title || item.mission_objective || shortId(item.mission_id)),
      );
      if (item.projection_status !== "current") {
        const explanations = {
          superseded: "This work item is no longer in the current mission plan. A manager must close or replace the assignment.",
          work_revision_changed: "The work contract changed after assignment. Do not act until a manager reconfirms it.",
          temporarily_unavailable: "Current work state is temporarily unavailable. Your assignment remains durable; retry shortly.",
          projection_conflict: "The work view is reconciling concurrent changes. Retry before acting.",
          not_materialized: "The mission has not materialized this work yet. Your assignment remains pending.",
        };
        card.append(el("p", "projection-warning", explanations[item.projection_status] || "Current work details are unavailable."));
      }
      const facts = el("div", "detail-grid");
      for (const [name, value] of [
        ["Responsibility", label(item.status)],
        ["Work status", label(work.status || item.projection_status)],
        ["Health", label(work.health || "unavailable")],
        ["AI delegate", label(work.delegate_id || work.owner_id || "not assigned")],
        ["Evidence", `${(work.evidence_ids || []).length} retained item(s)`],
        ["Waiting for", label(work.wait_reason || "nothing")],
      ]) {
        const fact = el("div"); fact.append(el("small", "", name), el("strong", "", value)); facts.append(fact);
      }
      const actions = el("div", "proposal-actions");
      if (item.status === "pending" && item.projection_status === "current") {
        const respond = async (action, reason = "") => {
          await api(`/v2/runs/${encodeURIComponent(item.mission_id)}/work/${encodeURIComponent(item.work_id)}/assignments/${encodeURIComponent(item.duty)}/response`, {
            method: "POST",
            headers: {"Idempotency-Key": `work-response-${crypto.randomUUID()}`},
            body: JSON.stringify({action, expected_version: item.version, reason}),
          });
          setFlash(action === "accept" ? "Responsibility accepted." : "Responsibility declined and returned to management.");
          await loadMyWork();
        };
        const accept = el("button", "primary", `Accept ${label(item.duty)} responsibility`); accept.type = "button";
        accept.addEventListener("click", async () => {
          accept.disabled = true;
          try { await respond("accept"); } catch (error) { setFlash(error.message, "error"); }
          finally { accept.disabled = false; }
        });
        const decline = el("button", "danger", `Decline ${label(item.duty)} responsibility`); decline.type = "button";
        decline.addEventListener("click", async () => {
          const reason = window.prompt("Why can you not take this responsibility? This returns it to mission management.", "");
          if (reason == null || !reason.trim()) return;
          decline.disabled = true;
          try { await respond("decline", reason.trim()); } catch (error) { setFlash(error.message, "error"); }
          finally { decline.disabled = false; }
        });
        actions.append(accept, decline);
      }
      const open = el("button", "quiet", `Open mission: ${item.mission_title || shortId(item.mission_id)}`); open.type = "button";
      open.addEventListener("click", async () => {
        await selectView("missions");
        await openMission({
          run_id: item.mission_id,
          title: item.mission_title || "Assigned mission",
          objective_preview: item.mission_objective || "",
        });
      });
      actions.append(open);
      if (item.projection_retryable) {
        const retry = el("button", "quiet", "Retry work status"); retry.type = "button";
        retry.addEventListener("click", () => loadMyWork()); actions.append(retry);
      }
      card.append(facts, actions); content.append(card);
    }
  } catch (error) {
    content.replaceChildren();
    const failed = el("div", "empty", `Assigned work could not be loaded: ${error.message}`);
    const retry = el("button", "quiet", "Retry assigned work"); retry.type = "button";
    retry.addEventListener("click", () => loadMyWork());
    content.append(failed, retry);
    throw error;
  }
}

function renderReadiness(readiness) {
  const panel = byId("readiness-panel");
  if (!readiness?.show_onboarding) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  const blocked = readiness.overall === "blocked";
  const verificationPending = readiness.overall === "verification_pending";
  const status = byId("readiness-status");
  status.className = `health ${blocked ? "failed" : (verificationPending ? "degraded" : "healthy")}`;
  status.textContent = blocked
    ? "Setup blocked" : (verificationPending ? "Verification pending" : "Ready to start");
  byId("readiness-summary").textContent = blocked
    ? "Resolve the required items below before asking the company to execute."
    : (verificationPending
      ? "The mission is accepted, but a successful metered provider turn has not yet proved model access."
      : "Required local controls are ready. Worker-only provider access will be verified by the first metered agent turn.");
  const content = byId("readiness-steps");
  content.replaceChildren();
  for (const step of readiness.steps || []) {
    const row = el("article", "readiness-step");
    const copy = el("div");
    copy.append(
      el("strong", "", step.title),
      el("small", "", step.detail),
    );
    const badge = el(
      "span",
      `health ${step.status === "blocked" ? "failed" : (step.status === "complete" ? "healthy" : "degraded")}`,
      label(step.status),
    );
    row.append(copy, badge);
    if (step.action_view && (step.status !== "complete" || step.id === "model_runtime")) {
      const action = el("button", "quiet", step.id === "first_mission" ? "Describe mission" : "Review");
      action.type = "button";
      action.addEventListener("click", () => {
        selectView(step.action_view);
        if (step.id === "first_mission") byId("directive").focus();
      });
      row.append(action);
    }
    content.append(row);
  }
}

function renderExecutionReadiness(readiness) {
  const banner = byId("execution-banner");
  const submit = byId("directive-submit");
  const execution = (readiness?.steps || []).find((step) => step.id === "execution_plane");
  const unavailable = !readiness;
  const blocked = unavailable || execution?.status === "blocked";
  state.missionAdmissionBlocked = blocked;
  banner.classList.toggle("hidden", !blocked);
  if (blocked) {
    byId("execution-banner-title").textContent = unavailable
      ? "Cannot verify execution right now" : "New missions are temporarily paused";
    byId("execution-banner-detail").textContent = unavailable
      ? "The workspace could not verify execution availability. Existing missions and drafts remain accessible."
      : execution.detail;
  }
  submit.disabled = blocked || submit.dataset.submitting === "true";
  submit.querySelector("span").textContent = blocked ? "Execution paused" : "Start mission";
}

async function refreshExecutionReadiness({force = false, onboarding = false} = {}) {
  if (!force && Date.now() - state.readinessCheckedAt < 15000) return state.readiness;
  let readiness = null;
  try { readiness = await api("/v2/readiness"); } catch (_) { readiness = null; }
  state.readiness = readiness;
  state.readinessCheckedAt = Date.now();
  renderExecutionReadiness(readiness);
  if (onboarding) renderReadiness(readiness);
  return readiness;
}

async function loadCompany() {
  const [org, memberships, modelSetting] = await Promise.all([
    api("/v2/company/organization"), api("/v2/memberships").catch(() => null),
    api("/v2/settings/model").catch(() => null),
  ]);
  const activeAgents = (org.agents || []).filter((item) => item.status === "active");
  byId("company-summary").replaceChildren(
    stat("AI agents", activeAgents.length, "good"), stat("Human teammates", (org.humans || []).length),
    stat("Teams", (org.teams || []).length), stat("Connected services", (org.services || []).length),
  );
  const content = byId("company-content");
  content.replaceChildren();
  content.append(accessPanel(memberships));
  if (modelSetting) content.append(modelPanel(modelSetting));
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

function modelPanel(payload) {
  const current = payload.setting || {};
  const block = el("section", "team-block access-panel");
  block.append(
    el("h4", "", "AI model policy"),
    el("small", "muted", current.model_name
      ? `Current: ${current.provider}:${current.model_name} · ${current.credential_source} credentials`
      : "Using the platform default model."),
  );
  if (!can("model.manage")) {
    block.append(el("small", "muted", "Your role can inspect this policy but cannot change it."));
    return block;
  }
  const form = el("div", "model-form");
  const provider = document.createElement("select");
  provider.setAttribute("aria-label", "Model provider");
  for (const value of ["openai", "anthropic", "google"]) {
    const option = el("option", "", value); option.value = value;
    option.selected = current.provider === value; provider.append(option);
  }
  const name = document.createElement("input");
  name.maxLength = 256; name.placeholder = "Model name, for example gpt-5";
  name.value = current.model_name || ""; name.setAttribute("aria-label", "Model name");
  const credential = document.createElement("input");
  credential.maxLength = 128; credential.placeholder = "Credential reference (optional BYOK)";
  credential.value = current.credential_ref || "";
  credential.setAttribute("aria-label", "Credential reference");
  const save = el("button", "primary", "Save model policy"); save.type = "button";
  save.addEventListener("click", async () => {
    if (!name.value.trim()) return setFlash("Enter a model name.", "error");
    save.disabled = true;
    try {
      await api("/v2/settings/model", {
        method: "PUT", headers: {"Idempotency-Key": `model-${crypto.randomUUID()}`},
        body: JSON.stringify({
          provider: provider.value, model_name: name.value.trim(),
          credential_ref: credential.value.trim() || null,
        }),
      });
      setFlash("Model policy saved. New agent turns use it immediately.");
      await loadCompany();
    } catch (error) { setFlash(error.message, "error"); }
    finally { save.disabled = false; }
  });
  form.append(provider, name, credential, save); block.append(form);
  return block;
}

function accessPanel(memberships) {
  const block = el("section", "team-block access-panel");
  block.append(el("h4", "", "Company access"));
  const join = el("div", "access-form");
  const token = document.createElement("input");
  token.type = "password"; token.maxLength = 2000; token.placeholder = "Paste an invitation token";
  token.setAttribute("aria-label", "Invitation token");
  const claim = el("button", "quiet", "Join organization"); claim.type = "button";
  claim.addEventListener("click", async () => {
    if (!token.value.trim()) return setFlash("Paste an invitation token first.", "error");
    claim.disabled = true;
    try {
      const result = await api("/v2/invitations/claim", {
        method: "POST", body: JSON.stringify({token: token.value.trim()}),
      });
      token.value = "";
      await loadOrganizations(result.organization_id);
      setFlash("Organization access activated.");
      await refreshView();
    } catch (error) { setFlash(error.message, "error"); }
    finally { claim.disabled = false; }
  });
  join.append(token, claim); block.append(join);
  if (!memberships) {
    block.append(el("small", "muted", "Your current role can use the company but cannot manage access."));
    return block;
  }
  if (can("membership.manage")) {
    const invite = el("div", "access-form");
    const role = document.createElement("select");
    const invitationRoles = [
      "viewer", "client", "builder", "reviewer", "billing", "manager", "operator",
    ];
    if (can("ownership.manage")) invitationRoles.push("admin", "owner");
    for (const value of invitationRoles) {
      const option = el("option", "", value); option.value = value; role.append(option);
    }
    role.setAttribute("aria-label", "Invitation role");
    const create = el("button", "primary", "Create invitation"); create.type = "button";
    create.addEventListener("click", async () => {
      create.disabled = true;
      try {
        const result = await api("/v2/invitations", {
          method: "POST", headers: {"Idempotency-Key": `invite-${crypto.randomUUID()}`},
          body: JSON.stringify({roles: [role.value], expires_in_seconds: 86400}),
        });
        const output = document.createElement("textarea");
        output.readOnly = true; output.rows = 3; output.value = result.claim_token;
        output.setAttribute("aria-label", "New invitation token");
        block.append(el("small", "muted", "Share this single-use token securely. It expires in 24 hours."), output);
        output.select();
        setFlash("Invitation created.");
      } catch (error) { setFlash(error.message, "error"); }
      finally { create.disabled = false; }
    });
    invite.append(role, create); block.append(invite);
  } else {
    block.append(el("small", "muted", "Access inventory is read-only for your role."));
  }
  const people = el("div", "people");
  for (const member of memberships.items || []) {
    const card = el("div", "person");
    card.append(
      el("strong", "", member.subject_id),
      el("small", "", `${(member.roles || []).join(", ")} · ${member.active ? "active" : "revoked"}`),
    );
    const protectedMember = (member.roles || []).some((value) => ["owner", "admin"].includes(value));
    if (
      member.active
      && can("membership.manage")
      && (!protectedMember || can("ownership.manage"))
    ) {
      const revoke = el("button", "danger", "Revoke"); revoke.type = "button";
      revoke.addEventListener("click", async () => {
        if (!window.confirm(`Revoke ${member.subject_id}'s access?`)) return;
        try {
          await api(`/v2/memberships/${encodeURIComponent(member.subject_id)}`, {
            method: "DELETE", headers: {"Idempotency-Key": `revoke-${crypto.randomUUID()}`},
            body: JSON.stringify({reason: "Revoked in the Agent OS workspace"}),
          });
          setFlash("Membership revoked."); await loadCompany();
        } catch (error) { setFlash(error.message, "error"); }
      });
      card.append(revoke);
    }
    people.append(card);
  }
  if (!(memberships.items || []).length) people.append(el("div", "empty", "No invited members yet."));
  block.append(people);
  return block;
}

async function loadInbox({ append = false, refresh = false } = {}) {
  const priorItems = state.inboxItems;
  const priorCursor = state.inboxCursor;
  const preserveHistory = refresh && priorItems.length > 100;
  const query = new URLSearchParams({limit: "100"});
  if (append && state.inboxCursor) query.set("cursor", state.inboxCursor);
  const payload = await api(`/v2/notifications?${query}`);
  if (append || preserveHistory) {
    const source = preserveHistory ? [...(payload.items || []), ...priorItems] : priorItems;
    const byNotification = new Map(state.inboxItems.map((item) => [item.notification_id, item]));
    if (preserveHistory) byNotification.clear();
    for (const item of source) {
      if (!byNotification.has(item.notification_id)) byNotification.set(item.notification_id, item);
    }
    if (!preserveHistory) {
      for (const item of payload.items || []) byNotification.set(item.notification_id, item);
    }
    state.inboxItems = [...byNotification.values()];
  } else {
    state.inboxItems = payload.items || [];
  }
  state.inboxCursor = preserveHistory ? priorCursor : (payload.next_cursor || null);
  state.notificationPreferences = payload.preferences || state.notificationPreferences;
  updateInboxBadge(state.inboxItems);
  hydrateInboxPreferences(state.notificationPreferences);
  renderInbox();
  deliverBrowserAlerts(state.inboxItems);
}

function updateInboxBadge(items) {
  const unread = items.filter((item) => (
    (item.user_state?.status || "unread") === "unread"
    && item.presentation?.disposition === "interrupt"
  )).length;
  const count = byId("inbox-count");
  count.textContent = String(unread);
  count.classList.toggle("hidden", !unread);
  count.setAttribute("aria-label", `${unread} unread attention items`);
}

async function refreshInboxBadge() {
  try {
    const payload = await api("/v2/notifications?limit=100");
    state.notificationPreferences = payload.preferences || state.notificationPreferences;
    updateInboxBadge(payload.items || []);
    deliverBrowserAlerts(payload.items || []);
  } catch (error) {
    if (state.view === "inbox") setFlash(`Inbox refresh failed: ${error.message}`, "error");
  }
}

function inboxItemVisible(item) {
  if (item.notification_id === state.focusedNotificationId) return true;
  const itemState = item.user_state?.status || "unread";
  if (state.inboxFilter === "all") return true;
  if (["dismissed", "snoozed", "resolved"].includes(itemState)) return false;
  if (state.inboxFilter === "open") {
    return Boolean(item.actionable || item.presentation?.disposition === "interrupt");
  }
  return !item.actionable && item.presentation?.disposition !== "interrupt";
}

function renderInbox() {
  const content = byId("inbox-content");
  content.replaceChildren();
  const items = state.inboxItems.filter(inboxItemVisible);
  if (!items.length) content.append(el("div", "empty", "Nothing needs your attention."));
  for (const item of items) {
    const itemState = item.user_state?.status || "unread";
    const attention = item.presentation?.level || "active";
    const node = el("article", `notice ${itemState} ${attention}`);
    node.dataset.notificationId = item.notification_id;
    if (item.notification_id === state.focusedNotificationId) {
      node.classList.add("focused");
      node.tabIndex = -1;
    }
    const heading = el("div", "notice-heading");
    const copy = el("div");
    copy.append(
      el("strong", "", item.subject || item.title || label(item.kind || item.category || "Company update")),
      el("small", "", item.body || item.message || item.reason || JSON.stringify(item.payload || {})),
    );
    const badge = el("span", `attention-badge ${attention}`, label(attention));
    heading.append(copy, badge);
    const timestamp = item.created_at ? new Date(item.created_at).toLocaleString() : "Time unavailable";
    const context = el("div", "notice-context");
    context.append(
      el("span", "", label(item.category)), el("span", "", timestamp),
      el("span", "", `Mission ${shortId(item.run_id)}`),
    );
    node.append(
      heading,
      context,
      el("p", "notice-why", `Why you are seeing this: ${item.presentation?.rationale || "It is part of the mission record."}`),
    );
    const brief = item.decision_context;
    if (item.category === "human_action_required" && brief) {
      const decision = el("section", "decision-brief");
      decision.append(el("h4", "", brief.request));
      const facts = el("div", "decision-facts");
      facts.append(
        el("span", "", `Requested by ${label(brief.requesting_role || "mission team")}`),
        el("span", "", `Reversibility: ${label(brief.reversibility || "unknown")}`),
      );
      if (brief.deadline_at) {
        facts.append(el("span", "", `Due ${new Date(brief.deadline_at).toLocaleString()}`));
      }
      if (Number.isFinite(brief.estimated_cost_cents)) {
        facts.append(el("span", "", `Estimated cost $${(brief.estimated_cost_cents / 100).toFixed(2)}`));
      }
      if (brief.evidence_ids?.length) {
        facts.append(el("span", "", `${brief.evidence_ids.length} supporting evidence item(s)`));
      }
      decision.append(facts);
      if (brief.recommendation) {
        decision.append(el("p", "decision-recommendation", `Recommendation: ${brief.recommendation}`));
      }
      for (const [title, values] of [
        ["Alternatives", brief.alternatives], ["Consequences", brief.consequences],
      ]) {
        if (!values?.length) continue;
        const group = el("div", "decision-list");
        group.append(el("strong", "", title));
        const list = document.createElement("ul");
        for (const value of values) list.append(el("li", "", value));
        group.append(list); decision.append(group);
      }
      decision.append(el("p", "decision-default", `Safe default: ${brief.safe_default}`));
      node.append(decision);
    }
    if (item.decision_context_error) {
      const contextError = el("p", "projection-warning", item.decision_context_error);
      contextError.setAttribute("role", "alert");
      node.append(contextError);
    }
    const humanRequest = item.human_request;
    if (
      item.category === "human_action_required"
      && item.actionable
      && humanRequest?.request_kind === "advisory"
    ) {
      const actions = el("div", "human-actions");
      const answer = document.createElement("input");
      answer.type = "text"; answer.maxLength = 2000;
      answer.placeholder = "Reply to this request";
      answer.setAttribute("aria-label", "Human request response");
      answer.value = state.decisionDrafts[humanRequest.request_id] || "";
      answer.addEventListener("input", () => {
        state.decisionDrafts[humanRequest.request_id] = answer.value;
      });
      const reply = el("button", "quiet", "Send response"); reply.type = "button";
      reply.addEventListener("click", () => {
        const value = answer.value.trim();
        if (!value) return setFlash("Enter a response first.", "error");
        answerAdvisoryRequest(item, {answer: value}, actions);
      });
      actions.append(answer, reply); node.append(actions);
    } else if (item.category === "human_action_required" && item.actionable && item.correlation_id) {
      const actions = el("div", "human-actions");
      const answer = document.createElement("input");
      answer.type = "text"; answer.maxLength = 2000; answer.placeholder = "Optional decision context";
      answer.setAttribute("aria-label", "Decision context");
      answer.value = state.decisionDrafts[item.notification_id] || "";
      answer.addEventListener("input", () => { state.decisionDrafts[item.notification_id] = answer.value; });
      const allowed = new Set(brief?.allowed_actions || ["approve", "decline", "respond"]);
      const controls = [];
      if (allowed.has("approve")) {
        const approve = el("button", "", "Approve"); approve.type = "button";
        approve.addEventListener("click", () => resolveHumanRequest(item, {
          action: "approve", approved: true,
          answer: answer.value.trim() || "Approved in the Agent OS workspace",
        }, actions));
        controls.push(approve);
      }
      if (allowed.has("decline")) {
        const decline = el("button", "", "Decline"); decline.type = "button";
        decline.addEventListener("click", () => resolveHumanRequest(item, {
          action: "decline", approved: false,
          answer: answer.value.trim() || "Declined in the Agent OS workspace",
        }, actions));
        controls.push(decline);
      }
      if (allowed.has("request_changes")) {
        const changes = el("button", "quiet", "Request changes"); changes.type = "button";
        changes.addEventListener("click", () => {
          const value = answer.value.trim();
          if (!value) return setFlash("Describe the required changes first.", "error");
          resolveHumanRequest(item, {
            action: "request_changes", approved: false, answer: value,
          }, actions);
        });
        controls.push(changes);
      }
      if (allowed.has("respond")) {
        const reply = el("button", "quiet", "Send response"); reply.type = "button";
        reply.addEventListener("click", () => {
          const value = answer.value.trim();
          if (!value) return setFlash("Enter a response first.", "error");
          resolveHumanRequest(item, {action: "respond", answer: value}, actions);
        });
        controls.push(reply);
      }
      actions.append(answer, ...controls);
      node.append(actions);
    } else if (item.category === "human_action_required" && !item.decision_context_error) {
      const decisionStatus = item.decision_response?.status;
      const requestStatus = humanRequest?.status;
      const decisionMessage = decisionStatus === "pending" || decisionStatus === "executing"
        ? "Your response is durably queued and will resume this work."
        : (decisionStatus === "failed"
          ? "The response could not be applied. Your intent remains recorded and can be retried safely."
          : (requestStatus === "open" && humanRequest?.recipient_id
            ? `This request is addressed to ${label(humanRequest.recipient_id)}; you can monitor it but cannot answer for them.`
            : (requestStatus === "answered"
            ? "This request was answered."
            : (requestStatus === "cancelled" || requestStatus === "superseded"
              ? "This request is no longer active."
              : "Resolved or no longer actionable"))));
      node.append(el("small", "", decisionMessage));
      if (decisionStatus === "failed" && can("decision.redrive")) {
        const recover = el("button", "quiet", "Retry recorded response");
        recover.type = "button";
        recover.addEventListener("click", () => redriveHumanRequest(item, recover));
        node.append(recover);
      }
    }
    const utility = el("div", "notice-utility");
    if (item.run_id) {
      const open = el("button", "quiet", "Open mission"); open.type = "button";
      open.addEventListener("click", () => openMission({ run_id: item.run_id, title: item.subject || "Mission" }));
      utility.append(open);
    }
    const mark = el("button", "quiet", itemState === "read" ? "Mark unread" : "Mark read"); mark.type = "button";
    mark.addEventListener("click", () => updateNotificationState(item, itemState === "read" ? "unread" : "read", null, mark));
    const snooze = el("button", "quiet", "Snooze 1 hour"); snooze.type = "button";
    snooze.addEventListener("click", () => updateNotificationState(
      item, "snoozed", new Date(Date.now() + 60 * 60 * 1000).toISOString(), snooze,
    ));
    const dismiss = el("button", "quiet", "Dismiss"); dismiss.type = "button";
    dismiss.addEventListener("click", () => updateNotificationState(item, "dismissed", null, dismiss));
    utility.append(mark, snooze, dismiss); node.append(utility);
    content.append(node);
  }
  if (state.inboxCursor) {
    const older = el("button", "quiet inbox-load-older", "Load older updates");
    older.type = "button";
    older.addEventListener("click", async () => {
      older.disabled = true;
      try { await loadInbox({append: true}); }
      catch (error) { older.disabled = false; setFlash(error.message, "error"); }
    });
    content.append(older);
  }
  if (state.focusInboxItemPending) {
    const target = [...content.querySelectorAll(".notice")].find(
      (node) => node.dataset.notificationId === state.focusedNotificationId,
    );
    if (target) {
      state.focusInboxItemPending = false;
      window.requestAnimationFrame(() => {
        target.scrollIntoView({behavior: "smooth", block: "center"});
        target.focus({preventScroll: true});
      });
    }
  }
}

function focusInboxItem(item) {
  state.inboxItems = [
    item,
    ...state.inboxItems.filter((entry) => entry.notification_id !== item.notification_id),
  ];
  state.focusedNotificationId = item.notification_id;
  state.focusInboxItemPending = true;
  updateInboxBadge(state.inboxItems);
  renderInbox();
}

async function openPushDelivery(deliveryId) {
  history.replaceState({}, document.title, "#view=inbox");
  if (!/^push-delivery-[0-9a-f]{64}$/.test(deliveryId)) {
    setFlash("This attention link is invalid.", "error");
    if (state.view !== "inbox") await selectView("inbox");
    return;
  }
  try {
    const receipt = await api(`/v2/me/push-deliveries/${encodeURIComponent(deliveryId)}`);
    const item = await api(`/v2/notifications/${encodeURIComponent(receipt.notification_id)}`);
    if (state.view !== "inbox") await selectView("inbox");
    focusInboxItem(item);
    setFlash("Opened the attention item from this device notification.");
  } catch (_) {
    if (state.view !== "inbox") await selectView("inbox");
    setFlash("This attention item is unavailable or no longer authorized.", "error");
  }
}

async function updateNotificationState(item, status, snoozedUntil, control) {
  control.disabled = true;
  try {
    await api(`/v2/notifications/${encodeURIComponent(item.notification_id)}/state`, {
      method: "PUT", headers: {"Idempotency-Key": `notice-state-${crypto.randomUUID()}`},
      body: JSON.stringify({status, snoozed_until: snoozedUntil}),
    });
    await loadInbox({refresh: true});
  } catch (error) {
    control.disabled = false; setFlash(error.message, "error");
  }
}

async function resolveHumanRequest(item, response, actions) {
  for (const control of actions.querySelectorAll("button,input")) control.disabled = true;
  try {
    const accepted = await api(`/v2/decisions/${encodeURIComponent(item.notification_id)}/responses`, {
      method: "POST", headers: {"Idempotency-Key": `decision-${crypto.randomUUID()}`},
      body: JSON.stringify({response}),
    });
    setFlash(accepted.status === "applied"
      ? "Your response was applied and the team can continue."
      : "Your response is durably queued. The team will resume from the exact decision point.");
    delete state.decisionDrafts[item.notification_id];
    await loadInbox({refresh: true});
  } catch (error) {
    for (const control of actions.querySelectorAll("button,input")) control.disabled = false;
    setFlash(error.message, "error");
  }
}

async function answerAdvisoryRequest(item, response, actions) {
  for (const control of actions.querySelectorAll("button,input")) control.disabled = true;
  try {
    await api(`/v2/human-requests/${encodeURIComponent(item.human_request.request_id)}/responses`, {
      method: "POST", headers: {"Idempotency-Key": `human-response-${crypto.randomUUID()}`},
      body: JSON.stringify({response}),
    });
    delete state.decisionDrafts[item.human_request.request_id];
    setFlash("Your response was recorded for the requester.");
    await loadInbox({refresh: true});
  } catch (error) {
    for (const control of actions.querySelectorAll("button,input")) control.disabled = false;
    setFlash(error.message, "error");
  }
}

async function redriveHumanRequest(item, control) {
  control.disabled = true;
  try {
    await api(`/v2/decisions/${encodeURIComponent(item.notification_id)}/redrive`, {
      method: "POST", headers: {"Idempotency-Key": `decision-redrive-${crypto.randomUUID()}`},
    });
    setFlash("The recorded response is queued for another bounded recovery cycle.");
    await loadInbox({refresh: true});
  } catch (error) {
    control.disabled = false;
    setFlash(error.message, "error");
  }
}

function hydrateInboxPreferences(preferences) {
  if (!preferences) return;
  byId("inbox-mode").value = preferences.mode || "balanced";
  byId("browser-notifications").checked = Boolean(preferences.browser_notifications);
  byId("quiet-start").value = preferences.quiet_hours_start || "";
  byId("quiet-end").value = preferences.quiet_hours_end || "";
  byId("notification-timezone").value = preferences.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  byId("digest-interval").value = String(preferences.digest_interval_minutes || 60);
}

function setPushStatus(message) {
  const node = byId("push-status");
  if (node) node.textContent = message;
}

function withinQuietHours(preferences) {
  if (!preferences?.quiet_hours_start || !preferences?.quiet_hours_end) return false;
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: preferences.timezone || "UTC", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date());
    const hour = Number(parts.find((part) => part.type === "hour").value);
    const minute = Number(parts.find((part) => part.type === "minute").value);
    const current = hour * 60 + minute;
    const toMinutes = (value) => Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5));
    const start = toMinutes(preferences.quiet_hours_start);
    const end = toMinutes(preferences.quiet_hours_end);
    return start < end ? current >= start && current < end : current >= start || current < end;
  } catch (_) { return false; }
}

function deliverBrowserAlerts(items) {
  const ids = new Set(items.map((item) => item.notification_id));
  if (state.browserAlertBaseline === null) {
    state.browserAlertBaseline = ids;
    return;
  }
  const fresh = items.filter((item) => !state.browserAlertBaseline.has(item.notification_id));
  state.browserAlertBaseline = ids;
  if (!state.notificationPreferences?.browser_notifications || !window.Notification || Notification.permission !== "granted") return;
  if (withinQuietHours(state.notificationPreferences)) return;
  for (const item of fresh.filter((entry) => entry.presentation?.disposition === "interrupt")) {
    const notice = new Notification(item.subject || "Agent OS needs your attention", {
      body: "Open Agent OS to review this item securely.",
      tag: item.notification_id,
      icon: "/assets/app-icon.svg",
    });
    notice.onclick = async () => {
      window.focus();
      await selectView("inbox");
      focusInboxItem(item);
      notice.close();
    };
  }
}

function webPushDeviceId() {
  const key = "aos.web-push.device-id";
  let value = localStorage.getItem(key);
  if (!value) {
    value = crypto.randomUUID();
    localStorage.setItem(key, value);
  }
  return value;
}

function applicationServerKey(value) {
  const padded = value.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - value.length % 4) % 4);
  const raw = atob(padded);
  return Uint8Array.from(raw, (character) => character.charCodeAt(0));
}

async function enrollBackgroundPush() {
  if (!state.config?.web_push_public_key) {
    setPushStatus("Background delivery is not provisioned for this deployment.");
    return false;
  }
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !window.Notification) {
    setPushStatus("This browser does not support background Web Push.");
    return false;
  }
  if (!window.isSecureContext) throw new Error("Background alerts require HTTPS (localhost is allowed for local testing).");
  const permission = Notification.permission === "granted"
    ? "granted" : await Notification.requestPermission();
  if (permission !== "granted") throw new Error("Browser notification permission was not granted.");
  const registration = await navigator.serviceWorker.ready;
  let subscription = await registration.pushManager.getSubscription();
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: applicationServerKey(state.config.web_push_public_key),
    });
  }
  const encoded = subscription.toJSON();
  if (!encoded.endpoint || !encoded.keys?.p256dh || !encoded.keys?.auth) {
    throw new Error("The browser returned an incomplete push subscription.");
  }
  const record = await api("/v2/me/push-subscriptions", {
    method: "POST",
    headers: {"Idempotency-Key": `push-register-${crypto.randomUUID()}`},
    body: JSON.stringify({
      device_id: webPushDeviceId(),
      device_name: "This browser",
      endpoint: encoded.endpoint,
      expiration_time: encoded.expirationTime || null,
      keys: encoded.keys,
    }),
  });
  state.pushSubscriptionId = record.subscription_id;
  setPushStatus("Background alerts are active on this device.");
  return true;
}

async function revokeBackgroundPush() {
  if (!("serviceWorker" in navigator)) return;
  const deviceId = webPushDeviceId();
  const records = await api("/v2/me/push-subscriptions");
  const record = (records.items || []).find((item) => item.device_id === deviceId && item.active);
  if (record) {
    await api(`/v2/me/push-subscriptions/${encodeURIComponent(record.subscription_id)}`, {
      method: "DELETE",
      headers: {"Idempotency-Key": `push-revoke-${crypto.randomUUID()}`},
      body: JSON.stringify({reason: "Disabled by the signed-in person"}),
    });
  }
  const registration = await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.getSubscription();
  if (subscription) await subscription.unsubscribe();
  state.pushSubscriptionId = null;
  setPushStatus("Background alerts are off on this device.");
}

async function reconcileBackgroundPush() {
  if (!state.token) return;
  if (!state.config?.web_push_public_key) {
    setPushStatus("Background delivery is not provisioned for this deployment.");
    return;
  }
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !window.Notification) {
    setPushStatus("This browser does not support background Web Push.");
    return;
  }
  if (!state.notificationPreferences?.browser_notifications) {
    setPushStatus("Background alerts are off on this device.");
    return;
  }
  if (Notification.permission === "denied") {
    setPushStatus("Background alerts are blocked in this browser's site settings.");
    return;
  }
  if (Notification.permission !== "granted") {
    setPushStatus("Save notification settings to allow background alerts on this device.");
    return;
  }
  const registration = await navigator.serviceWorker.ready;
  const browserSubscription = await registration.pushManager.getSubscription();
  const deviceId = webPushDeviceId();
  const records = await api("/v2/me/push-subscriptions");
  const activeRecord = (records.items || []).find((item) => item.device_id === deviceId && item.active);
  if (browserSubscription && activeRecord) {
    state.pushSubscriptionId = activeRecord.subscription_id;
    setPushStatus("Background alerts are active on this device.");
    return;
  }
  await enrollBackgroundPush();
}

function field(placeholder, ariaLabel, maxLength = 256) {
  const input = document.createElement("input");
  input.placeholder = placeholder; input.maxLength = maxLength;
  input.setAttribute("aria-label", ariaLabel);
  return input;
}

function integrationCard(title, description) {
  const card = el("section", "panel integration-card");
  card.append(el("h4", "", title), el("p", "muted", description));
  return card;
}

async function disableIntegration(path, reason) {
  await api(path, {
    method: "DELETE", headers: {"Idempotency-Key": `disable-${crypto.randomUUID()}`},
    body: JSON.stringify({reason}),
  });
  setFlash("Integration disabled. Queued notifications were cancelled.");
  await loadIntegrations();
}

async function loadIntegrations() {
  const content = byId("integrations-content");
  content.replaceChildren(el("div", "empty", "Loading governed capabilities…"));
  const [connectorPayload, routePayload, deliveryPayload] = await Promise.all([
    api("/v2/connectors"), api("/v2/notification-routes"),
    api("/v2/notification-deliveries?limit=100"),
  ]);
  const connectors = connectorPayload.items || [];
  const routes = routePayload.items || [];
  const deliveries = deliveryPayload.items || [];
  content.replaceChildren();

  const connectorCard = integrationCard(
    "Add an HTTPS connector",
    "Approve one exact origin, path family, and authentication reference. Secret values stay outside the application database.",
  );
  const connectorForm = el("div", "integration-form");
  const connectorId = field("Connector ID (for example slack-api)", "Connector ID", 64);
  const connectorName = field("Display name", "Connector display name", 200);
  const connectorOrigin = field("HTTPS origin (https://api.example.com)", "Connector HTTPS origin", 2000);
  const connectorPath = field("Allowed path prefix (/v1/events)", "Allowed connector path", 2000);
  const credentialRef = field("Credential reference (optional)", "Connector credential reference", 128);
  const authKind = document.createElement("select"); authKind.setAttribute("aria-label", "Authentication kind");
  for (const [value, title] of [["bearer", "Bearer token"], ["header", "API key header"], ["none", "No authentication"]]) {
    const option = el("option", "", title); option.value = value; authKind.append(option);
  }
  const authHeader = field("Header name (for API key)", "Authentication header", 128);
  const addConnector = el("button", "primary", "Add connector"); addConnector.type = "button";
  addConnector.addEventListener("click", async () => {
    addConnector.disabled = true;
    try {
      const auth = authKind.value;
      await api("/v2/connectors", {
        method: "POST", headers: {"Idempotency-Key": `connector-${crypto.randomUUID()}`},
        body: JSON.stringify({
          connector_id: connectorId.value.trim(), display_name: connectorName.value.trim(),
          base_url: connectorOrigin.value.trim(), allowed_path_prefixes: [connectorPath.value.trim()],
          allowed_methods: ["POST"], auth_kind: auth,
          credential_ref: auth === "none" ? null : credentialRef.value.trim(),
          auth_header: auth === "header" ? authHeader.value.trim() : null,
          idempotency_header: "Idempotency-Key", timeout_seconds: 30,
          max_response_bytes: 262144,
        }),
      });
      setFlash("Connector policy saved. Provision the named credential, then attach an alert route.");
      await loadIntegrations();
    } catch (error) { setFlash(error.message, "error"); }
    finally { addConnector.disabled = false; }
  });
  connectorForm.append(
    connectorId, connectorName, connectorOrigin, connectorPath,
    authKind, credentialRef, authHeader, addConnector,
  );
  connectorCard.append(connectorForm);
  const connectorList = el("div", "people");
  for (const connector of connectors) {
    const item = el("div", "person");
    item.append(
      el("strong", "", connector.display_name),
      el("small", "", `${connector.connector_id} · ${connector.active ? "active" : "disabled"} · ${connector.base_url}`),
    );
    if (connector.active) {
      const disable = el("button", "danger", "Disable"); disable.type = "button";
      disable.addEventListener("click", () => disableIntegration(
        `/v2/connectors/${encodeURIComponent(connector.connector_id)}`,
        "Disabled in the Agent OS workspace",
      ).catch((error) => setFlash(error.message, "error")));
      item.append(disable);
    }
    connectorList.append(item);
  }
  if (!connectors.length) connectorList.append(el("div", "empty", "No connectors yet."));
  connectorCard.append(connectorList); content.append(connectorCard);

  const routeCard = integrationCard(
    "Send important alerts externally",
    "Route human decisions, failures, and management escalations through an active POST connector. Deliveries retry durably and remain auditable.",
  );
  const routeForm = el("div", "integration-form compact-form");
  const routeId = field("Route ID (executive-alerts)", "Notification route ID", 64);
  const routeName = field("Route display name", "Notification route display name", 200);
  const routeConnector = document.createElement("select"); routeConnector.setAttribute("aria-label", "Notification connector");
  for (const connector of connectors.filter((item) => item.active && (item.allowed_methods || []).includes("POST"))) {
    const option = el("option", "", connector.display_name); option.value = connector.connector_id; routeConnector.append(option);
  }
  const routePath = field("Exact delivery path (/v1/events/agent-os)", "Notification delivery path", 2000);
  const routeFormat = document.createElement("select"); routeFormat.setAttribute("aria-label", "Notification payload format");
  for (const value of ["agent-os", "slack"]) {
    const option = el("option", "", value === "agent-os" ? "Generic Agent OS webhook" : "Slack channel message");
    option.value = value; routeFormat.append(option);
  }
  const routeAudience = document.createElement("select"); routeAudience.setAttribute("aria-label", "Notification audience");
  for (const [value, title] of [["human:ceo", "Executive decision makers"], ["operator:on-call", "Operations on-call"], ["*", "All notification recipients"]]) {
    const option = el("option", "", title); option.value = value; routeAudience.append(option);
  }
  const routeRedaction = document.createElement("select"); routeRedaction.setAttribute("aria-label", "Notification detail policy");
  for (const [value, title] of [["summary", "Redacted summary"], ["full", "Full notification record"]]) {
    const option = el("option", "", title); option.value = value; routeRedaction.append(option);
  }
  const destination = field("Slack channel (only for Slack)", "Notification destination", 256);
  const addRoute = el("button", "primary", "Activate alerts"); addRoute.type = "button";
  addRoute.disabled = !routeConnector.options.length;
  addRoute.addEventListener("click", async () => {
    addRoute.disabled = true;
    try {
      await api("/v2/notification-routes", {
        method: "POST", headers: {"Idempotency-Key": `route-${crypto.randomUUID()}`},
        body: JSON.stringify({
          route_id: routeId.value.trim(), display_name: routeName.value.trim(),
          connector_id: routeConnector.value, path: routePath.value.trim(),
          categories: ["human_action_required", "operator_attention", "run_failed", "management_attention", "work_recovered"],
          payload_format: routeFormat.value,
          destination: routeFormat.value === "slack" ? destination.value.trim() : null,
          recipient_ids: [routeAudience.value], redaction_policy: routeRedaction.value,
        }),
      });
      setFlash("External alert route activated."); await loadIntegrations();
    } catch (error) { setFlash(error.message, "error"); }
    finally { addRoute.disabled = false; }
  });
  routeForm.append(routeId, routeName, routeConnector, routePath, routeAudience, routeRedaction, routeFormat, destination, addRoute);
  routeCard.append(routeForm);
  const routeList = el("div", "people");
  for (const route of routes) {
    const item = el("div", "person");
    item.append(
      el("strong", "", route.display_name),
      el("small", "", `${route.connector_id}${route.path} · ${route.payload_format} · ${(route.recipient_ids || ["human:ceo"]).map(label).join(", ")} · ${route.redaction_policy || "summary"} · ${route.active ? "active" : "disabled"}`),
    );
    if (route.active) {
      const disable = el("button", "danger", "Disable"); disable.type = "button";
      disable.addEventListener("click", () => disableIntegration(
        `/v2/notification-routes/${encodeURIComponent(route.route_id)}`,
        "Disabled in the Agent OS workspace",
      ).catch((error) => setFlash(error.message, "error")));
      item.append(disable);
    }
    routeList.append(item);
  }
  if (!routes.length) routeList.append(el("div", "empty", "No external alert routes yet."));
  routeCard.append(routeList); content.append(routeCard);

  const auditCard = integrationCard(
    "Delivery audit", "Failed sends can be redriven after the external credential or provider is fixed.",
  );
  const auditList = el("div", "people");
  for (const delivery of deliveries) {
    const item = el("div", "person");
    item.append(
      el("strong", "", delivery.route_id),
      el("small", "", `${label(delivery.status)} · ${delivery.attempts} attempt(s) · ${shortId(delivery.notification_id)}`),
    );
    if (delivery.status === "failed") {
      const redrive = el("button", "quiet", "Retry after fix"); redrive.type = "button";
      redrive.addEventListener("click", async () => {
        redrive.disabled = true;
        try {
          await api(`/v2/notification-deliveries/${encodeURIComponent(delivery.delivery_id)}/redrive`, {
            method: "POST", headers: {"Idempotency-Key": `redrive-${crypto.randomUUID()}`},
          });
          setFlash("Delivery queued again."); await loadIntegrations();
        } catch (error) { setFlash(error.message, "error"); }
        finally { redrive.disabled = false; }
      });
      item.append(redrive);
    }
    auditList.append(item);
  }
  if (!deliveries.length) auditList.append(el("div", "empty", "No external deliveries yet."));
  auditCard.append(auditList); content.append(auditCard);
}

async function loadPreviews() {
  const payload = await api("/v2/deployments?limit=100");
  const items = payload.items || [];
  const content = byId("previews-content");
  content.replaceChildren();
  if (!items.length) return content.append(el("div", "empty", "Released applications and previews will appear here."));
  for (const item of items) {
    const node = el("article", "release");
    node.append(
      el("strong", "", item.app_slug || item.deployment_id || "Release"),
      el("small", "", `${label(item.kind || "deployment")} · ${label(item.status)}${item.expires_at ? ` · Expires ${item.expires_at}` : ""}`),
    );
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
    if (state.view !== "missions") await refreshExecutionReadiness({force: true});
    if (state.view === "missions") await loadMissions();
    if (state.view === "work") await loadMyWork();
    if (state.view === "company") await loadCompany();
    if (state.view === "inbox") await loadInbox({refresh: true});
    if (state.view === "integrations") await loadIntegrations();
    if (state.view === "previews") await loadPreviews();
    if (state.view === "billing") await loadBilling();
    byId("last-refresh").textContent = `Updated ${new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}`;
    if (state.selectedRun) await loadMissionDetail(state.selectedRun, true);
  } catch (error) {
    if (!silent) setFlash(error.message, "error");
  }
}

async function refreshAmbient() {
  await refreshInboxBadge();
  await refreshExecutionReadiness();
  if (userIsEditing()) return;
  try {
    if (state.view === "missions") await loadMissions();
    if (state.view === "work") await loadMyWork();
    if (state.view === "inbox") await loadInbox({refresh: true});
    if (state.view === "previews") await loadPreviews();
    byId("last-refresh").textContent = `Updated ${new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}`;
    if (state.selectedRun) await loadMissionDetail(state.selectedRun, true);
  } catch (_) {
    // Background refresh is intentionally quiet; explicit refresh reports errors.
  }
}

function detailSection(title) {
  const section = el("section", "detail-card");
  section.append(el("h4", "", title));
  return section;
}

async function openMission(item, {updateRoute = true} = {}) {
  state.drawerReturnFocus = document.activeElement;
  state.selectedRun = item;
  if (updateRoute) {
    history.pushState({}, document.title, workspaceRoute(state.view, item.run_id));
  }
  byId("drawer-title").textContent = item.title || "Mission";
  byId("mission-drawer").classList.add("open");
  byId("mission-drawer").setAttribute("aria-hidden", "false");
  document.querySelector(".sidebar").inert = true;
  byId("workspace-main").inert = true;
  byId("mission-drawer").focus();
  await loadMissionDetail(item);
}

async function openMissionDeepLink(runId) {
  if (!runId || runId.length > 256 || runId.includes("\0")) {
    history.replaceState({}, document.title, workspaceRoute(state.view));
    setFlash("This mission link is invalid.", "error");
    return;
  }
  try {
    const run = await api(`/v2/runs/${encodeURIComponent(runId)}`);
    await openMission(run, {updateRoute: false});
    history.replaceState({}, document.title, workspaceRoute(state.view, runId));
  } catch (_) {
    history.replaceState({}, document.title, workspaceRoute(state.view));
    closeDrawer({updateRoute: false});
    setFlash("This mission is unavailable or no longer authorized.", "error");
  }
}

function closeDrawer({updateRoute = true} = {}) {
  state.selectedRun = null;
  byId("mission-drawer").classList.remove("open");
  byId("mission-drawer").setAttribute("aria-hidden", "true");
  document.querySelector(".sidebar").inert = false;
  byId("workspace-main").inert = false;
  if (updateRoute && state.token) {
    history.replaceState({}, document.title, workspaceRoute(state.view));
  }
  if (
    state.drawerReturnFocus?.isConnected
    && !state.drawerReturnFocus.closest(".view.hidden")
  ) state.drawerReturnFocus.focus();
  else if (state.token) byId("workspace-main").focus();
  state.drawerReturnFocus = null;
}

async function loadMissionDetail(item, silent = false) {
  const content = byId("drawer-content");
  if (!silent) content.replaceChildren(el("div", "empty", "Reading durable mission state…"));
  try {
    const [run, missionFetch, managementFetch, company, participantsFetch, messagesFetch] = await Promise.all([
      api(`/v2/runs/${encodeURIComponent(item.run_id)}`),
      api(`/v2/runs/${encodeURIComponent(item.run_id)}/mission`).catch((error) => ({__error: error.message})),
      api(`/v2/runs/${encodeURIComponent(item.run_id)}/management`).catch((error) => ({__error: error.message})),
      can("company.read") ? api("/v2/company/organization") : Promise.resolve({teams: [], agents: []}),
      can("mission.steer")
        ? api(`/v2/runs/${encodeURIComponent(item.run_id)}/participants`).catch((error) => ({__error: error.message}))
        : Promise.resolve(null),
      api(`/v2/runs/${encodeURIComponent(item.run_id)}/messages?limit=100`).catch((error) => ({__error: error.message})),
    ]);
    if (!state.selectedRun || state.selectedRun.run_id !== item.run_id) return;
    const missionResult = missionFetch?.__error ? null : missionFetch;
    const managementResult = managementFetch?.__error ? null : managementFetch;
    content.replaceChildren();
    const overview = detailSection("Current state");
    const grid = el("div", "detail-grid");
    for (const [name, value] of [["Phase", label(run.phase)], ["Status", label(run.status)], ["Revision", run.version]]) {
      const cell = el("div"); cell.append(el("small", "", name), el("strong", "", value)); grid.append(cell);
    }
    overview.append(grid);
    if (managementResult) {
      const ratio = Math.round(Number(managementResult.progress?.materialized_completion_ratio || 0) * 100);
      const track = el("div", "progress-track");
      track.setAttribute("role", "progressbar"); track.setAttribute("aria-valuemin", "0");
      track.setAttribute("aria-valuemax", "100"); track.setAttribute("aria-valuenow", String(Math.min(100, ratio)));
      const bar = el("span"); bar.style.width = `${Math.min(100, ratio)}%`; track.append(bar);
      overview.append(track, el("small", "muted", `${ratio}% of currently materialized work complete · ${label(managementResult.health)}`));
    }
    if (navigator.clipboard?.writeText) {
      const share = el("button", "quiet", "Copy authorized mission link");
      share.type = "button";
      share.addEventListener("click", async () => {
        try {
          const target = new URL(workspaceRoute(state.view, item.run_id), window.location.href);
          await navigator.clipboard.writeText(target.href);
          setFlash("Mission link copied. Teammates still need access to this company.");
        } catch (_) {
          setFlash("The browser blocked clipboard access; copy the current address instead.", "error");
        }
      });
      overview.append(share);
    }
    content.append(overview);
    for (const [name, result] of [["Execution projection", missionFetch], ["Management projection", managementFetch]]) {
      if (!result?.__error) continue;
      const unavailable = el("div", "projection-warning");
      unavailable.setAttribute("role", "status");
      unavailable.textContent = `${name} is temporarily unavailable: ${result.__error}. Durable mission state is unchanged.`;
      content.append(unavailable);
    }

    if (messagesFetch && !messagesFetch.__error) {
      const conversation = detailSection("Mission conversation");
      conversation.append(el(
        "p", "program-rationale",
        "Ask, clarify, or update the mission here. Shared messages are visible to every assigned participant; internal messages stay with the delivery team.",
      ));
      const messages = messagesFetch.items || [];
      if (!messages.length) conversation.append(el("p", "muted", "No mission messages yet."));
      for (const message of messages) {
        const row = el("div", "work-row conversation-message");
        row.append(
          el("span", `phase message-${message.kind}`, label(message.kind)),
          el("p", "", message.body),
          el(
            "small", "",
            `${label(message.sender_persona)} · ${message.sender_id} · ${label(message.channel)} · ${new Date(message.created_at).toLocaleString()}`,
          ),
        );
        conversation.append(row);
      }
      const draft = state.missionMessageDrafts[item.run_id] || {
        body: "", kind: "comment", channel: "shared",
      };
      const composer = el("div", "access-form conversation-composer");
      const kind = document.createElement("select");
      kind.setAttribute("aria-label", "Message kind");
      const permittedKinds = can("work.read") ? ["comment", "question", "update"] : ["comment", "question"];
      if (!permittedKinds.includes(draft.kind)) draft.kind = "comment";
      for (const value of permittedKinds) {
        const option = el("option", "", label(value)); option.value = value;
        option.selected = draft.kind === value; kind.append(option);
      }
      const channel = document.createElement("select");
      channel.setAttribute("aria-label", "Message audience");
      const channels = messagesFetch.channels || ["shared"];
      if (!channels.includes(draft.channel)) draft.channel = "shared";
      for (const value of channels) {
        const option = el("option", "", value === "shared" ? "Shared with participants" : "Internal delivery team");
        option.value = value; option.selected = draft.channel === value; channel.append(option);
      }
      const messageBody = document.createElement("textarea");
      messageBody.rows = 3; messageBody.maxLength = 8000;
      messageBody.setAttribute("aria-label", "Mission message");
      messageBody.placeholder = "Ask a question, clarify a requirement, or share an update…";
      messageBody.value = draft.body;
      const retainDraft = () => {
        state.missionMessageDrafts[item.run_id] = {
          body: messageBody.value, kind: kind.value, channel: channel.value,
        };
      };
      messageBody.addEventListener("input", retainDraft);
      kind.addEventListener("change", retainDraft); channel.addEventListener("change", retainDraft);
      const send = el("button", "primary", "Send message"); send.type = "button";
      send.addEventListener("click", async () => {
        if (!messageBody.value.trim()) return setFlash("Write a mission message first.", "error");
        send.disabled = true;
        try {
          await api(`/v2/runs/${encodeURIComponent(item.run_id)}/messages`, {
            method: "POST",
            headers: {"Idempotency-Key": `mission-message-${crypto.randomUUID()}`},
            body: JSON.stringify({
              body: messageBody.value.trim(), kind: kind.value, channel: channel.value,
            }),
          });
          delete state.missionMessageDrafts[item.run_id];
          setFlash(kind.value === "question" ? "Question sent to the mission team." : "Mission message posted.");
          await loadMissionDetail(item);
        } catch (error) { setFlash(error.message, "error"); }
        finally { send.disabled = false; }
      });
      composer.append(kind, channel, messageBody, send); conversation.append(composer);
      content.append(conversation);
    } else if (messagesFetch?.__error) {
      const unavailable = el("div", "projection-warning");
      unavailable.setAttribute("role", "status");
      unavailable.textContent = `Mission conversation is temporarily unavailable: ${messagesFetch.__error}.`;
      content.append(unavailable);
    }

    if (participantsFetch && !participantsFetch.__error) {
      const collaboration = detailSection("Mission collaborators");
      collaboration.append(el(
        "p", "program-rationale",
        "Give a teammate access only to this mission. Their organization role must match the mission role.",
      ));
      const activeParticipants = (participantsFetch.items || []).filter((value) => value.active);
      for (const participant of participantsFetch.items || []) {
        const row = el("div", "work-row");
        row.append(
          el("span", `health ${participant.active ? "healthy" : "failed"}`, participant.active ? "Active" : "Revoked"),
          el("p", "", participant.subject_id),
          el("small", "", `${label(participant.participation_role)} · revision ${participant.version}`),
        );
        if (participant.active) {
          const revoke = el("button", "danger", "Remove access"); revoke.type = "button";
          revoke.addEventListener("click", async () => {
            if (!window.confirm(`Remove ${participant.subject_id} from this mission?`)) return;
            try {
              await api(`/v2/runs/${encodeURIComponent(item.run_id)}/participants/${encodeURIComponent(participant.subject_id)}`, {
                method: "DELETE",
                headers: {"Idempotency-Key": `participant-revoke-${crypto.randomUUID()}`},
                body: JSON.stringify({reason: "Mission access removed in the Agent OS workspace"}),
              });
              setFlash("Mission access removed."); await loadMissionDetail(item);
            } catch (error) { setFlash(error.message, "error"); }
          });
          row.append(revoke);
        }
        collaboration.append(row);
      }
      const assign = el("div", "access-form");
      const subject = field("Teammate identity subject", "Mission teammate identity", 255);
      const role = document.createElement("select");
      role.setAttribute("aria-label", "Mission participation role");
      for (const value of ["builder", "reviewer", "client", "viewer"]) {
        const option = el("option", "", label(value)); option.value = value; role.append(option);
      }
      const grant = el("button", "primary", "Add to mission"); grant.type = "button";
      grant.addEventListener("click", async () => {
        if (!subject.value.trim()) return setFlash("Enter the teammate identity first.", "error");
        grant.disabled = true;
        try {
          await api(`/v2/runs/${encodeURIComponent(item.run_id)}/participants`, {
            method: "POST",
            headers: {"Idempotency-Key": `participant-grant-${crypto.randomUUID()}`},
            body: JSON.stringify({
              subject_id: subject.value.trim(), participation_role: role.value,
            }),
          });
          setFlash("Mission access granted."); await loadMissionDetail(item);
        } catch (error) { setFlash(error.message, "error"); }
        finally { grant.disabled = false; }
      });
      assign.append(subject, role, grant); collaboration.append(assign); content.append(collaboration);
      if (activeParticipants.length) {
        const request = el("div", "access-form");
        request.append(el(
          "p", "program-rationale",
          "Request a response without pausing the mission. Only the selected teammate can answer; you can monitor the durable request in the inbox.",
        ));
        const draft = state.humanRequestDrafts[item.run_id] || {
          recipient_id: activeParticipants[0].subject_id, subject: "", body: "",
        };
        const recipient = document.createElement("select");
        recipient.setAttribute("aria-label", "Human request recipient");
        for (const participant of activeParticipants) {
          const option = el(
            "option", "",
            `${participant.subject_id} · ${label(participant.participation_role)}`,
          );
          option.value = participant.subject_id;
          option.selected = draft.recipient_id === participant.subject_id;
          recipient.append(option);
        }
        if (![...recipient.options].some((option) => option.selected)) {
          recipient.options[0].selected = true;
        }
        const requestSubject = field("What do you need?", "Human request subject", 500);
        requestSubject.value = draft.subject;
        const requestBody = document.createElement("textarea");
        requestBody.rows = 3; requestBody.maxLength = 16384;
        requestBody.setAttribute("aria-label", "Human request details");
        requestBody.placeholder = "Give the context and describe a useful response…";
        requestBody.value = draft.body;
        const retainRequest = () => {
          state.humanRequestDrafts[item.run_id] = {
            recipient_id: recipient.value,
            subject: requestSubject.value,
            body: requestBody.value,
          };
        };
        recipient.addEventListener("change", retainRequest);
        requestSubject.addEventListener("input", retainRequest);
        requestBody.addEventListener("input", retainRequest);
        const ask = el("button", "quiet", "Request response"); ask.type = "button";
        ask.addEventListener("click", async () => {
          if (!requestSubject.value.trim() || !requestBody.value.trim()) {
            return setFlash("Describe both the request and the context first.", "error");
          }
          ask.disabled = true;
          try {
            await api(`/v2/runs/${encodeURIComponent(item.run_id)}/human-requests`, {
              method: "POST",
              headers: {"Idempotency-Key": `human-request-${crypto.randomUUID()}`},
              body: JSON.stringify({
                recipient_id: recipient.value,
                subject: requestSubject.value.trim(),
                body: requestBody.value.trim(),
              }),
            });
            delete state.humanRequestDrafts[item.run_id];
            setFlash("The response request is in the selected teammate's inbox.");
            await loadMissionDetail(item);
          } catch (error) { setFlash(error.message, "error"); }
          finally { ask.disabled = false; }
        });
        request.append(recipient, requestSubject, requestBody, ask);
        collaboration.append(request);
      }
    }

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

    const assurance = managementResult?.assurance || missionResult?.assurance;
    if (assurance) {
      const trust = detailSection("Authority, safety, and evidence");
      const budget = assurance.budget || {};
      const mission = assurance.mission || {};
      const authorities = assurance.authorities || [];
      const activeAuthorities = authorities.filter((value) => !value.revoked);
      const trustGrid = el("div", "detail-grid");
      for (const [name, value] of [
        ["Mission revision", Number(mission.revision || 1)],
        ["Human mode", label(mission.human_involvement_mode || "balanced")],
        ["Daily interrupts", Number(mission.daily_interrupt_limit ?? 8)],
        ["Available", `$${(Number(budget.available_cents || 0) / 100).toFixed(2)}`],
        ["Reserved", `$${(Number(budget.reserved_cents || 0) / 100).toFixed(2)}`],
        ["Spent", `$${(Number(budget.spent_cents || 0) / 100).toFixed(2)}`],
        ["Evidence", Number(assurance.evidence?.length || 0)],
        ["Active grants", activeAuthorities.length],
      ]) {
        const cell = el("div");
        cell.append(el("small", "", name), el("strong", "", value));
        trustGrid.append(cell);
      }
      const revisions = assurance.mission_revisions || [];
      if (revisions.length > 1) {
        const latest = revisions[revisions.length - 1];
        const row = el("div", "work-row");
        row.append(
          el("span", "phase", `Revision ${latest.revision}`),
          el("p", "", latest.revision_reason || "Mission contract revised"),
          el("small", "", `Revised by ${label(latest.revised_by || "system")}`),
        );
        trust.append(row);
      }
      trust.append(trustGrid);
      const held = (assurance.effects || []).filter((value) =>
        ["denied", "human_required", "restricted"].includes(value.status));
      for (const value of held) {
        const decision = value.decision || {};
        const row = el("div", "work-row");
        row.append(
          el("span", `health ${value.status === "denied" ? "failed" : "degraded"}`, label(value.status)),
          el("p", "", `${label(value.request?.action || "effect")} · ${decision.reasons?.join(" · ") || "held by assurance"}`),
          el("small", "", `Safe mode: ${label(decision.safe_mode || "freeze")}`),
        );
        trust.append(row);
      }
      for (const hazard of assurance.hazards || []) {
        const row = el("div", "work-row");
        row.append(
          el("span", `health ${hazard.severity === "critical" ? "failed" : "degraded"}`, label(hazard.severity)),
          el("p", "", hazard.description),
          el("small", "", `Fallback: ${label(hazard.fallback_mode)}`),
        );
        trust.append(row);
      }
      content.append(trust);

      if ((assurance.claims || []).length || (assurance.evidence || []).length) {
        const review = detailSection("Claims and review evidence");
        const evidenceById = new Map((assurance.evidence || []).map((record) => [record.evidence_id, record]));
        for (const claim of assurance.claims || []) {
          const row = el("div", "work-row evidence-row");
          row.append(
            el("span", `phase claim-${claim.status}`, label(claim.status)),
            el("p", "", claim.statement),
            el("small", "", `Asserted by ${label(claim.asserted_by)} · ${claim.evidence_ids?.length || 0} evidence item(s)`),
          );
          for (const evidenceId of claim.evidence_ids || []) {
            const evidence = evidenceById.get(evidenceId);
            if (!evidence) continue;
            const detail = el("details", "evidence-detail");
            detail.append(
              el("summary", "", `${label(evidence.kind)} · ${evidence.media_type || "artifact"}`),
              el("small", "", `Observed ${evidence.observed_at || "unknown"} · SHA-256 ${shortId(evidence.sha256)}`),
            );
            if (can("artifact.read") && String(evidence.artifact_ref || "").startsWith("artifact-")) {
              const download = el("button", "quiet", "Download evidence"); download.type = "button";
              download.addEventListener("click", async () => {
                download.disabled = true;
                try { await downloadMissionArtifact(item.run_id, evidence.artifact_ref); }
                catch (error) { setFlash(error.message, "error"); }
                finally { download.disabled = false; }
              });
              detail.append(download);
            }
            row.append(detail);
          }
          review.append(row);
        }
        const unclaimed = (assurance.evidence || []).filter((record) =>
          !(assurance.claims || []).some((claim) => (claim.evidence_ids || []).includes(record.evidence_id)));
        if (unclaimed.length) review.append(el("small", "muted", `${unclaimed.length} retained evidence item(s) are not yet linked to a claim.`));
        content.append(review);
      }
    }

    if (managementResult?.management_signals?.length) {
      const signals = detailSection("Manager signals");
      for (const signal of managementResult.management_signals) {
        const row = el("div", "work-row");
        row.append(
          el("span", `health ${signal.severity === "critical" ? "failed" : "degraded"}`, label(signal.signal)),
          el("p", "", signal.reason || "A manager detected material execution variance."),
        );
        if (signal.recommended_action) row.append(el("small", "", `Recommended: ${signal.recommended_action}`));
        signals.append(row);
      }
      content.append(signals);
    }
    const narrativeGroups = [
      ["Next actions", managementResult?.next_actions, (entry) => entry.text || entry.action || entry.description],
      ["Risks and escalations", managementResult?.risks, (entry) => entry.description || entry.text || entry.reason],
      ["Decisions", managementResult?.decisions, (entry) => entry.intent || entry.decision || entry.text || entry.reason],
      ["Team communications", managementResult?.communications, (entry) => entry.body || entry.message || entry.text],
      ["Observations", managementResult?.observations, (entry) => entry.text || entry.observation || entry.description],
    ];
    for (const [title, values, describe] of narrativeGroups) {
      if (!values?.length) continue;
      const section = detailSection(title);
      for (const value of values) {
        const row = el("div", "work-row");
        row.append(
          el("p", "", describe(value) || "Recorded update"),
          el("small", "", `${label(value.actor_id || value.source_node_id || value.kind || "mission team")}${value.status ? ` · ${label(value.status)}` : ""}`),
        );
        section.append(row);
      }
      content.append(section);
    }
    if (managementResult?.work_items?.length) {
      const work = detailSection("Team execution");
      for (const itemValue of managementResult.work_items) {
        const row = el("div", "work-row");
        const queueAttempts = itemValue.queue_attempts == null ? "not dispatched" : `${itemValue.queue_attempts} queue attempt(s)`;
        row.append(
          el("span", `health ${itemValue.health}`, label(itemValue.health)),
          el("p", "", itemValue.objective),
          el("small", "", `Accountable: ${itemValue.accountable_owner_id || "role:manager"} · AI delegate: ${itemValue.delegate_id || itemValue.owner_id || "system"} · ${label(itemValue.status)} · iteration ${itemValue.iteration} · node attempt ${itemValue.attempt} · ${queueAttempts}`),
        );
        if (can("work.assign") && participantsFetch && !participantsFetch.__error) {
          for (const [duty, field, role] of [
            ["responsible", "human_assignment", "builder"],
            ["reviewer", "review_assignment", "reviewer"],
          ]) {
            const assignment = itemValue[field];
            const actions = el("div", "proposal-actions");
            if (assignment) {
              row.append(el("small", "", `${label(duty)}: ${assignment.subject_id} · ${label(assignment.assignment_status || assignment.status)} · revision ${assignment.version}`));
              if (assignment.active && ["participant_inactive", "work_revision_changed"].includes(assignment.assignment_status)) {
                row.append(el("p", "status-line warning", `${label(duty)} responsibility is not effective because access or the work contract changed. Mission management remains accountable until it is replaced.`));
              }
              if (assignment.active) {
                const remove = el("button", "danger", `Remove ${duty} assignment`); remove.type = "button";
                remove.addEventListener("click", async () => {
                  if (!window.confirm(`Remove ${assignment.subject_id} as ${duty} for this work item?`)) return;
                  remove.disabled = true;
                  try {
                    await api(`/v2/runs/${encodeURIComponent(item.run_id)}/work/${encodeURIComponent(itemValue.work_id)}/assignments/${duty}`, {
                      method: "DELETE",
                      headers: {"Idempotency-Key": `work-unassign-${crypto.randomUUID()}`},
                      body: JSON.stringify({reason: `${label(duty)} responsibility removed by mission management`, expected_version: assignment.version}),
                    });
                    setFlash(`${label(duty)} assignment removed and the participant was notified.`); await loadMissionDetail(item);
                  } catch (error) { setFlash(error.message, "error"); }
                  finally { remove.disabled = false; }
                });
                actions.append(remove);
              }
            }
            const eligible = (participantsFetch.items || []).filter((participant) =>
              participant.active
              && participant.participation_role === role
              && (!assignment?.active || participant.subject_id !== assignment.subject_id));
            if (eligible.length) {
              const assignee = document.createElement("select");
              assignee.setAttribute("aria-label", `${label(duty)} human for ${itemValue.objective}`);
              for (const participant of eligible) {
                const option = el("option", "", `${participant.subject_id} · ${label(participant.participation_role)}`);
                option.value = participant.subject_id; assignee.append(option);
              }
              const assign = el("button", "", assignment ? `Replace ${duty}` : `Assign ${duty}`); assign.type = "button";
              assign.addEventListener("click", async () => {
                assign.disabled = true;
                try {
                  await api(`/v2/runs/${encodeURIComponent(item.run_id)}/work/${encodeURIComponent(itemValue.work_id)}/assignments/${duty}`, {
                    method: "POST",
                    headers: {"Idempotency-Key": `work-assign-${crypto.randomUUID()}`},
                    body: JSON.stringify({
                      subject_id: assignee.value,
                      expected_version: assignment?.version || 0,
                      reason: assignment
                        ? `${label(duty)} responsibility atomically reassigned by mission management`
                        : `${label(duty)} responsibility assigned by mission management`,
                    }),
                  });
                  setFlash(`${label(duty)} responsibility requested; accountability changes only after acceptance.`);
                  await loadMissionDetail(item);
                } catch (error) { setFlash(error.message, "error"); }
                finally { assign.disabled = false; }
              });
              actions.append(assignee, assign);
            }
            if (actions.childElementCount) row.append(actions);
          }
        }
        const diagnostics = el("details", "work-diagnostics");
        diagnostics.append(el("summary", "", "Execution diagnostics"));
        const facts = el("dl", "diagnostic-grid");
        const fact = (name, value) => {
          if (value == null || value === "") return;
          facts.append(el("dt", "", name), el("dd", "", String(value)));
        };
        fact("Health reason", itemValue.diagnostic_reason || "No abnormal condition is currently detected.");
        fact("Queue state", itemValue.queue_status ? label(itemValue.queue_status) : "Not dispatched");
        fact("Last proven progress", itemValue.last_progress_at ? new Date(itemValue.last_progress_at).toLocaleString() : "No action receipt yet");
        fact("Recovery checkpoint", itemValue.next_infrastructure_checkpoint_at ? new Date(itemValue.next_infrastructure_checkpoint_at).toLocaleString() : "Not scheduled");
        fact("Waiting for", itemValue.wait_reason);
        fact("Manager", itemValue.manager_id);
        fact("Evidence", (itemValue.evidence_ids || []).length ? `${itemValue.evidence_ids.length} retained item(s)` : "None yet");
        fact("Last error", itemValue.last_error);
        diagnostics.append(facts);
        row.append(diagnostics);
        work.append(row);
      }
      content.append(work);
    }
    if (managementResult?.execution_timeline?.length) {
      const timeline = detailSection("Execution timeline");
      timeline.append(el("p", "program-rationale", "Newest durable queue actions first. Payloads and secrets are intentionally omitted."));
      for (const action of managementResult.execution_timeline) {
        const row = el("div", "work-row timeline-row");
        const started = action.created_at ? new Date(action.created_at) : null;
        const ended = action.completed_at ? new Date(action.completed_at) : null;
        const elapsed = started && ended
          ? `${Math.max(0, Math.round((ended - started) / 1000))}s elapsed`
          : (started ? `queued ${started.toLocaleString()}` : "time unavailable");
        row.append(
          el("span", `health ${action.status === "failed" ? "failed" : (action.status === "succeeded" ? "healthy" : "degraded")}`, label(action.status)),
          el("p", "", `${label(action.kind || "workflow action")} · ${label(action.node_id || "system")}`),
          el("small", "", `state v${action.state_version} · ${action.attempts || 0} attempt(s) · ${elapsed} · ${shortId(action.action_id)}`),
        );
        if (action.error?.message) row.append(el("small", "timeline-error", `${label(action.error.type || "error")}: ${action.error.message}`));
        timeline.append(row);
      }
      if (managementResult.execution_timeline_truncated) timeline.append(el("small", "muted", "Older actions are retained but omitted from this bounded view."));
      content.append(timeline);
    }
    if (managementResult?.hiring_requests?.length) {
      const proposals = detailSection("Staffing proposals");
      for (const proposal of managementResult.hiring_requests) {
        const row = el("div", "work-row");
        row.append(el("span", "phase", label(proposal.status)), el("p", "", `${proposal.requested_count || 1} × ${proposal.role}`));
        if (
          proposal.status === "pending"
          && (proposal.participant_kind || "agent") === "agent"
          && can("mission.steer")
        ) {
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
    if (can("mission.cancel") && !["succeeded", "failed", "cancelled"].includes(run.status)) {
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
        approved, reason: approved ? "Approved in the Agent OS workspace" : "Rejected in the Agent OS workspace",
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
      method: "POST", body: JSON.stringify({ event_id: `workspace-cancel-${crypto.randomUUID()}`, expected_version: version, reason: "Cancelled in the Agent OS workspace" }),
    });
    setFlash("Cancellation accepted."); closeDrawer(); await loadMissions();
  } catch (error) { setFlash(error.message, "error"); }
}

function selectView(name) {
  if (!viewAllowed(name)) {
    setFlash("That workspace is not available for your current role.", "error");
    name = personaDefaultView();
  }
  state.view = name;
  if (name !== "inbox") {
    state.focusedNotificationId = null;
    state.focusInboxItemPending = false;
  }
  byId("mobile-more-menu").classList.add("hidden");
  byId("mobile-more-toggle").setAttribute("aria-expanded", "false");
  document.querySelectorAll(".nav-item[data-view]").forEach((node) => {
    const active = node.dataset.view === name;
    node.classList.toggle("active", active);
    if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-mobile-view]").forEach((node) => {
    if (node.dataset.mobileView === name) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  byId("mobile-more-toggle").classList.toggle("active", ["company", "integrations", "previews", "billing"].includes(name));
  document.querySelectorAll(".view").forEach((node) => node.classList.add("hidden"));
  byId(`${name}-view`).classList.remove("hidden");
  byId("view-title").textContent = { missions: "Missions", work: "My Work", company: "Company", inbox: "Inbox", integrations: "Integrations", previews: "Releases", billing: "Billing" }[name];
  byId("workspace-main").focus({preventScroll: true});
  history.replaceState({}, document.title, workspaceRoute(name));
  closeDrawer({updateRoute: false}); return refreshView();
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
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js").catch(() => null);
  } catch (error) {
    byId("auth-status").textContent = error.message;
    byId("auth-status").className = "status-line error";
  }
}

byId("oidc-login").addEventListener("click", beginOidc);
byId("token-form").addEventListener("submit", (event) => { event.preventDefault(); connect(byId("token-input").value.trim()); });
byId("disconnect").addEventListener("click", () => disconnect());
byId("mobile-disconnect").addEventListener("click", () => disconnect());
byId("mobile-more-toggle").addEventListener("click", () => {
  const menu = byId("mobile-more-menu"); const opening = menu.classList.contains("hidden");
  menu.classList.toggle("hidden", !opening);
  byId("mobile-more-toggle").setAttribute("aria-expanded", String(opening));
});
document.querySelectorAll("[data-mobile-view]").forEach((node) => node.addEventListener("click", () => selectView(node.dataset.mobileView)));
byId("refresh").addEventListener("click", () => refreshView());
byId("execution-retry").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await refreshExecutionReadiness({force: true, onboarding: state.view === "missions"});
    setFlash(state.missionAdmissionBlocked
      ? "Execution is still unavailable. Your draft remains unchanged."
      : "Execution recovered. New missions can start.", state.missionAdmissionBlocked ? "error" : "");
  } finally { button.disabled = false; }
});
byId("organization-select").addEventListener("change", async (event) => {
  stopLiveEvents();
  state.organization = event.target.value;
  state.readiness = null;
  state.readinessCheckedAt = 0;
  state.missionAdmissionBlocked = true;
  state.missionSubmission = null;
  renderExecutionReadiness(null);
  state.browserAlertBaseline = null;
  state.inboxCursor = null;
  state.decisionDrafts = {};
  state.notificationPreferences = null;
  state.pushSubscriptionId = null;
  state.eventCursor = null;
  sessionStorage.setItem("aos.organization", state.organization);
  closeDrawer();
  state.session = await api("/v2/me");
  applyRoleExperience();
  setFlash("Organization changed.");
  await selectView(personaDefaultView());
  await refreshInboxBadge();
  reconcileBackgroundPush().catch((error) => setPushStatus(error.message));
  startLiveEvents();
});
byId("drawer-close").addEventListener("click", closeDrawer);
byId("directive").addEventListener("input", (event) => { byId("directive-count").textContent = `${event.target.value.length.toLocaleString()} / 50,000`; });
byId("directive-human-mode").addEventListener("change", (event) => {
  byId("human-mode-explanation").textContent = {
    autonomous: "Routine and reversible work continues without interruption; policy, authority, and high-risk gates still hold.",
    balanced: "Routine reversible work continues; material risk, ambiguity, or authority gaps come to you.",
    collaborative: "The team asks for more frequent direction and keeps you close to important trade-offs.",
  }[event.target.value];
});
byId("directive-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  if (state.missionAdmissionBlocked) {
    await refreshExecutionReadiness({force: true, onboarding: true});
    if (state.missionAdmissionBlocked) {
      setFlash("Execution is unavailable. Your mission draft has not been submitted.", "error");
      return;
    }
  }
  button.dataset.submitting = "true"; button.disabled = true;
  try {
    const mission = {
      prompt: byId("directive").value.trim(),
      title: byId("directive-title").value.trim() || null,
      budget_limit_cents: Math.round(Number(byId("directive-budget").value || 0) * 100),
      human_involvement_mode: byId("directive-human-mode").value,
      daily_interrupt_limit: Number(byId("directive-interrupt-limit").value || 0),
    };
    const missionFingerprint = JSON.stringify(mission);
    if (state.missionSubmission?.fingerprint !== missionFingerprint) {
      state.missionSubmission = {
        fingerprint: missionFingerprint,
        idempotencyKey: `ceo-${crypto.randomUUID()}`,
      };
    }
    const result = await api("/v2/runs", {
      method: "POST",
      headers: {"Idempotency-Key": state.missionSubmission.idempotencyKey},
      body: missionFingerprint,
    });
    state.missionSubmission = null;
    byId("directive").value = ""; byId("directive-title").value = ""; byId("directive-budget").value = "0"; byId("directive-count").textContent = "0 / 50,000";
    setFlash("Mission accepted. Your team is planning it now."); await loadMissions();
    openMission({ run_id: result.run_id, title: "New mission" });
  } catch (error) {
    if (error.code === "execution_plane_unavailable") {
      await refreshExecutionReadiness({force: true, onboarding: true});
    }
    setFlash(error.message, "error");
  }
  finally {
    button.dataset.submitting = "false";
    button.disabled = state.missionAdmissionBlocked;
  }
});
byId("inbox-preferences-toggle").addEventListener("click", () => {
  const panel = byId("inbox-preferences");
  const opening = panel.classList.contains("hidden");
  panel.classList.toggle("hidden", !opening);
  byId("inbox-preferences-toggle").setAttribute("aria-expanded", String(opening));
});
byId("save-inbox-preferences").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const quietStart = byId("quiet-start").value || null;
  const quietEnd = byId("quiet-end").value || null;
  if (Boolean(quietStart) !== Boolean(quietEnd)) return setFlash("Choose both quiet-hour times or clear both.", "error");
  let browserNotifications = byId("browser-notifications").checked;
  if (browserNotifications && window.Notification && Notification.permission === "default") {
    browserNotifications = (await Notification.requestPermission()) === "granted";
    byId("browser-notifications").checked = browserNotifications;
  }
  if (browserNotifications && (!window.Notification || Notification.permission !== "granted")) {
    browserNotifications = false; byId("browser-notifications").checked = false;
    setFlash("Browser alerts are blocked in this browser; the inbox remains active.", "error");
  }
  button.disabled = true;
  try {
    if (browserNotifications && state.config?.web_push_public_key) {
      await enrollBackgroundPush();
    } else if (!browserNotifications && state.config?.web_push_public_key) {
      await revokeBackgroundPush();
    }
    state.notificationPreferences = await api("/v2/notification-preferences", {
      method: "PUT", headers: {"Idempotency-Key": `notice-preferences-${crypto.randomUUID()}`},
      body: JSON.stringify({
        mode: byId("inbox-mode").value,
        browser_notifications: browserNotifications,
        quiet_hours_start: quietStart,
        quiet_hours_end: quietEnd,
        timezone: byId("notification-timezone").value.trim() || "UTC",
        digest_interval_minutes: Number(byId("digest-interval").value),
      }),
    });
    setFlash("Notification preferences saved."); await loadInbox({refresh: true});
  } catch (error) { setFlash(error.message, "error"); }
  finally { button.disabled = false; }
});
document.querySelectorAll("[data-inbox-filter]").forEach((node) => node.addEventListener("click", () => {
  state.inboxFilter = node.dataset.inboxFilter;
  state.focusedNotificationId = null;
  state.focusInboxItemPending = false;
  document.querySelectorAll("[data-inbox-filter]").forEach((item) => item.classList.toggle("active", item === node));
  renderInbox();
}));
document.querySelectorAll(".nav-item[data-view]").forEach((node) => node.addEventListener("click", () => selectView(node.dataset.view)));
window.addEventListener("hashchange", () => {
  if (!state.token) return;
  const requested = routeFromHash();
  if (requested.pushDeliveryId) {
    openPushDelivery(requested.pushDeliveryId);
    return;
  }
  (async () => {
    if (requested.view && requested.view !== state.view) await selectView(requested.view);
    if (requested.runId) {
      if (requested.runId !== state.selectedRun?.run_id) {
        await openMissionDeepLink(requested.runId);
      }
    } else if (state.selectedRun) {
      closeDrawer({updateRoute: false});
    }
  })();
});
document.addEventListener("visibilitychange", () => {
  if (!state.token) return;
  if (document.hidden) {
    stopLiveEvents();
    setLiveStatus("Polling paused while hidden");
    return;
  }
  setLiveStatus("Reconnecting live updates…");
  refreshAmbient();
  startLiveEvents();
});
navigator.serviceWorker?.addEventListener("message", (event) => {
  if (event.data?.type === "push-subscription-changed") {
    reconcileBackgroundPush().catch(() => null);
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeDrawer();
    byId("mobile-more-menu").classList.add("hidden");
    byId("mobile-more-toggle").setAttribute("aria-expanded", "false");
  }
  if (event.key !== "Tab" || !byId("mission-drawer").classList.contains("open")) return;
  const focusable = Array.from(byId("mission-drawer").querySelectorAll("button, a[href], input, select, textarea, [tabindex]:not([tabindex='-1'])"))
    .filter((node) => !node.disabled && !node.classList.contains("hidden"));
  if (!focusable.length) return;
  const first = focusable[0]; const last = focusable[focusable.length - 1];
  if (!focusable.includes(document.activeElement)) {
    event.preventDefault(); (event.shiftKey ? last : first).focus(); return;
  }
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
bootstrap();
