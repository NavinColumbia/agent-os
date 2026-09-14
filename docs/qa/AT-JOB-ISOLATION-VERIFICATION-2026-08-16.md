# QA/security verdict: headed-browser and real-AT isolation

Date: 2026-08-16 (America/Los_Angeles)
Gate owner: `qa-security`
Verdict: **PASS for this focused infrastructure gate** (not a full product-release verdict)

## Production defects found and repaired

- Browser/AT children now receive an explicit environment allowlist rather than controller credentials and
  provider/DB secrets. Required non-secret WSLg endpoints (`PULSE_SERVER`) remain available.
- Invalid resume storage is rejected before resource acquisition; all later failures release the exact lease,
  ownership record, and private runtime.
- Protocol close gets bounded graceful cleanup before TERM/KILL.
- Cancellation requires the exact `(tenant, run_id)` pair and cannot widen to another job.
- Orca is treated as the upstream single-user workstation it is: concurrent submissions queue behind one
  full-lifetime cross-process lease. The inherited lease FD is also held by the wrapper and Orca, so parent
  death cannot admit a successor while the old external tree survives.
- The workstation lease is acquired before browser capacity, so a queued AT job consumes no Chromium slot.
- Per-job Xvfb, private D-Bus/AT-SPI runtime, storage, artifacts, debug evidence, and exact process ownership
  remain isolated. The process-local `GSETTINGS_BACKEND=memory` experiment was rejected because it prevented
  Orca's accessibility state from reaching Chromium.
- A real probe found 14 detached speech-dispatcher processes created by earlier failed sessions. The missing
  `PULSE_SERVER` had blocked Orca inside speech output, preventing event processing and graceful shutdown.
- A later rapid handoff exposed a second dependency race: Orca's speechd client autospawn could block for the
  full startup window. Each AT job now owns a foreground speech-dispatcher on its private runtime socket and
  terminates that exact child with Orca/Chromium; Orca autospawn is no longer in the production path.
  The allowlist was fixed; all 14 processes were birth-identity revalidated and terminated. Sixty-two proven
  unreferenced `aos-qa-orca-*` / `aos-qa-at-runtime-*` directories were removed. These temporary artifacts are
  not recoverable; current survivor counts are zero.

## Executed evidence

Deterministic policy and failure-path tests:

```text
env PYTHONPATH=scripts:scripts/qa:scripts/orchestra \
  .venv/bin/pytest -q tests/test_qa_at_job_isolation.py -k 'not live_concurrent'
7 passed, 1 deselected in 0.57s
```

Real headed Chromium + Orca queue/handoff/cancellation/crash gate:

```text
env PYTHONPATH=scripts:scripts/qa:scripts/orchestra AOS_RUN_LIVE_AT_ISOLATION=1 \
  timeout --signal=TERM --kill-after=30s 300s .venv/bin/pytest -vv -s \
  tests/test_qa_at_job_isolation.py::test_live_concurrent_jobs_are_isolated_and_failure_cleanup_is_complete
1 passed in 19.17s
```

The live gate submitted Alpha and Beta simultaneously, observed exactly one active Orca workstation and one
queued job, required the first job's real uniquely labelled speech event, scoped-cancelled it, observed the
second start and emit its own non-cross-contaminated speech event, killed the second job's exact
`browser_bridge.js` birth identity, and proved all descendants, registrations, leases, and private paths gone.

Broader QA compatibility set:

```text
tests/test_qa_at_job_isolation.py tests/test_qa_review_runtime.py tests/test_qa_viewport.py
tests/test_qa_management_checkpoint.py tests/test_qa_campaign_checkpoint.py
80 passed, 1 skipped in 14.92s
```

The one skip is the opt-in live test in the broad command; it was separately executed and passed above.
Python compilation and scoped diff checks also passed.

## Boundary of this verdict

This proves the local headed-browser/Orca isolation, queuing, evidence, failure, and cleanup contract. It does
not by itself certify research quality, product-manager judgment, implementation quality, the complete QA
campaign, deployment, or the product release. Those require the progressively larger paid-agent and full
lifecycle gates.
