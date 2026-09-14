#!/usr/bin/env python3
"""Public sales intake and buyer-facing page for the managed Release Assurance offer.

This is intentionally a service funnel, not another product-control surface. A buyer submits the minimum
information required to scope a three-journey pilot. No repository credential, password, regulated dataset,
or production mutation is accepted here. The raw contact record expires after 90 days unless it is converted.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from aoscfg import get as cfg_get
from dbpool import connection
from secret_filter import looks_like_secret

MIGRATION = Path(__file__).resolve().parents[1] / "postgres" / "initdb" / "85-assurance-leads.sql"
SAMPLE_ROOT = Path(__file__).resolve().parents[1] / "docs" / "qa" / "sample-release-assurance"
SAMPLE_ASSETS = {
    "public-trust-boundary.png": SAMPLE_ROOT / "assets" / "public-trust-boundary.png",
    "redacted-operating-state.png": SAMPLE_ROOT / "assets" / "redacted-operating-state.png",
}
CONSENT_VERSION = "assurance-intake-v1-2026-08-25"
ACCESS_MODES = {"public_url", "temporary_test_account", "guided_session", "discuss"}
_ensured = False
_ensure_lock = threading.Lock()


def _ensure():
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        sql = MIGRATION.read_text()
        with connection() as c, c.cursor() as cur:
            cur.execute(sql)
        _ensured = True


def _clean(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _email(value):
    value = str(value or "").strip().lower()[:254]
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]{2,}", value):
        return None
    return value


def _app_url(value):
    """Retain a testable URL while refusing query/fragment credentials."""
    raw = str(value or "").strip()[:1000]
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    if parsed.query or parsed.fragment:
        return None
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return None
    clean = urlunparse((parsed.scheme, f"{parsed.hostname}{port}", parsed.path or "/", "", "", ""))
    return clean[:500]


def _configured_payment_url():
    raw = str(cfg_get("AOS_ASSURANCE_PAYMENT_URL") or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    # A static Stripe Payment Link is the smallest PCI-safe launch boundary. Never reflect an arbitrary URL
    # from request input into the public response.
    if parsed.scheme != "https" or parsed.hostname != "buy.stripe.com":
        return None
    return raw


def _insert(lead):
    _ensure()
    with connection() as c, c.cursor() as cur:
        cur.execute("DELETE FROM assurance_pilot_leads WHERE delete_after <= now() AND status NOT IN ('paid','won')")
        cur.execute("""INSERT INTO assurance_pilot_leads
                       (id,dedupe_hash,name,email,company,app_url,concern,access_mode,consent_version)
                       VALUES (%(id)s,%(dedupe_hash)s,%(name)s,%(email)s,%(company)s,%(app_url)s,
                               %(concern)s,%(access_mode)s,%(consent_version)s)
                       ON CONFLICT(dedupe_hash) DO UPDATE SET updated_at=now()
                       RETURNING id,status,(xmax=0) AS created""", lead)
        row = cur.fetchone()
    return {"id": row[0], "status": row[1], "created": bool(row[2])}


def submit(body):
    body = body if isinstance(body, dict) else {}
    # Honeypot: bots get the same success shape and no durable record.
    if _clean(body.get("website"), 200):
        return {"ok": True, "request_id": "received", "next": "email"}
    name = _clean(body.get("name"), 80)
    email = _email(body.get("email"))
    company = _clean(body.get("company"), 120)
    app_url = _app_url(body.get("app_url"))
    concern = _clean(body.get("concern"), 2000)
    access_mode = _clean(body.get("access_mode"), 40)
    consented = body.get("consent") is True
    errors = {}
    if len(name) < 2:
        errors["name"] = "Enter your name."
    if not email:
        errors["email"] = "Enter a valid work email."
    if len(company) < 2:
        errors["company"] = "Enter your company or team name."
    if not app_url:
        errors["app_url"] = "Enter an http(s) URL without credentials, query parameters, or fragments."
    if len(concern) < 20:
        errors["concern"] = "Describe the release risk or critical journey in at least 20 characters."
    if access_mode not in ACCESS_MODES:
        errors["access_mode"] = "Choose how we can inspect the app."
    if not consented:
        errors["consent"] = "Consent is required so we can contact you about this request."
    secret, _reason = looks_like_secret(" ".join([app_url or "", concern]))
    if secret:
        errors["concern"] = "Remove passwords, API keys, access tokens, or other secrets."
    if errors:
        return {"ok": False, "error": "invalid_intake", "fields": errors}

    dedupe = hashlib.sha256(f"{email}\n{app_url}".encode()).hexdigest()
    lead = {
        "id": f"arp_{secrets.token_hex(8)}",
        "dedupe_hash": dedupe,
        "name": name,
        "email": email,
        "company": company,
        "app_url": app_url,
        "concern": concern,
        "access_mode": access_mode,
        "consent_version": CONSENT_VERSION,
    }
    saved = _insert(lead)
    try:
        import notify
        notify.send(
            f"New Release Assurance pilot request {saved['id']}. Review the private intake queue.",
            title="New paid-pilot lead", priority="high", tags="moneybag,test_tube")
    except Exception:
        pass
    result = {
        "ok": True,
        "request_id": saved["id"],
        "next": "checkout" if _configured_payment_url() else "email",
    }
    if _configured_payment_url():
        result["payment_url"] = _configured_payment_url()
    return result


def recent(limit=100):
    """Founder/operator read path; console does not expose this unauthenticated."""
    _ensure()
    limit = max(1, min(500, int(limit)))
    with connection() as c, c.cursor() as cur:
        cur.execute("""SELECT id,name,email,company,app_url,concern,access_mode,status,created_at,delete_after
                         FROM assurance_pilot_leads ORDER BY created_at DESC LIMIT %s""", (limit,))
        rows = cur.fetchall()
    keys = ("id", "name", "email", "company", "app_url", "concern", "access_mode", "status",
            "created_at", "delete_after")
    return [dict(zip(keys, row)) for row in rows]


PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="One-business-day release assurance for AI-built web apps: reproducible browser evidence, defect triage, and an honest ship/no-ship verdict.">
<title>AI Release Assurance · Agent OS</title>
<style>
:root{--bg:#090b10;--panel:#131720;--panel2:#191f2b;--line:#2a3242;--text:#f2f4f8;--muted:#aab3c2;--blue:#7f9cff;--green:#62d8a3;--red:#ff8585;--ring:rgba(127,156,255,.4)}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0,#18213a 0,transparent 34rem),var(--bg);color:var(--text);font:16px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}a{color:inherit}.wrap{width:min(1120px,calc(100% - 36px));margin:auto}.nav{height:68px;display:flex;align-items:center;justify-content:space-between}.brand{font-weight:750;letter-spacing:-.02em}.brand span{color:var(--blue)}.nav a{font-size:14px;color:var(--muted)}.hero{display:grid;grid-template-columns:1.15fr .85fr;gap:56px;align-items:center;padding:72px 0 66px}.eyebrow{color:var(--green);font-size:13px;font-weight:750;letter-spacing:.1em;text-transform:uppercase}.hero h1{font-size:clamp(40px,6vw,70px);line-height:1.02;letter-spacing:-.055em;margin:14px 0 22px;max-width:13ch}.lead{font-size:20px;color:var(--muted);max-width:58ch}.chips{display:flex;flex-wrap:wrap;gap:9px;margin:26px 0}.chip{border:1px solid var(--line);background:rgba(19,23,32,.78);border-radius:999px;padding:7px 12px;color:#cbd2df;font-size:13px}.price{margin-top:22px;font-weight:700}.price span{color:var(--green);font-size:28px}.card{background:linear-gradient(160deg,rgba(25,31,43,.98),rgba(14,18,26,.98));border:1px solid var(--line);border-radius:20px;padding:26px;box-shadow:0 30px 70px rgba(0,0,0,.35)}.card h2{margin:0 0 5px;font-size:21px}.card p{margin:0 0 18px;color:var(--muted);font-size:14px}label{display:block;font-size:13px;font-weight:650;margin:13px 0 5px}input,textarea,select{width:100%;border:1px solid #364055;border-radius:10px;background:#0d1119;color:var(--text);padding:11px 12px;font:inherit;font-size:14px}textarea{min-height:105px;resize:vertical}input:focus,textarea:focus,select:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--ring)}[aria-invalid=true]{border-color:var(--red)}.check{display:flex;gap:9px;align-items:flex-start;margin:15px 0}.check input{width:auto;margin-top:4px}.check label{margin:0;font-weight:400;color:var(--muted);line-height:1.4}.button{display:flex;width:100%;align-items:center;justify-content:center;border:0;border-radius:11px;padding:13px 16px;background:var(--blue);color:#080b12;font-weight:800;font-size:15px;cursor:pointer}.button:disabled{opacity:.6;cursor:wait}.note{min-height:22px;margin-top:10px!important;color:var(--muted)!important}.note.err{color:var(--red)!important}.honeypot{position:absolute;left:-9999px}.section{padding:66px 0;border-top:1px solid rgba(42,50,66,.72)}.section h2{font-size:34px;letter-spacing:-.035em;margin:0 0 26px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.item{background:rgba(19,23,32,.75);border:1px solid var(--line);border-radius:15px;padding:21px}.item b{display:block;margin-bottom:7px}.item p{margin:0;color:var(--muted);font-size:14px}.scope{display:grid;grid-template-columns:1fr 1fr;gap:34px}.scope ul{padding-left:20px;color:var(--muted)}.scope li{margin:10px 0}.footer{padding:35px 0 55px;color:var(--muted);font-size:13px;border-top:1px solid var(--line)}
@media(max-width:850px){.hero{grid-template-columns:1fr;padding-top:42px}.grid{grid-template-columns:1fr}.scope{grid-template-columns:1fr}.hero h1{font-size:46px}.card{padding:20px}}
</style></head><body><div class="wrap"><nav class="nav"><div class="brand"><span>⬡</span> Agent OS</div><a href="/assurance/sample">Sample report</a></nav>
<main><section class="hero"><div><div class="eyebrow">Managed AI release assurance</div><h1>Know if your AI-built app is safe to ship.</h1><p class="lead">Give us a staging URL and your three most important user journeys. Within one business day, get reproducible browser evidence, real defects separated from infrastructure noise, and an honest ship/no-ship verdict.</p><div class="chips"><span class="chip">Real browser evidence</span><span class="chip">Desktop + mobile</span><span class="chip">Privacy-aware testing</span><span class="chip">Human-readable verdict</span></div><div class="price"><span>$500</span> founding release audit · credited toward a $1,000/month plan</div><p><a href="/assurance/sample">See an honest sample report from three completed journeys →</a></p></div>
<form class="card" id="intake" novalidate><h2>Request a founding audit</h2><p>We reply with scope and a secure checkout link. Never paste credentials or production secrets.</p>
<label for="name">Your name</label><input id="name" name="name" autocomplete="name" required maxlength="80">
<label for="email">Work email</label><input id="email" name="email" type="email" autocomplete="email" required maxlength="254">
<label for="company">Company or team</label><input id="company" name="company" autocomplete="organization" required maxlength="120">
<label for="app_url">Public or staging URL</label><input id="app_url" name="app_url" type="url" inputmode="url" placeholder="https://staging.example.com" required maxlength="500">
<label for="access_mode">Safe access method</label><select id="access_mode" name="access_mode" required><option value="">Choose one</option><option value="public_url">Public URL—no login</option><option value="temporary_test_account">Temporary test account (shared later via secure channel)</option><option value="guided_session">Guided screen-share session</option><option value="discuss">Discuss the safest option first</option></select>
<label for="concern">What release risk or journey matters most?</label><textarea id="concern" name="concern" minlength="20" maxlength="2000" placeholder="Example: signup → AI response → billing upgrade must work without duplicate charges." required></textarea>
<div class="honeypot" aria-hidden="true"><label for="website">Website</label><input id="website" name="website" tabindex="-1" autocomplete="off"></div>
<div class="check"><input id="consent" name="consent" type="checkbox" required><label for="consent">I agree that Agent OS may store this request for up to 90 days and contact me about this audit. I will not submit regulated data, passwords, API keys, or access tokens here.</label></div>
<button class="button" id="submit" type="submit">Request the $500 audit</button><p class="note" id="note" role="status" aria-live="polite"></p></form></section>
<section class="section"><h2>What arrives the next business day</h2><div class="grid"><div class="item"><b>1. Reproducible evidence</b><p>Story-by-story browser receipts, screenshots, and concise reproduction steps tied to the tested revision.</p></div><div class="item"><b>2. Defect triage</b><p>Product bugs separated from environment failures, test limitations, and ambiguous requirements.</p></div><div class="item"><b>3. Release decision</b><p>A plain-English ship, ship-with-known-risk, or do-not-ship verdict with the next action for every blocker.</p></div></div></section>
<section class="section scope"><div><h2>Founding audit scope</h2><ul><li>One browser-based web app</li><li>Up to three critical user journeys</li><li>Desktop and mobile viewport checks</li><li>One current revision and one evidence report</li><li>One-business-day target after safe access is ready</li></ul></div><div><h2>Safety boundaries</h2><ul><li>No production mutations or real customer records</li><li>No regulated health, financial, or government-ID data</li><li>No credentials submitted through this form</li><li>No penetration test, legal certification, or uptime guarantee</li><li>Additional browsers, revisions, and repair work are separately scoped</li></ul></div></section></main><footer class="footer">Agent OS · Managed release assurance for small AI SaaS teams and software agencies. Evidence is retained only for the agreed delivery window.</footer></div>
<script>
const form=document.getElementById('intake'),note=document.getElementById('note'),button=document.getElementById('submit');
form.addEventListener('submit',async e=>{e.preventDefault();note.className='note';note.textContent='';[...form.elements].forEach(x=>x.removeAttribute('aria-invalid'));if(!form.reportValidity())return;button.disabled=true;button.textContent='Submitting…';const data=Object.fromEntries(new FormData(form));data.consent=document.getElementById('consent').checked;try{const response=await fetch('/api/assurance/intake',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const result=await response.json();if(!response.ok||!result.ok){Object.keys(result.fields||{}).forEach(id=>{const el=document.getElementById(id);if(el)el.setAttribute('aria-invalid','true')});throw new Error(Object.values(result.fields||{})[0]||result.error||'Please check the form.')}form.reset();note.textContent=result.next==='checkout'?'Request received. Opening secure checkout…':'Request received. We will email you within one business day with scope and secure checkout.';if(result.payment_url)setTimeout(()=>location.assign(result.payment_url),700)}catch(err){note.className='note err';note.textContent=err.message||'Could not submit. Please try again.'}finally{button.disabled=false;button.textContent='Request the $500 audit'}});
</script></body></html>'''


SAMPLE_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="A redacted sample AI release assurance report with real browser evidence.">
<title>Sample Release Assurance Report · Agent OS</title>
<style>
:root{--bg:#090b10;--panel:#151a24;--line:#2a3242;--text:#f2f4f8;--muted:#aab3c2;--blue:#8ca5ff;--green:#62d8a3;--amber:#ffd37a}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.6 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}a{color:var(--blue)}main{width:min(960px,calc(100% - 36px));margin:auto;padding:42px 0 80px}.back{font-size:14px}.eyebrow{margin-top:44px;color:var(--green);font-size:13px;font-weight:750;letter-spacing:.1em;text-transform:uppercase}h1{font-size:clamp(38px,7vw,64px);line-height:1.04;letter-spacing:-.05em;margin:12px 0 18px}.lede{max-width:720px;color:var(--muted);font-size:19px}.verdict{margin:32px 0;padding:22px;border:1px solid #745d32;background:#211d14;border-radius:15px}.verdict b{color:var(--amber)}.meta,.stories{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.box{border:1px solid var(--line);background:var(--panel);border-radius:14px;padding:18px}.box span{display:block;color:var(--muted);font-size:13px}.box b{display:block;margin-top:4px}.section{margin-top:52px;padding-top:36px;border-top:1px solid var(--line)}h2{font-size:30px;letter-spacing:-.03em}.story{margin:18px 0;padding:22px;border:1px solid var(--line);border-radius:14px;background:var(--panel)}.story h3{margin:0 0 8px}.pass{color:var(--green)}img{display:block;width:100%;height:auto;border:1px solid var(--line);border-radius:12px;margin:18px 0}.caption{font-size:13px;color:var(--muted)}ul{color:var(--muted)}.cta{margin-top:50px;padding:28px;border-radius:15px;background:#171f35}.button{display:inline-block;margin-top:10px;padding:11px 16px;border-radius:9px;background:var(--blue);color:#080b12;text-decoration:none;font-weight:800}@media(max-width:720px){.meta,.stories{grid-template-columns:1fr}}
</style></head><body><main><a class="back" href="/assurance">← Release Assurance</a>
<div class="eyebrow">Redacted sample · real browser run</div><h1>Trust-first dog walking app</h1>
<p class="lede">This is a buyer-facing extract from a completed twelve-story campaign. It publishes three representative journeys with clean, revision-traceable evidence while keeping raw operational artifacts private.</p>
<div class="verdict"><b>Full campaign verdict: ship — 12 of 12 stories passed.</b> The final current-revision gate reported zero open or blocking findings. This redacted extract shows minimal enquiry, abusive-input privacy, and governed trust-content publishing; the internal gate preserves proof for all twelve stories. Gate 1138 · revision 3f6b4aa5bc82 · 25 August 2026.</div>
<div class="meta"><div class="box"><span>Campaign result</span><b>12 / 12 passed</b></div><div class="box"><span>Scope shown</span><b>3 featured journeys</b></div><div class="box"><span>Open findings</span><b>0</b></div></div>
<section class="section"><h2>Story results</h2><div class="stories"><div class="story"><h3>US-001 · Minimal enquiry</h3><b class="pass">PASS · 20 steps</b><p>Required inputs, consent, idle stability, confirmation, persistence, and safe operating-state updates were exercised in a real browser.</p></div><div class="story"><h3>US-005 · Privacy boundary</h3><b class="pass">PASS · 9 steps</b><p>Markup, credential-like text, private contact data, and long Unicode input were exercised without script execution or disclosure into public and diagnostic surfaces.</p></div><div class="story"><h3>US-010 · Governed publishing</h3><b class="pass">PASS · 12 steps</b><p>Ineligible claims and sensitive public content were blocked; valid evidenced content was published and survived a reload on the current product revision.</p></div></div></section>
<section class="section"><h2>Evidence excerpts</h2><img src="/assurance/sample/assets/public-trust-boundary.png" alt="Public dog walking app screen demonstrating the tested trust boundary"><p class="caption">Public trust surface exercised by US-001 and US-010. No customer identity or access credential is shown.</p><img src="/assurance/sample/assets/redacted-operating-state.png" alt="Redacted staff operating-state evidence from the dog walking app"><p class="caption">Operating-state evidence from the valid-enquiry path. Sensitive fields were redacted before publication.</p></section>
<section class="section"><h2>What a paid report adds</h2><ul><li>Your three highest-value journeys agreed before testing starts</li><li>Exact reproduction steps and screenshots for every material defect</li><li>Product failures separated from environment and test limitations</li><li>A current-revision ship, ship-with-known-risk, or do-not-ship decision</li><li>A short handoff call and prioritized next actions</li></ul></section>
<div class="cta"><h2>Get your release decision in one business day.</h2><p>The founding audit is $500 and is credited toward a $1,000/month recurring plan.</p><a class="button" href="/assurance#intake">Request a founding audit</a></div>
</main></body></html>'''


def page():
    return PAGE


def sample_page():
    return SAMPLE_PAGE


def sample_asset(name):
    """Return only the two deliberately published, redacted evidence assets."""
    path = SAMPLE_ASSETS.get(str(name or ""))
    return path.read_bytes() if path and path.is_file() else None


def cli_leads():
    print(json.dumps(recent(), indent=2, default=str))


if __name__ == "__main__":
    cli_leads()
