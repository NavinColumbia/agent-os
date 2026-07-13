#!/usr/bin/env python3
"""review.py — deterministic WORK-EXECUTION AUDIT for agentic runs.

A first-class, REPEATABLE capability (not a surprise event) that lets an AUDITOR — the controller, the QA
coordinator, or any reviewer — scrutinise HOW an agent did its job, grounded in GROUND-TRUTH EVIDENCE (its
per-step decisions + REASONING, the screenshots/footage, the coverage ledger, the video) rather than the
agent's own — possibly vague — self-report. It exists to catch exactly the failures a demanding human
reviewer would: flows a real user would try that the agent SKIPPED (e.g. never viewed or iterated on a
generated plan, just accepted it), aspects marked 'covered' with NO step/screenshot behind them, and status
docs written too vaguely to tell whether a specific flow was actually exercised.

    dossier(evidence_dir)          assemble the reviewable evidence pack (structured dict + markdown)
    review(work, rubric=None)      run the AI auditor over the dossier -> grounded verdict + writes AUDIT.md
    python review.py <evidence_dir | qa-pulse-work-id> [--rubric "specific flows to verify"]

`work` may be an evidence directory OR a pulse work_id (e.g. "qa:console-...", resolved via agent_pulse.meta).
The audit is grounded and adversarial: every judgement must cite a step index or a screenshot, and the
DEFAULT posture is skeptical — "not shown in the evidence" reads as NOT DONE, never "probably fine".
"""
import json
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

ROLE = "qa-auditor"

# The auditor's model. Research ("One Token to Fool LLM-as-a-Judge", arXiv 2507.08794): LARGER judges are MORE
# vulnerable to master-key gaming; a mid-sized model best balances robustness. We honour the owner's Opus-default
# but expose AOS_AUDITOR_MODEL so ops can pin the research-recommended mid-sized judge (e.g. sonnet) without a
# deploy. None => inherit factory's default (Opus). The stronger defenses (sanitization + ensemble below) do the
# heavy lifting regardless of model.
AUDITOR_MODEL = os.environ.get("AOS_AUDITOR_MODEL") or None

# --- Master-key sanitization (finding 7 / signal A2) ---------------------------------------------------------
# A single-pass LLM judge can be flipped to a false-positive "correct" verdict by trivial verdict-eliciting
# tokens embedded in the text it judges — reasoning-opener phrases ("Thought process:", "Let's solve step by
# step") or bare symbols (":", "."). Our auditor judges AGENT-WRITTEN evidence (a worker's per-step reasoning),
# so a sloppy-or-adversarial worker could prime the auditor the same way. We NEUTRALIZE those tokens in the
# agent-authored fields before the auditor reads them, and leave a visible marker (their presence is itself
# suspicious signal, not something to silently pass).
_MASTER_KEY_OPENERS = re.compile(
    r"^\s*(thought process|thought|solution|reasoning|answer|final answer|verdict|let'?s (?:solve|think)"
    r"(?: this)?(?:[ -]?(?:step[ -]by[ -]step|out))?|step[ -]by[ -]step)\s*[:：.\-]*\s*",
    re.IGNORECASE)
_SYMBOLS_ONLY = re.compile(r"^[\s\W_]+$")


def _sanitize(text) -> str:
    """Neutralize judge-priming 'master-key' tokens in an agent-written evidence field. Returns inert text with a
    visible marker where a priming token was stripped, so the auditor sees the tampering rather than being
    steered by it."""
    s = str(text or "")
    if _SYMBOLS_ONLY.match(s) and s.strip():
        return "⟨priming-symbols-only:neutralized⟩"
    stripped = _MASTER_KEY_OPENERS.sub("", s)
    if stripped != s:
        return "⟨priming-opener:neutralized⟩ " + stripped.strip()
    return s


def _resolve_dir(work) -> Path:
    """Accept an evidence dir OR a pulse work_id. A work_id is looked up in agent_pulse.meta.evidence_dir."""
    p = Path(str(work)).expanduser()
    if p.is_dir():
        return p
    try:
        import pulse
        if pulse.DB:
            import psycopg
            with psycopg.connect(pulse.DB) as c, c.cursor() as cur:
                cur.execute("SELECT meta->>'evidence_dir' FROM agent_pulse WHERE work_id=%s", (str(work),))
                row = cur.fetchone()
                if row and row[0] and Path(row[0]).is_dir():
                    return Path(row[0])
    except Exception:
        pass
    raise SystemExit(f"review: '{work}' is neither an evidence dir nor a known pulse work_id")


def _load(evidence_dir: Path) -> dict:
    def _j(name, default):
        f = evidence_dir / name
        try:
            return json.loads(f.read_text()) if f.exists() else default
        except Exception:
            return default
    run_final = _j("run-final.json", {})
    # The REAL dev work, not the self-report: each fix-round-N.json records what dev actually changed (files),
    # whether its own fix-judge said fixed, and the RESIDUAL bugs still open after the fix + restart. Grounding
    # the auditor in these catches the classic dishonest stop — "fixed!" while a residual bug remained.
    fix_rounds = []
    for p in sorted(evidence_dir.glob("fix-round-*.json")):
        fr = _j(p.name, {})
        if fr:
            fix_rounds.append({"round": p.stem.split("-")[-1], "fixed": fr.get("fixed"),
                               "files": fr.get("files") or [], "residual": fr.get("residual") or [],
                               "reason": ((fr.get("verdict") or {}).get("reason")
                                          or (fr.get("verdict") or {}).get("summary") or fr.get("error"))})
    return {
        "dir": evidence_dir,
        "input": _j("run-input.json", {}),
        "run": run_final,
        "coverage": _j("coverage.json", []),
        "checkpoint": _j("checkpoint.json", {}),
        "fix_rounds": fix_rounds,
        "stories": run_final.get("stories", []),
        "screenshots": sorted(str(p) for p in (evidence_dir / "screenshots").glob("*.png")),
        "video": next((str(p) for p in (evidence_dir / "videos").glob("*.mp4")
                       if p.name != "qa-session.mp4"), None),
        "session_video": str(evidence_dir / "qa-session.mp4") if (evidence_dir / "qa-session.mp4").exists() else None,
    }


def dossier(evidence_dir) -> dict:
    """Assemble the reviewable evidence pack: intent + coverage ledger + the per-step log (reasoning, action,
    expected, ACTUAL, verdict, aspects claimed, screenshot). Returns {'data':..., 'md': <str>}."""
    evidence_dir = _resolve_dir(evidence_dir)
    d = _load(evidence_dir)
    L = [f"# Work-execution evidence — {evidence_dir.name}", "",
         f"**Vision:** {d['input'].get('vision', '(none)')}", "",
         f"**Screenshots on disk:** {len(d['screenshots'])} · **Session video:** "
         f"{d['session_video'] or '(none)'}", ""]
    if d.get("fix_rounds"):
        L += ["## Dev-fix rounds (the ACTUAL changes + residual bugs — cross-check claims against these)", "",
              "| round | dev claims fixed | files changed | RESIDUAL bugs still open | fix-judge reason |",
              "|---|---|---|---|---|"]
        for fr in d["fix_rounds"]:
            L.append(f"| {fr['round']} | {fr.get('fixed')} | {_clip('; '.join(fr.get('files') or []), 60)} | "
                     f"**{len(fr.get('residual') or [])}** | {_clip(fr.get('reason'), 80)} |")
        L.append("")
    for cov in d["coverage"]:
        L += [f"## Story: {cov.get('story', '?')}",
              f"- reported stop reason: **{cov.get('stop_reason', '?')}**",
              f"- TESTED ({len(cov.get('tested', []))}): " + ("; ".join(cov.get("tested", [])) or "(none)"),
              f"- YET-TO-TEST ({len(cov.get('yet_to_test', []))}): "
              + ("; ".join(cov.get("yet_to_test", [])) or "(none)"), ""]
    for st in d["stories"]:
        L += [f"## Step-by-step for: {st.get('title', st.get('id', '?'))}  (reported status: {st.get('status')})",
              "", "| # | reasoning (WHY) | action | expected | ACTUAL | verdict | claimed-covers | screenshot |",
              "|---|---|---|---|---|---|---|---|"]
        for i, s in enumerate(st.get("steps", [])):
            shot = Path(s.get("screenshot") or "").name or "-"
            L.append(f"| {i} | {_clip(s.get('reasoning'))} | {_clip(s.get('action'))} | "
                     f"{_clip(s.get('expected'))} | {_clip(s.get('actual'))} | {s.get('verdict', '?')} | "
                     f"{_clip('; '.join(s.get('covers') or []))} | {shot} |")
        L.append("")
    return {"data": d, "md": "\n".join(L)}


def _clip(s, n=90):
    s = _sanitize(s).replace("\n", " ").replace("|", "/")   # neutralize judge-priming tokens before the auditor reads
    return (s[:n] + "…") if len(s) > n else s


def _audit_prompt(doss_md, rubric):
    return f"""ROLE: You are a demanding, SKEPTICAL work-execution AUDITOR (like a FAANG QA lead reviewing a
report). You are reviewing HOW a QA agent did its job — from the GROUND-TRUTH EVIDENCE below (its per-step
reasoning + actions + ACTUAL results + the screenshots on disk), NOT from any self-congratulatory summary.

Your job is to catch what a rigorous human reviewer would catch:
  * FLOWS A USER WOULD TRY THAT WERE SKIPPED — e.g. it never viewed/iterated on a generated plan, just
    accepted it; never tested cancel/back/re-entry; declared done with obvious paths untried.
  * COVERAGE CLAIMS WITH NO EVIDENCE — an aspect marked tested but no step/screenshot actually exercises it.
  * VAGUE / UNVERIFIABLE REPORTING — status or reasoning so hand-wavy you cannot tell from it whether a
    specific flow really happened. Vagueness that hides a gap is itself a finding.
  * DISHONEST STOP — stopped claiming success while the evidence shows it was stuck or gave up.
  * CLAIMED-FIX vs RESIDUAL — if a dev-fix round says fixed=true but its RESIDUAL-bugs count is >0, the fix did
    NOT hold; a run reported 'passed' over a nonzero residual is a dishonest stop. Cross-check the fix-round
    table against the final story verdicts.

DEFAULT POSTURE: if the evidence does not SHOW it, it did NOT happen. Do not give benefit of the doubt.
You MAY open specific screenshots (their filenames are in the table; they live in ./screenshots/) to verify
a suspicious claim — cite the screenshot when you do.

{("=== EXTRA RUBRIC — specific things this audit MUST check ===\n" + rubric + "\n") if rubric else ""}
=== EVIDENCE ===
{doss_md}

Reply with ONLY JSON, no prose:
{{
  "passed_audit": <true only if the agent genuinely did a thorough job a skeptic would accept>,
  "score": <0-10 integer — how well it actually did the job the evidence supports>,
  "skipped_flows": ["<a flow a real user/human would try that the evidence shows was NOT done>"],
  "unbacked_claims": ["<an aspect claimed covered with no step/screenshot behind it — cite why>"],
  "vague_reporting": ["<a status/reasoning entry too vague to verify a specific flow, and what it hides>"],
  "evidence_gaps": ["<missing evidence that SHOULD exist to prove a claim>"],
  "summary": "<2-3 sentences: did this agent actually do a job you'd accept? grounded in the evidence>",
  "recommendation": "<accept | redo-specific-flows | reject — and the concrete next action>"
}}"""


# Perspective-diverse jurors. Research (2604.16706: 3-LLM ensemble kappa 0.432 vs 0.049 for single heuristic;
# 2512.16041: panel juries +15%, and judges degrade ~200% on close calls) says a small, DIVERSE panel beats one
# pass and — crucially — turns a hard/close call into a visible split we can escalate instead of a confident
# wrong verdict. Each juror gets a distinct lens so they fail in different ways (diversity > redundancy).
_LENSES = {
    "skeptic": "Lead lens: assume nothing worked unless the evidence proves it. Hunt dishonest stops and "
               "success claimed over stuck/gave-up evidence.",
    "user-flow": "Lead lens: think like a real user. Enumerate the flows a user WOULD try (cancel, back, "
                 "re-entry, edit/iterate on generated output, error paths) and check which the evidence shows "
                 "were actually exercised vs skipped.",
    "evidence": "Lead lens: audit the evidence chain. For every 'covered'/'passed' claim, demand the specific "
                "step index or screenshot that backs it; flag coverage asserted with nothing behind it.",
}


def _parse_verdict(raw):
    try:
        if str(SCRIPTS / "qa") not in sys.path:
            sys.path.insert(0, str(SCRIPTS / "qa"))
        from qa_explorer import _extract_json     # reuse the tolerant JSON extractor (handles code fences)
        v = _extract_json(raw)
    except Exception:
        start, end = raw.find("{"), raw.rfind("}")
        v = json.loads(raw[start:end + 1]) if start >= 0 and end > start else {}
    return v or {"passed_audit": None, "summary": "auditor returned no parseable verdict", "raw": raw[:800]}


def _one_review(evidence_dir, doss_md, rubric, lens_name):
    """One juror pass under a given lens. Returns a verdict dict tagged with its lens."""
    import factory
    lens = _LENSES.get(lens_name, "")
    prompt = _audit_prompt(doss_md, "\n".join(x for x in (lens, rubric) if x) or None)
    res = factory.agent(ROLE, str(evidence_dir), prompt, model=AUDITOR_MODEL)
    v = _parse_verdict(res.get("out_full") or res.get("out") or "")
    v["lens"] = lens_name
    return v


def _aggregate(verdicts):
    """Fuse jury verdicts. Conservative + skeptical: ACCEPT only on a UNANIMOUS accept; ANY dissent → not
    accepted + close_call → escalate. Union the finding lists so nothing a single juror caught is lost."""
    def _uni(key):
        seen, out = set(), []
        for v in verdicts:
            for x in (v.get(key) or []):
                if str(x) not in seen:
                    seen.add(str(x)); out.append(x)
        return out
    votes = [v.get("passed_audit") for v in verdicts]
    yes = sum(1 for x in votes if x is True)
    no = sum(1 for x in votes if x is False)
    unanimous_accept = yes == len(votes) and no == 0 and all(x is not None for x in votes)
    split = not (yes == len([x for x in votes if x is not None]) or no == len([x for x in votes if x is not None]))
    scores = [v.get("score") for v in verdicts if isinstance(v.get("score"), (int, float))]
    passed = True if unanimous_accept else (False if no else None)
    rec = ("accept" if unanimous_accept else
           ("ESCALATE — jury split, treat as a close call (do NOT auto-accept); a human/CEO tier should rule"
            if split else "reject" if no else "inconclusive"))
    return {
        "passed_audit": passed,
        "close_call": bool(split),
        "score": round(sum(scores) / len(scores), 1) if scores else None,
        "jury": [{"lens": v.get("lens"), "passed_audit": v.get("passed_audit"), "score": v.get("score")}
                 for v in verdicts],
        "jury_vote": f"{yes} accept / {no} reject / {len(votes) - yes - no} unclear (of {len(votes)})",
        "skipped_flows": _uni("skipped_flows"),
        "unbacked_claims": _uni("unbacked_claims"),
        "vague_reporting": _uni("vague_reporting"),
        "evidence_gaps": _uni("evidence_gaps"),
        "summary": (" | ".join(v.get("summary", "") for v in verdicts if v.get("summary"))[:900]
                    or "jury returned no summaries"),
        "recommendation": rec,
    }


def review(work, rubric=None, write=True, ensemble=None, lenses=None):
    """Run the AI auditor over the run's evidence and return a grounded verdict dict. By default a small
    PERSPECTIVE-DIVERSE JURY (not a single pass) judges the evidence; a jury SPLIT is surfaced as a close call to
    ESCALATE rather than a confident wrong verdict (findings 7-9). Writes AUDIT.md + audit.json into the evidence
    dir (write=True). `ensemble` overrides the juror count (env AOS_AUDITOR_ENSEMBLE, default 3)."""
    evidence_dir = _resolve_dir(work)
    doss = dossier(evidence_dir)
    n = int(ensemble if ensemble is not None else os.environ.get("AOS_AUDITOR_ENSEMBLE", "3"))
    lens_names = list(lenses) if lenses else list(_LENSES)
    lens_names = (lens_names * ((n // len(lens_names)) + 1))[:max(1, n)]
    verdicts = [_one_review(evidence_dir, doss["md"], rubric, ln) for ln in lens_names]
    verdict = _aggregate(verdicts) if len(verdicts) > 1 else verdicts[0]
    if write:
        try:
            (evidence_dir / "audit.json").write_text(json.dumps(verdict, indent=2, default=str))
            (evidence_dir / "AUDIT.md").write_text(_verdict_md(evidence_dir, verdict))
        except Exception:
            pass
    return verdict


def _verdict_md(evidence_dir, v):
    def _bul(items):
        return "\n".join(f"- {x}" for x in (items or [])) or "- (none)"
    passed = v.get("passed_audit")
    if v.get("close_call"):
        badge = "⚖️ CLOSE CALL → ESCALATE (jury split)"
    else:
        badge = "✅ ACCEPT" if passed else ("❌ NOT ACCEPTED" if passed is False else "⚠️ INCONCLUSIVE")
    jury = f"  ·  **Jury:** {v['jury_vote']}" if v.get("jury_vote") else ""
    return (f"# Work-execution AUDIT — {evidence_dir.name}\n\n"
            f"**Verdict:** {badge}  ·  **Score:** {v.get('score', '?')}/10{jury}\n\n"
            f"{v.get('summary', '')}\n\n"
            f"**Recommendation:** {v.get('recommendation', '?')}\n\n"
            f"## Flows a user would try that were SKIPPED\n{_bul(v.get('skipped_flows'))}\n\n"
            f"## Coverage claims with NO evidence\n{_bul(v.get('unbacked_claims'))}\n\n"
            f"## Vague / unverifiable reporting\n{_bul(v.get('vague_reporting'))}\n\n"
            f"## Evidence gaps\n{_bul(v.get('evidence_gaps'))}\n")


def _selftest():
    """Offline proof of the Tier-0 hardening: master-key sanitization + perspective-diverse jury with
    disagreement→escalate. Stubs factory.agent (no model calls)."""
    import tempfile, types

    # 1. Sanitization neutralizes judge-priming tokens but keeps real content.
    assert _sanitize("Thought process: it works").startswith("⟨priming-opener"), _sanitize("Thought process: it works")
    assert _sanitize("Let's solve step by step").startswith("⟨priming-opener"), "opener not caught"
    assert _sanitize(":").startswith("⟨priming-symbols-only"), "bare symbol not caught"
    assert _sanitize("clicked Generate, plan rendered") == "clicked Generate, plan rendered", "real text altered"

    ev = Path(tempfile.mkdtemp(prefix="audit-selftest-"))
    (ev / "run-input.json").write_text(json.dumps({"vision": "console QA"}))
    (ev / "run-final.json").write_text(json.dumps({"stories": [{"id": "s1", "title": "generate a plan",
        "status": "done", "steps": [{"reasoning": "Thought process: obviously fine", "action": "click Generate",
        "expected": "plan shows", "actual": "plan shown", "verdict": "pass", "covers": ["generate"]}]}]}))
    (ev / "coverage.json").write_text(json.dumps([{"story": "generate a plan", "stop_reason": "covered",
        "tested": ["generate"], "yet_to_test": []}]))
    # a dev-fix round that CLAIMS fixed but left a residual bug — the auditor must be able to see this
    (ev / "fix-round-1.json").write_text(json.dumps({"fixed": True, "files": ["app.py"],
        "residual": [{"title": "save still 500s"}], "verdict": {"reason": "diff applied"}}))

    # 1b. The dossier must NOT contain the raw priming token (it was sanitized before the auditor sees it),
    #     and it MUST surface the dev-fix round + its residual count (grounding in the real changes).
    md = dossier(ev)["md"]
    assert "Thought process: obviously fine" not in md and "priming-opener" in md, "dossier not sanitized"
    assert "Dev-fix rounds" in md and "app.py" in md, "dossier must ground the auditor in the real fix rounds"

    _real = sys.modules.get("factory")
    fake = types.ModuleType("factory")
    # Split jury: skeptic rejects, the other two accept → must ESCALATE as a close call, never auto-accept.
    def _agent(role, repo, task, **k):
        v = ('{"passed_audit":false,"score":4,"skipped_flows":["never tested cancel"],"unbacked_claims":[],'
             '"vague_reporting":[],"evidence_gaps":[],"summary":"stuck","recommendation":"redo"}'
             if "assume nothing worked" in task else
             '{"passed_audit":true,"score":8,"skipped_flows":[],"unbacked_claims":[],"vague_reporting":[],'
             '"evidence_gaps":[],"summary":"looks ok","recommendation":"accept"}')
        return {"rc": 0, "out_full": v}
    fake.agent = _agent
    sys.modules["factory"] = fake
    try:
        v = review(ev, ensemble=3, write=True)
        assert v["close_call"] is True, f"a split jury must be a close call: {v}"
        assert v["passed_audit"] is not True, "must NOT auto-accept on a split"
        assert "ESCALATE" in v["recommendation"], v["recommendation"]
        assert "never tested cancel" in v["skipped_flows"], "must union each juror's findings"
        assert v["jury_vote"].startswith("2 accept / 1 reject") or v["jury_vote"].startswith("1 reject"), v["jury_vote"]
        # Unanimous accept path.
        fake.agent = lambda role, repo, task, **k: {"rc": 0, "out_full":
            '{"passed_audit":true,"score":9,"skipped_flows":[],"unbacked_claims":[],"vague_reporting":[],'
            '"evidence_gaps":[],"summary":"thorough","recommendation":"accept"}'}
        v2 = review(ev, ensemble=3, write=False)
        assert v2["passed_audit"] is True and not v2["close_call"], f"unanimous accept: {v2}"
        print("review.py selftest: PASS (sanitization neutralizes priming tokens; a split jury ESCALATES as a "
              "close call and never auto-accepts; unanimous accept still passes)")
        return 0
    finally:
        if _real is not None:
            sys.modules["factory"] = _real
        else:
            sys.modules.pop("factory", None)
        import shutil
        shutil.rmtree(ev, ignore_errors=True)


def _main(argv):
    if argv and argv[0] == "--selftest":
        return _selftest()
    if not argv:
        raise SystemExit("usage: review.py <evidence_dir | pulse-work-id> [--rubric \"...\"] [--dossier] "
                         "[--ensemble N] | --selftest")
    work = argv[0]
    rubric = None
    if "--rubric" in argv:
        rubric = argv[argv.index("--rubric") + 1]
    ensemble = int(argv[argv.index("--ensemble") + 1]) if "--ensemble" in argv else None
    if "--dossier" in argv:                       # just print the evidence pack, no AI call
        print(dossier(work)["md"])
        return 0
    v = review(work, rubric=rubric, ensemble=ensemble)
    print(_verdict_md(_resolve_dir(work), v))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
