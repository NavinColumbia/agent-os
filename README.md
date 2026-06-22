# agent-os — a private, governed operating system for AI agents

> **Run an autonomous AI software organization on your own machine. Bring any agent. Nothing leaves the box.**

**Proprietary — see [LICENSE](LICENSE).** Authorship cryptographically attested in [`PROVENANCE.json`](PROVENANCE.json).
Architecture: [WHITEPAPER](docs/WHITEPAPER.md) · IP: [PATENT-GUIDE](docs/PATENT-GUIDE.md) · build state: [BUILD_STATUS](BUILD_STATUS.md).

## What it is
agent-os turns one machine into a governed, crash-proof, fully-private home for autonomous AI agents.
A standing **Controller** runs products through a lifecycle (spec → build → QA → review → launch) using
agents you choose, where **every action an agent takes is constrained, sandboxed, and tamper-evidently
audited** — and the whole thing survives crashes and reboots without losing work.

## Why it's different
- **Bring Your Own Agent.** Claude (full agentic), or any OpenAI-compatible model — **DeepSeek, OpenAI,
  Together, Groq, or a local Ollama model**. Bring your own API key. The *same* governance applies to all.
- **Private by construction.** Single box; everything binds to localhost or your Tailscale tailnet — never
  `0.0.0.0`. No cloud, no data egress. Works offline. A strict sandbox denies network egress by default.
- **Governed & auditable.** Capability manifests + an OS sandbox + a policy decision point gate every tool
  call; every decision is written to a hash-chained log that detects tampering even by a DB admin.
- **Crash-proof.** Durable execution (on Postgres) resumes workflows from the exact step after any crash.
- **Communicates like a team.** Agents ask each other questions and **suspend with zero token cost until
  the answer arrives** (surviving crashes); deadlocks are detected and broken; humans approve risky actions
  from their phone.
- **Improves itself.** It measures its own throughput, rework rate, and cost, and enforces its own process
  (a stage can't start without its required artifacts).

## Proven, not promised
Everything re-proves in one command:
```bash
bash scripts/selftest.sh        # → SELFTEST: 18 passed, 0 failed
```
…covering enforcement, sandbox, tamper-evident audit, PDP, signed identity, durable execution,
crash-surviving ask-await, deadlock detection, graph memory + reflection, eval harness, tracing,
stage-gates, self-metrics, upward-feedback (change-request) re-flow, BYO providers, provenance, and the
full Controller lifecycle.

## Use it
```bash
git clone git@github.com:NavinColumbia/agent-os.git ~/projects/agent-os
cd ~/projects/agent-os && bash bootstrap.sh        # clones control-plane, builds venv, wires a product
# pick your agent:
#   AOS_PROVIDER=claude                              (full agentic, default)
#   AOS_PROVIDER=deepseek  AOS_PROVIDER_KEY=sk-...    (or openai / together / groq / ollama)
bash scripts/recover.sh                              # bring all services up
# run a product through the governed lifecycle:
.venv/bin/python scripts/controller.py run myproduct
```
After any reboot it self-recovers (WSL `[boot]` hook). See [QUICKSTART](QUICKSTART.md).

## Status
Reference architecture **v1 complete** and integrated into a running, autonomous, self-proving system.
Roadmap and deferred items in [BUILD_STATUS.md](BUILD_STATUS.md).
