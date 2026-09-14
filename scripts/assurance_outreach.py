#!/usr/bin/env python3
"""Send the small, researched Release Assurance queue with durable duplicate protection.

This is intentionally not a bulk-mailer. Every target is committed to the checked-in queue, one row is
reserved before transport, and an interrupted ``sending`` row fails closed for manual reconciliation. The
public offer/sample are checked immediately before a send so an expired preview link is never mailed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import requests

from aoscfg import get
from dbpool import connection

QUEUE = Path(__file__).resolve().parents[1] / "docs" / "assurance" / "outreach-send-queue.json"
USER_AGENT = "AgentOS-Release-Assurance/1"


def load_queue(path=QUEUE):
    data = json.loads(Path(path).read_text())
    if data.get("schema") != "agent-os.assurance-outreach/1":
        raise ValueError("unsupported outreach queue schema")
    ids = set()
    for item in data.get("messages") or []:
        target_id = str(item.get("id") or "").strip()
        recipient = str(item.get("recipient") or "").strip()
        subject = str(item.get("subject") or "").strip()
        body = str(item.get("body") or "").strip()
        if not target_id or target_id in ids:
            raise ValueError("every outreach target needs a unique id")
        ids.add(target_id)
        if "\n" in recipient or "\r" in recipient or parseaddr(recipient)[1] != recipient:
            raise ValueError(f"invalid recipient for {target_id}")
        if not subject or "\n" in subject or "\r" in subject or not body:
            raise ValueError(f"invalid message for {target_id}")
    return data


def _ensure():
    with connection() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS assurance_outreach_delivery (
            target_id TEXT PRIMARY KEY,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('sending','sent','failed')),
            attempts INTEGER NOT NULL DEFAULT 1,
            intent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            sent_at TIMESTAMPTZ,
            transport TEXT,
            transport_id TEXT,
            error TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")


def _content_hash(item, sample_url):
    raw = json.dumps({
        "recipient": item["recipient"], "subject": item["subject"],
        "body": item["body"].format(sample_url=sample_url),
    }, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _reserve(item, sample_url):
    """Reserve exactly one outbound attempt; sent/sending rows never auto-replay."""
    _ensure()
    digest = _content_hash(item, sample_url)
    with connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT status,content_hash,attempts FROM assurance_outreach_delivery WHERE target_id=%s FOR UPDATE",
                    (item["id"],))
        row = cur.fetchone()
        if row and row[0] in {"sending", "sent"}:
            return {"ok": False, "status": row[0], "reason": "already reserved or sent"}
        if row and row[1] != digest:
            return {"ok": False, "status": row[0],
                    "reason": "message changed after a prior attempt; choose a new target id"}
        if row:
            cur.execute("""UPDATE assurance_outreach_delivery
                           SET status='sending', attempts=attempts+1, intent_at=now(), updated_at=now(), error=NULL
                           WHERE target_id=%s""", (item["id"],))
            attempts = int(row[2]) + 1
        else:
            cur.execute("""INSERT INTO assurance_outreach_delivery
                           (target_id,recipient,subject,content_hash,status)
                           VALUES (%s,%s,%s,%s,'sending')""",
                        (item["id"], item["recipient"], item["subject"], digest))
            attempts = 1
    return {"ok": True, "status": "sending", "attempts": attempts, "content_hash": digest}


def _finish(target_id, *, status, transport=None, transport_id=None, error=None):
    with connection() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE assurance_outreach_delivery
                       SET status=%s, sent_at=CASE WHEN %s='sent' THEN now() ELSE sent_at END,
                           transport=%s, transport_id=%s, error=%s, updated_at=now()
                       WHERE target_id=%s""",
                    (status, status, transport, transport_id, str(error or "")[:1000] or None, target_id))


def _sender():
    # Reuse the Site's verified SendGrid sender by default so launch needs one
    # sender identity, not two differently named copies of the same setting.
    value = str(get("AOS_SMTP_FROM") or get("AOS_ASSURANCE_NOTIFY_FROM") or "").strip()
    name, address = parseaddr(value)
    if not address or "@" not in address or "\n" in value or "\r" in value:
        raise RuntimeError(
            "AOS_SMTP_FROM or AOS_ASSURANCE_NOTIFY_FROM must be a real sender address"
        )
    return value, name, address


def _check_public(url):
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"public sales URL returned HTTP {response.status}")
        response.read(128)


def _smtp_send(message):
    raw_url = str(get("AOS_SMTP_URL") or "").strip()
    if not raw_url:
        return None
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"smtp", "smtps"} or not parsed.hostname:
        raise RuntimeError("AOS_SMTP_URL must use smtp:// or smtps://")
    port = parsed.port or (465 if parsed.scheme == "smtps" else 587)
    if parsed.scheme == "smtps":
        client = smtplib.SMTP_SSL(parsed.hostname, port, timeout=15,
                                  context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(parsed.hostname, port, timeout=15)
        client.starttls(context=ssl.create_default_context())
    try:
        if parsed.username:
            client.login(unquote(parsed.username), unquote(parsed.password or ""))
        refused = client.send_message(message)
        if refused:
            raise RuntimeError("SMTP refused one or more recipients")
    finally:
        try:
            client.quit()
        except Exception:
            client.close()
    return {"transport": "smtp", "id": message.get("Message-ID")}


def _sendgrid_send(message, sender_address):
    key = str(get("SENDGRID_API_KEY") or "").strip()
    if not key:
        return None
    response = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {key}"},
        json={"personalizations": [{"to": [{"email": message["To"]}]}],
              "from": {"email": sender_address}, "subject": message["Subject"],
              "content": [{"type": "text/plain", "value": message.get_content()}]},
        timeout=15,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"SendGrid rejected delivery with HTTP {response.status_code}")
    return {"transport": "sendgrid", "id": response.headers.get("X-Message-Id")}


def status():
    queue = load_queue()
    _ensure()
    with connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT target_id,status,attempts,sent_at,transport,error
                       FROM assurance_outreach_delivery ORDER BY target_id""")
        deliveries = [dict(zip(("id", "status", "attempts", "sent_at", "transport", "error"), row))
                      for row in cur.fetchall()]
    return {"queued": len(queue["messages"]), "deliveries": deliveries}


def send(target_id):
    queue = load_queue()
    item = next((entry for entry in queue["messages"] if entry["id"] == target_id), None)
    if not item:
        return {"ok": False, "id": target_id, "error": "unknown target"}
    if not (get("AOS_SMTP_URL") or get("SENDGRID_API_KEY")):
        return {"ok": False, "id": target_id, "error": "email transport is not configured"}
    sender_value, _sender_name, sender_address = _sender()
    try:
        _check_public(queue["public_offer"])
        _check_public(queue["public_sample"])
    except Exception as exc:
        return {"ok": False, "id": target_id, "error": str(exc)}
    reserved = _reserve(item, queue["public_sample"])
    if not reserved["ok"]:
        return {"ok": False, "id": target_id, **reserved}
    message = EmailMessage()
    message["From"] = sender_value
    message["To"] = item["recipient"]
    message["Subject"] = item["subject"]
    message.set_content(item["body"].format(sample_url=queue["public_sample"]))
    try:
        receipt = _smtp_send(message) or _sendgrid_send(message, sender_address)
        if not receipt:
            raise RuntimeError("email transport is not configured")
        _finish(target_id, status="sent", transport=receipt["transport"], transport_id=receipt.get("id"))
        return {"ok": True, "id": target_id, "recipient": item["recipient"],
                "transport": receipt["transport"], "attempts": reserved["attempts"]}
    except Exception as exc:
        _finish(target_id, status="failed", error=exc)
        return {"ok": False, "id": target_id, "error": str(exc), "status": "failed"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    one = sub.add_parser("send")
    one.add_argument("target_id")
    args = parser.parse_args(argv)
    result = status() if args.command == "status" else send(args.target_id)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
