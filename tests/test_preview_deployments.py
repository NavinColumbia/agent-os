from __future__ import annotations

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


def stores(tmp_path: Path):
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'preview-artifacts.sqlite3'}", create_schema=True,
    )
    deployments = SQLStaticPreviewDeployer(
        f"sqlite:///{tmp_path / 'previews.sqlite3'}",
        artifacts,
        public_base_url="https://preview.example.test",
        capability_secret="test-capability-secret-with-32-bytes",
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

    class NoPublicAuthentication:
        def authenticate(self, authorization, session):
            del authorization, session
            raise AssertionError("the capability route must not invoke account authentication")

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
        api = TestClient(create_app(
            engine=InMemoryWorkflowEngine(),
            identity=NoPublicAuthentication(),
            artifact_store=artifacts,
            preview_deployments=deployments,
        ))

        response = api.get(path)
        assert response.status_code == 200
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
    finally:
        deployments.close()
        artifacts.close()
