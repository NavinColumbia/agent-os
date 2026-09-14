from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "qa"))


def test_qa_run_propagates_the_real_tenant_to_the_durable_org(monkeypatch):
    import qa_run

    seen = {}
    fake = types.ModuleType("qa_agentic")
    fake.run_agentic_qa = lambda *args, **kwargs: seen.update(kwargs) or {"ok": True}
    monkeypatch.setitem(sys.modules, "qa_agentic", fake)

    qa_run.qa_run("http://app", "vision", None, "42", "summary", product="p",
                  stories=[{"id": "US-1"}], tenant_id="tenant-real", agentic=True)

    assert seen["tenant"] == "tenant-real"


def test_factory_passes_controller_tenant_to_qa_entrypoint(monkeypatch, tmp_path):
    import factory

    seen = {}
    fake = types.ModuleType("qa_run")
    fake.qa_run = lambda *args, **kwargs: seen.update(kwargs) or {
        "passed": False, "total_stories": 1, "blocking_open": 1, "open_bugs": 1,
    }
    monkeypatch.setitem(sys.modules, "qa_run", fake)
    monkeypatch.setattr(factory, "_serve_static", lambda _repo: (None, "http://app"))
    monkeypatch.setattr(factory._ctx, "tenant", "tenant-controller", raising=False)

    factory.run_agentic_web_qa(str(tmp_path), "p", "vision", "summary", target_url="http://app")

    assert seen["tenant_id"] == "tenant-controller"
