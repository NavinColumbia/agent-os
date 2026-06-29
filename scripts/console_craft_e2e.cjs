// console_craft_e2e.cjs — standing GUARD for three product-craft lenses the triad audit flagged:
//   1) responsive-mobile     — at 390px width, no screen may horizontally overflow.
//   2) empty-loading-error-states — a fresh 0-data tenant must see a real empty-state (CTA/explanation),
//                              never a blank region or literal "undefined"/"null"/"NaN".
//   3) content-microcopy     — no screen may show the user raw JSON ({"…) or a bare backend enum token.
// Fails closed (exit 1) so a regression in any of these reds the suite. Drives a REAL fresh signup.
//   NODE_PATH=<noupload>/node_modules node console_craft_e2e.cjs <baseUrl>
const { chromium } = require('playwright');
const sleep = ms => new Promise(r => setTimeout(r, ms));

// screens that render tenant DATA (must show a real empty-state when the tenant has none)
const DATA_SCREENS = ['projects', 'design', 'approvals', 'activity', 'agents', 'portfolio'];
// every screen gets the responsive + microcopy check
const ALL = ['cockpit', 'projects', 'design', 'approvals', 'activity', 'billing', 'providers',
  'integrations', 'templates', 'orgs', 'portfolio', 'settings', 'agents', 'controller'];
// bare backend enums that must be humanized before reaching the user
const RAW_ENUMS = ['dead_letter', 'REQUEST-CHANGES', 'BLOCKED_AT_QA', 'request_hire', 'blocked_build',
  'consent_required', 'api_key', 'workspace-write'];

(async () => {
  const base = process.argv[2] || 'http://127.0.0.1:8099';
  const b = await chromium.launch();
  const fails = [];
  try {
    const ctx = await b.newContext({ viewport: { width: 390, height: 780 } });  // mobile-first
    const p = await ctx.newPage();
    await ctx.addInitScript(() => { try { localStorage.removeItem('aos_tenant'); localStorage.removeItem('aos_org'); } catch (e) {} });
    await p.goto(base + '/', { waitUntil: 'networkidle', timeout: 20000 });
    // fresh signup (0-data tenant) via the UI
    await p.waitForSelector('#signin', { state: 'visible', timeout: 8000 });
    await p.evaluate(() => suTab('up'));
    await p.fill('#su_name', 'Craft CEO');
    await p.fill('#su_email', `craft-${Date.now()}@example.com`);
    await p.fill('#su_pw', 'craft-password-123');
    await p.fill('#su_pw2', 'craft-password-123');
    await p.click('#su_signup button.pri');
    await p.waitForSelector('#app', { state: 'visible', timeout: 12000 });
    await sleep(2500);                               // let boot()'s one-time redirect settle

    for (const screen of ALL) {
      try {
        await p.evaluate(s => window.go(s), screen);
        await p.waitForFunction(() => {
          const v = document.querySelector('#view');
          return v && v.innerText.trim().length > 0 && !/^loading…?$/i.test(v.innerText.trim());
        }, { timeout: 8000 }).catch(() => {});
        await sleep(250);
        const r = await p.evaluate(() => ({
          overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          text: (document.querySelector('#view') || {}).innerText || '',
          html: (document.querySelector('#view') || {}).innerHTML || '',
        }));
        // 1) responsive: no horizontal overflow at 390px (small slack for sub-pixel)
        if (r.overflow > 4) fails.push(`${screen}: horizontal overflow ${r.overflow}px at 390px`);
        // 3) microcopy: no raw JSON / bad values / bare enums shown to the user
        if (/\{"|\[\{"/.test(r.text)) fails.push(`${screen}: raw JSON shown to user`);
        for (const bad of ['undefined', 'null', 'NaN', '[object Object]'])
          if (r.text.split(/\s+/).includes(bad)) fails.push(`${screen}: literal "${bad}" shown to user`);
        for (const e of RAW_ENUMS)
          if (r.text.includes(e)) fails.push(`${screen}: bare backend enum "${e}" shown to user`);
        // 2) empty-state: a data screen with no data must explain + offer a next step, not sit blank
        if (DATA_SCREENS.includes(screen)) {
          const t = r.text.trim();
          const hasCta = /\b(start|create|connect|new|add|get started|none yet|nothing|no )\b/i.test(t)
            || /button|onclick|class=pri/i.test(r.html);
          if (t.length < 8 || !hasCta) fails.push(`${screen}: weak/blank empty-state ("${t.slice(0, 40)}")`);
        }
      } catch (e) { fails.push(`${screen}: ${String(e).split('\n')[0]}`); }
    }

    if (fails.length) {
      console.log('FAIL: product-craft guard found ' + fails.length + ' issue(s):');
      fails.slice(0, 40).forEach(f => console.log('  - ' + f));
      await b.close(); process.exit(1);
    }
    console.log(`PASS: responsive (no overflow @390px) + real empty-states + no raw enum/JSON across ${ALL.length} screens ✅`);
    await b.close(); process.exit(0);
  } catch (e) {
    console.log('FAIL: craft guard fatal: ' + String(e).split('\n')[0]);
    await b.close(); process.exit(1);
  }
})();
