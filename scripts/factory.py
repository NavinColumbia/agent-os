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
VENV_PY = str(Path.home() / "projects" / "agent-os" / ".venv" / "bin" / "python")
MAX_FIX = 3  # bounded QA->BUILD re-flow attempts
# Global backpressure: no matter how many builds run concurrently, total live agent subprocesses are
# capped here (each ~430MB) so a big fleet can't exhaust RAM or hammer the API into rate-limits.
_AGENT_SEM = threading.BoundedSemaphore(int(os.environ.get("AOS_MAX_AGENTS", "8")))
# Model policy (pinned for reproducibility; cheaper model for low-stakes stages; fallback on overload).
BUILD_MODEL = os.environ.get("AOS_BUILD_MODEL", "claude-opus-4-8")
CHEAP_MODEL = os.environ.get("AOS_CHEAP_MODEL", "claude-haiku-4-5-20251001")
FALLBACK_MODEL = os.environ.get("AOS_FALLBACK_MODEL", "claude-sonnet-4-6")
# Tools every factory agent may use beyond auto-accepted file edits. Web is on by default so research/
# intel/build agents can reach live data instead of guessing. Override per-deployment with AOS_AGENT_TOOLS
# (space-separated), or per-call via agent(..., tools=[...]). SECURITY: web access turns an agent into a
# potential exfiltration path under prompt-injection — for agents processing UNTRUSTED tenant input, pass
# tools=[] (charters are already sanitized by sanitize.py; this is the second layer of that defense).
AGENT_TOOLS = os.environ.get("AOS_AGENT_TOOLS", "WebSearch WebFetch").split()
_TRANSIENT = ("overloaded", "rate limit", "rate_limit", "429", "529", "503", "timeout", "temporarily")


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


def _extract_json(text):
    import re
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


def _estimate_runtime(role, task, env):
    """Callee-driven handshake: ask the agent itself how long this task will take and how many retries
    it warrants, BEFORE committing to it. The caller then honors that number (× a safety margin) instead
    of guessing from prompt length. Sanity-bounded so a bad estimate can't hang forever."""
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


def _run_once(role, repo, prompt, timeout, env, model, tools=None):
    cmd = ["claude", "-p", prompt, "--permission-mode", "acceptEdits", "--output-format", "json",
           "--model", model, "--fallback-model", FALLBACK_MODEL]   # pin + auto-fallback on overload
    # Grant the agent the tools its job needs. File edits already flow via acceptEdits; non-edit tools
    # (WebSearch/WebFetch and friends) must be allow-listed or the agent can't reach them. Additive —
    # builders keep Edit/Write AND gain web. Configure the default set with AOS_AGENT_TOOLS.
    grant = tools if tools is not None else AGENT_TOOLS
    if grant:
        cmd += ["--allowedTools", *grant]
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout, env=env)
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


def agent(role: str, repo: str, task: str, timeout: int = None, retries: int = None, model: str = None,
          tools: list = None) -> dict:
    """Run one role-specialized agent (headless claude), RESILIENTLY. Timeout + retry from the CALLEE's
    own estimate (pre-flight handshake). Model is pinned (reproducible) with --fallback-model on overload;
    low-stakes stages pass a cheaper model. A timeout no longer kills the stage (retry w/ backoff),
    transient errors back off longer, a bad BYO key fails fast, exhausted retries escalate."""
    model = model or BUILD_MODEL
    # NB: no home-grown context handling — the agent CLI (claude/codex) manages its own context window
    # (agentic file search, on-demand reads, compaction) far better than a bolt-on retrieval layer would.
    prompt = f"{role_brief(role)}\n\nTASK:\n{task}\n\nWork now; create/edit files directly."
    env = None
    key = getattr(_ctx, "api_key", None)
    if key:
        env = {**os.environ, "ANTHROPIC_API_KEY": key}
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
            continue
        dt = round(time.time() - t0, 1)
        audit.append(actor=f"factory:{role}", action="AgentRun", resource=Path(repo).name,
                     decision="executed", payload={"rc": rc, "attempt": attempt + 1, "cost_usd": cost, "model": used})
        _trace("agent", role, prompt, out_text, rc, dt, cost, tin, tout, used)
        if key and "Invalid API key" in out_text:       # bad BYO key — don't waste retries
            return {"rc": rc, "out": out_text[-1500:], "failed": True, "reason": "invalid BYO key"}
        if rc == 0 and out_text.strip():
            return {"rc": 0, "out": out_text[-1500:], "cost_usd": cost, "tokens_in": tin,
                    "tokens_out": tout, "attempts": attempt + 1, "model": used}
        last = {"rc": rc, "out": out_text}
        transient = any(t in (out_text or "").lower() for t in _TRANSIENT)
        time.sleep((8 if transient else 4) * (attempt + 1))   # longer backoff on rate-limit/overload
    try:                                                # exhausted -> escalate, don't die silently
        import notify
        notify.send(f"⚠ agent '{role}' failed after {retries + 1} attempts on {Path(repo).name}",
                    title="factory", priority="high", tags="warning")
    except Exception:
        pass
    return {"rc": last["rc"], "out": (last["out"] or "")[-1500:], "failed": True, "attempts": retries + 1}


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


def run_web_qa(repo: str) -> tuple[bool, str]:
    """QA for the WEB line: serve the built app and load it in a real headless browser — assert it
    renders, capture console/page errors, screenshot it. Real signal, not 'a file exists'."""
    import functools
    import http.server
    import socket
    import threading
    root = repo
    if (Path(repo) / "public" / "index.html").exists():
        root = str(Path(repo) / "public")
    elif not (Path(repo) / "index.html").exists():
        idx = next(Path(repo).rglob("index.html"), None)
        if not idx:
            return False, "no index.html found in the build"
        root = str(idx.parent)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    httpd = http.server.HTTPServer(("127.0.0.1", port),
                                   functools.partial(http.server.SimpleHTTPRequestHandler, directory=root))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        shot = f"/tmp/webqa-{Path(repo).name}.png"
        env = {**os.environ, "NODE_PATH": str(Path.home() / "projects" / "products" / "noupload" / "node_modules")}
        p = subprocess.run(["node", str(SCRIPTS / "web_smoke.cjs"), f"http://127.0.0.1:{port}", shot],
                           capture_output=True, text=True, timeout=120, env=env)
        audit.append(actor="factory:qa-security", action="WebSmoke", resource=Path(repo).name,
                     decision="executed", payload={"rc": p.returncode})
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "") + f"\nscreenshot: {shot}"
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
    ok = not problems
    out = "extension QA OK (valid MV3, all refs present, JS parses)" if ok else \
          "EXTENSION QA FAILED:\n- " + "\n- ".join(problems)
    audit.append(actor="factory:qa-security", action="ExtQA", resource=root.name,
                 decision="executed", payload={"ok": ok, "problems": problems[:5]})
    return ok, out


def build_product(product: str, charter: str, kind: str = "lib", api_key: str = None) -> dict:
    """Drive one product end-to-end through the governed line with real agents + a real QA fix loop.
    kind='lib' -> Python library QA'd by pytest; kind='web' -> static web app QA'd by a real browser.
    api_key (BYO): if set, every agent runs on the tenant's own key — they pay their own inference."""
    _ctx.api_key = api_key
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
            log["stages"].append({name: {"resumed": True}})
            return {"resumed": True, "passed": True, "rc": 0}
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

    # SPEC — a PM turns the charter into a real spec + acceptance criteria
    stage("SPEC", "product-manager", lambda: agent("product-manager", str(repo),
          f"Read docs/CHARTER.md. Write docs/SPEC.md: scope, public API, and explicit acceptance "
          f"criteria as a bullet list of testable behaviours. Keep it tight and unambiguous."))

    # BUILD — a builder implements the product from the spec (library OR static web app)
    pkg = product.replace('-', '_')
    if web:
        build_task = (
            "Read docs/SPEC.md. Build a STATIC web app implementing it: index.html at the repo root plus "
            "CSS and vanilla JS. NO build step, NO external CDNs/network — it must work fully offline when "
            "opened over http. Clean, accessible, responsive UI. No console errors on load.")
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
            "reference a file that doesn't exist. Every file named in manifest.json must exist in the repo.")
    elif service:
        build_task = (
            f"Read docs/SPEC.md. Build a small HTTP API SERVICE as a MULTI-MODULE Python package under "
            f"src/{pkg}/: separate modules for (1) a SQLite-backed storage/repository layer, (2) the core "
            f"handlers/routing with input validation and correct status codes, (3) a thin stdlib "
            f"http.server adapter (NO external deps). CRITICAL for testability + sandboxed QA: the core "
            f"(routing/handlers/storage) MUST be callable WITHOUT binding a socket — the pytest suite under "
            f"tests/ exercises handler + storage functions directly (use a temp SQLite file per test), "
            f"covering every endpoint, validation error, and a persistence round-trip. Use `from src...` "
            f"imports. Make `python -m pytest -q` pass from the repo root. No network at test time.")
    else:
        build_task = (
            f"Read docs/SPEC.md. Implement the product as importable Python under src/ "
            f"(package '{pkg}') AND write a real pytest suite under tests/ that covers every acceptance "
            f"criterion, including edge cases. Use `from src...` imports. Make `python -m pytest -q` pass.")
    stage("BUILD", "builder", lambda: agent("builder", str(repo), build_task))

    # QA — run REAL verification (browser smoke for web, pytest for lib); bounded fix loop on failure
    def qa():
        run = ((lambda: run_ext_qa(str(repo))) if ext else
               (lambda: run_web_qa(str(repo))) if web else (lambda: run_tests(str(repo))))
        ok, out = run()
        label = "QA run (extension static)" if ext else "QA run (browser smoke)" if web else "QA run (pytest)"
        _trace("test", "qa-security", label, out, 0 if ok else 1)
        attempts = 0
        while not ok and attempts < MAX_FIX:
            attempts += 1
            print(f"[factory] QA red — fix attempt {attempts}/{MAX_FIX}", flush=True)
            fix = (f"The Chrome extension FAILED static QA. Output:\n\n{out[-1800:]}\n\n"
                   f"Fix manifest.json and its files so: it is valid Manifest V3, every file referenced in "
                   f"the manifest exists, and all JS is syntactically valid script-mode JS (no ES-module "
                   f"import/export). Add NO network calls or external resources."
                   if ext else
                   f"The web app FAILED QA (loaded in a headless browser). Output:\n\n{out[-1800:]}\n\n"
                   f"Fix the HTML/CSS/JS so it loads with HTTP 200, renders visible content, and has NO "
                   f"console/page errors."
                   if web else
                   f"`python -m pytest -q` is FAILING. Here is the output:\n\n{out[-1800:]}\n\n"
                   f"Fix the code under src/ (or a genuinely wrong test) so all tests pass. "
                   f"Do not delete tests to make them pass.")
            agent("builder", str(repo), fix)
            ok, out = run()
        return {"passed": ok, "fix_attempts": attempts, "tail": out[-400:]}
    qa_res = stage("QA", "qa-security", qa)

    # REVIEW — an independent reviewer records a verdict (read-mostly)
    stage("REVIEW", "reviewer", lambda: agent("reviewer", str(repo),
          f"Review the implementation against docs/SPEC.md. Write docs/REVIEW.md: what's correct, any "
          f"risks, and a clear APPROVE/REQUEST-CHANGES verdict. QA is currently "
          f"{'GREEN' if qa_res.get('passed') else 'RED'}.", model=CHEAP_MODEL))

    # LAUNCH — only if QA is green (a real gate, not a placeholder)
    if qa_res.get("passed"):
        stage("LAUNCH", "tech-lead", lambda: agent("tech-lead", str(repo),
              "Write docs/LAUNCH-CHECKLIST.md (how to install, run, and the test command) and a short "
              "README.md. This product passed QA and is cleared to ship.", model=CHEAP_MODEL))
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


def find_incomplete_builds(max_age_min: int = 20):
    """Builds that were INTERRUPTED, not finished: BUILD checkpointed (rc=0) but the line never reached a
    terminal verdict (no 'ProductComplete' audit — that row is written for BOTH launched AND blocked-at-QA,
    so a genuinely-blocked build is NOT considered interrupted and won't be re-resumed forever), and idle
    for >= max_age_min so we never grab one that's actively running. Returns [(product, kind), ...]."""
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
        print("PASS: factory prompt assembly + governance wiring ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
