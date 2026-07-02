#!/usr/bin/env python3
"""qa_explorer.py — the STATE-BASED agentic QA explorer (the heart of the AI-driven QA system).

Design philosophy (owner-mandated): EVERY decision is an AI call. There is no scripted click-path, no
selector heuristic deciding what to do next, no rule engine judging pass/fail. The loop is a tight
sense–think–act cycle where the THINK is always the model:

    observe (bridge.state)  ->  AI DECIDES next action (_ai_decide)  ->  act (bridge.act)
      ->  observe again (bridge.state)  ->  AI EVALUATES expected-vs-actual (_ai_evaluate)  ->  record

The model is given the ORIGINAL VISION of the product plus the STORY's EXPECTED behaviour on every turn,
so it judges what SHOULD happen against what DID happen — the definition of a real bug. Cost is explicitly
not a concern here; correctness and thoroughness are. Every _ai_decide / _ai_evaluate is a real
factory.agent call (role 'qa-security'), which already retries on 529/overload and fails over to Codex.

The browser is a persistent Playwright process (browser_bridge.js) we spawn and drive over stdin/stdout,
so one authenticated session is explored across many steps (cookies/localStorage/auth persist).

Public API:
    ex = Explorer(target_url, vision, token=None, org='0')
    steps = ex.explore(story, max_steps=25, on_bug=callback)   # -> list[step-record dicts]
    ex.close()

Each step-record: {step, state, action, expected, actual, bug, verdict, done}.

Run `python qa_explorer.py` for an offline self-test (factory.agent + the bridge are stubbed — no real
API calls, no real browser).
"""
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

BRIDGE_JS = Path(__file__).resolve().parent / "browser_bridge.js"
NODE_PATH = os.environ.get("NODE_PATH") or str(
    Path.home() / "projects" / "products" / "noupload" / "node_modules")
ROLE = "qa-security"


# ----------------------------------------------------------------------------------------------------
# JSON extraction — the model replies with a JSON object (sometimes wrapped in prose / a ```json fence).
# ----------------------------------------------------------------------------------------------------
def _extract_json(text):
    """Pull the last well-formed JSON object out of a model reply. Tries a fenced block first, then a
    brace-balanced scan from the last '{' — robust to leading reasoning and trailing chatter."""
    if not text:
        return {}
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # brace-balanced scan (handles nested objects better than a greedy regex)
    depth, start = 0, None
    best = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    best = text[start:i + 1]
    if best:
        try:
            return json.loads(best)
        except Exception:
            pass
    return {}


def _call_agent(role, repo, task):
    """The single seam to the LLM. Imported lazily so the module loads (and self-tests) without pulling
    in the heavy factory runtime, and so a stubbed `factory` in sys.modules is honoured. factory.agent
    owns all resilience (529/overload retry, Codex failover) and governance."""
    import factory  # noqa: E402 — lazy on purpose (see docstring)
    return factory.agent(role, repo, task)


# ----------------------------------------------------------------------------------------------------
# The browser bridge — a thin, resilient wrapper over the persistent Playwright subprocess.
# ----------------------------------------------------------------------------------------------------
class BrowserBridge:
    """Drives browser_bridge.js over its newline-JSON protocol: one command per line out, one reply
    line back (each reply echoes the request `id`). The bridge emits a `ready` line on startup, takes
    NO navigation argv (we drive it with explicit `goto`/`seedToken`), and exposes native commands
    (goto/state/click/fill/eval/inject/seedToken/close). This wrapper hides the protocol behind a small
    `state()` / `act(action)` surface the state-based loop uses, plus goto/seed for session setup."""

    def __init__(self, target_url, token=None, org="0", timeout=45, autostart=True):
        self.timeout = timeout
        self._id = 0
        self.proc = None
        if not autostart:
            return
        env = {**os.environ, "NODE_PATH": NODE_PATH}
        self.proc = subprocess.Popen(
            ["node", str(BRIDGE_JS)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
        self._await_ready()
        # session setup: seed auth BEFORE the app boots (addInitScript), then navigate.
        if token:
            self.seed_token(token, org)
        self.goto(target_url)

    def _readline(self):
        """Read one line from the bridge honouring the wall-clock timeout (a page load can be slow)."""
        if not self.proc or self.proc.poll() is not None:
            return None
        r, _, _ = select.select([self.proc.stdout], [], [], self.timeout)
        if not r:
            return None
        return self.proc.stdout.readline() or None

    def _await_ready(self):
        """Consume the startup handshake. The first non-noise line should be {cmd:'ready'}; a startup
        error line (playwright missing, etc.) surfaces here instead of hanging."""
        for _ in range(5):
            line = self._readline()
            if not line:
                break
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("cmd") == "ready":
                return j
            if j.get("ok") is False:
                raise RuntimeError(f"browser_bridge failed to start: {j.get('error')}")
        raise RuntimeError("browser_bridge did not become ready")

    def _send(self, obj):
        if not self.proc or self.proc.poll() is not None:
            return {"ok": False, "error": "bridge process is not running"}
        self._id += 1
        obj = {"id": self._id, **obj}
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except Exception as e:
            return {"ok": False, "error": f"bridge write failed: {e}"}
        # commands are strictly serial; skip any stray line until our id comes back (defensive).
        for _ in range(4):
            line = self._readline()
            if not line:
                return {"ok": False, "error": f"bridge timeout after {self.timeout}s"}
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("id") == self._id or j.get("id") is None:
                return j
        return {"ok": False, "error": "bridge desync: no reply matched the request id"}

    def goto(self, url):
        return self._send({"cmd": "goto", "url": url})

    def seed_token(self, token, org="0"):
        return self._send({"cmd": "seedToken", "token": token, "org": org})

    def state(self):
        """Observe: url/title, a screenshot path, enumerated interactable elements (each with a stable
        selector + idx), recent network requests, console/page errors — plus the page's visible text
        (fetched via a follow-up eval, since the native state doesn't include body text)."""
        st = self._send({"cmd": "state"})
        if st.get("ok"):
            ev = self._send({"cmd": "eval",
                             "expr": "document.body?document.body.innerText.replace(/\\s+/g,' ')"
                                     ".trim().slice(0,4000):''"})
            st["bodyText"] = ev.get("result") if ev.get("ok") else ""
        return st

    def act(self, action):
        """Act: translate the AI's action dict {cmd, idx|selector, value} into the bridge's native
        command. Supported cmds: click, type/fill, goto, scroll, wait, noop (unknowns pass through
        so a new bridge verb works without a code change here)."""
        action = action or {}
        cmd = (action.get("cmd") or "noop").lower()
        idx, sel, val = action.get("idx"), action.get("selector"), action.get("value")
        if cmd in ("click", "tap"):
            return self._send({"cmd": "click", "idx": idx, "selector": sel})
        if cmd in ("type", "fill"):
            return self._send({"cmd": "fill", "idx": idx, "selector": sel, "value": val})
        if cmd == "goto":
            return self._send({"cmd": "goto", "url": val or action.get("url")})
        if cmd == "scroll":
            return self._send({"cmd": "eval", "expr": f"window.scrollBy(0,{int(val or 600)});true"})
        if cmd in ("wait", "noop"):
            return {"ok": True, "cmd": cmd}
        return self._send({"cmd": cmd, **{k: v for k, v in action.items() if k != "cmd"}})

    def close(self):
        try:
            self._send({"cmd": "close"})
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# ----------------------------------------------------------------------------------------------------
# Prompt assembly — kept as pure functions so the self-test can assert exactly what the AI is shown.
# ----------------------------------------------------------------------------------------------------
def _fmt_elements(elements, limit=80):
    lines = []
    for e in (elements or [])[:limit]:
        parts = [f"[{e.get('idx')}]", e.get("tag", "?")]
        if e.get("type"):
            parts.append(e["type"])
        if e.get("text"):
            parts.append(f'"{e["text"]}"')
        for k in ("name", "placeholder", "href", "role"):
            if e.get(k):
                parts.append(f"{k}={e[k]}")
        if e.get("disabled"):
            parts.append("(disabled)")
        lines.append(" ".join(str(p) for p in parts))
    return "\n".join(lines) or "(no interactable elements found)"


def _fmt_history(history, limit=8):
    if not history:
        return "(this is the first step)"
    out = []
    for h in history[-limit:]:
        out.append(
            f"- step {h['step']}: action={json.dumps(h['action'])} expected={h['expected']!r} "
            f"-> {'MATCH' if h.get('matched') else 'MISMATCH'}"
            + (f" BUG: {h['bug']}" if h.get("bug") else ""))
    return "\n".join(out)


def _fmt_requests(reqs, limit=8):
    out = []
    for r in (reqs or [])[-limit:]:
        tag = r.get("failed") or r.get("status")
        out.append(f"{r.get('method')} {r.get('url')} -> {tag}")
    return "\n".join(out) or "(none)"


def _fmt_state(state):
    return (
        f"URL: {state.get('url')}\n"
        f"TITLE: {state.get('title')}\n"
        f"CONSOLE_ERRORS: {json.dumps((state.get('console_errors') or [])[:6])}\n"
        f"RECENT_NETWORK (method url -> status; null status/failed = a failed request):\n"
        f"{_fmt_requests(state.get('recent_requests'))}\n"
        f"VISIBLE_TEXT (truncated):\n{(state.get('bodyText') or '')[:1500]}\n"
        f"INTERACTABLE_ELEMENTS (refer to these by their [idx]):\n{_fmt_elements(state.get('elements'))}")


def _decide_prompt(vision, story, state, history):
    return f"""ROLE: You are the QA-SECURITY explorer. Task: DECIDE the single next action.

You are exercising a product as an adversarial, thorough QA engineer. Judge everything against the
ORIGINAL VISION and the STORY's EXPECTED behaviour — you are here to find where reality diverges.

=== ORIGINAL PRODUCT VISION ===
{vision}

=== CURRENT STORY (what a user is trying to do) ===
title: {story.get('title', story.get('name', 'exploration'))}
goal: {story.get('goal', story.get('description', ''))}
EXPECTED OUTCOME: {story.get('expected', story.get('expected_outcome', ''))}

=== HISTORY SO FAR ===
{_fmt_history(history)}

=== CURRENT OBSERVED STATE ===
{_fmt_state(state)}

A screenshot of the current page is at: {state.get('screenshot')}
Open/read that image to SEE the page (layout, rendering, visual bugs), not just its text.

Decide the ONE next action that best advances this story toward its EXPECTED OUTCOME (or that probes an
edge/error case a rigorous tester would try). Reply with ONLY a JSON object, no prose:
{{
  "reasoning": "<one sentence: why this action>",
  "next_action": {{"cmd": "click|type|goto|scroll|wait",
                   "idx": <element idx if targeting one, else omit>,
                   "selector": "<css selector alternative to idx, optional>",
                   "value": "<text to type / url to goto / scroll px, if the cmd needs one>"}},
  "expected": "<concretely, what SHOULD happen after this action — the yardstick for evaluation>",
  "done": <true only if the story's expected outcome is fully satisfied OR further exploration is pointless>
}}"""


def _evaluate_prompt(vision, story, expected, before_state, after_state):
    return f"""ROLE: You are the QA-SECURITY explorer. Task: EVALUATE expected-vs-actual after an action.

Judge honestly and adversarially. A bug is ANY divergence from what a correct product matching the vision
and story should do: wrong/missing result, console/page errors, broken layout, an action that did nothing,
a security/permission leak, confusing or dead-end UX, data loss, etc.

=== ORIGINAL PRODUCT VISION ===
{vision}

=== STORY ===
goal: {story.get('goal', story.get('description', ''))}
STORY EXPECTED OUTCOME: {story.get('expected', story.get('expected_outcome', ''))}

=== WHAT WAS EXPECTED FROM THE ACTION JUST TAKEN ===
{expected}

=== STATE BEFORE THE ACTION ===
{_fmt_state(before_state)}

=== STATE AFTER THE ACTION ===
{_fmt_state(after_state)}
Screenshot after the action: {after_state.get('screenshot')}  (read it to judge visual/rendering correctness)

Reply with ONLY a JSON object, no prose:
{{
  "matches_expected": <true|false>,
  "bug": "<clear description of the bug, or null if none>",
  "severity": "<none|low|medium|high|critical>",
  "blocking": <true if this bug prevents any further meaningful exploration of this story>
}}"""


# ----------------------------------------------------------------------------------------------------
# The Explorer.
# ----------------------------------------------------------------------------------------------------
class Explorer:
    def __init__(self, target_url, vision, token=None, org="0", autostart=True):
        self.target_url = target_url
        self.vision = vision
        self.token = token
        self.org = org
        # a per-session scratch dir is the agent's cwd (so it can Read the screenshots the bridge
        # writes under /tmp/aos-qa and reason over them multimodally).
        self.repo = tempfile.mkdtemp(prefix=f"aos-qa-{uuid.uuid4().hex[:8]}-")
        self.bugs = []
        self.bridge = None
        if autostart:
            self.bridge = BrowserBridge(target_url, token=token, org=org)

    # --- the two AI decisions (EVERY one is a real model call via factory.agent) -----------------
    def _ai_decide(self, story, state, history):
        prompt = _decide_prompt(self.vision, story, state, history)
        res = _call_agent(ROLE, self.repo, prompt)
        j = _extract_json(res.get("out_full") or res.get("out") or "")
        # defensive defaults so a malformed reply can't crash the loop; a missing action becomes a no-op
        # that the next evaluate will flag as "nothing happened".
        return {
            "reasoning": j.get("reasoning", ""),
            "next_action": j.get("next_action") or {"cmd": "noop"},
            "expected": j.get("expected", ""),
            "done": bool(j.get("done", False)),
            "_raw": res,
        }

    def _ai_evaluate(self, story, expected, before_state, after_state):
        prompt = _evaluate_prompt(self.vision, story, expected, before_state, after_state)
        res = _call_agent(ROLE, self.repo, prompt)
        j = _extract_json(res.get("out_full") or res.get("out") or "")
        return {
            "matches_expected": bool(j.get("matches_expected", False)),
            "bug": j.get("bug") if j.get("bug") not in (None, "", "null") else None,
            "severity": j.get("severity", "none"),
            "blocking": bool(j.get("blocking", False)),
            "_raw": res,
        }

    # --- the state-based loop --------------------------------------------------------------------
    def explore(self, story, max_steps=25, on_bug=None):
        """Run the observe -> AI-decide -> act -> observe -> AI-evaluate loop for one story.
        Returns a list of step-records. Fires on_bug(bug_record) for each bug the AI finds."""
        if self.bridge is None:
            raise RuntimeError("Explorer has no browser bridge (constructed with autostart=False)")
        records, history = [], []
        for step in range(max_steps):
            state = self.bridge.state()                          # OBSERVE
            decision = self._ai_decide(story, state, history)    # AI DECIDES
            action = decision["next_action"]
            act_result = self.bridge.act(action)                 # ACT
            after = self.bridge.state()                          # OBSERVE AGAIN
            verdict = self._ai_evaluate(story, decision["expected"], state, after)  # AI EVALUATES

            bug = None
            if verdict["bug"]:
                bug = {
                    "step": step,
                    "story": story.get("title", story.get("name", "")),
                    "url": after.get("url"),
                    "action": action,
                    "expected": decision["expected"],
                    "bug": verdict["bug"],
                    "severity": verdict["severity"],
                    "blocking": verdict["blocking"],
                    "shot": after.get("screenshot"),
                }
                self.bugs.append(bug)
                if on_bug:
                    try:
                        on_bug(bug)
                    except Exception:
                        pass

            record = {
                "step": step,
                "state": {k: state.get(k) for k in ("url", "title", "screenshot", "console_errors")},
                "action": action,
                "expected": decision["expected"],
                "actual": {k: after.get(k) for k in ("url", "title", "screenshot", "console_errors")},
                "bug": bug,
                "verdict": {k: verdict[k] for k in ("matches_expected", "bug", "severity", "blocking")},
                "done": decision["done"],
                "act_result": act_result,
            }
            records.append(record)
            history.append({
                "step": step, "action": action, "expected": decision["expected"],
                "matched": verdict["matches_expected"], "bug": verdict["bug"],
            })

            if decision["done"] or verdict["blocking"]:          # story satisfied, or a wall — stop
                break
        return records

    def close(self):
        if self.bridge:
            self.bridge.close()
            self.bridge = None


# ----------------------------------------------------------------------------------------------------
# Offline self-test — NO real API calls, NO real browser. Stubs factory.agent and the bridge, then
# asserts the loop truly observes -> decides -> acts -> observes -> evaluates and records a seeded bug.
# ----------------------------------------------------------------------------------------------------
def _selftest():
    import types

    # 1) Stub `factory` so _call_agent gets a deterministic, offline model. The stub distinguishes the
    #    DECIDE call from the EVALUATE call by a tag in the prompt, and seeds a bug on step 2.
    calls = {"decide": 0, "evaluate": 0, "order": []}
    fake = types.ModuleType("factory")

    def fake_agent(role, repo, task):
        assert role == ROLE, f"expected role {ROLE!r}, got {role!r}"
        if "DECIDE the single next action" in task:
            n = calls["decide"]
            calls["decide"] += 1
            calls["order"].append("decide")
            # the vision + story's expected outcome must be present in what the AI is shown
            assert "ORIGINAL PRODUCT VISION" in task and "EXPECTED OUTCOME" in task
            done = n >= 3
            body = json.dumps({
                "reasoning": "click the primary action",
                "next_action": {"cmd": "click", "idx": n},
                "expected": f"a result panel appears (step {n})",
                "done": done,
            })
            return {"rc": 0, "out": body, "out_full": "sure, here you go:\n```json\n" + body + "\n```"}
        elif "EVALUATE expected-vs-actual" in task:
            n = calls["evaluate"]
            calls["evaluate"] += 1
            calls["order"].append("evaluate")
            assert "STATE BEFORE THE ACTION" in task and "STATE AFTER THE ACTION" in task
            if n == 1:  # seed a blocking bug on the 2nd evaluation
                body = json.dumps({"matches_expected": False,
                                   "bug": "clicking the button did nothing — no result panel rendered",
                                   "severity": "high", "blocking": True})
            else:
                body = json.dumps({"matches_expected": True, "bug": None,
                                   "severity": "none", "blocking": False})
            return {"rc": 0, "out": body, "out_full": body}
        raise AssertionError("unexpected agent task (neither decide nor evaluate):\n" + task[:200])

    fake.agent = fake_agent
    sys.modules["factory"] = fake

    # 2) Stub the browser bridge — records the observe/act call order, returns synthetic states.
    class StubBridge:
        def __init__(self):
            self.log = []
            self._n = 0

        def state(self):
            self.log.append("state")
            self._n += 1
            return {
                "ok": True, "url": f"http://app.test/step{self._n}", "title": "Test App",
                "screenshot": f"/tmp/state-{self._n}.png", "bodyText": "Welcome to the app",
                "console_errors": [], "recent_requests": [], "elements": [
                    {"idx": 0, "tag": "button", "text": "Go", "type": "button",
                     "selector": '[data-aos-idx="0"]'},
                    {"idx": 1, "tag": "input", "type": "text", "text": "name",
                     "selector": '[data-aos-idx="1"]'},
                ],
            }

        def act(self, action):
            self.log.append(("act", action.get("cmd")))
            return {"ok": True, "action": action}

        def close(self):
            self.log.append("close")

    ex = Explorer("http://app.test", vision="An app that shows a result when you click Go.",
                  token="tok", org="7", autostart=False)
    stub = StubBridge()
    ex.bridge = stub

    seen_bugs = []
    story = {"title": "click Go shows a result",
             "goal": "click the Go button and see a result",
             "expected": "a result panel is displayed after clicking Go"}
    records = ex.explore(story, max_steps=25, on_bug=seen_bugs.append)

    # --- assertions -------------------------------------------------------------------------------
    # loop must have run: it stops after the 2nd step because the seeded bug is blocking.
    assert len(records) == 2, f"expected 2 steps (blocking bug on step 2), got {len(records)}"

    # per step the order MUST be: observe(state) -> decide -> act -> observe(state) -> evaluate.
    # so the bridge sees state,act,state,act (2 states + 1 act per step) and the agent sees
    # decide,evaluate,decide,evaluate.
    assert calls["order"] == ["decide", "evaluate", "decide", "evaluate"], calls["order"]
    # bridge log: state, act, state, (step2) state, act, state
    assert stub.log[:6] == ["state", ("act", "click"), "state",
                            "state", ("act", "click"), "state"], stub.log

    # the seeded bug must be recorded on the loop, surfaced via on_bug, and attached to the step record.
    assert len(ex.bugs) == 1 and ex.bugs[0]["blocking"] is True
    assert len(seen_bugs) == 1 and "did nothing" in seen_bugs[0]["bug"]
    assert records[1]["bug"] is not None and records[1]["bug"]["severity"] == "high"
    assert records[0]["bug"] is None and records[0]["verdict"]["matches_expected"] is True
    # the AI's expected string is captured on each record (the expected-vs-actual yardstick).
    assert records[0]["expected"].startswith("a result panel appears")

    # JSON extraction survived the fenced/prose-wrapped decide reply.
    assert records[0]["action"] == {"cmd": "click", "idx": 0}

    ex.close()
    assert stub.log[-1] == "close"

    print("qa_explorer selftest: PASS "
          f"(steps={len(records)}, ai_calls={calls['decide'] + calls['evaluate']}, bugs={len(ex.bugs)})")


if __name__ == "__main__":
    _selftest()
