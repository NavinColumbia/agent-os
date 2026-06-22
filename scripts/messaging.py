#!/usr/bin/env python3
"""messaging.py — typed message envelope + intent vocabulary (ADR 0005).

A small, FIPA-derived performative set carried as a typed envelope. Intent drives deterministic
dispatch; the mentalistic FIPA semantics are deliberately dropped. Payload is JSON (large blobs
go to git/Postgres by reference, never inline).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum


class Intent(str, Enum):
    DELEGATE = "delegate"   # request work
    ACCEPT = "accept"       # I will do it (commitment)
    REJECT = "reject"       # I will not
    RESULT = "result"       # done, here is the artifact (by ref)
    FAILURE = "failure"     # I tried and failed (distinct from silence)
    QUERY = "query"         # ask a question
    INFORM = "inform"       # share info / status
    CLARIFY = "clarify"     # request clarification (expects reply by deadline)
    PROPOSE = "propose"     # negotiation / contract-net bid or offer
    SUBSCRIBE = "subscribe" # watch a condition
    CANCEL = "cancel"       # preempt / withdraw
    ESCALATE = "escalate"   # raise to controller/human


def new_id(prefix="msg"):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class Message:
    intent: Intent
    sender: str
    recipient: str
    content: dict = field(default_factory=dict)
    conversation_id: str = field(default_factory=lambda: new_id("conv"))
    message_id: str = field(default_factory=lambda: new_id("msg"))
    in_reply_to: str | None = None
    reply_to: str | None = None      # NATS _INBOX or DBOS workflow_id
    reply_by: str | None = None      # ISO deadline — REQUIRED for acts expecting a reply
    priority: int = 5
    attachments: list = field(default_factory=list)  # FileParts by reference: [{kind:file, blob_id, mime, size}]

    def __post_init__(self):
        self.intent = Intent(self.intent)
        # acts that expect a reply MUST carry a deadline (deadlock prevention)
        if self.intent in (Intent.QUERY, Intent.CLARIFY, Intent.DELEGATE, Intent.PROPOSE) and not self.reply_by:
            raise ValueError(f"intent '{self.intent.value}' requires reply_by (no unbounded waits)")

    def to_json(self) -> str:
        d = asdict(self); d["intent"] = self.intent.value
        return json.dumps(d, separators=(",", ":"))

    @staticmethod
    def from_json(s: str) -> "Message":
        d = json.loads(s)
        return Message(**d)


if __name__ == "__main__":
    m = Message(intent="query", sender="builder", recipient="pm",
                content={"q": "is SendGrid v2 still supported?"}, reply_to="wf-123",
                reply_by="2026-06-22T12:00:00Z")
    print("envelope:", m.to_json())
    print("roundtrip ok:", Message.from_json(m.to_json()).intent == Intent.QUERY)
    try:
        Message(intent="query", sender="a", recipient="b")  # missing reply_by
    except ValueError as e:
        print("deadlock-guard works:", e)
