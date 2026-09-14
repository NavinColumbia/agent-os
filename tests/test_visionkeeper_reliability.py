import hashlib
import json
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import visionkeeper as mod  # noqa: E402
from dbpool import tenant_connection  # noqa: E402


def _reply():
    return {"refined_vision": "A durable company.", "goals": ["ship safely"], "non_goals": [],
            "quality_bar": ["no silent failure"], "prerequisites": [],
            "next_capabilities": ["bounded recovery"], "open_questions": []}


def test_standing_vision_preserves_the_commercial_product_mandate():
    assert "hosted multi-tenant product" in mod.SEED_VISION
    assert "enterprise self-hosting" in mod.SEED_VISION
    assert "millions of users" in mod.SEED_VISION
    assert "Never list building or selling Agent OS as a public SaaS product as a non-goal" in mod._REFINE_SYS


def test_refine_is_bounded_restores_factory_context_and_binds_atomic_doc(monkeypatch, tmp_path):
    import factory
    tid = f"vision-reliability-{uuid.uuid4().hex[:10]}"
    target = tmp_path / "SYSTEM-REQUIREMENTS.md"
    monkeypatch.setattr(mod, "_REQ_DOC", target)
    calls = []
    old_tenant = getattr(factory._ctx, "tenant", None)
    old_api = getattr(factory._ctx, "api_key", None)
    factory._ctx.tenant = "sentinel-tenant"
    factory._ctx.api_key = "sentinel-key"

    def fake_agent(*args, **kwargs):
        calls.append((args, kwargs, factory._ctx.tenant, factory._ctx.api_key))
        return {"rc": 0, "out_full": json.dumps(_reply())}

    monkeypatch.setattr(factory, "agent", fake_agent)
    try:
        result = mod.refine(tid, "meta", api_key="temporary-key")
        assert calls[0][1]["timeout"] <= 75 and calls[0][1]["retries"] == 0
        assert calls[0][2:] == (tid, "temporary-key")
        assert factory._ctx.tenant == "sentinel-tenant" and factory._ctx.api_key == "sentinel-key"
        text = target.read_text()
        marker = text.rsplit("<!-- requirements-digest: ", 1)[1].split(" -->", 1)[0]
        body = text.split("\n<!-- requirements-digest:", 1)[0]
        assert hashlib.sha256(body.encode()).hexdigest() == marker == result["_document_digest"]
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("SELECT requirements->>'_document_digest' FROM ceo_vision WHERE tenant_id=%s AND scope='meta'",
                        (tid,))
            assert cur.fetchone()[0] == marker
    finally:
        factory._ctx.tenant = old_tenant
        factory._ctx.api_key = old_api
        with tenant_connection(tid) as c, c.cursor() as cur:
            cur.execute("DELETE FROM ceo_vision WHERE tenant_id=%s", (tid,))


def test_atomic_doc_failure_preserves_previous_file(monkeypatch, tmp_path):
    target = tmp_path / "SYSTEM-REQUIREMENTS.md"
    target.write_text("known-good")
    monkeypatch.setattr(mod, "_REQ_DOC", target)
    monkeypatch.setattr(mod.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk fault")))
    try:
        mod._write_requirements_doc("vision", _reply())
    except OSError:
        pass
    else:
        raise AssertionError("filesystem failure must be visible")
    assert target.read_text() == "known-good"
    assert not list(tmp_path.glob(".SYSTEM-REQUIREMENTS.md.*"))


def test_parse_failure_keeps_prior_age_and_document_untouched(monkeypatch, tmp_path):
    import factory
    target = tmp_path / "SYSTEM-REQUIREMENTS.md"
    target.write_text("prior")
    monkeypatch.setattr(mod, "_REQ_DOC", target)
    prior = _reply()
    monkeypatch.setattr(mod, "get", lambda *_args, **_kwargs: {
        "vision": "vision", "requirements": prior, "age_s": 999})
    monkeypatch.setattr(factory, "agent", lambda *_args, **_kwargs: {"rc": 1, "out": "not json"})
    monkeypatch.setattr(mod, "_ensure", lambda: (_ for _ in ()).throw(AssertionError("must not persist failure")))
    assert mod.refine("t-x", "meta") == prior
    assert target.read_text() == "prior"
