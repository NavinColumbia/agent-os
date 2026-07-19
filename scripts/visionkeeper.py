#!/usr/bin/env python3
"""visionkeeper.py — the CEO's REQUIREMENTS-PROVIDER agent (a self-refining personal chief-of-staff).

North Star premise: "every user is a CEO; AI agents do ALL the work." A CEO should NOT have to hand the
controller a spec sheet each time — that defeats the premise and reads as pestering. This module is the
standing agent that HOLDS the CEO's vision and keeps REFINING the requirements itself, so a vague prompt
("build a YouTube competitor") is enough: the controller picks up a rich, refined requirements spec — goals,
non-goals, the quality bar, the next capabilities, and crucially the UPFRONT human prerequisites (creds,
accounts, ~credit/$, legal/compliance, approvals/emails) — instead of interrupting the CEO mid-build.

Two scopes:
  meta          the CEO's overall directive (multiple companies, agents do the work, keep refining, quality,
                ownership, rich comms). Refining it also maintains docs/SYSTEM-REQUIREMENTS.md (the living
                spec the controller/roadmap answer to, alongside the fixed docs/NORTH-STAR.md constitution).
  org:<id>      one company/product: the CEO's vague vision refined into concrete build requirements.

    visionkeeper.py get [scope]                 # current vision + refined requirements
    visionkeeper.py refine [scope] [--org N]    # run the refiner agent (spends: one factory.agent call)
    visionkeeper.py show                         # the living SYSTEM-REQUIREMENTS.md
    visionkeeper.py selftest                     # OFFLINE (factory.agent stubbed), real Postgres

The controller seam is requirements_for_controller(tenant, org_id, hint) — returns the refined spec (refining
on demand if missing/stale) so a build starts from the CEO's intent, not a blank page.
"""
import json
import os
import sys
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from aoscfg import DB  # noqa: E402

_REPO = SCRIPTS.parent
_REQ_DOC = _REPO / "docs" / "SYSTEM-REQUIREMENTS.md"
STALE_S = int(os.environ.get("AOS_VISION_STALE_S", "86400"))     # refine on read if older than a day

# The CEO's STANDING directive — seeded once, editable. This is the vision the refiner always answers to so
# the CEO never has to restate it. (Sourced from the owner's own words.)
SEED_VISION = (
    "I am a CEO who wants to run MULTIPLE companies and products at once, with AI agents — not humans — doing "
    "ALL the work. The agents should continuously REFINE the requirements themselves, OWN their work end to "
    "end, coordinate through rich, human-like communication, and hold a quality bar that ASTONISHES a skeptic "
    "(zero bugs reach a human; nothing fails invisibly). Humans (me) should be involved ONLY where absolutely "
    "necessary, and the system must tell me UP FRONT everything it needs from me — credentials, accounts, "
    "credit/budget, legal/compliance, and any approvals or emails — rather than interrupting me mid-build."
)

# The shape the refiner must return — a spec the controller can build from, and that names the human touch
# points up front (the thing the CEO explicitly asked for).
_REFINE_SYS = (
    "You are the CEO's CHIEF OF STAFF and requirements owner. Given the CEO's standing VISION, the current "
    "REQUIREMENTS, the fixed NORTH STAR, and the SYSTEM STATE, produce a SHARPER, more complete requirements "
    "spec — think like a world-class product+program leader anticipating what the CEO would want without "
    "being told. Refine, don't just restate. Reply ONLY with JSON:\n"
    '{"refined_vision":"<1-3 sentences>",'
    '"goals":["..."],"non_goals":["..."],'
    '"quality_bar":["concrete, testable bars"],'
    '"prerequisites":[{"item":"what the CEO must provide/do","kind":"credential|account|budget|legal|approval|email|other","why":"...","when":"before_start|before_launch|as_needed"}],'
    '"next_capabilities":["the most valuable things to build next, sharpest first"],'
    '"open_questions":["only genuinely CEO-level questions; keep few — infer the rest"]}'
)


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS ceo_vision (
                         tenant_id    TEXT NOT NULL,
                         scope        TEXT NOT NULL,
                         vision       TEXT NOT NULL DEFAULT '',
                         requirements JSONB,
                         updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                         PRIMARY KEY (tenant_id, scope))""")
        c.commit()


def _parse_json(text):
    """Robustly pull the JSON object out of an agent reply (tolerates code fences / prose around it)."""
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
        if m:
            t = m.group(1)
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(t[a:b + 1])
        except Exception:
            return None
    return None


def get(tenant, scope="meta"):
    """Current vision + refined requirements for a scope. Seeds the meta vision from SEED_VISION on first use
    so the CEO's directive is always present without being restated. Returns {vision, requirements, age_s}."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT vision, requirements, EXTRACT(EPOCH FROM now()-updated_at)::INT
                       FROM ceo_vision WHERE tenant_id=%s AND scope=%s""", (tenant, scope))
        row = cur.fetchone()
        if not row and scope == "meta":
            cur.execute("""INSERT INTO ceo_vision (tenant_id, scope, vision) VALUES (%s,'meta',%s)
                           ON CONFLICT DO NOTHING""", (tenant, SEED_VISION))
            c.commit()
            return {"vision": SEED_VISION, "requirements": None, "age_s": None}
    if not row:
        return {"vision": "", "requirements": None, "age_s": None}
    return {"vision": row[0], "requirements": row[1], "age_s": row[2]}


def set_vision(tenant, vision, scope="meta"):
    """Set the raw vision for a scope (the CEO's words, or an org's vague product idea). Refine turns it into
    a requirements spec — this just stores intent."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO ceo_vision (tenant_id, scope, vision, updated_at)
                       VALUES (%s,%s,%s, now()) ON CONFLICT (tenant_id, scope)
                       DO UPDATE SET vision=EXCLUDED.vision, updated_at=now()""", (tenant, scope, vision))
        c.commit()
    return {"tenant": tenant, "scope": scope, "vision": vision}


def _system_state():
    """A GROUNDED snapshot for the refiner: the North Star (constitution) + the current living requirements +
    the audit's open gaps, truncated. Best-effort — a missing file is a note, never a crash."""
    def _read(p, n):
        try:
            return (_REPO / p).read_text()[:n]
        except Exception:
            return f"({p} unavailable)"
    return {"north_star": _read("docs/NORTH-STAR.md", 2500),
            "living_requirements": _read("docs/SYSTEM-REQUIREMENTS.md", 2500),
            "open_gaps": _read("docs/SYSTEM-AUDIT-2026-07.md", 2500)}


def refine(tenant, scope="meta", org_id=None, hint=None, api_key=None):
    """Run the requirements-owner AGENT: read the vision + current requirements + (for meta) the North Star &
    system state, and produce a SHARPER requirements spec (goals/non-goals/quality bar/UPFRONT prerequisites/
    next capabilities). Persists it; for meta also rewrites docs/SYSTEM-REQUIREMENTS.md. One factory.agent
    call (spends). Returns the requirements dict. Fail-soft: keeps the prior requirements on a parse miss."""
    import factory
    cur_scope = scope if scope != "org" else f"org:{org_id}"
    st = get(tenant, cur_scope if cur_scope.startswith("org:") else "meta")
    vision = st["vision"] or SEED_VISION
    ctx = {"vision": vision, "current_requirements": st.get("requirements"), "hint": hint}
    if cur_scope == "meta":
        ctx.update(_system_state())
    else:
        ctx["scope"] = cur_scope
    if api_key is not None:
        factory._ctx.api_key = api_key
    factory._ctx.tenant = tenant if tenant not in (None, "platform") else None
    prompt = _REFINE_SYS + "\n\nCONTEXT:\n" + json.dumps(ctx, default=str)[:8000]
    # light=True: a constrained, tool-free reasoning call — no WebSearch/file-edit tools to derail a
    # "reply ONLY JSON" instruction, and cheaper/faster than a full heavy agent for a synthesis task.
    res = factory.agent("chief-of-staff", str(_REPO), prompt, light=True)
    raw = (res.get("out_full") or res.get("out") or "") if isinstance(res, dict) else str(res)  # full, not preview
    req = _parse_json(raw)
    if not req or not req.get("goals"):
        # FAIL-SOFT: a parse miss must NEVER destroy a previously-good spec. Keep the prior requirements +
        # doc untouched; just record the error (with a raw snippet to diagnose) and return what we had.
        prior = st.get("requirements")
        _ensure()
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""INSERT INTO ceo_vision (tenant_id, scope, vision, requirements, updated_at)
                           VALUES (%s,%s,%s,%s, now()) ON CONFLICT (tenant_id, scope) DO UPDATE
                           SET requirements=COALESCE(ceo_vision.requirements, EXCLUDED.requirements),
                               updated_at=now()""",
                        (tenant, cur_scope, vision,
                         json.dumps(prior or {"_last_refine_error": "unparseable reply", "_raw": raw[:400]})))
            c.commit()
        return prior or {"_last_refine_error": "agent reply was not parseable JSON", "_raw": raw[:400]}
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO ceo_vision (tenant_id, scope, vision, requirements, updated_at)
                       VALUES (%s,%s,%s,%s, now()) ON CONFLICT (tenant_id, scope)
                       DO UPDATE SET requirements=EXCLUDED.requirements, updated_at=now()""",
                    (tenant, cur_scope, vision, json.dumps(req)))
        c.commit()
    if cur_scope == "meta":
        _write_requirements_doc(vision, req)
    return req


def _write_requirements_doc(vision, req):
    """Maintain docs/SYSTEM-REQUIREMENTS.md — the LIVING system spec the refiner owns (the North Star stays
    the fixed constitution; this is the evolving requirements the controller/roadmap build toward)."""
    def _bullets(xs):
        return "\n".join(f"- {x}" for x in (xs or [])) or "- (none yet)"
    pre = req.get("prerequisites") or []
    pre_lines = "\n".join(
        f"- **{p.get('item','?')}** ({p.get('kind','other')}, {p.get('when','as_needed')}) — {p.get('why','')}"
        for p in pre) or "- (none identified yet)"
    body = f"""# System Requirements (living — maintained by visionkeeper)

> Auto-refined by the CEO's requirements-provider agent. The fixed constitution is `NORTH-STAR.md`; this is the
> evolving spec the controller and roadmap answer to. Do not hand-edit the sections below — run
> `visionkeeper.py refine` (or it refines on demand).

## CEO vision (standing)
{vision}

## Refined vision
{req.get('refined_vision','(not yet refined)')}

## Goals
{_bullets(req.get('goals'))}

## Non-goals
{_bullets(req.get('non_goals'))}

## Quality bar
{_bullets(req.get('quality_bar'))}

## What the system needs from the CEO (up front)
{pre_lines}

## Next capabilities (sharpest first)
{_bullets(req.get('next_capabilities'))}

## Open questions (CEO-level only)
{_bullets(req.get('open_questions'))}
"""
    try:
        _REQ_DOC.write_text(body)
    except Exception:
        pass


def requirements_for_controller(tenant, org_id=None, hint=None, api_key=None, allow_refine=True):
    """THE CONTROLLER SEAM: return the refined requirements a build should start from, so a vague CEO prompt is
    enough. Uses the org's refined spec if fresh; refines on demand when missing/stale (unless allow_refine is
    False, e.g. an offline path). Always returns the CEO's standing meta-vision alongside, so the controller
    has both the specific intent and the standing directive without asking the CEO to restate either."""
    scope = f"org:{org_id}" if org_id is not None else "meta"
    st = get(tenant, scope)
    stale = st.get("requirements") is None or (st.get("age_s") or 0) > STALE_S
    if hint and not st.get("vision"):
        set_vision(tenant, hint, scope=scope)
    if allow_refine and stale:
        try:
            reqs = refine(tenant, scope="org" if org_id is not None else "meta", org_id=org_id,
                          hint=hint, api_key=api_key)
        except Exception:
            reqs = st.get("requirements")
    else:
        reqs = st.get("requirements")
    meta = get(tenant, "meta")
    return {"scope": scope, "standing_vision": meta["vision"], "requirements": reqs,
            "vision": st.get("vision") or hint or ""}


def _selftest():
    import uuid
    import factory
    tid = f"vk-selftest-{uuid.uuid4().hex[:8]}"
    real_agent = factory.agent
    calls = {"n": 0}

    def fake_agent(role, repo, task, **k):
        calls["n"] += 1
        assert role == "chief-of-staff", role
        assert "YouTube" in task or "CEO" in task or "VISION" in task.upper()   # the vision reached the agent
        return {"rc": 0, "out": json.dumps({
            "refined_vision": "A resilient multi-company AI-agent operator.",
            "goals": ["one-prompt company creation", "agents own the work"],
            "non_goals": ["human micromanagement"],
            "quality_bar": ["zero bugs reach a human", "nothing fails invisibly"],
            "prerequisites": [
                {"item": "Connect an Anthropic or OpenAI provider", "kind": "credential",
                 "why": "agents need a model to run on", "when": "before_start"},
                {"item": "Approx $40 in model credit for the first build", "kind": "budget",
                 "why": "research+build+QA spend", "when": "before_start"},
                {"item": "A Google Play / App Store developer account", "kind": "account",
                 "why": "to publish the app", "when": "before_launch"}],
            "next_capabilities": ["upfront prerequisites brief", "wire the living org"],
            "open_questions": ["Which company should agents prioritize first?"]})}

    factory.agent = fake_agent
    ok = False
    try:
        # 1) meta seeds the standing vision without being told
        m = get(tid, "meta")
        seeded = "CEO" in m["vision"] and "ALL the work" in m["vision"] and m["requirements"] is None

        # 2) refine meta -> structured requirements incl. UPFRONT prerequisites; living doc written
        req = refine(tid, "meta")
        pres = {p["kind"] for p in req.get("prerequisites", [])}
        refined_ok = (req.get("refined_vision") and "before_start" in
                      {p["when"] for p in req["prerequisites"]} and {"credential", "budget", "account"} <= pres)
        doc_ok = _REQ_DOC.exists() and "What the system needs from the CEO" in _REQ_DOC.read_text()

        # 3) an org's vague idea -> the controller seam returns a build-ready spec WITHOUT the CEO restating it
        seam = requirements_for_controller(tid, org_id=7, hint="build a YouTube competitor app")
        seam_ok = (seam["requirements"] and seam["standing_vision"] and seam["scope"] == "org:7"
                   and seam["requirements"].get("goals"))

        # 4) idempotent-ish persistence: get returns the refined requirements now
        got = get(tid, "meta")
        persist_ok = got["requirements"] and got["requirements"].get("goals")

        ok = seeded and refined_ok and doc_ok and seam_ok and persist_ok and calls["n"] >= 2
        print(f"seed_meta={seeded} refine+prereqs={refined_ok} living_doc={doc_ok} "
              f"controller_seam={seam_ok} persisted={bool(persist_ok)} agent_calls={calls['n']}")
        print("PASS: visionkeeper — holds the CEO's standing vision, refines requirements incl. UPFRONT "
              "prerequisites, maintains the living spec, and feeds the controller from a vague prompt ✅"
              if ok else "FAIL")
    finally:
        factory.agent = real_agent
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM ceo_vision WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "get":
        print(json.dumps(get(os.environ.get("AOS_TENANT", "platform"),
                              a[1] if len(a) > 1 else "meta"), indent=2, default=str))
    elif a[0] == "refine":
        scope = a[1] if len(a) > 1 and not a[1].startswith("--") else "meta"
        org = None
        if "--org" in a:
            org = a[a.index("--org") + 1]; scope = "org"
        print(json.dumps(refine(os.environ.get("AOS_TENANT", "platform"), scope, org_id=org), indent=2, default=str))
    elif a[0] == "show":
        print(_REQ_DOC.read_text() if _REQ_DOC.exists() else "(no SYSTEM-REQUIREMENTS.md yet — run: refine)")
    elif a[0] == "selftest":
        _selftest()
    else:
        sys.exit("usage: visionkeeper.py get [scope] | refine [scope] [--org N] | show | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
