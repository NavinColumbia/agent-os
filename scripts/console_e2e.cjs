// console_e2e.cjs — REAL browser click-through of the tenant console. Signs in with a token, visits
// EVERY screen (sidebar groups + top-bar + account menu), captures JS/console errors per screen, asserts
// each view renders non-empty, and screenshots. This is the "click through every screen" check.
//   NODE_PATH=<noupload>/node_modules node console_e2e.cjs <baseUrl> <token> <shotDir>
// Prints a JSON verdict; exits non-zero if any screen errored or rendered empty.
const { chromium } = require('playwright');

const SCREENS = [
  'controller', 'chat', 'build', 'agents', 'templates', 'orgs', 'portfolio', 'design', 'agentic',  // Direct + orgs
  'cockpit', 'projects', 'approvals', 'activity', // Operate
  'billing', 'providers', 'integrations',        // Business
  'notifications', 'help', 'team', 'settings', 'status', // secondary surfaces
];

(async () => {
  const [base, token, shotDir] = process.argv.slice(2);
  const results = [];
  const b = await chromium.launch();
  try {
    const ctx = await b.newContext({ viewport: { width: 1380, height: 900 }, deviceScaleFactor: 1 });
    const p = await ctx.newPage();
    // signup mode: token is 'signup' (or empty) -> exercise the REAL new-user signup flow in the browser.
    // returning-user mode: a real token is seeded. Default to signup so a broken front door is CAUGHT.
    const signupMode = !token || token === 'signup';
    if (!signupMode) await ctx.addInitScript(tok => { try { localStorage.setItem('aos_tenant', tok); } catch (e) {} }, token);

    let current = 'boot';
    const errors = [];
    p.on('console', m => { if (m.type() === 'error') errors.push(`[${current}] ${m.text()}`); });
    p.on('pageerror', e => errors.push(`[${current}] PAGEERROR ${String(e)}`));

    await p.goto(base + '/', { waitUntil: 'networkidle', timeout: 20000 });
    if (signupMode) {
      // a brand-new visitor: the sign-in screen must be visible, and they must be able to CREATE AN ACCOUNT
      current = 'signup';
      await p.waitForSelector('#signin', { state: 'visible', timeout: 8000 });
      if (shotDir) await p.screenshot({ path: `${shotDir}/console-00-signin.png` }).catch(() => {});
      await p.fill('#su_name', 'E2E CEO');
      await p.fill('#su_email', `e2e-${Date.now()}@example.com`);   // real email+password account
      await p.fill('#su_pw', 'e2e-password-123');
      await p.click('#su_signup button.pri');           // "Create account"
      // a NEW user should land in the app WITHOUT being shown-and-vanished a token (the bug we just fixed)
    }
    // the app shell must become visible — via signup (new user) or seeded token (returning)
    await p.waitForSelector('#app', { state: 'visible', timeout: 12000 });

    for (const screen of SCREENS) {
      current = screen;
      const before = errors.length;
      try {
        // drive the SPA router directly (same path the nav onclick uses) and wait for the view to fill
        await p.evaluate(s => window.go(s), screen);
        await p.waitForFunction(() => {
          const v = document.querySelector('#view');
          return v && v.innerText.trim().length > 0 && !/^loading…?$/i.test(v.innerText.trim());
        }, { timeout: 8000 });
        const text = (await p.locator('#view').innerText()).trim();
        const title = (await p.locator('#tbtitle').innerText().catch(() => '')).trim();
        const screenErrs = errors.length - before;
        const ok = text.length > 0 && screenErrs === 0;
        results.push({ screen, ok, title, chars: text.length, errors: screenErrs });
        if (shotDir) await p.screenshot({ path: `${shotDir}/console-${screen}.png` }).catch(() => {});
      } catch (e) {
        results.push({ screen, ok: false, error: String(e).split('\n')[0] });
      }
    }

    const failed = results.filter(r => !r.ok);
    const ok = failed.length === 0;
    console.log(JSON.stringify({
      ok, screens: results.length, failed: failed.length,
      results, errors: errors.slice(0, 20),
    }, null, 2));
    await b.close();
    process.exit(ok ? 0 : 1);
  } catch (e) {
    console.log(JSON.stringify({ ok: false, fatal: String(e) }));
    await b.close();
    process.exit(1);
  }
})();
