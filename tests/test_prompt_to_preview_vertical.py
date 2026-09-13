from __future__ import annotations

from pathlib import Path
import time
from urllib.parse import urlparse

from dbos import DBOS
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel

from agent_os.api.app import create_app
from agent_os.api.auth import HMACTokenIdentity
from agent_os.application.command_worker import (
    CommandRunStatus,
    DurableCommandWorker,
)
from agent_os.application.graph_action_worker import DurableGraphActionWorker
from agent_os.domain.lifecycle import CommandKind, LifecycleStatus
from agent_os.domain.workflow_runtime import WorkflowRunStatus
from agent_os.infrastructure.command_router import LifecycleCommandRouter
from agent_os.infrastructure.dbos_lifecycle import DBOSLifecycleEngine
from agent_os.infrastructure.deployment_tool_nodes import DeploymentToolNodeHandlers
from agent_os.infrastructure.graph_action_executor import DurableGraphActionExecutor
from agent_os.infrastructure.graph_organization_effects import GraphOrganizationEffectHandler
from agent_os.infrastructure.mission_workflows import (
    MissionBootstrapHandler,
    MissionGraphEffectHandlers,
    WorkflowLaunchToolNodeHandlers,
    mission_planning_run_id,
)
from agent_os.infrastructure.pydantic_graph_nodes import PydanticGraphNodeRuntime
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore
from agent_os.infrastructure.sql_preview_deployments import SQLStaticPreviewDeployer
from agent_os.infrastructure.sql_workflow_graph import SQLGraphWorkflowEngine
from agent_os.infrastructure.tool_node_router import GraphToolNodeRouter


def test_authenticated_ceo_prompt_reaches_a_fetchable_public_preview(tmp_path: Path):
    lifecycle = DBOSLifecycleEngine(
        system_database_url=f"sqlite:///{tmp_path / 'system.sqlite3'}",
        application_database_url=f"sqlite:///{tmp_path / 'application.sqlite3'}",
        application_version="prompt-preview-proof-v1",
        create_schema=True,
    )
    graph = SQLGraphWorkflowEngine(
        f"sqlite:///{tmp_path / 'graph.sqlite3'}", create_schema=True,
    )
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'artifacts.sqlite3'}", create_schema=True,
    )
    deployments = SQLStaticPreviewDeployer(
        f"sqlite:///{tmp_path / 'deployments.sqlite3'}",
        artifacts,
        public_base_url="https://preview.example.test",
        capability_secret="prompt-to-preview-proof-secret-32-bytes",
        create_schema=True,
    )
    identity = HMACTokenIdentity("prompt-to-preview-proof-secret-32-bytes")
    owner_token = identity.issue(
        subject_id="human:ceo", organization_id="tenant-a", roles=("owner",),
    )
    api = TestClient(create_app(
        engine=lifecycle,
        identity=identity,
        graph_engine=graph,
        artifact_store=artifacts,
        preview_deployments=deployments,
    ))

    try:
        accepted = api.post(
            "/v2/runs",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": "prompt-preview-proof",
            },
            json={"prompt": "Build and publish a tiny interactive status dashboard."},
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        lifecycle.get_result(accepted.json()["workflow_id"])

        class NoLegacyAgentCommands:
            @staticmethod
            def supports(kind):
                return False

            def execute(self, envelope):
                raise AssertionError(f"unexpected coarse agent command: {envelope}")

        lifecycle_worker = DurableCommandWorker(
            outbox=lifecycle,
            executor=LifecycleCommandRouter(
                agent_executor=NoLegacyAgentCommands(),
                handlers={CommandKind.START_MISSION: MissionBootstrapHandler(graph).execute},
            ),
            worker_id="prompt-preview-lifecycle",
            lease_seconds=3,
            workflow_engine=lifecycle,
            workflow_result_waiter=lifecycle.get_result,
        )
        assert lifecycle_worker.run_one("tenant-a").status is CommandRunStatus.SUCCEEDED

        proposed_plan = {
            "name": "Build and publish the requested dashboard",
            "entry_node_id": "build",
            "nodes": [
                {
                    "node_id": "build",
                    "kind": "agent",
                    "purpose": "Create the complete static dashboard artifact.",
                    "owner_role": "frontend-engineer",
                    "configuration": {"max_iterations": 2},
                },
                {
                    "node_id": "publish",
                    "kind": "tool",
                    "purpose": "Publish the generated dashboard preview.",
                    "configuration": {
                        "tool": "deploy.preview",
                        "source": {
                            "node_id": "build",
                            "output_path": ["artifact_ids", "html-preview"],
                        },
                        "success_condition": "published",
                        "max_iterations": 1,
                    },
                },
                {
                    "node_id": "revise-program", "kind": "tool",
                    "purpose": "Atomically admit a repaired mission program.",
                    "configuration": {"tool": "workflow.revise",
                        "source": {"node_id": "build",
                                   "output_path": ["artifact_ids", "mission-program-revision"]},
                        "success_condition": "revised", "max_iterations": 1},
                },
                {
                    "node_id": "done",
                    "kind": "terminal",
                    "purpose": "Accept the durable build and deployment evidence.",
                    "configuration": {"max_iterations": 1},
                },
            ],
            "edges": [
                {"source": "build", "target": "publish", "condition": "built"},
                {"source": "build", "target": "revise-program", "condition": "repair"},
                {"source": "revise-program", "target": "build", "condition": "revised"},
                {"source": "publish", "target": "done", "condition": "published"},
            ],
        }
        proposed_program = {
            "format": "agent-os.mission-program.v1", "revision": 1,
            "objective": "Build and publish a tiny interactive status dashboard.",
            "authorized_budget_cents": 0,
            "success_measures": [{"measure_id": "preview-ready",
                "description": "The dashboard is publicly fetchable and interactive."}],
            "feasibility": {
                "verdict": "viable",
                "rationale": "The configured preview publisher can serve a bounded static app.",
                "delivery_estimate": {"optimistic": 1, "likely": 2, "pessimistic": 4,
                    "unit": "hours", "basis": "One static build and preview publication.",
                    "confidence": 0.8},
                "cost_estimate": {"optimistic": 0, "likely": 0, "pessimistic": 100,
                    "unit": "usd_cents", "basis": "Local deterministic preview storage.",
                    "confidence": 0.9},
                "assumptions": ["A static preview satisfies this proof."],
            },
            "clarifications": [],
            "roles": [{"role_id": "frontend-engineer", "title": "Frontend Engineer",
                "participant_kind": "agent",
                "responsibilities": ["Build, review, and repair the preview"]}],
            "resources": [],
            "capabilities": [
                {"capability_id": "static-build", "purpose": "Build the dashboard",
                 "status": "missing", "owner_role_id": "frontend-engineer",
                 "expansion_mode": "build_capability", "expansion_node_ids": ["build"],
                 "acceptance_checks": ["HTML is complete and interactive."]},
                {"capability_id": "preview-publish", "purpose": "Publish a fetchable preview",
                 "status": "available_unverified", "owner_role_id": "frontend-engineer",
                 "expansion_mode": "verify", "expansion_node_ids": ["publish"],
                 "required_tool_ids": ["deploy.preview"],
                 "acceptance_checks": ["Published URL returns the exact artifact."]},
            ],
            "workstreams": [{"workstream_id": "delivery",
                "objective": "Build, inspect, repair, and publish the dashboard",
                "accountable_role_id": "frontend-engineer",
                "workflow_node_ids": ["build", "publish", "revise-program"],
                "required_capability_ids": ["static-build", "preview-publish"],
                "acceptance_criteria": ["Public preview is fetchable."]}],
            "verification": [{"claim_id": "preview-ready",
                "claim": "The requested dashboard is publicly fetchable.",
                "success_measure_ids": ["preview-ready"],
                "reviewer_role_id": "frontend-engineer", "verification_node_ids": ["publish"],
                "required_evidence": ["Immutable artifact and deployment receipt"],
                "failure_routes_to_node_id": "build"}],
            "replanning": {"owner_role_id": "frontend-engineer",
                "review_cadence": "At each build result",
                "triggers": ["Build evidence is incomplete", "requirements change"],
                "replan_node_ids": ["build"], "continue_condition": "built",
                "replan_condition": "repair", "material_change_requires_new_revision": True,
                "notify_role_ids": ["frontend-engineer"]},
            "workflow": proposed_plan,
        }
        planner_output = {
            "summary": "Designed the smallest sufficient build-and-publish team.",
            "disposition": "complete",
            "satisfied_conditions": ["planned"],
            "artifacts": [{
                "label": "mission-workflow",
                "media_type": "application/json",
                "json_value": proposed_program,
            }],
        }
        launch_tools = WorkflowLaunchToolNodeHandlers(graph, artifacts)
        planner_runtime = PydanticGraphNodeRuntime(
            TestModel(custom_output_args=planner_output),
            handlers=GraphToolNodeRouter(launch_tools.named_handlers()).handlers(),
            artifact_store=artifacts,
            max_turn_budget_cents=1,
        )
        planner_worker = DurableGraphActionWorker(
            outbox=graph,
            executor=DurableGraphActionExecutor(engine=graph, node_runtime=planner_runtime),
            worker_id="prompt-preview-planner",
            lease_seconds=3,
        )
        assert planner_worker.run_one("tenant-a").status is CommandRunStatus.SUCCEEDED
        assert planner_worker.run_one("tenant-a").status is CommandRunStatus.SUCCEEDED

        planning = graph.get_graph_run("tenant-a", mission_planning_run_id(run_id))
        launch_token = next(token for token in planning.tokens if token.node_id == "launch")
        child_run_id = str(launch_token.output["child_run_id"])
        html = (
            "<!doctype html><title>Agent OS proof</title>"
            "<main><h1>System healthy</h1><button id='refresh'>Refresh</button></main>"
            "<script>document.querySelector('#refresh').onclick=()=>location.reload()</script>"
        )
        builder_output = {
            "summary": "Built the complete bounded dashboard preview.",
            "disposition": "complete",
            "satisfied_conditions": ["built"],
            "artifacts": [{
                "label": "html-preview",
                "media_type": "text/html",
                "content": html,
            }],
        }
        named_handlers = dict(launch_tools.named_handlers())
        named_handlers.update(DeploymentToolNodeHandlers(deployments).named_handlers())
        mission_runtime = PydanticGraphNodeRuntime(
            TestModel(custom_output_args=builder_output),
            handlers=GraphToolNodeRouter(named_handlers).handlers(),
            artifact_store=artifacts,
            max_turn_budget_cents=1,
        )
        mission_worker = DurableGraphActionWorker(
            outbox=graph,
            executor=DurableGraphActionExecutor(
                engine=graph,
                node_runtime=mission_runtime,
                effect_handlers={**MissionGraphEffectHandlers(
                    lifecycle_engine=lifecycle,
                    graph_engine=graph,
                    notification_handlers={},
                ).graph_handlers(), **GraphOrganizationEffectHandler(lifecycle).handlers()},
            ),
            worker_id="prompt-preview-mission",
            lease_seconds=3,
        )

        reports = []
        for _ in range(12):
            report = mission_worker.run_one("tenant-a")
            if report.status is CommandRunStatus.IDLE:
                break
            reports.append(report)
        assert reports and all(
            report.status is CommandRunStatus.SUCCEEDED for report in reports
        )
        assert mission_worker.run_one("tenant-a").status is CommandRunStatus.IDLE

        child = graph.get_graph_run("tenant-a", child_run_id)
        assert child is not None and child.status is WorkflowRunStatus.SUCCEEDED
        publish_token = next(token for token in child.tokens if token.node_id == "publish")
        public_url = str(publish_token.output["public_url"])

        deadline = time.monotonic() + 3
        while lifecycle.get_run("tenant-a", run_id).status is not LifecycleStatus.SUCCEEDED:
            if time.monotonic() >= deadline:
                raise AssertionError("mission result was not projected to the CEO lifecycle")
            time.sleep(0.02)

        mission_status = api.get(
            f"/v2/runs/{run_id}/mission",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        rendered = api.get(urlparse(public_url).path)
        activity = api.get(
            f"/v2/runs/{run_id}/activity",
            headers={"Authorization": f"Bearer {owner_token}"},
        ).json()
        assert mission_status.status_code == 200
        assert mission_status.json()["execution"]["status"] == "succeeded"
        assert mission_status.json()["program"]["feasibility"]["verdict"] == "viable"
        assert any(event["kind"] == "program_admitted" for event in activity["items"])
        assert mission_status.json()["deliverables"] == [{
            "kind": "static_preview",
            "node_id": "publish",
            "deployment_id": publish_token.output["deployment_id"],
            "public_url": public_url,
            "receipt_artifact_id": publish_token.output["receipt_artifact_id"],
            "expires_at": publish_token.output["expires_at"],
        }]
        assert rendered.status_code == 200
        assert rendered.text == html
        assert "sandbox allow-scripts" in rendered.headers["content-security-policy"]
    finally:
        api.close()
        deployments.close()
        artifacts.close()
        graph.close()
        lifecycle.close()
        DBOS.destroy(destroy_registry=True)
