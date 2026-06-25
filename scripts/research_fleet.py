#!/usr/bin/env python3
"""research_fleet.py — research & analysis through the GOVERNED FLEET (not a couple of side agents).

Mirrors the build factory for a *research* workload: a lead decomposes a question into sub-questions,
a PARALLEL fleet of web-enabled research agents answers each one (bounded by the same global agent cap),
then an aggregator synthesizes one cited report. This is how the platform does deep research/analysis
with real fan-out + aggregation — e.g. the product blueprint should be built this way, by our own fleet.

    research_fleet.py run "<question>" [out.md]     # decompose -> parallel research -> synthesize
    research_fleet.py selftest
Run with the agent-os venv python. Agents already have WebSearch/WebFetch (factory.AGENT_TOOLS).
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit     # noqa: E402
import factory   # noqa: E402

MAX_SUBQ = int(os.environ.get("AOS_RESEARCH_SUBQ", "8"))


def _slug(q):
    return "".join(c if c.isalnum() else "-" for c in q.lower())[:40].strip("-") or "research"


def _load_json(path: Path):
    txt = path.read_text().strip()
    if "```" in txt:
        txt = txt.split("```")[1]
        txt = txt[4:] if txt.lower().startswith("json") else txt
    if not txt.lstrip().startswith("{"):
        txt = txt[txt.find("{"): txt.rfind("}") + 1]
    return json.loads(txt)


def _json_from_text(txt):
    """Pull the first {...} JSON object out of an agent's free-text reply."""
    s = txt.find("{")
    if s < 0:
        raise ValueError("no json")
    return json.loads(txt[s: txt.rfind("}") + 1])


def _parse_subqs(text: str) -> list:
    """Pull sub-questions out of an agent's free-text reply, most-specific format first (unit-tested).
    1) 'Q: ...' lines; 2) bulleted/numbered list items; 3) any line that is itself a question ('...?').
    Strips leading bullets/numbers/bold so the same line parses under any of the three strategies."""
    import re
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]

    def _strip(l):  # drop leading "Q:", "- ", "1. ", "1) ", "**" decoration
        l = re.sub(r"^\**\s*", "", l)
        l = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", l).strip()
        l = re.sub(r"^[Qq]\s*[:.)]\s*", "", l).strip()
        return l.strip("* ").strip()

    qs = [_strip(l) for l in lines if l.lower().startswith("q:") or l.lower().startswith("q.")]
    if len(qs) < 2:
        qs = [_strip(l) for l in lines if re.match(r"^(?:[-*•]|\d+[.)])\s+", l)]
    if len(qs) < 2:
        qs = [_strip(l) for l in lines if l.rstrip().endswith("?") and len(l) > 25]
    seen, out = set(), []                                 # de-dupe, keep order, drop empties
    for q in qs:
        if q and q.lower() not in seen:
            seen.add(q.lower()); out.append(q)
    return out


def decompose(repo: Path, question: str) -> list:
    """LEAD agent splits the question into independent sub-questions. ROBUST: parses Q:/bulleted/question-
    shaped replies, logs the raw reply on a parse-miss (so degradation is never silent), and only then falls
    back to single-pass — so a live run never crashes just because the model formatted its answer differently."""
    factory._ctx.product = repo.name; factory._ctx.run = f"research-{repo.name}"; factory._ctx.stage = "DECOMPOSE"
    before = {p for p in repo.rglob("*") if p.is_file()}   # snapshot, so we can find whatever the agent writes
    # The research-growth role WRITES FILES by instinct (it has Write even with tools=[]) — so we ASK it to
    # write the sub-questions to a known file and READ them back, rather than fighting it for stdout. We then
    # parse, in priority order: the named file -> any other file it created -> its chat reply -> single-pass.
    target = "SUBQUESTIONS.md"
    r = factory.agent("research-growth", str(repo),
                      f"Split this research question into {MAX_SUBQ} INDEPENDENT, specific, separately-"
                      f"researchable sub-questions that together fully cover it. Do NOT research anything — "
                      f"just decompose. QUESTION:\n{question}\n\nWrite them to the file `{target}` in the repo "
                      f"root, ONE PER LINE, each line starting with 'Q: ' — nothing else in the file.", tools=[])
    # Selection PRIORITY (not max-count: a verbose report yields 100s of '?'-lines and would wrongly win).
    # 1) the named target file; 2) any other small new file the agent wrote OUTSIDE findings/ (that dir is
    # research output); 3) the chat reply. First source that yields a sane 2..MAX_SUBQ*2 wins.
    def _sane(qs):                                         # a real decomposition, not a whole report body
        return qs if 2 <= len(qs) <= MAX_SUBQ * 2 else []
    subqs, src = [], "none"
    tf = repo / target
    if tf.exists():
        subqs, src = _sane(_parse_subqs(tf.read_text())), target
    if len(subqs) < 2:
        for p in sorted((p for p in repo.rglob("*") if p.is_file() and p not in before),
                        key=lambda p: -p.stat().st_mtime):  # newest first
            if p != tf and "findings" not in p.parts and p.suffix.lower() in (".md", ".txt"):
                cand = _sane(_parse_subqs(p.read_text()))
                if len(cand) >= 2:
                    subqs, src = cand, str(p.relative_to(repo)); break
    if len(subqs) < 2:
        subqs, src = _parse_subqs(r.get("out", "") or ""), "reply"
    # the agent may have polluted findings/ during decompose (it sometimes researches anyway) — clear it so
    # those stray files don't masquerade as fleet results downstream.
    for stray in (repo / "findings").glob("*"):
        try: stray.unlink()
        except Exception: pass
    if len(subqs) < 2:                                    # genuine miss -> log evidence, then single-pass
        print(f"[research] decompose fallback: single-pass (best parse {len(subqs)} from {src}; "
              f"rc={r.get('rc')}). REPLY HEAD:\n{(r.get('out') or '')[:400]!r}", flush=True)
        subqs = [question]
    else:
        print(f"[research] decomposed into {len(subqs)} sub-questions (from {src})", flush=True)
    (repo / "PLAN.json").write_text(json.dumps({"subquestions": subqs}))   # persist for resume
    return subqs[:MAX_SUBQ]


def research_one(repo: Path, idx: int, subq: str, api_key=None):
    """One web-enabled research agent answers one sub-question, citing sources, into findings/<idx>.md."""
    factory._ctx.api_key = api_key
    factory._ctx.product = repo.name; factory._ctx.run = f"research-{repo.name}"; factory._ctx.stage = f"RESEARCH:{idx}"
    out = f"findings/{idx:02d}.md"
    r = factory.agent("research-growth", str(repo),
                      f"Research this sub-question using WebSearch/WebFetch. Be concrete and HONEST — cite "
                      f"source URLs, and if a search yields nothing solid say so (do not fabricate). "
                      f"SUB-QUESTION:\n{subq}\n\nWrite your findings to {out} (markdown, with a 'Sources:' list).")
    fp = repo / out
    if not fp.exists() and r.get("out"):                 # robust: persist the reply if the agent didn't write the file
        fp.write_text(f"# {subq}\n\n{r['out']}\n")
    return {"idx": idx, "subq": subq, "ok": fp.exists(), "file": out}


def synthesize(repo: Path, question: str, out_rel: str):
    """AGGREGATOR reads all findings and writes one coherent, cited report."""
    factory._ctx.product = repo.name; factory._ctx.run = f"research-{repo.name}"; factory._ctx.stage = "SYNTHESIZE"
    factory.agent("research-growth", str(repo),
                  f"You are the research SYNTHESIZER. Read every file under findings/ and write ONE coherent, "
                  f"well-structured report at {out_rel} answering:\n{question}\n\nRequirements: lead with the "
                  f"key findings, preserve source URLs, flag anything weakly-sourced or contradictory, and be "
                  f"honest about gaps. Do NOT invent facts not in the findings.")
    return repo / out_rel


def research(question: str, out_rel: str = "REPORT.md", api_key=None) -> dict:
    """Full pipeline: decompose -> PARALLEL fleet research -> synthesize. Bounded by factory._AGENT_SEM."""
    repo = factory.PRODUCTS / f"research-{_slug(question)}"
    (repo / "findings").mkdir(parents=True, exist_ok=True)
    audit.append(actor="research:lead", action="ResearchStart", resource=repo.name, decision="executed",
                 payload={"question": question[:160]})
    subqs = decompose(repo, question)
    print(f"[research] {len(subqs)} sub-questions -> parallel fleet", flush=True)
    results, workers = [], int(os.environ.get("AOS_FLEET_WORKERS", "5"))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(research_one, repo, i, sq, api_key) for i, sq in enumerate(subqs)]
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                results.append({"ok": False, "error": str(e)})
    report = synthesize(repo, question, out_rel)
    ok = sum(1 for r in results if r.get("ok"))
    audit.append(actor="research:lead", action="ResearchComplete", resource=repo.name, decision="executed",
                 payload={"subq": len(subqs), "ok": ok, "report": str(report)})
    print(f"[research] done: {ok}/{len(subqs)} sub-questions answered -> {report}", flush=True)
    return {"report": str(report), "subquestions": len(subqs), "answered": ok}


def _selftest():
    """Offline check of the decompose→parallel→synthesize wiring with mocked agents (no web, no spend)."""
    import types
    import tempfile
    # _parse_subqs must survive the real reply shapes agents actually produce, not just clean "Q:" lines.
    p_q = _parse_subqs("Q: alpha?\nQ: beta?\nQ: gamma?")                       # canonical
    p_bullet = _parse_subqs("Here are the questions:\n- What is X?\n- What is Y?\n- What is Z?")
    p_num = _parse_subqs("1. How does A work?\n2. How does B work?\n3. How does C scale?")
    p_qmark = _parse_subqs("Sure!\nWhat are the steps to publish an app to the store?\n"
                           "How do solo founders handle onboarding for new users?")
    p_none = _parse_subqs("I cannot decompose this without more context.")     # -> single-pass upstream
    parse_ok = (len(p_q) == 3 and len(p_bullet) == 3 and len(p_num) == 3
                and len(p_qmark) == 2 and "Q:" not in p_q[0] and len(p_none) < 2)
    print(f"parse: Q={len(p_q)} bullet={len(p_bullet)} num={len(p_num)} qmark={len(p_qmark)} none={len(p_none)}")
    if not parse_ok:
        print("FAIL: _parse_subqs"); sys.exit(1)
    workdir = Path(tempfile.mkdtemp())
    # fake the three agent phases via PLAN.json + findings + report files
    calls = {"research": 0, "synth": 0}
    real_agent = factory.agent
    real_products = factory.PRODUCTS
    factory.PRODUCTS = workdir
    def fake_agent(role, repo, task, **k):
        repo = Path(repo)
        if "Split this research question" in task:                  # decompose -> Q: lines in the reply
            return {"rc": 0, "out": "here you go:\nQ: q1\nQ: q2\nQ: q3"}
        if "Write your findings" in task:
            f = task.split("Write your findings to ")[1].split(" ")[0]
            (repo / f).parent.mkdir(parents=True, exist_ok=True); (repo / f).write_text("finding\nSources: x")
            calls["research"] += 1
        elif "SYNTHESIZER" in task:
            (repo / "REPORT.md").write_text("# report"); calls["synth"] += 1
        return {"rc": 0, "out": "ok"}
    factory.agent = fake_agent
    try:
        r = research("how do solo founders distribute faceless")
        ok = (calls["research"] == 3 and calls["synth"] == 1 and Path(r["report"]).exists() and r["answered"] == 3)
        print(f"decompose->{r['subquestions']} subq, parallel research={calls['research']}, synth={calls['synth']}")
        print("PASS: research fleet (decompose -> parallel -> synthesize) ✅" if ok else "FAIL")
        sys.exit(0 if ok else 1)
    finally:
        factory.agent = real_agent; factory.PRODUCTS = real_products


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "run":
        print(research(a[1], a[2] if len(a) > 2 else "REPORT.md"))
    else:
        sys.exit("usage: research_fleet.py run \"<question>\" [out.md] | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
