# ADR-010: Evidence-backed product council and admitted agent topology

- Status: accepted
- Date: 2026-09-18
- Owners: product, experience, agent runtime, assurance

## Context

Agent OS can run browser personas, multiple agents, independent reviewers, deterministic checks, and controlled
improvement candidates. Those capabilities did not yet answer two decisive questions:

1. when may synthetic users or model judges influence a customer-facing product decision; and
2. when does a multi-agent organization produce enough benefit to justify its extra cost, latency, context
   fragmentation, and failure surface over a direct model or single agent?

Letting a product-manager agent or persona council decide by preference would create an automated echo
chamber. Conversely, prohibiting synthetic evaluation would throw away a useful and inexpensive issue-discovery
tool. The system needs a boundary between discovery, evidence, and authority.

## Decision

Product changes use a versioned `ProductStudy` with a hypothesis, baseline and candidate variants, canonical
tasks, representative segments, directional measures, minimum deltas, and hard gates.

Every observation records its study revision, task, segment, variant, evaluator, evidence kind, numeric
measures, and durable evidence IDs. Synthetic comparisons must be blinded, repeated at least three times, and
position-counterbalanced. Critical deterministic failures reject a candidate and cannot be voted away.

Synthetic/deterministic evidence may request human validation but cannot represent customers. Representative
human evidence may admit only a bounded reversible experiment. Representative production evidence that meets
all pre-registered measures may admit adoption. An attributable authority still owns release/promotion where
policy requires it.

Every mission workstream also carries a `CoordinationPlan` naming its strategy, coupling, parallelism, simpler
comparison baseline, expected measured benefit, measure IDs, estimated model cost, latency budget, and fallback.
Parallel agents require low-coupling work, real workers, and registered measures. Coordination estimates cannot
exceed admitted budget authority. Evaluator-optimizer loops require a measured rubric. Subworkflow declarations
must reference executable subworkflow nodes.

Paid value is evaluated on matched tasks against a named baseline. Success, quality, and reliability may not
regress. Model cost and customer price are compared only with measured human-time value; subjective quality or
market outcomes are not converted into invented dollars.

## Required invariants

1. Synthetic users and LLM judges are never labeled human or production evidence.
2. A study's tasks, segments, variants, measures, and revision are fixed before results are admitted.
3. Every observation cites durable evidence; summaries alone are insufficient.
4. Critical deterministic failures cannot be averaged away.
5. Maker and independent checker identities remain explicit.
6. Judge identity and variant identity are blinded where possible; ordering is counterbalanced and repeated.
7. Human and production cohorts are representative of the stated segment or the decision reports insufficient
   evidence.
8. Extra agents require distinct low-coupling scopes or a measured evaluator loop; agent count is not a quality
   metric.
9. Estimated topology cost is inside the CEO's admitted budget and has a simpler fallback.
10. Value claims name their comparison baseline and retain failed/negative results.

## Consequences

A potential-customer agent, product-manager agent, design reviewer, accessibility explorer, and Figma adapter
can all participate without becoming product authority. The system can find more issues cheaply while remaining
honest about what has and has not been learned from actual users.

The planner may still create very large organizations, but scale becomes an evidence-backed response to mission
structure rather than a prompt bias. A customer can receive a concrete answer to “why not use Codex/Claude
directly?” based on matched outcomes and human effort instead of feature rhetoric.

Older mission-program artifacts remain readable as conservative single-agent workstreams. New planning prompts
require the explicit coordination contract. A future format revision may make those fields mandatory after old
runs drain.

## Rejected alternatives

- **Synthetic majority vote:** measures model preferences and persona prompts, not customer behavior.
- **One evaluator pass:** vulnerable to position, stochastic, and self-preference bias.
- **Aggregate score with no hard gates:** allows severe correctness, security, or accessibility failure to hide
  behind superficial gains.
- **Always maximize agents:** contradicts measured cost/context/reliability tradeoffs.
- **Always use one agent:** discards real breadth-first parallel gains and independent verification.
- **Figma or screenshots as product truth:** useful evidence surfaces that cannot prove comprehension, utility,
  or commercial value.
