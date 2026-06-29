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
  require_approval(role, action, payload, approval_id) -> raise PermissionError unless a hash-pinned,
      unexpired, single-use HUMAN approval (control-plane verify_approval.py) matches the EXACT
      payload — the action site that actually GATES deploy/spend/secret behind approval_required_for.
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
# Tools that perform file edits, split by how a read-only role (can_modify_code:false) is treated:
#   _EDIT_TOOLS_ALWAYS : MultiEdit/NotebookEdit — NO read-only role (doc/test author) needs these;
#                        denied unconditionally whenever can_modify_code is false.
#   _EDIT_TOOLS_SCOPED : Edit/Write — denied too UNLESS the role has a BOUNDED write scope (a
#                        concrete non-['**'] allowed_paths like docs/**, tests/**, tasks/**), so
#                        legitimate non-code authoring (the SPEC stage's docs/SPEC.md, qa-security's
#                        tests/**) is not bricked. A FULLY read-only role (empty allowed_paths) OR
#                        one scoped to the UNBOUNDED ['**'] wildcard (reviewer/audit-governance/
#                        security-appsec — auditors that mutate via flock'd scripts, not freehand
#                        edits) loses Edit/Write too.
_EDIT_TOOLS_ALWAYS = ["MultiEdit", "NotebookEdit"]
_EDIT_TOOLS_SCOPED = ["Edit", "Write"]
_EDIT_TOOLS = _EDIT_TOOLS_SCOPED + _EDIT_TOOLS_ALWAYS   # full set (kept for back-compat / docs)
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


# Control-plane Article VI human-approval verifier (hash-pin + expiry + single-use). It is the
# single source of truth for "did a human approve THIS exact payload"; we never re-implement the
# store, we read/consume its records so an approval can gate at most one action.
_VERIFY_APPROVAL = Path.home() / "projects" / "control-plane" / "scripts" / "verify_approval.py"


def _verify_approval_mod():
    """Load verify_approval.py by path WITHOUT polluting sys.path (avoids shadowing agent-os modules
    like audit). Raises if the verifier is absent — require_approval() turns that into a DENY so a
    gated action can never proceed when the approval machinery is unreachable (fail-closed)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("verify_approval", _VERIFY_APPROVAL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def require_approval(role: str, action: str, payload, approval_id: str = None) -> dict:
    """The MISSING gate (#40/#22/#36): turn approval_required_for from a paper list into a real block.

    Call this at an action site BEFORE a gated action (factory LAUNCH/deploy, budget spend, vault
    secret read). If `action` is NOT in this role's approval_required_for, it is ungated and returns
    immediately ({"gated": False}). If it IS gated, the action may proceed ONLY if a HUMAN approval
    exists that is (a) status 'approved', (b) not expired, (c) hash-pinned to the EXACT `payload`
    about to run, and (d) not already consumed — verified against control-plane verify_approval.py,
    which we then CONSUME (single-use) so the same approval can't silently gate a second action.

    FAIL-CLOSED by design: a missing approval_id, no record, wrong status, expiry, hash mismatch, OR
    any error reaching/loading the verifier all DENY (raise PermissionError) and audit. A deploy /
    spend / secret-read whose approval cannot be positively verified MUST NOT proceed — denying here
    only blocks gated, high-blast-radius actions (the role declared them needing approval), so this
    is safe for liveness: ungated work is untouched.
    """
    import json
    if action not in approval_required_for(role):
        return {"gated": False, "role": role, "action": action}

    payload_text = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True)
    try:
        if not approval_id:
            raise PermissionError("no approval_id supplied for a gated action")
        va = _verify_approval_mod()                  # may raise -> caught below -> DENY (fail-closed)
        rec_path = va._path(approval_id)
        if not rec_path.exists():
            raise PermissionError(f"no such approval {approval_id!r}")
        rec = json.loads(rec_path.read_text())
        if rec.get("status") != "approved":
            raise PermissionError(f"approval status={rec.get('status')!r} (need 'approved')")
        import datetime
        if va._now() > datetime.datetime.fromisoformat(rec["expires_at"]):
            rec["status"] = "expired"; rec_path.write_text(json.dumps(rec, indent=2))
            raise PermissionError("approval expired")
        if va._hash(payload_text) != rec.get("payload_hash"):
            raise PermissionError("payload hash mismatch — a human approved A, this is A-prime")
        # single-use: consume so this approval cannot gate a second action.
        rec["status"] = "consumed"; rec["consumed_at"] = va._now().isoformat()
        rec["consumed_for"] = {"role": role, "action": action}
        rec_path.write_text(json.dumps(rec, indent=2))
    except PermissionError as e:
        _audit("ApprovalDenied", role, "deny",
               {"action": action, "approval_id": approval_id, "reason": str(e)})
        raise PermissionError(
            f"role '{role}' may '{action}' only after a verified human approval — none valid "
            f"(approval_id={approval_id!r}): {e}")
    except Exception as e:                            # store unreachable / verifier missing / malformed
        _audit("ApprovalDenied", role, "deny",
               {"action": action, "approval_id": approval_id, "reason": f"verify error: {e}"})
        raise PermissionError(
            f"role '{role}' action '{action}' approval could not be verified, denying (fail-closed): {e}")

    _audit("ApprovalGranted", role, "allow", {"action": action, "approval_id": approval_id})
    return {"gated": True, "approved": True, "role": role, "action": action,
            "approval_id": approval_id}


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
    # can_modify_code:false forbids authoring CODE — but docs/specs/tests written WITHIN a role's
    # declared BOUNDED write scope are NOT code, so the edit-tool deny is graduated rather than
    # all-or-nothing:
    #   * MultiEdit/NotebookEdit are ALWAYS denied for a read-only role — no doc/test author needs
    #     them (Edit/Write suffice), so they must never leak into --disallowedTools just because the
    #     role has a write scope. (This closes the post-77793f0 regression where a non-empty
    #     allowed_paths skipped the WHOLE _EDIT_TOOLS block, omitting MultiEdit/NotebookEdit — and,
    #     for ['**'] roles with empty denied_tools, Edit/Write too — from the deny layer.)
    #   * Edit/Write are denied too UNLESS the role has a BOUNDED scope (non-empty allowed_paths that
    #     is not the unbounded ['**'] wildcard). product-manager (docs/**, tasks/**) and qa-security
    #     (tests/**) keep them to author the SPEC/test stages; a FULLY read-only role (empty
    #     allowed_paths) OR an unbounded ['**'] auditor scope (security-appsec/reviewer/
    #     audit-governance — they mutate via flock'd scripts, not freehand edits) loses them too.
    # This makes the --disallowedTools layer match the PreToolUse hook's tools-allowlist (defense in
    # depth) instead of relying on each read-only role's (often incomplete) denied_tools list.
    if not f["can_modify_code"]:
        disallowed += _EDIT_TOOLS_ALWAYS
        bounded_scope = f["allowed_paths"] and "**" not in f["allowed_paths"]
        if not bounded_scope:
            disallowed += _EDIT_TOOLS_SCOPED
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

    # 5) a FULLY read-only role disallows Edit/Write. resource-allocator (can_modify_code:false,
    #    mutates the registry only via a flock'd script) declares this explicitly via denied_tools.
    if "Edit" not in ra["disallowed_tools"] or "Write" not in ra["disallowed_tools"]:
        problems.append("resource-allocator (read-only) must disallow Edit/Write")

    # 5b) REGRESSION GUARD (SPEC-stage break): can_modify_code:false must NOT blanket-deny edits for a
    #     role that HAS a declared write scope — product-manager authors docs/SPEC.md within
    #     allowed_paths, the first stage of the build line. Denying Edit/Write here would brick it.
    pm = spawn_restrictions("product-manager")
    for t in ("Edit", "Write"):
        if t in pm["disallowed_tools"]:
            problems.append(f"product-manager (has write scope) must KEEP {t} to author docs/SPEC.md")
    if not pm["write_scope"]:
        problems.append("product-manager must have a non-empty write_scope (allowed_paths)")
    # ...but MultiEdit/NotebookEdit are STILL denied for a read-only role even WITH a write scope:
    # no doc/test author needs them, and they must not leak back into --disallowedTools.
    for t in ("MultiEdit", "NotebookEdit"):
        if t not in pm["disallowed_tools"]:
            problems.append(f"product-manager (read-only) must still disallow {t}")

    # 5c) REGRESSION GUARD (post-77793f0 edit-tool leak): a read-only role whose write scope is the
    #     UNBOUNDED ['**'] wildcard (security-appsec, denied_tools:[]) must have ALL FOUR edit tools
    #     denied in the --disallowedTools layer — a '**' scope is not a bounded doc/test scope, and
    #     this layer must match the PreToolUse hook's tools-allowlist rather than rely on the role's
    #     denied_tools (which is empty here).
    sa = spawn_restrictions("security-appsec")
    for t in _EDIT_TOOLS:
        if t not in sa["disallowed_tools"]:
            problems.append(f"security-appsec (read-only, ['**'] scope) must disallow {t}")

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

    # 9) require_approval ACTUALLY GATES (#40/#22/#36): the Article VI verifier is invoked, the gate
    #    is fail-closed, hash-pinned, and single-use.
    import json as _json
    import uuid as _uuid
    import datetime as _dt
    appr_gate = {"ungated_ok": False, "deny_no_appr": False, "allow_valid": False,
                 "deny_reuse": False, "deny_hash": False}
    created = []
    try:
        va = _verify_approval_mod()                  # proves verify_approval.py is reachable + wired
        payload = "deploy NoUpload@v1.2.3 to production"

        # (a) a non-listed action is ungated -> allowed with no approval at all.
        appr_gate["ungated_ok"] = not require_approval("controller", "not_a_gated_action", payload).get("gated")

        # (b) a GATED action with NO approval is DENIED (fail-closed).
        try:
            require_approval("controller", "deploy", payload)        # no approval_id
        except PermissionError:
            appr_gate["deny_no_appr"] = True

        # (c) a GATED action WITH a valid hash-pinned approval is ALLOWED, then CONSUMED (single-use).
        def _mk(aid, pinned):
            p = va._path(aid); created.append(p)
            p.write_text(_json.dumps({
                "approval_id": aid, "action": "deploy", "risk_tier": 2,
                "exact_command_or_diff": pinned, "payload_hash": va._hash(pinned),
                "requested_by": "selftest", "created_at": va._now().isoformat(),
                "expires_at": (va._now() + _dt.timedelta(minutes=10)).isoformat(),
                "status": "approved", "decided_by": "selftest"}, indent=2))
            return p

        aid = f"APR-selftest-{_uuid.uuid4().hex[:8]}"; _mk(aid, payload)
        appr_gate["allow_valid"] = bool(require_approval("controller", "deploy", payload,
                                                         approval_id=aid).get("approved"))
        # the SAME approval cannot gate a second deploy (now consumed).
        try:
            require_approval("controller", "deploy", payload, approval_id=aid)
        except PermissionError:
            appr_gate["deny_reuse"] = True

        # (d) a DIFFERENT payload under an approval pinned to the original is DENIED (hash-pin).
        aid2 = f"APR-selftest-{_uuid.uuid4().hex[:8]}"; _mk(aid2, payload)
        try:
            require_approval("controller", "deploy", payload + " --force", approval_id=aid2)
        except PermissionError:
            appr_gate["deny_hash"] = True
    finally:
        for p in created:
            try:
                p.unlink()
            except Exception:
                pass
    for k, ok in appr_gate.items():
        if not ok:
            problems.append(f"require_approval gate failed check: {k}")

    print("governance.selftest")
    print(f"  resource-allocator disallowed_tools : {ra['disallowed_tools']}")
    print(f"  builder write_scope                 : {b['write_scope']}")
    print(f"  builder deny_read                   : {b['deny_read']}")
    print(f"  may(builder,deploy)                 : {may('builder','deploy')}")
    print(f"  may(devops-sre,deploy)              : {may('devops-sre','deploy')}")
    print(f"  approval_required_for(controller)   : {approval_required_for('controller')}")
    print(f"  require_approval gate (ungate/deny/allow/reuse/hash) : {list(appr_gate.values())}")
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
