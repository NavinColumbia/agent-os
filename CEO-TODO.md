# CEO / Founder TODO — agent-os

Actions only **you** (Navin Ashok Swaminathan) can take. The engineering is built, proven (18/18),
and IP-protected; these unlock money, protection, and recognition. Ordered by priority.

## 🔴 NOW — protect the IP before any disclosure
1. **File a US Provisional Patent (PPA).** ~$60–130 (micro-entity). Establishes "patent pending" + a
   12-month priority date with no formal claims required.
   - Spec to upload: `docs/WHITEPAPER.md` + the claim drafts in `docs/PATENT-GUIDE.md`.
   - File at uspto.gov (Patent Center). You'll provide name/address/entity status.
   - *Do this BEFORE publishing anything publicly.* (A registered patent attorney is ideal but optional for a PPA.)
2. **(Optional, same day) Public timestamp:** anchor `PROVENANCE.json` → `root_hash` via OpenTimestamps for a
   trustless authorship date. (I can prepare the exact command; it just needs one network call you approve.)

## 🟠 NEXT — recognition (after the PPA)
3. **Publish the whitepaper** (arXiv or a venue) — counts toward O-1A/EB-1A "scholarly articles" + "original
   contributions." Only after priority is secured.
4. **Immigration:** take the (pending) patent + paper + product to an O-1A / EB-1A attorney. `docs/PATENT-GUIDE.md §4`
   maps the work to the criteria. EB-1A is self-petition (no employer needed).

## 🟡 BUSINESS — money / users
5. **Decide the model:** self-hostable license vs hosted SaaS (users bring their own agent + API keys). The
   BYO-provider layer (`scripts/providers.py`) already supports Claude/DeepSeek/OpenAI/Together/Groq/Ollama.
6. **First users:** stand up an instance for a handful of privacy-sensitive devs; gather testimonials (also
   immigration evidence: "critical role", "high remuneration", press).
7. **Pricing/packaging:** the README is the pitch; turn it into a landing page when ready.

## 🟢 HOUSEKEEPING / SECURITY
8. **Narrow sudo back:** you still have `NOPASSWD:ALL`. When convenient: `sudo rm /etc/sudoers.d/swami-nopasswd`
   (leaves the scoped docker sudoers). Low urgency on a single-user box, but tidy.
9. **Keys backup:** `~/projects/agent-os/keys/` (gitignored) holds the Ed25519 signing keys for identity +
   provenance. Back them up offline — losing them means you can't re-sign as the same author.

## What I needed from you
- ✅ Full legal name (got it: Navin Ashok Swaminathan) — now in LICENSE/NOTICE.
- Nothing else for signing — keys are generated and held locally; provenance is already signed.
- For the PPA itself you'll enter your own name/address/payment at USPTO; I don't need your payment info.

## State pointers
- Reproduce/verify everything: `bash scripts/selftest.sh` (18/18).
- What's built + what's deferred: `BUILD_STATUS.md`. Architecture: `docs/WHITEPAPER.md` + control-plane ADRs 0002–0005.
- Repos (private): github.com/NavinColumbia/{agent-os, control-plane, noupload}.
