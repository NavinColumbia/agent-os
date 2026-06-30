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
    # Defense-in-depth: control markup is never user-facing. Strip any orphan [[TAG]]/[[/TAG]] markers
    # (e.g. a dangling [[/PLAN]] left when a paired block couldn't be matched) before posting to chat.
    text = re.sub(r"\[\[/?[A-Z][A-Z0-9_]*\]\]", "", text or "").strip()
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
        # A long-but-healthy async run (e.g. a research fleet still going past the in-worker poll budget)
        # returns a 'pending' sentinel. We must NOT mark it done/failed (that would surface a FALSE timeout
        # and park the thread on a feedback gate, orphaning the eventual completion) nor advance(). Park the
        # job as 'pending' (the crash-reaper ignores it — it only reaps 'running') and leave the thread on
        # its 'fleet' gate so resume_stalled() reconciles it against the REAL run once it terminates.
        if isinstance(result, dict) and result.get("pending"):
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("UPDATE controller_jobs SET status='pending', result=%s WHERE id=%s",
                            (json.dumps(result), jid))
                c.commit()
            return
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


def _resolved_provider(tid):
    """A tenant has a USABLE model provider when they've connected one with a key, OR signed in via a
    SUBSCRIPTION login (runs on the host CLI's own account — no key needed). Nothing connected -> None,
    so we refuse BEFORE any spend instead of silently billing the platform default. (Mirrors the
    PLAN_APPROVAL credential check at line ~399 — applied up front so no _llm/fan-out runs un-provided.)"""
    try:
        import tenantproviders
        r = tenantproviders.resolve(tid)
    except Exception:
        return None
    return r if (r.get("key") or r.get("auth_mode") == "subscription") else None


def _apply_provider_ctx(r):
    """Wire the tenant's resolved provider into factory._ctx so model spend lands on THEIR account
    (engine + key), not the platform default — mirrors factory.build_product's engine routing. This
    SUPERSEDES the always-None `factory._ctx.api_key = api_key` set earlier in say()."""
    if (r or {}).get("engine") == "codex":
        factory._ctx.engine, factory._ctx.codex_key, factory._ctx.api_key = "codex", r.get("key"), None
    else:
        factory._ctx.engine, factory._ctx.api_key, factory._ctx.codex_key = "claude", (r or {}).get("key"), None


def say(tid, thread_id, msg, api_key=None):
    _ensure()
    s = _st(thread_id)
    if not s:
        return {"error": "no such controller thread"}
    _store_user(tid, thread_id, msg)
    phase = s["phase"]
    factory._ctx.api_key = api_key
    factory._ctx.tenant = tid          # lets factory.agent enforce the consent gate as a backstop (defense-in-depth)

    # CONSENT GATE (EU AI Act Art.50 / Apple 5.1.2(i) / Play AI policy): the controller's whole job is AI work —
    # every phase either sends the CEO's text to the provider (_llm) or fans out paid agent work. Refuse BEFORE
    # any of that, so we never send a single word to the model pre-consent (this is the "gate before the LLM"
    # layer; research.start() + factory.agent enforce the same downstream). The user accepts consent in Settings
    # out-of-band, then any message re-enters here and proceeds. Fail CLOSED (legal gate): a consent-infra error
    # is treated as "not on file" rather than waved through.
    try:
        import consent
        consented = consent.require_consent(tid)
    except Exception:
        consented = False
    if not consented:
        _report(tid, thread_id,
                 "Before I can research or build anything I need your OK to use AI: please accept the "
                 "AI-processing consent in Settings → Privacy (it names the provider your text is sent to), "
                 "then say \"ready\" and we'll get going.",
                 {"kind": "consent_required", "phase": phase}, urgent=True)
        audit.append(actor="loopcontroller", action="ConsentRequired", resource=str(thread_id),
                     decision=phase, payload={"tenant": tid})
        return {"phase": phase, "blocked": "consent_required"}

    # PROVIDER GATE (spend lands on the TENANT, not the platform): every phase below either sends the CEO's
    # text to the model (_llm) or fans out paid agent work. AFTER consent, require a RESOLVED provider — a
    # connected key, or a subscription login that runs on the CLI's own account — BEFORE any spend, and wire
    # it into factory._ctx so the cost is billed to THEIR account. Fail CLOSED: nothing connected -> ask them
    # to connect one in Settings; they say "ready" and the message re-enters here and proceeds.
    prov = _resolved_provider(tid)
    if not prov:
        _report(tid, thread_id,
                 "Before I can research or build I need a model provider connected — add Anthropic or "
                 "OpenAI/Codex in Settings → Providers (or sign in with your subscription), then say "
                 "\"ready\" and I'll pick up right where we left off.",
                 {"kind": "provider_required", "phase": phase}, urgent=True)
        audit.append(actor="loopcontroller", action="ProviderRequired", resource=str(thread_id),
                     decision=phase, payload={"tenant": tid})
        return {"phase": phase, "blocked": "provider_required"}
    _apply_provider_ctx(prov)

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
        sysp = ("Turn the chosen direction into a concrete, RIGOROUS plan that accounts for EVERYTHING before "
                "any code is written — un-propagated signature changes and un-analyzed enforcement edits are the "
                "#1 cause of rework loops, so the plan must leave nothing un-analyzed. Also cover THE CEO'S OWN "
                "SIDE: what the product looks like for (a) the CEO, (b) their staff/team to control & monitor, "
                "and (c) their external users — these surfaces are BUILT INTO their product (we don't host them).\n"
                "If the CEO wants AI AGENTS to handle part of their product ('let agents take care of X'), don't "
                "just pick a preset — ASK ONE follow-up about HOW the agent should be invoked, OR RECOMMEND an "
                "architecture: a button (on-demand/sync), an event→agent pipeline handled ASYNC (form-submit / "
                "inbound email / webhook — like Kafka but agent workers), or a schedule. Then describe that "
                "custom agentic feature + its invocation in the agentic line (free text). If they don't want any, "
                "put 'none'.\n"
                "The plan bullets MUST embed a detailed-design pass so implementation is right the FIRST time: "
                "(1) an IMPACT MAP — every file/component to change, and for any function/signature/schema/"
                "contract being changed, ALL its callers/dependents so nothing is left un-propagated; "
                "(2) the INVARIANTS to preserve (existing behaviour, guards, security constraints) plus the "
                "empty/error/loading/edge cases each surface must handle; "
                "(3) a PARALLELIZATION note — which work items are INDEPENDENT (can build concurrently) vs. "
                "ordered; (4) a DONE checklist mapping each item to the check that proves it.\n"
                "End with EXACTLY:\n[[PLAN]]\nname: <slug>\nkind: lib|web|service|project\n"
                "plan: <bullets incl. the impact map, invariants/edge cases, parallelization, and done checks; one per line '- '>\n"
                "agentic: <free-text: the agentic feature(s) the CEO wants + how each is invoked (button/event-async/"
                "schedule), or 'none'>\ncharter: <2-4 sentences incl. the team/external surfaces to build in>\n[[/PLAN]]")
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

    if phase == "OPTIONS":
        # Options only move via choose() (a chip tap). If the CEO TYPES instead of tapping,
        # don't run the generic affirmative branch — it would clear the gate and call advance(),
        # which is a no-op at OPTIONS, stalling the thread. Try to map a typed ordinal to a chip;
        # otherwise nudge them to tap. Never clear `awaiting`.
        oid = _option_ordinal(msg, s.get("options") or [])
        if oid is not None:
            return choose(tid, thread_id, oid)
        _report(tid, thread_id, "Tap one of the options above to pick a direction.")
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
        chosen = research.select(tid, s["research_run_id"], option_id) or chosen
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

    # A dispatched job came back BROKEN — the worker raised (_work() -> {'error':...}, status='failed')
    # or the underlying module reported failure/timeout. We must NOT fall through to the phase block:
    # that re-runs the SAME failing job, silently re-dispatching it forever (#9). Surface it to the user
    # and park on a feedback gate so they decide (e.g. say "retry" to re-dispatch the phase, or change it).
    if job_result and (job_result.get("error") or job_result.get("status") in ("failed", "timeout")):
        err = job_result.get("error") or job_result.get("status")
        _set(thread_id, awaiting="user_feedback")
        # Map a KNOWN governed-spend block (the gate refused BEFORE any spend) to an ACTIONABLE message so the
        # user can fix the precondition and resume, instead of an opaque 'failed'. Unknown errors keep the
        # generic surface. After fixing it they say "ready"/"retry" -> the user_feedback gate re-dispatches.
        es = str(err).lower()
        if "consent" in es:
            text = ("⚠️ I can't research or build yet because AI-processing consent isn't on file. Please accept "
                    "it in Settings → Privacy (it names the provider your text is sent to), then say \"ready\" "
                    "and I'll pick up right where we left off.")
            meta_kind = "consent_required"
        elif "quota" in es:
            text = ("⚠️ You've hit your plan's build quota, so I paused before spending anything. Upgrade your "
                    "plan (or wait for it to reset) in Settings → Billing, then say \"ready\" to continue.")
            meta_kind = "quota_reached"
        else:
            text = (f"⚠️ The **{phase}** step hit a problem and stopped: {err}. "
                    f"Tell me how you'd like to proceed, or say \"retry\" to run it again.")
            meta_kind = "job_failed"
        _report(tid, thread_id, text, {"kind": meta_kind, "phase": phase}, urgent=True)
        audit.append(actor="loopcontroller", action="JobFailed", resource=str(thread_id), decision=phase,
                     payload={"error": str(err)[:200]})
        return

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
    if job_result and "qa_ok" in job_result:                        # qa verdict in -> ENFORCE it (#48)
        if job_result.get("qa_ok"):
            _to(thread_id, "DELIVER"); advance(thread_id)
        else:
            # A failed (or unverifiable) build must NOT reach DELIVER. Loop back to IMPLEMENT, but gate on
            # the user so we don't silently auto-rebuild forever — they say "approve"/"retry" to rebuild.
            _set(thread_id, awaiting="user_feedback")
            _to(thread_id, "IMPLEMENT")
            _report(tid, thread_id,
                    "⚠️ QA did not pass — the build failed verification, so I'm holding it back from delivery. "
                    "Say \"approve\" to rebuild and re-test, or tell me what to change.",
                    {"kind": "qa_failed"}, urgent=True)
            audit.append(actor="loopcontroller", action="QAGate", resource=str(thread_id), decision="BLOCKED")
        return

    if phase == "RESEARCH":
        q = (s["brief"] or {}).get("question", "build my product")
        def _do_research():
            import research as _r, time
            started = _r.start(tid, s["org_id"], thread_id, q)
            rid = started.get("run_id")
            if started.get("error"):
                # The governed-spend gate (consent/quota) refused the fan-out up front — no thread was
                # started. Carry the REAL reason into the job result so advance() renders an actionable
                # message ("accept consent / upgrade plan, then say ready") instead of an opaque 'failed'.
                return {"run_id": rid, "status": "failed", "error": started["error"], "options": []}
            # Persist the run id up front so resume_stalled() can reconcile this thread against the REAL
            # research run even if this worker — or the whole process — dies before the run finishes.
            _set(thread_id, research_run_id=rid)
            # Poll in-worker for the common (fast) case, but bound the wait to the same window the
            # crash-sweeper uses (RUNNING_TIMEOUT_MIN) instead of a hard 300s cap that falsely declared a
            # still-healthy fleet 'timeout'. The research fleet runs in its OWN daemon, so if it outlives
            # this budget we hand off (pending) rather than killing a live run.
            deadline = time.time() + RUNNING_TIMEOUT_MIN * 60
            while time.time() < deadline:
                st = _r.run_state(tid, rid)
                if st["status"] in ("done", "failed"):
                    return {"run_id": rid, "status": st["status"], "options": st.get("options", [])}
                time.sleep(2)
            # Still running and healthy — hand off to resume_stalled() instead of declaring a false timeout.
            return {"run_id": rid, "status": "pending", "pending": True}
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
            return design_fleet.prototype(tid, str(s["org_id"]), product, plan)
        _dispatch(thread_id, "design", _do_proto)
        return

    if phase == "IMPLEMENT":
        plan = s["plan"] or {}
        product = (s.get("product") or f"{s['org_id'] or 'o'}-{plan.get('name', 'app')}")[:30]
        _set(thread_id, product=product)
        def _do_build():
            import frontdoor
            frontdoor._own(product, tid)
            charter = plan.get("charter", "build it")
            try:                                          # embed the agentic feature(s) the CEO described
                import agentfeatures
                frag = agentfeatures.charter_for(plan.get("agentic", ""))
                if frag:
                    charter += "\n\n" + frag
            except Exception:
                pass
            if plan.get("kind") == "project":
                import project
                log = project.build_complex(product, charter)
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
                # FAIL-CLOSED: if verification cannot run, we have NO evidence the build is good, so we
                # must not let it ship. Treat an unverifiable build as a QA failure (gate blocks DELIVER).
                return {"qa_ok": False}
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


RUNNING_TIMEOUT_MIN = 30  # a job still 'running' past this is presumed crashed (its worker died mid-run)


def resume_stalled():
    """Crash-recovery sweep (run from the scheduler). Recovers EVERY durable job a killed worker left
    behind on the 'fleet' gate — not just the clean 'done' ones the original query saw (#10):
      * status='running' older than RUNNING_TIMEOUT_MIN -> the worker died (often before writing status
        at all), so the row would pin the thread on 'fleet' forever -> flip it to 'failed' durably.
      * status='done'   -> the worker finished but died before calling advance() -> advance now.
      * status='failed' -> the worker caught an error but its advance() was lost -> advance now, which
        surfaces the failure (advance() handles error/failed dicts instead of re-dispatching).
    For each parked thread we act ONLY on its LATEST job: a thread whose newest job is still legitimately
    'running'/'pending' (not timed out) is left untouched, and stale older jobs never trigger a spurious
    advance.
    RESEARCH threads are special-cased FIRST (block 0): they are reconciled against the real research run,
    so a long fleet that outran the in-worker poll budget — or that completed after the thread was parked
    on a feedback gate by an old false-timeout build — still surfaces its options instead of being orphaned.
    """
    _ensure()
    advanced = 0
    # 0) RESEARCH threads are reconciled against the REAL research run (research_runs) — NOT the dispatch
    #    poll. A fleet run that outlived the in-worker poll budget (-> 'pending'), or whose worker/process
    #    died, still reaches a terminal state in its own daemon; pull its result through so the extracted
    #    options are never orphaned — even if an older build already parked the thread on a feedback gate.
    #    This OWNS RESEARCH recovery; the generic fleet sweep below skips RESEARCH to avoid a double-advance
    #    or a spurious failure from a controller_job the reaper marked 'failed' while the run was healthy.
    try:
        import research as _r
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT thread_id, tenant_id, research_run_id, awaiting FROM controller_state
                           WHERE phase='RESEARCH' AND research_run_id IS NOT NULL""")
            rrows = cur.fetchall()
        for thread_id, rtid, rid, awaiting in rrows:
            try:
                rs = _r.run_state(rtid, rid)
            except Exception:
                continue
            rstatus = rs.get("status")
            if rstatus == "done":
                jr = {"run_id": rid, "status": "done", "options": rs.get("options", [])}
            elif rstatus == "failed" and awaiting == "fleet":
                # Surface the failure once, from the active dispatch gate; don't re-spam a thread already
                # parked on a feedback gate (the failure was surfaced when it was first parked there).
                jr = {"run_id": rid, "status": "failed", "error": "research failed"}
            else:
                continue                          # still running, or an already-surfaced failure — leave it
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute("""UPDATE controller_jobs SET status=%s, finished_at=COALESCE(finished_at, now())
                               WHERE thread_id=%s AND kind='research' AND status IN ('running','pending')""",
                            (rstatus, thread_id))
                c.commit()
            _set(thread_id, awaiting=None)
            advance(thread_id, job_result=jr)     # done -> OPTIONS; failed -> surfaces failure
            advanced += 1
    except Exception:
        pass
    with psycopg.connect(DB) as c, c.cursor() as cur:
        # 1) Reap timed-out 'running' jobs: the worker is gone, so mark them failed (durable terminal state).
        cur.execute("""UPDATE controller_jobs SET status='failed',
                           result = COALESCE(result, '{}'::jsonb)
                                    || '{"error":"worker timed out / crashed","status":"failed"}'::jsonb,
                           finished_at = now()
                       WHERE status='running'
                         AND started_at < now() - make_interval(mins => %s)""",
                    (RUNNING_TIMEOUT_MIN,))
        c.commit()
        # 2) For every NON-research thread parked on 'fleet', take its most recent job (any status).
        #    RESEARCH is reconciled above against its real run, so exclude it here.
        cur.execute("""SELECT DISTINCT ON (cj.thread_id) cj.thread_id, cj.result, cj.status
                       FROM controller_jobs cj
                       JOIN controller_state cs ON cs.thread_id=cj.thread_id
                       WHERE cs.awaiting='fleet' AND cs.phase <> 'RESEARCH'
                       ORDER BY cj.thread_id, cj.id DESC""")
        rows = cur.fetchall()
    for thread_id, result, status in rows:
        if status in ("running", "pending"):
            continue                              # newest job still genuinely in flight — leave it alone
        res = result if isinstance(result, dict) else {}
        if status == "failed" and not (res.get("error") or res.get("status") == "failed"):
            res = {**res, "error": "job failed", "status": "failed"}
        _set(thread_id, awaiting=None)
        advance(thread_id, job_result=res)        # done -> advances phase; failed -> surfaces failure
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
    # Use the COMPLETE output (out_full) — never the tail-truncated 'out'. The controller's reply carries
    # leading control blocks ([[RESEARCH]]/[[PLAN]]); a >1500-char plan would lose its OPENING tag under
    # front-truncation, so _parse_block fails (plan never persists) and a dangling [[/PLAN]] leaks to chat.
    return (r.get("out_full") or r.get("out") or "").strip() or "Tell me a bit more."


def _parse_plan(body):
    def f(name, d=""):
        m = re.search(rf"{name}\s*:\s*(.+?)(?:\n[a-z]+\s*:|\Z)", body, re.S | re.I)
        return m.group(1).strip() if m else d
    kind = f("kind", "service").lower()
    return {"name": (f("name", "app").split()[0][:24] or "app"),
            "kind": kind if kind in ("lib", "web", "service", "project") else "service",
            "plan": f("plan"), "agentic": f("agentic", "").strip(),   # free-text: feature(s) + invocation
            "charter": f("charter", "Build a small, well-tested product.")}


def _affirmative(msg):
    # Includes the recovery words the failure UI tells the CEO to type ("retry" etc.) so the instructed
    # word actually clears the gate and re-dispatches the parked phase via advance() — not a no-op.
    return bool(re.search(r"\b(looks good|approve|approved|go ahead|yes|ship it|do it|ready|lgtm|perfect|good"
                          r"|retry|re-?run|try again|redo|run it again)\b",
                          (msg or "").lower()))


def _option_ordinal(msg, options):
    """Map a TYPED option choice ('option 2', 'the first one', '#3') to that option's id.
    Returns the option id, or None if the message isn't an unambiguous ordinal pick."""
    m = (msg or "").lower()
    words = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
             "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    idx = None
    num = re.search(r"\b(?:option|number|num|#|no\.?)\s*#?\s*(\d+)\b", m) or re.search(r"#\s*(\d+)\b", m)
    if num:
        idx = int(num.group(1))
    else:
        for word, n in words.items():
            if re.search(rf"\b{word}\b", m):
                idx = n
                break
    if idx is None or idx < 1 or idx > len(options):
        return None
    opt = options[idx - 1]
    return opt.get("id", idx) if isinstance(opt, dict) else idx


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
        # Mirror factory.agent's real contract: a tail-truncated 'out' (1500-char preview) PLUS the
        # complete 'out_full'. A long preamble pushes the OPENING [[..]] tag out of the 1500-char tail,
        # so this is a regression test: _llm MUST read out_full or the opening tag is lost.
        def both(body):
            full = ("preamble. " * 220) + body          # >1500 chars before the control block
            return {"rc": 0, "out": full[-1500:], "out_full": full}
        if "[[RESEARCH]]" in task:
            return both("Great.\n[[RESEARCH]]\nHow to build a YouTube competitor\n[[/RESEARCH]]")
        if "[[PLAN]]" in task:
            return both("Plan:\n[[PLAN]]\nname: vid\nkind: service\nplan: - api\n- ui\n"
                        "charter: A video API.\n[[/PLAN]]")
        return {"rc": 0, "out": "ok", "out_full": "ok"}
    factory.agent = fake_agent
    _r.start = lambda t, o, th, q: {"run_id": 999}
    _r.run_state = lambda t, rid: {"status": "done", "options": [{"id": 1, "title": "A", "recommended": True}]}
    _r.select = lambda t, rid, oid: {"option_id": oid, "title": "A"}
    _d.prototype = lambda t, o, p, pl, **k: {"screens": 3, "surfaces": ["cockpit", "team", "external"]}
    _q.run = lambda product, **k: {"run_id": 1, "status": "shipped", "shipped": True, "rounds": 1}
    _v.verify = lambda product, **k: {"passed": True}
    try:
        import tenantproviders; tenantproviders.connect(tid, "anthropic", "subscription")
    except Exception:
        pass
    import consent

    def wait(th, target, gate=None, tmax=14):
        for _ in range(tmax * 5):
            s = _st(th)
            if s["phase"] == target and (gate is None or s["awaiting"] == gate):
                return True
            time.sleep(0.2)
        return False
    try:
        th = start(tid, org)["thread_id"]
        # CONSENT GATE: pre-consent, say() must REFUSE before touching the LLM (no phase change, no spend) and
        # tell the CEO to accept consent — not silently send their text to the provider.
        pre = say(tid, th, "I want a YouTube competitor")
        consent_gate_ok = (pre.get("blocked") == "consent_required" and _st(th)["phase"] == "DISCOVER")
        consent.record(tid)                                       # CEO accepts AI-processing consent in Settings
        say(tid, th, "I want a YouTube competitor")               # DISCOVER->RESEARCH->OPTIONS
        opt = wait(th, "OPTIONS", "user_approval")
        gate_held = (say(tid, th, "hmm") or True) and _st(th)["phase"] == "OPTIONS"   # OPTIONS only moves via choose()
        choose(tid, th, 1)                                         # ->DEEP_DESIGN
        in_design = _st(th)["phase"] == "DEEP_DESIGN"
        say(tid, th, "go ahead")                                  # draft PLAN (awaiting feedback)
        # PLAN must parse from the (front-truncatable) LLM reply: persisted to state, rendered as a plan
        # card (meta.kind='plan'), and NO dangling control tag leaked into the user-visible chat.
        plan_persisted = bool((_st(th).get("plan") or {}).get("name"))
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("""SELECT content, meta FROM chat_messages WHERE thread_id=%s AND role='assistant'
                           ORDER BY id DESC LIMIT 1""", (th,))
            pc, pm = cur.fetchone()
        plan_card = isinstance(pm, dict) and pm.get("kind") == "plan"
        no_tag_leak = "[[" not in (pc or "")
        say(tid, th, "looks good")                                # approve plan -> PLAN_APPROVAL -> PROTOTYPE
        proto = wait(th, "IMPLEMENT", "user_feedback")            # prototype done -> gated at IMPLEMENT for approval
        say(tid, th, "approve")                                   # -> build -> TESTQA -> DELIVER
        deliver = wait(th, "DELIVER")
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("SELECT count(*) FROM controller_jobs WHERE thread_id=%s AND status='done'", (th,))
            jobs = cur.fetchone()[0]
        ok = (consent_gate_ok and opt and gate_held and in_design and plan_persisted and plan_card
              and no_tag_leak and proto and deliver and jobs >= 3)
        print(f"consent_gate={consent_gate_ok} options={opt} gate_held={gate_held} design={in_design} "
              f"plan_persisted={plan_persisted} plan_card={plan_card} no_tag_leak={no_tag_leak} "
              f"prototype={proto} deliver={deliver} jobs_done={jobs}")
        print("PASS: loopcontroller DISCOVER->DELIVER with gates + durable jobs ✅" if ok else "FAIL")
    finally:
        factory.agent = real_agent
        _r.start, _r.run_state, _r.select, _d.prototype, _q.run, _v.verify = real
        with psycopg.connect(DB) as c, c.cursor() as cur:
            for t in ("controller_jobs", "controller_state", "chat_messages", "chat_threads", "orgs",
                      "tenant_providers", "tenant_products", "ai_consent", "tenants"):
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
