import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_request
import auth


def test_provider_fact_closes_only_provider_credential_requests(monkeypatch):
    tenant = f"provider-request-{uuid.uuid4().hex}"
    provider_id = other_id = None
    try:
        with agent_request.connection() as c, c.cursor() as cur:
            cur.execute("""INSERT INTO agent_requests
                              (tenant_id,kind,question,status,correlation_id)
                           VALUES (%s,'credential','Connect an Anthropic or Codex model provider','open',%s)
                           RETURNING id""", (tenant, f"controller:{uuid.uuid4().hex}:provider"))
            provider_id = cur.fetchone()[0]
            cur.execute("""INSERT INTO agent_requests
                              (tenant_id,kind,question,status)
                           VALUES (%s,'credential','Add the Stripe live key','open') RETURNING id""",
                        (tenant,))
            other_id = cur.fetchone()[0]
        monkeypatch.setattr(auth, "provider_resolved", lambda tid: tid == tenant)
        monkeypatch.setitem(sys.modules, "notifications", type("Notifications", (), {
            "resolve": staticmethod(lambda *_a, **_k: True)})())

        assert agent_request.reconcile_satisfied_provider_requests(limit=10) == [provider_id]
        assert agent_request.get(provider_id, tenant_id=tenant)["status"] == "answered"
        assert agent_request.get(other_id, tenant_id=tenant)["status"] == "open"
        assert agent_request.reconcile_satisfied_provider_requests(limit=10) == []
    finally:
        with agent_request.connection() as c, c.cursor() as cur:
            cur.execute("DELETE FROM agent_requests WHERE tenant_id=%s", (tenant,))
