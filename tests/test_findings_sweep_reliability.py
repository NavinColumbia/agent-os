from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import findings_sweep as fs  # noqa: E402


def _finding(fid=1):
    return {
        "id": fid,
        "severity": "critical",
        "_rank": 0,
        "area": "test",
        "title": "critical remains visible",
        "status": "asked",
        "age_days": 10,
        "parked": False,
        "overdue": True,
    }


def test_escalation_cooldown_is_timestamped_and_legacy_safe(monkeypatch):
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    monkeypatch.setattr(fs, "COOLDOWN_DAYS", 7)
    recent = {1: "\n[asked] [escalated 2026-08-15T00:00:00+00:00] accepted"}
    old = {1: "\n[asked] [escalated 2026-08-01T00:00:00+00:00] accepted"}
    assert fs._already_escalated(1, recent, now=now)
    assert not fs._already_escalated(1, old, now=now)
    assert fs._already_escalated(1, {1: "\n[asked] [escalated] legacy"}, now=now)


def test_failed_delivery_never_marks_finding_escalated(monkeypatch):
    notes = []
    monkeypatch.setattr(fs, "open_findings", lambda **_kwargs: [_finding()])
    monkeypatch.setattr(fs, "_load_notes", lambda _items: {})
    monkeypatch.setattr(fs, "_already_escalated", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(fs, "_note", lambda fid, note: notes.append((fid, note)) or True)

    result = fs.sweep(notify_fn=lambda _text: False)
    assert result["attempted"] == 1
    assert result["escalated"] == 0
    assert result["notified"] is False
    assert notes == []


def test_accepted_delivery_records_timestamped_marker(monkeypatch):
    notes = []
    monkeypatch.setattr(fs, "open_findings", lambda **_kwargs: [_finding(7)])
    monkeypatch.setattr(fs, "_load_notes", lambda _items: {})
    monkeypatch.setattr(fs, "_already_escalated", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(fs, "_note", lambda fid, note: notes.append((fid, note)) or True)

    result = fs.sweep(notify_fn=lambda _text: True)
    assert result["escalated"] == 1
    assert result["notified"] is True
    assert len(notes) == 1 and notes[0][0] == 7
    assert notes[0][1].startswith("[escalated 20")


def test_default_delivery_is_tenant_scoped_and_content_is_not_operator_payload(monkeypatch):
    finding = _finding(11)
    finding["tenant"] = "t-acme"
    monkeypatch.setattr(fs, "open_findings", lambda **_kwargs: [finding])
    monkeypatch.setattr(fs, "_load_notes", lambda _items: {})
    monkeypatch.setattr(fs, "_note", lambda *_args: True)
    calls = []

    class Notifications:
        @staticmethod
        def send(*args, **kwargs):
            calls.append((args, kwargs))
            return {"id": 77, "duplicate": False}

    monkeypatch.setitem(sys.modules, "notifications", Notifications)
    result = fs.sweep()
    assert result["ids"] == [11]
    assert calls[0][0][0] == "t-acme"
    assert calls[0][1]["context_key"] == "finding-escalation:11:0"
    assert calls[0][1]["level"] == "urgent"


def test_sql_orders_before_limit_so_old_critical_cannot_hide(monkeypatch):
    captured = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, args):
            captured["query"] = query
            captured["args"] = args

        def fetchall(self):
            return []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return Cursor()

    class Psycopg:
        @staticmethod
        def connect(_dsn):
            return Conn()

    monkeypatch.setitem(sys.modules, "psycopg", Psycopg)
    fs.open_findings(limit=200)
    normalized = " ".join(captured["query"].split()).lower()
    assert "case when title" in normalized
    assert normalized.index("case when title") < normalized.index("limit %s")
    assert "created_at asc" in normalized


def test_scheduled_sweep_does_not_report_empty_on_database_failure(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(fs, "open_findings", broken)
    try:
        fs.sweep(notify_fn=lambda _text: True)
    except RuntimeError as exc:
        assert "database unavailable" in str(exc)
    else:
        raise AssertionError("scheduler must see database blindness as failure")
