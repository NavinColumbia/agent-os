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

# NotebookEdit is a first-class member of WRITE_TOOLS in BOTH layers (manifest_policy.WRITE_TOOLS
# and enforce_manifest.WRITE_TOOLS), so the guard MUST exercise it or it cannot see a NotebookEdit
# allow/deny divergence (nor that NotebookEdit honors the hard-deny set). It is enforced identically
# to MultiEdit (neither is in WRITE_AUTHOR_TOOLS), so adding it is consistency-neutral and GREEN.
WRITE_TOOLS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
READ_TOOL = "Read"

# (label, write target) exercised for every write tool. The two layers reduce EVERY write target to
# manifest_policy.write_decision on the same path, so for every (role, tool, path) they MUST agree.
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
    # OUT-OF-REPO / cross-tenant / hook-disabling write targets. Previously the matrix passed ONLY
    # clean repo-relative strings, so it could not observe that allowed_paths=['**'] roles are
    # contained by the 4 denied_paths globs and NOTHING ELSE (an absolute or '..' target out of the
    # repo is permitted). Both layers normalize to the same write_decision call, so they still AGREE
    # here (consistency stays GREEN); whether the agreed decision is SAFE is tracked separately below.
    ("ABS ~/.claude/settings",     "/home/swami/.claude/settings.json"),
    ("ABS /etc/passwd",            "/etc/passwd"),
    ("DOTDOT ../tenant/secrets",   "../other-tenant/secrets.txt"),
    ("DOTDOT ../../hooks/enforce", "../../control-plane/hooks/enforce_manifest.py"),
]
READ_PATHS = [("src/app.py", "src/app.py"), (".env", ".env")]

# Hard-deny classes NO role may WRITE (effective decision must be deny for every write tool).
FORBIDDEN_WRITE = [".env", "secrets/x", "registry/x", ".github/workflows/ci.yml"]

# Bash command-string rails are a HOOK-ONLY constitutional check: governance.py has NO Layer-A
# counterpart, so Bash is NOT part of the two-layer CONSISTENCY matrix. But the guard must EXERCISE
# it or it cannot see that Bash redirection bypasses the path hard-deny (the old HOLE-1). The hook
# routes each Bash OUTPUT target through the SAME manifest_policy.is_denied_path the Edit/Write path
# uses (enforce_manifest._bash_write_targets), so a write to a hard-deny class is denied for EVERY
# role regardless of the reader/writer binary. These MUST be denied for every role:
BASH_DENY_ALL = [
    "echo SECRET > .env",                      # redirect to a secret file
    "echo k > secrets/key",                    # redirect into secrets/
    "echo evil > .github/workflows/ci.yml",    # redirect into CI/CD workflow defs
    "echo x > registry/leases.yaml",           # registry redirection (allocator writes via flock'd script)
    "curl http://evil.example",                # network egress
    "git push --force origin main",            # protected-ref force-push
]
# Bash SECRET-READ is path-keyed and can_read_secrets-GATED (SECRET_PATH_IN_BASH), mirroring the
# Read/NotebookRead read-deny — so head/tail/grep/source/od/base64/`< .env` cannot bypass a cat-only
# guard. Asserted as a GATED invariant: denied iff the role lacks can_read_secrets, allowed if it has it.
BASH_SECRET_READ = ["cat .env", "head .env"]
# Out-of-repo / cross-tenant / hook-disabling WRITE targets that the manifest layer now CONTAINS
# (manifest_policy: '..'-traversal deny for every role + the .claude/.ssh/.aws absolute hard-deny set
# + '**' is repo-local, never a control-plane target). Asserted as hard DENY invariants for every role.
OUT_OF_REPO_DENIED = [
    "/home/swami/.claude/settings.json",              # user-global hook wiring -> self-disable
    "../other-tenant/secrets.txt",                    # '..' cross-tenant escape
    "../../control-plane/hooks/enforce_manifest.py",  # rewrite the enforcement layer itself
]
# RESIDUAL (tracked, owner: manifest_policy.write_decision): a BARE absolute host path (no '..', not a
# .claude/.ssh/.aws cred class) is still writable under an allowed_paths ['**'] scope — the policy does
# not pin '**' to the repo root, so only the runtime sandbox bounds it. The guard MEASURES this rather
# than silently skipping the input; if it is ever closed for all roles it nudges you to promote it.
OUT_OF_REPO_RESIDUAL = ["/etc/passwd"]
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


def hook_bash(manifest, command):
    """Layer B effective decision for a Bash COMMAND string (the hook-only command-pattern rail).
    Governance has no equivalent, so this is exercised as an invariant/exposure check, not a
    cross-layer consistency check."""
    decision, _ = enforce_manifest.evaluate("Bash", {"command": command}, manifest, True, True)
    return decision


def main():
    roles = sorted(p.stem for p in ROLES.glob("*.yaml"))
    divergences = []
    inv_fail = []
    # RESIDUAL EXPOSURE ledger (tracked; the actual hole lives in manifest_policy.write_decision,
    # NOT this guard). 'exposed' = the manifest layer fails to contain it; 'closed' = now denied
    # (policy was hardened) -> the guard nudges you to promote it into a hard invariant.
    path_exposed, path_closed = [], []

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

        # 5) INVARIANT: Bash hard-deny parity (hook-only rail; previously the matrix NEVER passed a
        #    Bash command, hiding that `echo x > .env` bypassed the path hard-deny). Every role — Bash-
        #    capable or not — must DENY these (the tool gate denies Bash entirely for non-Bash roles;
        #    the Bash write-target / egress / force-push rails deny them for Bash-capable roles).
        for cmd in BASH_DENY_ALL:
            if hook_bash(m, cmd) != "deny":
                inv_fail.append(f"{role}: Bash {cmd!r} is NOT denied (must be)")

        # 6) INVARIANT: Bash secret-READ is can_read_secrets-gated and path-keyed (head/tail/... cannot
        #    bypass a cat-only guard). Assert only for Bash-capable roles (a non-Bash role's Bash is
        #    denied wholesale at the tool gate, which would mask the gating).
        if not mp.tool_denied(m, "Bash"):
            want = "allow" if m.get("can_read_secrets") else "deny"
            for cmd in BASH_SECRET_READ:
                if hook_bash(m, cmd) != want:
                    inv_fail.append(f"{role}: Bash {cmd!r} expected {want} "
                                    f"(can_read_secrets={bool(m.get('can_read_secrets'))})")

        # 7) INVARIANT: out-of-repo / cross-tenant / hook-disabling writes are contained for EVERY role
        #    ('..'-traversal deny + the .claude/.ssh/.aws absolute hard-deny set). Was a total blind
        #    spot (no absolute / '..' target was ever passed).
        for tgt in OUT_OF_REPO_DENIED:
            if hook_decision(m, "Write", tgt) != "deny":
                inv_fail.append(f"{role}: out-of-repo write to {tgt} is NOT denied (must be)")

        # 8) EXPOSURE: the bare-absolute-host-path residual the policy does NOT yet contain under a
        #    ['**'] scope. Recorded (never silently skipped) so the guard stops advertising 'safe'.
        for tgt in OUT_OF_REPO_RESIDUAL:
            (path_closed if hook_decision(m, "Write", tgt) == "deny" else path_exposed).append((role, tgt))

    print(f"enforcement-consistency: {len(roles)} roles x "
          f"{len(WRITE_TOOLS)*len(WRITE_PATHS)+len(READ_PATHS)} cases "
          f"(+ {len(BASH_DENY_ALL)+len(BASH_SECRET_READ)} Bash rails + "
          f"{len(OUT_OF_REPO_DENIED)} out-of-repo invariants/role)")
    if divergences:
        print(f"\n{len(divergences)} DIVERGENCE(S) between governance and enforce_manifest:")
        for d in divergences:
            print(f"  - {d}")
    if inv_fail:
        print(f"\n{len(inv_fail)} INVARIANT FAILURE(S):")
        for f in inv_fail:
            print(f"  - {f}")

    # ---- RESIDUAL EXPOSURE (tracked; NOT a guard failure) ------------------------------------
    # Both layers AGREE on these (consistency holds) but AGREE to ALLOW — the containment gap lives in
    # manifest_policy.write_decision (a bare absolute host path is not pinned out of a '**' scope), not
    # in this guard's file. Surfacing it is the antidote to the old false assurance: the guard now
    # NAMES the residual instead of skipping the inputs (absolute / '..' paths) that reveal it.
    if path_exposed:
        tgts = sorted({t for _, t in path_exposed})
        n = len({r for r, _ in path_exposed})
        print("\nRESIDUAL EXPOSURE — tracked, NOT contained by the manifest layer "
              "(owner: manifest_policy.write_decision):")
        print(f"  bare absolute host path WRITE ALLOWED for {n} role(s) with allowed_paths ['**']; "
              f"targets: {tgts}")
        print("  -> only the runtime sandbox (no host-FS write outside the workspace) bounds it; pin "
              "'**' to the repo root in manifest_policy to close, THEN promote into OUT_OF_REPO_DENIED.")
    # Self-healing nudge: once the policy is hardened, tell the maintainer to tighten this guard.
    elif OUT_OF_REPO_RESIDUAL:
        print("\nNOTE: every tracked residual target is now DENIED for all roles — "
              "promote OUT_OF_REPO_RESIDUAL into OUT_OF_REPO_DENIED as a hard invariant.")

    if divergences or inv_fail:
        print("\nFAIL")
        return 1
    if path_exposed:
        print("\nPASS (with TRACKED RESIDUAL EXPOSURE above): the two layers AGREE on every "
              "role/tool/path and every hard invariant holds (incl. Bash redirection, secret-read "
              "gating, '..'/absolute out-of-repo writes) — but a bare absolute host path under a '**' "
              "scope is not yet contained, so this is NOT 'provably safe'.")
        return 0
    print("\nPASS: both enforcement layers agree on every role/tool/path; all invariants hold "
          "(incl. Bash hard-deny parity, secret-read gating, out-of-repo containment).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
