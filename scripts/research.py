#!/usr/bin/env python3
"""research.py — an async STATE + OPTIONS layer over the research fleet.

The non-technical CEO doesn't want a wall of report text — they want to "give me time to research"
and come back to a handful of SELECTABLE option cards ("which strategy should I pick?"). This wraps
research_fleet (decompose -> parallel fleet -> synthesize) with durable run state and a distillation
step: once the report lands, a research-growth agent reads it and proposes 3 DISTINCT strategic
options, one marked recommended. The controller starts a run, polls run_state, then select()s.

ENGINES (REBUILD-PLAN A1): with AOS_ORCHESTRA on (the DEFAULT), the run executes as a durable
ORCHESTRA org run (scripts/orchestra/research_org.py: controller actor -> research supervisor ->
N child researchers as Postgres rows, events on the persisted bus, heartbeats for sentinel) —
orchestra IS the engine with this as its production caller. Flag off (AOS_ORCHESTRA=0) falls back
to the legacy in-process fleet. Either way the output contract here (research_runs / report /
options) is identical, so the console UX never changes.

    research.py json <tenant_id> <run_id>     # the run state + option cards
    research.py selftest
Run with the agent-os venv python. No web server — DB + a daemon thread, like the orchestrator.
"""
import os
import sys
import threading
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "orchestra"))
import audit          # noqa: E402
import factory        # noqa: E402
import research_fleet  # noqa: E402
import research_org   # noqa: E402  — the durable ORCHESTRA org-run engine (default)


def orchestra_on():
    """AOS_ORCHESTRA — default ON: research runs as a durable orchestra org run. Set
    AOS_ORCHESTRA=0 to fall back to the legacy in-process research_fleet path."""
    return os.environ.get("AOS_ORCHESTRA", "1").strip().lower() not in ("0", "false", "off", "no")

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS research_runs (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT, org_id TEXT, thread_id BIGINT,
            question TEXT, status TEXT DEFAULT 'running', report_path TEXT,
            started_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS research_options (
            id BIGSERIAL PRIMARY KEY, run_id BIGINT, title TEXT, summary TEXT,
            recommended BOOLEAN DEFAULT false, chosen BOOLEAN DEFAULT false)""")
        c.commit()


def start(tenant_id, org_id, thread_id, question, api_key=None, engine=None):
    """Insert a research_runs row and kick off the run in a daemon thread. Returns {run_id}.

    ENGINE: None (default) resolves via orchestra_on() — 'orchestra' (the durable org run) unless
    AOS_ORCHESTRA=0 -> 'fleet' (legacy in-process). Callers may also pin it explicitly.

    GOVERNED SPEND PATH (mirrors orchestrator.confirm): launching the fleet fans out unbounded LLM
    work, so gate on consent + billing quota BEFORE any spend, and thread the TENANT's connected
    provider key (not the platform default) so the work is billed to the tenant who asked for it.
    A blocked run is still recorded durably with status='failed' (so a poller on run_state()
    terminates promptly instead of hanging) and the thread is NOT started.
    """
    _ensure()
    engine = engine or ("orchestra" if orchestra_on() else "fleet")
    import billing
    import consent
    import tenantproviders
    # Resolve the per-tenant provider key unless one was passed explicitly (mirrors frontdoor._run_build).
    if api_key is None:
        try:
            api_key = tenantproviders.build_kwargs(tenant_id).get("api_key")
        except Exception:
            api_key = None
    # Consent + quota gate: refuse the fan-out for a missing/over-quota tenant, but keep the contract
    # (always return a run_id) so the caller's poll loop sees a terminal status and stops.
    block = None
    if not consent.require_consent(tenant_id):
        block = "consent_required"
    else:
        try:
            q = billing.quota(tenant_id)
            if not q["within_quota"]:
                block = f"quota reached ({q['builds']})"
        except Exception as e:
            # An EXCEPTION verifying quota (e.g. 'no such tenant') is an INTERNAL error, NOT an over-quota
            # condition — label it so downstream never mis-renders it as a billing "upgrade your plan" message.
            block = f"internal_error: quota check failed ({str(e)[:100]})"
    status = "failed" if block else "running"
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO research_runs (tenant_id, org_id, thread_id, question, status)
                       VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                    (tenant_id, org_id, thread_id, question, status))
        run_id = cur.fetchone()[0]
        c.commit()
    if block:
        audit.append(actor="research", action="ResearchRunBlocked", resource=str(run_id),
                     decision="blocked", payload={"reason": block, "tenant": tenant_id})
        # Surface the REAL block reason (e.g. 'consent_required') alongside the terminal status, so a caller
        # that only sees this return — or polls run_state and gets status='failed' — can still propagate an
        # ACTIONABLE reason to the user instead of an opaque 'failed'. The thread is NOT started (no spend).
        return {"run_id": run_id, "status": "failed", "error": block}
    audit.append(actor="research", action="ResearchRunStart", resource=str(run_id),
                 decision="executed", payload={"question": (question or "")[:160], "tenant": tenant_id,
                                               "engine": engine})
    threading.Thread(target=_run, args=(run_id, question, api_key, engine, tenant_id, org_id),
                     daemon=True).start()
    return {"run_id": run_id}


def _run(run_id, question, api_key=None, engine="orchestra", tenant_id=None, org_id=None):
    """Daemon worker: run the research (on the tenant's provider key), persist the report, distill
    option cards. Exceptions -> failed. engine='orchestra' (default) executes as a durable org run
    (research_org: Postgres actors + persisted events + heartbeats); 'fleet' is the legacy
    in-process path. Both land the report at the same path, so everything below is engine-agnostic.
    The DB run_id is threaded into either engine so the workspace is per-run isolated."""
    try:
        if engine == "orchestra":
            res = research_org.run_research(question, "REPORT.md", api_key=api_key,
                                            research_run_id=run_id, tenant_id=tenant_id,
                                            org_id=org_id)
        else:
            res = research_fleet.research(question, "REPORT.md", api_key=api_key, run_id=run_id)
        report_path = res.get("report")
        report_text = ""
        try:
            report_text = Path(report_path).read_text()
        except Exception:
            report_text = ""
        # Persist the report path WITHOUT flipping status yet: the controller/UI polls run_state() and the
        # moment it sees status='done' it reads st['options'], so 'done' must NEVER be visible before the
        # option cards exist. Distill options FIRST, confirm >=1 row was stored, THEN mark 'done' — so a
        # 'done' status GUARANTEES the SELECTABLE cards are already queryable (no done-with-empty dead-end).
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("UPDATE research_runs SET report_path=%s WHERE id=%s", (report_path, run_id))
            c.commit()
        n_opts = _extract_options(run_id, report_text)
        if not n_opts:  # belt-and-suspenders: _extract_options always stores a fallback, but never flip
            raise RuntimeError("option distillation produced zero cards")  # 'done' on an empty result set.
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("UPDATE research_runs SET status='done', finished_at=now() WHERE id=%s", (run_id,))
            c.commit()
        audit.append(actor="research", action="ResearchRunDone", resource=str(run_id),
                     decision="executed", payload={"report": report_path, "options": n_opts})
    except Exception as e:
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("UPDATE research_runs SET status='failed', finished_at=now() WHERE id=%s", (run_id,))
            c.commit()
        audit.append(actor="research", action="ResearchRunFailed", resource=str(run_id),
                     decision="failed", payload={"error": str(e)[:300]})


def _parse_options(text):
    """Pull 'OPT: <title> :: <summary>' lines out of an agent's free-text reply, robustly (line-based,
    like research_fleet._parse_subqs). A leading '*' marks the recommended option. Returns a list of
    {title, summary, recommended}, de-duped on title, order preserved."""
    import re
    out, seen = [], set()
    for raw in (text or "").splitlines():
        l = raw.strip()
        if not l:
            continue
        rec = False
        # A leading '*' (optionally as a bullet) marks the recommended option — check BEFORE stripping
        # decoration, since '* OPT:' uses '*' as the recommend marker, not as throwaway bold.
        m = re.match(r"^([-*•]|\d+[.)])\s*(.*)$", l)  # drop a leading bullet/number, capture a '*' marker
        if m:
            if m.group(1) == "*":
                rec = True
            l = m.group(2).strip()
        l = re.sub(r"^\**\s*", "", l).strip()         # drop any remaining bold/decoration (e.g. '**OPT')
        if l.startswith("*"):                          # '* OPT: ...' -> recommended
            rec = True
            l = l[1:].strip()
        m = re.match(r"^OPT\s*[:.\-]\s*(.+)$", l, re.IGNORECASE)
        if not m:
            continue
        body = m.group(1).strip()
        if "::" in body:
            title, summary = body.split("::", 1)
        else:
            title, summary = body, ""
        title, summary = title.strip(" *"), summary.strip()
        key = title.lower()
        if title and key not in seen:
            seen.add(key)
            out.append({"title": title, "summary": summary, "recommended": rec})
    return out


def _extract_options(run_id, report_text):
    """Distill the synthesized report into 3 DISTINCT strategic option cards via a research-growth agent.
    Marks the recommended one. If parsing yields <2, falls back to a single option from the report head."""
    repo = factory.PRODUCTS
    prompt = (
        "You are advising a non-technical CEO. Read the research report BELOW and distill it into "
        "exactly 3 DISTINCT strategic options the CEO could choose between — not steps, but mutually "
        "exclusive directions. Output ONE OPTION PER LINE, nothing else, each formatted EXACTLY as:\n"
        "OPT: <short title> :: <one-sentence summary>\n"
        "Mark the single best option with a leading '*' (e.g. '* OPT: ...'). Do not write anything else.\n\n"
        f"REPORT:\n{report_text[:8000]}")
    try:
        r = factory.agent("research-growth", str(repo), prompt, tools=[])
        opts = _parse_options(r.get("out", "") or "")
    except Exception:
        # A distiller-agent failure must NOT throw away a completed (e.g. 13-min) research run: fall through
        # to the report-head fallback below so the user still gets the research, never a blank dead-end.
        opts = []
    if len(opts) < 2:                                  # parse miss / distiller failure -> surface the report itself
        head = " ".join((report_text or "").split())[:200].strip()
        opts = [{"title": "Read the research",
                 "summary": (head + "…") if head else
                            "Research is done — open the full report; it couldn't be auto-split into options.",
                 "recommended": True}]
    if not any(o["recommended"] for o in opts):        # ensure exactly one recommended
        opts[0]["recommended"] = True
    seen_rec = False
    with psycopg.connect(DB) as c, c.cursor() as cur:
        for o in opts:
            rec = bool(o["recommended"]) and not seen_rec
            if rec:
                seen_rec = True
            cur.execute("""INSERT INTO research_options (run_id, title, summary, recommended)
                           VALUES (%s,%s,%s,%s)""", (run_id, o["title"], o["summary"], rec))
        c.commit()
    return len(opts)


def run_state(tenant_id, run_id):
    """Full state for a run: status + the SELECTABLE option cards. Scoped by tenant_id so a tenant can
    never read another tenant's question or option cards (IDOR hardening, mirrors designview.gallery)."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT status, question FROM research_runs WHERE id=%s AND tenant_id=%s",
                    (run_id, tenant_id))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"no such run {run_id}")
        status, question = row
        # Join options back to the (already tenant-matched) run as defense-in-depth.
        cur.execute("""SELECT o.id, o.title, o.summary, o.recommended, o.chosen FROM research_options o
                       JOIN research_runs r ON o.run_id=r.id
                       WHERE o.run_id=%s AND r.tenant_id=%s ORDER BY o.id""", (run_id, tenant_id))
        options = [{"id": r[0], "title": r[1], "summary": r[2], "recommended": r[3], "chosen": r[4]}
                   for r in cur.fetchall()]
    return {"run_id": run_id, "status": status, "question": question, "options": options}


def select(tenant_id, run_id, option_id):
    """Mark an option chosen for this run; return the chosen option dict. Ownership via tenant match: the
    UPDATE only touches an option whose run belongs to tenant_id, so no cross-tenant caller can flip it."""
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""UPDATE research_options SET chosen=true WHERE id=%s AND run_id=%s
                       AND run_id IN (SELECT id FROM research_runs WHERE tenant_id=%s)""",
                    (option_id, run_id, tenant_id))
        if cur.rowcount == 0:
            raise ValueError(f"no option {option_id} for run {run_id}")
        c.commit()
        cur.execute("""SELECT id, title, summary, recommended, chosen FROM research_options
                       WHERE id=%s""", (option_id,))
        r = cur.fetchone()
    audit.append(actor="research", action="ResearchOptionChosen", resource=str(run_id),
                 decision="executed", payload={"option_id": option_id, "title": r[1], "tenant": tenant_id})
    return {"id": r[0], "title": r[1], "summary": r[2], "recommended": r[3], "chosen": r[4]}


def _selftest():
    """Offline check (NO LLM spend): monkeypatch the fleet + the distiller agent, drive a real run."""
    import tempfile
    import time
    import billing
    import consent

    # parse must survive the real reply shapes the distiller produces.
    p = _parse_options("* OPT: Creator-first :: focus on creators\nOPT: Ad-free subs :: subscription model\n"
                       "- OPT: Short-form :: tiktok style")
    parse_ok = len(p) == 3 and sum(1 for o in p if o["recommended"]) == 1 and p[0]["title"] == "Creator-first"
    print(f"parse: {len(p)} options, recommended={[o['title'] for o in p if o['recommended']]}")
    if not parse_ok:
        print("FAIL: _parse_options"); sys.exit(1)

    tmp = Path(tempfile.mkdtemp()) / "REPORT.md"
    tmp.write_text("# Research report\nKey finding: creators are underserved. Three paths exist.\n")
    real_research, real_agent = research_fleet.research, factory.agent
    real_org = research_org.run_research
    # ENGINE ROUTING under test: default (AOS_ORCHESTRA on) must dispatch the ORCHESTRA org run;
    # engine='fleet' must fall back to the legacy in-process fleet. Count each seam's calls.
    engines = {"orchestra": 0, "fleet": 0}

    def _fake_org(q, out_rel="REPORT.md", **k):
        engines["orchestra"] += 1
        return {"report": str(tmp), "subquestions": 3, "answered": 3}

    def _fake_fleet(q, out_rel="REPORT.md", **k):
        engines["fleet"] += 1
        return {"report": str(tmp)}

    research_org.run_research = _fake_org
    research_fleet.research = _fake_fleet
    factory.agent = lambda *a, **k: {"rc": 0, "out": ("* OPT: Creator-first :: focus on creators\n"
                                                      "OPT: Ad-free subs :: subscription model\n"
                                                      "OPT: Short-form :: tiktok style")}

    reg = billing.signup("research-selftest", "free")
    tid = reg["tenant_id"]
    # Pre-consent: start() must REFUSE the fan-out (no spend, no thread) and return the actionable reason
    # alongside a terminal status, recording the run as 'failed' durably — this is what lets the controller
    # render "accept consent, then say ready" instead of a generic 'failed'.
    pre = start(tid, "org-self", 1, "should be blocked pre-consent")
    blocked_ok = (pre.get("error") == "consent_required"
                  and run_state(tid, pre["run_id"])["status"] == "failed")
    print(f"pre-consent block: error={pre.get('error')} status={run_state(tid, pre['run_id'])['status']}")
    consent.record(tid)                                # start() now gates on consent (governed spend path)
    run_id = None
    ok = False
    try:
        run_id = start(tid, "org-self", 1, "how should we grow the creator platform")["run_id"]
        deadline = time.time() + 10
        st = run_state(tid, run_id)
        while st["status"] not in ("done", "failed") and time.time() < deadline:
            time.sleep(0.2)
            st = run_state(tid, run_id)
        opts = st["options"]
        recs = [o for o in opts if o["recommended"]]
        extracted_ok = st["status"] == "done" and len(opts) == 3 and len(recs) == 1
        # INVARIANT: a 'done' run must NEVER expose zero options (the reordered _run flips 'done' only
        # AFTER >=1 card is stored). Assert it directly on the run we just drove to completion.
        done_implies_options = (st["status"] != "done") or len(opts) >= 1
        # Fallback path: even when the distiller yields ZERO parseable OPT lines, a finished run must still
        # reach 'done' with >=1 (fallback) option — never 'done' with an empty/blank dead-end.
        factory.agent = lambda *a, **k: {"rc": 0, "out": "sorry, I have no idea\nnot an option line"}
        fb_id = start(tid, "org-self", 1, "distiller returns junk")["run_id"]
        dl2 = time.time() + 10
        fst = run_state(tid, fb_id)
        while fst["status"] not in ("done", "failed") and time.time() < dl2:
            time.sleep(0.2)
            fst = run_state(tid, fb_id)
        fallback_ok = (fst["status"] == "done" and len(fst["options"]) >= 1
                       and sum(1 for o in fst["options"] if o["recommended"]) == 1)
        print(f"fallback run {fb_id}: status={fst['status']} options={len(fst['options'])} "
              f"(zero-parse -> sensible fallback, done-implies-options={done_implies_options})")
        chosen = select(tid, run_id, opts[-1]["id"]) if opts else None
        chosen_ok = bool(chosen) and chosen["chosen"] and run_state(tid, run_id)["options"][-1]["chosen"]
        # Cross-tenant guard: another tenant can neither read this run nor flip its options (IDOR).
        try:
            xread = run_state("t-not-mine", run_id)["options"]
        except ValueError:
            xread = []
        try:
            select("t-not-mine", run_id, opts[-1]["id"]) if opts else None
            xselect_ok = False
        except ValueError:
            xselect_ok = True
        xtenant_ok = xread == [] and xselect_ok
        # ENGINE ROUTING: the two completed runs above (default engine) must BOTH have gone through
        # the orchestra org run; an explicit engine='fleet' run must take the legacy path and still
        # land the same report/options contract (console UX identical either way).
        lg_id = start(tid, "org-self", 1, "legacy engine path", engine="fleet")["run_id"]
        dl3 = time.time() + 10
        lst = run_state(tid, lg_id)
        while lst["status"] not in ("done", "failed") and time.time() < dl3:
            time.sleep(0.2)
            lst = run_state(tid, lg_id)
        routing_ok = (engines["orchestra"] == 2 and engines["fleet"] == 1
                      and lst["status"] == "done" and len(lst["options"]) >= 1)
        print(f"engine routing: orchestra={engines['orchestra']} (default ON) "
              f"fleet={engines['fleet']} (explicit fallback, status={lst['status']})")
        ok = (parse_ok and extracted_ok and chosen_ok and xtenant_ok and blocked_ok
              and done_implies_options and fallback_ok and routing_ok)
        print(f"run {run_id}: status={st['status']} options={len(opts)} recommended={len(recs)} "
              f"chosen={chosen['title'] if chosen else None} xtenant_guard={xtenant_ok} "
              f"preconsent_block={blocked_ok} fallback_ok={fallback_ok} engine_routing={routing_ok}")
        print("PASS: research STATE+OPTIONS (async run -> 3 selectable cards -> select; "
              "orchestra engine default, legacy fleet fallback) ✅" if ok else "FAIL")
    finally:
        research_fleet.research, factory.agent = real_research, real_agent
        research_org.run_research = real_org
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""DELETE FROM research_options WHERE run_id IN
                           (SELECT id FROM research_runs WHERE tenant_id=%s)""", (tid,))
            cur.execute("DELETE FROM research_runs WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM ai_consent WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
        try:
            tmp.unlink()
        except Exception:
            pass
    sys.exit(0 if ok else 1)


def _main(a):
    import json
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "json" and len(a) > 2:
        print(json.dumps(run_state(a[1], int(a[2])), indent=2))
    else:
        sys.exit("usage: research.py json <tenant_id> <run_id> | selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
