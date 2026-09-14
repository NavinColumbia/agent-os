"""Founder-only PydanticAI model adapter backed by a local Codex login.

This adapter exists for a trusted, loopback-only product rehearsal.  It does
not copy or expose the cached ChatGPT credential.  Each model request invokes
the locally authenticated Codex CLI in an isolated, read-only directory and
requires the CLI to write a JSON object matching PydanticAI's output schema.
Hosted and multi-tenant deployments must use normal provider API adapters.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel


class CodexCLIModelError(RuntimeError):
    """The local subscription-backed inference process did not complete safely."""


class CodexCLIModelTimeoutError(CodexCLIModelError, TimeoutError):
    """A useful model turn exceeded its transport deadline and may be retried."""


def _timeout_seconds(settings: Mapping[str, Any] | None, default: float) -> float:
    raw = default if settings is None else settings.get("timeout", default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise CodexCLIModelError("Codex CLI timeout must be numeric") from exc
    if not 1 <= value <= 3_600:
        raise CodexCLIModelError("Codex CLI timeout must be between 1 and 3600 seconds")
    return value


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()


def _bounded_detail(value: str, limit: int = 2_000) -> str:
    clean = value.strip()
    return clean if len(clean) <= limit else clean[-limit:]


_CODEX_ENVELOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "payload_json": {
            "type": "string",
            "description": "One JSON-serialized object matching the target schema in the prompt.",
        },
    },
    "required": ["payload_json"],
    "additionalProperties": False,
}


class CodexCLIFunctionModel(FunctionModel):
    """A no-tools FunctionModel that delegates one structured turn to Codex CLI."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        codex_model: str | None = None,
        reasoning_effort: str = "medium",
        timeout_seconds: float = 300,
    ) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise ValueError("Codex CLI model requires an installed codex executable")
        if codex_model is not None and (
            not codex_model.strip() or len(codex_model) > 256
            or any(character in codex_model for character in "\r\n\0")
        ):
            raise ValueError("Codex CLI model name is invalid")
        if not 1 <= timeout_seconds <= 3_600:
            raise ValueError("Codex CLI timeout must be between 1 and 3600 seconds")
        if reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("Codex CLI reasoning effort is invalid")
        self._executable = resolved
        self._codex_model = None if codex_model is None else codex_model.strip()
        self._reasoning_effort = reasoning_effort
        self._timeout_seconds = float(timeout_seconds)
        name = "codex-cli:" + (self._codex_model or "subscription-default")
        super().__init__(self._request, model_name=name)

    @staticmethod
    def _image_attachments(messages: Sequence[ModelMessage]) -> list[BinaryContent]:
        images: list[BinaryContent] = []
        total_bytes = 0
        for message in messages:
            for part in message.parts:
                if not isinstance(part, UserPromptPart):
                    continue
                values = part.content if isinstance(part.content, list) else [part.content]
                for value in values:
                    if not isinstance(value, BinaryContent) or not value.is_image:
                        continue
                    if len(images) >= 5 or total_bytes + len(value.data) > 5 * 1024 * 1024:
                        continue
                    images.append(value)
                    total_bytes += len(value.data)
        return images

    @staticmethod
    def _transcript(messages: Sequence[ModelMessage]) -> str:
        raw = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")

        def scrub(value: Any) -> Any:
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, dict):
                if value.get("kind") == "binary":
                    media_type = str(value.get("media_type") or "application/octet-stream")
                    return {
                        **{key: scrub(item) for key, item in value.items() if key != "data"},
                        "data": (
                            "[image attached to this model request]"
                            if media_type.startswith("image/")
                            else "[binary content omitted]"
                        ),
                    }
                return {key: scrub(item) for key, item in value.items()}
            return value

        return json.dumps(scrub(raw), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _prompt(
        messages: Sequence[ModelMessage],
        info: AgentInfo,
        target_schema: Mapping[str, Any],
    ) -> str:
        transcript = CodexCLIFunctionModel._transcript(messages)
        schema = json.dumps(target_schema, ensure_ascii=False, separators=(",", ":"))
        return (
            "You are the schema-bound inference component inside a durable local Agent OS "
            "rehearsal. Do not inspect files, run shell commands, or use external tools. "
            "Use only the instructions and conversation supplied below. Construct exactly one "
            "JSON object matching the target schema. Return it as a JSON-serialized string in "
            "the payload_json field required by the response envelope; do not add prose or "
            "fences. Keep the object compact: include all target-required fields, but omit "
            "defaulted or optional fields when they carry no material value.\n\n"
            f"Target output schema:\n{schema}\n\n"
            f"Standing instructions:\n{info.instructions or ''}\n\n"
            f"Conversation as typed PydanticAI messages:\n{transcript}"
        )

    def _request(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if info.function_tools:
            raise CodexCLIModelError(
                "the founder subscription bridge does not expose executable PydanticAI tools"
            )
        if len(info.output_tools) != 1:
            raise CodexCLIModelError(
                "the founder subscription bridge requires exactly one structured output tool"
            )
        output_tool = info.output_tools[0]
        timeout = _timeout_seconds(info.model_settings, self._timeout_seconds)
        with tempfile.TemporaryDirectory(prefix="agent-os-codex-model-") as temporary:
            root = Path(temporary)
            schema_path = root / "output.schema.json"
            result_path = root / "output.json"
            schema_path.write_text(
                json.dumps(_CODEX_ENVELOPE_SCHEMA, separators=(",", ":")),
                encoding="utf-8",
            )
            image_paths: list[Path] = []
            suffixes = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }
            for index, attachment in enumerate(self._image_attachments(messages), start=1):
                path = root / f"input-{index}{suffixes.get(str(attachment.media_type), '.img')}"
                path.write_bytes(attachment.data)
                image_paths.append(path)
            command = [
                self._executable,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--strict-config",
                "--config",
                f'model_reasoning_effort="{self._reasoning_effort}"',
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--cd",
                temporary,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
            ]
            if self._codex_model is not None:
                command.extend(("--model", self._codex_model))
            for path in image_paths:
                command.extend(("--image", str(path)))
            command.append("-")
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(
                    self._prompt(messages, info, output_tool.parameters_json_schema),
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                _terminate_process_group(process)
                raise CodexCLIModelTimeoutError(
                    f"Codex CLI model exceeded its {timeout:g}-second request limit"
                ) from exc
            if process.returncode != 0:
                detail = _bounded_detail(stderr or stdout) or "no diagnostic was returned"
                raise CodexCLIModelError(
                    f"Codex CLI model exited with status {process.returncode}: {detail}"
                )
            try:
                envelope = json.loads(result_path.read_text(encoding="utf-8"))
                if not isinstance(envelope, dict) or set(envelope) != {"payload_json"}:
                    raise ValueError("invalid Codex response envelope")
                payload = json.loads(envelope["payload_json"])
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                detail = _bounded_detail(stderr or stdout) or "missing structured output"
                raise CodexCLIModelError(
                    f"Codex CLI model returned invalid structured output: {detail}"
                ) from exc
            except (TypeError, ValueError) as exc:
                raise CodexCLIModelError(
                    "Codex CLI model returned an invalid structured response envelope"
                ) from exc
            if not isinstance(payload, dict):
                raise CodexCLIModelError("Codex CLI model output must be one JSON object")
        return ModelResponse(parts=[ToolCallPart(output_tool.name, payload)])


def codex_cli_model(configured: str, *, timeout_seconds: float) -> CodexCLIFunctionModel:
    """Resolve ``codex-cli:default`` or an explicit subscription model name."""

    provider, separator, raw_model = configured.partition(":")
    if provider != "codex-cli" or not separator:
        raise ValueError("Codex CLI model must use codex-cli:<model-or-default>")
    raw_model = raw_model.strip()
    if not raw_model:
        raise ValueError("Codex CLI model must name default or an explicit model")
    return CodexCLIFunctionModel(
        codex_model=None if raw_model in {"default", "subscription-default"} else raw_model,
        timeout_seconds=timeout_seconds,
    )
