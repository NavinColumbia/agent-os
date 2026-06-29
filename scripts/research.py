#!/usr/bin/env python3
"""research.py — an async STATE + OPTIONS layer over the research fleet.

The non-technical CEO doesn't want a wall of report text — they want to "give me time to research"
and come back to a handful of SELECTABLE option cards ("which strategy should I pick?"). This wraps
research_fleet (decompose -> parallel fleet -> synthesize) with durable run state and a distillation
step: once the report lands, a research-growth agent reads it and proposes 3 DISTINCT strategic
options, one marked recommended. The controller starts a run, polls run_state, then select()s.

    research.py json <tenant_id> <run_id>     # the run state + option cards
    research.py selftest
Run with the agent-os venv python. No web server — DB + a daemon thread, like the orchestrator.
"""
import sys
import threading
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit          # noqa: E402
import factory        # noqa: E402
import research_fleet  # noqa: E402

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


def start(tenant_id, org_id, thread_id, question, api_key=None):
    """Insert a research_runs row and kick off the fleet in a daemon thread. Returns {run_id}.

    GOVERNED SPEND PATH (mirrors orchestrator.confirm): launching the fleet fans out unbounded LLM
    work, so gate on consent + billing quota BEFORE any spend, and thread the TENANT's connected
    provider key (not the platform default) so the work is billed to the tenant who asked for it.
    A blocked run is still recorded durably with status='failed' (so a poller on run_state()
    terminates promptly instead of hanging) and the thread is NOT started.
    """
    _ensure()
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
            block = f"quota_check_failed: {str(e)[:120]}"
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
        return {"run_id": run_id, "error": block}
    audit.append(actor="research", action="ResearchRunStart", resource=str(run_id),
                 decision="executed", payload={"question": (question or "")[:160], "tenant": tenant_id})
    threading.Thread(target=_run, args=(run_id, question, api_key), daemon=True).start()
    return {"run_id": run_id}


def _run(run_id, question, api_key=None):
    """Daemon worker: run the fleet (on the tenant's provider key), persist the report, distill option
    cards. Exceptions -> failed."""
    try:
        res = research_fleet.research(question, "REPORT.md", api_key=api_key)
        report_path = res.get("report")
        report_text = ""
        try:
            report_text = Path(report_path).read_text()
        except Exception:
            report_text = ""
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""UPDATE research_runs SET report_path=%s, status='done', finished_at=now()
                           WHERE id=%s""", (report_path, run_id))
            c.commit()
        _extract_options(run_id, report_text)
        audit.append(actor="research", action="ResearchRunDone", resource=str(run_id),
                     decision="executed", payload={"report": report_path})
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
    r = factory.agent("research-growth", str(repo), prompt, tools=[])
    opts = _parse_options(r.get("out", "") or "")
    if len(opts) < 2:                                  # parse miss -> one fallback option from the report head
        head = " ".join((report_text or "").split())[:200].strip()
        opts = [{"title": "Proceed with research findings",
                 "summary": head or "See the full report for details.", "recommended": True}]
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
    research_fleet.research = lambda q, out_rel="REPORT.md", **k: {"report": str(tmp)}
    factory.agent = lambda *a, **k: {"rc": 0, "out": ("* OPT: Creator-first :: focus on creators\n"
                                                      "OPT: Ad-free subs :: subscription model\n"
                                                      "OPT: Short-form :: tiktok style")}

    reg = billing.signup("research-selftest", "free")
    tid = reg["tenant_id"]
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
        ok = parse_ok and extracted_ok and chosen_ok and xtenant_ok
        print(f"run {run_id}: status={st['status']} options={len(opts)} recommended={len(recs)} "
              f"chosen={chosen['title'] if chosen else None} xtenant_guard={xtenant_ok}")
        print("PASS: research STATE+OPTIONS (async run -> 3 selectable cards -> select) ✅" if ok else "FAIL")
    finally:
        research_fleet.research, factory.agent = real_research, real_agent
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
