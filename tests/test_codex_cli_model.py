from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, BinaryContent

from agent_os.infrastructure.codex_cli_model import (
    CodexCLIFunctionModel,
    CodexCLIModelError,
    CodexCLIModelTimeoutError,
)


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    number: int


def test_codex_timeout_is_retryable_but_remains_a_model_error():
    assert issubclass(CodexCLIModelTimeoutError, TimeoutError)
    assert issubclass(CodexCLIModelTimeoutError, CodexCLIModelError)


def test_codex_cli_model_uses_schema_bound_ephemeral_read_only_process(tmp_path: Path):
    arguments = tmp_path / "arguments.txt"
    prompt_capture = tmp_path / "prompt.txt"
    schema_capture = tmp_path / "schema.json"
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "pathlib.Path(os.environ['FAKE_CODEX_ARGUMENTS']).write_text('\\n'.join(args))\n"
        "schema = pathlib.Path(args[args.index('--output-schema') + 1])\n"
        "pathlib.Path(os.environ['FAKE_CODEX_SCHEMA']).write_text(schema.read_text())\n"
        "prompt = sys.stdin.read()\n"
        "assert 'schema-bound inference component' in prompt\n"
        "pathlib.Path(os.environ['FAKE_CODEX_PROMPT']).write_text(prompt)\n"
        "images = [pathlib.Path(args[i + 1]) for i, value in enumerate(args) if value == '--image']\n"
        "assert len(images) == 1 and images[0].read_bytes() == b'fake-png'\n"
        "output = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
        "payload = json.dumps({'status': 'ready', 'number': 7})\n"
        "output.write_text(json.dumps({'payload_json': payload}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)

    import os
    previous_arguments = os.environ.get("FAKE_CODEX_ARGUMENTS")
    previous_schema = os.environ.get("FAKE_CODEX_SCHEMA")
    previous_prompt = os.environ.get("FAKE_CODEX_PROMPT")
    os.environ["FAKE_CODEX_ARGUMENTS"] = str(arguments)
    os.environ["FAKE_CODEX_SCHEMA"] = str(schema_capture)
    os.environ["FAKE_CODEX_PROMPT"] = str(prompt_capture)
    try:
        model = CodexCLIFunctionModel(executable=str(executable), timeout_seconds=10)
        result = Agent(
            model, output_type=Result, instructions="Return the structured check.",
        ).run_sync([
            "Set status to ready and number to seven.",
            BinaryContent(data=b"fake-png", media_type="image/png"),
        ])
    finally:
        if previous_arguments is None:
            os.environ.pop("FAKE_CODEX_ARGUMENTS", None)
        else:
            os.environ["FAKE_CODEX_ARGUMENTS"] = previous_arguments
        if previous_schema is None:
            os.environ.pop("FAKE_CODEX_SCHEMA", None)
        else:
            os.environ["FAKE_CODEX_SCHEMA"] = previous_schema
        if previous_prompt is None:
            os.environ.pop("FAKE_CODEX_PROMPT", None)
        else:
            os.environ["FAKE_CODEX_PROMPT"] = previous_prompt

    assert result.output == Result(status="ready", number=7)
    supplied = arguments.read_text(encoding="utf-8").splitlines()
    assert "--ephemeral" in supplied
    assert "--ignore-user-config" in supplied
    assert "--ignore-rules" in supplied
    assert "--strict-config" in supplied
    assert supplied[supplied.index("--config") + 1] == 'model_reasoning_effort="medium"'
    assert supplied[supplied.index("--sandbox") + 1] == "read-only"
    assert "--output-schema" in supplied
    assert "--image" in supplied
    assert "fake-png" not in prompt_capture.read_text(encoding="utf-8")
    assert "[image attached to this model request]" in prompt_capture.read_text(
        encoding="utf-8",
    )
    schema = json.loads(schema_capture.read_text(encoding="utf-8"))
    assert schema["required"] == ["payload_json"]
    assert schema["additionalProperties"] is False


def test_founder_local_profile_is_loopback_unpaid_and_subscription_backed():
    root = Path(__file__).resolve().parents[1]
    operator = (root / "deploy/founder-local.sh").read_text(encoding="utf-8")
    worker = (root / "deploy/founder-local-worker.sh").read_text(encoding="utf-8")
    overlay = (root / "deploy/docker-compose.founder-local.yml").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8088" in operator
    assert "AOS_V2_BILLING_MODE=disabled" in operator
    assert "AOS_V2_MODEL=codex-cli:default" in operator
    assert "Logged in using ChatGPT" in operator
    assert "postgres migrate api" in operator
    assert "tmux new-session" in operator
    assert 'exec "$ROOT/.venv/bin/agentos-v2" worker' in worker
    assert '127.0.0.1:${AOS_V2_POSTGRES_HOST_PORT:-55432}:5432' in overlay
