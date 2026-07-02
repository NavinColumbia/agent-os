#!/usr/bin/env python3
"""actor.py — the autonomous Actor (an IC) of the event-driven agent-org.

Spec: docs/blueprint/AGENTIC-ORCHESTRATION.md § "Execution model: event-driven actors +
hierarchical escalation (NOT barriers)".

An Actor is NOT a step in a barrier fan-out. It is an autonomous unit running its own
DECIDE-LOOP that interleaves:
  (a) HANDLING its inbox — messages routed to it by the bus (a supervisor's resolution, a
      peer's clarification, a propagated context correction), and
  (b) doing a WORK-STEP — where every step is an AI decision (factory.agent) that returns one of:
        * continue  — keep working
        * emit(kind,payload) — raise an event to the supervisor MID-WORK. Kinds:
              next        ("what's next / I'm done, here's the result")
              blocked     (hit a wall it cannot pass alone, e.g. "no API creds for X")
              finding     (surfaced a result/correction worth propagating)
              question    (needs a human/peer answer)
              need_agent  (wants a helper spawned)
              need_context(needs info another actor holds)
        * finish    — terminal; reports the result up as a 'next' event

The pivotal property: an actor can raise a 'blocked' event *mid-work* and PARK, keeping a
resume handle (its own persisted context + step cursor), so when its supervisor sends a
'resolve' message the loop simply continues from where it stopped — no restart, no barrier.

  Actor(id, role, task, bus, supervisor_id=None)

THE BUS CONTRACT (duck-typed; a dedicated bus module can supersede LocalBus below):
    bus.send(msg: dict)        -> routes msg to msg["recipient"]'s inbox (non-blocking)
    bus.drain(actor_id) -> list -> pops & returns that actor's pending messages (non-blocking)
Message envelope is a plain JSON-able dict (see _msg): id, kind, sender, recipient, payload,
corr (request/reply correlation), ts, event(bool).

run() is a COOPERATIVE loop that returns control to the scheduler when the actor becomes idle
(done / parked-on-block / nothing-to-do), so a fast child never spins and a blocked child
yields instead of stalling siblings. The scheduler re-invokes run() when a new message lands.

Every decision is an AI call via factory.agent (cost is not a concern). The __main__ selftest
runs OFFLINE by stubbing factory.agent, so it is cheap + deterministic.

Run with the agent-os venv python.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parent.parent   # scripts/
sys.path.insert(0, str(SCRIPTS))

import factory   # noqa: E402  (the LLM call: factory.agent(role, repo, prompt, ...))
try:
    import audit   # noqa: E402  best-effort audit/persistence (reused where sensible)
except Exception:  # pragma: no cover - audit is optional for the offline core
    audit = None


# Event kinds an actor may EMIT up to its supervisor (mid-work, not only at completion).
EVENT_KINDS = ("next", "done", "blocked", "finding", "question", "need_agent", "need_context")
# Message kinds an actor may RECEIVE in its inbox.
MSG_KINDS = ("resolve", "resume", "context_update", "answer", "context", "task", "cancel",
             "question", "need_context", "clarify")


def _new_id(prefix="msg"):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _msg(kind, sender, recipient, payload=None, corr=None, event=False):
    return {"id": _new_id(), "kind": kind, "sender": sender, "recipient": recipient,
            "payload": payload or {}, "corr": corr, "event": bool(event), "ts": time.time()}


def _parse_directive(text):
    """Pull the first {...} JSON directive out of an agent's free-text reply. Tolerant of code
    fences and prose. On any failure, default to 'continue' (never crash the decide-loop)."""
    if not text:
        return {"action": "continue"}
    t = text.strip()
    if "```" in t:
        seg = t.split("```")[1]
        t = seg[4:] if seg.lower().startswith("json") else seg
    s = t.find("{")
    if s < 0:
        return {"action": "continue", "note": text[:200]}
    try:
        d = json.loads(t[s: t.rfind("}") + 1])
        if not isinstance(d, dict):
            return {"action": "continue"}
        return d
    except Exception:
        return {"action": "continue", "note": text[:200]}


class LocalBus:
    """Minimal in-process reference bus (per-actor inbox + non-blocking route). A dedicated,
    durable bus (on top of orchestrator.conversations) can drop in by honoring send/drain."""

    def __init__(self):
        self._inboxes: dict[str, list] = {}

    def send(self, msg: dict):
        self._inboxes.setdefault(msg["recipient"], []).append(msg)

    def drain(self, actor_id: str) -> list:
        q = self._inboxes.get(actor_id, [])
        self._inboxes[actor_id] = []
        return q

    def peek(self, actor_id: str) -> list:
        return list(self._inboxes.get(actor_id, []))


class Actor:
    """An autonomous IC. Owns a decide-loop; escalates via events; resumes on resolution."""

    def __init__(self, id, role, task, bus, supervisor_id=None, repo=".",
                 context=None, max_steps=64):
        self.id = id
        self.role = role
        self.task = task
        self.bus = bus
        self.supervisor_id = supervisor_id
        self.repo = str(repo)
        self.context = dict(context or {})
        self.max_steps = int(max_steps)

        self.steps = 0
        self.done = False
        self.result = None
        self.progress: list[str] = []       # short notes -> fed back into the next decision
        self.transcript: list[dict] = []     # audit trail of every emit/handle
        self.emitted: list[dict] = []        # events this actor sent up (for inspection)

        self._blocked = False
        self._block = None                    # the RESUME HANDLE: payload of the parked blocker
        self._pending: dict[str, str] = {}    # corr -> question we asked a peer (request_context)

    # ---- outbound ---------------------------------------------------------------------------
    def send(self, to, kind, payload=None, corr=None, event=False):
        m = _msg(kind, self.id, to, payload, corr, event)
        self.bus.send(m)
        self._journal("send", {"to": to, "kind": kind, "corr": corr, "event": event})
        return m

    def emit(self, kind, payload=None, to=None):
        """Raise an EVENT to the supervisor (or an explicit target) — MID-WORK is allowed. This
        is how a blocker/finding/question routes UP the tree the instant it happens."""
        if kind not in EVENT_KINDS:
            # not fatal — the org vocabulary can grow; just note the unusual kind
            self._journal("emit-unknown-kind", {"kind": kind})
        target = to or self.supervisor_id
        if target is None:
            # a top-level actor with no supervisor has nowhere to escalate — journal only.
            self._journal("emit-noroute", {"kind": kind, "payload": payload})
            self.emitted.append({"kind": kind, "payload": payload or {}, "to": None})
            return None
        m = self.send(target, kind, payload, event=True)
        self.emitted.append({"kind": kind, "payload": payload or {}, "to": target})
        return m

    def request_context(self, peer_id, q):
        """Open a context request to a PEER (inter-agent clarification / context-sharing). The
        actor does NOT block on it — it keeps working; the peer's 'answer'/'context' reply is
        applied later by handle_message. Returns the correlation id."""
        corr = _new_id("ctx")
        self._pending[corr] = q
        self.send(peer_id, "need_context", {"q": q}, corr=corr)
        return corr

    # ---- inbound ----------------------------------------------------------------------------
    def handle_message(self, msg: dict):
        """Apply one inbox message. The two headline cases the spec calls out: apply a
        context_update, and answer a clarification. 'resolve' is what unblocks a parked actor."""
        kind = msg.get("kind")
        payload = msg.get("payload") or {}
        self._journal("handle", {"kind": kind, "from": msg.get("sender"), "corr": msg.get("corr")})

        if kind in ("resolve", "resume"):
            self._apply_resolution(payload)
        elif kind == "context_update":
            # a correction the supervisor propagated so nobody works off stale context
            self.context.update(payload.get("context", payload))
        elif kind in ("answer", "context"):
            # reply to one of our request_context() asks -> fulfill + merge into context
            corr = msg.get("corr")
            if corr in self._pending:
                self._pending.pop(corr, None)
            self.context.update(payload.get("context", payload))
        elif kind in ("question", "need_context", "clarify"):
            # a PEER is asking US — answer it with an AI decision, reply on the same corr
            self._answer_peer(msg)
        elif kind == "task":
            # (re)assignment / additional work
            if payload.get("task"):
                self.task = payload["task"]
            self.done = False
        elif kind == "cancel":
            self.done = True
            self._journal("cancelled", {"by": msg.get("sender")})
        else:
            self._journal("handle-unknown", {"kind": kind})

    def _apply_resolution(self, payload):
        """Supervisor resolved our blocker — merge whatever it handed back (creds, a re-brief, a
        spawned helper's id) into context, mark resolved, and CLEAR the block so the decide-loop
        resumes from the parked step (the resume handle is our own persisted state)."""
        self.context.update(payload.get("context", {}))
        if payload.get("note"):
            self.progress.append(f"resolved: {payload['note']}")
        self.context["_resolved"] = True
        self.context["_resolution"] = payload
        self._blocked = False
        self._block = None
        self._journal("resumed", {"payload": payload})

    def _answer_peer(self, msg):
        """AI-decide an answer to a peer's clarification and send it back on the same corr."""
        q = (msg.get("payload") or {}).get("q", "")
        prompt = (f"You are {self.role} (actor {self.id}). A peer asks: {q}\n"
                  f"Your context: {json.dumps(self.context)[:1500]}\n"
                  f"Answer concisely. Reply as JSON: {{\"answer\": \"...\"}}")
        d = self._ai(prompt)
        ans = d.get("answer") or d.get("note") or ""
        self.send(msg.get("sender"), "answer", {"context": {"answer": ans}, "answer": ans},
                  corr=msg.get("corr"))

    # ---- the AI decision --------------------------------------------------------------------
    def _ai(self, prompt):
        """One AI call (the ONLY way a decision is ever made). Returns a parsed directive dict."""
        try:
            r = factory.agent(self.role, self.repo, prompt)
        except Exception as e:
            # a failed model call is itself a decision point -> surface as a blocker, don't crash
            return {"action": "emit", "kind": "blocked",
                    "payload": {"reason": f"AI call failed: {e}"}, "note": "model error"}
        out = r.get("out") if isinstance(r, dict) else str(r)
        return _parse_directive(out)

    def _decide_prompt(self):
        recent = "; ".join(self.progress[-6:]) or "(nothing yet)"
        return (
            f"You are actor {self.id}, role={self.role}, an autonomous IC in an agent org.\n"
            f"TASK: {self.task}\n"
            f"CONTEXT: {json.dumps(self.context)[:1800]}\n"
            f"PROGRESS SO FAR: {recent}\n"
            f"STEP {self.steps + 1}/{self.max_steps}.\n\n"
            "Decide your NEXT action and reply with ONE JSON object:\n"
            '  {"action":"continue","note":"what you did this step"}\n'
            '  {"action":"emit","kind":"blocked|finding|question|need_agent|need_context",'
            '"payload":{...},"note":"..."}\n'
            '  {"action":"finish","result":"the final result","note":"..."}\n'
            "Emit 'blocked' the MOMENT you hit a wall you cannot pass alone (e.g. missing API "
            "creds); your supervisor will resolve it and you'll resume. Emit 'finding' to "
            "propagate a correction. Reply with JSON only."
        )

    def step(self):
        """Do ONE work-step: make an AI decision and act on it. Never called while blocked."""
        d = self._ai(self._decide_prompt())
        action = (d.get("action") or "continue").lower()
        note = d.get("note")

        if action == "finish":
            self.result = d.get("result") or note or "done"
            self.emit("next", {"result": self.result, "note": note})
            self.done = True
        elif action == "emit":
            kind = d.get("kind") or "finding"
            payload = dict(d.get("payload") or {})
            if note and "note" not in payload:
                payload["note"] = note
            self.emit(kind, payload)
            if kind == "blocked":
                self._enter_blocked(payload)      # PARK, keep the resume handle
            elif kind == "need_context" and payload.get("peer"):
                self.request_context(payload["peer"], payload.get("q", note or ""))
        else:  # continue
            self.progress.append(note or f"step {self.steps + 1}")

        self.steps += 1

    def _enter_blocked(self, payload):
        self._blocked = True
        self._block = payload          # <-- the handle the supervisor's resolution resumes
        self._journal("blocked", {"payload": payload})

    # ---- the decide-loop --------------------------------------------------------------------
    def run(self):
        """Interleave inbox-handling with work-steps until the actor becomes idle. Returns a
        status the scheduler uses to decide whether to re-invoke on the next message:
          'done'      — finished
          'parked'    — blocked, awaiting a 'resolve' (yielded, not spinning)
          'idle'      — nothing to do right now
          'exhausted' — hit max_steps
        Re-invoking run() after delivering messages RESUMES exactly where it parked."""
        while self.steps < self.max_steps and not self.done:
            handled = self._pump_inbox()
            if self.done:
                break
            if self._blocked:
                if handled:
                    continue           # a message may have unblocked us — re-check
                return "parked"        # blocked & inbox empty -> yield to the scheduler
            self.step()
        if self.done:
            return "done"
        if self.steps >= self.max_steps:
            return "exhausted"
        return "idle"

    def _pump_inbox(self):
        msgs = self.bus.drain(self.id)
        for m in msgs:
            self.handle_message(m)
            if self.done:
                break
        return len(msgs)

    # ---- introspection / audit --------------------------------------------------------------
    def _journal(self, action, detail):
        rec = {"ts": time.time(), "actor": self.id, "role": self.role,
               "action": action, "detail": detail}
        self.transcript.append(rec)
        if audit is not None:
            try:   # best-effort; the offline core must not depend on a live DB
                audit.append(actor=f"actor:{self.id}", action=action,
                             resource=self.role, decision="orchestra", payload=detail)
            except Exception:
                pass

    def state(self):
        """The resume handle / inspectable snapshot."""
        return {"id": self.id, "role": self.role, "steps": self.steps, "done": self.done,
                "blocked": self._blocked, "block": self._block, "result": self.result,
                "pending_context": dict(self._pending), "context": dict(self.context)}


# =============================================================================================
# OFFLINE SELFTEST — stubs factory.agent so it is cheap + deterministic (no model, no DB).
# =============================================================================================
def _selftest():
    print("actor.selftest: start")
    real_agent = factory.agent
    calls = {"n": 0}

    def stub_agent(role, repo, task, *a, **k):
        """Deterministic org-in-a-can: the actor works one step and emits 'blocked' (no API
        creds); once its supervisor's resolution is merged into context (_resolved), the next
        work-step finishes."""
        calls["n"] += 1
        if "_resolved" in task:                      # resolution has landed -> wrap up
            return {"rc": 0, "out":
                    '{"action":"finish","result":"charges endpoint built","note":"creds present"}'}
        # first work-step: hit a wall and escalate mid-work
        return {"rc": 0, "out":
                '{"action":"emit","kind":"blocked",'
                '"payload":{"reason":"no API creds for PaymentsCo"},"note":"hit a wall"}'}

    factory.agent = stub_agent
    try:
        bus = LocalBus()
        a = Actor("dev-1", "backend-dev", "build the charges endpoint", bus,
                  supervisor_id="head-of-pay")

        # 1) run -> works a step, emits 'blocked' mid-work, PARKS.
        status = a.run()
        assert status == "parked", f"expected parked, got {status}"
        assert a._blocked and a._block and a._block["reason"].startswith("no API creds"), \
            "actor did not retain the resume handle for its blocker"

        # 2) the blocked EVENT landed on the bus addressed to its SUPERVISOR.
        sup_inbox = bus.peek("head-of-pay")
        blocked = [m for m in sup_inbox if m["kind"] == "blocked"]
        assert len(blocked) == 1, f"supervisor did not receive the blocked event: {sup_inbox}"
        ev = blocked[0]
        assert ev["recipient"] == "head-of-pay" and ev["sender"] == "dev-1" and ev["event"], \
            "blocked event mis-addressed"
        assert ev["payload"]["reason"].startswith("no API creds"), "blocked payload lost"
        print(f"  ok: emitted 'blocked' -> supervisor (reason: {ev['payload']['reason']})")

        # nothing further should have been emitted while parked, and it stayed at 1 step.
        assert a.steps == 1, f"expected exactly one work-step before parking, got {a.steps}"
        assert not a.done

        # 3) supervisor RESOLVES -> deliver a 'resolve' message to the actor's inbox.
        bus.send(_msg("resolve", "head-of-pay", "dev-1",
                      payload={"context": {"api_creds": "sk-live-…"}, "note": "creds provisioned"}))

        # 4) re-invoke run() -> pumps the resolve (unblocks), RESUMES the parked work, finishes.
        status = a.run()
        assert status == "done", f"expected done after resolve, got {status}"
        assert a.done and a.result == "charges endpoint built", f"bad result: {a.result}"
        assert not a._blocked, "still blocked after resolution"
        assert a.context.get("api_creds") == "sk-live-…", "resolution context not merged"
        print(f"  ok: 'resolve' resumed the parked actor -> finished ({a.result})")

        # 5) a 'next' (done) event went up to the supervisor with the result.
        nexts = [m for m in bus.peek("head-of-pay") if m["kind"] == "next"]
        assert len(nexts) == 1 and nexts[0]["payload"]["result"] == "charges endpoint built", \
            "completion 'next' event not delivered to supervisor"
        print("  ok: 'next' completion event -> supervisor")

        # 6) request_context: async peer ask, then an 'answer' fulfills it (no blocking).
        b = Actor("dev-2", "backend-dev", "build refunds", bus, supervisor_id="head-of-pay")
        corr = b.request_context("dev-1", "what auth header does PaymentsCo expect?")
        assert corr in b._pending, "pending context request not tracked"
        asked = [m for m in bus.peek("dev-1") if m["kind"] == "need_context"]
        assert asked and asked[0]["corr"] == corr, "context request not routed to peer"
        bus.send(_msg("answer", "dev-1", "dev-2",
                      payload={"context": {"auth": "Bearer"}}, corr=corr))
        b._pump_inbox()
        assert corr not in b._pending and b.context.get("auth") == "Bearer", \
            "peer answer did not fulfill the request / merge context"
        print("  ok: request_context resolved asynchronously via peer 'answer'")

        # 7) context_update (a propagated correction) merges without a work-step.
        bus.send(_msg("context_update", "head-of-pay", "dev-2",
                      payload={"context": {"currency": "USD-only"}}))
        b._pump_inbox()
        assert b.context.get("currency") == "USD-only", "propagated correction not applied"
        print("  ok: context_update correction propagated")

        print(f"actor.selftest: PASS ({calls['n']} stubbed AI calls)")
        return 0
    finally:
        factory.agent = real_agent


if __name__ == "__main__":
    raise SystemExit(_selftest())
