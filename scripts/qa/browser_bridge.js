#!/usr/bin/env node
// browser_bridge.js — the persistent "eyes + hands" for agent-os's agentic QA loop.
//
// A long-lived Node process wrapping a single Playwright chromium page. It reads JSON commands
// one-per-line from stdin and writes exactly one JSON response line per command to stdout. The
// AI QA loop (Python, via factory.agent) drives it: observe (`state`) -> decide (AI) ->
// act (`click`/`fill`/...) -> observe again. Because it's persistent, the browser keeps its
// session (cookies, localStorage, in-flight SPA state) across the whole AI conversation instead
// of cold-starting per step — so the AI can judge expected-vs-actual behavior over a real flow.
//
//   NODE_PATH=/home/swami/projects/products/noupload/node_modules node browser_bridge.js
//   NODE_PATH=...                                    node browser_bridge.js selftest
//
// PROTOCOL (one JSON object per line, both directions). Every response echoes the request `id`
// (if any) and `cmd`, plus `ok:true|false`. On any failure/timeout the response is
// {id,cmd,ok:false,error:"..."} — the process NEVER dies on a bad command, so the AI loop can
// always recover and try something else.
//
//   ->  {"id":1,"cmd":"goto","url":"https://app/"}
//   <-  {"id":1,"cmd":"goto","ok":true,"url":"https://app/#/","title":"...","status":200}
//
// COMMANDS
//   {cmd:"goto",   url}                 -> {url, title, status}                navigate (domcontentloaded)
//   {cmd:"state"}                       -> {url,title,screenshot,elements[],    the observation the AI reasons over
//                                           console_errors[], recent_requests[]}
//   {cmd:"click",  selector|idx}        -> {clicked}                           idx refers to elements[] from last state
//   {cmd:"fill",   selector|idx, value} -> {filled,value}
//   {cmd:"inject", script}              -> {result}                            run a statement block in the page
//   {cmd:"eval",   expr}                -> {result}                            evaluate an expression, return value
//   {cmd:"seedToken", token, org}       -> {seeded}                            localStorage aos_tenant/aos_org + reload
//   {cmd:"close"}                       -> {closed}  then the process exits 0
//   {cmd:"ping"}                        -> {pong:true}
//
// GUARANTEES
//   * Native dialogs (alert/confirm/prompt/beforeunload) auto-dismiss so nothing blocks.
//   * Every command runs under a HARD 2.5s timeout — a hung page yields ok:false, never a hang.
//   * `elements[]` items carry a stable `selector` ([data-aos-idx="N"]) so the AI can act by idx.
//   * Screenshots are PNGs written under /tmp/aos-qa/ (path returned in `state.screenshot`).

'use strict';

const CMD_TIMEOUT_MS = 2500;         // hard per-command ceiling — nothing may hang the loop
const SHOT_DIR = '/tmp/aos-qa';
const MAX_REQUESTS = 25;             // ring buffer of recent network requests surfaced in `state`
const MAX_CONSOLE = 25;

const fs = require('fs');
const path = require('path');
const readline = require('readline');

let chromium = null;
try { ({ chromium } = require('playwright')); } catch (_) { chromium = null; }

function nodePathHint() {
  return process.env.NODE_PATH || '/home/swami/projects/products/noupload/node_modules';
}
function ensureDir(d) { try { fs.mkdirSync(d, { recursive: true }); } catch (_) {} }

// Race async work against a hard timeout. On timeout we reject — the caller turns that into
// {ok:false,error:'timeout'} rather than letting a stuck page wedge the whole QA session.
function withTimeout(promise, ms, label) {
  return new Promise((resolve, reject) => {
    let done = false;
    const t = setTimeout(() => {
      if (done) return;
      done = true;
      reject(new Error('timeout after ' + ms + 'ms' + (label ? ' (' + label + ')' : '')));
    }, ms);
    Promise.resolve(promise).then(
      (v) => { if (done) return; done = true; clearTimeout(t); resolve(v); },
      (e) => { if (done) return; done = true; clearTimeout(t); reject(e); }
    );
  });
}

// In-page collector: tag every visible interactive element with data-aos-idx and return a compact
// description the AI can reason over. Runs fresh on every `state` so idx<->element stays consistent
// with whatever the page currently shows (re-tagging survives SPA re-renders / navigation).
const COLLECT_ELEMENTS = `(() => {
  const SEL = 'a,button,input,select,textarea,[role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="checkbox"],[onclick],[contenteditable="true"],summary,label';
  const out = [];
  const seen = new Set();
  let i = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (seen.has(el)) continue; seen.add(el);
    let r; try { r = el.getBoundingClientRect(); } catch (_) { continue; }
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) <= 0.05) continue;
    const isControl = /^(input|select|textarea|button)$/i.test(el.tagName);
    if (!isControl && r.width === 0 && r.height === 0) continue;
    el.setAttribute('data-aos-idx', String(i));
    const text = (el.innerText || el.value || el.getAttribute('placeholder') ||
                  el.getAttribute('aria-label') || el.getAttribute('name') ||
                  el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim().slice(0, 140);
    out.push({
      idx: i,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      text,
      selector: '[data-aos-idx="' + i + '"]'
    });
    i++;
    if (i >= 300) break;
  }
  return out;
})()`;

class Bridge {
  constructor() {
    this.browser = null;
    this.ctx = null;
    this.page = null;
    this.consoleErrors = [];
    this.requests = [];
  }

  async start() {
    if (!chromium) throw new Error('playwright not found — set NODE_PATH=' + nodePathHint());
    this.browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
    this.ctx = await this.browser.newContext({
      viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1,
    });
    this.page = await this.ctx.newPage();
    this._wire(this.page);
  }

  _wire(page) {
    // Auto-dismiss any native dialog so a stray confirm()/alert()/beforeunload can never block.
    page.on('dialog', async (d) => { try { await d.dismiss(); } catch (_) {} });
    page.on('console', (m) => {
      if (m.type() === 'error') {
        this.consoleErrors.push(String(m.text()).slice(0, 500));
        if (this.consoleErrors.length > MAX_CONSOLE) this.consoleErrors.shift();
      }
    });
    page.on('pageerror', (e) => {
      this.consoleErrors.push('pageerror: ' + String(e && e.message ? e.message : e).slice(0, 500));
      if (this.consoleErrors.length > MAX_CONSOLE) this.consoleErrors.shift();
    });
    page.on('requestfinished', async (req) => {
      let status = null;
      try { const r = await req.response(); status = r ? r.status() : null; } catch (_) {}
      this.requests.push({ method: req.method(), url: String(req.url()).slice(0, 300), status });
      if (this.requests.length > MAX_REQUESTS) this.requests.shift();
    });
    page.on('requestfailed', (req) => {
      this.requests.push({
        method: req.method(), url: String(req.url()).slice(0, 300),
        status: null, failed: (req.failure() && req.failure().errorText) || 'failed',
      });
      if (this.requests.length > MAX_REQUESTS) this.requests.shift();
    });
  }

  // Resolve a command's target element: explicit `selector`, or `idx` mapped to the data-attr the
  // last `state` stamped onto the page.
  _selectorFor(msg) {
    if (msg.selector) return msg.selector;
    if (msg.idx !== undefined && msg.idx !== null) return '[data-aos-idx="' + msg.idx + '"]';
    return null;
  }

  async goto(msg) {
    if (!msg.url) throw new Error('goto requires url');
    // domcontentloaded (not networkidle) keeps us inside the 2.5s ceiling on chatty SPAs; the AI
    // can always take another `state` once things settle.
    let status = null;
    try {
      const r = await this.page.goto(msg.url, { waitUntil: 'domcontentloaded', timeout: CMD_TIMEOUT_MS - 200 });
      status = r ? r.status() : null;
    } catch (e) {
      // Navigation may still have partially rendered something usable — report but don't hard-fail.
      return { url: this.page.url(), title: null, status, note: 'nav:' + e.message };
    }
    let title = null;
    try { title = await this.page.title(); } catch (_) {}
    return { url: this.page.url(), title, status };
  }

  async state() {
    ensureDir(SHOT_DIR);
    const file = path.join(SHOT_DIR, 'state-' + Date.now() + '-' + Math.floor(Math.random() * 1e4) + '.png');
    let shotOk = false;
    try { await this.page.screenshot({ path: file, fullPage: false }); shotOk = fs.existsSync(file); } catch (_) {}
    let elements = [];
    try { elements = await this.page.evaluate(COLLECT_ELEMENTS); } catch (_) { elements = []; }
    let url = null, title = null;
    try { url = this.page.url(); } catch (_) {}
    try { title = await this.page.title(); } catch (_) {}
    return {
      url,
      title,
      screenshot: shotOk ? file : null,
      elements,
      console_errors: this.consoleErrors.slice(-MAX_CONSOLE),
      recent_requests: this.requests.slice(-MAX_REQUESTS),
    };
  }

  async click(msg) {
    const sel = this._selectorFor(msg);
    if (!sel) throw new Error('click requires selector or idx');
    await this.page.click(sel, { timeout: CMD_TIMEOUT_MS - 200 });
    return { clicked: sel };
  }

  async fill(msg) {
    const sel = this._selectorFor(msg);
    if (!sel) throw new Error('fill requires selector or idx');
    const value = msg.value === undefined ? '' : String(msg.value);
    await this.page.fill(sel, value, { timeout: CMD_TIMEOUT_MS - 200 });
    return { filled: sel, value };
  }

  async inject(msg) {
    if (typeof msg.script !== 'string') throw new Error('inject requires script (string)');
    // Run an arbitrary statement block in the page; return whatever it returns (if serializable).
    const result = await this.page.evaluate((src) => (new Function(src))(), msg.script);
    return { result: result === undefined ? null : result };
  }

  async eval(msg) {
    if (typeof msg.expr !== 'string') throw new Error('eval requires expr (string)');
    // eslint-disable-next-line no-eval
    const result = await this.page.evaluate((src) => eval(src), msg.expr);
    return { result: result === undefined ? null : result };
  }

  async seedToken(msg) {
    if (!msg.token) throw new Error('seedToken requires token');
    const token = String(msg.token);
    const org = msg.org === undefined || msg.org === null ? '' : String(msg.org);
    // Seed BEFORE any app code runs on the next load (addInitScript) and on the current document,
    // then reload so the SPA boots authenticated against this tenant/org.
    await this.ctx.addInitScript(([t, o]) => {
      try { localStorage.setItem('aos_tenant', t); if (o) localStorage.setItem('aos_org', o); } catch (_) {}
    }, [token, org]);
    try {
      await this.page.evaluate(([t, o]) => {
        try { localStorage.setItem('aos_tenant', t); if (o) localStorage.setItem('aos_org', o); } catch (_) {}
      }, [token, org]);
    } catch (_) { /* page may be about:blank */ }
    try { await this.page.reload({ waitUntil: 'domcontentloaded', timeout: CMD_TIMEOUT_MS - 200 }); } catch (_) {}
    return { seeded: true, org };
  }

  async close() {
    try { if (this.browser) await this.browser.close(); } catch (_) {}
    return { closed: true };
  }

  // Dispatch one parsed command under the hard timeout. Returns a response object (never throws).
  async handle(msg) {
    const base = { id: msg.id !== undefined ? msg.id : null, cmd: msg.cmd };
    try {
      let out;
      switch (msg.cmd) {
        case 'goto':      out = await withTimeout(this.goto(msg), CMD_TIMEOUT_MS, 'goto'); break;
        case 'state':     out = await withTimeout(this.state(), CMD_TIMEOUT_MS, 'state'); break;
        case 'click':     out = await withTimeout(this.click(msg), CMD_TIMEOUT_MS, 'click'); break;
        case 'fill':      out = await withTimeout(this.fill(msg), CMD_TIMEOUT_MS, 'fill'); break;
        case 'inject':    out = await withTimeout(this.inject(msg), CMD_TIMEOUT_MS, 'inject'); break;
        case 'eval':      out = await withTimeout(this.eval(msg), CMD_TIMEOUT_MS, 'eval'); break;
        case 'seedToken': out = await withTimeout(this.seedToken(msg), CMD_TIMEOUT_MS, 'seedToken'); break;
        case 'close':     out = await this.close(); break;
        case 'ping':      out = { pong: true }; break;
        default:          return Object.assign(base, { ok: false, error: 'unknown cmd: ' + msg.cmd });
      }
      return Object.assign(base, { ok: true }, out);
    } catch (e) {
      return Object.assign(base, { ok: false, error: String(e && e.message ? e.message : e) });
    }
  }
}

function writeLine(obj) {
  try { process.stdout.write(JSON.stringify(obj) + '\n'); }
  catch (e) { process.stdout.write(JSON.stringify({ ok: false, error: 'serialize: ' + e.message }) + '\n'); }
}

async function runBridge() {
  const bridge = new Bridge();
  try {
    await bridge.start();
  } catch (e) {
    writeLine({ ok: false, cmd: 'ready', error: 'startup: ' + String(e && e.message ? e.message : e) });
    process.exit(1);
    return;
  }
  writeLine({ ok: true, cmd: 'ready', pid: process.pid, shot_dir: SHOT_DIR });

  // Serialize commands: one in flight at a time so responses map cleanly to requests (the AI loop is
  // strictly observe->act->observe, never concurrent).
  const rl = readline.createInterface({ input: process.stdin });
  let chain = Promise.resolve();
  rl.on('line', (line) => {
    const raw = line.trim();
    if (!raw) return;
    chain = chain.then(async () => {
      let msg;
      try { msg = JSON.parse(raw); }
      catch (e) { writeLine({ ok: false, error: 'bad json: ' + e.message }); return; }
      const res = await bridge.handle(msg);
      writeLine(res);
      if (msg.cmd === 'close') { rl.close(); process.exit(0); }
    });
  });
  process.on('SIGTERM', () => process.exit(0));
  process.on('SIGINT', () => process.exit(0));
}

// ---------------------------------------------------------------------------------------------
// SELFTEST — spawns THIS file as a real bridge child and drives the actual stdin/stdout protocol.
// No network, no API keys: a data: URL exercises goto + state (screenshot + element parsing) +
// act-by-idx + eval + close. Deterministic and cheap; exits non-zero on any failed assertion.
// ---------------------------------------------------------------------------------------------
async function runSelftest() {
  const { spawn } = require('child_process');
  const assert = require('assert');
  const HTML = 'data:text/html,' + encodeURIComponent(
    '<html><body><h1>QA selftest</h1>' +
    '<button id="go">Go</button>' +
    '<input id="q" type="text" placeholder="search here">' +
    '<a href="#next">Next link</a></body></html>');

  const child = spawn(process.execPath, [__filename], { env: process.env, stdio: ['pipe', 'pipe', 'inherit'] });

  const waiters = new Map();
  let ready;
  const readyP = new Promise((res) => { ready = res; });
  const rl = readline.createInterface({ input: child.stdout });
  rl.on('line', (line) => {
    let obj; try { obj = JSON.parse(line); } catch (_) { return; }
    if (obj.cmd === 'ready') { ready(obj); return; }
    if (obj.id !== undefined && obj.id !== null && waiters.has(obj.id)) {
      const w = waiters.get(obj.id); waiters.delete(obj.id); w(obj);
    }
  });
  const send = (msg) => new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('selftest: no response for ' + msg.cmd)), 15000);
    waiters.set(msg.id, (o) => { clearTimeout(t); resolve(o); });
    child.stdin.write(JSON.stringify(msg) + '\n');
  });

  let failed = false;
  try {
    await withTimeout(readyP, 30000, 'ready');

    const g = await send({ id: 1, cmd: 'goto', url: HTML });
    assert.strictEqual(g.ok, true, 'goto ok');

    const s = await send({ id: 2, cmd: 'state' });
    assert.strictEqual(s.ok, true, 'state ok');
    assert.ok(s.screenshot && fs.existsSync(s.screenshot), 'screenshot file exists on disk');
    assert.ok(Array.isArray(s.elements) && s.elements.length >= 3, 'parsed >=3 elements (button,input,link)');
    const tags = s.elements.map((e) => e.tag);
    assert.ok(tags.includes('button') && tags.includes('input') && tags.includes('a'), 'button+input+a present');
    assert.ok(s.elements.every((e) => typeof e.selector === 'string' && e.selector.includes('data-aos-idx')),
      'every element carries an actionable selector');
    assert.ok(Array.isArray(s.console_errors) && Array.isArray(s.recent_requests), 'state has console_errors+recent_requests arrays');

    // Exercise act-by-idx: fill the input, then read it back via eval.
    const inputEl = s.elements.find((e) => e.tag === 'input');
    const f = await send({ id: 3, cmd: 'fill', idx: inputEl.idx, value: 'hello' });
    assert.strictEqual(f.ok, true, 'fill ok');
    const ev = await send({ id: 4, cmd: 'eval', expr: "document.getElementById('q').value" });
    assert.strictEqual(ev.result, 'hello', 'eval reads back the filled value');

    const c = await send({ id: 9, cmd: 'close' });
    assert.strictEqual(c.ok, true, 'close ok');

    console.log(JSON.stringify({ selftest: 'PASS', elements: s.elements.length, screenshot: s.screenshot, tags }));
  } catch (e) {
    failed = true;
    console.error(JSON.stringify({ selftest: 'FAIL', error: String(e && e.message ? e.message : e) }));
  } finally {
    try { child.stdin.end(); } catch (_) {}
    try { child.kill(); } catch (_) {}
  }
  process.exit(failed ? 1 : 0);
}

if (require.main === module) {
  if (process.argv[2] === 'selftest') runSelftest();
  else runBridge();
}

module.exports = { Bridge, withTimeout };
