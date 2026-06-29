#!/usr/bin/env python3
"""test_enforcement_consistency.py — STANDING GUARD that the TWO enforcement layers agree.

agent-os enforces a role's path/tool permissions in two places that used to RE-IMPLEMENT the
same decision and kept DIVERGING (every audit round found a new allow/deny disagreement):

  * Layer A — governance.spawn_restrictions(role) + governance.validate_writes(...)   (spawn-time
              --disallowedTools / Read(<glob>) denies + the post-run write backstop / Codex path).
  * Layer B — enforce_manifest.evaluate(tool, tin, manifest, ...)                      (the
              deterministic PreToolUse hook).

Both now derive their allow/deny from control-plane/hooks/manifest_policy.py (ONE source of
truth), so for EVERY role and EVERY (tool, path) case they MUST return the same decision. This
test proves it: it computes each layer's EFFECTIVE decision across a matrix and FAILS, listing
every divergence, if they disagree. It also asserts standing invariants.

Run:  .venv/bin/python scripts/test_enforcement_consistency.py     -> PASS / FAIL
"""
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROLES = Path.home() / "projects" / "control-plane" / "roles"
HOOKS = Path.home() / "projects" / "control-plane" / "hooks"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(HOOKS))

import yaml
import governance
import enforce_manifest
import manifest_policy as mp

REPO = "/tmp/consistency_repo"

WRITE_TOOLS = ["Write", "Edit", "MultiEdit"]
READ_TOOL = "Read"

# (label, repo-relative path) write targets exercised for every write tool.
WRITE_PATHS = [
    ("src/app.py",                 "src/app.py"),
    ("docs/SPEC.md",               "docs/SPEC.md"),
    ("tests/x.py",                 "tests/x.py"),
    (".env",                       ".env"),
    ("secrets/x",                  "secrets/x"),
    ("registry/x",                 "registry/x"),
    (".github/workflows/ci.yml",   ".github/workflows/ci.yml"),
    ("index.html",                 "index.html"),
    ("package.json",               "package.json"),   # a product root file
]
READ_PATHS = [("src/app.py", "src/app.py"), (".env", ".env")]

# Hard-deny classes NO role may WRITE (effective decision must be deny for every write tool).
FORBIDDEN_WRITE = [".env", "secrets/x", "registry/x", ".github/workflows/ci.yml"]
# Roles whose core function is to READ source they audit/test/spec.
MUST_READ_SRC = {"qa-security", "product-manager", "security-appsec",
                 "security-redteam", "reviewer", "audit-governance"}


def load(role):
    return yaml.safe_load((ROLES / f"{role}.yaml").read_text()) or {}


# ---- each layer's EFFECTIVE allow/deny for a (role, tool, path) ------------------------------
def gov_decision(role, tool, path):
    """Layer A effective decision, read from governance's PUBLIC surface only."""
    sr = governance.spawn_restrictions(role)
    if tool in sr["disallowed_tools"]:
        return "deny"
    if tool in WRITE_TOOLS:
        viol = governance.validate_writes(role, REPO, [os.path.join(REPO, path)])
        return "deny" if viol else "allow"
    if tool == READ_TOOL:
        return "deny" if any(mp._match(path, g) for g in sr["deny_read"]) else "allow"
    return "allow"


def hook_decision(manifest, tool, path):
    """Layer B effective decision (the PreToolUse hook)."""
    decision, _ = enforce_manifest.evaluate(tool, {"file_path": path}, manifest, True, True)
    return decision


def main():
    roles = sorted(p.stem for p in ROLES.glob("*.yaml"))
    divergences = []
    inv_fail = []

    for role in roles:
        m = load(role)

        # 1) CONSISTENCY: both layers agree for every (tool, path).
        for tool in WRITE_TOOLS:
            for label, path in WRITE_PATHS:
                g, h = gov_decision(role, tool, path), hook_decision(m, tool, path)
                if g != h:
                    divergences.append(f"{role:24} {tool:9} {label:26} governance={g:5} hook={h}")
        for label, path in READ_PATHS:
            g, h = gov_decision(role, READ_TOOL, path), hook_decision(m, READ_TOOL, path)
            if g != h:
                divergences.append(f"{role:24} {'Read':9} {label:26} governance={g:5} hook={h}")

        # 2) INVARIANT: no role may write secrets/.env/registry/.github/workflows.
        for path in FORBIDDEN_WRITE:
            for tool in WRITE_TOOLS:
                if hook_decision(m, tool, path) != "deny":
                    inv_fail.append(f"{role}: {tool} to {path} is NOT denied (must be)")

        # 3) INVARIANT: every role that can BUILD can write its product output.
        is_builder = bool(m.get("can_modify_code")) and any(
            seg in (m.get("allowed_paths") or [])
            for seg in ("src/**", "app/**", "lib/**", "ios/**", "android/**", "game/**")
        )
        if is_builder:
            for path in ("index.html", "package.json"):
                if hook_decision(m, "Write", path) != "allow":
                    inv_fail.append(f"{role}: builder cannot write product output {path} (bricked)")

        # 4) INVARIANT: audit/security/PM roles can READ src.
        if role in MUST_READ_SRC:
            if hook_decision(m, "Read", "src/app.py") != "allow":
                inv_fail.append(f"{role}: must be able to READ src/app.py")

    print(f"enforcement-consistency: {len(roles)} roles x "
          f"{len(WRITE_TOOLS)*len(WRITE_PATHS)+len(READ_PATHS)} cases")
    if divergences:
        print(f"\n{len(divergences)} DIVERGENCE(S) between governance and enforce_manifest:")
        for d in divergences:
            print(f"  - {d}")
    if inv_fail:
        print(f"\n{len(inv_fail)} INVARIANT FAILURE(S):")
        for f in inv_fail:
            print(f"  - {f}")

    if divergences or inv_fail:
        print("\nFAIL")
        return 1
    print("\nPASS: both enforcement layers agree on every role/tool/path; invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
