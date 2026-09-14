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
//   {cmd:"back"|"forward"}             -> {from,url,history:true}             real browser-history traversal
//   {cmd:"reload"}                     -> {from,url,reloaded:true,status}      true browser refresh (not same-URL goto)
//   {cmd:"state"}                       -> {url,title,screenshot,elements[],    the observation the AI reasons over
//                                           settled:true,                        (snapshot taken AFTER the SPA settles —
//                                           console_errors[], recent_requests[]}  no half-painted skeletons)
//   {cmd:"settle", ms?}                 -> {settled,waited_ms}                   wait extra paint grace, then settle
//   {cmd:"wait", timeout_ms}             -> {waited,elapsed_ms}                   real bounded page dwell
//                                                                                (used before RE-OBSERVING a late control)
//   {cmd:"click",  selector|idx}        -> {clicked}                           idx refers to elements[] from last state
//   {cmd:"clickBurst",selector|idx,count,interval_ms?}
//                                        -> {clicked,count,timestamps,elapsed_ms} bounded rapid activation proof
//   {cmd:"timedTransition",selector|idx,duration_ms,pending_text?}
//                                        -> {timedTransition,transition} atomic transient-state proof
//   {cmd:"clickByText", text, role?}    -> {clicked, matched, matchedRole,      click the control whose LABEL best
//                                           score}                              matches text (exact>contains), role-filtered
//   {cmd:"fill",   selector|idx, value} -> {filled,value}
//   {cmd:"inject", script}              -> {result}                            run a statement block in the page
//   {cmd:"eval",   expr}                -> {result}                            evaluate an expression, return value
//   {cmd:"seedToken", token, org}       -> {seeded}                            localStorage aos_tenant/aos_org + reload
//   {cmd:"close"}                       -> {closed}  then the process exits 0
//   {cmd:"ping"}                        -> {pong:true}
//
// GUARANTEES
//   * Native dialogs (alert/confirm/prompt/beforeunload) auto-dismiss so nothing blocks.
//   * Commands run under bounded timeouts — long, finite batch actions receive a workload-sized budget,
//     while a hung page still yields ok:false instead of wedging the QA session.
//   * `elements[]` items carry a stable `selector` ([data-aos-idx="N"]) so the AI can act by idx.
//   * Screenshots are PNGs written under /tmp/aos-qa/ (path returned in `state.screenshot`).

'use strict';

// Ceilings are generous enough for a REAL app (a human waits several seconds for a heavy view), but still
// hard — nothing may hang the loop. All env-overridable so a slow target can be tuned WITHOUT a code change.
// Raised from the original 2.5s/9s (owner: those starved real journeys — QA gave up before the app settled).
const CMD_TIMEOUT_MS = +(process.env.AOS_QA_CMD_TIMEOUT_MS || 8000);   // hard per-command ceiling for ACTIONS
// A full keyboard audit is a batch of up to 120 trusted key presses, DOM snapshots, focus-style reads, and
// AT event collection. Giving that whole batch the single-control ceiling abandoned healthy traversals at
// ~70% and then made the explorer repeat them from scratch. Keep a bounded failsafe, but size it to the
// declared workload. When count is omitted tabTraverse derives the page inventory, so reserve the maximum
// supported batch; normal completion still returns immediately.
const TRAVERSE_TIMEOUT_BASE_MS = +(process.env.AOS_QA_TRAVERSE_TIMEOUT_BASE_MS || 15000);
const TRAVERSE_TIMEOUT_PER_STOP_MS = +(process.env.AOS_QA_TRAVERSE_TIMEOUT_PER_STOP_MS || 250);
const TRAVERSE_TIMEOUT_MAX_MS = +(process.env.AOS_QA_TRAVERSE_TIMEOUT_MAX_MS || 60000);
// `state`/`settle` SNAPSHOT the SPA only AFTER it stops painting, so they get a longer ceiling than an
// action: settle can spend up to networkidle + the poll cap before we even screenshot.
const STATE_TIMEOUT_MS = +(process.env.AOS_QA_STATE_TIMEOUT_MS || 15000); // hard ceiling for state/settle
const SETTLE_NETIDLE_MS = +(process.env.AOS_QA_SETTLE_NETIDLE_MS || 4000); // bounded best-effort networkidle
const SETTLE_MAX_MS = +(process.env.AOS_QA_SETTLE_MAX_MS || 7000);     // hard cap on the poll-until-stable loop
const ASYNC_WAIT_MAX_MS = +(process.env.AOS_QA_EXTERNAL_WAIT_MAX_MS || 300000);
const SETTLE_SAMPLE_MS = 250;        // gap between the two consecutive stability samples
const SETTLE_REOBSERVE_MS = 700;     // extra paint grace when the loop explicitly re-observes a late control
const SHOT_DIR = process.env.AOS_QA_SHOT_DIR || process.env.AOS_QA_DIR || '/tmp/aos-qa';
const VIDEO_DIR = process.env.AOS_QA_VIDEO_DIR || '';
const STORAGE_STATE_PATH = process.env.AOS_QA_STORAGE_STATE_PATH || '';
// A real external assistive-technology driver (Orca/AT-SPI) needs a headed Chromium connected to
// its X display.  Headless Chromium's CDP accessibility tree is useful evidence, but it is not a
// screen reader and must never be reported as one.  The Python bridge only sets this after it has
// started and health-checked the external AT session.
const ACTUAL_AT_DRIVER = (process.env.AOS_QA_AT_DRIVER || '').trim().toLowerCase();
const MAX_REQUESTS = 1000;           // bounded cumulative trace since the last explicit evidence clear
const MAX_CONSOLE = 1000;

const fs = require('fs');
const path = require('path');
const readline = require('readline');

let chromium = null;
try { ({ chromium } = require('playwright')); } catch (_) { chromium = null; }

function nodePathHint() {
  return process.env.NODE_PATH || '/home/swami/projects/products/noupload/node_modules';
}
function ensureDir(d) { try { fs.mkdirSync(d, { recursive: true }); } catch (_) {} }
function isExternalHandoffUrl(url) {
  return /^(mailto|tel|sms):/i.test(String(url || ''));
}

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

function tabTraverseTimeoutMs(msg) {
  const supplied = Number(msg && msg.count);
  const stops = Number.isInteger(supplied) && supplied > 0
    ? Math.min(120, Math.max(2, supplied))
    : 120;
  const paceMs = Math.min(1500, Math.max(0, Number(msg && msg.pace_ms || 35)));
  return Math.min(
    Math.max(CMD_TIMEOUT_MS, TRAVERSE_TIMEOUT_MAX_MS),
    Math.max(CMD_TIMEOUT_MS, TRAVERSE_TIMEOUT_BASE_MS + stops * Math.max(
      TRAVERSE_TIMEOUT_PER_STOP_MS, paceMs + 75)),
  );
}

// In-page collector: tag every visible interactive element with data-aos-idx and return a compact
// description the AI can reason over. Runs fresh on every `state` so idx<->element stays consistent
// with whatever the page currently shows (re-tagging survives SPA re-renders / navigation).
const COLLECT_ELEMENTS = `(() => {
  const SEL = 'a,button,input,select,textarea,[role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="checkbox"],[onclick],[contenteditable="true"],summary,label';
  const visible = (el) => {
    let r; try { r = el.getBoundingClientRect(); } catch (_) { return null; }
    if (r.width <= 0 || r.height <= 0) return null;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const st = window.getComputedStyle(n);
      if (n.hidden || n.getAttribute('aria-hidden') === 'true' || st.display === 'none' ||
          st.visibility === 'hidden' || Number(st.opacity) <= 0.05) return null;
    }
    return r;
  };
  const out = [];
  const seen = new Set();
  let i = 0;
  for (const old of document.querySelectorAll('[data-aos-idx]')) old.removeAttribute('data-aos-idx');
  for (const el of document.querySelectorAll(SEL)) {
    if (seen.has(el)) continue; seen.add(el);
    const isControl = /^(input|select|textarea|button)$/i.test(el.tagName);
    const r = visible(el);
    if (!r) continue;
    el.setAttribute('data-aos-idx', String(i));
    const aria = (el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 140);
    const associatedLabel = (isControl ? Array.from(el.labels || []).map((label) => label.innerText || '') : [])
      .join(' ').replace(/\\s+/g, ' ').trim().slice(0, 140);
    const text = (el.innerText || aria || associatedLabel || el.getAttribute('placeholder') ||
                  el.getAttribute('name') || el.value ||
                  el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim().slice(0, 140);
    const activatingType = (el.getAttribute('type') || '').toLowerCase();
    const owningForm = isControl ? (el.form || el.closest('form')) : null;
    const formIndex = owningForm ? Array.from(document.forms || []).indexOf(owningForm) : -1;
    const submitsForm = isControl && ((el.tagName.toLowerCase() === 'button' &&
      !['button', 'reset'].includes(activatingType)) ||
      (el.tagName.toLowerCase() === 'input' && ['submit', 'image'].includes(activatingType)));
    const form = submitsForm ? owningForm : null;
    const formValid = form ? Array.from(form.elements || []).every((control) =>
      !control || control.disabled || !control.willValidate ||
      Boolean(control.validity && control.validity.valid)) : null;
    out.push({
      idx: i,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      text,
      // the LABEL the AI targets by — surfaced explicitly so intent->control is unambiguous.
      ariaLabel: aria,
      associatedLabel,
      options: el.tagName.toLowerCase() === 'select' ? Array.from(el.options || [])
        .map((option) => String(option.label || option.textContent || '').replace(/\s+/g, ' ').trim())
        .filter(Boolean).join(' | ').slice(0, 500) : undefined,
      role: (el.getAttribute('role') || '').toLowerCase(),
      id: (el.getAttribute('id') || '').slice(0, 100),
      name: (el.getAttribute('name') || '').slice(0, 80),
      formIndex: formIndex >= 0 ? formIndex : undefined,
      placeholder: (el.getAttribute('placeholder') || '').slice(0, 80),
      href: (el.getAttribute('href') || '').slice(0, 120),
      // SEMANTIC STATE — the evaluator must judge selected/checked/disabled from the DOM, never from
      // pixels alone (hover/focus/transition styling in a screenshot is not state).
      pressed: el.getAttribute('aria-pressed') || undefined,
      selectedState: el.getAttribute('aria-selected') || undefined,
      current: el.getAttribute('aria-current') || undefined,
      expanded: el.getAttribute('aria-expanded') || undefined,
      checked: isControl && (el.type === 'checkbox' || el.type === 'radio') ? String(!!el.checked) : undefined,
      required: isControl && (el.required || el.getAttribute('aria-required') === 'true') ? 'true' : undefined,
      disabled: (el.disabled || el.getAttribute('aria-disabled') === 'true') ? 'true' : undefined,
      min: isControl && el.getAttribute('min') !== null ? el.getAttribute('min') : undefined,
      max: isControl && el.getAttribute('max') !== null ? el.getAttribute('max') : undefined,
      minLength: isControl && Number.isInteger(el.minLength) && el.minLength >= 0 ? el.minLength : undefined,
      maxLength: isControl && Number.isInteger(el.maxLength) && el.maxLength >= 0 ? el.maxLength : undefined,
      formValid: form ? String(Boolean(formValid)) : undefined,
      value: isControl && !/^(password)$/i.test(el.type || '') ? String(el.value || '').slice(0, 60) : undefined,
      selector: '[data-aos-idx="' + i + '"]'
    });
    i++;
    if (i >= 300) break;
  }
  return out;
})()`;

const ACTIVE_ELEMENT = `(() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return null;
  const isControl = /^(input|select|textarea|button)$/i.test(el.tagName);
  const aria = (el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 140);
  const associatedLabel = (isControl ? Array.from(el.labels || []).map((label) => label.innerText || '') : [])
    .join(' ').replace(/\\s+/g, ' ').trim().slice(0, 140);
  const text = (el.innerText || aria || associatedLabel || el.getAttribute('placeholder') ||
                el.getAttribute('name') || el.value ||
                el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim().slice(0, 140);
  const focusEvidence = (() => {
    try {
      const s = getComputedStyle(el);
      let pseudo = false;
      try { pseudo = el.matches(':focus-visible'); } catch (_) {}
      const outlineColor = String(s.outlineColor || '').toLowerCase();
      const outlineVisible = !['none', 'hidden'].includes(String(s.outlineStyle || '').toLowerCase()) &&
        Number.parseFloat(s.outlineWidth || '0') > 0 && outlineColor !== 'transparent' &&
        outlineColor !== 'rgba(0, 0, 0, 0)';
      const boxShadow = String(s.boxShadow || '');
      const shadowVisible = boxShadow !== '' && boxShadow !== 'none' &&
        !boxShadow.includes('rgba(0, 0, 0, 0)');
      return { visible: pseudo || outlineVisible || shadowVisible, pseudo, style: {
        outline: [s.outlineStyle, s.outlineWidth, s.outlineColor].join(' '),
        outlineOffset: s.outlineOffset,
        boxShadow: boxShadow.slice(0, 300),
      }};
    } catch (_) { return { visible: false, pseudo: false, style: null }; }
  })();
  return {
    tag: el.tagName.toLowerCase(),
    type: (el.getAttribute('type') || '').toLowerCase(),
    text,
    ariaLabel: aria,
    associatedLabel,
    role: (el.getAttribute('role') || '').toLowerCase(),
    name: (el.getAttribute('name') || '').slice(0, 80),
    placeholder: (el.getAttribute('placeholder') || '').slice(0, 80),
    href: (el.getAttribute('href') || '').slice(0, 120),
    current: el.getAttribute('aria-current') || undefined,
    disabled: (el.disabled || el.getAttribute('aria-disabled') === 'true') ? 'true' : undefined,
    value: isControl && !/^(password)$/i.test(el.type || '') ? String(el.value || '').slice(0, 60) : undefined,
    focusVisible: focusEvidence.visible,
    focusPseudoVisible: focusEvidence.pseudo,
    focusStyle: focusEvidence.style
  };
})()`;

const ACCESSIBILITY_REGIONS = `(() => {
  const out = [];
  const selectors = '[role="status"],[role="alert"],[aria-live],output,progress,meter';
  for (const el of document.querySelectorAll(selectors)) {
    const labelledBy = (el.getAttribute('aria-labelledby') || '').trim().slice(0, 240);
    const labelText = labelledBy.split(/\s+/).filter(Boolean).map((id) => {
      const node = document.getElementById(id);
      return node ? (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim() : '';
    }).filter(Boolean).join(' ').slice(0, 500);
    out.push({
      tag: el.tagName.toLowerCase(),
      id: (el.id || '').slice(0, 120),
      role: (el.getAttribute('role') || (el.tagName.toLowerCase() === 'output' ? 'status' : '')).slice(0, 80),
      ariaLive: (el.getAttribute('aria-live') || '').slice(0, 80),
      ariaAtomic: (el.getAttribute('aria-atomic') || '').slice(0, 80),
      ariaRelevant: (el.getAttribute('aria-relevant') || '').slice(0, 120),
      ariaLabel: (el.getAttribute('aria-label') || '').slice(0, 500),
      labelledBy,
      labelText,
      text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 1000),
    });
    if (out.length >= 100) break;
  }
  return out;
})()`;

// In-page settle probe: how many loading placeholders are still up, plus a cheap SIGNATURE of the
// current interactive-element set. The bridge samples this twice ~250ms apart; when the page shows NO
// skeletons AND the signature is identical across two samples, the SPA has stopped painting and it's
// safe to snapshot. This is what stops us observing a half-rendered view (the RACE-CONDITION false
// positive: seeing a loading skeleton and reporting "the composer never renders" while textarea#cmsg
// is still ~0.6-1.2s from painting).
const SETTLE_PROBE = `(() => {
  const skeletons = document.querySelectorAll('.skel,.skeleton,[aria-busy="true"]').length;
  const SEL = 'a,button,input,select,textarea,[role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="checkbox"],[onclick],[contenteditable="true"],summary,label';
  const visible = (el) => {
    let r; try { r = el.getBoundingClientRect(); } catch (_) { return false; }
    if (r.width <= 0 || r.height <= 0) return false;
    if (r.bottom < 0 || r.right < 0 || r.top > window.innerHeight || r.left > window.innerWidth) return false;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const st = window.getComputedStyle(n);
      if (n.hidden || n.getAttribute('aria-hidden') === 'true' || st.display === 'none' ||
          st.visibility === 'hidden' || Number(st.opacity) <= 0.05) return false;
    }
    return true;
  };
  const sig = [];
  for (const el of document.querySelectorAll(SEL)) {
    if (!visible(el)) continue;
    sig.push(el.tagName + '#' + (el.id || '') + '.' + (el.getAttribute('type') || '') +
             '|' + (el.textContent || el.value || '').replace(/\\s+/g, ' ').trim().slice(0, 40));
    if (sig.length >= 300) break;
  }
  return { skeletons, count: sig.length, sig: sig.join('~~') };
})()`;

class Bridge {
  constructor() {
    this.browser = null;
    this.ctx = null;
    this.page = null;
    this.cdp = null;
    this.consoleErrors = [];
    this.requests = [];
    this.accessibilityPlatformEvents = [];
    this._lastAxLiveSignature = null;
  }

  async start() {
    if (!chromium) throw new Error('playwright not found — set NODE_PATH=' + nodePathHint());
    const actualAt = ACTUAL_AT_DRIVER === 'orca';
    this.browser = await chromium.launch({
      headless: !actualAt,
      args: ['--no-sandbox', ...(actualAt ? ['--force-renderer-accessibility'] : [])],
    });
    const ctxOpts = { viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, hasTouch: true };
    // A coverage ledger is only reusable when the browser state that made those assertions true is
    // restored with it.  Loading the Playwright state at context creation restores cookies and every
    // origin's localStorage before application code boots; a fresh empty context must never inherit an
    // old ledger and hallucinate that fixtures still exist.
    if (STORAGE_STATE_PATH) {
      const resolved = path.resolve(STORAGE_STATE_PATH);
      if (!fs.existsSync(resolved)) throw new Error('storage state not found: ' + resolved);
      ctxOpts.storageState = resolved;
    }
    if (VIDEO_DIR) {
      ensureDir(VIDEO_DIR);
      ctxOpts.recordVideo = { dir: VIDEO_DIR, size: { width: 1280, height: 800 } };
    }
    this.ctx = await this.browser.newContext(ctxOpts);
    // PERCEPTION RECORDER: a human notices the screen JUMP — a full page reload, or the SPA wiping the main
    // view to a skeleton (the 'page refresh' flash) — even when the SETTLED DOM ends up correct. The old QA
    // only saw the settled end-state, so it missed transient/perceptual defects (the reload-after-reply bug).
    // This runs before app code on every load and counts those events so the evaluator can judge them.
    await this.ctx.addInitScript(() => {
      try {
        window.__qa = window.__qa || { loads: 0, clobbers: 0, firstAt: Date.now(), a11yEvents: [] };
        window.__qa.a11yEvents = window.__qa.a11yEvents || [];
        window.__qa.loads++;                                     // full page load/reload counter
        const liveSelector = '[role="status"],[role="alert"],[aria-live],output';
        const liveRegion = (node) => {
          const el = node && (node.nodeType === 1 ? node : node.parentElement);
          if (!el) return null;
          if (el.matches && el.matches(liveSelector)) return el;
          const parent = el.closest ? el.closest(liveSelector) : null;
          return parent || (el.querySelector ? el.querySelector(liveSelector) : null);
        };
        const recordA11y = (node, mutationType) => {
          try {
            const el = liveRegion(node);
            if (!el) return;
            const labelledBy = (el.getAttribute('aria-labelledby') || '').trim().slice(0, 240);
            const labelText = labelledBy.split(/\s+/).filter(Boolean).map((id) => {
              const label = document.getElementById(id);
              return label ? (label.innerText || label.textContent || '').replace(/\s+/g, ' ').trim() : '';
            }).filter(Boolean).join(' ').slice(0, 500);
            const event = {
              ts: Date.now(), mutationType,
              tag: el.tagName.toLowerCase(), id: (el.id || '').slice(0, 120),
              role: (el.getAttribute('role') || (el.tagName.toLowerCase() === 'output' ? 'status' : '')).slice(0, 80),
              ariaLive: (el.getAttribute('aria-live') || '').slice(0, 80),
              ariaAtomic: (el.getAttribute('aria-atomic') || '').slice(0, 80),
              labelledBy, labelText,
              text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 1000),
            };
            const prior = window.__qa.a11yEvents[window.__qa.a11yEvents.length - 1];
            const same = prior && prior.id === event.id && prior.role === event.role && prior.text === event.text;
            if (!same) window.__qa.a11yEvents.push(event);
            if (window.__qa.a11yEvents.length > 100) window.__qa.a11yEvents.splice(0, window.__qa.a11yEvents.length - 100);
          } catch (_) {}
        };
        // Detect a skeleton node being ADDED (the 'refresh flash'), not queried after the fact — the skeleton
        // is transient and can be replaced before a state-query callback runs, so inspect the mutations.
        const mo = new MutationObserver((muts) => {
          for (const m of muts) {
            recordA11y(m.target, m.type);
            for (const n of (m.addedNodes || [])) {
              recordA11y(n, 'added');
              if (n.nodeType === 1 && (n.classList && n.classList.contains('skel') ||
                  (n.querySelector && n.querySelector('.skel')))) { window.__qa.clobbers++; return; }
            }
          }
        });
        mo.observe(document, { subtree: true, childList: true, characterData: true,
                               attributes: true, attributeFilter: ['role', 'aria-live', 'aria-atomic'] });
      } catch (_) {}
    });
    this.page = await this.ctx.newPage();
    this._wire(this.page);
    // The DOM mutation trace proves the live-region markup changed.  This CDP Accessibility-domain trace is
    // deliberately separate: it proves Chromium's accessibility platform tree observed the change, which is
    // the closest automatable ground truth to what assistive technology receives in a headless release gate.
    try {
      this.cdp = await this.ctx.newCDPSession(this.page);
      await this.cdp.send('Accessibility.enable');
      this.cdp.on('Accessibility.nodesUpdated', (event) => {
        this._recordAxNodes((event && event.nodes) || [], 'Accessibility.nodesUpdated');
      });
      this.cdp.on('Accessibility.loadComplete', (event) => {
        this._recordAxNodes(event && event.root ? [event.root] : [], 'Accessibility.loadComplete');
      });
    } catch (_) { this.cdp = null; }
  }

  _recordAxNodes(nodes, source) {
    for (const node of (nodes || [])) {
      const role = String(node && node.role && node.role.value || '').toLowerCase();
      const props = {};
      for (const item of (node && node.properties || [])) {
        if (item && item.name) props[item.name] = item.value && item.value.value;
      }
      if (!['status', 'alert', 'log', 'timer', 'marquee'].includes(role) && !props.live) continue;
      this.accessibilityPlatformEvents.push({
        ts: Date.now(), source, nodeId: node.nodeId, role,
        name: String(node && node.name && node.name.value || '').slice(0, 1000),
        value: String(node && node.value && node.value.value || '').slice(0, 1000),
        descendantText: String(node && node.descendantText || '').slice(0, 1000),
        live: props.live || '', atomic: props.atomic || false, relevant: props.relevant || '',
      });
    }
    if (this.accessibilityPlatformEvents.length > 100) {
      this.accessibilityPlatformEvents.splice(0, this.accessibilityPlatformEvents.length - 100);
    }
  }

  async _snapshotAxLiveRegions() {
    if (!this.cdp) return;
    try {
      const tree = await this.cdp.send('Accessibility.getFullAXTree');
      const byId = new Map((tree.nodes || []).map((node) => [node.nodeId, node]));
      const descendantText = (root) => {
        const values = [];
        const seen = new Set();
        const visit = (node, depth) => {
          if (!node || seen.has(node.nodeId) || depth > 8 || values.length >= 40) return;
          seen.add(node.nodeId);
          if (depth > 0) {
            const name = String(node.name && node.name.value || '').trim();
            const value = String(node.value && node.value.value || '').trim();
            if (name) values.push(name);
            if (value && value !== name) values.push(value);
          }
          for (const childId of (node.childIds || [])) visit(byId.get(childId), depth + 1);
        };
        visit(root, 0);
        return values.join(' ').replace(/\s+/g, ' ').trim().slice(0, 1000);
      };
      const live = [];
      for (const node of (tree.nodes || [])) {
        const role = String(node && node.role && node.role.value || '').toLowerCase();
        const props = {};
        for (const item of (node && node.properties || [])) {
          if (item && item.name) props[item.name] = item.value && item.value.value;
        }
        if (!['status', 'alert', 'log', 'timer', 'marquee'].includes(role) && !props.live) continue;
        live.push({nodeId: node.nodeId, role,
          name: String(node && node.name && node.name.value || '').slice(0, 1000),
          value: String(node && node.value && node.value.value || '').slice(0, 1000),
          descendantText: descendantText(node),
          live: props.live || '', atomic: props.atomic || false, relevant: props.relevant || ''});
      }
      live.sort((a, b) => String(a.nodeId).localeCompare(String(b.nodeId)));
      const signature = JSON.stringify(live);
      if (this._lastAxLiveSignature !== null && signature !== this._lastAxLiveSignature) {
        for (const node of live) this._recordAxNodes([{
          nodeId: node.nodeId, role: {value: node.role}, name: {value: node.name}, value: {value: node.value},
          descendantText: node.descendantText,
          properties: [{name: 'live', value: {value: node.live}},
                       {name: 'atomic', value: {value: node.atomic}},
                       {name: 'relevant', value: {value: node.relevant}}],
        }], 'Accessibility.getFullAXTree:changed');
      }
      this._lastAxLiveSignature = signature;
    } catch (_) {}
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
      if (isExternalHandoffUrl(req.url())) return;
      let status = null;
      try { const r = await req.response(); status = r ? r.status() : null; } catch (_) {}
      this.requests.push({ ts: Date.now() / 1000, method: req.method(),
                           url: String(req.url()).slice(0, 300), status });
      if (this.requests.length > MAX_REQUESTS) this.requests.shift();
    });
    page.on('requestfailed', (req) => {
      if (isExternalHandoffUrl(req.url())) return;
      this.requests.push({
        ts: Date.now() / 1000, method: req.method(), url: String(req.url()).slice(0, 300),
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
    let beforeUrl = null;
    try { beforeUrl = this.page.url(); } catch (_) {}
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
    try {
      if (beforeUrl && this.page.url() === beforeUrl) {
        await this.page.evaluate(() => window.scrollTo(0, 0));
      }
    } catch (_) {}
    let title = null;
    try { title = await this.page.title(); } catch (_) {}
    return { url: this.page.url(), title, status };
  }

  clearEvidence() {
    // A QA report must prove that a scoped capture was explicitly reset; observing an empty array is not a
    // clear operation. Return the previous counts and the exact new boundary so the Python evidence ledger can
    // distinguish a real clear from an inference made after the run.
    const receipt = {
      cleared_at: Date.now() / 1000,
      console_before: this.consoleErrors.length,
      requests_before: this.requests.length,
    };
    this.consoleErrors = [];
    this.requests = [];
    return receipt;
  }

  async armPointerEvidence() {
    await this.page.evaluate(() => {
      window.__aosPointerEvidence = [];
      if (window.__aosPointerEvidenceInstalled) return;
      window.__aosPointerEvidenceInstalled = true;
      const labelOf = (el) => String((el && (el.innerText || el.value || el.getAttribute('aria-label') ||
        el.getAttribute('name'))) || '').replace(/\s+/g, ' ').trim().slice(0, 160);
      for (const type of ['pointerdown', 'pointerup', 'click']) {
        document.addEventListener(type, (event) => {
          const target = event.target && event.target.closest ?
            event.target.closest('a,button,input,select,textarea,[role],summary,label') : event.target;
          window.__aosPointerEvidence.push({
            ts: Date.now() / 1000, type, isTrusted: Boolean(event.isTrusted),
            pointerType: event.pointerType || (type === 'click' ? 'mouse-compatible-click' : ''),
            button: Number.isInteger(event.button) ? event.button : null,
            clientX: Number.isFinite(event.clientX) ? event.clientX : null,
            clientY: Number.isFinite(event.clientY) ? event.clientY : null,
            target: labelOf(target), tag: String((target && target.tagName) || '').toLowerCase(),
          });
          if (window.__aosPointerEvidence.length > 200) window.__aosPointerEvidence.shift();
        }, true);
      }
    });
  }

  async pointerEvidence() {
    return await this.page.evaluate(() => (window.__aosPointerEvidence || []).slice(-200));
  }

  async armKeyboardEvidence() {
    await this.page.evaluate(() => {
      window.__aosKeyboardEvidence = [];
      if (window.__aosKeyboardEvidenceInstalled) return;
      window.__aosKeyboardEvidenceInstalled = true;
      const labelOf = (el) => String((el && (el.innerText || el.value || el.getAttribute('aria-label') ||
        el.getAttribute('name'))) || '').replace(/\s+/g, ' ').trim().slice(0, 160);
      // Include the trusted click synthesized by the browser for keyboard activation.  Key events alone
      // prove that Space/Enter reached the control, but they cannot prove how many activations the browser
      // dispatched (especially for a held key with repeat=true).  Keeping the click in the same freshly
      // cleared action receipt makes the visible count delta mechanically attributable to this key gesture.
      for (const type of ['keydown', 'keyup', 'click']) {
        document.addEventListener(type, (event) => {
          const target = event.target;
          window.__aosKeyboardEvidence.push({
            ts: Date.now() / 1000, type, isTrusted: Boolean(event.isTrusted), key: event.key,
            code: event.code, repeat: Boolean(event.repeat), detail: event.detail,
            pointerType: event.pointerType, target: labelOf(target),
            tag: String((target && target.tagName) || '').toLowerCase(),
          });
          if (window.__aosKeyboardEvidence.length > 200) window.__aosKeyboardEvidence.shift();
        }, true);
      }
    });
  }

  async keyboardEvidence() {
    return await this.page.evaluate(() => (window.__aosKeyboardEvidence || []).slice(-200));
  }

  async pointer(msg) {
    const sel = this._selectorFor(msg);
    if (!sel) throw new Error('pointer requires selector or idx');
    const pointerType = String(msg.pointer_type || msg.pointerType || '').toLowerCase();
    if (!['touch', 'pen'].includes(pointerType)) throw new Error('pointer_type must be touch or pen');
    if (!this.cdp) throw new Error('Chromium input session unavailable');
    const loc = this.page.locator(sel).first();
    await loc.scrollIntoViewIfNeeded({ timeout: CMD_TIMEOUT_MS - 500 });
    const box = await loc.boundingBox();
    if (!box) throw new Error('pointer target has no visible bounding box');
    const x = box.x + box.width / 2, y = box.y + box.height / 2;
    await this.armPointerEvidence();
    if (pointerType === 'touch') {
      await this.cdp.send('Input.dispatchTouchEvent', {
        type: 'touchStart', touchPoints: [{ x, y, radiusX: 1, radiusY: 1, force: 1, id: 1 }],
      });
      await this.page.waitForTimeout(30);
      await this.cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    } else {
      await this.cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, pointerType: 'pen' });
      await this.cdp.send('Input.dispatchMouseEvent', {
        type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1, pointerType: 'pen', force: 1,
      });
      await this.page.waitForTimeout(30);
      await this.cdp.send('Input.dispatchMouseEvent', {
        type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1, pointerType: 'pen', force: 0,
      });
    }
    return { clicked: sel, pointerType,
      pointerEvidence: await this.pointerEvidence().catch(() => []) };
  }

  async reload() {
    const from = this.page.url();
    let status = null;
    const response = await this.page.reload({ waitUntil: 'domcontentloaded', timeout: CMD_TIMEOUT_MS - 200 });
    status = response ? response.status() : null;
    await this.settle();
    return { from, url: this.page.url(), status, reloaded: true };
  }

  // Wait for the SPA to STOP PAINTING before we observe it. Two bounded gates, so we return only the
  // settled DOM (never a mid-render skeleton) yet can never hang:
  //   1. networkidle — best-effort; many SPAs hold a socket open, so a timeout here is expected & fine.
  //   2. poll-until-stable — loop until there are ZERO loading skeletons (.skel/.skeleton/[aria-busy])
  //      AND the interactive-element signature is identical across two consecutive ~250ms samples.
  // The whole thing is capped at SETTLE_MAX_MS so a perpetually-busy page still yields *something*.
  // `extraMs` gives a late-painting control a bit more grace (used by the explicit `settle` command).
  async settle(extraMs) {
    try {
      await this.page.waitForLoadState('networkidle', { timeout: SETTLE_NETIDLE_MS });
    } catch (_) { /* chatty SPA / already idle — the poll below is the real gate */ }
    if (extraMs && extraMs > 0) {
      try { await this.page.waitForTimeout(Math.min(extraMs, 3000)); } catch (_) {}
    }
    const deadline = Date.now() + SETTLE_MAX_MS;
    let prevSig = null;
    while (Date.now() < deadline) {
      let probe;
      try { probe = await this.page.evaluate(SETTLE_PROBE); }
      catch (_) { break; } // page navigated/closed mid-probe — stop; caller snapshots what's there
      // settled == no skeletons AND element set unchanged since the previous sample.
      if (probe.skeletons === 0 && prevSig !== null && probe.sig === prevSig) return;
      prevSig = probe.sig;
      try { await this.page.waitForTimeout(SETTLE_SAMPLE_MS); } catch (_) { break; }
    }
  }

  // Explicit settle command: give the page extra paint grace, then settle. The Python loop calls this
  // before RE-OBSERVING when an expected control is missing, so a late-painting control gets one more
  // chance to appear before the evaluator judges it.
  async settleCmd(msg) {
    const extra = (msg && msg.ms !== undefined && msg.ms !== null) ? Number(msg.ms) : SETTLE_REOBSERVE_MS;
    await this.settle(extra);
    return { settled: true, waited_ms: Math.min(Math.max(extra || 0, 0), 3000) };
  }

  async waitCmd(msg) {
    const durationMs = Math.min(ASYNC_WAIT_MAX_MS,
      Math.max(0, Number((msg && msg.timeout_ms) || 0)));
    const started = Date.now();
    if (durationMs > 0) await this.page.waitForTimeout(durationMs);
    return { waited: true, elapsed_ms: Date.now() - started,
             requested_ms: durationMs };
  }

  // Wait mechanically for an explicitly named external/app condition.  This is intentionally NOT an LLM
  // loop: chat replies, sign-in callbacks and queued agent work may take real time, and repeatedly paying a
  // model to rediscover "still waiting" is both slower and less reliable than polling the browser fact.
  async waitFor(msg) {
    const aliases = { response: 'text', live: 'status', control_visible: 'control' };
    const requestedKind = String((msg && msg.kind) || 'text').trim().toLowerCase();
    const kind = aliases[requestedKind] || requestedKind;
    const value = String((msg && msg.value) || '').trim();
    const timeoutMs = Math.min(ASYNC_WAIT_MAX_MS,
      Math.max(0, Number((msg && msg.timeout_ms) || 60000)));
    const pollMs = Math.min(1000, Math.max(100, Number((msg && msg.poll_ms) || 400)));
    const started = Date.now();
    if (!['control', 'text', 'status', 'url', 'network_idle'].includes(kind)) {
      throw new Error('waitFor kind must be control, text, status, url, or network_idle');
    }
    if (kind !== 'network_idle' && !value) throw new Error('waitFor requires a non-empty value');
    if (kind === 'network_idle') {
      try {
        await this.page.waitForLoadState('networkidle', { timeout: timeoutMs });
        return { waited: true, matched: true, kind, value, elapsed_ms: Date.now() - started };
      } catch (_) {
        return { waited: true, matched: false, timed_out: true, kind, value,
                 elapsed_ms: Date.now() - started };
      }
    }
    let lastObserved = '';
    while (Date.now() - started <= timeoutMs) {
      const probe = await this.page.evaluate(({ kind, value }) => {
        const norm = (input) => String(input || '').replace(/\s+/g, ' ').trim();
        const wanted = norm(value).toLowerCase();
        const visible = (el) => {
          let rect; try { rect = el.getBoundingClientRect(); } catch (_) { return false; }
          if (rect.width <= 0 || rect.height <= 0) return false;
          const style = window.getComputedStyle(el);
          return !el.hidden && el.getAttribute('aria-hidden') !== 'true' &&
            style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) > 0.05;
        };
        let observed = '';
        if (kind === 'url') observed = location.href;
        else if (kind === 'status') observed = [...document.querySelectorAll(
          '[role="status"],[role="alert"],[aria-live],output')].filter(visible)
          .map((el) => norm(el.textContent)).filter(Boolean).join(' | ');
        else if (kind === 'control') observed = [...document.querySelectorAll(
          'a,button,input,select,textarea,[role="button"],[role="link"],[role="tab"],'
          + '[role="menuitem"],[role="checkbox"],[contenteditable="true"],summary,label')]
          .filter(visible).map((el) => norm(el.innerText || el.value || el.getAttribute('aria-label') ||
            el.getAttribute('placeholder') || el.getAttribute('name') || el.getAttribute('title')))
          .filter(Boolean).join(' | ');
        else observed = norm(document.body && document.body.innerText);
        const normalizedObserved = norm(observed).toLowerCase();
        // Status/live regions often expose structured state (for example
        // `{"agentJobsFailed": 1}`) while the planner names the semantic key as
        // `agentJobsFailed`.  Punctuation and pretty-print whitespace must not
        // turn that already-observed fact into a full timeout.  Keep the normal
        // human-text match first, then allow a conservative alphanumeric match
        // for non-trivial identifiers/phrases.
        const compact = (input) => String(input || '').replace(/[^a-z0-9]+/g, '');
        const compactWanted = compact(wanted);
        // Human-facing live copy and planner-generated state names often express the same transition with
        // harmless morphology (for example `follow_up_approval_approved` versus `Follow-up approved`).  Requiring
        // literal containment made the browser sit through the entire external timeout after the condition had
        // visibly arrived.  For status regions only, compare at least two distinct, meaningful six-character
        // word stems.  This stays substantially stricter than a fuzzy edit-distance match and cannot turn a
        // one-word state such as `ready` into an accidental success.
        const semanticStems = (input) => [...new Set(
          (String(input || '').toLowerCase().match(/[a-z0-9]+/g) || [])
            .filter((token) => token.length >= 3)
            .map((token) => token.slice(0, 6))
        )];
        const wantedStems = semanticStems(wanted);
        const observedStems = new Set(semanticStems(normalizedObserved));
        const semanticStatus = kind === 'status' && wantedStems.length >= 2 &&
          wantedStems.every((stem) => observedStems.has(stem));
        // A bare structured-state key means "that fact became truthy", not merely "the object has this
        // schema field". Otherwise waiting for agentJobsFailed would match agentJobsFailed: 0 immediately.
        // When the key has a positive/non-empty value, punctuation/pretty-printing no longer causes a timeout.
        let structured = null;
        if (/^[a-z][a-z0-9_.-]{3,}$/i.test(value)) {
          const escaped = wanted.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
          const keyMatch = normalizedObserved.match(new RegExp(
            '["\\\']?' + escaped + '["\\\']?\\s*[:=]\\s*("[^"]*"|\\\'[^\\\']*\\\'|-?\\d+(?:\\.\\d+)?|true|false|null|\\[\\s*\\]|\\{\\s*\\})', 'i'));
          if (keyMatch) {
            const rawValue = String(keyMatch[1] || '').trim().toLowerCase();
            const unquoted = rawValue.replace(/^['"]|['"]$/g, '').trim();
            structured = !['', '0', '0.0', 'false', 'null', 'none', '[]', '{}'].includes(unquoted);
          }
        }
        // Planners sometimes name a semantic transition ("confirmation") while a well-written customer UI
        // uses human copy rather than that implementation noun. Keep this alias deliberately narrow: it only
        // applies to a text wait for the exact generic value and requires strong confirmation-language copy.
        const semanticAlias = kind === 'text' && wanted === 'confirmation' &&
          /\b(?:thanks|received|submitted|success(?:ful|fully)?|we have your)\b/i.test(normalizedObserved);
        const matched = structured !== null ? structured : (semanticAlias || semanticStatus ||
          normalizedObserved.includes(wanted) ||
          (compactWanted.length >= 4 && compact(normalizedObserved).includes(compactWanted)));
        return { matched, observed: norm(observed).slice(0, 1200) };
      }, { kind, value }).catch(() => ({ matched: false, observed: '' }));
      lastObserved = probe.observed || '';
      if (probe.matched) {
        return { waited: true, matched: true, kind, value, observed: lastObserved,
                 elapsed_ms: Date.now() - started };
      }
      if (Date.now() - started >= timeoutMs) break;
      await this.page.waitForTimeout(Math.min(pollMs, Math.max(0, timeoutMs - (Date.now() - started))));
    }
    return { waited: true, matched: false, timed_out: true, kind, value,
             observed: lastObserved, elapsed_ms: Date.now() - started };
  }

  async state(msg) {
    await this.settle();   // observe ONLY the settled DOM — never snapshot a half-painted view
    const includeAccessibility = !msg || msg.include_accessibility !== false;
    // PARK THE MOUSE before observing: after a click the cursor rests on the clicked control, so its
    // :hover style bleeds into the state screenshot and the AI evaluator misreads hover as "selected"
    // (a real false-positive bug class: three QA rounds flagged a tip-preset as 'still active' when the
    // pixels showed only the hover treatment). State snapshots must capture STATE, not cursor incident.
    try { await this.page.mouse.move(0, 0); await this.page.waitForTimeout(120); } catch (_) {}
    ensureDir(SHOT_DIR);
    const file = path.join(SHOT_DIR, 'state-' + Date.now() + '-' + Math.floor(Math.random() * 1e4) + '.png');
    let shotOk = false;
    try { await this.page.screenshot({ path: file, fullPage: false }); shotOk = fs.existsSync(file); } catch (_) {}
    let elements = [];
    try { elements = await this.page.evaluate(COLLECT_ELEMENTS); } catch (_) { elements = []; }
    let activeElement = null;
    try { activeElement = await this.page.evaluate(ACTIVE_ELEMENT); } catch (_) { activeElement = null; }
    let accessibilityRegions = [];
    if (includeAccessibility) {
      try { accessibilityRegions = await this.page.evaluate(ACCESSIBILITY_REGIONS); } catch (_) { accessibilityRegions = []; }
    }
    let accessibilityTree = '';
    if (includeAccessibility) {
      try {
        const snapshot = await this.page.locator('body').ariaSnapshot({ timeout: Math.min(3000, CMD_TIMEOUT_MS - 200) });
        accessibilityTree = String(snapshot || '').slice(0, 12000);
      } catch (_) { accessibilityTree = ''; }
    }
    let accessibilityEvents = [];
    if (includeAccessibility) {
      try {
        accessibilityEvents = await this.page.evaluate(() =>
          window.__qa && Array.isArray(window.__qa.a11yEvents) ? window.__qa.a11yEvents.slice(-100) : []);
      } catch (_) { accessibilityEvents = []; }
      await this._snapshotAxLiveRegions();
    }
    let url = null, title = null, viewport = null;
    try { url = this.page.url(); } catch (_) {}
    try { title = await this.page.title(); } catch (_) {}
    try { viewport = this.page.viewportSize(); } catch (_) {}
    let perception = null;
    try { perception = await this.page.evaluate(() => window.__qa ? { loads: window.__qa.loads, clobbers: window.__qa.clobbers, firstAt: window.__qa.firstAt } : null); } catch (_) {}
    let renderedText = {
      bodyText: '', viewportText: '', statusText: '', scrollPosition: null, documentLandmarks: [],
      horizontalOverflow: false,
    };
    try {
      renderedText = await this.page.evaluate(() => {
        const norm = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const visibleText = [];
        const body = document.body;
        if (body) {
          const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
          for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            const text = norm(node.nodeValue);
            const parent = node.parentElement;
            if (!text || !parent) continue;
            let hidden = false;
            for (let el = parent; el && el.nodeType === 1; el = el.parentElement) {
              const style = getComputedStyle(el);
              if (el.hidden || el.getAttribute('aria-hidden') === 'true' || style.display === 'none' ||
                  style.visibility === 'hidden' || Number(style.opacity) <= 0.05) {
                hidden = true; break;
              }
            }
            if (hidden) continue;
            let rects = [];
            try {
              const range = document.createRange();
              range.selectNodeContents(node);
              rects = Array.from(range.getClientRects());
            } catch (_) {}
            if (!rects.some((rect) => rect.width > 0 && rect.height > 0 && rect.bottom >= 0 &&
                rect.right >= 0 && rect.top <= innerHeight && rect.left <= innerWidth)) continue;
            visibleText.push(text);
            if (visibleText.join(' ').length >= 5000) break;
          }
        }
        const statusRegions = [...document.querySelectorAll(
          '[role=status],[role=alert],[aria-live],[data-testid*=status]')]
          .map((el) => norm(el.textContent)).filter(Boolean);
        // Human-sized status messages come first. Large machine-readable audit/queue regions contribute
        // their newest tail rather than their oldest prefix, so an action judge sees the blocker/decision
        // created by THIS click instead of repeatedly attributing an earlier audit row to the new action.
        const shortStatuses = statusRegions.filter((value) => value.length <= 320);
        const longStatusTails = statusRegions.filter((value) => value.length > 320)
          .map((value) => `…latest: ${value.slice(-700)}`);
        const statusText = [...shortStatuses, ...longStatusTails].join(' | ').slice(0, 2400);
        const landmarkNodes = [...document.querySelectorAll(
          'h1,h2,h3,[role=main],[role=navigation],[role=region],[role=complementary],'
          + '[role=alert],[role=status],section[aria-label]')];
        const documentLandmarks = [];
        const seenLandmarks = new Set();
        for (const el of landmarkNodes) {
          const style = getComputedStyle(el);
          if (el.hidden || el.getAttribute('aria-hidden') === 'true' || style.display === 'none' ||
              style.visibility === 'hidden' || Number(style.opacity) <= 0.05) continue;
          const heading = el.matches('h1,h2,h3') ? el : el.querySelector(':scope > h1,:scope > h2,:scope > h3');
          const label = norm(el.getAttribute('aria-label') || (heading && heading.textContent) ||
            (el.matches('h1,h2,h3,[role=alert],[role=status]') ? el.textContent : ''));
          if (!label || label.length > 180) continue;
          const y = Math.max(0, Math.round(el.getBoundingClientRect().top + scrollY));
          const role = el.getAttribute('role') || el.tagName.toLowerCase();
          const key = `${label.toLowerCase()}|${y}`;
          if (seenLandmarks.has(key)) continue;
          seenLandmarks.add(key);
          documentLandmarks.push({ label, role, y });
          if (documentLandmarks.length >= 40) break;
        }
        return {
          bodyText: norm(body && body.innerText).slice(0, 4000),
          viewportText: norm(visibleText.join(' ')).slice(0, 4000),
          statusText,
          scrollPosition: { x: Math.round(scrollX), y: Math.round(scrollY),
                            documentHeight: Math.round(document.documentElement.scrollHeight) },
          horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
          documentLandmarks,
        };
      });
    } catch (_) {}
    return {
      url,
      title,
      screenshot: shotOk ? file : null,
      elements,
      activeElement,
      accessibilityRegions,
      accessibilityTree,
      accessibilityEvents,
      accessibilityPlatformEvents: includeAccessibility ? this.accessibilityPlatformEvents.slice(-100) : [],
      fullAccessibilityCaptured: includeAccessibility,
      viewport,
      settled: true,   // this snapshot was taken AFTER settle() — the DOM was done painting
      console_errors: this.consoleErrors.slice(-MAX_CONSOLE),
      recent_requests: this.requests.slice(-MAX_REQUESTS),
      perception,      // {loads, clobbers}: full reloads + view re-render/skeleton flashes seen so far (perceptual layer)
      ...renderedText, // current-viewport text prevents long-page scrolls from re-sending only the document prefix
    };
  }

  async click(msg) {
    const sel = this._selectorFor(msg);
    if (!sel) throw new Error('click requires selector or idx');
    const loc = this.page.locator(sel).first();
    const emptyRequiredFieldsBefore = await this._emptyRequiredFieldsBefore(sel);
    const disabledBeforeClick = await loc.evaluate((el) =>
      Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'));
    if (disabledBeforeClick) {
      return { clicked: false, disabledBeforeClick: true, disabledAfterClick: true,
               note: 'control is disabled; no activation attempted', pointerEvidence: [],
               emptyRequiredFieldsBefore };
    }
    await this.armPointerEvidence();
    // Playwright's selector click keeps an actionability/retry lifecycle open after dispatch. If a normal SPA
    // handler synchronously re-renders and detaches the button, the trusted click has already changed product
    // state but Playwright may retry the detached locator until the whole command times out. QA then labels a
    // successful denial/save/submit as a driver failure and repeats it. Resolve visible geometry first, then
    // dispatch one trusted mouse gesture. The pointer receipt below and ordinary before/after effect oracle
    // still catch an overlay/race/mis-target; no successful side effect can be retried merely because its
    // source node disappeared.
    await loc.scrollIntoViewIfNeeded({ timeout: CMD_TIMEOUT_MS - 1000 });
    const retryAfterNoEffect = msg && msg._qa_retry_after_no_effect === true;
    if (retryAfterNoEffect) {
      // The first trusted coordinate click reached no DOM target and changed no observable state. Repeating
      // the same coordinates merely reproduces an overlay/layout miss. Bind the one allowed retry to the
      // still-visible element instead. Playwright dispatches trusted pointer/mouse events here; `force` only
      // bypasses a stale actionability heuristic after the first no-effect receipt, and no successful
      // business action can enter this branch because Python compares the settled before/after states first.
      await loc.click({ timeout: CMD_TIMEOUT_MS - 200, noWaitAfter: true, force: true });
    } else {
      const box = await loc.boundingBox();
      if (!box) throw new Error('click target has no visible bounding box');
      await this.page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    }
    const pointerEvidence = await this.pointerEvidence().catch(() => []);
    let disabledAfterClick = null;
    try {
      // A locator.evaluate waits for a replacement selector to reappear. That is exactly the wrong semantic
      // after a successful handler removes/re-renders its source control, and was the final 8-second false
      // timeout in this path. querySelector is an immediate observation; rich non-CSS selectors simply return
      // unknown here and the following full state snapshot remains authoritative.
      disabledAfterClick = await this.page.evaluate((selector) => {
        try {
          const el = document.querySelector(selector);
          return el ? Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true') : null;
        } catch (_) { return null; }
      }, sel);
    } catch (_) {}
    return { clicked: sel, disabledBeforeClick: false, disabledAfterClick, pointerEvidence,
             activationMode: retryAfterNoEffect ? 'element-bound-no-effect-retry' : 'coordinate',
             emptyRequiredFieldsBefore };
  }

  async _emptyRequiredFieldsBefore(selector) {
    // Capture form readiness at the action boundary, before browser validation or app code changes the DOM.
    // This is driver-owned ground truth: if a tester skips required setup and clicks Submit, the judge can
    // immediately name the missing prerequisites instead of opening a false product finding.
    try {
      return await this.page.locator(selector).first().evaluate((el) => {
        const form = el.form || el.closest('form');
        if (!form) return [];
        const activatingTag = el.tagName.toLowerCase();
        const activatingType = (el.getAttribute('type') || '').toLowerCase();
        const submitsForm = (activatingTag === 'button' && !['button', 'reset'].includes(activatingType)) ||
          (activatingTag === 'input' && ['submit', 'image'].includes(activatingType));
        if (!submitsForm) return [];
        const norm = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const controls = Array.from(form.elements || []);
        const labelOf = (control) => {
          const associated = Array.from(control.labels || [])
            .map((label) => label.innerText || label.textContent || '').join(' ');
          return norm(associated || control.getAttribute('aria-label') ||
            control.getAttribute('placeholder') || control.getAttribute('name') ||
            control.getAttribute('id') || control.tagName.toLowerCase());
        };
        const missing = [];
        const seenRadioGroups = new Set();
        for (const control of controls) {
          if (!control || control.disabled || !control.required) continue;
          const tag = control.tagName.toLowerCase();
          const type = (control.getAttribute('type') || '').toLowerCase();
          if (type === 'hidden' || type === 'button' || type === 'submit' || type === 'reset') continue;
          let empty = false;
          if (type === 'radio') {
            const group = control.name || control.id || labelOf(control);
            if (seenRadioGroups.has(group)) continue;
            seenRadioGroups.add(group);
            empty = !controls.some((candidate) => candidate && candidate.type === 'radio' &&
              (candidate.name || candidate.id || labelOf(candidate)) === group && candidate.checked);
          } else if (type === 'checkbox') {
            empty = !control.checked;
          } else if (type === 'file') {
            empty = !(control.files && control.files.length);
          } else {
            empty = String(control.value || '') === '';
          }
          if (empty) missing.push({
            label: labelOf(control), name: control.name || '', id: control.id || '', tag, type,
          });
          if (missing.length >= 20) break;
        }
        return missing;
      });
    } catch (_) {
      return [];
    }
  }

  async clickBurst(msg) {
    const sel = this._selectorFor(msg);
    if (!sel) throw new Error('clickBurst requires selector or idx');
    const count = Number(msg.count === undefined ? 5 : msg.count);
    const intervalMs = Number(msg.interval_ms === undefined ? 25 : msg.interval_ms);
    if (!Number.isInteger(count) || count < 2 || count > 20) {
      throw new Error('clickBurst count must be an integer from 2 to 20');
    }
    if (!Number.isInteger(intervalMs) || intervalMs < 0 || intervalMs > 250) {
      throw new Error('clickBurst interval_ms must be an integer from 0 to 250');
    }
    const timestamps = [];
    const intermediateSamples = [];
    const started = Date.now();
    await this.armPointerEvidence();
    for (let i = 0; i < count; i++) {
      const loc = this.page.locator(sel).first();
      await loc.scrollIntoViewIfNeeded({ timeout: CMD_TIMEOUT_MS - 1000 });
      const box = await loc.boundingBox();
      if (!box) throw new Error('clickBurst target has no visible bounding box');
      await this.page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      const ts = Date.now();
      timestamps.push(ts);
      // Capture the observable result of EACH activation. A 0->3 terminal snapshot plus three timestamps
      // does not prove 0->1->2->3 or exactly-once behavior; the independent jury correctly rejected that.
      // DOM event handlers update synchronously, so this probe preserves burst timing without a settle wait.
      const sample = await this.page.evaluate(() => {
        const norm = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const regions = [...document.querySelectorAll(
          '[role="status"],[role="alert"],[aria-live],output')].slice(0, 8).map((el) => ({
            role: el.getAttribute('role') || (el.tagName.toLowerCase() === 'output' ? 'status' : ''),
            ariaLive: el.getAttribute('aria-live') || '',
            text: norm(el.textContent),
          }));
        return { bodyText: norm(document.body && document.body.innerText).slice(0, 500), regions };
      }).catch(() => ({ bodyText: '', regions: [] }));
      intermediateSamples.push({ index: i + 1, ts, ...sample });
      if (intervalMs && i + 1 < count) await this.page.waitForTimeout(intervalMs);
    }
    const pointerEvidence = await this.pointerEvidence().catch(() => []);
    return { clicked: sel, burst: true, count, interval_ms: intervalMs,
             timestamps, intermediateSamples, elapsed_ms: Date.now() - started, pointerEvidence };
  }

  // Prove a transient submit/busy boundary as one browser-owned operation. A normal `state` observation waits
  // for aria-busy/skeletons to settle, which is exactly wrong when the contract asks QA to prove that the busy
  // state remains present for N seconds. This command installs a high-resolution MutationObserver before the
  // trusted click, makes real pointer + keyboard duplicate attempts while the control is disabled, and records
  // the exact pending-to-complete transition without asking a model to schedule a later dwell after completion.
  async timedTransition(msg) {
    const sel = this._selectorFor(msg);
    if (!sel) throw new Error('timedTransition requires selector or idx');
    const durationMs = Math.min(ASYNC_WAIT_MAX_MS,
      Math.max(250, Number(msg.duration_ms === undefined ? 10000 : msg.duration_ms)));
    const completionGraceMs = Math.min(30000,
      Math.max(0, Number(msg.completion_grace_ms === undefined ? 5000 : msg.completion_grace_ms)));
    const pendingText = String(msg.pending_text || '').trim();
    const loc = this.page.locator(sel).first();
    const emptyRequiredFieldsBefore = await this._emptyRequiredFieldsBefore(sel);
    const disabledBeforeClick = await loc.evaluate((el) =>
      Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'));
    if (disabledBeforeClick) {
      return { timedTransition: true, clicked: false, disabledBeforeClick: true,
        disabledAfterClick: true, emptyRequiredFieldsBefore,
        transition: { required_duration_ms: durationMs, pending_seen: false,
          error: 'control was disabled before the triggering action' } };
    }
    const box = await loc.boundingBox().catch(() => null);
    await this.armPointerEvidence();
    await this.armKeyboardEvidence();
    await this.page.evaluate(({ selector, pendingText }) => {
      const previous = window.__aosTimedTransition;
      try { if (previous && previous.observer) previous.observer.disconnect(); } catch (_) {}
      const norm = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const receipt = {
        selector, pendingText: norm(pendingText).toLowerCase(),
        installedAt: performance.now(), clickAt: null, pendingSeenAt: null,
        completionAt: null, submitEvents: 0, mutationCount: 0,
      };
      const pendingNow = () => {
        const control = document.querySelector(selector);
        const form = control && (control.form || control.closest('form'));
        const label = norm(control && (control.innerText || control.value ||
          control.getAttribute('aria-label') || control.getAttribute('name')));
        const disabled = Boolean(control &&
          (control.disabled || control.getAttribute('aria-disabled') === 'true'));
        const ariaBusy = Boolean(form && form.getAttribute('aria-busy') === 'true');
        const textMatches = !receipt.pendingText || label.toLowerCase().includes(receipt.pendingText);
        return Boolean(control && disabled && (ariaBusy || textMatches));
      };
      const update = () => {
        const now = performance.now();
        const pending = pendingNow();
        if (pending && receipt.pendingSeenAt === null) receipt.pendingSeenAt = now;
        if (!pending && receipt.pendingSeenAt !== null && receipt.completionAt === null) {
          receipt.completionAt = now;
        }
      };
      const control = document.querySelector(selector);
      const onClick = (event) => {
        if (receipt.clickAt === null && (event.target === control || control?.contains(event.target))) {
          receipt.clickAt = performance.now();
          queueMicrotask(update);
        }
      };
      const onSubmit = () => { receipt.submitEvents += 1; queueMicrotask(update); };
      control?.addEventListener('click', onClick, { capture: true });
      document.addEventListener('submit', onSubmit, { capture: true });
      const observer = new MutationObserver(() => {
        receipt.mutationCount += 1;
        update();
      });
      observer.observe(document.documentElement, {
        subtree: true, childList: true, attributes: true, characterData: true,
        attributeFilter: ['disabled', 'aria-disabled', 'aria-busy'],
      });
      window.__aosTimedTransition = { receipt, observer, onClick, onSubmit, control, update, pendingNow };
    }, { selector: sel, pendingText });

    await this.page.click(sel, { timeout: CMD_TIMEOUT_MS - 200, noWaitAfter: true });
    const started = Date.now();
    const sample = async (name) => await this.page.evaluate(({ selector, name }) => {
      const norm = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const transition = window.__aosTimedTransition;
      try { transition?.update?.(); } catch (_) {}
      const control = document.querySelector(selector);
      const form = control && (control.form || control.closest('form'));
      const regions = [...document.querySelectorAll('[role="status"],[role="alert"],[aria-live],output')]
        .map((el) => norm(el.textContent)).filter(Boolean).join(' | ');
      return {
        name,
        at: performance.now(),
        pending: Boolean(transition?.pendingNow?.()),
        control_exists: Boolean(control),
        control_label: norm(control && (control.innerText || control.value ||
          control.getAttribute('aria-label') || control.getAttribute('name'))).slice(0, 160),
        control_disabled: Boolean(control &&
          (control.disabled || control.getAttribute('aria-disabled') === 'true')),
        form_aria_busy: Boolean(form && form.getAttribute('aria-busy') === 'true'),
        status_text: regions.slice(0, 900),
        body_text: norm(document.body && document.body.innerText).slice(0, 1400),
      };
    }, { selector: sel, name }).catch(() => ({ name, pending: false, probe_error: true }));

    const samples = [await sample('immediate')];
    // Reproduce two real human duplicate attempts while the synchronous submit handler has disabled the
    // control. Raw mouse coordinates avoid Playwright's enabled-state wait; Enter is sent through the browser
    // keyboard. The final transition receipt proves these attempts did not create a second completion.
    await this.page.waitForTimeout(50);
    if (box) {
      await this.page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    }
    await this.page.keyboard.press('Enter');
    samples.push(await sample('after-duplicate-attempts'));

    let nextMilestone = Math.max(250, Math.floor(durationMs / 4));
    while (Date.now() - started < durationMs) {
      const elapsed = Date.now() - started;
      await this.page.waitForTimeout(Math.min(100, Math.max(1, durationMs - elapsed)));
      const nowElapsed = Date.now() - started;
      if (nowElapsed >= nextMilestone || nowElapsed >= durationMs) {
        samples.push(await sample(nowElapsed >= durationMs ? 'required-boundary' : 'pending-milestone'));
        nextMilestone += Math.max(250, Math.floor(durationMs / 4));
      }
    }
    let boundary = samples[samples.length - 1];
    if (boundary.name !== 'required-boundary') {
      boundary = await sample('required-boundary');
      samples.push(boundary);
    }
    const graceStarted = Date.now();
    let finalSample = boundary;
    while (finalSample.pending && Date.now() - graceStarted < completionGraceMs) {
      await this.page.waitForTimeout(Math.min(100,
        Math.max(1, completionGraceMs - (Date.now() - graceStarted))));
      finalSample = await sample('completion-check');
    }
    if (finalSample !== boundary) samples.push(finalSample);

    const transition = await this.page.evaluate(() => {
      const scope = window.__aosTimedTransition;
      if (!scope) return null;
      try { scope.update(); } catch (_) {}
      try { scope.observer.disconnect(); } catch (_) {}
      try { scope.control?.removeEventListener('click', scope.onClick, { capture: true }); } catch (_) {}
      try { document.removeEventListener('submit', scope.onSubmit, { capture: true }); } catch (_) {}
      const out = { ...scope.receipt };
      delete window.__aosTimedTransition;
      return out;
    }).catch(() => null);
    const numericTimestamp = (value) => value === null || value === undefined ? null : Number(value);
    const clickAt = numericTimestamp(transition && transition.clickAt);
    const pendingSeenAt = numericTimestamp(transition && transition.pendingSeenAt);
    const completionAt = numericTimestamp(transition && transition.completionAt);
    const transitionDurationMs = Number.isFinite(clickAt) && Number.isFinite(completionAt)
      ? completionAt - clickAt : null;
    const pendingStartDelayMs = Number.isFinite(clickAt) && Number.isFinite(pendingSeenAt)
      ? pendingSeenAt - clickAt : null;
    const earlyCompletion = Number.isFinite(transitionDurationMs)
      ? transitionDurationMs + 1 < durationMs : false;
    return {
      clicked: sel, timedTransition: true, disabledBeforeClick: false,
      disabledAfterClick: Boolean(samples[0] && samples[0].control_disabled),
      emptyRequiredFieldsBefore,
      pointerEvidence: await this.pointerEvidence().catch(() => []),
      keyboardEvidence: await this.keyboardEvidence().catch(() => []),
      transition: {
        required_duration_ms: durationMs,
        full_duration_required: msg.full_duration_required !== false,
        completion_grace_ms: completionGraceMs,
        pending_text: pendingText,
        pending_seen: Number.isFinite(pendingSeenAt),
        pending_start_delay_ms: pendingStartDelayMs,
        completion_observed: Number.isFinite(completionAt),
        transition_duration_ms: transitionDurationMs,
        completed_before_required_duration: earlyCompletion,
        stable_through_required_boundary: !earlyCompletion && Boolean(boundary.pending ||
          (Number.isFinite(transitionDurationMs) && transitionDurationMs + 1 >= durationMs)),
        duplicate_attempts: ['trusted-pointer', 'trusted-keyboard-enter'],
        submit_event_count: transition ? transition.submitEvents : null,
        mutation_count: transition ? transition.mutationCount : null,
        elapsed_ms: Date.now() - started,
        samples,
      },
    };
  }

  // Click the clickable element whose visible LABEL best matches `text` (optionally constrained to a
  // `role`). Matching, in order of preference: exact (normalized, case-insensitive) label, then
  // case-insensitive substring either way. Returns what was matched so the caller can VERIFY the
  // intended control was actually actuated (not a blind index) — the anti-false-positive contract.
  async clickByText(msg) {
    const want = (msg.text === undefined || msg.text === null) ? '' : String(msg.text).trim();
    if (!want) throw new Error('clickByText requires text');
    const role = (msg.role || '').toString().trim().toLowerCase();
    const found = await this.page.evaluate(({ want, role }) => {
      const SEL = 'a,button,input,select,textarea,[role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="checkbox"],[onclick],[contenteditable="true"],summary,label';
      const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
      const wl = norm(want).toLowerCase();
      const visible = (el) => {
        let r; try { r = el.getBoundingClientRect(); } catch (_) { return false; }
        if (r.width <= 0 || r.height <= 0) return false;
        for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
          const st = window.getComputedStyle(n);
          if (n.hidden || n.getAttribute('aria-hidden') === 'true' || st.display === 'none' ||
              st.visibility === 'hidden' || Number(st.opacity) <= 0.05) return false;
        }
        return true;
      };
      const labelOf = (el) => {
        const associated = Array.from(el.labels || []).map((label) => label.innerText || '').join(' ');
        return norm(el.innerText || el.getAttribute('aria-label') || associated ||
          el.getAttribute('placeholder') || el.getAttribute('name') || el.value ||
          el.getAttribute('title') || '');
      };
      const hrefOf = (el) => norm(el.getAttribute('href') || '');
      const implicitRoleOf = (el) => {
        const tag = el.tagName.toLowerCase();
        const typ = (el.getAttribute('type') || '').toLowerCase();
        if (tag === 'a') return 'link';
        if (tag === 'button') return 'button';
        if (tag === 'textarea') return 'textbox';
        if (tag === 'select') return 'combobox';
        if (tag === 'input') {
          if (typ === 'checkbox') return 'checkbox';
          if (typ === 'radio') return 'radio';
          if (typ === 'button' || typ === 'submit') return 'button';
          return 'textbox';
        }
        return tag;
      };
      const roleOf = (el) => (el.getAttribute('role') || '').toLowerCase() || implicitRoleOf(el);
      let best = null, bestScore = 0, bestLabel = '', bestRole = '', bestHref = '';
      const scan = (respectRole) => {
        for (const el of document.querySelectorAll(SEL)) {
          if (!visible(el)) continue;
          const er = roleOf(el);
          if (respectRole && role && er !== role && el.tagName.toLowerCase() !== role) continue;
          const label = labelOf(el);
          const ll = label.toLowerCase();
          let score = 0;
          if (ll && ll === wl) score = 3;                              // exact
          else if (ll && wl && (ll.includes(wl) || wl.includes(ll))) score = 2;  // contains
          if (score > bestScore) {
            bestScore = score; best = el; bestLabel = label; bestRole = er; bestHref = hrefOf(el);
          }
        }
      };
      scan(true);
      if (!best && role) {
        scan(false);
      }
      if (!best || bestScore === 0) return null;
      best.setAttribute('data-aos-hit', '1');
      return { label: bestLabel, role: bestRole, score: bestScore, href: bestHref };
    }, { want, role });
    if (!found) return { clicked: false, matched: null, note: 'no clickable element matched "' + want + '"' };
    const sel = '[data-aos-hit="1"]';
    const emptyRequiredFieldsBefore = await this._emptyRequiredFieldsBefore(sel);
    const disabledBeforeClick = await this.page.evaluate(() => {
      const e = document.querySelector('[data-aos-hit="1"]');
      return e ? Boolean(e.disabled || e.getAttribute('aria-disabled') === 'true') : null;
    });
    if (disabledBeforeClick) {
      try { await this.page.evaluate(() => { const e = document.querySelector('[data-aos-hit="1"]'); if (e) e.removeAttribute('data-aos-hit'); }); } catch (_) {}
      return { clicked: false, matched: found.label, matchedRole: found.role,
               matchedHref: found.href, score: found.score, disabledBeforeClick: true,
               disabledAfterClick: true, note: 'control is disabled; no activation attempted',
               pointerEvidence: [], emptyRequiredFieldsBefore };
    }
    let disabledAfterClick = null;
    try {
      await this.armPointerEvidence();
      const loc = this.page.locator(sel).first();
      await loc.scrollIntoViewIfNeeded({ timeout: CMD_TIMEOUT_MS - 1000 });
      const box = await loc.boundingBox();
      if (!box) throw new Error('clickByText target has no visible bounding box');
      await this.page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      try {
        disabledAfterClick = await this.page.evaluate(() => {
          const e = document.querySelector('[data-aos-hit="1"]');
          return e ? Boolean(e.disabled || e.getAttribute('aria-disabled') === 'true') : null;
        });
      } catch (_) {}
    } finally {
      try { await this.page.evaluate(() => { const e = document.querySelector('[data-aos-hit="1"]'); if (e) e.removeAttribute('data-aos-hit'); }); } catch (_) {}
    }
    return {
      clicked: true,
      matched: found.label,
      matchedRole: found.role,
      matchedHref: found.href,
      score: found.score,
      disabledBeforeClick: false,
      disabledAfterClick,
      emptyRequiredFieldsBefore,
      pointerEvidence: await this.pointerEvidence().catch(() => []),
    };
  }

  async fill(msg) {
    const sel = this._selectorFor(msg);
    if (!sel) throw new Error('fill requires selector or idx');
    const value = msg.value === undefined ? '' : String(msg.value);
    const loc = this.page.locator(sel).first();
    const selected = await loc.evaluate((el, value) => {
      if (!el || el.tagName.toLowerCase() !== 'select') return null;
      const wanted = String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
      let match = null;
      for (const option of el.options || []) {
        const label = String(option.label || option.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
        const val = String(option.value || '').replace(/\s+/g, ' ').trim().toLowerCase();
        if (label === wanted || val === wanted || label.includes(wanted) || wanted.includes(label)) {
          match = option;
          break;
        }
      }
      if (!match) return { select: true, selected: false, value: el.value };
      el.value = match.value;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return { select: true, selected: true, value: el.value, label: match.label || match.textContent || '' };
    }, value).catch(() => null);
    if (selected && selected.select) {
      const norm = (v) => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
      const actualValueMatches = Boolean(selected.selected && (
        norm(selected.value) === norm(value) || norm(selected.label) === norm(value)
      ));
      return { filled: sel, value, ...selected, actualValue: selected.value, actualValueMatches };
    }
    await loc.fill(value, { timeout: CMD_TIMEOUT_MS - 200 });
    // Read the exact same live locator immediately after Playwright reports the fill complete.  A later
    // page-wide snapshot may omit a deep control, reindex a rerendered form, or intentionally redact a
    // password.  The action-boundary comparison is therefore the authoritative mechanical receipt.
    const proof = await loc.evaluate((el, expected) => {
      // Playwright deliberately forwards a fill invoked on <label> to its associated control. Read that same
      // effective control rather than the label's textContent, otherwise the receipt says "Public body" even
      // though the textarea contains the requested value.
      const target = (String(el.tagName || '').toLowerCase() === 'label')
        ? (el.control || (el.htmlFor && document.getElementById(el.htmlFor)) ||
           el.querySelector('input,select,textarea,[contenteditable="true"]') || el)
        : el;
      const actual = ('value' in target) ? String(target.value || '') : String(target.textContent || '');
      const secret = String(target.getAttribute && target.getAttribute('type') || '').toLowerCase() === 'password';
      return { actualValue: secret ? null : actual, actualValueMatches: actual === String(expected) };
    }, value);
    return { filled: sel, value, ...proof };
  }

  async humanType(msg) {
    const sel = this._selectorFor(msg);
    if (!sel) throw new Error('humanType requires selector or idx');
    const value = msg.value === undefined ? '' : String(msg.value);
    const paceMs = Math.min(500, Math.max(10, Number(msg.pace_ms || 35)));
    const loc = this.page.locator(sel).first();
    // A native select has no text caret. Keep the existing exact option-selection receipt for that control;
    // text-like controls below use trusted key events so a per-keystroke story is not "proven" by DOM fill.
    const tag = await loc.evaluate((el) => String(el && el.tagName || '').toLowerCase())
      .catch(() => '');
    if (tag === 'select') return this.fill(msg);
    await loc.fill('', { timeout: CMD_TIMEOUT_MS - 200 });
    await this.armKeyboardEvidence();
    await loc.focus({ timeout: CMD_TIMEOUT_MS - 200 });
    await this.page.keyboard.type(value, { delay: paceMs });
    const proof = await loc.evaluate((el, expected) => {
      const target = (String(el.tagName || '').toLowerCase() === 'label')
        ? (el.control || (el.htmlFor && document.getElementById(el.htmlFor)) ||
           el.querySelector('input,select,textarea,[contenteditable="true"]') || el)
        : el;
      const actual = ('value' in target) ? String(target.value || '') : String(target.textContent || '');
      const secret = String(target.getAttribute && target.getAttribute('type') || '').toLowerCase() === 'password';
      return { actualValue: secret ? null : actual, actualValueMatches: actual === String(expected) };
    }, value);
    return { typed: sel, value, pace_ms: paceMs, ...proof,
      keyboardEvidence: await this.keyboardEvidence().catch(() => []) };
  }

  async paste(msg) {
    const sel = this._selectorFor(msg);
    if (!sel) throw new Error('paste requires selector or idx');
    const value = msg.value === undefined ? '' : String(msg.value);
    const loc = this.page.locator(sel).first();
    const origin = new URL(this.page.url()).origin;
    await this.page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin });
    await this.page.evaluate(async (text) => navigator.clipboard.writeText(text), value);
    await this.page.evaluate(() => {
      window.__aosPasteEvidence = [];
      const record = (event) => window.__aosPasteEvidence.push({
        type: event.type,
        isTrusted: event.isTrusted === true,
        inputType: event.inputType || '',
        clipboardLength: event.clipboardData
          ? String(event.clipboardData.getData('text/plain') || '').length : undefined,
      });
      document.addEventListener('paste', record, { capture: true, once: true });
      document.addEventListener('input', record, { capture: true, once: true });
      document.addEventListener('change', record, { capture: true, once: true });
    });
    await loc.focus({ timeout: CMD_TIMEOUT_MS - 200 });
    // A user's whole-value paste conventionally replaces the selected field contents. Without this, a
    // sequential validation matrix appends to the prior case (for example -1 followed by 31 becomes -131),
    // corrupting the exact boundary while still producing a trusted paste event.
    try {
      await loc.selectText({ timeout: CMD_TIMEOUT_MS - 200 });
    } catch (_) {
      await this.page.keyboard.press(process.platform === 'darwin' ? 'Meta+A' : 'Control+A');
    }
    await this.page.keyboard.press(process.platform === 'darwin' ? 'Meta+V' : 'Control+V');
    const proof = await loc.evaluate((el, expected) => {
      const target = (String(el.tagName || '').toLowerCase() === 'label')
        ? (el.control || (el.htmlFor && document.getElementById(el.htmlFor)) ||
           el.querySelector('input,select,textarea,[contenteditable="true"]') || el)
        : el;
      const actual = ('value' in target) ? String(target.value || '') : String(target.textContent || '');
      const secret = String(target.getAttribute && target.getAttribute('type') || '').toLowerCase() === 'password';
      return { actualValue: secret ? null : actual.slice(0, 1000),
        actualValueLength: actual.length, actualValueMatches: actual === String(expected) };
    }, value);
    const pasteEvidence = await this.page.evaluate(() => window.__aosPasteEvidence || []).catch(() => []);
    return { pasted: sel, valueLength: value.length, ...proof, pasteEvidence };
  }

  async press(msg) {
    const sel = this._selectorFor(msg);
    const key = msg.key === undefined ? String(msg.value || 'Enter') : String(msg.key);
    // A page-level keyboard action intentionally targets the currently focused control.  This is how a human
    // submits a form after typing into its last field.  Requiring a selector turned a valid `press Enter`
    // decision into a bridge error which the old evaluator then misreported as a dead product control.
    await this.armKeyboardEvidence();
    if (!sel) {
      await this.page.keyboard.press(key);
      return { pressed: 'activeElement', key, pageLevel: true,
        keyboardEvidence: await this.keyboardEvidence().catch(() => []) };
    }
    const loc = this.page.locator(sel).first();
    if (msg._qa_reported_focus_source === true && (key === 'Tab' || key === 'Shift+Tab')) {
      const focusSnapshot = async () => await this.page.evaluate(() => {
        const el = document.activeElement;
        const norm = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const labels = el && el.labels ? [...el.labels].map((label) => label.innerText || '').join(' ') : '';
        const style = el ? getComputedStyle(el) : null;
        let pseudo = false;
        try { pseudo = Boolean(el && el.matches(':focus-visible')); } catch (_) {}
        const renderedFocus = Boolean(style && (
          (style.outlineStyle !== 'none' && parseFloat(style.outlineWidth || '0') > 0) ||
          (style.boxShadow && style.boxShadow !== 'none')));
        return {
          scroll: { x: Math.round(scrollX), y: Math.round(scrollY) },
          active: {
            tag: el && el.tagName ? el.tagName.toLowerCase() : '',
            id: String(el && el.id || ''), name: String(el && el.getAttribute && el.getAttribute('name') || ''),
            label: norm(el && (el.getAttribute('aria-label') || labels || el.getAttribute('placeholder') ||
              el.getAttribute('name') || el.textContent || el.value)).slice(0, 180),
            focusVisible: pseudo || renderedFocus,
          },
          horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
        };
      });
      await loc.focus({ timeout: CMD_TIMEOUT_MS - 200 });
      const source = await focusSnapshot();
      await this.page.keyboard.press(key);
      const destination = await focusSnapshot();
      return { pressed: sel, key, focusTransition: {
        source, destination,
        scroll_delta: { x: destination.scroll.x - source.scroll.x,
                        y: destination.scroll.y - source.scroll.y },
      }, keyboardEvidence: await this.keyboardEvidence().catch(() => []) };
    }
    // Native-select accessibility evidence must come from a real trusted keyboard event.  The former
    // in-DOM selectedIndex/change simulation was neither a user's input nor a trustworthy keyboard receipt;
    // on controlled selects it could also be synchronously overwritten by the app and look like a dead
    // control. Current Playwright/Chromium advances native selects correctly once the exact control is
    // focused, while emitting trusted keydown/input/change/keyup events.
    const isSelect = await loc.evaluate((el) => Boolean(el && el.tagName.toLowerCase() === 'select'))
      .catch(() => false);
    if (isSelect) {
      await loc.focus({ timeout: CMD_TIMEOUT_MS - 200 });
      const beforeValue = await loc.inputValue({ timeout: CMD_TIMEOUT_MS - 200 });
      await this.page.keyboard.press(key);
      const selected = await loc.evaluate((el) => {
        const option = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
        return { value: el.value, label: option ? (option.label || option.textContent || '') : '' };
      });
      return { pressed: sel, key, selected: true, select: true,
        changed: selected.value !== beforeValue, beforeValue, ...selected,
        keyboardEvidence: await this.keyboardEvidence().catch(() => []) };
    }
    await loc.press(key, { timeout: CMD_TIMEOUT_MS - 200 });
    return { pressed: sel, key, keyboardEvidence: await this.keyboardEvidence().catch(() => []) };
  }

  async tabTraverse(msg) {
    const direction = String((msg && msg.direction) || 'forward').toLowerCase();
    if (!['forward', 'backward'].includes(direction)) {
      throw new Error('tabTraverse direction must be forward or backward');
    }
    const derivedCount = await this.page.evaluate(() => {
      const selector = 'a[href],button,input,select,textarea,summary,[tabindex],[contenteditable="true"]';
      return [...document.querySelectorAll(selector)].filter((el) => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && !el.hidden && !el.disabled &&
          el.getAttribute('aria-hidden') !== 'true' && el.getAttribute('tabindex') !== '-1' &&
          style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) > 0.05;
      }).length;
    }).catch(() => 24);
    const supplied = Number(msg && msg.count);
    const count = Number.isInteger(supplied) && supplied > 0
      ? Math.min(120, Math.max(2, supplied))
      : Math.min(120, Math.max(8, Number(derivedCount || 0) + 8));
    const key = direction === 'backward' ? 'Shift+Tab' : 'Tab';
    // Real screen-reader verification needs enough time for AT-SPI to observe each focus boundary. This is
    // workload pacing, not a story timeout; ordinary visual-focus traversal keeps the 35ms default.
    const paceMs = Math.min(1500, Math.max(0, Number(msg && msg.pace_ms || 35)));
    const sequence = [];
    await this.armKeyboardEvidence();
    for (let index = 0; index < count; index++) {
      await this.page.keyboard.press(key);
      await this.page.waitForTimeout(paceMs);
      const snapshot = await this.page.evaluate(() => {
        const el = document.activeElement;
        if (!el || el === document.body || el === document.documentElement) {
          return { tag: 'body', label: '(document)', focusVisible: false,
            scrollX: Math.round(scrollX), scrollY: Math.round(scrollY),
            horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1 };
        }
        const norm = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const isControl = /^(input|select|textarea|button)$/i.test(el.tagName);
        const associated = isControl ? [...(el.labels || [])].map((label) => label.innerText || '').join(' ') : '';
        const label = norm(el.innerText || el.getAttribute('aria-label') || associated ||
          el.getAttribute('placeholder') || el.getAttribute('name') || el.value || el.title).slice(0, 160);
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        let focusPseudoVisible = false;
        try { focusPseudoVisible = el.matches(':focus-visible'); } catch (_) {}
        const outlineColor = String(style.outlineColor || '').toLowerCase();
        const outlineVisible = !['none', 'hidden'].includes(String(style.outlineStyle || '').toLowerCase()) &&
          Number.parseFloat(style.outlineWidth || '0') > 0 && outlineColor !== 'transparent' &&
          outlineColor !== 'rgba(0, 0, 0, 0)';
        const boxShadow = String(style.boxShadow || '');
        const shadowVisible = boxShadow !== '' && boxShadow !== 'none' &&
          !boxShadow.includes('rgba(0, 0, 0, 0)');
        const focusVisible = focusPseudoVisible || outlineVisible || shadowVisible;
        return {
          idx: el.getAttribute('data-aos-idx'), tag: el.tagName.toLowerCase(),
          type: (el.getAttribute('type') || '').toLowerCase(), role: (el.getAttribute('role') || '').toLowerCase(),
          id: String(el.id || '').slice(0, 100), name: String(el.getAttribute('name') || '').slice(0, 80),
          label, focusVisible, focusPseudoVisible,
          focusStyle: {
            outline: [style.outlineStyle, style.outlineWidth, style.outlineColor].join(' '),
            outlineOffset: style.outlineOffset, boxShadow: boxShadow.slice(0, 180),
          },
          rect: { left: Math.round(rect.left), top: Math.round(rect.top),
            width: Math.round(rect.width), height: Math.round(rect.height) },
          scrollX: Math.round(scrollX), scrollY: Math.round(scrollY),
          horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
        };
      });
      sequence.push({ order: index + 1, ...snapshot });
    }
    // Chromium may briefly put focus on the document between the final tabbable control and wrap-around.
    // Preserve that transition in the sequence (it is useful focus-order evidence), but do not count the
    // document sentinel as an interactive control or report it as a control with a missing focus indicator.
    const controlSequence = sequence.filter((item) => item.tag !== 'body');
    const unique = new Set(controlSequence.map((item) =>
      [item.idx, item.tag, item.id, item.name, item.label].join('|'))).size;
    return {
      traversal: true, direction, key, count, pace_ms: paceMs,
      derived_focusable_count: Number(derivedCount || 0),
      unique_controls: unique,
      document_focus_stops: sequence.length - controlSequence.length,
      all_focus_visible: controlSequence.length > 0 && controlSequence.every((item) => item.focusVisible),
      horizontal_overflow_seen: sequence.some((item) => item.horizontalOverflow), sequence,
      keyboardEvidence: await this.keyboardEvidence().catch(() => []),
    };
  }

  async hold(msg) {
    const sel = this._selectorFor(msg);
    const key = msg.key === undefined ? String(msg.value || 'Space') : String(msg.key);
    const durationMs = Math.max(50, Math.min(2000, Number(msg.duration_ms || 350)));
    await this.armKeyboardEvidence();
    if (sel) await this.page.locator(sel).first().focus({ timeout: CMD_TIMEOUT_MS - 500 });
    await this.page.keyboard.down(key);
    await this.page.waitForTimeout(durationMs);
    await this.page.keyboard.down(key); // repeated down while held yields repeat=true in Chromium
    await this.page.keyboard.up(key);
    return { pressed: sel || 'activeElement', held: true, key, duration_ms: durationMs,
      keyboardEvidence: await this.keyboardEvidence().catch(() => []) };
  }

  async viewport(msg) {
    const width = Number(msg.width);
    const height = Number(msg.height);
    if (!Number.isInteger(width) || !Number.isInteger(height)) {
      throw new Error('viewport requires integer width and height');
    }
    if (width < 320 || width > 3840 || height < 320 || height > 2160) {
      throw new Error('viewport width/height outside safe bounds');
    }
    await this.page.setViewportSize({ width, height });
    await this.settle();
    return { viewport: this.page.viewportSize() };
  }

  async scrollToText(msg) {
    const text = String(msg.text || '').replace(/\s+/g, ' ').trim();
    if (!text) throw new Error('scrollToText requires text');
    const result = await this.page.evaluate((wanted) => {
      const norm = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const nodes = [...document.querySelectorAll(
        'h1,h2,h3,[role=main],[role=navigation],[role=region],[role=complementary],'
        + '[role=alert],[role=status],section[aria-label]')];
      const candidates = nodes.map((el) => {
        const style = getComputedStyle(el);
        if (el.hidden || el.getAttribute('aria-hidden') === 'true' || style.display === 'none' ||
            style.visibility === 'hidden' || Number(style.opacity) <= 0.05) return null;
        const heading = el.matches('h1,h2,h3') ? el : el.querySelector(':scope > h1,:scope > h2,:scope > h3');
        const label = norm(el.getAttribute('aria-label') || (heading && heading.textContent) ||
          (el.matches('h1,h2,h3,[role=alert],[role=status]') ? el.textContent : ''));
        return label ? { el, label } : null;
      }).filter(Boolean);
      const lower = wanted.toLowerCase();
      const match = candidates.find((item) => item.label.toLowerCase() === lower) ||
        candidates.find((item) => item.label.toLowerCase().includes(lower) ||
          lower.includes(item.label.toLowerCase()));
      if (!match) return { scrolled: false, matched: null, requested: wanted };
      match.el.scrollIntoView({ block: 'start', inline: 'nearest', behavior: 'instant' });
      return {
        scrolled: true, matched: match.label,
        y: Math.max(0, Math.round(match.el.getBoundingClientRect().top + scrollY)),
      };
    }, text);
    await this.page.waitForTimeout(120);
    return result;
  }

  async dwellLandmarks(msg) {
    const targets = [...new Set((Array.isArray(msg.targets) ? msg.targets : [])
      .map((item) => String(item || '').replace(/\s+/g, ' ').trim()).filter(Boolean))].slice(0, 8);
    if (!targets.length) throw new Error('dwellLandmarks requires one or more landmark targets');
    const requestedMs = msg.duration_ms !== undefined
      ? Number(msg.duration_ms) : Number(msg.duration_s === undefined ? 10 : msg.duration_s) * 1000;
    const durationMs = Math.min(60000, Math.max(25, Math.round(requestedMs)));
    if (durationMs * targets.length > ASYNC_WAIT_MAX_MS) {
      throw new Error('dwellLandmarks total duration exceeds the external-wait bound');
    }
    const snapshot = async (target) => await this.page.evaluate((targetLabel) => {
      const norm = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const visible = (el) => {
        if (!el) return false;
        let rect; try { rect = el.getBoundingClientRect(); } catch (_) { return false; }
        const style = getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && !el.hidden &&
          el.getAttribute('aria-hidden') !== 'true' && style.display !== 'none' &&
          style.visibility !== 'hidden' && Number(style.opacity) > 0.05;
      };
      const landmarkNodes = [...document.querySelectorAll(
        'h1,h2,h3,[role=main],[role=navigation],[role=region],[role=complementary],'
        + '[role=alert],[role=status],section[aria-label]')];
      const candidates = landmarkNodes.map((el) => {
        if (!visible(el)) return null;
        const heading = el.matches('h1,h2,h3') ? el : el.querySelector(':scope > h1,:scope > h2,:scope > h3');
        const label = norm(el.getAttribute('aria-label') || (heading && heading.textContent) ||
          (el.matches('h1,h2,h3,[role=alert],[role=status]') ? el.textContent : ''));
        return label ? { el, label } : null;
      }).filter(Boolean);
      const wanted = norm(targetLabel).toLowerCase();
      const landmark = candidates.find((item) => item.label.toLowerCase() === wanted) ||
        candidates.find((item) => item.label.toLowerCase().includes(wanted) ||
          wanted.includes(item.label.toLowerCase()));
      const scope = landmark && (landmark.el.matches(
        'section,article,[role=main],[role=region],[role=complementary]')
        ? landmark.el
        : (landmark.el.closest('section,article,[role=main],[role=region],[role=complementary]') ||
          landmark.el));
      const scopeText = norm(scope && scope.innerText);
      const scopeHeadingNodes = scope ? [
        ...(scope.matches && scope.matches('h1,h2,h3,h4,h5,h6') ? [scope] : []),
        ...scope.querySelectorAll('h1,h2,h3,h4,h5,h6')
      ] : [];
      const scopeHeadings = scopeHeadingNodes
        .filter(visible).map((el) => norm(el.textContent)).filter(Boolean).slice(0, 120);
      const definitionPairs = scope ? [...scope.querySelectorAll('dt')].filter(visible).map((dt) => ({
        label: norm(dt.textContent).slice(0, 120),
        value: norm(dt.nextElementSibling && dt.nextElementSibling.matches('dd')
          ? dt.nextElementSibling.textContent : '').slice(0, 240),
      })).filter((item) => item.label).slice(0, 180) : [];
      const statusValues = [];
      for (const match of scopeText.matchAll(/\bStatus\s+([a-z][a-z0-9_-]*)\b/giu)) {
        const value = String(match[1] || '').toLowerCase();
        if (value && !statusValues.includes(value)) statusValues.push(value);
      }
      const rawReferencePattern = /\b(?:enquiry|job|result|draft|notification|audit|task)_[a-z0-9]+\b/giu;
      const emailPattern = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/giu;
      const phonePattern = /(?:\+\d[\d ()-]{7,}\d|\b\d{3}[-. ()]\d{3}[-. ]\d{4}\b)/gu;
      const isoTimestampPattern = /\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\b/gu;
      const rawMetadataLabels = new Set([
        'enquiry reference', 'job reference', 'result reference', 'draft reference',
        'notification reference', 'audit reference', 'source', 'submitted', 'updated',
        'task type', 'attempts', 'target', 'occurred'
      ]);
      const scopeSummary = {
        target: landmark ? landmark.label : norm(targetLabel),
        text_chars: scopeText.length,
        headings: scopeHeadings,
        definition_pairs: definitionPairs,
        status_values: statusValues,
        article_count: scope ? scope.querySelectorAll('article').length : 0,
        review_card_count: scope ? scope.querySelectorAll('[data-review-state]').length : 0,
        control_count: scope ? scope.querySelectorAll('a,button,input,select,textarea,summary,[tabindex]').length : 0,
        json_block_count: scope ? [...scope.querySelectorAll('pre')].filter(visible).length : 0,
        raw_reference_count: (scopeText.match(rawReferencePattern) || []).length,
        email_address_count: (scopeText.match(emailPattern) || []).length,
        phone_number_count: (scopeText.match(phonePattern) || []).length,
        iso_timestamp_count: (scopeText.match(isoTimestampPattern) || []).length,
        raw_metadata_labels: definitionPairs.map((item) => item.label.toLowerCase())
          .filter((label, index, all) => rawMetadataLabels.has(label) && all.indexOf(label) === index),
        text_prefix: scopeText.slice(0, 1200),
        text_suffix: scopeText.length > 1200 ? scopeText.slice(-1200) : '',
      };
      const active = document.activeElement;
      const activeLabel = (!active || active === document.body || active === document.documentElement) ? '' :
        norm(active.innerText || active.getAttribute('aria-label') ||
          [...(active.labels || [])].map((label) => label.innerText || '').join(' ') ||
          active.getAttribute('placeholder') || active.getAttribute('name') || active.value).slice(0, 160);
      const controls = [...document.querySelectorAll('input,select,textarea,[contenteditable="true"]')]
        .slice(0, 120).map((el) => ({
          id: String(el.id || '').slice(0, 80), name: String(el.getAttribute('name') || '').slice(0, 80),
          value: /password/i.test(el.type || '') ? '(redacted)' : String(el.value || '').slice(0, 160),
          checked: /^(checkbox|radio)$/i.test(el.type || '') ? Boolean(el.checked) : undefined,
        }));
      const visibleInViewport = (el) => {
        let rect; try { rect = el.getBoundingClientRect(); } catch (_) { return false; }
        if (rect.width <= 0 || rect.height <= 0 || rect.bottom < 0 || rect.top > innerHeight) return false;
        const style = getComputedStyle(el);
        return !el.hidden && el.getAttribute('aria-hidden') !== 'true' &&
          style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) > 0.05;
      };
      const viewportText = norm([...document.querySelectorAll(
        'main,section,article,[role="region"],[role="main"],h1,h2,h3,p,li,pre,output')]
        .filter(visibleInViewport).map((el) => el.innerText || el.textContent).join(' ')).slice(0, 5000);
      const statusText = norm([...document.querySelectorAll(
        '[role="status"],[role="alert"],[aria-live],output')]
        .filter(visibleInViewport).map((el) => el.textContent).join(' | ')).slice(0, 1800);
      return {
        url: location.href, title: document.title,
        scroll: { x: Math.round(scrollX), y: Math.round(scrollY) },
        active: { tag: active && active.tagName ? active.tagName.toLowerCase() : '', label: activeLabel },
        scopeSummary, controls, viewportText, statusText,
        horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
      };
    }, target);
    const observations = [];
    const started = Date.now();
    for (const target of targets) {
      const scroll = await this.scrollToText({ text: target });
      // Conditional screens (confirmation, error, empty-state) can disappear between the planner's
      // observation and execution. Waiting the full authored dwell against a target that did not resolve
      // can never produce evidence; fail this target immediately so the explorer can recreate the state.
      // Keep an explicit receipt so downstream judges still fail closed instead of mistaking the skip for a
      // successful zero-duration dwell.
      if (!scroll || scroll.scrolled !== true) {
        const missed = await snapshot(target);
        observations.push({
          target, scroll: scroll || { scrolled: false, matched: null, requested: target },
          requested_ms: durationMs, elapsed_ms: 0, stable: false,
          skipped: true, skip_reason: 'landmark-not-present', before: missed, after: missed,
        });
        continue;
      }
      const before = await snapshot(target);
      const dwellStarted = Date.now();
      await this.page.waitForTimeout(durationMs);
      const after = await snapshot(target);
      observations.push({
        target, scroll, requested_ms: durationMs, elapsed_ms: Date.now() - dwellStarted,
        stable: JSON.stringify(before) === JSON.stringify(after), before, after,
      });
    }
    return { landmarkDwell: true, targets, duration_ms_each: durationMs,
      elapsed_ms: Date.now() - started, observations };
  }

  async history(direction) {
    const from = this.page.url();
    if (direction === 'back') {
      await this.page.goBack({ waitUntil: 'domcontentloaded', timeout: CMD_TIMEOUT_MS - 200 });
    } else {
      await this.page.goForward({ waitUntil: 'domcontentloaded', timeout: CMD_TIMEOUT_MS - 200 });
    }
    await this.settle();
    return { from, url: this.page.url(), history: true, direction };
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

  async storageState() {
    // Playwright's portable state intentionally excludes transient DOM state.  That is correct: only
    // durable cookies/localStorage may justify carrying browser coverage across a worker rotation.
    return { state: await this.ctx.storageState() };
  }

  async resetStorage(msg) {
    // This bridge/context is disposable QA state.  Clearing is nevertheless an explicit command: goto/reload
    // must retain storage so persistence tests remain truthful.  Clear all common same-origin durable stores,
    // then navigate only after deletion completes so application code boots against a genuinely fresh state.
    const url = String(msg.url || (this.page && this.page.url()) || 'about:blank');
    try { await this.ctx.clearCookies(); } catch (_) {}
    try {
      await this.page.evaluate(async () => {
        try { localStorage.clear(); } catch (_) {}
        try { sessionStorage.clear(); } catch (_) {}
        try {
          if (self.caches) for (const key of await caches.keys()) await caches.delete(key);
        } catch (_) {}
        try {
          if (navigator.serviceWorker) {
            for (const reg of await navigator.serviceWorker.getRegistrations()) await reg.unregister();
          }
        } catch (_) {}
        try {
          if (indexedDB.databases) {
            for (const db of await indexedDB.databases()) {
              if (db && db.name) await new Promise(resolve => {
                const req = indexedDB.deleteDatabase(db.name);
                req.onsuccess = req.onerror = req.onblocked = () => resolve();
              });
            }
          }
        } catch (_) {}
      });
    } catch (_) { /* about:blank or a failed page has no origin to clear */ }
    await this.page.goto(url, { waitUntil: 'domcontentloaded', timeout: CMD_TIMEOUT_MS - 200 });
    return { reset: true, url };
  }

  async close() {
    // Return the recorded video's path so the Python side can transcode it to a scrollable .mp4. The file
    // is only FINALIZED when the context closes, and video.path() only resolves after that — so grab the
    // handle first, close the context, THEN read the path. Fail-open: a missing video is never an error.
    let videoPath = null;
    try {
      const v = (this.page && this.page.video) ? this.page.video() : null;
      if (this.ctx) await this.ctx.close();
      if (v) { try { videoPath = await v.path(); } catch (_) {} }
    } catch (_) {}
    try { if (this.browser) await this.browser.close(); } catch (_) {}
    return { closed: true, video: videoPath };
  }

  // Dispatch one parsed command under the hard timeout. Returns a response object (never throws).
  async handle(msg) {
    const base = { id: msg.id !== undefined ? msg.id : null, cmd: msg.cmd };
    try {
      let out;
      switch (msg.cmd) {
        case 'goto':      out = await withTimeout(this.goto(msg), CMD_TIMEOUT_MS, 'goto'); break;
        case 'reload':    out = await withTimeout(this.reload(), CMD_TIMEOUT_MS, 'reload'); break;
        case 'state':     out = await withTimeout(this.state(msg), STATE_TIMEOUT_MS, 'state'); break;
        case 'clearEvidence': out = this.clearEvidence(); break;
        case 'settle':    out = await withTimeout(this.settleCmd(msg), STATE_TIMEOUT_MS, 'settle'); break;
        case 'wait': {
          const waitMs = Math.min(ASYNC_WAIT_MAX_MS,
            Math.max(0, Number(msg.timeout_ms || 0)));
          out = await withTimeout(this.waitCmd(msg), waitMs + 1500, 'wait'); break;
        }
        case 'waitFor': {
          const waitMs = Math.min(ASYNC_WAIT_MAX_MS, Math.max(0, Number(msg.timeout_ms || 60000)));
          out = await withTimeout(this.waitFor(msg), waitMs + 1500, 'waitFor'); break;
        }
        case 'click':     out = await withTimeout(this.click(msg), CMD_TIMEOUT_MS, 'click'); break;
        case 'clickBurst': out = await withTimeout(this.clickBurst(msg), CMD_TIMEOUT_MS, 'clickBurst'); break;
        case 'timedTransition': {
          const durationMs = Math.min(ASYNC_WAIT_MAX_MS,
            Math.max(250, Number(msg.duration_ms === undefined ? 10000 : msg.duration_ms)));
          const graceMs = Math.min(30000,
            Math.max(0, Number(msg.completion_grace_ms === undefined ? 5000 : msg.completion_grace_ms)));
          out = await withTimeout(this.timedTransition(msg), durationMs + graceMs + 3000,
            'timedTransition'); break;
        }
        case 'clickByText': out = await withTimeout(this.clickByText(msg), CMD_TIMEOUT_MS, 'clickByText'); break;
        case 'pointer':   out = await withTimeout(this.pointer(msg), CMD_TIMEOUT_MS, 'pointer'); break;
        case 'fill':      out = await withTimeout(this.fill(msg), CMD_TIMEOUT_MS, 'fill'); break;
        case 'paste':     out = await withTimeout(this.paste(msg), CMD_TIMEOUT_MS, 'paste'); break;
        case 'humanType': out = await withTimeout(this.humanType(msg), CMD_TIMEOUT_MS, 'humanType'); break;
        case 'press':     out = await withTimeout(this.press(msg), CMD_TIMEOUT_MS, 'press'); break;
        case 'tabTraverse': {
          const timeoutMs = tabTraverseTimeoutMs(msg);
          out = await withTimeout(this.tabTraverse(msg), timeoutMs, 'tabTraverse');
          out.command_timeout_ms = timeoutMs;
          break;
        }
        case 'hold':      out = await withTimeout(this.hold(msg), CMD_TIMEOUT_MS, 'hold'); break;
        case 'back':      out = await withTimeout(this.history('back'), CMD_TIMEOUT_MS, 'back'); break;
        case 'forward':   out = await withTimeout(this.history('forward'), CMD_TIMEOUT_MS, 'forward'); break;
        case 'viewport':  out = await withTimeout(this.viewport(msg), CMD_TIMEOUT_MS, 'viewport'); break;
        case 'scrollToText': out = await withTimeout(this.scrollToText(msg), CMD_TIMEOUT_MS, 'scrollToText'); break;
        case 'dwellLandmarks': {
          const targetCount = Math.max(1, Math.min(8, Array.isArray(msg.targets) ? msg.targets.length : 1));
          const eachMs = msg.duration_ms !== undefined ? Number(msg.duration_ms) :
            Number(msg.duration_s === undefined ? 10 : msg.duration_s) * 1000;
          const dwellMs = Math.min(ASYNC_WAIT_MAX_MS, Math.max(0, eachMs * targetCount));
          out = await withTimeout(this.dwellLandmarks(msg), dwellMs + 3000, 'dwellLandmarks'); break;
        }
        case 'inject':    out = await withTimeout(this.inject(msg), CMD_TIMEOUT_MS, 'inject'); break;
        case 'eval':      out = await withTimeout(this.eval(msg), CMD_TIMEOUT_MS, 'eval'); break;
        case 'seedToken': out = await withTimeout(this.seedToken(msg), CMD_TIMEOUT_MS, 'seedToken'); break;
        case 'storageState': out = await withTimeout(this.storageState(), STATE_TIMEOUT_MS, 'storageState'); break;
        case 'resetStorage': out = await withTimeout(this.resetStorage(msg), CMD_TIMEOUT_MS, 'resetStorage'); break;
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

async function runBridge(bridge = new Bridge()) {
  try {
    await bridge.start();
  } catch (e) {
    writeLine({ ok: false, cmd: 'ready', error: 'startup: ' + String(e && e.message ? e.message : e) });
    process.exit(1);
    return;
  }
  writeLine({ ok: true, cmd: 'ready', pid: process.pid, shot_dir: SHOT_DIR, video_dir: VIDEO_DIR || null });

  // Serialize commands: one in flight at a time so responses map cleanly to requests (the AI loop is
  // strictly observe->act->observe, never concurrent).
  const rl = readline.createInterface({ input: process.stdin });
  let chain = Promise.resolve();
  let shuttingDown = false;
  const shutdown = async (code = 0) => {
    if (shuttingDown) return;
    shuttingDown = true;
    // Parent death closes stdin. Closing the Playwright context/browser here is what terminates Chromium and
    // its live video encoder; otherwise Node keeps its event loop alive and the whole tree becomes an orphan
    // that consumes a browser lease and CPU until the delayed host reaper notices it.
    try { await bridge.close(); } catch (_) {}
    process.exit(code);
  };
  rl.on('line', (line) => {
    const raw = line.trim();
    if (!raw) return;
    chain = chain.then(async () => {
      let msg;
      try { msg = JSON.parse(raw); }
      catch (e) { writeLine({ ok: false, error: 'bad json: ' + e.message }); return; }
      const res = await bridge.handle(msg);
      writeLine(res);
      if (msg.cmd === 'close') { shuttingDown = true; rl.close(); process.exit(0); }
    });
  });
  rl.on('close', () => { void shutdown(0); });
  process.on('SIGTERM', () => { void shutdown(0); });
  process.on('SIGINT', () => { void shutdown(0); });
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
	    '<style>#rendered-focus:focus{outline:3px solid rgb(214,163,59);outline-offset:2px}</style>' +
	    '<button id="go" aria-current="page" onclick="document.title=\'CLICKED GO\'">Go</button>' +
	    '<button id="rendered-focus">Rendered focus</button>' +
	    '<button id="replace" onclick="document.title=\'DETACHED CLICK\';this.outerHTML=\'<span id=detached-result>Replaced</span>\'">Replace self</button>' +
	    '<button id="disabled" disabled onclick="document.title=\'SHOULD NOT FIRE\'">Disabled submit</button>' +
	    '<button id="async" onclick="setTimeout(()=>document.getElementById(\'async-status\').textContent=\'Reply ready\',120)">Start async</button>' +
	    '<div id="async-status" role="status">Waiting</div>' +
	    '<h2>Thanks. We have your enquiry and will review service fit next.</h2>' +
	    '<form id="timed-form" onsubmit="event.preventDefault();const b=document.getElementById(\'timed-submit\');if(b.disabled)return;window.__timedSubmits=(window.__timedSubmits||0)+1;b.disabled=true;b.textContent=\'Sending...\';this.setAttribute(\'aria-busy\',\'true\');setTimeout(()=>{this.removeAttribute(\'aria-busy\');b.remove();document.getElementById(\'timed-result\').textContent=\'Completed once\'},350)">' +
	    '<button id="timed-submit" type="submit">Send timed</button></form><output id="timed-result"></output>' +
	    '<output id="machine-status">{&quot;agentJobsFailed&quot;: 1}</output>' +
	    '<output id="machine-zero-status">{&quot;agentJobsQueued&quot;: 0}</output>' +
	    '<output id="approval-status">follow_up_approved: Follow-up approved. Staff may now send it.</output>' +
	    '<label for="q">Search query</label><input id="q" type="text" placeholder="search here" onkeydown="if(event.key===\'Enter\')document.title=\'ACTIVE ENTER\'">' +
	    '<select id="svc" aria-label="Service"><option value="">Select</option><option value="solo">Solo walk (30 min)</option></select>' +
	    '<form id="publication" onsubmit="event.preventDefault();document.title=\'PUBLISHED\'">' +
	    '<label for="pub-title">Publication title</label><input id="pub-title" name="title" required>' +
	    '<label for="pub-body">Publication body</label><textarea id="pub-body" name="body" required></textarea>' +
	    '<button type="submit">Publish proof</button></form>' +
	    '<pre id="long-status" aria-live="polite">' + 'old-audit-row '.repeat(80) +
	    'Newest audit blocker: claim-current</pre>' +
	    '<a href="#next" role="link">Next link</a>' +
	    '<a href="mailto:test@example.com" role="link">Email handoff</a>' +
	    '<section style="display:none"><button>Hidden verify</button><input placeholder="hidden code"></section>' +
	    '<section aria-hidden="true"><button>Aria hidden</button></section>' +
      '<div style="height:1200px"></div><h2 id="bottom-evidence">Bottom viewport evidence</h2></body></html>');

  const childEnv = Object.assign({}, process.env);
  if (!childEnv.NODE_PATH) childEnv.NODE_PATH = nodePathHint();
  const child = spawn(process.execPath, [__filename], { env: childEnv, stdio: ['pipe', 'pipe', 'inherit'] });

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
    const readyObj = await withTimeout(readyP, 30000, 'ready');
    assert.strictEqual(readyObj.ok, true, readyObj.error || 'bridge ready');

    const g = await send({ id: 1, cmd: 'goto', url: HTML });
    assert.strictEqual(g.ok, true, 'goto ok');

    const s = await send({ id: 2, cmd: 'state' });
    assert.strictEqual(s.ok, true, 'state ok');
    assert.ok(s.screenshot && fs.existsSync(s.screenshot), 'screenshot file exists on disk');
    assert.ok(Array.isArray(s.elements) && s.elements.length >= 3, 'parsed >=3 elements (button,input,link)');
    const tags = s.elements.map((e) => e.tag);
    assert.ok(tags.includes('button') && tags.includes('input') && tags.includes('a'), 'button+input+a present');
    const go = s.elements.find((e) => e.text === 'Go');
    assert.strictEqual(go.current, 'page', 'aria-current semantic state is reported');
    const replace = s.elements.find((e) => e.text === 'Replace self');
    const replaceStarted = Date.now();
    const replaced = await send({ id: 53, cmd: 'click', idx: replace.idx });
    assert.strictEqual(replaced.ok, true,
      'a synchronous SPA replacement remains a successful click: ' + JSON.stringify(replaced));
    assert.ok(Date.now() - replaceStarted < 1000,
      'a handler that detaches its source control does not burn the command timeout');
    assert.ok((replaced.pointerEvidence || []).some((event) => event.isTrusted && event.type === 'click'),
      'the detached-control path retains its trusted pointer receipt');
    const replacedEffect = await send({ id: 54, cmd: 'eval', expr: 'document.title' });
    assert.strictEqual(replacedEffect.result, 'DETACHED CLICK',
      'the exactly-once detached-control activation committed its product effect');
    const labelledInput = s.elements.find((e) => e.tag === 'input');
    assert.strictEqual(labelledInput.text, 'Search query', 'associated label is the stable control label');
    assert.ok(!s.elements.some((e) => /hidden/i.test((e.text || '') + ' ' + (e.placeholder || ''))),
      'hidden descendant controls are not reported as interactable');
    assert.ok(s.elements.every((e) => typeof e.selector === 'string' && e.selector.includes('data-aos-idx')),
      'every element carries an actionable selector');
    assert.ok(Array.isArray(s.console_errors) && Array.isArray(s.recent_requests), 'state has console_errors+recent_requests arrays');
	    const timed = s.elements.find((e) => e.text === 'Send timed');
	    const timedProof = await send({ id: 139, cmd: 'timedTransition', idx: timed.idx,
	      duration_ms: 250, completion_grace_ms: 1000, pending_text: 'Sending' });
	    assert.strictEqual(timedProof.ok, true, 'timed transition command succeeds');
	    assert.strictEqual(timedProof.transition.pending_seen, true, 'timed transition sees pending state');
	    assert.strictEqual(timedProof.transition.completed_before_required_duration, false,
	      'timed transition remains pending through required duration');
	    assert.strictEqual(timedProof.transition.completion_observed, true,
	      'timed transition records completion after pending');
	    assert.strictEqual(timedProof.transition.submit_event_count, 1,
	      'duplicate pointer and Enter attempts do not resubmit');
	    const renderedFocus = s.elements.find((e) => e.text === 'Rendered focus');
	    const renderedFocusClick = await send({ id: 140, cmd: 'click', idx: renderedFocus.idx });
	    assert.strictEqual(renderedFocusClick.ok, true, 'rendered-focus control receives a trusted click');
	    const renderedFocusState = await send({ id: 141, cmd: 'state' });
	    assert.strictEqual(renderedFocusState.activeElement.focusVisible, true,
	      'a rendered outline is visible focus evidence even when input-modality :focus-visible does not match');
    assert.ok(/Newest audit blocker: claim-current/.test(s.statusText || ''),
      'large live regions expose the newest audit tail instead of only stale prefix rows');
    assert.strictEqual(s.settled, true, 'state was captured post-settle (SPA done painting)');
    assert.ok(/QA selftest/.test(s.viewportText || ''), 'state includes text rendered in the current viewport');
    assert.ok(!/Bottom viewport evidence/.test(s.viewportText || ''),
      'viewport text excludes off-screen document-tail text');
    assert.ok((s.documentLandmarks || []).some((item) => item.label === 'Bottom viewport evidence'),
      'state exposes bounded off-screen document landmarks');
    const scrolled = await send({ id: 44, cmd: 'scrollToText', text: 'Bottom viewport evidence' });
    assert.strictEqual(scrolled.scrolled, true, 'exact landmark scrolling succeeds');
    assert.strictEqual(scrolled.matched, 'Bottom viewport evidence', 'landmark receipt names the target');
    const bottomState = await send({ id: 45, cmd: 'state' });
    assert.ok(/Bottom viewport evidence/.test(bottomState.viewportText || ''),
      'state follows the current scroll position instead of repeating only the document prefix');
    assert.ok((bottomState.scrollPosition || {}).y > 0, 'state reports the actual scroll position');
    await send({ id: 46, cmd: 'eval', expr: 'window.scrollTo(0,0);true' });
    const cleared = await send({ id: 36, cmd: 'clearEvidence' });
    assert.strictEqual(cleared.ok, true, 'explicit recorder evidence clear succeeds');
    assert.ok(Number.isFinite(cleared.cleared_at), 'clear returns an auditable timestamp boundary');
    assert.ok(Number.isInteger(cleared.console_before) && Number.isInteger(cleared.requests_before),
      'clear returns pre-boundary console/request counts');
    const postClear = await send({ id: 37, cmd: 'state' });
    assert.deepStrictEqual(postClear.console_errors, [], 'console evidence is empty after explicit clear');
    const stored = await send({ id: 33, cmd: 'storageState' });
    assert.strictEqual(stored.ok, true, 'portable storage state can be checkpointed');
    assert.ok(stored.state && Array.isArray(stored.state.cookies) && Array.isArray(stored.state.origins),
      'storage state has Playwright cookies/origins shape');

    // explicit settle command: bounded, always resolves ok with the grace it waited.
    const st = await send({ id: 20, cmd: 'settle', ms: 50 });
    assert.strictEqual(st.ok, true, 'settle ok');
    assert.strictEqual(st.settled, true, 'settle reports settled');
    assert.ok(st.waited_ms >= 0, 'settle reports the paint grace it waited');

    const dwell = await send({ id: 40, cmd: 'wait', timeout_ms: 30 });
    assert.strictEqual(dwell.ok, true, 'bounded dwell ok');
    assert.strictEqual(dwell.waited, true, 'bounded dwell reports a real wait');
    assert.ok(dwell.elapsed_ms >= 20, 'bounded dwell advances real page time');

    const missingDwellStarted = Date.now();
    const missingDwell = await send({ id: 142, cmd: 'dwellLandmarks',
      targets: ['Conditional screen that is absent'], duration_s: 10 });
    assert.strictEqual(missingDwell.ok, true, 'missing conditional dwell returns an auditable receipt');
    assert.strictEqual(missingDwell.observations[0].skipped, true,
      'missing conditional dwell is explicitly skipped rather than falsely proven');
    assert.strictEqual(missingDwell.observations[0].elapsed_ms, 0,
      'missing conditional dwell records no elapsed evidence');
    assert.ok(Date.now() - missingDwellStarted < 2000,
      'missing conditional dwell fails fast instead of burning the authored duration');

    const disabledStart = Date.now();
    const disabledClick = await send({ id: 43, cmd: 'clickByText', text: 'Disabled submit', role: 'button' });
    assert.strictEqual(disabledClick.ok, true, 'disabled-control probe returns evidence');
    assert.strictEqual(disabledClick.clicked, false, 'disabled control is not actuated');
    assert.strictEqual(disabledClick.disabledBeforeClick, true, 'disabled state is explicit');
    assert.ok(Date.now() - disabledStart < 1000, 'disabled control does not burn the click timeout');

    const incompleteSubmit = await send({ id: 47, cmd: 'clickByText', text: 'Publish proof', role: 'button' });
    assert.strictEqual(incompleteSubmit.clicked, true, 'incomplete submit receives the requested activation');
    assert.deepStrictEqual((incompleteSubmit.emptyRequiredFieldsBefore || []).map((field) => field.label),
      ['Publication title', 'Publication body'],
      'submit receipt names required setup fields that were empty at the action boundary');

    const asyncStart = await send({ id: 38, cmd: 'clickByText', text: 'Start async', role: 'button' });
    assert.strictEqual(asyncStart.clicked, true, 'async fixture action starts');
    const asyncWait = await send({ id: 39, cmd: 'waitFor', kind: 'status', value: 'Reply ready', timeout_ms: 2000 });
    assert.strictEqual(asyncWait.ok, true, 'mechanical async wait returns');
    assert.strictEqual(asyncWait.matched, true, 'mechanical async wait observes the named status fact');
    assert.ok(asyncWait.elapsed_ms >= 0 && asyncWait.elapsed_ms < 2000, 'async wait is bounded and timed');
    const semanticConfirmation = await send({ id: 143, cmd: 'waitFor', kind: 'text',
      value: 'confirmation', timeout_ms: 500 });
    assert.strictEqual(semanticConfirmation.matched, true,
      'semantic confirmation waits match strong human confirmation copy without burning the timeout');
    const semanticStatus = await send({ id: 144, cmd: 'waitFor', kind: 'status',
      value: 'follow_up_approval_approved', timeout_ms: 500 });
    assert.strictEqual(semanticStatus.matched, true,
      'status waits match equivalent machine-style names and visible human copy without burning the timeout');
    const structuredWait = await send({ id: 48, cmd: 'waitFor', kind: 'status',
      value: 'agentJobsFailed', timeout_ms: 500 });
    assert.strictEqual(structuredWait.matched, true,
      'structured live-region keys match without wasting the full timeout on JSON punctuation');
    const structuredZero = await send({ id: 50, cmd: 'waitFor', kind: 'status',
      value: 'agentJobsQueued', timeout_ms: 100 });
    assert.strictEqual(structuredZero.matched, false,
      'a structured key with a zero value does not falsely satisfy a state-transition wait');
    const traversal = await send({ id: 49, cmd: 'tabTraverse', count: 4, direction: 'forward' });
    assert.strictEqual(traversal.traversal, true, 'batched Tab traversal returns a mechanical receipt');
    assert.strictEqual(traversal.sequence.length, 4, 'batched Tab traversal records every focus stop');
    assert.ok(traversal.command_timeout_ms > CMD_TIMEOUT_MS,
      'batched Tab traversal receives a workload-sized timeout instead of the single-action ceiling');
    assert.ok((traversal.keyboardEvidence || []).some((event) => event.isTrusted && event.key === 'Tab'),
      'batched Tab traversal is driven by trusted keyboard events');
    const wrappedTraversal = await send({ id: 52, cmd: 'tabTraverse', count: 16, direction: 'forward' });
    assert.ok(wrappedTraversal.document_focus_stops >= 1,
      'wrapped traversal preserves the document transition as focus-order evidence');
    assert.ok(wrappedTraversal.unique_controls <= wrappedTraversal.derived_focusable_count,
      'the document transition is not fabricated as an interactive control');
    assert.strictEqual(wrappedTraversal.all_focus_visible, true,
      'the document transition is excluded from interactive-control focus-visibility scoring');
    const landmarkDwell = await send({ id: 51, cmd: 'dwellLandmarks',
      targets: ['QA selftest', 'Bottom viewport evidence'], duration_ms: 25 });
    assert.strictEqual(landmarkDwell.landmarkDwell, true, 'multi-surface dwell returns one receipt');
    assert.strictEqual(landmarkDwell.observations.length, 2, 'every requested surface was observed');
    assert.ok(landmarkDwell.observations.every((item) => item.scroll.scrolled && item.stable),
      'each named surface remained stable for its real dwell interval');
    assert.ok(landmarkDwell.observations.every((item) =>
      item.before.scopeSummary && item.before.scopeSummary.target),
    'each named surface includes a bounded target-scoped structural summary');
    assert.ok(landmarkDwell.observations.some((item) =>
      (item.before.scopeSummary.headings || []).includes('Bottom viewport evidence')),
    'target-scoped evidence reaches the requested long-page section independent of viewport text truncation');

    // Exercise act-by-idx: fill the input, then read it back via eval.
    const inputEl = postClear.elements.find((e) => e.tag === 'input');
    const f = await send({ id: 3, cmd: 'fill', idx: inputEl.idx, value: 'hello' });
    assert.strictEqual(f.ok, true, 'fill ok');
    assert.strictEqual(f.actualValueMatches, true, 'fill returns an action-boundary value proof');
	    const ev = await send({ id: 4, cmd: 'eval', expr: "document.getElementById('q').value" });
	    assert.strictEqual(ev.result, 'hello', 'eval reads back the filled value');
	    const selectEl = postClear.elements.find((e) => e.tag === 'select');
	    const sf = await send({ id: 26, cmd: 'fill', idx: selectEl.idx, value: 'Solo walk (30 min)' });
	    assert.strictEqual(sf.ok, true, 'select fill ok');
	    assert.strictEqual(sf.selected, true, 'select fill selected an option by label');
	    const sv = await send({ id: 27, cmd: 'eval', expr: "document.getElementById('svc').value" });
	    assert.strictEqual(sv.result, 'solo', 'select option selected by visible label');
	    const sh = await send({ id: 29, cmd: 'press', idx: selectEl.idx, key: 'Home' });
	    assert.strictEqual(sh.changed, true, 'select keyboard Home deterministically changes the option');
	    assert.ok((sh.keyboardEvidence || []).some((event) => event.isTrusted && event.type === 'keydown'),
	      'select keyboard Home returns a trusted keyboard receipt');
	    const sd = await send({ id: 30, cmd: 'press', idx: selectEl.idx, key: 'ArrowDown' });
	    assert.strictEqual(sd.changed, true, 'select keyboard ArrowDown deterministically advances the option');
	    assert.strictEqual(sd.value, 'solo', 'select keyboard action reports the selected value');
	    assert.ok((sd.keyboardEvidence || []).some((event) => event.isTrusted && event.key === 'ArrowDown'),
	      'select keyboard ArrowDown is a real trusted browser event');
	    const richSel = "body:has-text('QA selftest') input[placeholder='search here']";
	    const richFill = await send({ id: 31, cmd: 'fill', selector: richSel, value: 'selector engine' });
	    assert.strictEqual(richFill.ok, true, 'fill supports Playwright selector engines');
	    const richVal = await send({ id: 32, cmd: 'eval', expr: "document.getElementById('q').value" });
	    assert.strictEqual(richVal.result, 'selector engine', 'Playwright selector fill lands in the intended input');
	    const activeEnter = await send({ id: 34, cmd: 'press', key: 'Enter' });
	    assert.strictEqual(activeEnter.ok, true, 'page-level press targets the active element');
	    assert.strictEqual(activeEnter.pageLevel, true, 'page-level press reports active-element semantics');
	    const activeEnterEffect = await send({ id: 35, cmd: 'eval', expr: 'document.title' });
	    assert.strictEqual(activeEnterEffect.result, 'ACTIVE ENTER',
	      'selectorless Enter reaches the currently focused input like a human keyboard action');

	    // clickByText: resolve the control by its visible LABEL (not a blind idx), case-insensitively, and
    // actuate it — proven by the button's onclick effect (title change).
    const cbt = await send({ id: 5, cmd: 'clickByText', text: 'go' });
    assert.strictEqual(cbt.ok, true, 'clickByText ok');
    assert.strictEqual(cbt.clicked, true, 'clickByText actuated a control');
    assert.ok(/^go$/i.test((cbt.matched || '').trim()), 'clickByText matched the intended label "Go"');
    const trustedPointer = (cbt.pointerEvidence || []).filter((event) => event.isTrusted === true);
    assert.ok(trustedPointer.some((event) => event.type === 'pointerdown' && event.pointerType === 'mouse'),
      'clickByText returns a trusted mouse pointerdown receipt');
    assert.ok(trustedPointer.some((event) => event.type === 'pointerup'),
      'clickByText returns a trusted pointerup receipt');
    assert.ok(trustedPointer.some((event) => event.type === 'click'),
      'clickByText returns a trusted click receipt');
    assert.ok(trustedPointer.every((event) => Number.isFinite(event.clientX) && Number.isFinite(event.clientY)),
      'trusted pointer receipts include browser coordinates');
    const eff = await send({ id: 6, cmd: 'eval', expr: 'document.title' });
    assert.strictEqual(eff.result, 'CLICKED GO', 'clicking "Go" by label fired its onclick (right control actuated)');
    await send({ id: 36, cmd: 'eval', expr: "document.title='RESET'" });
    const touch = await send({ id: 37, cmd: 'pointer', idx: go.idx, pointer_type: 'touch' });
    assert.ok((touch.pointerEvidence || []).some((event) => event.isTrusted && event.pointerType === 'touch'),
      'CDP touch activation returns a trusted touch pointer receipt');
    const touchEff = await send({ id: 38, cmd: 'eval', expr: 'document.title' });
    assert.strictEqual(touchEff.result, 'CLICKED GO', 'trusted touch input activates the target control');
    await send({ id: 39, cmd: 'eval', expr: "document.title='RESET'" });
    const pen = await send({ id: 40, cmd: 'pointer', idx: go.idx, pointer_type: 'pen' });
    assert.ok((pen.pointerEvidence || []).some((event) => event.isTrusted && event.pointerType === 'pen'),
      'CDP pen activation returns a trusted pen pointer receipt');
    const penEff = await send({ id: 41, cmd: 'eval', expr: 'document.title' });
    assert.strictEqual(penEff.result, 'CLICKED GO', 'trusted pen input activates the target control');
    await send({ id: 23, cmd: 'eval', expr: "document.title='RESET'" });
    const key = await send({ id: 24, cmd: 'press', idx: go.idx, key: 'Enter' });
    assert.strictEqual(key.ok, true, 'press ok');
    assert.ok((key.keyboardEvidence || []).some((event) => event.isTrusted && event.type === 'keydown'),
      'discrete key press returns trusted keyboard evidence');
    const keyEff = await send({ id: 25, cmd: 'eval', expr: 'document.title' });
    assert.strictEqual(keyEff.result, 'CLICKED GO', 'pressing Enter on "Go" fired its onclick');
    const held = await send({ id: 42, cmd: 'hold', idx: go.idx, key: 'Space', duration_ms: 50 });
    assert.ok((held.keyboardEvidence || []).some((event) => event.isTrusted && event.repeat === true),
      'held key returns a trusted repeat=true keydown receipt');
    assert.ok((held.keyboardEvidence || []).some((event) => event.isTrusted && event.type === 'click'),
      'held key returns the trusted browser activation click in the same receipt');
    // role filter: constrain to role=link so ONLY the anchor matches (not the button).
    const byRole = await send({ id: 7, cmd: 'clickByText', text: 'next link', role: 'link' });
	    assert.strictEqual(byRole.clicked, true, 'clickByText with role=link actuated the anchor');
	    assert.ok(/next link/i.test(byRole.matched || ''), 'role-filtered match hit the anchor label');
	    const handoff = await send({ id: 21, cmd: 'clickByText', text: 'email handoff', role: 'link' });
	    assert.strictEqual(handoff.clicked, true, 'mailto handoff link can be clicked');
	    assert.strictEqual(handoff.disabledAfterClick, false, 'clickByText reports immediate disabled state');
	    const postHandoffState = await send({ id: 22, cmd: 'state' });
	    assert.ok(!postHandoffState.recent_requests.some((r) => /^mailto:/i.test(r.url || '')),
	      'mailto browser handoff is not reported as a failed runtime request');
	    // a label that matches nothing must report clicked:false (NOT throw, NOT click something random).
	    const miss = await send({ id: 8, cmd: 'clickByText', text: 'this label does not exist anywhere' });
    assert.strictEqual(miss.ok, true, 'clickByText miss still ok');
    assert.strictEqual(miss.clicked, false, 'clickByText reports a miss rather than clicking a wrong control');

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
  if (process.argv[2] === 'selftest' || process.argv[2] === '--selftest') runSelftest();
  else runBridge();
}

module.exports = { Bridge, withTimeout, runBridge };
