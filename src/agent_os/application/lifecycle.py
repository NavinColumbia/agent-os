"""Application seam for the authoritative product lifecycle.

This module turns a domain transition into deterministically identified
commands.  A Temporal workflow can schedule those commands as activities; the
in-memory history is intentionally small and exists to prove the contract
without making a framework or database another owner of lifecycle semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Iterable, Mapping, Any

from agent_os.domain.lifecycle import (
    Command,
    Event,
    LifecycleState,
    Transition,
    TransitionRejected,
    evolve,
)


@dataclass(frozen=True)
class CommandEnvelope:
    """One replay-stable command emitted by a committed lifecycle event."""

    command_id: str
    run_id: str
    organization_id: str
    event_id: str
    aggregate_version: int
    index: int
    command: Command

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "run_id": self.run_id,
            "organization_id": self.organization_id,
            "event_id": self.event_id,
            "aggregate_version": self.aggregate_version,
            "index": self.index,
            "command": self.command.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommandEnvelope":
        command = raw.get("command")
        if not isinstance(command, Mapping):
            raise ValueError("command envelope must contain a command object")
        return cls(
            command_id=str(raw["command_id"]),
            run_id=str(raw["run_id"]),
            organization_id=str(raw["organization_id"]),
            event_id=str(raw["event_id"]),
            aggregate_version=int(raw["aggregate_version"]),
            index=int(raw["index"]),
            command=Command.from_dict(command),
        )


@dataclass(frozen=True)
class LifecycleDecision:
    """A domain transition plus commands ready for a workflow adapter."""

    transition: Transition
    commands: tuple[CommandEnvelope, ...]


def _command_id(state: LifecycleState, event: Event, version: int, index: int) -> str:
    material = (
        f"agent-os:lifecycle-command:v1:{state.organization_id}:{state.run_id}:"
        f"{event.event_id}:{version}:{index}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def event_fingerprint(event: Event) -> str:
    """Canonicalize a durable event and reject non-primitive payloads early."""

    record: Mapping[str, Any] = {
        "event_id": event.event_id,
        "kind": event.kind.value,
        "expected_version": event.expected_version,
        "payload": event.payload,
    }
    try:
        encoded = json.dumps(
            record,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise TransitionRejected("event payload must contain JSON-compatible primitives") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def plan_transition(state: LifecycleState, event: Event) -> LifecycleDecision:
    """Apply one event and deterministically envelope any emitted commands."""

    transition = evolve(state, event)
    if transition.duplicate:
        return LifecycleDecision(transition, ())

    commands = tuple(
        CommandEnvelope(
            command_id=_command_id(state, event, transition.state.version, index),
            run_id=state.run_id,
            organization_id=state.organization_id,
            event_id=event.event_id,
            aggregate_version=transition.state.version,
            index=index,
            command=command,
        )
        for index, command in enumerate(transition.commands)
    )
    return LifecycleDecision(transition, commands)


class LifecycleHistory:
    """In-memory event-history harness for replay and adapter contract tests.

    This is not a production event store.  Temporal will own durable history.
    The harness models the two rules its adapter must preserve: an event ID is
    unique across the *entire* run, and replay produces the same state and
    command identities without executing side effects.
    """

    def __init__(self, initial_state: LifecycleState) -> None:
        if initial_state.version != 0 or initial_state.last_event_id is not None:
            raise ValueError("history requires a new version-zero lifecycle state")
        self._initial_state = initial_state
        self._state = initial_state
        self._events: list[Event] = []
        self._fingerprints: dict[str, str] = {}

    @property
    def initial_state(self) -> LifecycleState:
        return self._initial_state

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def append(self, event: Event) -> LifecycleDecision:
        fingerprint = event_fingerprint(event)
        prior = self._fingerprints.get(event.event_id)
        if prior is not None:
            if prior != fingerprint:
                raise TransitionRejected("event_id was reused with different event content")
            duplicate = Transition(
                state=self._state,
                commands=(),
                event_id=event.event_id,
                prior_version=self._state.version,
                duplicate=True,
            )
            return LifecycleDecision(duplicate, ())

        decision = plan_transition(self._state, event)
        self._state = decision.transition.state
        self._events.append(event)
        self._fingerprints[event.event_id] = fingerprint
        return decision

    @classmethod
    def replay(
        cls,
        initial_state: LifecycleState,
        events: Iterable[Event],
    ) -> "LifecycleHistory":
        history = cls(initial_state)
        for event in events:
            history.append(event)
        return history
