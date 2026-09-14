#!/usr/bin/env python3
"""Fail-closed launch gate for the Release Assurance revenue funnel.

The gate never sends mail, creates a payment, or prints secret values. It
checks that the public funnel works and that the operator-supplied commercial
identity and credentials needed to turn a lead into revenue are present.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import assurance_outreach

DEFAULT_ENV = ROOT / "sites" / "release-assurance" / ".env.local"
PLACEHOLDER_MARKERS = (
    "replace-with", "change-me", "example.com", "[your ", "[jurisdiction]",
)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def configured(values: dict[str, str], key: str) -> str:
    value = str(os.environ.get(key) or values.get(key) or "").strip()
    if not value or any(marker in value.lower() for marker in PLACEHOLDER_MARKERS):
        return ""
    return value


def configuration_checks(values: dict[str, str]) -> list[dict]:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, owner: str = "system") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "owner": owner})

    payment = configured(values, "AOS_ASSURANCE_PAYMENT_URL")
    parsed_payment = urlparse(payment)
    payment_ok = parsed_payment.scheme == "https" and parsed_payment.hostname == "buy.stripe.com"
    add("$500 Stripe Payment Link", payment_ok,
        "configured" if payment_ok else "set AOS_ASSURANCE_PAYMENT_URL to a live buy.stripe.com link",
        "founder")

    sendgrid = configured(values, "SENDGRID_API_KEY")
    add("SendGrid transport", sendgrid.startswith("SG."),
        "configured" if sendgrid.startswith("SG.") else "set a live SG.* SENDGRID_API_KEY",
        "founder")
    sender = configured(values, "AOS_ASSURANCE_NOTIFY_FROM")
    recipient = configured(values, "AOS_ASSURANCE_NOTIFY_TO")
    add("verified sender", bool(EMAIL_RE.fullmatch(sender)),
        "configured" if EMAIL_RE.fullmatch(sender) else "set AOS_ASSURANCE_NOTIFY_FROM",
        "founder")
    add("lead notification inbox", bool(EMAIL_RE.fullmatch(recipient)),
        "configured" if EMAIL_RE.fullmatch(recipient) else "set AOS_ASSURANCE_NOTIFY_TO",
        "founder")

    legal_name = configured(values, "AOS_ASSURANCE_PROVIDER_LEGAL_NAME")
    jurisdiction = configured(values, "AOS_ASSURANCE_JURISDICTION")
    contact = configured(values, "AOS_ASSURANCE_PROVIDER_CONTACT")
    add("provider legal identity", len(legal_name) >= 2,
        "configured" if len(legal_name) >= 2 else "set AOS_ASSURANCE_PROVIDER_LEGAL_NAME",
        "founder")
    add("governing jurisdiction", len(jurisdiction) >= 2,
        "configured" if len(jurisdiction) >= 2 else "set AOS_ASSURANCE_JURISDICTION",
        "founder")
    add("provider contact", bool(EMAIL_RE.fullmatch(contact)),
        "configured" if EMAIL_RE.fullmatch(contact) else "set AOS_ASSURANCE_PROVIDER_CONTACT",
        "founder")

    try:
        queue = assurance_outreach.load_queue()
        messages = queue.get("messages") or []
        add("researched outbound queue", len(messages) > 0,
            f"{len(messages)} send-ready direct email(s)")
        offer = str(queue.get("public_offer") or "")
        sample = str(queue.get("public_sample") or "")
        links_ok = all(urlparse(item).scheme == "https" and urlparse(item).hostname
                       for item in (offer, sample))
        add("outbound public links", links_ok,
            "offer and sample use HTTPS" if links_ok else "queue offer/sample links are invalid")
    except Exception as exc:
        add("researched outbound queue", False,
            f"{type(exc).__name__}: {str(exc)[:160]}")
    return checks


def public_checks(base_url: str) -> list[dict]:
    base = base_url.rstrip("/")
    parsed = urlparse(base)
    checks: list[dict] = []
    if parsed.scheme != "https" or not parsed.hostname:
        return [{"check": "public sales URL", "ok": False,
                 "detail": "use an HTTPS public URL", "owner": "system"}]

    required_headers = {
        "content-security-policy": "frame-ancestors 'none'",
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
    }
    for path in ("/", "/sample", "/privacy", "/terms"):
        try:
            request = Request(base + path, headers={"User-Agent": "AgentOS-Launch-Preflight/1"})
            with urlopen(request, timeout=12) as response:
                headers = {key.lower(): value for key, value in response.headers.items()}
                response.read(128)
                missing = [name for name, expected in required_headers.items()
                           if expected.lower() not in headers.get(name, "").lower()]
                ok = response.status == 200 and not missing
                detail = "HTTP 200 with required security headers" if ok else (
                    f"HTTP {response.status}; invalid headers: {', '.join(missing)}")
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {str(exc)[:160]}"
        checks.append({"check": f"public {path}", "ok": ok, "detail": detail, "owner": "system"})

    try:
        with urlopen(Request(base + "/api/health", headers={
                "User-Agent": "AgentOS-Launch-Preflight/1"}), timeout=12) as response:
            body = json.loads(response.read())
            ok = response.status == 200 and body.get("ok") is True and body.get("version") == 2
            detail = "version 2 healthy" if ok else "unexpected health response"
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {str(exc)[:160]}"
    checks.append({"check": "public health", "ok": ok, "detail": detail, "owner": "system"})
    return checks


def evaluate(values: dict[str, str], *, base_url: str | None = None,
             check_public: bool = True) -> dict:
    checks = configuration_checks(values)
    if check_public:
        public_url = base_url or str(
            assurance_outreach.load_queue().get("public_offer") or ""
        )
        checks.extend(public_checks(public_url))
    founder_actions = [item["detail"] for item in checks
                       if not item["ok"] and item["owner"] == "founder"]
    system_gaps = [item["detail"] for item in checks
                   if not item["ok"] and item["owner"] == "system"]
    return {
        "ok": all(item["ok"] for item in checks),
        "state": "ready-to-sell" if all(item["ok"] for item in checks) else "blocked",
        "checks": checks,
        "founder_actions": founder_actions,
        "system_gaps": system_gaps,
        "note": "No email, payment, deployment, DNS, or external mutation was performed.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV))
    parser.add_argument("--public-url")
    parser.add_argument("--skip-public", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate(read_env(Path(args.env_file)), base_url=args.public_url,
                      check_public=not args.skip_public)
    report["env_file"] = str(Path(args.env_file).resolve())
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
