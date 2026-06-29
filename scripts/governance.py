#!/usr/bin/env python3
"""governance.py — the central governance enforcement module (the STANDING GUARD's read side).

A capability flag declared in a role manifest is only LAW if some code actually READS it and
turns it into a restriction. This module is that reader: it loads every governance-bearing flag
from the role manifest (~/projects/control-plane/roles/<role>.yaml) and exposes them as:

  spawn_restrictions(role) -> {disallowed_tools, deny_read, write_scope}
      What factory._run_once must wire into the spawned `claude` subprocess:
        * disallowed_tools -> `--disallowedTools ...` (deny beats allow in CC)
        * deny_read        -> the read-side deny globs the enforce_manifest.py hook keys on
        * write_scope      -> allowed_paths, the WRITE allowlist (real restriction, not display)
  may(role, capability)        -> bool   read the matching can_*/capability flag
  approval_required_for(role)  -> list   actions this role must get human approval for
  enforce(role, capability)    -> raise PermissionError (+ audit 'GovernanceDenied') if not may()
  validate_writes(role, repo, changed_paths) -> [violations]  (+ audit) post-run write backstop

The deterministic block happens in the control-plane PreToolUse hook (enforce_manifest.py); this
module is the in-repo consumer the wiring guard (test_governance_wired.py) can see, and the
validate_writes() backstop for the Codex engine path (which does not honor claude hooks).

CLI:  governance.py selftest
      governance.py show <role>
Run with the agent-os venv python.
"""
import fnmatch
import sys
from pathlib import Path

ROLES = Path.home() / "projects" / "control-plane" / "roles"

# capability name (as used by may/enforce) -> the manifest flag that grants it.
_CAP_FLAG = {
    "deploy": "can_deploy",
    "merge_main": "can_merge_main",
    "modify_code": "can_modify_code",
    "modify_registry": "can_modify_registry",
    "open_bounded_meeting": "can_open_bounded_meeting",
    "post_publicly": "can_post_publicly",
    "request_hire": "can_request_hire",
    "spawn": "can_spawn",
    "read_secrets": "can_read_secrets",
}

# Tools that would ENABLE a capability the role lacks: if the capability flag is false, these
# tools must be denied at spawn so the agent cannot route around the missing capability. Names
# cover the in-tree CC tools plus the conventional MCP tool names a posting/deploy integration
# would expose. Deny is conservative: a role that never had the tool loses nothing.
_CAP_TOOLS = {
    "post_publicly": ["WebPublish", "SlackPost", "TwitterPost", "mcp__social__post",
                      "mcp__slack__post_message"],
    "deploy": ["Deploy", "mcp__deploy__release", "mcp__vercel__deploy"],
}
# Tools that perform code edits — denied when can_modify_code is false (read-only agent).
_EDIT_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]
# Sub-agent spawn tool — denied unless can_spawn (true only for the controller).
_SPAWN_TOOLS = ["Task"]
# Read-side secret material denied unless can_read_secrets.
_SECRET_GLOBS = [".env", ".env.*", ".env*", "secrets/**", "**/secrets/**", "**/.env*"]


def load_manifest(role: str) -> dict:
    """Parse the role manifest. Missing file -> {} (caller treats as no-grants / maximally locked)."""
    import yaml
    f = ROLES / f"{role}.yaml"
    if not f.exists():
        return {}
    return yaml.safe_load(f.read_text()) or {}


def _read_flags(m: dict) -> dict:
    """Genuinely READ every governance-bearing flag out of the manifest. Each `m.get("flag")` here
    is the single source of truth the wiring guard detects — so a declared control can never be
    silently un-consumed. Booleans default False (least privilege); lists default []."""
    return {
        "can_deploy": bool(m.get("can_deploy")),
        "can_merge_main": bool(m.get("can_merge_main")),
        "can_modify_code": bool(m.get("can_modify_code")),
        "can_modify_registry": bool(m.get("can_modify_registry")),
        "can_open_bounded_meeting": bool(m.get("can_open_bounded_meeting")),
        "can_post_publicly": bool(m.get("can_post_publicly")),
        "can_request_hire": bool(m.get("can_request_hire")),
        "can_spawn": bool(m.get("can_spawn")),
        "can_read_secrets": bool(m.get("can_read_secrets")),
        "approval_required_for": list(m.get("approval_required_for") or []),
        "allowed_paths": list(m.get("allowed_paths") or []),
        "denied_paths": list(m.get("denied_paths") or []),
        "denied_tools": list(m.get("denied_tools") or []),
    }


def flags(role: str) -> dict:
    """The normalized governance flags for a role (all 12 controls, read from the manifest)."""
    return _read_flags(load_manifest(role))


def may(role: str, capability: str) -> bool:
    """True iff the role's manifest grants `capability` (capability in _CAP_FLAG)."""
    if capability not in _CAP_FLAG:
        raise ValueError(f"unknown capability {capability!r}; known: {sorted(_CAP_FLAG)}")
    return bool(_read_flags(load_manifest(role))[_CAP_FLAG[capability]])


def approval_required_for(role: str) -> list:
    """The list of actions this role may only perform AFTER a human approval (the approval gate)."""
    return _read_flags(load_manifest(role))["approval_required_for"]


def _audit(action: str, role: str, decision: str, payload: dict) -> None:
    """Best-effort tamper-evident audit (reuse audit.py). Never blocks enforcement if the DB is down."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import audit
        audit.append(actor=f"governance:{role}", action=action, resource=role,
                     decision=decision, payload=payload)
    except Exception:
        pass


def enforce(role: str, capability: str) -> None:
    """Raise PermissionError (and record a 'GovernanceDenied' audit entry) if the role lacks the
    capability. Call this at an action site BEFORE performing a gated action (deploy/merge/hire/...)."""
    if may(role, capability):
        return
    _audit("GovernanceDenied", role, "deny",
           {"capability": capability, "flag": _CAP_FLAG.get(capability)})
    raise PermissionError(
        f"role '{role}' is not permitted to '{capability}' "
        f"({_CAP_FLAG.get(capability)} is false in its manifest) — escalate for approval/role change")


def spawn_restrictions(role: str) -> dict:
    """The per-run spawn restrictions for a role, consumed by factory._run_once:

      disallowed_tools : denied_tools  PLUS  every tool that would grant a capability the role
                         lacks (posting/deploy tools, code-edit tools when read-only, Task when
                         it may not spawn). Passed to `claude --disallowedTools` (deny beats allow).
      deny_read        : secret material (.env/secrets) when not can_read_secrets, PLUS denied_paths
                         — the read-side denies the enforce_manifest.py hook keys on.
      write_scope      : allowed_paths — the WRITE allowlist (real restriction): the post-run
                         validator and the hook's allowlist branch both reject writes outside it.
    """
    f = _read_flags(load_manifest(role))

    disallowed = list(f["denied_tools"])
    if not f["can_post_publicly"]:
        disallowed += _CAP_TOOLS["post_publicly"]
    if not f["can_deploy"]:
        disallowed += _CAP_TOOLS["deploy"]
    if not f["can_modify_code"]:
        disallowed += _EDIT_TOOLS
    if not f["can_spawn"]:
        disallowed += _SPAWN_TOOLS
    # de-dup, preserve order
    seen, disallowed_tools = set(), []
    for t in disallowed:
        if t not in seen:
            seen.add(t)
            disallowed_tools.append(t)

    deny_read = []
    if not f["can_read_secrets"]:
        deny_read += _SECRET_GLOBS
    deny_read += f["denied_paths"]
    seen, deny_read_dedup = set(), []
    for g in deny_read:
        if g not in seen:
            seen.add(g)
            deny_read_dedup.append(g)

    return {
        "disallowed_tools": disallowed_tools,
        "deny_read": deny_read_dedup,
        "write_scope": list(f["allowed_paths"]),   # allowed_paths is a real WRITE restriction
    }


def _match(path: str, pat: str) -> bool:
    """Glob match a (repo-relative) path against a manifest glob, matching at any depth like the hook."""
    return (fnmatch.fnmatch(path, pat)
            or fnmatch.fnmatch(path, "**/" + pat)
            or fnmatch.fnmatch(path, pat.rstrip("/") + "/**"))


def validate_writes(role: str, repo: str, changed_paths) -> list:
    """Post-run WRITE backstop (secondary to the PreToolUse hook; primary for the Codex engine which
    does not honor claude hooks). Flags every changed path that is EITHER in denied_paths OR outside
    every allowed_paths glob (allowlist semantics). Records each as a 'GovernanceWriteViolation' audit
    entry and returns the list of violations: [{"path","reason","matched"}]. The caller reverts/flags
    them. allowed_paths empty => no allowlist restriction (only the denylist applies)."""
    f = _read_flags(load_manifest(role))
    allowed, denied = f["allowed_paths"], f["denied_paths"]
    repo_p = Path(repo)
    violations = []
    for raw in changed_paths:
        p = str(raw)
        try:                                            # normalize to a repo-relative path for globbing
            rel = str(Path(p).resolve().relative_to(repo_p.resolve()))
        except Exception:
            rel = p[len(str(repo_p)):].lstrip("/") if p.startswith(str(repo_p)) else p
        hit = next((d for d in denied if _match(rel, d)), None)
        if hit:
            violations.append({"path": rel, "reason": "denied_path", "matched": hit})
        elif allowed and not any(_match(rel, a) for a in allowed):
            violations.append({"path": rel, "reason": "outside_allowed_paths", "matched": None})
    if violations:
        _audit("GovernanceWriteViolation", role, "flag",
               {"repo": repo_p.name, "violations": violations[:10], "count": len(violations)})
    return violations


# --------------------------------------------------------------------------- selftest
def _selftest() -> int:
    """Prove the reads turn into real restrictions. Exits 0 on PASS."""
    problems = []

    # 1) denied_tools surface in disallowed_tools (resource-allocator denies WebFetch/WebSearch).
    ra = spawn_restrictions("resource-allocator")
    for t in ("WebFetch", "WebSearch"):
        if t not in ra["disallowed_tools"]:
            problems.append(f"denied_tools not enforced: {t} missing from resource-allocator disallowed_tools")

    # 2) can_deploy:false -> may() False AND enforce() raises (builder cannot deploy).
    if may("builder", "deploy"):
        problems.append("may('builder','deploy') should be False (can_deploy:false)")
    raised = False
    try:
        enforce("builder", "deploy")
    except PermissionError:
        raised = True
    if not raised:
        problems.append("enforce('builder','deploy') should raise PermissionError")

    # ...but a role WITH the capability passes (devops-sre can_deploy:true).
    if not may("devops-sre", "deploy"):
        problems.append("may('devops-sre','deploy') should be True (can_deploy:true)")

    # 3) allowed_paths drives write_scope (builder: src/**, tests/**, docs/**).
    b = spawn_restrictions("builder")
    if b["write_scope"] != ["src/**", "tests/**", "docs/**"]:
        problems.append(f"write_scope should be builder allowed_paths, got {b['write_scope']}")

    # 4) can_read_secrets:false adds secret read-denies (builder).
    if not any(".env" in g or "secrets" in g for g in b["deny_read"]):
        problems.append("can_read_secrets:false must add .env/secrets to deny_read")
    # builder denied_paths also propagate into deny_read.
    if not any("registry" in g for g in b["deny_read"]):
        problems.append("denied_paths should propagate into deny_read")

    # 5) can_modify_code:false makes the agent read-only (resource-allocator).
    if "Edit" not in ra["disallowed_tools"] or "Write" not in ra["disallowed_tools"]:
        problems.append("can_modify_code:false must disallow Edit/Write (read-only)")

    # 6) only the controller may spawn -> Task denied for everyone else, allowed for controller.
    if "Task" not in b["disallowed_tools"]:
        problems.append("can_spawn:false must disallow Task for builder")
    if "Task" in spawn_restrictions("controller")["disallowed_tools"]:
        problems.append("controller (can_spawn:true) must NOT have Task disallowed")

    # 7) validate_writes flags an out-of-scope / denied write but passes an in-scope one.
    repo = "/tmp/repo"
    v = validate_writes("builder", repo, [f"{repo}/src/app.py", f"{repo}/registry/leases.yaml",
                                          f"{repo}/infra/deploy.sh"])
    paths = {x["path"]: x["reason"] for x in v}
    if "src/app.py" in paths:
        problems.append("validate_writes wrongly flagged an in-scope write src/app.py")
    if paths.get("registry/leases.yaml") != "denied_path":
        problems.append("validate_writes must flag registry/ as denied_path")
    if paths.get("infra/deploy.sh") != "outside_allowed_paths":
        problems.append("validate_writes must flag infra/deploy.sh as outside allowed_paths")

    # 8) approval_required_for is read straight from the manifest.
    if "deploy" not in approval_required_for("controller"):
        problems.append("approval_required_for(controller) should include 'deploy'")

    print("governance.selftest")
    print(f"  resource-allocator disallowed_tools : {ra['disallowed_tools']}")
    print(f"  builder write_scope                 : {b['write_scope']}")
    print(f"  builder deny_read                   : {b['deny_read']}")
    print(f"  may(builder,deploy)                 : {may('builder','deploy')}")
    print(f"  may(devops-sre,deploy)              : {may('devops-sre','deploy')}")
    print(f"  approval_required_for(controller)   : {approval_required_for('controller')}")
    print(f"  validate_writes(builder, ...)       : {[ (x['path'], x['reason']) for x in v ]}")
    if problems:
        print("\nFAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nPASS: every declared governance flag reads into a real spawn/write/action restriction.")
    return 0


def _main(a):
    if a and a[0] == "selftest":
        return _selftest()
    if len(a) >= 2 and a[0] == "show":
        import json
        role = a[1]
        print(json.dumps({"flags": flags(role), "spawn_restrictions": spawn_restrictions(role),
                          "approval_required_for": approval_required_for(role)}, indent=2))
        return 0
    print("usage: governance.py selftest | show <role>")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
