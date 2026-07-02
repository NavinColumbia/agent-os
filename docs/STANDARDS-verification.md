# Verification Standard — "prove it's broken, don't confirm it works"

## Why this exists
The failure that made the owner the real QA: verification was run by generic agents asked to "confirm X
passes." LLM agents are biased to *confirm what they're asked* — they run the happy path, see no crash,
and report "green." Meanwhile the actual product (open the research, read the options, review the
prototype) was dead-text the agent never clicked. The `qa-security` role standard was rigorous, but the
verification step never wore it. This standard makes every verification adversarial, evidence-backed, and
independently double-checked — so a "green" is trustworthy and the owner never has to re-QA.

## The rules (every verification — mine or the fleet's — MUST follow these)
1. **Default to BROKEN. Your job is to find what's wrong, not confirm it works.** Frame the task as
   "break this like a hostile, impatient real user; assume it's broken until proven otherwise." A
   verification that only tried the happy path has not verified anything.
2. **Wear the QA mandate.** Verification is done as `qa-security` (the adversarial/evidence role brief),
   not a generic agent. Apply the CRAFT + journey + timing + acceptance standards live.
3. **SEED every intermediate/deep state — don't stop at the entry shell.** The biggest hole: agents test
   signup/onboarding and stop. You MUST seed and exercise the mid-journey states the product produces —
   options-ready (with real options + a research doc), plan-drafted, prototype/design-created, a job
   running, an error, an empty/expired/denied state — and **open/click EVERY artifact** the system claims
   exists. "A message says 'options ready'" is NOT proof; clicking in and reading them is.
4. **Evidence or it didn't happen.** A PASS/green with no concrete, checkable evidence — screenshots,
   seeded-state results, specific per-flow assertions, the exact commands + their real output — is itself
   a **QA FAILURE**, not a pass. State exactly what was exercised, what was observed, and — required —
   **what was NOT covered.**
5. **Verifier-of-the-verifier.** No agent grades its own homework. A second, independent agent checks that
   the verification actually did the deep work and the evidence is real and comprehensive; if the evidence
   is thin or the deep states weren't seeded, the verification is REJECTED and re-run.
6. **Reproduce the exact user complaint.** When a defect is reported, reproduce it first, then re-run the
   original repro after the fix to confirm it's gone — not merely re-described.

## The standing gate
`scripts/test_artifact_review.py` (and the growing seeded e2e) drive the product into every deep state and
assert every produced artifact is actually reviewable/usable — the check a shell test can't do. A relayed
fleet "green" is never sufficient on its own; the seeded, evidence-backed, double-checked verdict is.

## Honest limit
LLM QA has a real pull toward rubber-stamping; this raises the floor dramatically but not to zero. So the
controller never launders a fleet "green" to the owner as truth — the verdict is only as good as its
evidence, and "what was NOT covered" is always stated.
