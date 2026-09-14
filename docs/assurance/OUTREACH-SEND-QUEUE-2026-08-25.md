# Release Assurance — verified send queue

Prepared: 2026-08-25
Commercial status: **0 sent · 0 replies · 0 calls · 0 proposals · 0 paid · $0 revenue**

Public offer: <https://agent-os-release-assurance.artmusicasia.chatgpt.site/>
Honest sample: <https://agent-os-release-assurance.artmusicasia.chatgpt.site/sample>

The offer is a public production deployment. The sender still rechecks both URLs immediately before every
delivery. Every item below is still **NOT SENT**. Do not count a drafted message as outreach.

The four direct-email messages are also encoded in `outreach-send-queue.json`. Once a real mail transport is
configured, `PYTHONPATH=scripts .venv/bin/python scripts/assurance_outreach.py send <target-id>` checks both
public links, reserves one idempotent delivery attempt, sends the individual message, and records the receipt.
An interrupted `sending` record does not auto-replay; reconcile it first so a transport crash cannot duplicate
mail. Lunen remains a form/LinkedIn task and is not silently converted into a guessed email address. N71's
`discover@n71.ai` address is a public business channel posted by the company on LinkedIn.

## 1 — A Cubic / Aymen Krifa

- Channel: `aymenkrifa@gmail.com` (published on <https://aymenkrifa.com/>)
- Why now: Aymen's current site says A Cubic delivers commissioned AI products end to end. Release Assurance
  can be a white-label evidence layer for client handoff, which is more commercially relevant than the older
  Kit for AI hypothesis.
- Subject: `A one-day release evidence layer for A Cubic builds`

Hi Aymen — I saw that A Cubic is shipping commissioned AI products end to end. The awkward last-mile question
for that work is usually not “did the code build?” but “what browser evidence can we hand the client before
release?” I run a fixed-scope Release Assurance audit: three agreed user journeys, reproducible evidence,
defect triage, and a ship/no-ship decision in one business day. The founding audit is $500, and it can sit
behind your client delivery rather than compete with it. Here is the redacted sample:
<https://agent-os-release-assurance.artmusicasia.chatgpt.site/sample>. Would it be useful to scope one current
A Cubic delivery?

— Agent OS Release Assurance

## 2 — Yasmine

- Channel: `hello@yasmine.works` (published on <https://yasmine.works/contact>)
- Why now: the public product describes tenant-scoped runtime identity, encrypted state, Slack actions, and
  credentials. Those create visible isolation, approval, and audit journeys buyers need to trust.
- Subject: `Three release journeys for Yasmine's Slack trust boundary`

Hi Yasmine team — your Slack coworker promise depends on boundaries users can actually see: one tenant's state
stays isolated, risky work pauses for approval, and the action record never leaks a credential. I run a narrow
Release Assurance audit for AI web products. We agree on three browser-visible journeys and I return
reproducible evidence plus an honest ship/no-ship decision in one business day. For Yasmine I would scope
workspace install/isolation, a staged action through approval, and the resulting audit/redaction view using
synthetic data. The founding audit is $500. Sample:
<https://agent-os-release-assurance.artmusicasia.chatgpt.site/sample>. Is there a current release where that
evidence would help?

— Agent OS Release Assurance

## 3 — Leaping AI

- Channel: `contact@leapingai.com` (published on <https://leapingai.com/privacy/imprint>)
- Why now: Leaping's current product and August 2026 material describe multi-channel campaigns that combine
  calls and texts and pause automatically after a response. Consent and stop-state propagation are directly
  testable release risks.
- Subject: `Release evidence for Leaping's cross-channel stop state`

Hi Leaping team — the trust-critical part of a multi-channel call/text campaign is what happens after the lead
responds: every remaining scheduled contact should stop, the campaign state should stay consistent, and the
operator should be able to prove why. I run a fixed-scope Release Assurance audit covering three agreed
browser journeys with screenshots, reproduction steps, and a ship/no-ship decision in one business day. I
would scope synthetic campaign creation, a response/stop transition, and operator evidence across the two
channels. The founding audit is $500. Redacted sample:
<https://agent-os-release-assurance.artmusicasia.chatgpt.site/sample>. Useful for an upcoming release?

— Agent OS Release Assurance

## 4 — Lunen

- Channel: early-access form at <https://lunen.ai/>; Product Hunt maker Mike Rudolph at
  <https://www.producthunt.com/products/lunen-ai>
- Why now: Lunen publicly promises per-tool allow/approve policies, paused writes, agent identity, and an
  exportable audit record that includes the approval decision.
- Subject: `Test the allow → approve → audit-export boundary in Lunen`

Hi Mike — Lunen's per-tool policy is a concrete promise worth proving end to end. I run a one-business-day
Release Assurance audit for three browser journeys with reproducible evidence and an honest release decision.
For Lunen I would scope an allowed read, a write that pauses until the right approver acts, and the exported
audit record showing the policy plus approval identity. The founding audit is $500. Here is a redacted sample:
<https://agent-os-release-assurance.artmusicasia.chatgpt.site/sample>. Would that be useful for the current
early-access revision?

— Agent OS Release Assurance

## 5 — N71

- Channel: `discover@n71.ai`, published by N71 on its LinkedIn company page; founding-team member Mira
  Charkawi also publicly discusses the product:
  <https://www.linkedin.com/posts/miracharkawi_ive-joined-the-founding-team-at-n71ai-to-activity-7470079520293785600-jYMz>
- Why now: N71 publicly describes source-linked facts and a company model built from competing, changing
  realities. Source authorization and stale/conflicting-fact handling are release-significant journeys.
- Subject: `Three evidence journeys for N71's source-linked company brain`

Hi Mira — your description of N71 makes the hard promise unusually clear: facts remain tied to the source even
when company realities conflict and change. I run a fixed-scope Release Assurance audit for AI products. We
choose three critical browser journeys and I return reproducible evidence, defect triage, and a ship/no-ship
decision in one business day. For N71 I would scope source connection, role-scoped retrieval, and a synthetic
stale/conflicting-fact update. The founding audit is $500. Sample:
<https://agent-os-release-assurance.artmusicasia.chatgpt.site/sample>. Worth scoping for a current release?

— Agent OS Release Assurance

## Send gate

Before the first message, configure a real sender address and mail transport (`AOS_SMTP_FROM` plus
`AOS_SMTP_URL` or `SENDGRID_API_KEY`). A $500 Stripe Payment Link in `AOS_ASSURANCE_PAYMENT_URL` lets qualified
buyers pay immediately; without it, the public intake truthfully promises email follow-up. Fill the provider's
legal name and jurisdiction in the pilot order form before accepting payment. Send individually, record the
provider acceptance response, and stop after two unanswered follow-ups.
