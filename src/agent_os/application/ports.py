"""Stable application ports for replaceable Agent OS infrastructure.

The concrete bootstrap uses DBOS, but API handlers and domain behavior depend
only on these contracts.  A future Temporal adapter must pass the same contract
suite rather than changing product semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from agent_os.domain.lifecycle import Event, LifecycleState
from agent_os.domain.notifications import Notification
from agent_os.domain.organization_events import OrganizationEvent
from agent_os.domain.workflow import WorkflowDefinition
from agent_os.domain.workflow_runtime import WorkflowAction, WorkflowEvent, WorkflowRunState


@dataclass(frozen=True)
class WorkflowReceipt:
    """Durable acknowledgement returned before asynchronous work completes."""

    workflow_id: str
    accepted: bool = True
    duplicate: bool = False


@dataclass(frozen=True)
class CommandLease:
    """A crash-recoverable claim on one durable command-outbox entry."""

    envelope: Mapping[str, Any]
    worker_id: str
    attempt: int
    lease_expires_at: str


@dataclass(frozen=True)
class OrganizationEventReceipt:
    stream_version: int
    duplicate: bool = False


@dataclass(frozen=True)
class GraphWorkflowReceipt:
    state: WorkflowRunState
    actions: tuple[WorkflowAction, ...]
    duplicate: bool = False


@dataclass(frozen=True)
class GraphActionLease:
    """Crash-recoverable ownership of one arbitrary-workflow action."""

    envelope: Mapping[str, Any]
    worker_id: str
    attempt: int
    lease_expires_at: str


@runtime_checkable
class WorkflowEngine(Protocol):
    """Durable lifecycle execution used by the control API."""

    def start_run(
        self,
        initial_state: LifecycleState,
        initial_event: Event | None = None,
    ) -> WorkflowReceipt: ...

    def submit_event(
        self,
        organization_id: str,
        run_id: str,
        event: Event,
    ) -> WorkflowReceipt: ...

    def get_run(self, organization_id: str, run_id: str) -> LifecycleState | None: ...

    def cancel_run(
        self,
        organization_id: str,
        run_id: str,
        *,
        reason: str,
        expected_version: int,
        event_id: str,
    ) -> WorkflowReceipt: ...

    def health(self) -> Mapping[str, Any]: ...


@runtime_checkable
class CommandExecutor(Protocol):
    """Executes one command using ``command_id`` as its idempotency key."""

    def execute(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]: ...


@runtime_checkable
class CommandOutbox(Protocol):
    """Lease-based command delivery; an expired worker claim is reclaimable."""

    def claim_command(
        self,
        organization_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> CommandLease | None: ...

    def heartbeat_command(
        self,
        organization_id: str,
        command_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> bool: ...

    def complete_command(
        self,
        organization_id: str,
        command_id: str,
        *,
        worker_id: str,
        result: Mapping[str, Any],
    ) -> bool: ...

    def retry_command(
        self,
        organization_id: str,
        command_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
        delay_seconds: int,
    ) -> bool: ...

    def fail_command(
        self,
        organization_id: str,
        command_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
    ) -> bool: ...


@runtime_checkable
class OrganizationLedger(Protocol):
    """Immutable, optimistic-versioned history for the internal AI company."""

    def append_organization_event(
        self,
        event: OrganizationEvent,
    ) -> OrganizationEventReceipt: ...

    def append_organization_events(
        self,
        events: tuple[OrganizationEvent, ...],
    ) -> tuple[OrganizationEventReceipt, ...]: ...

    def load_organization_events(
        self,
        tenant_id: str,
        run_id: str,
        *,
        after_version: int = 0,
        limit: int = 500,
    ) -> tuple[Mapping[str, Any], ...]: ...


@runtime_checkable
class GraphWorkflowEngine(Protocol):
    """Persistence boundary for arbitrary versioned customer workflow graphs."""

    def register_workflow(self, definition: WorkflowDefinition) -> bool: ...

    def start_graph_run(
        self,
        tenant_id: str,
        workflow_id: str,
        workflow_version: int,
        *,
        run_id: str,
        request_id: str,
        context: Mapping[str, Any] | None = None,
    ) -> GraphWorkflowReceipt: ...

    def submit_graph_event(
        self,
        tenant_id: str,
        run_id: str,
        event: WorkflowEvent,
    ) -> GraphWorkflowReceipt: ...

    def get_graph_run(self, tenant_id: str, run_id: str) -> WorkflowRunState | None: ...

    def get_workflow_definition(
        self,
        tenant_id: str,
        workflow_id: str,
        version: int,
    ) -> WorkflowDefinition | None: ...


@runtime_checkable
class GraphActionOutbox(Protocol):
    """Tenant-fenced lease delivery for actions emitted by workflow graphs."""

    def claim_graph_action(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> GraphActionLease | None: ...

    def heartbeat_graph_action(
        self,
        tenant_id: str,
        action_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> bool: ...

    def complete_graph_action(
        self,
        tenant_id: str,
        action_id: str,
        *,
        worker_id: str,
        result: Mapping[str, Any],
    ) -> bool: ...

    def retry_graph_action(
        self,
        tenant_id: str,
        action_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
        delay_seconds: int,
    ) -> bool: ...

    def fail_graph_action(
        self,
        tenant_id: str,
        action_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
    ) -> bool: ...


@runtime_checkable
class GraphActionExecutor(Protocol):
    def execute(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]: ...


@runtime_checkable
class GraphNodeRuntime(Protocol):
    def execute_node(
        self,
        *,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class NotificationStore(Protocol):
    """Durable in-product notification publication and tenant inbox."""

    def publish_notification(self, notification: Notification) -> bool: ...

    def list_notifications(
        self,
        tenant_id: str,
        *,
        run_id: str | None = None,
        recipient_id: str | None = None,
        limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]: ...


@runtime_checkable
class AgentRuntime(Protocol):
    def run_agent(
        self,
        *,
        organization_id: str,
        run_id: str,
        role: str,
        prompt: str,
        idempotency_key: str,
        budget_cents: int = 100,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class ModelGateway(Protocol):
    def generate(
        self,
        *,
        organization_id: str,
        messages: tuple[Mapping[str, Any], ...],
        budget_cents: int,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class PolicyEngine(Protocol):
    def authorize(
        self,
        *,
        organization_id: str,
        subject: Mapping[str, Any],
        action: str,
        resource: Mapping[str, Any],
    ) -> bool: ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Immutable tenant-scoped artifact bytes and metadata."""

    def put(
        self,
        *,
        organization_id: str,
        content: bytes,
        media_type: str,
        idempotency_key: str,
    ) -> str: ...

    def get(self, organization_id: str, artifact_id: str) -> bytes | None: ...

    def describe(
        self, organization_id: str, artifact_id: str,
    ) -> Mapping[str, Any] | None: ...

    def find_by_idempotency_key(
        self, organization_id: str, idempotency_key: str,
    ) -> Mapping[str, Any] | None: ...


@runtime_checkable
class SandboxRunner(Protocol):
    def run(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        command: tuple[str, ...],
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class Deployer(Protocol):
    def deploy(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class IdentityProvider(Protocol):
    def authenticate(self, authorization: str | None, session: str | None) -> Mapping[str, Any]: ...


@runtime_checkable
class Notifier(Protocol):
    def notify(
        self,
        *,
        organization_id: str,
        recipient_id: str,
        message: str,
        idempotency_key: str,
    ) -> None: ...
