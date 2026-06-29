# Standard: Detailed Plan Before Implementation

Status: REQUIRED for every planning step (factory SPEC stage, loopcontroller DEEP_DESIGN /
PLAN_APPROVAL, and any planning/architect role: product-manager, software-architect, tech-lead,
staff-engineer).

## Why this exists

Our rework loops do not come from bad coding — they come from implementing before the change has
been fully analyzed. Two failure modes from this session are the proof:

- A **function signature was changed without enumerating its callers**, so dependents were left
  calling the old shape — and it broke production.
- **Enforcement was tightened without mapping which roles needed each path**, so legitimate work
  was blocked and the change had to be reworked.

Both were correct in isolation and wrong in context. The cost was multiple passes for a change that
could have landed once. The fix is not "code more carefully" — it is **make the plan account for
everything before any code is written**, so implementation is right the first time and the fleet can
fan out safely.

## What every plan/spec MUST contain

A plan is not done until it has all five of these. A missing item here is a bug shipped later.

1. **IMPACT MAP.** Every file to be created or changed. For ANY function signature, schema, API, or
   contract being changed, grep the codebase and list **all** its callers/dependents that must change
   in the same pass — nothing left un-propagated. For an enforcement/permission change, map which
   **roles and paths** each rule affects (who gains/loses access).

2. **INVARIANTS TO PRESERVE.** The existing tests, guards, and security constraints that must still
   hold after the change. Do not weaken a safety constraint to make a change easier — call it out and
   escalate instead.

3. **EDGE / EMPTY / ERROR / LOADING CASES.** The non-happy-path behavior each touched surface must
   handle (empty input, malformed input, boundary values, dependency timeout, unauthorized, loading
   state). The happy path working is not evidence the change is correct.

4. **PARALLELIZATION PLAN.** Group the work items into those that are **INDEPENDENT** (can run
   concurrently) versus **ORDERED** (and state why). This lets execution fan out across the fleet
   without one stream silently breaking another.

5. **DONE CHECKLIST.** Map each scope item to the specific selftest / guard / test that **proves** it.
   "Done" means demonstrated by a named check, not "should work."

## How this is enforced

- **factory.py — SPEC stage:** the product-manager prompt requires docs/SPEC.md to contain all five
  sections above (scope/API, acceptance criteria, impact map, invariants, parallelization, done
  checklist).
- **loopcontroller.py — DEEP_DESIGN:** the plan prompt requires the `[[PLAN]]` bullets to embed the
  impact map, invariants/edge cases, parallelization note, and done checks before PLAN_APPROVAL.
- **Role manifests** (`control-plane/roles/`): the planning roles (product-manager,
  software-architect, tech-lead, staff-engineer) carry the impact-map + parallelization requirement in
  their `responsibilities`, `definition_of_done`, and a `must_never` against handing off a
  signature/schema/contract change without enumerating its callers.

## The one rule

If you are about to change a signature, schema, contract, or an enforcement rule, you must enumerate
who depends on it **before** you change it. Discovering propagation during implementation is the
definition of a rework loop.
