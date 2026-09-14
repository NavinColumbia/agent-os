# Release Assurance live status

Updated: 2026-08-25

- Production public site: <https://agent-os-release-assurance.artmusicasia.chatgpt.site>
- Local sales boundary: `http://127.0.0.1:8100`
- Production routes: offer, sample report, two redacted PNG evidence assets, privacy, pilot terms, and rate-limited D1-backed intake
- Production health: `/api/health`; version 2 also adds response security headers and durable founder-notification state
- Agent OS tenant console: not routed through this public boundary
- Checkout: email follow-up until a $500 Stripe Payment Link is configured
- Saved/deployed Sites version: `3`, source commit `3a0b87e9740fe930b5b33a38c9c9166bc2655a65`
- Production verification: offer/evidence/legal/health routes return HTTP 200, security headers are present, invalid intake fails closed, and the public sample now reports the completed 12/12 gate instead of the superseded pending-campaign state. Labeled synthetic request `arp_e4e9cd7772414590` previously persisted to D1 with a durable `unconfigured` notification receipt.
- Production runtime configuration: empty. The Site continues capturing leads safely, but Stripe redirect and SendGrid founder alerts remain disabled until their environment values are supplied and the saved version is redeployed.

## Dog-app proof handoff

Durable source run `4017` completed all 12 story verdicts. Report-only continuation `4184` reused all 12 exact
grounded receipts and launched zero browser explorers. Product QA gate `1138` is **ALL CLEAR: 12/12 passed, 0
open bugs, 0 blocking findings**. The current product passes 174/174 automated tests at executable revision
`3f6b4aa5bc82d6f33c8c874dc7954515966499656ef9a6e33246e426316a1a20`.

The final reporting defect was in adjudication composition: an immutable explorer receipt retained a finding
count after its exact false-positive resolution. The reducer now composes those two durable facts only when
every finding ID matches a same-story `verified_false_positive` disposition; other supersession dispositions
fail closed. Selectively preserved revision evidence is carried through the coordinator's audited result
ledger, and report-only continuation joins without generic hires or browser work.

The proof is complete and is no longer an active workstream. The public report is an honestly redacted
three-journey extract of the completed twelve-story campaign. Selling the fixed-scope audit is the only
immediate operating priority.

The final Agent OS suite passes 1,025 tests with one intentional skip and zero failures. The completion audit
also found and fixed two live-test interference defects: the DB sentinel now measures time actually spent idle
instead of the age of the backend's prior query, and the scheduler-monitor fixture is fenced from the live
ticker while observed. No QA, browser, or pytest workers remained after verification.

The prior Cloudflare Quick Tunnel remains a temporary fallback only. Buyer-facing outreach must use the Sites
production URL so a local process or hostname rotation cannot break mailed links.

## Health checks

```bash
curl -fsS http://127.0.0.1:8100/health
curl -fsS https://agent-os-release-assurance.artmusicasia.chatgpt.site/
curl -fsS https://agent-os-release-assurance.artmusicasia.chatgpt.site/sample
curl -fsS https://agent-os-release-assurance.artmusicasia.chatgpt.site/api/health
cd sites/release-assurance && npm run verify:production -- https://agent-os-release-assurance.artmusicasia.chatgpt.site
docker inspect agentos-assurance-preview --format '{{.State.Status}} {{.HostConfig.RestartPolicy.Name}}'
```

## Disable the temporary public preview

```bash
docker stop agentos-assurance-preview
```
