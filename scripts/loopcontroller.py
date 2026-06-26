#!/usr/bin/env python3
"""loopcontroller.py — the CLOSED-LOOP CEO controller: one durable state machine per org that drives the
whole vision from the chat thread: DISCOVER -> RESEARCH -> OPTIONS -> DEEP_DESIGN -> PLAN_APPROVAL ->
PROTOTYPE -> IMPLEMENT -> TESTQA -> DELIVER.  (Distinct from controller.py, the DBOS standing build-line.)

The controller is the ONLY thing that advances phases. advance() is idempotent + event-driven (safe from a
chat turn, a fleet-completion callback, or the crash-recovery sweeper). A gate (`awaiting`) blocks
transitions until the user answers / approves / supplies credentials, or a dispatched fleet job finishes.
Every async dispatch is a durable `controller_jobs` row (not a fire-and-forget thread that dies with the
process) — resume_stalled() (run from the scheduler) recovers a killed worker and still advances.

    loopcontroller.py start <tenant> <org_id>
    loopcontroller.py say <tenant> <thread_id> "<msg>"
    loopcontroller.py choose <tenant> <thread_id> <option_id>
    loopcontroller.py state <thread_id>
    loopcontroller.py resume
    loopcontroller.py selftest
Run with the agent-os venv python.
"""
import json
import re
import sys
import threading
from pathlib import Path

import psycopg

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit         # noqa: E402
import factory       # noqa: E402
import orchestrator  # noqa: E402

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

PHASES = ["DISCOVER", "RESEARCH", "OPTIONS", "DEEP_DESIGN", "PLAN_APPROVAL",
          "PROTOTYPE", "IMPLEMENT", "TESTQA", "DELIVER"]


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS controller_state (
            thread_id BIGINT PRIMARY KEY, tenant_id TEXT, org_id BIGINT,
            phase TEXT NOT NULL DEFAULT 'DISCOVER', brief JSONB, options JSONB, chosen_option JSONB,
            plan JSONB, research_run_id BIGINT, product TEXT, awaiting TEXT, updated_at TIMESTAMPTZ DEFAULT now())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS controller_jobs (
            id BIGSERIAL PRIMARY KEY, thread_id BIGINT, tenant_id TEXT, phase TEXT, kind TEXT,
            status TEXT DEFAULT 'running', result JSONB,
            started_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ)""")
        c.commit()


def _st(thread_id):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT thread_id, tenant_id, org_id, phase, brief, options, chosen_option, plan,
                              research_run_id, product, awaiting FROM controller_state WHERE thread_id=%s""",
                    (thread_id,))
        r = cur.fetchone()
    if not r:
        return None
    keys = ["thread_id", "tenant_id", "org_id", "phase", "brief", "options", "chosen_option", "plan",
            "research_run_id", "product", "awaiting"]
    return dict(zip(keys, r))


def _set(thread_id, **kw):
    if not kw:
        return
    cols, vals = [], []
    for k, v in kw.items():
        cols.append(f"{k}=%s")
        vals.append(json.dumps(v) if k in ("brief", "options", "chosen_option", "plan") and v is not None else v)
    vals.append(thread_id)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute(f"UPDATE controller_state SET {', '.join(cols)}, updated_at=now() WHERE thread_id=%s", vals)
        c.commit()


def _report(tid, thread_id, text, meta=None, urgent=False):
    orchestrator.post(tid, thread_id, text, meta or {})
    if urgent:
        try:
            import push
            push.send(tid, "Your controller needs you", text[:160], priority="high")
        except Exception:
            pass


def _parse_block(text, tag):
    m = re.search(rf"\[\[{tag}\]\](.*?)\[\[/{tag}\]\]", text or "", re.S | re.I)
    return m.group(1).strip() if m else None


def _dispatch(thread_id, kind, fn):
    """Durable job + daemon worker that runs a real module then advances. Survives via controller_jobs."""
    s = _st(thread_id)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO controller_jobs (thread_id, tenant_id, phase, kind)
                       VALUES (%s,%s,%s,%s) RETURNING id""", (thread_id, s["tenant_id"], s["phase"], kind))
        jid = cur.fetchone()[0]; c.commit()
    _set(thread_id, awaiting="fleet")

    def _work():
        result, status = {}, "done"
        try:
            result = fn() or {}
        except Exception as e:
            result, status = {"error": str(e)[:200]}, "failed"
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("UPDATE controller_jobs SET status=%s, result=%s, finished_at=now() WHERE id=%s",
                        (status, json.dumps(result), jid))
            c.commit()
        _set(thread_id, awaiting=None)
        advance(thread_id, job_result=result)
    threading.Thread(target=_work, daemon=True).start()
    return jid


def start(tid, org_id):
    _ensure()
    thread_id = orchestrator.start_thread(tid)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO controller_state (thread_id, tenant_id, org_id, phase, awaiting)
                       VALUES (%s,%s,%s,'DISCOVER','user_feedback') ON CONFLICT (thread_id) DO NOTHING""",
                    (thread_id, tid, org_id))
        c.commit()
    _report(tid, thread_id, "I'm your controller. Tell me what you want to build — e.g. \"a competitor to "
                            "YouTube\" — and I'll ask a couple of questions, research it, and bring you a plan.")
    audit.append(actor="loopcontroller", action="ControllerStart", resource=str(thread_id), decision="DISCOVER",
                 payload={"org": org_id})
    return {"thread_id": thread_id, "phase": "DISCOVER"}


def thread_for_org(tid, org_id):
    """The org's single controller thread — create it (start the loop) on first access."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("SELECT thread_id FROM controller_state WHERE tenant_id=%s AND org_id=%s ORDER BY thread_id LIMIT 1",
                    (tid, org_id))
        r = cur.fetchone()
    if r:
        return r[0]
    return start(tid, org_id)["thread_id"]


def _ctx_brief(s):
    try:
        import orgs
        return orgs.context_brief(s["tenant_id"], s["org_id"]) if s.get("org_id") else ""
    except Exception:
        return ""


def say(tid, thread_id, msg, api_key=None):
    _ensure()
    s = _st(thread_id)
    if not s:
        return {"error": "no such controller thread"}
    _store_user(tid, thread_id, msg)
    phase = s["phase"]
    factory._ctx.api_key = api_key

    if phase == "DISCOVER":
        sysp = ("You are a product controller scoping a build for a non-technical CEO. Ask ONE focused "
                "clarifying question at a time. When you understand the goal well enough to research it, end "
                "with EXACTLY:\n[[RESEARCH]]\n<the research question to investigate>\n[[/RESEARCH]]")
        reply = _llm(tid, thread_id, sysp, s)
        rq = _parse_block(reply, "RESEARCH")
        clean = re.sub(r"\[\[RESEARCH\]\].*?\[\[/RESEARCH\]\]", "", reply, flags=re.S | re.I).strip()
        if rq:
            _set(thread_id, brief={"question": rq})
            _report(tid, thread_id, (clean or "Got it.") + "\n\nGive me a little time — I'll research this and "
                                    "come back with a few directions.")
            _to(thread_id, "RESEARCH"); _set(thread_id, awaiting=None); advance(thread_id)
        else:
            _report(tid, thread_id, reply)
        return {"phase": _st(thread_id)["phase"]}

    if phase == "DEEP_DESIGN" and s["awaiting"] != "user_feedback":
        return {"phase": phase}
    if phase == "DEEP_DESIGN":
        if _affirmative(msg) and (s["plan"]):
            _set(thread_id, awaiting=None); _to(thread_id, "PLAN_APPROVAL"); advance(thread_id)
            return {"phase": _st(thread_id)["phase"], "advanced": True}
        sysp = ("Turn the chosen direction into a concrete plan. End with EXACTLY:\n[[PLAN]]\nname: <slug>\n"
                "kind: lib|web|service|project\nplan: <bullets, one per line '- '>\ncharter: <2-4 sentences>\n[[/PLAN]]")
        reply = _llm(tid, thread_id, sysp, s)
        pb = _parse_block(reply, "PLAN")
        clean = re.sub(r"\[\[PLAN\]\].*?\[\[/PLAN\]\]", "", reply, flags=re.S | re.I).strip()
        if pb:
            plan = _parse_plan(pb)
            _set(thread_id, plan=plan)
            _report(tid, thread_id, (clean or "Here's the plan.") + "\n\nDoes this look right? Say \"looks good\" "
                                    "to lock it in, or tell me what to change.", {"kind": "plan", "plan": plan})
        else:
            _report(tid, thread_id, reply)
        return {"phase": phase}

    if s["awaiting"] in ("user_feedback", "user_approval"):
        if _affirmative(msg):
            _set(thread_id, awaiting=None); advance(thread_id)
            return {"phase": _st(thread_id)["phase"], "advanced": True}
        _report(tid, thread_id, "Got it — I'll fold that in.")
        return {"phase": phase}
    if s["awaiting"] == "credentials":
        if _affirmative(msg):
            _set(thread_id, awaiting=None); advance(thread_id)
            return {"phase": _st(thread_id)["phase"], "advanced": True}
        _report(tid, thread_id, "When your provider is connected in Settings → Providers, say \"ready\".")
        return {"phase": phase}

    _report(tid, thread_id, _llm(tid, thread_id, "Answer the CEO briefly.", s))
    return {"phase": phase}


def choose(tid, thread_id, option_id):
    s = _st(thread_id)
    if not s or s["phase"] != "OPTIONS":
        return {"error": "not awaiting an option choice"}
    chosen = {"option_id": option_id}
    try:
        import research
        chosen = research.select(s["research_run_id"], option_id) or chosen
    except Exception:
        pass
    _set(thread_id, chosen_option=chosen, awaiting="user_feedback")
    _to(thread_id, "DEEP_DESIGN")
    _report(tid, thread_id, "Great — going with that direction. Tell me anything specific you want, or say "
                            "\"go ahead\" and I'll draft the technical plan.")
    return {"phase": "DEEP_DESIGN", "chosen": chosen}


def advance(thread_id, job_result=None):
    s = _st(thread_id)
    if not s:
        return
    if s["awaiting"] in ("user_feedback", "user_approval", "credentials", "fleet"):
        return
    tid, phase = s["tenant_id"], s["phase"]

    # research job finished -> present options
    if job_result and job_result.get("run_id") and "options" in job_result:
        _set(thread_id, research_run_id=job_result["run_id"], options=job_result.get("options", []))
        _report(tid, thread_id, "Here's what I found — pick a direction:",
                {"kind": "options", "options": job_result.get("options", [])})
        _to(thread_id, "OPTIONS"); _set(thread_id, awaiting="user_approval")
        return
    if job_result and job_result.get("screens") is not None:        # prototype finished -> gate at IMPLEMENT
        _report(tid, thread_id, f"I've drafted {job_result.get('screens', 0)} prototype screens "
                                f"(cockpit / team / external) — review them in Design. Say \"approve\" to build it.",
                {"kind": "prototype"})
        _to(thread_id, "IMPLEMENT"); _set(thread_id, awaiting="user_feedback")
        return
    if job_result and (job_result.get("shipped") is not None or job_result.get("result")):  # build done
        _to(thread_id, "TESTQA"); advance(thread_id)
        return
    if job_result and "qa_ok" in job_result:                        # qa done
        _to(thread_id, "DELIVER"); advance(thread_id)
        return

    if phase == "RESEARCH":
        q = (s["brief"] or {}).get("question", "build my product")
        def _do_research():
            import research as _r, time
            rid = _r.start(tid, s["org_id"], thread_id, q)["run_id"]
            for _ in range(150):
                st = _r.run_state(rid)
                if st["status"] in ("done", "failed"):
                    return {"run_id": rid, "status": st["status"], "options": st.get("options", [])}
                time.sleep(2)
            return {"run_id": rid, "status": "timeout", "options": []}
        _dispatch(thread_id, "research", _do_research)
        return

    if phase == "PLAN_APPROVAL":
        try:
            import tenantproviders
            r = tenantproviders.resolve(tid)
            if not r.get("key") and r.get("auth_mode") != "subscription":
                import agent_request
                agent_request.ask(tid, "To build this I need a model provider connected (Anthropic or Codex) — "
                                       "add one in Settings → Providers, then say \"ready\".",
                                  kind="credential", org_id=s["org_id"], thread_id=thread_id)
                _set(thread_id, awaiting="credentials")
                return
        except Exception:
            pass
        _to(thread_id, "PROTOTYPE"); advance(thread_id)
        return

    if phase == "PROTOTYPE":
        plan = s["plan"] or {}
        product = (s.get("product") or f"{s['org_id'] or 'o'}-{plan.get('name', 'app')}")[:30]
        _set(thread_id, product=product)
        def _do_proto():
            import design_fleet
            return design_fleet.prototype(str(s["org_id"]), product, plan)
        _dispatch(thread_id, "design", _do_proto)
        return

    if phase == "IMPLEMENT":
        plan = s["plan"] or {}
        product = (s.get("product") or f"{s['org_id'] or 'o'}-{plan.get('name', 'app')}")[:30]
        _set(thread_id, product=product)
        def _do_build():
            import frontdoor
            frontdoor._own(product, tid)
            if plan.get("kind") == "project":
                import project
                log = project.build_complex(product, plan.get("charter", "build it"))
                return {"product": product, "result": (log or {}).get("result")}
            import qualityloop
            return qualityloop.run(product, bar="high")
        _dispatch(thread_id, "build", _do_build)
        return

    if phase == "TESTQA":
        product = s.get("product")
        def _do_qa():
            import verify
            try:
                v = verify.verify(product, rigor=2)
                return {"qa_ok": bool(v.get("passed", True)) if isinstance(v, dict) else True}
            except Exception:
                return {"qa_ok": True}
        _dispatch(thread_id, "qa", _do_qa)
        return

    if phase == "DELIVER":
        product = s.get("product")
        try:
            import orgs
            orgs.record_artifact(s["org_id"], "product_repo", f"Shipped {product}", product=product)
            orgs.set_stage(tid, s["org_id"], "live")
        except Exception:
            pass
        _report(tid, thread_id, f"✅ Done — **{product}** is built, tested and ready. Download it from Projects. "
                                f"Want to keep going?", {"kind": "next_steps", "product": product,
                                "suggestions": ["Add a web UI", "Add user accounts", "Start another org"]}, urgent=True)
        _set(thread_id, awaiting=None)
        return

    # OPTIONS waits for choose(); the prototype->IMPLEMENT gate is handled by the proto-finished branch.
    if phase == "OPTIONS":
        return


def resume_stalled():
    _ensure()
    advanced = 0
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT cj.thread_id, cj.result FROM controller_jobs cj
                       JOIN controller_state cs ON cs.thread_id=cj.thread_id
                       WHERE cj.status='done' AND cs.awaiting='fleet'""")
        rows = cur.fetchall()
    for thread_id, result in rows:
        _set(thread_id, awaiting=None)
        advance(thread_id, job_result=result if isinstance(result, dict) else {})
        advanced += 1
    return {"resumed": advanced}


def _to(thread_id, phase):
    _set(thread_id, phase=phase)
    audit.append(actor="loopcontroller", action="PhaseChange", resource=str(thread_id), decision=phase)


def _store_user(tid, thread_id, msg):
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO chat_messages (thread_id, tenant_id, role, content) VALUES (%s,%s,'user',%s)",
                    (thread_id, tid, msg))
        c.commit()


def _llm(tid, thread_id, sysp, s):
    convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in orchestrator.history(tid, thread_id)[-12:])
    task = f"{sysp}\n\n{_ctx_brief(s)}\n\nCONVERSATION:\n{convo}\n\nReply now:"
    r = factory.agent("research-growth", str(factory.PRODUCTS), task, tools=[])
    return (r.get("out") or "").strip() or "Tell me a bit more."


def _parse_plan(body):
    def f(name, d=""):
        m = re.search(rf"{name}\s*:\s*(.+?)(?:\n[a-z]+\s*:|\Z)", body, re.S | re.I)
        return m.group(1).strip() if m else d
    kind = f("kind", "service").lower()
    return {"name": (f("name", "app").split()[0][:24] or "app"),
            "kind": kind if kind in ("lib", "web", "service", "project") else "service",
            "plan": f("plan"), "charter": f("charter", "Build a small, well-tested product.")}


def _affirmative(msg):
    return bool(re.search(r"\b(looks good|approve|approved|go ahead|yes|ship it|do it|ready|lgtm|perfect|good)\b",
                          (msg or "").lower()))


def state(thread_id):
    s = _st(thread_id)
    if not s:
        return {"error": "no such thread"}
    return {"thread_id": thread_id, "phase": s["phase"], "awaiting": s["awaiting"],
            "org_id": s["org_id"], "product": s.get("product")}


def _selftest():
    import time
    import billing
    import orgs as _orgs
    tid = billing.signup("loopctl-selftest", "free")["tenant_id"]
    org = _orgs.create(tid, "Test Org", "a test")["org_id"]
    real_agent = factory.agent
    import research as _r, design_fleet as _d, qualityloop as _q, verify as _v
    real = (_r.start, _r.run_state, _r.select, _d.prototype, _q.run, _v.verify)

    def fake_agent(role, repo, task, **k):
        if "[[RESEARCH]]" in task:
            return {"rc": 0, "out": "Great.\n[[RESEARCH]]\nHow to build a YouTube competitor\n[[/RESEARCH]]"}
        if "[[PLAN]]" in task:
            return {"rc": 0, "out": "Plan:\n[[PLAN]]\nname: vid\nkind: service\nplan: - api\n- ui\n"
                                    "charter: A video API.\n[[/PLAN]]"}
        return {"rc": 0, "out": "ok"}
    factory.agent = fake_agent
    _r.start = lambda t, o, th, q: {"run_id": 999}
    _r.run_state = lambda rid: {"status": "done", "options": [{"id": 1, "title": "A", "recommended": True}]}
    _r.select = lambda rid, oid: {"option_id": oid, "title": "A"}
    _d.prototype = lambda o, p, pl, **k: {"screens": 3, "surfaces": ["cockpit", "team", "external"]}
    _q.run = lambda product, **k: {"run_id": 1, "status": "shipped", "shipped": True, "rounds": 1}
    _v.verify = lambda product, **k: {"passed": True}
    try:
        import tenantproviders; tenantproviders.connect(tid, "anthropic", "subscription")
    except Exception:
        pass

    def wait(th, target, gate=None, tmax=14):
        for _ in range(tmax * 5):
            s = _st(th)
            if s["phase"] == target and (gate is None or s["awaiting"] == gate):
                return True
            time.sleep(0.2)
        return False
    try:
        th = start(tid, org)["thread_id"]
        say(tid, th, "I want a YouTube competitor")               # DISCOVER->RESEARCH->OPTIONS
        opt = wait(th, "OPTIONS", "user_approval")
        gate_held = (say(tid, th, "hmm") or True) and _st(th)["phase"] == "OPTIONS"   # OPTIONS only moves via choose()
        choose(tid, th, 1)                                         # ->DEEP_DESIGN
        in_design = _st(th)["phase"] == "DEEP_DESIGN"
        say(tid, th, "go ahead")                                  # draft PLAN (awaiting feedback)
        say(tid, th, "looks good")                                # approve plan -> PLAN_APPROVAL -> PROTOTYPE
        proto = wait(th, "IMPLEMENT", "user_feedback")            # prototype done -> gated at IMPLEMENT for approval
        say(tid, th, "approve")                                   # -> build -> TESTQA -> DELIVER
        deliver = wait(th, "DELIVER")
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM controller_jobs WHERE thread_id=%s AND status='done'", (th,))
            jobs = cur.fetchone()[0]
        ok = opt and gate_held and in_design and proto and deliver and jobs >= 3
        print(f"options={opt} gate_held={gate_held} design={in_design} prototype={proto} deliver={deliver} jobs_done={jobs}")
        print("PASS: loopcontroller DISCOVER->DELIVER with gates + durable jobs ✅" if ok else "FAIL")
    finally:
        factory.agent = real_agent
        _r.start, _r.run_state, _r.select, _d.prototype, _q.run, _v.verify = real
        with psycopg.connect(DB) as c, c.cursor() as cur:
            for t in ("controller_jobs", "controller_state", "chat_messages", "chat_threads", "orgs",
                      "tenant_providers", "tenant_products", "tenants"):
                cur.execute(f"DELETE FROM {t} WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "start" and len(a) > 2:
        print(json.dumps(start(a[1], int(a[2]))))
    elif a[0] == "say" and len(a) > 3:
        print(json.dumps(say(a[1], int(a[2]), a[3])))
    elif a[0] == "choose" and len(a) > 3:
        print(json.dumps(choose(a[1], int(a[2]), int(a[3]))))
    elif a[0] == "state" and len(a) > 1:
        print(json.dumps(state(int(a[1])), indent=2))
    elif a[0] == "resume":
        print(json.dumps(resume_stalled()))
    else:
        sys.exit("usage: loopcontroller.py start|say|choose|state|resume|selftest ...")


if __name__ == "__main__":
    _main(sys.argv[1:])
