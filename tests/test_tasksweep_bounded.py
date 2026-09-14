import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import tasksweep as mod  # noqa: E402


class _Cursor:
    def __init__(self):
        self.queries = []

    def execute(self, query, args=()):
        self.queries.append((" ".join(query.split()).lower(), args))

    def fetchall(self):
        return [(n,) for n in range(7)]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_reclaim_is_bounded_skip_locked_and_reports_tail(monkeypatch):
    cursor = _Cursor()
    monkeypatch.setattr(mod, "_depths", lambda: (5000, 3))
    monkeypatch.setattr(mod, "connection", lambda: _Conn(cursor))
    result = mod.sweep(limit=7)
    update = next(q for q, _args in cursor.queries if "update tasks t" in q)
    assert "for update skip locked" in update
    assert "limit %s" in update
    assert result == {"reclaimed": 7, "reclaimable": 5000, "remaining_reclaimable": 4993,
                      "dead_letter_depth": 3, "batch_limit": 7}


def test_dry_run_never_opens_update_transaction(monkeypatch):
    monkeypatch.setattr(mod, "_depths", lambda: (12, 0))
    monkeypatch.setattr(mod, "connection",
                        lambda: (_ for _ in ()).throw(AssertionError("must not connect")))
    result = mod.sweep(dry=True, limit=5)
    assert result["reclaimed"] == 0 and result["remaining_reclaimable"] == 12
