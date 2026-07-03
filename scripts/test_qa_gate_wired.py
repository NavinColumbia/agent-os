#!/usr/bin/env python3
"""test_qa_gate_wired.py — the SHIP-GATE wiring guard for REBUILD-PLAN C1 (quality engine).

The bug class this guards against is the repo's own documented "declared-not-wired" failure: the
agentic QA stack (scripts/qa/*) exists, but the actual ship gates never consume its verdict — the
builder grades its own homework and a prose "Recommend: LAUNCH" string satisfies the LAUNCH gate.
Per docs/blueprint/ARCH-REVIEW-2026-07.json (quality-engine domain) + REBUILD-PLAN C1, the TARGET
state is:

  1. qa_report's machine JSON is the ONE verdict artifact: `passed` (bool), `bugs.blocking_open`,
     `coverage.total_stories` — and a zero-story run fails CLOSED (NO VERDICT, passed=False).
  2. factory's QA stage runs the agentic explorer stack (scripts/qa) against the running product
     and gates on that verdict; the local `qa_run = (lambda: run_tests/run_ext_qa...)` shadow —
     builder-authored tests AS the gate — is deleted. Builder tests may remain only as a pre-gate.
  3. gate_check LAUNCH binds to the qa verdict JSON (passed==True, blocking_open==0, stories>0),
     not to prose string-matching in QA-REPORT.md.
  4. loopcontroller's TESTQA phase consumes the same qa verdict before DELIVER.

This test FAILS if any of those consumers stops consuming the verdict JSON (or never starts).
Per the STANDARDS-verification.md pattern (test_quality_lenses_wired.py): grep/AST-probe the
ACTUAL mechanism, never a relayed claim. Do NOT weaken a probe to go green — wire the gate.

Run:  .venv/bin/python scripts/test_qa_gate_wired.py     (offline, no API calls, no network)
Exit 0 = every ship gate consumes the qa verdict JSON.  Exit 1 = a gate is unwired.
"""
import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FACTORY = REPO / "scripts" / "factory.py"
LOOPCTL = REPO / "scripts" / "loopcontroller.py"
GATECHECK = Path.home() / "projects" / "control-plane" / "scripts" / "gate_check.py"
QA_DIR = REPO / "scripts" / "qa"

# Modules of the agentic QA engine — a consumer must reference at least one entrypoint.
ENGINE_MODULES = {"qa_run", "qa_explorer", "story_gen", "dev_loop", "qa_report"}
# Builder-graded checks that must never BE the gate (fast pre-gate only).
SELF_GRADED = ("run_tests", "run_js_tests", "run_web_qa", "run_ext_qa")


def _imports_engine(tree: ast.AST, text: str) -> bool:
    """True if the file imports/invokes the scripts/qa engine (any honest mechanism counts:
    a python import of an engine module, a `qa.` package import, or a subprocess/path invocation)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if a.name in ENGINE_MODULES or parts[0] == "qa" or any(p in ENGINE_MODULES for p in parts):
                    return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            parts = mod.split(".")
            if mod in ENGINE_MODULES or parts[0] == "qa" or any(p in ENGINE_MODULES for p in parts):
                return True
            if any(a.name in ENGINE_MODULES for a in node.names):
                return True
    # subprocess / path-based invocation (e.g. scripts/qa/qa_run.py, importlib on the qa dir)
    return bool(re.search(r"(scripts[./]qa\b|\bqa/(qa_run|qa_explorer|qa_report)\.py\b)", text))


def _self_grading_shadow(tree: ast.AST) -> str:
    """The exact anti-pattern from the arch review (factory.py:1474): a local binding named `qa_run`
    that dispatches to builder-authored checks — the name of the real engine wrapping self-graded
    homework. Returns a description of the offending binding, or '' if clean."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if "qa_run" in names and node.value is not None:
                rhs = ast.unparse(node.value)
                if "lambda" in rhs or any(fn in rhs for fn in SELF_GRADED):
                    return f"local `qa_run = {rhs[:80]}...` shadows the engine with self-graded checks"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "qa_run":
            src = ast.unparse(node)
            if any(fn in src for fn in SELF_GRADED):
                return "local def qa_run(...) shadows the engine with self-graded checks"
    return ""


def _check_producer_contract() -> list:
    """Behavioral probe of the verdict PRODUCER (offline, deterministic — no AI, no browser):
    qa_report's tally must fail closed on zero stories, fail on blocking-open bugs, and pass only
    on all-green; and build_report's machine JSON must carry the exact keys the gates bind to."""
    missing = []
    sys.path.insert(0, str(QA_DIR))
    sys.path.insert(0, str(REPO / "scripts"))
    import qa_report  # noqa: E402

    zero = qa_report._tally({"stories": [], "bugs": []})
    if zero["passed"] or "NO VERDICT" not in qa_report._verdict_line(zero):
        missing.append("qa_report no longer fails CLOSED on a zero-story run")

    blocked = qa_report._tally({
        "stories": [{"id": "S1", "status": "passed", "steps": []}],
        "bugs": [{"id": "B1", "blocking": True, "fixed": False}]})
    if blocked["passed"] or blocked["blocking_open"] != 1:
        missing.append("qa_report no longer fails a run with an open BLOCKING bug")

    green = qa_report._tally({
        "stories": [{"id": "S1", "status": "passed", "steps": []}], "bugs": []})
    if not green["passed"] or green["total_stories"] != 1:
        missing.append("qa_report no longer passes an all-green run (verdict semantics broke)")

    src = (QA_DIR / "qa_report.py").read_text()
    for key in ('"passed"', '"blocking_open"', '"total_stories"'):
        if key not in src:
            missing.append(f"machine JSON key {key} vanished from qa_report.py — gates bind to it")
    return missing


def _check_factory() -> list:
    """factory's QA stage must consume the agentic engine's verdict, never its own homework."""
    missing = []
    text = FACTORY.read_text()
    tree = ast.parse(text)                    # also keeps the ast.parse-clean invariant honest
    shadow = _self_grading_shadow(tree)
    if shadow:
        missing.append(f"factory.py: {shadow} (arch-review factory.py:1474 — delete it; "
                       f"builder tests are a pre-gate, never the gate)")
    if not _imports_engine(tree, text):
        missing.append("factory.py: QA stage does not invoke the agentic engine (scripts/qa) at all")
    if "blocking_open" not in text:
        missing.append("factory.py: QA stage does not consume the qa verdict JSON "
                       "(no `blocking_open` binding anywhere)")
    return missing


def _check_gate_check() -> list:
    """gate_check LAUNCH must bind to the machine verdict (passed, blocking_open==0, stories>0),
    not prose. A prose 'recommend: launch' matcher WITHOUT the JSON binding is the old theatre."""
    missing = []
    if not GATECHECK.exists():
        return [f"gate_check.py missing at {GATECHECK}"]
    text = GATECHECK.read_text()
    ast.parse(text)
    if "blocking_open" not in text:
        missing.append("gate_check.py: does not read `blocking_open` from the qa verdict JSON")
    if not re.search(r"\bjson\b", text):
        missing.append("gate_check.py: never parses JSON — the gate is not bound to the machine verdict")
    if not re.search(r"total_stories|stories", text):
        missing.append("gate_check.py: does not enforce stories>0 (a zero-story run must not launch)")
    if not re.search(r"\bpassed\b", text):
        missing.append("gate_check.py: does not read the `passed` verdict")
    if re.search(r"recommend:? launch", text, re.I) and "blocking_open" not in text:
        missing.append("gate_check.py: still gates on PROSE ('recommend: launch') with no JSON binding")
    return missing


def _shared_gate_entry_ok() -> bool:
    """True iff factory exposes the ONE shared gate entry (`def run_grounded_qa`) AND that entry is in
    a file proven (by _check_factory's own probes) to invoke the scripts/qa engine. A consumer calling
    factory.run_grounded_qa is therefore consuming the engine's verdict — same gate, one implementation,
    N consumers. This is NOT a weakening: if factory's engine wiring breaks, _check_factory fails and
    this path stops counting."""
    text = FACTORY.read_text()
    tree = ast.parse(text)
    return bool(re.search(r"(?m)^def run_grounded_qa\(", text)) and _imports_engine(tree, text)


def _check_loopcontroller() -> list:
    """loopcontroller TESTQA must run the agentic engine on the build and consume its verdict
    before DELIVER — verify.verify() alone is not QA. Consumption may be direct (import scripts/qa)
    or through factory.run_grounded_qa, the shared gate entry (verified to wrap the engine)."""
    missing = []
    text = LOOPCTL.read_text()
    tree = ast.parse(text)
    if "TESTQA" not in text:
        missing.append("loopcontroller.py: TESTQA phase disappeared (rename the probe, not the gate)")
    via_shared = "run_grounded_qa" in text and _shared_gate_entry_ok()
    if not (_imports_engine(tree, text) or via_shared):
        missing.append("loopcontroller.py: TESTQA does not invoke the agentic engine (scripts/qa) — "
                       "neither directly nor via factory.run_grounded_qa")
    if "blocking_open" not in text:
        missing.append("loopcontroller.py: does not consume the qa verdict JSON "
                       "(no `blocking_open` anywhere) — DELIVER is not gated on real QA")
    return missing


def main() -> int:
    sections = [
        ("qa verdict producer (qa_report contract)", _check_producer_contract),
        ("factory QA stage", _check_factory),
        ("gate_check LAUNCH gate", _check_gate_check),
        ("loopcontroller TESTQA", _check_loopcontroller),
    ]
    broken = {}
    for name, fn in sections:
        miss = fn()
        print(("  ok " if not miss else "  UNWIRED ") + name)
        if miss:
            broken[name] = miss

    if broken:
        print(f"\nFAIL: {len(broken)} ship-gate consumer(s) do not consume the qa verdict JSON:")
        for name, miss in broken.items():
            for m in miss:
                print(f"  - {name}: {m}")
        print("\nThis guard asserts the REBUILD-PLAN C1 TARGET state. If factory.py/loopcontroller.py/"
              "gate_check.py are mid-rewrite, land the wiring — do not weaken this test.")
        return 1

    print("PASS: factory QA stage, gate_check LAUNCH and loopcontroller TESTQA all consume the "
          "qa verdict JSON (passed / blocking_open / stories>0); no self-grading shadow ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
