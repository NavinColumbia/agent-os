# Patent & Recognition Guide — agent-os

**Not legal advice.** This is an engineering-side guide to (a) what is plausibly patentable here, (b) how to
file cheaply and fast to establish priority, and (c) how the work maps to U.S. extraordinary-ability
immigration criteria. Engage a registered patent attorney before filing. Proprietary — see LICENSE.

## 1. Establish priority NOW (cheap, do first)
- **Provenance is already in place:** `PROVENANCE.json` (Ed25519-signed manifest + timestamp + git commit) +
  private GitHub commit history = dated, attributable evidence of conception/reduction-to-practice.
- **U.S. Provisional Patent Application (PPA):** ~$60–$130 (micro/small entity), no formal claims required,
  gives "patent pending" + a 12-month priority date. File the whitepaper + this claim set as the spec. This is
  the single highest-leverage IP action.
- Optional public timestamp: anchor `root_hash` from PROVENANCE.json via OpenTimestamps for a trustless date.

## 2. Candidate patentable subject matter (novel *integrations*, not generic ideas)
Frame as a system + method; the defensibility is in the specific combination on a single host.

**Independent claim A (system — uniform heterogeneous-agent governance):**
> A system for governing autonomous AI agents comprising: a capability manifest declaring permitted tools,
> paths, and prohibited command patterns for an agent role; a pre-execution reference monitor that intercepts
> each agent tool invocation and renders an allow/deny decision by combining the manifest with an externalized
> policy decision point; an OS-level sandbox enforcing namespace, syscall, and default-deny network-egress
> constraints on the invocation; and an append-only hash-chained audit recording each decision such that any
> later modification is cryptographically detectable; wherein the same manifest, monitor, sandbox, and audit
> govern agents from a plurality of distinct, unmodified third-party model providers **identically**.

**Independent claim B (method — durable token-free agent communication):**
> A method wherein a first agent, executing as a durable workflow, transmits a typed request and then suspends
> such that no process and no model-inference cost is incurred while awaiting a reply; the workflow state is
> persisted to a database; a second agent's reply event resumes the first workflow from the suspension point,
> including after a failure of the process that initiated the request; and a controller maintains a wait-for
> graph over suspended workflows and resolves detected cycles by deterministic victim selection.

**Dependent claims (add specificity / fallback breadth):** Postgres-co-located durable state giving
transactional exactly-once steps (C1); schema-checked output verification before acceptance (C5);
artifact-gated stage transitions that block advancement absent required artifacts (C6); signed-manifest agent
identity verified at the reference monitor (identity); cryptographic authorship provenance with a build-
fingerprint canary (C7); the entire system operating on a single host with no external network dependency.

## 3. Filing strategy
1. **PPA now** (spec = WHITEPAPER.md + §2 claims + architecture diagrams from the ADRs).
2. Within 12 months: **non-provisional U.S.** + **PCT** (preserves most countries incl. Switzerland/EU) if
   pursuing international. Switzerland/EU patents via EPO national phase.
3. Keep building + shipping; continuation applications can capture new claims (CR re-flow, the comm fabric).
4. Defensive option if budget-limited: a dated **defensive publication** (arXiv of the whitepaper) blocks
   others from patenting it — but a PPA is better since it preserves *your* ability to patent.

## 4. U.S. immigration evidence mapping (O-1A / EB-1A — "extraordinary ability")
This work can support several regulatory criteria; an immigration attorney assembles the petition.
- **Original contributions of major significance:** the whitepaper + a granted/pending patent + the novel
  architecture (independent claims above) are direct evidence.
- **Authorship of scholarly articles:** publish the whitepaper (arXiv / a venue) — *after* the PPA priority date.
- **Judging / membership / press / high remuneration / critical role:** accrue over time (talks, a funded
  startup, paying users, media coverage of the product).
- **Patents** themselves are strong evidence of original contribution for both O-1A and EB-1A.
- Practical path: (PPA → arXiv → product traction/users/revenue → press) builds a portfolio; EB-1A
  (green card, self-petition, no employer needed) and O-1A both reward this combination.

## 5. Anti-theft posture (your stated concern)
- Repos are **private**; LICENSE is **all-rights-reserved**; provenance is **signed + watermarked**.
- If a third party ships a derivative: match their source/design hashes against `PROVENANCE.json`, or detect
  the build fingerprint, to prove derivation. The signed manifest + GitHub-timestamped history are evidence.
- Before any public release or demo, decide deliberately what to reveal — a public PPA/arXiv establishes
  priority so disclosure no longer risks others patenting it out from under you.
