from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from agent_os.api.app import create_app
from agent_os.application.command_worker import FatalCommandError
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import begin_node, complete_node, start_workflow
from agent_os.infrastructure.deployment_tool_nodes import DeploymentToolNodeHandlers
from agent_os.infrastructure.memory import InMemoryWorkflowEngine
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore
from agent_os.infrastructure.sql_preview_deployments import SQLStaticPreviewDeployer


def stores(tmp_path: Path, *, clock=None, ttl_seconds: int = 7 * 24 * 60 * 60):
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'preview-artifacts.sqlite3'}", create_schema=True,
    )
    deployments = SQLStaticPreviewDeployer(
        f"sqlite:///{tmp_path / 'previews.sqlite3'}",
        artifacts,
        public_base_url="https://preview.example.test",
        capability_secret="test-capability-secret-with-32-bytes",
        clock=clock,
        ttl_seconds=ttl_seconds,
        create_schema=True,
    )
    return artifacts, deployments


def test_static_preview_is_idempotent_tenant_fenced_and_content_backed(tmp_path: Path):
    artifacts, deployments = stores(tmp_path)
    try:
        artifact_id = artifacts.put(
            organization_id="tenant-a",
            content=b"<!doctype html><h1>Generated app</h1>",
            media_type="text/html; charset=utf-8",
            idempotency_key="html-source",
        )
        first = deployments.deploy(
            organization_id="tenant-a",
            artifact_id=artifact_id,
            idempotency_key="publish-preview",
        )
        replay = deployments.deploy(
            organization_id="tenant-a",
            artifact_id=artifact_id,
            idempotency_key="publish-preview",
        )

        assert replay == first
        assert first["public_url"].startswith("https://preview.example.test/v2/public/previews/")
        parts = urlparse(first["public_url"]).path.split("/")
        resolved = deployments.resolve_public(parts[-2], parts[-1])
        assert resolved == first
        assert artifacts.describe(
            "tenant-a", first["receipt_artifact_id"],
        )["media_type"] == "application/json"
        assert deployments.resolve_public("dGVuYW50LWI", parts[-1]) is None

        other_id = artifacts.put(
            organization_id="tenant-a",
            content=b"<h1>Different</h1>",
            media_type="text/html",
            idempotency_key="other-html",
        )
        with pytest.raises(FatalCommandError, match="different artifact"):
            deployments.deploy(
                organization_id="tenant-a",
                artifact_id=other_id,
                idempotency_key="publish-preview",
            )

        artifacts.put(
            organization_id="tenant-a",
            content=b'{"conflicting":"receipt"}',
            media_type="application/json",
            idempotency_key="preview-receipt:receipt-race",
        )
        with pytest.raises(FatalCommandError, match="different artifact"):
            deployments.deploy(
                organization_id="tenant-a",
                artifact_id=artifact_id,
                idempotency_key="receipt-race",
            )
    finally:
        deployments.close()
        artifacts.close()


def test_static_preview_rejects_non_html_and_weak_capability_secret(tmp_path: Path):
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'invalid-artifacts.sqlite3'}", create_schema=True,
    )
    try:
        with pytest.raises(ValueError, match="at least 32 bytes"):
            SQLStaticPreviewDeployer(
                f"sqlite:///{tmp_path / 'invalid-preview.sqlite3'}",
                artifacts,
                public_base_url="https://preview.example.test",
                capability_secret="weak",
            )
        with pytest.raises(ValueError, match="between 60 seconds and 30 days"):
            SQLStaticPreviewDeployer(
                f"sqlite:///{tmp_path / 'invalid-ttl.sqlite3'}",
                artifacts,
                public_base_url="https://preview.example.test",
                capability_secret="test-capability-secret-with-32-bytes",
                ttl_seconds=31 * 24 * 60 * 60,
            )
        deployments = SQLStaticPreviewDeployer(
            f"sqlite:///{tmp_path / 'valid-preview.sqlite3'}",
            artifacts,
            public_base_url="https://preview.example.test",
            capability_secret="test-capability-secret-with-32-bytes",
            create_schema=True,
        )
        artifact_id = artifacts.put(
            organization_id="tenant-a",
            content=b"not html",
            media_type="text/plain",
            idempotency_key="plain-source",
        )
        with pytest.raises(FatalCommandError, match="text/html"):
            deployments.deploy(
                organization_id="tenant-a",
                artifact_id=artifact_id,
                idempotency_key="plain-preview",
            )
        deployments.close()
    finally:
        artifacts.close()


def test_preview_tool_selects_upstream_artifact_and_emits_deployment_evidence():
    definition = WorkflowDefinition(
        "preview", "tenant-a", "Preview", 1, "build",
        (
            WorkflowNode("build", NodeKind.AGENT, "Build HTML", "engineer"),
            WorkflowNode("deploy", NodeKind.TOOL, "Publish preview", configuration={
                "tool": "deploy.preview",
                "source": {
                    "node_id": "build",
                    "output_path": ["artifact_ids", "html-preview"],
                },
                "success_condition": "published",
            }),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
        ),
        (
            WorkflowEdge("build", "deploy", "built"),
            WorkflowEdge("deploy", "done", "published"),
        ),
        "architect",
    )
    started = start_workflow(definition, run_id="run-preview")
    build_action = started.actions[0]
    running = begin_node(started.state, build_action.token_id, expected_version=0).state
    built = complete_node(
        definition,
        running,
        build_action.token_id,
        expected_version=1,
        satisfied_conditions=frozenset({"built"}),
        evidence_ids=("artifact-html",),
        output={"artifact_ids": {"html-preview": "artifact-html"}},
    )
    deploy_action = built.actions[0]
    deploy_running = begin_node(
        built.state, deploy_action.token_id, expected_version=2,
    ).state
    deploy_node = next(node for node in definition.nodes if node.node_id == "deploy")

    class StubDeployer:
        def deploy(self, **kwargs):
            assert kwargs["artifact_id"] == "artifact-html"
            return {
                "deployment_id": "deployment-one",
                "receipt_artifact_id": "artifact-deployment-receipt",
                "public_url": "https://preview.example.test/app",
            }

    result = DeploymentToolNodeHandlers(StubDeployer()).execute(
        "tenant-a", "run-preview", definition, deploy_running, deploy_action, deploy_node,
    )

    assert result["satisfied_conditions"] == ["published"]
    assert result["evidence_ids"] == [
        "artifact-html", "artifact-deployment-receipt",
    ]
    assert result["output"]["public_url"] == "https://preview.example.test/app"


def test_public_preview_serves_only_the_opaque_capability_with_a_browser_sandbox(
    tmp_path: Path,
):
    artifacts, deployments = stores(tmp_path)

    class PreviewIdentity:
        calls = 0

        def authenticate(self, authorization, session):
            del session
            self.calls += 1
            if authorization == "Bearer owner-a":
                return {"sub": "owner-a", "org": "tenant-a", "roles": ["owner"]}
            if authorization == "Bearer owner-b":
                return {"sub": "owner-b", "org": "tenant-b", "roles": ["owner"]}
            if authorization == "Bearer viewer-a":
                return {"sub": "viewer-a", "org": "tenant-a", "roles": ["viewer"]}
            raise ValueError("authentication required")

    try:
        html = b"<!doctype html><title>Proof</title><h1>Prompt-built app</h1>"
        artifact_id = artifacts.put(
            organization_id="tenant-a",
            content=html,
            media_type="text/html",
            idempotency_key="public-html",
        )
        deployed = deployments.deploy(
            organization_id="tenant-a",
            artifact_id=artifact_id,
            idempotency_key="public-preview",
        )
        path = urlparse(str(deployed["public_url"])).path
        identity = PreviewIdentity()
        api = TestClient(create_app(
            engine=InMemoryWorkflowEngine(),
            identity=identity,
            artifact_store=artifacts,
            preview_deployments=deployments,
        ))

        response = api.get(path)
        assert response.status_code == 200
        assert identity.calls == 0
        assert response.content == html
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"
        policy = response.headers["content-security-policy"]
        assert "sandbox allow-scripts" in policy
        assert "connect-src 'none'" in policy
        assert "form-action 'none'" in policy

        bad_capability = path[:-1] + ("A" if path[-1] != "A" else "B")
        assert api.get(bad_capability).status_code == 404
        assert api.get(path.replace("dGVuYW50LWE", "dGVuYW50LWI")).status_code == 404

        owner_headers = {"Authorization": "Bearer owner-a"}
        inventory = api.get("/v2/deployments/previews", headers=owner_headers)
        assert inventory.status_code == 200
        assert inventory.json()["items"][0]["status"] == "active"
        assert api.get(
            "/v2/deployments/previews", headers={"Authorization": "Bearer owner-b"},
        ).json()["items"] == []
        assert api.get(
            "/v2/deployments/previews", headers={"Authorization": "Bearer viewer-a"},
        ).status_code == 403
        assert api.delete(
            f"/v2/deployments/previews/{deployed['deployment_id']}",
            headers={"Authorization": "Bearer owner-b", "Idempotency-Key": "revoke-other"},
        ).status_code == 404

        revoke_headers = {
            "Authorization": "Bearer owner-a", "Idempotency-Key": "revoke-preview-one",
        }
        revoked = api.delete(
            f"/v2/deployments/previews/{deployed['deployment_id']}", headers=revoke_headers,
        )
        replay = api.delete(
            f"/v2/deployments/previews/{deployed['deployment_id']}", headers=revoke_headers,
        )
        assert revoked.status_code == 200
        assert replay.json() == revoked.json()
        assert revoked.json()["status"] == "revoked"
        assert api.get(path).status_code == 404
    finally:
        deployments.close()
        artifacts.close()


def test_preview_capability_expires_without_deleting_its_audit_record(tmp_path: Path):
    now = [datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)]
    artifacts, deployments = stores(
        tmp_path, clock=lambda: now[0], ttl_seconds=60,
    )
    try:
        artifact_id = artifacts.put(
            organization_id="tenant-a",
            content=b"<h1>Short lived</h1>",
            media_type="text/html",
            idempotency_key="expiring-html",
        )
        deployed = deployments.deploy(
            organization_id="tenant-a",
            artifact_id=artifact_id,
            idempotency_key="expiring-preview",
        )
        path = urlparse(str(deployed["public_url"])).path.split("/")
        assert deployments.resolve_public(path[-2], path[-1]) is not None

        now[0] += timedelta(seconds=61)

        assert deployments.resolve_public(path[-2], path[-1]) is None
        inventory = deployments.list_previews("tenant-a")
        assert len(inventory) == 1
        assert inventory[0]["status"] == "expired"
        assert inventory[0]["active"] is True
    finally:
        deployments.close()
        artifacts.close()
