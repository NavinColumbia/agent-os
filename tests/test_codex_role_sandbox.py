from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_read_only_roles_receive_an_os_enforced_codex_sandbox():
    import factory

    assert factory._codex_sandbox("reviewer") == "read-only"


def test_bounded_writer_roles_keep_workspace_access_for_their_governed_outputs():
    import factory

    assert factory._codex_sandbox("qa-security") == "workspace-write"
    assert factory._codex_sandbox("product-manager") == "workspace-write"
    assert factory._codex_sandbox("builder") == "workspace-write"
    assert factory._codex_sandbox("resource-allocator") == "workspace-write"


def test_unknown_role_fails_closed_to_read_only():
    import factory

    assert factory._codex_sandbox("role-that-does-not-exist") == "read-only"


def test_codex_primary_honors_caller_timeout_and_retry_budget(monkeypatch, tmp_path):
    import factory

    calls = []
    monkeypatch.setattr(factory.shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(factory, "_agent_codex", lambda *args, **kwargs: calls.append((args, kwargs)) or {
        "rc": 1, "failed": True, "out": "", "out_full": ""
    })
    monkeypatch.setattr(factory._ctx, "engine", "codex", raising=False)
    monkeypatch.setattr(factory._ctx, "codex_key", None, raising=False)

    factory.agent("reviewer", str(tmp_path), "inspect only", timeout=37, retries=0)

    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 37
    assert calls[0][1]["retries"] == 0


def test_codex_primary_honors_stage_specific_model_and_reasoning(monkeypatch, tmp_path):
    import factory

    calls = []
    monkeypatch.setattr(factory.shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(factory, "_agent_codex", lambda *args, **kwargs: calls.append(kwargs) or {
        "rc": 0, "out": "{}", "out_full": "{}"
    })
    monkeypatch.setattr(factory._ctx, "engine", "codex", raising=False)
    monkeypatch.setattr(factory._ctx, "codex_key", None, raising=False)

    factory.agent("reviewer", str(tmp_path), "bounded plan", timeout=37, retries=0,
                  compact=True, codex_model="gpt-5.6-terra", reasoning_effort="high")

    assert calls[0]["model"] == "gpt-5.6-terra"
    assert calls[0]["reasoning_effort"] == "high"
