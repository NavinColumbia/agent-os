#!/usr/bin/env python3
"""demo_e2e.py — drive a FRESH product through the ENTIRE CEO pipeline end-to-end, unattended, and report.

The "astonish a skeptic" harness. It plays the CEO: starts a thread, states the product, then auto-navigates
every human gate (answers clarifications, picks the recommended research direction, approves the plan) while
the fleet does the work (dispatch-and-park), phase by phase, until DELIVER — printing live progress the whole
way.

    python scripts/demo_e2e.py "a Calendly competitor for dog groomers"   # REAL run (spends; needs a
                                                                          # provider connected for the tenant)
    python scripts/demo_e2e.py --dry-run                                  # validate the DRIVER logic, no spend

Real runs default AOS_DISPATCH_PARK=1 so the whole thing showcases the durable detached-worker engine. Env:
  AOS_DEMO_TENANT (default 'demo')  AOS_DEMO_ORG (default 1)  AOS_DEMO_POLL_S (default 10)  AOS_DEMO_MAX_MIN (default 360)
"""
import json
import os
import sys
import time

POLL_S = float(os.environ.get("AOS_DEMO_POLL_S", "10"))
MAX_MIN = float(os.environ.get("AOS_DEMO_MAX_MIN", "360"))     # match the fleet's 6h hard ceiling
AFFIRM = "Go ahead — go with your recommendation and build it."


class LiveController:
    """Thin adapter over loopcontroller — the seam the dry-run simulator swaps out."""
    def __init__(self):
        import loopcontroller as lc
        self.lc = lc

    def start(self, tid, org):
        r = self.lc.start(tid, org)
        # lc.start returns {"thread_id":..,"phase":..}; the driver needs the thread_id itself.
        return r["thread_id"] if isinstance(r, dict) else r

    def say(self, tid, th, msg):
        return self.lc.say(tid, th, msg)

    def choose(self, tid, th, oid):
        return self.lc.choose(tid, th, oid)

    def state(self, th):
        return self.lc.state(th)

    def options(self, th):
        s = self.lc._st(th) or {}
        return s.get("options") or []


def _pick_option(options):
    """The recommended option's id, else the first. Cards vary in shape — try the common id keys, fall back
    to the leading option so the demo never stalls at the OPTIONS gate."""
    if not options:
        return None
    rec = next((o for o in options if isinstance(o, dict) and o.get("recommended")), options[0])
    if isinstance(rec, dict):
        return rec.get("id") or rec.get("option_id") or rec.get("title")
    return rec


def drive(ctrl, tid, org, brief, poll_s=None, max_min=None, on_event=lambda e: None):
    """Play the CEO end-to-end. Returns {ok, phase, ...}. Never raises on a gate — an unfixable one (a missing
    provider) is reported as blocked so the caller sees WHY, not a hang."""
    poll_s = POLL_S if poll_s is None else poll_s
    deadline = time.time() + (MAX_MIN if max_min is None else max_min) * 60
    th = ctrl.start(tid, org)
    on_event({"event": "started", "thread": th, "brief": brief})
    ctrl.say(tid, th, brief)
    last = None
    while time.time() < deadline:
        s = ctrl.state(th) or {}
        phase, awaiting = s.get("phase"), s.get("awaiting")
        sig = (phase, awaiting, s.get("job_kind"))
        if sig != last:
            on_event({"event": "state", "phase": phase, "awaiting": awaiting,
                      "running": s.get("running"), "live": s.get("live"), "elapsed_min": s.get("elapsed_min")})
            last = sig
        if phase == "DELIVER":
            return {"ok": True, "phase": "DELIVER", "product": s.get("product")}
        if awaiting == "credentials":
            return {"ok": False, "blocked": "credentials",
                    "detail": "connect a model provider for this tenant (Settings → Providers), then re-run"}
        if phase == "OPTIONS":
            oid = _pick_option(ctrl.options(th))
            ctrl.choose(tid, th, oid)
            on_event({"event": "chose_option", "option": oid})
            time.sleep(min(poll_s, 2))
        elif awaiting in ("user_feedback", "user_approval"):
            ctrl.say(tid, th, AFFIRM)
            on_event({"event": "approved_gate", "awaiting": awaiting})
            time.sleep(min(poll_s, 2))
        else:                                       # 'fleet' / running / transient — let the work proceed
            time.sleep(poll_s)
    return {"ok": False, "blocked": "timeout", "detail": f"did not reach DELIVER within {MAX_MIN} min"}


# ── dry-run: an in-memory controller that validates the driver navigates every gate, no spend ──────────
class _SimController:
    STEPS = ["DISCOVER/user_feedback", "RESEARCH/fleet", "OPTIONS/user_approval",
             "DEEP_DESIGN/user_feedback", "PLAN_APPROVAL/user_approval",
             "PROTOTYPE/fleet", "IMPLEMENT/fleet", "TESTQA/fleet", "DELIVER/none"]

    def __init__(self):
        self.i = 0
        self.saw_choose = False
        self.gate_actions = 0

    def _cur(self):
        p, a = self.STEPS[self.i].split("/")
        return p, (None if a == "none" else a)

    def start(self, tid, org):
        return 1

    def options(self, th):
        return [{"id": "opt-a", "recommended": True}, {"id": "opt-b"}]

    def state(self, th):
        p, a = self._cur()
        if a == "fleet" and self.i < len(self.STEPS) - 1:     # the fleet is working -> it completes, advance
            self.i += 1
            p, a = self._cur()
        return {"phase": p, "awaiting": a, "product": "demo-sim", "job_kind": None}

    def say(self, tid, th, msg):
        p, a = self._cur()
        if a in ("user_feedback", "user_approval"):
            self.gate_actions += 1
            self.i += 1
        return {}

    def choose(self, tid, th, oid):
        if self._cur()[0] == "OPTIONS":
            self.saw_choose = True
            self.i += 1
        return {}


def _dry_run():
    sim = _SimController()
    events = []
    r = drive(sim, "sim", 1, "a demo product", poll_s=0, max_min=1, on_event=events.append)
    phases = [e["phase"] for e in events if e.get("event") == "state"]
    ok = (r.get("ok") and r.get("phase") == "DELIVER" and sim.saw_choose
          and "OPTIONS" in phases and "IMPLEMENT" in phases)
    print(json.dumps({"result": r, "phases_seen": phases, "chose_option": sim.saw_choose,
                      "gate_actions": sim.gate_actions}, indent=2))
    print("demo_e2e --dry-run:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        sys.exit(_dry_run())
    brief = " ".join(a for a in sys.argv[1:] if not a.startswith("-")).strip()
    if not brief:
        sys.exit('usage: demo_e2e.py "<product idea>"   |   demo_e2e.py --dry-run')
    os.environ.setdefault("AOS_DISPATCH_PARK", "1")           # showcase the durable detached-worker engine
    tid = os.environ.get("AOS_DEMO_TENANT", "demo")
    org = int(os.environ.get("AOS_DEMO_ORG", "1"))
    print(f"[demo] driving a full build for tenant={tid} org={org}, park={os.environ['AOS_DISPATCH_PARK']}")
    res = drive(LiveController(), tid, org, brief,
                on_event=lambda e: print("[demo] " + json.dumps(e, default=str), flush=True))
    print(json.dumps(res, indent=2, default=str))
    sys.exit(0 if res.get("ok") else 1)
