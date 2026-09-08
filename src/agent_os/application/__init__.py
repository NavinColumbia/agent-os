"""Agent OS application services and framework-facing ports.

Application code coordinates domain decisions.  It may be called by an API,
CLI, Temporal workflow, or an in-memory test harness, but it does not import
those adapters itself.
"""

from .lifecycle import (
    CommandEnvelope,
    LifecycleDecision,
    LifecycleHistory,
    plan_transition,
)

__all__ = [
    "CommandEnvelope",
    "LifecycleDecision",
    "LifecycleHistory",
    "plan_transition",
]
"""Framework-neutral Agent OS application layer."""

from .ports import WorkflowEngine, WorkflowReceipt

__all__ = ["WorkflowEngine", "WorkflowReceipt"]
