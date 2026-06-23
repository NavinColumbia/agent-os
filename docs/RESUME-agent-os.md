# agent-os — Résumé / Portfolio Description

> **Repo:** https://github.com/NavinColumbia/agent-os *(private — grant reviewers temporary read access on request)*
> **Role:** Sole architect & engineer · **Stage:** Working system, pre-revenue
> **Scale:** 66 core Python modules · ~7,000 LOC core · 108 commits · 53-check self-test suite (green)

---

## Headline (one line)

**agent-os** — a privacy-first, single-box, *governed* autonomous AI software factory: role-specialized LLM agents take a product from spec to launch through a safety-gated lifecycle, with tamper-evident audit, crash-resilient orchestration, and self-healing operations.

---

## Project summary (profile / "About" paragraph)

Designed and built **agent-os**, a governed platform that turns role-specialized LLM agents (Anthropic Claude / OpenAI Codex) into an autonomous software factory. Products flow through a governed **SPEC → BUILD → QA → REVIEW → LAUNCH** pipeline with an enforced quality gate that refuses to ship code failing its tests. The system is engineered to run privately on a single machine while remaining ~1:1 portable to the cloud through configuration alone. Every agent action is recorded in a tamper-evident audit log; builds resume after a crash from persisted checkpoints; and operational failures self-heal where safe and escalate to a human where judgement is required.

---

## Résumé bullets (core engineering)

- **Autonomous multi-agent software factory** — built a governed pipeline in which headless, role-specialized LLM agents (`claude -p` / `codex exec`) drive a product through SPEC → BUILD → QA → REVIEW → LAUNCH, with an enforced QA gate (pytest / headless-browser smoke / static manifest validation) that blocks shipping broken code. Supports three artifact types: Python libraries, static web apps, and Chrome extensions.
- **PostgreSQL as a single transactional control plane** — unified application state, a **tamper-evident hash-chained audit log**, inter-agent messaging, a distributed work queue (`SELECT … FOR UPDATE SKIP LOCKED`), an encrypted secrets vault (Fernet), object storage, and metrics onto one substrate — deliberately portable to managed Postgres / S3 / cloud KMS with config-only changes.
- **Crash-resilient orchestration** — checkpoint-based resume (completed stages detected from persisted execution traces and skipped on restart), retry-with-backoff sized by a pre-flight agent runtime estimate, model pinning with automatic fallback on provider overload, and **global concurrency backpressure** (bounded semaphore) enabling safe fan-out across many concurrent builds.
- **Governance & safety layer** — prompt-injection defense (untrusted input wrapped as DATA with a policy backstop), per-role least-privilege file-path claims, **sandboxed, network-denied execution of untrusted generated code**, and advisory roles structurally barred from spending, publishing, or deploying without explicit human approval.
- **Self-healing operations** — watchdog/responder loops that auto-remediate safe failures (dead daemons, downed containers, disk pressure) while escalating judgement calls (deadlocks, build stalls, SLA breaches) to a human, via a tiered auto / escalate / unknown classifier.
- **Multi-tenant SaaS scaffolding** — bring-your-own-key routing (tenants fund their own inference), metered billing (plans, quota, overage, invoicing), self-serve onboarding, and per-app circuit-breaker auto-pause.
- **Operational tooling** — encrypted portable system snapshots (passphrase-derived Fernet), an agent-queryable "OS query plane," a live-web-grounded market-intelligence agent, a founder digest, a risk register, and a mission-control dashboard.

---

## Condensed version (3 bullets, for a one-page résumé)

- Architected **agent-os**, a governed autonomous AI software factory where role-specialized LLM agents drive products through an enforced SPEC→BUILD→QA→REVIEW→LAUNCH pipeline; ~7,000 LOC, 66 modules, 53-check self-test green.
- Engineered crash-resilient multi-agent orchestration on a single PostgreSQL control plane: checkpoint resume, retry/backoff, model fallback, global concurrency backpressure, and a tamper-evident audit log — designed for ~1:1 local→cloud portability.
- Built the safety layer: prompt-injection defense, sandboxed network-denied execution of untrusted generated code, least-privilege role claims, and self-healing ops that auto-remediate safe failures and escalate judgement calls.

---

## LinkedIn "Projects" version

**agent-os — Governed Autonomous AI Software Factory** *(Sole architect & engineer)*
A privacy-first platform where role-specialized LLM agents (Claude / Codex) build software through a safety-gated SPEC→BUILD→QA→REVIEW→LAUNCH lifecycle. Highlights: enforced QA gate that won't ship failing code; PostgreSQL as a single control plane (state, tamper-evident audit, work queue, vault); crash-resume from checkpoints; sandboxed execution of untrusted generated code; self-healing operations; multi-tenant billing & BYO-key. Runs on one box, cloud-portable by config. ~7,000 LOC, 53-check self-test.

---

## Tech stack

Python · PostgreSQL (+ pgvector) · Docker / Compose · Cerbos (policy-as-code) · Tailscale · Anthropic Claude & OpenAI Codex CLIs · headless-browser QA (Node) · Fernet / Ed25519 cryptography.

---

## Citable facts (all demonstrable from the code + self-test)

| Metric | Value |
|---|---|
| Core Python modules | 66 |
| Core lines of code | ~7,000 |
| Commits | 108 |
| Self-test checks (green) | 53 |
| Artifact types built end-to-end | 3 (lib, web app, Chrome extension) |
| Concurrency | configurable fleet workers + global agent cap |

---

## Honest framing notes (so claims hold up under scrutiny)

- This is a **solo, pre-revenue** project — lead with *designed / architected / built*, which is the genuine strength (systems breadth + safety engineering), not adoption metrics.
- Every capability above (governed pipeline, audit chain, crash-resume, sandboxing, self-healing) is **real and demonstrable** in the source and the self-test output — you can back each bullet with a file and a passing check.
- For visa / "original contribution" evidence: pair this doc with (a) a private-repo read invite, (b) a short demo video of a build running end-to-end, and (c) the self-test + audit-log output as artifacts.

---

*Generated for résumé/portfolio use. The repository is private; keep core mechanisms undisclosed until any patent (provisional) is filed.*
