// web_smoke.cjs — generic web-app QA: load the app in a real headless browser, assert it actually
// renders, capture console/page errors, and screenshot it. Used by factory.run_web_qa for the web
// product line. Run with NODE_PATH pointed at a node_modules that has playwright:
//   NODE_PATH=<dir>/node_modules node web_smoke.cjs <url> <screenshot.png>
// Prints a JSON verdict and exits non-zero on failure (so the factory's QA gate sees a real signal).
const { chromium } = require('playwright');
(async () => {
  const [url, shot] = process.argv.slice(2);
  const errors = [];
  let status = 0, bodyLen = 0;
  const b = await chromium.launch();
  try {
    const ctx = await b.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 });
    const p = await ctx.newPage();
    p.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    p.on('pageerror', e => errors.push(String(e)));
    const r = await p.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
    status = r ? r.status() : 0;
    bodyLen = (await p.locator('body').innerText()).trim().length;
    if (shot) await p.screenshot({ path: shot, fullPage: true });
  } catch (e) {
    errors.push('nav:' + e.message);
  } finally {
    await b.close();
  }
  const ok = status >= 200 && status < 400 && bodyLen > 10 && errors.length === 0;
  console.log(JSON.stringify({ ok, status, bodyLen, errors: errors.slice(0, 6) }));
  process.exit(ok ? 0 : 1);
})();
