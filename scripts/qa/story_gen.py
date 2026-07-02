#!/usr/bin/env python3
"""story_gen.py — AI user-story generator for the agentic QA system.

Design philosophy (owner-mandated): EVERY decision is an AI call. This module makes the FIRST
decision of the QA loop — "what should a real customer be able to DO with this product?" — by asking
a role-specialized agent (via factory.agent, which retries on 529/overload and fails over to Codex)
to enumerate the FULL set of customer behaviors / user-stories that exhaustively cover the ORIGINAL
VISION. Not a sample: complete coverage — happy paths, edge cases, empty states, error/denied/auth
states, and every persona. Downstream the QA loop drives each story state-by-state (observe -> AI
decides -> act -> observe -> AI evaluates) and judges expected-vs-actual against the story's
expected_outcome — which is why we hold the vision + expected behavior in context here.

Cost is explicitly NOT a concern: be maximally AI-driven.

    generate_stories(vision, product_summary) -> [{id,title,persona,steps:[...],expected_outcome}]
    python story_gen.py selftest          # offline, mocked factory.agent — no real API calls

Run with the agent-os venv python.
"""
import json
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent      # .../agent-os/scripts
sys.path.insert(0, str(SCRIPTS))
import factory   # noqa: E402  — the LLM call (retries 529/overload, fails over to Codex)

# The role whose CHARTER frames the enumeration. qa-security carries the adversarial, edge/error/denied
# mindset we want for exhaustive coverage; product-manager is the alternative for persona breadth. Both
# are governed roles in control-plane/roles. Override per-deployment with AOS_STORY_ROLE.
STORY_ROLE = os.environ.get("AOS_STORY_ROLE", "qa-security")


def _strip_fences(txt: str) -> str:
    """Drop a leading ```json / ``` code fence if the model wrapped its reply in one."""
    txt = (txt or "").strip()
    if "```" in txt:
        # take the content of the first fenced block
        parts = txt.split("```")
        if len(parts) >= 3:
            body = parts[1]
            body = body[4:] if body.lower().startswith("json") else body
            return body.strip()
    return txt


def _parse_stories(text: str) -> list:
    """Pull the JSON array of stories out of an agent's free-text reply, ROBUSTLY.
    The agent is asked for a bare JSON array, but real models sometimes add prose or a code fence — so
    we (1) strip fences, (2) try a straight parse, (3) fall back to slicing the outermost [...] span.
    Returns a list of dicts; raises ValueError only when nothing array-shaped can be recovered."""
    body = _strip_fences(text)
    # 1) straight parse (bare array, or an object wrapping {"stories":[...]})
    for candidate in (body,):
        try:
            obj = json.loads(candidate)
            return _coerce_list(obj)
        except Exception:
            pass
    # 2) slice the outermost [ ... ] span
    s, e = body.find("["), body.rfind("]")
    if s >= 0 and e > s:
        try:
            return _coerce_list(json.loads(body[s:e + 1]))
        except Exception:
            pass
    # 3) an object with a stories/user_stories key somewhere in the text
    s, e = body.find("{"), body.rfind("}")
    if s >= 0 and e > s:
        try:
            return _coerce_list(json.loads(body[s:e + 1]))
        except Exception:
            pass
    raise ValueError("no JSON story array found in agent reply")


def _coerce_list(obj):
    """Accept a bare list, or an object that wraps the list under a common key."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("stories", "user_stories", "userStories", "items", "results"):
            v = obj.get(k)
            if isinstance(v, list):
                return v
        # a single story object -> a one-element list
        if obj.get("steps") is not None or obj.get("expected_outcome") is not None:
            return [obj]
    raise ValueError("parsed JSON is not a story list")


def _normalize(raw: list) -> list:
    """Coerce each raw story into the contract shape {id,title,persona,steps:[...],expected_outcome},
    filling stable ids and tolerating minor key/type drift so a downstream driver never KeyErrors.
    Stories missing BOTH steps and an expected_outcome are dropped (not real stories)."""
    out = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            continue
        steps = s.get("steps")
        if isinstance(steps, str):
            steps = [ln.strip(" -*\t") for ln in steps.splitlines() if ln.strip()]
        elif isinstance(steps, list):
            steps = [str(x).strip() for x in steps if str(x).strip()]
        else:
            steps = []
        expected = s.get("expected_outcome") or s.get("expected") or s.get("expectedOutcome") or ""
        expected = expected.strip() if isinstance(expected, str) else str(expected)
        if not steps and not expected:
            continue
        sid = str(s.get("id") or "").strip() or f"US-{i + 1:03d}"
        out.append({
            "id": sid,
            "title": (s.get("title") or s.get("name") or f"Story {i + 1}").strip()
                     if isinstance(s.get("title") or s.get("name"), str) else f"Story {i + 1}",
            "persona": (s.get("persona") or s.get("user") or s.get("role") or "user"),
            "category": s.get("category") or s.get("type") or "",   # happy/edge/error/empty/denied (if given)
            "steps": steps,
            "expected_outcome": expected,
        })
    return out


def _build_prompt(vision: str, product_summary: str, existing: list = None) -> str:
    """The enumeration prompt. Holds the ORIGINAL VISION + product summary in context and demands
    EXHAUSTIVE coverage (every persona × happy/edge/empty/error/denied), returned as a strict JSON array
    the QA loop can drive. `existing` (already-known story titles) lets a caller ask for MORE without
    duplicating — supporting iterative coverage expansion."""
    avoid = ""
    if existing:
        avoid = ("\n\nDo NOT repeat these ALREADY-COVERED stories (produce NEW ones that fill the gaps):\n- "
                 + "\n- ".join(str(t) for t in existing[:80]))
    return (
        "You are enumerating the COMPLETE set of customer behaviors (user-stories) for a product, so an "
        "automated QA fleet can exhaustively exercise it and judge expected-vs-actual against the original "
        "vision. This is the source-of-truth coverage set — aim for COMPLETENESS, not a sample.\n\n"
        "=== ORIGINAL VISION (the intent every story must trace back to) ===\n"
        f"{vision.strip()}\n\n"
        "=== PRODUCT SUMMARY (what actually exists to test) ===\n"
        f"{product_summary.strip()}\n\n"
        "=== WHAT TO PRODUCE ===\n"
        "Enumerate EVERY meaningful thing a real customer could try. Cover, explicitly and exhaustively:\n"
        "  - every PERSONA (new/returning/admin/guest/power-user/unauthorized/abusive/etc. as the product implies)\n"
        "  - HAPPY paths (the core value delivered end-to-end)\n"
        "  - EDGE cases (boundaries, unusual-but-valid input, concurrency, large/slow data)\n"
        "  - EMPTY states (first run, no data yet, nothing found)\n"
        "  - ERROR states (bad input, network/backend failure, timeouts, validation)\n"
        "  - DENIED / auth states (not logged in, wrong role, over quota, blocked)\n"
        "Think adversarially: hunt the ways a real user or attacker breaks it. Do not stop early — a thin "
        "list is a failure. Enumerate as many distinct stories as the vision genuinely warrants.\n\n"
        "=== OUTPUT FORMAT (STRICT) ===\n"
        "Reply with ONLY a JSON array (no prose, no markdown fence). Each element:\n"
        "{\n"
        '  "id": "US-001",                       // stable unique id\n'
        '  "title": "short imperative title",\n'
        '  "persona": "who is doing this",\n'
        '  "category": "happy|edge|empty|error|denied",\n'
        '  "steps": ["concrete user action 1", "action 2", "..."],   // what to DO, in order, UI/observable terms\n'
        '  "expected_outcome": "the precise observable result a correct product must produce for this story"\n'
        "}\n"
        "Every story MUST have non-empty steps AND a concrete expected_outcome (that is what QA judges "
        "against)." + avoid
    )


def generate_stories(vision: str, product_summary: str, role: str = None, existing: list = None,
                     repo: str = None) -> list:
    """Generate the exhaustive user-story coverage set for a product, via ONE role-specialized AI call.

    Args:
        vision: the ORIGINAL product vision/intent (held in context so every story traces back to it,
                and so the QA loop can later judge expected-vs-actual against the intent).
        product_summary: what actually exists to test (features/surfaces/flows).
        role: governing role for the enumeration (default STORY_ROLE = qa-security; product-manager is
              a reasonable alternative). Passed straight to factory.agent.
        existing: optional list of already-covered story titles, to expand coverage without duplicating.
        repo: optional working dir for factory.agent (defaults to factory.PRODUCTS; unused for output
              since we parse the reply, but factory.agent requires a cwd).

    Returns:
        list of {id, title, persona, category, steps:[...], expected_outcome}. Never partial-shaped —
        each story is normalized to the full contract; malformed entries are dropped. Returns [] only
        when the agent call failed or produced nothing parseable (caller can retry / escalate).
    """
    role = role or STORY_ROLE
    repo = repo or str(factory.PRODUCTS)
    prompt = _build_prompt(vision, product_summary, existing)
    # tools=[] : pure reasoning turn — no web, no file writes. We consume the REPLY (out_full) directly.
    r = factory.agent(role, repo, prompt, tools=[])
    if r.get("failed") or r.get("rc", 1) != 0:
        # Never fabricate a coverage set — surface emptiness so the loop can retry/escalate honestly.
        print(f"[story_gen] agent call failed (rc={r.get('rc')}, reason={r.get('reason') or r.get('blocker')})",
              flush=True)
        return []
    text = r.get("out_full") or r.get("out") or ""
    try:
        raw = _parse_stories(text)
    except ValueError as e:
        print(f"[story_gen] parse miss: {e}. REPLY HEAD:\n{text[:400]!r}", flush=True)
        return []
    stories = _normalize(raw)
    print(f"[story_gen] generated {len(stories)} user-stories (role={role})", flush=True)
    return stories


# ---------------------------------------------------------------------------------------------------
def _selftest():
    """Offline check (NO real spend): monkeypatch factory.agent to return a CANNED JSON story list, then
    assert generate_stories parses it, normalizes to the contract shape, and that every story carries
    non-empty steps + expected_outcome. Also exercises the robust parser on fenced/wrapped/messy replies.
    Deterministic, no DB, no network. Restores factory.agent in finally."""
    canned = [
        {"id": "US-001", "title": "New user signs up", "persona": "new visitor", "category": "happy",
         "steps": ["Open the landing page", "Click Sign Up", "Enter email + password", "Submit"],
         "expected_outcome": "Account is created and the user lands on the empty dashboard."},
        {"title": "Sign up with taken email", "persona": "new visitor", "category": "error",
         "steps": "Open Sign Up\nEnter an already-registered email\nSubmit",
         "expected": "A clear 'email already in use' error; no duplicate account."},
        {"name": "First run empty dashboard", "user": "returning user", "type": "empty",
         "steps": ["Log in as a user with no projects yet"],
         "expectedOutcome": "An empty-state prompt to create the first project is shown."},
        {"title": "Unauthorized access blocked", "persona": "logged-out user", "category": "denied",
         "steps": ["Navigate directly to /admin without logging in"],
         "expected_outcome": "Redirected to login; admin content never renders."},
        {"garbage": "no steps or outcome — must be dropped"},
    ]

    real_agent, real_products = factory.agent, factory.PRODUCTS
    factory.PRODUCTS = Path("/nonexistent-selftest")   # never touched (tools=[], reply-only) but must exist as a str

    captured = {}

    def fake_agent(role, repo, task, **k):
        captured["role"] = role
        captured["task"] = task
        captured["tools"] = k.get("tools")
        # Return the canned list as an agent WOULD — wrapped in a ```json fence + prose, to prove the parser is robust.
        body = "Here are the exhaustive user-stories:\n```json\n" + json.dumps(canned) + "\n```\nDone."
        return {"rc": 0, "out": body[-1500:], "out_full": body}

    ok = False
    try:
        factory.agent = fake_agent
        stories = generate_stories("Let anyone ship a product by chatting.",
                                   "A web app with signup, dashboard, and an admin area.")
        # contract assertions
        assert isinstance(stories, list) and len(stories) == 4, f"expected 4 normalized stories, got {len(stories)}"
        ids = [s["id"] for s in stories]
        assert len(set(ids)) == 4, f"ids not unique/filled: {ids}"
        for s in stories:
            assert set(("id", "title", "persona", "steps", "expected_outcome")).issubset(s), f"missing keys: {s}"
            assert isinstance(s["steps"], list) and s["steps"], f"steps must be a non-empty list: {s}"
            assert isinstance(s["expected_outcome"], str) and s["expected_outcome"].strip(), \
                f"expected_outcome must be non-empty: {s}"
        # the string-steps story was split into a list
        taken = next(s for s in stories if "taken" in s["title"].lower())
        assert len(taken["steps"]) == 3, f"string steps not split: {taken['steps']}"
        # key-drift normalization (name/user/expectedOutcome -> title/persona/expected_outcome)
        empty = next(s for s in stories if s["category"] == "empty")
        assert empty["persona"] == "returning user" and empty["expected_outcome"].startswith("An empty"), empty
        # the garbage entry (no steps + no outcome) was dropped
        assert all("garbage" not in json.dumps(s) for s in stories)
        # the call went to the QA role, with tools disabled, and held the VISION in the prompt
        assert captured["role"] == STORY_ROLE, captured["role"]
        assert captured["tools"] == [], captured["tools"]
        assert "ORIGINAL VISION" in captured["task"] and "ship a product by chatting" in captured["task"]

        # parser robustness: bare array, object-wrapped, and a failed-agent path
        assert len(_parse_stories(json.dumps(canned[:2]))) == 2
        assert len(_parse_stories('{"stories": ' + json.dumps(canned[:1]) + '}')) == 1
        factory.agent = lambda *a, **k: {"rc": 1, "failed": True, "out": "boom", "out_full": "boom"}
        assert generate_stories("v", "p") == [], "failed agent call must yield []"

        ok = True
        print(f"PASS: story_gen — {len(stories)} stories normalized, robust parse, QA role + vision wired ✅")
    except AssertionError as e:
        print(f"FAIL: {e}")
    finally:
        factory.agent, factory.PRODUCTS = real_agent, real_products
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "selftest":
        _selftest()
    else:
        sys.exit("usage: story_gen.py selftest")
