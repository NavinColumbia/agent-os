#!/usr/bin/env python3
"""frontdoor.py — the self-serve front door. The bridge from 'powerful factory on a laptop' to
'a product strangers use and pay for, with no pitching from you'.

A user: signs up (gets a tenant + token), pastes THEIR OWN LLM API key (BYO-token, stored encrypted in
the vault), describes a product, and the governed factory builds it — quota-enforced, tenant-owned,
traced, billed. They watch status and download the result. Runs on localhost + your tailnet now;
flip to a public host later (a small, separate step).

    frontdoor.py serve [port]      # default 8093
Run with the agent-os venv python.
"""
import io
import json
import re
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit     # noqa: E402
import billing   # noqa: E402
import consent   # noqa: E402
import factory   # noqa: E402
import notifications  # noqa: E402
import tenancy   # noqa: E402
import vault     # noqa: E402

from aoscfg import ENV, DB
PRODUCTS = factory.PRODUCTS


def _tenant(token):
    if not token:
        return None
    try:
        t = tenancy.tenant_for_token(token)
        return t["tenant_id"] if isinstance(t, dict) else t
    except Exception:
        return None


def _own(product, tid):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (product, tid))
        c.commit()


def _status(product):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT DISTINCT stage FROM traces WHERE product=%s", (product,))
        stages = [r[0] for r in cur.fetchall() if r[0]]
        cur.execute("""SELECT decision, payload FROM audit_log WHERE resource=%s AND action='ProductComplete'
                       ORDER BY id DESC LIMIT 1""", (product,))
        r = cur.fetchone()
    result = r[0] if r else ("building" if stages else "queued")
    failed = result not in ("LAUNCHED", "building", "queued")   # FAILED / BLOCKED_AT_* / crashed
    err = ""
    if failed and r and isinstance(r[1], dict):
        err = (r[1].get("blocker") or r[1].get("error") or "")[:240]
    # what the user can DO about it — resilience-to-failure UX, not a dead end
    cta = "download" if result == "LAUNCHED" else ("retry" if failed else "wait")
    return {"product": product, "stages": stages, "result": result,
            "ready": result == "LAUNCHED", "failed": failed, "error": err, "cta": cta}


def _run_build(tid, product, charter, kind):
    _own(product, tid)
    # MULTI-PROVIDER: route to whichever engine the tenant connected (Claude, or Codex if that's all they
    # have). Falls back to the legacy single byo_llm_key, then to the platform default.
    import tenantproviders
    bk = tenantproviders.build_kwargs(tid)           # {engine, provider_key, api_key}
    if bk["engine"] == "claude" and not bk["api_key"]:
        try:
            v = vault.get_secret("byo_llm_key", f"tenant:{tid}", "prod", "builder", tenant_id=tid)
            bk["api_key"] = v if isinstance(v, str) else (v.get("value") if isinstance(v, dict) else None)
        except Exception:
            pass
    try:
        factory.build_product(product, charter, kind, **bk)
    except Exception as e:
        # DON'T swallow: record a terminal FAILED status so the user sees a real failure (+ retry CTA),
        # not an eternal "building" spinner. _status reads this ProductComplete row.
        audit.append(actor="frontdoor", action="ProductComplete", resource=product, decision="FAILED",
                     payload={"error": str(e)[:300], "tenant": tid})
    # emit a tenant-facing notification reflecting the REAL outcome (build category)
    try:
        st = _status(product)
        if st["ready"]:
            notifications.send(tid, "build", f"Build ready: {product}",
                               "Your product passed QA and is ready to download.", level="standard",
                               url=f"/download/{product}")
        elif st["failed"]:
            notifications.send(tid, "build", f"Build failed: {product}",
                               st["error"] or "The build did not complete — you can retry.", level="standard")
    except Exception:
        pass


def _zip(product):
    repo = PRODUCTS / product
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in repo.rglob("*"):
            s = str(f)
            if f.is_file() and "__pycache__" not in s and "/.git" not in s and "node_modules" not in s:
                z.write(f, f.relative_to(repo))
    return buf.getvalue()


PAGE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>agent-os · build anything</title>
<style>
:root{--bg:#0a0d13;--panel:#111722;--line:#1e2733;--tx:#d7dee8;--mut:#7d8795;--accent:#4f8cff;--g:#3fb950;--mono:ui-monospace,Menlo,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:28px 18px}
h1{font-size:24px;margin:0 0 4px}.sub{color:var(--mut);margin:0 0 22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:14px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.7px;color:var(--mut);margin:0 0 12px}
label{display:block;font-size:13px;color:var(--mut);margin:8px 0 4px}
input,select,textarea,button{width:100%;background:#0d131c;border:1px solid var(--line);color:var(--tx);border-radius:8px;padding:10px;font-size:14px;font-family:inherit}
textarea{min-height:80px;resize:vertical}
button{background:var(--accent);border:none;color:#fff;font-weight:600;cursor:pointer;margin-top:12px;width:auto;padding:10px 18px}
button:hover{filter:brightness(1.08)}.row{display:flex;gap:10px}.row>*{flex:1}
.note{font-size:12px;color:var(--mut);margin-top:8px}.tok{font-family:var(--mono);font-size:12px;word-break:break-all;color:var(--g)}
.build{border-top:1px solid var(--line);padding:10px 0;font-size:14px;display:flex;align-items:center;gap:8px}
.pill{font-size:11px;padding:2px 8px;border-radius:999px;background:#16202c;color:var(--mut)}.pill.ok{background:rgba(63,185,80,.15);color:var(--g)}.pill.bad{background:rgba(248,81,73,.15);color:#f85149}
a{color:var(--accent)}
</style></head><body><div class=wrap>
<h1>⬡ agent-os</h1><p class=sub>Describe a product. A governed AI factory builds, tests, and ships it. Bring your own API key.</p>

<div class=card><h2>1 · Sign up</h2>
  <div class=row><input id=name placeholder="your name / company"><select id=plan><option value=free>Free (3 builds)</option><option value=pro>Pro ($49 · 50 builds)</option></select></div>
  <button onclick=signup()>Create account</button>
  <div id=acct class=note></div></div>

<div class=card><h2>2 · Your LLM API key (bring your own)</h2>
  <input id=key type=password placeholder="sk-… (Claude / OpenAI / DeepSeek). Stored encrypted, scoped to you.">
  <button onclick=savekey()>Save key</button>
  <div class=note>You pay your own inference — we never see or bill your tokens. <span id=keynote></span></div></div>

<div class=card id=consentcard style=display:none><h2>3 · AI processing consent</h2>
  <div id=disclosure class=note></div>
  <label><input type=checkbox id=consentbox style="width:auto;margin-right:8px">I consent to this processing</label>
  <button onclick=acceptConsent()>Accept &amp; continue</button>
  <div class=note>Required before any build. You can revoke it later (revoking disables AI builds).</div></div>

<div class=card><h2>4 · Build a product</h2>
  <label>Name</label><input id=pname placeholder="splitbill">
  <label>Type</label><select id=kind><option value=lib>Python library</option><option value=web>Web app</option><option value=service>API service</option></select>
  <label>What should it do?</label><textarea id=charter placeholder="Describe it concretely: the API, behaviours, edge cases…"></textarea>
  <button onclick=build()>Build it</button><div id=buildnote class=note></div></div>

<div class=card><h2>Your builds</h2><div id=builds><div class=note>none yet</div></div></div>
</div>
<script>
const $=s=>document.querySelector(s);let TOK=localStorage.getItem('aos_tenant')||'';
function H(){return {'Content-Type':'application/json','X-Tenant-Token':TOK}}
async function signup(){
 const r=await (await fetch('/api/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('#name').value||'anon',plan:$('#plan').value})})).json();
 TOK=r.api_token;localStorage.setItem('aos_tenant',TOK);
 $('#acct').innerHTML='Account ready · plan <b>'+r.plan+'</b><br>token: <span class=tok>'+r.api_token+'</span>';refresh();
}
async function savekey(){
 if(!TOK){$('#keynote').textContent='sign up first';return}
 const r=await fetch('/api/byok',{method:'POST',headers:H(),body:JSON.stringify({key:$('#key').value})});
 $('#keynote').textContent=r.ok?'saved ✓':'failed';
}
async function loadConsent(){
 if(!TOK)return true;
 let c;try{c=await (await fetch('/api/consent',{headers:H()})).json()}catch(e){return true}
 $('#disclosure').innerHTML='<b>'+c.provider+'</b> — '+c.disclosure;
 $('#consentcard').style.display=c.required?'block':'none';
 return !c.required;
}
async function acceptConsent(){
 if(!$('#consentbox').checked){return}
 await fetch('/api/consent',{method:'POST',headers:H()});
 await loadConsent();$('#buildnote').textContent='consent recorded — you can build now';
}
async function build(){
 if(!TOK){$('#buildnote').textContent='sign up first';return}
 $('#buildnote').textContent='submitting…';
 const r=await (await fetch('/api/build',{method:'POST',headers:H(),body:JSON.stringify({name:$('#pname').value,kind:$('#kind').value,charter:$('#charter').value})})).json();
 if(r.error==='consent_required'){$('#buildnote').textContent='please accept AI consent above first';await loadConsent();return}
 $('#buildnote').textContent=r.error?('✗ '+r.error):('building '+r.product+' — watch below');refresh();
}
async function refresh(){
 if(!TOK)return;loadConsent();let d;try{d=await (await fetch('/api/builds',{headers:H()})).json()}catch(e){return}
 $('#builds').innerHTML=(d.builds&&d.builds.length)?d.builds.map(b=>{
  const pill=b.ready?'ok':(b.failed?'bad':'');
  let right;
  if(b.ready)right=`<a href="/download/${encodeURIComponent(b.product)}">download .zip</a>`;
  else if(b.failed)right=`<span class=note title="${(b.error||'').replace(/"/g,'&quot;')}">${(b.error||'failed').slice(0,60)}</span> <a href="#" onclick="retry('${encodeURIComponent(b.product)}');return false">retry</a>`;
  else right=`<span class=note>${b.stages.length} stages</span>`;
  return `<div class=build><b>${b.product}</b><span class="pill ${pill}">${b.result}</span><span style=flex:1></span>${right}</div>`;
 }).join(''):'<div class=note>none yet</div>';
}
async function retry(prod){
 $('#pname').value=decodeURIComponent(prod).replace(/^[^-]*-/,'');
 $('#buildnote').textContent='re-submitting '+decodeURIComponent(prod)+'… set the description and click Build it';
}
loadConsent();refresh();setInterval(refresh,4000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            b = PAGE.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif p == "/health":
            self._json(200, {"service": "agent-os-frontdoor", "ok": True})
        elif p == "/api/consent":
            tid = _tenant(self.headers.get("X-Tenant-Token"))
            if not tid:
                return self._json(401, {"error": "sign up first"})
            self._json(200, consent.state(tid))
        elif p == "/api/notifications":
            tid = _tenant(self.headers.get("X-Tenant-Token"))
            if not tid:
                return self._json(401, {"error": "sign up first"})
            self._json(200, {"unread": notifications.unread_count(tid), "feed": notifications.feed(tid)})
        elif p == "/api/builds":
            tid = _tenant(self.headers.get("X-Tenant-Token"))
            if not tid:
                return self._json(401, {"error": "sign up first"})
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s ORDER BY created_at DESC", (tid,))
                prods = [r[0] for r in cur.fetchall()]
            self._json(200, {"builds": [_status(p) for p in prods]})
        elif p.startswith("/download/"):
            product = p[len("/download/"):]
            tid = _tenant(self.headers.get("X-Tenant-Token"))
            if not tid:                                          # AUTH: a token is mandatory
                return self._json(401, {"error": "sign up first"})
            # PATH-TRAVERSAL: only flat, slug-shaped names, and the resolved path must stay inside PRODUCTS
            if not re.fullmatch(r"[a-z0-9-]+", product):
                return self._json(404, {"error": "not found"})
            if not (PRODUCTS / product).resolve().is_relative_to(PRODUCTS.resolve()):
                return self._json(404, {"error": "not found"})
            # OWNERSHIP: a tenant may only download a product it owns
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("SELECT 1 FROM tenant_products WHERE tenant_id=%s AND product=%s", (tid, product))
                owned = cur.fetchone() is not None
            if not owned:
                return self._json(404, {"error": "not found"})
            if not (PRODUCTS / product).exists():
                return self._json(404, {"error": "not found"})
            data = _zip(product)
            self.send_response(200); self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{product}.zip"')
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/signup":
            b = self._body()
            try:
                self._json(200, billing.signup(b.get("name", "anon"), b.get("plan", "free")))
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        tid = _tenant(self.headers.get("X-Tenant-Token"))
        if not tid:
            return self._json(401, {"error": "sign up first"})
        if p == "/api/byok":
            key = self._body().get("key", "")
            if key:
                vault.put_secret("byo_llm_key", f"tenant:{tid}", "prod", ["builder", "factory"], key)
            self._json(200, {"ok": bool(key)})
        elif p == "/api/consent":
            consent.record(tid)
            self._json(200, {"ok": True, "accepted": True})
        elif p == "/api/build":
            b = self._body()
            if not consent.require_consent(tid):       # MANDATORY AI-consent gate (Apple/Play/EU AI Act)
                return self._json(403, {"error": "consent_required",
                                        "consent": consent.state(tid)})
            q = billing.quota(tid)
            if not q["within_quota"]:
                return self._json(402, {"error": f"quota reached ({q['builds']}) — upgrade your plan"})
            raw = (b.get("name") or "app").strip().lower().replace(" ", "-")[:24] or "app"
            product = f"{tid.replace('t-', '')[:6]}-{raw}"
            charter = b.get("charter") or "Build a small, well-tested product."
            threading.Thread(target=_run_build, args=(tid, product, charter, b.get("kind", "lib")), daemon=True).start()
            self._json(200, {"product": product, "status": "building"})
        else:
            self._json(404, {"error": "not found"})


def main(a):
    port = int(a[1]) if len(a) > 1 and a[0] == "serve" else 8093
    print(f"front door on http://127.0.0.1:{port}  (tailnet: tailscale serve --bg --https=8095 http://127.0.0.1:{port})")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
