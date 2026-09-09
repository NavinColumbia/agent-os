from __future__ import annotations

import base64
import os
from pathlib import Path
import sys

import agent_os.entrypoints.sandbox_job as sandbox_job
from agent_os.entrypoints.sandbox_job import (
    OUTPUT_REJECTED_EXIT_CODE,
    RESULT_FORMAT,
    execute_bundle,
)


def bundle(files: dict[str, bytes]):
    return {
        "format": "agent-os.source-bundle.v1",
        "files": {
            name: {
                "encoding": "base64",
                "content": base64.b64encode(content).decode(),
                "executable": False,
            }
            for name, content in files.items()
        },
    }


def test_secretless_job_controller_executes_direct_argv_and_returns_bounded_bundle():
    result = execute_bundle(
        bundle({"main.py": b"from pathlib import Path\nPath('built.txt').write_text('done')\nprint('ok')\n"}),
        (sys.executable, "main.py"),
        fingerprint="a" * 64,
        timeout_seconds=5,
        maximum_files=10,
        maximum_output_bytes=4096,
        maximum_workspace_bytes=4096,
        maximum_log_bytes=1024,
        run_uid=os.getuid(),
        run_gid=os.getgid(),
    )

    assert result["format"] == RESULT_FORMAT
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    assert result["stdout"] == "ok\n"
    built = result["output_bundle"]["files"]["built.txt"]
    assert base64.b64decode(built["content"]) == b"done"


def test_job_controller_turns_oversized_output_into_failure_evidence():
    result = execute_bundle(
        bundle({"main.py": b"from pathlib import Path\nPath('huge').write_bytes(b'x' * 100)\n"}),
        (sys.executable, "main.py"),
        fingerprint="b" * 64,
        timeout_seconds=5,
        maximum_files=10,
        maximum_output_bytes=50,
        maximum_workspace_bytes=4096,
        maximum_log_bytes=1024,
        run_uid=os.getuid(),
        run_gid=os.getgid(),
    )

    assert result["exit_code"] == OUTPUT_REJECTED_EXIT_CODE
    assert "artifact byte limit" in result["output_error"]
    assert result["output_bundle"]["files"] == {}


def test_hosted_sandbox_image_keeps_controller_root_and_tenant_child_unprivileged():
    dockerfile = Path("deploy/Dockerfile.sandbox-v2").read_text()
    assert "@sha256:" in dockerfile
    assert "useradd --system --uid 65532" in dockerfile
    assert "chmod 0555 /tmp" in dockerfile
    assert "-perm /6000" in dockerfile
    assert "USER 65532" not in dockerfile
    assert 'ENTRYPOINT ["python", "-I", "/opt/agent-os/sandbox_job.py"]' in dockerfile


def test_job_main_erases_transfer_urls_before_starting_untrusted_child(monkeypatch):
    command = base64.b64encode(b'["python","main.py"]').decode()
    isolated_environment = {
        "AOS_SANDBOX_INPUT_URL": "https://storage.googleapis.com/bucket/input?signature=x",
        "AOS_SANDBOX_OUTPUT_URL": "https://storage.googleapis.com/bucket/output?signature=x",
        "AOS_SANDBOX_COMMAND_B64": command,
        "AOS_SANDBOX_FINGERPRINT": "c" * 64,
        "AOS_SANDBOX_TIMEOUT_SECONDS": "30",
        "AOS_SANDBOX_MAX_FILES": "10",
        "AOS_SANDBOX_MAX_OUTPUT_BYTES": "1024",
        "AOS_SANDBOX_MAX_WORKSPACE_BYTES": "2048",
        "AOS_SANDBOX_MAX_LOG_BYTES": "256",
        "AOS_SANDBOX_WORK_ROOT": "/sandbox-work",
    }
    observed = {}
    monkeypatch.setattr(sandbox_job.os, "environ", isolated_environment)
    monkeypatch.setattr(sandbox_job, "_download_json", lambda *_: bundle({"main.py": b"pass"}))

    def fake_execute(*args, **kwargs):
        del args
        observed["environment"] = dict(sandbox_job.os.environ)
        observed["work_root"] = kwargs["work_root"]
        return {
            "format": RESULT_FORMAT,
            "fingerprint": "c" * 64,
            "output_bundle": {"format": "agent-os.source-bundle.v1", "files": {}},
            "exit_code": 0,
            "timed_out": False,
            "output_error": None,
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr(sandbox_job, "execute_bundle", fake_execute)
    monkeypatch.setattr(sandbox_job, "_upload_json", lambda *args: observed.setdefault("uploaded", args))

    assert sandbox_job.main() == 0
    assert observed["environment"] == {}
    assert observed["work_root"] == "/sandbox-work"
    assert observed["uploaded"]
