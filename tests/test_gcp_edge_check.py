from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gcp_edge_check", ROOT / "deploy" / "gcp" / "edge_check.py",
)
assert SPEC is not None and SPEC.loader is not None
edge_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(edge_check)


def test_edge_check_requires_two_exact_https_origins_on_the_reserved_ip():
    probes = []

    def probe(hostname: str, path: str, timeout: float) -> int:
        probes.append((hostname, path, timeout))
        return 204

    report = edge_check.check_once(
        "https://control.example.test",
        "https://apps.example.test",
        "203.0.113.10",
        resolver=lambda _: {"203.0.113.10", "2001:db8::10"},
        probe=probe,
        probe_timeout_seconds=7,
    )

    assert report["ok"] is True
    assert probes == [
        ("control.example.test", "/ready", 7),
        ("apps.example.test", "/health", 7),
    ]


def test_edge_check_fails_closed_on_wrong_dns_redirect_or_invalid_origin():
    wrong_dns = edge_check.check_once(
        "https://control.example.test", "https://apps.example.test", "203.0.113.10",
        resolver=lambda _: {"203.0.113.11"},
        probe=lambda *_: 200,
    )
    assert wrong_dns["ok"] is False
    assert all(item["health_ok"] is False for item in wrong_dns["checks"])

    redirect = edge_check.check_once(
        "https://control.example.test", "https://apps.example.test", "203.0.113.10",
        resolver=lambda _: {"203.0.113.10"},
        probe=lambda *_: 302,
    )
    assert redirect["ok"] is False
    with pytest.raises(ValueError, match="HTTPS origin"):
        edge_check.check_once(
            "https://user:secret@control.example.test/path",
            "https://apps.example.test",
            "203.0.113.10",
        )
    with pytest.raises(ValueError, match="HTTPS origin"):
        edge_check.check_once(
            "https://localhost", "https://apps.example.test", "203.0.113.10",
        )


def test_edge_wait_reports_progress_and_completes_after_dns_and_health_converge():
    clock = [0.0]
    progress = []

    def advance(seconds: float) -> None:
        clock[0] += seconds

    report = edge_check.wait_until_ready(
        "https://control.example.test",
        "https://apps.example.test",
        "203.0.113.10",
        timeout_seconds=20,
        interval_seconds=5,
        monotonic=lambda: clock[0],
        sleep=advance,
        resolver=lambda _: {"203.0.113.11"} if clock[0] < 5 else {"203.0.113.10"},
        probe=lambda *_: 200,
        progress=progress.append,
    )

    assert report["ok"] is True
    assert report["attempt"] == 2
    assert len(progress) == 1


def test_dns_only_mode_does_not_probe_services_before_they_exist():
    report = edge_check.wait_until_ready(
        "https://control.example.test",
        "https://apps.example.test",
        "203.0.113.10",
        timeout_seconds=0,
        interval_seconds=1,
        dns_only=True,
        resolver=lambda _: {"203.0.113.10"},
        probe=lambda *_: (_ for _ in ()).throw(AssertionError("must not probe")),
    )
    assert report["ok"] is True
    assert report["mode"] == "dns"
