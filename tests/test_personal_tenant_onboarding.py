from __future__ import annotations

from dataclasses import dataclass
import time

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt

from agent_os.api.app import create_app
from agent_os.api.auth import OIDCTokenIdentity
from agent_os.infrastructure.memory import InMemoryWorkflowEngine
from agent_os.infrastructure.sql_company_directory import SQLCompanyDirectory


@dataclass
class SigningKey:
    key: object


class SigningKeys:
    def __init__(self, key: object) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, token: str) -> SigningKey:
        del token
        return SigningKey(self.key)


def token(private_key: object, subject: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": subject,
            "iss": "https://identity.example.test",
            "aud": "agent-os-api",
            "iat": now - 1,
            "exp": now + 300,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "onboarding-proof-key"},
    )


def test_first_oidc_login_enters_an_isolated_company_and_can_start_work(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    identity = OIDCTokenIdentity(
        issuer="https://identity.example.test",
        audience="agent-os-api",
        jwks_url="https://identity.example.test/.well-known/jwks.json",
        algorithms=("RS256",),
        leeway_seconds=0,
        signing_keys=SigningKeys(private_key.public_key()),
        personal_tenant_secret="personal-company-proof-secret-32bytes",
    )
    company = SQLCompanyDirectory(
        f"sqlite:///{tmp_path / 'company.sqlite3'}", create_schema=True,
    )
    client = TestClient(create_app(
        engine=InMemoryWorkflowEngine(),
        identity=identity,
        company_directory=company,
    ))
    first_headers = {
        "Authorization": f"Bearer {token(private_key, 'first-user')}",
        "X-Organization-Id": "forged-tenant",
    }
    second_headers = {
        "Authorization": f"Bearer {token(private_key, 'second-user')}",
        "X-Organization-Id": "forged-tenant",
    }

    try:
        first_company = client.get("/v2/company/organization", headers=first_headers)
        second_company = client.get("/v2/company/organization", headers=second_headers)
        assert first_company.status_code == second_company.status_code == 200
        first_tenant = first_company.json()["tenant_id"]
        second_tenant = second_company.json()["tenant_id"]
        assert first_tenant.startswith("tenant-")
        assert second_tenant.startswith("tenant-")
        assert first_tenant != second_tenant
        assert "forged-tenant" not in {first_tenant, second_tenant}

        started = client.post(
            "/v2/runs",
            headers={**first_headers, "Idempotency-Key": "personal-first-mission"},
            json={"prompt": "Build a useful product", "title": "First mission"},
        )
        assert started.status_code == 202
        assert len(client.get("/v2/runs", headers=first_headers).json()["items"]) == 1
        assert client.get("/v2/runs", headers=second_headers).json()["items"] == []
    finally:
        client.close()
        company.close()
