#!/usr/bin/env python3
"""mast.py — the MAST failure-mode taxonomy as a reusable checklist (IMPROVEMENTS-PLAN item 8).

"Why Do Multi-Agent LLM Systems Fail?" (MAST, arXiv 2503.13657, NeurIPS 2025) derived — from 1600+ annotated
real multi-agent traces — a taxonomy of 14 fine-grained failure modes in 3 categories, and showed the failures
are STRUCTURAL (prompt tweaks give only single-digit gains). We encode it once here so two places can consult
the same checklist:
  * the skeptical work-execution AUDITOR (review.py) — as explicit things a run can fail on (esp. the
    task-verification category, which the auditor IS the backstop for);
  * spawn/verification gates in the runtime — as a pre-flight the org can check a plan/hand-off against.

This is a CHECKLIST, not an oracle: it names the failure classes to look for; the judgement stays with the
auditor/gate. Grounded, not decorative — each mode is phrased as a concrete question a reviewer would ask.
"""

# category -> [(code, short, the question a reviewer asks)]
TAXONOMY = {
    "system-design (specification)": [
        ("1.1", "disobey task spec", "Did an agent ignore or violate the stated task requirements/constraints?"),
        ("1.2", "disobey role spec", "Did an agent act outside its assigned role (do another agent's job)?"),
        ("1.3", "step repetition", "Did an agent needlessly repeat a step already completed?"),
        ("1.4", "lost history", "Was earlier conversation/context lost so work was redone or contradicted?"),
        ("1.5", "unaware of termination", "Did an agent not know when it was allowed to stop (ran on / stopped blind)?"),
    ],
    "inter-agent misalignment (coordination)": [
        ("2.1", "conversation reset", "Did a hand-off drop context so the receiver started cold?"),
        ("2.2", "no clarification", "Did an agent proceed on an ambiguous task instead of asking?"),
        ("2.3", "task derailment", "Did the work drift away from the actual objective?"),
        ("2.4", "info withheld", "Did an agent fail to pass on a fact a sibling needed?"),
        ("2.5", "input ignored", "Did an agent ignore another agent's finding/correction?"),
        ("2.6", "reasoning-action mismatch", "Did an agent's action contradict its own stated reasoning?"),
    ],
    "task verification/termination": [
        ("3.1", "premature termination", "Did the run stop before the objective was actually met?"),
        ("3.2", "no/incomplete verification", "Was the output shipped without (adequate) verification?"),
        ("3.3", "incorrect verification", "Did verification pass something that was actually wrong?"),
    ],
}

# flat list of (code, category, short, question)
MODES = [(code, cat, short, q) for cat, items in TAXONOMY.items() for (code, short, q) in items]


def checklist(categories=None) -> str:
    """A compact, promptable checklist. `categories` filters (e.g. ['task verification/termination'] for the
    auditor's verification backstop); default = all 14 modes."""
    lines = []
    for cat, items in TAXONOMY.items():
        if categories and cat not in categories:
            continue
        lines.append(f"{cat}:")
        lines += [f"  - [{code}] {short} — {q}" for (code, short, q) in items]
    return "\n".join(lines)


def codes() -> list:
    return [code for (code, *_ ) in MODES]


def _selftest():
    assert len(MODES) == 14, f"MAST has 14 modes, got {len(MODES)}"
    assert len(TAXONOMY) == 3, "3 categories"
    cl = checklist(["task verification/termination"])
    assert "premature termination" in cl and "disobey task spec" not in cl, "category filter works"
    assert "3.1" in codes() and "2.6" in codes(), codes()
    full = checklist()
    assert full.count("- [") == 14, "full checklist lists every mode"
    print("mast selftest: PASS (14 modes / 3 categories; checklist + category filter)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
