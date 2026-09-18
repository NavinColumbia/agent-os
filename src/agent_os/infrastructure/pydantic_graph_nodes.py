"""PydanticAI runtime for executable nodes in customer-designed graphs."""

from __future__ import annotations

import base64
import binascii
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re
from typing import Callable, Mapping, Sequence, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai import Agent, BinaryContent, UsageLimits
from pydantic_ai.models import Model

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.ports import ArtifactStore, GraphNodeRuntime, UsageMeter
from agent_os.domain.organization import Organization
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowNode
from agent_os.domain.workflow_runtime import TokenStatus, WorkflowAction, WorkflowRunState
from agent_os.infrastructure.proposed_artifacts import (
    ProposedArtifact,
    persist_and_validate_artifacts,
)
from agent_os.infrastructure.pydantic_agents import (
    HiringRequest,
    ProposedDecision,
    ProposedMessage,
    ProposedWork,
    ModelSelection,
    default_model_name,
    model_usage_record,
)
from agent_os.infrastructure.mission_programs import HumanDecisionBrief


class GraphNodeDisposition(str, Enum):
    COMPLETE = "complete"
    WAIT = "wait"
    FAIL = "fail"


class GraphAgentNodeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    disposition: GraphNodeDisposition
    satisfied_conditions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    artifacts: list[ProposedArtifact] = Field(default_factory=list, max_length=16)
    output: dict[str, Any] = Field(default_factory=dict)
    recipient_ids: list[str] = Field(default_factory=list)
    correlation_id: str | None = None
    reason: str | None = None
    retryable: bool = False
    observations: list[str] = Field(default_factory=list, max_length=100)
    risks: list[str] = Field(default_factory=list, max_length=100)
    messages: list[ProposedMessage] = Field(default_factory=list, max_length=100)
    proposed_work: list[ProposedWork] = Field(default_factory=list, max_length=100)
    hiring_requests: list[HiringRequest] = Field(default_factory=list, max_length=32)
    decisions: list[ProposedDecision] = Field(default_factory=list, max_length=100)
    next_actions: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_disposition(self) -> "GraphAgentNodeOutput":
        if self.disposition is GraphNodeDisposition.COMPLETE and not (
            self.evidence_ids or self.artifacts
        ):
            raise ValueError("graph node completion requires evidence")
        if self.disposition is GraphNodeDisposition.WAIT:
            if not self.recipient_ids or not self.correlation_id or not self.reason:
                raise ValueError("graph node wait requires recipients, correlation, and reason")
        if self.disposition is GraphNodeDisposition.FAIL and not self.reason:
            raise ValueError("graph node failure requires a reason")
        return self


GraphNodeHandler = Callable[
    [str, str, WorkflowDefinition, WorkflowRunState, WorkflowAction, WorkflowNode],
    Mapping[str, Any],
]


_INSTRUCTIONS = """
You are the accountable owner of one node inside a durable, non-linear company workflow.
Complete only this node. Use authoritative context, expose uncertainty, and choose only listed outgoing
conditions. Never report completion without durable evidence IDs. If human authority or missing facts are
required, return a correlated wait. If work cannot proceed, fail honestly and state whether retry is useful.
Create new evidence through the bounded artifacts field. Cite an evidence ID only when it appears in the
authoritative prior-token context; never invent one. Source code uses a source-bundle artifact with a files map.
Artifacts proposed in the current output are persisted automatically by the authority layer and their durable
IDs are appended before completion is committed. Therefore, do not ask a human or another node to persist a
same-turn artifact first; propose it now and complete with the appropriate allowed condition.
Proactively report risks, decisions, messages, delegations, missing specialists, and next actions. A hiring or
external message is a proposal until the organization authority applies it. Your structured output is a
proposal; deterministic workflow policy commits the transition.
""".strip()


class PydanticGraphNodeRuntime(GraphNodeRuntime):
    """Execute safe structural nodes and provider-backed agent/decision nodes."""

    def __init__(
        self,
        model: Model | str,
        *,
        tools: Sequence[Any] = (),
        handlers: Mapping[NodeKind, GraphNodeHandler] | None = None,
        artifact_store: ArtifactStore | None = None,
        request_limit: int = 12,
        output_tokens_limit: int = 8_000,
        request_timeout_seconds: float = 120,
        max_turn_budget_cents: int = 100,
        context_character_limit: int = 64_000,
        organization_loader: Callable[[str], Organization] | None = None,
        usage_meter: UsageMeter | None = None,
        model_name: str | None = None,
        model_selector: Callable[[str], ModelSelection] | None = None,
    ) -> None:
        if (
            request_limit < 1
            or output_tokens_limit < 1
            or request_timeout_seconds <= 0
            or max_turn_budget_cents < 1
            or context_character_limit < 1_000
        ):
            raise ValueError("graph node runtime limits must be positive")
        self._model = model
        self._tools = tuple(tools)
        self._handlers = dict(handlers or {})
        self._artifact_store = artifact_store
        self._request_limit = request_limit
        self._output_tokens_limit = output_tokens_limit
        self._request_timeout_seconds = request_timeout_seconds
        self._max_turn_budget_cents = max_turn_budget_cents
        self._context_character_limit = context_character_limit
        self._organization_loader = organization_loader
        self._usage_meter = usage_meter
        self._model_name = default_model_name(model, model_name)
        self._model_selector = model_selector
        if usage_meter is not None and not self._model_name:
            raise ValueError("a metered graph runtime requires a model name")

    def _selection(self, tenant_id: str) -> ModelSelection:
        if self._model_selector is None:
            return ModelSelection(self._model, self._model_name)
        selected = self._model_selector(tenant_id)
        if not isinstance(selected, ModelSelection):
            raise TypeError("model selector must return ModelSelection")
        return selected

    def _prior_artifact_context(
        self,
        tenant_id: str,
        state: WorkflowRunState,
        *,
        current_token_id: str,
        preferred_artifact_labels: frozenset[str] = frozenset(),
        preferred_artifact_ids: frozenset[str] = frozenset(),
        preferred_source_paths: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """Hydrate bounded, tenant-scoped text evidence for real agent handoffs."""

        if self._artifact_store is None:
            return []
        candidates: list[tuple[int, int, dict[str, Any]]] = []
        seen: set[str] = set()
        labels_by_artifact: dict[str, set[str]] = {}
        producer_by_artifact: dict[str, tuple[str, int]] = {}
        for prior in state.tokens:
            if prior.token_id == current_token_id:
                continue
            artifact_ids = prior.output.get("artifact_ids", {})
            if not isinstance(artifact_ids, Mapping):
                continue
            for label, artifact_id in artifact_ids.items():
                normalized_id = str(artifact_id)
                labels_by_artifact.setdefault(normalized_id, set()).add(str(label))
                producer_by_artifact[normalized_id] = (prior.node_id, prior.iteration)
        textual_application_types = {
            "application/json",
            "application/vnd.agent-os.source-bundle+json",
            "application/vnd.agent-os.sandbox-result+json",
        }
        for recency, prior in enumerate(reversed(state.tokens)):
            if prior.token_id == current_token_id:
                continue
            for evidence_id in prior.evidence_ids:
                if evidence_id in seen:
                    continue
                seen.add(evidence_id)
                record = self._artifact_store.describe(tenant_id, evidence_id)
                if record is None:
                    continue
                media_type = str(record.get("media_type") or "")
                content = self._artifact_store.get(tenant_id, evidence_id)
                producer_node_id, producer_iteration = producer_by_artifact.get(
                    evidence_id, (prior.node_id, prior.iteration),
                )
                item: dict[str, Any] = {
                    "artifact_id": evidence_id,
                    "media_type": media_type,
                    "byte_length": 0 if content is None else len(content),
                    "producer_node_id": producer_node_id,
                    "producer_iteration": producer_iteration,
                }
                artifact_labels = sorted(labels_by_artifact.get(evidence_id, ()))
                if artifact_labels:
                    item["labels"] = artifact_labels
                is_textual = media_type.startswith("text/") or media_type in textual_application_types
                if content is not None and is_textual:
                    if media_type == "application/vnd.agent-os.source-bundle+json":
                        bundle = self._source_bundle_text_context(
                            content, preferred_paths=preferred_source_paths,
                        )
                        if bundle is None:
                            item["content_omitted"] = "source bundle is not valid bounded JSON"
                        else:
                            item.update(bundle)
                    else:
                        try:
                            decoded = content.decode("utf-8")
                        except UnicodeDecodeError:
                            item["content_omitted"] = "artifact is not valid UTF-8"
                        else:
                            hydration_limit = (
                                40_000
                                if "replacement-mission-program" in artifact_labels
                                else 24_000
                            )
                            if len(decoded) <= hydration_limit:
                                item["content"] = decoded
                            else:
                                item["content_omitted"] = (
                                    f"artifact exceeds the {hydration_limit}-character "
                                    "per-artifact hydration limit"
                                )
                elif content is not None:
                    item["content_omitted"] = "binary evidence is available by durable artifact ID"
                priority = 1
                if evidence_id in preferred_artifact_ids:
                    # The artifact named by the immediately preceding node is the
                    # authoritative repair input.  Keep it ahead of artifacts it
                    # references; otherwise a large dependency bundle can consume
                    # the context budget before a rejected replacement program is
                    # hydrated for correction.
                    priority = -6
                elif not preferred_artifact_ids and any(
                    label in preferred_artifact_labels for label in artifact_labels
                ):
                    priority = -5
                if media_type == "application/vnd.agent-os.source-bundle+json":
                    priority = min(priority, 0)
                    hydrated_files = item.get("source_bundle_text_files", {})
                    if isinstance(hydrated_files, Mapping) and any(
                        any(word in str(path).lower() for word in ("result", "report", "summary"))
                        for path in hydrated_files
                    ) and not preferred_artifact_ids:
                        priority = -1
                candidates.append((priority, recency, item))
                if len(candidates) >= 64:
                    break
            if len(candidates) >= 64:
                break
        expanded_preferred = set(preferred_artifact_ids)
        for _ in range(2):
            discovered = set()
            for _, _, item in candidates:
                if item["artifact_id"] not in expanded_preferred:
                    continue
                discovered.update(self._artifact_references(item))
            if discovered <= expanded_preferred:
                break
            expanded_preferred.update(discovered)
        if expanded_preferred != set(preferred_artifact_ids):
            rescored = []
            for priority, recency, item in candidates:
                if item["artifact_id"] in preferred_artifact_ids:
                    priority = -6
                elif item["artifact_id"] in expanded_preferred:
                    priority = (
                        -4 if item["media_type"] == "application/vnd.agent-os.source-bundle+json"
                        else -3
                    )
                rescored.append((priority, recency, item))
            candidates = rescored
        candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
        return [item for _, _, item in candidates]

    @staticmethod
    def _artifact_references(value: Any) -> set[str]:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return set(re.findall(r"artifact-[0-9a-f]{64}", serialized))

    @staticmethod
    def _source_path_references(value: Any) -> set[str]:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return {
            match.replace("\\\\", "/").lstrip("./")
            for match in re.findall(
                r"(?:[A-Za-z0-9_.-]+[/\\\\])*[A-Za-z0-9_.-]+\."
                r"(?:css|html|js|json|log|md|py|svg|txt)",
                serialized,
                flags=re.IGNORECASE,
            )
        }

    @staticmethod
    def _source_bundle_files(content: bytes) -> Mapping[str, Any] | None:
        try:
            raw = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(raw, Mapping)
            or raw.get("format") != "agent-os.source-bundle.v1"
            or not isinstance(raw.get("files"), Mapping)
        ):
            return None
        return raw["files"]

    @classmethod
    def _source_bundle_text_context(
        cls, content: bytes, *, preferred_paths: frozenset[str] = frozenset(),
    ) -> dict[str, Any] | None:
        """Expose high-value text files from large sandbox bundles without base64 bloat."""

        files = cls._source_bundle_files(content)
        if files is None:
            return None
        candidates: list[tuple[int, str, str]] = []
        omitted: list[dict[str, Any]] = []
        for raw_path, specification in files.items():
            path = str(raw_path)
            if not isinstance(specification, Mapping):
                omitted.append({"path": path, "reason": "invalid file specification"})
                continue
            encoding = specification.get("encoding")
            raw_content = specification.get("content")
            lowered = path.lower()
            decoded_text: str | None = None
            if encoding == "utf-8" and isinstance(raw_content, str):
                decoded_text = raw_content
            elif (
                encoding == "base64"
                and isinstance(raw_content, str)
                and lowered.endswith((
                    ".css", ".html", ".js", ".json", ".log", ".md", ".py", ".svg", ".txt",
                ))
            ):
                try:
                    decoded_bytes = base64.b64decode(raw_content, validate=True)
                    if len(decoded_bytes) <= 256 * 1024:
                        decoded_text = decoded_bytes.decode("utf-8")
                except (ValueError, binascii.Error, UnicodeDecodeError):
                    decoded_text = None
            if decoded_text is None:
                approximate_bytes = (
                    len(raw_content) * 3 // 4
                    if encoding == "base64" and isinstance(raw_content, str)
                    else None
                )
                omitted.append({
                    "path": path,
                    "reason": "binary file available through attached image or durable artifact",
                    "byte_length": approximate_bytes,
                })
                continue
            if "result" in lowered and lowered.endswith(".json"):
                try:
                    result = json.loads(decoded_text)
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(result, Mapping) and isinstance(result.get("checks"), list):
                        checks = result["checks"]
                        compact_result = {
                            key: value for key, value in result.items() if key != "checks"
                        }
                        compact_result["passed_check_names"] = [
                            str(check.get("name"))
                            for check in checks
                            if isinstance(check, Mapping) and check.get("status") == "pass"
                        ]
                        compact_result["failed_or_blocked_checks"] = [
                            dict(check)
                            for check in checks
                            if isinstance(check, Mapping) and check.get("status") != "pass"
                        ]
                        compact_result["evidence_compaction"] = (
                            "All non-passing checks retain full details; passing checks retain names."
                        )
                        decoded_text = json.dumps(
                            compact_result,
                            allow_nan=False,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
            explicitly_requested = any(
                lowered == requested.lower()
                or lowered.endswith("/" + requested.lower())
                for requested in preferred_paths
            )
            priority = (
                -1 if explicitly_requested
                else 0 if any(word in lowered for word in ("result", "report", "summary", "run.log"))
                else 1 if lowered.endswith((".js", ".py"))
                else 2 if lowered.endswith((".css", ".html", ".json", ".log", ".md", ".txt"))
                else 3
            )
            candidates.append((priority, path, decoded_text))
        hydrated: dict[str, str] = {}
        remaining = 24_000
        for _, path, value in sorted(candidates, key=lambda item: (item[0], item[1])):
            if remaining <= 0:
                omitted.append({"path": path, "reason": "bundle text hydration limit reached"})
                continue
            selected = value[:remaining]
            hydrated[path] = selected
            remaining -= len(selected)
            if len(selected) != len(value):
                omitted.append({
                    "path": path,
                    "reason": "file truncated at bundle text hydration limit",
                    "character_length": len(value),
                })
        return {
            "source_bundle_text_files": hydrated,
            "source_bundle_omitted_files": omitted[:64],
        }

    def _prior_image_context(
        self,
        tenant_id: str,
        state: WorkflowRunState,
        *,
        current_token_id: str,
    ) -> tuple[list[dict[str, Any]], list[BinaryContent]]:
        """Attach a bounded representative screenshot set to visual-review turns."""

        if self._artifact_store is None:
            return [], []
        candidates: list[tuple[int, int, str, str, bytes]] = []
        seen_artifacts: set[str] = set()
        for recency, prior in enumerate(reversed(state.tokens)):
            if prior.token_id == current_token_id:
                continue
            for evidence_id in prior.evidence_ids:
                if evidence_id in seen_artifacts:
                    continue
                seen_artifacts.add(evidence_id)
                record = self._artifact_store.describe(tenant_id, evidence_id)
                if record is None or record.get("media_type") != (
                    "application/vnd.agent-os.source-bundle+json"
                ):
                    continue
                content = self._artifact_store.get(tenant_id, evidence_id)
                files = None if content is None else self._source_bundle_files(content)
                if files is None:
                    continue
                for raw_path, specification in files.items():
                    path = str(raw_path)
                    lowered = path.lower()
                    media_type = (
                        "image/png" if lowered.endswith(".png")
                        else "image/jpeg" if lowered.endswith((".jpg", ".jpeg"))
                        else "image/webp" if lowered.endswith(".webp")
                        else None
                    )
                    if media_type is None or not isinstance(specification, Mapping):
                        continue
                    if specification.get("encoding") != "base64":
                        continue
                    raw_data = specification.get("content")
                    if not isinstance(raw_data, str):
                        continue
                    try:
                        data = base64.b64decode(raw_data, validate=True)
                    except (ValueError, binascii.Error):
                        continue
                    if not data or len(data) > 512 * 1024:
                        continue
                    image_priority = 0 if "/page-" in f"/{lowered}" else 1
                    candidates.append((image_priority, recency, path, media_type, data))
        metadata: list[dict[str, Any]] = []
        attachments: list[BinaryContent] = []
        digests: set[str] = set()
        total_bytes = 0
        for _, _, path, media_type, data in sorted(
            candidates, key=lambda item: (item[0], item[1], item[2]),
        ):
            digest = hashlib.sha256(data).hexdigest()
            if digest in digests or len(attachments) >= 3 or total_bytes + len(data) > 2 * 1024 * 1024:
                continue
            digests.add(digest)
            total_bytes += len(data)
            identifier = f"screenshot-{len(attachments) + 1}-{path.rsplit('/', 1)[-1]}"
            metadata.append({
                "identifier": identifier,
                "source_path": path,
                "media_type": media_type,
                "byte_length": len(data),
                "sha256": digest,
            })
            attachments.append(BinaryContent(
                data=data, media_type=media_type, identifier=identifier,
            ))
        return metadata, attachments

    @staticmethod
    def _node(definition: WorkflowDefinition, node_id: str) -> WorkflowNode:
        return next(node for node in definition.nodes if node.node_id == node_id)

    @staticmethod
    def _compact_prior_output(output: Mapping[str, Any]) -> dict[str, Any]:
        """Keep routing/audit facts in prompt history; artifacts carry bulky detail."""

        retained_keys = {
            "summary",
            "artifact_ids",
            "human_response",
            "validation_error",
            "repair_required",
            "rejected_artifact_id",
            "rejected_evidence_ids",
            "revision_reason",
            "revision_evidence_ids",
            "mission_program_revision",
            "workflow_version",
            "entry_node_id",
            "exit_code",
            "timed_out",
            "stdout",
            "stderr",
            "output_error",
            "output_artifact_id",
            "result_artifact_id",
            "source_artifact_id",
            "deployment_id",
            "receipt_artifact_id",
            "verification_artifact_id",
            "public_url",
            "verified",
            "status_code",
            "content_type",
            "content_sha256",
            "expected_sha256",
            "digest_matches",
        }
        compact = {
            key: value for key, value in output.items()
            if key in retained_keys
        }
        return compact

    @staticmethod
    def _compact_run_context(context: Mapping[str, Any]) -> dict[str, Any]:
        """Keep mission authority without duplicating the entire executable program per node."""

        compact = dict(context)
        mission_program = compact.get("mission_program")
        if isinstance(mission_program, Mapping):
            workstreams = mission_program.get("workstreams")
            if isinstance(workstreams, list):
                coordination = []
                for workstream in workstreams:
                    if not isinstance(workstream, Mapping):
                        continue
                    item = {
                        key: workstream[key]
                        for key in (
                            "workstream_id", "objective", "accountable_role_id",
                            "acceptance_criteria", "coordination",
                        )
                        if key in workstream
                    }
                    if item:
                        coordination.append(item)
                if coordination:
                    compact["mission_coordination"] = coordination
            retained_keys = {
                "format",
                "revision",
                "objective",
                "authorized_budget_cents",
                "success_measures",
                "clarifications",
                "replanning",
            }
            compact["mission_program"] = {
                key: value for key, value in mission_program.items() if key in retained_keys
            }
            omitted = sorted(set(mission_program) - retained_keys)
            if omitted:
                compact["mission_program_omitted_sections"] = omitted
        return compact

    @staticmethod
    def _needs_visual_evidence(
        node: WorkflowNode, node_requirements: Mapping[str, Any],
    ) -> bool:
        visual_request = json.dumps(
            dict(node_requirements), ensure_ascii=False, separators=(",", ":"),
        ).lower()
        review_request = f"{node.purpose} {node.owner_role or ''} {visual_request}".lower()
        return (
            any(term in visual_request for term in ("screenshot", "rendered image", "visual review"))
            and any(term in review_request for term in (
                "inspect", "review", "quality", "qa", "acceptance",
            ))
        )

    def execute_node(
        self,
        *,
        tenant_id: str,
        run_id: str,
        definition: WorkflowDefinition,
        state: WorkflowRunState,
        action: WorkflowAction,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        if not action.node_id or not action.token_id:
            raise FatalCommandError("graph execution requires node and token identity")
        node = self._node(definition, action.node_id)
        token = state.token(action.token_id)
        if token.status is not TokenStatus.RUNNING:
            raise FatalCommandError("graph node runtime requires a running token")

        handler = self._handlers.get(node.kind)
        if handler is not None:
            return dict(handler(tenant_id, run_id, definition, state, action, node))
        if node.kind is NodeKind.HUMAN:
            response = token.output.get("human_response")
            if response is not None:
                if not isinstance(response, Mapping):
                    raise FatalCommandError("durable human response must be an object")
                raw_brief = node.configuration.get("decision_brief")
                if raw_brief is not None:
                    try:
                        brief = HumanDecisionBrief.model_validate(raw_brief)
                    except (TypeError, ValueError) as exc:
                        raise FatalCommandError(
                            f"human node decision_brief is invalid: {exc}"
                        ) from exc
                    allowed_actions = (
                        {"respond"}
                        if brief.kind in {"input", "choice"}
                        else {"approve"}
                    )
                    if brief.kind == "approval" and node.configuration.get(
                        "rejection_condition"
                    ) is not None:
                        allowed_actions.add("decline")
                        if brief.allow_request_changes:
                            allowed_actions.add("request_changes")
                    response_action = response.get("action")
                    if response_action is None:
                        response_action = (
                            "approve" if response.get("approved") is True
                            else "decline" if response.get("approved") is False
                            else "respond"
                        )
                    if (
                        not isinstance(response_action, str)
                        or response_action not in allowed_actions
                    ):
                        raise FatalCommandError(
                            "human response action is not valid for the decision brief"
                        )
                    if response_action in {"respond", "decline", "request_changes"} and not str(
                        response.get("answer") or ""
                    ).strip():
                        raise FatalCommandError("human response action requires durable context")
                    if response_action == "respond" and "approved" in response:
                        raise FatalCommandError("human response intent is inconsistent")
                conditions = [edge.condition for edge in definition.outgoing(node.node_id)
                              if edge.condition != "always"]
                configured_condition = node.configuration.get("response_condition")
                rejection_condition = node.configuration.get("rejection_condition")
                if response.get("approved") is False and rejection_condition is not None:
                    selected = [str(rejection_condition)]
                elif response.get("approved") is False and configured_condition is not None:
                    raise FatalCommandError(
                        "rejected human response has no declared rejection condition"
                    )
                elif configured_condition is not None:
                    selected = [str(configured_condition)]
                elif len(set(conditions)) == 1:
                    selected = [conditions[0]]
                elif conditions:
                    raise FatalCommandError(
                        "human node with multiple response paths requires response_condition"
                    )
                else:
                    selected = []
                evidence_material = json.dumps(
                    dict(response), allow_nan=False, separators=(",", ":"), sort_keys=True,
                )
                evidence_id = "human-response-" + hashlib.sha256(
                    f"{tenant_id}:{run_id}:{token.token_id}:{evidence_material}".encode()
                ).hexdigest()
                return {
                    "disposition": "complete",
                    "satisfied_conditions": selected,
                    "evidence_ids": [evidence_id],
                    "output": {"human_response": dict(response)},
                }
            recipients = node.configuration.get("recipient_ids", ["human:ceo"])
            if not isinstance(recipients, (list, tuple)) or not recipients:
                raise FatalCommandError("human node requires configured recipient_ids")
            raw_brief = node.configuration.get("decision_brief")
            decision_context: dict[str, Any] | None = None
            if raw_brief is not None:
                try:
                    brief = HumanDecisionBrief.model_validate(raw_brief)
                except (TypeError, ValueError) as exc:
                    raise FatalCommandError(
                        f"human node decision_brief is invalid: {exc}"
                    ) from exc
                allowed_actions = (
                    ["respond"]
                    if brief.kind in {"input", "choice"}
                    else ["approve"]
                )
                if brief.kind == "approval" and node.configuration.get(
                    "rejection_condition"
                ) is not None:
                    allowed_actions.append("decline")
                    if brief.allow_request_changes:
                        allowed_actions.append("request_changes")
                decision_context = {
                    **brief.model_dump(mode="json", exclude_none=True),
                    "requesting_role": brief.requesting_role or node.owner_role or "mission-team",
                    "allowed_actions": allowed_actions,
                    "evidence_ids": list(dict.fromkeys(
                        evidence_id
                        for prior in state.tokens
                        if prior.token_id != token.token_id
                        and prior.status is TokenStatus.SUCCEEDED
                        for evidence_id in prior.evidence_ids
                    ))[-16:],
                }
            correlation = str(node.configuration.get("correlation_id") or (
                "graph-question-" + hashlib.sha256(action.action_id.encode()).hexdigest()
            ))
            result = {
                "disposition": "wait",
                "recipient_ids": [str(item) for item in recipients],
                "correlation_id": correlation,
                "reason": node.purpose,
            }
            if decision_context is not None:
                result["decision_context"] = decision_context
            return result
        if node.kind is NodeKind.TERMINAL:
            evidence = tuple(dict.fromkeys(
                evidence_id
                for prior in state.tokens
                if prior.token_id != token.token_id and prior.status is TokenStatus.SUCCEEDED
                for evidence_id in prior.evidence_ids
            ))
            if not evidence:
                raise FatalCommandError("terminal node cannot accept a run without upstream evidence")
            return {
                "disposition": "complete",
                "satisfied_conditions": [],
                "evidence_ids": list(evidence),
                "output": {"accepted_upstream_evidence": list(evidence)},
            }
        if node.kind not in {NodeKind.AGENT, NodeKind.DECISION}:
            raise FatalCommandError(
                f"node kind {node.kind.value} requires an explicitly registered, idempotent handler"
            )

        result_receipt_key = f"{idempotency_key}:agent-result"
        if self._artifact_store is not None:
            prior_receipt = self._artifact_store.find_by_idempotency_key(
                tenant_id, result_receipt_key,
            )
            if prior_receipt is not None:
                receipt_artifact_id = str(prior_receipt.get("artifact_id") or "")
                receipt_content = self._artifact_store.get(tenant_id, receipt_artifact_id)
                if receipt_content is None:
                    raise FatalCommandError("persisted graph agent result receipt is unavailable")
                try:
                    replayed = json.loads(receipt_content)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise FatalCommandError("persisted graph agent result receipt is invalid") from exc
                if not isinstance(replayed, Mapping):
                    raise FatalCommandError("persisted graph agent result receipt is not an object")
                return dict(replayed)

        outgoing = definition.outgoing(node.node_id)
        available_conditions = sorted({edge.condition for edge in outgoing if edge.condition != "always"})
        instructions = (
            f"{_INSTRUCTIONS}\n\nAssigned role: {node.owner_role or 'decision-owner'}. "
            f"Node: {node.node_id}. Purpose: {node.purpose}. "
            f"Allowed conditional paths: {available_conditions}."
        )
        node_requirements = node.configuration.get("agent_context", {})
        if not isinstance(node_requirements, Mapping):
            raise FatalCommandError("agent node configuration agent_context must be an object")
        authoritative = {
            "run_context": self._compact_run_context(state.context),
            "action": dict(action.payload),
            "node_requirements": dict(node_requirements),
            "current_token_input": {
                "iteration": token.iteration,
                "attempt": token.attempt,
                "resumed_output": dict(token.output),
            },
            "prior_tokens": [{
                "node_id": prior.node_id,
                "status": prior.status.value,
                "iteration": prior.iteration,
                "evidence_ids": list(prior.evidence_ids),
                "output": self._compact_prior_output(prior.output),
            } for prior in state.tokens if prior.token_id != token.token_id],
        }
        rejected_revision_exists = any(
            prior.node_id == "revise"
            and prior.output.get("repair_required") is True
            for prior in state.tokens
            if prior.token_id != token.token_id
        )
        if "replacement_artifact_label" in node_requirements and not rejected_revision_exists:
            authoritative["current_workflow_definition"] = definition.to_dict()
        if "replacement_artifact_label" in node_requirements:
            authoritative["workflow_revision_submission"] = {
                "initial_revision": (
                    "Propose one complete agent-os.mission-program.v1 JSON artifact."
                ),
                "after_validation_rejection": {
                    "format": "agent-os.mission-program-merge-patch.v1",
                    "base_artifact_id": (
                        "Use the exact rejected_artifact_id from the latest workflow.revise output."
                    ),
                    "patch": (
                        "A nonempty RFC 7396 JSON Merge Patch containing only changed sections."
                    ),
                },
                "authority": (
                    "The runtime materializes a complete immutable candidate and reapplies the full "
                    "schema, topology, tool, budget, revision, and tenant checks. Prefer a patch after "
                    "rejection; do not rewrite unchanged program sections."
                ),
            }
        if self._organization_loader is not None:
            organization = self._organization_loader(tenant_id)
            active_agents = sorted(
                (item for item in organization.agents.values() if item.status.value == "active"),
                key=lambda item: item.agent_id,
            )
            visible_agents = active_agents[:128]
            authoritative["standing_organization"] = {
                "organization_id": organization.organization_id,
                "teams": [{
                    "team_id": item.team_id,
                    "purpose": item.purpose,
                    "manager_id": item.manager_id,
                } for item in organization.teams.values()],
                "agents": [{
                    "agent_id": item.agent_id,
                    "role": item.role,
                    "team_id": item.team_id,
                    "manager_id": item.manager_id,
                    "capabilities": sorted(item.capabilities),
                    "tool_grants": sorted(item.tool_grants),
                    "hiring_authority": item.hiring_authority,
                    "spending_limit_cents": item.spending_limit_cents,
                } for item in visible_agents],
                "active_agent_count": len(active_agents),
                "directory_truncated": len(visible_agents) != len(active_agents),
            }
        image_metadata: list[dict[str, Any]] = []
        image_attachments: list[BinaryContent] = []
        if self._needs_visual_evidence(node, node_requirements):
            image_metadata, image_attachments = self._prior_image_context(
                tenant_id, state, current_token_id=token.token_id,
            )
        if image_metadata:
            authoritative["attached_images"] = image_metadata
        authoritative["prior_artifacts"] = []
        preferred_artifact_labels = frozenset({
            str(node_requirements["replacement_artifact_label"])
        }) if node_requirements.get("replacement_artifact_label") else frozenset()
        # Explicit artifact IDs in the assigned node contract are stronger than
        # incidental references from the immediately preceding routing node.
        # Release nodes, for example, need the exact approved source bytes—not a
        # newly admitted workflow program that merely points at those bytes.
        preferred_artifact_ids = frozenset(self._artifact_references(node_requirements))
        if not preferred_artifact_ids:
            recent_references: set[str] = set()
            referenced_tokens = 0
            for prior in reversed(state.tokens):
                if prior.token_id == token.token_id or prior.status is not TokenStatus.SUCCEEDED:
                    continue
                references = self._artifact_references(prior.output)
                if not references:
                    continue
                recent_references.update(references)
                referenced_tokens += 1
                # A validator rejection identifies one complete authoritative
                # repair input.  Ordinary acceptance nodes need a short window
                # instead: fetch evidence, publication receipt, and released
                # bytes are commonly produced by three adjacent nodes.
                if (
                    prior.node_id == "revise"
                    and prior.output.get("repair_required") is True
                ):
                    break
                if referenced_tokens >= 3 or len(recent_references) >= 8:
                    break
            preferred_artifact_ids = frozenset(recent_references)
        for artifact in self._prior_artifact_context(
            tenant_id,
            state,
            current_token_id=token.token_id,
            preferred_artifact_labels=preferred_artifact_labels,
            preferred_artifact_ids=preferred_artifact_ids,
            preferred_source_paths=frozenset(
                self._source_path_references(node_requirements)
            ),
        ):
            authoritative["prior_artifacts"].append(artifact)
            candidate_text = json.dumps(
                authoritative,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if len(candidate_text) <= self._context_character_limit:
                continue
            authoritative["prior_artifacts"].pop()
            compact = {
                key: value for key, value in artifact.items()
                if key not in {"content", "source_bundle_text_files"}
            }
            compact["content_omitted"] = "overall model-context boundary reached"
            authoritative["prior_artifacts"].append(compact)
            if len(json.dumps(
                authoritative,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )) > self._context_character_limit:
                authoritative["prior_artifacts"].pop()
                break
        context_text = json.dumps(
            authoritative, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
        if len(context_text) > self._context_character_limit:
            raise FatalCommandError(
                "authoritative graph context exceeds the configured model-context boundary; "
                "a compaction node is required"
            )
        selection = self._selection(tenant_id)
        agent = Agent(
            selection.model,
            output_type=GraphAgentNodeOutput,
            instructions=instructions,
            tools=self._tools,
            retries=2,
            name="agent-os-graph-node",
        )
        if self._usage_meter is not None:
            self._usage_meter.reserve_model_turn(
                tenant_id=tenant_id,
                source_id=idempotency_key,
                run_id=run_id,
                category="graph_agent",
                model=selection.name,
                maximum_cost_cents=self._max_turn_budget_cents,
            )
        user_prompt: str | list[str | BinaryContent] = (
            f"Execute this node using the authoritative context below:\n{context_text}"
        )
        if image_attachments:
            user_prompt = [user_prompt, *image_attachments]
        result = agent.run_sync(
            user_prompt,
            run_id=idempotency_key,
            metadata={
                "tenant.id": tenant_id,
                "agent_os.run_id": run_id,
                "agent_os.workflow_id": definition.workflow_id,
                "agent_os.node_id": node.node_id,
            },
            model_settings={"timeout": self._request_timeout_seconds},
            usage_limits=UsageLimits(
                cost_limit=Decimal(self._max_turn_budget_cents) / Decimal(100),
                request_limit=self._request_limit,
                output_tokens_limit=self._output_tokens_limit,
            ),
        )
        output = result.output
        usage = model_usage_record(result.usage)
        if self._usage_meter is not None:
            self._usage_meter.settle_model_turn(
                tenant_id=tenant_id,
                source_id=idempotency_key,
                usage=usage,
            )
        unknown = set(output.satisfied_conditions) - set(available_conditions)
        if unknown:
            raise FatalCommandError(f"graph agent selected unknown conditions: {sorted(unknown)}")
        prior_evidence = {
            evidence_id
            for prior in state.tokens
            if prior.token_id != token.token_id
            for evidence_id in prior.evidence_ids
        }
        proposed_output = output.model_dump(mode="json")
        rejected_evidence_ids = list(dict.fromkeys(
            evidence_id for evidence_id in proposed_output.get("evidence_ids", ())
            if evidence_id not in prior_evidence
        ))
        proposed_output["evidence_ids"] = [
            evidence_id for evidence_id in proposed_output.get("evidence_ids", ())
            if evidence_id in prior_evidence
        ]
        for decision in proposed_output.get("decisions", ()):
            references = decision.get("evidence_ids", ())
            rejected_evidence_ids.extend(
                evidence_id for evidence_id in references
                if evidence_id not in prior_evidence
                and evidence_id not in rejected_evidence_ids
            )
            decision["evidence_ids"] = [
                evidence_id for evidence_id in references if evidence_id in prior_evidence
            ]
        raw = dict(persist_and_validate_artifacts(
            store=self._artifact_store,
            organization_id=tenant_id,
            idempotency_key=idempotency_key,
            output=proposed_output,
            allowed_evidence_ids=prior_evidence,
        ))
        artifact_records = raw.get("artifacts", ())
        artifact_ids = {
            str(record["label"]): str(record["artifact_id"])
            for record in artifact_records
            if isinstance(record, Mapping) and record.get("label") and record.get("artifact_id")
        }
        raw["output"] = {
            **raw["output"],
            "summary": output.summary,
            "artifacts": list(artifact_records),
            "artifact_ids": artifact_ids,
            "rejected_evidence_ids": rejected_evidence_ids,
            "organization_actions": {
                "observations": list(output.observations),
                "risks": list(output.risks),
                "messages": [item.model_dump(mode="json") for item in output.messages],
                "proposed_work": [item.model_dump(mode="json") for item in output.proposed_work],
                "hiring_requests": [item.model_dump(mode="json") for item in output.hiring_requests],
                "decisions": [item.model_dump(mode="json") for item in output.decisions],
                "next_actions": list(output.next_actions),
            },
            "usage": usage,
        }
        if self._artifact_store is not None:
            receipt_content = json.dumps(
                raw,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            self._artifact_store.put(
                organization_id=tenant_id,
                content=receipt_content,
                media_type="application/vnd.agent-os.graph-node-result+json",
                idempotency_key=result_receipt_key,
            )
        return raw
