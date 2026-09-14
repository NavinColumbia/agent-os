"""Provider outages must not be mistaken for intelligent management decisions."""

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import factory
import management


@pytest.mark.parametrize("result", [
    {"rc": 1, "failed": True, "reason": "codex provider usage exhausted",
     "out_full": '{"type":"error","message":"usage limit"}'},
    {"rc": 1, "failed": True, "blocker": "provider_required"},
    {"rc": 0, "out": "not a decision"},
    {"rc": 0, "out": '{"action":"invented_action"}'},
])
def test_management_never_invents_a_decision_from_provider_or_parse_failure(result):
    with pytest.raises(management.ManagementDecisionUnavailable):
        management._parse_decision(result)


def test_codex_failure_is_classified_as_provider_exhaustion(monkeypatch, tmp_path):
    monkeypatch.setattr(factory, "_run_once_codex",
                        lambda *_a, **_k: (1, "You've hit your usage limit", 0.0, 0, 0,
                                           "gpt-5.6-sol"))
    monkeypatch.setattr(factory.time, "sleep", lambda *_a, **_k: None)
    result = factory._agent_codex("manager", str(tmp_path), "decide", None,
                                  timeout=5, retries=0, model="gpt-5.6-sol")
    assert result["failed"] is True
    assert result["provider_unavailable"] is True
    assert result["provider_exhausted"] is True
    assert result["reason"] == "codex provider usage exhausted"


def test_platform_codex_outage_falls_back_to_claude_without_crossing_tenant_credentials(
        monkeypatch, tmp_path):
    old = {name: getattr(factory._ctx, name, None)
           for name in ("tenant", "api_key", "codex_key", "engine")}
    try:
        factory._ctx.tenant = None
        factory._ctx.api_key = None
        factory._ctx.codex_key = None
        factory._ctx.engine = "codex"
        monkeypatch.setattr(factory, "BUDGET_USD", 0)
        monkeypatch.setattr(factory, "CODEX_FALLBACK_ENGINE", "claude")
        monkeypatch.setattr(factory.shutil, "which",
                            lambda name: f"/usr/bin/{name}" if name in {"codex", "claude"} else None)
        monkeypatch.setattr(factory, "_agent_codex", lambda *_a, **_k: {
            "rc": 1, "failed": True, "provider_unavailable": True,
            "provider_exhausted": True, "reason": "codex provider usage exhausted",
        })
        calls = []
        monkeypatch.setattr(factory, "_run_once",
                            lambda *_a, **_k: calls.append((_a, _k)) or
                            (0, "real alternate-provider answer", 0.01, 10, 5,
                             factory.CHEAP_MODEL))
        monkeypatch.setattr(factory.audit, "append", lambda **_k: None)
        monkeypatch.setattr(factory.killswitch, "is_halted", lambda *_a, **_k: {"halted": False})
        result = factory.agent("manager", str(tmp_path), "make a decision",
                               timeout=5, retries=0, light=True)
        assert result["rc"] == 0 and result["out_full"] == "real alternate-provider answer"
        assert calls and calls[0][0][5] == factory.CHEAP_MODEL

        # An explicit tenant/BYO Codex credential may not fail over to host Claude.
        factory._ctx.codex_key = "tenant-openai-key"
        calls.clear()
        result = factory.agent("manager", str(tmp_path), "tenant decision",
                               timeout=5, retries=0, light=True)
        assert result["failed"] is True
        assert calls == []
    finally:
        for name, value in old.items():
            setattr(factory._ctx, name, value)
