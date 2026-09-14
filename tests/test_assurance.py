import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import assurance
import auth
import notify


def _valid():
    return {
        "name": "Avery Founder",
        "email": "avery@example.com",
        "company": "Example AI",
        "app_url": "https://staging.example.com/app",
        "access_mode": "temporary_test_account",
        "concern": "Signup, AI response, and billing upgrade must not create duplicate charges.",
        "consent": True,
        "website": "",
    }


def test_valid_pilot_intake_is_durable_and_returns_checkout_only_when_configured(monkeypatch):
    captured = []
    paged = []
    monkeypatch.setattr(assurance, "_insert", lambda lead: captured.append(lead) or {
        "id": lead["id"], "status": "new", "created": True})
    monkeypatch.setattr(assurance, "_configured_payment_url",
                        lambda: "https://buy.stripe.com/test_link")
    monkeypatch.setattr(notify, "send", lambda *args, **kwargs: paged.append((args, kwargs)) or True)

    result = assurance.submit(_valid())

    assert result["ok"] is True and result["next"] == "checkout"
    assert result["payment_url"] == "https://buy.stripe.com/test_link"
    assert result["request_id"].startswith("arp_")
    assert captured[0]["email"] == "avery@example.com"
    assert captured[0]["consent_version"] == assurance.CONSENT_VERSION
    assert paged and "private intake queue" in paged[0][0][0]
    assert "avery" not in paged[0][0][0].lower()


def test_intake_rejects_secrets_query_credentials_and_missing_consent(monkeypatch):
    called = []
    monkeypatch.setattr(assurance, "_insert", lambda lead: called.append(lead))
    body = _valid()
    body.update({
        "app_url": "https://staging.example.com/?token=private",
        "concern": "Use api_key=sk_live_abcdefghijklmnopqrstuvwxyz123456789 to test billing.",
        "consent": False,
    })

    result = assurance.submit(body)

    assert result["ok"] is False
    assert {"app_url", "concern", "consent"}.issubset(result["fields"])
    assert called == []


def test_honeypot_is_non_enumerating_and_never_persists(monkeypatch):
    called = []
    monkeypatch.setattr(assurance, "_insert", lambda lead: called.append(lead))
    body = _valid() | {"website": "https://spam.example"}

    assert assurance.submit(body) == {"ok": True, "request_id": "received", "next": "email"}
    assert called == []


def test_buyer_page_states_offer_price_scope_and_safety_boundary():
    page = assurance.page()

    assert "$500" in page and "$1,000/month" in page
    assert "up to three critical user journeys" in page.lower()
    assert "No production mutations" in page
    assert "Never paste credentials" in page
    assert "/api/assurance/intake" in page
    assert 'href="/assurance/sample"' in page
    assert auth.PUBLIC_RATE_POLICIES["assurance-intake"] == (3600, 8, 3)


def test_sample_report_matches_completed_gate_and_assets_are_allowlisted():
    page = assurance.sample_page()

    assert "12 of 12 stories passed" in page
    assert "zero open or blocking findings" in page
    assert "3 featured journeys" in page
    assert "Gate 1138" in page
    assert "full-product verdict remains pending" not in page.lower()
    assert "US-001" in page and "US-005" in page and "US-010" in page
    assert "US-003" not in page and "US-009" not in page
    assert assurance.sample_asset("public-trust-boundary.png").startswith(b"\x89PNG")
    assert assurance.sample_asset("../public-trust-boundary.png") is None
    assert assurance.sample_asset("unknown.png") is None


def test_sales_lead_schema_has_retention_and_no_tenant_operational_coupling():
    sql = assurance.MIGRATION.read_text()

    assert "delete_after" in sql and "interval '90 days'" in sql
    assert "dedupe_hash TEXT NOT NULL UNIQUE" in sql
    assert "tenant_id" not in sql
