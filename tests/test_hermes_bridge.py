import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import factory
import hermes_bridge
import providers


def test_status_requires_enable_and_fresh_successful_canary(monkeypatch, tmp_path):
    monkeypatch.setattr(hermes_bridge, "CANARY_PATH", tmp_path / "canary.json")
    monkeypatch.setattr(hermes_bridge, "_hermes_binary", lambda: "/usr/bin/hermes")
    monkeypatch.setattr(hermes_bridge.subprocess, "run", lambda *_a, **_k: type(
        "P", (), {"returncode": 0, "stdout": "Hermes Agent v0.16.0", "stderr": ""})())
    monkeypatch.delenv("AOS_HERMES_ENABLED", raising=False)
    assert hermes_bridge.status()["reason"] == "AOS_HERMES_ENABLED is not enabled"

    monkeypatch.setenv("AOS_HERMES_ENABLED", "1")
    assert hermes_bridge.status()["ready"] is False
    hermes_bridge.CANARY_PATH.write_text(json.dumps({"ok": True, "checked_at": 900.0}))
    assert hermes_bridge.status(now=1000.0)["ready"] is True


def test_bridge_fails_closed_for_tenants_mutators_and_broad_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(hermes_bridge, "status", lambda **_k: {"ready": True})
    assert "not isolated" in hermes_bridge.run(
        "researcher", tmp_path, "task", tenant_id="tenant-1")["reason"]
    assert hermes_bridge._role_toolsets("builder") is None
    assert hermes_bridge._role_toolsets("researcher", ["terminal"]) is None
    assert hermes_bridge._role_toolsets("researcher") == ["clarify", "web"]
    assert hermes_bridge._role_toolsets("qa-evidence-reviewer") == ["clarify"]


def test_provider_registry_exposes_hermes_without_making_it_default():
    assert isinstance(providers.get_provider({"AOS_PROVIDER": "hermes"}), providers.HermesAgentProvider)
    assert isinstance(providers.get_provider({"AOS_PROVIDER": "claude"}), providers.ClaudeAgentProvider)


def test_factory_hermes_failure_falls_back_to_codex_for_platform_only(monkeypatch, tmp_path):
    old = {name: getattr(factory._ctx, name, None)
           for name in ("tenant", "api_key", "codex_key", "engine")}
    try:
        factory._ctx.tenant = None
        factory._ctx.api_key = None
        factory._ctx.codex_key = None
        factory._ctx.engine = "hermes"
        monkeypatch.setattr(factory, "BUDGET_USD", 0)
        monkeypatch.setattr(factory, "HERMES_FALLBACK_ENGINE", "codex")
        monkeypatch.setattr(factory.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(factory, "_agent_hermes", lambda *_a, **_k: {
            "rc": 1, "failed": True, "provider_unavailable": True,
            "reason": "Hermes provider credits exhausted", "engine": "hermes"})
        monkeypatch.setattr(factory, "_agent_codex", lambda *_a, **_k: {
            "rc": 0, "out": "fallback", "out_full": "fallback", "engine": "codex"})
        monkeypatch.setattr(factory.audit, "append", lambda **_k: None)
        monkeypatch.setattr(factory.killswitch, "is_halted", lambda *_a, **_k: {"halted": False})

        result = factory.agent("researcher", str(tmp_path), "research", timeout=5, retries=0,
                               compact=True)
        assert result["rc"] == 0 and result["engine"] == "codex"
        assert result["failover"] is True and result["failover_source"] == "hermes"

        factory._ctx.tenant = "tenant-1"
        monkeypatch.setitem(sys.modules, "consent", type("Consent", (), {
            "require_consent": staticmethod(lambda _tenant: True)})())
        monkeypatch.setitem(sys.modules, "auth", type("Auth", (), {
            "provider_resolved": staticmethod(lambda _tenant: True)})())
        result = factory.agent("researcher", str(tmp_path), "tenant research", timeout=5,
                               retries=0, compact=True)
        assert result["failed"] is True and result["engine"] == "hermes"
        assert "not isolated" in result["reason"]
    finally:
        for name, value in old.items():
            setattr(factory._ctx, name, value)
