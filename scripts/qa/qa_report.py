#!/usr/bin/env python3
"""qa_report.py — the RUN REPORT for agent-os's agentic QA system.

The agentic QA loop is state-based: observe -> AI decides -> act -> observe -> AI evaluates, all while
holding the product's ORIGINAL VISION + the EXPECTED behavior of every user story in context so it can
judge EXPECTED-vs-ACTUAL. This module is where that whole run is turned into an artifact a HUMAN (and the
controller) can read: what was tested, what each step was SUPPOSED to do vs what it ACTUALLY did, every bug
found (with its screenshot, whether it BLOCKS, whether it was FIXED), and a final VERDICT.

Design stance (owner-mandated): every DECISION in this system is an AI call. The report's core FACTS
(pass/fail counts, open-bug counts, the verdict) are computed DETERMINISTICALLY from the run — you never
want a summary that HALLUCINATES a green light. But the human-facing one-liner is polished by an AI call
(factory.agent) that reads the vision + the tallies and writes the sentence a founder actually wants to
read; it degrades to a deterministic sentence if the model is unavailable. So: facts are grounded, prose
is AI. Cost is not a concern.

    qa_report.py selftest      # offline: canned run -> asserts md+json written with bugs + verdict
Run with the agent-os venv python.

build_report(run) writes:
    /tmp/aos-qa/report-<product>-<ts>.md    (human-readable)
    /tmp/aos-qa/report-<product>-<ts>.json  (machine-readable, the same structured verdict)
and returns a dict: {md, json, summary, verdict, passed, open_bugs, ...}.

The `run` dict it consumes (all keys optional; the loop fills what it observed):
    {
      "product":   "noupload",
      "vision":    "<the original one-liner / charter the QA agent judged against>",
      "url":       "http://localhost:3000",
      "started_at": <epoch>, "finished_at": <epoch>,
      "stories": [
        { "id": "US-1", "title": "Sign up", "expected": "<expected end-state>",
          "status": "passed"|"failed"|"blocked",
          "steps": [
            { "action": "<what the agent did>", "expected": "<what should happen>",
              "actual": "<what was observed>", "verdict": "match"|"mismatch",
              "screenshot": "<path>" }, ... ] }, ... ],
      "bugs": [
        { "id": "BUG-1", "story": "US-2", "title": "...", "detail": "...",
          "expected": "...", "actual": "...", "screenshot": "<path>",
          "blocking": true, "fixed": false, "severity": "high" }, ... ],
    }
"""
import json
import os
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

OUT_DIR = Path(os.environ.get("AOS_QA_DIR", "/tmp/aos-qa"))


# ---------------------------------------------------------------------------------------------------
# Grounded tallies — NEVER an AI call. The verdict a human trusts must be derived from the run itself.
# ---------------------------------------------------------------------------------------------------
def _incomplete(story: dict) -> bool:
    """True when the run did NOT finish testing this story — coverage aspects remain untested, or it stopped
    for an incomplete/stuck/stalled/capped/deadline reason. Such a story is NEVER 'passed' no matter what its
    label says: a run that gave up mid-way has not earned a pass (the auditor caught exactly this dishonesty)."""
    cov = story.get("coverage") or []
    if any(not c.get("covered") for c in cov):
        return True
    stop = (story.get("stop_reason") or "").lower()
    return any(k in stop for k in ("incomplete", "stalled", "stuck", "cap", "deadline"))


def _norm_status(story: dict) -> str:
    """A story's status. An INCOMPLETE run can never be 'passed' (honest reporting — see _incomplete). Then
    prefer an explicit status; otherwise DERIVE it from steps so a run that only recorded per-step verdicts
    still gets an honest story-level pass/fail (no silent 'unknown -> green')."""
    if _incomplete(story):
        return "incomplete"
    s = (story.get("status") or "").strip().lower()
    if s in ("passed", "pass", "ok", "green"):
        return "passed"
    if s in ("failed", "fail", "broken", "red"):
        return "failed"
    if s in ("blocked", "blocker", "skipped"):
        return "blocked"
    steps = story.get("steps") or []
    if steps and any((st.get("verdict") or "").lower() in ("mismatch", "fail", "failed") for st in steps):
        return "failed"
    if steps and all((st.get("verdict") or "").lower() in ("match", "pass", "passed", "ok") for st in steps):
        return "passed"
    return "unknown"


def _tally(run: dict) -> dict:
    """Deterministic coverage + bug accounting for the whole run."""
    stories = run.get("stories") or []
    bugs = run.get("bugs") or []
    per = {"passed": 0, "failed": 0, "blocked": 0, "unknown": 0, "incomplete": 0}
    for st in stories:
        per[_norm_status(st)] = per.get(_norm_status(st), 0) + 1

    def _truthy(b, *keys, default=False):
        for k in keys:
            if k in b:
                return bool(b[k])
        return default

    open_bugs = [b for b in bugs if not _truthy(b, "fixed", "resolved")]
    blocking_open = [b for b in open_bugs if _truthy(b, "blocking", "blocker")]
    all_passed = (bool(stories) and per["failed"] == 0 and per["blocked"] == 0
                  and per["unknown"] == 0 and per["incomplete"] == 0)
    # A run passes only if every story passed AND nothing blocking is still open. A non-blocking open bug
    # is noted but does not veto the verdict (ship-with-known-issues is a real, honest outcome).
    passed = all_passed and not blocking_open
    return {
        "total_stories": len(stories), "per_status": per,
        "total_bugs": len(bugs), "open_bugs": len(open_bugs),
        "blocking_open": len(blocking_open), "fixed_bugs": len(bugs) - len(open_bugs),
        "passed": passed, "_open_bug_objs": open_bugs, "_blocking_open_objs": blocking_open,
    }


def _verdict_line(t: dict) -> str:
    """The grounded one-liner (no model). This is the fallback AND the ground truth the AI must not contradict."""
    if t["total_stories"] == 0:
        return "NO VERDICT — no user stories were exercised in this run"
    if t["passed"]:
        extra = f" ({t['open_bugs']} non-blocking issue(s) noted)" if t["open_bugs"] else ""
        return f"ALL {t['total_stories']} STORIES PASSED{extra}"
    parts = []
    p = t["per_status"]
    if p["failed"]:
        parts.append(f"{p['failed']} failed")
    if p["blocked"]:
        parts.append(f"{p['blocked']} blocked")
    if p["unknown"]:
        parts.append(f"{p['unknown']} inconclusive")
    if p.get("incomplete"):
        parts.append(f"{p['incomplete']} INCOMPLETE (coverage not finished)")
    story_part = ", ".join(parts) or "0 clean"
    bug_part = f"{t['open_bugs']} open bug(s)"
    if t["blocking_open"]:
        bug_part += f" ({t['blocking_open']} BLOCKING)"
    return f"FAILED — {story_part} of {t['total_stories']} stories; {bug_part}"


# ---------------------------------------------------------------------------------------------------
# AI narrative — the ONE decision-flavored call in the report: turn grounded tallies + the original
# vision into the sentence a human actually wants. Grounded verdict is passed in so the model DESCRIBES,
# never DECIDES, the outcome. Fails soft to the deterministic line.
# ---------------------------------------------------------------------------------------------------
def _ai_summary(run: dict, t: dict, verdict: str) -> str:
    try:
        import factory
        prompt = (
            "You are the QA lead reporting a run to the product's founder. Write ONE plain-English sentence "
            "(<= 30 words) summarizing the QA outcome. Do NOT change the verdict — state it faithfully.\n\n"
            f"ORIGINAL VISION: {run.get('vision', '(none given)')}\n"
            f"GROUNDED VERDICT (authoritative — do not contradict): {verdict}\n"
            f"STORIES: {t['total_stories']} total, {t['per_status']}\n"
            f"BUGS: {t['total_bugs']} found, {t['open_bugs']} open, {t['blocking_open']} blocking-open, "
            f"{t['fixed_bugs']} fixed.\n\n"
            "Return only the sentence, no preamble."
        )
        res = factory.agent("qa-reporter", os.getcwd(), prompt, light=True)
        if isinstance(res, dict) and res.get("rc") == 0:
            line = (res.get("out_full") or res.get("out") or "").strip().splitlines()
            line = next((l.strip() for l in line if l.strip()), "")
            if line:
                return line
    except Exception:
        pass
    return verdict  # deterministic fallback — always truthful


# ---------------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------------
def _fmt_screenshot(path):
    return f"`{path}`" if path else "_(none)_"


def _render_md(run: dict, t: dict, verdict: str, summary: str) -> str:
    product = run.get("product", "unknown")
    L = []
    L.append(f"# QA Run Report — {product}")
    L.append("")
    L.append(f"> {summary}")
    L.append("")
    L.append(f"**Verdict:** {verdict}")
    if run.get("vision"):
        L.append("")
        L.append(f"**Original vision:** {run['vision']}")
    meta = []
    if run.get("url"):
        meta.append(f"url: {run['url']}")
    if run.get("started_at"):
        meta.append("started: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(run["started_at"])))
    if run.get("finished_at"):
        meta.append("finished: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(run["finished_at"])))
    if meta:
        L.append("")
        L.append("  \n".join(meta))
    p = t["per_status"]
    L.append("")
    L.append("## Coverage")
    L.append("")
    L.append(f"- Stories: **{t['total_stories']}** — "
             f"{p['passed']} passed, {p['failed']} failed, {p['blocked']} blocked, {p['unknown']} inconclusive")
    L.append(f"- Bugs: **{t['total_bugs']}** — {t['open_bugs']} open "
             f"({t['blocking_open']} blocking), {t['fixed_bugs']} fixed")

    # Per-story: expected vs actual, step by step.
    L.append("")
    L.append("## User stories")
    for st in (run.get("stories") or []):
        status = _norm_status(st)
        badge = {"passed": "PASS", "failed": "FAIL", "blocked": "BLOCKED"}.get(status, "?")
        sid = st.get("id", "")
        title = st.get("title", "(untitled)")
        L.append("")
        L.append(f"### [{badge}] {sid} {title}".rstrip())
        if st.get("expected"):
            L.append(f"- **Expected:** {st['expected']}")
        steps = st.get("steps") or []
        if steps:
            L.append("")
            L.append("| # | Action | Expected | Actual | Verdict | Screenshot |")
            L.append("|---|--------|----------|--------|---------|------------|")
            for i, s in enumerate(steps, 1):
                v = (s.get("verdict") or "").lower()
                vb = {"match": "✓ match", "mismatch": "✗ MISMATCH"}.get(v, v or "-")
                L.append(f"| {i} | {s.get('action','')} | {s.get('expected','')} | "
                         f"{s.get('actual','')} | {vb} | {_fmt_screenshot(s.get('screenshot'))} |")

    # Bugs — every one, with screenshot + blocking + fixed.
    L.append("")
    L.append("## Bugs")
    bugs = run.get("bugs") or []
    if not bugs:
        L.append("")
        L.append("_None found._")
    else:
        L.append("")
        L.append("| ID | Story | Title | Severity | Blocking | Fixed | Screenshot |")
        L.append("|----|-------|-------|----------|----------|-------|------------|")
        for b in bugs:
            blocking = bool(b.get("blocking") or b.get("blocker"))
            fixed = bool(b.get("fixed") or b.get("resolved"))
            L.append(f"| {b.get('id','')} | {b.get('story','')} | {b.get('title','(untitled)')} | "
                     f"{b.get('severity','?')} | {'YES' if blocking else 'no'} | "
                     f"{'yes' if fixed else 'NO'} | {_fmt_screenshot(b.get('screenshot'))} |")
        # Details block for each bug (expected vs actual is the whole point of this QA system).
        for b in bugs:
            L.append("")
            L.append(f"#### {b.get('id','')} — {b.get('title','(untitled)')}")
            if b.get("detail") or b.get("description"):
                L.append(f"{b.get('detail') or b.get('description')}")
            if b.get("expected"):
                L.append(f"- **Expected:** {b['expected']}")
            if b.get("actual"):
                L.append(f"- **Actual:** {b['actual']}")
            L.append(f"- **Blocking:** {'yes' if (b.get('blocking') or b.get('blocker')) else 'no'} · "
                     f"**Fixed:** {'yes' if (b.get('fixed') or b.get('resolved')) else 'no'}")
            if b.get("screenshot"):
                L.append(f"- **Screenshot:** {_fmt_screenshot(b.get('screenshot'))}")
    L.append("")
    return "\n".join(L)


def build_report(run: dict, out_dir=None) -> dict:
    """Turn a completed agentic-QA `run` into a human markdown report + a machine JSON report on disk.

    Returns {md, json, summary, verdict, passed, total_stories, total_bugs, open_bugs, blocking_open}.
    Core facts (verdict/counts) are computed deterministically from `run`; only the human one-liner is
    AI-polished (with a deterministic fallback), so the report can never green-light a failing run."""
    out = Path(out_dir) if out_dir else OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    t = _tally(run)
    verdict = _verdict_line(t)
    summary = _ai_summary(run, t, verdict)

    product = run.get("product", "run")
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(run.get("finished_at") or time.time()))
    stem = f"report-{product}-{ts}"
    md_path = out / f"{stem}.md"
    json_path = out / f"{stem}.json"

    md = _render_md(run, t, verdict, summary)
    md_path.write_text(md)

    doc = {
        "product": product,
        "vision": run.get("vision"),
        "summary": summary,
        "verdict": verdict,
        "passed": t["passed"],
        "coverage": {"total_stories": t["total_stories"], "per_status": t["per_status"]},
        "bugs": {
            "total": t["total_bugs"], "open": t["open_bugs"],
            "blocking_open": t["blocking_open"], "fixed": t["fixed_bugs"],
            "items": run.get("bugs") or [],
        },
        "stories": [
            {"id": s.get("id"), "title": s.get("title"), "status": _norm_status(s),
             "expected": s.get("expected"),
             "steps": s.get("steps") or []}
            for s in (run.get("stories") or [])
        ],
        "generated_at": time.time(),
    }
    json_path.write_text(json.dumps(doc, indent=2, default=str))

    return {
        "md": str(md_path), "json": str(json_path),
        "summary": summary, "verdict": verdict, "passed": t["passed"],
        "total_stories": t["total_stories"], "total_bugs": t["total_bugs"],
        "open_bugs": t["open_bugs"], "blocking_open": t["blocking_open"],
    }


# ---------------------------------------------------------------------------------------------------
# Selftest — offline, deterministic, NO real API calls (factory.agent is stubbed).
# ---------------------------------------------------------------------------------------------------
def _selftest():
    import tempfile
    # Stub the ONE AI call so the selftest is cheap + deterministic and needs no credentials/network.
    try:
        import factory
        _real = getattr(factory, "agent", None)
        factory.agent = lambda role, repo, prompt, **k: {
            "rc": 0, "out": "QA found a blocking login bug; 1 of 2 stories failed.",
            "out_full": "QA found a blocking login bug; 1 of 2 stories failed."}
    except Exception:
        _real = None

    run = {
        "product": "noupload",
        "vision": "Anyone can share a file by dropping it on the page and copying the link — no account.",
        "url": "http://localhost:3000",
        "started_at": 1_700_000_000, "finished_at": 1_700_000_600,
        "stories": [
            {"id": "US-1", "title": "Drop a file and get a link",
             "expected": "After dropping a file, a shareable link appears within 2s.",
             "status": "passed",
             "steps": [
                 {"action": "open homepage", "expected": "dropzone visible", "actual": "dropzone visible",
                  "verdict": "match", "screenshot": "/tmp/aos-qa/shots/us1-s1.png"},
                 {"action": "drop sample.pdf", "expected": "link appears", "actual": "link appeared",
                  "verdict": "match", "screenshot": "/tmp/aos-qa/shots/us1-s2.png"},
             ]},
            {"id": "US-2", "title": "Open the shared link in a new browser",
             "expected": "The recipient sees the file and can download it.",
             # no explicit status -> must be DERIVED as failed from the mismatch step below
             "steps": [
                 {"action": "open link in incognito", "expected": "file preview shown",
                  "actual": "500 error page", "verdict": "mismatch",
                  "screenshot": "/tmp/aos-qa/shots/us2-s1.png"},
             ]},
        ],
        "bugs": [
            {"id": "BUG-1", "story": "US-2", "title": "Shared link returns 500 for recipients",
             "detail": "The download route crashes when the viewer has no session cookie.",
             "expected": "recipient sees the file", "actual": "HTTP 500",
             "screenshot": "/tmp/aos-qa/shots/us2-s1.png", "blocking": True, "fixed": False,
             "severity": "high"},
            {"id": "BUG-2", "story": "US-1", "title": "Copy-link button has no hover state",
             "expected": "button dims on hover", "actual": "no visual change",
             "screenshot": "/tmp/aos-qa/shots/us1-s2.png", "blocking": False, "fixed": True,
             "severity": "low"},
        ],
    }

    tmp = Path(tempfile.mkdtemp())
    try:
        res = build_report(run, out_dir=tmp)
        md_p, json_p = Path(res["md"]), Path(res["json"])

        assert md_p.exists(), "markdown report not written"
        assert json_p.exists(), "json report not written"
        md = md_p.read_text()
        doc = json.loads(json_p.read_text())

        # Verdict: US-2 failed (derived) + BUG-1 blocking-open -> whole run must FAIL.
        assert res["passed"] is False, "run with a blocking open bug must not pass"
        assert doc["passed"] is False
        assert "FAILED" in res["verdict"], res["verdict"]
        assert res["open_bugs"] == 1 and res["blocking_open"] == 1, res
        assert res["total_stories"] == 2 and res["total_bugs"] == 2

        # Story status derivation: US-2 had no explicit status but a mismatch step.
        us2 = next(s for s in doc["stories"] if s["id"] == "US-2")
        assert us2["status"] == "failed", us2

        # Every bug surfaced in the md, with its screenshot + blocking + fixed columns.
        assert "BUG-1" in md and "BUG-2" in md
        assert "us2-s1.png" in md, "bug screenshot path missing from report"
        assert "YES" in md and "NO" in md, "blocking/fixed flags not rendered"
        # Expected-vs-actual made it into the report (the core of this QA system).
        assert "HTTP 500" in md and "500 error page" in md
        # The (stubbed) AI summary was used verbatim.
        assert res["summary"] == "QA found a blocking login bug; 1 of 2 stories failed."
        assert doc["summary"] == res["summary"]

        # Now a fully-green run: no bugs, all stories pass -> ALL STORIES PASSED.
        green = {"product": "noupload", "vision": "x",
                 "stories": [{"id": "US-1", "title": "ok", "status": "passed"}], "bugs": []}
        gres = build_report(green, out_dir=tmp)
        assert gres["passed"] is True and "ALL 1 STORIES PASSED" in gres["verdict"], gres

        print(f"wrote {md_p.name} ({len(md)} bytes) + {json_p.name}")
        print(f"verdict: {res['verdict']}")
        print(f"summary: {res['summary']}")
        print("PASS: qa_report — grounded verdict, per-story EXPECTED-vs-ACTUAL, bugs w/ screenshot+blocking+fixed, md+json written ✅")
        return 0
    finally:
        if _real is not None:
            factory.agent = _real


def _main(a):
    if not a or a[0] == "selftest":
        sys.exit(_selftest())
    elif a[0] == "build" and len(a) > 1:
        run = json.loads(Path(a[1]).read_text())
        print(json.dumps(build_report(run), indent=2))
    else:
        sys.exit("usage: qa_report.py selftest | build <run.json>")


if __name__ == "__main__":
    _main(sys.argv[1:])
