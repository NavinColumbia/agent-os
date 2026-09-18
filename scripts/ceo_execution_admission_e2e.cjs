// Browser-level proof that an execution outage never loses a CEO's mission draft.
//
// Run with:
//   NODE_PATH=/path/to/node_modules node scripts/ceo_execution_admission_e2e.cjs
//
// Every API is intercepted. No real mission, model call, or external effect is created.

"use strict";

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "src/agent_os/api/web/ceo.html"), "utf8");
const script = fs.readFileSync(path.join(root, "src/agent_os/api/web/ceo.js"), "utf8");
const css = fs.readFileSync(path.join(root, "src/agent_os/api/web/ceo.css"), "utf8");

const readiness = (healthy) => ({
  overall: healthy ? "ready_for_first_mission" : "blocked",
  can_start_mission: healthy,
  show_onboarding: true,
  blockers: healthy ? [] : ["execution_plane"],
  steps: [{
    id: "execution_plane",
    title: "Autonomous execution",
    status: healthy ? "complete" : "blocked",
    required: true,
    detail: healthy
      ? "A release-fenced worker is healthy and accepting durable work."
      : "No current execution worker is available. Existing mission state is safe, but new work is paused until a worker recovers.",
    action_view: null,
    reason: healthy ? "ready" : "worker_unavailable",
    retryable: !healthy,
  }],
});

(async () => {
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({
    viewport: {width: 1280, height: 900},
    serviceWorkers: "block",
  });
  const page = await context.newPage();
  const errors = [];
  let healthy = false;
  let createAttempts = 0;
  const createKeys = [];
  const acceptedRun = "run-browser-admission-proof";

  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("console", (message) => {
    if (
      message.type() === "error"
      && !message.text().includes("503 (Service Unavailable)")
    ) errors.push(message.text());
  });
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    const json = (body, status = 200, headers = {}) => route.fulfill({
      status,
      contentType: "application/json",
      headers,
      body: JSON.stringify(body),
    });

    if (pathname === "/app" || pathname === "/") {
      return route.fulfill({status: 200, contentType: "text/html", body: html});
    }
    if (pathname === "/assets/ceo.js") {
      return route.fulfill({status: 200, contentType: "text/javascript", body: script});
    }
    if (pathname === "/assets/ceo.css") {
      return route.fulfill({status: 200, contentType: "text/css", body: css});
    }
    if (pathname === "/v2/client-config") {
      return json({identity_mode: "local", billing_mode: "disabled"});
    }
    if (pathname === "/v2/organizations") {
      return json({
        default_organization_id: "org-browser",
        items: [{organization_id: "org-browser", roles: ["owner"]}],
      });
    }
    if (pathname === "/v2/me") {
      return json({
        persona: "executive",
        capabilities: ["mission.create", "mission.read", "company.read"],
      });
    }
    if (pathname === "/v2/company/organization") {
      return json({teams: [], agents: [], humans: [], services: []});
    }
    if (pathname === "/v2/usage/summary") {
      return json({committed_cents: 0, remaining_cents: 1000});
    }
    if (pathname === "/v2/readiness") return json(readiness(healthy));
    if (pathname === "/v2/notifications") {
      return json({items: [], preferences: {browser_notifications: false}});
    }
    if (pathname === "/v2/events/stream") {
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "event: cursor\ndata: {\"cursor\":\"1\"}\n\n",
      });
    }
    if (pathname === "/v2/runs" && request.method() === "GET") {
      return json({items: []});
    }
    if (pathname === "/v2/runs" && request.method() === "POST") {
      createAttempts += 1;
      createKeys.push(request.headers()["idempotency-key"]);
      if (createAttempts === 1) {
        healthy = false; // Simulate a worker failure after the UI's green check.
        return json({detail: {
          code: "execution_plane_unavailable",
          message: "New mission admission is temporarily paused because execution availability is not verified. Your draft was not submitted.",
          state: "unavailable",
          reason: "worker_unavailable",
        }}, 503, {"Retry-After": "15"});
      }
      return json({
        workflow_id: "workflow-browser-admission-proof",
        run_id: acceptedRun,
        accepted: true,
        duplicate: false,
      }, 202);
    }
    if (pathname === `/v2/runs/${acceptedRun}`) {
      return json({
        run_id: acceptedRun,
        title: "Browser admission proof",
        objective: "Launch without losing this exact draft",
        phase: "research",
        status: "active",
        version: 1,
      });
    }
    if (pathname.startsWith(`/v2/runs/${acceptedRun}/`)) return json({items: []});
    if (pathname === "/service-worker.js") {
      return route.fulfill({status: 200, contentType: "text/javascript", body: ""});
    }
    if (pathname.endsWith("app-icon.svg")) {
      return route.fulfill({status: 200, contentType: "image/svg+xml", body: "<svg xmlns='http://www.w3.org/2000/svg'/>"});
    }
    return json({detail: `unhandled browser-proof route: ${request.method()} ${pathname}`}, 404);
  });

  try {
    await page.goto("http://127.0.0.1/app", {waitUntil: "domcontentloaded"});
    await page.fill("#token-input", "browser-proof-token");
    await page.click("#token-form button[type=submit]");
    await page.waitForSelector("#workspace:not(.hidden)");
    try {
      await page.waitForSelector("#execution-banner:not(.hidden)", {timeout: 10000});
    } catch (error) {
      const diagnostics = await page.evaluate(() => ({
        bannerClass: document.querySelector("#execution-banner")?.className,
        submitDisabled: document.querySelector("#directive-submit")?.disabled,
        flash: document.querySelector("#flash")?.textContent,
        auth: document.querySelector("#auth-status")?.textContent,
      }));
      throw new Error(`initial blocked readiness was not rendered: ${JSON.stringify({diagnostics, errors})}; ${error.message}`);
    }
    if (!(await page.locator("#directive-submit").isDisabled())) {
      throw new Error("mission submission was enabled during an execution outage");
    }

    const draft = "Launch without losing this exact draft";
    await page.fill("#directive", draft);
    healthy = true;
    await page.click("#execution-retry");
    await page.waitForFunction(() => !document.querySelector("#directive-submit").disabled);
    if (await page.inputValue("#directive") !== draft) {
      throw new Error("draft changed while execution recovered");
    }

    await page.click("#directive-submit");
    try {
      await page.waitForSelector("#execution-banner:not(.hidden)", {timeout: 10000});
    } catch (error) {
      const diagnostics = await page.evaluate(() => ({
        bannerClass: document.querySelector("#execution-banner")?.className,
        submitDisabled: document.querySelector("#directive-submit")?.disabled,
        submitting: document.querySelector("#directive-submit")?.dataset.submitting,
        flash: document.querySelector("#flash")?.textContent,
        draft: document.querySelector("#directive")?.value,
      }));
      throw new Error(`503 race did not render blocked readiness: ${JSON.stringify({diagnostics, errors, createAttempts, healthy})}; ${error.message}`);
    }
    if (await page.inputValue("#directive") !== draft) {
      throw new Error("draft was lost after the API admission race returned 503");
    }
    if (!(await page.locator("#directive-submit").isDisabled())) {
      throw new Error("mission submission stayed enabled after the API rejected admission");
    }

    healthy = true;
    await page.click("#execution-retry");
    await page.waitForFunction(() => !document.querySelector("#directive-submit").disabled);
    await page.click("#directive-submit");
    await page.waitForFunction(() => document.querySelector("#directive").value === "");
    if (createAttempts !== 2) throw new Error(`expected 2 create attempts, saw ${createAttempts}`);
    if (!createKeys[0] || createKeys[0] !== createKeys[1]) {
      throw new Error(`unchanged draft did not reuse its idempotency key: ${JSON.stringify(createKeys)}`);
    }
    if (errors.length) throw new Error(`browser errors: ${errors.join(" | ")}`);
    console.log(JSON.stringify({
      ok: true,
      draft_preserved_across_outage: true,
      race_rejected_without_loss: true,
      accepted_after_recovery: true,
      stable_idempotency_key: true,
      create_attempts: createAttempts,
    }));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || String(error));
  process.exit(1);
});
