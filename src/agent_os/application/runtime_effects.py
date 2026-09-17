"""Bridge durable workflow tools through the mission assurance reference monitor.

The graph runtime remains framework-neutral.  This adapter turns a concrete
tool-node attempt into an effect request, derives an attenuated one-effect
grant from the CEO's mission authority, and settles the reservation after the
idempotent handler returns.  Raw tool inputs are never copied into the
authority ledger; an exact canonical digest binds the approval instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Mapping

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.ports import MissionControlStore
from agent_os.domain.mission_model import (
    AssuranceDisposition,
    AuthorityGrant,
    EffectRequest,
    EffectRisk,
    MissionSpec,
    canonical_fingerprint,
)
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowNode
from agent_os.domain.workflow_runtime import (
    TokenStatus,
    WorkflowAction,
    WorkflowRunState,
)


RUNTIME_AUTHORITY_ACTOR = "agent:mission-runtime"
RUNTIME_EFFECT_SCOPES = (
    "connector.invoke",
    "deploy.*",
    "preview.fetch",
    "sandbox.run",
)
_INTERNAL_TOOL_PREFIXES = ("artifact.", "workflow.")
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def runtime_authority_id(tenant_id: str, mission_id: str, mission_revision: int) -> str:
    digest = hashlib.sha256(
        f"agent-os:runtime-authority:v1:{tenant_id}:{mission_id}:r{mission_revision}".encode()
    ).hexdigest()
    return f"grant-runtime-{digest[:40]}"


def build_runtime_authority(spec: MissionSpec) -> AuthorityGrant:
    """Materialize the stable, renewable root authority for runtime effects."""

    authorized_at = spec.revised_at or spec.created_at
    created = datetime.fromisoformat(authorized_at.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
    return AuthorityGrant(
        grant_id=runtime_authority_id(spec.tenant_id, spec.mission_id, spec.revision),
        tenant_id=spec.tenant_id,
        mission_id=spec.mission_id,
        principal_id=spec.principal_id,
        delegate_id=RUNTIME_AUTHORITY_ACTOR,
        allowed_effects=RUNTIME_EFFECT_SCOPES,
        allowed_resources=(f"mission:{spec.mission_id}:*",),
        budget_limit_cents=spec.budget_limit_cents,
        valid_from=created.isoformat(),
        expires_at=(created + timedelta(days=30)).isoformat(),
        delegation_chain=(spec.principal_id, RUNTIME_AUTHORITY_ACTOR),
        mission_revision=spec.revision,
        policy_version="agent-os-baseline-policy-v1",
    )


def _token_attempt(state: WorkflowRunState, action: WorkflowAction) -> int:
    if action.token_id is None:
        return 0
    try:
        return state.token(action.token_id).attempt
    except LookupError:
        return 0


def _referenced_node_ids(configuration: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for name in ("source", "approval"):
        value = configuration.get(name)
        if isinstance(value, Mapping) and isinstance(value.get("node_id"), str):
            result.add(str(value["node_id"]))
    approval_node_id = configuration.get("approval_node_id")
    if isinstance(approval_node_id, str) and approval_node_id:
        result.add(approval_node_id)
    return result


def _input_digest(
    *,
    definition: WorkflowDefinition,
    state: WorkflowRunState,
    action: WorkflowAction,
    node: WorkflowNode,
) -> str:
    referenced = _referenced_node_ids(node.configuration)
    inputs = [
        token.to_dict()
        for token in state.tokens
        if token.node_id in referenced and token.status is TokenStatus.SUCCEEDED
    ]
    return canonical_fingerprint({
        "workflow_id": definition.workflow_id,
        "workflow_version": definition.version,
        "node": node.to_dict(),
        "action": action.to_dict(),
        "attempt": _token_attempt(state, action),
        "referenced_inputs": inputs,
    })


def _approval_evidence(
    definition: WorkflowDefinition,
    state: WorkflowRunState,
    node: WorkflowNode,
) -> tuple[str, ...]:
    configuration = node.configuration
    reference = configuration.get("approval")
    approval_node_id = (
        str(reference.get("node_id") or "")
        if isinstance(reference, Mapping)
        else str(configuration.get("approval_node_id") or "")
    )
    if not approval_node_id:
        return ()
    declared = next(
        (candidate for candidate in definition.nodes if candidate.node_id == approval_node_id),
        None,
    )
    tokens = [
        token for token in state.tokens
        if token.node_id == approval_node_id and token.status is TokenStatus.SUCCEEDED
    ]
    if declared is None or declared.kind is not NodeKind.HUMAN or len(tokens) != 1:
        return ()
    response = tokens[0].output.get("human_response")
    if not isinstance(response, Mapping) or response.get("approved") is not True:
        return ()
    return tuple(dict.fromkeys((*tokens[0].evidence_ids, f"token:{tokens[0].token_id}")))


def _effect_profile(
    mission_id: str,
    run_id: str,
    node: WorkflowNode,
) -> tuple[EffectRisk, bool, bool, str]:
    tool = str(node.configuration.get("tool") or "workflow.spawn")
    resource_prefix = f"mission:{mission_id}"
    if tool == "deploy.preview":
        return EffectRisk.REVERSIBLE, True, False, f"{resource_prefix}:preview:{run_id}"
    if tool == "preview.fetch":
        return EffectRisk.READ_ONLY, True, False, f"{resource_prefix}:preview-read:{run_id}"
    if tool == "sandbox.run":
        return EffectRisk.REVERSIBLE, True, False, f"{resource_prefix}:sandbox:{node.node_id}"
    if tool in {"deploy.static", "deploy.service"}:
        slug = str(node.configuration.get("app_slug") or "unknown")
        return EffectRisk.CONSEQUENTIAL, True, True, f"{resource_prefix}:{tool}:{slug}"
    if tool == "connector.invoke":
        method = str(node.configuration.get("method") or "GET").upper()
        connector = str(node.configuration.get("connector_id") or "unknown")
        path = str(node.configuration.get("path") or "")
        path_digest = hashlib.sha256(path.encode()).hexdigest()[:24]
        write = method in _WRITE_METHODS
        return (
            EffectRisk.CONSEQUENTIAL if write else EffectRisk.READ_ONLY,
            not write,
            write,
            f"{resource_prefix}:connector:{connector}:{method}:{path_digest}",
        )
    # A newly registered external tool is denied until a human has approved
    # its exact invocation and the root capability is deliberately expanded.
    tool_digest = hashlib.sha256(tool.encode()).hexdigest()[:24]
    return (
        EffectRisk.CONSEQUENTIAL,
        False,
        True,
        f"{resource_prefix}:unclassified:{tool_digest}",
    )


class WorkflowEffectGuard:
    """Fail-closed effect guard used by the production graph tool router."""

    def __init__(self, mission_control: MissionControlStore) -> None:
        self._missions = mission_control

    @staticmethod
    def applies(tool: str) -> bool:
        return not tool.startswith(_INTERNAL_TOOL_PREFIXES)

    def admit(
        self,
        *,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        node: WorkflowNode,
    ) -> Mapping[str, Any] | None:
        tool = (
            "workflow.spawn"
            if node.kind is NodeKind.SUBWORKFLOW
            else str(node.configuration.get("tool") or "")
        )
        if not self.applies(tool):
            return None
        mission_id = str(state.context.get("lifecycle_run_id") or "")
        if not mission_id:
            raise FatalCommandError("external tool effect has no canonical lifecycle mission")
        mission = self._missions.get_mission(tenant_id, mission_id)
        if mission is None:
            raise FatalCommandError("external tool effect has no canonical mission contract")
        root = build_runtime_authority(mission)
        try:
            self._missions.grant_authority(root)
        except ValueError as exc:
            raise FatalCommandError(f"runtime authority is invalid: {exc}") from exc

        input_sha256 = _input_digest(
            definition=definition, state=state, action=action, node=node,
        )
        attempt = _token_attempt(state, action)
        effect_digest = hashlib.sha256(
            f"agent-os:workflow-effect:v1:{tenant_id}:{mission_id}:"
            f"{action.action_id}:{attempt}:{input_sha256}".encode()
        ).hexdigest()
        effect_id = f"effect-{effect_digest[:48]}"
        grant_id = f"grant-effect-{effect_digest[:40]}"
        actor_id = f"agent:workflow-effect:{effect_digest[:32]}"
        risk, reversible, human_required, resource = _effect_profile(
            mission_id, run_id, node,
        )
        approval_evidence = _approval_evidence(definition, state, node)
        approved = bool(approval_evidence)
        child = AuthorityGrant(
            grant_id=grant_id,
            tenant_id=tenant_id,
            mission_id=mission_id,
            principal_id=mission.principal_id,
            delegate_id=actor_id,
            allowed_effects=(tool,),
            allowed_resources=(resource,),
            budget_limit_cents=0,
            valid_from=root.valid_from,
            expires_at=root.expires_at,
            delegation_chain=(*root.delegation_chain, actor_id),
            mission_revision=mission.revision,
            parent_grant_id=root.grant_id,
            human_approved=approved,
            approval_binding_sha256=input_sha256 if approved else None,
            approval_evidence_ids=approval_evidence,
            policy_version=root.policy_version,
        )
        try:
            self._missions.grant_authority(child)
            admission = self._missions.admit_effect(EffectRequest(
                effect_id=effect_id,
                tenant_id=tenant_id,
                mission_id=mission_id,
                actor_id=actor_id,
                authority_grant_id=grant_id,
                action=tool,
                resource=resource,
                risk=risk,
                estimated_cost_cents=0,
                reversible=reversible,
                idempotency_key=f"workflow-effect:{effect_digest}",
                requested_at=datetime.now(timezone.utc).isoformat(),
                input_sha256=input_sha256,
                purpose=node.purpose,
                requires_human_approval=human_required,
            ))
        except (LookupError, ValueError) as exc:
            raise FatalCommandError(f"effect admission failed closed: {exc}") from exc
        decision = admission.get("decision", {})
        if decision.get("disposition") != AssuranceDisposition.ALLOWED.value:
            reasons = decision.get("reasons", ())
            summary = "; ".join(str(item) for item in reasons) or "effect was not admitted"
            raise FatalCommandError(f"effect admission denied: {summary}")
        if admission.get("status") == "failed":
            raise FatalCommandError(
                "effect attempt was already settled as failed; a new workflow attempt is required"
            )
        return {
            "tenant_id": tenant_id,
            "mission_id": mission_id,
            "effect_id": effect_id,
        }

    def settle(self, admission: Mapping[str, Any], *, succeeded: bool) -> None:
        try:
            self._missions.settle_effect(
                tenant_id=str(admission["tenant_id"]),
                mission_id=str(admission["mission_id"]),
                effect_id=str(admission["effect_id"]),
                actual_cost_cents=0,
                succeeded=succeeded,
            )
        except (LookupError, ValueError) as exc:
            raise FatalCommandError(f"effect settlement failed closed: {exc}") from exc
