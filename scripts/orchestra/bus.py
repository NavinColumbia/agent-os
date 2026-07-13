#!/usr/bin/env python3
"""bus.py — the NERVOUS SYSTEM of the event-driven agent-org (docs/blueprint/AGENTIC-ORCHESTRATION.md).

The org is an ASYNC actor system, NOT a wait-for-all barrier. Every agent is an autonomous actor with its
own inbox and decide-loop; any actor can emit an event at ANY time (not just at completion), and the instant
that event lands, its supervisor can act on it — even while other children are still running. That requires a
real message bus with per-actor inboxes and IMMEDIATE, in-process delivery. This module is that substrate.

Design (§ "Execution model: event-driven actors + hierarchical escalation"):
  - MessageBus.register(actor_id) -> Inbox            # an actor's own mailbox
  - send(Message)                                     # IMMEDIATE cross-thread delivery to the recipient inbox
  - poll(actor_id) / await_next(actor_id)             # actor drains its inbox (non-blocking / blocking)
  - subscribe(actor_id, handler)                      # supervisor reacts to a child's event stream as it arrives
  - broadcast(frm, kind, payload, recipients)         # propagate a context_update/correction to a set of siblings
Every message is persisted to Postgres (orchestra_messages) for a fully auditable run — the owner can inspect
who sent what to whom, when, in what conversation (corr_id), mirroring orchestrator.conversations.

This module is pure infrastructure: it MOVES the events. The DECISIONS an actor makes when an event arrives
(resolve locally / spawn a helper / broadcast a correction / ESCALATE) are AI calls via factory.agent, made
in the actor's handler — the selftest below stubs factory.agent to show that decide-on-event loop OFFLINE.

Run the selftest with the agent-os venv python:
    scripts/orchestra/bus.py            # OFFLINE, deterministic (no LLM, local Postgres only)
"""
import json
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

import psycopg

# factory.py (the LLM call) + siblings live in the parent scripts/ dir; put it on the path so actors'
# handlers can `import factory` and make their decide-on-event AI calls.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- DB wiring (same convention as orchestrator.py / commfabric.py) ----------------------------------------
ENV = Path.home() / "projects" / "agent-os" / ".env.local"
try:
    DB = next((l.split("=", 1)[1].strip() for l in ENV.read_text().splitlines()
               if l.strip().startswith("DATABASE_URL=")), None)
except Exception:
    DB = None

# The full event vocabulary an actor can emit (§ execution model). Each emission is the actor making an AI
# decision about its OWN state; the bus just routes it. 'broadcast' is the fan-out kind used for corrections.
KINDS = {
    "task",            # supervisor -> child: here is your work
    "done",            # child -> supervisor: finished this unit
    "next",            # child -> supervisor: "what do I do next?" (no idle waiting on slow siblings)
    "blocked",         # child -> supervisor: stuck (e.g. no API creds) — route up immediately
    "finding",         # any -> up: a discovered fact worth sharing
    "question",        # any -> peer/up: a clarification request
    "need_agent",      # child/supervisor -> up: I need a new agent spawned for X
    "need_context",    # any -> up/peer: I'm missing context to proceed
    "escalate",        # supervisor -> controller: beyond my capability, kick it upstairs
    "resolve",         # supervisor/controller -> down: here's the unblock / next task / correction
    "context_update",  # any -> siblings: shared-context change to propagate
    "broadcast",       # generic fan-out marker (context_update to a whole sibling set)
    "disagree",        # child -> up: a professional OBJECTION to the directive (not a capability blocker) —
                       # the agent thinks the assignment is wrong/unwise; routed UP to the CEO to rule on
                       # (proceed / revise). The agent parks until the ruling. Human-pattern disagreement.
    "tool_result",     # jobrunner -> the tool-worker ITSELF: a dispatched tool finished (the worker's next
                       # step reports its findings+done up and finishes). Event-based so ONLY the pool ever
                       # writes an actor row — the job thread never touches actor state (avoids a lock race).
}


@dataclass
class Message:
    """One event on the bus. Shape (per spec): {id, frm, to, kind, payload, ts, corr_id}.

    corr_id ties every message in one logical exchange together (a conversation / escalation chain); if the
    caller doesn't supply one, the message opens a new conversation and its corr_id defaults to its own id.
    in_reply_to is an optional extra that threads a direct reply to a specific prior message (audit clarity).
    """
    frm: str
    to: str
    kind: str
    payload: dict = field(default_factory=dict)
    corr_id: str = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    in_reply_to: str = None

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown message kind {self.kind!r}; must be one of {sorted(KINDS)}")
        if self.corr_id is None:
            self.corr_id = self.id            # first message of a new conversation

    def as_dict(self):
        return asdict(self)


class Inbox:
    """An actor's mailbox. Thin, thread-safe wrapper over a queue so an actor can `inbox.poll()` /
    `inbox.await_next()` directly, while the bus can still deliver into it from any other actor's thread."""
    def __init__(self, bus, actor_id):
        self._bus = bus
        self.actor_id = actor_id
        self._q = queue.Queue()

    def _deliver(self, msg):
        self._q.put(msg)

    def poll(self):
        """Non-blocking: return the next Message or None if the inbox is empty."""
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    def await_next(self, timeout=None):
        """Block until a Message arrives (or `timeout` seconds elapse -> None). Wakes the instant a message
        is delivered from another actor's thread — this is what makes delivery feel interrupt-driven."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def pending(self):
        return self._q.qsize()


_STOP = object()   # sentinel pushed to a subscribed inbox to unwind its dispatcher thread on close()


class MessageBus:
    """Async, in-process message bus with per-actor inboxes + durable audit.

    Delivery is IMMEDIATE: send() enqueues straight into the recipient's inbox (a thread-safe queue), so a
    message to a supervisor is deliverable even while other actors are mid-run in other threads. Persistence
    to Postgres is best-effort and never blocks delivery; an in-memory journal always mirrors every message
    so audit works even if the DB is momentarily unreachable."""

    def __init__(self, persist=True, table="orchestra_messages"):
        self._inboxes = {}          # actor_id -> Inbox
        self._handlers = {}         # actor_id -> handler(msg) (subscribed actors)
        self._dispatchers = {}      # actor_id -> daemon Thread draining the inbox into the handler
        self._lock = threading.RLock()
        self.journal = []           # in-memory audit mirror (always written; offline-safe)
        self.table = table
        self.persist = bool(persist and DB)
        if self.persist:
            self._ensure()

    # --- persistence (audit) -------------------------------------------------------------------------------
    def _ensure(self):
        try:
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute(f"""CREATE TABLE IF NOT EXISTS {self.table} (
                    id          TEXT PRIMARY KEY,
                    corr_id     TEXT,
                    frm         TEXT NOT NULL,
                    to_actor    TEXT NOT NULL,
                    kind        TEXT NOT NULL,
                    payload     JSONB NOT NULL DEFAULT '{{}}',
                    in_reply_to TEXT,
                    ts          TIMESTAMPTZ NOT NULL DEFAULT now())""")
                cur.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_corr_idx ON {self.table} (corr_id)")
                c.commit()
        except Exception:
            self.persist = False    # DB unavailable -> degrade to journal-only, never crash the bus

    def _persist(self, msg):
        """Best-effort durable write. Idempotent on id (a redelivered message won't duplicate a row)."""
        if not self.persist:
            return
        try:
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute(f"""INSERT INTO {self.table}
                                (id, corr_id, frm, to_actor, kind, payload, in_reply_to, ts)
                                VALUES (%s,%s,%s,%s,%s,%s,%s, to_timestamp(%s))
                                ON CONFLICT (id) DO NOTHING""",
                            (msg.id, msg.corr_id, msg.frm, msg.to, msg.kind,
                             json.dumps(msg.payload), msg.in_reply_to, msg.ts))
                c.commit()
        except Exception:
            pass                    # audit is best-effort; the journal already has it

    def messages(self, corr_id=None):
        """Audit read-back from Postgres (used by tooling/tests). Falls back to the in-memory journal if the
        DB isn't reachable, so callers get a consistent view either way."""
        if self.persist:
            try:
                with psycopg.connect(DB) as c, c.cursor() as cur:
                    if corr_id:
                        cur.execute(f"""SELECT id, corr_id, frm, to_actor, kind, payload, ts FROM {self.table}
                                        WHERE corr_id=%s ORDER BY ts, id""", (corr_id,))
                    else:
                        cur.execute(f"""SELECT id, corr_id, frm, to_actor, kind, payload, ts FROM {self.table}
                                        ORDER BY ts, id""")
                    return [{"id": i, "corr_id": cc, "frm": f, "to": t, "kind": k, "payload": p, "ts": str(ts)}
                            for i, cc, f, t, k, p, ts in cur.fetchall()]
            except Exception:
                pass
        return [m.as_dict() for m in self.journal if corr_id is None or m.corr_id == corr_id]

    # --- registration + delivery ---------------------------------------------------------------------------
    def register(self, actor_id):
        """Give an actor a mailbox (idempotent). Returns its Inbox."""
        with self._lock:
            if actor_id not in self._inboxes:
                self._inboxes[actor_id] = Inbox(self, actor_id)
            return self._inboxes[actor_id]

    def inbox(self, actor_id):
        return self._inboxes.get(actor_id)

    def send(self, msg):
        """Route one Message to its recipient RIGHT NOW. Auto-registers an unknown recipient so an event
        addressed to a not-yet-spawned supervisor still lands (the org tree is elastic — actors come and go).
        Persists for audit, mirrors to the journal, then delivers into the recipient's inbox."""
        if not isinstance(msg, Message):
            raise TypeError("send() expects a Message")
        with self._lock:
            self.journal.append(msg)
            inbox = self._inboxes.get(msg.to) or self.register(msg.to)
        self._persist(msg)          # outside the lock: audit must never stall delivery
        inbox._deliver(msg)         # IMMEDIATE — wakes any await_next() / feeds the subscribed handler
        return msg

    def emit(self, frm, to, kind, payload=None, corr_id=None, in_reply_to=None):
        """Convenience: build + send a Message in one call. Returns the sent Message."""
        return self.send(Message(frm=frm, to=to, kind=kind, payload=payload or {},
                                 corr_id=corr_id, in_reply_to=in_reply_to))

    def broadcast(self, frm, kind, payload, recipients, corr_id=None):
        """Fan a single logical event out to a SET of actors — the mechanism for propagating a context_update
        / correction to all of a child's siblings so nobody works off stale context (§ inter-agent context
        sharing). Each recipient gets its own Message (own id) sharing one corr_id so the fan-out is one
        auditable conversation. Returns the list of sent Messages."""
        corr_id = corr_id or uuid.uuid4().hex
        sent = []
        for to in recipients:
            sent.append(self.send(Message(frm=frm, to=to, kind=kind, payload=dict(payload or {}),
                                          corr_id=corr_id)))
        return sent

    # --- reactive draining (supervisors) -------------------------------------------------------------------
    def poll(self, actor_id):
        """Non-blocking drain of an actor's inbox: next Message or None."""
        ib = self._inboxes.get(actor_id)
        return ib.poll() if ib else None

    def await_next(self, actor_id, timeout=None):
        """Blocking drain: wait (up to timeout) for the next Message to this actor."""
        ib = self._inboxes.get(actor_id) or self.register(actor_id)
        return ib.await_next(timeout=timeout)

    def subscribe(self, actor_id, handler):
        """Make an actor INTERRUPT-DRIVEN: spawn a daemon thread that blocks on its inbox and calls
        handler(msg) the instant each event arrives — this is how a supervisor reacts to a child's event
        stream while other children keep running. handler exceptions are swallowed (a bad decision on one
        event must not kill the supervisor's whole event loop). Returns the dispatcher thread."""
        self.register(actor_id)
        with self._lock:
            if actor_id in self._dispatchers:
                raise ValueError(f"{actor_id!r} already has a subscriber")
            self._handlers[actor_id] = handler

        def _loop():
            ib = self._inboxes[actor_id]
            while True:
                msg = ib.await_next()
                if msg is _STOP:
                    return
                try:
                    handler(msg)
                except Exception:
                    pass            # keep the event loop alive through a bad handler decision

        t = threading.Thread(target=_loop, name=f"dispatch:{actor_id}", daemon=True)
        with self._lock:
            self._dispatchers[actor_id] = t
        t.start()
        return t

    def close(self):
        """Unwind all subscriber dispatcher threads cleanly (push a stop sentinel to each and join)."""
        with self._lock:
            ids = list(self._dispatchers.keys())
        for actor_id in ids:
            ib = self._inboxes.get(actor_id)
            if ib:
                ib._deliver(_STOP)
        for actor_id in ids:
            t = self._dispatchers.get(actor_id)
            if t:
                t.join(timeout=2.0)
        with self._lock:
            self._dispatchers.clear()
            self._handlers.clear()


# ==============================================================================================================
# OFFLINE selftest — no LLM calls (factory.agent is stubbed), local Postgres only. Deterministic + cheap.
# ==============================================================================================================
def _selftest():
    run = uuid.uuid4().hex[:8]                       # tag every actor so DB rows for THIS run are isolable
    a, b = f"childA-{run}", f"childB-{run}"
    bus = MessageBus()

    # 1) point-to-point + await: a message a->b is deliverable immediately and await_next returns it.
    ib_a, ib_b = bus.register(a), bus.register(b)
    corr = f"corr-{run}"
    bus.emit(a, b, "task", {"do": "step-1"}, corr_id=corr)
    got = ib_b.await_next(timeout=2.0)
    p2p = got is not None and got.frm == a and got.kind == "task" and got.payload["do"] == "step-1"

    # 2) ordering: two sends drain FIFO.
    bus.emit(a, b, "finding", {"n": 1}, corr_id=corr)
    bus.emit(a, b, "finding", {"n": 2}, corr_id=corr)
    m1, m2 = bus.poll(b), bus.poll(b)
    ordering = m1 and m2 and m1.payload["n"] == 1 and m2.payload["n"] == 2 and bus.poll(b) is None

    # 3) broadcast: one context_update reaches a whole sibling set, sharing one corr_id.
    sibs = [f"sib{i}-{run}" for i in range(3)]
    for s in sibs:
        bus.register(s)
    sent = bus.broadcast("supervisor-" + run, "context_update",
                         {"correction": "API v2 is deprecated, use v3"}, sibs)
    recv = [bus.poll(s) for s in sibs]
    bcorr = sent[0].corr_id
    broadcast_ok = (all(m is not None for m in recv)
                    and all(m.kind == "context_update" for m in recv)
                    and all(m.corr_id == bcorr for m in recv)
                    and len({m.corr_id for m in recv}) == 1
                    and all(m.payload["correction"].startswith("API v2") for m in recv))

    # 4) subscribe + AI-driven decide-on-event: a child emits `blocked`; the supervisor is interrupt-driven,
    #    makes a DECISION (an AI call — stubbed here, OFFLINE) and sends a `resolve` straight back. Proves the
    #    bus is the substrate for the event->AI-decision->reaction loop the blueprint describes.
    import factory
    real_agent = factory.agent
    calls = {"n": 0}

    def fake_agent(role, repo, prompt, **k):
        calls["n"] += 1
        # the "supervisor" reasons about the blocker and decides to unblock locally (not escalate)
        return {"rc": 0, "out": "RESOLVE: enable web access and retry"}

    factory.agent = fake_agent
    sup, child = f"sup-{run}", f"child-{run}"
    bus.register(child)

    def supervisor_handler(msg):
        if msg.kind == "blocked":
            decision = factory.agent("supervisor", "-", f"A child is blocked: {msg.payload}. Resolve or escalate?")
            out = (decision.get("out") or "")
            if out.startswith("RESOLVE"):
                bus.emit(sup, msg.frm, "resolve", {"fix": out}, corr_id=msg.corr_id, in_reply_to=msg.id)
            else:
                bus.emit(sup, f"controller-{run}", "escalate", {"why": out}, corr_id=msg.corr_id)

    try:
        bus.subscribe(sup, supervisor_handler)
        bmsg = bus.emit(child, sup, "blocked", {"reason": "no web access for API lookup"}, corr_id=f"esc-{run}")
        reply = bus.await_next(child, timeout=2.0)          # child waits for the supervisor's decision
        react_ok = (reply is not None and reply.kind == "resolve"
                    and reply.in_reply_to == bmsg.id and calls["n"] == 1
                    and "web access" in reply.payload["fix"])
    finally:
        factory.agent = real_agent
        bus.close()

    # 5) persistence: every message we sent has an audit row (query DB by the run's corr_ids).
    corr_ids = {corr, bcorr, f"esc-{run}"}
    rows = [r for cc in corr_ids for r in bus.messages(cc)]
    # expected: 3 (task + 2 findings) + 3 (broadcast) + 2 (blocked + resolve) = 8
    persisted = len(rows) >= 8
    journal_ok = len(bus.journal) >= 8

    # cleanup: remove only THIS run's rows.
    if bus.persist:
        try:
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute(f"DELETE FROM {bus.table} WHERE corr_id = ANY(%s)", (list(corr_ids),))
                c.commit()
        except Exception:
            pass

    ok = p2p and ordering and broadcast_ok and react_ok and persisted and journal_ok
    print(f"p2p={p2p} ordering={ordering} broadcast={broadcast_ok} "
          f"react(AI-decide-on-event)={react_ok} persisted>=8={persisted}({len(rows)}) journal={journal_ok}")
    print("PASS: async message bus — immediate delivery, ordering, broadcast fan-out, interrupt-driven "
          "supervisor decision, durable audit ✅" if ok else "FAIL")
    sys.exit(0 if ok else 1)


# Public API surface.
__all__ = ["MessageBus", "Message", "Inbox", "KINDS", "DB"]


if __name__ == "__main__":
    _selftest()
