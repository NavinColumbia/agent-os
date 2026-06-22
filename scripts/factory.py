#!/usr/bin/env python3
"""factory.py — the autonomous app factory.

This is the composition that turns the Controller skeleton into "ask anything → the fleet ships it":
real, role-specialized agents drive a product through SPEC → BUILD → QA → REVIEW → LAUNCH doing REAL
work — real code, real tests — with a genuine test-driven fix loop. Untrusted generated code runs
inside the srt sandbox (network+FS restricted); the trusted agent runtime calls the model normally.
Every step is audited.

  factory.py agent <role> <repo> "<task>"        # run one role-specialized real agent
  factory.py build <product> "<charter>"         # run a product end-to-end through the line
  factory.py fleet <specs.json> [workers]        # build SEVERAL products concurrently (the factory)
  factory.py selftest                            # offline check (prompt assembly, no model calls)
Run with the agent-os venv python. Needs the `claude` CLI authenticated; pytest in the venv.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

_ENV = Path.home() / "projects" / "agent-os" / ".env.local"
_DB = next((l.split("=", 1)[1].strip() for l in _ENV.read_text().splitlines()
            if l.strip().startswith("DATABASE_URL=")), None)


def _log_comm(cid, sender, recipient, intent, content):
    """Record a durable handoff in the conversation fabric so the dashboard's comms graph + message
    queue reflect REAL agent-to-agent communication (not just the audit stream). Best-effort."""
    try:
        with psycopg.connect(_DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO conversations (conversation_id, message_id, intent, sender, recipient, content)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (cid, f"{sender}->{recipient}-{int(time.time()*1000)}", intent, sender, recipient,
                         json.dumps(content)))
            c.commit()
    except Exception:
        pass

ROLES = Path.home() / "projects" / "control-plane" / "roles"
PRODUCTS = Path.home() / "projects" / "products"
VENV_PY = str(Path.home() / "projects" / "agent-os" / ".venv" / "bin" / "python")
MAX_FIX = 3  # bounded QA->BUILD re-flow attempts


def role_brief(role: str) -> str:
    """A role-aware system preamble pulled from the governed manifest — so the agent acts in-role
    and within its constitutional limits (must_never), not as a generic assistant."""
    f = ROLES / f"{role}.yaml"
    if not f.exists():
        return f"You are the {role}."
    import yaml
    m = yaml.safe_load(f.read_text())
    never = "; ".join(m.get("must_never", []))
    paths = ", ".join(m.get("allowed_paths", []))
    return (f"You are the {m.get('display_name', role)} ({role}) in a governed agent OS. "
            f"Responsibility: {m.get('summary', '')}. "
            f"You may only write within: {paths}. "
            f"Hard rules you must NEVER break: {never}. "
            f"Do not touch .env, secrets, or anything outside this product repo.")


def agent(role: str, repo: str, task: str, timeout: int = 420) -> dict:
    """Run one real, role-specialized agent (headless claude) inside the product repo. Audited."""
    prompt = f"{role_brief(role)}\n\nTASK:\n{task}\n\nWork now; create/edit files directly."
    p = subprocess.run(["claude", "-p", prompt, "--permission-mode", "acceptEdits"],
                       cwd=repo, capture_output=True, text=True, timeout=timeout)
    audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                 decision="executed", payload={"rc": p.returncode, "task": task[:80]})
    return {"rc": p.returncode, "out": (p.stdout or "")[-1500:], "err": (p.stderr or "")[-500:]}


def _sandbox_config(repo: str) -> dict:
    """srt policy for running UNTRUSTED generated code: write only to the repo + /tmp, read allowed
    (so the venv/stdlib import), and NO network egress (empty allowedDomains). Full schema required."""
    return {"filesystem": {"denyRead": [], "allowWrite": [repo, "/tmp"], "denyWrite": []},
            "network": {"allowedDomains": [], "deniedDomains": []}}


def run_tests(repo: str, sandboxed: bool = True) -> tuple[bool, str]:
    """Run the product's pytest suite. Untrusted generated code runs inside the srt sandbox (write-
    limited to the repo, network denied). Falls back to direct exec ONLY if the sandbox infra itself
    is unavailable (never to mask a real test failure)."""
    inner = f"cd {repo} && {VENV_PY} -m pytest -q"
    if sandboxed:
        sf = tempfile.NamedTemporaryFile("w", suffix=".srt.json", delete=False)
        json.dump(_sandbox_config(repo), sf); sf.close()
        try:
            p = subprocess.run(["srt", "-s", sf.name, "-c", inner], capture_output=True, text=True, timeout=300)
            out = (p.stdout or "") + (p.stderr or "")
            infra_broken = ("Could not load settings" in out or "No usable temporary directory" in out
                            or "srt:" in out.lower()[:40])
            if not infra_broken:
                audit.append(actor="factory:qa-security", action="RunTests", resource=Path(repo).name,
                             decision="executed", payload={"rc": p.returncode, "sandboxed": True})
                return p.returncode == 0, out[-2500:]
        except Exception:
            pass
        finally:
            os.unlink(sf.name)
    # sandbox unavailable — best-effort direct run, clearly flagged in the audit
    p = subprocess.run(["bash", "-c", inner], capture_output=True, text=True, timeout=300)
    out = (p.stdout or "") + (p.stderr or "")
    audit.append(actor="factory:qa-security", action="RunTests", resource=Path(repo).name,
                 decision="executed", payload={"rc": p.returncode, "sandboxed": False})
    return p.returncode == 0, out[-2500:]


def build_product(product: str, charter: str) -> dict:
    """Drive one product end-to-end through the governed line with real agents + a real QA fix loop."""
    repo = PRODUCTS / product
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "CHARTER.md").write_text(f"# {product} — charter\n\n{charter}\n")
    log = {"product": product, "stages": []}

    cid = f"build-{product}"

    def stage(name, role, fn):
        print(f"\n[factory] === {name} ===", flush=True)
        _log_comm(cid, "controller", role, "delegate", {"stage": name})        # hand-off out
        r = fn()
        ok = (r.get("passed", True) if isinstance(r, dict) else True)
        _log_comm(cid, role, "controller", "done" if ok else "blocked", {"stage": name})  # hand-back
        log["stages"].append({name: r})
        print(f"[factory] {name}: {r}", flush=True)
        return r

    # SPEC — a PM turns the charter into a real spec + acceptance criteria
    stage("SPEC", "product-manager", lambda: agent("product-manager", str(repo),
          f"Read docs/CHARTER.md. Write docs/SPEC.md: scope, public API, and explicit acceptance "
          f"criteria as a bullet list of testable behaviours. Keep it tight and unambiguous."))

    # BUILD — a builder implements the library + a real pytest suite from the spec
    stage("BUILD", "builder", lambda: agent("builder", str(repo),
          f"Read docs/SPEC.md. Implement the product as importable Python under src/ "
          f"(package '{product.replace('-', '_')}') AND write a real pytest suite under tests/ that "
          f"covers every acceptance criterion, including edge cases. Use `from src...` imports. "
          f"Make `python -m pytest -q` pass from the repo root."))

    # QA — run the REAL suite; on failure, a bounded test-driven fix loop (the re-flow)
    def qa():
        ok, out = run_tests(str(repo))
        attempts = 0
        while not ok and attempts < MAX_FIX:
            attempts += 1
            print(f"[factory] QA red — fix attempt {attempts}/{MAX_FIX}", flush=True)
            agent("builder", str(repo),
                  f"`python -m pytest -q` is FAILING. Here is the output:\n\n{out[-1800:]}\n\n"
                  f"Fix the code under src/ (or a genuinely wrong test) so all tests pass. "
                  f"Do not delete tests to make them pass.")
            ok, out = run_tests(str(repo))
        return {"passed": ok, "fix_attempts": attempts, "tail": out[-400:]}
    qa_res = stage("QA", "qa-security", qa)

    # REVIEW — an independent reviewer records a verdict (read-mostly)
    stage("REVIEW", "reviewer", lambda: agent("reviewer", str(repo),
          f"Review src/ against docs/SPEC.md. Write docs/REVIEW.md: what's correct, any risks, and a "
          f"clear APPROVE/REQUEST-CHANGES verdict. Tests are currently "
          f"{'GREEN' if qa_res.get('passed') else 'RED'}."))

    # LAUNCH — only if QA is green (a real gate, not a placeholder)
    if qa_res.get("passed"):
        stage("LAUNCH", "tech-lead", lambda: agent("tech-lead", str(repo),
              "Write docs/LAUNCH-CHECKLIST.md (how to install, run, and the test command) and a short "
              "README.md. This product passed QA and is cleared to ship."))
        log["result"] = "LAUNCHED"
    else:
        log["result"] = "BLOCKED_AT_QA"   # the line refuses to ship red code
    audit.append(actor="factory:controller", action="ProductComplete", resource=product,
                 decision=log["result"], payload={"stages": len(log["stages"])})
    # proactive push so you learn the outcome without watching anything
    try:
        import notify
        fixes = qa_res.get("fix_attempts", 0)
        if log["result"] == "LAUNCHED":
            notify.send(f"✅ {product} shipped — LAUNCHED (QA green, {fixes} fix loops)",
                        title="app factory", tags="rocket")
        else:
            notify.send(f"⛔ {product} BLOCKED_AT_QA after {fixes} fix attempts — needs you",
                        title="app factory", priority="high", tags="warning")
    except Exception:
        pass
    print(f"\n[factory] {product}: {log['result']}", flush=True)
    return log


def dispatch_fleet(specs, max_workers=3):
    """Build several products CONCURRENTLY — the app factory at scale. Each runs its own governed line
    (own repo, own audit/comms rows), so they all show up together on the mission-control dashboard.
    specs = [{"product": "...", "charter": "..."}, ...]. Bounded by max_workers parallel lines."""
    results = {}
    audit.append(actor="factory:controller", action="FleetStart", resource=f"{len(specs)} products",
                 decision="executed", payload={"workers": max_workers})
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(build_product, s["product"], s.get("charter", "")): s["product"] for s in specs}
        for f in as_completed(futs):
            name = futs[f]
            try:
                results[name] = (f.result() or {}).get("result", "UNKNOWN")
            except Exception as e:
                results[name] = f"ERROR: {e}"
    launched = sum(1 for v in results.values() if v == "LAUNCHED")
    try:
        import notify
        notify.send(f"🏭 fleet done: {launched}/{len(results)} LAUNCHED — " +
                    ", ".join(f"{k}:{v}" for k, v in results.items()), title="app factory", tags="factory")
    except Exception:
        pass
    print(f"\n[factory] FLEET COMPLETE: {results}", flush=True)
    return results


def _main(a):
    if not a:
        sys.exit("usage: factory.py agent|build|fleet|selftest ...")
    if a[0] == "agent":
        print(agent(a[1], a[2], a[3]))
    elif a[0] == "build":
        charter = a[2] if len(a) > 2 else "Build a small, well-tested Python library."
        build_product(a[1], charter)
    elif a[0] == "fleet":
        raw = Path(a[1]).read_text() if len(a) > 1 and Path(a[1]).exists() else (a[1] if len(a) > 1 else "[]")
        dispatch_fleet(json.loads(raw), int(a[2]) if len(a) > 2 else 3)
    elif a[0] == "selftest":
        brief = role_brief("builder")
        ok = "builder" in brief and "NEVER" in brief.upper() and PRODUCTS.parent.exists()
        print("role-brief assembled from manifest:", brief[:90], "...")
        print("PASS: factory prompt assembly + governance wiring ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
