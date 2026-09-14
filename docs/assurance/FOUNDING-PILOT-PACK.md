# AI Release Assurance — founding pilot pack

Version: 2026-08-25
Status: ready for founder and customer review; not reviewed by a lawyer

## One-sentence offer

Give us a safe staging URL and your three most important browser journeys. Within one business day after access is ready, we deliver reproducible evidence, defect triage, and a plain-English release decision.

## Founding audit order form

**Customer:** `[legal name]`
**Provider:** `[your legal name/entity]`, operating as Agent OS
**App/revision:** `[staging URL and commit/release identifier]`
**Journeys:** `[journey 1]`, `[journey 2]`, `[journey 3]`
**Start condition:** safe access, test data, and written journey scope received
**Target delivery:** one business day after the start condition
**Fee:** $500 USD, paid before work starts; credited once toward the first $1,000 monthly plan purchased within 30 days
**Customer contact:** `[name/email]`
**Provider contact:** `[name/email]`

### Included

- One browser-based web application and one identified revision
- Up to three agreed critical user journeys
- Desktop and mobile viewport checks in Chromium
- Reproduction steps and screenshots for material findings
- Separation of likely product defects, environment failures, and test limitations
- One ship, ship-with-known-risk, or do-not-ship report
- One 30-minute handoff call

### Excluded unless separately agreed

- Fixing source code, retesting a new revision, load testing, penetration testing, certification, continuous monitoring, additional browsers, production data changes, or regulated-data handling
- A guarantee that the app has no defects, is secure, complies with law, or will remain available

## Plain-language pilot terms

1. **Safe access.** Customer supplies a dedicated temporary test account or guided session and synthetic data. Customer does not send passwords, tokens, production customer records, health data, payment-card data, government IDs, or other regulated data through the public form.
2. **Authorization.** Customer confirms it owns the app or has authority to authorize the agreed browser testing. Testing stays inside the written scope. No destructive, evasive, or exploitative security testing is performed.
3. **Timing.** The delivery clock starts only when scope and safe access are usable. A customer, service, or third-party outage pauses the target and is identified in the report. The one-business-day timing is a service target, not an uptime guarantee.
4. **Payment and cancellation.** The $500 fee is due before work. Customer may cancel before testing starts for a full refund. Once testing starts, the fee is non-refundable because capacity has been reserved and work performed. Provider may stop and refund unearned work if safe testing is impossible.
5. **Confidentiality and data.** Each party uses the other's non-public information only for the pilot and protects it with reasonable care. Published samples require separate written permission and redaction. Raw contact requests expire after 90 days unless converted; evidence retention is agreed in the order form and defaults to 30 days after delivery.
6. **Ownership.** Customer keeps ownership of its app and data. After payment, customer may use the delivered report internally and with its own clients. Provider keeps its pre-existing tools, methods, templates, and generalized learning that does not identify customer or reveal confidential information.
7. **Customer responsibility.** The report is decision support based on a limited scope and point-in-time revision. Customer remains responsible for deployment, security, compliance, backups, and business decisions.
8. **Warranty disclaimer.** Provider will perform the service professionally and in the agreed scope. Except for that promise, the service and report are provided as-is to the extent permitted by law.
9. **Liability boundary.** To the extent permitted by law, neither party is liable for indirect, special, or consequential damages. Provider's aggregate liability for the pilot is capped at the fee paid. This does not limit liability that applicable law does not allow the parties to limit.
10. **Law and disputes.** Governing law and venue must be filled in for the provider's actual jurisdiction before signature: `[jurisdiction]`. The parties first try in good faith to resolve a dispute directly.

**Customer signature/name/date:** `[fill]`
**Provider signature/name/date:** `[fill]`

## Security and privacy answer sheet

| Buyer question | Pilot answer |
| --- | --- |
| Do you need source access? | Not for the founding audit. We start with browser-visible behavior and agreed requirements. |
| Do you test production? | No. We require staging, a public demo, or a guided session unless a separately reviewed scope says otherwise. |
| How are credentials shared? | Never through the public intake. Use a dedicated temporary account through an agreed secure channel, then revoke it after delivery. |
| What data is allowed? | Synthetic test data. No regulated or live customer data. |
| What is retained? | The lead request expires after 90 days unless converted. Evidence defaults to deletion 30 days after delivery; the order form can shorten it. |
| Is evidence public? | No. A public sample requires separate written permission and redaction. |
| Is this a penetration test or certification? | No. It is functional browser release assurance for an agreed point-in-time scope. |
| What if your automation is wrong? | The report identifies evidence, limitations, and ambiguous requirements. A human-readable verdict does not silently turn uncertain evidence into a pass. |

## Qualification call — 15 minutes

1. What release are you deciding on, and by when?
2. Which three user failures would cause refunds, churn, support load, or reputational harm?
3. What automated and manual QA already exists, and what does it fail to tell you?
4. Is there a safe staging environment with synthetic data and a revocable test account?
5. Who can approve the scope and $500 fee today?

Disqualify the pilot if there is no authorized staging access, the buyer requires regulated/production data, the request is primarily offensive security work, the buyer cannot name a release decision, or no one owns the buying decision.

## Delivery checklist

- Confirm payment, authorization, revision, three journeys, access method, retention, and delivery clock
- Record the starting state and environment limitations
- Run each journey and preserve exact evidence lineage
- Reproduce material failures once without broadening scope
- Label every item product defect, environment failure, test limitation, or ambiguous requirement
- Produce the decision using the template below
- Revoke/delete temporary access material and schedule evidence deletion
- Conduct the handoff and ask for the monthly conversion or a referral
