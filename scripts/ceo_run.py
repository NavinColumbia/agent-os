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
    ceo_run.py preflight "<prompt>" [--tenant T]  no-spend readiness gate + run plan
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
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import loopcontroller as lc  # noqa: E402
from dbpool import connection  # noqa: E402


def _notify(msg, title="agent-os · your company", priority="default", tag="robot"):
    try:
        import notify
        notify.send(msg, title=title, priority=priority, tags=tag)
    except Exception:
        pass


def _process_count(pattern):
    try:
        r = subprocess.run(["pgrep", "-fc", pattern], capture_output=True, text=True, timeout=5)
        return int((r.stdout or "").strip() or "0")
    except Exception:
        return 0


def _db_ok():
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False


def _tenant_exists(tenant):
    try:
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT 1 FROM tenants WHERE tenant_id=%s", (tenant,))
            return cur.fetchone() is not None
    except Exception:
        return False


def _consent_ok(tenant):
    try:
        import consent
        return bool(consent.require_consent(tenant))
    except Exception:
        return False


def _provider_ok(tenant):
    try:
        import auth
        return bool(auth.provider_resolved(tenant))
    except Exception:
        return False


def _halted(scope):
    try:
        import killswitch
        return bool(killswitch.is_halted(scope).get("halted"))
    except Exception:
        return False


def _billing_snapshot(tenant):
    """Read-only billing/quota readiness for preflight.

    Do not call billing.quota() here: that enforcement path may auto-suspend a tenant. Preflight is a gate/report,
    not a mutating billing sweep.
    """
    try:
        import billing
        billing._ensure()
        with connection() as c, c.cursor() as cur:
            cur.execute("SELECT plan, suspended, period_start FROM tenants WHERE tenant_id=%s", (tenant,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "reason": "tenant_not_found"}
            plan, suspended, start = row
            p = billing.PLANS.get(plan, billing.PLANS["free"])
            cur.execute("SELECT product FROM tenant_products WHERE tenant_id=%s", (tenant,))
            prods = [r[0] for r in cur.fetchall()]
            end = "infinity"
            builds, tokens = billing._usage_window(cur, prods, start, end)
        over_builds = max(0, builds - p["builds"])
        over_tokens = max(0, tokens - p["tokens"])
        no_overage = p["ov_build"] == 0 and p["ov_1k_tok"] == 0
        blocks = bool(suspended) or (no_overage and (over_builds > 0 or over_tokens > 0))
        return {
            "ok": not blocks,
            "plan": plan,
            "suspended": bool(suspended),
            "products": len(prods),
            "builds": int(builds),
            "tokens": int(tokens),
            "included_builds": int(p["builds"]),
            "included_tokens": int(p["tokens"]),
            "over_builds": int(over_builds),
            "over_tokens": int(over_tokens),
            "billable_overage": bool((over_builds > 0 or over_tokens > 0) and not no_overage),
            "reason": "suspended" if suspended else ("over_quota_no_overage_plan" if blocks else ""),
        }
    except Exception as e:
        return {"ok": False, "reason": f"billing_check_failed: {str(e)[:160]}"}


def _build_budget_ok():
    try:
        return float(getattr(lc, "BUILD_BUDGET_USD", 0) or 0) > 0
    except Exception:
        return False


def _critical_ops_alerts():
    try:
        import dashboard
        st = dashboard.state() or {}
        return [a for a in st.get("alerts", []) if a.get("level") == "crit"]
    except Exception as e:
        return [{"level": "crit", "msg": f"ops_health_check_failed: {str(e)[:160]}"}]


def _watchdog_critical_issues():
    try:
        import contextlib
        import io
        import watchdog
        with contextlib.redirect_stdout(io.StringIO()):
            issues = watchdog.check() or []
        return [i for i in issues if i.get("level") == "crit"]
    except Exception as e:
        return [{"level": "crit", "msg": f"watchdog_check_failed: {str(e)[:160]}"}]


def preflight(prompt, tenant="demo", org=1):
    """No-spend readiness gate for the expensive one-shot live proof.

    This does not start a controller thread, write a prompt, launch browser/model work, or notify the CEO. It
    answers whether the durable one-prompt -> product flow is safe to attempt now, with the exact command and
    evidence surfaces to watch if it is green.
    """
    prompt = (prompt or "").strip()
    billing_snapshot = _billing_snapshot(tenant) if tenant else {"ok": False, "reason": "missing_tenant"}
    critical_ops_alerts = _critical_ops_alerts()
    watchdog_critical_issues = _watchdog_critical_issues()
    checks = {
        "prompt_present": bool(prompt),
        "db_reachable": _db_ok(),
        "tenant_exists": bool(tenant) and _tenant_exists(tenant),
        "ai_consent_on_file": bool(tenant) and _consent_ok(tenant),
        "model_provider_resolved": bool(tenant) and _provider_ok(tenant),
        "billing_quota_ready": bool(billing_snapshot.get("ok")),
        "build_budget_configured": _build_budget_ok(),
        "no_critical_ops_alerts": not critical_ops_alerts,
        "no_watchdog_critical_issues": not watchdog_critical_issues,
        "jobd_running": _process_count("jobd.py serve") > 0,
        "reply_listener_running": _process_count("reply_listener.py") > 0,
        "replybridge_running": _process_count("replybridge.py serve") > 0,
        "global_kill_switch_clear": not _halted("global"),
        "tenant_kill_switch_clear": not _halted(str(tenant)),
    }
    command = f"{sys.executable} {Path(__file__).resolve()} {json.dumps(prompt)} --tenant {tenant} --org {int(org)}"
    return {
        "ok": all(checks.values()),
        "tenant": tenant,
        "org": int(org),
        "prompt_preview": prompt[:160],
        "command": command,
        "checks": checks,
        "billing": billing_snapshot,
        "build_budget_usd": float(getattr(lc, "BUILD_BUDGET_USD", 0) or 0),
        "critical_ops_alerts": critical_ops_alerts,
        "watchdog_critical_issues": watchdog_critical_issues,
        "artifacts": [
            "controller_state row for the new thread",
            "controller_jobs rows for parked/durable phase work",
            "audit_log rows for ControllerStart, phase decisions, ProductComplete",
            "traces rows for agent/model/test work",
            "notifications/feed or phone pings for CEO decisions and delivery",
            "pulse.live() rows while QA/build/fleet work is active",
        ],
        "monitoring": [
            "scripts/ceo_run.py watch output",
            "scripts/dashboard.py state alerts/pulse/messages",
            "scripts/watchdog.py check for stalled work",
            "scripts/scheduler.py list and scheduler_runs if recovery loops fail",
        ],
        "stop_conditions": [
            "any preflight check is false",
            "dashboard reports a critical ops alert",
            "watchdog reports DB/scheduler/jobd/pulse stall",
            "controller awaits credentials/consent",
            "budget or kill-switch blocks the product",
            "QA/review blocks the build; fix findings before claiming the live proof",
        ],
    }


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
    helper_names = ("_db_ok", "_tenant_exists", "_consent_ok", "_provider_ok", "_billing_snapshot",
                    "_build_budget_ok", "_critical_ops_alerts", "_watchdog_critical_issues",
                    "_process_count", "_halted")
    real_helpers = {name: globals()[name] for name in helper_names}
    prior_notify_mod = sys.modules.get("notify")
    notify_mod = prior_notify_mod or types.ModuleType("notify")
    prior_notify_send = getattr(notify_mod, "send", None)
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
        globals()["_db_ok"] = lambda: True
        globals()["_tenant_exists"] = lambda tenant: tenant == "acme"
        globals()["_consent_ok"] = lambda tenant: True
        globals()["_provider_ok"] = lambda tenant: True
        globals()["_billing_snapshot"] = lambda tenant: {"ok": True, "plan": "free"}
        globals()["_build_budget_ok"] = lambda: True
        globals()["_critical_ops_alerts"] = lambda: []
        globals()["_watchdog_critical_issues"] = lambda: []
        globals()["_process_count"] = lambda pat: 1
        globals()["_halted"] = lambda scope: False
        pf = preflight("build a todo app", tenant="acme", org=1)
        globals()["_provider_ok"] = lambda tenant: False
        blocked_pf = preflight("build a todo app", tenant="acme", org=1)
        globals()["_provider_ok"] = lambda tenant: True
        th = submit("build a todo app", tenant="acme", org=1)
        assert th == 7 and said.get("m") == "build a todo app", (th, said)
        r = watch("acme", 7, poll_s=0, max_min=1)
        decision_pings = [p for p in pings if "needs you" in p[0]]
        deliver_pings = [p for p in pings if "delivered" in p[0]]
        ok = (pf.get("ok") and blocked_pf.get("ok") is False
              and blocked_pf["checks"]["model_provider_resolved"] is False
              and r.get("done") and len(decision_pings) == 1        # pinged ONCE for the decision (deduped)
              and len(deliver_pings) == 1)                       # pinged on delivery
        print(f"preflight_ok={pf.get('ok')} preflight_provider_blocks={not blocked_pf.get('ok')} "
              f"submitted={th} decision_pings={len(decision_pings)} deliver_pings={len(deliver_pings)} done={r.get('done')}")
        print("ceo_run selftest: PASS (durable submit; proactive ping once per decision + on deliver; jobd "
              "drives; preflight gates live spend) ✅" if ok else "ceo_run selftest: FAIL")
        return 0 if ok else 1
    finally:
        lc.start, lc.say, lc.state = real
        globals().update(real_helpers)
        # This selftest is also called in-process by pytest.  Never leave its fake pager installed in the
        # shared module cache: doing so can make later notification-safety tests (or callers) observe a
        # successful no-op instead of the real transport guard.
        if prior_notify_mod is None:
            sys.modules.pop("notify", None)
        elif prior_notify_send is not None:
            prior_notify_mod.send = prior_notify_send


def _main(argv):
    if not argv or argv[0] == "selftest":
        sys.exit(_selftest())
    tenant = argv[argv.index("--tenant") + 1] if "--tenant" in argv else os.environ.get("AOS_CEO_TENANT", "demo")
    org = int(argv[argv.index("--org") + 1]) if "--org" in argv else 1
    if argv[0] == "preflight":
        prompt = " ".join(a for a in argv[1:] if not a.startswith("--") and a not in (tenant, str(org))).strip()
        pf = preflight(prompt, tenant, org)
        print(json.dumps(pf, indent=2, default=str))
        sys.exit(0 if pf.get("ok") else 1)
    if argv[0] == "submit":
        prompt = " ".join(a for a in argv[1:] if not a.startswith("--") and a not in (tenant, str(org))).strip()
        if not prompt:
            sys.exit('usage: ceo_run.py submit "<prompt>" [--tenant T] [--org N]')
        pf = preflight(prompt, tenant, org)
        if not pf.get("ok"):
            print(json.dumps({"blocked": "preflight", "preflight": pf}, indent=2, default=str))
            sys.exit(1)
        th = submit(prompt, tenant, org)
        print(json.dumps({"submitted": True, "thread": th, "note": "jobd is driving it; you'll be pinged for decisions + at delivery"}))
        return
    prompt = " ".join(a for a in argv if not a.startswith("--") and a not in (tenant, str(org))).strip()
    if not prompt:
        sys.exit('usage: ceo_run.py "<prompt>" [--tenant T] [--org N]')
    pf = preflight(prompt, tenant, org)
    if not pf.get("ok"):
        print(json.dumps({"blocked": "preflight", "preflight": pf}, indent=2, default=str))
        sys.exit(1)
    th = submit(prompt, tenant, org)
    print(f"[ceo_run] thread {th} submitted — jobd driving. Watching for decisions + delivery…", flush=True)
    r = watch(tenant, th, on_event=lambda e: print("[ceo_run] " + json.dumps(e, default=str), flush=True))
    print(json.dumps(r, indent=2, default=str))
    sys.exit(0 if r.get("done") else 1)


if __name__ == "__main__":
    _main(sys.argv[1:])
