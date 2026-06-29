#!/usr/bin/env python3
"""test_quality_lenses_wired.py — the META-GUARD that enforces the quality-lens triad.

Our recurring failure has ONE shape: a whole CLASS of issues ships because no STANDING check enforced
that lens (journey, then timing, then product-craft...). We kept discovering lenses reactively — one per
user complaint. This guard makes the rule from docs/QUALITY-LENS-AUDIT.md machine-enforced:

  a lens is "in the pipeline" (status: enforced) ONLY if it has all three legs —
    STANDARD (a doc or executable spec) + ROLE (a gating role names it) + GUARD (wired into selftest.sh).

For every `enforced` lens in docs/quality-lenses.yaml this asserts all three legs are present, so:
  * a lens can't silently decay from enforced -> aspirational (drop its guard -> this test goes red),
  * promoting a lens to `enforced` REQUIRES wiring all three legs,
  * `backlog` lenses (no coverage yet) are visible in the open, not discovered by a user hitting the bug.

`building` lenses are reported (not failed) — coverage is landing. Run with the project venv:
    python scripts/test_quality_lenses_wired.py
Exit 0 = every enforced lens has its full triad. Exit 1 = a lens lost a leg (or an enforced lens is
incomplete) — fix the leg, don't downgrade the lens to hide it.
"""
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
REGISTRY = REPO / "docs" / "quality-lenses.yaml"
SELFTEST = REPO / "scripts" / "selftest.sh"
ROLES = Path.home() / "projects" / "control-plane" / "roles"


def _selftest_labels(text: str) -> set:
    """Every ck/ckp check label wired into the standing suite."""
    return set(re.findall(r'ck[p]?\s+"([^"]+)"', text))


def _legs_for(lens: dict, labels: set) -> list:
    """Return the list of MISSING legs for one lens ([] == fully wired)."""
    missing = []
    # 1) STANDARD — a doc that must exist, or null (executable spec: the guard IS the standard).
    std = lens.get("standard")
    if std and not (REPO / std).exists():
        missing.append(f"standard file missing: {std}")
    # 2) ROLE — a gating role whose manifest names the lens.
    role = lens.get("role") or {}
    rf, kw = role.get("file"), (role.get("keyword") or "")
    if not rf or not kw:
        missing.append("no role mandate declared")
    else:
        rpath = ROLES / f"{rf}.yaml"
        if not rpath.exists():
            missing.append(f"role file missing: {rf}.yaml")
        elif kw.lower() not in rpath.read_text().lower():
            missing.append(f"role {rf}.yaml does not mention '{kw}'")
    # 3) GUARD — a check actually WIRED into selftest.sh by its exact label.
    g = lens.get("guard")
    if not g:
        missing.append("no guard wired")
    elif g not in labels:
        missing.append(f"guard not wired into selftest.sh: \"{g}\"")
    return missing


def main() -> int:
    reg = yaml.safe_load(REGISTRY.read_text())
    lenses = reg.get("lenses", [])
    labels = _selftest_labels(SELFTEST.read_text())

    enforced = [l for l in lenses if l.get("status") == "enforced"]
    building = [l for l in lenses if l.get("status") == "building"]
    deferred = [l for l in lenses if l.get("status") == "deferred"]
    backlog = [l for l in lenses if l.get("status") == "backlog"]

    broken = {}
    for l in enforced:                                # enforced -> must have the full triad
        miss = _legs_for(l, labels)
        if miss:
            broken[l["id"]] = miss
    for l in deferred:                                # deferred -> must carry an honest reason (no silent skips)
        if not (l.get("reason") or "").strip():
            broken[l["id"]] = ["deferred without a documented reason"]

    print(f"quality lenses: {len(enforced)} enforced, {len(deferred)} deferred, "
          f"{len(building)} building, {len(backlog)} undecided")
    if building:
        print("  building (coverage landing): " + ", ".join(l["id"] for l in building))
    for l in deferred:
        print(f"  deferred: {l['id']} — {l.get('reason', '')}")
    # 'backlog' = a lens with no decision yet. The goal is ZERO undecided: every lens is either enforced
    # or explicitly deferred-with-reason. Undecided lenses are reported but do not fail the suite.
    if backlog:
        print("  UNDECIDED (drive to enforced or deferred): " + ", ".join(l["id"] for l in backlog))

    if broken:
        print(f"\nFAIL: {len(broken)} lens(es) violate the rule (enforced must have all 3 legs; "
              f"deferred must state a reason):")
        for lid, miss in broken.items():
            for m in miss:
                print(f"  - {lid}: {m}")
        return 1

    tail = "" if not backlog else f" ({len(backlog)} undecided — see above)"
    print(f"PASS: {len(enforced)} enforced lenses have the full triad; {len(deferred)} deferred with reason{tail} ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
