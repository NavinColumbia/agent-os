#!/usr/bin/env python3
"""cr_reflow.py — the upward-feedback loop end-to-end (ADR 0002).

Demonstrates a downstream stage discovering an infeasibility, filing a Change Request (validated
against control-plane/schemas/change-request.schema.json), the Controller triaging it (amend_spec),
and the task RE-FLOWING to its prior stage and completing — with every step metered + audited.
This is the bidirectional comms the forward-only board lacked.

    cr_reflow.py demo
Run with the agent-os venv python.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import metrics  # noqa: E402
import audit    # noqa: E402

CR_SCHEMA = Path.home() / "projects" / "control-plane" / "schemas" / "change-request.schema.json"


def _validate_cr(cr):
    schema = json.loads(CR_SCHEMA.read_text())
    missing = [k for k in schema["required"] if k not in cr]
    if missing:
        raise ValueError(f"CR missing required fields: {missing}")
    if cr["kind"] not in schema["properties"]["kind"]["enum"]:
        raise ValueError(f"bad kind {cr['kind']}")
    return True


def demo():
    import os
    product = f"reflow-demo-{os.urandom(3).hex()}"  # unique so KPIs are deterministic across re-runs
    print("[stage BUILD] implementing against SPEC §4.2 (SendGrid v2)…")
    # 1) downstream discovers infeasibility -> file a CR instead of improvising
    cr = {
        "id": "CR-reflow-demo-0001", "raised_by": "builder", "product": product,
        "against": {"spec_ref": "SPEC.md#4.2", "adr_ref": None, "task_id": "build-mailer-014"},
        "kind": "deprecated", "severity": "blocker",
        "discovered_in_task": "build-mailer-014@branch:build/mailer",
        "summary": "SendGrid v2 API returns 410 — deprecated",
        "evidence": ["410 response log", "vendor deprecation notice URL"],
        "proposed_resolution": "Amend SPEC §4.2 to SendGrid v3 /mail/send (personalizations[] shape)",
        "blocks": ["build-mailer-014"], "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _validate_cr(cr)
    metrics.record("cr_filed", product=product, task_id="build-mailer-014", outcome="rework")
    audit.append(actor="builder", action="ChangeRequest", resource="SPEC.md#4.2", decision="raised",
                 payload={"cr": cr["id"], "kind": cr["kind"]})
    print(f"[upward] CR {cr['id']} filed (validates against schema); task -> blocked-needs-spec-change")

    # 2) Controller routes to tech-lead (kind=deprecated -> technical); decision = amend_spec
    cr["status"] = "decided"
    cr["owner"] = "tech-lead"
    cr["decision"] = {"outcome": "amend_spec", "rationale": "v2 gone; migrate to v3",
                      "decided_by": "tech-lead", "approval_id": "APR-0031"}
    cr["resulting_artifacts"] = ["SPEC.md@v2", "ADR-012"]
    metrics.record("cr_decided", product=product, task_id="build-mailer-014")
    print(f"[triage] tech-lead decided: {cr['decision']['outcome']} -> SPEC v2 + ADR-012 (dep approved {cr['decision']['approval_id']})")

    # 3) re-flow: task returns to BUILD with amended refs; agent re-hydrates; now feasible
    cr["status"] = "re-flowed"
    print("[re-flow] build-mailer-014 -> in-progress with SPEC@v2; re-implementing against v3…")
    metrics.record("state_change", product=product, task_id="build-mailer-014", to_state="done",
                   tokens_in=700, tokens_out=220, outcome="success")
    cr["status"] = "closed"

    k = metrics.kpis(product)
    print(f"[done] KPIs: {k}")
    ok = k["change_requests"] == 1 and k["crs_resolved"] == 1 and k["rework_events"] >= 1 and audit.verify()[0]
    print("PASS: upward feedback CR filed→decided→re-flowed→closed; metered + audited ✅" if ok else f"FAIL: {k}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    demo()
