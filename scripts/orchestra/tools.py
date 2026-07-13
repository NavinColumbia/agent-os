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
import sys
from pathlib import Path

_QA = Path(__file__).resolve().parent.parent / "qa"
if str(_QA) not in sys.path:
    sys.path.insert(0, str(_QA))


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


_TOOLS = {"qa_explore": qa_explore, "dev_fix": dev_fix}


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

    # 3) unknown tool + a raising tool are FAILED results, never exceptions (org never crashes on a tool).
    assert run_tool("nope", {})["status"] == "failed"

    def _boom(a):
        raise RuntimeError("browser died")
    _TOOLS["boom"] = _boom
    assert run_tool("boom", {})["status"] == "failed" and "browser died" in run_tool("boom", {})["result"]["error"]
    del _TOOLS["boom"]

    print("tools selftest: PASS (qa_explore bugs->findings + coverage; dev_fix judge-gated; errors fail-soft)")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
