#!/usr/bin/env python3
"""cockpit.py — the CEO cockpit: ONE tenant-scoped pane of glass over a user's whole factory.

The front door lets a user start builds; the cockpit is where they DIRECT and WATCH the fleet — the
"I'm the CEO, show me everything" view the dashboard never gave end users (that one is operator-only,
zero tenant scoping). Everything here is scoped to the caller's own products (via tenant_products), and
shows exactly what a factory owner asks for:

  • Workers      — which agents are live on their products (role, status, current task)   [directory]
  • Comms        — recent agent-to-agent messages on their work (who→who, intent)          [conversations]
  • Task progress— per-product pipeline stages (pass/fail/timing) + queue + final status    [traces+tasks+audit]
  • Budget/spend — real $ + tokens per product, and plan/quota                              [traces+billing]
  • Controls     — pause/resume a product at runtime (kill-switch the factory honors)       [killswitch]

    cockpit.py serve [port]          # default 8094 ; web UI + JSON at /api/cockpit
    cockpit.py json <tenant_id>      # the same payload on the CLI
    cockpit.py selftest
Run with the agent-os venv python. Binds 127.0.0.1 (front with Tailscale serve, like the other surfaces).
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import billing      # noqa: E402
import killswitch   # noqa: E402
import tenancy      # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)
PIPELINE = ["SPEC", "BUILD", "QA", "REVIEW", "LAUNCH"]   # the governed line, for progress rendering


def _tenant(token):
    if not token:
        return None
    try:
        t = tenancy.tenant_for_token(token)
        return t["tenant_id"] if isinstance(t, dict) else t
    except Exception:
        return None


def _products(cur, tid):
    cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s ORDER BY created_at DESC", (tid,))
    return [r[0] for r in cur.fetchall()]


def _product_view(cur, product):
    # pipeline stages seen + their last rc/timing/spend (task progress + per-product spend in one pass)
    cur.execute("""SELECT stage, max(ts) last, bool_or(rc=0) any_ok, bool_and(rc=0) all_ok,
                          sum(COALESCE(cost_usd,0)) cost, sum(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)) toks,
                          sum(COALESCE(elapsed_s,0)) secs
                   FROM traces WHERE product=%s AND kind='agent' GROUP BY stage""", (product,))
    stage_rows = {r[0]: r for r in cur.fetchall()}
    stages = []
    for s in PIPELINE:
        hit = next((v for k, v in stage_rows.items() if k and k.upper().startswith(s)), None)
        stages.append({"stage": s, "done": hit is not None,
                       "ok": bool(hit[3]) if hit else None})
    cost = round(sum(float(r[4]) for r in stage_rows.values()), 4)
    toks = int(sum(int(r[5]) for r in stage_rows.values()))
    secs = int(sum(int(r[6]) for r in stage_rows.values()))
    # terminal status
    cur.execute("""SELECT decision FROM audit_log WHERE resource=%s AND action='ProductComplete'
                   ORDER BY id DESC LIMIT 1""", (product,))
    r = cur.fetchone()
    result = r[0] if r else ("building" if stage_rows else "queued")
    # live workers on this product
    cur.execute("""SELECT agent_id, role, status, task FROM directory
                   WHERE product=%s AND updated_at > now() - interval '15 minutes'""", (product,))
    workers = [{"agent": a, "role": ro, "status": st, "task": (tk or "")[:80]} for a, ro, st, tk in cur.fetchall()]
    return {"product": product, "result": result, "ready": result == "LAUNCHED",
            "failed": result not in ("LAUNCHED", "building", "queued"),
            "stages": stages, "cost_usd": cost, "tokens": toks, "elapsed_s": secs,
            "workers": workers, "halted": killswitch.is_halted(product).get("halted", False)}


def cockpit(tid):
    """The whole tenant-scoped payload: per-product progress/spend/workers + comms + budget + queue."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        prods = _products(cur, tid)
        products = [_product_view(cur, p) for p in prods]
        # communications among THIS tenant's agents (agents currently/recently on their products)
        comms = []
        agents = []
        if prods:
            cur.execute("""SELECT DISTINCT agent_id FROM directory WHERE product = ANY(%s)""", (prods,))
            agents = [r[0] for r in cur.fetchall()]
            if agents:
                cur.execute("""SELECT sender, recipient, intent, ts FROM conversations
                               WHERE sender = ANY(%s) OR recipient = ANY(%s)
                               ORDER BY turn DESC LIMIT 15""", (agents, agents))
                comms = [{"from": s, "to": r, "intent": i, "ts": t.strftime("%H:%M:%S")}
                         for s, r, i, t in cur.fetchall()]
        # queue depth across THIS tenant's products' work (pending/active/dead) — scoped to the
        # tenant's agents (tasks have no product column; assignee identifies the tenant's agents,
        # the same scoping used for the per-product worker counts below). A global GROUP BY would
        # leak other tenants' queue depth and report wrong numbers.
        qd = {}
        if agents:
            cur.execute("""SELECT status, count(*) FROM tasks WHERE assignee = ANY(%s) GROUP BY status""",
                        (agents,))
            qd = {s: n for s, n in cur.fetchall()}
    spend = round(sum(p["cost_usd"] for p in products), 4)
    tokens = sum(p["tokens"] for p in products)
    q = billing.quota(tid)
    return {
        "tenant": tid,
        "summary": {"products": len(products), "launched": sum(1 for p in products if p["ready"]),
                    "building": sum(1 for p in products if p["result"] == "building"),
                    "failed": sum(1 for p in products if p["failed"]),
                    "live_workers": sum(len(p["workers"]) for p in products),
                    "spend_usd": spend, "tokens": tokens},
        "budget": {"plan": q["plan"], "builds": q["builds"], "tokens_quota": q["tokens"],
                   "within_quota": q["within_quota"], "spend_usd": spend},
        "products": products,
        "communications": comms,
        "queue": {"pending": qd.get("pending", 0), "active": qd.get("active", 0), "dead": qd.get("dead", 0)},
    }


def control(tid, product, action):
    """Tenant control: pause/resume one of THEIR products at runtime. Ownership-checked."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT 1 FROM tenant_products WHERE tenant_id=%s AND product=%s", (tid, product))
        if not cur.fetchone():
            return {"error": "not your product"}
    if action == "pause":
        killswitch.halt(product, f"paused by tenant {tid}", set_by=tid)
        return {"product": product, "halted": True}
    if action == "resume":
        killswitch.resume(product)
        return {"product": product, "halted": False}
    return {"error": "unknown action"}


# ── tenant-scoped org-health, plain-language verdict, and comms graph ────────────────────────────
# These PORT the operator-only dashboard.py logic (comms graph nodes+edges, "what's blocked" panel,
# deadlock cycles, conflicts) but scope EVERYTHING to the caller's own agents — the agents on their
# products, derived exactly the way cockpit() does (tenant_products -> directory -> agent_ids).

def _tenant_agents(cur, tid):
    """The agent_ids working on THIS tenant's products (same derivation cockpit() uses for comms)."""
    cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s", (tid,))
    prods = [r[0] for r in cur.fetchall()]
    if not prods:
        return [], []
    cur.execute("SELECT DISTINCT agent_id FROM directory WHERE product = ANY(%s)", (prods,))
    return prods, [r[0] for r in cur.fetchall()]


def health(tid):
    """Tenant org-health: blocked waits, deadlock cycles, conflicts, dead-letter + stuck tasks — all
    scoped to the tenant's own agents. Every table read is guarded; never crashes (returns [] on error)."""
    out = {"blocked": [], "deadlocks": [], "conflicts": [], "dead_letter": 0, "stuck": 0, "ok": True}
    with psycopg.connect(DB) as c, c.cursor() as cur:
        try:
            prods, agents = _tenant_agents(cur, tid)
        except Exception:
            prods, agents = [], []
        aset = set(agents)

        # blocked: long/overdue waits whose waiter is one of THEIR agents (port of dashboard's waits panel)
        try:
            if agents:
                cur.execute("""SELECT waiter, awaited, round(EXTRACT(EPOCH FROM now()-since)/60), reply_by
                               FROM waits WHERE waiter = ANY(%s)""", (agents,))
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                for waiter, awaited, mins, rb in cur.fetchall():
                    mins = int(mins or 0)
                    overdue = bool(rb and rb < now)
                    if overdue or mins >= 10:          # long (>=10 min) or past its SLA
                        out["blocked"].append({"agent": waiter, "waiting_on": awaited, "minutes": mins})
        except Exception:
            out["blocked"] = []

        # deadlocks: detect() cycles that intersect their agents (the whole-fleet graph, filtered)
        try:
            import contextlib
            import io
            import deadlock
            with contextlib.redirect_stdout(io.StringIO()):    # detect() prints; keep this quiet
                cycles = deadlock.detect() or []
            out["deadlocks"] = [d for d in cycles if aset & set(d.get("cycle", []))]
        except Exception:
            out["deadlocks"] = []

        # conflicts: overlapping resource claims, filtered to their products
        try:
            import directory
            out["conflicts"] = [cf for cf in directory.conflicts() if cf.get("product") in set(prods)]
        except Exception:
            out["conflicts"] = []

        # dead-letter + stuck tasks, scoped by assignee (the agent the task belongs to)
        try:
            if agents:
                cur.execute("SELECT count(*) FROM tasks WHERE status='dead' AND assignee = ANY(%s)", (agents,))
                out["dead_letter"] = int(cur.fetchone()[0] or 0)
                cur.execute("""SELECT count(*) FROM tasks WHERE status='active' AND assignee = ANY(%s)
                               AND locked_at IS NOT NULL AND locked_at < now() - interval '30 minutes'""", (agents,))
                out["stuck"] = int(cur.fetchone()[0] or 0)
        except Exception:
            out["dead_letter"], out["stuck"] = 0, 0

    out["ok"] = not (out["blocked"] or out["deadlocks"] or out["conflicts"] or out["dead_letter"] or out["stuck"])
    return out


def company_summary(tid):
    """ONE plain-language verdict over the whole tenant, composed from signals already computed
    (cockpit summary + forecast level + qualityview + health). healthy | attention | critical."""
    try:
        view = cockpit(tid)
        s = view["summary"]
    except Exception:
        s = {"products": 0, "launched": 0, "building": 0, "failed": 0}
    live = int(s.get("launched", 0)); building = int(s.get("building", 0)); failed = int(s.get("failed", 0))

    try:
        import forecast
        f_level = forecast.forecast(tid).get("level", "ok")
    except Exception:
        f_level = "ok"

    try:
        import qualityview
        quality = qualityview.summary(tid) or []
    except Exception:
        quality = []
    quality_failed = sum(1 for q in quality if q.get("overall") == "failed")

    h = health(tid)
    has_deadlock = bool(h["deadlocks"])
    over_budget = (f_level == "over")
    failed_unaddressed = (failed > 0 or quality_failed > 0)

    if has_deadlock or over_budget or failed_unaddressed:
        verdict = "critical"
    elif building or h["blocked"] or h["conflicts"] or h["dead_letter"] or h["stuck"] or f_level == "warn":
        verdict = "attention"
    else:
        verdict = "healthy"

    bud = {"ok": "on budget", "warn": "approaching budget", "over": "over budget"}[f_level]
    pct = ""
    try:
        import forecast
        pct = f" ({forecast.forecast(tid).get('pct_of_quota_projected', 0)}% projected)"
    except Exception:
        pct = ""
    parts = [f"{live} product{'s' if live != 1 else ''} live"]
    if building:
        parts.append(f"{building} building")
    if failed_unaddressed:
        parts.append(f"{max(failed, quality_failed)} failed")
    blockers = []
    if has_deadlock:
        blockers.append("DEADLOCK")
    if h["blocked"]:
        blockers.append(f"{len(h['blocked'])} blocked")
    if h["conflicts"]:
        blockers.append(f"{len(h['conflicts'])} conflict{'s' if len(h['conflicts']) != 1 else ''}")
    if h["dead_letter"]:
        blockers.append(f"{h['dead_letter']} dead-letter")
    if h["stuck"]:
        blockers.append(f"{h['stuck']} stuck")
    tail = ", ".join(blockers) if blockers else "nothing blocked"
    word = {"healthy": "Healthy", "attention": "Attention", "critical": "Critical"}[verdict]
    line = f"{word} — {', '.join(parts)}, {bud}{pct}, {tail}."
    return {"verdict": verdict, "line": line}


def comms_graph(tid):
    """Tenant-scoped agent communication graph: nodes + edges from the last 6h of conversations among
    THIS tenant's agents (same aggregation dashboard.py uses, but scoped). Nodes capped ~20."""
    nodes, edges = {}, []
    with psycopg.connect(DB) as c, c.cursor() as cur:
        try:
            _, agents = _tenant_agents(cur, tid)
        except Exception:
            agents = []
        if agents:
            try:
                cur.execute("""SELECT sender, recipient, count(*), (array_agg(intent ORDER BY turn DESC))[1]
                               FROM conversations
                               WHERE ts > now() - interval '6 hours'
                                 AND (sender = ANY(%s) OR recipient = ANY(%s))
                               GROUP BY sender, recipient
                               ORDER BY count(*) DESC""", (agents, agents))
                for s, r, n, intent in cur.fetchall():
                    edges.append({"from": s, "to": r, "count": int(n), "intent": intent})
                    for k in (s, r):
                        if k not in nodes and len(nodes) < 20:
                            role = "human" if str(k).startswith("human") else \
                                   ("controller" if k == "controller" else "agent")
                            nodes[k] = {"id": k, "role": role}
                # drop edges whose endpoints we capped out (keep nodes/edges consistent)
                edges = [e for e in edges if e["from"] in nodes and e["to"] in nodes]
            except Exception:
                nodes, edges = {}, []
    return {"nodes": list(nodes.values()), "edges": edges}


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>agent-os · cockpit</title><style>
:root{--bg:#0a0d13;--panel:#111722;--line:#1e2733;--tx:#d7dee8;--mut:#7d8795;--accent:#4f8cff;--g:#3fb950;--r:#f85149;--y:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:22px 16px}h1{font-size:22px;margin:0 0 2px}.sub{color:var(--mut);margin:0 0 18px}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:104px}
.kpi b{display:block;font-size:20px}.kpi span{color:var(--mut);font-size:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.7px;color:var(--mut);margin:0 0 12px}
.prod{border-top:1px solid var(--line);padding:12px 0}.prod:first-child{border-top:none}
.prow{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pill{font-size:11px;padding:2px 8px;border-radius:999px;background:#16202c;color:var(--mut)}
.pill.ok{background:rgba(63,185,80,.15);color:var(--g)}.pill.bad{background:rgba(248,81,73,.15);color:var(--r)}
.stages{display:flex;gap:4px;margin:8px 0}
.st{flex:1;height:6px;border-radius:3px;background:#1c2733}.st.ok{background:var(--g)}.st.run{background:var(--y)}.st.bad{background:var(--r)}
.meta{color:var(--mut);font-size:12px}.w{font-size:12px;color:var(--tx)}.muted{color:var(--mut)}
button{background:#16202c;border:1px solid var(--line);color:var(--tx);border-radius:7px;padding:5px 10px;cursor:pointer;font-size:12px}
button:hover{filter:brightness(1.2)}table{width:100%;border-collapse:collapse;font-size:12px}td{padding:4px 6px;border-top:1px solid var(--line);color:var(--mut)}
input{background:#0d131c;border:1px solid var(--line);color:var(--tx);border-radius:7px;padding:7px;font-size:13px;width:340px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:760px){.grid{grid-template-columns:1fr}}
</style></head><body><div class=wrap>
<h1>⬡ Cockpit</h1><p class=sub>Your whole factory in one view — workers, comms, progress, budget, controls.</p>
<div class=card><label class=meta>Tenant token</label><br><input id=tok placeholder="paste your tenant token (from the front door)"><button onclick=load()>Open</button></div>
<div id=app></div></div>
<script>
const $=s=>document.querySelector(s);let TOK=localStorage.getItem('aos_tenant')||'';
function H(){return {'X-Tenant-Token':TOK}}
async function ctl(prod,act){await fetch('/api/control?product='+encodeURIComponent(prod)+'&action='+act,{method:'POST',headers:H()});load()}
function stClass(s){return s.done?(s.ok===false?'bad':'ok'):''}
async function load(){
 TOK=$('#tok').value||TOK;if(!TOK)return;localStorage.setItem('aos_tenant',TOK);
 let d;try{d=await (await fetch('/api/cockpit',{headers:H()})).json()}catch(e){$('#app').innerHTML='<div class=card>load failed</div>';return}
 if(d.error){$('#app').innerHTML='<div class=card>'+d.error+'</div>';return}
 const s=d.summary,b=d.budget;
 const kpis=[['Products',s.products],['Launched',s.launched],['Building',s.building],['Failed',s.failed],['Live workers',s.live_workers],['Spend $',s.spend_usd],['Plan',b.plan]];
 let h='<div class=kpis>'+kpis.map(k=>`<div class=kpi><b>${k[1]}</b><span>${k[0]}</span></div>`).join('')+'</div>';
 h+=`<div class=card><h2>Budget</h2><div class=prow><span>Plan <b>${b.plan}</b></span><span class=meta>builds ${b.builds}</span><span class=meta>tokens ${b.tokens_quota}</span><span class=meta>spend $${b.spend_usd}</span><span class="pill ${b.within_quota?'ok':'bad'}">${b.within_quota?'within quota':'over quota'}</span></div></div>`;
 h+='<div class=card><h2>Projects · task progress</h2>'+(d.products.length?d.products.map(p=>{
   const stages=p.stages.map(st=>`<div class="st ${stClass(st)}" title="${st.stage}"></div>`).join('');
   const pill=p.ready?'ok':(p.failed?'bad':'');
   const ctlBtn=p.halted?`<button onclick="ctl('${p.product}','resume')">resume</button>`:`<button onclick="ctl('${p.product}','pause')">pause</button>`;
   const ws=p.workers.length?p.workers.map(w=>`<span class=w title="${w.task}">${w.role}·${w.status}</span>`).join(' &middot; '):'<span class=muted>no live workers</span>';
   return `<div class=prod><div class=prow><b>${p.product}</b><span class="pill ${pill}">${p.result}</span>${p.halted?'<span class="pill bad">paused</span>':''}<span style=flex:1></span>${ctlBtn}</div>
     <div class=stages>${stages}</div>
     <div class=meta>$${p.cost_usd} &middot; ${p.tokens} tok &middot; ${p.elapsed_s}s &middot; workers: ${ws}</div></div>`;
 }).join(''):'<div class=muted>no projects yet — start one in the front door</div>')+'</div>';
 h+='<div class=grid>';
 h+='<div class=card><h2>Communications</h2><table>'+(d.communications.length?d.communications.map(m=>`<tr><td>${m.ts}</td><td>${m.from}</td><td>&rarr; ${m.to}</td><td>${m.intent}</td></tr>`).join(''):'<tr><td class=muted>no recent agent messages</td></tr>')+'</table></div>';
 h+=`<div class=card><h2>Work queue</h2><div class=prow><div class=kpi><b>${d.queue.pending}</b><span>pending</span></div><div class=kpi><b>${d.queue.active}</b><span>active</span></div><div class=kpi><b style="color:${d.queue.dead?'var(--r)':'inherit'}">${d.queue.dead}</b><span>dead-letter</span></div></div></div>`;
 h+='</div>';
 $('#app').innerHTML=h;
}
if(TOK){$('#tok').value=TOK;load();setInterval(load,5000)}
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            b = PAGE.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif p == "/health":
            self._json(200, {"service": "agent-os-cockpit", "ok": True})
        elif p == "/api/cockpit":
            tid = _tenant(self.headers.get("X-Tenant-Token"))
            if not tid:
                return self._json(401, {"error": "sign up first"})
            self._json(200, cockpit(tid))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/control":
            tid = _tenant(self.headers.get("X-Tenant-Token"))
            if not tid:
                return self._json(401, {"error": "sign up first"})
            qs = parse_qs(urlparse(self.path).query)
            self._json(200, control(tid, qs.get("product", [""])[0], qs.get("action", [""])[0]))
        else:
            self._json(404, {"error": "not found"})


def _selftest():
    """Offline: build a fake tenant + product + traces, prove the cockpit aggregates the 5 views."""
    import os
    reg = billing.signup("cockpit-selftest", "free")     # a REAL tenant (tenant_products has an FK to tenants)
    tid = reg["tenant_id"]
    prod = tid.replace("t-", "")[:6] + "-demo"
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (prod, tid))
        # two pipeline stages with real cost so spend + progress are non-trivial
        for st, cost in (("SPEC", 0.16), ("BUILD", 1.2)):
            cur.execute("""INSERT INTO traces (run_id, product, stage, role, kind, rc, cost_usd, tokens_in, tokens_out, elapsed_s, prompt, output, model)
                           VALUES (%s,%s,%s,'builder','agent',0,%s,1000,2000,30,'p','o','m')""",
                        (f"run-{prod}", prod, st, cost))
        cur.execute("""INSERT INTO directory (agent_id, role, status, product, task, updated_at)
                       VALUES (%s,'builder','active',%s,'building it', now())
                       ON CONFLICT (agent_id) DO UPDATE SET updated_at=now()""", (f"builder@{prod}", prod))
        c.commit()
    try:
        v = cockpit(tid)
        ctl = control(tid, prod, "pause")
        halted = killswitch.is_halted(prod).get("halted")
        control(tid, prod, "resume")
        ok = (v["summary"]["products"] == 1 and v["summary"]["spend_usd"] >= 1.3
              and v["summary"]["live_workers"] == 1
              and len(v["products"][0]["stages"]) == len(PIPELINE)
              and v["budget"]["plan"] and ctl.get("halted") is True and halted)
        print(f"products={v['summary']['products']} spend=${v['summary']['spend_usd']} "
              f"workers={v['summary']['live_workers']} stages={len(v['products'][0]['stages'])} "
              f"pause-control={ctl.get('halted')}")
        print("PASS: cockpit aggregates workers+comms+progress+budget+controls ✅" if ok else "FAIL")
        ok = ok and _selftest_health()
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM traces WHERE product=%s", (prod,))
            cur.execute("DELETE FROM directory WHERE product=%s", (prod,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
        killswitch.resume(prod)
    sys.exit(0 if ok else 1)


def _selftest_health():
    """Tenant-scoped health + company_summary + comms_graph over a real tenant with a directory agent
    and a couple of conversation rows. Returns True on pass; cleans up everything in finally."""
    tid = billing.signup("cockpit-health-selftest", "free")["tenant_id"]
    prod = tid.replace("t-", "")[:6] + "-hp"
    agent = f"builder@{prod}"; peer = f"reviewer@{prod}"
    ok = False
    try:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (prod, tid))
            cur.execute("""INSERT INTO directory (agent_id, role, status, product, task, updated_at)
                           VALUES (%s,'builder','active',%s,'building it', now())
                           ON CONFLICT (agent_id) DO UPDATE SET updated_at=now(), product=EXCLUDED.product""", (agent, prod))
            for i, (s, r, intent) in enumerate(((agent, peer, "ask"), (peer, agent, "answer"))):
                cur.execute("""INSERT INTO conversations (conversation_id, message_id, intent, sender, recipient, content, turn)
                               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                            (f"cv-{prod}", f"m-{prod}-{i}", intent, s, r, json.dumps({"text": "hi"}), i))
            c.commit()

        h = health(tid)
        cs = company_summary(tid)
        g = comms_graph(tid)
        ok = (set(h) >= {"blocked", "deadlocks", "conflicts", "dead_letter", "stuck", "ok"}
              and isinstance(h["blocked"], list) and isinstance(h["deadlocks"], list)
              and isinstance(h["conflicts"], list) and isinstance(h["ok"], bool)
              and cs.get("verdict") in ("healthy", "attention", "critical") and bool(cs.get("line"))
              and isinstance(g.get("nodes"), list) and isinstance(g.get("edges"), list)
              and any(n["id"] == agent for n in g["nodes"]) and len(g["edges"]) >= 1)
        print(f"health.ok={h['ok']} keys={len(h)} verdict={cs.get('verdict')!r} "
              f"graph_nodes={len(g['nodes'])} graph_edges={len(g['edges'])}")
        print("PASS: cockpit org-health + company verdict + comms-graph (tenant-scoped) ✅" if ok else "FAIL")
    finally:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM conversations WHERE conversation_id=%s", (f"cv-{prod}",))
            cur.execute("DELETE FROM directory WHERE product=%s", (prod,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    return ok


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 1:
        print(json.dumps(cockpit(a[1]), indent=2))
    elif a[0] == "serve":
        port = int(a[1]) if len(a) > 1 else 8094
        print(f"cockpit on http://127.0.0.1:{port}")
        ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
    else:
        sys.exit("usage: cockpit.py serve [port] | json <tenant_id> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
