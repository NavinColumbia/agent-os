# Acceptance / Dogfood Standard — "test it like a demanding real user, not a checklist"

## Why this exists
Our other QA lenses (journey, human-paced/timing, craft, a11y, functional) all test **what already
exists against our own spec**. They cannot catch the class of issue the OWNER keeps catching for us:

1. **Missing capabilities** — a whole thing a real product needs that simply isn't built (email
   verification, an ETA + ping when async work finishes, model routing for fast replies, password reset).
2. **Edge / first-run journeys** — the brand-new 0-org user's very first click; the empty/expired/denied
   path nobody set up test data for.
3. **Product judgment vs best-in-class** — "this single-line box should be a multi-line textarea like
   ChatGPT/Claude"; "saying 'I'll come back' with no ETA/notification is unacceptable"; "this copy is
   confusing." Comparison against what ChatGPT, Claude, Stripe, Linear, Vercel, Notion actually do.

A checklist test passes while the product is still frustrating, incomplete, or amateur. The owner has been
the only one applying this lens. That is the gap this standard closes.

## The mandate (for QA, reviewers, and the dogfood pass)
Do NOT only verify the happy path and the screens that exist. **Role-play a skeptical, experienced user
with high standards pursuing a REAL goal end-to-end**, and for each goal ask:

- **Can I actually accomplish this goal start to finish?** Sign up → verify → create a company →
  describe a product → get it built → see progress + an ETA + a ping when done → review the result →
  pay/upgrade. Walk the WHOLE arc, including the very first run with zero data.
- **What's MISSING that a real product would have?** (verification, reset, undo, search, empty states,
  confirmations, ETAs, notifications, keyboard, mobile, export, error recovery, rate limits, onboarding.)
- **Where would I get frustrated or churn?** Slowness with no feedback, a promise with no follow-through,
  a dead end, a scary error instead of a guiding next step, a confusing label, an action that lies about
  its result ("connected"/"sent" when it didn't).
- **How does this compare to the best products I use?** Name the specific better behavior
  (ChatGPT's textarea + streaming + stop; Stripe's connect flow; Linear's empty states; a real "we
  emailed you a code"). If we're materially worse, that's a finding.
- **Did the system keep its promises?** If it said "I'll research and come back," does it give an ETA,
  show progress, and actually PING when done, with an SLA/escalation if it stalls?

Every finding gets a severity (blocker/high/med/low), the goal it broke, and the concrete fix — and is
filed so an owner is responsible (findings.py), not left in a head.

## How it's enforced
- A standing **dogfood pass** (`scripts/dogfood.py`) runs a fleet of demanding-user agents over the real
  goals on the live app, benchmarks against best-in-class, and produces a prioritized backlog. Run it
  proactively (and on a schedule) so it finds these BEFORE the owner does.
- This is the `acceptance-dogfood` quality lens (docs/quality-lenses.yaml): STANDARD (this doc) + ROLE
  mandate (qa-security/reviewer) + GUARD (the dogfood pass surfaces zero unresolved blocker/high before a
  release is called "ready").

## Honest limit
No automated pass fully replaces a real human's product taste. The goal is to shrink the gap so the owner
finds *dramatically* fewer things — not zero — and so the embarrassing, basic, or missing-capability ones
are caught by us first.
