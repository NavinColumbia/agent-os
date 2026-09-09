from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound, PreconditionFailed
import pytest

from agent_os.application.command_worker import FatalCommandError
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import (
    NodeToken,
    TokenStatus,
    WorkflowAction,
    WorkflowActionKind,
    WorkflowRunState,
    WorkflowRunStatus,
)
from agent_os.entrypoints.static_site_router import create_static_site_router
from agent_os.infrastructure.deployment_tool_nodes import DeploymentToolNodeHandlers
from agent_os.infrastructure.docker_sandbox import (
    SOURCE_BUNDLE_MEDIA_TYPE,
    encode_source_bundle,
)
from agent_os.infrastructure.gcs_static_sites import (
    GCSStaticSiteDeployer,
    STATIC_SITE_RECEIPT_MEDIA_TYPE,
)
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore


class FakeBlob:
    def __init__(self, bucket: "FakeBucket", name: str) -> None:
        self.bucket = bucket
        self.name = name
        self.generation = None
        self.content_type = None
        self.size = None

    def _load(self):
        try:
            stored = self.bucket.objects[self.name]
        except KeyError as exc:
            raise NotFound("missing") from exc
        self.generation = stored["generation"]
        self.content_type = stored["content_type"]
        self.size = len(stored["content"])
        return stored

    def reload(self, **kwargs) -> None:
        del kwargs
        self._load()

    def download_as_bytes(self, **kwargs) -> bytes:
        del kwargs
        return bytes(self._load()["content"])

    def upload_from_string(
        self,
        content: bytes,
        *,
        content_type: str,
        if_generation_match: int,
        **kwargs,
    ) -> None:
        del kwargs
        current = self.bucket.objects.get(self.name)
        current_generation = 0 if current is None else current["generation"]
        if if_generation_match != current_generation:
            raise PreconditionFailed("generation mismatch")
        self.bucket.generation += 1
        self.bucket.objects[self.name] = {
            "content": bytes(content),
            "content_type": content_type,
            "generation": self.bucket.generation,
        }
        self._load()


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.generation = 0

    def blob(self, name: str, **kwargs) -> FakeBlob:
        del kwargs
        return FakeBlob(self, name)


class FakeStorageClient:
    def __init__(self) -> None:
        self.value = FakeBucket()

    def bucket(self, name: str) -> FakeBucket:
        assert name == "valid-private-app-bucket"
        return self.value


def test_static_release_is_immutable_idempotent_and_served_from_separate_router(
    tmp_path: Path,
):
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'static-artifacts.sqlite3'}", create_schema=True,
    )
    cloud = FakeStorageClient()
    try:
        source_id = artifacts.put(
            organization_id="tenant-a",
            content=encode_source_bundle({
                "index.html": "<!doctype html><title>Shipped</title><script src=app.js></script>",
                "app.js": "document.body.append(' ready')",
            }),
            media_type=SOURCE_BUNDLE_MEDIA_TYPE,
            idempotency_key="source",
        )
        deployer = GCSStaticSiteDeployer(
            artifacts,
            bucket_name="valid-private-app-bucket",
            public_base_url="https://apps.example.test",
            capability_secret="production-capability-secret-at-least-32-bytes",
            storage_client=cloud,
        )
        first = deployer.deploy_static(
            organization_id="tenant-a",
            artifact_id=source_id,
            app_slug="customer-portal",
            idempotency_key="publish-action",
        )
        replay = deployer.deploy_static(
            organization_id="tenant-a",
            artifact_id=source_id,
            app_slug="customer-portal",
            idempotency_key="publish-action",
        )

        assert replay == {**first, "cached": True}
        assert len(first["route_id"]) == 43
        assert "tenant-a" not in first["public_url"]
        assert artifacts.describe(
            "tenant-a", first["receipt_artifact_id"],
        )["media_type"] == STATIC_SITE_RECEIPT_MEDIA_TYPE
        release_prefix = f"releases/{first['route_id']}/{first['revision']}/"
        assert cloud.value.objects[release_prefix + "index.html"]["content_type"] == "text/html"
        assert json.loads(cloud.value.objects[f"routes/{first['route_id']}.json"]["content"])[
            "revision"
        ] == first["revision"]

        router = TestClient(create_static_site_router(
            bucket_name="valid-private-app-bucket", storage_client=cloud,
        ))
        stable = router.get(f"/p/{first['route_id']}/", follow_redirects=False)
        assert stable.status_code == 307
        assert stable.headers["cache-control"] == "no-store"
        immutable = router.get(stable.headers["location"])
        assert immutable.status_code == 200
        assert immutable.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert "connect-src 'none'" in immutable.headers["content-security-policy"]
        assert immutable.headers["x-content-type-options"] == "nosniff"
        assert "Shipped" in immutable.text
        assert router.get("/p/not-a-route/").status_code == 404
    finally:
        artifacts.close()


def test_static_release_rejects_unsafe_or_incomplete_bundles(tmp_path: Path):
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'invalid-static.sqlite3'}", create_schema=True,
    )
    try:
        deployer = GCSStaticSiteDeployer(
            artifacts,
            bucket_name="valid-private-app-bucket",
            public_base_url="https://apps.example.test",
            capability_secret="production-capability-secret-at-least-32-bytes",
            storage_client=FakeStorageClient(),
        )
        missing_index = artifacts.put(
            organization_id="tenant-a",
            content=encode_source_bundle({"app.js": "ok"}),
            media_type=SOURCE_BUNDLE_MEDIA_TYPE,
            idempotency_key="missing-index",
        )
        with pytest.raises(FatalCommandError, match="requires index.html"):
            deployer.deploy_static(
                organization_id="tenant-a", artifact_id=missing_index,
                app_slug="app", idempotency_key="missing-index-publish",
            )
        unsafe_name = artifacts.put(
            organization_id="tenant-a",
            content=encode_source_bundle({"index.html": "ok", "space name.js": "no"}),
            media_type=SOURCE_BUNDLE_MEDIA_TYPE,
            idempotency_key="unsafe-name",
        )
        with pytest.raises(FatalCommandError, match="URL-safe"):
            deployer.deploy_static(
                organization_id="tenant-a", artifact_id=unsafe_name,
                app_slug="app", idempotency_key="unsafe-name-publish",
            )
        secret_source = artifacts.put(
            organization_id="tenant-a",
            content=encode_source_bundle({
                "index.html": "<script>const apiKey='sk-proj-abcdefghijklmnopqrstuvwxyz012345'</script>",
            }),
            media_type=SOURCE_BUNDLE_MEDIA_TYPE,
            idempotency_key="secret-source",
        )
        with pytest.raises(FatalCommandError, match="credential-like material"):
            deployer.deploy_static(
                organization_id="tenant-a", artifact_id=secret_source,
                app_slug="app", idempotency_key="secret-source-publish",
            )
    finally:
        artifacts.close()


def _static_tool_state(approved: bool) -> tuple[
    WorkflowDefinition, WorkflowRunState, WorkflowAction, WorkflowNode,
]:
    definition = WorkflowDefinition(
        "static", "tenant-a", "Static", 1, "build",
        (
            WorkflowNode("build", NodeKind.AGENT, "Build", "engineer"),
            WorkflowNode("approve", NodeKind.HUMAN, "Approve production"),
            WorkflowNode("publish", NodeKind.TOOL, "Publish", configuration={
                "tool": "deploy.static",
                "source": {"node_id": "build", "output_path": ["artifact_ids", "source"]},
                "approval": {
                    "node_id": "approve", "output_path": ["human_response", "approved"],
                },
                "app_slug": "customer-portal",
                "success_condition": "published",
            }),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
        ),
        (
            WorkflowEdge("build", "approve", "built"),
            WorkflowEdge("approve", "publish", "approved"),
            WorkflowEdge("publish", "done", "published"),
        ),
        "architect",
    )
    tokens = (
        NodeToken(
            "build-token", "build", TokenStatus.SUCCEEDED, 1,
            evidence_ids=("source-artifact",),
            output={"artifact_ids": {"source": "source-artifact"}},
        ),
        NodeToken(
            "approval-token", "approve", TokenStatus.SUCCEEDED, 1,
            evidence_ids=("human-evidence",),
            output={"human_response": {"approved": approved}},
        ),
        NodeToken("publish-token", "publish", TokenStatus.RUNNING, 1),
    )
    state = WorkflowRunState(
        "run", "tenant-a", "static", 1, 5, WorkflowRunStatus.ACTIVE, tokens,
    )
    return definition, state, WorkflowAction(
        "action-static", WorkflowActionKind.EXECUTE_NODE, "publish-token", "publish",
    ), next(item for item in definition.nodes if item.node_id == "publish")


def test_production_static_tool_requires_explicit_durable_human_approval():
    class Preview:
        def deploy(self, **kwargs):
            raise AssertionError(kwargs)

    class Static:
        def deploy_static(self, **kwargs):
            assert kwargs == {
                "organization_id": "tenant-a",
                "artifact_id": "source-artifact",
                "app_slug": "customer-portal",
                "idempotency_key": "action-static",
            }
            return {
                "deployment_id": "static-one",
                "receipt_artifact_id": "receipt-one",
                "public_url": "https://apps.example.test/p/opaque/",
            }

    handler = DeploymentToolNodeHandlers(Preview(), Static()).named_handlers()["deploy.static"]
    definition, state, action, node = _static_tool_state(True)
    result = handler("tenant-a", "run", definition, state, action, node)
    assert result["satisfied_conditions"] == ["published"]
    assert result["evidence_ids"] == ["source-artifact", "receipt-one"]

    definition, state, action, node = _static_tool_state(False)
    with pytest.raises(FatalCommandError, match="explicit human approval"):
        handler("tenant-a", "run", definition, state, action, node)
