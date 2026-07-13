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
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

ROLE = "qa-auditor"


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
    return {
        "dir": evidence_dir,
        "input": _j("run-input.json", {}),
        "run": run_final,
        "coverage": _j("coverage.json", []),
        "checkpoint": _j("checkpoint.json", {}),
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
    s = str(s or "").replace("\n", " ").replace("|", "/")
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


def review(work, rubric=None, write=True):
    """Run the AI auditor over the run's evidence and return a grounded verdict dict. Writes AUDIT.md +
    audit.json into the evidence dir (write=True). Uses the FULL model (scrutiny is high-stakes, not 'light')."""
    evidence_dir = _resolve_dir(work)
    doss = dossier(evidence_dir)
    import factory
    res = factory.agent(ROLE, str(evidence_dir), _audit_prompt(doss["md"], rubric))
    raw = res.get("out_full") or res.get("out") or ""
    try:
        if str(SCRIPTS / "qa") not in sys.path:
            sys.path.insert(0, str(SCRIPTS / "qa"))
        from qa_explorer import _extract_json     # reuse the tolerant JSON extractor (handles code fences)
        verdict = _extract_json(raw)
    except Exception:
        start, end = raw.find("{"), raw.rfind("}")
        verdict = json.loads(raw[start:end + 1]) if start >= 0 and end > start else {}
    verdict = verdict or {"passed_audit": None, "summary": "auditor returned no parseable verdict",
                          "raw": raw[:800]}
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
    badge = "✅ ACCEPT" if passed else ("❌ NOT ACCEPTED" if passed is False else "⚠️ INCONCLUSIVE")
    return (f"# Work-execution AUDIT — {evidence_dir.name}\n\n"
            f"**Verdict:** {badge}  ·  **Score:** {v.get('score', '?')}/10\n\n"
            f"{v.get('summary', '')}\n\n"
            f"**Recommendation:** {v.get('recommendation', '?')}\n\n"
            f"## Flows a user would try that were SKIPPED\n{_bul(v.get('skipped_flows'))}\n\n"
            f"## Coverage claims with NO evidence\n{_bul(v.get('unbacked_claims'))}\n\n"
            f"## Vague / unverifiable reporting\n{_bul(v.get('vague_reporting'))}\n\n"
            f"## Evidence gaps\n{_bul(v.get('evidence_gaps'))}\n")


def _main(argv):
    if not argv:
        raise SystemExit("usage: review.py <evidence_dir | pulse-work-id> [--rubric \"...\"] [--dossier]")
    work = argv[0]
    rubric = None
    if "--rubric" in argv:
        rubric = argv[argv.index("--rubric") + 1]
    if "--dossier" in argv:                       # just print the evidence pack, no AI call
        print(dossier(work)["md"])
        return 0
    v = review(work, rubric=rubric)
    print(_verdict_md(_resolve_dir(work), v))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
