"""Stable application ports for replaceable Agent OS infrastructure.

The concrete bootstrap uses DBOS, but API handlers and domain behavior depend
only on these contracts.  A future Temporal adapter must pass the same contract
suite rather than changing product semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from agent_os.domain.lifecycle import Event, LifecycleState
from agent_os.domain.mission_model import (
    AuthorityGrant,
    Claim,
    EffectRequest,
    EvidenceRef,
    Hazard,
    MissionSpec,
)
from agent_os.domain.notifications import Notification, NotificationPreferences
from agent_os.domain.web_push import WebPushSubscriptionMaterial
from agent_os.domain.organization import Organization
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


@dataclass(frozen=True)
class ManagementWatchLease:
    """Crash-recoverable ownership of one scheduled mission-health review."""

    tenant_id: str
    run_id: str
    worker_id: str
    attempt: int
    lease_expires_at: str
    last_signal_fingerprint: str | None
    consecutive_signal_checks: int
    notified_level: int
    last_result: Mapping[str, Any] | None


@dataclass(frozen=True)
class NotificationDeliveryLease:
    """Crash-recoverable ownership of one external notification delivery."""

    tenant_id: str
    delivery_id: str
    notification: Mapping[str, Any]
    route: Mapping[str, Any]
    worker_id: str
    attempt: int
    lease_expires_at: str


@dataclass(frozen=True)
class WebPushDeliveryLease:
    """Crash-recoverable ownership of one privacy-reduced device push."""

    tenant_id: str
    delivery_id: str
    subscription_id: str
    subscription: WebPushSubscriptionMaterial
    payload: Mapping[str, Any]
    worker_id: str
    attempt: int
    lease_expires_at: str


@dataclass(frozen=True)
class DecisionResponseLease:
    """Crash-recoverable intent to resume one exact human wait."""

    tenant_id: str
    response_id: str
    notification_id: str
    run_id: str
    correlation_id: str
    event_id: str
    response: Mapping[str, Any]
    expected_version: int
    actor_id: str
    worker_id: str
    attempt: int
    lease_expires_at: str


@dataclass(frozen=True)
class ExperienceEventPage:
    """One bounded, tenant-scoped page from the user-visible event log.

    ``cursor_sequence`` is the safe high-water mark a client may acknowledge.
    When ``reset_required`` is true, the requested cursor predates retained
    history and the client must reload authoritative REST projections before
    resuming at that high-water mark.
    """

    events: tuple[Mapping[str, Any], ...]
    cursor_sequence: int
    minimum_sequence: int
    latest_sequence: int
    has_more: bool = False
    reset_required: bool = False


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

    def list_runs(
        self,
        organization_id: str,
        *,
        limit: int = 100,
    ) -> tuple[LifecycleState, ...]: ...

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
class ReadyTenantSource(Protocol):
    """Discover tenant shards that currently have claimable durable work."""

    def list_ready_tenants(
        self,
        *,
        after_tenant_id: str | None = None,
        limit: int = 128,
    ) -> tuple[str, ...]: ...


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
class CompanyDirectory(Protocol):
    """Standing tenant organization and its immutable change history."""

    def get_organization(self, tenant_id: str) -> Organization: ...

    def hire_agent(
        self,
        *,
        tenant_id: str,
        role: str,
        team_id: str,
        manager_id: str,
        capabilities: tuple[str, ...],
        tool_grants: tuple[str, ...],
        hiring_authority: bool,
        spending_limit_cents: int,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def retire_agent(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        reason: str,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def decide_hiring_proposal(
        self,
        *,
        tenant_id: str,
        proposal_id: str,
        approved: bool,
        participant_kind: str,
        reason: str,
        role: str,
        requested_count: int,
        team_id: str | None,
        manager_id: str | None,
        capabilities: tuple[str, ...],
        tool_grants: tuple[str, ...],
        spending_limit_cents: int,
        actor_id: str,
    ) -> Mapping[str, Any]: ...

    def list_external_onboarding(
        self, tenant_id: str,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def confirm_external_onboarding(
        self,
        *,
        tenant_id: str,
        onboarding_id: str,
        display_name: str,
        identity_subject: str | None,
        response_sla_seconds: int,
        quality_criteria: tuple[str, ...],
        attestations: tuple[str, ...],
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def list_company_events(
        self,
        tenant_id: str,
        *,
        after_version: int = 0,
        limit: int = 500,
    ) -> tuple[Mapping[str, Any], ...]: ...


@runtime_checkable
class ConnectorRegistry(Protocol):
    """Tenant-owned external capability definitions; secrets stay out of this port."""

    def register_connector(
        self,
        *,
        tenant_id: str,
        definition: Mapping[str, Any],
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def get_connector(
        self, tenant_id: str, connector_id: str,
    ) -> Mapping[str, Any] | None: ...

    def list_connectors(
        self, tenant_id: str,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def disable_connector(
        self,
        *,
        tenant_id: str,
        connector_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None: ...


@runtime_checkable
class MembershipStore(Protocol):
    """Identity-bound tenant membership and short-lived invitation authority."""

    def roles_for(self, tenant_id: str, subject_id: str) -> frozenset[str] | None: ...

    def organizations_for(self, subject_id: str) -> tuple[Mapping[str, Any], ...]: ...

    def list_members(self, tenant_id: str) -> tuple[Mapping[str, Any], ...]: ...

    def create_invitation(
        self,
        *,
        tenant_id: str,
        roles: tuple[str, ...],
        actor_id: str,
        expires_in_seconds: int,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def claim_invitation(
        self,
        *,
        token: str,
        subject_id: str,
    ) -> Mapping[str, Any]: ...

    def revoke_member(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None: ...


@runtime_checkable
class MissionParticipantStore(Protocol):
    """Subject-bound access assignments for one tenant mission."""

    def can_access(self, tenant_id: str, mission_id: str, subject_id: str) -> bool: ...

    def mission_ids_for_subject(
        self, tenant_id: str, subject_id: str, *, limit: int = 1_000,
    ) -> tuple[str, ...]: ...

    def list_participants(
        self, tenant_id: str, mission_id: str,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def grant_participant(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        subject_id: str,
        participation_role: str,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def revoke_participant(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        subject_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None: ...


@runtime_checkable
class MissionConversationStore(Protocol):
    """Immutable, mission-scoped human and agent communication."""

    def list_messages(
        self,
        tenant_id: str,
        mission_id: str,
        *,
        include_internal: bool,
        limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def append_message(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        sender_id: str,
        sender_persona: str,
        channel: str,
        kind: str,
        body: str,
        reply_to_message_id: str | None,
        audience_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class MissionWorkAssignmentStore(Protocol):
    """Human accountability assignments over durable mission work items."""

    def list_for_mission(
        self, tenant_id: str, mission_id: str,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def list_for_subject(
        self,
        tenant_id: str,
        subject_id: str,
        *,
        mission_ids: tuple[str, ...],
        limit: int = 200,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def list_history(
        self, tenant_id: str, mission_id: str, *, limit: int = 500,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def assign_work(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        work_id: str,
        duty: str,
        work_fingerprint: str,
        subject_id: str,
        participation_role: str,
        assigned_by: str,
        reason: str,
        expected_version: int,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def respond_to_assignment(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        work_id: str,
        duty: str,
        subject_id: str,
        response: str,
        reason: str,
        expected_version: int,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def revoke_assignment(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        work_id: str,
        duty: str,
        revoked_by: str,
        reason: str,
        expected_version: int,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None: ...


@runtime_checkable
class TenantModelStore(Protocol):
    """Tenant-owned model choice with an optional opaque credential reference."""

    def get_model_setting(self, tenant_id: str) -> Mapping[str, Any] | None: ...

    def set_model_setting(
        self,
        *,
        tenant_id: str,
        provider: str,
        model_name: str,
        credential_ref: str | None,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


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
class GraphRunInspector(Protocol):
    """Bounded operational facts for truthful mission-management projections."""

    def inspect_graph_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        action_limit: int = 1_000,
    ) -> Mapping[str, Any] | None: ...


@runtime_checkable
class ManagementWatchStore(Protocol):
    """Scheduled durable health-review queue for active graph runs."""

    def claim_management_watch(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> ManagementWatchLease | None: ...

    def heartbeat_management_watch(
        self,
        tenant_id: str,
        run_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> bool: ...

    def complete_management_watch(
        self,
        tenant_id: str,
        run_id: str,
        *,
        worker_id: str,
        next_check_seconds: int,
        signal_fingerprint: str | None,
        consecutive_signal_checks: int,
        notified_level: int,
        result: Mapping[str, Any],
        retire: bool = False,
    ) -> bool: ...

    def retry_management_watch(
        self,
        tenant_id: str,
        run_id: str,
        *,
        worker_id: str,
        delay_seconds: int,
        error: Mapping[str, Any],
    ) -> bool: ...


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
class ExperienceEventStore(Protocol):
    """Durable user-visible invalidations and safe activity summaries."""

    def list_experience_events(
        self,
        tenant_id: str,
        *,
        after_sequence: int = 0,
        audience_ids: tuple[str, ...] | None = None,
        limit: int = 100,
    ) -> ExperienceEventPage: ...


@runtime_checkable
class NotificationStore(Protocol):
    """Durable notification truth, route policy, and external delivery outbox."""

    def publish_notification(self, notification: Notification) -> bool: ...

    def list_notifications(
        self,
        tenant_id: str,
        *,
        run_id: str | None = None,
        recipient_id: str | None = None,
        recipient_ids: tuple[str, ...] | None = None,
        before: tuple[datetime, str] | None = None,
        limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def get_notification(
        self, tenant_id: str, notification_id: str,
    ) -> Mapping[str, Any] | None: ...

    def list_notification_states(
        self,
        tenant_id: str,
        *,
        subject_id: str,
        notification_ids: tuple[str, ...],
    ) -> Mapping[str, Mapping[str, Any]]: ...

    def set_notification_state(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        notification_id: str,
        status: str,
        snoozed_until: str | None,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def get_notification_preferences(
        self, tenant_id: str, *, subject_id: str,
    ) -> Mapping[str, Any]: ...

    def set_notification_preferences(
        self,
        preferences: NotificationPreferences,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def register_push_subscription(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        device_id: str,
        device_name: str,
        audience_ids: tuple[str, ...],
        material: WebPushSubscriptionMaterial,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def list_push_subscriptions(
        self,
        tenant_id: str,
        *,
        subject_id: str,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def revoke_push_subscription(
        self,
        *,
        tenant_id: str,
        subject_id: str,
        subscription_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None: ...

    def claim_web_push_delivery(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> WebPushDeliveryLease | None: ...

    def heartbeat_web_push_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> bool: ...

    def complete_web_push_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        worker_id: str,
        result: Mapping[str, Any],
    ) -> bool: ...

    def retry_web_push_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
        delay_seconds: int,
    ) -> bool: ...

    def fail_web_push_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
        revoke_subscription: bool = False,
    ) -> bool: ...

    def list_web_push_deliveries(
        self,
        tenant_id: str,
        *,
        subject_id: str,
        limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def get_web_push_delivery(
        self,
        tenant_id: str,
        *,
        subject_id: str,
        delivery_id: str,
    ) -> Mapping[str, Any] | None: ...

    def admit_decision_response(
        self,
        *,
        tenant_id: str,
        notification_id: str,
        run_id: str,
        correlation_id: str,
        response: Mapping[str, Any],
        expected_version: int,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def get_decision_response(
        self, tenant_id: str, *, notification_id: str,
    ) -> Mapping[str, Any] | None: ...

    def list_decision_responses(
        self, tenant_id: str, *, notification_ids: tuple[str, ...],
    ) -> Mapping[str, Mapping[str, Any]]: ...

    def claim_decision_response(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> DecisionResponseLease | None: ...

    def rebase_decision_response(
        self,
        tenant_id: str,
        response_id: str,
        *,
        worker_id: str,
        expected_version: int,
        error: Mapping[str, Any],
    ) -> bool: ...

    def complete_decision_response(
        self,
        tenant_id: str,
        response_id: str,
        *,
        worker_id: str,
        outcome: str,
        result: Mapping[str, Any],
    ) -> bool: ...

    def retry_decision_response(
        self,
        tenant_id: str,
        response_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
        delay_seconds: int,
    ) -> bool: ...

    def fail_decision_response(
        self,
        tenant_id: str,
        response_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
    ) -> bool: ...

    def redrive_decision_response(
        self,
        *,
        tenant_id: str,
        notification_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None: ...

    def register_notification_route(
        self,
        *,
        tenant_id: str,
        definition: Mapping[str, Any],
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def list_notification_routes(
        self, tenant_id: str,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def disable_notification_route(
        self,
        *,
        tenant_id: str,
        route_id: str,
        actor_id: str,
        reason: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None: ...

    def claim_notification_delivery(
        self,
        tenant_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> NotificationDeliveryLease | None: ...

    def heartbeat_notification_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> bool: ...

    def complete_notification_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        worker_id: str,
        result: Mapping[str, Any],
    ) -> bool: ...

    def retry_notification_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
        delay_seconds: int,
    ) -> bool: ...

    def fail_notification_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        worker_id: str,
        error: Mapping[str, Any],
    ) -> bool: ...

    def list_notification_deliveries(
        self, tenant_id: str, *, limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def redrive_notification_delivery(
        self,
        *,
        tenant_id: str,
        delivery_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None: ...


@runtime_checkable
class UsageMeter(Protocol):
    """Durable tenant model-usage reservations and exact post-turn settlement."""

    def reserve_model_turn(
        self,
        *,
        tenant_id: str,
        source_id: str,
        run_id: str,
        category: str,
        model: str,
        maximum_cost_cents: int,
    ) -> Mapping[str, Any]: ...

    def settle_model_turn(
        self,
        *,
        tenant_id: str,
        source_id: str,
        usage: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def usage_summary(self, tenant_id: str) -> Mapping[str, Any]: ...

    def set_monthly_budget(
        self,
        *,
        tenant_id: str,
        monthly_budget_cents: int,
    ) -> Mapping[str, Any]: ...

    def list_usage_events(
        self,
        tenant_id: str,
        *,
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
        usage_category: str = "lifecycle_agent",
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
class MissionControlStore(Protocol):
    """Canonical mission intent, authority, assurance, and evidence boundary."""

    def create_mission(self, spec: MissionSpec) -> Mapping[str, Any]: ...

    def revise_mission(
        self,
        spec: MissionSpec,
        *,
        expected_revision: int,
        revised_by: str,
        reason: str,
    ) -> Mapping[str, Any]: ...

    def get_mission(self, tenant_id: str, mission_id: str) -> MissionSpec | None: ...

    def add_evidence(self, tenant_id: str, evidence: EvidenceRef) -> bool: ...

    def tombstone_evidence(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        evidence_id: str,
        erased_by: str,
        reason: str,
    ) -> Mapping[str, Any]: ...

    def add_claim(self, tenant_id: str, claim: Claim) -> bool: ...

    def add_hazard(self, tenant_id: str, hazard: Hazard) -> bool: ...

    def grant_authority(self, grant: AuthorityGrant) -> bool: ...

    def revoke_authority(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        grant_id: str,
        revoked_by: str,
        reason: str,
    ) -> Mapping[str, Any]: ...

    def admit_effect(self, effect: EffectRequest) -> Mapping[str, Any]: ...

    def settle_effect(
        self,
        *,
        tenant_id: str,
        mission_id: str,
        effect_id: str,
        actual_cost_cents: int,
        succeeded: bool,
    ) -> Mapping[str, Any]: ...

    def control_view(
        self, tenant_id: str, mission_id: str,
    ) -> Mapping[str, Any] | None: ...


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

    def list_artifacts(
        self,
        organization_id: str,
        *,
        media_types: tuple[str, ...] = (),
        limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]: ...


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
class StaticSiteDeployer(Protocol):
    """Publish one immutable static bundle behind a stable application route."""

    def deploy_static(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        app_slug: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class ApplicationDeployer(Protocol):
    """Build and promote one verified source bundle as an isolated web service."""

    def deploy_service(
        self,
        *,
        organization_id: str,
        artifact_id: str,
        app_slug: str,
        health_path: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class PreviewDeploymentStore(Deployer, Protocol):
    """Static public deployment authority addressed by an unguessable capability."""

    def resolve_public(
        self, tenant_slug: str, public_id: str,
    ) -> Mapping[str, Any] | None: ...

    def verify_fetch(
        self,
        *,
        organization_id: str,
        public_url: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def list_previews(
        self, organization_id: str, *, limit: int = 100,
    ) -> tuple[Mapping[str, Any], ...]: ...

    def revoke(
        self,
        *,
        organization_id: str,
        deployment_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None: ...


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
