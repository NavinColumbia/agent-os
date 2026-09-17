"use strict";

const state = {
  token: "", organization: "", config: null, view: "missions", selectedRun: null,
  timer: null, session: null, inboxFilter: "open", inboxItems: [], notificationPreferences: null,
  browserAlertBaseline: null, drawerReturnFocus: null,
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
  state.organization = sessionStorage.getItem("aos.organization") || "";
  state.browserAlertBaseline = null;
  try {
    await loadOrganizations();
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
    await refreshInboxBadge();
    state.timer = window.setInterval(() => {
      if (!document.hidden && state.token) refreshAmbient();
    }, 10000);
  } catch (error) {
    state.token = "";
    byId("auth-status").textContent = error.message;
    byId("auth-status").className = "status-line error";
  }
}

function disconnect(message = "Disconnected. No credential was stored.") {
  state.token = "";
  state.organization = "";
  sessionStorage.removeItem("aos.organization");
  window.clearInterval(state.timer);
  state.timer = null;
  state.session = null;
  state.inboxItems = [];
  state.notificationPreferences = null;
  state.browserAlertBaseline = null;
  closeDrawer();
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

function applyRoleExperience() {
  const persona = state.session?.persona || "viewer";
  const labels = { executive: "Executive", operator: "Operations", builder: "Builder", viewer: "Viewer" };
  byId("workspace-role-label").textContent = `${labels[persona] || "Member"} workspace`;
  byId("identity-avatar").textContent = { executive: "CEO", operator: "OPS", builder: "BUILD", viewer: "VIEW" }[persona] || "USER";
  document.querySelectorAll(".requires-mission-create").forEach((node) => node.classList.toggle("hidden", !can("mission.create")));
  document.querySelectorAll(".requires-integration-manage").forEach((node) => node.classList.toggle("hidden", !can("integration.manage")));
  const billingAvailable = state.config?.billing_mode === "stripe";
  document.querySelectorAll(".requires-billing-manage").forEach((node) => node.classList.toggle("hidden", !can("billing.manage") || !billingAvailable));
  if ((!can("integration.manage") && state.view === "integrations") || ((!can("billing.manage") || !billingAvailable) && state.view === "billing")) {
    selectView("missions");
  }
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
  const invite = el("div", "access-form");
  const role = document.createElement("select");
  for (const value of ["viewer", "operator", "owner"]) {
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
  const people = el("div", "people");
  for (const member of memberships.items || []) {
    const card = el("div", "person");
    card.append(
      el("strong", "", member.subject_id),
      el("small", "", `${(member.roles || []).join(", ")} · ${member.active ? "active" : "revoked"}`),
    );
    if (member.active) {
      const revoke = el("button", "danger", "Revoke"); revoke.type = "button";
      revoke.addEventListener("click", async () => {
        if (!window.confirm(`Revoke ${member.subject_id}'s access?`)) return;
        try {
          await api(`/v2/memberships/${encodeURIComponent(member.subject_id)}`, {
            method: "DELETE", headers: {"Idempotency-Key": `revoke-${crypto.randomUUID()}`},
            body: JSON.stringify({reason: "Revoked in the CEO workspace"}),
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

async function loadInbox() {
  const payload = await api("/v2/notifications?limit=100");
  state.inboxItems = payload.items || [];
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
  if (!items.length) return content.append(el("div", "empty", "Nothing needs your attention."));
  for (const item of items) {
    const itemState = item.user_state?.status || "unread";
    const attention = item.presentation?.level || "active";
    const node = el("article", `notice ${itemState} ${attention}`);
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
      const decisionStatus = item.decision_response?.status;
      const decisionMessage = decisionStatus === "pending" || decisionStatus === "executing"
        ? "Your response is durably queued and will resume this work."
        : (decisionStatus === "failed"
          ? "The response could not be applied. Operator attention is required; your intent remains recorded."
          : "Resolved or no longer actionable");
      node.append(el("small", "", decisionMessage));
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
}

async function updateNotificationState(item, status, snoozedUntil, control) {
  control.disabled = true;
  try {
    await api(`/v2/notifications/${encodeURIComponent(item.notification_id)}/state`, {
      method: "PUT", headers: {"Idempotency-Key": `notice-state-${crypto.randomUUID()}`},
      body: JSON.stringify({status, snoozed_until: snoozedUntil}),
    });
    await loadInbox();
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
    await loadInbox();
  } catch (error) {
    for (const control of actions.querySelectorAll("button,input")) control.disabled = false;
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
    notice.onclick = () => { window.focus(); selectView("inbox"); notice.close(); };
  }
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
        "Disabled in the CEO workspace",
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
        "Disabled in the CEO workspace",
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
    if (state.view === "missions") await loadMissions();
    if (state.view === "company") await loadCompany();
    if (state.view === "inbox") await loadInbox();
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
  if (userIsEditing()) return;
  try {
    if (state.view === "missions") await loadMissions();
    if (state.view === "inbox") await loadInbox();
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

async function openMission(item) {
  state.drawerReturnFocus = document.activeElement;
  state.selectedRun = item;
  byId("drawer-title").textContent = item.title || "Mission";
  byId("mission-drawer").classList.add("open");
  byId("mission-drawer").setAttribute("aria-hidden", "false");
  byId("mission-drawer").focus();
  await loadMissionDetail(item);
}

function closeDrawer() {
  state.selectedRun = null;
  byId("mission-drawer").classList.remove("open");
  byId("mission-drawer").setAttribute("aria-hidden", "true");
  if (state.drawerReturnFocus?.isConnected) state.drawerReturnFocus.focus();
  state.drawerReturnFocus = null;
}

async function loadMissionDetail(item, silent = false) {
  const content = byId("drawer-content");
  if (!silent) content.replaceChildren(el("div", "empty", "Reading durable mission state…"));
  try {
    const [run, missionFetch, managementFetch, company] = await Promise.all([
      api(`/v2/runs/${encodeURIComponent(item.run_id)}`),
      api(`/v2/runs/${encodeURIComponent(item.run_id)}/mission`).catch((error) => ({__error: error.message})),
      api(`/v2/runs/${encodeURIComponent(item.run_id)}/management`).catch((error) => ({__error: error.message})),
      api("/v2/company/organization"),
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
    content.append(overview);
    for (const [name, result] of [["Execution projection", missionFetch], ["Management projection", managementFetch]]) {
      if (!result?.__error) continue;
      const unavailable = el("div", "projection-warning");
      unavailable.setAttribute("role", "status");
      unavailable.textContent = `${name} is temporarily unavailable: ${result.__error}. Durable mission state is unchanged.`;
      content.append(unavailable);
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
        row.append(el("span", `health ${signal.severity === "critical" ? "failed" : "degraded"}`, label(signal.signal)), el("p", "", signal.reason || signal.recommended_action));
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
  if (name === "integrations" && !can("integration.manage")) return setFlash("Your role cannot manage integrations.", "error");
  if (name === "billing" && (!can("billing.manage") || state.config?.billing_mode !== "stripe")) return setFlash("Billing is not available in this deployment.", "error");
  state.view = name;
  byId("mobile-more-menu").classList.add("hidden");
  byId("mobile-more-toggle").setAttribute("aria-expanded", "false");
  document.querySelectorAll(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.view === name));
  byId("mobile-more-toggle").classList.toggle("active", ["company", "integrations", "billing"].includes(name));
  document.querySelectorAll(".view").forEach((node) => node.classList.add("hidden"));
  byId(`${name}-view`).classList.remove("hidden");
  byId("view-title").textContent = { missions: "Missions", company: "Company", inbox: "Inbox", integrations: "Integrations", previews: "Releases", billing: "Billing" }[name];
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
byId("organization-select").addEventListener("change", async (event) => {
  state.organization = event.target.value;
  state.browserAlertBaseline = null;
  sessionStorage.setItem("aos.organization", state.organization);
  closeDrawer();
  state.session = await api("/v2/me");
  applyRoleExperience();
  setFlash("Organization changed.");
  await refreshView();
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
  const button = event.submitter; button.disabled = true;
  try {
    const result = await api("/v2/runs", {
      method: "POST", headers: { "Idempotency-Key": `ceo-${crypto.randomUUID()}` },
      body: JSON.stringify({
        prompt: byId("directive").value.trim(),
        title: byId("directive-title").value.trim() || null,
        budget_limit_cents: Math.round(Number(byId("directive-budget").value || 0) * 100),
        human_involvement_mode: byId("directive-human-mode").value,
        daily_interrupt_limit: Number(byId("directive-interrupt-limit").value || 0),
      }),
    });
    byId("directive").value = ""; byId("directive-title").value = ""; byId("directive-budget").value = "0"; byId("directive-count").textContent = "0 / 50,000";
    setFlash("Mission accepted. Your team is planning it now."); await loadMissions();
    openMission({ run_id: result.run_id, title: "New mission" });
  } catch (error) { setFlash(error.message, "error"); }
  finally { button.disabled = false; }
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
    setFlash("Notification preferences saved."); await loadInbox();
  } catch (error) { setFlash(error.message, "error"); }
  finally { button.disabled = false; }
});
document.querySelectorAll("[data-inbox-filter]").forEach((node) => node.addEventListener("click", () => {
  state.inboxFilter = node.dataset.inboxFilter;
  document.querySelectorAll("[data-inbox-filter]").forEach((item) => item.classList.toggle("active", item === node));
  renderInbox();
}));
document.querySelectorAll(".nav-item[data-view]").forEach((node) => node.addEventListener("click", () => selectView(node.dataset.view)));
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
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
bootstrap();
