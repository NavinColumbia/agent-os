from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_os.application.command_worker import FatalCommandError
from agent_os.infrastructure.docker_sandbox import SOURCE_BUNDLE_MEDIA_TYPE
from agent_os.infrastructure.proposed_artifacts import persist_and_validate_artifacts
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore


def store(tmp_path: Path) -> SQLArtifactStore:
    return SQLArtifactStore(f"sqlite:///{tmp_path / 'artifacts.sqlite3'}", create_schema=True)


def test_agent_artifact_proposals_become_real_content_addressed_evidence(tmp_path: Path):
    artifacts = store(tmp_path)
    output = {
        "disposition": "complete",
        "evidence_ids": [],
        "artifacts": [
            {
                "label": "research-report",
                "media_type": "text/markdown",
                "content": "# Evidence\n\nGrounded result.",
            },
            {
                "label": "application-source",
                "media_type": SOURCE_BUNDLE_MEDIA_TYPE,
                "files": {"index.html": "<h1>Working app</h1>"},
            },
        ],
    }
    try:
        first = persist_and_validate_artifacts(
            store=artifacts,
            organization_id="tenant-a",
            idempotency_key="command-a",
            output=output,
        )
        replay = persist_and_validate_artifacts(
            store=artifacts,
            organization_id="tenant-a",
            idempotency_key="command-a",
            output=output,
        )

        assert replay == first
        assert first["evidence_ids"] == [item["artifact_id"] for item in first["artifacts"]]
        report_id, source_id = first["evidence_ids"]
        assert artifacts.get("tenant-a", report_id) == b"# Evidence\n\nGrounded result."
        source = json.loads(artifacts.get("tenant-a", source_id))
        assert source["files"]["index.html"]["content"] == "<h1>Working app</h1>"
    finally:
        artifacts.close()


def test_agent_cannot_cite_unknown_or_cross_tenant_evidence(tmp_path: Path):
    artifacts = store(tmp_path)
    try:
        other_id = artifacts.put(
            organization_id="tenant-b",
            content=b"private",
            media_type="text/plain",
            idempotency_key="private-evidence",
        )
        with pytest.raises(FatalCommandError, match="unknown or cross-tenant"):
            persist_and_validate_artifacts(
                store=artifacts,
                organization_id="tenant-a",
                idempotency_key="command-a",
                output={
                    "disposition": "complete",
                    "evidence_ids": [other_id, "invented-evidence"],
                    "artifacts": [],
                },
            )
        with pytest.raises(FatalCommandError, match="unknown or cross-tenant"):
            persist_and_validate_artifacts(
                store=artifacts,
                organization_id="tenant-a",
                idempotency_key="command-decision",
                output={
                    "disposition": "continue",
                    "evidence_ids": [],
                    "artifacts": [],
                    "decisions": [{"evidence_ids": ["invented-decision-evidence"]}],
                },
            )
    finally:
        artifacts.close()


def test_prior_durable_evidence_may_be_cited_but_bad_proposals_fail_closed(tmp_path: Path):
    artifacts = store(tmp_path)
    try:
        accepted = persist_and_validate_artifacts(
            store=artifacts,
            organization_id="tenant-a",
            idempotency_key="command-a",
            output={
                "disposition": "complete",
                "evidence_ids": ["human-response-record"],
                "artifacts": [],
            },
            allowed_evidence_ids={"human-response-record"},
        )
        assert accepted["evidence_ids"] == ["human-response-record"]

        with pytest.raises(FatalCommandError, match="JSON artifact is invalid"):
            persist_and_validate_artifacts(
                store=artifacts,
                organization_id="tenant-a",
                idempotency_key="command-b",
                output={
                    "disposition": "complete",
                    "evidence_ids": [],
                    "artifacts": [{
                        "label": "bad-json",
                        "media_type": "application/json",
                        "content": "not json",
                    }],
                },
            )
    finally:
        artifacts.close()


def test_structured_json_value_is_canonicalized_without_string_escaping(tmp_path: Path):
    artifacts = store(tmp_path)
    try:
        result = persist_and_validate_artifacts(
            store=artifacts,
            organization_id="tenant-a",
            idempotency_key="structured-json",
            output={
                "disposition": "complete",
                "evidence_ids": [],
                "artifacts": [{
                    "label": "mission-workflow",
                    "media_type": "application/json",
                    "json_value": {"nodes": [{"node_id": "build"}], "name": "Mission"},
                }],
            },
        )

        artifact_id = result["evidence_ids"][0]
        assert artifacts.get("tenant-a", artifact_id) == (
            b'{"name":"Mission","nodes":[{"node_id":"build"}]}'
        )
    finally:
        artifacts.close()
