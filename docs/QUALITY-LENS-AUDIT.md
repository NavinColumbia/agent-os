# Quality-Lens Gap Audit — "Why does our QA pipeline keep missing whole classes of issues?"

Status: AUDIT (no fixes applied). Grounded in the live repo as of 2026-06-29.

## The one-sentence answer

A lens is only reliably caught when it has the **full triad**: a written **STANDARD**, a **role
must_never/DoD** that mandates it, AND a **standing automated guard that fails CLOSED**. The two lenses
we already "fixed" (user-journey, timing/idle-dwell) each have all three. Every lens we keep missing has
**at most role prose** and **no standard and no guard** — so an LLM QA agent does the bullets that a guard
will red-flag and *skims* the rest, because nothing fails when craft/a11y/responsive/content are skipped.
We don't have a QC gap; we have a **guard gap**: quality lenses live as aspirational sentences inside three
on-demand QA-role manifests, not as enforced gates in the factory line.

## What the pipeline actually is

`scripts/factory.py` runs one linear product line:

> **SPEC** (product-manager) → **BUILD** (builder) → **QA** (qa-security) → **REVIEW** (reviewer) →
> **VERIFY** (adversarial, `scripts/verify.py`) → **LAUNCH** (tech-lead)

There is **no DESIGN / UX / A11Y / CONTENT / PERF stage**. `design-ux`, `design-systems`,
`ux-researcher`, `copywriter`, `localization-translator`, `privacy-dpo`, `devops-sre`, `database-admin`,
`technical-writer` all exist as rich role manifests in `~/projects/control-plane/roles/` but are
`standing: on-demand` and **never invoked by the factory line**. They are bench players, not gates.

### Inventory of what exists

- **Standards** (`agent-os/docs/`): only two — `STANDARDS-qa.md` (real-journey + human-paced/idle-dwell
  timing) and `STANDARDS-planning.md` (impact map + edge/empty/error/loading must be in the plan). That's it.
- **Standing automated guards**:
  - `scripts/console_e2e.cjs` — real browser, clicks every console screen, asserts non-empty render +
    per-screen console-error count, **idle-dwell** (signup 12s, cockpit 8s) and **timer-leak** (login/logout
    x2 interval registry). Runs at a **single viewport 1380x900**. No a11y, no contrast, no ARIA, no
    responsive matrix, no visual-diff, no forced empty/error-state assertion.
  - `scripts/security_scan.py` — hardcoded-secret / git-tracked-secret / bind scan (platform source).
  - `scripts/verify.py` — tiered adversarial correctness tests (`tests/adversarial/`).
  - `scripts/selftest.sh` — 104 checks, almost all about **agent-os the platform** (governance, billing,
    consent gate, GDPR delete, console click-through), not about a built product's craft.
  - `scripts/test_*.py` — callsite-wiring, enforcement-consistency, governance-wiring (platform invariants).
  - `control-plane/checklists/pre-launch.md` — human launch gate (security, privacy, legal, observability,
    rollback, terms/privacy pages). A **manual checklist**, not an automated guard.

## The audit table

| Lens | Standard? | Role mandate? | Standing guard? | GAP severity | Concrete what-to-add |
|---|---|---|---|---|---|
| functional-correctness | Partial — `STANDARDS-qa.md` ("renders ≠ verified") | Yes — qa-security/reviewer/sdet DoD | Yes — pytest `test_core`, `console_e2e` render, `verify.py` | **none** | Keep. |
| user-journey / E2E | **Yes** — `STANDARDS-qa.md` (full signup→persist→re-auth journey) | Yes — all 3 QA roles DoD | **Yes** — `console_e2e.cjs` drives real signup + factory E2EQA | **none** | Keep; this is the model the others should copy. |
| timing / concurrency / idle-disruption | **Yes** — `STANDARDS-qa.md` (entire doc) | Yes — explicit `must_never` in all 3 | **Yes** — `console_e2e` idle-dwell + timer-leak registry | **none** | Keep; fully closed (the lens we just added). |
| **product-craft / UX-completeness** (form best-practice, expected affordances) | **No** | Only `design-ux` (on-demand, not in line); QA roles say "states" not "affordances" | **No** | **absent** | `STANDARDS-ux-craft.md` + a DESIGN/UX gate in factory.py + a heuristic guard (forms have labels/autocomplete/enter-submit/inline-validation/disabled-while-submitting; primary action present). **← the lens the user just hit.** |
| **accessibility** (WCAG / keyboard / contrast / ARIA) | **No** (only one sentence inside QA bullets + design-ux summary) | Prose-only — 1 bullet each in qa/reviewer/sdet; design-ux owns WCAG but on-demand | **No** — `console_e2e` has zero axe/contrast/ARIA/keyboard assertions | **major** | `STANDARDS-a11y.md` (WCAG 2.2 AA) + add `@axe-core/playwright` to `console_e2e.cjs` failing closed on serious/critical + a keyboard-only tab-traversal assertion. |
| visual / design-system consistency | **No** | `design-systems` (on-demand, not in line) | **No** — `console_e2e` screenshots but never asserts on them | **absent** | Token/component standard + visual-regression baseline (assert on the screenshots already captured). |
| responsive / mobile | **No** | Prose-only — "mobile viewport" 1 bullet in qa/sdet; `mobile-engineer` on-demand | **Weak** — `console_e2e` tests a single 1380x900 desktop width | **major** | Run `console_e2e` across a viewport matrix (e.g. 390/768/1380) and assert no overflow/clipping/hidden primary action at each. |
| content / microcopy quality | **No** | `copywriter`/`content-strategist` exist, on-demand, not in line | **No** | **absent** | Microcopy standard (voice, error-message rules, no lorem/placeholder/TODO) + a lint guard that fails on placeholder text and empty button/aria labels. |
| error-handling / resilience | Partial — `STANDARDS-planning.md` §3 | Yes — qa/reviewer/sdet DoD (error/slow-net/unauthorized) | Partial — `verify.py` adversarial; no forced-error UI assertion | **minor** | Add an explicit "kill backend / 500 / timeout → assert intended UI not crash/blank" step to `console_e2e`. |
| empty / loading / error states | Partial — `STANDARDS-planning.md` §3 | Yes — explicit in all 3 QA DoD | **No automated assertion** — `console_e2e` only checks non-empty render | **major** | Guard that loads each surface with zero rows / pending / failed fetch and asserts a real empty/loading/error UI (not blank, not infinite spinner). |
| security | **Yes** — `checklists/security-review.md` + pre-launch | Yes — security-appsec, qa, reviewer, redteam | **Yes** — `security_scan.py` + selftest + qa diff-grep for egress/secrets | **none** | Keep; extend secret scan to the built product repo, not just platform source. |
| performance / load | **No** | Prose-only — reviewer notes N+1/race; `devops-sre` on-demand | **No** | **major** | Perf-budget standard + a guard (Lighthouse perf score / TTI budget, or a small load test) failing closed on regression. |
| data-integrity / migrations | **No** (for products) | `database-admin` on-demand ("reversible, proven on real-data copy") | **No** product guard (platform has snapshot tests) | **absent** | Migration standard (reversible + dry-run on copy) + a guard that every migration has a tested down-path. |
| privacy / compliance | Partial — `pre-launch.md` + `legal-compliance.md` + consent | Yes — privacy-dpo + qa flags PII | Partial — selftest consent-gate + GDPR-delete (platform); launch-gated | **minor** | Promote PII/data-flow check into the line (not just launch) + a guard that new analytics/PII egress in a diff blocks. |
| observability / telemetry | Partial — `pre-launch.md` ("observability/alerting in place") | `devops-sre` on-demand | **No** product-level guard (platform self-observes via prometheus) | **major** | Standard requiring built products emit health + key events + error telemetry; guard that asserts the product exposes a health endpoint and logs errors. |
| i18n-readiness | **No** | `localization-translator` on-demand | **No** | **absent** | i18n standard (no hardcoded user-facing strings, locale-safe dates/numbers/plurals) + a guard that greps for hardcoded strings / missing string-catalog keys. |
| docs / onboarding | **No** (for products) | `technical-writer` on-demand | **No** product guard (platform has onboarding-wizard selftest) | **major** | Docs standard (README/quickstart verified against running product) + a guard that the documented first-run steps actually succeed. |

Severity key: **none** = full triad present · **minor** = standard+role but no fail-closed guard ·
**major** = role prose only, no standard, no guard · **absent** = not mandated anywhere as a standing gate.

## The biggest gaps to close (prioritized) — the genuinely ABSENT lenses

These have **no standard, no line-stage role mandate, and no guard**. They are where the next user-found
bug will come from, in priority order. For each: install the same triad that closed journey + timing.

### 1. product-craft / UX-completeness  ← the class the user just hit
- **Standard:** `docs/STANDARDS-ux-craft.md` — forms have visible labels, correct `autocomplete`,
  Enter-submits, inline validation, disabled-while-submitting, a single clear primary affordance, no
  dead-ends; every screen answers "what do I do next?".
- **Role mandate:** add a **DESIGN/UX stage** to `factory.py` between BUILD and QA invoking `design-ux`
  (flip it to a line role); add `definition_of_done` + `must_never` to qa-security/reviewer requiring
  "expected affordances present" as a blocking item, citing the new standard (mirror how `STANDARDS-qa.md`
  is cited today).
- **Guard:** extend `console_e2e.cjs` with a craft pass — every `<form>` field has a label, every primary
  button is reachable and not the only nav, Enter submits, submit disables; fail closed.

### 2. accessibility (WCAG / keyboard / contrast / ARIA)
- **Standard:** `docs/STANDARDS-a11y.md` (WCAG 2.2 AA: keyboard operability, visible focus, contrast,
  names/roles/labels).
- **Role mandate:** promote the single a11y bullet in qa/reviewer/sdet to a blocking `definition_of_done`
  item tied to the standard; design-ux already owns WCAG — wire it into the line.
- **Guard:** add `@axe-core/playwright` to `console_e2e.cjs`, fail closed on serious/critical; add a
  keyboard-only traversal assertion (every interactive control reachable + visible focus).

### 3. responsive / mobile (currently a one-viewport illusion of coverage)
- **Standard:** breakpoint matrix + "primary action visible and tappable at 390px" rule.
- **Role mandate:** make the "mobile viewport" bullet blocking in qa/sdet DoD.
- **Guard:** loop `console_e2e` over `[390, 768, 1380]` and assert no horizontal overflow, no clipped
  content, primary action present at each.

### 4. empty / loading / error states (strong in standard+role, ZERO automated proof)
- **Guard (the missing leg):** force zero-rows / pending / failed-fetch on each surface and assert a real
  state UI — not blank, not an infinite spinner. The standard (`STANDARDS-planning.md` §3) and role DoD
  already mandate it; only the fail-closed guard is missing, which is exactly why it still slips.

### 5. content / microcopy
- **Standard + guard:** voice/error-message rules + a lint that fails on placeholder/lorem/TODO text and
  empty button/aria labels. Cheap, high signal.

### 6. performance/load, 7. data-integrity/migrations, 8. i18n, 9. observability(product), 10. docs/onboarding
- Each needs the same triad. Lower immediate-blast-radius than 1–5, but each is an unowned class today:
  a role exists on the bench for every one of them, none is a standing gate, none has a guard.

## The systemic fix (not lens-by-lens)

The recurring failure is structural, so the durable fix is a **meta-rule**, recorded as a standard and
enforced in `factory.py`: *a quality lens does not count as "in the pipeline" until it has all three of
{written standard, blocking role DoD, fail-closed standing guard}.* Maintain a checklist of lenses (this
table) and have `selftest.sh` assert each "in-pipeline" lens still has its guard wired — so a lens can
never silently regress from enforced back to aspirational, and new lenses are added deliberately instead
of only after a user hits the bug.
