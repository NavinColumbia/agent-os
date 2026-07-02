#!/usr/bin/env python3
"""supervisor.py — the interrupt-driven Supervisor: a LEAD in the event-driven agent-org.

Spec: docs/blueprint/AGENTIC-ORCHESTRATION.md § "Execution model: event-driven actors + hierarchical
escalation (NOT barriers)" and § "Recursive, elastic org scaling".

The single biggest shift from today's fleets (fan-out -> wait-for-ALL -> aggregate) is that this is an
ASYNC ACTOR SYSTEM with HIERARCHICAL ESCALATION. A Supervisor is a lead Actor that:

  (1) decompose(task)  — one AI call splits the task into subtasks AND decides HOW MANY children it needs
      and WHETHER any child should itself be a sub-Supervisor (recursive, elastic org — bias to expand).
  (2) spawns children  — dynamically, on demand: it can spawn MORE later when a child emits `need_agent`.
  (3) is INTERRUPT-DRIVEN — on_event(child_msg) fires the INSTANT a child's event arrives (even while
      OTHER children are still running; NO barrier). Each such handling is an AI decision that either
      RESOLVES locally (unblock / re-brief / spawn a helper / hand the next task / BROADCAST a correction
      or context_update to the OTHER children) OR ESCALATES upward (`escalate` to its own supervisor /
      the controller) when the blocker is beyond this lead's capability.
  (4) aggregate() — an AI call synthesizes the children's results once they are all terminal, and the
      Supervisor emits its own `done` (with the synthesis) up to ITS supervisor.

Recursive: a child can itself be a Supervisor, which decomposes its own subtask into its own children —
arbitrary depth/width, the tree grows itself (Head of Pay / Head of Wallet under a fintech supervisor).

EVERY decision here is an AI call (`factory.agent`; cost is not a constraint). Persistence/audit reuse
`audit.append` (and, best-effort, orchestrator.conversations) so the whole run is auditable — all logging
is fail-open so an offline/DB-less selftest is cheap and deterministic.

    python supervisor.py selftest      # OFFLINE: stubs factory.agent, no spend, deterministic
"""
from __future__ import annotations

import json
import sys
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import factory  # noqa: E402  — factory.agent(role, repo, task, ...) is THE llm call

try:                                    # audit is best-effort; never let logging brick the fleet
    import audit                        # noqa: E402
except Exception:                       # pragma: no cover
    audit = None

# Event kinds a child (IC or sub-supervisor) can emit UP to its supervisor at ANY time (not only at
# completion). See spec § execution model. A supervisor RESOLVES or ESCALATES each the instant it lands.
CHILD_EVENTS = {"done", "next?", "blocked", "finding", "question", "need_agent", "need_context", "escalate"}

# Actions a supervisor's AI decision can take on a child event. resolve-locally vs escalate-upward.
RESOLVE_ACTIONS = {"unblock", "rebrief", "spawn_helper", "hand_next", "broadcast", "ack"}


# --------------------------------------------------------------------------- message bus + Actor base
@dataclass
class Event:
    """One asynchronous message on the bus. `sender`/`to` are actor names."""
    kind: str
    sender: str
    payload: Any = None
    to: Optional[str] = None
    mid: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


class Bus:
    """A tiny single-threaded reactor: a FIFO of (recipient, Event). It is genuinely interrupt-driven
    and BARRIER-FREE — events are processed one at a time as they arrive, so a child's `blocked` is
    handled the instant it reaches the front of the queue, WITHOUT waiting for slower siblings to finish.
    (A thread-per-actor backing could replace this; the reactor keeps the selftest deterministic.)"""

    def __init__(self):
        self._q: deque = deque()
        self.actors: dict[str, "Actor"] = {}
        self.trace: list[tuple[str, str, str]] = []   # (to, kind, sender) — audit of every message

    def register(self, actor: "Actor"):
        self.actors[actor.name] = actor

    def post(self, to: str, event: Event, urgent: bool = False):
        """Enqueue a message. `urgent` events (a child's emit UP to its supervisor) go to the FRONT — an
        INTERRUPT preempts queued background work, so a `blocked` is handled BEFORE a not-yet-started
        sibling runs. Directives DOWN (starts / tasks / broadcasts) are normal background work at the back."""
        event.to = to
        if urgent:
            self._q.appendleft((to, event))
        else:
            self._q.append((to, event))
        self.trace.append((to, event.kind, event.sender))

    def pump(self, max_steps: int = 10000):
        """Drain the queue, delivering each event to its recipient's on_event. New events emitted during
        handling are appended and drained in turn — so resolutions (unblock/broadcast/spawn) and the
        follow-on work they trigger all flow without any global stop."""
        steps = 0
        while self._q and steps < max_steps:
            to, ev = self._q.popleft()
            steps += 1
            actor = self.actors.get(to)
            if actor is not None:
                actor.on_event(ev)


class Actor:
    """Autonomous actor with its own decide-loop. Has a name, a role (its charter), a repo (workspace),
    a bus, and a reference to its `supervisor` (parent). It can emit() an event UP to its supervisor or
    send() a message to any actor. on_event() is the interrupt handler (override)."""

    def __init__(self, name: str, role: str, repo: str, bus: Bus, supervisor: Optional["Actor"] = None):
        self.name = name
        self.role = role
        self.repo = repo
        self.bus = bus
        self.supervisor = supervisor
        self.inbox: list[Event] = []          # everything this actor has received (audit)
        self.done = False
        bus.register(self)

    # --- messaging -------------------------------------------------------
    def emit(self, kind: str, payload: Any = None):
        """Send an event UP to my supervisor (the escalation/report path) — an INTERRUPT (urgent)."""
        if self.supervisor is not None:
            self.bus.post(self.supervisor.name, Event(kind, self.name, payload), urgent=True)

    def send(self, actor_name: str, kind: str, payload: Any = None):
        """Send a message to a SPECIFIC actor (used to hand tasks / broadcast context to siblings)."""
        self.bus.post(actor_name, Event(kind, self.name, payload))

    def on_event(self, event: Event):        # override
        self.inbox.append(event)

    # --- persistence/audit (fail-open so offline selftest is deterministic) ---
    def _audit(self, action: str, decision: str = "executed", payload: Any = None):
        if audit is None:
            return
        try:
            audit.append(actor=f"orchestra:{self.name}", action=action, resource=self.role,
                         decision=decision, payload=payload or {})
        except Exception:
            pass


# --------------------------------------------------------------------------- AI helper
def _ai_json(role: str, repo: str, prompt: str, spawner: Optional[str] = None) -> dict:
    """Make an AI decision via factory.agent and parse a single JSON object out of the reply. EVERY
    supervisor decision routes through here (decompose / on_event / aggregate). Robust to code-fenced or
    chatty replies; returns {} on a hard parse miss so the caller can fall back safely."""
    r = factory.agent(role, repo, prompt, spawner=spawner)
    txt = (r.get("out_full") or r.get("out") or "") if isinstance(r, dict) else str(r)
    # carry through a hard factory blocker (budget/consent/governance) as an escalate signal
    if isinstance(r, dict) and r.get("blocker"):
        return {"_blocker": r["blocker"]}
    s, e = txt.find("{"), txt.rfind("}")
    if s < 0 or e <= s:
        return {}
    try:
        return json.loads(txt[s:e + 1])
    except Exception:
        return {}


# --------------------------------------------------------------------------- Worker (leaf IC actor)
class Worker(Actor):
    """A leaf individual-contributor actor. It does its task via an AI call; the RESULT of that call is
    the agent making an AI decision about its own state — a factory `blocker` (no creds / denied / stuck)
    surfaces as a `blocked` event, otherwise it emits `done`. On a resolution from its lead
    (unblock / rebrief / context_update / a new task) it re-attempts, so the loop closes without a barrier."""

    def __init__(self, name, role, repo, bus, supervisor, task: str):
        super().__init__(name, role, repo, bus, supervisor)
        self.task = task
        self.context: list[Any] = []          # corrections/context broadcast down from the lead
        self.attempts = 0

    def on_event(self, event: Event):
        super().on_event(event)
        if event.kind in ("start", "task", "brief", "context_update", "unblock", "rebrief"):
            if event.kind == "task" and event.payload:
                self.task = event.payload if isinstance(event.payload, str) else event.payload.get("task", self.task)
                self.done = False               # a NEW task re-activates the IC
            elif event.payload is not None:
                self.context.append(event.payload)
            # A correction to an ALREADY-DONE worker is just recorded (don't redo finished work); a new
            # task, a start, or a correction to a still-working/blocked IC (re)runs the work.
            if not self.done or event.kind in ("start", "task"):
                self.act()

    def act(self):
        self.attempts += 1
        ctx = ("\n\nCONTEXT/CORRECTIONS FROM LEAD:\n" + "\n".join(map(str, self.context))) if self.context else ""
        r = factory.agent(self.role, self.repo, f"{self.task}{ctx}", spawner=self.supervisor.role if self.supervisor else None)
        if isinstance(r, dict) and (r.get("blocker") or r.get("failed")):
            self._audit("WorkerBlocked", "blocked", {"task": self.task[:120]})
            self.emit("blocked", {"task": self.task, "blocker": r.get("blocker") or r.get("out"), "attempts": self.attempts})
        else:
            self.done = True
            out = r.get("out_full") or r.get("out") if isinstance(r, dict) else str(r)
            self._audit("WorkerDone", "executed", {"task": self.task[:120]})
            self.emit("done", {"task": self.task, "out": out})


# --------------------------------------------------------------------------- Supervisor (the lead)
class Supervisor(Actor):
    """The interrupt-driven lead. Decomposes a task (AI), spawns children on demand (AI decides how many
    and whether any are sub-Supervisors), handles each child event the instant it arrives (AI decides:
    resolve locally or escalate), and aggregates (AI) when the children are terminal."""

    def __init__(self, name, role, repo, bus, supervisor=None):
        super().__init__(name, role, repo, bus, supervisor)
        self.children: dict[str, Actor] = {}
        self.child_specs: dict[str, dict] = {}
        self.results: dict[str, Any] = {}         # child name -> done payload
        self.escalations: list[dict] = []         # things we escalated (or that bubbled up unresolved)
        self.handled: list[dict] = []             # ordered log of on_event decisions (for audit/tests)
        self.task: Optional[str] = None
        self.aggregated: Optional[dict] = None
        self._task_seq = 0

    # --- (1) decompose --------------------------------------------------
    def decompose(self, task: str) -> list[dict]:
        """AI call: split `task` into subtasks and DECIDE the org shape — how many children, and which (if
        any) should be sub-Supervisors (recursive expansion; bias toward expanding when the scope is large).
        Returns a list of child specs: {name, role, kind: 'worker'|'supervisor', task}."""
        self.task = task
        self._audit("Decompose", "executed", {"task": task[:160]})
        d = _ai_json(self.role, self.repo,
                     "You are a LEAD decomposing a task for your team in a recursive, elastic agent-org. "
                     "Decide (a) the INDEPENDENT subtasks, (b) HOW MANY children to staff, and (c) for each "
                     "child whether it is a single IC ('worker') or, if its subtask is itself broad enough to "
                     "need its OWN team, a sub-lead ('supervisor'). Bias toward EXPANDING structure when the "
                     "scope is large (cost is not a constraint). Reply ONLY JSON:\n"
                     '{"org_note":"...", "children":[{"role":"<role>","kind":"worker|supervisor","task":"..."}]}\n'
                     f"TASK:\n{task}", spawner=self.role)
        specs = d.get("children") or []
        norm = []
        for i, c in enumerate(specs):
            if not isinstance(c, dict) or not c.get("task"):
                continue
            norm.append({"name": f"{self.name}.c{i}", "role": c.get("role") or self.role,
                         "kind": "supervisor" if c.get("kind") == "supervisor" else "worker",
                         "task": c["task"]})
        if not norm:                              # never crash on a parse miss — degrade to a single IC
            norm = [{"name": f"{self.name}.c0", "role": self.role, "kind": "worker", "task": task}]
        self.child_specs = {c["name"]: c for c in norm}
        return norm

    # --- (2) spawn ------------------------------------------------------
    def spawn_child(self, spec: dict) -> Actor:
        """Instantiate one child actor and register it. A 'supervisor' child is itself a Supervisor that
        will decompose its own subtask (RECURSION). Callable mid-flight to add capacity on demand."""
        name = spec.get("name") or f"{self.name}.c{len(self.children)}"
        spec["name"] = name
        self.child_specs[name] = spec
        if spec.get("kind") == "supervisor":
            child: Actor = Supervisor(name, spec["role"], self.repo, self.bus, supervisor=self)
        else:
            child = Worker(name, spec["role"], self.repo, self.bus, self, spec["task"])
        self.children[name] = child
        self._audit("SpawnChild", "executed", {"child": name, "kind": spec.get("kind"), "role": spec["role"]})
        return child

    def spawn_children(self, specs: list[dict]):
        for s in specs:
            self.spawn_child(s)

    def _start_child(self, child: Actor, spec: dict):
        """Kick a child off. A Worker gets a `start`; a sub-Supervisor is handed its subtask to run its
        own decompose->spawn->aggregate recursively."""
        if isinstance(child, Supervisor):
            child.run(spec["task"])
        else:
            self.bus.post(child.name, Event("start", self.name, None))

    # --- (3) interrupt-driven handling ----------------------------------
    def on_event(self, event: Event):
        """THE interrupt handler. Fires the instant a child's event lands — even while other children run.
        Makes an AI decision to RESOLVE locally or ESCALATE upward, then records the child's result if it
        is `done` and aggregates once every child is terminal. NEVER waits for a barrier."""
        super().on_event(event)
        if event.sender not in self.children and event.kind not in ("start", "task", "brief", "context_update"):
            # not one of my children (e.g. a directive from MY supervisor) — ignore/record
            return
        if event.kind == "done":
            self.results[event.sender] = event.payload

        decision = self._decide(event)
        action = decision.get("action", "ack")
        self.handled.append({"from": event.sender, "kind": event.kind, "action": action,
                             "outstanding": self._outstanding()})
        self._audit("HandleEvent", action, {"from": event.sender, "kind": event.kind})

        if decision.get("_blocker") or action == "escalate":
            self._escalate(event, decision)
        elif action == "broadcast":
            self._broadcast(decision.get("message") or decision.get("context"), origin=event.sender)
        elif action == "spawn_helper":
            self._spawn_helper(decision.get("spec") or {"role": self.role, "task": decision.get("task", "assist")})
        elif action == "hand_next":
            self._hand_next(event.sender, decision.get("task"))
        elif action in ("unblock", "rebrief"):
            self.send(event.sender, "context_update", decision.get("message") or "proceed")
        # 'ack' / finding / next?: recorded; no side effect required

        self._maybe_aggregate()

    def _decide(self, event: Event) -> dict:
        """AI call: given the child event, decide how to RESOLVE or ESCALATE. This is the core agentic
        judgement — a lead reasoning about one report and choosing to unblock, re-brief, spawn a helper,
        hand the next task, broadcast a correction to the whole team, or escalate beyond its capability."""
        return _ai_json(self.role, self.repo,
                        "You are an interrupt-driven LEAD. A child just emitted an event. Decide ONE action "
                        "and reply ONLY JSON. RESOLVE locally when you can: "
                        '"unblock"/"rebrief" (send guidance to the child; include "message"), '
                        '"spawn_helper" (staff a new agent; include "spec":{"role","task"}), '
                        '"hand_next" (give the child its next task; include "task"), '
                        '"broadcast" (a correction/context EVERY sibling must get; include "message"), '
                        '"ack" (note it, no action). ESCALATE ("action":"escalate", include "reason") ONLY '
                        "when the blocker is beyond your capability (needs a capability enabled, a sub-fleet "
                        "restart, or a human). "
                        f'EVENT: kind={event.kind} from={event.sender} payload={json.dumps(event.payload)[:800]}',
                        spawner=self.role)

    # --- resolution primitives -----------------------------------------
    def _broadcast(self, message: Any, origin: Optional[str] = None):
        """Propagate a correction/context_update to ALL live children (including the origin, so it can
        retry) — so no sibling keeps working off stale context. Spec § inter-agent context-sharing."""
        self._audit("Broadcast", "executed", {"origin": origin, "msg": str(message)[:160]})
        for name, child in self.children.items():
            if not child.done:
                self.send(name, "context_update", message)

    def _spawn_helper(self, spec: dict):
        """Dynamic spawn ON DEMAND (mid-flight) — e.g. in response to a child's `need_agent`."""
        spec.setdefault("name", f"{self.name}.h{len(self.children)}")
        spec.setdefault("kind", "worker")
        spec.setdefault("role", self.role)
        child = self.spawn_child(spec)
        self._start_child(child, spec)

    def _hand_next(self, child_name: str, task: Optional[str]):
        self._task_seq += 1
        self.send(child_name, "task", task or f"continue ({self._task_seq})")

    def _escalate(self, event: Event, decision: dict):
        """Beyond this lead's capability -> route `escalate` UP to my supervisor/controller (the next tier).
        If I am the top, the escalation is recorded for the controller/human to resolve."""
        reason = decision.get("reason") or decision.get("_blocker") or "beyond supervisor capability"
        rec = {"from": event.sender, "kind": event.kind, "reason": reason, "payload": event.payload}
        self.escalations.append(rec)
        self._audit("Escalate", "escalated", rec)
        if self.supervisor is not None:
            self.emit("escalate", rec)          # bubbles up the tree, only as far as needed
        # a child that escalated is considered terminal for aggregation purposes
        if event.sender in self.children:
            self.children[event.sender].done = True

    # --- (4) aggregate --------------------------------------------------
    def _outstanding(self) -> list[str]:
        return [n for n, c in self.children.items() if not c.done]

    def _maybe_aggregate(self):
        if self.children and not self._outstanding() and self.aggregated is None:
            self.aggregate()

    def aggregate(self) -> dict:
        """AI call: synthesize the children's results into one result, then emit `done` UP to my own
        supervisor (or finalize if I am the top). Runs only when every child is terminal — the ONLY
        join point, and it is reached by events, not a blocking barrier."""
        summary = _ai_json(self.role, self.repo,
                           "You are the LEAD. Synthesize your children's results into ONE coherent result. "
                           'Reply ONLY JSON: {"result":"...", "ok":true}. '
                           f"RESULTS: {json.dumps(self.results)[:2000]} "
                           f"ESCALATIONS(unresolved): {json.dumps(self.escalations)[:800]}",
                           spawner=self.role)
        self.aggregated = {"result": summary.get("result", ""), "ok": summary.get("ok", True),
                           "children": len(self.children), "escalations": len(self.escalations)}
        self.done = True
        self._audit("Aggregate", "executed", {"children": len(self.children), "escalations": len(self.escalations)})
        self.emit("done", {"task": self.task, "out": self.aggregated})
        return self.aggregated

    # --- driver ---------------------------------------------------------
    def run(self, task: str) -> Optional[dict]:
        """Top-level entry (also called recursively for a sub-Supervisor child): decompose -> spawn ->
        start every child -> let the reactor drain events (interrupt-driven, barrier-free). Returns the
        aggregated result when this Supervisor is the ROOT (drives its own pump); a sub-Supervisor returns
        None here and its `done` flows up to its parent via the shared bus/pump."""
        specs = self.decompose(task)
        self.spawn_children(specs)
        for s in specs:
            self._start_child(self.children[s["name"]], s)
        if self.supervisor is None:              # root drives the reactor
            self.bus.pump()
            return self.aggregated
        return None


# --------------------------------------------------------------------------- offline selftest
def _selftest():
    """OFFLINE, deterministic: stub factory.agent so there is no spend and no web. Two scenarios exercise
    the interrupt-driven core:
      A) RESOLVE  — decompose into 2 children; child .c0 emits `blocked`; the lead handles it the INSTANT
         it arrives (child .c1 still outstanding), decides to BROADCAST a correction, which reaches BOTH
         children; .c0 retries and succeeds; the lead aggregates only after both are done.
      B) ESCALATE — same block, but the stubbed decision is beyond the lead's capability, so it emits
         `escalate` to its controller WITHOUT waiting for the sibling."""
    import types

    real_agent = factory.agent
    results = {"A": False, "B": False}

    def make_stub(mode: str) -> Callable:
        state = {"c0_calls": 0}

        def stub(role, repo, task, **kw):
            # ---- decompose: two children (one 'supervisor' to prove recursion is representable) ----
            if task.startswith("You are a LEAD decomposing"):
                return {"rc": 0, "out_full": json.dumps({
                    "org_note": "two independent subtasks",
                    "children": [{"role": "dev", "kind": "worker", "task": "build module A"},
                                 {"role": "dev", "kind": "worker", "task": "build module B"}]})}
            # ---- the lead's per-event decision ----
            if task.startswith("You are an interrupt-driven LEAD"):
                if "kind=blocked" in task:
                    if mode == "A":
                        return {"rc": 0, "out_full": json.dumps(
                            {"action": "broadcast", "message": "use the staging API key STAGE-123"})}
                    return {"rc": 0, "out_full": json.dumps(
                        {"action": "escalate", "reason": "no prod creds — needs controller to enable"})}
                return {"rc": 0, "out_full": json.dumps({"action": "ack"})}
            # ---- aggregate ----
            if task.startswith("You are the LEAD. Synthesize"):
                return {"rc": 0, "out_full": json.dumps({"result": "A+B integrated", "ok": True})}
            # ---- worker doing its task ----  (.c0 is blocked on first attempt, ok after a broadcast)
            if "build module A" in task:
                state["c0_calls"] += 1
                if state["c0_calls"] == 1:
                    return {"rc": -1, "failed": True, "out": "blocked", "blocker": "no API creds for X"}
                return {"rc": 0, "out_full": "module A built (used broadcast creds)"}
            return {"rc": 0, "out_full": f"done: {task[:40]}"}

        return stub

    # ---------------- Scenario A: resolve locally via broadcast ----------------
    factory.agent = make_stub("A")
    try:
        bus = Bus()
        sup = Supervisor("lead", "principal", "/tmp/repo", bus)
        out = sup.run("build a two-module feature")
        c0 = sup.children["lead.c0"]
        # The block was handled the instant it arrived — while .c1 was still outstanding (no barrier):
        block_handled = next((h for h in sup.handled if h["kind"] == "blocked"), None)
        interrupt_ok = bool(block_handled) and block_handled["action"] == "broadcast" \
            and "lead.c1" in block_handled["outstanding"]           # sibling had NOT finished yet
        # The correction reached the OTHER child too (context-sharing), .c0 retried and succeeded:
        sibling_got_broadcast = any(e.kind == "context_update" for e in sup.children["lead.c1"].inbox)
        c0_retried_ok = c0.attempts >= 2 and c0.done
        aggregated_after_all = out is not None and out["ok"] and not sup._outstanding()
        results["A"] = interrupt_ok and sibling_got_broadcast and c0_retried_ok and aggregated_after_all
        print(f"[A resolve] handled={block_handled} sibling_broadcast={sibling_got_broadcast} "
              f"c0_attempts={c0.attempts} done={c0.done} aggregated={out}")
    finally:
        factory.agent = real_agent

    # ---------------- Scenario B: escalate upward ----------------
    factory.agent = make_stub("B")
    try:
        bus = Bus()
        controller = Supervisor("controller", "controller", "/tmp/repo", bus)   # top tier
        sup = Supervisor("lead", "principal", "/tmp/repo", bus, supervisor=controller)
        controller.children["lead"] = sup
        controller.child_specs["lead"] = {"name": "lead", "role": "principal", "kind": "supervisor",
                                          "task": "delegate"}
        sup.run("build a two-module feature")
        bus.pump()
        block_handled = next((h for h in sup.handled if h["kind"] == "blocked"), None)
        escalated_locally = len(sup.escalations) == 1 and block_handled and block_handled["action"] == "escalate"
        # handled without waiting for the sibling, and it bubbled to the controller:
        no_barrier = block_handled and "lead.c1" in block_handled["outstanding"]
        controller_got = any(e["kind"] == "escalate" for e in controller.escalations) \
            or any(t[1] == "escalate" for t in bus.trace if t[0] == "controller")
        results["B"] = escalated_locally and no_barrier and controller_got
        print(f"[B escalate] handled={block_handled} sup_escalations={sup.escalations} "
              f"controller_saw_escalate={controller_got}")
    finally:
        factory.agent = real_agent

    ok = all(results.values())
    print(f"scenarios: {results}")
    print("PASS: interrupt-driven Supervisor (resolve-or-escalate, no barrier) OK" if ok else "FAIL")
    sys.exit(0 if ok else 1)


def _main(argv):
    if not argv or argv[0] == "selftest":
        _selftest()
    else:
        sys.exit("usage: supervisor.py selftest")


if __name__ == "__main__":
    _main(sys.argv[1:])
