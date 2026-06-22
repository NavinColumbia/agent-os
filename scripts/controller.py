#!/usr/bin/env python3
"""controller.py — the standing Controller: a DBOS workflow that COMPOSES every keystone
into one running, crash-resumable orchestrator (the integration the architecture was building toward).

Per product it walks the lifecycle SPEC→BUILD→QA→REVIEW→LAUNCH, and at each stage:
  * gate_check (control-plane) — refuses to enter a stage without its required artifacts,
  * the stage "does work" (here: produces the stage's artifact; in prod this wraps an agent),
  * enforces a representative tool action through the Cerbos PDP, logging the decision to the
    tamper-evident audit chain,
  * records a metrics state_change.
Because the whole thing is a DBOS workflow, a crash mid-lifecycle resumes from the exact stage,
and completed stages are never re-run (exactly-once).

    controller.py run <product>        # run a product through the lifecycle (resumable)
    controller.py result <product>     # fetch the completed workflow result
Run with the agent-os venv python.
"""
import os
import sys
from pathlib import Path

import psycopg
from dbos import DBOS, DBOSConfig, SetWorkflowID

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path.home() / "projects" / "control-plane" / "scripts"))
import audit            # noqa: E402
import metrics          # noqa: E402
import cerbos_check     # noqa: E402
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


DBOS(config=DBOSConfig(name="agentos-controller", database_url=DB))


@DBOS.step()
def stage_step(product: str, stage: str) -> str:
    repo = _repo(product)
    # 1) gate: required prior artifacts must exist (enforced, not assumed)
    missing = gate_check.check(str(repo), stage)
    if missing:
        raise RuntimeError(f"GATE BLOCKED entering {stage}: missing {missing}")
    # 2) enforce a representative tool action through the PDP + audit chain
    action, attr = STAGE_PLAN[stage]["action"]
    decision = cerbos_check.decide("builder", action, attr)   # logs to audit_log
    if decision != "allow":
        raise RuntimeError(f"PDP denied {action} {attr} in {stage}")
    # 3) do the work. With AGENT_WORKERS=1 a real governed agent does the BUILD stage
    # (proven in agent_worker.py); otherwise stages produce their artifacts directly (cheap+deterministic).
    if stage == "BUILD" and os.environ.get("AGENT_WORKERS") == "1":
        import agent_worker
        agent_worker.run_agent(str(repo), "Implement the spec in src/. Edit/create files under src/ only.")
    for rel in STAGE_PLAN[stage]["artifacts"]:
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
    """A product enters with a charter only; the Controller produces the rest as it advances."""
    repo = _repo(product)
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    # SPEC gate needs SPEC.md + an ADR; the SPEC stage produces them, but SPEC's OWN gate (BUILD reqs)
    # is checked when entering BUILD. SPEC stage entry has no prereqs. Seed nothing but the dir.


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
