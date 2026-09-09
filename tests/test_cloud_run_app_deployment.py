from __future__ import annotations

import io
import tarfile
from pathlib import Path

from google.api_core.exceptions import PreconditionFailed
import pytest
import requests

from agent_os.application.command_worker import FatalCommandError, RetryableCommandError
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import (
    NodeToken,
    TokenStatus,
    WorkflowAction,
    WorkflowActionKind,
    WorkflowRunState,
    WorkflowRunStatus,
)
from agent_os.infrastructure.cloud_run_apps import (
    CloudRunServiceDeployer,
    SERVICE_RELEASE_RECEIPT_MEDIA_TYPE,
)
from agent_os.infrastructure.deployment_tool_nodes import DeploymentToolNodeHandlers
from agent_os.infrastructure.docker_sandbox import SOURCE_BUNDLE_MEDIA_TYPE, encode_source_bundle
from agent_os.infrastructure.mission_workflows import materialize_mission_workflow
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore


class Blob:
    def __init__(self, bucket: "Bucket", name: str) -> None:
        self.bucket = bucket
        self.name = name
        self.generation = None

    def upload_from_string(self, content: bytes, *, if_generation_match: int, **kwargs) -> None:
        del kwargs
        prior = self.bucket.objects.get(self.name)
        generation = 0 if prior is None else prior["generation"]
        if generation != if_generation_match:
            raise PreconditionFailed("generation mismatch")
        self.bucket.generation += 1
        self.bucket.objects[self.name] = {
            "content": bytes(content), "generation": self.bucket.generation,
        }
        self.generation = self.bucket.generation

    def download_as_bytes(self, **kwargs) -> bytes:
        del kwargs
        return bytes(self.bucket.objects[self.name]["content"])

    def reload(self, **kwargs) -> None:
        del kwargs
        self.generation = self.bucket.objects[self.name]["generation"]


class Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.generation = 0

    def blob(self, name: str) -> Blob:
        return Blob(self, name)


class Storage:
    def __init__(self) -> None:
        self.value = Bucket()

    def bucket(self, name: str) -> Bucket:
        assert name == "generated-app-sources"
        return self.value


class Response:
    def __init__(self, value, status_code: int = 200) -> None:
        self.value = value
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.value


class GoogleSession:
    def __init__(self) -> None:
        self.builds: list[dict] = []
        self.build_requests: list[dict] = []
        self.service_requests: list[dict] = []
        self.rollback_requests: list[dict] = []
        self.service = None
        self.fail_promotion_once = False

    def get(self, url: str, *, timeout: float, params=None):
        assert timeout == 7
        if url.endswith("/builds"):
            assert params["filter"].startswith('tags="agentos-')
            return Response({"builds": self.builds})
        if "/builds/" in url:
            build_id = url.rsplit("/", 1)[-1]
            return Response(next(item for item in self.builds if item["id"] == build_id))
        if "/services/" in url:
            assert params is None
            return Response(self.service, status_code=404 if self.service is None else 200)
        raise AssertionError(url)

    def post(self, url: str, *, json, timeout: float):
        assert url.endswith("/builds") and timeout == 7
        self.build_requests.append(json)
        image = json["images"][0]
        build = {
            "id": "build-one",
            "name": "projects/generated-apps/locations/us-central1/builds/build-one",
            "status": "SUCCESS",
            "createTime": "2026-09-08T00:00:00Z",
            "tags": list(json["tags"]),
            "results": {"images": [{"name": image, "digest": "sha256:" + "c" * 64}]},
        }
        self.builds.append(build)
        return Response({"metadata": {"build": build}})

    def patch(self, url: str, *, params, json, timeout: float):
        assert url.endswith(json["name"].rsplit("/", 1)[-1]) and timeout == 7
        if params.get("updateMask") == "traffic":
            self.rollback_requests.append(json)
            self.service["reconciling"] = False
            self.service["traffic"] = json["traffic"]
            self.service["trafficStatuses"] = [{
                "revision": json["traffic"][0]["revision"], "percent": 100,
            }]
            return Response({"name": "rollback-operation"})
        if self.fail_promotion_once:
            self.fail_promotion_once = False
            raise requests.ConnectionError("response lost")
        assert params["allowMissing"] == "true"
        self.service_requests.append(json)
        revision = json["template"]["revision"]
        self.service = {
            **json,
            "reconciling": False,
            "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
            "latestReadyRevision": json["name"].replace("/services/", "/revisions/")
            + f"/{revision}",
            "latestCreatedRevision": revision,
            "uri": "https://opaque-generated-service.run.app",
        }
        return Response({"name": "projects/generated-apps/locations/us-central1/operations/op-one"})


class PublicSession:
    def __init__(self, status_code: int = 200) -> None:
        self.requests = []
        self.status_code = status_code

    def get(self, url: str, *, timeout: float, allow_redirects: bool):
        self.requests.append((url, timeout, allow_redirects))
        return Response({}, status_code=self.status_code)


def make_deployer(tmp_path: Path, *, health_status: int = 200):
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'service-artifacts.sqlite3'}", create_schema=True,
    )
    cloud = Storage()
    google = GoogleSession()
    public = PublicSession(health_status)
    clock = [0.0]

    def advance(seconds: float) -> None:
        clock[0] += seconds

    deployer = CloudRunServiceDeployer(
        artifacts,
        project_id="generated-apps",
        region="us-central1",
        source_bucket="generated-app-sources",
        repository="customer-apps",
        build_service_account_email="builder@generated-apps.iam.gserviceaccount.com",
        runtime_service_account_email="runtime@generated-apps.iam.gserviceaccount.com",
        builder_image="gcr.io/cloud-builders/docker@sha256:" + "b" * 64,
        request_timeout_seconds=7,
        storage_client=cloud,
        session=google,
        public_session=public,
        sleep=advance,
        monotonic=lambda: clock[0],
    )
    return artifacts, cloud, google, public, deployer


def source_bundle(dockerfile: str | None = None, **files: str) -> bytes:
    dockerfile = dockerfile or (
        "FROM python@sha256:" + "a" * 64 + "\n"
        "COPY . /app\nWORKDIR /app\nUSER 10001:10001\n"
        "CMD [\"python\", \"app.py\"]\n"
    )
    return encode_source_bundle({"Dockerfile": dockerfile, "app.py": "print('ready')\n", **files})


def test_service_release_builds_once_promotes_digest_checks_health_and_replays(tmp_path: Path):
    artifacts, cloud, google, public, deployer = make_deployer(tmp_path)
    try:
        source_id = artifacts.put(
            organization_id="tenant-a",
            content=source_bundle(),
            media_type=SOURCE_BUNDLE_MEDIA_TYPE,
            idempotency_key="verified-source",
        )
        first = deployer.deploy_service(
            organization_id="tenant-a",
            artifact_id=source_id,
            app_slug="customer-api",
            health_path="/health",
            idempotency_key="publish-service-action",
        )
        replay = deployer.deploy_service(
            organization_id="tenant-a",
            artifact_id=source_id,
            app_slug="customer-api",
            health_path="/health",
            idempotency_key="publish-service-action",
        )

        assert replay == {**first, "cached": True}
        assert first["kind"] == "cloud_run_service"
        assert first["image"].endswith("@sha256:" + "c" * 64)
        assert "tenant-a" not in first["service_name"]
        assert artifacts.describe(
            "tenant-a", first["receipt_artifact_id"],
        )["media_type"] == SERVICE_RELEASE_RECEIPT_MEDIA_TYPE
        assert len(google.build_requests) == 1
        build = google.build_requests[0]
        assert build["serviceAccount"] == (
            "projects/generated-apps/serviceAccounts/"
            "builder@generated-apps.iam.gserviceaccount.com"
        )
        assert build["steps"][0]["name"].endswith("@sha256:" + "b" * 64)
        assert "availableSecrets" not in build and "secretEnv" not in str(build)
        assert len(google.service_requests) == 1
        service = google.service_requests[0]
        assert service["invokerIamDisabled"] is True
        assert service["template"]["serviceAccount"] == (
            "runtime@generated-apps.iam.gserviceaccount.com"
        )
        container = service["template"]["containers"][0]
        assert "@sha256:" in container["image"]
        assert "env" not in container
        assert public.requests == [(
            "https://opaque-generated-service.run.app/health", 7, False,
        )]

        staged = next(iter(cloud.value.objects.values()))["content"]
        with tarfile.open(fileobj=io.BytesIO(staged), mode="r:gz") as archive:
            assert sorted(archive.getnames()) == ["Dockerfile", "app.py"]
            assert archive.extractfile("app.py").read() == b"print('ready')\n"
    finally:
        artifacts.close()


def test_unhealthy_release_restores_the_prior_ready_revision_and_records_evidence(
    tmp_path: Path,
):
    artifacts, _, google, _, deployer = make_deployer(tmp_path, health_status=503)
    try:
        source_id = artifacts.put(
            organization_id="tenant-a", content=source_bundle(),
            media_type=SOURCE_BUNDLE_MEDIA_TYPE, idempotency_key="updated-source",
        )
        google.service = {
            "name": "prior-service",
            "reconciling": False,
            "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
            "latestReadyRevision": "projects/generated-apps/locations/us-central1/"
            "services/prior/revisions/known-good",
            "latestCreatedRevision": "known-good",
            "trafficStatuses": [{"revision": "known-good", "percent": 100}],
            "uri": "https://opaque-generated-service.run.app",
        }
        with pytest.raises(FatalCommandError, match="traffic rolled back; failure evidence"):
            deployer.deploy_service(
                organization_id="tenant-a", artifact_id=source_id,
                app_slug="customer-api", health_path="/health",
                idempotency_key="unhealthy-release",
            )
        assert google.rollback_requests[0]["traffic"] == [{
            "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
            "revision": "known-good",
            "percent": 100,
        }]
        failure = artifacts.find_by_idempotency_key(
            "tenant-a", "unhealthy-release:service-failed-release",
        )
        assert failure is not None
        assert failure["media_type"].endswith("service-release-failure+json")
    finally:
        artifacts.close()


def test_retry_after_lost_promotion_response_reuses_the_recorded_build(tmp_path: Path):
    artifacts, _, google, _, deployer = make_deployer(tmp_path)
    try:
        source_id = artifacts.put(
            organization_id="tenant-a", content=source_bundle(),
            media_type=SOURCE_BUNDLE_MEDIA_TYPE, idempotency_key="retry-source",
        )
        google.fail_promotion_once = True
        with pytest.raises(RetryableCommandError, match="promotion did not complete"):
            deployer.deploy_service(
                organization_id="tenant-a", artifact_id=source_id,
                app_slug="retry-app", health_path="/health", idempotency_key="retry-action",
            )
        recovered = deployer.deploy_service(
            organization_id="tenant-a", artifact_id=source_id,
            app_slug="retry-app", health_path="/health", idempotency_key="retry-action",
        )
        assert recovered["build_id"] == "build-one"
        assert len(google.build_requests) == 1
    finally:
        artifacts.close()


def test_concurrent_build_record_adopts_the_durable_winner(tmp_path: Path):
    artifacts, _, _, _, deployer = make_deployer(tmp_path)
    try:
        first_artifact, first_build = deployer._record_build(
            "tenant-a", "same-action", "f" * 64, {"id": "build-one"},
        )
        second_artifact, second_build = deployer._record_build(
            "tenant-a", "same-action", "f" * 64, {"id": "build-two"},
        )
        assert second_artifact == first_artifact
        assert first_build == second_build == "build-one"
    finally:
        artifacts.close()


def test_failed_prior_build_allows_a_fresh_workflow_action_to_rebuild(tmp_path: Path):
    artifacts, _, google, _, deployer = make_deployer(tmp_path)
    try:
        content = source_bundle()
        source_id = artifacts.put(
            organization_id="tenant-a", content=content,
            media_type=SOURCE_BUNDLE_MEDIA_TYPE, idempotency_key="retryable-source",
        )
        canonical, _ = deployer._source("tenant-a", source_id)
        fingerprint = deployer._fingerprint("tenant-a", "retry-app", canonical)
        google.builds.append({
            "id": "failed-build",
            "status": "FAILURE",
            "createTime": "2026-09-08T00:00:00Z",
            "tags": [f"agentos-{fingerprint}"],
        })

        release = deployer.deploy_service(
            organization_id="tenant-a", artifact_id=source_id,
            app_slug="retry-app", health_path="/health", idempotency_key="fresh-action",
        )

        assert release["build_id"] == "build-one"
        assert len(google.build_requests) == 1
    finally:
        artifacts.close()


@pytest.mark.parametrize(
    ("bundle", "error"),
    [
        (source_bundle("FROM python:3.12-slim\n"), "pinned by sha256"),
        (
            source_bundle("FROM python@sha256:" + "a" * 64 + "\n"),
            "numeric non-root USER",
        ),
        (source_bundle(**{".env": "PASSWORD=do-not-ship-this-secret"}), "credential-bearing path"),
        (source_bundle(config="OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz"), "credential-like material"),
    ],
)
def test_service_release_rejects_mutable_dependencies_and_credentials(
    tmp_path: Path, bundle: bytes, error: str,
):
    artifacts, _, _, _, deployer = make_deployer(tmp_path)
    try:
        source_id = artifacts.put(
            organization_id="tenant-a", content=bundle,
            media_type=SOURCE_BUNDLE_MEDIA_TYPE, idempotency_key="unsafe-source",
        )
        with pytest.raises(FatalCommandError, match=error):
            deployer.deploy_service(
                organization_id="tenant-a", artifact_id=source_id,
                app_slug="unsafe", health_path="/health", idempotency_key="unsafe-publish",
            )
    finally:
        artifacts.close()


def _service_tool_state(approved: bool):
    definition = WorkflowDefinition(
        "service", "tenant-a", "Service", 1, "build",
        (
            WorkflowNode("build", NodeKind.AGENT, "Build", "engineer"),
            WorkflowNode("approve", NodeKind.HUMAN, "Approve"),
            WorkflowNode("publish", NodeKind.TOOL, "Publish", configuration={
                "tool": "deploy.service",
                "source": {"node_id": "build", "output_path": ["artifact_ids", "source"]},
                "approval": {
                    "node_id": "approve", "output_path": ["human_response", "approved"],
                },
                "app_slug": "customer-api", "health_path": "/health",
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
    state = WorkflowRunState(
        "run", "tenant-a", "service", 1, 5, WorkflowRunStatus.ACTIVE,
        (
            NodeToken(
                "build-token", "build", TokenStatus.SUCCEEDED, 1,
                evidence_ids=("source-artifact",),
                output={"artifact_ids": {"source": "source-artifact"}},
            ),
            NodeToken(
                "approval-token", "approve", TokenStatus.SUCCEEDED, 1,
                evidence_ids=("approval",),
                output={"human_response": {"approved": approved}},
            ),
            NodeToken("publish-token", "publish", TokenStatus.RUNNING, 1),
        ),
    )
    return definition, state, WorkflowAction(
        "service-action", WorkflowActionKind.EXECUTE_NODE, "publish-token", "publish",
    ), next(node for node in definition.nodes if node.node_id == "publish")


def test_service_tool_and_planner_require_durable_human_approval():
    class Preview:
        def deploy(self, **kwargs):
            raise AssertionError(kwargs)

    class Service:
        def deploy_service(self, **kwargs):
            assert kwargs["health_path"] == "/health"
            return {
                "deployment_id": "service-one", "receipt_artifact_id": "receipt-one",
                "public_url": "https://service.example.test",
            }

    handler = DeploymentToolNodeHandlers(
        Preview(), service_deployer=Service(),
    ).named_handlers()["deploy.service"]
    definition, state, action, node = _service_tool_state(True)
    result = handler("tenant-a", "run", definition, state, action, node)
    assert result["evidence_ids"] == ["source-artifact", "receipt-one"]
    definition, state, action, node = _service_tool_state(False)
    with pytest.raises(FatalCommandError, match="explicit human approval"):
        handler("tenant-a", "run", definition, state, action, node)

    plan = {
        "name": "Build and publish service", "entry_node_id": "build",
        "nodes": [
            {"node_id": "build", "kind": "agent", "purpose": "Build", "owner_role": "engineer"},
            {"node_id": "approve", "kind": "human", "purpose": "Approve", "configuration": {
                "recipient_ids": ["human:ceo"], "response_condition": "approved",
            }},
            {"node_id": "publish", "kind": "tool", "purpose": "Publish", "configuration": {
                "tool": "deploy.service",
                "source": {"node_id": "build", "output_path": ["artifact_ids", "source"]},
                "approval": {
                    "node_id": "approve", "output_path": ["human_response", "approved"],
                },
                "app_slug": "customer-api", "health_path": "/health",
                "success_condition": "published",
            }},
            {"node_id": "done", "kind": "terminal", "purpose": "Done"},
        ],
        "edges": [
            {"source": "build", "target": "approve", "condition": "built"},
            {"source": "approve", "target": "publish", "condition": "approved"},
            {"source": "publish", "target": "done", "condition": "published"},
        ],
    }
    materialized = materialize_mission_workflow(
        plan, tenant_id="tenant-a", planning_run_id="plan", artifact_id="artifact",
    )
    publish = next(item for item in materialized.nodes if item.node_id == "publish")
    assert publish.configuration["health_path"] == "/health"
