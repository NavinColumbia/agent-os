from __future__ import annotations

import socket
from pathlib import Path

import pytest

from agent_os.application.command_worker import FatalCommandError
from agent_os.domain.workflow import NodeKind, WorkflowDefinition, WorkflowEdge, WorkflowNode
from agent_os.domain.workflow_runtime import NodeToken, TokenStatus, WorkflowAction, WorkflowActionKind, WorkflowRunState, WorkflowRunStatus
from agent_os.infrastructure.http_connector_tools import FileConnectorSecretResolver, GCPConnectorSecretResolver, HTTPConnectorToolNodeHandlers, _public_addresses
from agent_os.infrastructure.mission_workflows import materialize_mission_workflow
from agent_os.infrastructure.sql_artifacts import SQLArtifactStore
from agent_os.infrastructure.sql_connectors import SQLConnectorRegistry
from agent_os.infrastructure.sql_notifications import SQLNotificationStore


def connector_definition(**overrides):
    value = {
        "connector_id": "issue-tracker",
        "display_name": "Issue tracker",
        "base_url": "https://issues.example.test",
        "allowed_path_prefixes": ["/api/issues"],
        "allowed_methods": ["GET", "POST"],
        "auth_kind": "bearer",
        "credential_ref": "issue-tracker-token",
        "idempotency_header": "Idempotency-Key",
        "timeout_seconds": 15,
        "max_response_bytes": 1024,
    }
    value.update(overrides)
    return value


@pytest.fixture
def connector_stores(tmp_path: Path):
    registry = SQLConnectorRegistry(
        f"sqlite:///{tmp_path / 'connectors.sqlite3'}", create_schema=True,
    )
    artifacts = SQLArtifactStore(
        f"sqlite:///{tmp_path / 'connector-artifacts.sqlite3'}", create_schema=True,
    )
    try:
        yield registry, artifacts
    finally:
        artifacts.close()
        registry.close()


def test_connector_registry_is_immutable_idempotent_tenant_fenced_and_secretless(connector_stores):
    registry, _ = connector_stores
    first = registry.register_connector(
        tenant_id="tenant-a", definition=connector_definition(), actor_id="human:ceo",
        idempotency_key="register-issues",
    )
    replay = registry.register_connector(
        tenant_id="tenant-a", definition=connector_definition(), actor_id="human:ceo",
        idempotency_key="register-issues",
    )

    assert first["duplicate"] is False
    assert replay["duplicate"] is True
    assert first["credential_ref"] == "issue-tracker-token"
    assert "secret" not in first
    assert registry.get_connector("tenant-b", "issue-tracker") is None
    with pytest.raises(ValueError, match="immutable"):
        registry.register_connector(
            tenant_id="tenant-a",
            definition=connector_definition(display_name="Changed tracker"),
            actor_id="human:ceo", idempotency_key="another-registration",
        )
    disabled = registry.disable_connector(
        tenant_id="tenant-a", connector_id="issue-tracker", actor_id="human:ceo",
        reason="Credential rotation", idempotency_key="disable-issues",
    )
    assert disabled["active"] is False
    assert registry.disable_connector(
        tenant_id="tenant-a", connector_id="issue-tracker", actor_id="human:ceo",
        reason="Credential rotation", idempotency_key="disable-issues",
    )["duplicate"] is True


def test_connector_changes_publish_secretless_live_invalidations(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'connector-events.sqlite3'}"
    registry = SQLConnectorRegistry(database_url, create_schema=True)
    experience = SQLNotificationStore(database_url, create_schema=True)
    try:
        definition = connector_definition(
            display_name="Confidential acquisition tracker",
            credential_ref="highly-private-credential-reference",
        )
        registry.register_connector(
            tenant_id="tenant-a", definition=definition, actor_id="human:ceo",
            idempotency_key="register-private-issues",
        )
        assert registry.register_connector(
            tenant_id="tenant-a", definition=definition, actor_id="human:ceo",
            idempotency_key="register-private-issues",
        )["duplicate"] is True
        registry.disable_connector(
            tenant_id="tenant-a", connector_id="issue-tracker", actor_id="human:ceo",
            reason="Private credential may be compromised",
            idempotency_key="disable-private-issues",
        )

        events = experience.list_experience_events(
            "tenant-a", audience_ids=("tenant:members",), limit=100,
        ).events
        assert [event["kind"] for event in events] == [
            "integration.connector.registered", "integration.connector.disabled",
        ]
        assert [event["projection_revision"] for event in events] == [1, 2]
        summaries = " ".join(event["safe_summary"] for event in events)
        assert "acquisition" not in summaries.lower()
        assert "credential" not in summaries.lower()
        assert not experience.list_experience_events(
            "tenant-b", audience_ids=("tenant:members",), limit=100,
        ).events
    finally:
        experience.close()
        registry.close()


@pytest.mark.parametrize("definition", [
    connector_definition(base_url="http://issues.example.test"),
    connector_definition(base_url="https://user:pass@issues.example.test"),
    connector_definition(allowed_path_prefixes=["/api/../admin"]),
    connector_definition(auth_kind="header", auth_header="Host"),
    connector_definition(idempotency_header=None),
])
def test_connector_registry_rejects_unsafe_capabilities(connector_stores, definition):
    registry, _ = connector_stores
    with pytest.raises(ValueError):
        registry.register_connector(
            tenant_id="tenant-a", definition=definition, actor_id="human:ceo",
            idempotency_key="unsafe-connector",
        )


def test_connector_tool_is_credential_safe_idempotent_and_path_bounded(connector_stores):
    registry, artifacts = connector_stores
    registry.register_connector(
        tenant_id="tenant-a", definition=connector_definition(), actor_id="human:ceo",
        idempotency_key="register-issues",
    )
    calls = []

    class Secrets:
        def resolve(self, tenant_id, credential_ref):
            assert (tenant_id, credential_ref) == ("tenant-a", "issue-tracker-token")
            return "private-token-value"

    def transport(method, url, headers, body, timeout, max_bytes):
        calls.append((method, url, dict(headers), body, timeout, max_bytes))
        assert headers["Authorization"] == "Bearer private-token-value"
        return 200, {"Content-Type": "application/json"}, b'{"issue":"ready"}'

    definition = WorkflowDefinition(
        "connector-workflow", "tenant-a", "Fetch issue", 1, "fetch",
        (
            WorkflowNode("fetch", NodeKind.TOOL, "Read the issue", "research-lead", {
                "tool": "connector.invoke", "connector_id": "issue-tracker",
                "method": "GET", "path": "/api/issues/42",
                "query": {"fields": ["title", "status"]}, "success_condition": "fetched",
            }),
            WorkflowNode("done", NodeKind.TERMINAL, "Accept issue evidence"),
        ),
        (WorkflowEdge("fetch", "done", "fetched"),), "architect",
    )
    state = WorkflowRunState(
        "connector-run", "tenant-a", definition.workflow_id, 1, 1,
        WorkflowRunStatus.ACTIVE,
        (NodeToken("fetch-token", "fetch", TokenStatus.RUNNING, 1, attempt=1),),
    )
    action = WorkflowAction(
        "connector-action", WorkflowActionKind.EXECUTE_NODE, "fetch-token", "fetch",
    )
    handler = HTTPConnectorToolNodeHandlers(
        registry, artifacts, Secrets(), transport=transport,
    )
    first = handler.execute(
        "tenant-a", "connector-run", definition, state, action, definition.nodes[0],
    )
    replay = handler.execute(
        "tenant-a", "connector-run", definition, state, action, definition.nodes[0],
    )

    assert len(calls) == 1
    assert replay == first
    assert "private-token-value" not in str(first)
    assert artifacts.get("tenant-a", first["output"]["response_artifact_id"]) == b'{"issue":"ready"}'
    bad_node = WorkflowNode("fetch", NodeKind.TOOL, "Escape", "research-lead", {
        **definition.nodes[0].configuration, "path": "/api/issues-admin",
    })
    with pytest.raises(FatalCommandError, match="owner-approved capability"):
        handler.execute("tenant-a", "connector-run", definition, state, WorkflowAction(
            "bad-path-action", WorkflowActionKind.EXECUTE_NODE, "fetch-token", "fetch",
        ), bad_node)


def test_connector_write_requires_real_human_approval_and_sends_idempotency_key(connector_stores):
    registry, artifacts = connector_stores
    registry.register_connector(
        tenant_id="tenant-a", definition=connector_definition(), actor_id="human:ceo",
        idempotency_key="register-issues",
    )
    captured = {}

    class Secrets:
        def resolve(self, *_):
            return "token"

    def transport(method, url, headers, body, timeout, max_bytes):
        captured.update({"method": method, "headers": dict(headers), "body": body})
        return 201, {"Content-Type": "application/json"}, b'{"created":true}'

    definition = WorkflowDefinition(
        "connector-write", "tenant-a", "Create issue", 1, "approve",
        (
            WorkflowNode("approve", NodeKind.HUMAN, "Approve ticket creation"),
            WorkflowNode("create", NodeKind.TOOL, "Create ticket", "product-architect", {
                "tool": "connector.invoke", "connector_id": "issue-tracker",
                "method": "POST", "path": "/api/issues", "approval_node_id": "approve",
                "success_condition": "created",
            }),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
        ),
        (WorkflowEdge("approve", "create", "approved"), WorkflowEdge("create", "done", "created")),
        "architect",
    )
    action = WorkflowAction("write-action", WorkflowActionKind.EXECUTE_NODE, "create-token", "create")
    denied = WorkflowRunState(
        "write-run", "tenant-a", definition.workflow_id, 1, 1, WorkflowRunStatus.ACTIVE,
        (
            NodeToken("approval-token", "approve", TokenStatus.SUCCEEDED, 1,
                      evidence_ids=("human-denial",),
                      output={"human_response": {"approved": False}}),
            NodeToken("create-token", "create", TokenStatus.RUNNING, 1, attempt=1),
        ),
    )
    handler = HTTPConnectorToolNodeHandlers(registry, artifacts, Secrets(), transport=transport)
    with pytest.raises(FatalCommandError, match="explicit human approval"):
        handler.execute("tenant-a", "write-run", definition, denied, action, definition.nodes[1])
    approved = WorkflowRunState.from_dict({
        **denied.to_dict(),
        "tokens": [
            {**denied.tokens[0].to_dict(), "output": {"human_response": {"approved": True}}},
            denied.tokens[1].to_dict(),
        ],
    })
    result = handler.execute(
        "tenant-a", "write-run", definition, approved, action, definition.nodes[1],
    )
    assert result["output"]["status_code"] == 201
    assert captured["method"] == "POST"
    assert captured["headers"]["Idempotency-Key"] == "write-action"


def test_ssrf_guard_rejects_any_non_public_dns_answer(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(FatalCommandError, match="non-public"):
        _public_addresses("example.test", 443)


def test_connector_prefix_ending_in_slash_does_not_admit_unrelated_paths(connector_stores):
    registry, artifacts = connector_stores
    registry.register_connector(
        tenant_id="tenant-a",
        definition=connector_definition(
            allowed_path_prefixes=["/api/issues/"], allowed_methods=["GET"],
        ),
        actor_id="human:ceo",
        idempotency_key="register-trailing-prefix",
    )
    definition = WorkflowDefinition(
        "prefix-workflow", "tenant-a", "Prove path boundary", 1, "escape",
        (
            WorkflowNode("escape", NodeKind.TOOL, "Escape", "research-lead", {
                "tool": "connector.invoke", "connector_id": "issue-tracker",
                "method": "GET", "path": "/admin", "success_condition": "done",
            }),
            WorkflowNode("done", NodeKind.TERMINAL, "Done"),
        ),
        (WorkflowEdge("escape", "done", "done"),), "architect",
    )
    state = WorkflowRunState(
        "prefix-run", "tenant-a", definition.workflow_id, 1, 1,
        WorkflowRunStatus.ACTIVE,
        (NodeToken("escape-token", "escape", TokenStatus.RUNNING, 1, attempt=1),),
    )
    handler = HTTPConnectorToolNodeHandlers(
        registry, artifacts, type("Secrets", (), {"resolve": lambda *_: "token"})(),
        transport=lambda *_: (200, {}, b"ok"),
    )
    with pytest.raises(FatalCommandError, match="owner-approved capability"):
        handler.execute(
            "tenant-a", "prefix-run", definition, state,
            WorkflowAction(
                "prefix-action", WorkflowActionKind.EXECUTE_NODE,
                "escape-token", "escape",
            ),
            definition.nodes[0],
        )


def test_file_secret_resolver_is_tenant_derived_and_rejects_symlinks(tmp_path: Path):
    resolver = FileConnectorSecretResolver(str(tmp_path))
    tenant_dir = tmp_path / resolver.tenant_directory("tenant-a")
    tenant_dir.mkdir()
    secret = tenant_dir / "api-token"
    secret.write_text("tenant-secret\n")
    assert resolver.resolve("tenant-a", "api-token") == "tenant-secret"
    symlink = tenant_dir / "linked-token"
    symlink.symlink_to(secret)
    with pytest.raises(FatalCommandError, match="not provisioned"):
        resolver.resolve("tenant-a", "linked-token")


def test_gcp_secret_resolver_uses_tenant_derived_name_and_never_raw_identity():
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"payload": {"data": "dGVuYW50LXNlY3JldA=="}}

    class Session:
        url = None

        def get(self, url, timeout):
            self.url = url
            assert timeout == 15
            return Response()

    session = Session()
    resolver = GCPConnectorSecretResolver("project-a", session=session)
    assert resolver.resolve("private-tenant-name", "jira-token") == "tenant-secret"
    assert "private-tenant-name" not in session.url
    assert "jira-token" not in session.url
    assert GCPConnectorSecretResolver.secret_name(
        "private-tenant-name", "jira-token",
    ) in session.url


def test_connector_migration_is_rls_fenced_and_contains_no_secret_bytes():
    migration = (Path(__file__).resolve().parents[1] / "postgres/initdb/99zz-connectors-v2.sql").read_text()
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "current_setting('app.tenant_id', true)" in migration
    assert "credential_ref" not in migration
    assert "secret_value" not in migration


def test_mission_materializer_admits_reads_and_requires_human_before_writes():
    plan = {
        "name": "Read external truth", "entry_node_id": "fetch",
        "nodes": [
            {"node_id": "fetch", "kind": "tool", "purpose": "Fetch issue",
             "owner_role": "research-lead", "configuration": {
                 "tool": "connector.invoke", "connector_id": "issue-tracker",
                 "method": "GET", "path": "/api/issues/42",
                 "success_condition": "fetched", "max_iterations": 1,
             }},
            {"node_id": "done", "kind": "terminal", "purpose": "Done",
             "configuration": {"max_iterations": 1}},
        ],
        "edges": [{"source": "fetch", "target": "done", "condition": "fetched"}],
    }
    materialize_mission_workflow(
        plan, tenant_id="tenant-a", planning_run_id="connector-plan",
        artifact_id="connector-program", allowed_tools={"connector.invoke"},
    )
    plan["nodes"][0]["configuration"]["method"] = "POST"
    with pytest.raises(FatalCommandError, match="human approval"):
        materialize_mission_workflow(
            plan, tenant_id="tenant-a", planning_run_id="connector-plan-write",
            artifact_id="connector-program-write", allowed_tools={"connector.invoke"},
        )
