// Browser proof for exact-recipient, non-blocking human requests.
// All APIs are intercepted; no model call or external effect is created.

"use strict";

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "src/agent_os/api/web/ceo.html"), "utf8");
const script = fs.readFileSync(path.join(root, "src/agent_os/api/web/ceo.js"), "utf8");
const css = fs.readFileSync(path.join(root, "src/agent_os/api/web/ceo.css"), "utf8");
const runId = "run-human-request-proof";
const requestId = `human-request-${"a".repeat(64)}`;

(async () => {
  const browser = await chromium.launch({headless: true});
  const context = await browser.newContext({
    viewport: {width: 1280, height: 1000}, serviceWorkers: "block",
  });
  const errors = [];
  let created = null;
  let answered = null;

  const install = async (page) => {
    page.on("pageerror", (error) => errors.push(String(error)));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    await page.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const pathname = url.pathname;
      const reviewer = request.headers().authorization === "Bearer reviewer-token";
      const json = (body, status = 200) => route.fulfill({
        status, contentType: "application/json", body: JSON.stringify(body),
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
          default_organization_id: "org-proof",
          items: [{organization_id: "org-proof", roles: [reviewer ? "reviewer" : "owner"]}],
        });
      }
      if (pathname === "/v2/me") {
        return json(reviewer ? {
          persona: "reviewer", capabilities: ["decision.respond", "mission.read"],
        } : {
          persona: "executive",
          capabilities: ["mission.create", "mission.read", "mission.steer", "company.read", "work.read"],
        });
      }
      if (pathname === "/v2/readiness") {
        return json({
          overall: "ready_for_first_mission", can_start_mission: true,
          show_onboarding: false, blockers: [], steps: [],
        });
      }
      if (pathname === "/v2/usage/summary") {
        return json({committed_cents: 0, remaining_cents: 1000});
      }
      if (pathname === "/v2/company/organization") {
        return json({teams: [], agents: [], humans: [], services: []});
      }
      if (pathname === "/v2/me/push-subscriptions") return json({items: []});
      if (pathname === "/v2/events/stream") {
        return route.fulfill({
          status: 200, contentType: "text/event-stream",
          body: "event: cursor\ndata: {\"cursor\":\"1\"}\n\n",
        });
      }
      if (pathname === "/v2/runs" && request.method() === "GET") {
        return json({items: [{
          run_id: runId, title: "Human collaboration proof",
          objective_preview: "Ask for input while work continues",
          phase: "build", status: "active", version: 3,
        }]});
      }
      if (pathname === `/v2/runs/${runId}`) {
        return json({
          run_id: runId, title: "Human collaboration proof",
          objective: "Ask for input while work continues",
          phase: "build", status: "active", version: 3,
        });
      }
      if (pathname === `/v2/runs/${runId}/mission`) return json({});
      if (pathname === `/v2/runs/${runId}/management`) {
        return json({health: "healthy", progress: {materialized_completion_ratio: 0.6}});
      }
      if (pathname === `/v2/runs/${runId}/participants`) {
        return json({items: [{
          subject_id: "reviewer-subject", participation_role: "reviewer",
          active: true, version: 1,
        }]});
      }
      if (pathname === `/v2/runs/${runId}/messages`) {
        return json({items: [], channels: ["shared", "internal"]});
      }
      if (
        pathname === `/v2/runs/${runId}/human-requests`
        && request.method() === "POST"
      ) {
        created = {
          body: request.postDataJSON(),
          idempotencyKey: request.headers()["idempotency-key"],
        };
        return json({
          request_id: requestId, run_id: runId, request_kind: "advisory",
          recipient_id: created.body.recipient_id, requested_by: "owner-subject",
          subject: created.body.subject, body: created.body.body,
          status: "open", duplicate: false,
        }, 201);
      }
      if (
        pathname === `/v2/human-requests/${requestId}/responses`
        && request.method() === "POST"
      ) {
        answered = {
          body: request.postDataJSON(),
          idempotencyKey: request.headers()["idempotency-key"],
        };
        return json({
          request_id: requestId, status: "answered",
          responded_by: "reviewer-subject", duplicate: false,
        });
      }
      if (pathname === "/v2/notifications") {
        return json({
          items: reviewer ? [{
            notification_id: "notification-proof", run_id: runId,
            category: "human_action_required", subject: "Review the launch claim",
            body: "Flag anything unsupported.", correlation_id: null,
            actionable: !answered,
            human_request: {
              request_id: requestId, request_kind: "advisory",
              recipient_id: "reviewer-subject", status: answered ? "answered" : "open",
            },
            user_state: {status: "unread"},
            presentation: {disposition: "interrupt", level: "active"},
            created_at: "2026-09-17T12:00:00+00:00",
          }] : [],
          preferences: {
            mode: "balanced", browser_notifications: false,
            quiet_hours_start: null, quiet_hours_end: null,
            timezone: "UTC", digest_interval_minutes: 60,
          },
          next_cursor: null,
        });
      }
      if (pathname === "/service-worker.js") {
        return route.fulfill({status: 200, contentType: "text/javascript", body: ""});
      }
      if (pathname.endsWith("app-icon.svg")) {
        return route.fulfill({
          status: 200, contentType: "image/svg+xml",
          body: "<svg xmlns='http://www.w3.org/2000/svg'/>",
        });
      }
      return json({detail: `unhandled browser-proof route: ${request.method()} ${pathname}`}, 404);
    });
  };

  try {
    const ownerPage = await context.newPage();
    await install(ownerPage);
    await ownerPage.goto("http://127.0.0.1/app", {waitUntil: "domcontentloaded"});
    await ownerPage.fill("#token-input", "owner-token");
    await ownerPage.click("#token-form button[type=submit]");
    await ownerPage.waitForSelector("#workspace:not(.hidden)");
    await ownerPage.click("#missions-list .mission");
    await ownerPage.getByRole("button", {name: "Request response"}).waitFor();
    await ownerPage.getByLabel("Human request subject").fill("Review the launch claim");
    await ownerPage.getByLabel("Human request details").fill("Flag anything unsupported.");
    await ownerPage.getByRole("button", {name: "Request response"}).click();
    await ownerPage.waitForFunction(() => (
      document.querySelector("#flash")?.textContent.includes("selected teammate")
    ));
    if (!created || created.body.recipient_id !== "reviewer-subject") {
      throw new Error(`request was not routed to the exact participant: ${JSON.stringify(created)}`);
    }

    const reviewerPage = await context.newPage();
    await install(reviewerPage);
    await reviewerPage.goto("http://127.0.0.1/app", {waitUntil: "domcontentloaded"});
    await reviewerPage.fill("#token-input", "reviewer-token");
    await reviewerPage.click("#token-form button[type=submit]");
    await reviewerPage.waitForSelector("#inbox-view:not(.hidden)");
    await reviewerPage.getByLabel("Human request response").fill("Remove the guarantee.");
    await reviewerPage.getByRole("button", {name: "Send response"}).click();
    await reviewerPage.waitForFunction(() => (
      document.querySelector("#flash")?.textContent.includes("recorded for the requester")
    ));
    if (!answered || answered.body.response.answer !== "Remove the guarantee.") {
      throw new Error(`recipient response was not recorded: ${JSON.stringify(answered)}`);
    }
    if (!created.idempotencyKey || !answered.idempotencyKey) {
      throw new Error("human request mutations did not carry idempotency keys");
    }
    if (errors.length) throw new Error(`browser errors: ${errors.join(" | ")}`);
    console.log(JSON.stringify({
      ok: true,
      nonblocking_request_created: true,
      exact_participant_selected: true,
      recipient_answered_in_inbox: true,
      idempotency_keys_present: true,
    }));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || String(error));
  process.exit(1);
});

