"""Versioned customer workflow graphs generated and evolved by agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class NodeKind(str, Enum):
    AGENT = "agent"
    TOOL = "tool"
    DECISION = "decision"
    HUMAN = "human"
    WAIT = "wait"
    SUBWORKFLOW = "subworkflow"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class WorkflowNode:
    node_id: str
    kind: NodeKind
    purpose: str
    owner_role: str | None = None
    configuration: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.node_id.strip() or not self.purpose.strip():
            raise ValueError("workflow node_id and purpose are required")
        if self.kind is NodeKind.AGENT and not self.owner_role:
            raise ValueError("agent nodes require an owner_role")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "purpose": self.purpose,
            "owner_role": self.owner_role,
            "configuration": dict(self.configuration),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorkflowNode":
        configuration = raw.get("configuration", {})
        if not isinstance(configuration, Mapping):
            raise ValueError("workflow node configuration must be an object")
        return cls(
            node_id=str(raw["node_id"]),
            kind=NodeKind(str(raw["kind"])),
            purpose=str(raw["purpose"]),
            owner_role=raw.get("owner_role"),
            configuration=dict(configuration),
        )


@dataclass(frozen=True)
class WorkflowEdge:
    source: str
    target: str
    condition: str = "always"
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.target.strip() or not self.condition.strip():
            raise ValueError("workflow edge source, target, and condition are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "condition": self.condition,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorkflowEdge":
        return cls(
            source=str(raw["source"]),
            target=str(raw["target"]),
            condition=str(raw.get("condition", "always")),
            priority=int(raw.get("priority", 0)),
        )


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_id: str
    tenant_id: str
    name: str
    version: int
    entry_node_id: str
    nodes: tuple[WorkflowNode, ...]
    edges: tuple[WorkflowEdge, ...]
    created_by: str
    supersedes_version: int | None = None

    def __post_init__(self) -> None:
        if not self.workflow_id or not self.tenant_id or not self.name or not self.created_by:
            raise ValueError("workflow identity, tenant, name, and creator are required")
        if self.version < 1 or not self.nodes:
            raise ValueError("workflow version must be positive and contain nodes")
        node_map = {node.node_id: node for node in self.nodes}
        if len(node_map) != len(self.nodes):
            raise ValueError("workflow node IDs must be unique")
        if self.entry_node_id not in node_map:
            raise ValueError("workflow entry node does not exist")
        if not any(node.kind is NodeKind.TERMINAL for node in self.nodes):
            raise ValueError("workflow requires at least one explicit terminal node")
        for edge in self.edges:
            if edge.source not in node_map or edge.target not in node_map:
                raise ValueError("workflow edge references an unknown node")
        reachable = {self.entry_node_id}
        changed = True
        while changed:
            changed = False
            for edge in self.edges:
                if edge.source in reachable and edge.target not in reachable:
                    reachable.add(edge.target)
                    changed = True
        unreachable = set(node_map) - reachable
        if unreachable:
            raise ValueError(f"workflow contains unreachable nodes: {sorted(unreachable)}")

    def outgoing(self, node_id: str) -> tuple[WorkflowEdge, ...]:
        return tuple(sorted(
            (edge for edge in self.edges if edge.source == node_id),
            key=lambda edge: (-edge.priority, edge.target),
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "version": self.version,
            "entry_node_id": self.entry_node_id,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "created_by": self.created_by,
            "supersedes_version": self.supersedes_version,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorkflowDefinition":
        return cls(
            workflow_id=str(raw["workflow_id"]),
            tenant_id=str(raw["tenant_id"]),
            name=str(raw["name"]),
            version=int(raw["version"]),
            entry_node_id=str(raw["entry_node_id"]),
            nodes=tuple(WorkflowNode.from_dict(item) for item in raw.get("nodes", ())),
            edges=tuple(WorkflowEdge.from_dict(item) for item in raw.get("edges", ())),
            created_by=str(raw["created_by"]),
            supersedes_version=(
                None if raw.get("supersedes_version") is None else int(raw["supersedes_version"])
            ),
        )
