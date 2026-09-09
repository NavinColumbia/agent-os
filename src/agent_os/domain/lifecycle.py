"""The single framework-neutral Agent OS product lifecycle.

This module deliberately contains no clocks, sleeps, database calls, queues,
model calls, or workflow-framework decorators.  It answers only one question:
given a valid lifecycle state and a durable event, what is the next state and
which application commands should be scheduled?

Temporal will eventually persist and replay these decisions.  Temporal is not
allowed to redefine them.  Worker health, leases, activity attempts, browser
sessions, and QA explorer actors are execution telemetry rather than product
lifecycle states.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping


class LifecyclePhase(str, Enum):
    """Coarse customer-visible progress; deliberately small and monotonic."""

    INTAKE = "intake"
    RESEARCH = "research"
    SPECIFY = "specify"
    BUILD = "build"
    VERIFY = "verify"
    RELEASE = "release"


class LifecycleStatus(str, Enum):
    """Execution condition, kept separate from the product phase."""

    ACTIVE = "active"
    WAITING = "waiting"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"


class WaitKind(str, Enum):
    HUMAN = "human"
    EXTERNAL = "external"
    CAPACITY = "capacity"
    RETRY = "retry"


class EventKind(str, Enum):
    SCOPE_ACCEPTED = "scope_accepted"
    MISSION_COMPLETED = "mission_completed"
    RESEARCH_COMPLETED = "research_completed"
    SPECIFICATION_APPROVED = "specification_approved"
    BUILD_COMPLETED = "build_completed"
    VERIFICATION_REPAIR_REQUIRED = "verification_repair_required"
    REPAIR_COMPLETED = "repair_completed"
    VERIFICATION_PASSED = "verification_passed"
    RELEASE_COMPLETED = "release_completed"

    WAIT_REQUESTED = "wait_requested"
    WAIT_RESOLVED = "wait_resolved"
    OPERATION_FAILED = "operation_failed"
    RECOVERY_REQUESTED = "recovery_requested"
    CANCEL_REQUESTED = "cancel_requested"


class CommandKind(str, Enum):
    START_MISSION = "start_mission"
    START_RESEARCH = "start_research"
    START_SPECIFICATION = "start_specification"
    START_BUILD = "start_build"
    START_VERIFICATION = "start_verification"
    START_REPAIR = "start_repair"
    START_RELEASE = "start_release"
    RESUME_PHASE = "resume_phase"
    NOTIFY_HUMAN = "notify_human"
    NOTIFY_OPERATOR = "notify_operator"
    SCHEDULE_RETRY = "schedule_retry"
    CANCEL_ACTIVE_OPERATION = "cancel_active_operation"
    PUBLISH_COMPLETION = "publish_completion"


@dataclass(frozen=True)
class Failure:
    operation: str
    reason: str
    recoverable: bool = True


@dataclass(frozen=True)
class WaitState:
    kind: WaitKind
    correlation_id: str
    reason: str
    resume_command: CommandKind
    retry_at: str | None = None


@dataclass(frozen=True)
class LifecycleState:
    run_id: str
    organization_id: str
    phase: LifecyclePhase = LifecyclePhase.INTAKE
    status: LifecycleStatus = LifecycleStatus.ACTIVE
    version: int = 0
    title: str | None = None
    objective: str | None = None
    wait: WaitState | None = None
    failure: Failure | None = None
    artifact_revision: str | None = None
    verification_cycle: int = 0
    last_event_id: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip() or not self.organization_id.strip():
            raise ValueError("run_id and organization_id are required")
        if self.version < 0 or self.verification_cycle < 0:
            raise ValueError("version and verification_cycle cannot be negative")
        if self.title is not None and (not self.title.strip() or len(self.title) > 200):
            raise ValueError("lifecycle title must contain 1 to 200 characters")
        if self.objective is not None and (not self.objective.strip() or len(self.objective) > 50_000):
            raise ValueError("lifecycle objective must contain 1 to 50000 characters")
        if (self.status is LifecycleStatus.WAITING) != (self.wait is not None):
            raise ValueError("wait details exist if and only if status is waiting")
        if (self.status is LifecycleStatus.FAILED) != (self.failure is not None):
            raise ValueError("failure details exist if and only if status is failed")
        if self.status is LifecycleStatus.SUCCEEDED and self.phase is not LifecyclePhase.RELEASE:
            raise ValueError("only the release phase can succeed")
        if self.status in {LifecycleStatus.ACTIVE, LifecycleStatus.SUCCEEDED,
                           LifecycleStatus.CANCELLED} and (self.wait or self.failure):
            raise ValueError("active and terminal states cannot retain wait/failure details")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "organization_id": self.organization_id,
            "phase": self.phase.value,
            "status": self.status.value,
            "version": self.version,
            "title": self.title,
            "objective": self.objective,
            "wait": None if self.wait is None else {
                "kind": self.wait.kind.value,
                "correlation_id": self.wait.correlation_id,
                "reason": self.wait.reason,
                "resume_command": self.wait.resume_command.value,
                "retry_at": self.wait.retry_at,
            },
            "failure": None if self.failure is None else {
                "operation": self.failure.operation,
                "reason": self.failure.reason,
                "recoverable": self.failure.recoverable,
            },
            "artifact_revision": self.artifact_revision,
            "verification_cycle": self.verification_cycle,
            "last_event_id": self.last_event_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LifecycleState":
        wait_raw = raw.get("wait")
        failure_raw = raw.get("failure")
        return cls(
            run_id=str(raw["run_id"]),
            organization_id=str(raw["organization_id"]),
            phase=LifecyclePhase(str(raw.get("phase", LifecyclePhase.INTAKE.value))),
            status=LifecycleStatus(str(raw.get("status", LifecycleStatus.ACTIVE.value))),
            version=int(raw.get("version", 0)),
            title=raw.get("title"),
            objective=raw.get("objective"),
            wait=None if wait_raw is None else WaitState(
                kind=WaitKind(str(wait_raw["kind"])),
                correlation_id=str(wait_raw["correlation_id"]),
                reason=str(wait_raw["reason"]),
                resume_command=CommandKind(str(wait_raw["resume_command"])),
                retry_at=wait_raw.get("retry_at"),
            ),
            failure=None if failure_raw is None else Failure(
                operation=str(failure_raw["operation"]),
                reason=str(failure_raw["reason"]),
                recoverable=bool(failure_raw.get("recoverable", True)),
            ),
            artifact_revision=raw.get("artifact_revision"),
            verification_cycle=int(raw.get("verification_cycle", 0)),
            last_event_id=raw.get("last_event_id"),
        )


@dataclass(frozen=True)
class Event:
    event_id: str
    kind: EventKind
    expected_version: int
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "expected_version": self.expected_version,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Event":
        payload = raw.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ValueError("event payload must be an object")
        return cls(
            event_id=str(raw["event_id"]),
            kind=EventKind(str(raw["kind"])),
            expected_version=int(raw["expected_version"]),
            payload=dict(payload),
        )


@dataclass(frozen=True)
class Command:
    kind: CommandKind
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "payload": dict(self.payload)}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Command":
        payload = raw.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ValueError("command payload must be an object")
        return cls(kind=CommandKind(str(raw["kind"])), payload=dict(payload))


@dataclass(frozen=True)
class Transition:
    state: LifecycleState
    commands: tuple[Command, ...]
    event_id: str
    prior_version: int
    duplicate: bool = False


class TransitionRejected(ValueError):
    """An event is stale, out of order, or invalid for the current state."""


_FORWARD: dict[tuple[LifecyclePhase, EventKind], tuple[LifecyclePhase, CommandKind]] = {
    (LifecyclePhase.INTAKE, EventKind.SCOPE_ACCEPTED):
        (LifecyclePhase.RESEARCH, CommandKind.START_MISSION),
    (LifecyclePhase.RESEARCH, EventKind.RESEARCH_COMPLETED):
        (LifecyclePhase.SPECIFY, CommandKind.START_SPECIFICATION),
    (LifecyclePhase.SPECIFY, EventKind.SPECIFICATION_APPROVED):
        (LifecyclePhase.BUILD, CommandKind.START_BUILD),
    (LifecyclePhase.BUILD, EventKind.BUILD_COMPLETED):
        (LifecyclePhase.VERIFY, CommandKind.START_VERIFICATION),
    (LifecyclePhase.VERIFY, EventKind.VERIFICATION_PASSED):
        (LifecyclePhase.RELEASE, CommandKind.START_RELEASE),
}

_TERMINAL = {LifecycleStatus.SUCCEEDED, LifecycleStatus.CANCELLED}


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise TransitionRejected(f"{key} is required")
    return value


def _command_kind(payload: Mapping[str, Any], key: str = "resume_command") -> CommandKind:
    try:
        return CommandKind(str(payload.get(key) or CommandKind.RESUME_PHASE.value))
    except ValueError as exc:
        raise TransitionRejected(f"unknown {key}") from exc


def _commit(
    state: LifecycleState,
    event: Event,
    commands: tuple[Command, ...] = (),
    **changes: Any,
) -> Transition:
    next_state = replace(
        state,
        version=state.version + 1,
        last_event_id=event.event_id,
        **changes,
    )
    return Transition(next_state, commands, event.event_id, state.version)


def evolve(state: LifecycleState, event: Event) -> Transition:
    """Apply one durable event with optimistic-version and transition checks.

    Re-delivery of the immediately committed event is an idempotent no-op.  Any
    other stale version is rejected.  The event store/workflow adapter must also
    enforce a unique ``event_id`` so older duplicates remain harmless after many
    subsequent transitions.
    """

    if not event.event_id.strip():
        raise TransitionRejected("event_id is required")
    if event.event_id == state.last_event_id:
        return Transition(state, (), event.event_id, state.version, duplicate=True)
    if event.expected_version != state.version:
        raise TransitionRejected(
            f"stale event version {event.expected_version}; current version is {state.version}"
        )

    if state.status in _TERMINAL:
        raise TransitionRejected(f"{state.status.value} lifecycle is terminal")

    if event.kind is EventKind.CANCEL_REQUESTED:
        reason = str(event.payload.get("reason") or "cancelled")
        return _commit(
            state,
            event,
            (Command(CommandKind.CANCEL_ACTIVE_OPERATION, {"reason": reason}),),
            status=LifecycleStatus.CANCELLED,
            wait=None,
            failure=None,
        )

    if state.status is LifecycleStatus.WAITING:
        if event.kind is not EventKind.WAIT_RESOLVED:
            raise TransitionRejected("waiting lifecycle accepts only wait_resolved or cancellation")
        correlation_id = _required_text(event.payload, "correlation_id")
        assert state.wait is not None
        if correlation_id != state.wait.correlation_id:
            raise TransitionRejected("wait correlation_id does not match")
        command_payload = dict(event.payload)
        command_payload.pop("correlation_id", None)
        return _commit(
            state,
            event,
            (Command(state.wait.resume_command, command_payload),),
            status=LifecycleStatus.ACTIVE,
            wait=None,
        )

    if state.status is LifecycleStatus.FAILED:
        if event.kind is not EventKind.RECOVERY_REQUESTED:
            raise TransitionRejected("failed lifecycle accepts only recovery_requested or cancellation")
        assert state.failure is not None
        if not state.failure.recoverable:
            raise TransitionRejected("failure is not recoverable; create a new lifecycle")
        return _commit(
            state,
            event,
            (Command(_command_kind(event.payload), dict(event.payload)),),
            status=LifecycleStatus.ACTIVE,
            failure=None,
        )

    if event.kind is EventKind.WAIT_REQUESTED:
        try:
            wait_kind = WaitKind(_required_text(event.payload, "wait_kind"))
        except ValueError as exc:
            raise TransitionRejected("unknown wait_kind") from exc
        correlation_id = _required_text(event.payload, "correlation_id")
        reason = _required_text(event.payload, "reason")
        resume = _command_kind(event.payload)
        wait = WaitState(wait_kind, correlation_id, reason, resume, event.payload.get("retry_at"))
        commands: tuple[Command, ...] = ()
        if wait_kind is WaitKind.HUMAN:
            commands = (Command(CommandKind.NOTIFY_HUMAN, {
                "correlation_id": correlation_id,
                "reason": reason,
            }),)
        return _commit(state, event, commands, status=LifecycleStatus.WAITING, wait=wait)

    if event.kind is EventKind.OPERATION_FAILED:
        operation = _required_text(event.payload, "operation")
        reason = _required_text(event.payload, "reason")
        recoverable = bool(event.payload.get("recoverable", True))
        retryable = bool(event.payload.get("retryable", False))
        if retryable:
            correlation_id = str(event.payload.get("correlation_id") or event.event_id)
            resume = _command_kind(event.payload)
            wait = WaitState(
                WaitKind.RETRY,
                correlation_id,
                reason,
                resume,
                event.payload.get("retry_at"),
            )
            return _commit(
                state,
                event,
                (Command(CommandKind.SCHEDULE_RETRY, {
                    "correlation_id": correlation_id,
                    "operation": operation,
                    "retry_at": wait.retry_at,
                }),),
                status=LifecycleStatus.WAITING,
                wait=wait,
            )
        failure = Failure(operation, reason, recoverable)
        return _commit(
            state,
            event,
            (Command(CommandKind.NOTIFY_OPERATOR, {
                "operation": operation,
                "reason": reason,
                "recoverable": recoverable,
            }),),
            status=LifecycleStatus.FAILED,
            failure=failure,
        )

    if event.kind is EventKind.MISSION_COMPLETED:
        raw_evidence = event.payload.get("evidence_ids", ())
        if (
            not isinstance(raw_evidence, (list, tuple))
            or not raw_evidence
            or any(not isinstance(item, str) or not item.strip() for item in raw_evidence)
        ):
            raise TransitionRejected("mission completion requires durable evidence_ids")
        evidence_ids = list(dict.fromkeys(item.strip() for item in raw_evidence))
        summary = _required_text(event.payload, "summary")
        return _commit(
            state,
            event,
            (Command(CommandKind.PUBLISH_COMPLETION, {
                "summary": summary,
                "evidence_ids": evidence_ids,
            }),),
            phase=LifecyclePhase.RELEASE,
            status=LifecycleStatus.SUCCEEDED,
            artifact_revision=evidence_ids[0],
            wait=None,
            failure=None,
        )

    forward = _FORWARD.get((state.phase, event.kind))
    if forward is not None:
        phase, command = forward
        changes: dict[str, Any] = {"phase": phase}
        if event.kind is EventKind.SCOPE_ACCEPTED:
            prompt = event.payload.get("prompt")
            title = event.payload.get("title")
            if isinstance(prompt, str) and prompt.strip():
                changes["objective"] = prompt.strip()
            if isinstance(title, str) and title.strip():
                changes["title"] = title.strip()
        if event.kind is EventKind.BUILD_COMPLETED:
            changes["artifact_revision"] = _required_text(event.payload, "artifact_revision")
        return _commit(
            state,
            event,
            (Command(command, dict(event.payload)),),
            **changes,
        )

    if state.phase is LifecyclePhase.VERIFY:
        if event.kind is EventKind.VERIFICATION_REPAIR_REQUIRED:
            return _commit(
                state,
                event,
                (Command(CommandKind.START_REPAIR, dict(event.payload)),),
                verification_cycle=state.verification_cycle + 1,
            )
        if event.kind is EventKind.REPAIR_COMPLETED:
            revision = _required_text(event.payload, "artifact_revision")
            return _commit(
                state,
                event,
                (Command(CommandKind.START_VERIFICATION, dict(event.payload)),),
                artifact_revision=revision,
            )

    if state.phase is LifecyclePhase.RELEASE and event.kind is EventKind.RELEASE_COMPLETED:
        return _commit(
            state,
            event,
            (Command(CommandKind.PUBLISH_COMPLETION, dict(event.payload)),),
            status=LifecycleStatus.SUCCEEDED,
        )

    raise TransitionRejected(
        f"event {event.kind.value} is invalid in {state.phase.value}/{state.status.value}"
    )
