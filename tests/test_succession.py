import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import succession


def test_role_requires_explicit_coverage_policy_and_manager():
    with pytest.raises(ValueError, match="criticality"):
        succession.normalize_role("release captain", "product:x", "vp", criticality="urgent")
    with pytest.raises(ValueError, match="duty_manager"):
        succession.normalize_role("release captain", "product:x", "")
    role = succession.normalize_role("release captain", "product:x", "vp", minimum_backups=2)
    assert role["minimum_backups"] == 2


def test_appointments_and_takeovers_require_manager_decision_evidence():
    with pytest.raises(ValueError, match="decision evidence"):
        succession.validate_appointment("backup", "agent-b", "manager", {})
    with pytest.raises(ValueError, match="trigger evidence"):
        succession.validate_takeover("agent-a", "agent-b", "absence", {}, "cover now",
                                     {"policy": "p1"}, "manager")
    fact = succession.validate_takeover(
        "agent-a", "agent-b", "unresponsive", {"missed_checkins": 3},
        "activate trained backup", {"review": "dm-12"}, "duty-manager")
    assert fact["successor"] == "agent-b"


def test_direct_acting_appointment_is_not_a_fence_bypass(monkeypatch):
    monkeypatch.setattr(succession, "ensure", lambda: True)
    with pytest.raises(ValueError, match="take_over"):
        succession.appoint("t", "r", "agent-b", "acting", appointed_by="manager",
                           decision_evidence={"decision": "temporary cover"})


def test_fence_rejects_displaced_holder_and_stale_epoch():
    assert succession.assert_fence("backup", 8, "backup", 8)
    with pytest.raises(PermissionError, match="fence"):
        succession.assert_fence("backup", 8, "primary", 7)
    with pytest.raises(PermissionError, match="fence"):
        succession.assert_fence("backup", 8, "backup", 7)


def test_takeover_prefers_ready_backup_but_allows_audited_emergency_judgment():
    result = succession.validate_takeover_candidate(
        "backup", "available", True, {"review": "dm-12"},
        authorized_by="ops", duty_manager="ops")
    assert result == {"normal_cover": True, "emergency_override": False}
    with pytest.raises(ValueError, match="unsafe takeover"):
        succession.validate_takeover_candidate(
            None, "unknown", False, {"review": "dm-13"},
            authorized_by="ops", duty_manager="ops")
    result = succession.validate_takeover_candidate(
        None, "unknown", False,
        {"continuity_override": True, "risk_acceptance": "SEV1 requires immediate cover"},
        authorized_by="ops", duty_manager="ops")
    assert result["emergency_override"]
    with pytest.raises(PermissionError, match="authority"):
        succession.validate_takeover_candidate(
            "backup", "available", True, {"review": "dm-14"},
            authorized_by="worker", duty_manager="ops")


def test_coverage_matrix_explains_missing_depth_and_readiness():
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    role = {"role_id": "r1", "criticality": "critical", "minimum_backups": 2,
            "readiness_max_age_s": 3600, "duty_manager": "ops-lead"}
    apps = [
        {"appointee": "a", "appointment_kind": "primary", "status": "active", "priority": 1},
        {"appointee": "b", "appointment_kind": "backup", "status": "active", "priority": 1},
    ]
    availability = [{"subject": "a", "state": "available", "effective_at": now},
                    {"subject": "b", "state": "available", "effective_at": now}]
    readiness = [{"to_subject": "b", "readiness_state": "verified",
                  "verified_at": now - timedelta(minutes=10)}]
    result = succession.evaluate_coverage(role, apps, availability, readiness, now=now)
    assert not result["healthy"]
    assert {g["code"] for g in result["gaps"]} == {"backup_depth", "takeover_readiness"}


def test_unavailable_primary_requires_an_acting_holder():
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    role = {"role_id": "r1", "minimum_backups": 1, "readiness_max_age_s": 3600,
            "duty_manager": "ops"}
    apps = [{"appointee": "a", "appointment_kind": "primary", "status": "active"},
            {"appointee": "b", "appointment_kind": "backup", "status": "active"}]
    availability = [{"subject": "a", "state": "unavailable", "effective_at": now},
                    {"subject": "b", "state": "available", "effective_at": now}]
    readiness = [{"to_subject": "b", "readiness_state": "verified", "verified_at": now}]
    result = succession.evaluate_coverage(role, apps, availability, readiness, now=now)
    assert [g["code"] for g in result["gaps"]] == ["uncovered_absence"]
    apps.append({"appointee": "b", "appointment_kind": "acting", "status": "active"})
    assert succession.evaluate_coverage(role, apps, availability, readiness, now=now)["healthy"]


def test_stale_or_expired_handoff_does_not_count_as_ready():
    now = datetime(2026, 8, 15, tzinfo=timezone.utc)
    role = {"role_id": "r1", "minimum_backups": 1, "readiness_max_age_s": 60,
            "duty_manager": "ops"}
    apps = [{"appointee": "a", "appointment_kind": "primary", "status": "active"},
            {"appointee": "b", "appointment_kind": "backup", "status": "active"}]
    availability = [{"subject": "b", "state": "available", "effective_at": now}]
    readiness = [{"to_subject": "b", "readiness_state": "verified",
                  "verified_at": now - timedelta(seconds=61)}]
    result = succession.evaluate_coverage(role, apps, availability, readiness, now=now)
    assert result["ready_backups"] == 0
    assert any(g["code"] == "takeover_readiness" for g in result["gaps"])


def test_schema_keeps_observations_and_fences_append_only_and_tenant_scoped():
    schema = (ROOT / "postgres" / "initdb" / "64-succession-continuity.sql").read_text()
    assert "continuity_one_active_primary_idx" in schema
    assert "continuity_one_active_takeover_idx" in schema
    assert "resulting_epoch = previous_epoch + 1" in schema
    assert "GRANT SELECT, INSERT ON TABLE public.%I TO agentos_app" in schema
    assert "current_setting(''app.tenant_id'', true)" in schema
