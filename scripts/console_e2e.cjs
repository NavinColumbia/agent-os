// console_e2e.cjs — REAL browser click-through of the tenant console. Signs in with a token, visits
// EVERY screen (sidebar groups + top-bar + account menu), captures JS/console errors per screen, asserts
// each view renders non-empty, and screenshots. This is the "click through every screen" check.
//   NODE_PATH=<noupload>/node_modules node console_e2e.cjs <baseUrl> <token> <shotDir>
// Prints a JSON verdict; exits non-zero if any screen errored or rendered empty.
//
// IDLE-DWELL / no-disruption regression class (machine-speed tests race past this; humans hit it):
//   1) signup-idle-dwell — type into #su_name, sit idle 12s, assert we're NOT bounced off the
//      create-account tab and the typed value survives (the front-door bug we just fixed).
//   2) cockpit-dwell      — sit on the cockpit 8s after sign-in, assert no unexpected navigation.
//   3) timer-leak         — login/logout 2x; a window-level interval registry must show NO leaked
//      polling timers (catches "duplicate poller" leaks that only surface across sessions).
// These run alongside the per-screen checks and flip the overall verdict to false on failure.
const { chromium } = require('playwright');

const SCREENS = [
  'controller', 'chat', 'build', 'agents', 'templates', 'orgs', 'portfolio', 'design', 'agentic',  // Direct + orgs
  'cockpit', 'projects', 'approvals', 'activity', // Operate
  'billing', 'providers', 'integrations',        // Business
  'notifications', 'help', 'team', 'settings', 'status', // secondary surfaces
];

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const [base, token, shotDir] = process.argv.slice(2);
  const results = [];
  const b = await chromium.launch();
  try {
    const ctx = await b.newContext({ viewport: { width: 1380, height: 900 }, deviceScaleFactor: 1 });

    // Instrument timers BEFORE any page script runs: a window-level registry of *live* intervals.
    // setInterval adds the real id; clearInterval removes it. window.__ivalCount() = # of intervals
    // the page believes are running. A poller that gets re-created without the old one being cleared
    // (the classic login/logout leak) shows up here as a monotonically growing count — invisible to
    // the app's own `window.CTLPOLL`-style single-slot bookkeeping, but caught dead by this registry.
    await ctx.addInitScript(() => {
      try {
        window.__ivals = new Set();
        const _si = window.setInterval.bind(window);
        const _ci = window.clearInterval.bind(window);
        window.setInterval = function (...a) { const id = _si(...a); window.__ivals.add(id); return id; };
        window.clearInterval = function (id) { window.__ivals.delete(id); return _ci(id); };
        window.__ivalCount = () => window.__ivals.size;
      } catch (e) {}
    });

    const p = await ctx.newPage();
    // signup mode: token is 'signup' (or empty) -> exercise the REAL new-user signup flow in the browser.
    // returning-user mode: a real token is seeded. Default to signup so a broken front door is CAUGHT.
    const signupMode = !token || token === 'signup';
    if (!signupMode) await ctx.addInitScript(tok => { try { localStorage.setItem('aos_tenant', tok); } catch (e) {} }, token);

    // Credentials for the new account — reused by the login/logout timer-leak test below.
    const e2eEmail = `e2e-${Date.now()}@example.com`;
    const e2ePw = 'e2e-password-123';

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

      // STANDING product-quality assertions on the create-account screen. These are permanent
      // guards: if a basic form/a11y affordance regresses the WHOLE suite fails. We check the
      // create-account tab as freshly loaded (su_signup is the default-visible form, su_signin is
      // display:none). Requirements asserted:
      //   - a confirm-password field exists (#su_pw2, type=password)
      //   - a show/hide-password toggle exists (a .pwtoggle button in the create-account form)
      //   - password inputs carry the right autocomplete hints (new-password on create, the
      //     sign-in password gets current-password) so password managers behave
      //   - every VISIBLE input has an accessible name: <label for=…>, a wrapping <label>,
      //     aria-label, or aria-labelledby (placeholder alone does NOT count)
      //   - the first field of the form is autofocused (cursor lands ready to type)
      current = 'create-account-quality';
      const caq = await p.evaluate(() => {
        const $ = s => document.querySelector(s);
        const visible = el => !!el && getComputedStyle(el).display !== 'none' && el.offsetParent !== null;
        const named = el => !!(el.getAttribute('aria-label') || el.getAttribute('aria-labelledby')
          || el.closest('label') || (el.id && document.querySelector(`label[for="${el.id}"]`)));
        const ac = (sel, want) => { const e = $(sel); return !!e && e.getAttribute('autocomplete') === want; };
        const inputs = [...document.querySelectorAll('#signin input')].filter(visible);
        const unlabeled = inputs.filter(i => !named(i)).map(i => i.id || i.outerHTML.slice(0, 60));
        const first = inputs[0] || null;
        return {
          confirmPw: !!($('#su_pw2') && $('#su_pw2').type === 'password'),
          toggle: document.querySelectorAll('#su_signup .pwtoggle, #su_signup button[aria-pressed]').length >= 1,
          acCreatePw: ac('#su_pw', 'new-password'),
          acConfirmPw: ac('#su_pw2', 'new-password'),
          acSignInPw: ac('#si_pw', 'current-password'),
          allLabeled: unlabeled.length === 0,
          unlabeled,
          autofocusFirst: !!(first && first.hasAttribute('autofocus')),
          firstId: first && first.id,
        };
      });
      const caqOk = caq.confirmPw && caq.toggle && caq.acCreatePw && caq.acConfirmPw
        && caq.acSignInPw && caq.allLabeled && caq.autofocusFirst;
      results.push({ screen: 'create-account-quality', ok: caqOk, detail: caq });

      // (1) IDLE-DWELL / no-disruption regression: go to the create-account tab, type a name, then
      //     SIT IDLE for 12s. The app must NOT bounce us to the sign-in tab, hide the create-account
      //     form, reveal the app shell, or wipe what we typed. This is a permanent regression test for
      //     the bug where an idle signed-out user got yanked off the signup form mid-type.
      current = 'signup-idle-dwell';
      await p.evaluate(() => suTab('up'));
      await p.fill('#su_name', 'E2E CEO');
      await sleep(12000);
      const dwell = await p.evaluate(() => ({
        signupTabVisible: getComputedStyle(document.querySelector('#su_signup')).display !== 'none',
        signinTabVisible: getComputedStyle(document.querySelector('#su_signin')).display !== 'none',
        nameVal: document.querySelector('#su_name').value,
        appVisible: getComputedStyle(document.querySelector('#app')).display !== 'none',
        signinScreenVisible: getComputedStyle(document.querySelector('#signin')).display !== 'none',
      }));
      const dwellOk = dwell.signupTabVisible && !dwell.signinTabVisible
        && dwell.nameVal === 'E2E CEO' && !dwell.appVisible && dwell.signinScreenVisible;
      results.push({ screen: 'signup-idle-dwell', ok: dwellOk, detail: dwell });

      // now actually create the account (su_name already holds 'E2E CEO')
      current = 'signup';
      await p.fill('#su_email', e2eEmail);   // real email+password account
      await p.fill('#su_pw', e2ePw);
      await p.fill('#su_pw2', e2ePw);        // confirm-password must match or signUp() rejects
      await p.click('#su_signup button.pri');           // "Create account"
      // a NEW user should land in the app WITHOUT being shown-and-vanished a token (the bug we just fixed)
    }
    // the app shell must become visible — via signup (new user) or seeded token (returning)
    await p.waitForSelector('#app', { state: 'visible', timeout: 12000 });

    // (2) IDLE-DWELL on the cockpit: after sign-in, sit 8s and assert nothing navigates us away.
    //     boot() does a one-time post-login redirect (a brand-new 0-org account is routed to 'orgs'),
    //     so we let that land FIRST, then pin to the cockpit and confirm it sticks before dwelling.
    //     refreshView() legitimately re-renders content in the background, so we assert on NAVIGATION
    //     stability (route + title + shell visibility) rather than byte-identical DOM.
    current = 'cockpit-dwell';
    await sleep(2500);                                       // let boot()'s one-time redirect settle
    await p.evaluate(() => window.go('cockpit'));
    await p.waitForFunction(() => typeof CUR !== 'undefined' && CUR === 'cockpit', { timeout: 8000 }).catch(() => {});
    await p.waitForFunction(() => {
      const v = document.querySelector('#view');
      return v && v.innerText.trim().length > 0 && !/^loading…?$/i.test(v.innerText.trim());
    }, { timeout: 8000 }).catch(() => {});
    const t0 = await p.evaluate(() => document.querySelector('#tbtitle').innerText.trim());
    await sleep(8000);
    const stab = await p.evaluate(() => ({
      cur: (typeof CUR !== 'undefined') ? CUR : null,
      title: document.querySelector('#tbtitle').innerText.trim(),
      appVisible: getComputedStyle(document.querySelector('#app')).display !== 'none',
      signinVisible: getComputedStyle(document.querySelector('#signin')).display !== 'none',
    }));
    // no unexpected navigation: we settled on the cockpit and we're STILL on the cockpit after 8s idle.
    const stableOk = stab.cur === 'cockpit' && stab.title === t0 && stab.appVisible && !stab.signinVisible;
    results.push({ screen: 'cockpit-dwell', ok: stableOk, detail: { ...stab, expectedTitle: t0 } });

    // LIGHT a11y guard on the signed-in app: every visible TEXT-ish input on the primary
    // working screens must have an accessible name (label / aria-label / aria-labelledby). A
    // placeholder is NOT a label — it vanishes on type and screen readers skip it. This is a
    // STANDING assertion: a new placeholder-only input on any of these screens fails the suite.
    current = 'app-a11y';
    const a11yScreens = ['controller', 'chat', 'orgs', 'agentic', 'help', 'settings', 'build', 'agents'];
    const a11yBad = [];
    for (const s of a11yScreens) {
      await p.evaluate(x => window.go(x), s);
      await p.waitForFunction(() => {
        const v = document.querySelector('#view');
        return v && v.innerText.trim().length > 0 && !/^loading…?$/i.test(v.innerText.trim());
      }, { timeout: 8000 }).catch(() => {});
      const bad = await p.evaluate(() => {
        const visible = el => getComputedStyle(el).display !== 'none' && el.offsetParent !== null;
        const named = el => !!(el.getAttribute('aria-label') || el.getAttribute('aria-labelledby')
          || el.closest('label') || (el.id && document.querySelector(`label[for="${el.id}"]`)));
        const textish = i => ['text', 'email', 'password', 'search', 'url', 'tel', 'number', '']
          .includes((i.getAttribute('type') || '').toLowerCase());
        return [...document.querySelectorAll('#view input')]
          .filter(i => textish(i) && visible(i) && !named(i)).map(i => i.id || 'anon');
      });
      if (bad.length) a11yBad.push({ screen: s, unlabeled: bad });
    }
    results.push({ screen: 'app-a11y', ok: a11yBad.length === 0, detail: { unlabeled: a11yBad } });

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

    // (3) TIMER-LEAK regression: login/logout 2x and assert no polling intervals leak across sessions.
    //     Baseline = # of live intervals while idle on the cockpit (the per-screen pollers, e.g.
    //     controller/chat, self-clear on navigation, so settle first). After each logout+login the
    //     count must NOT exceed the baseline — a duplicate/orphaned poller would push it up.
    //     This phase needs real credentials, so it only runs in signup mode.
    if (signupMode) {
      current = 'timer-leak';
      try {
        await p.evaluate(() => window.go('cockpit'));
        await sleep(7000);                                   // let any per-screen pollers self-clear
        const baseline = await p.evaluate(() => window.__ivalCount());
        const cycles = [];
        for (let i = 0; i < 2; i++) {
          await p.evaluate(() => signOut());
          await p.waitForSelector('#signin', { state: 'visible', timeout: 8000 });
          await sleep(500);
          const afterOut = await p.evaluate(() => window.__ivalCount());
          await p.evaluate(() => suTab('in'));
          await p.fill('#si_email', e2eEmail);
          await p.fill('#si_pw', e2ePw);
          await p.click('#su_signin button.pri');
          await p.waitForSelector('#app', { state: 'visible', timeout: 12000 });
          await p.evaluate(() => window.go('cockpit'));
          await sleep(7000);
          const afterIn = await p.evaluate(() => window.__ivalCount());
          cycles.push({ afterOut, afterIn });
        }
        const finalCount = await p.evaluate(() => window.__ivalCount());
        // baseline must be real (>=1 poller running) AND no session may exceed it (no leak).
        const leakOk = baseline >= 1 && finalCount <= baseline && cycles.every(c => c.afterIn <= baseline);
        results.push({ screen: 'timer-leak', ok: leakOk, detail: { baseline, finalCount, cycles } });
      } catch (e) {
        results.push({ screen: 'timer-leak', ok: false, error: String(e).split('\n')[0] });
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
