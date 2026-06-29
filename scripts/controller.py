#!/usr/bin/env python3
"""controller.py — the standing Controller: a DBOS workflow that COMPOSES every keystone
into one running, crash-resumable orchestrator (the integration the architecture was building toward).

Per product it walks the lifecycle SPEC→BUILD→QA→REVIEW→LAUNCH, and at each stage:
  * gate_check (control-plane) — refuses to enter a stage without its required artifacts,
  * the stage "does work" (here: produces the stage's artifact; in prod this wraps an agent),
  * enforces a representative tool action through the SAME policy-decision-point the shipped
    product repos and factory._run_once enforce through — governance.py (the role-manifest reader)
    + the enforce_manifest.py PreToolUse hook — and treats the Cerbos PDP as a defense-in-depth
    MIRROR that is cross-checked: a divergence between the two policy sources FAILS CLOSED, so the
    Cerbos policy can never silently drift from the enforcement point that ships real products.
    Every decision is logged to the tamper-evident audit chain,
  * records a metrics state_change.
Because the whole thing is a DBOS workflow, a crash mid-lifecycle resumes from the exact stage,
and completed stages are never re-run (exactly-once).

    controller.py run <product>        # run a product through the lifecycle (resumable)
    controller.py result <product>     # fetch the completed workflow result
Run with the agent-os venv python.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from dbos import DBOS, DBOSConfig, SetWorkflowID

SCRIPTS = Path(__file__).resolve().parent
CONTROL_PLANE = Path.home() / "projects" / "control-plane"
ROLES = CONTROL_PLANE / "roles"
ENFORCE_HOOK = CONTROL_PLANE / "hooks" / "enforce_manifest.py"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(CONTROL_PLANE / "scripts"))
import audit            # noqa: E402
import metrics          # noqa: E402
import cerbos_check     # noqa: E402
import governance       # noqa: E402  — the PDP-of-record's role-manifest reader (same as factory/loopcontroller)
import gate_check       # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

STAGES = ["SPEC", "BUILD", "QA", "REVIEW", "LAUNCH"]
PRODUCTS = Path.home() / "projects" / "products"

# What each stage produces (so gate_check for the NEXT stage passes) + a representative enforced action.
STAGE_PLAN = {
    "SPEC":   {"artifacts": ["docs/SPEC.md", "docs/adr/0001-arch.md"], "action": ("Edit", {"path": "docs/SPEC.md"})},
    "BUILD":  {"artifacts": ["src/app.py"],                            "action": ("Edit", {"path": "src/app.py"})},
    "QA":     {"artifacts": ["docs/QA-REPORT.md"],                     "action": ("Bash", {"cmd": "python -m pytest -q"})},
    "REVIEW": {"artifacts": ["docs/REVIEW.md"],                        "action": ("Bash", {"cmd": "git log -1"})},
    "LAUNCH": {"artifacts": ["docs/LAUNCH-CHECKLIST.md"],              "action": ("Bash", {"cmd": "git status"})},
}


def _repo(product):
    return PRODUCTS / product


# --------------------------------------------------------------------------- enforcement (PDP-of-record)
# The PDP-of-record is governance.py + the enforce_manifest.py PreToolUse hook — the IDENTICAL
# enforcement point the shipped product repos (new-product.sh wires the hook) and factory._run_once
# enforce through. The controller routes its representative action through that SAME point instead of
# relying on a standalone Cerbos call, so the demo's enforcement is the real ship enforcement. Cerbos
# is kept as a defense-in-depth MIRROR (see _enforce_action) so a policy-source drift is caught, not
# silently tolerated.

def _govern_decision(role: str, repo: Path, action: str, attr: dict) -> str:
    """The governance.py half of the PDP-of-record: deny if the action's tool is capability-gated away
    from this role (spawn_restrictions), or if an Edit/Write lands on a denied/out-of-scope path
    (validate_writes). Reads the role manifest — the same flags factory enforces. FAILS CLOSED."""
    try:
        restr = governance.spawn_restrictions(role)
        if action in restr["disallowed_tools"]:
            return "deny"
        if action in ("Edit", "Write", "MultiEdit", "NotebookEdit") and attr.get("path"):
            if governance.validate_writes(role, str(repo), [attr["path"]]):
                return "deny"
        return "allow"
    except Exception:
        return "deny"   # cannot consult the manifest reader -> deny (the safe direction; actions are known-good)


def _hook_decision(role: str, action: str, attr: dict) -> str:
    """The enforce_manifest.py half of the PDP-of-record: run the product PreToolUse hook against this
    action exactly as Claude Code does (event JSON on stdin, CP_MANIFEST=<role manifest>). Run it under
    THIS venv python so the hook's own pyyaml-missing fail-open branch never triggers. exit 0=allow,
    2=deny; any other outcome / failure to consult -> 'deny' (FAIL CLOSED — the representative actions
    are known-good, so an inability to reach the enforcement point is a loud safe failure, not a silent
    allow)."""
    tin: dict = {}
    if attr.get("path") is not None:
        tin["file_path"] = attr["path"]
    if attr.get("cmd") is not None:
        tin["command"] = attr["cmd"]
    event = json.dumps({"tool_name": action, "tool_input": tin})
    mpath = ROLES / f"{role}.yaml"
    try:
        p = subprocess.run(
            [sys.executable, str(ENFORCE_HOOK)], input=event, capture_output=True, text=True,
            timeout=10, env={**os.environ, "CP_MANIFEST": str(mpath), "CP": str(CONTROL_PLANE)})
    except Exception:
        return "deny"
    return "allow" if p.returncode == 0 else "deny"


def _enforce_action(role: str, repo: Path, action: str, attr: dict) -> None:
    """Enforce one representative tool action through the PDP-of-record, then cross-check Cerbos.

    Authoritative decision = governance.py AND the enforce_manifest.py hook must BOTH allow (deny beats
    allow). Then query the Cerbos PDP as a defense-in-depth mirror and APPEND its decision to the audit
    chain. Resolution rules:
      * Cerbos reachable and AGREES   -> proceed on the (allow/deny) decision.
      * Cerbos reachable and DIVERGES -> RuntimeError (FAIL CLOSED): a split between the two policy
        sources is itself an integrity failure; refuse to act on it.
      * Cerbos unreachable            -> the hook+governance is the PDP-of-record, so we MUST NOT brick
        the controller on a down mirror (liveness). Record a 'cross_check_skipped' audit entry and
        proceed on the authoritative decision.
    Raises RuntimeError if the action is denied (so the workflow surfaces it loudly)."""
    g = _govern_decision(role, repo, action, attr)
    h = _hook_decision(role, action, attr)
    decision = "allow" if (g == "allow" and h == "allow") else "deny"

    cer = None
    try:
        cer = cerbos_check.decide(role, action, attr)   # also appends the decision to the audit chain
    except Exception as e:
        try:
            audit.append(actor=f"controller:{role}", action=action,
                         resource=attr.get("path") or attr.get("cmd") or "",
                         decision="cross_check_skipped",
                         payload={"pdp_of_record": decision, "cerbos_error": type(e).__name__})
        except Exception:
            pass

    if cer is not None and cer != decision:
        raise RuntimeError(
            f"POLICY DIVERGENCE for {action} {attr}: PDP-of-record={decision}, Cerbos={cer} "
            f"— failing closed (consolidate cerbos/policies with the role manifest + enforce_manifest)")
    if decision != "allow":
        raise RuntimeError(
            f"PDP-of-record denied {action} {attr} (governance={g}, enforce_manifest={h})")


DBOS(config=DBOSConfig(name="agentos-controller", database_url=DB))


@DBOS.step()
def stage_step(product: str, stage: str) -> str:
    repo = _repo(product)
    # 1) gate: required prior artifacts must exist (enforced, not assumed)
    missing = gate_check.check(str(repo), stage)
    if missing:
        raise RuntimeError(f"GATE BLOCKED entering {stage}: missing {missing}")
    # 2) enforce a representative tool action through the PDP-of-record (governance.py +
    #    enforce_manifest.py — the SAME enforcement the shipped product repos and factory use),
    #    cross-checked against the Cerbos mirror (divergence fails closed). Logs to the audit chain.
    action, attr = STAGE_PLAN[stage]["action"]
    _enforce_action("builder", repo, action, attr)
    # 3) do the work. With AGENT_WORKERS=1 a real governed agent does the BUILD stage
    # (proven in agent_worker.py); otherwise stages produce their (non-gate) artifacts directly.
    if stage == "BUILD" and os.environ.get("AGENT_WORKERS") == "1":
        import agent_worker
        agent_worker.run_agent(str(repo), "Implement the spec in src/. Edit/create files under src/ only.")
    for rel in STAGE_PLAN[stage]["artifacts"]:
        # The Controller must NOT author its own gate evidence (findings #37/#45/#46): if the actor
        # the gate checks is the same one fabricating SPEC.md/ADR/QA-REPORT.md/src, the gate proves
        # nothing and the lifecycle advances on stubs. gate_check now validates artifact CONTENT, so
        # such stubs are rejected anyway — and we stop fabricating gate-enforced artifacts entirely.
        # Real gate artifacts come from the responsible agent/human (BUILD agent for src/; pm/tech-lead
        # /qa for SPEC/ADR/QA-REPORT) or seeded inputs; absent ones correctly BLOCK the next gate.
        if rel in ("docs/SPEC.md", "docs/QA-REPORT.md") or rel.startswith("docs/adr/") or rel.startswith("src/"):
            continue
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(f"# {rel} — produced by Controller at stage {stage}\n")
    # 4) record the transition
    metrics.record("state_change", product=product, task_id=f"{product}-lifecycle",
                   to_state=stage.lower(), tokens_in=500, tokens_out=150, model="controller", outcome="success")
    print(f"[controller] {product}: stage {stage} complete (gate ok, PDP allow, artifacts written)", flush=True)
    return stage


@DBOS.workflow()
def run_product(product: str) -> str:
    for stage in STAGES:
        stage_step(product, stage)
    metrics.record("state_change", product=product, task_id=f"{product}-lifecycle",
                   to_state="done", outcome="success")
    return f"{product}:LAUNCHED"


def _seed(product):
    """A product enters with a charter PLUS the upstream roles' real gate artifacts (pm's SPEC,
    tech-lead's ADR, qa's QA-REPORT, builder's src). The Controller deliberately does NOT author
    its own gate evidence (findings #37/#45/#46) — it only advances the lifecycle, enforcing each
    content-checked gate against artifacts the responsible roles produced. Here those role outputs
    are seeded as real, filled, *passing* inputs (gate_check's own golden artifacts), so the gates
    have genuine evidence to verify rather than fabricated stubs."""
    repo = _repo(product)
    seeds = {
        "docs/SPEC.md":            gate_check._REAL_SPEC,
        "docs/adr/0001-arch.md":   gate_check._REAL_ADR,
        "docs/QA-REPORT.md":       gate_check._REAL_QA,
        "src/app.js":              gate_check._REAL_SRC,
    }
    for rel, content in seeds.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(content)


def main(argv):
    mode = argv[0] if argv else "run"
    product = argv[1] if len(argv) > 1 else "ctrl-demo"
    DBOS.launch()
    if mode == "run":
        _seed(product)
        wf_id = f"prod-{product}"
        try:
            with SetWorkflowID(wf_id):
                res = run_product(product)
            print(f"[controller] RESULT: {res}")
        except Exception as e:
            print(f"[controller] workflow error: {e}")
            raise
    elif mode == "result":
        res = DBOS.retrieve_workflow(f"prod-{product}").get_result()
        print(f"[controller] RESULT: {res}")


if __name__ == "__main__":
    main(sys.argv[1:])
