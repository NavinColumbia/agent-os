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


def _tenant(token):
    if not token:
        return None
    try:
        t = tenancy.tenant_for_token(token)
        return t["tenant_id"] if isinstance(t, dict) else t
    except Exception:
        return None


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
ORG_SCOPED_GET = {"/api/controller/state", "/api/design"}
ORG_SCOPED_POST = {"/api/controller/say", "/api/controller/choose", "/api/design/decide"}


def _fleet(tid):
    """Area 5 — every live worker across the tenant's products, flattened from the cockpit payload."""
    c = cockpit.cockpit(tid)
    workers = []
    for p in c["products"]:
        for w in p.get("workers", []):
            workers.append({**w, "product": p["product"]})
    return {"workers": workers, "count": len(workers),
            "products_active": sum(1 for p in c["products"] if p.get("workers"))}


def _controller_state(tid, org_id):
    """The per-org controller: its thread, phase/gate, and the conversation (for the closed-loop chat)."""
    if not org_id:
        orgs = orgsmod.list_orgs(tid)
        if not orgs:
            return {"error": "no org — create one first"}
        org_id = orgs[0]["org_id"]
    thread = loopcontroller.thread_for_org(tid, org_id)
    st = loopcontroller.state(thread)
    return {"org_id": org_id, "thread": thread, "phase": st.get("phase"), "awaiting": st.get("awaiting"),
            "messages": orchestrator.history(tid, thread)}


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
    "/api/cockpit": lambda tid, q: cockpit.cockpit(tid),
    "/api/projects": lambda tid, q: {"projects": projectsview.list_projects(tid)},
    "/api/project": lambda tid, q: projectsview.project_detail(tid, q.get("product", [""])[0]),
    "/api/fleet": lambda tid, q: _fleet(tid),
    "/api/observability": lambda tid, q: traceview.overview(tid),
    "/api/runs": lambda tid, q: {"runs": traceview.runs(tid)},
    "/api/replay": lambda tid, q: traceview.replay(tid, q.get("run_id", [""])[0]),
    "/api/approvals": lambda tid, q: approvals.inbox(tid),
    "/api/integrations": lambda tid, q: {"integrations": integrationsview.status(tid)},
    "/api/billing": lambda tid, q: billingview.billing_view(tid),
    "/api/forecast": lambda tid, q: forecast.forecast(tid),
    "/api/health": lambda tid, q: cockpit.health(tid),
    "/api/company": lambda tid, q: cockpit.company_summary(tid),
    "/api/comms_graph": lambda tid, q: cockpit.comms_graph(tid),
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
    "/api/agents": lambda tid, q: {"agents": customagents.list_agents(tid), "roles": list(customagents.ALLOWED_ROLES)},
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
    "/api/orgs/new": lambda tid, q, b: orgsmod.create(tid, b.get("name", "New org"), b.get("vision", "")),
    "/api/controller/say": lambda tid, q, b: loopcontroller.say(tid, loopcontroller.thread_for_org(tid, int(b.get("org") or 0)), b.get("message", "")),
    "/api/controller/choose": lambda tid, q, b: loopcontroller.choose(tid, loopcontroller.thread_for_org(tid, int(b.get("org") or 0)), int(b.get("option_id") or 0)),
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
.topbar .chip .dot{width:8px;height:8px;border-radius:50%}.dot.ok{background:var(--g)}.dot.warn{background:var(--y)}.dot.bad{background:var(--r)}
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
.stages{display:flex;gap:4px;margin:8px 0;max-width:280px}.st{flex:1;height:6px;border-radius:3px;background:#1c2733}.st.ok{background:var(--g)}.st.bad{background:var(--r)}
button{display:inline-flex;align-items:center;gap:6px;background:var(--panel2);border:1px solid var(--line2);color:var(--tx);border-radius:10px;padding:8px 14px;cursor:pointer;font-size:13px;font-weight:550;line-height:1;transition:background .15s,border-color .15s,transform .05s}
button:hover{background:var(--hover);border-color:#3a4150}button:active{transform:translateY(.5px)}
button.pri{background:var(--accent);border:1px solid var(--accent);color:#0b0d12;font-weight:600;box-shadow:inset 0 1px 0 rgba(255,255,255,.12)}button.pri:hover{background:var(--accent2);border-color:var(--accent2)}
input,select,textarea{width:100%;font-family:inherit;font-size:13.5px;color:var(--tx);background:var(--bg2);border:1px solid var(--line2);border-radius:10px;padding:9px 11px;transition:border-color .15s,box-shadow .15s}
input::placeholder,textarea::placeholder{color:var(--mut)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);background:var(--panel);box-shadow:0 0 0 3px var(--asoft)}
textarea{min-height:96px;resize:vertical}label{display:block;font-size:12px;font-weight:550;color:var(--tx2);margin:14px 0 5px}
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
</style></head><body>
<div id=signin style="display:none;max-width:440px;margin:11vh auto;padding:0 18px">
  <div class=card><div class=brand style="padding-bottom:6px;font-size:20px"><span class=mk>⬡</span> agent-os</div>
  <p class=muted style="margin:0 0 18px">Be the CEO of a company of AI agents that build &amp; ship your software.</p>
  <div id=su_signup>
    <h2 style="text-transform:none;font-size:16px;letter-spacing:0;color:var(--tx);margin:0 0 4px">Create your account</h2>
    <p class=muted style="margin:0 0 10px">Free to start — no card needed. You'll create your companies (orgs) once you're in.</p>
    <label>Your name</label><input id=su_name placeholder="Jane Doe">
    <label>Email</label><input id=su_email type=email placeholder="you@example.com">
    <label>Password</label><input id=su_pw type=password placeholder="at least 8 characters" onkeydown="if(event.key==='Enter')signUp()">
    <div style="margin-top:14px"><button class=pri onclick=signUp() style=width:100%>Create account</button></div>
    <div id=su_note class=muted style="margin-top:10px"></div>
    <p class=muted style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">Already have an account? <a onclick="suTab('in')" style="cursor:pointer;color:var(--atext)">Sign in</a></p>
  </div>
  <div id=su_signin style=display:none>
    <h2 style="text-transform:none;font-size:16px;letter-spacing:0;color:var(--tx);margin:0 0 4px">Sign in</h2>
    <label>Email</label><input id=si_email type=email placeholder="you@example.com">
    <label>Password</label><input id=si_pw type=password placeholder="your password" onkeydown="if(event.key==='Enter')signIn()">
    <div style="margin-top:14px"><button class=pri onclick=signIn() style=width:100%>Sign in</button></div>
    <div id=si_note class=muted style="margin-top:10px"></div>
    <p class=muted style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px"><a onclick="suTab('up')" style="cursor:pointer;color:var(--atext)">← Create an account</a></p>
  </div></div>
</div>
<div class=app id=app style="display:none">
<div class=side><div class=brand><span class=mk>⬡</span> agent-os</div><div class=nav id=nav style=flex:1></div>
  <a class=nav-foot id=statusfoot onclick="go('status')" style="display:flex;gap:10px;align-items:center;padding:8px 10px;border-radius:8px;color:var(--tx2);cursor:pointer;font-size:13px;border-top:1px solid var(--line);margin-top:8px"><span class="dot ok" id=statusdot></span><span class=lbl>Status</span></a>
</div>
<div class=colmain>
  <div class=topbar><span class=title id=tbtitle>Cockpit</span>
    <span class=chip id=orgchip onclick="go('orgs')" title="Switch org" style=cursor:pointer><span id=orgname>—</span> ▾</span>
    <span class=sp></span>
    <span class=chip id=spendchip onclick="go('billing')"><span class="dot ok" id=spenddot></span><span id=spendtxt>—</span></span>
    <button class=tbtn onclick="go('notifications')" title=Notifications>◔<span class=nb id=bellbadge style=display:none></span></button>
    <button class=tbtn onclick="go('help')" title=Help>?</button>
    <button class=tbtn onclick="toggleAcct()" title=Account>☰</button>
    <div class=menu id=acctmenu style=display:none><a onclick="go('settings')">Settings</a><a onclick="go('team')">Org chart</a><a onclick="go('billing')">Billing &amp; plan</a><a onclick="go('settings')">AI consent &amp; data</a><a onclick="go('status')">Status</a><a onclick=signOut()>Sign out</a></div>
  </div>
  <div class=main><div id=view></div></div>
</div>
</div>
<script>
const NAV=[
 ['Direct',[['controller','Controller','🧭'],['chat','Quick build','💬'],['agents','Agents','🤖'],['templates','Templates','▦']]],
 ['This org',[['cockpit','Cockpit','◧'],['projects','Projects','▤'],['design','Design','🎨'],['agentic','Agentic features','⚡'],['approvals','Approvals','✓'],['activity','Activity','◴']]],
 ['All orgs',[['orgs','My orgs','🏢'],['portfolio','Portfolio','◎']]],
 ['Business',[['billing','Billing','▣'],['providers','Providers','🔌'],['integrations','Integrations','⌁']]],
];
const LABEL={controller:'Controller',chat:'Quick build',build:'New build',agents:'Agents',templates:'Templates',cockpit:'Cockpit',projects:'Projects',design:'Design',agentic:'Agentic features',approvals:'Approvals',activity:'Activity',orgs:'My orgs',portfolio:'Portfolio',billing:'Billing',providers:'Providers',integrations:'Integrations',notifications:'Notifications',help:'Help',team:'Org',settings:'Settings',status:'Status'};
const $=s=>document.querySelector(s);let TOK=localStorage.getItem('aos_tenant')||'';let CUR='cockpit';let BADGES={};
let ORG=parseInt(localStorage.getItem('aos_org')||'0')||0;let ORGS=[];
async function loadOrgs(){try{const d=await get('/api/orgs');ORGS=d.orgs||[];
  if(ORG && !ORGS.some(o=>o.org_id==ORG)){ORG=0;localStorage.removeItem('aos_org');}
  if(!ORG&&ORGS.length){ORG=ORGS[0].org_id;localStorage.setItem('aos_org',ORG);}
  const cur=ORGS.find(o=>o.org_id==ORG);if($('#orgname'))$('#orgname').textContent=cur?cur.name:(ORGS.length?'pick an org':'no orgs');}catch(e){}}
function switchOrg(id){ORG=id;localStorage.setItem('aos_org',id);loadOrgs();go('controller');}
function H(){return {'Content-Type':'application/json','X-Tenant-Token':TOK}}
function showApp(on){$('#signin').style.display=on?'none':'block';$('#app').style.display=on?'flex':'none'}
function resetSession(){localStorage.removeItem('aos_org');localStorage.removeItem('aos_email');ORG=0;ORGS=[];THREAD=null;CUR='cockpit'}
function suTab(t){$('#su_signup').style.display=t==='up'?'block':'none';$('#su_signin').style.display=t==='in'?'block':'none'}
async function signUp(){
 const note=$('#su_note');const email=($('#su_email').value||'').trim();const pw=$('#su_pw').value||'';const name=($('#su_name').value||'').trim();
 if(!email){note.textContent='Enter your email';return} if(!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)){note.textContent='Enter a valid email';return} if(pw.length<8){note.textContent='Password must be at least 8 characters';return}
 resetSession();
 note.textContent='Creating your account…';
 let r;try{r=await (await fetch('/api/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,email,password:pw})})).json()}catch(e){note.textContent='Could not reach the server — is the console running?';return}
 if(r.error){note.textContent='✗ '+r.error;return}
 TOK=r.api_token;localStorage.setItem('aos_tenant',TOK);localStorage.setItem('aos_email',r.email||email);
 showApp(true);boot();
}
async function signIn(){
 const note=$('#si_note');const email=($('#si_email').value||'').trim();const pw=$('#si_pw').value||'';
 if(!email||!pw){note.textContent='Enter your email and password';return}
 resetSession();
 note.textContent='Signing in…';
 let r;try{r=await (await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pw})})).json()}catch(e){note.textContent='Could not reach the server';return}
 if(r.error){note.textContent='✗ '+r.error;return}
 TOK=r.api_token;localStorage.setItem('aos_tenant',TOK);localStorage.setItem('aos_email',r.email||email);
 showApp(true);boot();
}
function signOut(){localStorage.removeItem('aos_tenant');TOK='';resetSession();suTab('in');showApp(false)}
function toggleAcct(){const m=$('#acctmenu');m.style.display=m.style.display==='none'?'block':'none'}
async function get(p){
 if(!TOK){const e=new Error('Your session expired — sign in again with your email and password.');e.kind='auth';throw e}
 let r;try{r=await fetch(p,{headers:H()})}catch(_){const e=new Error('Can\'t reach the server — is the console running?');e.kind='net';throw e}
 let d={};try{d=await r.json()}catch(_){}
 if(r.status===401){const e=new Error(d.error||'Your session expired — sign in again with your email and password.');e.kind='auth';throw e}
 if(!r.ok||(d&&d.error)){const e=new Error((d&&d.error)||('Request failed ('+r.status+')'));e.kind='error';throw e}
 return d||{};
}
async function post(p,b){
 try{const r=await fetch(p,{method:'POST',headers:H(),body:JSON.stringify(b||{})});let d={};try{d=await r.json()}catch(_){}; return d||{}}
 catch(_){return {error:'request failed'}}
}
function authCard(msg){return `<div class=card><h2>Sign in</h2><p class=muted>${esc(msg||'Your session expired — sign in again with your email and password.')}</p></div>`}
function errCard(k,msg){return `<div class=card><h2>Something went wrong</h2><p class=muted>${esc(msg)}</p><div style=margin-top:8px><button class=pri onclick="go('${k}')">retry</button></div></div>`}
function esc(s){return (s==null?'':''+s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function pill(txt,cls){return `<span class="pill ${cls||''}">${esc(txt)}</span>`}
function emptyB(ic,t,m,cta){return '<div class=empty><div class=ic>'+ic+'</div><h3>'+esc(t)+'</h3><p>'+esc(m)+'</p>'+(cta||'')+'</div>'}
function renderNav(){$('#nav').innerHTML=NAV.map(([sec,items])=>`<div class=nsec>${sec}</div>`+items.map(([k,l,ic])=>`<a class="${k==CUR?'on':''}" onclick="go('${k}')"><span class=ico>${ic}</span><span class=lbl>${l}</span>${BADGES[k]?`<span class=b>${BADGES[k]}</span>`:''}</a>`).join('')).join('')}
async function refreshTopbar(){
 try{const b=await get('/api/billing');const f=await get('/api/forecast').catch(()=>null);
   const lvl=f?(f.level==='over'?'bad':(f.level==='warn'?'warn':'ok')):'ok';
   $('#spenddot').className='dot '+lvl;$('#spendtxt').textContent='$'+((b.usage&&b.usage.tokens!=null)?(b.invoice&&b.invoice.total!=null?b.invoice.total:'') : '')+(f?(' · '+f.pct_of_quota_projected+'% quota'):'');
   if(!$('#spendtxt').textContent.trim()||$('#spendtxt').textContent==='$')$('#spendtxt').textContent=(b.plan||'')+(f?(' · '+f.pct_of_quota_projected+'%'):'');
 }catch(e){}
 try{const n=await get('/api/notifications');const bb=$('#bellbadge');if(n.unread>0){bb.style.display='inline-flex';bb.textContent=n.unread}else bb.style.display='none'}catch(e){}
 try{const ap=await get('/api/approvals');BADGES.approvals=(ap.items||[]).filter(i=>i.kind!=='consent').length;renderNav()}catch(e){}
 try{const s=await get('/api/status');$('#statusdot').className='dot '+({operational:'ok',degraded:'warn',major_outage:'bad'}[s.verdict]||'ok')}catch(e){}
}
async function go(k){CUR=k;renderNav();$('#tbtitle').textContent=LABEL[k]||k;$('#acctmenu').style.display='none';$('#view').innerHTML='<div class=card><div class=skel style=width:40%></div><div class=skel style=width:75%></div></div>';
 try{await VIEWS[k]()}catch(e){if(e.kind==='auth'){signOut();return}$('#view').innerHTML=errCard(k,e.message)}}
function kpis(arr){return '<div class=kpis>'+arr.map(a=>`<div class=kpi><b>${esc(a[1])}</b><span>${esc(a[0])}</span></div>`).join('')+'</div>'}
function stages(ss){return '<div class=stages>'+ss.map(s=>`<div class="st ${s.done?(s.ok===false?'bad':'ok'):''}" title="${s.stage}"></div>`).join('')+'</div>'}

let THREAD=null;let CTLBUSY=false;let CHATBUSY=false;
const VIEWS={
 controller:async()=>{
  if(!ORG){await loadOrgs();if(!ORG){$('#view').innerHTML=emptyB('🏢','No org yet','Create your first organization — then your controller will help you build it.','<button class=pri onclick="go(\'orgs\')">Create an org</button>');return}}
  let d;try{d=await get('/api/controller/state?org='+ORG)}catch(e){$('#view').innerHTML=errCard('controller',e.message);return}
  if(d.error){$('#view').innerHTML='<div class=card>'+esc(d.error)+'</div>';return}
  const GATE={user_feedback:'your reply',user_approval:'your go-ahead',credentials:'a connected provider',fleet:'your agents to finish'};
  const rawPhase=d.phase||'DISCOVER';const phase=rawPhase.charAt(0)+rawPhase.slice(1).toLowerCase();const gate=d.awaiting?(' · waiting on '+esc(GATE[d.awaiting]||d.awaiting)):'';
  $('#view').innerHTML=`<h1>Controller</h1><p class=sub>Tell your controller what to build. It researches, brings options, designs, and ships — asking you at each step. <b>${esc(phase)}</b>${gate}</p>
   <div class=card id=clog style="max-height:54vh;overflow:auto;display:flex;flex-direction:column;gap:10px"></div>
   <div class=card><div class=row><input id=cmsg placeholder="e.g. build a competitor to YouTube" onkeydown="if(event.key==='Enter')ctlSend()"><button class=pri id=ctlsend onclick=ctlSend()>Send</button></div><div id=cnote class=muted style=margin-top:6px></div></div>`;
  ctlRender(d.messages||[]);
  if(!window.CTLPOLL)window.CTLPOLL=setInterval(async()=>{if(CUR!=='controller'){clearInterval(window.CTLPOLL);window.CTLPOLL=null;return}if(CTLBUSY)return;try{const s=await get('/api/controller/state?org='+ORG);ctlRender(s.messages||[])}catch(e){}},5000);
 },
 orgs:async()=>{const d=await get('/api/orgs');ORGS=d.orgs||[];
  let h='<h1>My orgs</h1><p class=sub>Each org is its own company — its own controller, research, design, build and budget. You can run as many as you like.</p>';
  h+='<div class=card><h2>Create an org</h2><div class=row><input id=onm placeholder="YouTube competitor"><input id=ovis placeholder="one-line vision (optional)"><button class=pri onclick=orgNew()>Create</button></div></div>';
  h+='<div class=grid>'+(ORGS.length?ORGS.map(o=>`<div class=tile><div class="row spread"><b>${esc(o.name)}</b>${o.org_id==ORG?pill('active','ok'):''}</div><div class=muted style=margin:6px_0>${esc(o.vision||'—')} · ${esc(o.stage)} · ${o.products} product(s)</div><button class=pri onclick="switchOrg(${o.org_id})">Open</button></div>`).join(''):emptyB('🏢','No orgs yet','Create your first organization above.'))+'</div>';
  $('#view').innerHTML=h;},
 portfolio:async()=>{let p={},a={},f={};try{p=await get('/api/portfolio')}catch(e){}try{a=await get('/api/portfolio/analytics')}catch(e){}try{f=await get('/api/portfolio/failures')}catch(e){}
  const t=p.totals||{};let h='<h1>Portfolio</h1><p class=sub>Everything across all your orgs.</p>';
  h+=kpis([['Orgs',t.orgs||0],['Products',t.products||0],['Live',t.live||0],['Building',t.building||0],['Failed',t.failed||0],['Spend $',t.spend_usd||0]]);
  h+='<div class=card><h2>Your orgs</h2><table><tr><th>org</th><th>stage</th><th>products</th><th>live</th><th>spend</th></tr>'+((p.orgs||[]).length?p.orgs.map(o=>`<tr><td>${esc(o.name)}</td><td>${esc(o.stage)}</td><td>${o.products}</td><td>${o.live||0}</td><td>$${o.spend_usd||0}</td></tr>`).join(''):'<tr><td class=muted colspan=5>no orgs yet</td></tr>')+'</table></div>';
  const fails=(f.failures||f.items||[]);h+='<div class=card><h2>What needs attention (across orgs)</h2>'+(fails.length?fails.map(x=>`<div class=item>${pill('failed','bad')} ${esc(x.product||x.title||'')} <span class=muted>${esc(x.org||x.org_name||'')} ${esc(x.decision||'')}</span></div>`).join(''):'<div class=muted>nothing broken across your orgs ✓</div>')+'</div>';
  $('#view').innerHTML=h;},
 agentic:async()=>{const d=await get('/api/agentfeatures');
  let h='<h1>Agentic features</h1><p class=sub>Embed your AI fleet INTO your product — a button or endpoint your users/staff trigger that runs an agent. We build these into your app (you host it); we don\'t run them for you. Tell your Controller which you want during design.</p>';
  h+='<div class=grid>'+(d.features||[]).map(f=>`<div class=tile><div class="row spread"><b>${esc(f.name)}</b>${pill(f.audience,f.audience=='external'?'accent':'')}</div><div class=muted style=margin:6px_0>${esc(f.blurb)}</div><div class=muted>${pill('trigger: '+(f.trigger||f.surface),f.trigger=='event'?'warn':'')} ${esc(f.surface)} · ${esc(f.category)}</div></div>`).join('')+'</div>';
  $('#view').innerHTML=h;},
 design:async()=>{if(!ORG){$('#view').innerHTML='<div class=card>Pick an org first.</div>';return}const d=await get('/api/design?org='+ORG);
  let h='<h1>Design</h1><p class=sub>Prototype screens your fleet drafted — for your cockpit, your team, and your external users.</p>';
  const g=d.gallery||[];h+='<div class=grid>'+(g.length?g.map(s=>`<div class=tile><div class="row spread"><b>${esc(s.surface||'')}</b>${pill(s.status,s.status=='approved'?'ok':'')}</div><div class=muted style=margin:6px_0>${esc(s.title||'')}</div>${s.status!='approved'?`<button class=pri onclick="designOk(${s.id})">approve</button>`:''}</div>`).join(''):emptyB('🎨','No prototypes yet','When your controller reaches the design phase, screens appear here.'))+'</div>';
  $('#view').innerHTML=h;},
 chat:async()=>{
  if(!THREAD){const r=await post('/api/chat/new',{});THREAD=r.thread}
  const CHIPS=['Track my gym members','An invoice generator','A URL shortener','A booking page for my salon','An internal tool for my team'];
  $('#view').innerHTML=`<h1>Direct your fleet</h1><p class=sub>Describe what you want in plain words. I'll ask questions, then build it — you approve.</p>
   <div class=chips>`+CHIPS.map(c=>`<span class=chip-s onclick="chipFill('${c.replace(/'/g,"")}')">${esc(c)}</span>`).join('')+`</div>
   <div class=card id=chatlog style="max-height:52vh;overflow:auto;display:flex;flex-direction:column;gap:10px"></div>
   <div class=card><div class=row><input id=msg placeholder="e.g. I want an app to track my gym members…" onkeydown="if(event.key==='Enter')chatSend()"><button class=pri id=chatsend onclick=chatSend()>Send</button></div><div id=chatnote class=muted style=margin-top:6px></div></div>`;
  await chatRender();
 },
 cockpit:async()=>{const d=await get('/api/cockpit');const s=d.summary||{},b=d.budget||{};d.products=d.products||[];d.communications=d.communications||[];d.queue=d.queue||{};
  let fc=null;try{fc=await get('/api/forecast')}catch(e){}
  let ql={};try{const q=await get('/api/quality');(q.products||[]).forEach(p=>ql[p.product]=p)}catch(e){}
  let ob=null;try{ob=await get('/api/onboarding')}catch(e){}
  let co=null;try{co=await get('/api/company')}catch(e){}
  let hl=null;try{hl=await get('/api/health')}catch(e){}
  let ls={};try{const l=await get('/api/livestatus');(l.products||[]).forEach(p=>ls[p.product]=p)}catch(e){}
  let h='<h1>Cockpit</h1><p class=sub>Your whole company at a glance.</p>';
  if(co){const cm={healthy:'ok',attention:'warn',critical:'bad'}[co.verdict]||'ok';h+=`<div class=card><div class=row><span class="dot ${cm}" style="width:10px;height:10px;border-radius:50%"></span> <b>${esc(co.line||co.verdict)}</b></div></div>`;}
  if(ob&&!ob.completed&&ob.step!=='done'){h+=`<div class=card style="border-color:var(--accent)"><div class="row spread"><span><b>Get set up</b> — ${esc(ob.framing||'')} Next: <b>${esc(ob.step)}</b></span><span><button class=pri onclick="obContinue('${esc(ob.step)}')">continue</button> <button onclick="post('/api/onboarding/skip',{}).then(()=>go('cockpit'))">skip</button></span></div></div>`;}
  h+=kpis([['Products',s.products||0],['Launched',s.launched||0],['Building',s.building||0],['Failed',s.failed||0],['Workers',s.live_workers||0],['Spend $',s.spend_usd||0]]);
  if(fc){const fcls=fc.level==='over'?'bad':(fc.level==='warn'?'warn':'ok');h+=`<div class=card><h2>Budget forecast</h2><div class="row spread"><span>${esc(fc.headline||'')}</span>${pill(fc.pct_of_quota_projected+'% projected',fcls)}</div><div class=meta>burn ~${fc.burn_tokens_per_day} tok/day · $${fc.burn_usd_per_day}/day${fc.eta_days_to_quota?(' · hits quota in ~'+fc.eta_days_to_quota+'d'):''}</div></div>`;}
  if(hl&&!hl.ok){let hi='';
    (hl.blocked||[]).forEach(b=>hi+=`<div class=item>${pill('blocked','warn')} ${esc(b.agent||'')} waiting on ${esc(b.waiting_on||'')} ${b.minutes?('· '+b.minutes+'m'):''}</div>`);
    (hl.deadlocks||[]).forEach(d2=>hi+=`<div class=item>${pill('deadlock','bad')} ${esc(JSON.stringify(d2).slice(0,80))}</div>`);
    (hl.conflicts||[]).forEach(cf=>hi+=`<div class=item>${pill('conflict','warn')} ${esc(JSON.stringify(cf).slice(0,80))}</div>`);
    if((hl.dead_letter||0))hi+=`<div class=item>${pill('stuck','bad')} ${hl.dead_letter} task(s) need a human decision — see Approvals</div>`;
    if(hi)h+='<div class=card><h2>Needs attention · org health</h2>'+hi+'</div>';}
  else if(hl&&hl.ok){h+='<div class=card><h2>Org health</h2><div class=row>'+pill('all clear','ok')+' <span class=muted>no blocked or stuck agents</span></div></div>';}
  h+='<div class=card><h2>Projects</h2>'+(d.products.length?d.products.map(p=>`<div class=item><div class="row spread"><span><b>${esc(p.product)}</b> ${pill(p.result,p.ready?'ok':(p.failed?'bad':''))} ${ql[p.product]?pill('✓ '+ql[p.product].overall,(ql[p.product].overall=='verified'||ql[p.product].overall=='passed')?'ok':(ql[p.product].overall=='failed'?'bad':'')):''} ${ls[p.product]&&ls[p.product].url?'<a href="'+ls[p.product].url+'" target=_blank>'+(ls[p.product].reachable?'● live':'open ↗')+'</a>':''} ${p.halted?pill('paused','bad'):''}</span><span>${p.halted?`<button onclick="ctl('${p.product}','resume')">resume</button>`:`<button onclick="ctl('${p.product}','pause')">pause</button>`}</span></div>${stages(p.stages)}<div class=muted>$${p.cost_usd} · ${p.tokens} tok · ${p.workers.length} workers</div></div>`).join(''):'<div class=muted>none yet — start one in New build</div>')+'</div>';
  h+='<div class=grid><div class=card><h2>Communications</h2><table>'+(d.communications.length?d.communications.map(m=>`<tr><td>${m.ts}</td><td>${esc(m.from)}</td><td>→ ${esc(m.to)}</td><td>${esc(m.intent)}</td></tr>`).join(''):'<tr><td class=muted>no recent agent messages</td></tr>')+'</table></div>';
  h+=`<div class=card><h2>Work queue</h2>${kpis([['pending',d.queue.pending],['active',d.queue.active],['dead',d.queue.dead]])}</div></div>`;
  $('#view').innerHTML=h;},
 build:async()=>{$('#view').innerHTML=`<h1>New build</h1><p class=sub>Describe a product; the governed factory builds, tests and ships it.</p>
  <div class=card><label>Name</label><input id=bn placeholder=splitbill><label>Type</label><select id=bk onchange=showEst()><option value=lib>Python library</option><option value=web>Web app</option><option value=service>API service</option></select><label>What should it do?</label><textarea id=bc placeholder="Describe the API, behaviours, edge cases…"></textarea><div id=est class=muted style=margin-top:8px></div><div style=margin-top:10px><button class=pri onclick=doBuild()>Build it</button></div><div id=bnote class=muted style=margin-top:8px></div></div>`;showEst();},
 agents:async()=>{const d=await get('/api/agents');d.agents=d.agents||[];const roles=(d.roles||['research-growth']);
  let h='<h1>Your agents</h1><p class=sub>It\'s a factory — hire standing agents that work on a schedule and report back to you. E.g. a weekly market-watch.</p>';
  h+='<div class=card><h2>Your standing agents</h2>'+(d.agents.length?d.agents.map(a=>`<div class=item><div class="row spread"><span><b>${esc(a.name)}</b> ${pill(a.role)} ${pill(a.trigger==='recurring'?('every '+Math.round((a.interval_s||0)/86400)+'d'):'manual',a.trigger==='recurring'?'accent':'')} ${a.enabled?pill('on','ok'):pill('off')}</span><span><button onclick="agentRun(${a.id})">run now</button> <button onclick="agentToggle(${a.id},${a.enabled?'false':'true'})">${a.enabled?'pause':'enable'}</button> <button class=danger onclick="agentDel(${a.id})">delete</button></span></div><div class=muted>last run: ${a.last_run?esc(a.last_run):'never'} ${a.last_status?('· '+esc(a.last_status)):''}</div></div>`).join(''):emptyB('🤖','No agents yet','Create a standing agent below — it runs on a schedule and reports into your feed.'))+'</div>';
  h+='<div class=card><h2>Create an agent</h2><label>Name</label><input id=an placeholder="Market Watch"><div class=grid><div><label>Specialty</label><select id=ar>'+roles.map(r=>`<option value="${r}">${r}</option>`).join('')+'</select></div><div><label>Runs</label><select id=at><option value=manual>On demand</option><option value=recurring>Every week</option></select></div></div><label>What should it do?</label><textarea id=ai placeholder="Every week, scan my market for new competitors and pricing changes; give me 3 prioritized takeaways with sources."></textarea><div style=margin-top:10px><button class=pri onclick=agentCreate()>Create agent</button></div><div id=anote class=muted style=margin-top:8px></div></div>';
  $('#view').innerHTML=h;},
 templates:async()=>{const d=await get('/api/templates');$('#view').innerHTML=`<h1>Templates</h1><p class=sub>Start from a curated, factory-ready blueprint.</p><div class=grid>`+(d.templates||[]).map(t=>`<div class=tile><div class="row spread"><b>${esc(t.name)}</b>${pill(t.kind)}</div><div class=muted style=margin:6px_0>${esc(t.blurb)}</div><button class=pri onclick="buildTpl('${t.slug}')">Build this</button></div>`).join('')+'</div>';},
 projects:async()=>{const d=await get('/api/projects');d.projects=d.projects||[];$('#view').innerHTML='<h1>Projects</h1><p class=sub>Everything you have built.</p><div class=card>'+(d.projects.length?d.projects.map(p=>`<div class=item><div class="row spread"><span><b>${esc(p.product)}</b> ${pill(p.result,p.ready?'ok':(p.failed?'bad':''))}</span><span class=muted>$${p.cost_usd||0} · ${p.stages_done||0} stages</span></div></div>`).join(''):emptyB('▤','No projects yet','Describe your first product and the factory builds, tests and ships it.','<button class=pri onclick="go(\'chat\')">Start your first build</button>'))+'</div>';},
 activity:async()=>{const o=await get('/api/observability');let fl={workers:[]};try{fl=await get('/api/fleet')}catch(e){}fl.workers=fl.workers||[];
  let h='<h1>Activity</h1><p class=sub>Runs, errors, spend, and the live workers across your fleet.</p>';
  h+=kpis([['Runs',o.runs||0],['Steps',o.steps||0],['Errors',o.errors||0],['Cost $',o.cost_usd||0],['Workers',fl.workers.length]]);
  h+='<div class=card><h2>Live workers</h2><table><tr><th>agent</th><th>role</th><th>status</th><th>product</th></tr>'+(fl.workers.length?fl.workers.map(w=>`<tr><td>${esc(w.agent)}</td><td>${esc(w.role)}</td><td>${pill(w.status,w.status=='active'?'ok':'')}</td><td>${esc(w.product)}</td></tr>`).join(''):'<tr><td class=muted colspan=4>no live workers right now</td></tr>')+'</table></div>';
  h+='<div class=card><h2>By stage</h2><table><tr><th>stage</th><th>steps</th><th>errors</th><th>cost</th><th>avg s</th></tr>'+(o.by_stage||[]).map(s=>`<tr><td>${esc(s.stage)}</td><td>${s.steps}</td><td>${s.errors}</td><td>$${s.cost_usd}</td><td>${s.avg_elapsed_s}</td></tr>`).join('')+'</table></div>';
  h+='<div class=card><h2>Recent errors</h2>'+((o.recent_errors||[]).length?o.recent_errors.map(e=>`<div class=item><b>${esc(e.product)}</b> · ${esc(e.stage)} <span class=muted>${e.ts}</span><div><code>${esc(e.snippet)}</code></div></div>`).join(''):'<div class=muted>no errors — clean ✓</div>')+'</div>';
  $('#view').innerHTML=h;},
 fleet:async()=>{const d=await get('/api/fleet');d.workers=d.workers||[];$('#view').innerHTML='<h1>Agent fleet</h1><p class=sub>Live workers across your products.</p>'+kpis([['Live workers',d.count||0],['Active products',d.products_active||0]])+'<div class=card><table><tr><th>agent</th><th>role</th><th>status</th><th>product</th><th>task</th></tr>'+(d.workers.length?d.workers.map(w=>`<tr><td>${esc(w.agent)}</td><td>${esc(w.role)}</td><td>${pill(w.status,w.status=='active'?'ok':'')}</td><td>${esc(w.product)}</td><td>${esc(w.task)}</td></tr>`).join(''):'<tr><td class=muted colspan=5>no live workers right now</td></tr>')+'</table></div>';},
 observability:async()=>{const d=await get('/api/observability');$('#view').innerHTML='<h1>Observability</h1><p class=sub>Runs, errors, spend across your fleet.</p>'+kpis([['Runs',d.runs],['Steps',d.steps],['Errors',d.errors],['Cost $',d.cost_usd],['Tokens',d.tokens]])+'<div class=card><h2>By stage</h2><table><tr><th>stage</th><th>steps</th><th>errors</th><th>cost</th><th>avg s</th></tr>'+(d.by_stage||[]).map(s=>`<tr><td>${esc(s.stage)}</td><td>${s.steps}</td><td>${s.errors}</td><td>$${s.cost_usd}</td><td>${s.avg_elapsed_s}</td></tr>`).join('')+'</table></div><div class=card><h2>Recent errors</h2>'+((d.recent_errors||[]).length?d.recent_errors.map(e=>`<div class=item><b>${esc(e.product)}</b> · ${esc(e.stage)} <span class=muted>${e.ts}</span><div><code>${esc(e.snippet)}</code></div></div>`).join(''):'<div class=muted>no errors — clean</div>')+'</div>';},
 approvals:async()=>{const d=await get('/api/approvals');$('#view').innerHTML='<h1>Approvals</h1><p class=sub>Decisions awaiting you. Governed: nothing risky happens without this.</p><div class=card>'+(d.count?d.items.map(i=>i.kind=='consent'?`<div class=item><div class="row spread"><span>${pill('setup','accent')} <b>Approve AI processing</b></span><span><button class=pri onclick="decide('consent','${esc(i.ref)}','approve')">Approve AI processing</button></span></div><div class=muted>Nothing's broken — this is a one-time, revocable OK to let your AI provider process your prompts so your agents can build. Approving it unlocks your first build.</div></div>`:`<div class=item><div class="row spread"><span>${pill(i.kind,i.severity=='high'?'bad':(i.severity=='med'?'warn':''))} <b>${esc(i.title)}</b></span><span><button class=pri onclick="decide('${i.kind}','${esc(i.ref)}','${i.kind=='dead_letter'?'retry':'approve'}')">${esc(i.action_label||'approve')}</button> ${i.kind=='hire_request'||i.kind=='dead_letter'||i.kind=='blocked_build'?`<button onclick="decide('${i.kind}','${esc(i.ref)}','${i.kind=='dead_letter'?'drop':'deny'}')">${i.kind=='blocked_build'?'abandon':'deny'}</button>`:''}</span></div><div class=muted>${esc(i.detail||'')}</div></div>`).join(''):emptyB('✓','All clear','Nothing needs your decision right now. We\'ll bring anything important here.'))+'</div>';},
 integrations:async()=>{const d=await get('/api/integrations');$('#view').innerHTML='<h1>Integrations</h1><p class=sub>Connect the services your products need.</p><div class=grid>'+(d.integrations||[]).map(i=>`<div class=tile><div class=row style=margin-bottom:6px><span class=int-ic>${esc((i.name||'?')[0])}</span><b>${esc(i.name)}</b><span style=flex:1></span>${pill(i.status=='connected'?'connected':'disconnected',i.status=='connected'?'ok':'off')}</div><div class=muted style=margin:0_0_8px>${esc(i.blurb)} · ${esc(i.category)}</div>${i.status=='connected'?`<button onclick="integ('disconnect','${i.slug}')">disconnect</button>`:`<button class=pri onclick="integ('connect','${i.slug}')">connect</button>`}</div>`).join('')+'</div>';},
 billing:async()=>{const d=await get('/api/billing');$('#view').innerHTML=`<h1>Billing & plans</h1><p class=sub>Usage, quota and plan. Real payment is gated (BYO Stripe).</p>`+kpis([['Plan',d.plan],['Builds',(d.usage&&d.usage.builds)||0],['Tokens',(d.usage&&d.usage.tokens)||0]])+'<div class=card><h2>Plans</h2><table><tr><th>plan</th><th>price</th><th>builds</th><th>tokens</th><th></th></tr>'+(d.plans||[]).map(p=>`<tr><td>${esc(p.slug)} ${p.current?pill('current','ok'):''}</td><td>$${p.price}</td><td>${p.builds}</td><td>${p.tokens}</td><td>${p.current?'':`<button onclick="plan('${p.slug}')">switch</button>`}</td></tr>`).join('')+'</table></div>';},
 notifications:async()=>{const d=await get('/api/notifications');d.feed=d.feed||[];$('#view').innerHTML=`<h1>Notifications</h1><p class=sub>${d.unread||0} unread.</p><div class=card>`+(d.feed.length?d.feed.map(n=>`<div class=item><div class="row spread"><span>${pill(n.level,n.level=='urgent'?'bad':(n.level=='standard'?'':'warn'))} <b>${esc(n.title)}</b></span><span class=muted>${esc(n.category)} · ${n.created_at}</span></div><div class=muted>${esc(n.body||'')}</div></div>`).join(''):emptyB('◔','You\'re all caught up','Build updates, billing alerts and agent reports will appear here.'))+'</div>';},
 team:async()=>{let o=null;try{o=await get('/api/org')}catch(e){}
  let h='<h1>Your org</h1><p class=sub>Your company of AI agents — who does what, and who\'s working right now.</p>';
  if(o&&o.tree){const root=o.tree.find(n=>!n.reports_to)||o.tree[0];
   const node=(n)=>`<div class=item><div class="row spread"><span><b>${esc(n.title||n.role)}</b> <span class=muted>${esc(n.role)}</span> ${n.live?pill('live · '+(n.count||1),'ok'):pill('idle')}</span><span class=muted>${esc(n.task||'')}</span></div></div>`;
   h+='<div class=card><h2>Controller</h2>'+(root?node(root):'')+'</div>';
   h+='<div class=card><h2>Reports</h2>'+o.tree.filter(n=>n.reports_to).map(node).join('')+'</div>';
  } else { h+='<div class=card>'+emptyB('◍','Org unavailable','Your agent org chart will appear here.')+'</div>'; }
  $('#view').innerHTML=h;},
 settings:async()=>{const d=await get('/api/settings');const c=d.ai_consent||{};const pr=d.profile||{};$('#view').innerHTML=`<h1>Settings</h1><p class=sub>Profile, AI consent, keys, notifications.</p>
  <div class=card><h2>Profile</h2><div class=row>plan <b>${esc(pr.plan||'—')}</b> ${pr.suspended?pill('suspended','bad'):pill('active','ok')}</div></div>
  <div class=card><h2>AI consent</h2><div class=row>${c.accepted?pill('accepted','ok'):pill('not accepted','bad')} <span class=muted>${esc(c.provider||'')} ${esc(c.version||'')}</span></div><div style=margin-top:8px>${c.accepted?'<button onclick="setConsent(false)">revoke</button>':'<button class=pri onclick="setConsent(true)">accept</button>'}</div></div>
  <div class=card><h2>BYO API key</h2><div class=row>${d.byo_key_set?pill('key on file','ok'):pill('no key','warn')}</div><div style=margin-top:8px><input id=bk placeholder="sk-… (stored encrypted)"><button class=pri style=margin-top:6px onclick=saveKey()>save key</button></div></div>
  <div class=card><h2>Notification preferences</h2><table><tr><th>category</th><th>in-app</th><th>email</th><th>push</th></tr>`+(d.notification_prefs||[]).map(p=>`<tr><td>${esc(p.category)}</td><td><input type=checkbox ${p.in_app?'checked':''} onchange="pref('${p.category}',this.checked,null,null)"></td><td><input type=checkbox ${p.email?'checked':''} onchange="pref('${p.category}',null,this.checked,null)"></td><td><input type=checkbox ${p.push?'checked':''} onchange="pref('${p.category}',null,null,this.checked)"></td></tr>`).join('')+`</table></div>
  <div class=card><h2>Your data</h2><div class=row><button onclick=acctExport()>Export my data</button><button onclick=acctDelete() style="border-color:var(--r);color:var(--r)">Delete my account</button></div><div id=acctnote class=muted style=margin-top:8px></div></div>`;PREFS=d.notification_prefs;},
 providers:async()=>{const d=await get('/api/providers');$('#view').innerHTML='<h1>Model providers</h1><p class=sub>Run on Claude, on Codex, or both — you only need one. Use your subscription login (no per-token billing) OR bring an API key.</p><div class=grid>'+(d.providers||[]).map(p=>`<div class=tile><div class="row spread"><b>${esc(p.name)}</b>${p.connected?pill(p.auth_mode==='subscription'?'subscription login':'api key','ok'):pill('not connected')}</div><div class=muted style=margin:6px_0>${esc(p.blurb)} · runs on <b>${esc(p.engine)}</b></div>${p.connected?`<button onclick="provRemove('${p.slug}')">disconnect</button>`:`<div class=row><button class=pri onclick="provSub('${p.slug}')">Use subscription login</button><button onclick="provAdd('${p.slug}','${esc(p.key_hint)}')">Use API key</button></div>`}</div>`).join('')+'</div><p class=muted>The build uses your highest-priority connected provider. Subscription login uses the Claude/ChatGPT account signed in on this machine; no key → platform default.</p>';},
 help:async()=>{const d=await get('/api/help/topics');$('#view').innerHTML=`<h1>Help</h1><p class=sub>Ask me anything about using agent-os.</p>
  <div class=card><div class=row><input id=hq placeholder="e.g. how do I add my Codex key?" onkeydown="if(event.key==='Enter')helpAsk()"><button class=pri onclick=helpAsk()>Ask</button></div><div id=hans style=margin-top:10px></div></div>
  <div class=card><h2>Topics</h2>`+(d.topics||[]).map(t=>`<div class=item><b>${esc(t.area||t.key||'')}</b> <span class=muted>${esc(t.desc||t.description||'')}</span></div>`).join('')+'</div>';},
 status:async()=>{const d=await get('/api/status');const m={operational:'ok',degraded:'warn',major_outage:'bad',unknown:''}[d.verdict];$('#view').innerHTML='<h1>Status</h1><p class=sub>Live platform health.</p><div class=card><div class=row>'+pill(d.verdict,m)+'</div></div><div class=card><h2>Services</h2>'+Object.entries(d.components||{}).map(([k,v])=>{const up=v===true||v=='ok'||v=='up';return `<div class="row spread item"><span>${esc(k)}</span>${pill(up?'Operational':'Down',up?'ok':'bad')}</div>`}).join('')+`<div class="row spread item"><span>dead-letter depth</span>${pill(d.dead_letter_depth,d.dead_letter_depth?'bad':'ok')}</div></div>`;},
};
let PREFS=[];
let LAST_PROPOSAL=null;
function md(s){return esc(s).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>')}
async function chatRender(){
 if(CUR!=='chat'){if(window.CHATPOLL){clearInterval(window.CHATPOLL);window.CHATPOLL=null}return}
 if(CHATBUSY)return;
 let d;try{d=await get('/api/chat/history?thread='+THREAD)}catch(e){return}
 const log=$('#chatlog');if(!log)return;
 log.innerHTML=(d.messages||[]).map(m=>{
  const me=m.role==='user';const meta=m.meta||{};const prop=meta.proposal;const ns=(meta.kind==='next_steps')&&meta.suggestions;
  let extra='';
  if(prop){const planHtml=(prop.plan&&prop.plan.trim())?('<div style="margin:6px 0"><b>Plan</b>'+prop.plan.split('\n').filter(l=>l.trim()).map(l=>'<div class=muted style=margin-left:6px>'+esc(l.replace(/^[-*]\s*/,'• '))+'</div>').join('')+'</div>'):'';
   extra=`<div class=tile style="margin:8px 0;text-align:left"><b>Proposed build:</b> ${esc(prop.name)} ${pill(prop.kind,prop.kind==='project'?'accent':'')}${planHtml}<div class=muted style=margin:4px_0>${esc(prop.charter)}</div><div class=row style=margin-top:6px><button class=pri onclick=chatConfirm()>Approve &amp; build</button><button onclick="chipFill('Actually, change: ')">Revise</button></div></div>`;}
  if(ns)extra=`<div class=chips style="margin:8px 0">`+meta.suggestions.map(s=>`<span class=chip-s onclick="chipFill('${s.replace(/'/g,"")}')">${esc(s)}</span>`).join('')+`</div>`;
  return `<div class="msg ${me?'me':'ai'}" style="margin:8px 0"><div><span class=bubble>${md(m.content)}</span>${extra}</div></div>`;
 }).join('')||'<div class=muted>Say hello, or describe a product — I\'ll ask a couple of questions, then build it.</div>';
 log.scrollTop=log.scrollHeight;
 if(!window.CHATPOLL)window.CHATPOLL=setInterval(chatRender,5000);   // live updates as the controller reports back
}
function chipFill(t){const i=$('#msg');if(i){i.value=t;i.focus()}}
function ctlFill(t){const i=$('#cmsg');if(i){i.value=t;i.focus()}}
function ctlRender(msgs){const log=$('#clog');if(!log)return;
 log.innerHTML=(msgs||[]).map(m=>{const me=m.role==='user';const meta=m.meta||{};let extra='';
  if(meta.kind==='options'&&meta.options)extra='<div class=chips style="margin:8px 0">'+meta.options.map(o=>`<span class=chip-s onclick="ctlChoose(${o.id})">${esc(o.title||('Option '+o.id))}${o.recommended?' ★':''}</span>`).join('')+'</div>';
  else if(meta.kind==='plan'&&meta.plan)extra='<div class=tile style="margin:8px 0;text-align:left"><b>Plan: '+esc(meta.plan.name||'')+'</b> '+pill(meta.plan.kind||'')+'<div class=muted style=margin-top:4px>'+esc(meta.plan.charter||'')+'</div></div>';
  else if(meta.kind==='next_steps'&&meta.suggestions)extra='<div class=chips style="margin:8px 0">'+meta.suggestions.map(s=>`<span class=chip-s onclick="ctlFill('${s.replace(/'/g,"")}')">${esc(s)}</span>`).join('')+'</div>';
  return `<div class="msg ${me?'me':'ai'}" style="margin:8px 0"><div><span class=bubble>${md(m.content)}</span>${extra}</div></div>`;
 }).join('')||'<div class=muted>Describe what you want to build — I\'ll ask a few questions, then research it and bring you options.</div>';
 log.scrollTop=log.scrollHeight;
}
async function ctlSend(){
 if(CTLBUSY)return;const i=$('#cmsg');const m=(i?i.value:'').trim();if(!m)return;CTLBUSY=true;
 const btn=$('#ctlsend');if(btn)btn.disabled=true;if(i){i.value='';i.disabled=true}
 const log=$('#clog');if(log){log.insertAdjacentHTML('beforeend','<div class="msg me" style="margin:8px 0"><div><span class=bubble>'+esc(m)+'</span></div></div><div class="msg ai" id=ctltyping style="margin:8px 0"><div><span class=bubble><span class=muted>… thinking</span></span></div></div>');log.scrollTop=log.scrollHeight}
 $('#cnote').textContent='thinking…';
 try{await post('/api/controller/say',{org:ORG,message:m});}
 finally{CTLBUSY=false;if(btn)btn.disabled=false;if(i){i.disabled=false;i.focus()}}
 const t=$('#ctltyping');if(t)t.remove();$('#cnote').textContent='';go('controller');
}
async function ctlChoose(oid){await post('/api/controller/choose',{org:ORG,option_id:oid});go('controller');}
async function orgNew(){const r=await post('/api/orgs/new',{name:($('#onm')||{}).value||'New org',vision:($('#ovis')||{}).value||''});if(r.org_id)switchOrg(r.org_id);}
async function designOk(id){await post('/api/design/decide',{org:ORG,id,status:'approved'});go('design');}
async function chatSend(){
 if(CHATBUSY)return;const i=$('#msg');const m=(i?i.value:'').trim();if(!m)return;CHATBUSY=true;
 const btn=$('#chatsend');if(btn)btn.disabled=true;if(i){i.value='';i.disabled=true}
 const log=$('#chatlog');if(log){log.insertAdjacentHTML('beforeend','<div class="msg me" style="margin:8px 0"><div><span class=bubble>'+esc(m)+'</span></div></div><div class="msg ai" id=chattyping style="margin:8px 0"><div><span class=bubble><span class=muted>… thinking</span></span></div></div>');log.scrollTop=log.scrollHeight}
 $('#chatnote').textContent='thinking…';
 try{await post('/api/chat/say',{thread:THREAD,message:m});}
 finally{CHATBUSY=false;if(btn)btn.disabled=false;if(i){i.disabled=false;i.focus()}}
 $('#chatnote').textContent='';await chatRender();
}
async function chatConfirm(){
 $('#chatnote').textContent='starting build…';const r=await post('/api/chat/confirm',{thread:THREAD});
 $('#chatnote').textContent=r.error?('✗ '+(r.error==='consent_required'?'accept AI consent in Settings first':r.error)):('building '+r.product+' — see Cockpit');
}
async function showEst(){const k=($('#bk')||{}).value||'lib';let e;try{e=await get('/api/estimate?kind='+k)}catch(_){return}if($('#est'))$('#est').textContent='Estimate: '+(e.note||('~$'+e.cost_usd_estimate+', ~'+e.minutes_estimate+' min'));}
async function doBuild(){$('#bnote').textContent='submitting…';const r=await post('/api/build',{name:$('#bn').value,kind:$('#bk').value,charter:$('#bc').value});$('#bnote').textContent=r.error?('✗ '+(r.error==='consent_required'?'accept AI consent in Settings first':r.error)):('building '+r.product+' — see Cockpit');}
async function obContinue(step){
 const SCREEN={welcome:'providers',provider:'providers',consent:'settings',first_build:'chat'};
 const NEXT={welcome:'provider',provider:'consent',consent:'first_build',first_build:'done'};
 await post('/api/onboarding/advance',{step:NEXT[step]||'done'});   // record progress past the current step
 go(SCREEN[step]||'chat');
}
async function buildTpl(slug){
 const r=await post('/api/build_template',{slug});
 if(r.error==='consent_required'){go('settings');return}
 if(r.error){alert('Could not build: '+r.error);return}
 go('cockpit');
}
async function ctl(p,a){await post('/api/control',{product:p,action:a});go('cockpit');}
async function decide(kind,ref,verdict){await post('/api/approvals/decide',{kind,ref,verdict});go('approvals');}
async function integ(act,slug){let secret=null;if(act=='connect')secret=prompt('API key / secret for '+slug+' (leave blank if OAuth):')||null;await post('/api/integrations/'+act,{slug,secret});go('integrations');}
async function plan(p){await post('/api/billing/plan',{plan:p});go('billing');}
async function provAdd(slug,hint){const k=prompt('Paste your '+slug+' API key ('+hint+'):');if(k===null)return;await post('/api/providers/connect',{provider:slug,mode:'api_key',key:k});go('providers');}
async function provSub(slug){await post('/api/providers/connect',{provider:slug,mode:'subscription'});go('providers');}
async function agentCreate(){const t=$('#at').value;const r=await post('/api/agents/create',{name:$('#an').value,instructions:$('#ai').value,role:$('#ar').value,trigger:t,interval_s:t==='recurring'?604800:0,output:'report'});$('#anote').textContent=r.error?('✗ '+r.error):'agent created';if(!r.error)go('agents');}
async function agentRun(id){await post('/api/agents/run',{id});$('#anote')&&($('#anote').textContent='running — it\'ll report into your notifications');}
async function agentToggle(id,en){await post('/api/agents/toggle',{id,enabled:en});go('agents');}
async function agentDel(id){if(!confirm('Delete this agent?'))return;await post('/api/agents/delete',{id});go('agents');}
async function provRemove(slug){await post('/api/providers/remove',{provider:slug});go('providers');}
async function helpAsk(){const q=$('#hq').value.trim();if(!q)return;$('#hans').innerHTML='<span class=muted>thinking…</span>';const r=await post('/api/help/ask',{question:q});$('#hans').innerHTML='<div class=tile>'+esc(r.answer||r.error||'(no answer)')+(r.suggested_area?` <button onclick="go('${r.suggested_area}')">go there</button>`:'')+'</div>';}
async function setConsent(a){await post('/api/settings/consent',{accept:a});go('settings');}
async function saveKey(){await post('/api/byok',{key:$('#bk').value});go('settings');}
async function acctExport(){$('#acctnote').textContent='preparing export…';const r=await post('/api/account/export',{});$('#acctnote').textContent=r.ok?('Export ready ('+r.products+' products) on the server: '+(r.path||'')):'export failed';}
async function acctDelete(){const r=await post('/api/account/delete',{confirm:false});if(!confirm('Permanently delete your account and ALL data? This cannot be undone.'))return;const r2=await post('/api/account/delete',{confirm:true});if(r2.ok){localStorage.removeItem('aos_tenant');TOK='';$('#view').innerHTML='<div class=card>Your account and data were deleted. Goodbye.</div>';}}
async function pref(cat,ia,em,pu){const cur=(PREFS||[]).find(p=>p.category==cat)||{in_app:true,email:true,push:false};await post('/api/settings/pref',{category:cat,in_app:ia==null?cur.in_app:ia,email:em==null?cur.email:em,push:pu==null?cur.push:pu});}
async function boot(){renderNav();refreshTopbar();await loadOrgs();
 let ob=null;try{ob=await get('/api/onboarding')}catch(e){}
 if(ob&&!ob.completed&&ob.step!=='done')go('cockpit');   // first-run: Cockpit renders the guided onboarding banner
 else go(ORG?'controller':'orgs');
 setInterval(refreshTopbar,15000);setInterval(()=>{if(['cockpit','activity'].includes(CUR))go(CUR)},6000)}
document.addEventListener('click',e=>{if(!e.target.closest('#acctmenu')&&!String(e.target.getAttribute&&e.target.getAttribute('onclick')||'').includes('toggleAcct'))$('#acctmenu').style.display='none'});
if(TOK){showApp(true);boot()}else{showApp(false)}
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
        if not tid:
            return self._json(401, {"error": "sign up first"})
        q = parse_qs(u.query)
        if p in ORG_SCOPED_GET and not _owns(tid, int((q.get("org", ["0"])[0]) or 0)):
            return self._json(403, {"error": "not your org"})
        try:
            self._json(200, fn(tid, q))
        except Exception as e:
            self._json(500, {"error": str(e)[:200]})

    def do_POST(self):
        u = urlparse(self.path); p = u.path
        if p in ("/api/signup", "/api/login"):            # UNAUTHENTICATED: real email+password accounts
            import auth
            b = self._body()
            try:
                if p == "/api/signup":
                    r = auth.signup(b.get("email", ""), b.get("password", ""),
                                    (b.get("name") or "").strip()[:60] or None, b.get("plan", "free"))
                else:
                    r = auth.login(b.get("email", ""), b.get("password", ""))
                return self._json(200 if not r.get("error") else 400, r)
            except Exception as e:
                return self._json(400, {"error": str(e)[:200]})
        fn = POSTS.get(p)
        if not fn:
            return self._json(404, {"error": "not found"})
        tid = _tenant(self.headers.get("X-Tenant-Token"))
        if not tid:
            return self._json(401, {"error": "sign up first"})
        body = self._body()
        if p in ORG_SCOPED_POST and not _owns(tid, int(body.get("org") or 0)):
            return self._json(403, {"error": "not your org"})
        if p == "/api/xorg/propose":
            for k in ("source", "target"):
                if not _owns(tid, int(body.get(k) or 0)):
                    return self._json(403, {"error": "not your org"})
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
