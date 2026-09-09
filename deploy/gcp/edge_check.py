#!/usr/bin/env python3
"""Read-only DNS, TLS, and health gate for the Agent OS public edge."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import socket
import ssl
import sys
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse


def _public_hostname(value: str) -> bool:
    if not value.isascii() or len(value) > 253 or "." not in value or value.endswith("."):
        return False
    return all(
        1 <= len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in label)
        for label in value.rstrip(".").split(".")
    )


def _origin(value: str) -> tuple[str, int]:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("public URL has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or not _public_hostname(parsed.hostname)
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is not None
    ):
        raise ValueError("public URL must be an HTTPS origin without credentials or a path")
    return parsed.hostname, 443


def _resolve(hostname: str) -> set[str]:
    return {
        str(item[4][0])
        for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    }


def _probe(hostname: str, path: str, timeout_seconds: float) -> int:
    connection = http.client.HTTPSConnection(
        hostname,
        443,
        timeout=timeout_seconds,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "GET", path,
            headers={"User-Agent": "agent-os-edge-readiness/1", "Connection": "close"},
        )
        response = connection.getresponse()
        response.read(4096)
        return response.status
    finally:
        connection.close()


def check_once(
    api_url: str,
    apps_url: str,
    expected_ip: str,
    *,
    dns_only: bool = False,
    probe_timeout_seconds: float = 10,
    resolver: Callable[[str], set[str]] = _resolve,
    probe: Callable[[str, str, float], int] = _probe,
) -> dict[str, Any]:
    expected = str(ipaddress.IPv4Address(expected_ip))
    endpoints = ((api_url, "/ready"), (apps_url, "/health"))
    checks: list[dict[str, Any]] = []
    for url, path in endpoints:
        hostname, _ = _origin(url)
        try:
            addresses = resolver(hostname)
            dns_ok = expected in addresses
            dns_detail = sorted(addresses)
        except OSError as exc:
            dns_ok = False
            dns_detail = [f"{type(exc).__name__}: {str(exc)[:160]}"]
        item: dict[str, Any] = {
            "hostname": hostname,
            "expected_ipv4": expected,
            "resolved": dns_detail,
            "dns_ok": dns_ok,
        }
        if dns_ok and not dns_only:
            try:
                status = probe(hostname, path, probe_timeout_seconds)
                item.update({
                    "path": path,
                    "http_status": status,
                    "health_ok": 200 <= status < 300,
                })
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                item.update({
                    "path": path,
                    "http_status": None,
                    "health_ok": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                })
        elif not dns_only:
            item.update({"path": path, "http_status": None, "health_ok": False})
        checks.append(item)
    required = ("dns_ok",) if dns_only else ("dns_ok", "health_ok")
    return {
        "ok": all(all(check.get(field) is True for field in required) for check in checks),
        "mode": "dns" if dns_only else "dns_tls_health",
        "checks": checks,
    }


def wait_until_ready(
    api_url: str,
    apps_url: str,
    expected_ip: str,
    *,
    timeout_seconds: float,
    interval_seconds: float,
    dns_only: bool = False,
    probe_timeout_seconds: float = 10,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    resolver: Callable[[str], set[str]] = _resolve,
    probe: Callable[[str, str, float], int] = _probe,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if timeout_seconds < 0 or interval_seconds <= 0:
        raise ValueError("timeout must be nonnegative and interval must be positive")
    deadline = monotonic() + timeout_seconds
    attempt = 0
    while True:
        attempt += 1
        report = check_once(
            api_url,
            apps_url,
            expected_ip,
            dns_only=dns_only,
            probe_timeout_seconds=probe_timeout_seconds,
            resolver=resolver,
            probe=probe,
        )
        report["attempt"] = attempt
        if report["ok"] or monotonic() >= deadline:
            return report
        if progress is not None:
            progress(json.dumps(report, separators=(",", ":"), sort_keys=True))
        sleep(min(interval_seconds, max(0, deadline - monotonic())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--apps-url", required=True)
    parser.add_argument("--expected-ip", required=True)
    parser.add_argument("--dns-only", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument("--interval-seconds", type=float, default=15)
    parser.add_argument("--probe-timeout-seconds", type=float, default=10)
    args = parser.parse_args(argv)
    try:
        report = wait_until_ready(
            args.api_url,
            args.apps_url,
            args.expected_ip,
            timeout_seconds=args.timeout_seconds,
            interval_seconds=args.interval_seconds,
            dns_only=args.dns_only,
            probe_timeout_seconds=args.probe_timeout_seconds,
            progress=lambda value: print(f"edge not ready: {value}", file=sys.stderr),
        )
    except ValueError as exc:
        report = {"ok": False, "error": str(exc)}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
