#!/usr/bin/env python3
"""story_gen.py — AI user-story generator + SATURATION loop + persistent story corpus.

Design philosophy (owner-mandated): EVERY decision is an AI call. This module makes the FIRST
decision of the QA loop — "what should a real customer be able to DO with this product?" — and it
does so per the 2026-07 architecture review (quality-engine verdict on story_gen.py:126-205):

  * `generate_stories`   — ONE role-specialized enumeration call (the primitive).
  * `saturate_stories`   — the LOOP the verdict demanded: generate -> AI SELF-CRITIQUE for gaps
    (per surface × persona × category: happy, edge, error/empty/denied, abuse, latency, a11y) ->
    expand via the `existing` param -> repeat until an INDEPENDENT coverage-judge AI call (a
    different role — the enumerator never grades its own homework) says SATURATED, bounded rounds.
  * Postgres persistence — the corpus lives in the `story_corpus` table PER PRODUCT so it GROWS
    across releases instead of being regenerated from scratch each run (`save_stories`,
    `load_corpus`).
  * `add_regression_story(bug) -> story` — every FIXED bug pins a PERMANENT regression story
    (source='regression'; an upsert can never downgrade it back to 'generated'), so a bug fixed in
    round 2 can never silently return next release with no memory of it.

Downstream the QA loop drives each story state-by-state (observe -> AI decides -> act -> observe ->
AI evaluates) and judges expected-vs-actual against the story's expected_outcome — which is why we
hold the vision + expected behavior in context here. Cost is explicitly NOT a concern: be maximally
AI-driven. Persistence failures are LOUD but never fabricate coverage.

    generate_stories(vision, product_summary) -> [{id,title,persona,category,steps,expected_outcome}]
    saturate_stories(vision, product_summary, product="slug") -> saturated + persisted corpus
    add_regression_story(bug, product="slug") -> the pinned regression story
    python story_gen.py selftest             # offline AI (mocked factory.agent) + REAL local Postgres
    python story_gen.py corpus <product>     # dump a product's persisted story corpus as JSON

Run with the agent-os venv python.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Json

SCRIPTS = Path(__file__).resolve().parent.parent      # .../agent-os/scripts
sys.path.insert(0, str(SCRIPTS))
import factory   # noqa: E402  — the LLM call (retries 529/overload, fails over to Codex)

# The role whose CHARTER frames the enumeration + self-critique. qa-security carries the adversarial,
# edge/error/denied mindset we want for exhaustive coverage. The coverage JUDGE is a DIFFERENT governed
# role by default (product-manager: persona/journey breadth) so the enumerator never grades its own
# homework (STANDARDS-verification.md rule 5). Override per-deployment with the env vars.
STORY_ROLE = os.environ.get("AOS_STORY_ROLE", "qa-security")
JUDGE_ROLE = os.environ.get("AOS_STORY_JUDGE_ROLE", "product-manager")
MAX_ROUNDS = int(os.environ.get("AOS_STORY_MAX_ROUNDS", "4"))   # bounded saturation rounds

# The coverage matrix every critique/judge call reasons over: per SURFACE × PERSONA × CATEGORY.
CATEGORIES = ("happy", "edge", "empty", "error", "denied", "abuse", "latency", "a11y")

# ── Postgres (same resolution pattern as findings.py, but tolerant of a missing .env.local) ─────────
ENV = Path.home() / "projects" / "agent-os" / ".env.local"


def _dsn():
    """The corpus DB. Env override first (AOS_DATABASE_URL / DATABASE_URL), then .env.local. None -> no DB
    (persistence is skipped LOUDLY; enumeration still works in-memory so a QA run is never bricked)."""
    dsn = os.environ.get("AOS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if dsn:
        return dsn
    if ENV.exists():
        for ln in ENV.read_text().splitlines():
            if ln.strip().startswith("DATABASE_URL="):
                return ln.split("=", 1)[1].strip()
    return None


def _ensure(cur):
    """story_corpus — the per-product, release-spanning story corpus. UNIQUE(product, title) is the merge
    key (same as the in-memory dedupe), so re-running QA upserts rather than duplicates, and a 'regression'
    row can NEVER be downgraded by a later generated story with the same title (see save_stories)."""
    cur.execute("""CREATE TABLE IF NOT EXISTS story_corpus (
        id BIGSERIAL PRIMARY KEY,
        product TEXT NOT NULL,
        story_id TEXT NOT NULL,
        title TEXT NOT NULL,
        persona TEXT NOT NULL DEFAULT 'user',
        category TEXT NOT NULL DEFAULT '',
        steps JSONB NOT NULL DEFAULT '[]',
        expected_outcome TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'generated',     -- generated | regression (a pinned fixed bug)
        bug_ref TEXT,                                  -- for regression pins: the originating bug record
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (product, title))""")
    cur.execute("CREATE INDEX IF NOT EXISTS story_corpus_product_idx ON story_corpus (product)")


def save_stories(product: str, stories: list, source: str = "generated") -> int:
    """UPSERT stories into the per-product corpus. Returns rows written (0 on no-DB/failure — LOUD).
    Regression pins are permanent: ON CONFLICT the row's source stays 'regression' once set, the original
    story_id/bug_ref are kept, and content fields refresh to the newest wording."""
    dsn = _dsn()
    if not dsn or not product or not stories:
        if not dsn:
            print("[story_gen] WARNING: no DATABASE_URL — story corpus NOT persisted", flush=True)
        return 0
    try:
        with psycopg.connect(dsn) as c, c.cursor() as cur:
            _ensure(cur)
            for s in stories:
                cur.execute(
                    """INSERT INTO story_corpus
                         (product, story_id, title, persona, category, steps, expected_outcome, source, bug_ref)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (product, title) DO UPDATE SET
                         persona = EXCLUDED.persona,
                         category = CASE WHEN story_corpus.source = 'regression'
                                         THEN story_corpus.category ELSE EXCLUDED.category END,
                         steps = EXCLUDED.steps,
                         expected_outcome = EXCLUDED.expected_outcome,
                         source = CASE WHEN story_corpus.source = 'regression'
                                       THEN 'regression' ELSE EXCLUDED.source END,
                         bug_ref = COALESCE(story_corpus.bug_ref, EXCLUDED.bug_ref),
                         updated_at = now()""",
                    (product, s.get("id") or "", (s.get("title") or "").strip(), s.get("persona") or "user",
                     s.get("category") or "", Json(s.get("steps") or []), s.get("expected_outcome") or "",
                     s.get("source") or source, s.get("bug_ref")))
            c.commit()
        return len(stories)
    except Exception as e:
        print(f"[story_gen] WARNING: corpus save failed for {product!r}: {e}", flush=True)
        return 0


def load_corpus(product: str) -> list:
    """The persisted corpus for a product, in the story contract shape (+source/bug_ref), insertion order.
    [] on no-DB/no-rows/failure — LOUD on failure, so 'regenerated from scratch' is never silent."""
    dsn = _dsn()
    if not dsn or not product:
        return []
    try:
        with psycopg.connect(dsn) as c, c.cursor() as cur:
            _ensure(cur)
            cur.execute("""SELECT story_id, title, persona, category, steps, expected_outcome, source, bug_ref
                           FROM story_corpus WHERE product = %s ORDER BY id""", (product,))
            rows = cur.fetchall()
        return [{"id": r[0], "title": r[1], "persona": r[2], "category": r[3],
                 "steps": list(r[4] or []), "expected_outcome": r[5] or "",
                 "source": r[6], "bug_ref": r[7]} for r in rows]
    except Exception as e:
        print(f"[story_gen] WARNING: corpus load failed for {product!r}: {e} — starting from scratch",
              flush=True)
        return []


# ── parsing helpers ─────────────────────────────────────────────────────────────────────────────────
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


def _stories_from_repo(repo) -> list:
    """RECOVERY (F13): some agents WRITE the story set to a JSON file in the repo and reply with only a prose
    summary ('written to docs/qa-user-stories.json'), so the inline parse finds nothing. The work isn't lost —
    read it off disk instead of paying for an expensive Opus regeneration. Returns the newest parseable stories
    file's list, or [] if none. Best-effort."""
    if not repo:
        return []
    from pathlib import Path
    root = Path(repo)
    try:
        cands = [p for p in (list(root.glob("**/*stor*.json")) + list(root.glob("**/*user-stories*.json")))
                 if "node_modules" not in p.parts and ".git" not in p.parts]
    except Exception:
        return []
    for p in sorted(set(cands), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            lst = _coerce_list(json.loads(p.read_text()))
            if lst:
                print(f"[story_gen] recovered {len(lst)} stories from {p.name} the agent wrote (no retry)", flush=True)
                return lst
        except Exception:
            continue
    return []


def _coerce_list(obj):
    """Accept a bare list, or an object that wraps the list under a common key."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("stories", "user_stories", "userStories", "items", "results", "gaps", "missing"):
            v = obj.get(k)
            if isinstance(v, list):
                return v
        # a single story object -> a one-element list
        if obj.get("steps") is not None or obj.get("expected_outcome") is not None:
            return [obj]
    raise ValueError("parsed JSON is not a story list")


def _parse_obj(text: str) -> dict:
    """Pull ONE JSON object out of a free-text agent reply (for the coverage judge). Raises ValueError."""
    body = _strip_fences(text)
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    s, e = body.find("{"), body.rfind("}")
    if s >= 0 and e > s:
        obj = json.loads(body[s:e + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object found in agent reply")


def _normalize(raw: list) -> list:
    """Coerce each raw story into the contract shape {id,title,persona,category,steps,expected_outcome},
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
            "category": s.get("category") or s.get("type") or "",   # one of CATEGORIES (if given)
            "steps": steps,
            "expected_outcome": expected,
        })
    return out


def _tkey(title: str) -> str:
    """The dedupe/merge key for a story title (case/whitespace-insensitive)."""
    return " ".join(str(title or "").lower().split())


def _merge(base: list, new: list) -> list:
    """Union by title key — the corpus only ever GROWS; earlier stories (incl. regression pins) win."""
    seen = {_tkey(s.get("title")) for s in base}
    out = list(base)
    for s in new:
        k = _tkey(s.get("title"))
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _assign_ids(stories: list) -> list:
    """Make ids unique across the merged corpus WITHOUT renaming already-stable ids (US-/REG- pins keep
    their identity across releases); colliding/blank ids get the next free US-### slot."""
    used, n = set(), 0
    for s in stories:
        sid = str(s.get("id") or "").strip()
        if not sid or sid in used:
            while True:
                n += 1
                sid = f"US-{n:03d}"
                if sid not in used:
                    break
        used.add(sid)
        s["id"] = sid
    return stories


# ── the AI calls ────────────────────────────────────────────────────────────────────────────────────
def _ai_text(role: str, repo: str, prompt: str):
    """ONE pure-reasoning factory.agent turn (tools=[]; we consume the REPLY). None on failure — never
    fabricate: the caller surfaces emptiness / fails closed so the loop can retry or escalate honestly."""
    r = factory.agent(role, repo or str(factory.PRODUCTS), prompt, tools=[])
    if r.get("failed") or r.get("rc", 1) != 0:
        print(f"[story_gen] agent call failed (role={role}, rc={r.get('rc')}, "
              f"reason={r.get('reason') or r.get('blocker')})", flush=True)
        return None
    return r.get("out_full") or r.get("out") or ""


def _story_digest(stories: list) -> str:
    """A compact one-line-per-story digest (title | persona | category) for critique/judge prompts."""
    return "\n".join(f"- {s.get('title')} | persona={s.get('persona')} | category={s.get('category') or '?'}"
                     for s in stories)


def _build_prompt(vision: str, product_summary: str, existing: list = None, gaps: list = None) -> str:
    """The enumeration prompt. Holds the ORIGINAL VISION + product summary in context and demands
    EXHAUSTIVE coverage (every persona × category matrix), returned as a strict JSON array the QA loop
    can drive. `existing` (already-known story titles) + `gaps` (self-critique findings) support the
    saturation loop: expand coverage without duplicating, aimed at the known holes."""
    avoid = ""
    if existing:
        avoid = ("\n\nDo NOT repeat these ALREADY-COVERED stories (produce NEW ones that fill the gaps):\n- "
                 + "\n- ".join(str(t) for t in existing[:120]))
    aim = ""
    if gaps:
        aim = ("\n\n=== KNOWN COVERAGE GAPS (from the coverage self-critique — every new story should close "
               "one of these) ===\n- " + "\n- ".join(str(g) for g in gaps[:40]))
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
        "  - ABUSE (hostile/injection input, spam, repeated misuse, attempts to break isolation)\n"
        "  - LATENCY (slow backend/network perception, human-pace dwell, long-running work, timeouts)\n"
        "  - A11Y (keyboard-only navigation, screen-reader labels, focus order, mobile viewport)\n"
        "Think adversarially: hunt the ways a real user or attacker breaks it. Do not stop early — a thin "
        "list is a failure. Enumerate as many distinct stories as the vision genuinely warrants.\n\n"
        "=== OUTPUT FORMAT (STRICT) ===\n"
        "Reply with ONLY a JSON array (no prose, no markdown fence). Each element:\n"
        "{\n"
        '  "id": "US-001",                       // stable unique id\n'
        '  "title": "short imperative title",\n'
        '  "persona": "who is doing this",\n'
        '  "category": "happy|edge|empty|error|denied|abuse|latency|a11y",\n'
        '  "steps": ["concrete user action 1", "action 2", "..."],   // what to DO, in order, UI/observable terms\n'
        '  "expected_outcome": "the precise observable result a correct product must produce for this story"\n'
        "}\n"
        "Every story MUST have non-empty steps AND a concrete expected_outcome (that is what QA judges "
        "against)." + avoid + aim
    )


def generate_stories(vision: str, product_summary: str, role: str = None, existing: list = None,
                     repo: str = None, gaps: list = None) -> list:
    """Generate a user-story coverage set for a product, via ONE role-specialized AI call.

    Args:
        vision: the ORIGINAL product vision/intent (held in context so every story traces back to it,
                and so the QA loop can later judge expected-vs-actual against the intent).
        product_summary: what actually exists to test (features/surfaces/flows).
        role: governing role for the enumeration (default STORY_ROLE = qa-security).
        existing: optional list of already-covered story titles, to expand coverage without duplicating.
        repo: optional working dir for factory.agent (defaults to factory.PRODUCTS).
        gaps: optional list of known coverage gaps (from critique_gaps) the new stories must close.

    Returns:
        list of {id, title, persona, category, steps:[...], expected_outcome}. Never partial-shaped —
        each story is normalized to the full contract; malformed entries are dropped. Returns [] only
        when the agent call failed or produced nothing parseable (caller can retry / escalate).

    NOTE: one completion physically caps at a few dozen stories — for the SATURATED corpus the review
    demanded, call `saturate_stories` (which loops this + critique + an independent coverage judge)."""
    role = role or STORY_ROLE
    text = _ai_text(role, repo, _build_prompt(vision, product_summary, existing, gaps))
    if text is None:
        return []
    try:
        raw = _parse_stories(text)
    except ValueError as e:
        raw = _stories_from_repo(repo)          # F13: the agent may have WRITTEN the stories to a file + replied prose
        if not raw:
            print(f"[story_gen] parse miss: {e}. REPLY HEAD:\n{text[:400]!r}", flush=True)
            return []
    stories = _normalize(raw)
    print(f"[story_gen] generated {len(stories)} user-stories (role={role})", flush=True)
    return stories


def critique_gaps(vision: str, product_summary: str, stories: list, role: str = None,
                  repo: str = None) -> list:
    """AI SELF-CRITIQUE: given the current story set, enumerate the coverage HOLES across the full matrix
    (per surface × persona × category: happy/edge/empty/error/denied/abuse/latency/a11y). Returns a list
    of concrete gap descriptions ([] = the critique found none, or the call failed — the independent
    judge, not this self-critique, decides saturation)."""
    role = role or STORY_ROLE
    prompt = (
        "=== COVERAGE SELF-CRITIQUE ===\n"
        "You wrote the user-story coverage set below for an automated QA fleet. Now attack it: assume it "
        "is INCOMPLETE and hunt the holes a hostile, impatient real user (or attacker) would fall into.\n\n"
        "=== ORIGINAL VISION ===\n"
        f"{vision.strip()}\n\n"
        "=== PRODUCT SUMMARY (surfaces that exist to test) ===\n"
        f"{product_summary.strip()}\n\n"
        "=== CURRENT STORY SET ===\n"
        f"{_story_digest(stories)}\n\n"
        "=== WHAT TO PRODUCE ===\n"
        "Walk the full coverage matrix — for EVERY product surface, EVERY persona, and EVERY category in "
        f"{'/'.join(CATEGORIES)} — and list each concrete MISSING story as one line: "
        "'<surface> × <persona> × <category>: <what is untested and why it matters>'.\n"
        "Reply with ONLY a JSON array of strings (no prose, no fence). [] if you genuinely find no gap."
    )
    text = _ai_text(role, repo, prompt)
    if text is None:
        return []
    try:
        raw = _parse_stories(text) if "[" in (text or "") else []
    except ValueError:
        print(f"[story_gen] critique parse miss. REPLY HEAD:\n{(text or '')[:200]!r}", flush=True)
        return []
    gaps = []
    for g in raw:
        if isinstance(g, str) and g.strip():
            gaps.append(g.strip())
        elif isinstance(g, dict):
            gaps.append(str(g.get("gap") or g.get("missing") or json.dumps(g)))
    print(f"[story_gen] self-critique found {len(gaps)} coverage gap(s) (role={role})", flush=True)
    return gaps


def judge_saturation(vision: str, product_summary: str, stories: list, role: str = None,
                     repo: str = None) -> dict:
    """The INDEPENDENT coverage judge (a DIFFERENT governed role than the enumerator — no agent grades
    its own homework). Decides whether the story set is SATURATED for this vision/product. FAILS CLOSED:
    any call/parse failure means NOT saturated (the loop keeps expanding within its round bound).

    Returns {"saturated": bool, "reason": str, "missing": [...]}."""
    role = role or JUDGE_ROLE
    prompt = (
        "=== COVERAGE JUDGE ===\n"
        "You are an INDEPENDENT judge (you did NOT write these stories). An automated QA fleet will "
        "exercise ONLY the stories below — anything they miss ships untested to paying customers, and the "
        "bar is ZERO bugs ever reaching a human. Decide whether this coverage set is SATURATED: every "
        "product surface × persona × category "
        f"({'/'.join(CATEGORIES)}) that the vision and summary imply is genuinely exercised.\n"
        "Be pessimistic on the product's behalf: if ANY meaningful behavior is untested, it is NOT "
        "saturated.\n\n"
        "=== ORIGINAL VISION ===\n"
        f"{vision.strip()}\n\n"
        "=== PRODUCT SUMMARY ===\n"
        f"{product_summary.strip()}\n\n"
        "=== STORY SET UNDER JUDGMENT ===\n"
        f"{_story_digest(stories)}\n\n"
        "=== OUTPUT (STRICT) ===\n"
        'Reply with ONLY a JSON object (no prose, no fence): {"saturated": true|false, '
        '"reason": "one-paragraph justification", "missing": ["concrete missing story", ...]} '
        '("missing" MUST be non-empty when saturated is false).'
    )
    text = _ai_text(role, repo, prompt)
    if text is None:
        return {"saturated": False, "reason": "coverage-judge call failed (fail closed)", "missing": []}
    try:
        obj = _parse_obj(text)
    except Exception as e:
        print(f"[story_gen] judge parse miss: {e}. REPLY HEAD:\n{text[:200]!r}", flush=True)
        return {"saturated": False, "reason": "coverage-judge reply unparseable (fail closed)", "missing": []}
    missing = obj.get("missing") or []
    if not isinstance(missing, list):
        missing = [str(missing)]
    verdict = {"saturated": bool(obj.get("saturated")), "reason": str(obj.get("reason") or ""),
               "missing": [str(m) for m in missing]}
    print(f"[story_gen] coverage judge (role={role}): saturated={verdict['saturated']} "
          f"missing={len(verdict['missing'])}", flush=True)
    return verdict


def saturate_stories(vision: str, product_summary: str, *, product: str = None, role: str = None,
                     judge_role: str = None, max_rounds: int = None, repo: str = None,
                     persist: bool = True) -> list:
    """The SATURATION loop the arch-review verdict demanded (story_gen.py falls-short, :126-205):

        load the persisted per-product corpus (it GROWS across releases; regression pins ride along)
        repeat (bounded rounds):
            generate NEW stories (existing titles excluded; aimed at known gaps)  # AI call, STORY_ROLE
            merge into the corpus (dedupe by title; earlier stories + pins win)
            INDEPENDENT coverage judge: saturated?                               # AI call, JUDGE_ROLE
                yes -> stop
            AI self-critique for gaps per surface × persona × category           # AI call, STORY_ROLE
            (gaps + the judge's missing list steer the next generation round)
        persist the merged corpus (upsert; regression pins can never be downgraded)

    Args:
        product: the product slug keying the story_corpus table. None -> in-memory only (no persistence).
        role / judge_role: enumerator+critic vs independent judge roles (default qa-security vs
                           product-manager — never the same homework-grader).
        max_rounds: bound on generate->critique->expand cycles (default AOS_STORY_MAX_ROUNDS=4).
        persist: set False to skip the corpus write (e.g. dry runs).

    Returns the merged story list (contract shape; unique ids). Returns [] only when every generation
    round failed AND no prior corpus exists — never a fabricated set."""
    role = role or STORY_ROLE
    judge_role = judge_role or JUDGE_ROLE
    max_rounds = max_rounds or MAX_ROUNDS
    stories = load_corpus(product) if product else []
    if stories:
        print(f"[story_gen] corpus for {product!r} starts at {len(stories)} persisted stories "
              f"(grows across releases)", flush=True)
    gaps = []
    saturated, verdict = False, {}
    for rnd in range(1, max_rounds + 1):
        new = generate_stories(vision, product_summary, role=role,
                               existing=[s["title"] for s in stories], repo=repo, gaps=gaps)
        before = len(stories)
        stories = _assign_ids(_merge(stories, new))
        print(f"[story_gen] saturation round {rnd}/{max_rounds}: +{len(stories) - before} new "
              f"(corpus={len(stories)})", flush=True)
        if not stories:
            continue        # generation failed with no prior corpus — retry within the round bound
        verdict = judge_saturation(vision, product_summary, stories, role=judge_role, repo=repo)
        if verdict.get("saturated"):
            saturated = True
            break
        gaps = critique_gaps(vision, product_summary, stories, role=role, repo=repo)
        gaps += [m for m in verdict.get("missing", []) if m not in gaps]
    if persist and product and stories:
        wrote = save_stories(product, stories)
        print(f"[story_gen] persisted {wrote}/{len(stories)} stories to story_corpus for {product!r}",
              flush=True)
    print(f"[story_gen] saturation {'REACHED' if saturated else 'NOT reached (round bound hit)'} — "
          f"{len(stories)} stories{' — ' + verdict.get('reason', '') if verdict.get('reason') else ''}",
          flush=True)
    return stories


def add_regression_story(bug: dict, product: str = None, vision: str = "", role: str = None,
                         repo: str = None, persist: bool = True) -> dict:
    """Pin a FIXED bug as a PERMANENT regression story so it can never silently return next release.

    The story is written by an AI call (the QA role turns the observed bug record into a re-runnable
    story whose steps reproduce the original repro and whose expected_outcome asserts the CORRECT
    behavior). If the AI call fails, we still pin a GROUNDED deterministic story built purely from the
    observed bug fields (never fabricated, never lost). The pin is persisted with source='regression'
    — save_stories guarantees a later generated story with the same title can never downgrade it.

    Args:
        bug: the explorer/dev_loop bug record ({bug, expected, url, action, story, severity, ...}).
        product: the story_corpus product slug (None -> the story is returned but not persisted).
        vision: optional product vision for context.

    Returns the regression story dict (contract shape + source/bug_ref + persisted: bool)."""
    role = role or STORY_ROLE
    bug_json = json.dumps(bug, default=str, sort_keys=True)
    rid = "REG-" + hashlib.sha1(bug_json.encode()).hexdigest()[:8]   # deterministic, collision-safe id
    prompt = (
        "=== REGRESSION PIN ===\n"
        "A bug was found by the QA explorer and has been FIXED. Write ONE permanent regression user-story "
        "that would CATCH this exact bug if it ever returns: the steps must reproduce the original repro "
        "path, and expected_outcome must assert the CORRECT behavior the fix restored.\n\n"
        + (f"=== PRODUCT VISION ===\n{vision.strip()}\n\n" if vision.strip() else "")
        + "=== THE FIXED BUG (observed record) ===\n"
        f"{bug_json}\n\n"
        "=== OUTPUT (STRICT) ===\n"
        "Reply with ONLY a JSON object (no prose, no fence):\n"
        '{"title": "Regression: <short bug summary>", "persona": "who hits this", '
        '"steps": ["concrete repro action 1", "..."], '
        '"expected_outcome": "the correct observable behavior (the bug must NOT reappear)"}'
    )
    story = None
    text = _ai_text(role, repo, prompt)
    if text is not None:
        try:
            cand = _normalize(_parse_stories(text))
            if cand:
                story = cand[0]
        except ValueError as e:
            print(f"[story_gen] regression-pin parse miss: {e}", flush=True)
    if story is None:
        # GROUNDED fallback — every field comes from the observed bug record; the pin is never lost.
        print("[story_gen] regression-pin AI call failed — pinning a grounded story from the bug record",
              flush=True)
        steps = []
        if bug.get("url"):
            steps.append(f"Go to {bug['url']}")
        if bug.get("action"):
            steps.append(f"Perform the original repro action: {json.dumps(bug['action'], default=str)}")
        steps.append("Observe the result at human pace (dwell; watch for the original failure)")
        story = {
            "title": f"Regression: {(bug.get('bug') or 'fixed defect').strip()[:90]}",
            "persona": bug.get("persona") or "user who originally hit the bug",
            "steps": steps,
            "expected_outcome": (bug.get("expected") or "").strip()
                                or f"The previously-fixed defect must not reappear: {bug.get('bug', '')}",
        }
    story.update({"id": rid, "category": "regression", "source": "regression", "bug_ref": bug_json[:4000]})
    story["persisted"] = False
    if persist and product:
        story["persisted"] = save_stories(product, [story], source="regression") > 0
    print(f"[story_gen] pinned regression story {rid} ({story['title'][:60]!r}) "
          f"persisted={story['persisted']}", flush=True)
    return story


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# selftest — AI calls are ALWAYS mocked (no spend/network). Persistence + regression pinning are
# exercised against the REAL local Postgres (findings.py precedent — evidence, not claims) under a
# unique throwaway product slug, cleaned up in finally.
# ═══════════════════════════════════════════════════════════════════════════════════════════════════
def _ok_reply(body: str) -> dict:
    return {"rc": 0, "out": body[-1500:], "out_full": body}


def _selftest_generation(captured: dict) -> list:
    """Original contract checks: parse/normalize/key-drift/garbage-drop + role/vision/tools wiring."""
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

    def fake_agent(role, repo, task, **k):
        captured["role"], captured["task"], captured["tools"] = role, task, k.get("tools")
        # wrapped in a ```json fence + prose, to prove the parser is robust.
        return _ok_reply("Here are the exhaustive user-stories:\n```json\n" + json.dumps(canned) + "\n```\nDone.")

    factory.agent = fake_agent
    stories = generate_stories("Let anyone ship a product by chatting.",
                               "A web app with signup, dashboard, and an admin area.")
    assert isinstance(stories, list) and len(stories) == 4, f"expected 4 normalized stories, got {len(stories)}"
    ids = [s["id"] for s in stories]
    assert len(set(ids)) == 4, f"ids not unique/filled: {ids}"
    for s in stories:
        assert set(("id", "title", "persona", "steps", "expected_outcome")).issubset(s), f"missing keys: {s}"
        assert isinstance(s["steps"], list) and s["steps"], f"steps must be a non-empty list: {s}"
        assert isinstance(s["expected_outcome"], str) and s["expected_outcome"].strip(), \
            f"expected_outcome must be non-empty: {s}"
    taken = next(s for s in stories if "taken" in s["title"].lower())
    assert len(taken["steps"]) == 3, f"string steps not split: {taken['steps']}"
    empty = next(s for s in stories if s["category"] == "empty")
    assert empty["persona"] == "returning user" and empty["expected_outcome"].startswith("An empty"), empty
    assert all("garbage" not in json.dumps(s) for s in stories)
    assert captured["role"] == STORY_ROLE, captured["role"]
    assert captured["tools"] == [], captured["tools"]
    assert "ORIGINAL VISION" in captured["task"] and "ship a product by chatting" in captured["task"]
    # the generation prompt demands the FULL coverage matrix (incl. the review's added categories)
    for cat in ("ABUSE", "LATENCY", "A11Y"):
        assert cat in captured["task"], f"generation prompt missing {cat} coverage demand"
    # parser robustness + failed-agent honesty
    assert len(_parse_stories(json.dumps(canned[:2]))) == 2
    assert len(_parse_stories('{"stories": ' + json.dumps(canned[:1]) + '}')) == 1
    factory.agent = lambda *a, **k: {"rc": 1, "failed": True, "out": "boom", "out_full": "boom"}
    assert generate_stories("v", "p") == [], "failed agent call must yield []"
    return stories


def _selftest_saturation() -> list:
    """The saturation loop: gen -> judge(not saturated) -> critique -> gen(existing+gaps) -> judge(SAT).
    Asserts round-2 expansion carries existing titles + the critique gaps, dedupe-by-title merge, the
    INDEPENDENT judge role, bounded rounds, and the fail-closed judge."""
    round1 = [
        {"id": "US-001", "title": "Sign up happy path", "persona": "new visitor", "category": "happy",
         "steps": ["open", "sign up"], "expected_outcome": "account created"},
        {"id": "US-002", "title": "Bad password rejected", "persona": "new visitor", "category": "error",
         "steps": ["open", "weak password"], "expected_outcome": "clear validation error"},
    ]
    round2 = [
        {"id": "US-001", "title": "SIGN UP  happy path", "persona": "x", "category": "happy",   # dup by title-key
         "steps": ["s"], "expected_outcome": "dup must be merged away"},
        {"id": "US-001", "title": "Abusive signup flood is throttled", "persona": "attacker",   # colliding id
         "category": "abuse", "steps": ["script 100 signups"], "expected_outcome": "rate limited"},
    ]
    calls = {"gen": 0, "critique": 0, "judge": 0}
    prompts = {"gen": [], "critique": [], "judge": []}

    def fake_agent(role, repo, task, **k):
        if "=== COVERAGE JUDGE ===" in task:
            calls["judge"] += 1
            prompts["judge"].append((role, task))
            sat = calls["judge"] >= 2
            return _ok_reply(json.dumps({"saturated": sat, "reason": "judged",
                                         "missing": [] if sat else ["signup abuse untested"]}))
        if "=== COVERAGE SELF-CRITIQUE ===" in task:
            calls["critique"] += 1
            prompts["critique"].append((role, task))
            return _ok_reply(json.dumps(["auth surface × attacker × abuse: signup flood untested",
                                         "dashboard × any × latency: slow-backend dwell untested"]))
        calls["gen"] += 1
        prompts["gen"].append((role, task))
        return _ok_reply(json.dumps(round1 if calls["gen"] == 1 else round2))

    factory.agent = fake_agent
    stories = saturate_stories("Signups must be safe.", "A signup web app.", max_rounds=5, persist=False)
    assert calls == {"gen": 2, "judge": 2, "critique": 1}, f"loop shape wrong: {calls}"
    assert len(stories) == 3, f"merge/dedupe wrong (expected 3, got {len(stories)}): " \
                              f"{[s['title'] for s in stories]}"
    sids = [s["id"] for s in stories]
    assert len(set(sids)) == 3, f"ids not unique after merge: {sids}"
    assert any(s["category"] == "abuse" for s in stories), "expansion story lost"
    # round-2 generation was steered: existing titles excluded + critique/judge gaps aimed
    g2 = prompts["gen"][1][1]
    assert "ALREADY-COVERED" in g2 and "Sign up happy path" in g2, "existing titles not passed to expansion"
    assert "KNOWN COVERAGE GAPS" in g2 and "signup flood untested" in g2, "critique gaps not steering expansion"
    assert "signup abuse untested" in g2, "judge's missing list not steering expansion"
    # the judge is INDEPENDENT: a different governed role than the enumerator
    jrole, grole = prompts["judge"][0][0], prompts["gen"][0][0]
    assert jrole == JUDGE_ROLE and grole == STORY_ROLE and jrole != grole, \
        f"judge must not be the enumerator: gen={grole} judge={jrole}"
    assert "did NOT write these stories" in prompts["judge"][0][1]
    # BOUNDED: a never-saturating judge stops at max_rounds
    calls.update(gen=0, critique=0, judge=0)
    prompts.update(gen=[], critique=[], judge=[])

    def never_sat(role, repo, task, **k):
        if "=== COVERAGE JUDGE ===" in task:
            calls["judge"] += 1
            return _ok_reply('{"saturated": false, "reason": "never", "missing": ["m"]}')
        if "=== COVERAGE SELF-CRITIQUE ===" in task:
            calls["critique"] += 1
            return _ok_reply('["g"]')
        calls["gen"] += 1
        return _ok_reply(json.dumps(round1))

    factory.agent = never_sat
    out = saturate_stories("v", "p", max_rounds=3, persist=False)
    assert calls["gen"] == 3 and calls["judge"] == 3 and calls["critique"] == 3, f"round bound broken: {calls}"
    assert len(out) == 2, "corpus should hold the union even when never saturated"
    # FAIL CLOSED: judge call failure / unparseable reply -> NOT saturated
    factory.agent = lambda *a, **k: {"rc": 1, "failed": True, "out": "", "out_full": ""}
    assert judge_saturation("v", "p", round1)["saturated"] is False, "failed judge must fail CLOSED"
    factory.agent = lambda *a, **k: _ok_reply("I think it is fine, ship it.")
    assert judge_saturation("v", "p", round1)["saturated"] is False, "unparseable judge must fail CLOSED"
    return stories


def _selftest_persistence(prod: str, stories: list):
    """REAL local Postgres: upsert round-trip, no duplicates, and cross-release corpus GROWTH."""
    n = save_stories(prod, stories)
    assert n == len(stories), f"save_stories wrote {n}, expected {len(stories)}"
    loaded = load_corpus(prod)
    assert len(loaded) == len(stories), f"round-trip lost stories: {len(loaded)} != {len(stories)}"
    for s in loaded:
        assert isinstance(s["steps"], list) and s["steps"], f"steps did not round-trip as a list: {s}"
        assert s["expected_outcome"], f"expected_outcome lost: {s}"
        assert s["source"] == "generated", s
    # UPSERT: saving again must not duplicate
    save_stories(prod, stories)
    assert len(load_corpus(prod)) == len(stories), "re-save duplicated corpus rows"

    # NEXT RELEASE: saturate again — the corpus preloads (grows), never regenerates from scratch.
    prompts = []

    def next_release(role, repo, task, **k):
        if "=== COVERAGE JUDGE ===" in task:
            return _ok_reply('{"saturated": true, "reason": "complete", "missing": []}')
        prompts.append(task)
        return _ok_reply(json.dumps([{"title": "Keyboard-only signup works", "persona": "a11y user",
                                      "category": "a11y", "steps": ["tab through signup"],
                                      "expected_outcome": "signup completes without a mouse"}]))

    factory.agent = next_release
    grown = saturate_stories("Signups must be safe.", "A signup web app.", product=prod, max_rounds=2)
    assert len(grown) == len(stories) + 1, f"corpus did not grow: {len(grown)}"
    assert "Sign up happy path" in prompts[0], "persisted corpus titles not excluded from regeneration"
    assert len(load_corpus(prod)) == len(stories) + 1, "grown corpus not persisted"


def _selftest_regression(prod: str):
    """Regression pinning: AI path, permanence vs generated upserts, and the grounded AI-failure fallback."""
    bug = {"bug": "sign-in button does nothing", "expected": "dashboard loads after sign-in",
           "url": "http://app/login", "action": {"cmd": "click", "idx": 0},
           "severity": "high", "blocking": True}
    seen = {}

    def pin_agent(role, repo, task, **k):
        seen["task"] = task
        return _ok_reply(json.dumps({"title": "Regression: sign-in button does nothing",
                                     "persona": "returning user",
                                     "steps": ["Go to http://app/login", "Click the sign-in button"],
                                     "expected_outcome": "The dashboard loads; the button is never inert."}))

    factory.agent = pin_agent
    st = add_regression_story(bug, product=prod, vision="Users sign in to a dashboard.")
    assert st["id"].startswith("REG-") and st["category"] == "regression" and st["source"] == "regression", st
    assert st["persisted"] is True, "regression pin must be persisted"
    assert "sign-in button does nothing" in seen["task"], "bug record not in the pin prompt"
    corpus = load_corpus(prod)
    reg = next(s for s in corpus if s["id"] == st["id"])
    assert reg["source"] == "regression" and reg["bug_ref"] and "sign-in" in reg["bug_ref"], reg
    # PERMANENCE: a later generated story with the same title can NEVER downgrade the pin
    save_stories(prod, [{"id": "US-999", "title": st["title"], "persona": "x", "category": "happy",
                         "steps": ["s"], "expected_outcome": "e"}], source="generated")
    reg2 = next(s for s in load_corpus(prod) if s["title"] == st["title"])
    assert reg2["source"] == "regression" and reg2["category"] == "regression" and reg2["id"] == st["id"], \
        f"regression pin was downgraded by a generated upsert: {reg2}"
    # GROUNDED FALLBACK: AI failure must still pin a story built from the observed bug record
    factory.agent = lambda *a, **k: {"rc": 1, "failed": True, "out": "", "out_full": ""}
    bug2 = {"bug": "assistant panel never opens", "expected": "assistant opens",
            "url": "http://app/home", "action": {"cmd": "click", "selector": "#assistant"}}
    st2 = add_regression_story(bug2, product=prod)
    assert st2["persisted"] is True and st2["steps"] and st2["expected_outcome"] == "assistant opens", st2
    assert any("http://app/home" in x for x in st2["steps"]), f"fallback steps not grounded in the bug: {st2}"
    assert any(s["id"] == st2["id"] and s["source"] == "regression" for s in load_corpus(prod)), \
        "fallback pin not in the corpus"


def _selftest():
    """AI mocked throughout (no spend/network); persistence hits the REAL local Postgres under a unique
    throwaway product slug (evidence, not claims), cleaned up in finally. Restores factory.agent."""
    real_agent, real_products = factory.agent, factory.PRODUCTS
    factory.PRODUCTS = Path("/nonexistent-selftest")   # never touched (tools=[], reply-only)
    prod = f"selftest-storycorpus-{os.urandom(3).hex()}"
    ok = False
    try:
        captured = {}
        _selftest_generation(captured)
        merged = _selftest_saturation()
        if _dsn() is None:
            raise AssertionError("no DATABASE_URL — the story_corpus persistence contract is UNVERIFIED")
        _selftest_persistence(prod, merged)
        _selftest_regression(prod)
        ok = True
        print("PASS: story_gen — enumeration contract, SATURATION loop (self-critique + independent "
              "judge, bounded, fail-closed), Postgres story_corpus persistence (upsert + cross-release "
              "growth), and permanent regression pinning all verified ✅")
    except AssertionError as e:
        print(f"FAIL: {e}")
    finally:
        factory.agent, factory.PRODUCTS = real_agent, real_products
        dsn = _dsn()
        if dsn:
            try:
                with psycopg.connect(dsn) as c, c.cursor() as cur:
                    cur.execute("DELETE FROM story_corpus WHERE product = %s", (prod,))
                    c.commit()
            except Exception as e:
                print(f"[story_gen] selftest cleanup warning: {e}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "corpus" and len(a) > 1:
        print(json.dumps(load_corpus(a[1]), indent=2))
    else:
        sys.exit("usage: story_gen.py [selftest | corpus <product>]")
