# Root-cause architecture note — single source of truth + validated handoffs

Why the dogfood build hit a string of "different" bugs (F6 product-name mismatch, F7 build-error-ignored,
QA-escalates-to-human, F3 error mis-map, G1 dying threads) — and the one architectural fix for the whole class.

## The one disease
Every one of those bugs is the same failure wearing a different mask: in the **older loopcontroller CEO
pipeline**, each phase **independently re-derives shared truth** and each handoff **trusts the previous phase
instead of verifying it**.

- The controller computed the product id `1-ceo-cockpit`; the build layer computed `ceo-cockpit`. Two places
  invented the same fact → they diverged → QA looked in the wrong folder (F6).
- Build→QA advanced on job `status='done'` without checking the build actually **succeeded** or **produced an
  artifact** (F7).
- A QA failure was dumped on the **CEO** ("approve to rebuild") instead of auto-looping to the builder.

There was **no authoritative record** of "what is this product, where does it live, did each phase succeed,"
and **no contract** at the boundaries. (This is exactly what our own research already flagged: finding #5
mandatory task contracts, and MAST modes 2.4 *information withholding* / 3.2 *no-or-incomplete verification*. We
applied contracts to the **orchestra runtime** but never to this **older pipeline** — so it kept failing in the
ways the research predicted.)

## Why it exists
agent-os is two generations glued together. The **orchestra runtime** (durable actors, task contracts,
single-writer state, validated events) is robust — research runs on it never hit these bugs. The **loopcontroller
pipeline** (DISCOVER→…→DELIVER) predates that discipline and re-derives / loose-hands-off. Every dogfood bug
lived in the pipeline.

## The cure (shipped): registry + boundary contracts on the pipeline
`scripts/productregistry.py` — ONE authoritative row per product, and a validated contract at each boundary.

1. **Single source of truth.** `register(product_id, tenant, org, repo_path, plan)` writes the one canonical
   record; `path(product_id)` is the *only* place a phase learns where the product lives. The canonical path is
   **immutable across re-registration** — no phase can silently re-point the product. Two phases physically
   cannot disagree about what/where the product is → **F6 becomes impossible.**
2. **Validated handoff contracts.** `record_phase(product, phase, ok, artifact, verdict)` writes each phase's
   outcome; `precondition(product, phase)` is checked **before** the next phase runs:
   - `qa` requires **build succeeded AND its artifact exists (non-empty) at the registered path** → a failed or
     mislocated build can never reach a QA that can't find it (**F7 caught deterministically**).
   - `deliver` requires **QA passed**.
3. **Auto-loop, human last.** A failed/unverifiable build routes back to the **builder automatically**
   (`loopcontroller._autoloop_build`, bounded by `attempt()` / `AOS_MAX_BUILD_RETRY`, default 3); the CEO is
   escalated to **only when the autonomous loop is exhausted** — never as the first responder. Since it leaves
   the thread runnable, **jobd** re-dispatches the build in a long-lived process.

### Wiring
`loopcontroller` PROTOTYPE/IMPLEMENT `register()` the product; the build→QA and QA-verdict transitions
`record_phase` + check `precondition` and call `_autoloop_build` on failure. Fail-open: a registry hiccup never
blocks the pipeline. Selftest green (the stubbed build now produces a real artifact at the registered path, as a
real successful build would).

## Known residual (separate, concrete)
The registry+contracts make the F6 divergence **impossible to pass silently** — but for a build to actually
DELIVER, the build layer must *write its artifact at the registered path*. If the real build still slugs the id
(e.g. drops the `1-` org prefix), the contract will correctly fail and auto-loop/escalate rather than deliver.
Fixing the build layer to honor the registered path is the concrete follow-up (tracked in
[`E2E-FINDINGS-AND-FIXES.md`](E2E-FINDINGS-AND-FIXES.md)). The architecture now *surfaces* that bug honestly
instead of hiding it as "QA found 0 stories."

## The principle going forward
Any new phase/subsystem: **read shared truth from the registry, never recompute it; and validate the previous
phase's output against the registry before you act.** That is the same discipline that makes the orchestra core
reliable — now the law for the pipeline too.
