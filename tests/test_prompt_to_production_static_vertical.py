from __future__ import annotations

import json
from pathlib import Path
import time
from urllib.parse import urlparse

from dbos import DBOS
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound, PreconditionFailed
from pydantic_ai.models.test import TestModel

from agent_os.api.app import create_app
from agent_os.api.auth import HMACTokenIdentity
from agent_os.application.command_worker import CommandRunStatus, DurableCommandWorker
from agent_os.application.graph_action_worker import DurableGraphActionWorker
from agent_os.domain.lifecycle import CommandKind, LifecycleStatus
from agent_os.domain.workflow_runtime import WorkflowRunStatus
from agent_os.entrypoints.static_site_router import create_static_site_router
from agent_os.infrastructure.command_router import LifecycleCommandRouter
from agent_os.infrastructure.dbos_lifecycle import DBOSLifecycleEngine
from agent_os.infrastructure.deployment_tool_nodes import DeploymentToolNodeHandlers
from agent_os.infrastructure.docker_sandbox import (
    SOURCE_BUNDLE_MEDIA_TYPE,
)
from agent_os.infrastructure.graph_action_executor import DurableGraphActionExecutor
from agent_os.infrastructure.gcs_static_sites import GCSStaticSiteDeployer
from agent_os.infrastructure.mission_workflows import (
    MissionBootstrapHandler,
    MissionGraphEffectHandlers,
    WorkflowLaunchToolNodeHandlers,
    mission_planning_run_id,
)
from agent_os.infrastructure.notification_effects import NotificationEffectHandlers
from agent_os.infrastructure.pydantic_graph_nodes import PydanticGraphNodeRuntime
from agent_os.infrastructure.sandbox_tool_nodes import SandboxToolNodeHandlers
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore
from agent_os.infrastructure.sql_notifications import SQLNotificationStore
from agent_os.infrastructure.sql_preview_deployments import SQLStaticPreviewDeployer
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine
from agent_os.infrastructure.tool_node_router import GraphToolNodeRouter


class Blob:
    def __init__(self, bucket: "Bucket", name: str) -> None:
        self.bucket = bucket
        self.name = name
        self.generation = None

    def reload(self, **kwargs) -> None:
        del kwargs
        if self.name not in self.bucket.objects:
            raise NotFound("missing")
        self.generation = self.bucket.objects[self.name]["generation"]

    def download_as_bytes(self, **kwargs) -> bytes:
        del kwargs
        try:
            stored = self.bucket.objects[self.name]
        except KeyError as exc:
            raise NotFound("missing") from exc
        self.generation = stored["generation"]
        return bytes(stored["content"])

    def upload_from_string(
        self, content: bytes, *, content_type: str, if_generation_match: int, **kwargs,
    ) -> None:
        del kwargs
        prior = self.bucket.objects.get(self.name)
        generation = 0 if prior is None else prior["generation"]
        if generation != if_generation_match:
            raise PreconditionFailed("generation mismatch")
        self.bucket.generation += 1
        self.bucket.objects[self.name] = {
            "content": bytes(content),
            "content_type": content_type,
            "generation": self.bucket.generation,
        }
        self.generation = self.bucket.generation


class Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.generation = 0

    def blob(self, name: str, **kwargs) -> Blob:
        del kwargs
        return Blob(self, name)


class Storage:
    def __init__(self) -> None:
        self.bucket_value = Bucket()

    def bucket(self, name: str) -> Bucket:
        assert name == "production-static-proof"
        return self.bucket_value


def drain(worker: DurableGraphActionWorker, *, maximum: int = 24) -> None:
    for _ in range(maximum):
        report = worker.run_one("tenant-a")
        if report.status is CommandRunStatus.IDLE:
            return
        assert report.status is CommandRunStatus.SUCCEEDED
    raise AssertionError("graph worker did not drain within its deterministic action bound")


def test_one_ceo_prompt_reaches_human_approved_fetchable_production_static_app(
    tmp_path: Path,
):
    lifecycle = DBOSLifecycleEngine(
        system_database_url=f"sqlite:///{tmp_path / 'system.sqlite3'}",
        application_database_url=f"sqlite:///{tmp_path / 'application.sqlite3'}",
        application_version="prompt-production-static-proof-v1",
        create_schema=True,
    )
    graph = SQLGraphWorkflowEngine(
        f"sqlite:///{tmp_path / 'graph.sqlite3'}", create_schema=True,
    )
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'artifacts.sqlite3'}", create_schema=True,
    )
    notifications = SQLNotificationStore(
        f"sqlite:///{tmp_path / 'notifications.sqlite3'}", create_schema=True,
    )
    previews = SQLStaticPreviewDeployer(
        f"sqlite:///{tmp_path / 'previews.sqlite3'}", artifacts,
        public_base_url="https://control.example.test",
        capability_secret="prompt-production-static-secret-32-bytes",
        create_schema=True,
    )
    cloud = Storage()
    static_deployer = GCSStaticSiteDeployer(
        artifacts,
        bucket_name="production-static-proof",
        public_base_url="https://apps.example.test",
        capability_secret="prompt-production-static-secret-32-bytes",
        storage_client=cloud,
    )
    identity = HMACTokenIdentity("prompt-production-static-secret-32-bytes")
    owner_token = identity.issue(
        subject_id="human:ceo", organization_id="tenant-a", roles=("owner",),
    )
    api = TestClient(create_app(
        engine=lifecycle,
        identity=identity,
        graph_engine=graph,
        notification_store=notifications,
        artifact_store=artifacts,
        preview_deployments=previews,
    ))
    router = TestClient(create_static_site_router(
        bucket_name="production-static-proof", storage_client=cloud,
    ))
    try:
        accepted = api.post(
            "/v2/runs",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": "prompt-production-static-proof",
            },
            json={"prompt": "Build, verify, and publish a tiny production status dashboard."},
        )
        assert accepted.status_code == 202
        lifecycle.get_result(accepted.json()["workflow_id"])
        lifecycle_run_id = accepted.json()["run_id"]

        class NoLegacyCommands:
            @staticmethod
            def supports(kind):
                return False

            def execute(self, envelope):
                raise AssertionError((envelope,))

        lifecycle_worker = DurableCommandWorker(
            outbox=lifecycle,
            executor=LifecycleCommandRouter(
                agent_executor=NoLegacyCommands(),
                handlers={CommandKind.START_MISSION: MissionBootstrapHandler(graph).execute},
            ),
            worker_id="production-static-lifecycle",
            lease_seconds=3,
            workflow_engine=lifecycle,
            workflow_result_waiter=lifecycle.get_result,
        )
        assert lifecycle_worker.run_one("tenant-a").status is CommandRunStatus.SUCCEEDED

        proposed_plan = {
            "name": "Build, verify, approve, and publish production dashboard",
            "entry_node_id": "build",
            "nodes": [
                {
                    "node_id": "build", "kind": "agent",
                    "purpose": "Build the complete static source bundle.",
                    "owner_role": "frontend-engineer", "configuration": {"max_iterations": 2},
                },
                {
                    "node_id": "verify", "kind": "tool",
                    "purpose": "Verify the generated bundle in the isolated sandbox.",
                    "configuration": {
                        "tool": "sandbox.run",
                        "source": {
                            "node_id": "build",
                            "output_path": ["artifact_ids", "application-source"],
                        },
                        "command": ["python", "-m", "pytest", "-q"],
                        "success_condition": "verified", "failure_condition": "repair",
                        "max_iterations": 1,
                    },
                },
                {
                    "node_id": "approve", "kind": "human",
                    "purpose": "Approve the verified dashboard for production publication.",
                    "configuration": {
                        "recipient_ids": ["human:ceo"],
                        "response_condition": "approved",
                        "rejection_condition": "rejected",
                        "max_iterations": 1,
                    },
                },
                {
                    "node_id": "publish", "kind": "tool",
                    "purpose": "Publish the approved immutable production application.",
                    "configuration": {
                        "tool": "deploy.static",
                        "source": {"node_id": "verify", "output_path": ["output_artifact_id"]},
                        "approval": {
                            "node_id": "approve",
                            "output_path": ["human_response", "approved"],
                        },
                        "app_slug": "status-dashboard",
                        "success_condition": "published", "max_iterations": 1,
                    },
                },
                {
                    "node_id": "repair", "kind": "terminal",
                    "purpose": "Stop publication and retain verification evidence.",
                    "configuration": {"max_iterations": 1},
                },
                {
                    "node_id": "declined", "kind": "terminal",
                    "purpose": "Honor the CEO decision not to publish.",
                    "configuration": {"max_iterations": 1},
                },
                {
                    "node_id": "done", "kind": "terminal",
                    "purpose": "Accept verified production publication evidence.",
                    "configuration": {"max_iterations": 1},
                },
            ],
            "edges": [
                {"source": "build", "target": "verify", "condition": "built"},
                {"source": "verify", "target": "approve", "condition": "verified"},
                {"source": "verify", "target": "repair", "condition": "repair"},
                {"source": "approve", "target": "publish", "condition": "approved"},
                {"source": "approve", "target": "declined", "condition": "rejected"},
                {"source": "publish", "target": "done", "condition": "published"},
            ],
        }
        planner_output = {
            "summary": "Designed a bounded build, verify, human approval, and publish team.",
            "disposition": "complete",
            "satisfied_conditions": ["planned"],
            "artifacts": [{
                "label": "mission-workflow", "media_type": "application/json",
                "json_value": proposed_plan,
            }],
        }
        launch_tools = WorkflowLaunchToolNodeHandlers(graph, artifacts)
        planner_worker = DurableGraphActionWorker(
            outbox=graph,
            executor=DurableGraphActionExecutor(
                engine=graph,
                node_runtime=PydanticGraphNodeRuntime(
                    TestModel(custom_output_args=planner_output),
                    handlers=GraphToolNodeRouter(launch_tools.named_handlers()).handlers(),
                    artifact_store=artifacts,
                    max_turn_budget_cents=1,
                ),
            ),
            worker_id="production-static-planner", lease_seconds=3,
        )
        assert planner_worker.run_one("tenant-a").status is CommandRunStatus.SUCCEEDED
        assert planner_worker.run_one("tenant-a").status is CommandRunStatus.SUCCEEDED
        planning = graph.get_graph_run("tenant-a", mission_planning_run_id(lifecycle_run_id))
        launch = next(token for token in planning.tokens if token.node_id == "launch")
        child_run_id = str(launch.output["child_run_id"])

        source_files = {
            "index.html": (
                "<!doctype html><title>Production proof</title>"
                "<main><h1>Agent OS shipped this</h1><p id=status>healthy</p></main>"
            ),
            "test_app.py": "def test_release():\n    assert True\n",
        }
        builder_output = {
            "summary": "Built a complete static application and release check.",
            "disposition": "complete",
            "satisfied_conditions": ["built"],
            "artifacts": [{
                "label": "application-source",
                "media_type": SOURCE_BUNDLE_MEDIA_TYPE,
                "files": source_files,
            }],
        }

        class VerifiedSandbox:
            def run(self, *, organization_id, artifact_id, command, idempotency_key):
                assert organization_id == "tenant-a"
                assert command == ("python", "-m", "pytest", "-q")
                assert artifacts.describe(organization_id, artifact_id)[
                    "media_type"
                ] == SOURCE_BUNDLE_MEDIA_TYPE
                result_id = artifacts.put(
                    organization_id=organization_id,
                    content=json.dumps({"exit_code": 0, "tests": "passed"}).encode(),
                    media_type="application/json",
                    idempotency_key=f"{idempotency_key}:verified-result",
                )
                return {
                    "output_artifact_id": artifact_id,
                    "result_artifact_id": result_id,
                    "exit_code": 0,
                    "timed_out": False,
                }

        notification_effects = NotificationEffectHandlers(notifications)
        named_handlers = dict(launch_tools.named_handlers())
        named_handlers.update(
            DeploymentToolNodeHandlers(previews, static_deployer).named_handlers()
        )
        named_handlers.update(SandboxToolNodeHandlers(VerifiedSandbox()).named_handlers())
        mission_worker = DurableGraphActionWorker(
            outbox=graph,
            executor=DurableGraphActionExecutor(
                engine=graph,
                node_runtime=PydanticGraphNodeRuntime(
                    TestModel(custom_output_args=builder_output),
                    handlers=GraphToolNodeRouter(named_handlers).handlers(),
                    artifact_store=artifacts,
                    max_turn_budget_cents=1,
                ),
                effect_handlers=MissionGraphEffectHandlers(
                    lifecycle_engine=lifecycle,
                    graph_engine=graph,
                    notification_handlers=notification_effects.graph_handlers(),
                ).graph_handlers(),
            ),
            worker_id="production-static-mission", lease_seconds=3,
        )
        drain(mission_worker)
        child = graph.get_graph_run("tenant-a", child_run_id)
        assert child is not None and child.status is WorkflowRunStatus.WAITING

        inbox = api.get(
            "/v2/notifications", headers={"Authorization": f"Bearer {owner_token}"},
        )
        request = next(
            item for item in inbox.json()["items"]
            if item["category"] == "human_action_required" and item["actionable"]
        )
        approved = api.post(
            f"/v2/graph-runs/{child_run_id}/events",
            headers={"Authorization": f"Bearer {owner_token}"},
            json={
                "event_id": "ceo-production-approval",
                "kind": "wait_resumed",
                "expected_version": child.version,
                "payload": {
                    "correlation_id": request["correlation_id"],
                    "response": {"approved": True, "answer": "Publish this verified revision"},
                },
            },
        )
        assert approved.status_code == 202
        drain(mission_worker)

        completed = graph.get_graph_run("tenant-a", child_run_id)
        assert completed is not None and completed.status is WorkflowRunStatus.SUCCEEDED
        publish = next(token for token in completed.tokens if token.node_id == "publish")
        public_url = str(publish.output["public_url"])
        path = urlparse(public_url).path
        stable = router.get(path, follow_redirects=False)
        rendered = router.get(stable.headers["location"])
        assert stable.status_code == 307
        assert rendered.status_code == 200
        assert "Agent OS shipped this" in rendered.text
        assert rendered.headers["cache-control"] == "public, max-age=31536000, immutable"

        deadline = time.monotonic() + 3
        while lifecycle.get_run("tenant-a", lifecycle_run_id).status is not LifecycleStatus.SUCCEEDED:
            if time.monotonic() >= deadline:
                raise AssertionError("production mission result was not projected to the CEO lifecycle")
            time.sleep(0.02)
        mission = api.get(
            f"/v2/runs/{lifecycle_run_id}/mission",
            headers={"Authorization": f"Bearer {owner_token}"},
        ).json()
        assert mission["execution"]["status"] == "succeeded"
        assert mission["deliverables"] == [{
            "kind": "static_site",
            "node_id": "publish",
            "deployment_id": publish.output["deployment_id"],
            "public_url": public_url,
            "receipt_artifact_id": publish.output["receipt_artifact_id"],
        }]
    finally:
        router.close()
        api.close()
        previews.close()
        notifications.close()
        artifacts.close()
        graph.close()
        lifecycle.close()
        DBOS.destroy(destroy_registry=True)
