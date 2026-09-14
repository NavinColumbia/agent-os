import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import assurance_launch_preflight as preflight


def complete_values():
    return {
        "AOS_ASSURANCE_PAYMENT_URL": "https://buy.stripe.com/live_500",
        "SENDGRID_API_KEY": "SG.live-value",
        "AOS_ASSURANCE_NOTIFY_FROM": "verified@agentos.test",
        "AOS_ASSURANCE_NOTIFY_TO": "founder@agentos.test",
        "AOS_ASSURANCE_PROVIDER_LEGAL_NAME": "Agent OS LLC",
        "AOS_ASSURANCE_JURISDICTION": "California, USA",
        "AOS_ASSURANCE_PROVIDER_CONTACT": "founder@agentos.test",
    }


def test_configuration_gate_is_ready_without_exposing_secrets(monkeypatch):
    monkeypatch.setattr(preflight.assurance_outreach, "load_queue", lambda: {
        "public_offer": "https://offer.test/",
        "public_sample": "https://offer.test/sample",
        "messages": [{"id": "one"}, {"id": "two"}, {"id": "three"}],
    })
    report = preflight.evaluate(complete_values(), check_public=False)
    assert report["ok"] is True
    assert report["state"] == "ready-to-sell"
    assert report["founder_actions"] == []
    assert "SG.live-value" not in str(report)


def test_gate_reports_only_concrete_founder_actions(monkeypatch):
    monkeypatch.setattr(preflight.assurance_outreach, "load_queue", lambda: {
        "public_offer": "https://offer.test/",
        "public_sample": "https://offer.test/sample",
        "messages": [{"id": "one"}],
    })
    report = preflight.evaluate({}, check_public=False)
    assert report["ok"] is False
    assert report["state"] == "blocked"
    assert len(report["founder_actions"]) == 7
    assert report["system_gaps"] == []


def test_public_gate_rejects_non_https_without_network():
    result = preflight.public_checks("http://example.test")
    assert result == [{
        "check": "public sales URL", "ok": False,
        "detail": "use an HTTPS public URL", "owner": "system",
    }]
