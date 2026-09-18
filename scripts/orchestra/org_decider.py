#!/usr/bin/env python3
"""org_decider.py — the ELASTIC-SCALING BRAIN of the event-driven agent-org (see
docs/blueprint/AGENTIC-ORCHESTRATION.md § "Recursive, elastic org scaling").

The org tree is NOT fixed-depth: any supervisor can spawn sub-supervisors, a sub-team, or more
agents. This module is the AI that proposes that structural call. Deterministic normalization keeps
an ambiguous or malformed proposal from silently creating paid work:

  should_expand(scope, current_structure, signals) -> {expand, how, detail, rationale}
      Called when material scope/load signals suggest the current team may be insufficient. Expansion
      needs an explicit, coherent proposal; ambiguity, an unparseable reply, or a failed call safely
      retains the current organization for re-evaluation.

  plan_org(vision) -> a recursive org tree (controller -> domains -> teams -> devs) for a vision.
      Decomposes a large vision into a DEEP, nested structure; normalized so callers always get a
      well-formed nested tree they can walk + spawn from.

Design notes:
- factory.agent(role, repo, task) returns {"rc", "out", "out_full", ...}; we read the model's JSON
  out of out_full/out robustly (tolerant of ```json fences and surrounding prose).
- OFFLINE selftest (`python org_decider.py [selftest]`) monkeypatches factory.agent to a stub, so it
  is deterministic + free. No DB, no model, no network.
"""
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import factory  # noqa: E402  — factory.agent is THE LLM call (retries 529/failover built in)

# The role we spawn these structural-reasoning agents under. "controller" is the top escalation
# tier in the org (it owns org-shape decisions) and has a manifest with can_spawn.
DECIDER_ROLE = "controller"
# A neutral repo for the decision agent to run in (these calls produce JSON, not files).
_REPO = str(SCRIPTS.parent)

HOW_KINDS = ("new_supervisor", "sub_team", "more_agents", "none")


# --------------------------------------------------------------------------------------------------
# robust model-JSON extraction
# --------------------------------------------------------------------------------------------------
def _text_of(res: dict) -> str:
    """The fullest model text available from a factory.agent result."""
    if not isinstance(res, dict):
        return str(res or "")
    return (res.get("out_full") or res.get("out") or "").strip()


def _extract_json(text: str):
    """Pull the first JSON object out of a model reply. Tolerant of ```json fences and prose around
    the object. Returns a dict, or None if nothing parses."""
    if not text:
        return None
    # 1) fenced block ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    candidates = [m.group(1)] if m else []
    # 2) the whole thing, then the widest {...} span (greedy) as a fallback.
    candidates.append(text)
    m2 = re.search(r"\{.*\}", text, re.S)
    if m2:
        candidates.append(m2.group(0))
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _ask(prompt: str, repo: str = None) -> dict:
    """One structural-decision AI call. Returns the parsed dict or None (caller decides the default)."""
    res = factory.agent(DECIDER_ROLE, repo or _REPO, prompt)
    return _extract_json(_text_of(res))


# --------------------------------------------------------------------------------------------------
# should_expand — the elastic-scaling decision, called at EVERY decision point
# --------------------------------------------------------------------------------------------------
_EXPAND_PROMPT = """You are the ORG-STRUCTURE decision maker for a recursive, self-organizing fleet
of AI agents (a company that grows its own org chart). At every decision point we ask: is the CURRENT
team structure sufficient for this scope, or do we EXPAND it?

You may expand in one of these ways (field `how`):
- "new_supervisor" : add a supervisor / sub-supervisor over a new domain (grow DEPTH).
- "sub_team"       : add a sub-team (a head + its own devs) under an existing supervisor.
- "more_agents"    : add more individual-contributor agents to an existing team (grow WIDTH).
- "none"           : the current structure is genuinely sufficient; do NOT expand.

Choose the SMALLEST sufficient organization. Expand only when observed scope/load/blocker signals show
that a distinct domain, independently governed team, or genuinely low-coupling parallel worker will
produce a concrete coverage, quality, or latency benefit. Coordination, context fragmentation, and
model spend are real failure surfaces. When evidence is insufficient, answer "none" and explain what
signal should trigger re-evaluation; never speculate by adding staff.

SCOPE (what we're taking on now):
{scope}

CURRENT STRUCTURE (the team as it stands):
{structure}

SIGNALS (events/load/blockers observed — e.g. blocked children, backlog, breadth of work):
{signals}

Reply with ONE JSON object and nothing else:
{{"expand": true|false,
  "how": "new_supervisor"|"sub_team"|"more_agents"|"none",
  "detail": "<concrete: what to spawn, e.g. 'Head of Payments-Fraud + 3 devs'>",
  "rationale": "<one or two sentences>"}}"""


def should_expand(scope, current_structure, signals=None) -> dict:
    """Ask the AI whether the org should grow to meet `scope`, given `current_structure` and live
    `signals`. Returns a normalized decision dict:
        {expand: bool, how: 'new_supervisor'|'sub_team'|'more_agents'|'none', detail, rationale}
    Ambiguous, missing, inconsistent, or unparseable replies retain the current org."""
    prompt = _EXPAND_PROMPT.format(
        scope=_as_text(scope),
        structure=_as_text(current_structure),
        signals=_as_text(signals) if signals else "(none reported)",
    )
    parsed = _ask(prompt)
    return _normalize_decision(parsed)


def _normalize_decision(parsed) -> dict:
    """Coerce the proposal into a fail-closed organizational decision."""
    if not isinstance(parsed, dict):
        return {"expand": False, "how": "none", "detail": "",
                "rationale": "ambiguous/unparseable decision — retaining current organization",
                "ambiguous": True}
    raw_expand = parsed.get("expand", None)
    how = str(parsed.get("how", "") or "").strip().lower()
    detail = str(parsed.get("detail", "") or "").strip()
    rationale = str(parsed.get("rationale", "") or "").strip()

    expand = raw_expand if isinstance(raw_expand, bool) else False
    ambiguous = not isinstance(raw_expand, bool)
    if how not in HOW_KINDS:
        ambiguous = True
        expand, how = False, "none"
    elif (expand and how == "none") or (not expand and how != "none"):
        ambiguous = True
        expand, how = False, "none"
    if expand and (not detail or not rationale):
        ambiguous = True
        expand, how = False, "none"
        rationale = "incomplete expansion proposal — retaining current organization"
    if not rationale:
        rationale = "no rationale given — retaining current organization"
    return {"expand": expand, "how": how, "detail": detail, "rationale": rationale,
            "ambiguous": ambiguous}


# --------------------------------------------------------------------------------------------------
# plan_org — decompose a vision into an initial recursive org tree
# --------------------------------------------------------------------------------------------------
_PLAN_PROMPT = """You are the ORG ARCHITECT for a self-organizing fleet of AI agents. Turn the VISION
below into an INITIAL org tree that can build it. The tree is RECURSIVE and DEEP:

  controller (root)
    -> DOMAINS      (major areas; each has a domain SUPERVISOR)
       -> TEAMS     (each has a HEAD and can itself contain sub-teams for a big domain)
          -> DEVS   (individual-contributor agents; each has a role like backend-engineer)

Choose the SMALLEST organization that still has explicit ownership and independent verification.
Split domains only when their work, authority, or evidence can be independently owned. Add parallel
teams only for low-coupling work with a concrete coverage, quality, or latency benefit; needless
hierarchy is a reliability and coordination defect. Give every node a role/title.

VISION:
{vision}

Reply with ONE JSON object and nothing else, shaped like:
{{"vision": "<echo the vision>",
  "root": {{"kind": "controller", "title": "Controller",
    "children": [
      {{"kind": "domain", "name": "<domain>", "supervisor": "<role>",
        "teams": [
          {{"kind": "team", "name": "<team>", "head": "<role>",
            "devs": [{{"kind": "dev", "role": "<role>", "name": "<name>"}}],
            "teams": []  // optional nested sub-teams for a broad domain
          }}
        ]}}
    ]}}}}"""


def plan_org(vision) -> dict:
    """Ask the AI to decompose `vision` into an initial recursive org tree
    (controller -> domains -> teams -> devs). Returns a NORMALIZED, always well-formed nested tree:
        {vision, root: {kind:'controller', children:[ {kind:'domain', teams:[ {kind:'team',
         devs:[...], teams:[...] } ]} ]}}
    If the model reply doesn't parse, a minimal single-domain nested tree is returned so callers
    always get a walkable structure."""
    parsed = _ask(_PLAN_PROMPT.format(vision=_as_text(vision)))
    return _normalize_tree(parsed, vision)


def _normalize_tree(parsed, vision) -> dict:
    """Coerce the model's tree into the nested contract. Missing/broken -> a minimal nested tree so
    downstream org walkers never crash. Guarantees controller -> domain(s) -> team(s) -> dev(s)."""
    vtext = _as_text(vision)
    if not isinstance(parsed, dict):
        return _fallback_tree(vtext)
    root = parsed.get("root") if isinstance(parsed.get("root"), dict) else parsed
    domains = root.get("children") if isinstance(root, dict) else None
    if not isinstance(domains, list) or not domains:
        return _fallback_tree(vtext)

    norm_domains = [_norm_domain(d) for d in domains if isinstance(d, dict)]
    norm_domains = [d for d in norm_domains if d]
    if not norm_domains:
        return _fallback_tree(vtext)
    return {"vision": parsed.get("vision") or vtext,
            "root": {"kind": "controller",
                     "title": (root.get("title") if isinstance(root, dict) else None) or "Controller",
                     "children": norm_domains}}


def _norm_domain(d: dict) -> dict:
    teams = d.get("teams")
    teams = teams if isinstance(teams, list) else []
    norm_teams = [_norm_team(t) for t in teams if isinstance(t, dict)]
    norm_teams = [t for t in norm_teams if t]
    if not norm_teams:  # a domain with no teams still gets one team so the tree stays nested
        norm_teams = [_norm_team({"name": (d.get("name") or "team")})]
    return {"kind": "domain",
            "name": str(d.get("name") or "domain"),
            "supervisor": str(d.get("supervisor") or "supervisor"),
            "teams": norm_teams}


def _norm_team(t: dict) -> dict:
    devs = t.get("devs")
    devs = devs if isinstance(devs, list) else []
    norm_devs = []
    for dev in devs:
        if isinstance(dev, dict):
            norm_devs.append({"kind": "dev",
                              "role": str(dev.get("role") or "engineer"),
                              "name": str(dev.get("name") or dev.get("role") or "dev")})
        elif isinstance(dev, str):
            norm_devs.append({"kind": "dev", "role": dev, "name": dev})
    if not norm_devs:
        norm_devs = [{"kind": "dev", "role": "engineer", "name": "dev-1"}]
    # optional nested sub-teams (recursion) — keep the tree elastic in depth.
    subteams = t.get("teams")
    norm_sub = []
    if isinstance(subteams, list):
        norm_sub = [x for x in (_norm_team(s) for s in subteams if isinstance(s, dict)) if x]
    return {"kind": "team",
            "name": str(t.get("name") or "team"),
            "head": str(t.get("head") or "team-head"),
            "devs": norm_devs,
            "teams": norm_sub}


def _fallback_tree(vtext: str) -> dict:
    """A minimal but well-formed nested tree, used only when the model reply can't be parsed."""
    return {"vision": vtext,
            "root": {"kind": "controller", "title": "Controller", "children": [
                {"kind": "domain", "name": "core", "supervisor": "supervisor", "teams": [
                    {"kind": "team", "name": "build", "head": "team-head",
                     "devs": [{"kind": "dev", "role": "fullstack-engineer", "name": "dev-1"}],
                     "teams": []}
                ]}
            ]}}


# --------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------
def _as_text(x) -> str:
    """Render scope/structure/signals (str | dict | list) as compact text for the prompt."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    try:
        return json.dumps(x, indent=2, default=str)
    except Exception:
        return str(x)


def tree_depth(node: dict) -> int:
    """Max nesting depth of an org tree (controller=1, domain=2, team=3, dev=4, sub-teams deeper).
    Handy for callers/tests to confirm the tree actually nests."""
    if not isinstance(node, dict):
        return 0
    root = node.get("root", node)
    return _depth(root)


def _depth(n: dict) -> int:
    if not isinstance(n, dict):
        return 0
    kids = []
    for key in ("children", "teams", "devs"):
        v = n.get(key)
        if isinstance(v, list):
            kids.extend(x for x in v if isinstance(x, dict))
    if not kids:
        return 1
    return 1 + max(_depth(k) for k in kids)


# --------------------------------------------------------------------------------------------------
# OFFLINE selftest — monkeypatch factory.agent; deterministic + free (no DB/model/network)
# --------------------------------------------------------------------------------------------------
def _selftest() -> int:
    real_agent = factory.agent
    ok = True

    def check(cond, label):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + f": {label}")
        ok = ok and bool(cond)

    try:
        # ---- should_expand: model returns a clean expansion decision ----
        def stub_expand(role, repo, task, **k):
            return {"rc": 0, "out": '```json\n{"expand": true, "how": "new_supervisor", '
                    '"detail": "Head of Payments-Fraud + 3 devs", "rationale": "breadth growing"}\n```'}
        factory.agent = stub_expand
        d = should_expand("ship a fintech suite", {"teams": ["pay", "wallet"]},
                          {"blocked": 2, "backlog": 40})
        check(d["expand"] is True and d["how"] == "new_supervisor"
              and "Payments-Fraud" in d["detail"] and d["rationale"], "should_expand parses decision")

        # ---- should_expand: a confident 'no' is honored ----
        def stub_no(role, repo, task, **k):
            return {"rc": 0, "out": '{"expand": false, "how": "none", "detail": "", '
                    '"rationale": "team already covers scope with headroom"}'}
        factory.agent = stub_no
        d = should_expand("tiny tweak", {"teams": ["core"]}, None)
        check(d["expand"] is False and d["how"] == "none", "should_expand honors confident 'none'")

        # ---- should_expand: ambiguity cannot silently create paid work ----
        def stub_garbage(role, repo, task, **k):
            return {"rc": 0, "out": "hmm, I think maybe we could add some people? unclear."}
        factory.agent = stub_garbage
        d = should_expand("unclear scope", {}, None)
        check(d["expand"] is False and d["how"] == "none" and d.get("ambiguous"),
              "should_expand retains current org under ambiguity")

        # ---- should_expand: inconsistent reply fails closed ----
        def stub_inconsistent(role, repo, task, **k):
            return {"rc": 0, "out": '{"expand": true, "how": "none"}'}
        factory.agent = stub_inconsistent
        d = should_expand("x", {}, None)
        check(d["expand"] is False and d["how"] == "none" and d.get("ambiguous"),
              "should_expand rejects expand+none")

        # ---- plan_org: model returns a nested tree ----
        def stub_plan(role, repo, task, **k):
            tree = {"vision": "google-scale fintech suite",
                    "root": {"kind": "controller", "title": "Controller", "children": [
                        {"kind": "domain", "name": "fintech", "supervisor": "eng-director", "teams": [
                            {"kind": "team", "name": "Pay", "head": "head-of-pay",
                             "devs": [{"kind": "dev", "role": "backend-engineer", "name": "d1"},
                                      {"kind": "dev", "role": "backend-engineer", "name": "d2"}],
                             "teams": [{"kind": "team", "name": "Pay-Fraud", "head": "head-fraud",
                                        "devs": [{"role": "data-scientist", "name": "d3"}]}]},
                            {"kind": "team", "name": "Wallet", "head": "head-of-wallet",
                             "devs": [{"kind": "dev", "role": "backend-engineer", "name": "d4"}]}]},
                        {"kind": "domain", "name": "platform", "supervisor": "sre-lead", "teams": [
                            {"kind": "team", "name": "Infra", "head": "head-infra",
                             "devs": [{"role": "devops-sre", "name": "d5"}]}]}]}}
            return {"rc": 0, "out_full": "here is the org:\n" + json.dumps(tree)}
        factory.agent = stub_plan
        org = plan_org("google-scale fintech suite")
        root = org["root"]
        domains = root["children"]
        nested = (root["kind"] == "controller" and len(domains) == 2
                  and all(dom["teams"] for dom in domains)
                  and all(team["devs"] for dom in domains for team in dom["teams"]))
        check(nested, "plan_org yields a nested domains->teams->devs tree")
        # sub-team nesting preserved -> depth exceeds the flat 4 (controller/domain/team/dev)
        check(tree_depth(org) >= 5, "plan_org preserves nested sub-teams (recursive depth)")
        # nested dev given as bare role dict got normalized
        pay = domains[0]["teams"][0]
        check(pay["teams"] and pay["teams"][0]["devs"][0]["role"] == "data-scientist",
              "plan_org normalizes nested sub-team devs")

        # ---- plan_org: unparseable reply -> minimal nested fallback tree ----
        def stub_plan_bad(role, repo, task, **k):
            return {"rc": 0, "out": "I could not produce JSON, sorry."}
        factory.agent = stub_plan_bad
        org2 = plan_org("something")
        check(org2["root"]["children"][0]["teams"][0]["devs"], "plan_org falls back to a nested tree")

    finally:
        factory.agent = real_agent

    print("\n" + ("ALL PASS ✅" if ok else "FAILURES ❌"))
    return 0 if ok else 1


def _main(argv):
    if not argv or argv[0] == "selftest":
        return _selftest()
    if argv[0] == "plan":
        print(json.dumps(plan_org(argv[1] if len(argv) > 1 else "a simple app"), indent=2))
        return 0
    print("usage: org_decider.py [selftest] | plan \"<vision>\"", file=sys.stderr)
    return 2


# Public API
__all__ = ["should_expand", "plan_org", "tree_depth", "HOW_KINDS", "DECIDER_ROLE"]


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
