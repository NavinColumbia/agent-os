"""Turn bounded model artifact proposals into tenant-scoped durable evidence."""

from __future__ import annotations

import json
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.ports import ArtifactStore
from agent_os.infrastructure.docker_sandbox import (
    SOURCE_BUNDLE_MEDIA_TYPE,
    encode_source_bundle,
)


_MAX_PROPOSED_ARTIFACT_BYTES = 256 * 1024
_TEXT_MEDIA_TYPES = {
    "text/plain",
    "text/plain; charset=utf-8",
    "text/markdown",
    "text/markdown; charset=utf-8",
    "text/html",
    "text/html; charset=utf-8",
}


class ProposedArtifact(BaseModel):
    """A small artifact a model asks the authority layer to persist."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    media_type: Literal[
        "text/plain",
        "text/plain; charset=utf-8",
        "text/markdown",
        "text/markdown; charset=utf-8",
        "text/html",
        "text/html; charset=utf-8",
        "application/json",
        "application/vnd.agent-os.source-bundle+json",
    ]
    content: str | None = Field(default=None, min_length=1, max_length=200_000)
    files: dict[str, str] | None = Field(default=None, min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def content_matches_media_type(self) -> "ProposedArtifact":
        if self.media_type == SOURCE_BUNDLE_MEDIA_TYPE:
            if self.files is None or self.content is not None:
                raise ValueError("source-bundle artifacts require files and no content")
        elif self.content is None or self.files is not None:
            raise ValueError("text/JSON artifacts require content and no files")
        return self


def _artifact_content(proposal: ProposedArtifact) -> bytes:
    if proposal.media_type == SOURCE_BUNDLE_MEDIA_TYPE:
        try:
            content = encode_source_bundle(proposal.files or {})
        except (TypeError, ValueError) as exc:
            raise FatalCommandError(f"proposed source bundle is invalid: {exc}") from exc
    elif proposal.media_type == "application/json":
        try:
            value = json.loads(proposal.content or "")
            content = json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise FatalCommandError("proposed JSON artifact is invalid") from exc
    elif proposal.media_type in _TEXT_MEDIA_TYPES:
        content = (proposal.content or "").encode("utf-8")
    else:  # The Pydantic Literal is defense in depth; fake runtimes still enter here.
        raise FatalCommandError("proposed artifact media type is not allowlisted")
    if len(content) > _MAX_PROPOSED_ARTIFACT_BYTES:
        raise FatalCommandError(
            f"proposed artifact exceeds {_MAX_PROPOSED_ARTIFACT_BYTES} bytes"
        )
    return content


def persist_and_validate_artifacts(
    *,
    store: ArtifactStore | None,
    organization_id: str,
    idempotency_key: str,
    output: Mapping[str, Any],
    allowed_evidence_ids: set[str] | None = None,
) -> Mapping[str, Any]:
    """Persist proposals and reject evidence that has no authoritative source."""

    raw_artifacts = output.get("artifacts", ())
    if not isinstance(raw_artifacts, (list, tuple)) or len(raw_artifacts) > 16:
        raise FatalCommandError("agent artifacts must be a list of at most 16 proposals")
    try:
        proposals = tuple(ProposedArtifact.model_validate(item) for item in raw_artifacts)
    except (TypeError, ValueError) as exc:
        raise FatalCommandError(f"agent proposed an invalid artifact: {exc}") from exc
    if len({item.label for item in proposals}) != len(proposals):
        raise FatalCommandError("agent artifact labels must be unique within one turn")

    raw_evidence = output.get("evidence_ids", ())
    if (
        not isinstance(raw_evidence, (list, tuple))
        or len(raw_evidence) > 100
        or any(not isinstance(item, str) or not item.strip() for item in raw_evidence)
    ):
        raise FatalCommandError("agent evidence_ids must be at most 100 nonempty strings")
    evidence_ids = list(dict.fromkeys(item.strip() for item in raw_evidence))

    decision_evidence: list[str] = []
    raw_decisions = output.get("decisions", ())
    if isinstance(raw_decisions, (list, tuple)):
        for decision in raw_decisions:
            if not isinstance(decision, Mapping):
                continue
            references = decision.get("evidence_ids", ())
            if (
                not isinstance(references, (list, tuple))
                or len(references) > 100
                or any(not isinstance(item, str) or not item.strip() for item in references)
            ):
                raise FatalCommandError(
                    "agent decision evidence_ids must be at most 100 nonempty strings"
                )
            decision_evidence.extend(item.strip() for item in references)

    if store is None:
        if proposals:
            raise FatalCommandError("agent proposed artifacts but no artifact store is configured")
        return dict(output)

    allowed = allowed_evidence_ids or set()
    unknown = [
        artifact_id for artifact_id in dict.fromkeys([*evidence_ids, *decision_evidence])
        if artifact_id not in allowed and store.describe(organization_id, artifact_id) is None
    ]
    if unknown:
        raise FatalCommandError(
            f"agent cited unknown or cross-tenant evidence: {unknown[:10]}"
        )

    prepared = tuple((proposal, _artifact_content(proposal)) for proposal in proposals)
    records = []
    for position, (proposal, content) in enumerate(prepared):
        try:
            artifact_id = store.put(
                organization_id=organization_id,
                content=content,
                media_type=proposal.media_type,
                idempotency_key=(
                    f"{idempotency_key}:agent-artifact:{position}:{proposal.label}"
                ),
            )
        except (TypeError, ValueError) as exc:
            raise FatalCommandError(f"agent artifact could not be persisted: {exc}") from exc
        evidence_ids.append(artifact_id)
        records.append({
            "label": proposal.label,
            "artifact_id": artifact_id,
            "media_type": proposal.media_type,
            "byte_length": len(content),
        })
    normalized = dict(output)
    normalized["evidence_ids"] = list(dict.fromkeys(evidence_ids))
    normalized["artifacts"] = records
    if normalized.get("disposition") == "complete" and not normalized["evidence_ids"]:
        raise FatalCommandError("agent completion requires durable evidence")
    return normalized
