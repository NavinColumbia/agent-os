import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import accountability  # noqa: E402
import process_assurance  # noqa: E402
import reap  # noqa: E402


def _handoff(turn, *, conversation="conv", tenant="tenant-a", context=None, truncated=False):
    return {"conversation": conversation, "turn": turn, "from": "reviewer", "to": "builder",
            "intent": "review_request", "message_id": f"message-{turn}",
            "ts": "2026-08-01T00:00:00+00:00", "tenant_id": tenant,
            "sender_tenant": tenant, "recipient_tenant": tenant,
            "sender_product": "product-a", "recipient_product": "product-a",
            "context": list(context or []), "context_truncated": truncated}


def _wait(index, *, tenant="tenant-a"):
    return {"waiter": f"worker-{index}", "awaited": "lead", "reply_by": f"deadline-{index}",
            "tenant_id": tenant, "waiter_tenant": tenant, "awaited_tenant": tenant,
            "waiter_product": "product-a", "awaited_product": "product-a"}


def _install_sweep_fakes(monkeypatch, handoffs, waits, *, next_stream="handoffs"):
    saved = []
    calls = []
    monkeypatch.setattr(accountability, "_load_sweep_state", lambda: {
        "handoff": ("old", 9), "wait": ("old-w", "old-a"),
        "next_stream": next_stream, "version": 7})

    def handoff_page(cursor, limit):
        calls.append(("handoffs", cursor, limit))
        return {"items": list(handoffs)[:limit],
                "cursor": ((handoffs[min(len(handoffs), limit) - 1]["conversation"],
                            handoffs[min(len(handoffs), limit) - 1]["turn"])
                           if handoffs and limit else cursor), "wrapped": False}

    def wait_page(cursor, limit):
        calls.append(("waits", cursor, limit))
        return {"items": list(waits)[:limit],
                "cursor": ((waits[min(len(waits), limit) - 1]["waiter"],
                            waits[min(len(waits), limit) - 1]["awaited"])
                           if waits and limit else cursor), "wrapped": False}

    monkeypatch.setattr(accountability, "_handoff_page", handoff_page)
    monkeypatch.setattr(accountability, "_wait_page", wait_page)
    monkeypatch.setattr(accountability, "_save_sweep_state",
                        lambda *args: saved.append(args))
    return calls, saved


def test_accountability_has_one_global_budget_and_advances_both_tails(monkeypatch):
    handoffs = [_handoff(i) for i in range(1, 10)]
    waits = [_wait(i) for i in range(1, 10)]
    calls, saved = _install_sweep_fakes(monkeypatch, handoffs, waits)
    routed = []

    result = accountability.sweep(
        limit=5, management_fn=lambda *args, **kwargs: routed.append((args, kwargs)) or {})

    assert calls == [("handoffs", ("old", 9), 3), ("waits", ("old-w", "old-a"), 2)]
    assert result["scanned"] == {"handoffs": 3, "waits": 2}
    assert sum(result["scanned"].values()) <= result["budget"] == 5
    assert result["routed"] == 5 and len(routed) == 5
    assert saved and saved[0][1] == ("conv", 3) and saved[0][2] == ("worker-2", "lead")


def test_one_item_accountability_budget_alternates_streams_durably():
    assert accountability._stream_budgets(1, "handoffs") == (1, 0, "waits")
    assert accountability._stream_budgets(1, "waits") == (0, 1, "handoffs")


def test_first_handoff_failure_does_not_starve_later_item_or_cursor(monkeypatch):
    handoffs = [_handoff(10), _handoff(11)]
    _calls, saved = _install_sweep_fakes(monkeypatch, handoffs, [])
    attempted = []

    def route(*args, **kwargs):
        attempted.append(args[0])
        if len(attempted) == 1:
            raise RuntimeError("first route unavailable")
        return {}

    result = accountability.sweep(limit=4, management_fn=route)

    assert attempted and len(attempted) == 2
    assert result["status"] == "degraded" and result["routed"] == 1
    assert any(error["component"] == "handoff_route" for error in result["errors"])
    assert saved[0][1] == ("conv", 11), "failed prefix item must not pin durable tail progress"


def test_truncated_resolution_context_is_unknown_not_a_false_drop(monkeypatch):
    _install_sweep_fakes(monkeypatch, [_handoff(1, truncated=True)], [])
    result = accountability.sweep(limit=2, management_fn=lambda *_a, **_k: {})
    assert result["dropped"] == 0 and result["unknown_context"] == 1
    assert result["status"] == "degraded"


def test_generic_resolver_is_consumed_once_within_bounded_page():
    resolver = {"turn": 3, "sender": "builder", "intent": "done",
                "message_id": "done-1", "in_reply_to": None}
    dropped, unknown = accountability._classify_handoffs(
        [_handoff(1, context=[resolver]), _handoff(2, context=[resolver])])
    assert not unknown and [item["turn"] for item in dropped] == [2]


def test_management_routing_is_tenant_owned_and_semantically_stable():
    calls = []
    item = _handoff(8)
    item["age_min"] = 31
    for age in (31, 999):
        item["age_min"] = age
        accountability._route_management(
            item, "handoff", management_fn=lambda *a, **k: calls.append((a, k)) or {})
    assert calls[0][0][0] == calls[1][0][0], "changing age must not create a new semantic case"
    assert calls[0][1]["tenant_id"] == "tenant-a"
    assert calls[0][1]["product"] == "product-a"
    assert "age_min" not in calls[0][0][3]


def test_unscoped_or_mixed_tenant_item_is_not_routed(monkeypatch):
    item = _handoff(1)
    item["recipient_tenant"] = "tenant-b"
    _install_sweep_fakes(monkeypatch, [item], [])
    routed = []
    result = accountability.sweep(
        limit=2, management_fn=lambda *a, **k: routed.append((a, k)) or {})
    assert routed == []
    assert result["status"] == "degraded"
    assert any(error["component"] == "handoff_route" for error in result["errors"])


def test_accountability_db_blindness_is_explicit_and_cli_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(accountability, "_load_sweep_state",
                        lambda: (_ for _ in ()).throw(RuntimeError("database offline")))
    result = accountability.sweep()
    assert result["status"] == "degraded" and result["dropped"] == 0
    monkeypatch.setattr(accountability, "sweep", lambda **_kwargs: result)
    assert accountability._main(["sweep"]) == 2
    assert '"status": "degraded"' in capsys.readouterr().out


def test_accountability_query_and_cursor_contracts_are_bounded():
    source = (ROOT / "scripts" / "accountability.py").read_text()
    migration = (ROOT / "postgres" / "initdb" / "71-accountability-sweep-cursors.sql").read_text()
    assert "LIMIT %s" in source and "statement_timeout" in source and "lock_timeout" in source
    assert "LEFT JOIN LATERAL" in source and "int(context_limit) + 1" in source
    assert "COALESCE(c.tenant_id,ds.tenant_id,dr.tenant_id) IS NOT NULL" in source
    assert "COALESCE(w.tenant_id,dw.tenant_id,da.tenant_id) IS NOT NULL" in source
    assert "AND version=%s" in source
    assert "CREATE TABLE IF NOT EXISTS accountability_sweep_state" in migration
    assert "conversations_accountability_request_idx" in migration


def test_reap_process_and_registry_scans_report_truncation(monkeypatch, tmp_path):
    (tmp_path / "uptime").write_text("1000 0")
    for pid in ("10", "11", "12"):
        (tmp_path / pid).mkdir()
    identity = process_assurance.ProcessIdentity(10, 1, "boot")
    snapshot = process_assurance.ProcessSnapshot(identity, 1, 10, os.getuid(), "worker")
    monkeypatch.setattr(reap.process_assurance, "read_snapshot", lambda *_a, **_k: snapshot)
    scan = reap._scan_process_table(limit=2, proc_root=tmp_path)
    assert scan["scanned"] == 2 and scan["truncated"] is True

    registry = tmp_path / "registry"
    registry.mkdir()
    for i in range(3):
        (registry / f"{i}.json").write_text("{}")
    records = reap._registered_browser_records(registry, limit=2)
    assert records["scanned"] == 2 and records["truncated"] is True


def test_scratch_scan_and_recursive_inventory_are_hard_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(reap, "SCRATCH_GLOBS", ["item-*"])
    monkeypatch.setattr(reap, "SCRATCH_VISIT_LIMIT", 2)
    for i in range(3):
        path = tmp_path / f"item-{i}"
        path.write_text("x")
        os.utime(path, (0, 0))
    result = reap._clean_scratch(dry=True, tmp=tmp_path)
    assert result["scanned"] == 2 and result["truncated"] is True

    tree = tmp_path / "tree"
    tree.mkdir()
    for i in range(3):
        (tree / str(i)).write_text("x")
    assert reap._bounded_tree_inventory(tree, limit=2) is None
    assert tree.exists(), "oversized scratch trees must be left intact, not partially removed"


def test_unrelated_scratch_does_not_starve_bounded_owned_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr(reap, "SCRATCH_GLOBS", ["owned-*"])
    monkeypatch.setattr(reap, "SCRATCH_VISIT_LIMIT", 20)
    monkeypatch.setattr(reap, "SCRATCH_SCAN_LIMIT", 2)
    for i in range(8):
        (tmp_path / f"unrelated-{i}").write_text("x")
    for i in range(3):
        path = tmp_path / f"owned-{i}"
        path.write_text("x")
        os.utime(path, (i, i))
    result = reap._clean_scratch(dry=True, tmp=tmp_path)
    assert result["truncated"] is False
    assert result["matched"] == 3 and result["deferred"] == 1
    assert result["removed"] == ["owned-0", "owned-1"]


def test_reap_first_component_failure_does_not_hide_later_results(monkeypatch):
    empty_scan = {"rows": {}, "scanned": 0, "truncated": False, "limit": 10}
    monkeypatch.setattr(reap, "_scan_process_table", lambda: empty_scan)
    monkeypatch.setattr(reap, "_reap_owned_browser_trees",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("registry offline")))
    monkeypatch.setattr(reap, "_clean_scratch",
                        lambda _dry: {"removed": ["old"], "errors": [], "truncated": False})
    monkeypatch.setattr(reap, "_sweep_directory", lambda _dry: 7)
    monkeypatch.setattr(reap, "_sweep_stale_runs", lambda _dry: 3)
    monkeypatch.setattr(reap, "_sweep_terminal_step_claims", lambda _dry: 2)
    monkeypatch.setattr(reap, "_sweep_terminal_event_claims", lambda _dry: 6)
    monkeypatch.setattr(reap, "_sweep_stuck_builds", lambda _dry: 1)
    monkeypatch.setattr(reap, "_sweep_stale_research", lambda _dry: 4)

    result = reap.reap(dry=True)

    assert result["status"] == "degraded"
    assert result["stale_directory_released"] == 7
    assert result["stale_runs_abandoned"] == 3 and result["dead_research_failed"] == 4
    assert result["terminal_event_claims_released"] == 6
    assert result["components"]["owned_browser_trees"]["status"] == "degraded"


def test_pid_birth_mismatch_refuses_signal(monkeypatch):
    identity = process_assurance.ProcessIdentity(42, 100, "boot")
    first = process_assurance.ProcessSnapshot(
        identity, 1, 42, os.getuid(), "/ms-playwright/chrome-headless-shell --headless")
    reused = process_assurance.ProcessSnapshot(
        process_assurance.ProcessIdentity(42, 101, "boot"), 1, 42, os.getuid(), first.cmdline)
    snapshots = iter((first, reused))
    monkeypatch.setattr(reap.process_assurance, "read_snapshot", lambda _pid: next(snapshots))
    signaled = []
    monkeypatch.setattr(reap.os, "kill", lambda *args: signaled.append(args))
    assert reap._identity_safe_signal(42, 1, first.cmdline, reap.signal.SIGKILL) is False
    assert signaled == []


def test_owned_browser_reap_requires_registry_and_signals_exact_tree_leaf_first(monkeypatch, tmp_path):
    registry = tmp_path / "registry"
    registry.mkdir()
    root_id = process_assurance.ProcessIdentity(410, 10, "boot")
    child_id = process_assurance.ProcessIdentity(411, 11, "boot")
    peer_id = process_assurance.ProcessIdentity(499, 12, "boot")
    root = process_assurance.ProcessSnapshot(root_id, 1, 410, os.getuid(), "node browser_bridge.js")
    child = process_assurance.ProcessSnapshot(child_id, 410, 410, os.getuid(), "chromium")
    peer = process_assurance.ProcessSnapshot(peer_id, 1, 410, os.getuid(), "unrelated")
    record = registry / "root.json"
    record.write_text('{"pid":410,"start_ticks":10,"boot_id":"boot",'
                      '"owner":"qa-browser:tenant-a:run"}')
    snapshots = {410: root, 411: child, 499: peer}
    monkeypatch.setattr(reap.process_assurance, "read_snapshot", lambda pid: snapshots.get(pid))
    killed = []
    monkeypatch.setattr(reap.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    result = reap._reap_owned_browser_trees(
        {"rows": {pid: (snap, 100) for pid, snap in snapshots.items()}, "truncated": False},
        registry=registry)

    assert [pid for pid, _sig in killed] == [411, 410]
    assert 499 not in [pid for pid, _sig in killed]
    assert result["reaped"] == 1 and not record.exists()


def test_owned_browser_registry_preserves_reused_root_pid(monkeypatch, tmp_path):
    registry = tmp_path / "registry"
    registry.mkdir()
    expected = process_assurance.ProcessIdentity(510, 20, "boot")
    reused = process_assurance.ProcessSnapshot(
        process_assurance.ProcessIdentity(510, 21, "boot"), 1, 510, os.getuid(), "browser")
    record = registry / "root.json"
    record.write_text('{"pid":510,"start_ticks":20,"boot_id":"boot",'
                      '"owner":"qa-browser:tenant-a:run"}')
    monkeypatch.setattr(reap.process_assurance, "read_snapshot", lambda _pid: reused)
    monkeypatch.setattr(reap.os, "kill", lambda *_args: (_ for _ in ()).throw(
        AssertionError("reused PID must never be signalled")))

    result = reap._reap_owned_browser_trees(
        {"rows": {510: (reused, 100)}, "truncated": False}, registry=registry)

    assert result["reaped"] == 0 and record.exists()


def test_reap_db_mutations_are_batched_skip_locked_and_time_bounded():
    source = (ROOT / "scripts" / "reap.py").read_text()
    assert "pg_try_advisory_xact_lock" in source, "audit chain must never wait indefinitely"
    for function in ("_sweep_stale_runs", "_sweep_terminal_step_claims",
                     "_sweep_terminal_event_claims",
                     "_sweep_stale_research", "_sweep_directory"):
        body = source[source.index(f"def {function}"):]
        body = body[:body.find("\ndef ", 1)]
        assert "SKIP LOCKED" in body and "LIMIT %s" in body
        assert "_set_db_timeouts(cur)" in body
    assert "return 0\n    except Exception" not in source


def test_reap_degraded_outcome_is_cli_nonzero(monkeypatch, capsys):
    degraded = {"status": "degraded", "components": {}, "degraded": [{"component": "db"}]}
    monkeypatch.setattr(reap, "reap", lambda dry=False: degraded)
    assert reap._main(["status"]) == 2
    assert '"status": "degraded"' in capsys.readouterr().out
