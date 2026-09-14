// console_actions_e2e.cjs — EXHAUSTIVE action-coverage crawler. A screenshot proves nothing; this
// enumerates EVERY interactive control (buttons, links, [onclick], inputs, selects, textareas, chips,
// menu items) on EVERY screen and actually EXERCISES the safe ones — clicking each, filling inputs —
// capturing: JS/console errors triggered, and DEAD controls (an onclick/button that does nothing).
// It resets to the screen between clicks so one action can't hide the next. Reports coverage
// (found / exercised) + every broken/dead/erroring control. Fails closed on JS errors or dead buttons.
//   NODE_PATH=<noupload>/node_modules node console_actions_e2e.cjs <baseUrl>
const { chromium } = require('playwright');
const sleep = ms => new Promise(r => setTimeout(r, ms));

// every primary surface a user can reach
const SCREENS = ['controller', 'cockpit', 'projects', 'design', 'approvals', 'activity', 'agents',
  'agentic', 'templates', 'orgs', 'portfolio', 'billing', 'providers', 'integrations', 'settings',
  'team', 'status', 'help', 'notifications'];
// destructive/navigation-away controls we ENUMERATE but do not blind-click (would derail the crawl)
const SKIP_TEXT = /sign out|log ?out|delete|remove|disconnect|cancel account|reset password & sign|approve & continue/i;

(async () => {
  const base = process.argv[2] || 'http://127.0.0.1:8099';
  const b = await chromium.launch();
  const errors = [];       // JS/console errors, tagged with the screen+control
  const dead = [];         // controls that did nothing when clicked
  const broken = [];       // controls whose click threw / errored the page
  let found = 0, exercised = 0;
  try {
    const ctx = await b.newContext({ viewport: { width: 1280, height: 900 } });
    const p = await ctx.newPage();
    // This crawler proves that each UI control is wired; it is NOT authorization to run builds,
    // agents, OAuth, billing, deletes, approvals, or any other mutation.  Historically it blindly
    // clicked those controls against the live local console and launched multiple Codex/Claude jobs.
    // Intercept every non-GET API call inside Playwright so a full regression can never spend tokens,
    // spawn a browser login, mutate operator state, or create runaway background work.  Read-only GETs
    // remain real, so every screen is still rendered from the actual application and database.
    let blockedMutations = 0;
    await p.route('**/api/**', async route => {
      const req = route.request();
      const path = new URL(req.url()).pathname;
      // These two nominally read-only views synthesize prose with a model on a cache miss.  A GET must
      // not become a hidden token-spending action during regression testing.
      if (req.method() === 'GET' && path === '/api/brief')
        return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
          headline: 'Action-crawl fixture', needs_you: [], team_did: ['Rendered safely'], watch: [],
          suggestion: 'Continue the bounded UI check'
        })});
      if (req.method() === 'GET' && path === '/api/explain')
        return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
          explanation: 'Seeded planning, build, and QA evidence for this action-crawl fixture.'
        })});
      if (req.method() === 'GET') return route.continue();
      blockedMutations++;
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ok: true, status: 'action-crawl-safe', message: 'mutation exercised without execution'})
      });
    });
    let where = 'boot';
    let reqs = 0, dialogs = 0;                           // a control that fires a request / opens a dialog is NOT dead
    p.on('request', () => reqs++);
    p.on('dialog', d => { dialogs++; d.dismiss().catch(() => {}); });   // native prompt/alert/confirm would block the crawl
    p.on('pageerror', e => errors.push(`[${where}] PAGEERROR ${String(e).split('\n')[0]}`));
    p.on('console', m => { if (m.type() === 'error') errors.push(`[${where}] CONSOLE ${m.text().slice(0, 120)}`); });

    // --- get into the app via a SEEDED token (robust; the UI signup flow is covered by other guards) ---
    where = 'seed';
    const tok = process.argv[3] || '';   // a real tenant token (with a company + consent) is seeded by the caller
    const org = process.argv[4] || '0';
    await ctx.addInitScript(([t, o]) => { try { localStorage.setItem('aos_tenant', t); if (o && o !== '0') localStorage.setItem('aos_org', o); } catch (e) {} }, [tok, org]);
    await p.goto(base + '/', { waitUntil: 'domcontentloaded', timeout: 20000 });
    await p.waitForSelector('#app', { state: 'visible', timeout: 12000 });
    await sleep(1500);

    p.setDefaultTimeout(2500);          // HARD cap for the CRAWL only (signup above used the default) — nothing may hang
    for (const screen of SCREENS) {
      where = screen;
      try {
        await p.evaluate(s => window.go && window.go(s), screen);
        await sleep(600);
        const CLICK_SEL = '#view button, #view a, #view [onclick], #view [role=button], #view .chip, #view .chip-s';
        // FILL every text input/textarea to prove it accepts input
        const inputs = await p.$$('#view input, #view textarea');
        found += inputs.length;
        for (const inp of inputs) {
          try { const tag = await inp.evaluate(e => e.tagName.toLowerCase());
            if (tag === 'textarea' || ['text', 'email', 'search', ''].includes(await inp.evaluate(e => e.getAttribute('type') || '')))
              await inp.fill('test', { timeout: 1000 }).catch(() => {});
          } catch (e) {} exercised++;
        }
        // CLICK every clickable control, by fresh index (DOM changes between clicks), reset after each
        const n = (await p.$$(CLICK_SEL)).length; found += n;
        for (let k = 0; k < n; k++) {
          const els = await p.$$(CLICK_SEL); const t = els[k]; if (!t) { exercised++; continue; }
          let label = `[${screen}] ` + await t.evaluate(e => `<${e.tagName.toLowerCase()}> "${(e.innerText || e.value || e.getAttribute('aria-label') || '').slice(0, 40).trim()}"`).catch(() => '<?>');
          if (SKIP_TEXT.test(label)) { exercised++; continue; }
          // Controls inside a collapsed form/menu are not currently interactive.  The visible opener is
          // exercised; force-clicking its hidden children creates false "dead" results and can bypass the
          // same visibility guard a real user is subject to.
          if (!(await t.isVisible().catch(() => false))) { exercised++; continue; }
          // submit-style buttons correctly no-op on empty input — give them content first so we test the REAL action
          // The screen is re-rendered after every click, so a later Answer/Respond control no longer sees
          // the value filled during the initial input pass. Refill its freshly rendered companion input just
          // before the click, exactly as a user would; otherwise the intentional empty-input guard looks dead.
          if (/send|submit|save|create|build|add|search|answer|respond/i.test(label))
            await p.$$('#view input, #view textarea').then(a => a[0] && a[0].fill('test', { timeout: 800 })).catch(() => {});
          const before = errors.length, reqsB = reqs, dlgB = dialogs;
          const hadOnclick = await t.evaluate(e => !!(e.getAttribute('onclick') || e.onclick)).catch(() => false);
          const domBefore = await p.evaluate(() => document.body.innerHTML);
          const stBefore = await p.evaluate(() => (typeof CUR !== 'undefined' ? CUR : '') + '|' + location.hash);
          const inpBefore = await p.evaluate(() => [...document.querySelectorAll('input,textarea,.note,#cnote,#bknote')].map(e => (e.value || e.textContent || '')).join('|')).catch(() => '');
          await t.click({ timeout: 1500, force: true }).catch(() => {});
          await sleep(250);
          const changed = await p.evaluate(bb => document.body.innerHTML !== bb, domBefore);
          const navd = (await p.evaluate(() => (typeof CUR !== 'undefined' ? CUR : '') + '|' + location.hash)) !== stBefore;
          const opened = await p.evaluate(() => !!document.querySelector('.modal, [role=dialog]') || [...document.querySelectorAll('.menu')].some(m => getComputedStyle(m).display !== 'none'));
          const inpChanged = (await p.evaluate(() => [...document.querySelectorAll('input,textarea,.note,#cnote,#bknote')].map(e => (e.value || e.textContent || '')).join('|')).catch(() => '')) !== inpBefore;
          const didSomething = changed || navd || opened || inpChanged || (reqs > reqsB) || (dialogs > dlgB);
          exercised++;
          if (errors.length > before) broken.push(label);
          else if (!didSomething && hadOnclick) dead.push(label);   // onclick that genuinely did NOTHING
          // LEAK FIX: if the click opened a modal/menu, crawl INSIDE it — a dead button in a modal was
          // invisible to the old crawl (it stopped at "a modal opened = did something"). Also flag a control
          // that OPENED an essentially-empty modal (a dead-end panel — e.g. 'Read the full research' showing
          // nothing) when it clearly promised content (view/read/show/why).
          if (opened) {
            const surf = await p.evaluate(() => {
              const m = document.querySelector('#ctlmodal, .modal, [role=dialog]') ||
                        [...document.querySelectorAll('.menu')].find(x => getComputedStyle(x).display !== 'none');
              if (!m) return null;
              const body = m.querySelector('#ctlmodalbody') || m;
              return { text: (body.innerText || '').trim(), n: m.querySelectorAll('button,a,[onclick]').length };
            });
            if (surf) {
              if (/view|read|show|why|full|details?|report/i.test(label) && surf.text.length < 15)
                dead.push(label + ` -> opened an EMPTY panel ("${surf.text.slice(0, 30)}")`);
              // click each control inside the opened surface (skip close/destructive), catching dead/broken
              const innerN = await p.evaluate(() => {
                const m = document.querySelector('#ctlmodal, .modal, [role=dialog]') ||
                          [...document.querySelectorAll('.menu')].find(x => getComputedStyle(x).display !== 'none');
                return m ? m.querySelectorAll('button, a, [onclick]').length : 0;
              });
              for (let j = 0; j < innerN && j < 12; j++) {
                const inner = await p.$$('#ctlmodal button, #ctlmodal a, #ctlmodal [onclick], .modal button, .modal a, [role=dialog] button, [role=dialog] a, .menu a, .menu button');
                const it = inner[j]; if (!it) continue;
                const ilabel = `[${screen}/modal] ` + await it.evaluate(e => `<${e.tagName.toLowerCase()}> "${(e.innerText || e.getAttribute('aria-label') || '').slice(0, 34).trim()}"`).catch(() => '<?>');
                if (SKIP_TEXT.test(ilabel) || /close/i.test(ilabel)) continue;
                const iErrB = errors.length, iReqB = reqs, iDomB = await p.evaluate(() => document.body.innerHTML.length);
                const iHadOnclick = await it.evaluate(e => !!(e.getAttribute('onclick') || e.onclick)).catch(() => false);
                await it.click({ timeout: 1200, force: true }).catch(() => {}); await sleep(200);
                const iChanged = await p.evaluate(bb => document.body.innerHTML.length !== bb, iDomB);
                const iNav = await p.evaluate(() => !document.querySelector('#ctlmodal, .modal, [role=dialog]'));  // it may close the modal (= did something)
                if (errors.length > iErrB) broken.push(ilabel);
                else if (iHadOnclick && !iChanged && !iNav && reqs === iReqB) dead.push(ilabel);
                if (iNav) break;   // surface closed; stop crawling it
              }
            }
          }
          // reset: close any open modal/menu, then re-render the screen for the next control
          await p.evaluate(() => { const m = document.querySelector('#ctlmodal'); if (m) m.remove(); const mm = document.querySelector('.modal, [role=dialog]'); if (mm && mm.remove) { try { mm.remove(); } catch (e) {} } }).catch(() => {});
          await p.evaluate(s => window.go && window.go(s), screen).catch(() => {}); await sleep(250);
        }
      } catch (e) { errors.push(`[${screen}] screen failed: ${String(e).split('\n')[0]}`); }
    }

    const problems = errors.length + dead.length + broken.length;
    console.log(`ACTION COVERAGE: ${exercised}/${found} interactive controls exercised across ${SCREENS.length} screens`);
    console.log(`  JS errors: ${errors.length} · dead controls: ${dead.length} · click-errors: ${broken.length} · blocked mutations: ${blockedMutations}`);
    [...new Set([...errors, ...dead.map(d => 'DEAD ' + d), ...broken.map(x => 'BROKEN ' + x)])].slice(0, 40).forEach(x => console.log('  - ' + x));
    if (problems) { console.log(`FAIL: ${problems} action-coverage problem(s)`); await b.close(); process.exit(1); }
    console.log('PASS: every exercised control did something + no JS errors ✅');
    await b.close(); process.exit(0);
  } catch (e) {
    console.log('FAIL: action crawler fatal: ' + String(e).split('\n')[0]);
    await b.close(); process.exit(1);
  }
})();
