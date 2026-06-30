#!/usr/bin/env python3
"""orchestrator.py — the ORCHESTRATOR CHAT: how a CEO actually DIRECTS the agent company by talking.

The requirements discovery found this is the defining missing interaction: today intake is one-shot forms,
so a non-technical CEO can only fill a form, not *direct*. This is a real conversational thread where the
CEO describes an idea in plain language, the orchestrator asks ONE clarifying question at a time when the
idea is underspecified, answers questions about the running factory, and — when the idea is concrete —
proposes a build the CEO confirms with one tap (which then runs through the SAME governed flow: consent
gate + quota + factory). Context (the tenant's live factory state) is fed to the orchestrator each turn.

    orchestrator.py say <tid> <thread> "<message>"   # one chat turn -> reply (+ maybe a build proposal)
    orchestrator.py confirm <tid> <thread>           # commit the last proposed build
    orchestrator.py history <tid> <thread>
    orchestrator.py selftest
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
import audit       # noqa: E402
import billing     # noqa: E402
import cockpit     # noqa: E402
import consent     # noqa: E402
import factory     # noqa: E402
import frontdoor   # noqa: E402
import governance  # noqa: E402  (read side of the role manifest: enforce()/may())

ENV = Path.home() / "projects" / "agent-os" / ".env.local"
DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
           if l.strip().startswith("DATABASE_URL=")), None)

SYS = (
    "You are the ORCHESTRATOR for a non-technical CEO who directs a company of AI agents that build and "
    "operate their software. Be warm, plain-spoken, and concise — no jargon. Behave like a great human "
    "manager. Each turn do ONE of:\n"
    "1) If the CEO's product idea is still vague, ask exactly ONE focused clarifying question.\n"
    "2) SUGGEST: proactively offer advice when useful — 'most apps like this also want X', 'I'd recommend a "
    "web app over an API so your staff get a screen' — phrased as a short suggestion the CEO can accept.\n"
    "3) If the CEO asks about their factory (status/cost/projects), answer from the FACTORY STATE provided.\n"
    "4) When the idea is concrete enough to build, end your reply with a build block EXACTLY:\n"
    "[[BUILD]]\nname: <short-slug>\nkind: lib|web|service|project\n"
    "plan: <a short bullet list (one per line, prefixed '- ') of the main components/features you'll build>\n"
    "charter: <2-4 sentence concrete spec>\n[[/BUILD]]\n"
    "Use kind 'project' for a BIG multi-component system (several modules/services); use lib/web/service for "
    "a single focused artifact. Always include the plan so the CEO can see WHAT you'll build and say if it "
    "looks right. Only emit a build block when you can write a good plan + charter. Never emit more than one."
)


def _ensure():
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS chat_threads (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS chat_messages (
            id BIGSERIAL PRIMARY KEY, thread_id BIGINT NOT NULL, tenant_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL, meta JSONB DEFAULT '{}',
            ts TIMESTAMPTZ DEFAULT now())""")
        c.commit()


def start_thread(tid):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO chat_threads (tenant_id) VALUES (%s) RETURNING id", (tid,))
        thread = cur.fetchone()[0]; c.commit()
    return thread


def history(tid, thread_id):
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""SELECT role, content, meta, ts FROM chat_messages
                       WHERE tenant_id=%s AND thread_id=%s ORDER BY id""", (tid, thread_id))
        return [{"role": r, "content": ct, "meta": m, "ts": str(t)} for r, ct, m, t in cur.fetchall()]


def _state_brief(tid):
    """A compact factory-state summary fed to the orchestrator so it can answer status questions."""
    try:
        c = cockpit.cockpit(tid); s = c["summary"]
        prods = ", ".join(f"{p['product']}({p['result']})" for p in c["products"][:8]) or "none yet"
        return (f"FACTORY STATE — plan {c['budget'].get('plan')}, products {s['products']} "
                f"(launched {s['launched']}, building {s['building']}, failed {s['failed']}), "
                f"live workers {s['live_workers']}, spend ${s['spend_usd']}. Products: {prods}.")
    except Exception:
        return "FACTORY STATE — unavailable."


def _parse_build(text):
    """Pull the optional [[BUILD]] block out of the orchestrator reply (robust, tolerant of whitespace)."""
    m = re.search(r"\[\[BUILD\]\](.*?)\[\[/BUILD\]\]", text, re.S | re.I)
    if not m:
        return None, text
    body = m.group(1)
    def field(name, default=""):
        fm = re.search(rf"{name}\s*:\s*(.+?)(?:\n[a-z]+\s*:|\Z)", body, re.S | re.I)
        return fm.group(1).strip() if fm else default
    kind = field("kind", "lib").lower()
    kind = kind if kind in ("lib", "web", "service", "project") else "lib"
    proposal = {"name": (field("name", "app").split()[0][:24] or "app"), "kind": kind,
                "plan": field("plan", ""), "charter": field("charter", "")}
    reply = text[:m.start()].strip() or "Here's what I'll build — confirm to start."
    return (proposal if proposal["charter"] else None), reply


def _resolved_provider(tid):
    """A tenant has a USABLE model provider when they've connected one with a key, OR signed in via a
    SUBSCRIPTION login (runs on the host CLI's own account — no key needed). Nothing connected -> None,
    so we refuse BEFORE any spend instead of silently billing the platform default."""
    try:
        import tenantproviders
        r = tenantproviders.resolve(tid)
    except Exception:
        return None
    return r if (r.get("key") or r.get("auth_mode") == "subscription") else None


def _apply_provider_ctx(tid):
    """Wire the tenant's resolved provider into factory._ctx so model spend lands on THEIR account
    (engine + key), not the platform default — mirrors factory.build_product's engine routing."""
    r = _resolved_provider(tid) or {}
    if r.get("engine") == "codex":
        factory._ctx.engine, factory._ctx.codex_key, factory._ctx.api_key = "codex", r.get("key"), None
    else:
        factory._ctx.engine, factory._ctx.api_key, factory._ctx.codex_key = "claude", r.get("key"), None


def _gate(tid, thread_id):
    """Refuse a model turn BEFORE any spend when AI-processing consent isn't on file, or no provider is
    connected. Posts a visible assistant turn explaining how to unblock and returns the blocked result;
    returns None to proceed. Fail CLOSED on the (legal) consent gate."""
    try:
        consented = consent.require_consent(tid)
    except Exception:
        consented = False
    if not consented:
        msg = ("Before I can build anything I need your OK to use AI — accept the AI-processing consent in "
               "Settings → Privacy (it names the provider your text is sent to), then tell me again what "
               "you'd like to build.")
        post(tid, thread_id, msg, {"kind": "consent_required"})
        audit.append(actor="orchestrator", action="ConsentRequired", resource=str(thread_id),
                     decision="say", payload={"tenant": tid})
        return {"reply": msg, "proposal": None, "blocked": "consent_required"}
    if not _resolved_provider(tid):
        msg = ("I need a model provider connected first — add Anthropic or OpenAI/Codex in Settings → "
               "Providers (or sign in with your subscription), then tell me again what to build.")
        post(tid, thread_id, msg, {"kind": "provider_required"})
        audit.append(actor="orchestrator", action="ProviderRequired", resource=str(thread_id),
                     decision="say", payload={"tenant": tid})
        return {"reply": msg, "proposal": None, "blocked": "provider_required"}
    return None


def say(tid, thread_id, message, api_key=None):
    """One chat turn: persist the CEO msg, ask the orchestrator agent, persist + return its reply/proposal."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("INSERT INTO chat_messages (thread_id, tenant_id, role, content) VALUES (%s,%s,'user',%s)",
                    (thread_id, tid, message))
        c.commit()
    # CONSENT + PROVIDER GATE: this turn sends the CEO's text to the model. Refuse BEFORE any spend if
    # AI-processing consent isn't on file, or no provider is connected — so we never leak pre-consent text
    # and spend always lands on the tenant's own account (the quick-build front door used to skip this).
    blocked = _gate(tid, thread_id)
    if blocked:
        return blocked
    convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history(tid, thread_id))
    task = f"{SYS}\n\n{_state_brief(tid)}\n\nCONVERSATION SO FAR:\n{convo}\n\nReply now as ORCHESTRATOR:"
    factory._ctx.api_key = api_key
    factory._ctx.tenant = tid          # lets factory.agent enforce the consent backstop (was never set -> bypassed)
    _apply_provider_ctx(tid)           # spend lands on the tenant's account (engine + key), not platform default
    factory._ctx.product = None; factory._ctx.run = f"chat-{thread_id}"; factory._ctx.stage = "ORCHESTRATE"
    r = factory.agent("orchestrator", str(frontdoor.PRODUCTS), task, tools=[])
    reply_raw = (r.get("out") or "").strip() or "Tell me a bit more about what you'd like to build."
    proposal, reply = _parse_build(reply_raw)
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO chat_messages (thread_id, tenant_id, role, content, meta)
                       VALUES (%s,%s,'assistant',%s,%s)""",
                    (thread_id, tid, reply, json.dumps({"proposal": proposal} if proposal else {})))
        c.commit()
    return {"reply": reply, "proposal": proposal}


def post(tid, thread_id, content, meta=None):
    """Post an assistant turn back into the thread (controller -> CEO). This is how the controller REPORTS
    progress/results/next-steps so the chat is an ongoing dialogue, not a write-once intake form."""
    _ensure()
    with psycopg.connect(DB) as c, c.cursor() as cur:
        cur.execute("""INSERT INTO chat_messages (thread_id, tenant_id, role, content, meta)
                       VALUES (%s,%s,'assistant',%s,%s)""", (thread_id, tid, content, json.dumps(meta or {})))
        c.commit()


def _run_and_report(tid, thread_id, product, charter, kind):
    """Run the governed build, then REPORT back into the chat thread like a manager would: a kickoff note,
    the real outcome, and proposed next steps. (Runs in a daemon thread from confirm().)
    kind 'project' routes to the RECURSIVE multi-component engine (project.build_complex)."""
    post(tid, thread_id, f"On it — starting the build for **{product}**. I'll report back here when it's ready.")
    if kind == "project":
        return _run_project_and_report(tid, thread_id, product, charter)
    try:
        frontdoor._run_build(tid, product, charter, kind)
    except Exception as e:
        post(tid, thread_id, f"⚠️ I hit an error launching **{product}**: {str(e)[:200]}. Want me to retry?")
        return
    st = frontdoor._status(product)
    if st.get("ready"):
        post(tid, thread_id,
             f"✅ **{product}** is built, tested and ready — download it from Projects. "
             f"Want me to keep going? Common next steps:",
             {"kind": "next_steps", "product": product,
              "suggestions": ["Add user accounts / login", "Add a simple dashboard", "Write the launch copy"]})
    elif st.get("failed"):
        post(tid, thread_id,
             f"⚠️ **{product}** didn't pass: {(st.get('error') or 'the build was blocked')[:200]}. "
             f"I can retry, or adjust the spec — tell me what to change.", {"kind": "result"})
    else:
        post(tid, thread_id, f"**{product}** is still working — check the Cockpit for live progress.")


def _run_project_and_report(tid, thread_id, product, goal):
    """Run a COMPLEX multi-component build via the recursive engine, then report its real outcome."""
    import project
    frontdoor._own(product, tid)                          # tenant owns it (so it shows in their console)
    bk = None
    try:
        import tenantproviders
        bk = tenantproviders.build_kwargs(tid)
    except Exception:
        bk = {}
    try:
        log = project.build_complex(product, goal, api_key=bk.get("api_key"))
    except Exception as e:
        post(tid, thread_id, f"⚠️ **{product}** hit an error during the multi-component build: {str(e)[:200]}.")
        return
    result = (log or {}).get("result", "")
    if result == "INTEGRATED" or (log or {}).get("passed"):
        post(tid, thread_id,
             f"✅ **{product}** — the full multi-component system is built and integrated. "
             f"Download it from Projects. Want me to keep going?",
             {"kind": "next_steps", "product": product,
              "suggestions": ["Add a web UI on top", "Add user accounts", "Deploy it"]})
    else:
        post(tid, thread_id,
             f"⚠️ **{product}** blocked at {result or 'a step'}: "
             f"{(log or {}).get('blocker', 'needs a decision')[:200]}. It's in your Approvals — retry or tell me what to change.",
             {"kind": "result"})


def confirm(tid, thread_id, api_key=None):
    """Commit the most recent proposed build — through the SAME governed gate as every other build —
    and report progress/results back into the thread (feedback-driven controller)."""
    msgs = [m for m in history(tid, thread_id) if m["role"] == "assistant" and m.get("meta", {}).get("proposal")]
    if not msgs:
        return {"error": "nothing to confirm — describe a product first"}
    p = msgs[-1]["meta"]["proposal"]
    if not consent.require_consent(tid):
        return {"error": "consent_required", "consent": consent.state(tid)}
    if not _resolved_provider(tid):    # no usable provider -> refuse before the build fans out any paid work
        return {"error": "provider_required"}
    q = billing.quota(tid)
    if not q["within_quota"]:
        return {"error": f"quota reached ({q['builds']}) — upgrade your plan"}
    # Launching a build makes the controller spawn worker agents — enforce the spawn capability on
    # the live build-launch path (only the controller may spawn; manifest invariant, read here).
    governance.enforce("controller", "spawn")
    product = f"{tid.replace('t-', '')[:6]}-{p['name'].lower()}"
    threading.Thread(target=_run_and_report, args=(tid, thread_id, product, p["charter"], p["kind"]), daemon=True).start()
    audit.append(actor="orchestrator", action="BuildFromChat", resource=product, decision="started",
                 payload={"thread": thread_id, "kind": p["kind"]})
    return {"product": product, "status": "building", "charter": p["charter"]}


def _selftest():
    """Mock the agent (no spend): prove clarify→propose→parse→confirm-gated flow end to end."""
    reg = billing.signup("orchestrator-selftest", "free")
    tid = reg["tenant_id"]
    real = factory.agent
    real_build = frontdoor._run_build
    frontdoor._run_build = lambda *a, **k: None             # don't spawn a real build in selftest
    real_rar = globals()["_run_and_report"]
    globals()["_run_and_report"] = lambda *a, **k: None     # don't spawn the reporting thread work in selftest
    turns = {"n": 0}

    def fake_agent(role, repo, task, **k):
        turns["n"] += 1
        if turns["n"] == 1:                                 # first turn: a clarifying question
            return {"rc": 0, "out": "Happy to help! Who is this expense tracker for — just you, or a team?"}
        return {"rc": 0, "out": "Great — I'll build that.\n[[BUILD]]\nname: expense\nkind: service\n"
                                "charter: A REST API to track expenses with categories, monthly totals, and CSV export. "
                                "Includes input validation and tests.\n[[/BUILD]]"}
    factory.agent = fake_agent
    import tenantproviders
    try:
        th = start_thread(tid)
        # CONSENT GATE: pre-consent, say() must REFUSE before touching the agent (no proposal, no spend).
        pre_consent = say(tid, th, "I want to track my expenses")
        consent_gate = pre_consent.get("blocked") == "consent_required" and turns["n"] == 0
        consent.record(tid)                                          # CEO accepts AI-processing consent
        # PROVIDER GATE: consented but NO provider connected -> still refuse before any spend.
        pre_prov = say(tid, th, "I want to track my expenses")
        provider_gate = pre_prov.get("blocked") == "provider_required" and turns["n"] == 0
        tenantproviders.connect(tid, "anthropic", "subscription")   # subscription login = resolved (no key)
        t1 = say(tid, th, "I want to track my expenses")
        clarified = t1["proposal"] is None and "?" in t1["reply"]    # asked a question, no build yet
        t2 = say(tid, th, "just me, a simple API")
        proposed = t2["proposal"] and t2["proposal"]["kind"] == "service" and "expense" in t2["proposal"]["charter"].lower()
        consent.revoke(tid)
        gate = confirm(tid, th).get("error") == "consent_required"   # confirm independently re-checks consent
        consent.record(tid)
        built = confirm(tid, th).get("status") == "building"
        hist = len(history(tid, th)) >= 4                            # 2 user + 2 assistant (+ async reports)
        ok = consent_gate and provider_gate and clarified and proposed and gate and built and hist
        print(f"consent_gate={consent_gate} provider_gate={provider_gate} clarify={clarified} propose={proposed} "
              f"confirm-consent-gated={gate} build-on-confirm={built} history>=4={hist}")
        print("PASS: orchestrator chat (gated clarify -> propose -> governed build, reports back) ✅" if ok else "FAIL")
    finally:
        factory.agent = real
        frontdoor._run_build = real_build
        globals()["_run_and_report"] = real_rar
        with psycopg.connect(DB) as c, c.cursor() as cur:
            cur.execute("DELETE FROM chat_messages WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM chat_threads WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM ai_consent WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenant_providers WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenant_products WHERE tenant_id=%s", (tid,))
            cur.execute("DELETE FROM tenants WHERE tenant_id=%s", (tid,))
            c.commit()
    sys.exit(0 if ok else 1)


def _main(a):
    if not a or a[0] == "selftest":
        _selftest()
    elif a[0] == "say" and len(a) > 3:
        print(json.dumps(say(a[1], int(a[2]), a[3]), indent=2))
    elif a[0] == "confirm" and len(a) > 2:
        print(json.dumps(confirm(a[1], int(a[2])), indent=2))
    elif a[0] == "history" and len(a) > 2:
        print(json.dumps(history(a[1], int(a[2])), indent=2))
    else:
        sys.exit('usage: orchestrator.py say <tid> <thread> "<msg>" | confirm <tid> <thread> | history ... | selftest')


if __name__ == "__main__":
    _main(sys.argv[1:])
