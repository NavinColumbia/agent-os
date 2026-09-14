import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import assurance_outreach as outreach


def test_checked_queue_contains_only_valid_individual_email_targets():
    queue = outreach.load_queue()
    assert queue["schema"] == "agent-os.assurance-outreach/1"
    assert [item["id"] for item in queue["messages"]] == [
        "a-cubic-aymen", "yasmine", "leaping-ai", "n71"]
    assert all("{sample_url}" in item["body"] for item in queue["messages"])


def test_send_fails_before_reservation_without_mail_transport(monkeypatch):
    monkeypatch.setattr(outreach, "get", lambda _key: None)
    called = []
    monkeypatch.setattr(outreach, "_reserve", lambda *_args: called.append(True))
    result = outreach.send("yasmine")
    assert result == {"ok": False, "id": "yasmine", "error": "email transport is not configured"}
    assert called == []


def test_send_checks_public_links_and_records_transport_receipt(monkeypatch):
    config = {"AOS_SMTP_FROM": "Swami <sender@example.com>", "AOS_SMTP_URL": "smtp://configured"}
    monkeypatch.setattr(outreach, "get", config.get)
    checked = []
    monkeypatch.setattr(outreach, "_check_public", checked.append)
    monkeypatch.setattr(outreach, "_reserve", lambda *_args: {"ok": True, "attempts": 1})
    monkeypatch.setattr(outreach, "_smtp_send", lambda message: {
        "transport": "smtp", "id": "message-1"})
    finished = []
    monkeypatch.setattr(outreach, "_finish", lambda *args, **kwargs: finished.append((args, kwargs)))

    result = outreach.send("a-cubic-aymen")

    assert result["ok"] is True
    assert result["transport"] == "smtp"
    assert len(checked) == 2
    assert finished == [(('a-cubic-aymen',), {
        "status": "sent", "transport": "smtp", "transport_id": "message-1"})]


def test_sender_reuses_site_verified_address(monkeypatch):
    config = {"AOS_ASSURANCE_NOTIFY_FROM": "Agent OS <verified@example.org>"}
    monkeypatch.setattr(outreach, "get", config.get)
    value, name, address = outreach._sender()
    assert value == "Agent OS <verified@example.org>"
    assert name == "Agent OS"
    assert address == "verified@example.org"


def test_queue_rejects_header_injection(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text('''{"schema":"agent-os.assurance-outreach/1","messages":[{
        "id":"bad","recipient":"victim@example.com\\nBcc: all@example.com",
        "subject":"Hello","body":"Body"}]}''')
    try:
        outreach.load_queue(path)
    except ValueError as exc:
        assert "invalid recipient" in str(exc)
    else:
        raise AssertionError("header injection was accepted")
