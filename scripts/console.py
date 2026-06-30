#!/usr/bin/env python3
"""console.py — the agent-os tenant CONSOLE: the full CEO-facing application, one nav over the whole spec.

This is the productized front-of-house the blueprint's 16 areas describe, wired to REAL data: it mounts
every tenant-scoped view module behind a single authenticated shell (X-Tenant-Token):

  Cockpit (A3) · Projects (A4) · Fleet (A5) · Observability (A6) · Incidents/Status (A7) ·
  Approvals (A8) · Integrations (A9) · Billing (A11) · Team (A12) · Settings (A13) · Templates+Build (A14)

Each nav item calls a JSON endpoint backed by a dedicated module (cockpit/projectsview/traceview/approvals/
integrationsview/billingview/settingsview/templatesview/notifications/statuspage). Builds reuse the front
door's governed flow (consent gate + quota + factory). Binds 127.0.0.1 (front with Tailscale serve).

    console.py serve [port]      # default 8099
    console.py selftest          # asserts the routes resolve + render for a real tenant
Run with the agent-os venv python.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import account           # noqa: E402
import approvals          # noqa: E402
import billing           # noqa: E402
import billingview       # noqa: E402
import cockpit           # noqa: E402
import consent           # noqa: E402
import customagents      # noqa: E402
import estimate          # noqa: E402
import forecast          # noqa: E402
import frontdoor         # noqa: E402  (reuse its governed build flow + zip)
import helpagent         # noqa: E402
import agentfeatures     # noqa: E402
import crossorg          # noqa: E402
import crossorgview      # noqa: E402
import designview        # noqa: E402
import integrationsview  # noqa: E402
import livestatus        # noqa: E402
import loopcontroller    # noqa: E402
import notifications     # noqa: E402
import onboarding        # noqa: E402
import orchestrator      # noqa: E402
import orgs as orgsmod   # noqa: E402
import orgview           # noqa: E402
import projbudget        # noqa: E402
import projectsview      # noqa: E402
import qualityview       # noqa: E402
import settingsview      # noqa: E402
import statuspage        # noqa: E402
import templatesview     # noqa: E402
import tenancy           # noqa: E402
import tenantproviders   # noqa: E402
import traceview         # noqa: E402
import vault             # noqa: E402
import versions          # noqa: E402


# Sentinel for an infrastructure/transient failure (e.g. Postgres briefly unreachable during WSL
# boot) — distinct from a genuine "unknown token". A real no-match returns None -> 401; this sentinel
# -> 503 so a momentary backend blip never force-logs-out a valid user nor wipes their saved token.
_TENANT_ERROR = object()


def _tenant(token):
    if not token:
        return None
    try:
        t = tenancy.tenant_for_token(token)
        return t["tenant_id"] if isinstance(t, dict) else t   # None when no match -> genuine 401
    except Exception:
        return _TENANT_ERROR                                  # transient/infra failure -> 503, not 401


def _owns(tid, org_id):
    """Authoritative ownership check for org-scoped routes (IDOR guard). A falsy org id is allowed
    (server picks the tenant's own default); a truthy id must belong to this tenant."""
    if not org_id:
        return True
    try:
        r = orgsmod.get(tid, int(org_id))
        return isinstance(r, dict) and not r.get("error")
    except Exception:
        return False


# Routes that carry a client-supplied org id — gated through _owns() before they ever touch a module.
ORG_SCOPED_GET = {"/api/controller/state", "/api/design",
                  "/api/cockpit", "/api/projects", "/api/fleet",
                  "/api/health", "/api/company", "/api/comms_graph"}
ORG_SCOPED_POST = {"/api/controller/say", "/api/controller/choose", "/api/controller/cancel",
                   "/api/design/decide"}


def _fleet(tid, org_id=0):
    """Area 5 — every live worker across the active org's products (or the whole tenant when
    org_id=0), flattened from the cockpit payload."""
    c = cockpit.cockpit(tid, org_id)
    workers = []
    for p in c["products"]:
        for w in p.get("workers", []):
            workers.append({**w, "product": p["product"]})
    return {"workers": workers, "count": len(workers),
            "products_active": sum(1 for p in c["products"] if p.get("workers"))}


def _assistant():
    """Account-level home router for the org=0 'All orgs (home)' thread. Lands as a separate, parallel
    module (assistant.py); until it does, org=0 gracefully falls back to the tenant's first company so
    the single Assistant surface stays functional. Imported lazily so console boots/selftests without it.
    Expected interface: state(tid) -> {messages,...} · say(tid, msg) · choose(tid, option_id)."""
    try:
        import assistant as _a
        return _a
    except Exception:
        return None


def _home_org(tid):
    """Fallback target for org=0 home work when assistant.py isn't present yet: the tenant's first
    company. Returns its org_id, or 0 when the tenant has no company yet."""
    orgs = orgsmod.list_orgs(tid)
    return orgs[0]["org_id"] if orgs else 0


def _controller_state(tid, org_id):
    """The Assistant surface state. org=N -> that company's loopcontroller thread (unchanged). org=0 ->
    the account-wide home thread via assistant.py when present, else the first company (fallback)."""
    if not org_id:
        a = _assistant()
        if a is not None and hasattr(a, "state"):
            return a.state(tid)
        org_id = _home_org(tid)
        if not org_id:
            # Brand-new tenant with 0 companies: a NON-error first-run state. The frontend turns this
            # into a welcoming "create your first company" screen — never an error+retry card.
            return {"home": True, "first_run": True, "messages": []}
    thread = loopcontroller.thread_for_org(tid, org_id)
    st = loopcontroller.state(thread)
    out = {"org_id": org_id, "thread": thread, "phase": st.get("phase"), "awaiting": st.get("awaiting"),
           "phase_label": _phase_label(st.get("phase")), "messages": orchestrator.history(tid, thread)}
    # LIVE PROGRESS: while a build/research job runs, hand the Assistant a phase label, elapsed seconds
    # and an ETA so it can render a live, working bubble instead of a static "give me a little time".
    if st.get("awaiting") == "fleet":
        try:
            out["progress"] = _controller_progress(thread, st)
        except Exception:
            pass
    return out


# Plain-language phase names + coarse per-phase ETAs (minutes) for the live-progress bubble. Build phases
# defer to estimate.py's historical median; the rest use these sensible defaults. We prefer anything the
# loopcontroller agent exposes (loopcontroller.progress / a richer state) and only fall back to these.
_PHASE_LABEL = {"DISCOVER": "Scoping", "RESEARCH": "Researching", "OPTIONS": "Options ready",
                "DEEP_DESIGN": "Designing the plan", "PLAN_APPROVAL": "Checking setup",
                "PROTOTYPE": "Designing screens", "IMPLEMENT": "Building", "TESTQA": "Testing",
                "DELIVER": "Finishing up"}
_PHASE_ETA_MIN = {"RESEARCH": 10, "DEEP_DESIGN": 4, "PROTOTYPE": 6,
                  "IMPLEMENT": 14, "TESTQA": 5, "DELIVER": 1}


def _phase_label(phase):
    return _PHASE_LABEL.get((phase or "").upper(), (phase or "Working").title())


def _job_eta_min(phase, kind):
    """Best ETA (minutes) for the in-flight phase: build/prototype reuse estimate.py's historical median;
    everything else uses a coarse per-phase default."""
    p = (phase or "").upper()
    if p in ("IMPLEMENT", "PROTOTYPE"):
        try:
            m = estimate.estimate(kind or "lib").get("minutes_estimate")
            if m:
                return int(round(m))
        except Exception:
            pass
    return _PHASE_ETA_MIN.get(p, 8)


def _active_job(thread_id):
    """The newest in-flight (running/pending) controller job for this thread, with elapsed seconds.
    Reads through loopcontroller's own DB handle so console stays a thin shell over it."""
    with loopcontroller.psycopg.connect(loopcontroller.DB) as c, c.cursor() as cur:
        cur.execute("""SELECT id, kind, phase, EXTRACT(EPOCH FROM (now()-started_at))::int
                       FROM controller_jobs WHERE thread_id=%s AND status IN ('running','pending')
                       ORDER BY id DESC LIMIT 1""", (thread_id,))
        r = cur.fetchone()
    if not r:
        return None
    return {"job_id": r[0], "kind": r[1], "job_phase": r[2], "elapsed_s": int(r[3] or 0)}


def _controller_progress(thread_id, st):
    """Live-progress payload for an in-flight job: phase label, elapsed seconds, ETA minutes + a
    plain-language note. Prefers loopcontroller's own live snapshot (live_status/progress/live); only
    falls back to a local controller_jobs read + estimate.py so the bubble works regardless."""
    for fn in ("live_status", "progress", "live"):
        f = getattr(loopcontroller, fn, None)
        if callable(f):
            try:
                p = f(thread_id)
            except Exception:
                p = None
            if isinstance(p, dict) and p and not p.get("error"):
                label = (p.get("status") or p.get("label") or p.get("phase_label")
                         or _phase_label(p.get("phase") or st.get("phase")))
                label = str(label).rstrip("…. ") or _phase_label(st.get("phase"))
                elapsed_s = int(p.get("elapsed_s") or (p.get("elapsed_min") or 0) * 60)
                eta = p.get("eta_min")
                mins_in = elapsed_s // 60
                note = "" if not eta else (f"about {max(1, int(eta) - mins_in)} min left" if mins_in < eta
                                           else "taking a little longer than usual — still working")
                return {"phase_label": label, "elapsed_s": elapsed_s, "eta_min": eta,
                        "eta_note": note, "kind": p.get("job_kind") or p.get("kind")}
    job = _active_job(thread_id) or {}
    phase = job.get("job_phase") or st.get("phase")
    eta = _job_eta_min(phase, job.get("kind"))
    elapsed_s = int(job.get("elapsed_s") or 0)
    mins_in = elapsed_s // 60
    note = (f"about {max(1, eta - mins_in)} min left" if mins_in < eta
            else "taking a little longer than usual — still working")
    return {"phase_label": _phase_label(phase), "elapsed_s": elapsed_s,
            "eta_min": eta, "eta_note": note, "kind": job.get("kind")}


def _ctl_cancel(tid, org_id):
    """STOP/CANCEL: abort the in-flight controller job and park the thread on a feedback gate so the CEO
    can say "retry" to resume. Defers to loopcontroller.cancel() when present; otherwise marks the active
    controller_jobs row cancelled and posts a 'Stopped' note (the fallback the dogfood brief specifies)."""
    if not org_id:
        a = _assistant()
        if a is not None and hasattr(a, "cancel"):
            return a.cancel(tid)
        org_id = _home_org(tid)
        if not org_id:
            return {"error": "Create a company first."}
    thread = loopcontroller.thread_for_org(tid, org_id)
    if hasattr(loopcontroller, "cancel"):
        try:
            return loopcontroller.cancel(tid, thread)
        except Exception as e:
            return {"error": str(e)[:160]}
    try:
        with loopcontroller.psycopg.connect(loopcontroller.DB) as c, c.cursor() as cur:
            cur.execute("""UPDATE controller_jobs SET status='cancelled', finished_at=now()
                           WHERE thread_id=%s AND status IN ('running','pending')""", (thread,))
            cur.execute("UPDATE controller_state SET awaiting='user_feedback', updated_at=now() WHERE thread_id=%s",
                        (thread,))
            c.commit()
        orchestrator.post(tid, thread, "⏹ Stopped. Say \"retry\" to pick up where we left off, or tell me "
                                       "what to change.", {"kind": "cancelled"})
        return {"cancelled": True, "thread": thread}
    except Exception as e:
        return {"error": str(e)[:160]}


def _ctl_say(tid, org_id, msg):
    """Route a message: org=0 home -> assistant.py (account router) when present; org=N -> the company's
    loopcontroller (which applies the consent + provider gate before any model call). org=0 is never sent
    to thread_for_org(tid, 0); it resolves to a real company first."""
    if not org_id:
        a = _assistant()
        if a is not None and hasattr(a, "say"):
            return a.say(tid, msg)
        org_id = _home_org(tid)
        if not org_id:
            return {"error": "Create a company first — open My companies to start one."}
    return loopcontroller.say(tid, loopcontroller.thread_for_org(tid, org_id), msg)


def _ctl_choose(tid, org_id, option_id):
    if not org_id:
        a = _assistant()
        if a is not None and hasattr(a, "choose"):
            return a.choose(tid, option_id)
        org_id = _home_org(tid)
        if not org_id:
            return {"error": "Create a company first — open My companies to start one."}
    return loopcontroller.choose(tid, loopcontroller.thread_for_org(tid, org_id), option_id)


def _system_agents():
    """Tier-A system roles — the standing org hierarchy, surfaced READ-ONLY in the Agents view. Sourced
    from the real role manifest (orgview.ORG); 'controller' is the sole spawner
    (control-plane/roles/controller.yaml can_spawn:true). None are tenant-editable."""
    return [{"role": role, "title": title, "reports_to": reports_to,
             "can_spawn": role == "controller"}
            for role, title, reports_to in orgview.ORG]


def _team(tid):
    """Area 12 — org/team. Minimal but honest: the owner + any roles the platform knows; RBAC is roadmap."""
    plan = "free"
    try:
        plan, _ = billing._plan_of(tid)
    except Exception:
        pass
    return {"members": [{"id": tid, "role": "owner", "status": "active"}],
            "plan": plan, "seats_note": "Multi-seat RBAC is on the roadmap (Team tier)."}


# ---- routing tables: path -> (callable taking tid, needs_tid) -----------------------------------------
def _build(tid, body):
    """Start a governed build (reuses the front door's consent gate + quota + factory thread)."""
    if not consent.require_consent(tid):
        return {"error": "consent_required", "consent": consent.state(tid)}
    q = billing.quota(tid)
    if not q["within_quota"]:
        return {"error": f"quota reached ({q['builds']}) — upgrade your plan"}
    raw = (body.get("name") or "app").strip().lower().replace(" ", "-")[:24] or "app"
    product = f"{tid.replace('t-', '')[:6]}-{raw}"
    charter = body.get("charter") or "Build a small, well-tested product."
    kind = body.get("kind", "lib")
    threading.Thread(target=frontdoor._run_build, args=(tid, product, charter, kind), daemon=True).start()
    return {"product": product, "status": "building"}


def _build_template(tid, body):
    t = templatesview.get(body.get("slug", ""))
    if not t or t.get("error"):
        return {"error": "unknown template"}
    return _build(tid, {"name": t["slug"], "charter": templatesview.charter_for(t["slug"]), "kind": t["kind"]})


GETS = {
    "/api/cockpit": lambda tid, q: cockpit.cockpit(tid, int(q.get("org", ["0"])[0] or 0)),
    "/api/projects": lambda tid, q: {"projects": projectsview.list_projects(tid, int(q.get("org", ["0"])[0] or 0))},
    "/api/project": lambda tid, q: projectsview.project_detail(tid, q.get("product", [""])[0]),
    "/api/fleet": lambda tid, q: _fleet(tid, int(q.get("org", ["0"])[0] or 0)),
    "/api/observability": lambda tid, q: traceview.overview(tid),
    "/api/runs": lambda tid, q: {"runs": traceview.runs(tid)},
    "/api/replay": lambda tid, q: traceview.replay(tid, q.get("run_id", [""])[0]),
    "/api/approvals": lambda tid, q: approvals.inbox(tid),
    "/api/integrations": lambda tid, q: {"integrations": integrationsview.status(tid)},
    "/api/billing": lambda tid, q: billingview.billing_view(tid),
    "/api/forecast": lambda tid, q: forecast.forecast(tid),
    "/api/health": lambda tid, q: cockpit.health(tid, int(q.get("org", ["0"])[0] or 0)),
    "/api/company": lambda tid, q: cockpit.company_summary(tid, int(q.get("org", ["0"])[0] or 0)),
    "/api/comms_graph": lambda tid, q: cockpit.comms_graph(tid, int(q.get("org", ["0"])[0] or 0)),
    "/api/org": lambda tid, q: orgview.orgchart(tid),
    "/api/livestatus": lambda tid, q: {"products": livestatus.live_status(tid)},
    "/api/chat/history": lambda tid, q: {"messages": orchestrator.history(tid, int(q.get("thread", ["0"])[0] or 0))},
    "/api/providers": lambda tid, q: {"providers": tenantproviders.list_providers(tid)},
    "/api/onboarding": lambda tid, q: onboarding.state(tid),
    "/api/quality": lambda tid, q: {"products": qualityview.summary(tid)},
    "/api/quality/product": lambda tid, q: qualityview.verdict(tid, q.get("product", [""])[0]),
    "/api/estimate": lambda tid, q: estimate.estimate(q.get("kind", ["lib"])[0]),
    "/api/budgets": lambda tid, q: {"budgets": projbudget.list_budgets(tid)},
    "/api/versions": lambda tid, q: {"versions": versions.versions(tid, q.get("product", [""])[0])},
    "/api/help/topics": lambda tid, q: {"topics": helpagent.topics()},
    "/api/agents": lambda tid, q: {"agents": customagents.list_agents(tid), "roles": list(customagents.ALLOWED_ROLES), "system": _system_agents()},
    "/api/orgs": lambda tid, q: {"orgs": orgsmod.list_orgs(tid)},
    "/api/portfolio": lambda tid, q: crossorgview.portfolio(tid),
    "/api/portfolio/analytics": lambda tid, q: crossorgview.analytics(tid),
    "/api/portfolio/failures": lambda tid, q: crossorgview.failures(tid),
    "/api/design": lambda tid, q: {"gallery": designview.gallery(tid, str(int(q.get("org", ["0"])[0] or 0))), "surfaces": designview.surfaces()},
    "/api/agentfeatures": lambda tid, q: {"features": agentfeatures.catalog(), "categories": agentfeatures.categories(), "triggers": agentfeatures.triggers()},
    "/api/agentfeatures/recommend": lambda tid, q: agentfeatures.recommend(q.get("need", [""])[0]),
    "/api/controller/state": lambda tid, q: _controller_state(tid, int(q.get("org", ["0"])[0] or 0)),
    "/api/xorg": lambda tid, q: {"ops": crossorg.list_ops(tid)},
    "/api/team": lambda tid, q: _team(tid),
    "/api/settings": lambda tid, q: settingsview.settings(tid),
    "/api/templates": lambda tid, q: {"templates": templatesview.gallery(), "categories": templatesview.categories()},
    "/api/notifications": lambda tid, q: {"unread": notifications.unread_count(tid), "feed": notifications.feed(tid)},
    "/api/status": lambda tid, q: statuspage.status(),
}
POSTS = {
    "/api/build": lambda tid, q, b: _build(tid, b),
    "/api/build_template": lambda tid, q, b: _build_template(tid, b),
    "/api/chat/new": lambda tid, q, b: {"thread": orchestrator.start_thread(tid)},
    "/api/chat/say": lambda tid, q, b: orchestrator.say(tid, int(b.get("thread") or 0), b.get("message", "")),
    "/api/chat/confirm": lambda tid, q, b: orchestrator.confirm(tid, int(b.get("thread") or 0)),
    "/api/providers/add": lambda tid, q, b: tenantproviders.add_key(tid, b.get("provider", ""), b.get("key", "")),
    "/api/providers/connect": lambda tid, q, b: tenantproviders.connect(tid, b.get("provider", ""), b.get("mode", "api_key"), b.get("key")),
    "/api/providers/subscription/start": lambda tid, q, b: tenantproviders.start_subscription_login(b.get("provider", "")),
    "/api/providers/subscription/status": lambda tid, q, b: tenantproviders.subscription_status(b.get("provider", "")),
    "/api/providers/remove": lambda tid, q, b: tenantproviders.remove_key(tid, b.get("provider", "")),
    "/api/providers/priority": lambda tid, q, b: tenantproviders.set_priority(tid, b.get("order", [])),
    "/api/onboarding/advance": lambda tid, q, b: onboarding.advance(tid, b.get("step", "")),
    "/api/onboarding/skip": lambda tid, q, b: onboarding.skip(tid),
    "/api/budget/set": lambda tid, q, b: projbudget.set_budget(tid, b.get("product", ""), float(b.get("cap_usd", 0) or 0)),
    "/api/versions/snapshot": lambda tid, q, b: versions.snapshot(tid, b.get("product", ""), b.get("label", "")),
    "/api/versions/rollback": lambda tid, q, b: versions.rollback(tid, b.get("product", ""), int(b.get("version") or 0)),
    "/api/account/export": lambda tid, q, b: account.export(tid),
    "/api/account/delete": lambda tid, q, b: account.delete(tid, confirm=bool(b.get("confirm"))),
    "/api/help/ask": lambda tid, q, b: helpagent.ask(tid, b.get("question", "")),
    "/api/orgs/new": lambda tid, q, b: orgsmod.create(tid, b.get("name", ""), b.get("vision", "")),
    "/api/controller/say": lambda tid, q, b: _ctl_say(tid, int(b.get("org") or 0), b.get("message", "")),
    "/api/controller/choose": lambda tid, q, b: _ctl_choose(tid, int(b.get("org") or 0), int(b.get("option_id") or 0)),
    "/api/controller/cancel": lambda tid, q, b: _ctl_cancel(tid, int(b.get("org") or 0)),
    "/api/design/decide": lambda tid, q, b: designview.decide(tid, str(int(b.get("org") or 0)), int(b.get("id") or 0), b.get("status", "approved")),
    "/api/xorg/propose": lambda tid, q, b: crossorg.propose(tid, b.get("kind", "steal_feature"), int(b.get("source") or 0), int(b.get("target") or 0) or None, b.get("feature")),
    "/api/agents/create": lambda tid, q, b: customagents.define(tid, b.get("name", ""), b.get("instructions", ""), b.get("role", "research-growth"), b.get("trigger", "manual"), int(b.get("interval_s") or 0) or None, b.get("output", "report"), b.get("product")),
    "/api/agents/run": lambda tid, q, b: customagents.run_now(tid, int(b.get("id") or 0)),
    "/api/agents/toggle": lambda tid, q, b: customagents.toggle(tid, int(b.get("id") or 0), bool(b.get("enabled"))),
    "/api/agents/delete": lambda tid, q, b: customagents.delete(tid, int(b.get("id") or 0)),
    "/api/control": lambda tid, q, b: cockpit.control(tid, b.get("product", ""), b.get("action", "")),
    "/api/approvals/decide": lambda tid, q, b: approvals.decide(tid, b.get("kind"), b.get("ref"), b.get("verdict")),
    "/api/integrations/connect": lambda tid, q, b: integrationsview.connect(tid, b.get("slug", ""), b.get("secret")),
    "/api/integrations/disconnect": lambda tid, q, b: integrationsview.disconnect(tid, b.get("slug", "")),
    "/api/billing/plan": lambda tid, q, b: billingview.change_plan(tid, b.get("plan", "")),
    "/api/settings/pref": lambda tid, q, b: settingsview.set_pref(tid, b.get("category"), b.get("in_app", True), b.get("email", True), b.get("push", False)),
    "/api/settings/consent": lambda tid, q, b: settingsview.set_consent(tid, b.get("accept", True)),
    "/api/notifications/read": lambda tid, q, b: {"read": notifications.mark_read(tid, b.get("id"))},
    "/api/byok": lambda tid, q, b: ({"ok": bool(b.get("key"))} if not b.get("key") else
                                    (vault.put_secret("byo_llm_key", f"tenant:{tid}", "prod", ["builder", "factory"], b["key"]) or {"ok": True})),
}

PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>agent-os · console</title><style>
:root{--bg:#0b0d12;--bg2:#0f1117;--panel:#14171f;--panel2:#191d27;--hover:#1c212c;--line:#232834;--line2:#2e3542;
--tx:#e7eaf0;--tx2:#b3bbc9;--mut:#8b93a3;--accent:#6e8bff;--accent2:#8aa1ff;--asoft:rgba(110,139,255,.14);--aring:rgba(110,139,255,.45);--atext:#aebcff;
--g:#3ecf8e;--gsoft:rgba(62,207,142,.13);--gtext:#6fe3ad;--y:#e3b341;--ysoft:rgba(227,179,65,.13);--ytext:#f0cd6e;--r:#f06363;--rsoft:rgba(240,99,99,.13);--rtext:#ff8a8a;
--mono:ui-monospace,"SF Mono",Menlo,monospace}
*{box-sizing:border-box}*{scrollbar-width:thin;scrollbar-color:#2a3140 transparent}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:#262c38;border-radius:6px;border:2px solid transparent;background-clip:content-box}
::selection{background:rgba(110,139,255,.3)}
body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.55 "Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.app{display:flex;min-height:100vh}
.side{width:228px;background:var(--bg2);border-right:1px solid var(--line);padding:16px 12px;position:sticky;top:0;height:100vh;overflow:auto;display:flex;flex-direction:column}
.brand{display:flex;align-items:center;gap:8px;font-weight:680;font-size:15px;letter-spacing:-.01em;padding:4px 10px 14px}.brand .mk{color:var(--accent)}
.nsec{font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--mut);padding:14px 10px 6px}
.nav a{display:flex;gap:10px;align-items:center;padding:8px 10px;border-radius:8px;color:var(--tx2);text-decoration:none;cursor:pointer;font-size:13px;font-weight:500;transition:background .12s,color .12s}
.nav a .ico{width:16px;text-align:center;opacity:.85;flex:none}
.nav a:hover{background:var(--hover);color:var(--tx)}.nav a.on{background:var(--asoft);color:var(--atext);font-weight:600;box-shadow:inset 0 0 0 1px rgba(110,139,255,.18)}
.nav a .b{margin-left:auto;background:var(--asoft);color:var(--atext);border-radius:999px;font-size:10px;font-weight:600;min-width:18px;height:18px;padding:0 6px;display:inline-flex;align-items:center;justify-content:center}
.colmain{flex:1;display:flex;flex-direction:column;min-width:0}
.topbar{display:flex;align-items:center;gap:12px;height:56px;padding:0 24px;background:rgba(15,17,23,.72);backdrop-filter:saturate(160%) blur(10px);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}
.topbar .sp{flex:1}.topbar .chip{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--tx2);background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:5px 12px;cursor:pointer}
.topbar .chip .dot{width:8px;height:8px;border-radius:50%}
/* a11y: operational state is NOT carried by hue alone — each state also has a distinct SHAPE (ok=solid disc, warn=hollow ring, bad=square) so it reads for color-blind users; screen readers get a verdict aria-label set in JS. */
.dot.ok{background:var(--g)}.dot.warn{background:radial-gradient(circle at 50% 50%,transparent 1.4px,var(--y) 1.9px)}.dot.bad{background:var(--r);border-radius:2px}
.tbtn{position:relative;background:transparent;border:none;color:var(--tx2);font-size:16px;cursor:pointer;padding:6px 8px;border-radius:8px}.tbtn:hover{background:var(--hover);color:var(--tx)}
.tbtn .nb{position:absolute;top:0;right:0;background:var(--r);color:#fff;border-radius:999px;font-size:9px;font-weight:700;min-width:15px;height:15px;padding:0 4px;display:inline-flex;align-items:center;justify-content:center}
.main{flex:1;padding:26px 30px;max-width:1080px;width:100%;margin:0 auto}
h1{font-size:22px;font-weight:650;letter-spacing:-.02em;margin:0 0 3px}.sub{color:var(--mut);margin:0 0 20px;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(122px,1fr));gap:10px;margin-bottom:18px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 16px;box-shadow:0 1px 2px rgba(0,0,0,.25)}
.kpi b{display:block;font-size:25px;font-weight:680;letter-spacing:-.02em;line-height:1.1}.kpi span{color:var(--mut);font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.25)}
#view{animation:rise .18s cubic-bezier(.4,0,.2,1)}
@keyframes rise{from{transform:translateY(3px)}to{transform:none}}
@media(prefers-reduced-motion:reduce){#view{animation:none}}
.card h2{font-size:11px;text-transform:uppercase;letter-spacing:.08em;font-weight:600;color:var(--tx2);margin:0 0 12px}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:52px 20px;color:var(--tx2)}
.empty .ic{font-size:30px;opacity:.55;margin-bottom:10px}.empty h3{font-size:15px;font-weight:600;color:var(--tx);margin:0 0 4px}.empty p{color:var(--mut);margin:0 0 16px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 14px}.chip-s{padding:7px 12px;border-radius:999px;background:var(--panel2);border:1px solid var(--line2);color:var(--tx2);cursor:pointer;font-size:12.5px}.chip-s:hover{border-color:var(--accent);color:var(--tx)}
.pill.off{background:rgba(255,255,255,.04);color:var(--mut)}.int-ic{width:28px;height:28px;border-radius:7px;background:var(--panel2);border:1px solid var(--line2);display:inline-flex;align-items:center;justify-content:center;font-size:14px;flex:none}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.spread{justify-content:space-between}
.item{border-top:1px solid var(--line);padding:12px 0}.item:first-child{border-top:none;padding-top:0}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;padding:3px 9px;border-radius:999px;background:rgba(255,255,255,.05);color:var(--tx2)}
.pill.ok{background:var(--gsoft);color:var(--gtext)}.pill.bad{background:var(--rsoft);color:var(--rtext)}.pill.warn{background:var(--ysoft);color:var(--ytext)}.pill.accent{background:var(--asoft);color:var(--atext)}
.stages{display:flex;gap:4px;margin:8px 0;max-width:280px}.st{flex:1;height:6px;border-radius:3px;background:#1c2733}.st.ok{background:var(--g)}
/* a11y: failed stage isn't red-only — it also carries a diagonal hatch texture so pass/fail is distinguishable without hue; each bar gets a "stage: passed/failed/pending" title+aria-label in stages(). */
.st.bad{background:var(--r);background-image:repeating-linear-gradient(45deg,rgba(0,0,0,.34) 0,rgba(0,0,0,.34) 1.5px,transparent 1.5px,transparent 3px)}
button{display:inline-flex;align-items:center;gap:6px;background:var(--panel2);border:1px solid var(--line2);color:var(--tx);border-radius:10px;padding:8px 14px;cursor:pointer;font-size:13px;font-weight:550;line-height:1;transition:background .15s,border-color .15s,transform .05s}
button:hover{background:var(--hover);border-color:#3a4150}button:active{transform:translateY(.5px)}
button.pri{background:var(--accent);border:1px solid var(--accent);color:#0b0d12;font-weight:600;box-shadow:inset 0 1px 0 rgba(255,255,255,.12)}button.pri:hover{background:var(--accent2);border-color:var(--accent2)}
input,select,textarea{width:100%;font-family:inherit;font-size:13.5px;color:var(--tx);background:var(--bg2);border:1px solid var(--line2);border-radius:10px;padding:9px 11px;transition:border-color .15s,box-shadow .15s}
input::placeholder,textarea::placeholder{color:var(--mut)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);background:var(--panel);box-shadow:0 0 0 3px var(--asoft)}
textarea{min-height:96px;resize:vertical}label{display:block;font-size:12px;font-weight:550;color:var(--tx2);margin:14px 0 5px}
textarea.chatbox{min-height:76px;max-height:200px;resize:none;line-height:1.5;padding:11px 14px;border-radius:14px;overflow-y:auto}
.composer{align-items:flex-end}
table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);padding:0 10px 10px;border-bottom:1px solid var(--line2)}
td{padding:11px 10px;color:var(--tx2);border-bottom:1px solid var(--line)}td:first-child{color:var(--tx);font-weight:500}tbody tr{transition:background .12s}tbody tr:hover{background:var(--hover)}tbody tr:last-child td{border-bottom:none}
.muted{color:var(--mut)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:820px){html,body,.app{max-width:100%;overflow-x:hidden}.grid{grid-template-columns:1fr}.side{width:64px}.side .lbl,.side .nsec{display:none}.side .brand{font-size:0;justify-content:center;padding:4px 0 14px}.side .brand .mk{font-size:18px}.topbar{padding:0 12px;gap:8px;flex-wrap:wrap;height:auto;min-height:56px}.topbar .chip{padding:5px 9px;max-width:42vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.main{padding:18px 14px}}
.tile{background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:14px}
.chat{display:flex;flex-direction:column;gap:14px}.msg{display:flex;max-width:80%}.msg.me{align-self:flex-end;justify-content:flex-end}
.chat{display:flex;flex-direction:column;gap:12px}
.msg{display:flex;max-width:82%}.msg.me{align-self:flex-end;justify-content:flex-end}.msg.ai{align-self:flex-start}
.bubble{display:inline-block;max-width:100%;padding:11px 15px;border-radius:18px;font-size:14px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere}
.msg.ai .bubble{background:var(--panel2);border:1px solid var(--line);border-bottom-left-radius:6px}
.msg.me .bubble{background:var(--accent);color:#0b0d12;font-weight:500;border-bottom-right-radius:6px;box-shadow:0 2px 8px -2px rgba(110,139,255,.4)}
code{font-family:var(--mono);font-size:12.5px;color:var(--atext);background:var(--asoft);padding:1px 5px;border-radius:5px}
:focus-visible{outline:none;box-shadow:0 0 0 3px var(--aring)}
.skel{background:linear-gradient(90deg,#171b24 25%,#1d222d 50%,#171b24 75%);background-size:200% 100%;animation:shim 1.4s infinite;border-radius:6px;height:14px;margin:8px 0}@keyframes shim{to{background-position:-200% 0}}
.menu{position:absolute;right:20px;top:52px;background:var(--panel2);border:1px solid var(--line2);border-radius:12px;box-shadow:0 12px 36px -10px rgba(0,0,0,.6);padding:8px;min-width:240px;z-index:30}
.menu a{display:block;padding:8px 10px;border-radius:8px;color:var(--tx2);text-decoration:none;cursor:pointer;font-size:13px}.menu a:hover{background:var(--hover);color:var(--tx)}
.orgsw{position:relative;display:inline-flex}
#orgmenu{position:absolute;left:0;top:38px;right:auto;min-width:280px;max-width:340px}
.orgsearch{margin-bottom:6px}
.orglist{max-height:300px;overflow:auto;display:flex;flex-direction:column}
.orgopt{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;color:var(--tx2);cursor:pointer;font-size:13px}
.orgopt:hover{background:var(--hover);color:var(--tx)}.orgopt.on{background:var(--asoft);color:var(--atext);font-weight:600}
.orgopt .oname{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.orgopt .ock{color:var(--gtext)}
.orgopt .ovis{color:var(--mut);font-size:11px;font-weight:400}
.orgnew{display:block;margin-top:6px;padding:8px 10px;border-top:1px solid var(--line);border-radius:0 0 8px 8px;color:var(--atext);cursor:pointer;font-size:13px;font-weight:550}.orgnew:hover{background:var(--hover)}
.pwwrap{position:relative}.pwwrap input{padding-right:62px}
.pwtoggle{position:absolute;right:6px;top:50%;transform:translateY(-50%);background:transparent;border:none;color:var(--mut);font-size:12px;font-weight:600;padding:5px 8px;border-radius:7px;cursor:pointer}
.pwtoggle:hover{background:var(--hover);border:none;color:var(--tx2)}
.pwhint{margin-top:8px;font-size:12px;min-height:0}.pwbar{height:5px;border-radius:3px;background:var(--line2);overflow:hidden;margin-bottom:6px}
.pwbar span{display:block;height:100%;width:0;border-radius:3px;transition:width .2s,background .2s}.pwbar span.bad{background:var(--r)}.pwbar span.warn{background:var(--y)}.pwbar span.ok{background:var(--g)}
.pwreq{display:flex;justify-content:space-between;align-items:center;color:var(--mut)}.pwreq .met{color:var(--gtext)}.pwlvl{font-weight:600}
.note{font-size:12.5px}.note.err{color:var(--rtext)}
.linkbtn{background:none;border:none;padding:2px 0;color:var(--atext);font-size:inherit;font-weight:550;cursor:pointer;border-radius:4px}.linkbtn:hover{background:none;border:none;text-decoration:underline}
input[aria-invalid=true]{border-color:var(--r);box-shadow:0 0 0 3px var(--rsoft)}
.spin{width:13px;height:13px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;display:inline-block;animation:sp .6s linear infinite;vertical-align:-2px}@keyframes sp{to{transform:rotate(360deg)}}
button:disabled{opacity:.6;cursor:not-allowed;pointer-events:none}
.authwrap{display:grid;grid-template-columns:minmax(0,1fr) 412px;gap:38px;align-items:center}
.hero{min-width:0}.hero .brand{font-size:20px;margin:0 0 18px}
.heroh{font-size:31px;line-height:1.12;letter-spacing:-.022em;font-weight:680;color:var(--tx);margin:0 0 12px}
.herosub{color:var(--tx2);font-size:15px;line-height:1.55;margin:0 0 24px;max-width:48ch}
.steps{list-style:none;margin:0 0 22px;padding:0;display:flex;flex-direction:column;gap:15px}
.steps li{display:flex;gap:12px;align-items:flex-start}
.stepn{flex:none;width:26px;height:26px;border-radius:999px;background:var(--asoft);color:var(--atext);font-weight:700;font-size:13px;display:inline-flex;align-items:center;justify-content:center;margin-top:1px}
.steps b{display:block;font-size:14px;font-weight:600;color:var(--tx);line-height:1.3}.steps span{display:block;color:var(--mut);font-size:13px;line-height:1.45;margin-top:2px}
.example{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 18px;box-shadow:0 1px 2px rgba(0,0,0,.25)}
.example .exhead{display:flex;align-items:center;gap:8px;margin-bottom:10px}.example .exh{font-size:13.5px;font-weight:600;color:var(--tx)}
.exline{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--tx2);padding:3px 0}.exline .exok{color:var(--gtext);font-weight:700}.exline code{margin-left:auto}
.trust{display:flex;flex-wrap:wrap;gap:8px;list-style:none;margin:0;padding:0}
.trust li{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--tx2);background:var(--panel2);border:1px solid var(--line2);border-radius:999px;padding:5px 11px}.trust li .ic{color:var(--gtext);font-weight:700}
@media(max-width:880px){#signin{max-width:440px!important}.authwrap{grid-template-columns:1fr;gap:24px}.heroh{font-size:25px}.herosub{margin-bottom:20px}.hero .example,.hero .trust{display:none}}
.menubtn{display:none}.navscrim{display:none}
@media(max-width:700px){
 .menubtn{display:inline-flex}
 .side{position:fixed;left:0;top:0;width:248px;height:100vh;z-index:60;transform:translateX(-100%);transition:transform .2s ease;box-shadow:0 0 50px rgba(0,0,0,.6)}
 .side .lbl,.side .nsec{display:block}.side .brand{font-size:15px;justify-content:flex-start;padding:4px 10px 14px}.side .brand .mk{font-size:15px}
 .app.navopen .side{transform:none}
 .navscrim{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:55}.app.navopen .navscrim{display:block}
 .main{padding:18px 14px}.topbar{padding:0 10px}
}
</style></head><body>
<div id=splash style="display:none;position:fixed;inset:0;align-items:center;justify-content:center;flex-direction:column;gap:14px;background:var(--bg);z-index:50;color:var(--mut)">
  <div class=brand style="font-size:22px"><span class=mk>⬡</span> agent-os</div><div style="font-size:13px">Reconnecting…</div></div>
<div id=signin style="display:none;max-width:920px;margin:8vh auto;padding:0 18px">
 <div class=authwrap>
  <div class=hero>
   <div class=brand><span class=mk>⬡</span> agent-os</div>
   <h1 class=heroh>Be the CEO of a company of AI agents.</h1>
   <p class=herosub>Set the vision. Specialized agents plan, build, review, and ship your software — you approve the calls that matter, they do the work.</p>
   <ol class=steps>
    <li><span class=stepn aria-hidden=true>1</span><div><b>Describe what to build</b><span>Tell your agents the goal in plain English — no tickets or specs to write.</span></div></li>
    <li><span class=stepn aria-hidden=true>2</span><div><b>Agents plan, build &amp; review</b><span>They write the code and check each other's work — you watch it happen live.</span></div></li>
    <li><span class=stepn aria-hidden=true>3</span><div><b>You approve &amp; ship</b><span>Sign off on the big calls. It ships to your repo, on your stack.</span></div></li>
   </ol>
   <div class=example role=group aria-label="Example build: checkout-api, shipped">
    <div class=exhead><span class=exh>checkout-api</span><span class="pill ok" style="margin-left:auto">shipped</span></div>
    <div class=stages style="max-width:none;margin:0 0 10px"><span class="st ok"></span><span class="st ok"></span><span class="st ok"></span><span class="st ok"></span></div>
    <div class=exline><span class=exok aria-hidden=true>✓</span> Spec &amp; plan approved <code>$0.04</code></div>
    <div class=exline><span class=exok aria-hidden=true>✓</span> Built 14 files · tests passing <code>$0.21</code></div>
    <div class=exline><span class=exok aria-hidden=true>✓</span> Reviewed &amp; merged to main <code>$0.07</code></div>
   </div>
   <ul class=trust>
    <li><span class=ic aria-hidden=true>✓</span> No card to start</li>
    <li><span class=ic aria-hidden=true>✓</span> Your code, your repo</li>
    <li><span class=ic aria-hidden=true>✓</span> Every action logged &amp; costed</li>
   </ul>
  </div>
  <div class=card>
  <form id=su_signup onsubmit="signUp();return false" novalidate>
    <h2 style="text-transform:none;font-size:16px;letter-spacing:0;color:var(--tx);margin:0 0 4px">Create your account</h2>
    <p class=muted style="margin:0 0 10px">Free to start — no card needed. You'll create your companies (orgs) once you're in.</p>
    <label for=su_name>Your name</label><input id=su_name autocomplete=name autofocus placeholder="Jane Doe">
    <label for=su_email>Email</label><input id=su_email type=email autocomplete=email placeholder="you@example.com" onblur="vEmail('su')" aria-describedby=su_note>
    <label for=su_pw>Password</label>
    <div class=pwwrap><input id=su_pw type=password autocomplete=new-password placeholder="at least 8 characters" oninput="pwStrength()" onblur="vPw()" aria-describedby="su_pwhint su_note"><button type=button class=pwtoggle aria-label="Show password" aria-pressed=false onclick="pwToggle('su_pw',this)">Show</button></div>
    <div id=su_pwhint class=pwhint aria-live=polite></div>
    <label for=su_pw2>Confirm password</label>
    <div class=pwwrap><input id=su_pw2 type=password autocomplete=new-password placeholder="re-enter your password" onblur="vPw2()" aria-describedby=su_note><button type=button class=pwtoggle aria-label="Show password" aria-pressed=false onclick="pwToggle('su_pw2',this)">Show</button></div>
    <div style="margin-top:14px"><button type=submit id=su_btn class=pri style=width:100%>Create account</button></div>
    <div id=su_note class="note muted" role=alert aria-live=polite style="margin-top:10px"></div>
    <p class=muted style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">Already have an account? <button type=button class=linkbtn onclick="suTab('in')">Sign in</button></p>
  </form>
  <form id=su_signin style=display:none onsubmit="signIn();return false" novalidate>
    <h2 style="text-transform:none;font-size:16px;letter-spacing:0;color:var(--tx);margin:0 0 4px">Sign in</h2>
    <label for=si_email>Email</label><input id=si_email type=email autocomplete=email placeholder="you@example.com" onblur="vEmail('si')" aria-describedby=si_note>
    <label for=si_pw>Password</label>
    <div class=pwwrap><input id=si_pw type=password autocomplete=current-password placeholder="your password" aria-describedby=si_note><button type=button class=pwtoggle aria-label="Show password" aria-pressed=false onclick="pwToggle('si_pw',this)">Show</button></div>
    <div style="margin-top:6px"><button type=button class=linkbtn onclick=forgotPw()>Forgot password?</button></div>
    <div style="margin-top:14px"><button type=submit id=si_btn class=pri style=width:100%>Sign in</button></div>
    <div id=si_note class="note muted" role=alert aria-live=polite style="margin-top:10px"></div>
    <p class=muted style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px"><button type=button class=linkbtn onclick="suTab('up')">← Create an account</button></p>
  </form>
  <form id=su_verify style=display:none onsubmit="verifyEmail();return false" novalidate>
    <h2 style="text-transform:none;font-size:16px;letter-spacing:0;color:var(--tx);margin:0 0 4px">Verify your email</h2>
    <p class=muted id=ve_intro style="margin:0 0 10px">We sent a 6-digit code to your email — enter it below.</p>
    <div id=ve_dev class=note style="display:none;background:var(--asoft);border:1px solid var(--line2);border-radius:10px;padding:10px 12px;margin:0 0 12px;color:var(--atext)"></div>
    <label for=ve_code>6-digit code</label>
    <input id=ve_code inputmode=numeric autocomplete=one-time-code maxlength=6 placeholder="123456" aria-describedby=ve_note>
    <div style="margin-top:14px"><button type=submit id=ve_btn class=pri style=width:100%>Verify</button></div>
    <div id=ve_note class="note muted" role=alert aria-live=polite style="margin-top:10px"></div>
    <p class=muted style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">Didn't get it? <button type=button class=linkbtn onclick=resendCode()>Resend code</button></p>
  </form></div>
 </div>
</div>
<div class=app id=app style="display:none">
<div class=navscrim onclick="toggleNav(false)"></div>
<div class=side><div class=brand><span class=mk>⬡</span> agent-os</div><div class=nav id=nav style=flex:1></div>
  <a class=nav-foot id=statusfoot onclick="go('status')" style="display:flex;gap:10px;align-items:center;padding:8px 10px;border-radius:8px;color:var(--tx2);cursor:pointer;font-size:13px;border-top:1px solid var(--line);margin-top:8px"><span class="dot ok" id=statusdot role=img title="Status" aria-label="Status"></span><span class=lbl>Status</span></a>
</div>
<div class=colmain>
  <div class=topbar><button class="tbtn menubtn" aria-label="Open menu" onclick="toggleNav()">☰</button><span class=title id=tbtitle>Cockpit</span>
    <span class=orgsw>
      <span class=chip id=orgchip onclick="toggleOrgSw()" title="Switch company" aria-haspopup=listbox aria-expanded=false style=cursor:pointer><span id=orgname>—</span> ▾</span>
      <div class=menu id=orgmenu role=listbox aria-label="Companies" style=display:none>
        <input id=orgsearch class=orgsearch aria-label="Search companies" placeholder="Search companies…" oninput=renderOrgSw() onkeydown="if(event.key==='Escape')toggleOrgSw(false)">
        <div id=orglist class=orglist></div>
        <a class=orgnew onclick="toggleOrgSw(false);go('orgs')">+ New company</a>
      </div>
    </span>
    <span class=sp></span>
    <span class=chip id=spendchip onclick="go('billing')"><span class="dot ok" id=spenddot role=img title="Spend & quota" aria-label="Spend & quota"></span><span id=spendtxt>—</span></span>
    <button class=tbtn onclick="go('notifications')" title=Notifications>◔<span class=nb id=bellbadge style=display:none></span></button>
    <button class=tbtn onclick="go('help')" title=Help>?</button>
    <button class=tbtn onclick="toggleAcct()" title=Account>⋯</button>
    <div class=menu id=acctmenu style=display:none><a onclick="go('settings')">Settings</a><a onclick="go('team')">Org chart</a><a onclick="go('billing')">Billing &amp; plan</a><a onclick="go('settings')">AI consent &amp; data</a><a onclick="go('status')">Status</a><a onclick=signOut()>Sign out</a></div>
  </div>
  <div class=main><div id=view></div></div>
</div>
</div>
<script>
const NAV=[
 ['Workspace',[['controller','Assistant','🧭'],['cockpit','Cockpit','◧'],['projects','Projects','▤'],['design','Design','🎨'],['approvals','Approvals','✓'],['activity','Activity','◴']]],
 ['Build',[['agents','Agents','🤖'],['agentic','Agentic features','⚡'],['templates','Templates','▦']]],
 ['Portfolio',[['orgs','My companies','🏢'],['portfolio','Portfolio','◎']]],
 ['Account',[['billing','Billing','▣'],['providers','Providers','🔌'],['integrations','Integrations','⌁']]],
];
const LABEL={controller:'Assistant',chat:'Quick build',build:'New build',agents:'Agents',templates:'Templates',cockpit:'Cockpit',projects:'Projects',design:'Design',agentic:'Agentic features',approvals:'Approvals',activity:'Activity',orgs:'My companies',portfolio:'Portfolio',billing:'Billing',providers:'Providers',integrations:'Integrations',notifications:'Notifications',help:'Help',team:'Org',settings:'Settings',status:'Status'};
const $=s=>document.querySelector(s);let TOK=localStorage.getItem('aos_tenant')||'';let CUR='cockpit';let BADGES={};
let ORG=parseInt(localStorage.getItem('aos_org')||'0')||0;let ORGS=[];let PROVIDER_OK=true;let PEND_EMAIL='';
async function loadOrgs(){try{const d=await get('/api/orgs');ORGS=d.orgs||[];
  if(ORG && !ORGS.some(o=>o.org_id==ORG)){ORG=0;localStorage.removeItem('aos_org');}   // stale/deleted org -> home (org=0)
  setOrgName();}catch(e){}}
function setOrgName(){const cur=ORGS.find(o=>o.org_id==ORG);if($('#orgname'))$('#orgname').textContent=ORG?(cur?cur.name:'company'):'All companies (home)';}
async function loadProviders(){try{const d=await get('/api/providers');PROVIDER_OK=(d.providers||[]).some(p=>p.connected);}catch(e){}}   // subscription login OR api key both count as connected
function toggleOrgSw(force){const m=$('#orgmenu');if(!m)return;const open=force!==undefined?force:(m.style.display==='none');m.style.display=open?'block':'none';const chip=$('#orgchip');if(chip)chip.setAttribute('aria-expanded',open?'true':'false');if(open){renderOrgSw();const s=$('#orgsearch');if(s){s.value='';setTimeout(()=>{try{s.focus()}catch(_){}} ,0)}}}
function renderOrgSw(){const list=$('#orglist');if(!list)return;const q=(($('#orgsearch')||{}).value||'').toLowerCase();
  const opts=[{org_id:0,name:'All companies (home)',vision:'Ask across every company · start a new one'}].concat(ORGS);
  const f=opts.filter(o=>!q||(''+o.name).toLowerCase().includes(q)||(''+(o.vision||'')).toLowerCase().includes(q));
  list.innerHTML=f.length?f.map(o=>`<a class="orgopt${o.org_id==ORG?' on':''}" role=option aria-selected=${o.org_id==ORG} onclick="pickOrg(${o.org_id})"><span class=oname>${esc(o.name)}${o.vision?(' <span class=ovis>· '+esc(o.vision)+'</span>'):''}</span>${o.org_id==ORG?'<span class=ock>✓</span>':''}</a>`).join(''):'<div class=muted style=padding:8px_10px>No matching company</div>';}
function pickOrg(id){ORG=id;if(id)localStorage.setItem('aos_org',id);else localStorage.removeItem('aos_org');   // org=0 = home; org=N = a company
  setOrgName();toggleOrgSw(false);if(window.CTLPOLL){clearInterval(window.CTLPOLL);window.CTLPOLL=null;}   // re-target the controller poll at the new context
  if(VIEWS[CUR])go(CUR);else go('controller');}   // re-render the CURRENT view in place — never navigate away
function switchOrg(id){ORG=id;localStorage.setItem('aos_org',id);setOrgName();go('controller');}   // intentional navigation: "Open" from My orgs lands on the Assistant
function gateError(e){return e==='consent_required'||e==='provider_required';}
function gateKey(r){return r?(r.error||r.blocked):'';}   // same gates surface as 'error' (front door) OR 'blocked' (controller/chat loop) — handle both
function gateNote(e){
 if(e==='provider_required')return 'Connect an AI model to start — <button class=linkbtn onclick="go(\'providers\')">connect a model</button>. It takes one click.';
 if(e==='consent_required')return 'One-time setup: approve AI use before your agents run — <button class=linkbtn onclick="go(\'settings\')">approve AI use</button>.';
 return esc(''+e);}
function H(){return {'Content-Type':'application/json','X-Tenant-Token':TOK}}
function showApp(on){$('#signin').style.display=on?'none':'block';$('#app').style.display=on?'flex':'none';if(!on){const up=($('#su_signup')||{}).style&&$('#su_signup').style.display!=='none';const f=$(up?'#su_name':'#si_email');if(f)setTimeout(()=>{try{f.focus()}catch(_){}},0)}}
function resetSession(){localStorage.removeItem('aos_org');localStorage.removeItem('aos_email');ORG=0;ORGS=[];THREAD=null;PROVIDER_OK=true;CUR='controller'}
function suTab(t){const up=t==='up';$('#su_signup').style.display=up?'block':'none';$('#su_signin').style.display=up?'none':'block';const v=$('#su_verify');if(v)v.style.display='none';setNote(up?'su':'si','');const f=$(up?'#su_name':'#si_email');if(f)setTimeout(()=>{try{f.focus()}catch(_){}},0)}
function emailOK(e){return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e)}
function aInv(id,on){const el=$('#'+id);if(el)el.setAttribute('aria-invalid',on?'true':'false')}
function setNote(p,msg,kind){const n=$('#'+p+'_note');if(!n)return;n.textContent=msg||'';const err=kind==='err';n.classList.toggle('err',err);n.classList.toggle('muted',!err)}
function pwToggle(id,btn){const el=$('#'+id);if(!el)return;const pos=el.selectionStart;const show=el.type==='password';el.type=show?'text':'password';btn.setAttribute('aria-pressed',show?'true':'false');btn.setAttribute('aria-label',show?'Hide password':'Show password');btn.textContent=show?'Hide':'Show';try{el.focus();el.setSelectionRange(pos,pos)}catch(_){}}
function pwStrength(){const el=$('#su_pw');const hint=$('#su_pwhint');if(!el||!hint)return;const pw=el.value||'';const ok8=pw.length>=8;let s=0;if(ok8)s++;if(pw.length>=12)s++;if(/[0-9]/.test(pw))s++;if(/[^A-Za-z0-9]/.test(pw))s++;const lvl=pw.length===0?0:(s<=1?1:(s<=2?2:3));const L={1:'Weak',2:'Fair',3:'Strong'},C={1:'bad',2:'warn',3:'ok'};hint.innerHTML='<div class=pwbar><span class="'+(C[lvl]||'')+'" style="width:'+(lvl*33.4)+'%"></span></div><div class=pwreq><span class="'+(ok8?'met':'')+'">'+(ok8?'✓':'•')+' At least 8 characters</span>'+(lvl?'<span class=pwlvl>'+L[lvl]+' password</span>':'')+'</div>'}
function vEmail(p){const v=($('#'+p+'_email').value||'').trim();if(v&&!emailOK(v)){aInv(p+'_email',true);setNote(p,'That email doesn\'t look right — check for a typo.','err');return false}aInv(p+'_email',false);if(($('#'+p+'_note')||{}).classList&&$('#'+p+'_note').classList.contains('err'))setNote(p,'');return true}
function vPw(){const v=$('#su_pw').value||'';if(v&&v.length<8){aInv('su_pw',true);setNote('su','Password must be at least 8 characters.','err');return false}aInv('su_pw',false);if($('#su_note').classList.contains('err'))setNote('su','');return true}
function vPw2(){const a=$('#su_pw').value||'',b=$('#su_pw2').value||'';if(b&&a!==b){aInv('su_pw2',true);setNote('su','Passwords don\'t match.','err');return false}aInv('su_pw2',false);if($('#su_note').classList.contains('err'))setNote('su','');return true}
function forgotPw(){setNote('si','Password reset isn\'t available yet — email support@agent-os.dev and we\'ll get you back in.');aInv('si_email',false);}
function pend(btn,label){if(!btn)return ()=>{};const html=btn.innerHTML;btn.disabled=true;btn.innerHTML='<span class=spin></span>'+label;return ()=>{btn.disabled=false;btn.innerHTML=html}}
function humanError(raw,ctx){const s=String(raw||'').toLowerCase();
 if(s.includes('already exists')||s.includes('already regist'))return 'This email is already registered — sign in instead.';
 if(s.includes('wrong password')||s.includes('no account'))return 'That email or password doesn\'t match. Check both and try again.';
 if(s.includes('valid email'))return 'That email doesn\'t look right — check for a typo.';
 if(s.includes('8 characters'))return 'Password must be at least 8 characters.';
 return ctx==='signup'?'We couldn\'t create your account — please try again.':'We couldn\'t sign you in — please try again.';}
async function signUp(){
 const email=($('#su_email').value||'').trim();const pw=$('#su_pw').value||'';const pw2=$('#su_pw2').value||'';const name=($('#su_name').value||'').trim();const btn=$('#su_btn');
 if(!email){aInv('su_email',true);setNote('su','Enter your email.','err');$('#su_email').focus();return}
 if(!emailOK(email)){aInv('su_email',true);setNote('su','That email doesn\'t look right — check for a typo.','err');$('#su_email').focus();return}
 if(pw.length<8){aInv('su_pw',true);setNote('su','Password must be at least 8 characters.','err');$('#su_pw').focus();return}
 if(pw!==pw2){aInv('su_pw2',true);setNote('su','Passwords don\'t match.','err');$('#su_pw2').focus();return}
 aInv('su_email',false);aInv('su_pw',false);aInv('su_pw2',false);
 resetSession();
 const restore=pend(btn,'Creating your account…');setNote('su','Creating your account…');
 let r;try{r=await (await fetch('/api/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,email,password:pw})})).json()}
 catch(e){restore();setNote('su','Couldn\'t reach the server — is the console running?','err');return}
 if(r.error){restore();if(/already/.test(r.error)){aInv('su_email',true);$('#su_email').focus()}setNote('su',humanError(r.error,'signup'),'err');return}
 if(r.pending_verification){restore();setNote('su','');showVerify(r.email||email,r.dev_code);return}   // email-verification gate: collect the 6-digit code before entering the app
 restore();setNote('su','');  // re-enable the button & clear the note so a later sign-out→sign-in isn't stuck disabled
 TOK=r.api_token;localStorage.setItem('aos_tenant',TOK);localStorage.setItem('aos_email',r.email||email);
 showApp(true);boot();
}
function showVerify(email,devCode){
 PEND_EMAIL=email;
 $('#su_signup').style.display='none';$('#su_signin').style.display='none';$('#su_verify').style.display='block';
 $('#ve_intro').innerHTML='We sent a 6-digit code to <b>'+esc(email)+'</b> — enter it below.';
 const dev=$('#ve_dev');
 if(devCode){dev.style.display='block';dev.innerHTML='Your code is <b>'+esc(devCode)+'</b><br><span class=muted>(self-hosted: no email service configured, so here\'s your code)</span>'}
 else dev.style.display='none';
 aInv('ve_code',false);setNote('ve','');const c=$('#ve_code');if(c){c.value='';setTimeout(()=>{try{c.focus()}catch(_){}},0)}
}
async function verifyEmail(){
 const code=($('#ve_code').value||'').trim();const btn=$('#ve_btn');
 if(!code){aInv('ve_code',true);setNote('ve','Enter the 6-digit code we emailed you.','err');$('#ve_code').focus();return}
 aInv('ve_code',false);
 const restore=pend(btn,'Verifying…');setNote('ve','Verifying…');
 let r;try{r=await (await fetch('/api/verify-email',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:PEND_EMAIL,code})})).json()}
 catch(e){restore();setNote('ve','Couldn\'t reach the server — is the console running?','err');return}
 if(r.error){restore();aInv('ve_code',true);setNote('ve',r.error,'err');$('#ve_code').focus();return}
 restore();setNote('ve','');
 TOK=r.api_token;localStorage.setItem('aos_tenant',TOK);localStorage.setItem('aos_email',r.email||PEND_EMAIL);
 showApp(true);boot();
}
async function resendCode(){
 setNote('ve','Sending a new code…');
 let r;try{r=await (await fetch('/api/resend-code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:PEND_EMAIL})})).json()}
 catch(e){setNote('ve','Couldn\'t reach the server — is the console running?','err');return}
 if(r.error){setNote('ve',r.error,'err');return}
 if(r.dev_code){const dev=$('#ve_dev');dev.style.display='block';dev.innerHTML='Your code is <b>'+esc(r.dev_code)+'</b><br><span class=muted>(self-hosted: no email service configured, so here\'s your code)</span>'}
 setNote('ve','We sent a new code — check your email.');
}
async function signIn(){
 const email=($('#si_email').value||'').trim();const pw=$('#si_pw').value||'';const btn=$('#si_btn');
 if(!email){aInv('si_email',true);setNote('si','Enter your email.','err');$('#si_email').focus();return}
 if(!pw){aInv('si_pw',true);setNote('si','Enter your password.','err');$('#si_pw').focus();return}
 aInv('si_email',false);aInv('si_pw',false);
 resetSession();
 const restore=pend(btn,'Signing in…');setNote('si','Signing in…');
 let r;try{r=await (await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pw})})).json()}
 catch(e){restore();setNote('si','Couldn\'t reach the server — is the console running?','err');return}
 if(r.error){restore();setNote('si',humanError(r.error,'signin'),'err');return}
 restore();setNote('si','');  // re-enable the button & clear the note so a later sign-out→sign-in isn't stuck disabled
 TOK=r.api_token;localStorage.setItem('aos_tenant',TOK);localStorage.setItem('aos_email',r.email||email);
 showApp(true);boot();
}
function signOut(msg){localStorage.removeItem('aos_tenant');TOK='';clearTimers();resetSession();suTab('in');showApp(false);const n=$('#si_note');if(n)n.textContent=msg||''}
function toggleAcct(){const m=$('#acctmenu');m.style.display=m.style.display==='none'?'block':'none'}
function toggleNav(force){const a=$('#app');if(!a)return;const open=force!==undefined?force:!a.classList.contains('navopen');a.classList.toggle('navopen',open)}   // mobile: slide the sidebar in/out as a drawer
async function get(p){
 if(!TOK){const e=new Error('Your session expired — sign in again with your email and password.');e.kind='auth';throw e}
 let r;try{r=await fetch(p,{headers:H()})}catch(_){const e=new Error('Can\'t reach the server — is the console running?');e.kind='net';throw e}
 let d={};try{d=await r.json()}catch(_){}
 if(r.status===401){const m=d.error||'Your session expired — sign in again with your email and password.';if(TOK)signOut(m);const e=new Error(m);e.kind='auth';throw e}
 if(!r.ok||(d&&d.error)){const e=new Error((d&&d.error)||('Request failed ('+r.status+')'));e.kind='error';throw e}
 return d||{};
}
async function post(p,b,timeoutMs){
 const ctl=timeoutMs?new AbortController():null;const tid=ctl?setTimeout(()=>ctl.abort(),timeoutMs):null;
 try{const r=await fetch(p,{method:'POST',headers:H(),body:JSON.stringify(b||{}),signal:ctl?ctl.signal:undefined});let d={};try{d=await r.json()}catch(_){}; return d||{}}
 catch(e){return {error:(e&&e.name==='AbortError')?'timeout':'request failed'}}
 finally{if(tid)clearTimeout(tid)}
}
function authCard(msg){return `<div class=card><h2>Sign in</h2><p class=muted>${esc(msg||'Your session expired — sign in again with your email and password.')}</p></div>`}
function errCard(k,msg){return `<div class=card><h2>Something went wrong</h2><p class=muted>${esc(msg)}</p><div style=margin-top:8px><button class=pri onclick="go('${k}')">retry</button></div></div>`}
function esc(s){return (s==null?'':''+s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function pill(txt,cls){return `<span class="pill ${cls||''}">${esc(txt)}</span>`}
function emptyB(ic,t,m,cta){return '<div class=empty><div class=ic>'+ic+'</div><h3>'+esc(t)+'</h3><p>'+esc(m)+'</p>'+(cta||'')+'</div>'}
const NAV_ESSENTIALS=['controller','orgs'];   // PROGRESSIVE NAV: a 0-company user sees only Assistant + create-company
function renderNav(){const lean=!ORGS.length;   // reveal the full 13-item nav only once a company exists
 $('#nav').innerHTML=NAV.map(([sec,items])=>{
   const vis=lean?items.filter(([k])=>NAV_ESSENTIALS.includes(k)):items;
   if(!vis.length)return '';   // drop section headers that have nothing under them in the lean state
   return `<div class=nsec>${sec}</div>`+vis.map(([k,l,ic])=>`<a class="${k==CUR?'on':''}" onclick="go('${k}')"><span class=ico>${ic}</span><span class=lbl>${l}</span>${BADGES[k]?`<span class=b>${BADGES[k]}</span>`:''}</a>`).join('');
 }).join('')}
function clearTimers(){for(const k of ['CTLPOLL','CTLTICK','CHATPOLL','TOPBARPOLL','AUTOPOLL']){if(window[k]){clearInterval(window[k]);window[k]=null}}}
async function refreshTopbar(){
 if(!TOK)return;
 try{const b=await get('/api/billing');const f=await get('/api/forecast').catch(()=>null);
   const lvl=f?(f.level==='over'?'bad':(f.level==='warn'?'warn':'ok')):'ok';
   const _sd=$('#spenddot');_sd.className='dot '+lvl;const _QV={ok:'within limits',warn:'approaching limit',bad:'over limit'};const _ql='Spend & quota — '+(f?f.pct_of_quota_projected+'% of quota projected, ':'')+(_QV[lvl]||'within limits');_sd.title=_ql;_sd.setAttribute('aria-label',_ql);$('#spendtxt').textContent='$'+((b.usage&&b.usage.tokens!=null)?(b.invoice&&b.invoice.total!=null?b.invoice.total:'') : '')+(f?(' · '+f.pct_of_quota_projected+'% quota'):'');
   if(!$('#spendtxt').textContent.trim()||$('#spendtxt').textContent==='$')$('#spendtxt').textContent=(b.plan||'')+(f?(' · '+f.pct_of_quota_projected+'%'):'');
 }catch(e){}
 try{const n=await get('/api/notifications');const bb=$('#bellbadge');if(n.unread>0){bb.style.display='inline-flex';bb.textContent=n.unread}else bb.style.display='none'}catch(e){}
 try{const ap=await get('/api/approvals');BADGES.approvals=(ap.items||[]).filter(i=>i.kind!=='consent').length;renderNav()}catch(e){}
 try{const s=await get('/api/status');const _sv=({operational:'ok',degraded:'warn',major_outage:'bad'}[s.verdict]||'ok');const _SV={operational:'all systems operational',degraded:'degraded performance',major_outage:'major outage'};const _st=$('#statusdot');_st.className='dot '+_sv;const _sl='Status — '+(_SV[s.verdict]||s.verdict||'operational');_st.title=_sl;_st.setAttribute('aria-label',_sl)}catch(e){}
 loadProviders();   // keep PROVIDER_OK fresh so the Assistant connect-banner reflects the latest provider state
}
async function go(k){CUR=k;renderNav();$('#tbtitle').textContent=LABEL[k]||k;$('#acctmenu').style.display='none';toggleNav(false);$('#view').innerHTML='<div class=card><div class=skel style=width:40%></div><div class=skel style=width:75%></div></div>';
 try{await VIEWS[k]()}catch(e){if(e.kind==='auth'){if(TOK)signOut();return}$('#view').innerHTML=errCard(k,e.message)}}
function userBusy(){const a=document.activeElement;if(a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA'||a.tagName==='SELECT'||a.isContentEditable))return true;const m=$('#acctmenu');if(m&&m.style.display!=='none')return true;return false}
async function refreshView(){ // background poll: no skeleton flash, no nav/scroll disruption, never clobber what the user is touching
 if(!TOK)return;const k=CUR;if(!VIEWS[k])return;if(userBusy())return;
 const sx=window.scrollX,sy=window.scrollY;
 try{await VIEWS[k]()}catch(e){if(e.kind==='auth'){if(TOK)signOut();return}return}
 if(CUR===k&&!userBusy())window.scrollTo(sx,sy)}
function kpis(arr){return '<div class=kpis>'+arr.map(a=>`<div class=kpi><b>${esc(a[1])}</b><span>${esc(a[0])}</span></div>`).join('')+'</div>'}
function stages(ss){return '<div class=stages>'+ss.map(s=>{const cls=s.done?(s.ok===false?'bad':'ok'):'';const verdict=s.done?(s.ok===false?'failed':'passed'):'pending';const lab=`${s.stage}: ${verdict}`;return `<div class="st ${cls}" role=img title="${lab}" aria-label="${lab}"></div>`}).join('')+'</div>'}

let THREAD=null;let CTLBUSY=false;let CHATBUSY=false;
let CONSENT_OK=false;let PROG=null;let CTLABORT=null;   // guided first-run + live-progress state
let PEND_IDEA='';try{PEND_IDEA=localStorage.getItem('aos_pend')||''}catch(_){}   // the idea typed before setup is done — persisted so a full reload never silently drops it
function setPend(t){PEND_IDEA=t||'';try{PEND_IDEA?localStorage.setItem('aos_pend',PEND_IDEA):localStorage.removeItem('aos_pend')}catch(_){}}   // single writer so the saved idea and its storage mirror never drift
function firstRunSteps(){return [
 {k:'company',done:ORGS.length>0,label:'Create your company'},
 {k:'provider',done:PROVIDER_OK,label:'Connect an AI model'},
 {k:'consent',done:CONSENT_OK,label:'Approve AI use'},
 {k:'describe',done:false,label:'Describe your product'}];}
// ONE checklist renderer for EVERY first-run surface (welcome, Assistant, providers detour, Cockpit).
// Each step is a labelled list item so a screen reader announces "Connect an AI model — current step".
function firstRunRow(steps){const cur=steps.findIndex(s=>!s.done);
 return '<div class=row role=list aria-label="Setup checklist" style="gap:10px;flex-wrap:wrap;align-items:center">'+steps.map((s,i)=>{
   const ic=s.done?'✓':(i+1);const cls=s.done?'ok':(i===cur?'accent':'');const st=s.done?'done':(i===cur?'current step':'upcoming');
   return '<span class=row role=listitem aria-label="'+esc(s.label)+' — '+st+'" style="gap:6px;align-items:center">'+pill(ic,cls)+'<span aria-hidden=true class="'+(s.done?'muted':'')+'" style="'+(i===cur?'font-weight:600':'')+'">'+esc(s.label)+'</span></span>';
 }).join('<span class=muted aria-hidden=true>›</span>')+'</div>';}
function firstRunChecklist(steps){return '<div class=card>'+firstRunRow(steps)+'</div>';}
// Map the server /api/onboarding step keys onto the SAME wording the Assistant's client checklist uses,
// so the Cockpit banner and the Assistant can never label the same step differently.
const OB_LABEL={welcome:'Create your company',provider:'Connect an AI model',consent:'Approve AI use',first_build:'Describe your product'};
async function obSkip(){try{await post('/api/onboarding/skip',{})}catch(e){}go('cockpit');}
async function firstRunCreate(){const nm=(($('#fr_nm')||{}).value||'').trim();const vis=(($('#fr_vis')||{}).value||'').trim();const note=$('#fr_note');
 if(!nm){if(note)note.textContent='Enter a name for your company';const f=$('#fr_nm');if(f)f.focus();return}
 if(note)note.textContent='Creating…';
 const r=await post('/api/orgs/new',{name:nm,vision:vis});
 if(r&&r.error){if(note)note.textContent='✗ '+r.error;return}
 if(r.org_id){ORG=r.org_id;localStorage.setItem('aos_org',ORG);await loadOrgs();setOrgName();go('controller');}}   // advance the wizard to step 2 in place
async function frConsent(){const r=await post('/api/settings/consent',{accept:true});if(r&&r.error){const n=$('#cnote');if(n)n.textContent='✗ '+r.error;else alert('Could not record consent: '+r.error);return}CONSENT_OK=true;go('controller');}
async function frFlush(){if(PEND_IDEA&&PROVIDER_OK&&CONSENT_OK){const t=PEND_IDEA;setPend('');const i=$('#cmsg');if(i){i.value=t;grow(i)}await ctlSend();}}   // auto-resume the saved idea once setup is done
function setSendMode(busy){const b=$('#ctlsend');if(!b)return;if(busy){b.classList.remove('pri');b.textContent='Stop';b.title='Stop';b.onclick=ctlStop;}else{b.classList.add('pri');b.textContent='Send';b.title='Send';b.onclick=ctlSend;}}
async function ctlStop(){
 if(CTLABORT){try{CTLABORT.abort()}catch(_){}}
 try{await post('/api/controller/cancel',{org:ORG||0})}catch(e){}
 CTLBUSY=false;CTLABORT=null;setSendMode(false);stopTick();PROG=null;
 const t=$('#ctltyping');if(t)t.remove();const p=$('#ctlprog');if(p)p.remove();
 const cm=$('#cmsg');if(cm)cm.disabled=false;const n=$('#cnote');if(n)n.textContent='Stopped.';
 go('controller');}
function fmtElapsed(s){s=Math.max(0,Math.floor(s));const m=Math.floor(s/60),ss=s%60;return m+'m '+(ss<10?'0':'')+ss+'s elapsed';}
function progressBubble(p){const lab=esc(p.phase_label||'Working');const eta=p.eta_note?(' · '+esc(p.eta_note)):'';
 return '<div class="msg ai" id=ctlprog style="margin:8px 0"><div><span class=bubble><span class=spin></span> <b>'+lab+'…</b> <span id=progelapsed class=muted>'+fmtElapsed(p.elapsed_s||0)+'</span>'+eta+'<div style="margin-top:8px"><button onclick=ctlStop()>Stop</button></div></span></div></div>';}
function startTick(){if(window.CTLTICK)return;window.CTLTICK=setInterval(()=>{
  if(!TOK||CUR!=='controller'||!PROG){clearInterval(window.CTLTICK);window.CTLTICK=null;return}
  const el=$('#progelapsed');if(el)el.textContent=fmtElapsed((Date.now()-PROG._anchor)/1000);},1000);}
function stopTick(){if(window.CTLTICK){clearInterval(window.CTLTICK);window.CTLTICK=null}}
const VIEWS={
 controller:async()=>{
  if(!ORGS.length){try{await loadOrgs()}catch(e){}}
  try{await loadProviders()}catch(e){}                                  // keep setup steps current after a provider is added
  try{const sd=await get('/api/settings');CONSENT_OK=!!(sd.ai_consent&&sd.ai_consent.accepted)}catch(e){}
  if(!ORGS.length){   // GUIDED FIRST-RUN · Step 1: name your company INLINE (no detour to a list page)
   if(window.CTLPOLL){clearInterval(window.CTLPOLL);window.CTLPOLL=null;}stopTick();
   $('#view').innerHTML='<h1>Welcome to agent-os</h1>'
    +'<p class=sub>'+pill('Step 1 of 4','accent')+' You\'re the CEO — your AI agents research, build and ship your software. Let\'s get you set up; it takes about a minute.</p>'
    +firstRunChecklist(firstRunSteps())
    +'<div class=card style="border-color:var(--accent)"><h2>Name your company</h2>'
    +'<p class=muted style="margin:0 0 8px">Give it a name and an optional one-line vision. Your assistant takes it from there, asking you at each step.</p>'
    +'<label for=fr_nm>Company name</label><input id=fr_nm placeholder="Acme" aria-label="Company name" onkeydown="if(event.key===\'Enter\')firstRunCreate()">'
    +'<label for=fr_vis>Vision (optional)</label><input id=fr_vis placeholder="one line on what it does" aria-label="Company vision" onkeydown="if(event.key===\'Enter\')firstRunCreate()">'
    +'<div style="margin-top:12px"><button class=pri onclick=firstRunCreate()>Create company →</button></div>'
    +'<div id=fr_note class=muted style=margin-top:8px></div></div>';
   const f=$('#fr_nm');if(f)setTimeout(()=>{try{f.focus()}catch(_){}},0);
   return;
  }
  const home=!ORG;   // org=0 -> account-wide home · org=N -> that company's controller
  let d={};try{d=await get('/api/controller/state?org='+(ORG||0))}catch(e){$('#view').innerHTML=errCard('controller',e.message);return}
  const gateErr=d&&d.error&&gateError(d.error);
  const cur=ORGS.find(o=>o.org_id==ORG)||{};
  const scope=home?'All companies · home':('Company · '+(cur.name||''));
  let phaseLine='';
  if(!home&&d.phase&&d.awaiting!=='fleet'){phaseLine=' · <b>'+esc(d.phase_label||d.phase)+'</b>';}   // live phase lives in the progress bubble instead
  const intro=home?'Your account-wide assistant — ask across all your companies, spin up a new one, or start a quick build. I route it to the right place.':'Tell this company\'s controller what to build. It researches, brings options, designs and ships — asking you at each step.';
  let h='<h1>Assistant</h1><p class=sub>'+pill(scope,home?'accent':'')+' '+esc(intro)+phaseLine+'</p>';
  const setupDone=PROVIDER_OK&&CONSENT_OK;
  if(!setupDone){   // GUIDED FIRST-RUN · Steps 2-3: connect a model, then approve AI use — shown PROACTIVELY, not as a rejection
   h+=firstRunChecklist(firstRunSteps());
   if(!PROVIDER_OK)h+='<div class=card style="border-color:var(--accent)"><div class="row spread"><span><b>Step 2 — Connect an AI model.</b> Your agents need an AI model to do the work. It takes one click.</span><button class=pri onclick="go(\'providers\')">Connect an AI model</button></div></div>';
   else h+='<div class=card style="border-color:var(--accent)"><div class="row spread"><span><b>Step 3 — Approve AI use.</b> A one-time, revocable OK to let your AI model process what you type, so your agents can build.</span><button class=pri aria-label="Approve AI use and continue" onclick=frConsent()>Approve &amp; continue</button></div></div>';
  }
  h+='<div class=card id=clog style="max-height:54vh;overflow:auto;display:flex;flex-direction:column;gap:10px"></div>';
  // CONTEXT-AWARE CHIPS: hide the generic starters once a build is in flight; once a scoping conversation is underway swap them for next-step suggestions
  const inFlight=(d.awaiting==='fleet')||!!d.progress;
  const ph0=(d.phase||'').toUpperCase();
  const scoping=(d.messages||[]).length>0;   // any back-and-forth yet? then the generic starters are stale — go context-aware
  let CHIPS;
  if(inFlight||gateErr)CHIPS=[];
  else if(home)CHIPS=['Create a new company','What needs my attention across all companies?','A quick throwaway prototype'];
  else if(ph0&&ph0!=='DISCOVER')CHIPS=ctlNextChips(ph0);
  else if(scoping)CHIPS=ctlNextChips('DISCOVER');   // mid-scoping: suggest how to move the conversation forward, not "Build a competitor to YouTube"
  else CHIPS=['Build a competitor to YouTube','An internal tool for my team','A booking page for my salon'];
  const ph=home?'e.g. start a new company, or ask about any of them…':'e.g. build a competitor to YouTube';
  h+='<div class=card>'+(CHIPS.length?'<div class=chips>'+CHIPS.map(c=>`<span class=chip-s onclick="ctlFill('${c.replace(/'/g,"")}')">${esc(c)}</span>`).join('')+'</div>':'')+`<div class="row composer"><textarea id=cmsg class=chatbox rows=1 aria-label="Message your assistant" placeholder="${ph}" oninput="grow(this)" onkeydown="taKey(event,ctlSend)"></textarea><button class=pri id=ctlsend onclick=ctlSend()>Send</button></div><div id=cnote class=muted style=margin-top:6px></div></div>`;
  $('#view').innerHTML=h;
  if(gateErr){$('#cnote').innerHTML=gateNote(d.error);ctlRender([]);}
  else if(d&&d.error){const log=$('#clog');if(log)log.innerHTML='<div class=muted>'+esc(d.error)+'</div>';}
  else ctlRender(d.messages||[],d.progress);
  if(PEND_IDEA&&setupDone)frFlush();   // auto-resume the idea they typed before setup was finished
  if(!window.CTLPOLL)window.CTLPOLL=setInterval(async()=>{if(!TOK||CUR!=='controller'){clearInterval(window.CTLPOLL);window.CTLPOLL=null;stopTick();return}if(CTLBUSY)return;try{const s=await get('/api/controller/state?org='+(ORG||0));if(!(s&&s.error))ctlRender(s.messages||[],s.progress)}catch(e){}},5000);
 },
 orgs:async()=>{const d=await get('/api/orgs');ORGS=d.orgs||[];
  let h='<h1>My companies</h1><p class=sub>Each company has its own controller, research, design, build and budget. You can run as many as you like.</p>';
  h+='<div class=card><h2>Create a company</h2><div class=row><input id=onm aria-label="Company name" placeholder="YouTube competitor"><input id=ovis aria-label="Company vision (optional)" placeholder="one-line vision (optional)"><button class=pri onclick=orgNew()>Create</button></div><div id=onote class=muted style=margin-top:8px></div></div>';
  h+='<div class=grid>'+(ORGS.length?ORGS.map(o=>`<div class=tile><div class="row spread"><b>${esc(o.name)}</b>${o.org_id==ORG?pill('active','ok'):''}</div><div class=muted style=margin:6px_0>${esc(o.vision||'—')} · ${esc(o.stage)} · ${o.products} product(s)</div><button class=pri onclick="switchOrg(${o.org_id})">Open</button></div>`).join(''):emptyB('🏢','No companies yet','Create your first company above.'))+'</div>';
  $('#view').innerHTML=h;},
 portfolio:async()=>{let p={},a={},f={};try{p=await get('/api/portfolio')}catch(e){}try{a=await get('/api/portfolio/analytics')}catch(e){}try{f=await get('/api/portfolio/failures')}catch(e){}
  const t=p.totals||{};let h='<h1>Portfolio</h1><p class=sub>Everything across all your companies.</p>';
  h+=kpis([['Companies',t.orgs||0],['Products',t.products||0],['Live',t.live||0],['Building',t.building||0],['Failed',t.failed||0],['Spend $',t.spend_usd||0]]);
  h+='<div class=card><h2>Your companies</h2><table><tr><th>company</th><th>stage</th><th>products</th><th>live</th><th>spend</th></tr>'+((p.orgs||[]).length?p.orgs.map(o=>`<tr><td>${esc(o.name)}</td><td>${esc(o.stage)}</td><td>${o.products}</td><td>${o.live||0}</td><td>$${o.spend_usd||0}</td></tr>`).join(''):'<tr><td class=muted colspan=5>no companies yet</td></tr>')+'</table></div>';
  const fails=(f.failures||f.items||[]);h+='<div class=card><h2>What needs attention (across companies)</h2>'+(fails.length?fails.map(x=>`<div class=item>${pill('failed','bad')} ${esc(x.product||x.title||'')} <span class=muted>${esc(x.org||x.org_name||'')} ${esc(x.decision||'')}</span></div>`).join(''):'<div class=muted>nothing broken across your companies ✓</div>')+'</div>';
  $('#view').innerHTML=h;},
 agentic:async()=>{const d=await get('/api/agentfeatures');
  let h='<h1>Agentic features</h1><p class=sub>Embed your AI fleet INTO your product — a button or endpoint your users/staff trigger that runs an agent. We build these into your app (you host it); we don\'t run them for you. Tell your Controller which you want during design.</p>';
  h+=`<div class=card><h2>Recommend an invocation pattern</h2><p class=muted style=margin:0_0_10px>Describe what you need; I'll suggest how to invoke an agent (event, schedule, button, or endpoint) and the closest catalog features.</p><div class=row><input id=recneed aria-label="Describe what you need" placeholder="e.g. process uploads when a user submits" onkeydown="if(event.key==='Enter')recAgentic()"><button class=pri id=recbtn onclick=recAgentic()>Recommend</button></div><div id=recout style=margin-top:10px></div></div>`;
  h+='<div class=grid>'+(d.features||[]).map(f=>`<div class=tile><div class="row spread"><b>${esc(f.name)}</b>${pill(f.audience,f.audience=='external'?'accent':'')}</div><div class=muted style=margin:6px_0>${esc(f.blurb)}</div><div class=muted>${pill('trigger: '+(f.trigger||f.surface),f.trigger=='event'?'warn':'')} ${esc(f.surface)} · ${esc(f.category)}</div></div>`).join('')+'</div>';
  $('#view').innerHTML=h;},
 design:async()=>{if(!ORG){$('#view').innerHTML='<div class=card>'+emptyB('🎨','No company selected','Pick or create a company first — then your prototypes and screens will show up here.','<button class=pri onclick="go(\'orgs\')">Go to My companies</button>')+'</div>';return}const d=await get('/api/design?org='+ORG);
  let h='<h1>Design</h1><p class=sub>Prototype screens your fleet drafted — for your cockpit, your team, and your external users.</p>';
  const g=d.gallery||[];h+='<div class=grid>'+(g.length?g.map(s=>`<div class=tile><div class="row spread"><b>${esc(s.surface||'')}</b>${pill(s.status,s.status=='approved'?'ok':'')}</div><div class=muted style=margin:6px_0>${esc(s.title||'')}</div>${s.status!='approved'?`<button class=pri onclick="designOk(${s.id})">approve</button>`:''}</div>`).join(''):emptyB('🎨','No prototypes yet','When your controller reaches the design phase, screens appear here.'))+'</div>';
  $('#view').innerHTML=h;},
 chat:async()=>{
  if(!THREAD){const r=await post('/api/chat/new',{});THREAD=r.thread}
  const CHIPS=['Track my gym members','An invoice generator','A URL shortener','A booking page for my salon','An internal tool for my team'];
  $('#view').innerHTML=`<h1>Direct your fleet</h1><p class=sub>Describe what you want in plain words. I'll ask questions, then build it — you approve.</p>
   <div class=chips>`+CHIPS.map(c=>`<span class=chip-s onclick="chipFill('${c.replace(/'/g,"")}')">${esc(c)}</span>`).join('')+`</div>
   <div class=card id=chatlog style="max-height:52vh;overflow:auto;display:flex;flex-direction:column;gap:10px"></div>
   <div class=card><div class="row composer"><textarea id=msg class=chatbox rows=1 aria-label="Describe your app idea" placeholder="e.g. I want an app to track my gym members…" oninput="grow(this)" onkeydown="taKey(event,chatSend)"></textarea><button class=pri id=chatsend onclick=chatSend()>Send</button></div><div id=chatnote class=muted style=margin-top:6px></div></div>`;
  await chatRender();
 },
 cockpit:async()=>{const d=await get('/api/cockpit?org='+ORG);const s=d.summary||{},b=d.budget||{};d.products=d.products||[];d.communications=d.communications||[];d.queue=d.queue||{};
  let fc=null;try{fc=await get('/api/forecast')}catch(e){}
  let ql={};try{const q=await get('/api/quality');(q.products||[]).forEach(p=>ql[p.product]=p)}catch(e){}
  let ob=null;try{ob=await get('/api/onboarding')}catch(e){}
  let co=null;try{co=await get('/api/company?org='+ORG)}catch(e){}
  let hl=null;try{hl=await get('/api/health?org='+ORG)}catch(e){}
  let ls={};try{const l=await get('/api/livestatus');(l.products||[]).forEach(p=>ls[p.product]=p)}catch(e){}
  let h='<h1>Cockpit</h1><p class=sub>Your whole company at a glance.</p>';
  if(co){const cm={healthy:'ok',attention:'warn',critical:'bad'}[co.verdict]||'ok';h+=`<div class=card><div class=row><span class="dot ${cm}" style="width:10px;height:10px;border-radius:50%"></span> <b>${esc(co.line||co.verdict)}</b></div></div>`;}
  if(ob&&!ob.completed){   // SINGLE SOURCE OF TRUTH: render the SAME checklist as the Assistant, driven by the server's real per-step status — never a second, divergent "next step"
   const obSteps=(ob.steps||[]).map(s=>({done:!!s.done,label:OB_LABEL[s.key]||s.title||s.key}));
   const nx=obSteps.find(s=>!s.done);
   h+='<section class=card aria-label="Finish setting up your company" style="border-color:var(--accent)">'
     +'<div class="row spread" style="gap:12px;flex-wrap:wrap;align-items:center">'
       +'<span style="min-width:220px;flex:1"><b>Finish setting up</b>'+(nx?('<div class=muted style=margin-top:4px>Next: '+esc(nx.label)+'. Your Assistant walks you through it.</div>'):'')+'</span>'
       +'<span class=row style="gap:8px;flex-wrap:wrap">'
         +'<button class=pri onclick="go(\'controller\')" aria-label="'+(nx?('Continue setup: '+esc(nx.label)):'Continue setup in your Assistant')+'">Continue in Assistant →</button>'
         +'<button onclick=obSkip() aria-label="Skip the setup checklist">Skip</button>'
       +'</span>'
     +'</div>'+(obSteps.length?('<div style=margin-top:10px>'+firstRunRow(obSteps)+'</div>'):'')
   +'</section>';}
  h+=kpis([['Products',s.products||0],['Launched',s.launched||0],['Building',s.building||0],['Failed',s.failed||0],['Workers',s.live_workers||0],['Spend $',s.spend_usd||0]]);
  if(fc){const fcls=fc.level==='over'?'bad':(fc.level==='warn'?'warn':'ok');h+=`<div class=card><h2>Budget forecast</h2><div class="row spread"><span>${esc(fc.headline||'')}</span>${pill(fc.pct_of_quota_projected+'% projected',fcls)}</div><div class=meta>burn ~${fc.burn_tokens_per_day} tok/day · $${fc.burn_usd_per_day}/day${fc.eta_days_to_quota?(' · hits quota in ~'+fc.eta_days_to_quota+'d'):''}</div></div>`;}
  if(hl&&!hl.ok){let hi='';
    (hl.blocked||[]).forEach(b=>hi+=`<div class=item>${pill('blocked','warn')} ${esc(b.agent||'')} waiting on ${esc(b.waiting_on||'')} ${b.minutes?('· '+b.minutes+'m'):''}</div>`);
    (hl.deadlocks||[]).forEach(d2=>hi+=`<div class=item>${pill('deadlock','bad')} ${esc(JSON.stringify(d2).slice(0,80))}</div>`);
    (hl.conflicts||[]).forEach(cf=>hi+=`<div class=item>${pill('conflict','warn')} ${esc(JSON.stringify(cf).slice(0,80))}</div>`);
    if((hl.dead_letter||0))hi+=`<div class=item>${pill('stuck','bad')} ${hl.dead_letter} task(s) need a human decision — see Approvals</div>`;
    if(hi)h+='<div class=card><h2>Needs attention · org health</h2>'+hi+'</div>';}
  else if(hl&&hl.ok){h+='<div class=card><h2>Org health</h2><div class=row>'+pill('all clear','ok')+' <span class=muted>no blocked or stuck agents</span></div></div>';}
  h+='<div class=card><h2>Projects</h2>'+(d.products.length?d.products.map(p=>`<div class=item><div class="row spread"><span><b>${esc(p.product)}</b> ${pill(p.result,p.ready?'ok':(p.failed?'bad':''))} ${ql[p.product]?pill('✓ '+ql[p.product].overall,(ql[p.product].overall=='verified'||ql[p.product].overall=='passed')?'ok':(ql[p.product].overall=='failed'?'bad':'')):''} ${ls[p.product]&&ls[p.product].url?'<a href="'+ls[p.product].url+'" target=_blank>'+(ls[p.product].reachable?'● live':'open ↗')+'</a>':''} ${p.halted?pill('paused','bad'):''}</span><span>${p.halted?`<button onclick="ctl('${p.product}','resume')">resume</button>`:`<button onclick="ctl('${p.product}','pause')">pause</button>`}</span></div>${stages(p.stages)}<div class=muted>$${p.cost_usd} · ${p.tokens} tok · ${p.workers.length} workers</div></div>`).join(''):'<div class=muted>none yet — start one in the Assistant</div>')+'</div>';
  h+='<div class=grid><div class=card><h2>Communications</h2><table>'+(d.communications.length?d.communications.map(m=>`<tr><td>${m.ts}</td><td>${esc(m.from)}</td><td>→ ${esc(m.to)}</td><td>${esc(m.intent)}</td></tr>`).join(''):'<tr><td class=muted>no recent agent messages</td></tr>')+'</table></div>';
  h+=`<div class=card><h2>Work queue</h2>${kpis([['pending',d.queue.pending],['active',d.queue.active],['dead',d.queue.dead]])}</div></div>`;
  $('#view').innerHTML=h;},
 build:async()=>{$('#view').innerHTML=`<h1>New build</h1><p class=sub>Describe a product; the governed factory builds, tests and ships it.</p>
  <div class=card><label>Name</label><input id=bn aria-label="Project name" placeholder=splitbill><label>Type</label><select id=bk onchange=showEst()><option value=lib>Python library</option><option value=web>Web app</option><option value=service>API service</option></select><label>What should it do?</label><textarea id=bc placeholder="Describe the API, behaviours, edge cases…"></textarea><div id=est class=muted style=margin-top:8px></div><div style=margin-top:10px><button class=pri onclick=doBuild()>Build it</button></div><div id=bnote class=muted style=margin-top:8px></div></div>`;showEst();},
 agents:async()=>{const d=await get('/api/agents');d.agents=d.agents||[];const roles=(d.roles||['research-growth']);const sys=d.system||[];
  let h='<h1>Your agents</h1><p class=sub>Three tiers: the built-in system org that runs your builds (read-only), your own standing agents (yours to edit), and agentic features you embed in your product.</p>';
  h+='<div class=card><h2>System agents · read-only</h2><p class=muted style=margin:0_0_10px>The standing org your Assistant runs. These are governed and built-in — you can\'t edit or delete them, and only the Controller can spawn workers.</p>'+(sys.length?sys.map(a=>`<div class=item><div class="row spread"><span><b>${esc(a.title)}</b> ${pill(a.role)} ${a.can_spawn?pill('sole spawner','accent'):''} ${a.reports_to?('<span class=muted>reports to '+esc(a.reports_to)+'</span>'):pill('chief of staff','ok')}</span>${pill('read-only')}</div></div>`).join(''):'<div class=muted>—</div>')+'</div>';
  h+='<div class=card><h2>Your custom agents</h2>'+(d.agents.length?d.agents.map(a=>`<div class=item><div class="row spread"><span><b>${esc(a.name)}</b> ${pill(a.role)} ${pill(a.trigger==='recurring'?('every '+Math.round((a.interval_s||0)/86400)+'d'):'manual',a.trigger==='recurring'?'accent':'')} ${a.enabled?pill('on','ok'):pill('off')}</span><span><button onclick="agentRun(${a.id})">run now</button> <button onclick="agentToggle(${a.id},${a.enabled?'false':'true'})">${a.enabled?'pause':'enable'}</button> <button class=danger onclick="agentDel(${a.id})">delete</button></span></div><div class=muted>last run: ${a.last_run?esc(a.last_run):'never'} ${a.last_status?('· '+esc(a.last_status)):''}</div></div>`).join(''):emptyB('🤖','No custom agents yet','Create a standing agent below — it runs on a schedule and reports into your feed.'))+'</div>';
  h+='<div class=card><h2>Create an agent</h2><label>Name</label><input id=an aria-label="Agent name" placeholder="Market Watch"><div class=grid><div><label>Specialty</label><select id=ar>'+roles.map(r=>`<option value="${r}">${r}</option>`).join('')+'</select></div><div><label>Runs</label><select id=at><option value=manual>On demand</option><option value=recurring>Every week</option></select></div></div><label>What should it do?</label><textarea id=ai placeholder="Every week, scan my market for new competitors and pricing changes; give me 3 prioritized takeaways with sources."></textarea><div style=margin-top:10px><button class=pri onclick=agentCreate()>Create agent</button></div><div id=anote class=muted style=margin-top:8px></div></div>';
  h+='<div class=card><h2>Agentic features</h2><div class="row spread"><span class=muted>Embed agents INTO your product — buttons and endpoints your own users trigger. A curated catalog your fleet builds into your app.</span><button onclick="go(\'agentic\')">Browse features</button></div></div>';
  $('#view').innerHTML=h;},
 templates:async()=>{const d=await get('/api/templates');$('#view').innerHTML=`<h1>Templates</h1><p class=sub>Start from a curated, factory-ready blueprint.</p><div class=grid>`+(d.templates||[]).map(t=>`<div class=tile><div class="row spread"><b>${esc(t.name)}</b>${pill(t.kind)}</div><div class=muted style=margin:6px_0>${esc(t.blurb)}</div><button class=pri onclick="buildTpl('${t.slug}')">Build this</button></div>`).join('')+'</div>';},
 projects:async()=>{const d=await get('/api/projects?org='+ORG);d.projects=d.projects||[];$('#view').innerHTML='<h1>Projects</h1><p class=sub>Everything you have built.</p><div class=card>'+(d.projects.length?d.projects.map(p=>`<div class=item><div class="row spread"><span><b>${esc(p.product)}</b> ${pill(p.result,p.ready?'ok':(p.failed?'bad':''))}</span><span class=muted>$${p.cost_usd||0} · ${p.stages_done||0} stages</span></div></div>`).join(''):emptyB('▤','No projects yet','Describe your first product and the factory builds, tests and ships it.','<button class=pri onclick="go(\'controller\')">Start your first build</button>'))+'</div>';},
 activity:async()=>{const o=await get('/api/observability');let fl={workers:[]};try{fl=await get('/api/fleet?org='+ORG)}catch(e){}fl.workers=fl.workers||[];
  let h='<h1>Activity</h1><p class=sub>Runs, errors, spend, and the live workers across your fleet.</p>';
  h+=kpis([['Runs',o.runs||0],['Steps',o.steps||0],['Errors',o.errors||0],['Cost $',o.cost_usd||0],['Workers',fl.workers.length]]);
  h+='<div class=card><h2>Live workers</h2><table><tr><th>agent</th><th>role</th><th>status</th><th>product</th></tr>'+(fl.workers.length?fl.workers.map(w=>`<tr><td>${esc(w.agent)}</td><td>${esc(w.role)}</td><td>${pill(w.status,w.status=='active'?'ok':'')}</td><td>${esc(w.product)}</td></tr>`).join(''):'<tr><td class=muted colspan=4>no live workers right now</td></tr>')+'</table></div>';
  h+='<div class=card><h2>By stage</h2><table><tr><th>stage</th><th>steps</th><th>errors</th><th>cost</th><th>avg s</th></tr>'+(o.by_stage||[]).map(s=>`<tr><td>${esc(s.stage)}</td><td>${s.steps}</td><td>${s.errors}</td><td>$${s.cost_usd}</td><td>${s.avg_elapsed_s}</td></tr>`).join('')+'</table></div>';
  h+='<div class=card><h2>Recent errors</h2>'+((o.recent_errors||[]).length?o.recent_errors.map(e=>`<div class=item><b>${esc(e.product)}</b> · ${esc(e.stage)} <span class=muted>${e.ts}</span><div><code>${esc(e.snippet)}</code></div></div>`).join(''):'<div class=muted>no errors — clean ✓</div>')+'</div>';
  $('#view').innerHTML=h;},
 fleet:async()=>{const d=await get('/api/fleet?org='+ORG);d.workers=d.workers||[];$('#view').innerHTML='<h1>Agent fleet</h1><p class=sub>Live workers across your products.</p>'+kpis([['Live workers',d.count||0],['Active products',d.products_active||0]])+'<div class=card><table><tr><th>agent</th><th>role</th><th>status</th><th>product</th><th>task</th></tr>'+(d.workers.length?d.workers.map(w=>`<tr><td>${esc(w.agent)}</td><td>${esc(w.role)}</td><td>${pill(w.status,w.status=='active'?'ok':'')}</td><td>${esc(w.product)}</td><td>${esc(w.task)}</td></tr>`).join(''):'<tr><td class=muted colspan=5>no live workers right now</td></tr>')+'</table></div>';},
 observability:async()=>{const d=await get('/api/observability');$('#view').innerHTML='<h1>Observability</h1><p class=sub>Runs, errors, spend across your fleet.</p>'+kpis([['Runs',d.runs],['Steps',d.steps],['Errors',d.errors],['Cost $',d.cost_usd],['Tokens',d.tokens]])+'<div class=card><h2>By stage</h2><table><tr><th>stage</th><th>steps</th><th>errors</th><th>cost</th><th>avg s</th></tr>'+(d.by_stage||[]).map(s=>`<tr><td>${esc(s.stage)}</td><td>${s.steps}</td><td>${s.errors}</td><td>$${s.cost_usd}</td><td>${s.avg_elapsed_s}</td></tr>`).join('')+'</table></div><div class=card><h2>Recent errors</h2>'+((d.recent_errors||[]).length?d.recent_errors.map(e=>`<div class=item><b>${esc(e.product)}</b> · ${esc(e.stage)} <span class=muted>${e.ts}</span><div><code>${esc(e.snippet)}</code></div></div>`).join(''):'<div class=muted>no errors — clean</div>')+'</div>';},
 approvals:async()=>{const d=await get('/api/approvals');$('#view').innerHTML='<h1>Approvals</h1><p class=sub>Decisions awaiting you. Governed: nothing risky happens without this.</p><div class=card>'+(d.count?d.items.map(i=>i.kind=='consent'?`<div class=item><div class="row spread"><span>${pill('setup','accent')} <b>Approve AI processing</b></span><span><button class=pri onclick="decide('consent','${esc(i.ref)}','approve')">Approve AI processing</button></span></div><div class=muted>Nothing's broken — this is a one-time, revocable OK to let your AI provider process your prompts so your agents can build. Approving it unlocks your first build.</div></div>`:`<div class=item><div class="row spread"><span>${pill(i.kind,i.severity=='high'?'bad':(i.severity=='med'?'warn':''))} <b>${esc(i.title)}</b></span><span><button class=pri onclick="decide('${i.kind}','${esc(i.ref)}','${i.kind=='dead_letter'?'retry':'approve'}')">${esc(i.action_label||'approve')}</button> ${i.kind=='hire_request'||i.kind=='dead_letter'||i.kind=='blocked_build'?`<button onclick="decide('${i.kind}','${esc(i.ref)}','${i.kind=='dead_letter'?'drop':'deny'}')">${i.kind=='blocked_build'?'abandon':'deny'}</button>`:''}</span></div><div class=muted>${esc(i.detail||'')}</div></div>`).join(''):emptyB('✓','All clear','Nothing needs your decision right now. We\'ll bring anything important here.'))+'</div>';},
 integrations:async()=>{const d=await get('/api/integrations');$('#view').innerHTML='<h1>Integrations</h1><p class=sub>Connect the services your products need.</p><div class=grid>'+(d.integrations||[]).map(i=>`<div class=tile><div class=row style=margin-bottom:6px><span class=int-ic>${esc((i.name||'?')[0])}</span><b>${esc(i.name)}</b><span style=flex:1></span>${pill(i.status=='connected'?'connected':'disconnected',i.status=='connected'?'ok':'off')}</div><div class=muted style=margin:0_0_8px>${esc(i.blurb)} · ${esc(i.category)}</div>${i.status=='connected'?`<button onclick="integ('disconnect','${i.slug}')">disconnect</button>`:`<button class=pri onclick="integ('connect','${i.slug}')">connect</button>`}</div>`).join('')+'</div>';},
 billing:async()=>{const d=await get('/api/billing');$('#view').innerHTML=`<h1>Billing & plans</h1><p class=sub>Usage, quota and plan. Real payment is gated (BYO Stripe).</p>`+kpis([['Plan',d.plan],['Builds',(d.usage&&d.usage.builds)||0],['Tokens',(d.usage&&d.usage.tokens)||0]])+'<div class=card><h2>Plans</h2><table><tr><th>plan</th><th>price</th><th>builds</th><th>tokens</th><th></th></tr>'+(d.plans||[]).map(p=>`<tr><td>${esc(p.slug)} ${p.current?pill('current','ok'):''}</td><td>$${p.price}</td><td>${p.builds}</td><td>${p.tokens}</td><td>${p.current?'':`<button onclick="plan('${p.slug}')">switch</button>`}</td></tr>`).join('')+'</table></div>';},
 notifications:async()=>{const d=await get('/api/notifications');d.feed=d.feed||[];const unread=d.unread||0;
  let h=`<h1>Notifications</h1><p class=sub>${unread} unread.</p><div class=card>`;
  if(unread>0)h+=`<div class="row spread" style=margin-bottom:10px><span class=muted>${unread} unread</span><button onclick=markAllRead()>Mark all read</button></div>`;
  h+=(d.feed.length?d.feed.map(n=>`<div class=item><div class="row spread"><span>${pill(n.level,n.level=='urgent'?'bad':(n.level=='standard'?'':'warn'))} <b>${esc(n.title)}</b></span><span class=row style=gap:8px><span class=muted>${esc(n.category)} · ${n.created_at}</span>${n.read?'':`<button onclick="markRead(${n.id})">Mark read</button>`}</span></div><div class=muted>${esc(n.body||'')}</div></div>`).join(''):emptyB('◔','You\'re all caught up','Build updates, billing alerts and agent reports will appear here.'));
  $('#view').innerHTML=h+'</div>';},
 team:async()=>{let o=null,t=null;try{o=await get('/api/org')}catch(e){}try{t=await get('/api/team')}catch(e){}
  let h='<h1>Org chart</h1><p class=sub>Your fleet of AI agents — who does what, and who\'s working right now.</p>';
  if(t){const mem=t.members||[];
   h+='<div class=card><h2>Seats</h2><table><tr><th>member</th><th>role</th><th>status</th></tr>'+(mem.length?mem.map(m=>`<tr><td>${esc(m.id)}</td><td>${esc(m.role)}</td><td>${pill(m.status,m.status=='active'?'ok':'')}</td></tr>`).join(''):'<tr><td class=muted colspan=3>no seats</td></tr>')+'</table>'+(t.seats_note?`<div class=muted style=margin-top:10px>${pill('plan: '+(t.plan||'free'))} ${esc(t.seats_note)}</div>`:'')+'</div>';}
  if(o&&o.tree){const root=o.tree.find(n=>!n.reports_to)||o.tree[0];
   const node=(n)=>`<div class=item><div class="row spread"><span><b>${esc(n.title||n.role)}</b> <span class=muted>${esc(n.role)}</span> ${n.live?pill('live · '+(n.count||1),'ok'):pill('idle')}</span><span class=muted>${esc(n.task||'')}</span></div></div>`;
   h+='<div class=card><h2>Controller</h2>'+(root?node(root):'')+'</div>';
   h+='<div class=card><h2>Reports</h2>'+o.tree.filter(n=>n.reports_to).map(node).join('')+'</div>';
  } else { h+='<div class=card>'+emptyB('◍','Org unavailable','Your agent org chart will appear here.')+'</div>'; }
  $('#view').innerHTML=h;},
 settings:async()=>{const d=await get('/api/settings');const c=d.ai_consent||{};const pr=d.profile||{};$('#view').innerHTML=`<h1>Settings</h1><p class=sub>Profile, AI consent, keys, notifications.</p>
  <div class=card><h2>Profile</h2><div class=row>plan <b>${esc(pr.plan||'—')}</b> ${pr.suspended?pill('suspended','bad'):pill('active','ok')}</div></div>
  <div class=card><h2>AI consent</h2><div class=row>${c.accepted?pill('accepted','ok'):pill('not accepted','bad')} <span class=muted>${esc(c.provider||'')} ${esc(c.version||'')}</span></div><div style=margin-top:8px>${c.accepted?'<button onclick="setConsent(false)">revoke</button>':'<button class=pri onclick="setConsent(true)">accept</button>'}</div></div>
  <div class=card><h2>BYO API key</h2><div class=row>${d.byo_key_set?pill('key on file','ok'):pill('no key','warn')}</div><div style=margin-top:8px><input id=bk aria-label="API key" placeholder="sk-… (stored encrypted)"><button class=pri style=margin-top:6px onclick=saveKey()>save key</button></div><div id=bknote class=muted style=margin-top:8px></div></div>
  <div class=card><h2>Notification preferences</h2><table><tr><th>category</th><th>in-app</th><th>email</th><th>push</th></tr>`+(d.notification_prefs||[]).map(p=>`<tr><td>${esc(p.category)}</td><td><input type=checkbox ${p.in_app?'checked':''} onchange="pref('${p.category}',this.checked,null,null)"></td><td><input type=checkbox ${p.email?'checked':''} onchange="pref('${p.category}',null,this.checked,null)"></td><td><input type=checkbox ${p.push?'checked':''} onchange="pref('${p.category}',null,null,this.checked)"></td></tr>`).join('')+`</table></div>
  <div class=card><h2>Your data</h2><div class=row><button onclick=acctExport()>Export my data</button><button onclick=acctDelete() style="border-color:var(--r);color:var(--r)">Delete my account</button></div><div id=acctnote class=muted style=margin-top:8px></div></div>`;PREFS=d.notification_prefs;},
 providers:async()=>{const d=await get('/api/providers');const connected=(d.providers||[]).some(p=>p.connected);PROVIDER_OK=connected;
  let banner='';
  if(PEND_IDEA){   // arrived here via the first-run gate (an idea was typed before a model was connected) — keep that thread visible and offer a one-tap way forward the moment a model connects
   try{const sd=await get('/api/settings');CONSENT_OK=!!(sd.ai_consent&&sd.ai_consent.accepted)}catch(e){}   // keep the step bar honest about the consent step too
   const idea=PEND_IDEA.length>160?esc(PEND_IDEA.slice(0,160))+'…':esc(PEND_IDEA);
   banner=firstRunChecklist(firstRunSteps())
    +'<div class=card style="border-color:var(--accent)" role=status aria-live=polite><div class="row spread" style="gap:12px;flex-wrap:wrap;align-items:center">'
    +'<span style="min-width:220px;flex:1"><b>'+(connected?'Your idea is saved and ready ✓':'Your idea is waiting')+'</b><div class=muted style=margin-top:4px>“'+idea+'”</div>'+(connected?'':'<div class=muted style=margin-top:6px>Connect a model below and I\'ll pick this straight back up — nothing to retype.</div>')+'</span>'
    +(connected?'<button class=pri onclick="go(\'controller\')" aria-label="Continue to describe your product">Continue → describe your product</button>':'<button disabled aria-disabled=true title="Connect a model first to continue" style="opacity:.5;cursor:not-allowed">Continue → describe your product</button>')
    +'</div></div>';
  }
  $('#view').innerHTML='<h1>Connect an AI model</h1>'
   +'<p class=sub>Your agents need an AI model to do their work — Claude, ChatGPT/Codex, or both. You only need one to get started.</p>'
   +banner
   +'<div id=provnote class=muted style="margin:0 0 10px"></div><div class=grid>'+(d.providers||[]).map(p=>`<div class=tile><div class="row spread"><b>${esc(p.name)}</b>${p.connected?pill('connected','ok'):pill('not connected')}</div><div class=muted style=margin:6px_0>${esc(p.blurb)}</div>${p.connected?`<button onclick="provRemove('${p.slug}')">Disconnect</button>`:`<div class=row><button class=pri onclick="provSub('${p.slug}')">Connect</button><button onclick="provAdd('${p.slug}','${esc(p.key_hint)}')">Use an API key</button></div>`}</div>`).join('')+'</div>'
   +'<details style="margin-top:14px"><summary style="cursor:pointer;color:var(--mut)">Technical details</summary>'
   +'<p class=muted style="margin-top:10px"><b>Connect</b> signs this machine into your Claude or ChatGPT account using the provider\'s own secure login (it opens a browser on this host and connects only once you\'re genuinely signed in, via <code>claude auth login</code> / <code>codex login</code>). Because it uses the machine\'s own sign-in, it connects the whole machine to one account — ideal for a self-hosted, single-operator setup. <b>Use an API key</b> connects with a key instead. Builds use your highest-priority connected model; with none connected, the platform default is used.</p></details>';},
 help:async()=>{const d=await get('/api/help/topics');$('#view').innerHTML=`<h1>Help</h1><p class=sub>Ask me anything about using agent-os.</p>
  <div class=card><div class=row><input id=hq aria-label="Ask a help question" placeholder="e.g. how do I add my Codex key?" onkeydown="if(event.key==='Enter')helpAsk()"><button class=pri onclick=helpAsk()>Ask</button></div><div id=hans style=margin-top:10px></div></div>
  <div class=card><h2>Topics</h2>`+(d.topics||[]).map(t=>`<div class=item><b>${esc(t.area||t.key||'')}</b> <span class=muted>${esc(t.desc||t.description||'')}</span></div>`).join('')+'</div>';},
 status:async()=>{const d=await get('/api/status');const m={operational:'ok',degraded:'warn',major_outage:'bad',unknown:''}[d.verdict];$('#view').innerHTML='<h1>Status</h1><p class=sub>Live platform health.</p><div class=card><div class=row>'+pill(d.verdict,m)+'</div></div><div class=card><h2>Services</h2>'+Object.entries(d.components||{}).map(([k,v])=>{const up=v===true||v=='ok'||v=='up';return `<div class="row spread item"><span>${esc(k)}</span>${pill(up?'Operational':'Down',up?'ok':'bad')}</div>`}).join('')+`<div class="row spread item"><span>dead-letter depth</span>${pill(d.dead_letter_depth,d.dead_letter_depth?'bad':'ok')}</div></div>`;},
};
let PREFS=[];
let LAST_PROPOSAL=null;
function md(s){return esc(s).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')}
async function chatRender(){
 if(!TOK||CUR!=='chat'){if(window.CHATPOLL){clearInterval(window.CHATPOLL);window.CHATPOLL=null}return}
 if(CHATBUSY)return;
 let d;try{d=await get('/api/chat/history?thread='+THREAD)}catch(e){return}
 const log=$('#chatlog');if(!log)return;
 const atBottom=(log.scrollHeight-log.scrollTop-log.clientHeight)<40;
 log.innerHTML=(d.messages||[]).map(m=>{
  const me=m.role==='user';const meta=m.meta||{};const prop=meta.proposal;const ns=(meta.kind==='next_steps')&&meta.suggestions;
  let extra='';
  if(prop){const planHtml=(prop.plan&&prop.plan.trim())?('<div style="margin:6px 0"><b>Plan</b>'+prop.plan.split('\n').filter(l=>l.trim()).map(l=>'<div class=muted style=margin-left:6px>'+esc(l.replace(/^[-*]\s*/,'• '))+'</div>').join('')+'</div>'):'';
   extra=`<div class=tile style="margin:8px 0;text-align:left"><b>Proposed build:</b> ${esc(prop.name)} ${pill(prop.kind,prop.kind==='project'?'accent':'')}${planHtml}<div class=muted style=margin:4px_0>${esc(prop.charter)}</div><div class=row style=margin-top:6px><button class=pri onclick=chatConfirm()>Approve &amp; build</button><button onclick="chipFill('Actually, change: ')">Revise</button></div></div>`;}
  if(ns)extra=`<div class=chips style="margin:8px 0">`+meta.suggestions.map(s=>`<span class=chip-s onclick="chipFill('${s.replace(/'/g,"")}')">${esc(s)}</span>`).join('')+`</div>`;
  return `<div class="msg ${me?'me':'ai'}" style="margin:8px 0"><div><span class=bubble>${md(m.content)}</span>${extra}</div></div>`;
 }).join('')||'<div class=muted>Say hello, or describe a product — I\'ll ask a couple of questions, then build it.</div>';
 if(atBottom)log.scrollTop=log.scrollHeight;   // only re-pin if user was already at the bottom; don't yank scrollback
 if(!window.CHATPOLL)window.CHATPOLL=setInterval(chatRender,5000);   // live updates as the controller reports back
}
function grow(t){if(!t)return;t.style.height='auto';t.style.height=Math.min(t.scrollHeight,200)+'px'}   // auto-grow the composer up to its max, then it scrolls
function taKey(e,fn){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();fn()}}   // ChatGPT/Claude: Enter sends, Shift+Enter inserts a newline
function chipFill(t){const i=$('#msg');if(i){i.value=t;i.focus();grow(i)}}
function ctlFill(t){const i=$('#cmsg');if(i){i.value=t;i.focus();grow(i)}}
function ctlNextChips(phase){   // CONTEXT-AWARE CHIPS: after scoping, swap the generic starters for next-step suggestions tied to the phase
 const M={DISCOVER:['That covers it — show me options','Add a must-have detail','Who is it for?'],
  OPTIONS:['Go with the recommended option','Compare the options'],
  DEEP_DESIGN:['Approve the plan','Change part of the plan'],
  PLAN_APPROVAL:['Approve the plan','Change part of the plan'],
  PROTOTYPE:['Approve the screens','Request design changes'],
  DELIVER:['Start another build','Add a feature to this one']};
 return M[(phase||'').toUpperCase()]||['Add a feature','Refine what we have'];}
function ctlRender(msgs,prog){const log=$('#clog');if(!log)return;
 const atBottom=(log.scrollHeight-log.scrollTop-log.clientHeight)<40;
 let html=(msgs||[]).map(m=>{const me=m.role==='user';const meta=m.meta||{};let extra='';
  if(meta.kind==='options'&&meta.options)extra='<div class=chips style="margin:8px 0">'+meta.options.map(o=>`<span class=chip-s onclick="ctlChoose(${o.id})">${esc(o.title||('Option '+o.id))}${o.recommended?' ★':''}</span>`).join('')+'</div>';
  else if(meta.kind==='plan'&&meta.plan)extra='<div class=tile style="margin:8px 0;text-align:left"><b>Plan: '+esc(meta.plan.name||'')+'</b> '+pill(meta.plan.kind||'')+'<div class=muted style=margin-top:4px>'+esc(meta.plan.charter||'')+'</div></div>';
  else if(meta.kind==='next_steps'&&meta.suggestions)extra='<div class=chips style="margin:8px 0">'+meta.suggestions.map(s=>`<span class=chip-s onclick="ctlFill('${s.replace(/'/g,"")}')">${esc(s)}</span>`).join('')+'</div>';
  return `<div class="msg ${me?'me':'ai'}" style="margin:8px 0"><div><span class=bubble>${md(m.content)}</span>${extra}</div></div>`;
 }).join('')||'<div class=muted>Describe what you want to build — I\'ll ask a few questions, then research it and bring you options.</div>';
 // LIVE PROGRESS: while a job runs, render a working bubble (phase + elapsed + ETA + Stop) instead of a static line
 if(prog){PROG=Object.assign({},prog);PROG._anchor=Date.now()-((prog.elapsed_s||0)*1000);html+=progressBubble(prog);}
 else{PROG=null;stopTick();}
 log.innerHTML=html;
 if(prog)startTick();
 if(atBottom)log.scrollTop=log.scrollHeight;   // only re-pin if user was already at the bottom; don't yank scrollback
}
async function ctlSend(){
 if(CTLBUSY)return;const i=$('#cmsg');const m=(i?i.value:'').trim();if(!m)return;
 // GUIDED FIRST-RUN: never reject the first idea behind a gate — save it and walk them through the missing step
 if(!PROVIDER_OK){setPend(m);if(i){i.value='';grow(i)}const n=$('#cnote');if(n)n.innerHTML='Saved your idea ✓ — connect an AI model first and I\'ll start automatically.';go('providers');return;}
 if(!CONSENT_OK){setPend(m);if(i){i.value='';grow(i)}const n=$('#cnote');if(n)n.innerHTML='Saved your idea ✓ — tap <b>Approve &amp; continue</b> above and I\'ll start automatically.';return;}
 CTLBUSY=true;setSendMode(true);if(i){i.value='';grow(i);i.disabled=true}
 const log=$('#clog');if(log){log.insertAdjacentHTML('beforeend','<div class="msg me" style="margin:8px 0"><div><span class=bubble>'+esc(m)+'</span></div></div><div class="msg ai" id=ctltyping style="margin:8px 0"><div><span class=bubble><span class=spin></span> <span class=muted>thinking</span></span></div></div>');log.scrollTop=log.scrollHeight}
 $('#cnote').textContent='Working… press Stop to cancel.';
 CTLABORT=(typeof AbortController!=='undefined')?new AbortController():null;
 let r=null,gotDelta=false,gotDone=false,streamFailed=false;
 // PRIMARY: stream the reply token-by-token (SSE) so the bubble fills live. The whole turn is still
 // persisted server-side by say(), so a closed tab / partial stream loses nothing — the reload reconciles.
 try{
  const resp=await fetch('/api/controller/stream',{method:'POST',headers:H(),body:JSON.stringify({org:ORG||0,message:m}),signal:CTLABORT?CTLABORT.signal:undefined});
  if(!resp.ok||!resp.body||!resp.body.getReader){streamFailed=true;}
  else{
   const reader=resp.body.getReader();const dec=new TextDecoder();let buf='',acc='';
   while(true){
    const{value,done}=await reader.read();if(done)break;
    buf+=dec.decode(value,{stream:true});let idx;
    while((idx=buf.indexOf('\n\n'))>=0){
     const chunk=buf.slice(0,idx);buf=buf.slice(idx+2);
     const line=chunk.replace(/^data:\s?/,'').trim();if(!line)continue;
     let ev;try{ev=JSON.parse(line)}catch(_){continue}
     if(ev.delta!=null){gotDelta=true;acc+=ev.delta;ctlStreamInto(acc);}
     else if(ev.error){r={error:ev.error};}
     else if(ev.done){gotDone=true;if(ev.result)r=ev.result;}
    }
   }
  }
 }catch(e){
  if(e&&e.name==='AbortError'){r={error:'stopped'};}
  else if(!gotDelta){streamFailed=true;}   // never connected -> safe to fall back to the blocking path
  // gotDelta but broke mid-stream: say() still finished + persisted server-side; the reload below reconciles
 }
 // FALLBACK: streaming unavailable (old server / proxy / connect error) -> the proven blocking POST.
 if(streamFailed){
  try{const resp=await fetch('/api/controller/say',{method:'POST',headers:H(),body:JSON.stringify({org:ORG||0,message:m}),signal:CTLABORT?CTLABORT.signal:undefined});try{r=await resp.json()}catch(_){r={}}}
  catch(e){r={error:(e&&e.name==='AbortError')?'stopped':'request failed'}}
 }
 CTLBUSY=false;CTLABORT=null;setSendMode(false);if(i){i.disabled=false;i.focus()}
 const t=$('#ctltyping');if(t)t.remove();
 if(r&&r.error==='stopped'){$('#cnote').textContent='Stopped.';return;}   // ctlStop already parked the job + reloaded
 const cg=gateKey(r);if(r&&gateError(cg)){$('#cnote').innerHTML=gateNote(cg);if(i){i.value=m;grow(i)}return;}   // friendly connect/consent prompt — keep their text
 if(r&&r.error){$('#cnote').textContent='✗ '+r.error+' — your message is in the box, press Send to retry.';if(i){i.value=m;grow(i)}return;}
 $('#cnote').textContent='';go('controller');
}
function ctlStreamInto(text){   // paint streamed tokens into the live assistant bubble (final reload re-renders via md())
 const t=$('#ctltyping');if(!t)return;const b=t.querySelector('.bubble');if(b)b.textContent=text;
 const log=$('#clog');if(log){const atBottom=(log.scrollHeight-log.scrollTop-log.clientHeight)<60;if(atBottom)log.scrollTop=log.scrollHeight;}
}
async function ctlChoose(oid){const r=await post('/api/controller/choose',{org:ORG||0,option_id:oid});const cg=gateKey(r);if(r&&gateError(cg)){$('#cnote')&&($('#cnote').innerHTML=gateNote(cg));return;}go('controller');}
async function orgNew(){const inp=$('#onm');const name=(inp?inp.value:'').trim();const note=$('#onote');
 if(!name){if(note)note.textContent='Enter a name for your company';if(inp)inp.focus();return}
 if(note)note.textContent='';
 const r=await post('/api/orgs/new',{name,vision:($('#ovis')||{}).value||''});
 if(r&&r.error){if(note)note.textContent='✗ '+r.error;return}
 if(r.org_id)switchOrg(r.org_id);}
async function designOk(id){await post('/api/design/decide',{org:ORG,id,status:'approved'});go('design');}
async function recAgentic(){
 const i=$('#recneed');const need=(i?i.value:'').trim();const out=$('#recout');if(!out)return;
 if(!need){out.innerHTML='<span class=muted>Describe what you need first.</span>';if(i)i.focus();return}
 out.innerHTML='<span class=muted>thinking…</span>';
 let r;try{r=await get('/api/agentfeatures/recommend?need='+encodeURIComponent(need))}catch(e){out.innerHTML='<span class=muted>✗ '+esc(e.message)+'</span>';return}
 const feats=r.suggested_features||[];
 out.innerHTML=`<div class=tile style=text-align:left><div class=row>${pill('pattern: '+esc(r.pattern||''),'accent')}</div><div class=muted style=margin:8px_0>${esc(r.rationale||'')}</div>${feats.length?('<div class=row style=flex-wrap:wrap><span class=muted style=margin-right:4px>suggested:</span>'+feats.map(s=>pill(s)).join(' ')+'</div>'):'<div class=muted>No closely matching catalog feature — the pattern above is the recommendation.</div>'}</div>`;
}
async function markRead(id){await post('/api/notifications/read',{id});refreshTopbar();go('notifications');}
async function markAllRead(){let d={};try{d=await get('/api/notifications')}catch(e){}const feed=(d&&d.feed)||[];for(const n of feed){if(!n.read)await post('/api/notifications/read',{id:n.id});}refreshTopbar();go('notifications');}
async function chatSend(){
 if(CHATBUSY)return;const i=$('#msg');const m=(i?i.value:'').trim();if(!m)return;CHATBUSY=true;
 const btn=$('#chatsend');if(btn)btn.disabled=true;if(i){i.value='';grow(i);i.disabled=true}
 const log=$('#chatlog');if(log){log.insertAdjacentHTML('beforeend','<div class="msg me" style="margin:8px 0"><div><span class=bubble>'+esc(m)+'</span></div></div><div class="msg ai" id=chattyping style="margin:8px 0"><div><span class=bubble><span class=muted>… thinking</span></span></div></div>');log.scrollTop=log.scrollHeight}
 $('#chatnote').textContent='thinking…';
 let r;
 try{r=await post('/api/chat/say',{thread:THREAD,message:m},90000);}
 finally{CHATBUSY=false;if(btn)btn.disabled=false;if(i){i.disabled=false}}
 const cg=gateKey(r);if(r&&gateError(cg)){const t=$('#chattyping');if(t)t.remove();$('#chatnote').innerHTML=gateNote(cg);if(i){i.value=m;grow(i);i.focus()}await chatRender();return;}   // gate (consent/provider) -> friendly CTA, keep their text
 if(r&&r.error){   // never leave a silent spinner: surface a clear, retryable failure
  const t=$('#chattyping');if(t)t.remove();
  const em=r.error==='timeout'?'The assistant is taking too long to respond. Your message is still in the box — press Send to try again.':('✗ '+r.error+' — your message is still in the box, press Send to retry.');
  $('#chatnote').textContent=em;
  if(i){i.value=m;grow(i);i.focus()}
  if(log){log.insertAdjacentHTML('beforeend','<div class="msg ai" style="margin:8px 0"><div><span class=bubble><span class=muted>'+esc(em)+'</span></span></div></div>');log.scrollTop=log.scrollHeight}
  return;
 }
 if(i)i.focus();
 $('#chatnote').textContent='';await chatRender();
}
async function chatConfirm(){
 $('#chatnote').textContent='starting build…';const r=await post('/api/chat/confirm',{thread:THREAD});
 const cg=gateKey(r);if(r&&gateError(cg)){$('#chatnote').innerHTML=gateNote(cg);return;}
 $('#chatnote').textContent=r.error?('✗ '+r.error):('building '+r.product+' — see Cockpit');
}
async function showEst(){const k=($('#bk')||{}).value||'lib';let e;try{e=await get('/api/estimate?kind='+k)}catch(_){return}if($('#est'))$('#est').textContent='Estimate: '+(e.note||('~$'+e.cost_usd_estimate+', ~'+e.minutes_estimate+' min'));}
async function doBuild(){$('#bnote').textContent='submitting…';const r=await post('/api/build',{name:$('#bn').value,kind:$('#bk').value,charter:$('#bc').value});if(r&&r.error&&gateError(r.error)){$('#bnote').innerHTML=gateNote(r.error);return;}$('#bnote').textContent=r.error?('✗ '+r.error):('building '+r.product+' — see Cockpit');}
async function buildTpl(slug){
 const r=await post('/api/build_template',{slug});
 if(r.error==='consent_required'){go('settings');return}
 if(r.error==='provider_required'){go('providers');return}
 if(r.error){alert('Could not build: '+r.error);return}
 go('cockpit');
}
async function ctl(p,a){await post('/api/control',{product:p,action:a});go('cockpit');}
async function decide(kind,ref,verdict){await post('/api/approvals/decide',{kind,ref,verdict});go('approvals');}
async function integ(act,slug){let secret=null;if(act=='connect')secret=prompt('API key / secret for '+slug+' (leave blank if OAuth):')||null;await post('/api/integrations/'+act,{slug,secret});go('integrations');}
async function plan(p){await post('/api/billing/plan',{plan:p});go('billing');}
function provNote(msg,bad){const n=$('#provnote');if(!n){if(bad)alert(msg);return}n.style.color=bad?'var(--accent)':'';n.textContent=msg;}
async function provAdd(slug,hint){const k=prompt('Paste your '+slug+' API key ('+hint+'):');if(k===null)return;provNote('Checking that key with '+slug+'…',false);const r=await post('/api/providers/connect',{provider:slug,mode:'api_key',key:k});if(r&&r.error){provNote('✗ '+r.error,true);return}go('providers');}   // validate the key for real before claiming connected; show the real reason inline if rejected
async function provSub(slug){
  // 1) If the host CLI is ALREADY signed in, connect for real (verified server-side) and we're done.
  provNote('Checking this machine\'s '+slug+' sign-in…',false);
  const r=await post('/api/providers/connect',{provider:slug,mode:'subscription'});
  if(r&&!r.error){go('providers');return}
  // 2) Not signed in -> TRIGGER the real provider OAuth on THIS host (opens a browser here), then offer Check connection.
  const s=await post('/api/providers/subscription/start',{provider:slug});
  if(s&&s.error){provNote('✗ '+s.error,true);return}
  if(s&&s.already){const r2=await post('/api/providers/connect',{provider:slug,mode:'subscription'});if(r2&&!r2.error){go('providers');return}provNote('✗ '+((r2&&r2.error)||'sign-in not detected yet'),true);return}
  const n=$('#provnote');if(n){n.style.color='';n.innerHTML=esc((s&&s.instruction)||('Sign in to '+slug+' in the browser on this machine, then check the connection.'))+' <button class=pri onclick="provSub(\''+slug+'\')">Check connection</button>';}
}
async function agentCreate(){const t=$('#at').value;const r=await post('/api/agents/create',{name:$('#an').value,instructions:$('#ai').value,role:$('#ar').value,trigger:t,interval_s:t==='recurring'?604800:0,output:'report'});$('#anote').textContent=r.error?('✗ '+r.error):'agent created';if(!r.error)go('agents');}
async function agentRun(id){const r=await post('/api/agents/run',{id});const n=$('#anote');if(!n)return;if(r&&r.error&&gateError(r.error)){n.innerHTML=gateNote(r.error);return;}n.textContent=r&&r.error?('✗ '+r.error):'running — it\'ll report into your notifications';}
async function agentToggle(id,en){await post('/api/agents/toggle',{id,enabled:en});go('agents');}
async function agentDel(id){if(!confirm('Delete this agent?'))return;await post('/api/agents/delete',{id});go('agents');}
async function provRemove(slug){await post('/api/providers/remove',{provider:slug});go('providers');}
async function helpAsk(){const q=$('#hq').value.trim();if(!q)return;$('#hans').innerHTML='<span class=muted>thinking…</span>';const r=await post('/api/help/ask',{question:q});$('#hans').innerHTML='<div class=tile>'+esc(r.answer||r.error||'(no answer)')+(r.suggested_area?` <button onclick="go('${r.suggested_area}')">go there</button>`:'')+'</div>';}
async function setConsent(a){await post('/api/settings/consent',{accept:a});go('settings');}
async function saveKey(){const v=($('#bk')||{}).value||'';const note=$('#bknote');if(note)note.textContent='saving…';const r=await post('/api/byok',{key:v});if(r&&r.error){if(note)note.textContent='✗ '+r.error;else alert('Could not save key: '+r.error);return}go('settings');}
async function acctExport(){$('#acctnote').textContent='preparing export…';const r=await post('/api/account/export',{});$('#acctnote').textContent=r.ok?('Export ready ('+r.products+' products) on the server: '+(r.path||'')):'export failed';}
async function acctDelete(){const r=await post('/api/account/delete',{confirm:false});if(!confirm('Permanently delete your account and ALL data? This cannot be undone.'))return;const r2=await post('/api/account/delete',{confirm:true});if(r2.ok){localStorage.removeItem('aos_tenant');TOK='';clearTimers();$('#view').innerHTML='<div class=card>Your account and data were deleted. Goodbye.</div>';}}
async function pref(cat,ia,em,pu){const cur=(PREFS||[]).find(p=>p.category==cat)||{in_app:true,email:true,push:false};await post('/api/settings/pref',{category:cat,in_app:ia==null?cur.in_app:ia,email:em==null?cur.email:em,push:pu==null?cur.push:pu});}
async function boot(){renderNav();refreshTopbar();await loadOrgs();await loadProviders();
 if(!ORG&&ORGS.length){ORG=ORGS[0].org_id;localStorage.setItem('aos_org',ORG);setOrgName();}   // default into a company at boot; the user can switch to All orgs (home) anytime
 let ob=null;try{ob=await get('/api/onboarding')}catch(e){}
 if(ORGS.length&&ob&&!ob.completed&&ob.step!=='done')go('cockpit');   // first-run with a company: Cockpit renders the guided onboarding banner
 else go('controller');   // Assistant is the primary surface — it handles the zero-company home state itself
 if(!window.TOPBARPOLL)window.TOPBARPOLL=setInterval(refreshTopbar,15000);
 if(!window.AUTOPOLL)window.AUTOPOLL=setInterval(()=>{if(!TOK)return;if(!['cockpit','activity'].includes(CUR))return;if(($('#acctmenu')||{}).style&&$('#acctmenu').style.display==='block')return;if(document.activeElement&&document.activeElement.closest&&document.activeElement.closest('#view'))return;refreshView()},6000)}
document.addEventListener('click',e=>{if(!e.target.closest('#acctmenu')&&!String(e.target.getAttribute&&e.target.getAttribute('onclick')||'').includes('toggleAcct'))$('#acctmenu').style.display='none';
  if(!e.target.closest('.orgsw'))toggleOrgSw(false);});
async function start(){                                   // validate the saved token BEFORE revealing the app shell
 if(!TOK){showApp(false);return}
 $('#signin').style.display='none';$('#app').style.display='none';   // neutral loading state — no flash of the signed-in UI
 const sp=$('#splash');if(sp)sp.style.display='flex';
 let authFailed=false;
 try{await get('/api/onboarding');}                       // lightweight authed probe
 catch(e){if(e.kind==='auth')authFailed=true;}            // 5xx/net errors fall through: token still valid, reveal + reconnect
 if(sp)sp.style.display='none';
 if(authFailed||!TOK){showApp(false);return}              // stale/invalid token -> sign-in; app shell never shown
 showApp(true);boot();                                    // valid (or transient backend blip) -> reveal; boot()/go() surface reconnecting state
}
start();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, default=str).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _ctl_stream(self, tid, body):
        """SSE: stream the controller's CONVERSATIONAL clarifying reply token-by-token so the chat bubble
        fills live (first token in seconds) instead of a blocking turn that reveals the whole message at once.
        Defers ALL logic to loopcontroller.say() (gates, persistence, block parsing, phase advance) — we only
        pass an on_delta sink that writes `data: {...}` chunks and FLUSHES each. On done we send the say()
        result so the browser can finalize (gate CTA / error / reload). The whole reply is also persisted by
        say() exactly like the non-stream path, so a closed tab loses nothing. The frontend keeps the regular
        POST /api/controller/say as a FALLBACK if this stream can't connect or errors before any token."""
        org = int(body.get("org") or 0)
        msg = (body.get("message") or "")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")          # defeat any proxy buffering of the token stream
        self.send_header("Connection", "close")
        self.end_headers()
        st = {"dead": False, "suppress": False, "pending": ""}

        def _write(obj):
            if st["dead"]:
                return
            try:
                self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
                self.wfile.flush()
            except Exception:
                st["dead"] = True                            # client gone (Stop/disconnect) -> stop writing

        def on_delta(text):
            # Hide control markup from the wire: the controller's clarify/plan turns may END with a
            # [[RESEARCH]]/[[PLAN]] block — once we see '[[' we suppress the rest (it's always trailing).
            # A lone trailing '[' is held back so a block split across two deltas can't leak its first '['.
            if st["suppress"] or st["dead"]:
                return
            s = st["pending"] + text
            st["pending"] = ""
            i = s.find("[[")
            if i != -1:
                visible = s[:i]
                st["suppress"] = True
            elif s.endswith("["):
                st["pending"] = "["
                visible = s[:-1]
            else:
                visible = s
            if visible:
                _write({"delta": visible})

        thread = loopcontroller.thread_for_org(tid, org)
        try:
            r = loopcontroller.say(tid, thread, msg, on_delta=on_delta)
        except Exception as e:
            _write({"error": str(e)[:200]})
            _write({"done": True})
            return
        _write({"done": True, "result": r})

    def do_GET(self):
        u = urlparse(self.path); p = u.path
        if p == "/":
            b = PAGE.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        if p == "/health":
            return self._json(200, {"service": "agent-os-console", "ok": True})
        if p.startswith("/download/"):
            product = p[len("/download/"):]
            if not (frontdoor.PRODUCTS / product).exists():
                return self._json(404, {"error": "not found"})
            data = frontdoor._zip(product)
            self.send_response(200); self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{product}.zip"')
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            return
        fn = GETS.get(p)
        if not fn:
            return self._json(404, {"error": "not found"})
        tid = _tenant(self.headers.get("X-Tenant-Token"))
        if tid is _TENANT_ERROR:                              # transient backend blip — keep the session
            return self._json(503, {"error": "backend temporarily unavailable — reconnecting"})
        if not tid:
            return self._json(401, {"error": "sign up first"})
        q = parse_qs(u.query)
        if p in ORG_SCOPED_GET and not _owns(tid, int((q.get("org", ["0"])[0]) or 0)):
            return self._json(403, {"error": "not your company"})
        try:
            self._json(200, fn(tid, q))
        except Exception as e:
            self._json(500, {"error": str(e)[:200]})

    def do_POST(self):
        u = urlparse(self.path); p = u.path
        if p in ("/api/signup", "/api/login", "/api/verify-email", "/api/resend-code"):   # UNAUTHENTICATED: account + email-verification
            import auth
            b = self._body()
            try:
                if p == "/api/signup":
                    r = auth.signup(b.get("email", ""), b.get("password", ""),
                                    (b.get("name") or "").strip()[:60] or None, b.get("plan", "free"))
                elif p == "/api/verify-email":
                    r = auth.verify_email(b.get("email", ""), b.get("code", ""))
                elif p == "/api/resend-code":
                    r = auth.resend_code(b.get("email", ""))
                else:
                    r = auth.login(b.get("email", ""), b.get("password", ""))
                return self._json(200 if not r.get("error") else 400, r)
            except Exception as e:
                return self._json(400, {"error": str(e)[:200]})
        if p == "/api/controller/stream":                     # SSE: stream the controller's clarifying reply
            tid = _tenant(self.headers.get("X-Tenant-Token"))
            if tid is _TENANT_ERROR:
                return self._json(503, {"error": "backend temporarily unavailable — reconnecting"})
            if not tid:
                return self._json(401, {"error": "sign up first"})
            body = self._body()
            if not _owns(tid, int(body.get("org") or 0)):
                return self._json(403, {"error": "not your company"})
            return self._ctl_stream(tid, body)
        fn = POSTS.get(p)
        if not fn:
            return self._json(404, {"error": "not found"})
        tid = _tenant(self.headers.get("X-Tenant-Token"))
        if tid is _TENANT_ERROR:                              # transient backend blip — keep the session
            return self._json(503, {"error": "backend temporarily unavailable — reconnecting"})
        if not tid:
            return self._json(401, {"error": "sign up first"})
        body = self._body()
        if p in ORG_SCOPED_POST and not _owns(tid, int(body.get("org") or 0)):
            return self._json(403, {"error": "not your company"})
        if p == "/api/xorg/propose":
            for k in ("source", "target"):
                if not _owns(tid, int(body.get(k) or 0)):
                    return self._json(403, {"error": "not your company"})
        try:
            self._json(200, fn(tid, parse_qs(u.query), body))
        except Exception as e:
            self._json(500, {"error": str(e)[:200]})


def _selftest():
    """Prove every GET route resolves + renders for a REAL tenant with one product, end to end."""
    import billing as _b
    import psycopg
    reg = _b.signup("console-selftest", "free")
    tid = reg["tenant_id"]
    prod = tid.replace("t-", "")[:6] + "-demo"
    DB = frontdoor.DB
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (prod, tid))
        cur.execute("""INSERT INTO traces (run_id, product, stage, role, kind, rc, cost_usd, tokens_in, tokens_out, elapsed_s, prompt, output, model)
                       VALUES (%s,%s,'SPEC','builder','agent',0,0.16,500,800,20,'p','o','m')""", (f"run-{prod}", prod))
        c.commit()
    try:
        results = {}
        for path, fn in GETS.items():
            try:
                out = fn(tid, {"product": [prod], "run_id": [f"run-{prod}"]})
                results[path] = isinstance(out, (dict, list))   # replay returns a list timeline
            except Exception as e:
                results[path] = f"ERR {e}"
        bad = {k: v for k, v in results.items() if v is not True}
        # the build path must enforce the consent gate (no consent yet -> refused)
        gate = _build(tid, {"name": "x", "charter": "y"}).get("error") == "consent_required"
        ok = not bad and gate and len(GETS) >= 13
        print(f"routes ok: {len(results)-len(bad)}/{len(results)} · consent-gated build: {gate} · nav areas: {len(GETS)}")
        if bad:
            print("FAILING ROUTES:", bad)
        print("PASS: console mounts all area views for a real tenant ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE product=%s", (prod,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "serve":
        port = int(a[1]) if len(a) > 1 else 8099
        print(f"console on http://127.0.0.1:{port}")
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    else:
        sys.exit("usage: console.py serve [port] | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
