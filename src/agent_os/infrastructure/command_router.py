"""Explicit routing for durable lifecycle effects.

Agent/model work and external side effects have different authority and
idempotency requirements.  This router prevents a model executor from
accidentally acknowledging notifications, deployment publication, cancellation,
or retry scheduling that no concrete integration actually performed.
"""

from __future__ import annotations

from typing import Callable, Mapping, Any

from agent_os.application.command_worker import FatalCommandError
from agent_os.application.lifecycle import CommandEnvelope
from agent_os.application.ports import CommandExecutor
from agent_os.domain.lifecycle import CommandKind
from agent_os.infrastructure.agent_command_executor import DurableAgentCommandExecutor


CommandHandler = Callable[[CommandEnvelope], Mapping[str, Any]]


class LifecycleCommandRouter(CommandExecutor):
    """Route each command kind to a deliberately registered executor."""

    def __init__(
        self,
        *,
        agent_executor: DurableAgentCommandExecutor,
        handlers: Mapping[CommandKind, CommandHandler] | None = None,
    ) -> None:
        self._agent_executor = agent_executor
        self._handlers = dict(handlers or {})
        overlap = [kind.value for kind in self._handlers if agent_executor.supports(kind)]
        if overlap:
            raise ValueError(f"agent command handlers cannot be shadowed: {sorted(overlap)}")

    def execute(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        item = CommandEnvelope.from_dict(envelope)
        if self._agent_executor.supports(item.command.kind):
            return self._agent_executor.execute(envelope)
        handler = self._handlers.get(item.command.kind)
        if handler is None:
            raise FatalCommandError(
                f"no executor is configured for external effect {item.command.kind.value}; "
                "the command was not acknowledged as delivered"
            )
        result = handler(item)
        if not isinstance(result, Mapping):
            raise FatalCommandError(
                f"executor for {item.command.kind.value} must return a result object"
            )
        return dict(result)
