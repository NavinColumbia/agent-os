#!/usr/bin/env python3
"""ceo_run.py — the REAL end-to-end CEO loop: one prompt in, a finished product out, with proactive
communication for the few decisions that genuinely need the human — answerable from anywhere.

Unlike demo_e2e.py (an ephemeral in-process driver that AUTO-approves every gate — a demo hack), this is the
production-shaped flow:

    submit(prompt)  ─▶  a durable controller thread (Postgres row)
                        jobd (the long-lived daemon) DRIVES it through the phases — NOT this process, so it
                        survives this script exiting / crashing / the box rebooting.
    on a DECISION    ─▶  the controller PINGS the CEO (phone/console) with a phone-answerable task tag
    (options/plan/       "ceo-<thread_id>"; the CEO replies from their phone; replybridge feeds the reply
     credentials)       back into the controller (loopcontroller.say) and jobd continues.
    DELIVER          ─▶  the product is built + QA'd; the CEO is pinged that it's ready.

Modes:
    ceo_run.py "<prompt>" [--tenant T] [--org N]     submit + WATCH: relies on jobd to drive, pings on
                                                     decisions, returns when DELIVER or a decision needs you.
    ceo_run.py submit "<prompt>" [--tenant T]        submit only (fire-and-forget); jobd drives, phone answers.
    ceo_run.py selftest

REQUIRES jobd running (the durable driver) and, for phone answers, reply_listener + replybridge (all
supervised daemons). This script NEVER drives phases itself — that is jobd's job — so it is not the ephemeral
driver that must stay alive.
"""
import json
import os
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import loopcontroller as lc  # noqa: E402


def _notify(msg, title="agent-os · your company", priority="default", tag="robot"):
    try:
        import notify
        notify.send(msg, title=title, priority=priority, tags=tag)
    except Exception:
        pass


def submit(prompt, tenant="demo", org=1):
    """Start a durable build thread from one prompt. jobd drives it; this returns the thread id immediately.
    The prompt is delivered via the real say() path (consent/provider gates + phase machine all apply)."""
    r = lc.start(tenant, org)
    thread_id = r["thread_id"] if isinstance(r, dict) else r
    lc.say(tenant, thread_id, prompt)             # kicks off DISCOVER -> RESEARCH; jobd takes over from here
    _notify(f"Started building from your prompt (thread {thread_id}). I'll ping you only for decisions that "
            f"need you, and when it's ready. Reply to a decision ping from your phone to keep it moving.",
            title="agent-os · building")
    return thread_id


# A decision the CEO must make; the phone task tag they reply to is ceo-<thread_id>.
_DECISION = {"user_feedback", "user_approval", "credentials"}


def watch(tenant, thread_id, poll_s=15, max_min=1200, on_event=lambda e: None):
    """Follow a jobd-driven thread. PROACTIVELY pings the CEO the moment a decision is needed (once per
    distinct gate) and when it DELIVERS. Never drives phases itself. Returns {done|awaiting_decision|timeout}.
    Fully durable: if THIS watcher dies, jobd keeps driving and the phone loop still answers decisions."""
    deadline = time.time() + max_min * 60
    pinged_gate = None
    last = None
    while time.time() < deadline:
        s = lc.state(thread_id) or {}
        phase, awaiting = s.get("phase"), s.get("awaiting")
        sig = (phase, awaiting)
        if sig != last:
            on_event({"phase": phase, "awaiting": awaiting, "product": s.get("product")})
            last = sig
        if s.get("done") or (phase == "DELIVER" and not awaiting):
            _notify(f"✅ Your product is ready — build complete (thread {thread_id}). Open the console to see it.",
                    title="agent-os · delivered", priority="high", tag="white_check_mark")
            return {"done": True, "phase": "DELIVER", "product": s.get("product"), "thread": thread_id}
        if awaiting in _DECISION and awaiting != pinged_gate:
            # PROACTIVE COMMS: a real decision. Ping once, tell them how to answer from their phone.
            what = {"user_feedback": "needs your input", "user_approval": "needs your approval",
                    "credentials": "needs a provider/credential connected"}.get(awaiting, "needs you")
            _notify(f"🗣️ Decision needed (thread {thread_id}, {phase}): {what}. Reply to task 'ceo-{thread_id}' "
                    f"from your phone (e.g. \"go with your recommendation\"), or answer in the console.",
                    title="agent-os · needs you", priority="high", tag="speech_balloon")
            pinged_gate = awaiting
        elif awaiting not in _DECISION:
            pinged_gate = None                    # left the gate (answered) -> re-arm for the next decision
        time.sleep(poll_s)
    return {"timeout": True, "phase": last[0] if last else None, "thread": thread_id}


def _selftest():
    """Offline: submit starts a thread + says the prompt; watch pings on a decision gate and on DELIVER.
    Stubs lc.start/say/state + notify so no fleet/DB/agent work runs."""
    import types
    real = (lc.start, lc.say, lc.state)
    notify_mod = sys.modules.get("notify") or types.ModuleType("notify")
    pings = []
    notify_mod.send = lambda msg, **k: pings.append((k.get("title", ""), msg[:40]))
    sys.modules["notify"] = notify_mod
    said = {}
    lc.start = lambda t, o: {"thread_id": 7}
    lc.say = lambda t, th, m, **k: said.update(t=t, th=th, m=m)
    # state timeline: RESEARCH(fleet) -> OPTIONS(user_approval=decision) -> IMPLEMENT(fleet) -> DELIVER(done)
    seq = iter([{"phase": "RESEARCH", "awaiting": "fleet"},
                {"phase": "OPTIONS", "awaiting": "user_approval"},
                {"phase": "OPTIONS", "awaiting": "user_approval"},   # same gate -> must NOT re-ping
                {"phase": "IMPLEMENT", "awaiting": "fleet"},
                {"phase": "DELIVER", "awaiting": None, "done": True}])
    cur = {"s": {"phase": "RESEARCH", "awaiting": "fleet"}}
    def fake_state(th):
        try: cur["s"] = next(seq)
        except StopIteration: pass
        return cur["s"]
    lc.state = fake_state
    ok = False
    try:
        th = submit("build a todo app", tenant="acme", org=1)
        assert th == 7 and said.get("m") == "build a todo app", (th, said)
        r = watch("acme", 7, poll_s=0, max_min=1)
        decision_pings = [p for p in pings if "needs you" in p[0]]
        deliver_pings = [p for p in pings if "delivered" in p[0]]
        ok = (r.get("done") and len(decision_pings) == 1        # pinged ONCE for the decision (deduped)
              and len(deliver_pings) == 1)                       # pinged on delivery
        print(f"submitted={th} decision_pings={len(decision_pings)} deliver_pings={len(deliver_pings)} done={r.get('done')}")
        print("ceo_run selftest: PASS (durable submit; proactive ping once per decision + on deliver; jobd "
              "drives — this never drives phases itself) ✅" if ok else "ceo_run selftest: FAIL")
        return 0 if ok else 1
    finally:
        lc.start, lc.say, lc.state = real


def _main(argv):
    if not argv or argv[0] == "selftest":
        sys.exit(_selftest())
    tenant = argv[argv.index("--tenant") + 1] if "--tenant" in argv else os.environ.get("AOS_CEO_TENANT", "demo")
    org = int(argv[argv.index("--org") + 1]) if "--org" in argv else 1
    if argv[0] == "submit":
        prompt = " ".join(a for a in argv[1:] if not a.startswith("--") and a not in (tenant, str(org))).strip()
        if not prompt:
            sys.exit('usage: ceo_run.py submit "<prompt>" [--tenant T] [--org N]')
        th = submit(prompt, tenant, org)
        print(json.dumps({"submitted": True, "thread": th, "note": "jobd is driving it; you'll be pinged for decisions + at delivery"}))
        return
    prompt = " ".join(a for a in argv if not a.startswith("--") and a not in (tenant, str(org))).strip()
    if not prompt:
        sys.exit('usage: ceo_run.py "<prompt>" [--tenant T] [--org N]')
    th = submit(prompt, tenant, org)
    print(f"[ceo_run] thread {th} submitted — jobd driving. Watching for decisions + delivery…", flush=True)
    r = watch(tenant, th, on_event=lambda e: print("[ceo_run] " + json.dumps(e, default=str), flush=True))
    print(json.dumps(r, indent=2, default=str))
    sys.exit(0 if r.get("done") else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
