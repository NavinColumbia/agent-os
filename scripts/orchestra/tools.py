#!/usr/bin/env python3
"""tools.py — the TOOL LAYER for the agentic QA/dev org (see docs/AGENTIC-QA-ORG.md, phase 1).

A tool-worker actor never runs long work inline (that would blow the runtime's 900s event lease and pin a
live browser across decide-steps). Instead it DISPATCHES one of these tools as a tracked background job and
PARKS. Each tool wraps proven code — the coverage-driven QA explorer, the git-diff-judged dev fixer — behind
ONE uniform, JSON-serialisable contract, so the runtime hook stays tiny and each tool is unit-testable in
isolation (this file's selftest stubs the heavy deps, no browser / no API):

    run_tool(name, args) -> {"status": "done"|"failed", "findings": [...], "result": {...}}

Tools:
  qa_explore  {story, target_url, vision, token?, org?, artifact_dir?, max_steps?}
              -> explore ONE story (coverage-driven, checkpointed, video). findings = the bugs it found;
                 result = {coverage ledger, stop_reason, video, steps}.
  dev_fix     {bug, code_context, vision, repo?, target_url?, stories?, restart_cmd?, health_url?, token?, org?}
              -> plan+spawn fixers, judge on the REAL git diff + a fresh observation. result = the fix dict.
"""
import json
import os
import re
import sys
from pathlib import Path

_QA = Path(__file__).resolve().parent.parent / "qa"
_SCRIPTS = Path(__file__).resolve().parent.parent          # for pulse, factory, etc.
for _p in (str(_QA), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def effect_record(action: str, resource: str, *, content=None, artifact=None, tenant=None,
                  actor="tool-worker", extra=None) -> dict:
    """PROVABLE side-effect (IMPROVEMENTS-PLAN item 13). Every REAL external action — a file written, an egress
    fetched, a commit — records a tamper-evident EFFECT into the audit chain with a content HASH + an
    IDEMPOTENCY key, so the org can PROVE what actually happened (not just that an agent said it did) and a
    retried step is recognisable. Returns {hash, idempotency_key, effect_id}. Best-effort: never raises — an
    observability write must not break the work it observes."""
    import hashlib
    body = content
    if body is None and artifact:
        try:
            body = Path(artifact).read_bytes()
        except Exception:
            body = None
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    digest = hashlib.sha256(body).hexdigest() if body is not None else None
    idem = hashlib.sha256(f"{action}|{resource}|{digest}".encode()).hexdigest()[:16]
    eid = None
    try:
        import audit
        payload = {"artifact": str(resource), "sha256": digest, "idempotency_key": idem,
                   "bytes": len(body) if body is not None else None}
        if extra:
            payload.update(extra)
        eid = audit.append(actor=actor, action=f"Effect:{action}", resource=str(resource),
                           payload=payload, tenant_id=tenant)[0]
    except Exception:
        pass
    return {"hash": digest, "idempotency_key": idem, "effect_id": eid}


def qa_explore(args: dict) -> dict:
    """Explore ONE story to coverage-completion. The browser lives only for this call (the dispatch-and-park
    job), never across decide-steps. Bugs become findings; the coverage ledger + stop reason + video go in
    result so the qa-coordinator can decide gap-fill / hand-off / accept."""
    import qa_explorer
    story = args.get("story") or {}
    ex = qa_explorer.Explorer(args["target_url"], args.get("vision", ""),
                              token=args.get("token"), org=str(args.get("org", "0")),
                              artifact_dir=args.get("artifact_dir"))
    bugs = []
    try:
        records = ex.explore(story, max_steps=args.get("max_steps"), on_bug=bugs.append)
    finally:
        try:
            ex.close()
        except Exception:
            pass
    findings = [{"kind": "bug", "title": (b.get("bug") or "defect")[:120], "detail": b.get("bug"),
                 "severity": b.get("severity", "medium"), "blocking": bool(b.get("blocking")),
                 "story": story.get("id") or story.get("title"), "screenshot": b.get("shot") or b.get("screenshot"),
                 "url": b.get("url")} for b in bugs]
    # compact per-step record (the same shape qa_run._story_report emits) so the AGENTIC evidence is
    # auditable by review.py — reasoning + action + expected + ACTUAL + verdict + screenshot per step.
    def _actual(r):
        a = r.get("actual") or {}
        return f"{a.get('url', '')} {('; '.join(a.get('console_errors') or []))}".strip()
    steps_detail = [{"action": _fmt_action(r.get("action")), "reasoning": r.get("reasoning", ""),
                     "expected": r.get("expected", ""), "actual": _actual(r),
                     "verdict": "match" if (r.get("verdict") or {}).get("matches_expected") else "mismatch",
                     "covers": r.get("covers", []),
                     "screenshot": (r.get("actual") or {}).get("screenshot")} for r in (records or [])]
    return {"status": "done", "findings": findings,
            "result": {"story": story.get("id") or story.get("title"),
                       "title": story.get("title") or story.get("id"),
                       "coverage": getattr(ex, "coverage", None),
                       "stop_reason": getattr(ex, "stop_reason", None),
                       "video": getattr(ex, "video_mp4", None),
                       "steps": len(records or []), "steps_detail": steps_detail, "bugs": len(bugs)}}


def _fmt_action(action) -> str:
    if not isinstance(action, dict):
        return str(action)
    tgt = action.get("selector") or (f"idx={action['idx']}" if action.get("idx") is not None else "")
    val = action.get("value")
    return f"{action.get('cmd', '?')} {tgt}{(' =' + repr(val)[:40]) if val else ''}".strip()


def dev_fix(args: dict) -> dict:
    """Fix one bug: the proven dev-fix loop (AI plans #agents -> spawns them -> judges FIXED on the real git
    diff + a fresh live observation). status reflects whether the judge actually confirmed the fix."""
    import dev_loop
    fix = dev_loop.fix_bug(args["bug"], args.get("code_context") or {}, args.get("vision", ""),
                           repo=args.get("repo"), target_url=args.get("target_url"),
                           stories=args.get("stories"), restart_cmd=args.get("restart_cmd"),
                           health_url=args.get("health_url"), token=args.get("token"), org=args.get("org"))
    return {"status": "done" if fix.get("fixed") else "failed", "findings": [], "result": fix}


def _agent_tool(role: str, prompt: str, args: dict) -> dict:
    """Shared shape for knowledge-work tools: run ONE role-specialized factory agent (web on by default, so
    research/intel/finance agents reach live data) and return its report as the result. The tool-worker
    dispatch-and-parks it, so a long web-research or analysis call never blocks a decide-step."""
    import factory
    res = factory.agent(role, args.get("repo") or str(getattr(factory, "PRODUCTS", "/tmp")), prompt)
    out = (res.get("out_full") or res.get("out") or "") if isinstance(res, dict) else str(res)
    ok = isinstance(res, dict) and res.get("rc", 0) == 0 and bool(out.strip())
    return {"status": "done" if ok else "failed", "findings": [], "result": {"report": out, "role": role}}


def research(args: dict) -> dict:
    """A researcher's real work: thorough, web-grounded research on a topic → a concise, cited report.
    (Same tool-worker pattern as QA — this is how a 'researcher' role DOES work instead of guessing.)"""
    topic = args.get("topic") or args.get("task") or args.get("question") or ""
    r = _agent_tool("researcher", "Research this THOROUGHLY using live web sources. Cross-check claims across "
                    "independent sources; be concrete and skeptical. Produce a concise report with the key "
                    "findings, the evidence, and CITATIONS (urls).\n\nTOPIC:\n" + topic, args)
    r["result"]["topic"] = topic
    return r


def _apply_tenant_ctx(tenant, org):
    """BILLING CORRECTNESS for a tool running in a jobrunner background thread — which does NOT inherit the
    caller's thread-local factory._ctx. Resolve THIS tenant's connected provider and wire engine/keys into
    _ctx so model spend lands on THEIR account, never the platform default. Returns the resolved CLAUDE
    api_key (or None for codex/subscription/platform) to hand to research_one. tenant None/'platform' -> the
    host subscription CLI (no key). Mirrors loopcontroller._apply_provider_ctx so both engines agree."""
    import factory
    tid = tenant if tenant not in (None, "", "platform") else None
    factory._ctx.tenant, factory._ctx.org = tid, org
    factory._ctx.engine, factory._ctx.api_key, factory._ctx.codex_key = "claude", None, None
    if not tid:
        return None
    try:
        import tenantproviders
        r = tenantproviders.resolve(tid) or {}
    except Exception:
        r = {}
    if r.get("engine") == "codex":
        factory._ctx.engine, factory._ctx.codex_key = "codex", r.get("key")
        return None
    factory._ctx.api_key = r.get("key")
    return r.get("key")


def research_subq(args: dict) -> dict:
    """One researcher's real work IN THE run_org ENGINE: answer ONE sub-question via
    research_fleet.research_one so it writes the CONTRACT finding (findings/NN.md) that
    research_fleet.synthesize later reads — this is what lets research run as a crash-resumable durable org
    (each subq is a dispatch-and-parked, lease-reclaimable tool job) WITHOUT changing the console output.
    Rebuilds factory._ctx from the tenant id in args (never a persisted api_key) so spend is billed to the
    right account. args: {idx, subq|task, repo, tenant?, org?}."""
    import research_fleet
    idx = int(args.get("idx") or 0)
    subq = args.get("subq") or args.get("task") or ""
    repo = args.get("repo")
    if not (subq and repo):
        return {"status": "failed", "findings": [], "result": {"error": "research_subq needs {subq, repo}"}}
    key = _apply_tenant_ctx(args.get("tenant"), args.get("org"))
    res = research_fleet.research_one(Path(repo), idx, subq, api_key=key)
    return {"status": "done" if res.get("ok") else "failed", "findings": [], "result": res}


def finance_report(args: dict) -> dict:
    """A finance function's real work: assemble a CEO-facing financial report from the data provided (or the
    platform's own metrics/billing if present) — spend, revenue, burn, runway, unit economics — honestly."""
    data = args.get("data")
    if data is None:                              # pull the platform's real numbers when no data is passed
        try:
            import billing
            data = billing.summary() if hasattr(billing, "summary") else None
        except Exception:
            data = None
    prompt = ("You are the finance function reporting to the CEO. From the DATA below, produce a crisp, honest "
              "financial report: spend, revenue, burn, runway, unit economics, and the ONE number the CEO "
              "should watch. State assumptions; never invent figures not supported by the data.\n\nDATA:\n"
              + json.dumps(data, default=str)[:4000])
    r = _agent_tool("finance-cost-controller", prompt, args)
    r["result"]["kind"] = "finance-report"
    return r


_LEGAL_RISKS = [(r"(?i)\bunlimited\s+liability\b", "unlimited liability", "high"),
                (r"\b\d{3}-\d{2}-\d{4}\b", "possible SSN / PII in the document", "high"),
                (r"(?i)\bperpetual\b.*\b(?:license|right)s?\b", "perpetual grant — review", "medium"),
                (r"(?i)\bindemnif", "indemnification clause — review scope", "medium"),
                (r"(?i)\bauto[- ]?renew", "auto-renewal — confirm notice period", "low")]


def legal_scan(args: dict) -> dict:
    """A legal/compliance function's REAL action: scan a document against policy. Deterministic first (required
    clauses that are MISSING = blocking; risky/PII patterns = flagged), then an AI compliance review. Findings
    flow to the coordinator exactly like QA bugs. `doc`/`text` inline or `path` to a file; `policy` = required
    clauses/keywords."""
    doc = args.get("doc") or args.get("text") or ""
    if not doc and args.get("path"):
        try:
            doc = Path(args["path"]).read_text(errors="ignore")[:40000]
        except Exception:
            doc = ""
    policy = args.get("policy") or []
    findings = []
    for req in policy:                            # required clause missing -> a BLOCKING finding
        if str(req).lower() not in doc.lower():
            findings.append({"kind": "missing-clause", "title": f"policy requires '{req}' — not present",
                             "severity": "high", "blocking": True})
    for pat, label, sev in _LEGAL_RISKS:
        if re.search(pat, doc):
            findings.append({"kind": "risk", "title": label, "severity": sev, "blocking": False})
    review = _agent_tool("legal-compliance-checklist",
                         "Review this document for legal/compliance risk: missing protections, risky terms, "
                         f"and PII. Policy requirements: {policy}. Be specific.\n\nDOC:\n{doc[:6000]}", args)
    blocking = any(f["blocking"] for f in findings)
    return {"status": "failed" if blocking else "done", "findings": findings,
            "result": {"issues": len(findings), "blocking": blocking,
                       "review": (review.get("result") or {}).get("report")}}


def connector_ingest(args: dict) -> dict:
    """Reach a LIVE external source through the GOVERNED connector (DNS-pinned, allowlisted egress — the same
    egress policy the platform enforces), then summarise it. This is how research/data/intel agents pull real
    external data safely. Denied/blocked egress is a failed result, never a crash."""
    url = args.get("url") or ""
    try:
        import connectors
        content = connectors.ingest(url, args.get("product", "platform"),
                                    role=args.get("role", "data-engineer"))
    except Exception as e:
        return {"status": "failed", "findings": [], "result": {"error": f"connector denied/failed: {e}"}}
    eff = effect_record("connector_ingest", url, content=str(content),
                        tenant=args.get("org") or args.get("tenant"),
                        actor=f"tool:{args.get('role', 'data-engineer')}", extra={"egress": True})
    s = _agent_tool(args.get("role", "data-engineer"),
                    f"Summarise this content fetched from {url} for the CEO (2-4 honest lines):\n"
                    + str(content)[:5000], args)
    return {"status": "done", "findings": [],
            "result": {"url": url, "summary": (s.get("result") or {}).get("report"), "effect": eff}}


def data_query(args: dict) -> dict:
    """A data function's REAL external action: run a READ-ONLY query against the platform DB and summarise the
    result for the CEO. Refuses anything but a single SELECT (no writes, no semicolons) — a data agent reads,
    it never mutates. This is the template for external-action tools (connectors, live systems)."""
    sql = (args.get("sql") or "").strip().rstrip(";")
    if not sql.lower().startswith("select") or ";" in sql:
        return {"status": "failed", "findings": [], "result": {"error": "data_query runs ONE read-only SELECT"}}
    try:
        import psycopg
        import pulse
        with psycopg.connect(pulse.DB) as c, c.cursor() as cur:
            cur.execute(sql + ("" if "limit" in sql.lower() else " LIMIT 200"))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        return {"status": "failed", "findings": [], "result": {"error": f"query failed: {e}"}}
    s = _agent_tool("data-analyst", "Summarise this query result for the CEO in 2-3 honest lines (call out "
                    f"anything notable).\nQUERY: {sql}\nROWS ({len(rows)}): {json.dumps(rows, default=str)[:3000]}",
                    args)
    return {"status": "done", "findings": [],
            "result": {"sql": sql, "row_count": len(rows), "rows": rows[:20],
                       "summary": (s.get("result") or {}).get("report")}}


def produce_artifact(args: dict) -> dict:
    """A function that OUTPUTS a real deliverable FILE — marketing copy, a landing page, a spec doc, a design
    mockup, a report. The role agent generates the content; we WRITE it to a Windows-visible artifacts dir and
    return the path (so the CEO gets a real file, not just chat). `role`, `task`, `filename` (its extension
    picks the format), optional `context`. This is the output counterpart to the read tools (research/data)."""
    import artifacts
    role = args.get("role") or args.get("worker_role") or "specialist"
    filename = Path(args.get("filename") or "artifact.md").name
    fmt = (Path(filename).suffix.lstrip(".") or "md")
    prompt = (f"You are the {role}. Produce the deliverable below as a COMPLETE, ready-to-use {fmt} file — "
              f"output ONLY the file content, no preamble or code fences.\n\nDELIVERABLE:\n{args.get('task', '')}"
              + (f"\n\nCONTEXT:\n{json.dumps(args.get('context'), default=str)[:3000]}" if args.get("context") else ""))
    r = _agent_tool(role, prompt, args)
    content = (r.get("result") or {}).get("report") or ""
    if r["status"] != "done" or not content.strip():
        return {"status": "failed", "findings": [], "result": {"error": "agent produced no content"}}
    try:
        path = artifacts.run_dir(args.get("product") or role) / filename
        path.write_text(content)
    except Exception as e:
        return {"status": "failed", "findings": [], "result": {"error": f"write failed: {e}"}}
    eff = effect_record("produce_artifact", str(path), content=content,
                        tenant=args.get("org") or args.get("tenant"), actor=f"tool:{role}")
    return {"status": "done", "findings": [],
            "result": {"artifact": str(path), "role": role, "format": fmt, "bytes": len(content), "effect": eff}}


def design_asset(args: dict) -> dict:
    """A design function's real artifact: generate a self-contained SVG (or HTML) mockup/asset the CEO can open.
    The designer agent outputs valid, self-contained markup; we save it as a viewable file."""
    fmt = (args.get("format") or "svg").lower()
    spec = args.get("spec") or args.get("task") or ""
    return produce_artifact({**args, "role": args.get("role") or "brand-designer",
                             "task": f"Design a clean, on-brand {fmt.upper()} asset for: {spec}. Output valid, "
                                     f"SELF-CONTAINED {fmt} markup only (no external refs).",
                             "filename": args.get("filename") or f"asset.{fmt}"})


def knowledge_work(args: dict) -> dict:
    """The catch-all for any KNOWLEDGE-WORK function — a product-manager writing a spec, a legal-compliance
    review of a provided doc, a strategy memo, an analysis. Runs the given role agent on the task and returns
    its deliverable. This is how MOST of the 92 role charters do real work (no external tool needed) — pair it
    with a coordinator's tool-team context (tool='knowledge_work', worker_role='<role>')."""
    role = args.get("role") or args.get("worker_role") or "specialist"
    task = args.get("task") or args.get("prompt") or args.get("topic") or ""
    ctx = args.get("context")
    prompt = (f"You are the {role}. Do this task to an elite, CEO-grade standard and return your deliverable "
              f"(be concrete and honest; state assumptions).\n\nTASK:\n{task}"
              + (f"\n\nCONTEXT:\n{json.dumps(ctx, default=str)[:3000]}" if ctx else ""))
    return _agent_tool(role, prompt, args)


_TOOLS = {"qa_explore": qa_explore, "dev_fix": dev_fix, "research": research, "research_subq": research_subq,
          "finance_report": finance_report, "knowledge_work": knowledge_work, "data_query": data_query,
          "legal_scan": legal_scan, "connector_ingest": connector_ingest,
          "produce_artifact": produce_artifact, "design_asset": design_asset}


def run_tool(name: str, args: dict) -> dict:
    """The single uniform entry the tool-worker calls. Never raises — a tool error is a failed result the
    coordinator can react to (park/retry/escalate), never a crash of the org."""
    fn = _TOOLS.get(name)
    if not fn:
        return {"status": "failed", "findings": [], "result": {"error": f"unknown tool {name!r}"}}
    try:
        out = fn(args or {})
    except Exception as e:
        return {"status": "failed", "findings": [], "result": {"error": f"{type(e).__name__}: {e}"}}
    # normalise the contract so the runtime hook can trust the shape
    out.setdefault("status", "done")
    out.setdefault("findings", [])
    out.setdefault("result", {})
    return out


def _selftest():
    import types
    # 1) qa_explore routes to the Explorer, shapes bugs->findings + coverage into result. Stub the Explorer.
    fake_qx = types.ModuleType("qa_explorer")

    class _StubEx:
        def __init__(self, *a, **k):
            self.coverage = [{"aspect": "open panel", "covered": True},
                             {"aspect": "empty submit", "covered": False}]
            self.stop_reason = "coverage-complete"
            self.video_mp4 = "/tmp/x/qa-session.mp4"

        def explore(self, story, max_steps=None, on_bug=None):
            on_bug({"bug": "panel rendered blank", "severity": "high", "blocking": True,
                    "shot": "/tmp/x/s1.png", "url": "http://app/#/x"})
            return [{"step": 0}, {"step": 1}]

        def close(self):
            pass

    fake_qx.Explorer = _StubEx
    sys.modules["qa_explorer"] = fake_qx
    r = run_tool("qa_explore", {"target_url": "http://app", "vision": "v",
                                "story": {"id": "US1", "title": "open"}})
    assert r["status"] == "done", r
    assert len(r["findings"]) == 1 and r["findings"][0]["blocking"] is True, r
    assert r["findings"][0]["title"] == "panel rendered blank" and r["findings"][0]["story"] == "US1"
    assert r["result"]["stop_reason"] == "coverage-complete" and r["result"]["steps"] == 2
    assert r["result"]["video"].endswith(".mp4")

    # 2) dev_fix routes to dev_loop.fix_bug; status reflects the JUDGE's verdict, not the agent's claim.
    fake_dl = types.ModuleType("dev_loop")
    fake_dl.fix_bug = lambda bug, ctx, vision, **k: {"fixed": True, "files": ["src/app.js"], "judged": True}
    sys.modules["dev_loop"] = fake_dl
    r2 = run_tool("dev_fix", {"bug": {"bug": "500 on pay"}, "vision": "v", "repo": "/tmp/r"})
    assert r2["status"] == "done" and r2["result"]["files"] == ["src/app.js"], r2
    fake_dl.fix_bug = lambda *a, **k: {"fixed": False, "error": "judge said not fixed"}
    r3 = run_tool("dev_fix", {"bug": {"bug": "x"}, "vision": "v"})
    assert r3["status"] == "failed", r3

    # 3) knowledge-work tools (research, finance_report) run a role agent and return its report. Stub factory.
    fake_f = types.ModuleType("factory")
    fake_f.PRODUCTS = "/tmp"
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": f"[{role}] report: " + task[:30]}
    sys.modules["factory"] = fake_f
    rr = run_tool("research", {"topic": "the market for AI QA tools"})
    assert rr["status"] == "done" and rr["result"]["role"] == "researcher" and "report" in rr["result"], rr
    assert rr["result"]["topic"] == "the market for AI QA tools"
    rf = run_tool("finance_report", {"data": {"spend": 100, "revenue": 250}})
    assert rf["status"] == "done" and rf["result"]["role"] == "finance-cost-controller", rf
    # knowledge_work: any role (product-manager, legal-compliance, strategist, …) does a deliverable.
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": f"[{role}] deliverable"}
    kw = run_tool("knowledge_work", {"role": "product-manager", "task": "write the launch spec"})
    assert kw["status"] == "done" and kw["result"]["role"] == "product-manager", kw
    lg = run_tool("knowledge_work", {"role": "legal-compliance-checklist", "task": "review the ToS", "context": {"doc": "..."}})
    assert lg["status"] == "done" and "deliverable" in lg["result"]["report"], lg
    # data_query: a REAL read-only DB query (external action) + write-refusal.
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": "summary"}
    import pulse as _pulse
    if _pulse.DB:                                 # a real SELECT against the platform DB
        dq = run_tool("data_query", {"sql": "SELECT 1 AS one"})
        assert dq["status"] == "done" and dq["result"]["row_count"] == 1, dq
    assert run_tool("data_query", {"sql": "DELETE FROM agent_pulse"})["status"] == "failed", "writes refused"
    assert run_tool("data_query", {"sql": "SELECT 1; DROP TABLE x"})["status"] == "failed", "multi-stmt refused"

    # legal_scan: a MISSING required clause = blocking finding; risky patterns (PII, unlimited liability) flagged.
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": "legal review"}
    ls = run_tool("legal_scan", {"doc": "This has unlimited liability and SSN 123-45-6789.",
                                 "policy": ["limitation of liability", "governing law"]})
    assert ls["status"] == "failed", ls          # required clauses missing -> blocking
    ts = " ".join(f["title"] for f in ls["findings"])
    assert "limitation of liability" in ts and "liability" in ts.lower() and ("PII" in ts or "SSN" in ts), ts
    lok = run_tool("legal_scan", {"doc": "limitation of liability applies; governing law is X.",
                                  "policy": ["limitation of liability", "governing law"]})
    assert lok["status"] == "done" and not any(f["blocking"] for f in lok["findings"]), lok

    # connector_ingest: governed external fetch (stub connectors) + summary; a denial -> failed, never a crash.
    fake_c = types.ModuleType("connectors")
    fake_c.ingest = lambda url, product, role="data-engineer", **k: "fetched: " + url
    sys.modules["connectors"] = fake_c
    ci = run_tool("connector_ingest", {"url": "https://example.com/x"})
    assert ci["status"] == "done" and ci["result"]["url"] == "https://example.com/x", ci
    fake_c.ingest = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("egress denied"))
    assert run_tool("connector_ingest", {"url": "http://evil"})["status"] == "failed"

    # artifact-output tools: the role agent's content is WRITTEN to a real file the CEO can open.
    import tempfile
    _prev_ev = os.environ.get("AOS_QA_EVIDENCE_DIR")
    os.environ["AOS_QA_EVIDENCE_DIR"] = tempfile.mkdtemp(prefix="artifact-test-")
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": "# Launch copy\nBuy our thing."}
    pa = run_tool("produce_artifact", {"role": "content-marketer", "task": "landing page copy",
                                       "filename": "landing.md", "product": "demo"})
    assert pa["status"] == "done" and Path(pa["result"]["artifact"]).exists(), pa
    assert Path(pa["result"]["artifact"]).read_text().startswith("# Launch copy"), "content written to the file"
    # item 13: a provable EFFECT record — content hash + idempotency key — accompanies the real file write.
    eff = pa["result"].get("effect") or {}
    assert eff.get("hash") and len(eff["hash"]) == 64 and eff.get("idempotency_key"), f"effect record: {eff}"
    import hashlib as _h
    assert eff["hash"] == _h.sha256(b"# Launch copy\nBuy our thing.").hexdigest(), "effect hash must bind the real bytes"
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": "<svg xmlns='http://www.w3.org/2000/svg'/>"}
    da = run_tool("design_asset", {"spec": "a logo", "product": "demo"})
    assert da["status"] == "done" and da["result"]["artifact"].endswith(".svg") and Path(da["result"]["artifact"]).exists(), da
    fake_f.agent = lambda role, repo, task, **k: {"rc": 0, "out_full": "   "}   # empty content -> failed, no file
    assert run_tool("produce_artifact", {"role": "x", "task": "y", "filename": "z.md"})["status"] == "failed"
    import shutil as _sh
    _sh.rmtree(os.environ["AOS_QA_EVIDENCE_DIR"], ignore_errors=True)
    if _prev_ev is None:
        os.environ.pop("AOS_QA_EVIDENCE_DIR", None)
    else:
        os.environ["AOS_QA_EVIDENCE_DIR"] = _prev_ev

    fake_f.agent = lambda role, repo, task, **k: {"rc": 1, "out_full": ""}   # a failed agent -> failed tool
    assert run_tool("research", {"topic": "x"})["status"] == "failed"

    # 4) unknown tool + a raising tool are FAILED results, never exceptions (org never crashes on a tool).
    assert run_tool("nope", {})["status"] == "failed"

    def _boom(a):
        raise RuntimeError("browser died")
    _TOOLS["boom"] = _boom
    assert run_tool("boom", {})["status"] == "failed" and "browser died" in run_tool("boom", {})["result"]["error"]
    del _TOOLS["boom"]

    print("tools selftest: PASS (qa_explore, dev_fix, research, finance_report, knowledge_work, data_query, "
          "legal_scan, connector_ingest, produce_artifact, design_asset — read + OUTPUT work; errors fail-soft)")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
