#!/usr/bin/env python3
"""orchestra.py — ⚠️ DEPRECATED / SUPERSEDED. Original in-memory prototype controller; the LIVE durable engine
is scripts/orchestra/runtime.py (`run_org` + controller logic). No production callers (selftest/demo only).
Do not build on it.

orchestra.py — the CONTROLLER (top actor) that wires the event-driven agent-org together.

Spec: docs/blueprint/AGENTIC-ORCHESTRATION.md.

This is the integration layer over the three substrate modules in scripts/orchestra/:
  - org_decider.plan_org(vision) — the AI that decomposes a VISION into a recursive org tree.
  - supervisor.{Bus,Supervisor,Worker} — the interrupt-driven, barrier-free reactor: leads decompose,
    spawn children on demand, handle each child event the instant it lands (resolve locally OR escalate),
    and aggregate when children are terminal.
  - (bus.py / actor.py provide the durable async-bus + parked/resume Actor variants; supervisor.py's
    single-threaded reactor is used here because it makes the end-to-end demo DETERMINISTIC.)

The Controller is the TOP ESCALATION TIER (§ execution model): it takes a vision, calls plan_org, spawns
the top supervisor(s), and RESOLVES what a supervisor cannot — enabling a capability / granting creds /
restarting a sub-fleet — with a HUMAN-CONSULT hook for the truly-beyond-AI case. Its resolution flows back
DOWN, the parked child RESUMES, and the correction is BROADCAST to the siblings so nobody works off stale
context. EVERY decision (plan / escalation-resolution / aggregate) is an AI call via factory.agent; cost is
not a concern. Escalation, resume and broadcast all work at every level (the tree is recursive + elastic).

    python orchestra.py selftest   # OFFLINE, deterministic (stubs factory.agent) — asserts the whole chain
    python orchestra.py demo       # OFFLINE, prints the who-said-what TIMELINE of the escalation flow
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import factory                                          # noqa: E402  — factory.agent is THE llm call
import org_decider                                      # noqa: E402  — plan_org / should_expand (AI)
from supervisor import Bus, Event, Actor, Supervisor, Worker, _ai_json   # noqa: E402


# ============================================================================ instrumented message bus
class LoggingBus(Bus):
    """Bus that also records a linear TIMELINE of every message posted + every actor's narration note, in
    true causal order (the reactor is single-threaded, so append order == the order things actually happen).
    This is purely for the audit/demo view; delivery semantics are unchanged from the base reactor Bus."""

    def __init__(self):
        super().__init__()
        self.timeline: list[dict] = []

    def post(self, to: str, event: Event, urgent: bool = False):
        super().post(to, event, urgent)
        self.timeline.append({"type": "msg", "frm": event.sender, "to": to,
                              "kind": event.kind, "payload": event.payload, "urgent": urgent})

    def note(self, who: str, text: str):
        """An actor narrates a decision it made between messages (e.g. 'this blocker is beyond me')."""
        self.timeline.append({"type": "note", "who": who, "text": text})


def _note(bus, who, text):
    """Narrate to the timeline if the bus supports it (LoggingBus); a plain Bus just no-ops."""
    fn = getattr(bus, "note", None)
    if callable(fn):
        fn(who, text)


# ============================================================================ leaf worker (IC)
class TeamWorker(Worker):
    """A leaf IC. Same contract as supervisor.Worker, plus: it narrates its BLOCKED / DONE / RESUME
    moments to the timeline, tracks whether it ever parked on a blocker, and ignores a redundant `start`
    once it has already completed (so a worker that finished via a broadcast correction isn't re-run)."""

    def __init__(self, name, role, repo, bus, supervisor, task):
        super().__init__(name, role, repo, bus, supervisor, task)
        self._was_blocked = False

    def on_event(self, event: Event):
        if event.kind == "start" and self.done:
            return                                       # already finished — ignore a stale start
        if event.kind == "context_update" and self._was_blocked and not self.done:
            _note(self.bus, self.name,
                  f"correction arrived ({_short(event.payload)}) -> RESUMING the parked work")
        super().on_event(event)

    def act(self):
        """One work attempt = one AI call. A factory blocker (no creds / denied) surfaces as a `blocked`
        event UP to the lead; otherwise `done` goes up. (Overrides the base only to narrate + set the
        _was_blocked resume flag; the messaging contract is identical.)"""
        self.attempts += 1
        ctx = ("\n\nCONTEXT/CORRECTIONS FROM LEAD:\n" + "\n".join(map(str, self.context))) if self.context else ""
        spawner = self.supervisor.role if self.supervisor else None
        r = factory.agent(self.role, self.repo, f"{self.task}{ctx}", spawner=spawner)
        if isinstance(r, dict) and (r.get("blocker") or r.get("failed")):
            self._was_blocked = True
            blk = r.get("blocker") or r.get("out")
            self._audit("WorkerBlocked", "blocked", {"task": self.task[:120]})
            _note(self.bus, self.name, f"hit a wall: {blk} -> emitting 'blocked' up to my lead")
            self.emit("blocked", {"task": self.task, "blocker": blk, "attempts": self.attempts})
        else:
            self.done = True
            out = (r.get("out_full") or r.get("out")) if isinstance(r, dict) else str(r)
            self._audit("WorkerDone", "executed", {"task": self.task[:120]})
            _note(self.bus, self.name, f"finished (attempt {self.attempts}): {_short(out)}")
            self.emit("done", {"task": self.task, "out": out})


# ============================================================================ team lead (interrupt-driven)
class TeamSupervisor(Supervisor):
    """A lead Supervisor tuned for the FULL escalate->resolve->resume->broadcast chain.

    Differences from the base Supervisor:
      - it spawns TeamWorker / TeamSupervisor children (so recursion keeps the resume/narrate behaviour);
      - when it ESCALATES a blocker, it does NOT mark the blocked child done — the child stays parked so it
        can RESUME once the controller's resolution comes back down (the base collapses it for aggregation);
      - it accepts a `resolve` message DOWN from its controller and turns it into the team-wide BROADCAST
        that unblocks the parked child and corrects every sibling (context_update)."""

    def __init__(self, name, role, repo, bus, supervisor=None):
        super().__init__(name, role, repo, bus, supervisor)
        self._blocked_child: str | None = None          # who is parked awaiting a resolution from above

    # spawn OUR actor types (keeps recursion + resume behaviour all the way down) -----------------------
    def spawn_child(self, spec: dict) -> Actor:
        name = spec.get("name") or f"{self.name}.c{len(self.children)}"
        spec["name"] = name
        self.child_specs[name] = spec
        if spec.get("kind") == "supervisor":
            child: Actor = TeamSupervisor(name, spec["role"], self.repo, self.bus, supervisor=self)
        else:
            child = TeamWorker(name, spec["role"], self.repo, self.bus, self, spec["task"])
        self.children[name] = child
        self._audit("SpawnChild", "executed", {"child": name, "kind": spec.get("kind"), "role": spec["role"]})
        _note(self.bus, self.name,
              f"spawned {spec.get('kind', 'worker')} {name} ({spec['role']}): {_short(spec['task'])}")
        return child

    # interrupt handler: also accept a resolution coming DOWN from the controller ------------------------
    def on_event(self, event: Event):
        if event.kind == "resolve" and self.supervisor is not None and event.sender == self.supervisor.name:
            self.inbox.append(event)
            self._apply_resolution(event.payload)
            self._maybe_aggregate()
            return
        super().on_event(event)

    # ESCALATE without collapsing the child, so it can resume -------------------------------------------
    def _escalate(self, event: Event, decision: dict):
        reason = decision.get("reason") or decision.get("_blocker") or "beyond supervisor capability"
        self._blocked_child = event.sender
        rec = {"from": event.sender, "kind": event.kind, "reason": reason, "payload": event.payload}
        self.escalations.append(rec)
        self._audit("Escalate", "escalated", rec)
        _note(self.bus, self.name,
              f"blocker from {event.sender} is BEYOND me -> ESCALATE to controller: {reason}")
        if self.supervisor is not None:
            self.emit("escalate", rec)                   # bubbles UP, only as far as needed
        # NOTE: deliberately do NOT mark the child done — it stays parked to RESUME on the resolution.

    def _apply_resolution(self, payload: dict):
        """The controller resolved our escalation. Turn the grant into a team-wide BROADCAST: correct every
        SIBLING first (so nobody works off stale context), then hand it to the PARKED child, which resumes."""
        grant = payload.get("grant") if isinstance(payload, dict) else payload
        origin = self._blocked_child
        _note(self.bus, self.name,
              f"controller resolved it (grant: {_short(grant)}); broadcasting correction to the team "
              f"and resuming {origin}")
        for name, child in self.children.items():        # siblings first
            if name != origin:
                self.send(name, "context_update", grant)
        if origin and origin in self.children:           # parked child last -> its resume triggers aggregate
            self.send(origin, "context_update", grant)
        self._blocked_child = None


# ============================================================================ the Controller (top actor)
class Controller(Supervisor):
    """The top actor + TOP ESCALATION TIER. Takes a VISION, calls org_decider.plan_org, spawns the top
    supervisor(s), and drives the interrupt-driven reactor. When a supervisor escalates something beyond
    its capability, the controller makes an AI decision to RESOLVE it (enable a capability / grant creds /
    restart a sub-fleet) — or, if it truly needs a person, invokes the HUMAN-CONSULT hook — then sends the
    resolution back down. It aggregates the supervisors' results into the final `done`."""

    def __init__(self, bus, repo=".", name="controller", role="controller", human_hook=None):
        super().__init__(name, role, repo, bus, supervisor=None)
        self.human_hook = human_hook                     # callable(event, decision) -> grant str (last resort)
        self.plan: dict | None = None

    # --- top-level driver ------------------------------------------------
    def run(self, vision: str) -> dict | None:
        """VISION -> plan org (AI) -> spawn top supervisor(s) -> drive the barrier-free reactor -> return
        the aggregated final result. Each top supervisor recursively decomposes + spawns its own team."""
        _note(self.bus, self.name, f"VISION received: {_short(vision, 90)}")
        self.plan = org_decider.plan_org(vision)
        domains = self.plan["root"]["children"]
        _note(self.bus, self.name,
              f"org planned: {len(domains)} domain(s) [{', '.join(d['name'] for d in domains)}] "
              f"-> spawning top supervisor(s)")
        started = []
        for dom in domains:
            sname = f"{self.name}:{dom['name']}"
            sup = TeamSupervisor(sname, dom.get("supervisor") or "supervisor", self.repo, self.bus,
                                 supervisor=self)
            self.children[sname] = sup
            self.child_specs[sname] = {"name": sname, "role": sup.role, "kind": "supervisor",
                                       "task": dom["name"]}
            task = f"Deliver the '{dom['name']}' domain toward the vision: {vision}"
            started.append((sup, task))
        for sup, task in started:                        # each supervisor decomposes + starts its team
            sup.run(task)
        self.bus.pump()                                  # the reactor drains every event to quiescence
        return self.aggregated

    # --- top escalation tier ---------------------------------------------
    def on_event(self, event: Event):
        if event.kind == "escalate":
            Actor.on_event(self, event)                  # record for audit
            self.escalations.append(event.payload)
            reason = event.payload.get("reason") if isinstance(event.payload, dict) else str(event.payload)
            _note(self.bus, self.name, f"received ESCALATION from {event.sender}: {reason}")
            self._handle_escalation(event)
            return
        super().on_event(event)                          # a supervisor's `done` -> record + aggregate/finalize

    def _handle_escalation(self, event: Event):
        """AI decision (the top-tier judgement): RESOLVE the blocker myself, or CONSULT THE HUMAN. Either
        way a grant is sent back DOWN to the escalating supervisor, which broadcasts it to unblock+correct."""
        decision = self._decide_escalation(event)
        action = (decision.get("action") or "resolve").lower()
        if action in ("consult_human", "ask_human", "human"):
            question = decision.get("message") or decision.get("question") or "approve capability grant?"
            if callable(self.human_hook):
                _note(self.bus, self.name, f"consulting HUMAN: {_short(question)}")
                grant = self.human_hook(event, decision)
            else:                                        # no human wired -> auto-approve (record it)
                grant = decision.get("grant") or "auto-approved (no human hook wired)"
                _note(self.bus, self.name, "no human hook wired -> auto-approving the grant")
        else:
            grant = decision.get("grant") or decision.get("message") or "capability granted"
            _note(self.bus, self.name, f"RESOLVING: granting '{_short(grant)}' back to {event.sender}")
        self.send(event.sender, "resolve", {"grant": grant, "for": event.payload})

    def _decide_escalation(self, event: Event) -> dict:
        """One AI call: the controller reasoning about an escalated blocker and choosing how to clear it."""
        return _ai_json(self.role, self.repo,
                        "You are the CONTROLLER — the TOP escalation tier of a recursive agent-org. A "
                        "supervisor escalated a blocker beyond its capability. Decide how to CLEAR it: "
                        "enable a capability / grant creds / restart a sub-fleet, or CONSULT THE HUMAN if it "
                        "truly needs a person. Reply ONLY JSON: "
                        '{"action":"resolve"|"consult_human",'
                        '"grant":"<capability or creds to hand back to the team>",'
                        '"message":"<what to tell the team / ask the human>"}. '
                        f"ESCALATION: {json.dumps(event.payload, default=str)[:800]}",
                        spawner=self.role)


# ============================================================================ helpers / rendering
def _short(x, n=64):
    s = x if isinstance(x, str) else json.dumps(x, default=str) if isinstance(x, (dict, list)) else str(x)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _summ(payload):
    if isinstance(payload, dict):
        for k in ("reason", "blocker", "grant", "result", "out", "task", "message"):
            if k in payload and payload[k]:
                return f"{k}={_short(payload[k], 70)}"
        return _short(payload, 70)
    return _short(payload, 70) if payload is not None else ""


def print_timeline(bus, title="ORCHESTRA DEMO — event-driven escalation timeline"):
    """Render the who-said-what timeline: every message (--kind-->) and every actor's decision note (·)."""
    print("\n" + "=" * 96)
    print(title)
    print("=" * 96)
    for i, e in enumerate(bus.timeline, 1):
        if e["type"] == "msg":
            arrow = f"--{e['kind']}-->"
            urgent = "  [urgent]" if e.get("urgent") else ""
            print(f"{i:>3}  {e['frm']:>22} {arrow:^18} {e['to']:<24} {_summ(e['payload'])}{urgent}")
        else:
            print(f"{i:>3}  {e['who']:>22}   ·  {e['text']}")
    print("=" * 96)


# ============================================================================ deterministic offline agent
def _make_demo_agent(controller_action="resolve"):
    """A deterministic, OFFLINE stand-in for factory.agent that drives the exact demo storyline. It routes
    on the prompt PREFIX (each AI decision point has a distinct opening), so the whole flow is reproducible
    with no model, no network, no spend. `controller_action` flips the top-tier decision between resolving
    directly and consulting the human (to exercise both paths)."""
    GRANT = "serviceX-cred=LIVE-OK (use v3 endpoint)"

    def agent(role, repo, task, **kw):
        t = task or ""
        # (1) org planning — a single 'payments' domain so the controller spawns one top supervisor.
        if t.startswith("You are the ORG ARCHITECT"):
            tree = {"vision": "payments service",
                    "root": {"kind": "controller", "title": "Controller", "children": [
                        {"kind": "domain", "name": "payments", "supervisor": "eng-director", "teams": [
                            {"kind": "team", "name": "core", "head": "head-of-pay",
                             "devs": [{"role": "backend-engineer", "name": "d1"}], "teams": []}]}]}}
            return {"rc": 0, "out_full": json.dumps(tree)}
        # (2) supervisor decompose -> TWO independent children; one needs creds for serviceX (will block).
        if t.startswith("You are a LEAD decomposing"):
            return {"rc": 0, "out_full": json.dumps({"org_note": "two independent endpoints", "children": [
                {"role": "backend-engineer", "kind": "worker",
                 "task": "Build the charge endpoint (needs serviceX API creds)"},
                {"role": "backend-engineer", "kind": "worker",
                 "task": "Build the refunds endpoint"}]})}
        # (3) supervisor per-event decision — a blocker is beyond the lead -> ESCALATE; else ack.
        if t.startswith("You are an interrupt-driven LEAD"):
            if "kind=blocked" in t:
                return {"rc": 0, "out_full": json.dumps(
                    {"action": "escalate",
                     "reason": "no API creds for serviceX — needs the controller to provision"})}
            return {"rc": 0, "out_full": json.dumps({"action": "ack"})}
        # (4) controller escalation decision — resolve directly, or consult the human.
        if t.startswith("You are the CONTROLLER"):
            if controller_action == "consult_human":
                return {"rc": 0, "out_full": json.dumps(
                    {"action": "consult_human", "message": "approve serviceX prod creds for the pay team?"})}
            return {"rc": 0, "out_full": json.dumps(
                {"action": "resolve", "grant": GRANT, "message": "serviceX creds provisioned; use v3"})}
        # (5) aggregate (supervisor or controller).
        if t.startswith("You are the LEAD. Synthesize"):
            return {"rc": 0, "out_full": json.dumps({"result": "charge + refund endpoints integrated", "ok": True})}
        # (6) a worker doing its task. The charge task blocks until the serviceX grant is in its context.
        if "serviceX" in t:
            if "serviceX-cred=LIVE-OK" in t:
                return {"rc": 0, "out_full": "charge endpoint built (serviceX v3 creds OK)"}
            return {"rc": -1, "failed": True, "out": "blocked", "blocker": "no API creds for serviceX"}
        return {"rc": 0, "out_full": f"done: {t[:48]}"}

    agent.GRANT = GRANT
    return agent


def run_demo(vision=None, controller_action="resolve", human_hook=None, verbose=True):
    """Run the full flow OFFLINE and (optionally) print the timeline. Returns (controller, bus, result)."""
    vision = vision or "A payments service: charge + refund endpoints, production-ready — one prompt to a working app."
    real = factory.agent
    factory.agent = _make_demo_agent(controller_action)
    try:
        bus = LoggingBus()
        controller = Controller(bus, repo="/tmp/orchestra-demo", human_hook=human_hook)
        result = controller.run(vision)
    finally:
        factory.agent = real
    if verbose:
        print_timeline(bus)
        print(f"\nController FINAL result: {json.dumps(result, default=str)}")
    return controller, bus, result


# ============================================================================ offline selftest
def _selftest():
    """OFFLINE, deterministic. Proves the whole event-driven escalation chain end to end, twice:
      Scenario 1 — controller RESOLVES autonomously.
      Scenario 2 — controller CONSULTS THE HUMAN via the hook.
    Both must show: plan -> 2 children -> one blocks MID-FLIGHT (sibling still outstanding) -> supervisor
    escalates -> controller resolves -> blocked child RESUMES -> correction BROADCAST to sibling -> all
    finish -> supervisor aggregates -> controller returns done."""
    ok = True

    def check(cond, label):
        nonlocal ok
        print(("PASS" if cond else "FAIL") + f": {label}")
        ok = ok and bool(cond)

    def assert_full_chain(controller, bus, result, tag):
        # plan happened, exactly one top supervisor spawned
        check(controller.plan is not None and len(controller.plan["root"]["children"]) == 1,
              f"[{tag}] controller planned the org (1 domain)")
        sups = [c for c in controller.children.values() if isinstance(c, TeamSupervisor)]
        check(len(sups) == 1, f"[{tag}] one top supervisor spawned")
        sup = sups[0]
        workers = [c for c in sup.children.values() if isinstance(c, TeamWorker)]
        check(len(workers) == 2, f"[{tag}] supervisor decomposed into 2 children")

        blocked = next((w for w in workers if w._was_blocked), None)
        sibling = next((w for w in workers if w is not blocked), None)
        check(blocked is not None and sibling is not None, f"[{tag}] one child blocked, one sibling")

        # the block was handled the INSTANT it arrived — while the sibling was still outstanding (no barrier)
        blk_handled = next((h for h in sup.handled if h["kind"] == "blocked"), None)
        check(bool(blk_handled) and blk_handled["action"] == "escalate"
              and sibling.name in blk_handled["outstanding"],
              f"[{tag}] blocker handled mid-flight (sibling still outstanding) -> escalate (no barrier)")

        # it bubbled to the controller as an `escalate`, and the controller recorded + resolved it
        escalate_up = any(e["type"] == "msg" and e["kind"] == "escalate" and e["to"] == controller.name
                          for e in bus.timeline)
        check(escalate_up and len(controller.escalations) >= 1, f"[{tag}] escalation reached the controller")
        resolve_down = any(e["type"] == "msg" and e["kind"] == "resolve"
                           and e["frm"] == controller.name and e["to"] == sup.name for e in bus.timeline)
        check(resolve_down, f"[{tag}] controller sent a resolution back DOWN to the supervisor")

        # the parked child RESUMED (2nd attempt) and finished
        check(blocked.attempts >= 2 and blocked.done, f"[{tag}] blocked child RESUMED and finished")

        # the correction was BROADCAST to the sibling (context_update landed in its inbox)
        sib_corrected = any(ev.kind == "context_update" for ev in sibling.inbox)
        check(sib_corrected, f"[{tag}] correction BROADCAST to the sibling (context_update)")

        # supervisor aggregated once every child was terminal, controller returned a final done
        check(sup.aggregated is not None and not sup._outstanding(), f"[{tag}] supervisor aggregated")
        check(result is not None and result.get("ok"), f"[{tag}] controller returned final done")

    # ---- Scenario 1: controller resolves autonomously --------------------------------------------------
    print("\n--- scenario 1: controller RESOLVES ---")
    c1, b1, r1 = run_demo(controller_action="resolve", verbose=False)
    assert_full_chain(c1, b1, r1, "resolve")

    # ---- Scenario 2: controller consults the HUMAN hook ------------------------------------------------
    print("\n--- scenario 2: controller CONSULTS HUMAN ---")
    called = {"hit": False}

    def human_hook(event, decision):
        called["hit"] = True
        return "serviceX-cred=LIVE-OK (use v3 endpoint) [granted by human]"

    c2, b2, r2 = run_demo(controller_action="consult_human", human_hook=human_hook, verbose=False)
    assert_full_chain(c2, b2, r2, "human")
    check(called["hit"], "[human] human-consult hook was invoked")

    print("\n" + ("ALL PASS ✅ — plan->escalate->resolve->resume->broadcast->aggregate proven offline"
                  if ok else "FAILURES ❌"))
    return 0 if ok else 1


def _main(argv):
    if not argv or argv[0] == "selftest":
        return _selftest()
    if argv[0] == "demo":
        run_demo(verbose=True)
        return 0
    print("usage: orchestra.py [selftest] | demo", file=sys.stderr)
    return 2


__all__ = ["Controller", "TeamSupervisor", "TeamWorker", "LoggingBus", "run_demo", "print_timeline"]


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
