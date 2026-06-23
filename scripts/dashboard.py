#!/usr/bin/env python3
"""dashboard.py — the agent-OS mission-control dashboard (human UI).

A single polished web view of the whole fleet, served on 127.0.0.1 (reach it privately over Tailscale).
Everything is REAL data: the tamper-evident audit stream, the durable conversation log + wait-for graph
(ADR 0005), live process state, component health, disk/backup pressure, and deadlock detection. It also
lets you message any agent two-way. Read endpoints are open on localhost; POST (send message) needs the
AOS_API_TOKEN bearer.

    dashboard.py serve [port]      # default 8092
    dashboard.py state             # print the JSON state once (debug)
Run with the agent-os venv python.
"""
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import deadlock  # noqa: E402
import monitor   # noqa: E402
import notify    # noqa: E402

ROOT = Path.home() / "projects" / "agent-os"
ENV = ROOT / ".env.local"
_cfg = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
        for l in ENV.read_text().splitlines() if l.strip() and not l.startswith("#") and "=" in l}
DB = _cfg.get("DATABASE_URL")
TOKEN = _cfg.get("AOS_API_TOKEN", "")

PROCS = [("controller", "controller.py"), ("factory", "factory.py build"),
         ("ticker", "ticker.sh"), ("watchdog", "watchdog.sh"), ("api", "api.py serve"),
         ("listener", "reply_listener.py"), ("dashboard", "dashboard.py serve")]

_hcache = {"ts": 0, "data": {}}


def _running(pat):
    r = subprocess.run(["pgrep", "-fc", pat], capture_output=True, text=True)
    try:
        return int(r.stdout.strip() or "0")
    except ValueError:
        return 0


def _health():
    if time.time() - _hcache["ts"] < 12:
        return _hcache["data"]
    try:
        h = monitor.check()
    except Exception:
        h = {"postgres": False, "ntfy": False, "nats": False, "cerbos": False}
    _hcache.update(ts=time.time(), data=h)
    return h


def _backup_age_h():
    snaps = sorted((ROOT / "backups").glob("agent-os-*.aosnap"))
    if not snaps:
        return None
    newest = max(s.stat().st_mtime for s in snaps)
    return round((time.time() - newest) / 3600, 1)


def state():
    du = shutil.disk_usage("/")
    disk_pct = round(du.used / du.total * 100)
    health = _health()
    backup_h = _backup_age_h()
    out = {"ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
           "health": health,
           "disk_pct": disk_pct,
           "backup_age_h": backup_h,
           "processes": [{"label": l, "n": _running(p)} for l, p in PROCS]}

    with psycopg.connect(DB) as c, c.cursor() as cur:
        # products in flight (from factory agent activity)
        cur.execute("""SELECT resource, count(*) steps, max(ts) last,
                          (array_agg(actor ORDER BY id DESC))[1] last_actor,
                          (array_agg(decision ORDER BY id DESC))[1] last_dec
                       FROM audit_log WHERE actor LIKE 'factory:%%' AND ts > now() - interval '6 hours'
                       GROUP BY resource ORDER BY last DESC LIMIT 10""")
        out["products"] = [{"name": r, "steps": s, "last": l.strftime("%H:%M:%S"),
                            "actor": (a or "").replace("factory:", ""), "decision": d}
                           for r, s, l, a, d in cur.fetchall()]
        # recent agent activity
        cur.execute("SELECT ts, actor, action, resource, decision FROM audit_log ORDER BY id DESC LIMIT 25")
        out["activity"] = [{"ts": t.strftime("%H:%M:%S"), "actor": ac, "action": an,
                            "resource": (rs or "")[:40], "decision": de}
                           for t, ac, an, rs, de in cur.fetchall()]
        # messages (durable conversation log) + per-message latency vs previous turn
        cur.execute("""SELECT sender, recipient, intent, ts,
                          EXTRACT(EPOCH FROM ts - lag(ts) OVER (PARTITION BY conversation_id ORDER BY turn)) lat
                       FROM conversations ORDER BY turn DESC LIMIT 20""")
        out["messages"] = [{"from": s, "to": r, "intent": i, "ts": t.strftime("%H:%M:%S"),
                            "latency_s": round(float(lat), 2) if lat is not None else None}
                           for s, r, i, t, lat in cur.fetchall()]
        # comms graph: nodes + edges from conversations, augmented by factory lifecycle edges
        cur.execute("""SELECT sender, recipient, count(*), (array_agg(intent ORDER BY turn DESC))[1]
                       FROM conversations WHERE ts > now() - interval '6 hours'
                       GROUP BY sender, recipient""")
        edges = [{"from": s, "to": r, "count": n, "intent": i, "kind": "msg"} for s, r, n, i in cur.fetchall()]
        cur.execute("""SELECT DISTINCT replace(actor,'factory:','') FROM audit_log
                       WHERE actor LIKE 'factory:%%' AND ts > now() - interval '6 hours'""")
        roles = [r[0] for r in cur.fetchall() if r[0] != "controller"]
        for role in roles:
            edges.append({"from": "controller", "to": role, "count": 1, "intent": "delegate", "kind": "msg"})
        # waits (what is blocked on what) + SLA breach
        cur.execute("SELECT waiter, awaited, since, reply_by FROM waits")
        waits = []
        for w, a, since, rb in cur.fetchall():
            waits.append({"from": w, "to": a, "kind": "wait",
                          "overdue": bool(rb and rb < datetime.now(timezone.utc))})
            edges.append({"from": w, "to": a, "count": 1, "intent": "awaiting", "kind": "wait"})
        nodes = {}
        for e in edges:
            for k in (e["from"], e["to"]):
                nodes.setdefault(k, {"id": k, "type": "controller" if k == "controller"
                                     else ("human" if k.startswith("human") else "agent")})
        out["graph"] = {"nodes": list(nodes.values()), "edges": edges}
        out["waits"] = waits
        # throughput + denials
        cur.execute("SELECT count(*) FROM audit_log WHERE ts > now() - interval '10 minutes'")
        a10 = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM audit_log WHERE decision='deny' AND ts > now() - interval '1 hour'")
        denies = cur.fetchone()[0]
        out["throughput"] = {"actions_10m": a10, "denies_1h": denies}
        # liveness heartbeats (loops that beat each cycle) -> show age on the matching process
        try:
            cur.execute("SELECT component, EXTRACT(EPOCH FROM now()-ts)::int FROM heartbeats")
            hb = dict(cur.fetchall())
            for p in out["processes"]:
                if p["label"] in hb:
                    p["beat"] = int(hb[p["label"]])
        except Exception:
            pass

    # live agent directory (presence + conflicts)
    confs = []
    try:
        import directory
        out["directory"] = directory.roster(active_only=True)[:12]
        confs = directory.conflicts()
        out["conflicts"] = confs
    except Exception:
        out["directory"], out["conflicts"] = [], []

    # recent runs (for the debug drill-down)
    try:
        import trace as tracemod
        out["runs"] = [{"product": r["product"], "steps": r["steps"], "total_s": r["total_s"],
                        "errors": r["errors"]} for r in tracemod.runs(8)]
    except Exception:
        out["runs"] = []

    # derived alerts
    cycles = []
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):   # detect() prints; keep state() output clean
            cycles = deadlock.detect() or []
    except Exception:
        pass
    alerts = []
    for k, v in health.items():
        if not v:
            alerts.append({"level": "crit", "msg": f"{k} is DOWN"})
    if disk_pct >= 90:
        alerts.append({"level": "crit", "msg": f"disk {disk_pct}% — critical"})
    elif disk_pct >= 80:
        alerts.append({"level": "warn", "msg": f"disk {disk_pct}%"})
    if backup_h is None:
        alerts.append({"level": "warn", "msg": "no snapshot yet"})
    elif backup_h > 36:
        alerts.append({"level": "warn", "msg": f"last backup {backup_h}h ago"})
    if cycles:
        alerts.append({"level": "crit", "msg": f"DEADLOCK: {len(cycles)} cycle(s)"})
    if any(w["overdue"] for w in waits):
        alerts.append({"level": "warn", "msg": "a wait is past its SLA"})
    if out["throughput"]["denies_1h"] > 80:
        alerts.append({"level": "warn", "msg": f"{out['throughput']['denies_1h']} policy denials/hr"})
    for cf in confs:
        alerts.append({"level": "warn", "msg": f"conflict: {cf['agents'][0]} & {cf['agents'][1]} on {cf['resource']}"})
    out["alerts"] = alerts or [{"level": "ok", "msg": "all systems nominal"}]
    out["overall"] = ("crit" if any(a["level"] == "crit" for a in out["alerts"])
                      else "warn" if any(a["level"] == "warn" for a in out["alerts"]) else "ok")
    return out


def send_message(recipient, intent, content):
    """Two-way: operator -> agent. Logs a durable conversation row + pings the role if it's a human ask."""
    mid = f"op-{int(time.time()*1000)}"
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO conversations (conversation_id, message_id, intent, sender, recipient, content)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (f"op-{recipient}", mid, intent or "instruct", "human:operator", recipient,
                     json.dumps({"text": content})))
        c.commit()
    notify.send(f"→ {recipient}: {content[:120]}", title="operator → agent", tags="speech_balloon")
    return {"ok": True, "message_id": mid}


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>agent-os · mission control</title>
<style>
:root{--bg:#0a0d13;--panel:#111722;--panel2:#0d131c;--line:#1e2733;--tx:#d7dee8;--mut:#7d8795;
--accent:#4f8cff;--g:#3fb950;--r:#f85149;--a:#d29922;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:12px 18px;background:linear-gradient(90deg,#0d131c,#111722);
border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
header h1{font-size:15px;margin:0;letter-spacing:.3px;font-weight:600}
.pill{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600}
.ok{background:rgba(63,185,80,.15);color:var(--g)}.warn{background:rgba(210,153,34,.15);color:var(--a)}
.crit{background:rgba(248,81,73,.15);color:var(--r)}
.mut{color:var(--mut)}.grow{flex:1}
.wrap{display:grid;grid-template-columns:repeat(12,1fr);gap:12px;padding:14px;max-width:1500px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow:hidden}
.card h2{font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:var(--mut);margin:0 0 10px}
.col3{grid-column:span 3}.col4{grid-column:span 4}.col5{grid-column:span 5}.col6{grid-column:span 6}
.col7{grid-column:span 7}.col8{grid-column:span 8}.col12{grid-column:span 12}
@media(max-width:900px){.wrap>*{grid-column:span 12!important}}
.kpi{display:flex;gap:18px;flex-wrap:wrap}.kpi div{display:flex;flex-direction:column}
.kpi b{font-size:22px;font-weight:700}.kpi span{font-size:11px;color:var(--mut)}
.row{display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--panel2);font-size:13px}
.dot{width:8px;height:8px;border-radius:50%;flex:none}.d-ok{background:var(--g)}.d-off{background:#39414d}
.d-crit{background:var(--r)}.d-warn{background:var(--a)}
.feed{font-family:var(--mono);font-size:12px;max-height:330px;overflow:auto}
.feed .row{border-bottom:1px solid #0c1119}.tag{color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:12.5px}th{text-align:left;color:var(--mut);font-weight:500;
font-size:11px;text-transform:uppercase;letter-spacing:.5px;padding:4px 6px;border-bottom:1px solid var(--line)}
td{padding:5px 6px;border-bottom:1px solid var(--panel2);font-family:var(--mono)}
.chip{display:inline-block;padding:1px 7px;border-radius:6px;font-size:11px;background:#16202c;color:var(--mut)}
.alert{display:flex;gap:8px;align-items:center;padding:6px 8px;border-radius:7px;margin-bottom:6px;font-size:13px}
.alert.crit{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3)}
.alert.warn{background:rgba(210,153,34,.1);border:1px solid rgba(210,153,34,.3)}
.alert.ok{background:rgba(63,185,80,.08);border:1px solid rgba(63,185,80,.25)}
svg{width:100%;height:360px;display:block}.edge{stroke:#3a5170;stroke-width:1.6}.edge.wait{stroke:var(--a);
stroke-dasharray:5 4}.edge.over{stroke:var(--r)}.node circle{stroke:#0a0d13;stroke-width:2.5}
.node text{fill:#aeb9c7;font-size:11px;font-family:var(--mono)}
input,select,button{background:var(--panel2);border:1px solid var(--line);color:var(--tx);
border-radius:7px;padding:7px 9px;font-size:13px}button{background:var(--accent);border:none;color:#fff;
font-weight:600;cursor:pointer}button:hover{filter:brightness(1.08)}
.send{display:flex;gap:7px;flex-wrap:wrap}.send input[type=text]{flex:1;min-width:120px}
.bar{height:6px;border-radius:4px;background:#1b2430;overflow:hidden;margin-top:5px}
.bar>i{display:block;height:100%}
.lat-hi{color:var(--a)}.lat-ok{color:var(--g)}
</style></head><body>
<header>
  <h1>⬡ agent-os <span class=mut>· mission control</span></h1>
  <span id=overall class="pill ok">…</span>
  <span class="mut" id=clock></span>
  <span class=grow></span>
  <span class="mut" id=auto>live · 3s</span>
</header>
<div class=wrap>
  <div class="card col3"><h2>Alerts</h2><div id=alerts></div></div>
  <div class="card col3"><h2>Health</h2><div id=health></div>
     <div style="margin-top:8px"><span class=mut>disk</span><div class=bar><i id=diskbar></i></div>
     <span class=mut id=disktxt></span></div>
     <div style="margin-top:6px" class=mut id=backup></div></div>
  <div class="card col3"><h2>Processes</h2><div id=procs></div></div>
  <div class="card col3"><h2>Throughput</h2><div class=kpi style="margin-top:4px">
     <div><b id=a10>–</b><span>actions / 10m</span></div>
     <div><b id=den>–</b><span>denials / hr</span></div></div>
     <h2 style="margin-top:14px">Products in flight</h2><div id=products class=feed style="max-height:150px"></div></div>

  <div class="card col7"><h2>Agent communication graph <span class=mut>· msgs ▸ &nbsp; waits ▱ amber/red</span></h2>
     <svg id=graph viewBox="0 0 700 340"></svg></div>
  <div class="card col5"><h2>Message queue <span class=mut>· sender → recipient · latency</span></h2>
     <table><thead><tr><th>time</th><th>from → to</th><th>intent</th><th>lat</th></tr></thead>
     <tbody id=msgs></tbody></table></div>

  <div class="card col7"><h2>Live agent activity <span class=mut>· tamper-evident audit stream</span></h2>
     <div id=activity class=feed></div></div>
  <div class="card col5"><h2>Message an agent <span class=mut>· two-way</span></h2>
     <div class=send>
       <select id=to></select>
       <select id=intent><option>instruct</option><option>ask</option><option>review</option>
         <option>approve</option><option>halt</option></select>
       <input id=msg type=text placeholder="message…">
       <button onclick=send()>Send</button>
     </div>
     <div class=mut style="margin-top:6px;font-size:12px" id=sendnote>needs AOS_API_TOKEN (saved locally once)</div>
     <h2 style="margin-top:14px">What's blocked</h2><div id=waits class=feed style="max-height:120px"></div></div>

  <div class="card col12"><h2>Agent directory <span class=mut>· live presence · who's working on what · direct-contact (no sockets, brokered mailboxes)</span></h2>
     <div id=directory class=feed style="max-height:180px"></div></div>

  <div class="card col12"><h2>Runs <span class=mut>· click a run to replay its full trace — every agent prompt/response + test output (debug)</span></h2>
     <div id=runs class=feed style="max-height:140px"></div>
     <div id=tracebox style="margin-top:8px;max-height:420px;overflow:auto"></div></div>
</div>
<script>
const $=s=>document.querySelector(s);
let TOKEN=localStorage.getItem('aos_token')||'';
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function color(d){return d==='deny'?'d-crit':d==='ask'?'d-warn':'d-ok'}
async function tick(){
 let s; try{s=await (await fetch('/api/state')).json()}catch(e){return}
 $('#clock').textContent=s.ts+' UTC';
 const ov=$('#overall');ov.className='pill '+s.overall;ov.textContent=s.overall==='ok'?'● all nominal':s.overall==='warn'?'▲ warnings':'■ critical';
 $('#alerts').innerHTML=s.alerts.map(a=>`<div class="alert ${a.level}"><span>${a.level==='crit'?'■':a.level==='warn'?'▲':'●'}</span><span>${esc(a.msg)}</span></div>`).join('');
 $('#health').innerHTML=Object.entries(s.health).map(([k,v])=>`<div class=row><span class="dot ${v?'d-ok':'d-off'}"></span>${k}<span class=grow></span><span class=mut>${v?'up':'down'}</span></div>`).join('');
 const dp=s.disk_pct;$('#diskbar').style.width=dp+'%';$('#diskbar').style.background=dp>=90?'var(--r)':dp>=80?'var(--a)':'var(--g)';$('#disktxt').textContent=dp+'% used';
 $('#backup').textContent=s.backup_age_h==null?'no snapshot yet':('last backup '+s.backup_age_h+'h ago');
 $('#procs').innerHTML=s.processes.map(p=>`<div class=row><span class="dot ${p.n?'d-ok':'d-off'}"></span>${p.label}<span class=grow></span><span class=mut>${p.beat!=null?('♥ '+p.beat+'s · '):''}${p.n?('×'+p.n):'idle'}</span></div>`).join('');
 $('#a10').textContent=s.throughput.actions_10m;$('#den').textContent=s.throughput.denies_1h;
 $('#products').innerHTML=s.products.length?s.products.map(p=>`<div class=row><b>${esc(p.name)}</b><span class=grow></span><span class=tag>${p.steps} steps · ${esc(p.actor)}</span></div>`).join(''):'<div class=mut>none active</div>';
 $('#msgs').innerHTML=s.messages.map(m=>`<tr><td>${m.ts}</td><td>${esc(m.from)} → ${esc(m.to)}</td><td><span class=chip>${esc(m.intent)}</span></td><td class="${m.latency_s>5?'lat-hi':'lat-ok'}">${m.latency_s==null?'–':m.latency_s+'s'}</td></tr>`).join('')||'<tr><td class=mut colspan=4>no messages yet</td></tr>';
 $('#activity').innerHTML=s.activity.map(a=>`<div class=row><span class="dot ${color(a.decision)}"></span><span class=tag>${a.ts}</span> ${esc(a.actor)} <span class=mut>${esc(a.action)}</span> ${esc(a.resource)}</div>`).join('');
 $('#waits').innerHTML=s.waits.length?s.waits.map(w=>`<div class=row><span class="dot ${w.overdue?'d-crit':'d-warn'}"></span>${esc(w.from)} → ${esc(w.to)}<span class=grow></span><span class=mut>${w.overdue?'OVERDUE':'waiting'}</span></div>`).join(''):'<div class=mut>nothing blocked</div>';
 // recipients dropdown
 const to=$('#to');const cur=to.value;const names=[...new Set(s.graph.nodes.map(n=>n.id).filter(n=>!n.startsWith('human')))].sort();
 to.innerHTML=names.map(n=>`<option>${esc(n)}</option>`).join('');if(cur)to.value=cur;
 $('#runs').innerHTML=(s.runs&&s.runs.length)?s.runs.map(r=>`<div class=row style="cursor:pointer" onclick="showTrace('${esc(r.product)}')"><span class="dot ${r.errors?'d-warn':'d-ok'}"></span><b>${esc(r.product)}</b><span class=grow></span><span class=mut>${r.steps} steps · ${r.total_s}s · errs ${r.errors} ▸</span></div>`).join(''):'<div class=mut>no traced runs yet (only new builds are traced)</div>';
 const conf=new Set((s.conflicts||[]).flatMap(c=>c.agents));
 $('#directory').innerHTML=(s.directory&&s.directory.length)?s.directory.map(d=>`<div class=row><span class="dot ${conf.has(d.agent_id)?'d-crit':'d-ok'}"></span><b>${esc(d.agent_id)}</b> <span class=tag>${esc(d.role)}</span><span class=grow></span><span class=mut>${esc(d.product||'-')} · ${esc(d.task||'-')} · ${esc((d.resources||[]).join(' '))}</span></div>`).join(''):'<div class=mut>no active agents right now</div>';
 drawGraph(s.graph,s.waits);
}
function drawGraph(g,waits){
 const W=700,H=360,cx=W/2,cy=H/2,Rx=W/2-96,Ry=H/2-50;
 const others=g.nodes.filter(n=>n.id!=='controller');const m=others.length||1;const pos={};
 if(g.nodes.some(n=>n.id==='controller'))pos['controller']={x:cx,y:cy};
 others.forEach((nd,i)=>{const ang=2*Math.PI*i/m-Math.PI/2;pos[nd.id]={x:cx+Rx*Math.cos(ang),y:cy+Ry*Math.sin(ang)};});
 let e='<defs><marker id=arr markerWidth=8 markerHeight=8 refX=7 refY=3 orient=auto>'+
   '<path d="M0,0 L7,3 L0,6 z" fill="#5f7da3"/></marker>'+
   '<marker id=arrw markerWidth=8 markerHeight=8 refX=7 refY=3 orient=auto>'+
   '<path d="M0,0 L7,3 L0,6 z" fill="#d29922"/></marker></defs>';
 g.edges.forEach(ed=>{const a=pos[ed.from],b=pos[ed.to];if(!a||!b)return;
   const dx=b.x-a.x,dy=b.y-a.y,L=Math.hypot(dx,dy)||1,ux=dx/L,uy=dy/L;
   const x1=a.x+ux*13,y1=a.y+uy*13,x2=b.x-ux*15,y2=b.y-uy*15;
   const wait=ed.kind==='wait';
   e+=`<line class="${wait?'edge wait':'edge'}" x1=${x1} y1=${y1} x2=${x2} y2=${y2} marker-end="url(#${wait?'arrw':'arr'})"/>`;});
 g.nodes.forEach(nd=>{const p=pos[nd.id];if(!p)return;
   const col=nd.type==='controller'?'#4f8cff':nd.type==='human'?'#d29922':'#3fb950';
   const r=nd.type==='controller'?15:10;const ctrl=nd.type==='controller';
   e+=`<g class=node><circle cx=${p.x} cy=${p.y} r=${r} fill="${col}" opacity=.92/>`+
      `<text x=${p.x} y=${p.y+(ctrl?4:23)} text-anchor=middle>${esc(nd.id)}</text></g>`;});
 $('#graph').innerHTML=e;
}
async function send(){
 const text=$('#msg').value.trim();if(!text)return;
 if(!TOKEN){TOKEN=prompt('Paste AOS_API_TOKEN (saved on this device):')||'';localStorage.setItem('aos_token',TOKEN);}
 const r=await fetch('/api/message',{method:'POST',headers:{'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'},
   body:JSON.stringify({recipient:$('#to').value,intent:$('#intent').value,content:text})});
 $('#sendnote').textContent=r.ok?'sent ✓':'failed — check token';if(r.ok)$('#msg').value='';tick();
}
async function showTrace(p){
 const box=$('#tracebox');box.innerHTML='<div class=mut>loading trace…</div>';
 let d; try{d=await (await fetch('/api/trace?product='+encodeURIComponent(p))).json()}catch(e){box.innerHTML='<div class=mut>failed</div>';return}
 box.innerHTML='<div style="font-family:var(--mono);font-size:12px"><b>'+esc(p)+' — '+d.steps.length+' steps</b>'+
  d.steps.map((x,i)=>{const bad=x.rc&&x.rc!=0;return `<div style="border-top:1px solid var(--line);padding:7px 0">`+
   `<b>[${i+1}] ${esc(x.stage||'')} · ${esc(x.role||'')} · ${esc(x.kind||'')}</b> `+
   `<span class="${bad?'crit':'ok'} pill" style="padding:1px 6px">${bad?('rc='+x.rc):'ok'}</span> `+
   `<span class=mut>${x.elapsed_s?(x.elapsed_s+'s'):''}</span>`+
   `<div class=mut style="white-space:pre-wrap;margin-top:4px">▸ prompt: ${esc((x.prompt||'').slice(0,400))}${(x.prompt||'').length>400?'…':''}</div>`+
   `<div style="white-space:pre-wrap;margin-top:4px;color:#9fb0c3">◂ output: ${esc((x.output||'').slice(-700))}</div></div>`}).join('')+'</div>';
}
tick();setInterval(tick,3000);
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
            self._json(200, {"service": "agent-os-dashboard", "ok": True})
        elif p == "/api/state":
            try:
                self._json(200, state())
            except Exception as e:
                self._json(500, {"error": str(e)})
        elif p == "/api/trace":
            from urllib.parse import parse_qs
            prod = (parse_qs(urlparse(self.path).query).get("product") or [""])[0]
            try:
                import trace as tracemod
                self._json(200, {"product": prod, "steps": [
                    {"stage": s["stage"], "role": s["role"], "kind": s["kind"], "rc": s["rc"],
                     "elapsed_s": s["elapsed_s"], "prompt": (s["prompt"] or "")[:4000],
                     "output": (s["output"] or "")[-4000:]} for s in tracemod.steps(prod)]})
            except Exception as e:
                self._json(500, {"error": str(e)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/api/message":
            return self._json(404, {"error": "not found"})
        if self.headers.get("Authorization", "") != f"Bearer {TOKEN}" or not TOKEN:
            return self._json(401, {"error": "unauthorized"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self._json(200, send_message(body.get("recipient", ""), body.get("intent", ""), body.get("content", "")))


def main(a):
    if a and a[0] == "state":
        print(json.dumps(state(), indent=2)); return
    port = int(a[1]) if len(a) > 1 and a[0] == "serve" else 8092
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"dashboard on http://127.0.0.1:{port}  (expose privately: tailscale serve --bg --https=8092 http://127.0.0.1:{port})")
    srv.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
