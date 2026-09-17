from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rls_readiness as readiness  # noqa: E402
import audit  # noqa: E402
import dbpool  # noqa: E402


def _migration(number: int) -> str:
    matches = list((ROOT / "postgres" / "initdb").glob(f"{number:02d}-*.sql"))
    assert len(matches) == 1
    return matches[0].read_text()


def test_rls_rollout_series_is_contiguous_and_ordered():
    assert [int(path.name.split("-", 1)[0]) for path in readiness._migration_paths()] == list(range(53, 85))


def test_task_board_schema_precedes_rls_prep_that_indexes_it():
    schema = _migration(33)
    prep = _migration(53)

    assert "CREATE TABLE IF NOT EXISTS task_board" in schema
    assert "ON task_board (tenant)" in prep


def test_lazy_tenant_relations_are_canonical_before_rls_rollout():
    schema = _migration(35)
    expected = {
        "accounts", "ceo_vision", "proactive_sent", "brief_cache",
        "tenant_providers", "product_registry", "findings",
        "finding_verifications", "qa_runs", "story_corpus",
    }

    for table in expected:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema


def test_organization_schema_precedes_cross_org_lineage_and_tenant_backfill():
    schema = _migration(40)
    lineage = _migration(49)
    spine = _migration(54)

    assert "CREATE TABLE IF NOT EXISTS orgs" in schema
    assert "CREATE TABLE IF NOT EXISTS org_lineage" in lineage
    assert "FROM orgs o" in spine


def test_controller_schema_precedes_recovery_and_execution_scope_migrations():
    schema = _migration(39)
    recovery = _migration(66)
    scope = _migration(73)

    assert "CREATE TABLE IF NOT EXISTS controller_state" in schema
    assert "CREATE TABLE IF NOT EXISTS controller_jobs" in schema
    assert "worker_start_ticks" in recovery
    assert "ALTER TABLE controller_state" in scope


def test_answered_agentic_decision_requires_durable_application_ack():
    migration = _migration(78)
    assert "applied_at" in migration
    assert "agentic_decisions_pending_apply_idx" in migration


def test_org_artifacts_schema_precedes_tenant_writes_and_enforces_rls():
    migration = _migration(79)
    assert "CREATE TABLE IF NOT EXISTS org_artifacts" in migration
    assert "CREATE TABLE IF NOT EXISTS orphaned_org_artifacts_archive" in migration
    assert "INSERT INTO orphaned_org_artifacts_archive" in migration
    assert "ALTER TABLE org_artifacts FORCE ROW LEVEL SECURITY" in migration
    assert "CREATE POLICY org_artifacts_tenant_isolation" in migration
    assert "aos_set_tenant_from_org('org_id')" in migration
    assert "GRANT USAGE,SELECT ON SEQUENCE org_artifacts_id_seq" in migration
    assert "REVOKE ALL ON TABLE orphaned_org_artifacts_archive FROM agentos_app" in migration


def test_legacy_alerts_are_preserved_but_excluded_from_production_sla():
    migration = _migration(80)
    assert "execution_scope='legacy'" in migration
    assert "agent_alerts_execution_scope_check" in migration
    assert "agent_alerts_open_scope_sig" in migration


def test_legacy_agent_questions_are_preserved_but_excluded_from_production_reminders():
    migration = _migration(81)
    assert "execution_scope=cs.execution_scope" in migration
    assert "execution_scope='legacy'" in migration
    assert "agent_requests_execution_scope_check" in migration
    assert "agent_requests_scope_open_idx" in migration


def test_tenant_role_requires_every_owned_sequence_not_only_id_sequences():
    healthy = readiness.TableState("orchestra_runs", {"tenant_id", "run_id"},
                                   rls_enabled=True, force_rls=True, policy_count=1,
                                   tenant_index=True)
    result = readiness.evaluate([healthy])
    assert result["ok"] is True
    broken = readiness.TableState("orchestra_runs", {"tenant_id", "run_id"},
                                  rls_enabled=True, force_rls=True, policy_count=1,
                                  tenant_index=True,
                                  sequence_gaps={"orchestra_runs_run_id_seq"})
    result = readiness.evaluate([broken])
    assert result["missing_tenant_sequence_grant"] == [
        "orchestra_runs.orchestra_runs_run_id_seq"]
    assert result["ok"] is False
    migration = _migration(77)
    assert "pg_depend" in migration
    assert "GRANT USAGE, SELECT ON SEQUENCE" in migration


def test_restricted_database_roles_are_safe_defaults_not_optional(monkeypatch):
    monkeypatch.delenv("AOS_DB_APP_ROLE", raising=False)
    monkeypatch.delenv("AOS_DB_AUDIT_ROLE", raising=False)
    assert dbpool._app_role() == "agentos_app"
    assert audit._audit_role() == "agentos_audit_writer"
    monkeypatch.setenv("AOS_DB_APP_ROLE", "off")
    monkeypatch.setenv("AOS_DB_AUDIT_ROLE", "disabled")
    assert dbpool._app_role() is None
    assert audit._audit_role() is None


def test_lock_heavy_ddl_refuses_active_work_by_default(monkeypatch):
    active = {"controller_jobs": [[7, 42, "qa", "running"]], "orchestra_runs": [],
              "tool_leases": [], "active_queries": []}
    monkeypatch.setattr(readiness, "ddl_activity", lambda: active)
    monkeypatch.delenv("AOS_RLS_ALLOW_ACTIVE", raising=False)

    safe, facts = readiness.require_ddl_quiescence("migration-dry-run")

    assert safe is False and facts == active


def test_lock_heavy_ddl_allows_quiescent_database(monkeypatch):
    quiet = {"controller_jobs": [], "orchestra_runs": [], "tool_leases": [], "active_queries": []}
    monkeypatch.setattr(readiness, "ddl_activity", lambda: quiet)
    safe, facts = readiness.require_ddl_quiescence("migration-dry-run")
    assert safe is True and facts == quiet


def test_every_live_catalog_mutator_checks_quiescence_before_ddl():
    source = (ROOT / "scripts" / "rls_readiness.py").read_text()
    for operation in ("apply-indexes", "harness", "audit-harness", "role-lessons-harness",
                      "app-role-harness", "migration-dry-run", "enforced-smoke",
                      "enforced-module-smoke"):
        assert f'require_ddl_quiescence("{operation}")' in source


def test_global_operational_tables_are_audited_owner_only_exemptions():
    states = [
        readiness.TableState("accountability_sweep_state", {"name", "version"}),
        readiness.TableState("agent_slots", {"slot_id", "holder", "owner_token"}),
        readiness.TableState("app_spend_reservations", {"token", "app", "amount"}),
        readiness.TableState("auth_rate_limits", {"action", "key_hash", "bucket"}),
        readiness.TableState("factory_resume_claims", {"product", "claim_token"}),
        readiness.TableState("factory_resume_sweep_state",
                             {"singleton", "cursor_generation_ts", "cursor_run_id"}),
        readiness.TableState("forecast_sweep_state", {"name", "cursor_tenant_id"}),
        readiness.TableState("host_resource_leases", {"lease_id", "holder", "owner_token"}),
        readiness.TableState("orphaned_org_artifacts_archive", {"original_id", "payload"}),
        readiness.TableState("qa_evidence_encoding_jobs", {"id", "source_path", "status"}),
        readiness.TableState("scheduler_claims", {"claim_token", "name", "status"}),
    ]
    result = readiness.evaluate(states)
    assert result["ok"] is True
    assert set(result["global_operational_exemptions"]) == {
        "accountability_sweep_state", "agent_slots", "app_spend_reservations", "auth_rate_limits",
        "assurance_outreach_delivery", "assurance_pilot_leads", "factory_resume_claims",
        "factory_resume_sweep_state", "forecast_sweep_state",
        "host_resource_leases", "orphaned_org_artifacts_archive", "qa_evidence_encoding_jobs",
        "scheduler_claims"}
    exposed = readiness.evaluate([
        readiness.TableState("agent_slots", {"slot_id"}, app_role_access=True),
    ])
    assert exposed["global_operational_app_role_exposure"] == ["agent_slots"]
    assert exposed["ok"] is False
    migration = _migration(66)
    assert "CREATE TABLE IF NOT EXISTS agent_slots" in migration
    assert "REVOKE ALL ON TABLE agent_slots FROM agentos_app" in migration
    assert "REVOKE ALL ON TABLE scheduler_claims FROM agentos_app" in migration
    host_migration = _migration(70)
    assert "CREATE TABLE IF NOT EXISTS host_resource_leases" in host_migration
    assert "REVOKE ALL ON TABLE host_resource_leases FROM agentos_app" in host_migration
    archive_migration = _migration(79)
    assert "REVOKE ALL ON TABLE orphaned_org_artifacts_archive FROM agentos_app" in archive_migration
    cursor_migration = _migration(71)
    assert "CREATE TABLE IF NOT EXISTS accountability_sweep_state" in cursor_migration
    assert "REVOKE ALL ON TABLE accountability_sweep_state FROM agentos_app" in cursor_migration
    exactness_migration = _migration(72)
    assert "CREATE TABLE IF NOT EXISTS factory_resume_claims" in exactness_migration
    assert "CREATE TABLE IF NOT EXISTS factory_resume_sweep_state" in exactness_migration
    assert "REVOKE ALL ON TABLE factory_resume_claims FROM agentos_app" in exactness_migration
    assert "REVOKE ALL ON TABLE factory_resume_sweep_state FROM agentos_app" in exactness_migration
    forecast_migration = _migration(74)
    assert "CREATE TABLE IF NOT EXISTS forecast_sweep_state" in forecast_migration
    assert "REVOKE ALL ON TABLE forecast_sweep_state FROM agentos_app" in forecast_migration
    boundary_migration = _migration(84)
    for table in ("app_spend_reservations", "auth_rate_limits", "qa_evidence_encoding_jobs"):
        assert table in boundary_migration
    assert "REVOKE ALL ON TABLE public.%I FROM agentos_app" in boundary_migration
    assurance_migration = _migration(85)
    for table in ("assurance_pilot_leads", "assurance_outreach_delivery"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in assurance_migration
        assert f"REVOKE ALL ON TABLE {table} FROM agentos_app" in assurance_migration


def test_browser_sessions_are_tenant_scoped_but_pre_auth_resolution_stays_explicit():
    migration = _migration(84)
    assert "ALTER TABLE public.auth_sessions ENABLE ROW LEVEL SECURITY" in migration
    assert "ALTER TABLE public.auth_sessions FORCE ROW LEVEL SECURITY" in migration
    assert "CREATE POLICY auth_sessions_tenant_guc" in migration
    assert "tenant_id = current_setting('app.tenant_id', true)" in migration
    auth_source = (ROOT / "scripts" / "auth.py").read_text()
    assert "def tenant_for_session(token):" in auth_source
    assert "with _conn() as c" in auth_source


def test_mission_revision_history_is_tenant_fenced_and_authority_bound():
    migration = _migration(101)
    assert "CREATE TABLE IF NOT EXISTS aos_v2_mission_revisions" in migration
    assert "ON CONFLICT (tenant_id, mission_id, revision) DO NOTHING" in migration
    assert "aos_v2_mission_authorities_revision_valid" in migration
    assert "ALTER TABLE aos_v2_mission_revisions FORCE ROW LEVEL SECURITY" in migration
    assert "CREATE POLICY aos_v2_mission_revisions_tenant_guc" in migration
    assert "GRANT SELECT, INSERT ON TABLE aos_v2_mission_revisions TO agentos_app" in migration
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" not in migration


def test_controller_execution_scope_migration_preserves_tenant_rls_tables():
    migration = _migration(73)
    for table in ("controller_state", "controller_jobs"):
        assert f"ALTER TABLE {table}" in migration
        assert f"{table}_execution_scope_check" in migration
        assert f"{table}_execution_" in migration
    assert "execution_scope IN ('production', 'test')" in migration


def test_qa_campaign_accounting_migration_preserves_controller_tenant_boundary():
    migration = _migration(76)
    assert "ALTER TABLE controller_state" in migration
    assert "qa_campaign_key TEXT" in migration


def test_post_prep_tenant_tables_define_tenant_leading_indexes():
    expected = {
        59: ["work_delegations", "work_contract_updates"],
        60: ["assurance_evidence", "assurance_verdicts", "learning_incidents", "postmortems",
             "learning_findings", "corrective_actions", "recurrence_checks"],
        61: ["incident_responders", "incident_timeline", "incident_verifications",
             "incident_corrective_actions", "incident_postmortems"],
        62: ["objective_tradeoffs"],
        64: ["continuity_fence_events"],
        67: ["agentic_decisions", "agentic_decision_reviews"],
    }
    for number, tables in expected.items():
        sql = _migration(number)
        for table in tables:
            assert f"ON {table} (tenant_id" in sql or f"ON {table}(tenant_id" in sql


def test_special_policy_and_parent_scope_metadata_remain_distinct():
    assert set(readiness.SPECIAL_POLICY_TABLES) == {"audit_log", "role_lessons"}
    assert readiness.PARENT_SCOPED_TABLES["research_options"].startswith("research_runs.id")


def test_mission_assurance_tables_are_tenant_fenced_and_privacy_aware():
    migration = _migration(100)
    tables = (
        "aos_v2_missions", "aos_v2_mission_evidence",
        "aos_v2_mission_evidence_tombstones", "aos_v2_mission_claims",
        "aos_v2_mission_hazards", "aos_v2_mission_authorities",
        "aos_v2_mission_effects", "aos_v2_assurance_decisions",
        "aos_v2_mission_budget_entries",
    )
    for table in tables:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in migration
        assert f"'{table}'" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "current_setting(''app.tenant_id'', true)" in migration
    assert "NOT contains_personal_data OR retention_until IS NOT NULL" in migration
    assert "UNIQUE (tenant_id, mission_id, idempotency_key)" in migration
    assert "account IN ('authorized', 'available', 'reserved', 'spent')" in migration
    assert "GRANT SELECT, INSERT ON TABLE aos_v2_mission_evidence TO agentos_app" in migration
    assert "GRANT SELECT, INSERT ON TABLE aos_v2_assurance_decisions TO agentos_app" in migration
    assert "GRANT SELECT, INSERT ON TABLE aos_v2_mission_budget_entries TO agentos_app" in migration
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" not in migration
