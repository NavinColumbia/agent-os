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

Design stance (owner-mandated): cost is NOT a concern — be maximally AI-driven; the loop is STATE-BASED
(observe -> decide -> act -> observe -> evaluate); and it judges EXPECTED-vs-ACTUAL against the vision.

    python qa_run.py selftest    # offline wiring check — stubs every AI call + the browser, no network
    python qa_run.py smoke       # BOUNDED end-to-end: live console + one tiny REAL story (real AI calls)

Run with the agent-os venv python.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

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

MAX_ROUNDS = int(os.environ.get("AOS_QA_MAX_ROUNDS", "6"))     # bounded fix-and-re-run cycles
MAX_STEPS = int(os.environ.get("AOS_QA_MAX_STEPS", "25"))      # per-story exploration ceiling


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
            "expected": r.get("expected", ""),
            "actual": _actual_str(r.get("actual"), v),
            "verdict": "match" if v.get("matches_expected") else "mismatch",
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
               bug_seq, on_event=None):
    """Drive all stories once against the live app. Stops the round EARLY on the first BLOCKING bug
    (the app will be fixed + reset + re-run from scratch, so finishing the round is wasted work)."""
    story_reports, bug_reports, first_blocking = [], [], None
    for story in stories:
        collected = []
        ex = None
        try:
            ex = explorer_cls(target_url, vision, token=token, org=org)
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

        story_reports.append(_story_report(story, records, collected))
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
           on_event=None, out_dir=None):
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

    Returns qa_report.build_report(run)'s dict (md/json paths + grounded verdict). EVERY decision inside
    — story enumeration, each explore step, each fix plan/judge, the report narrative — is a real
    factory.agent call (resilient: overload retry + Codex failover)."""
    started = time.time()
    repo = repo or str(factory.PRODUCTS)
    explorer_cls = qa_explorer.Explorer                   # module attr -> patchable in the offline selftest

    # AI DECISION #0: enumerate the exhaustive user-story coverage set from the vision (unless supplied).
    if stories is None:
        stories = story_gen.generate_stories(vision, product_summary)
    if on_event:
        on_event("stories", {"count": len(stories)})

    bug_seq = [0]
    fixed_history = []                                    # bugs fixed across earlier rounds (shown in report)
    last_story_reports, last_bug_reports = [], []
    rounds_ran = 0
    clean = False
    for rnd in range(max_rounds):
        rounds_ran = rnd + 1
        if on_event:
            on_event("round_start", {"round": rounds_ran, "stories": len(stories)})
        story_reports, bug_reports, blocking = _run_round(
            target_url, vision, token, org, stories,
            max_steps=max_steps, explorer_cls=explorer_cls, bug_seq=bug_seq, on_event=on_event)
        last_story_reports, last_bug_reports = story_reports, bug_reports

        if blocking is None:                              # a clean round: no blocking bug -> we're done
            clean = True
            if on_event:
                on_event("clean_round", {"round": rounds_ran})
            break

        if rounds_ran >= max_rounds:                      # cap hit — do NOT start a fix we can't verify
            if on_event:
                on_event("cap_reached", {"round": rounds_ran})
            break

        # BLOCKING bug -> AI dev-fix loop (plans #agents -> spawns them -> AI judges fixed), then RESET.
        if on_event:
            on_event("fixing", {"round": rounds_ran, "bug": blocking["report_bug"]["title"]})
        code_context = {"repo": repo, "target_url": target_url,
                        "story": blocking["story"], "summary": product_summary}
        try:
            fix = dev_loop.fix_bug(blocking["explorer_bug"], code_context, vision, repo=repo)
        except Exception as e:
            fix = {"fixed": False, "error": str(e)}
        blocking["report_bug"]["fixed"] = bool(fix.get("fixed"))
        fixed_history.append(blocking["report_bug"])
        if on_event:
            on_event("fixed", {"round": rounds_ran, "fixed": bool(fix.get("fixed")),
                               "files": fix.get("files")})

        # RESET: restart the app so the NEXT round observes a fresh process serving the fixed code.
        if restart_cmd:
            try:
                restart = dev_loop.restart_target(restart_cmd, health_url=health_url,
                                                  cwd=repo if isinstance(repo, str) else None)
            except Exception as e:
                restart = {"restarted": False, "healthy": False, "detail": str(e)}
            if on_event:
                on_event("reset", {"round": rounds_ran, "healthy": restart.get("healthy")})

    # Assemble the run for the report: the LAST round's stories + its open bugs + every earlier fix.
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
    report = qa_report.build_report(run, out_dir=out_dir)
    report["rounds"] = rounds_ran
    report["clean"] = clean
    if on_event:
        on_event("report", {"md": report["md"], "verdict": report["verdict"]})
    return report


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# offline selftest — stubs EVERY AI call + the browser; asserts the full loop wiring, no network/spend.
# ═══════════════════════════════════════════════════════════════════════════════════════════════════
def _selftest():
    import tempfile
    import types

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
    try:
        report = qa_run("http://app.test", "Let users sign in and open the Assistant.",
                        token="TOK", org="9", product_summary="A web app with sign-in and an Assistant.",
                        product="demo", restart_cmd=["python", "app.py"], health_url=None,
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

        # the event stream shows the true state machine: stories -> round -> fixing -> fixed -> reset ->
        # round -> clean_round -> report.
        kinds = [k for k, _ in events]
        for expect in ("stories", "round_start", "fixing", "fixed", "reset", "clean_round", "report"):
            assert expect in kinds, f"missing event {expect}: {kinds}"
        assert kinds.count("round_start") == 2, kinds

        ok = True
        print("qa_run selftest: PASS "
              f"(rounds={report['rounds']}, fixes={len(fix_calls)}, resets={len(restart_calls)}, "
              f"verdict={report['verdict']!r})")
    except AssertionError as e:
        print(f"qa_run selftest: FAIL — {e}")
    finally:
        story_gen.generate_stories = real_gen
        qa_explorer.Explorer = real_explorer
        dev_loop.fix_bug, dev_loop.restart_target = real_fix, real_restart
        factory.agent = real_agent
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
__all__ = ["qa_run"]


def _main(argv):
    if not argv or argv[0] == "selftest":
        _selftest()
    elif argv[0] == "smoke":
        sys.exit(_smoke())
    else:
        sys.exit("usage: qa_run.py [selftest|smoke]")


if __name__ == "__main__":
    _main(sys.argv[1:])
