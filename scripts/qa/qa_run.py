#!/usr/bin/env python3
"""qa_run.py — the TOP-LEVEL autonomous loop of agent-os's agentic QA system.

This is the "vision in -> bug-free out, hours OK" loop. It composes the four state-based pieces
(each of which is itself maximally AI-driven — EVERY decision is a `factory.agent` call that retries
529/overload and fails over to Codex) into one closed loop that holds the ORIGINAL VISION + the
EXPECTED behavior in context and drives a real product until it is bug-free (or a round cap trips):

    stories = story_gen.generate_stories(vision, product_summary)     # AI enumerates the coverage set
    repeat (bounded rounds):
        for each story:
            Explorer(target_url, vision, token, org).explore(story, on_bug=...)   # observe->AI decide
                                                                                  # ->act->observe->AI eval
        if the round found a BLOCKING bug:
            dev_loop.fix_bug(bug, code_context, vision, ...)   # AI plans #agents -> spawns -> AI judges
            RESET: restart the app (dev_loop.restart_target)   # observe a FRESH process next round
            RE-RUN all stories from scratch                    # a fix can regress anything
        else:
            stop — a clean round means the product matches its vision
    qa_report.build_report(run)                                # grounded verdict + AI narrative, on disk
    _persist_run(...)                                          # qa_runs row in Postgres — durable QA history
    _gate_artifact(...)                                        # machine verdict JSON the LAUNCH gate binds to
    _file_open_bugs(...)                                       # EVERY open bug -> a governed findings.py item

C1 hardening (REBUILD-PLAN, quality-engine): a run is never just a /tmp markdown file anymore.
(1) Every run is persisted to Postgres (qa_runs: product, rounds, verdict json, report paths, ts) so a
reboot cannot erase QA history. (2) The run writes a machine-verifiable VERDICT ARTIFACT
(verdict-*.json next to the report + <repo>/docs/QA-VERDICT.json when a repo is given) — passed /
blocking_open / stories, sha256-linked to the full report JSON — which is what the LAUNCH gate consumes,
never prose. (3) ANY bug still OPEN at the end (not just blocking) is filed through findings.py with an
AI-routed owner + SLA and a stored re-verification recipe (the originating story), because 'zero OPEN
bugs' is the ship bar; blocking-only is merely the emergency bar. No bug can reach a human as a footnote
in a report nobody owns.

Design stance (owner-mandated): cost is NOT a concern — be maximally AI-driven; the loop is STATE-BASED
(observe -> decide -> act -> observe -> evaluate); and it judges EXPECTED-vs-ACTUAL against the vision.

    python qa_run.py selftest    # offline wiring check — stubs every AI call + the browser, no network
    python qa_run.py smoke       # BOUNDED end-to-end: live console + one tiny REAL story (real AI calls)

Run with the agent-os venv python.
"""
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

SCRIPTS = Path(__file__).resolve().parent.parent          # scripts/ (qa_run.py lives in scripts/qa/)
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
QA = Path(__file__).resolve().parent
if str(QA) not in sys.path:
    sys.path.insert(0, str(QA))

import factory        # noqa: E402  — the resilient LLM seam (retries overload, fails over to Codex)
import story_gen      # noqa: E402  — AI enumerates the user-story coverage set
import qa_explorer    # noqa: E402  — the state-based AI explorer (observe->decide->act->observe->evaluate)
import dev_loop       # noqa: E402  — AI dev-fix loop + app restart/reset
import qa_report      # noqa: E402  — grounded verdict + AI narrative report writer
import artifacts      # noqa: E402  — Windows-visible evidence archive paths
import pulse          # noqa: E402  — live heartbeat plane (observability: this run is alive + what it's doing)

# Rounds = fix-and-re-test cycles + coverage gap-fill cycles. This is only a RUNAWAY cap, not the real
# terminator: the loop stops itself on coverage-complete or an honest gap-stall (a gap round that covers
# nothing new). Liberal by default (was 6); raise/lower with AOS_QA_MAX_ROUNDS.
MAX_ROUNDS = int(os.environ.get("AOS_QA_MAX_ROUNDS", "12"))    # runaway cap on fix + gap-fill rounds
# NOT a target, NOT a quality cap — a pure SAFETY BACKSTOP. The explorer is COVERAGE-DRIVEN: it stops when
# it has tested everything a real user would try (an AI judgment), not after N clicks. This number only
# guards against a runaway loop; when it trips, the run is marked INCOMPLETE and checkpoints what's left.
# Default 250 (very high — a real end-to-end journey rarely needs it). Set AOS_QA_MAX_STEPS=0 for unbounded.
_SAFETY_STEPS = int(os.environ.get("AOS_QA_MAX_STEPS", "250"))
MAX_STEPS = _SAFETY_STEPS or None                              # None => unbounded (ledger + stall guard bound it)
# One AI reply occasionally comes back as prose instead of the JSON story array; a single flaky reply
# must not collapse the whole gate into a 0-story NO VERDICT — retry the enumeration (each attempt is a
# fresh factory.agent call) before failing closed.
STORY_GEN_ATTEMPTS = int(os.environ.get("AOS_QA_STORYGEN_ATTEMPTS", "3"))
# Optional cap on the exercised story count (0 = unlimited). An operator-set bound for small/simple
# products where exhaustive enumeration overshoots (a tip calculator does not need 60 stories); the
# verdict facts stay honest — stories>0 must still be REAL explored stories for the gate to pass.
MAX_STORIES = int(os.environ.get("AOS_QA_MAX_STORIES", "0"))

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


# ───────────────────────────────────────────────────────────────────────────────────────────────────
# durable evidence — every run is a Postgres row; every open bug is a governed, owned finding.
# ───────────────────────────────────────────────────────────────────────────────────────────────────
def _persist_run(report: dict, run: dict) -> int:
    """Every QA run becomes a qa_runs row (product, rounds, verdict json, report paths, ts): QA history
    is durable evidence in Postgres, not files in /tmp a reboot erases. Returns the row id."""
    verdict = {k: report.get(k) for k in ("verdict", "summary", "passed", "total_stories", "total_bugs",
                                          "open_bugs", "blocking_open", "clean", "rounds")}
    verdict["url"] = run.get("url")
    verdict["vision"] = (run.get("vision") or "")[:500]
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS qa_runs (
            id BIGSERIAL PRIMARY KEY, product TEXT NOT NULL, rounds INT NOT NULL,
            passed BOOLEAN NOT NULL, clean BOOLEAN NOT NULL, verdict JSONB NOT NULL,
            report_md TEXT, report_json TEXT, ts TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute("""INSERT INTO qa_runs (product, rounds, passed, clean, verdict, report_md, report_json)
                       VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (run.get("product", "app"), int(report.get("rounds") or 0), bool(report.get("passed")),
                     bool(report.get("clean")), Jsonb(verdict), report.get("md"), report.get("json")))
        rid = cur.fetchone()[0]; c.commit()
    return rid


def _route_role(bug: dict) -> str:
    """AI DECISION: which role OWNS this bug's fix (factory.agent, strict-JSON answer validated against
    the live role manifests). Deterministic fallback 'builder' so a routing hiccup never drops a bug."""
    try:
        import orchestrate
        roles = orchestrate.known_roles()
    except Exception:
        roles = set()
    cands = [r for r in ("builder", "frontend-engineer", "backend-engineer", "fullstack-engineer",
                         "devops-sre", "qa-security") if r in roles] or ["builder"]
    try:
        prompt = ("A QA explorer found this bug in a shipped product. Choose the ONE role that should "
                  f"OWN the fix, from exactly this list: {', '.join(cands)}\n"
                  f"BUG: {bug.get('title', '')} — {(bug.get('detail') or '')[:300]}\n"
                  f"WHERE: {(bug.get('actual') or '')[:200]}\n"
                  'Reply with ONLY a JSON object: {"role": "<role-name>"}')
        res = factory.agent("controller", str(factory.PRODUCTS), prompt, light=True)
        out = (res.get("out_full") or res.get("out") or "") if isinstance(res, dict) else ""
        cand = json.loads(out[out.index("{"): out.rindex("}") + 1]).get("role", "")
        if cand in cands:
            return cand
    except Exception:
        pass
    return "builder"


def _file_open_bugs(report: dict, run: dict, stories: list, ctx: dict) -> list:
    """THE SHIP BAR: every bug still OPEN at the end of a run — blocking or NOT — is filed through
    findings.py as a governed, AI-routed, owned, SLA-tracked work item carrying its re-verification
    recipe (the originating story), so findings.resolve() can only land on a fresh passing re-run of
    that story. 'Noted in a markdown file' is exactly how bugs reach humans; zero OPEN bugs is the bar
    (blocking-only is merely the emergency bar). Returns the filing dispositions."""
    open_bugs = [b for b in (run.get("bugs") or []) if not (b.get("fixed") or b.get("resolved"))]
    if not open_bugs:
        return []
    import findings
    by_id = {s.get("id"): s for s in (stories or [])}
    filed = []
    for b in open_bugs:
        story = by_id.get(b.get("story"))
        verify = None
        if story:
            verify = {"kind": "qa_story", "product": run.get("product", "app"),
                      "target_url": ctx["target_url"], "vision": ctx["vision"], "token": ctx["token"],
                      "org": ctx["org"], "summary": ctx["summary"], "story": story,
                      "max_steps": ctx["max_steps"]}
        detail = json.dumps({"bug": b.get("detail") or b.get("title"), "expected": b.get("expected"),
                             "actual": b.get("actual"), "screenshot": b.get("screenshot"),
                             "story": b.get("story"), "blocking": bool(b.get("blocking")),
                             "report_md": report.get("md"), "qa_run_id": report.get("qa_run_id")},
                            default=str)
        try:
            r = findings.file(f"qa:{run.get('product', 'app')}", _route_role(b),
                              f"[{run.get('product', 'app')}] {b.get('title', 'defect')}", detail,
                              severity=b.get("severity", "medium"),
                              priority=2 if b.get("blocking") else 4, verify=verify)
        except Exception as e:                            # captured, surfaced in the report — never silent
            r = {"error": str(e), "bug": b.get("id")}
        filed.append(r)
    return filed


def _file_audit_gaps(av: dict, product: str, report: dict, evidence_dir) -> list:
    """The AUDITOR's found gaps -> governed, owned, SLA-tracked findings, so a REJECTED audit becomes real
    dev/QA work, not just a blocked gate. Each skipped flow + unbacked coverage claim becomes an item,
    AI-routed to the owning role exactly like a QA bug (with the AUDIT.md as evidence)."""
    import findings
    gaps = ([("skipped-flow", g) for g in (av.get("skipped_flows") or [])]
            + [("unbacked-coverage-claim", g) for g in (av.get("unbacked_claims") or [])])
    filed = []
    for kind, g in gaps:
        pseudo = {"title": f"audit {kind}", "detail": str(g), "actual": av.get("recommendation", "")}
        detail = json.dumps({"audit_finding": g, "kind": kind, "auditor_score": av.get("score"),
                             "recommendation": av.get("recommendation"),
                             "audit_md": str(Path(evidence_dir) / "AUDIT.md"),
                             "qa_run_id": report.get("qa_run_id"), "report_md": report.get("md")},
                            default=str)
        try:
            r = findings.file(f"audit:{product}", _route_role(pseudo),
                              f"[{product}] audit gap: {str(g)[:80]}", detail, severity="high", priority=3)
        except Exception as e:
            r = {"error": str(e), "gap": str(g)[:80]}
        filed.append(r)
    return filed


# ───────────────────────────────────────────────────────────────────────────────────────────────────
# record shaping — turn Explorer step-records + accumulated bugs into the qa_report `run` contract.
# ───────────────────────────────────────────────────────────────────────────────────────────────────
def _fmt_action(action) -> str:
    """A compact human string for a bridge action dict {cmd, idx|selector, value}."""
    if not isinstance(action, dict):
        return str(action)
    cmd = action.get("cmd", "?")
    tgt = action.get("selector")
    if tgt is None and action.get("idx") is not None:
        tgt = f"idx={action['idx']}"
    val = action.get("value")
    bits = [cmd]
    if tgt:
        bits.append(str(tgt))
    if val not in (None, ""):
        bits.append(f"={val!r}")
    return " ".join(bits)


def _actual_str(actual: dict, verdict: dict) -> str:
    """What was OBSERVED after the action, for the report's expected-vs-actual column."""
    actual = actual or {}
    parts = []
    if actual.get("url"):
        parts.append(actual["url"])
    errs = actual.get("console_errors") or []
    if errs:
        parts.append(f"{len(errs)} console error(s)")
    if verdict and verdict.get("bug"):
        parts.append(f"BUG: {verdict['bug']}")
    return " · ".join(str(p) for p in parts) or "(no change observed)"


def _story_report(story: dict, records: list, story_bugs: list) -> dict:
    """One story's report entry: status + per-step expected-vs-actual. Status is grounded in what the
    explorer actually saw: a blocking bug BLOCKS the story; any other bug FAILS it; otherwise it PASSED."""
    steps = []
    for r in (records or []):
        v = r.get("verdict") or {}
        steps.append({
            "action": _fmt_action(r.get("action")),
            "reasoning": r.get("reasoning", ""),                 # WHY — so an auditor can judge intent, not just outcome
            "expected": r.get("expected", ""),
            "actual": _actual_str(r.get("actual"), v),
            "verdict": "match" if v.get("matches_expected") else "mismatch",
            "covers": r.get("covers", []),                       # which ledger aspects this step CLAIMED to exercise
            "screenshot": (r.get("actual") or {}).get("screenshot"),
        })
    blocking = any(b.get("blocking") for b in story_bugs)
    status = "blocked" if blocking else ("failed" if story_bugs else "passed")
    return {
        "id": story.get("id", ""),
        "title": story.get("title", story.get("name", "(untitled)")),
        "expected": story.get("expected_outcome", story.get("expected", "")),
        "status": status,
        "steps": steps,
    }


def _skey(d: dict) -> str:
    """A stable key for a story or its report (id, else title, else name) — used to accumulate coverage
    across rounds and to re-run just the incomplete stories on a gap-fill round."""
    return str(d.get("id") or d.get("title") or d.get("name") or "").strip()


def _report_incomplete(sr: dict) -> bool:
    """A story report is INCOMPLETE when coverage aspects remain untested, or it stopped for an
    incomplete/stalled/stuck/capped/deadline reason. This is the signal the loop uses to spawn a GAP-FILL
    round (re-test the gaps) instead of declaring the run done — a QA coordinator does not accept a story
    it never finished testing."""
    if any(not c.get("covered") for c in (sr.get("coverage") or [])):
        return True
    stop = (sr.get("stop_reason") or "").lower()
    return any(k in stop for k in ("incomplete", "stalled", "stuck", "cap", "deadline"))


def _bug_report(bug: dict, story: dict, n: int, fixed: bool = False) -> dict:
    """One explorer bug -> the report's bug contract (with screenshot + blocking + fixed)."""
    return {
        "id": f"BUG-{n}",
        "story": story.get("id", story.get("title", "")),
        "title": (bug.get("bug") or "unspecified defect")[:100],
        "detail": bug.get("bug") or "",
        "expected": bug.get("expected", ""),
        "actual": (f"at {bug.get('url')}: " if bug.get("url") else "")
                  + f"after action {_fmt_action(bug.get('action'))}",
        "screenshot": bug.get("shot"),
        "blocking": bool(bug.get("blocking")),
        "fixed": bool(fixed),
        "severity": bug.get("severity", "medium"),
    }


# ───────────────────────────────────────────────────────────────────────────────────────────────────
# one round — explore EVERY story fresh, returning (story_reports, bug_reports, first_blocking_bug).
# ───────────────────────────────────────────────────────────────────────────────────────────────────
def _run_round(target_url, vision, token, org, stories, *, max_steps, explorer_cls,
               bug_seq, on_event=None, artifact_dir=None, pulse_work_id=None):
    """Drive all stories once against the live app. Stops the round EARLY on the first BLOCKING bug
    (the app will be fixed + reset + re-run from scratch, so finishing the round is wasted work)."""
    story_reports, bug_reports, first_blocking = [], [], None
    for story in stories:
        collected = []
        ex = None
        try:
            try:
                ex = explorer_cls(target_url, vision, token=token, org=org, artifact_dir=artifact_dir)
            except TypeError:
                ex = explorer_cls(target_url, vision, token=token, org=org)
            if ex is not None and pulse_work_id:
                setattr(ex, "pulse_work_id", pulse_work_id)   # per-step heartbeat during explore
        except Exception as e:                            # a browser/bridge failure is itself a blocking bug
            bug = {"bug": f"could not launch the explorer/browser: {e}", "expected": "app is reachable",
                   "blocking": True, "severity": "critical", "url": target_url, "action": {"cmd": "goto"}}
            collected = [bug]
            records = []
        else:
            try:
                records = ex.explore(story, max_steps=max_steps, on_bug=collected.append)
            except Exception as e:
                records = []
                collected.append({"bug": f"explorer crashed mid-story: {e}", "expected": "story completes",
                                  "blocking": True, "severity": "critical", "url": target_url,
                                  "action": {"cmd": "noop"}})
            finally:
                try:
                    ex.close()
                except Exception:
                    pass

        sr = _story_report(story, records, collected)
        # carry the coverage ledger (tested-vs-yet-to-test), why the story stopped, and its scrollable clip.
        if ex is not None:
            sr["coverage"] = getattr(ex, "coverage", None)
            sr["stop_reason"] = getattr(ex, "stop_reason", None)
            sr["video"] = str(getattr(ex, "video_mp4", None) or "") or None
        story_reports.append(sr)
        for b in collected:
            bug_seq[0] += 1
            br = _bug_report(b, story, bug_seq[0])
            bug_reports.append(br)
            if b.get("blocking") and first_blocking is None:
                first_blocking = {"explorer_bug": b, "report_bug": br, "story": story}
        if on_event:
            on_event("story_done", {"story": story.get("id"), "status": story_reports[-1]["status"],
                                    "bugs": len(collected)})
        if first_blocking is not None:                    # stop early — fix + reset + re-run from scratch
            break
    return story_reports, bug_reports, first_blocking


# ───────────────────────────────────────────────────────────────────────────────────────────────────
# the top-level autonomous loop.
# ───────────────────────────────────────────────────────────────────────────────────────────────────
def qa_run(target_url, vision, token, org, product_summary, *,
           product="app", repo=None, restart_cmd=None, health_url=None,
           stories=None, max_rounds=MAX_ROUNDS, max_steps=MAX_STEPS,
           on_event=None, out_dir=None, file_findings=True):
    """Autonomously QA a product from its VISION until it is bug-free (or a round cap trips), then report.

      target_url       — the running app to exercise (the explorer drives a real browser against it).
      vision           — the ORIGINAL product vision; held in context so every AI call judges intent.
      token, org       — tenant auth seeded into the browser session (localStorage aos_tenant/aos_org).
      product_summary  — what actually exists to test (feeds the AI story enumeration).
      repo             — the app's source repo (where dev-fix agents work); defaults to factory.PRODUCTS.
      restart_cmd      — argv/str to relaunch the app after a fix (the RESET). None -> no restart.
      health_url       — polled after a restart until healthy (readiness gate).
      stories          — optional pre-supplied story list (skips AI generation; used by the smoke test).
      max_rounds       — bounded fix-and-re-run cycles (the "hours OK" cap).
      file_findings    — file EVERY still-open bug through findings.py at the end (the ship bar).
                         findings.verify() re-runs set this False so a verification run can never
                         recursively file new findings.

    Returns qa_report.build_report(run)'s dict (md/json paths + grounded verdict). EVERY decision inside
    — story enumeration, each explore step, each fix plan/judge, the report narrative — is a real
    factory.agent call (resilient: overload retry + Codex failover)."""
    started = time.time()
    repo = repo or str(factory.PRODUCTS)
    explorer_cls = qa_explorer.Explorer                   # module attr -> patchable in the offline selftest
    evidence_dir = Path(out_dir) if out_dir else artifacts.run_dir(product, started)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    events_path = evidence_dir / "events.jsonl"

    # LIVE PULSE: register this run so the observability plane + watchdog can see it's alive and what phase
    # it's in — every explore step beats, and the finalize phases below beat too (they used to go dark).
    pulse_work_id = f"qa:{evidence_dir.name}"
    pulse.start(pulse_work_id, "qa-run", label=f"QA: {product}", tenant_id=(org or None), stage="planning",
                expected_cadence_s=int(os.environ.get("AOS_QA_PULSE_CADENCE_S", "150")),
                meta={"evidence_dir": str(evidence_dir), "target_url": target_url})

    def emit(kind, data=None):
        payload = {"ts": time.time(), "kind": kind, "data": data or {}}
        try:
            with events_path.open("a") as f:
                f.write(json.dumps(payload, default=str) + "\n")
        except Exception:
            pass
        if on_event:
            on_event(kind, data or {})

    try:
        (evidence_dir / "run-input.json").write_text(json.dumps({
            "product": product,
            "target_url": target_url,
            "vision": vision,
            "org": org,
            "product_summary": product_summary,
            "repo": repo,
            "restart_cmd": restart_cmd,
            "health_url": health_url,
            "max_rounds": max_rounds,
            "max_steps": max_steps,
            "started_at": started,
        }, indent=2, default=str))
    except Exception:
        pass

    # AI DECISION #0: enumerate the exhaustive user-story coverage set from the vision (unless supplied).
    # RETRY on an empty result (a prose-instead-of-JSON reply, a failed call): one flaky completion must
    # not become a 0-story round + NO VERDICT. Still fail-closed after the attempts — an unverifiable
    # build does not ship, but only after the enumeration genuinely could not be obtained.
    if stories is None:
        for attempt in range(1, max(1, STORY_GEN_ATTEMPTS) + 1):
            stories = story_gen.generate_stories(vision, product_summary)
            if stories:
                break
            print(f"[qa_run] story enumeration returned nothing (attempt {attempt}/{STORY_GEN_ATTEMPTS})"
                  + (" — retrying" if attempt < STORY_GEN_ATTEMPTS else " — giving up (fail-closed)"),
                  flush=True)
    if MAX_STORIES > 0 and stories and len(stories) > MAX_STORIES:
        print(f"[qa_run] AOS_QA_MAX_STORIES={MAX_STORIES}: exercising the first {MAX_STORIES} of "
              f"{len(stories)} enumerated stories", flush=True)
        stories = stories[:MAX_STORIES]
    try:
        (evidence_dir / "stories.json").write_text(json.dumps(stories or [], indent=2, default=str))
    except Exception:
        pass
    emit("stories", {"count": len(stories), "evidence_dir": str(evidence_dir)})

    bug_seq = [0]
    fixed_history = []                                    # bugs fixed across earlier rounds (shown in report)
    last_story_reports, last_bug_reports = [], []
    rounds_ran = 0
    clean = False
    # The QA-coordinator loop. Two ways a round is NOT done: a BLOCKING bug (fix it, re-test everything) or
    # INCOMPLETE COVERAGE (untested aspects remain -> a GAP-FILL round re-tests just those stories). It only
    # declares the run clean when there is no blocking bug AND every story's coverage is complete. Liberal by
    # design: it keeps going while gaps remain and progress is being made; max_rounds is only a runaway cap,
    # and an honest "gap-stalled" stop fires if a gap-fill round covers nothing new (never an infinite loop).
    report_by_key = {}                                   # story-key -> latest report, accumulated across rounds
    acc_bugs = []                                        # bugs across gap-fill rounds (reset when code is fixed)
    active = list(stories)                               # stories THIS round exercises (shrinks to the gaps)
    prev_covered = -1                                    # total covered aspects last round (detects no-progress)
    for rnd in range(max_rounds):
        rounds_ran = rnd + 1
        gap = rnd > 0 and len(active) < len(stories)
        emit("round_start", {"round": rounds_ran, "stories": len(active), "mode": "gap-fill" if gap else "explore"})
        pulse.beat(pulse_work_id, stage=f"round {rounds_ran}/{max_rounds} ({'gap-fill' if gap else 'explore'})",
                   progress=f"{'filling gaps in' if gap else 'exploring'} {len(active)} stories")
        story_reports, bug_reports, blocking = _run_round(
            target_url, vision, token, org, active,
            max_steps=max_steps, explorer_cls=explorer_cls, bug_seq=bug_seq, on_event=emit,
            artifact_dir=evidence_dir, pulse_work_id=pulse_work_id)
        for sr in story_reports:                          # accumulate the latest report per story across rounds
            report_by_key[_skey(sr)] = sr
        acc_bugs.extend(bug_reports)                       # keep bugs from complete stories across gap-fill rounds
        last_story_reports, last_bug_reports = list(report_by_key.values()), list(acc_bugs)

        # BLOCKING bug -> AI dev-fix loop (plans #agents -> spawns them -> AI judges fixed), then re-test ALL.
        if blocking is not None:
            if rounds_ran >= max_rounds:                  # cap hit — do NOT start a fix we can't verify
                emit("cap_reached", {"round": rounds_ran})
                break
            emit("fixing", {"round": rounds_ran, "bug": blocking["report_bug"]["title"]})
            code_context = {"repo": repo, "target_url": target_url,
                            "story": blocking["story"], "summary": product_summary}
            try:
                # target_url + the failing story are MANDATORY: fix_bug always restarts (tracked PID)
                # and re-explores the story, so its judge sees a real diff + a fresh observation.
                fix = dev_loop.fix_bug(blocking["explorer_bug"], code_context, vision, repo=repo,
                                       target_url=target_url, stories=[blocking["story"]],
                                       restart_cmd=restart_cmd, health_url=health_url,
                                       token=token, org=org)
            except Exception as e:
                fix = {"fixed": False, "error": str(e)}
            blocking["report_bug"]["fixed"] = bool(fix.get("fixed"))
            fixed_history.append(blocking["report_bug"])
            try:
                (evidence_dir / f"fix-round-{rounds_ran}.json").write_text(json.dumps(fix, indent=2, default=str))
            except Exception:
                pass
            emit("fixed", {"round": rounds_ran, "fixed": bool(fix.get("fixed")), "files": fix.get("files")})
            # RESET: restart so the NEXT round observes a FRESH process serving the fixed code, and re-test
            # EVERY story from scratch — a fix can regress anything, so coverage is rebuilt clean.
            if restart_cmd:
                try:
                    restart = dev_loop.restart_target(restart_cmd, health_url=health_url,
                                                      cwd=repo if isinstance(repo, str) else None)
                except Exception as e:
                    restart = {"restarted": False, "healthy": False, "detail": str(e)}
                emit("reset", {"round": rounds_ran, "healthy": restart.get("healthy")})
            active, report_by_key, prev_covered, acc_bugs = list(stories), {}, -1, []
            continue

        # NO blocking bug -> is COVERAGE complete? (the check the old loop skipped). A story is not accepted
        # while aspects a real user would try remain untested.
        incomplete = [sr for sr in report_by_key.values() if _report_incomplete(sr)]
        total_covered = sum(1 for sr in report_by_key.values()
                            for c in (sr.get("coverage") or []) if c.get("covered"))
        if not incomplete:
            clean = True
            emit("clean_round", {"round": rounds_ran, "coverage": "complete"})
            break
        if rounds_ran >= max_rounds:
            emit("cap_reached", {"round": rounds_ran, "incomplete": len(incomplete)})
            break
        if total_covered <= prev_covered:                 # a gap-fill round that covered NOTHING new -> honest stop
            emit("gap_stalled", {"round": rounds_ran, "incomplete": len(incomplete)})
            break
        prev_covered = total_covered
        # GAP-FILL: re-run ONLY the still-incomplete stories — another crack at the untested aspects with a
        # fresh browser. The still-open aspects also become governed findings at the end, so nothing is lost.
        inc_keys = {_skey(sr) for sr in incomplete}
        active = [s for s in stories if _skey(s) in inc_keys] or list(stories)
        untested = sum(len([c for c in (sr.get("coverage") or []) if not c.get("covered")]) for sr in incomplete)
        emit("gap_fill", {"round": rounds_ran, "stories": len(active), "untested_aspects": untested})

    # Assemble the run for the report: accumulated per-story reports + open bugs + every earlier fix.
    seen = {b["id"] for b in last_bug_reports}
    all_bugs = list(last_bug_reports) + [b for b in fixed_history if b["id"] not in seen]
    run = {
        "product": product,
        "vision": vision,
        "url": target_url,
        "started_at": started,
        "finished_at": time.time(),
        "stories": last_story_reports,
        "bugs": all_bugs,
        "rounds": rounds_ran,
        "clean": clean,
    }
    try:
        (evidence_dir / "run-final.json").write_text(json.dumps(run, indent=2, default=str))
    except Exception:
        pass
    pulse.beat(pulse_work_id, stage="report", progress="writing verdict + narrative")   # was the DARK phase
    report = qa_report.build_report(run, out_dir=evidence_dir)
    report["rounds"] = rounds_ran
    report["clean"] = clean
    # SESSION RECORDING: stitch every per-story .mp4 clip (written by each Explorer.close) into ONE
    # scrollable video of the whole QA run — the CEO/dev can watch QA drive the product end to end, like
    # reviewing a real QA team's screen recording. Fail-open: absent ffmpeg/clips just leaves it unset.
    try:
        clips = sorted((evidence_dir / "videos").glob("*.mp4")) if (evidence_dir / "videos").exists() else []
        clips = [c for c in clips if c.name != "qa-session.mp4"]
        report["session_video"] = str(artifacts.stitch_mp4s(clips, evidence_dir / "qa-session.mp4") or "") or None
        report["story_videos"] = [str(c) for c in clips]
    except Exception:
        report["session_video"] = None
    emit("session_video", {"path": report.get("session_video"), "clips": len(report.get("story_videos") or [])})
    # TESTED-vs-YET-TO-BE-TESTED ledger — the hand-off doc a human/dev/coordinator reads to see EXACTLY what
    # QA covered, what it did NOT, and WHY each story stopped. This is the coverage audit, not a step count.
    try:
        cov_json, lines = [], [f"# QA Coverage — {product}", "",
                               f"Rounds: {rounds_ran} · Verdict: {report.get('verdict', '')}", ""]
        for sr in last_story_reports:
            cov = sr.get("coverage") or []
            tested = [c["aspect"] for c in cov if c.get("covered")]
            untested = [c["aspect"] for c in cov if not c.get("covered")]
            name = sr.get("title") or sr.get("story") or sr.get("id") or "story"
            cov_json.append({"story": name, "stop_reason": sr.get("stop_reason"),
                             "tested": tested, "yet_to_test": untested})
            lines.append(f"## {name}")
            lines.append(f"- stop reason: **{sr.get('stop_reason', '?')}**")
            lines.append(f"- tested ({len(tested)}):")
            lines += ([f"  - [x] {a}" for a in tested] or ["  - (none)"])
            lines.append(f"- yet to test ({len(untested)}):")
            lines += ([f"  - [ ] {a}" for a in untested] or ["  - (none — fully covered)"])
            lines.append("")
        (evidence_dir / "coverage.json").write_text(json.dumps(cov_json, indent=2, default=str))
        (evidence_dir / "COVERAGE.md").write_text("\n".join(lines))
        report["coverage_doc"], report["coverage"] = str(evidence_dir / "COVERAGE.md"), cov_json
    except Exception:
        report["coverage_doc"] = None
    # AUDITOR SIGN-OFF GATE: a skeptical work-execution auditor reviews HOW the run actually did its job —
    # from the footage + per-step decisions, NOT the self-report — and a run it REJECTS cannot be reported
    # 'passed' (so it can't clear the LAUNCH gate). This is the QA coordinator scrutinising its own work
    # before hand-off. Runs BEFORE _persist_run/write_verdict so the downgrade flows into the gate artifact.
    # Fail-open: an auditor error never blocks the pipeline (degrades to the coverage-grounded verdict).
    report["audit"] = None
    if file_findings and os.environ.get("AOS_QA_AUDIT_GATE", "1").lower() not in ("0", "false", "no"):
        pulse.beat(pulse_work_id, stage="audit", progress="skeptical work-execution audit")
        try:
            import review
            av = review.review(str(evidence_dir), write=True)
            report["audit"] = av
            if av.get("passed_audit") is False:               # the skeptic rejected it -> honestly not a pass
                report["passed"] = False
                report["verdict"] = (f"AUDIT REJECTED (score {av.get('score', '?')}/10) — "
                                     f"{(av.get('summary') or '')[:180]}")
                emit("audit_rejected", {"score": av.get("score"),
                                        "skipped_flows": len(av.get("skipped_flows") or []),
                                        "unbacked_claims": len(av.get("unbacked_claims") or [])})
        except Exception as e:
            report["audit_error"] = str(e)
    # DURABLE HISTORY (REBUILD-PLAN C1): every run is a qa_runs row in Postgres — the evidence chain
    # survives reboots. A persistence failure is surfaced on the report, never swallowed silently.
    try:
        report["qa_run_id"] = _persist_run(report, run)
        report["persist_error"] = None
    except Exception as e:
        report["qa_run_id"], report["persist_error"] = None, str(e)
    # THE LAUNCH ARTIFACT (REBUILD-PLAN C1): persist the machine-readable verdict into the product's
    # own docs/ so gate_check's REVIEW/LAUNCH gates bind to real QA evidence — never prose, never /tmp.
    report["verdict_json"] = write_verdict(repo, report, product=product, target_url=target_url,
                                           qa_run_id=report["qa_run_id"])
    emit("persisted", {"qa_run_id": report["qa_run_id"],
                       "verdict_json": report["verdict_json"],
                       "evidence_dir": str(evidence_dir)})
    # THE SHIP BAR (REBUILD-PLAN C1): ANY bug still open — blocking or not — becomes a governed,
    # AI-routed, SLA-tracked finding with its originating story stored for gated re-verification.
    pulse.beat(pulse_work_id, stage="filing", progress="routing open findings")   # also a former dark phase
    report["findings_filed"] = (_file_open_bugs(
        report, run, stories, {"target_url": target_url, "vision": vision, "token": token,
                               "org": org, "summary": product_summary, "max_steps": max_steps})
        if file_findings else [])
    if report["findings_filed"]:
        emit("findings_filed", {"count": len(report["findings_filed"]),
                                "ids": [f.get("finding_id") for f in report["findings_filed"]]})
    # AUDIT REJECTION -> governed dev work: the auditor's found gaps become owned, SLA-tracked findings,
    # so "the skeptic rejected it" doesn't just block the gate — it hands dev/QA a concrete, tracked list.
    report["audit_findings"] = []
    if file_findings and (report.get("audit") or {}).get("passed_audit") is False:
        report["audit_findings"] = _file_audit_gaps(report["audit"], product, report, evidence_dir)
        if report["audit_findings"]:
            emit("audit_findings_filed", {"count": len(report["audit_findings"])})
    report["evidence_dir"] = str(evidence_dir)
    try:
        (evidence_dir / "manifest.json").write_text(json.dumps({
            "product": product,
            "qa_run_id": report.get("qa_run_id"),
            "verdict": report.get("verdict"),
            "passed": report.get("passed"),
            "rounds": report.get("rounds"),
            "report_md": report.get("md"),
            "report_json": report.get("json"),
            "verdict_json": report.get("verdict_json"),
            "events_jsonl": str(events_path),
            "screenshots_dir": str(evidence_dir / "screenshots"),
        }, indent=2, default=str))
    except Exception:
        pass
    emit("report", {"md": report["md"], "verdict": report["verdict"],
                    "verdict_json": report["verdict_json"],
                    "evidence_dir": str(evidence_dir)})
    # PULSE TERMINAL: a clean run is 'done'; anything else (open bugs, incomplete coverage, cap hit) is
    # 'incomplete' — honest, and it leaves the live view so the observer stops watching it.
    pulse.finish(pulse_work_id, status=("done" if clean and not report.get("findings_filed") else "incomplete"),
                 result={"verdict": report.get("verdict"), "rounds": rounds_ran,
                         "open_findings": len(report.get("findings_filed") or [])})
    return report


def write_verdict(repo, report, *, product="app", target_url=None, producer="qa_run", qa_run_id=None):
    """Write the machine-readable QA VERDICT — THE LAUNCH ARTIFACT — into the product's own docs/
    (docs/QA-VERDICT.json, never /tmp). gate_check's REVIEW/LAUNCH gates consume exactly this JSON
    (passed==true, blocking_open==0, stories>0), so a product ships only on a grounded verdict from an
    actual QA execution — the builder's own tests can never satisfy the gate. The artifact is
    hash-linked to the full report JSON (report_sha256) and to its durable qa_runs row (qa_run_id),
    so the verdict can always be traced back to the exact run evidence that produced it.

    `report` is qa_report.build_report()'s return dict (or any dict carrying the same keys). Returns
    the written path, or None when there is no real product repo to write into (repo unset, missing,
    or the whole products/ dir — the default `repo` of ad-hoc runs like the console smoke)."""
    if not repo:
        return None
    rp = Path(repo)
    try:
        if not rp.exists() or rp.resolve() == Path(str(factory.PRODUCTS)).resolve():
            return None
    except Exception:
        return None
    js = report.get("json")
    try:
        sha = hashlib.sha256(Path(js).read_bytes()).hexdigest() if js and Path(js).exists() else None
    except Exception:
        sha = None
    doc = {
        "schema": "aos.qa.verdict/1",
        "product": product,
        "passed": bool(report.get("passed")),
        "stories": int(report.get("total_stories") or 0),
        "blocking_open": int(report.get("blocking_open") or 0),
        "open_bugs": int(report.get("open_bugs") or 0),
        "verdict": report.get("verdict"),
        "summary": report.get("summary"),
        "report_md": report.get("md"),
        "report_json": js,
        "report_sha256": sha,
        "qa_run_id": qa_run_id,
        "rounds": report.get("rounds"),
        "target_url": target_url,
        "producer": producer,
        "generated_at": time.time(),
    }
    out = rp / "docs" / "QA-VERDICT.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2, default=str))
    return str(out)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# offline selftest — stubs EVERY AI call + the browser; asserts the full loop wiring, no network/spend.
# ═══════════════════════════════════════════════════════════════════════════════════════════════════
def _selftest():
    import tempfile
    import types

    os.environ["AOS_QA_AUDIT_GATE"] = "0"    # offline wiring test — no real auditor (Opus) call
    events = []
    on_event = lambda k, d: events.append((k, d))

    # 1) story_gen -> two canned stories (skip real AI enumeration).
    real_gen = story_gen.generate_stories
    story_gen.generate_stories = lambda vision, summary, **k: [
        {"id": "US-1", "title": "Sign in", "expected_outcome": "user reaches the dashboard",
         "steps": ["open app", "click sign in"]},
        {"id": "US-2", "title": "Open Assistant", "expected_outcome": "assistant panel opens",
         "steps": ["click Assistant"]},
    ]

    # 2) a fake Explorer: round 1 -> US-1 hits a BLOCKING bug (loop must fix+reset+re-run); round 2 -> clean.
    state = {"round": 0}

    class FakeExplorer:
        def __init__(self, url, vision, token=None, org="0", **k):
            assert token == "TOK" and org == "9", (token, org)   # tenant auth threaded through
            assert "sign in" in vision.lower() or vision, vision
            self.bugs = []
            self.closed = False

        def explore(self, story, max_steps=25, on_bug=None):
            assert max_steps == 5, max_steps                      # the caller's cap is honored
            # emit ONE real step-record so the report gets expected-vs-actual rows
            rec = {"step": 0, "action": {"cmd": "click", "idx": 0}, "expected": "panel opens",
                   "actual": {"url": "http://app/x", "console_errors": []},
                   "verdict": {"matches_expected": True, "bug": None, "severity": "none", "blocking": False}}
            if state["round"] == 0 and story["id"] == "US-1":
                bug = {"step": 0, "story": "Sign in", "url": "http://app/login",
                       "action": {"cmd": "click", "idx": 0},
                       "expected": "dashboard loads", "bug": "sign-in button does nothing",
                       "severity": "high", "blocking": True, "shot": "/tmp/shot.png"}
                rec["verdict"] = {"matches_expected": False, "bug": bug["bug"],
                                  "severity": "high", "blocking": True}
                if on_bug:
                    on_bug(bug)
                self.bugs.append(bug)
            if story["id"] == "US-3":                             # a NON-blocking bug, never fixed in-run
                bug = {"step": 0, "story": "Rename org", "url": "http://app/org",
                       "action": {"cmd": "click", "idx": 1}, "expected": "the new name shows",
                       "bug": "rename saves but the UI still shows the old name",
                       "severity": "medium", "blocking": False, "shot": "/tmp/shot3.png"}
                rec["verdict"] = {"matches_expected": False, "bug": bug["bug"],
                                  "severity": "medium", "blocking": False}
                if on_bug:
                    on_bug(bug)
                self.bugs.append(bug)
            return [rec]

        def close(self):
            self.closed = True

    real_explorer = qa_explorer.Explorer
    qa_explorer.Explorer = FakeExplorer

    # 3) dev_loop.fix_bug + restart_target — record the calls, report fixed.
    fix_calls, restart_calls = [], []

    def fake_fix_bug(bug, code_context, vision, **k):
        fix_calls.append((bug.get("bug"), code_context.get("repo")))
        assert bug.get("blocking") is True                        # only BLOCKING bugs drive a fix
        return {"fixed": True, "files": ["src/login.js"], "attempts": 1}

    def fake_restart(cmd, **k):
        restart_calls.append(cmd)
        state["round"] += 1                                       # the RESET flips us to the (fixed) next round
        return {"restarted": True, "healthy": True, "pid": 123}

    real_fix, real_restart = dev_loop.fix_bug, dev_loop.restart_target
    dev_loop.fix_bug, dev_loop.restart_target = fake_fix_bug, fake_restart

    # 4) stub the ONE AI call inside qa_report (the narrative) so the report is deterministic + offline.
    real_agent = factory.agent
    factory.agent = lambda role, repo, prompt, **k: {
        "rc": 0, "out": "All stories passed after one fix.", "out_full": "All stories passed after one fix."}

    ok = False
    tmp = Path(tempfile.mkdtemp())
    fake_repo = tmp / "repo"                                      # a real product repo -> verdict lands in docs/
    fake_repo.mkdir()
    suf = os.urandom(3).hex()
    owner_id = f"builder@qa-selftest-{suf}"
    prod_b = f"demo-{suf}"
    run_ids = []                                                  # qa_runs rows to clean up
    try:
        report = qa_run("http://app.test", "Let users sign in and open the Assistant.",
                        token="TOK", org="9", product_summary="A web app with sign-in and an Assistant.",
                        product="demo", repo=str(fake_repo),
                        restart_cmd=["python", "app.py"], health_url=None,
                        max_rounds=3, max_steps=5, on_event=on_event, out_dir=tmp)

        # the loop ran TWO rounds: round 1 found the blocking bug, fixed+reset, round 2 was clean.
        assert report["rounds"] == 2, f"expected 2 rounds, got {report['rounds']}"
        assert report["clean"] is True, "second round should be clean"
        assert len(fix_calls) == 1 and fix_calls[0][0] == "sign-in button does nothing", fix_calls
        assert restart_calls == [["python", "app.py"]], restart_calls   # RESET happened between rounds

        # the report was written to disk with a passing verdict (final round clean, fixed bug shown).
        md, js = Path(report["md"]), Path(report["json"])
        assert md.exists() and js.exists(), "report files not written"
        doc = json.loads(js.read_text())
        assert report["passed"] is True, f"final clean run must pass: {report['verdict']}"
        assert report["total_stories"] == 2, report
        # the earlier BLOCKING bug is preserved in the report, marked FIXED.
        bug_items = doc["bugs"]["items"]
        assert any(b["fixed"] and "sign-in" in b["title"] for b in bug_items), bug_items
        assert report["blocking_open"] == 0, "no blocking bug should remain open"
        # expected-vs-actual rows made it into the report (the core of this QA system).
        text = md.read_text()
        assert "US-1" in text and "US-2" in text and "panel opens" in text, "story steps missing"

        # THE LAUNCH ARTIFACT: qa_run persisted the machine verdict into the product's docs/ and the
        # gates' three bindings (passed / blocking_open / stories) carry the grounded facts.
        vpath = fake_repo / "docs" / "QA-VERDICT.json"
        assert report["verdict_json"] == str(vpath) and vpath.exists(), \
            f"verdict artifact not written to the product docs/: {report.get('verdict_json')}"
        v = json.loads(vpath.read_text())
        assert (v["passed"] is True and v["blocking_open"] == 0 and v["stories"] == 2
                and v["producer"] == "qa_run" and v["report_md"] == report["md"]), v
        # ad-hoc runs (repo defaulting to the whole products/ dir) must NOT write a verdict anywhere.
        assert write_verdict(str(factory.PRODUCTS), report, product="demo") is None, \
            "write_verdict must refuse the products/ dir default (no fake LAUNCH artifact)"

        # DURABLE HISTORY: the run landed as a qa_runs row (product, rounds, verdict json, report md,
        # ts) and the verdict artifact is hash-linked to the run's evidence (qa_run_id + report sha).
        assert isinstance(report["qa_run_id"], int) and report["persist_error"] is None, report
        run_ids.append(report["qa_run_id"])
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT product, rounds, passed, clean, verdict, report_md, ts
                           FROM qa_runs WHERE id=%s""", (report["qa_run_id"],))
            rp, rr, rpass, rclean, rverdict, rmd, rts = cur.fetchone()
        assert (rp == "demo" and rr == 2 and rpass is True and rclean is True
                and rmd == report["md"] and rts is not None), (rp, rr, rpass, rclean, rmd)
        assert rverdict["verdict"] == report["verdict"] and rverdict["blocking_open"] == 0, rverdict
        import hashlib as _h
        assert v["qa_run_id"] == report["qa_run_id"], v
        assert v["report_sha256"] == _h.sha256(js.read_bytes()).hexdigest(), "verdict not hash-linked"
        # a fully-fixed clean run files NO findings (nothing left open).
        assert report["findings_filed"] == [], report["findings_filed"]

        # the event stream shows the true state machine: stories -> round -> fixing -> fixed -> reset ->
        # round -> clean_round -> persisted -> report.
        kinds = [k for k, _ in events]
        for expect in ("stories", "round_start", "fixing", "fixed", "reset", "clean_round",
                       "persisted", "report"):
            assert expect in kinds, f"missing event {expect}: {kinds}"
        assert kinds.count("round_start") == 2, kinds

        # ── scenario B: THE SHIP BAR — a NON-blocking bug left open at the end must (a) never enter
        # the fix loop, (b) fail the grounded verdict, and (c) be FILED through findings.py with a
        # routed owner + SLA + the originating story as its gated re-verification recipe.
        import directory
        directory.register(owner_id, "builder", product=prod_b, task="idle")
        stories_b = [
            {"id": "US-1", "title": "Sign in", "expected_outcome": "user reaches the dashboard"},
            {"id": "US-3", "title": "Rename org", "expected_outcome": "the new org name shows"},
        ]
        events_b = []
        report_b = qa_run("http://app.test", "Let users sign in and open the Assistant.",
                          token="TOK", org="9", product_summary="A web app with sign-in and an Assistant.",
                          product=prod_b, repo=str(fake_repo), restart_cmd=None, stories=stories_b,
                          max_rounds=2, max_steps=5,
                          on_event=lambda k, d: events_b.append((k, d)), out_dir=tmp)
        run_ids.append(report_b["qa_run_id"])
        assert report_b["rounds"] == 1 and report_b["clean"] is True, report_b["rounds"]
        assert len(fix_calls) == 1, "a NON-blocking bug must never enter the fix loop"
        assert (report_b["passed"] is False and report_b["blocking_open"] == 0
                and report_b["open_bugs"] == 1), report_b["verdict"]
        filed = report_b["findings_filed"]
        assert len(filed) == 1 and filed[0].get("finding_id"), filed
        assert filed[0]["status"] == "routed" and filed[0]["owner"] == owner_id, filed
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT verify_check, severity FROM findings WHERE id=%s",
                        (filed[0]["finding_id"],))
            vc, sev = cur.fetchone()
        vc = vc if isinstance(vc, dict) else json.loads(vc or "{}")
        assert (vc.get("kind") == "qa_story" and (vc.get("story") or {}).get("id") == "US-3"
                and vc.get("target_url") == "http://app.test" and sev == "medium"), (vc, sev)
        v2 = json.loads(vpath.read_text())        # the artifact reflects the FAILED (open-bug) run
        assert v2["passed"] is False and v2["open_bugs"] == 1 and v2["qa_run_id"] == report_b["qa_run_id"], v2
        kinds_b = [k for k, _ in events_b]
        assert "persisted" in kinds_b and "findings_filed" in kinds_b, kinds_b
        # file_findings=False (findings.verify()'s re-run) must NOT recursively file more findings.
        report_c = qa_run("http://app.test", "Let users sign in and open the Assistant.",
                          token="TOK", org="9", product_summary="A web app with sign-in and an Assistant.",
                          product=prod_b, repo=str(fake_repo), restart_cmd=None,
                          stories=[stories_b[1]], max_rounds=1, max_steps=5, out_dir=tmp,
                          file_findings=False)
        run_ids.append(report_c["qa_run_id"])
        assert report_c["findings_filed"] == [], "verification re-runs must not file findings"
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM findings WHERE source=%s", (f"qa:{prod_b}",))
            assert cur.fetchone()[0] == 1, "verification re-run filed a duplicate finding"

        ok = True
        print("qa_run selftest: PASS "
              f"(rounds={report['rounds']}, fixes={len(fix_calls)}, resets={len(restart_calls)}, "
              f"verdict={report['verdict']!r}, qa_run_ids={run_ids}, "
              f"open-bug finding routed to {filed[0]['owner']})")
    except AssertionError as e:
        print(f"qa_run selftest: FAIL — {e}")
    finally:
        story_gen.generate_stories = real_gen
        qa_explorer.Explorer = real_explorer
        dev_loop.fix_bug, dev_loop.restart_target = real_fix, real_restart
        factory.agent = real_agent
        try:                                                     # scrub every DB row the selftest created
            with psycopg.connect(DB) as c, c.cursor() as cur:
                if run_ids:
                    cur.execute("DELETE FROM qa_runs WHERE id = ANY(%s)", (run_ids,))
                cur.execute("DELETE FROM findings WHERE source=%s", (f"qa:{prod_b}",))
                cur.execute("DELETE FROM tasks WHERE assignee=%s", (owner_id,))
                cur.execute("DELETE FROM directory WHERE agent_id=%s", (owner_id,))
                cur.execute("DELETE FROM task_board WHERE source=%s", (f"review:qa:{prod_b}",))
                c.commit()
        except Exception as e:
            print(f"qa_run selftest: cleanup warning — {e}")
    sys.exit(0 if ok else 1)


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# BOUNDED smoke — PROVE the loop end-to-end against the LIVE console with ONE tiny REAL story.
#   * start the console on :8099 (setsid) and wait for health
#   * seed a real tenant + org + consent + provider (like scripts/console_actions_e2e.sh)
#   * run ONE story ('sign in and open the Assistant') through the REAL Explorer, max_steps=3, REAL AI
#   * confirm observe->decide->act->observe->evaluate->report ran without crashing + a report was written
# This makes REAL factory.agent calls (needs the claude CLI authenticated). Prints PASS/FAIL + report path.
# ═══════════════════════════════════════════════════════════════════════════════════════════════════
def _console_up(port=8099) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4) as r:
            return r.status == 200 and b"console" in r.read()
    except Exception:
        return False


def _ensure_console(port=8099) -> bool:
    if _console_up(port):
        return True
    import subprocess
    root = SCRIPTS.parent
    py = str(root / ".venv" / "bin" / "python")
    subprocess.Popen(["setsid", py, str(SCRIPTS / "console.py"), "serve", str(port)],
                     cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        if _console_up(port):
            return True
        time.sleep(0.5)
    return False


def _seed_tenant(port=8099) -> tuple:
    """billing.signup + tenantproviders.connect + /api/orgs/new + /api/settings/consent -> (token, org)."""
    import billing
    import tenantproviders
    r = billing.signup("QASmoke", "free")
    tok, tid = r["api_token"], r["tenant_id"]
    try:
        tenantproviders.connect(tid, "anthropic", "subscription")
    except Exception:
        pass

    def _post(path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
            headers={"X-Tenant-Token": tok, "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()

    org = "0"
    try:
        out = _post("/api/orgs/new", {"name": "QASmoke Co"})
        import re
        m = re.search(r'"(?:org_id|id|org)"\s*:\s*"?(\d+)"?', out) or re.search(r"(\d+)", out)
        if m:
            org = m.group(1)
    except Exception:
        pass
    try:
        _post("/api/settings/consent", {"accept": True})
    except Exception:
        pass
    return tok, org, tid


def _smoke():
    port = 8099
    url = f"http://127.0.0.1:{port}"
    vision = ("A team console where a signed-in user can open an AI Assistant to run and watch their "
              "product builds. Signing in must land the user in the console, and the Assistant must open.")
    summary = "A single-page web console served at / with tenant auth, an org switcher, and an Assistant panel."

    print("[smoke] ensuring console is up on :%d ..." % port, flush=True)
    if not _ensure_console(port):
        print("SMOKE: FAIL — console did not become healthy on :%d" % port)
        return 1
    print("[smoke] console healthy. seeding tenant+org+consent+provider ...", flush=True)
    try:
        token, org, tid = _seed_tenant(port)
    except Exception as e:
        print(f"SMOKE: FAIL — seeding failed: {e}")
        return 1
    print(f"[smoke] seeded tenant={tid} org={org} token={token[:8]}…", flush=True)

    story = {"id": "US-SMOKE", "title": "Sign in and open the Assistant",
             "persona": "signed-in user", "category": "happy",
             "steps": ["Open the console (already signed in via seeded token)",
                       "Locate and open the Assistant"],
             "expected_outcome": "The console loads signed-in and the Assistant panel/view opens."}

    events = []
    print("[smoke] running ONE real story through the REAL Explorer (max_steps=3, real AI calls) ...", flush=True)
    try:
        report = qa_run(url, vision, token=token, org=str(org), product_summary=summary,
                        product="console-smoke", restart_cmd=None, health_url=f"{url}/health",
                        stories=[story], max_rounds=1, max_steps=3,
                        on_event=lambda k, d: events.append((k, d)))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"SMOKE: FAIL — qa_run crashed: {e}")
        return 1

    kinds = [k for k, _ in events]
    md = report.get("md")
    wrote = bool(md and Path(md).exists())
    # the loop must have actually cycled observe->decide->act->observe->evaluate (>=1 story) and reported.
    saw_loop = "story_done" in kinds and "report" in kinds
    ok = wrote and saw_loop
    print("[smoke] events:", kinds, flush=True)
    print("[smoke] verdict:", report.get("verdict"), flush=True)
    print(f"[smoke] report: {md}", flush=True)
    if ok:
        print(f"SMOKE: PASS — observe->decide->act->observe->evaluate->report ran end-to-end. report={md}")
        return 0
    print(f"SMOKE: FAIL — wrote_report={wrote} saw_loop={saw_loop} report={md}")
    return 1


# public API
__all__ = ["qa_run", "write_verdict"]


def _main(argv):
    if not argv or argv[0] == "selftest":
        _selftest()
    elif argv[0] == "smoke":
        sys.exit(_smoke())
    else:
        sys.exit("usage: qa_run.py [selftest|smoke]")


if __name__ == "__main__":
    _main(sys.argv[1:])
