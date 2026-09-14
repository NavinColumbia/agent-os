#!/usr/bin/env python3
"""rls_readiness.py - preflight for DB-enforced tenant isolation.

The current system has good application-level tenant tests, but the product blueprint and arch review call
for Postgres Row-Level Security as defense in depth. This checker is intentionally read-only: it inventories
the live schema and tells us what must be fixed before we can safely turn RLS on.

Checks:
  1. Tables with tenant_id (or an audited legacy tenant alias) must have RLS enabled, FORCE RLS, at least
     one policy, and a tenant index.
  2. Tables with product/org/agent/message scoping but no direct tenant column are flagged as missing a
     tenant spine. RLS cannot protect them without a direct tenant column or a reviewed join policy.
  3. Known platform-only tables are explicitly exempted so exemptions are auditable, not accidental.

    python scripts/rls_readiness.py report
    python scripts/rls_readiness.py rollout-gate
    python scripts/rls_readiness.py selftest
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aoscfg import DB  # noqa: E402
from dbpool import connection, tenant_connection  # noqa: E402


RLS_MIGRATION_FIRST = 53
RLS_MIGRATION_LAST = 84


def ddl_activity() -> dict:
    """Return live work that makes catalog-wide RLS DDL unsafe, without taking locks itself."""
    facts = {"controller_jobs": [], "orchestra_runs": [], "tool_leases": [], "active_queries": []}
    with psycopg.connect(DB, autocommit=True) as c, c.cursor() as cur:
        cur.execute("""SELECT to_regclass('public.controller_jobs') IS NOT NULL,
                              to_regclass('public.orchestra_runs') IS NOT NULL,
                              to_regclass('public.orchestra_tool_leases') IS NOT NULL""")
        has_jobs, has_runs, has_leases = cur.fetchone()
        if has_jobs:
            cur.execute("""SELECT id,thread_id,kind,status FROM controller_jobs
                           WHERE status IN ('running','pending') ORDER BY id LIMIT 50""")
            facts["controller_jobs"] = [list(row) for row in cur.fetchall()]
        if has_runs:
            cur.execute("""SELECT run_id,tenant_id,status FROM orchestra_runs
                           WHERE status='running' ORDER BY run_id LIMIT 50""")
            facts["orchestra_runs"] = [list(row) for row in cur.fetchall()]
        if has_leases:
            cur.execute("""SELECT run_id,tenant_id,actor_id,tool FROM orchestra_tool_leases
                           WHERE lease_until>now() ORDER BY lease_until LIMIT 50""")
            facts["tool_leases"] = [list(row) for row in cur.fetchall()]
        cur.execute("""SELECT pid,coalesce(application_name,''),left(coalesce(query,''),160)
                       FROM pg_stat_activity
                       WHERE pid<>pg_backend_pid() AND datname=current_database() AND state='active'
                         AND query_start < now()-interval '1 second'
                       ORDER BY query_start LIMIT 50""")
        facts["active_queries"] = [list(row) for row in cur.fetchall()]
    return facts


def require_ddl_quiescence(operation: str) -> tuple[bool, dict]:
    """Fail closed before lock-heavy dry-runs/rollouts while autonomous work is live."""
    facts = ddl_activity()
    active = any(facts.values())
    override = os.environ.get("AOS_RLS_ALLOW_ACTIVE", "").strip().lower() in {"1", "true", "yes"}
    if active and not override:
        print(json.dumps({"refused": operation, "reason": "active_work_makes_ddl_unsafe",
                          "activity": facts}, indent=2, sort_keys=True, default=str))
        print("REFUSED: wait for controller/orchestra/tool work to become quiescent before RLS DDL")
        return False, facts
    return True, facts


def quiescence() -> int:
    """Machine-usable migration fence: no catalog-changing deploy may race durable work."""
    safe, facts = require_ddl_quiescence("migration-apply")
    if safe:
        print(json.dumps({"safe": True, "activity": facts}, indent=2, sort_keys=True))
        print("PASS: database is quiescent for ordered migration apply")
        return 0
    return 2


# Explicit platform/control-plane tables whose rows are not tenant-owned user data. If a table graduates to
# tenant-facing data, remove it from this set and add tenant_id/RLS.
PLATFORM_EXEMPT = {
    "agent_alerts",
    "app_policies",
    "blobs",
    "browser_slots",
    "budgets",
    "claude_slots",
    "dbpool_selftest",
    "email_codes",
    "experiments",
    "flags",
    "heartbeats",
    "kill_switch",
    "mem_edges",
    "memories",
    "proactive_sweep_state",
    "schedules",
    "scheduler_runs",
    "sentinel_state",
    "skills",
    "stripe_events",
    "task_checkpoints",
    "watchdog_alerts",
}

# Global coordination rows are deliberately not tenant data: a provider/process slot and a recurring-job
# claim fence capacity across every tenant on the host.  They must stay owner-only rather than receive a
# tenant equality policy that would either invent false ownership or expose global control state.  Keep the
# rationale machine-readable so a new unscoped table cannot disappear into an unexplained exemption.
GLOBAL_OPERATIONAL_EXEMPTIONS = {
    "accountability_sweep_state": "global cross-tenant duty keyset cursors; owner-only control plane",
    "agent_slots": "global cross-tenant agent process capacity and fencing leases; owner-only control plane",
    "factory_resume_claims": "global build-resume occurrence claims and fencing tokens; owner-only control plane",
    "factory_resume_sweep_state": "global build-resume fairness cursor; owner-only control plane",
    "forecast_sweep_state": "global cross-tenant forecast fairness cursor; owner-only control plane",
    "host_resource_leases": "global weighted RAM/CPU admission and fencing leases; owner-only control plane",
    "app_spend_reservations": "global cross-tenant paid-call admission reservations; owner-only control plane",
    "auth_rate_limits": "global pre-authentication abuse-control buckets with HMAC-only identities; owner-only "
                        "security boundary",
    "orphaned_org_artifacts_archive": "forensic archive of legacy artifacts whose tenant ownership cannot "
                                       "be recovered; owner-only compliance evidence",
    "qa_evidence_encoding_jobs": "host-local deferred evidence-transcoding queue; owner-only control plane",
    "scheduler_claims": "global recurring-job execution claims and fencing tokens; owner-only control plane",
    "assurance_pilot_leads": "global pre-account sales leads containing founder-operated contact data; "
                             "owner-only commercial control plane",
    "assurance_outreach_delivery": "global pre-account curated outreach delivery ledger; owner-only "
                                   "commercial control plane",
}
PLATFORM_EXEMPT.update(GLOBAL_OPERATIONAL_EXEMPTIONS)

# Legacy tables that already carry tenant ownership under a non-standard column name. Keep this list short
# and audited: new tenant-facing tables should use tenant_id.
TENANT_COLUMN_ALIASES = {
    "task_board": "tenant",
}

# Tables where product/org scoping exists but the table does not yet carry tenant_id. These are not "okay";
# they are called out separately because their fix is schema work, not just CREATE POLICY.
INDIRECT_SCOPE_COLUMNS = {
    "agent_id",
    "awaited",
    "corr_id",
    "frm",
    "org_id",
    "product",
    "recipient",
    "sender",
    "source_org",
    "subscriber",
    "target_org",
    "thread_id",
    "to_actor",
    "waiter",
}

# Child tables whose tenant scope is inherited from a parent row. They still need a tenant_id backfill or
# reviewed SECURITY DEFINER / EXISTS policies; this list prevents them from hiding as "unknown unscoped".
PARENT_SCOPED_TABLES = {
    "app_registry": "tenant_products.product -> app_registry.name",
    "build_outcomes": "tenant_products.product -> build_outcomes.product",
    "directory": "tenant_products.product -> directory.product",
    "finding_verifications": "findings.id -> finding_verifications.finding_id",
    "findings": "orgs.id -> findings.org_id",
    "org_artifacts": "orgs.id -> org_artifacts.org_id",
    "org_lineage": "orgs.id -> org_lineage.org_id/derived_from",
    "org_metrics": "tenant_products.product -> org_metrics.product",
    "qa_runs": "tenant_products.product -> qa_runs.product",
    "quality_measurements": "quality_runs.id -> quality_measurements.run_id",
    "quality_runs": "tenant_products.product/orgs.id -> quality_runs.product/org_id",
    "research_options": "research_runs.id -> research_options.run_id",
    "story_corpus": "tenant_products.product -> story_corpus.product",
    "traces": "tenant_products.product -> traces.product",
}

# Tenant-owned tables that cannot safely use the generic tenant_id = current_setting(...) policy for every
# operation. Keep them tenant-indexed and RLS-tracked, but force a reviewed policy/migration instead of
# generating unsafe SQL.
SPECIAL_POLICY_TABLES = {
    "audit_log": "global hash-chain append must see the previous global row; needs SECURITY DEFINER append "
                 "or split writer plus tenant-scoped read policy",
    "role_lessons": "tenant_id NULL rows are shared fleet lessons; needs policy that allows shared reads plus "
                    "tenant-scoped rows, not a tenant-only equality policy",
}


@dataclass
class TableState:
    name: str
    columns: set[str]
    rls_enabled: bool = False
    force_rls: bool = False
    policy_count: int = 0
    tenant_index: bool = False
    policy_names: set[str] = field(default_factory=set)
    app_role_access: bool = False
    sequence_gaps: set[str] = field(default_factory=set)

    @property
    def tenant_column(self) -> str | None:
        if "tenant_id" in self.columns:
            return "tenant_id"
        alias = TENANT_COLUMN_ALIASES.get(self.name)
        if alias and alias in self.columns:
            return alias
        return None

    @property
    def tenant_scoped(self) -> bool:
        return self.tenant_column is not None

    @property
    def indirect_scoped(self) -> bool:
        return bool(self.columns & INDIRECT_SCOPE_COLUMNS) and not self.tenant_scoped

    @property
    def parent_scoped(self) -> bool:
        return self.name in PARENT_SCOPED_TABLES and not self.tenant_scoped


def _catalog_from_cursor(cur) -> list[TableState]:
    cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='agentos_app')")
    app_role_exists = bool(cur.fetchone()[0])
    cur.execute("""SELECT c.relname,
                              bool_or(a.attname='tenant_id') AS has_tenant,
                              array_agg(a.attname ORDER BY a.attnum) FILTER (WHERE a.attnum > 0) AS cols,
                              c.relrowsecurity,
                              c.relforcerowsecurity,
                              (SELECT count(*) FROM pg_policies p
                                WHERE p.schemaname='public' AND p.tablename=c.relname) AS policies,
                              (SELECT array_agg(p.policyname ORDER BY p.policyname)
                                 FROM pg_policies p
                                WHERE p.schemaname='public' AND p.tablename=c.relname) AS policy_names
                         FROM pg_class c
                         JOIN pg_namespace n ON n.oid=c.relnamespace
                         JOIN pg_attribute a ON a.attrelid=c.oid
                        WHERE n.nspname='public' AND c.relkind='r'
                          AND c.relname NOT LIKE 'pg_%'
                          AND c.relname NOT LIKE 'rls_probe_%'
                        GROUP BY c.oid, c.relname, c.relrowsecurity, c.relforcerowsecurity
                        ORDER BY c.relname""")
    rows = cur.fetchall()
    states = []
    for name, _has_tenant, cols, rls, force, policies, policy_names in rows:
        cols = set(cols or [])
        probe = TableState(name, cols)
        tenant_index = False
        tenant_col = probe.tenant_column
        if tenant_col:
            cur.execute("""SELECT 1
                                 FROM pg_index i
                                 JOIN pg_class ic ON ic.oid=i.indexrelid
                                 JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum = ANY(i.indkey)
                                WHERE i.indrelid = %s::regclass
                                  AND a.attname=%s
                                LIMIT 1""", (name, tenant_col))
            tenant_index = cur.fetchone() is not None
        app_role_access = False
        if app_role_exists and name in GLOBAL_OPERATIONAL_EXEMPTIONS:
            cur.execute("""SELECT has_table_privilege('agentos_app', %s, 'SELECT')
                                    OR has_table_privilege('agentos_app', %s, 'INSERT')
                                    OR has_table_privilege('agentos_app', %s, 'UPDATE')
                                    OR has_table_privilege('agentos_app', %s, 'DELETE')""",
                        (name, name, name, name))
            app_role_access = bool(cur.fetchone()[0])
        sequence_gaps = set()
        if app_role_exists and tenant_col and name not in PLATFORM_EXEMPT and name not in SPECIAL_POLICY_TABLES:
            # MATERIALIZED prevents PostgreSQL from evaluating
            # has_sequence_privilege() against non-sequence catalog rows while
            # planning.  Ownership dependencies cover serial and identity
            # columns regardless of their column name.
            cur.execute("""WITH seqs AS MATERIALIZED (
                               SELECT s.oid,s.relname
                                 FROM pg_class s
                                 JOIN pg_depend d ON d.objid=s.oid AND d.deptype IN ('a','i')
                                WHERE s.relkind='S' AND d.refobjid=%s::regclass
                           )
                           SELECT relname FROM seqs
                            WHERE NOT has_sequence_privilege('agentos_app',oid,'USAGE')
                            ORDER BY relname""", (name,))
            sequence_gaps = {row[0] for row in cur.fetchall()}
        states.append(TableState(name, cols, bool(rls), bool(force), int(policies or 0), tenant_index,
                                 set(policy_names or []), app_role_access, sequence_gaps))
    return states


def _catalog(conn=None) -> list[TableState]:
    if not DB:
        raise RuntimeError("DATABASE_URL is not configured")
    if conn is not None:
        with conn.cursor() as cur:
            return _catalog_from_cursor(cur)
    with connection() as c, c.cursor() as cur:
        return _catalog_from_cursor(cur)


def _migration_paths() -> list[Path]:
    """Return the contiguous, reviewable RLS rollout series in required application order."""
    initdb = Path(__file__).resolve().parent.parent / "postgres" / "initdb"
    by_number = {int(path.name.split("-", 1)[0]): path for path in initdb.glob("[0-9][0-9]-*.sql")}
    missing = [number for number in range(RLS_MIGRATION_FIRST, RLS_MIGRATION_LAST + 1)
               if number not in by_number]
    if missing:
        raise RuntimeError(f"missing RLS rollout migration(s): {missing}")
    return [by_number[number] for number in range(RLS_MIGRATION_FIRST, RLS_MIGRATION_LAST + 1)]


def _special_policy_satisfied(st: TableState) -> bool:
    required = {
        "audit_log": {"audit_log_tenant_read", "audit_log_writer_append"},
        "role_lessons": {
            "role_lessons_shared_read",
            "role_lessons_tenant_insert",
            "role_lessons_use_increment",
        },
    }.get(st.name)
    return bool(required and required.issubset(st.policy_names))


def evaluate(states: list[TableState]) -> dict:
    tenant_tables = [s for s in states if s.tenant_scoped and s.name not in PLATFORM_EXEMPT]
    missing_rls = [s.name for s in tenant_tables if not s.rls_enabled]
    missing_force = [s.name for s in tenant_tables if not s.force_rls]
    missing_policy = [s.name for s in tenant_tables if s.policy_count < 1]
    missing_index = [s.name for s in tenant_tables if not s.tenant_index]
    indirect = [s.name for s in states
                if (s.indirect_scoped or s.parent_scoped) and s.name not in PLATFORM_EXEMPT]
    parent_scoped = {s.name: PARENT_SCOPED_TABLES[s.name] for s in states
                     if s.parent_scoped and s.name not in PLATFORM_EXEMPT}
    special = {s.name: SPECIAL_POLICY_TABLES[s.name] for s in tenant_tables
               if s.name in SPECIAL_POLICY_TABLES and not _special_policy_satisfied(s)}
    unknown_unscoped = [s.name for s in states
                        if not s.tenant_scoped and not s.indirect_scoped and not s.parent_scoped
                        and s.name not in PLATFORM_EXEMPT]
    global_operational_exposure = [s.name for s in states
                                   if s.name in GLOBAL_OPERATIONAL_EXEMPTIONS and s.app_role_access]
    missing_sequence_grants = sorted(
        f"{s.name}.{seq}" for s in tenant_tables for seq in s.sequence_gaps)
    ok = not (missing_rls or missing_force or missing_policy or missing_index or indirect
              or unknown_unscoped or special or global_operational_exposure or missing_sequence_grants)
    return {
        "ok": ok,
        "tenant_tables": [s.name for s in tenant_tables],
        "tenant_alias_tables": {s.name: s.tenant_column for s in tenant_tables
                                if s.tenant_column != "tenant_id"},
        "missing_rls": missing_rls,
        "missing_force_rls": missing_force,
        "missing_policy": missing_policy,
        "missing_tenant_index": missing_index,
        "indirect_scope_needs_tenant_id": indirect,
        "parent_scope_needs_join_policy": parent_scoped,
        "special_policy_needed": special,
        "unknown_unscoped_tables": unknown_unscoped,
        "exemptions": sorted(PLATFORM_EXEMPT),
        "global_operational_exemptions": dict(sorted(GLOBAL_OPERATIONAL_EXEMPTIONS.items())),
        "global_operational_app_role_exposure": global_operational_exposure,
        "missing_tenant_sequence_grant": missing_sequence_grants,
    }


def report() -> int:
    res = evaluate(_catalog())
    print(json.dumps(res, indent=2, sort_keys=True))
    if not res["ok"]:
        print("FAIL: RLS is not ready - fix the listed tables before enabling DB-enforced tenancy")
        return 1
    print("PASS: RLS readiness - tenant tables have enabled+forced RLS, policies, indexes, and sequence grants")
    return 0


def apply_indexes() -> int:
    """Create missing tenant_id indexes for existing tenant-scoped tables. This is safe prep for RLS: it is
    idempotent, does not enable policies, and avoids the common RLS rollout failure where every tenant query
    becomes a table scan."""
    safe, _ = require_ddl_quiescence("apply-indexes")
    if not safe:
        return 2
    states = _catalog()
    targets = [s for s in states if s.tenant_scoped and not s.tenant_index and s.name not in PLATFORM_EXEMPT]
    if not targets:
        print("PASS: all tenant-scoped tables already have a tenant_id index")
        return 0
    with connection() as c, c.cursor() as cur:
        for st in targets:
            tenant_col = st.tenant_column or "tenant_id"
            idx = f"{st.name}_{tenant_col}_rls_idx"
            cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} ({})")
                        .format(sql.Identifier(idx), sql.Identifier(st.name), sql.Identifier(tenant_col)))
            print(f"created/confirmed {idx}")
        c.commit()
    after = evaluate(_catalog())
    if after["missing_tenant_index"]:
        print(json.dumps({"missing_tenant_index": after["missing_tenant_index"]}, indent=2))
        print("FAIL: tenant indexes still missing after apply-indexes")
        return 1
    print(f"PASS: tenant_id indexes ready on {len(targets)} table(s)")
    return 0


def _policy_statements(states: list[TableState], app_role: str) -> list[str]:
    direct = [s for s in states
              if s.tenant_scoped and s.name not in PLATFORM_EXEMPT and s.name not in SPECIAL_POLICY_TABLES]
    lines = [
        "-- Generated by scripts/rls_readiness.py policy-sql.",
        "-- Review before applying. This assumes tenant-facing DB sessions run as a non-owner app role",
        "-- and set transaction-local context with: SELECT set_config('app.tenant_id', $1, true).",
        "-- Auth/bootstrap paths that must resolve a tenant before the GUC is known need SECURITY DEFINER",
        "-- functions or a separate narrowly-granted auth role; do not run the whole app as table owner/superuser.",
        "",
        f"-- Intended app role: {app_role}",
        "",
    ]
    with connection() as c:
        for st in direct:
            tenant_col = st.tenant_column or "tenant_id"
            table_sql = sql.Identifier(st.name).as_string(c)
            tenant_sql = sql.Identifier(tenant_col).as_string(c)
            role_sql = sql.Identifier(app_role).as_string(c)
            pol = sql.Identifier(f"{st.name}_tenant_guc").as_string(c)
            lines.extend([
                f"ALTER TABLE {table_sql} ENABLE ROW LEVEL SECURITY;",
                f"ALTER TABLE {table_sql} FORCE ROW LEVEL SECURITY;",
                f"DROP POLICY IF EXISTS {pol} ON {table_sql};",
                f"CREATE POLICY {pol} ON {table_sql}",
                f"  FOR ALL TO {role_sql}",
                f"  USING ({tenant_sql} = current_setting('app.tenant_id', true))",
                f"  WITH CHECK ({tenant_sql} = current_setting('app.tenant_id', true));",
                "",
            ])
    return lines


def policy_sql(app_role="agentos_app") -> int:
    for line in _policy_statements(_catalog(), app_role):
        print(line)
    return 0


def _audit_policy_statements(app_role: str, writer_role: str) -> list[str]:
    """Reviewable split-role RLS for audit_log.

    audit_log is not a normal tenant table: appending a new global hash-chain entry requires seeing the
    previous global row. The tenant app role must only read its own rows and must not write. A narrow writer
    role may SELECT all rows to compute prev_hash and INSERT append-only rows, but should not receive UPDATE
    or DELETE grants."""
    with connection() as c:
        table = sql.Identifier("audit_log").as_string(c)
        app = sql.Identifier(app_role).as_string(c)
        writer = sql.Identifier(writer_role).as_string(c)
        app_pol = sql.Identifier("audit_log_tenant_read").as_string(c)
        writer_pol = sql.Identifier("audit_log_writer_append").as_string(c)
    return [
        "-- Generated by scripts/rls_readiness.py audit-policy-sql.",
        "-- Review before applying. audit_log needs split-role RLS because global hash-chain append",
        "-- must see the previous global row, while tenant-facing reads must stay tenant-scoped.",
        "",
        f"-- Intended tenant app role: {app_role}",
        f"-- Intended audit writer role: {writer_role}",
        "",
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
        f"REVOKE ALL ON {table} FROM {app};",
        f"REVOKE ALL ON {table} FROM {writer};",
        f"GRANT SELECT ON {table} TO {app};",
        f"GRANT SELECT, INSERT ON {table} TO {writer};",
        f"DROP POLICY IF EXISTS {app_pol} ON {table};",
        f"CREATE POLICY {app_pol} ON {table}",
        f"  FOR SELECT TO {app}",
        "  USING (tenant_id = current_setting('app.tenant_id', true));",
        f"DROP POLICY IF EXISTS {writer_pol} ON {table};",
        f"CREATE POLICY {writer_pol} ON {table}",
        f"  FOR ALL TO {writer}",
        "  USING (true)",
        "  WITH CHECK (true);",
        "",
        "-- Do not grant UPDATE or DELETE on audit_log to either app role. reseal/break-glass remains",
        "-- an operator/admin path, not a tenant app path.",
    ]


def audit_policy_sql(app_role="agentos_app", writer_role="agentos_audit_writer") -> int:
    for line in _audit_policy_statements(app_role, writer_role):
        print(line)
    return 0


def _role_lessons_policy_statements(app_role: str) -> list[str]:
    """Reviewable shared-plus-tenant RLS for role_lessons.

    role_lessons is not a plain tenant table: tenant_id NULL rows are shared fleet lessons that every tenant
    may read, while tenant-scoped rows are private to one tenant. Tenant app sessions may insert only their own
    tenant rows. They may update only the `uses` counter for visible rows; grant shape enforces the column
    boundary while RLS enforces row visibility."""
    with connection() as c:
        table = sql.Identifier("role_lessons").as_string(c)
        seq = sql.Identifier("role_lessons_id_seq").as_string(c)
        app = sql.Identifier(app_role).as_string(c)
        read_pol = sql.Identifier("role_lessons_shared_read").as_string(c)
        insert_pol = sql.Identifier("role_lessons_tenant_insert").as_string(c)
        uses_pol = sql.Identifier("role_lessons_use_increment").as_string(c)
    tenant_expr = "tenant_id = current_setting('app.tenant_id', true)"
    visible_expr = f"({tenant_expr} OR tenant_id IS NULL)"
    return [
        "-- Generated by scripts/rls_readiness.py role-lessons-policy-sql.",
        "-- Review before applying. role_lessons has shared fleet rows (tenant_id NULL) plus tenant rows.",
        "-- Tenant app sessions may read shared+own lessons, insert only own lessons, and update only uses.",
        "",
        f"-- Intended tenant app role: {app_role}",
        "",
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
        f"REVOKE ALL ON {table} FROM {app};",
        f"GRANT SELECT, INSERT ON {table} TO {app};",
        f"GRANT UPDATE (uses) ON {table} TO {app};",
        f"GRANT USAGE, SELECT ON SEQUENCE {seq} TO {app};",
        f"DROP POLICY IF EXISTS {read_pol} ON {table};",
        f"CREATE POLICY {read_pol} ON {table}",
        f"  FOR SELECT TO {app}",
        f"  USING {visible_expr};",
        f"DROP POLICY IF EXISTS {insert_pol} ON {table};",
        f"CREATE POLICY {insert_pol} ON {table}",
        f"  FOR INSERT TO {app}",
        f"  WITH CHECK ({tenant_expr});",
        f"DROP POLICY IF EXISTS {uses_pol} ON {table};",
        f"CREATE POLICY {uses_pol} ON {table}",
        f"  FOR UPDATE TO {app}",
        f"  USING {visible_expr}",
        f"  WITH CHECK {visible_expr};",
        "",
        "-- Do not grant broad UPDATE/DELETE on role_lessons to the tenant app role. Operator curation",
        "-- and global fleet-lesson insertion remain admin paths.",
    ]


def role_lessons_policy_sql(app_role="agentos_app") -> int:
    for line in _role_lessons_policy_statements(app_role):
        print(line)
    return 0


def harness() -> int:
    """Live proof of the intended RLS pattern on throwaway objects:
      - app role does not own the table;
      - no app.tenant_id GUC sees zero rows;
      - SET LOCAL app.tenant_id scopes reads;
      - WITH CHECK blocks cross-tenant writes.

    This does not mutate production tables. It creates and drops one temp-named public table and one NOLOGIN
    role so the pattern is proven against the same Postgres server/role privileges we will use for rollout."""
    safe, _ = require_ddl_quiescence("harness")
    if not safe:
        return 2
    suffix = uuid4().hex[:10]
    table = f"rls_probe_{suffix}"
    role = f"rls_probe_role_{suffix}"

    def ident(name):
        return sql.Identifier(name)

    try:
        with connection() as c, c.cursor() as cur:
            cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(ident(role)))
            cur.execute(sql.SQL("CREATE TABLE {} (id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, note TEXT NOT NULL)")
                        .format(ident(table)))
            cur.execute(sql.SQL("INSERT INTO {} (tenant_id, note) VALUES ('t-a','alpha'),('t-b','beta')")
                        .format(ident(table)))
            cur.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(ident(table)))
            cur.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(ident(table)))
            cur.execute(sql.SQL("""CREATE POLICY tenant_guc_policy ON {} FOR ALL TO {}
                                  USING (tenant_id = current_setting('app.tenant_id', true))
                                  WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")
                        .format(ident(table), ident(role)))
            cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(ident(table), ident(role)))
            cur.execute(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}")
                        .format(ident(f"{table}_id_seq"), ident(role)))
            c.commit()

        def as_role(stmts):
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute(sql.SQL("SET ROLE {}").format(ident(role)))
                out = []
                for stmt, params in stmts:
                    cur.execute(stmt, params or ())
                    if cur.description:
                        out.append(cur.fetchall())
                c.rollback()  # resets SET ROLE and SET LOCAL; discard probe writes unless explicitly committed
                return out

        no_guc = as_role([(sql.SQL("SELECT count(*) FROM {}").format(ident(table)), None)])[0][0][0]
        tenant_a = as_role([
            ("SELECT set_config('app.tenant_id', %s, true)", ("t-a",)),
            (sql.SQL("SELECT note FROM {} ORDER BY id").format(ident(table)), None),
        ])[1]
        tenant_b = as_role([
            ("SELECT set_config('app.tenant_id', %s, true)", ("t-b",)),
            (sql.SQL("SELECT note FROM {} ORDER BY id").format(ident(table)), None),
        ])[1]
        same_tenant_write = True
        try:
            as_role([
                ("SELECT set_config('app.tenant_id', %s, true)", ("t-a",)),
                (sql.SQL("INSERT INTO {} (tenant_id, note) VALUES ('t-a','ok')").format(ident(table)), None),
            ])
        except Exception:
            same_tenant_write = False
        cross_write_blocked = False
        try:
            as_role([
                ("SELECT set_config('app.tenant_id', %s, true)", ("t-a",)),
                (sql.SQL("INSERT INTO {} (tenant_id, note) VALUES ('t-b','bad')").format(ident(table)), None),
            ])
        except Exception:
            cross_write_blocked = True

        ok = (no_guc == 0 and tenant_a == [("alpha",)] and tenant_b == [("beta",)]
              and same_tenant_write and cross_write_blocked)
        print(f"no_guc_zero={no_guc == 0} tenant_a_only={tenant_a == [('alpha',)]} "
              f"tenant_b_only={tenant_b == [('beta',)]} same_tenant_write={same_tenant_write} "
              f"cross_write_blocked={cross_write_blocked}")
        print("PASS: RLS harness proves SET LOCAL app.tenant_id read/write isolation under a non-owner role"
              if ok else "FAIL")
        return 0 if ok else 1
    finally:
        try:
            with connection(autocommit=True) as c, c.cursor() as cur:
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(ident(table)))
                cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(ident(role)))
        except Exception:
            pass


def audit_harness() -> int:
    """Live proof for audit_log's split-role RLS pattern on throwaway objects:
      - tenant app role can read only rows matching app.tenant_id;
      - tenant app role cannot insert audit rows;
      - audit writer role can see the global tail and insert append-only rows;
      - audit writer role cannot update rows because it is not granted UPDATE.

    This does not mutate production audit_log."""
    safe, _ = require_ddl_quiescence("audit-harness")
    if not safe:
        return 2
    suffix = uuid4().hex[:10]
    table = f"rls_audit_probe_{suffix}"
    app_role = f"rls_audit_app_{suffix}"
    writer_role = f"rls_audit_writer_{suffix}"

    def ident(name):
        return sql.Identifier(name)

    try:
        with connection() as c, c.cursor() as cur:
            cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(ident(app_role)))
            cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(ident(writer_role)))
            cur.execute(sql.SQL("""CREATE TABLE {} (
                id BIGSERIAL PRIMARY KEY,
                tenant_id TEXT,
                entry_hash TEXT NOT NULL,
                payload JSONB NOT NULL DEFAULT '{{}}'
            )""").format(ident(table)))
            cur.execute(sql.SQL("""INSERT INTO {} (tenant_id, entry_hash, payload)
                                   VALUES ('t-a','h1','{{}}'),('t-b','h2','{{}}'),(NULL,'platform','{{}}')""")
                        .format(ident(table)))
            cur.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(ident(table)))
            cur.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(ident(table)))
            cur.execute(sql.SQL("""CREATE POLICY tenant_read ON {} FOR SELECT TO {}
                                  USING (tenant_id = current_setting('app.tenant_id', true))""")
                        .format(ident(table), ident(app_role)))
            cur.execute(sql.SQL("""CREATE POLICY writer_append ON {} FOR ALL TO {}
                                  USING (true) WITH CHECK (true)""")
                        .format(ident(table), ident(writer_role)))
            cur.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(ident(table), ident(app_role)))
            cur.execute(sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(ident(table), ident(writer_role)))
            cur.execute(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}")
                        .format(ident(f"{table}_id_seq"), ident(writer_role)))
            c.commit()

        def as_role(role, stmts):
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute(sql.SQL("SET ROLE {}").format(ident(role)))
                out = []
                for stmt, params in stmts:
                    cur.execute(stmt, params or ())
                    if cur.description:
                        out.append(cur.fetchall())
                c.rollback()
                return out

        app_no_guc = as_role(app_role, [(sql.SQL("SELECT count(*) FROM {}").format(ident(table)), None)])[0][0][0]
        app_a = as_role(app_role, [
            ("SELECT set_config('app.tenant_id', %s, true)", ("t-a",)),
            (sql.SQL("SELECT entry_hash FROM {} ORDER BY id").format(ident(table)), None),
        ])[1]
        app_insert_denied = False
        try:
            as_role(app_role, [
                ("SELECT set_config('app.tenant_id', %s, true)", ("t-a",)),
                (sql.SQL("INSERT INTO {} (tenant_id, entry_hash) VALUES ('t-a','bad')").format(ident(table)), None),
            ])
        except Exception:
            app_insert_denied = True
        writer_tail = as_role(writer_role, [
            (sql.SQL("SELECT entry_hash FROM {} ORDER BY id DESC LIMIT 1").format(ident(table)), None),
        ])[0]
        writer_insert = True
        try:
            as_role(writer_role, [
                (sql.SQL("INSERT INTO {} (tenant_id, entry_hash) VALUES ('t-a','h3')").format(ident(table)), None),
            ])
        except Exception:
            writer_insert = False
        writer_update_denied = False
        try:
            as_role(writer_role, [
                (sql.SQL("UPDATE {} SET entry_hash='changed' WHERE id=1").format(ident(table)), None),
            ])
        except Exception:
            writer_update_denied = True

        ok = (app_no_guc == 0 and app_a == [("h1",)] and app_insert_denied
              and writer_tail == [("platform",)] and writer_insert and writer_update_denied)
        print(f"app_no_guc_zero={app_no_guc == 0} app_tenant_a_only={app_a == [('h1',)]} "
              f"app_insert_denied={app_insert_denied} writer_sees_global_tail={writer_tail == [('platform',)]} "
              f"writer_insert={writer_insert} writer_update_denied={writer_update_denied}")
        print("PASS: audit_log split-role RLS harness preserves global append while tenant reads are scoped"
              if ok else "FAIL")
        return 0 if ok else 1
    finally:
        try:
            with connection(autocommit=True) as c, c.cursor() as cur:
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(ident(table)))
                cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(ident(app_role)))
                cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(ident(writer_role)))
        except Exception:
            pass


def role_lessons_harness() -> int:
    """Live proof for role_lessons' shared-plus-tenant RLS pattern on throwaway objects:
      - tenant app role can read shared fleet lessons plus its own tenant lessons;
      - tenant app role cannot read another tenant's lessons;
      - tenant app role can insert only rows for the current tenant;
      - tenant app role can update only the uses counter for visible shared/own rows.

    This does not mutate production role_lessons."""
    safe, _ = require_ddl_quiescence("role-lessons-harness")
    if not safe:
        return 2
    suffix = uuid4().hex[:10]
    table = f"rls_lessons_probe_{suffix}"
    role = f"rls_lessons_app_{suffix}"

    def ident(name):
        return sql.Identifier(name)

    try:
        with connection() as c, c.cursor() as cur:
            cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(ident(role)))
            cur.execute(sql.SQL("""CREATE TABLE {} (
                id BIGSERIAL PRIMARY KEY,
                role TEXT NOT NULL,
                lesson TEXT NOT NULL UNIQUE,
                uses INT DEFAULT 0,
                tenant_id TEXT
            )""").format(ident(table)))
            cur.execute(sql.SQL("""INSERT INTO {} (role, lesson, tenant_id)
                                   VALUES ('engineer','shared',NULL),
                                          ('engineer','tenant-a','t-a'),
                                          ('engineer','tenant-b','t-b')""").format(ident(table)))
            cur.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(ident(table)))
            cur.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(ident(table)))
            cur.execute(sql.SQL("""CREATE POLICY role_lessons_shared_read ON {} FOR SELECT TO {}
                                  USING (tenant_id = current_setting('app.tenant_id', true)
                                         OR tenant_id IS NULL)""").format(ident(table), ident(role)))
            cur.execute(sql.SQL("""CREATE POLICY role_lessons_tenant_insert ON {} FOR INSERT TO {}
                                  WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")
                        .format(ident(table), ident(role)))
            cur.execute(sql.SQL("""CREATE POLICY role_lessons_use_increment ON {} FOR UPDATE TO {}
                                  USING (tenant_id = current_setting('app.tenant_id', true)
                                         OR tenant_id IS NULL)
                                  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)
                                              OR tenant_id IS NULL)""").format(ident(table), ident(role)))
            cur.execute(sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(ident(table), ident(role)))
            cur.execute(sql.SQL("GRANT UPDATE (uses) ON {} TO {}").format(ident(table), ident(role)))
            cur.execute(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}")
                        .format(ident(f"{table}_id_seq"), ident(role)))
            c.commit()

        def as_role(stmts):
            with psycopg.connect(DB) as c, c.cursor() as cur:
                cur.execute(sql.SQL("SET ROLE {}").format(ident(role)))
                out = []
                for stmt, params in stmts:
                    cur.execute(stmt, params or ())
                    if cur.description:
                        out.append(cur.fetchall())
                c.rollback()
                return out

        visible_a = as_role([
            ("SELECT set_config('app.tenant_id', %s, true)", ("t-a",)),
            (sql.SQL("SELECT lesson FROM {} WHERE role='engineer' ORDER BY lesson").format(ident(table)), None),
        ])[1]
        tenant_insert = True
        try:
            as_role([
                ("SELECT set_config('app.tenant_id', %s, true)", ("t-a",)),
                (sql.SQL("INSERT INTO {} (role, lesson, tenant_id) VALUES ('engineer','own-new','t-a')")
                 .format(ident(table)), None),
            ])
        except Exception:
            tenant_insert = False
        cross_insert_blocked = False
        try:
            as_role([
                ("SELECT set_config('app.tenant_id', %s, true)", ("t-a",)),
                (sql.SQL("INSERT INTO {} (role, lesson, tenant_id) VALUES ('engineer','cross-new','t-b')")
                 .format(ident(table)), None),
            ])
        except Exception:
            cross_insert_blocked = True
        uses_update = True
        try:
            as_role([
                ("SELECT set_config('app.tenant_id', %s, true)", ("t-a",)),
                (sql.SQL("UPDATE {} SET uses=uses+1 WHERE lesson IN ('shared','tenant-a')").format(ident(table)), None),
            ])
        except Exception:
            uses_update = False
        lesson_update_blocked = False
        try:
            as_role([
                ("SELECT set_config('app.tenant_id', %s, true)", ("t-a",)),
                (sql.SQL("UPDATE {} SET lesson='mutated' WHERE lesson='tenant-a'").format(ident(table)), None),
            ])
        except Exception:
            lesson_update_blocked = True

        ok = (visible_a == [("shared",), ("tenant-a",)] and tenant_insert and cross_insert_blocked
              and uses_update and lesson_update_blocked)
        print(f"tenant_a_sees_shared_and_own={visible_a == [('shared',), ('tenant-a',)]} "
              f"tenant_insert={tenant_insert} cross_insert_blocked={cross_insert_blocked} "
              f"uses_update={uses_update} lesson_update_blocked={lesson_update_blocked}")
        print("PASS: role_lessons RLS harness preserves shared reads, tenant writes, and uses-only updates"
              if ok else "FAIL")
        return 0 if ok else 1
    finally:
        try:
            with connection(autocommit=True) as c, c.cursor() as cur:
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(ident(table)))
                cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(ident(role)))
        except Exception:
            pass


def app_role_runtime_harness() -> int:
    """Live proof that the real dbpool tenant primitive can run under a non-owner RLS role.

    This is closer to production than the SQL-only harnesses: it warms the shared pool with autocommit borrows
    first, then calls `dbpool.tenant_connection(..., app_role=...)` against a throwaway RLS table. It proves
    the app-role transaction sees only its tenant, can write only its tenant rows, blocks cross-tenant writes,
    and does not leak role/GUC state to the next pooled borrower."""
    safe, _ = require_ddl_quiescence("app-role-harness")
    if not safe:
        return 2
    suffix = uuid4().hex[:10]
    table = f"rls_runtime_probe_{suffix}"
    role = f"rls_runtime_app_{suffix}"

    def ident(name):
        return sql.Identifier(name)

    try:
        with connection() as c, c.cursor() as cur:
            cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(ident(role)))
            cur.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(ident(role)))
            cur.execute(sql.SQL("CREATE TABLE {} (id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, note TEXT NOT NULL)")
                        .format(ident(table)))
            cur.execute(sql.SQL("INSERT INTO {} (tenant_id, note) VALUES ('t-a','alpha'),('t-b','beta')")
                        .format(ident(table)))
            cur.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(ident(table)))
            cur.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(ident(table)))
            cur.execute(sql.SQL("""CREATE POLICY tenant_guc_policy ON {} FOR ALL TO {}
                                  USING (tenant_id = current_setting('app.tenant_id', true))
                                  WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""")
                        .format(ident(table), ident(role)))
            cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(ident(table), ident(role)))
            cur.execute(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}")
                        .format(ident(f"{table}_id_seq"), ident(role)))
            c.commit()

        for _ in range(5):                              # regression guard: prior autocommit borrows must reset
            with connection(autocommit=True) as c, c.cursor() as cur:
                cur.execute("SELECT 1")

        with tenant_connection("t-a", app_role=role) as c, c.cursor() as cur:
            cur.execute("SELECT current_role, current_setting('app.tenant_id', true)")
            current_role, guc = cur.fetchone()
            cur.execute(sql.SQL("SELECT note FROM {} ORDER BY id").format(ident(table)))
            tenant_a_rows = cur.fetchall()
            cur.execute(sql.SQL("INSERT INTO {} (tenant_id, note) VALUES ('t-a','own-write')").format(ident(table)))

        with tenant_connection("t-b", app_role=role) as c, c.cursor() as cur:
            cur.execute(sql.SQL("SELECT note FROM {} ORDER BY id").format(ident(table)))
            tenant_b_rows = cur.fetchall()

        cross_write_blocked = False
        try:
            with tenant_connection("t-a", app_role=role) as c, c.cursor() as cur:
                cur.execute(sql.SQL("INSERT INTO {} (tenant_id, note) VALUES ('t-b','bad-cross-write')")
                            .format(ident(table)))
        except Exception:
            cross_write_blocked = True

        with connection(autocommit=True) as c, c.cursor() as cur:
            cur.execute("SELECT current_role, nullif(current_setting('app.tenant_id', true),'')")
            role_after, guc_after = cur.fetchone()

        ok = (current_role == role and guc == "t-a" and tenant_a_rows == [("alpha",)]
              and tenant_b_rows == [("beta",)] and cross_write_blocked
              and role_after != role and guc_after is None)
        print(f"dbpool_role={current_role == role} dbpool_guc={guc == 't-a'} "
              f"tenant_a_only={tenant_a_rows == [('alpha',)]} tenant_b_only={tenant_b_rows == [('beta',)]} "
              f"cross_write_blocked={cross_write_blocked} role_reset={role_after != role} "
              f"guc_reset={guc_after is None}")
        print("PASS: dbpool tenant_connection runs under non-owner app role with transaction-local tenant RLS"
              if ok else "FAIL")
        return 0 if ok else 1
    finally:
        try:
            with connection(autocommit=True) as c, c.cursor() as cur:
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(ident(table)))
                cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(ident(role)))
        except Exception:
            pass


def migration_dry_run() -> int:
    """Apply the complete reviewed RLS rollout series inside one transaction, evaluate it, then roll back.

    The tenant-index preparation must precede policy enablement, while later module migrations carry their
    own RLS and index definitions.  Executing only migration 56 can therefore produce a false green against
    an already-mutated developer database and miss clean-install ordering regressions.
    """
    safe, _ = require_ddl_quiescence("migration-dry-run")
    if not safe:
        return 2
    try:
        migrations = _migration_paths()
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1
    with psycopg.connect(DB) as c:
        try:
            with c.cursor() as cur:
                for migration in migrations:
                    cur.execute(migration.read_text())
            res = evaluate(_catalog(c))
            ok = bool(res["ok"])
            summary = {
                "ok": ok,
                "missing_rls": res["missing_rls"],
                "missing_force_rls": res["missing_force_rls"],
                "missing_policy": res["missing_policy"],
                "missing_tenant_index": res["missing_tenant_index"],
                "missing_tenant_sequence_grant": res["missing_tenant_sequence_grant"],
                "special_policy_needed": res["special_policy_needed"],
                "unknown_unscoped_tables": res["unknown_unscoped_tables"],
                "global_operational_app_role_exposure": res["global_operational_app_role_exposure"],
            }
            print(json.dumps(summary, indent=2, sort_keys=True))
            print("PASS: RLS migration series dry-run makes readiness green, then rolls back"
                  if ok else "FAIL: RLS migration series dry-run still leaves readiness gaps")
            return 0 if ok else 1
        finally:
            c.rollback()


def enforced_tables_smoke() -> int:
    """Apply the reviewed RLS migration inside one transaction, seed representative REAL public tables, then
    exercise the policies as the intended non-owner roles. Rolls back everything.

    This is stronger than `migration-dry-run` (catalog-only) and stronger than throwaway table harnesses: it
    proves actual tenant_products/ai_consent generic policies plus audit_log and role_lessons special policies
    enforce tenant reads/writes on the live schema shape without permanently enabling RLS."""
    safe, _ = require_ddl_quiescence("enforced-smoke")
    if not safe:
        return 2
    migration = Path(__file__).resolve().parent.parent / "postgres" / "initdb" / "56-rls-policies.sql"
    suffix = uuid4().hex[:10]
    ta, tb = f"rls-smoke-a-{suffix}", f"rls-smoke-b-{suffix}"
    pa, pb = f"rls-smoke-prod-a-{suffix}", f"rls-smoke-prod-b-{suffix}"

    def app_case(cur, tenant, stmts, role="agentos_app"):
        out = []
        cur.execute("SAVEPOINT rls_case")
        try:
            cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            if tenant is not None:
                cur.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
            for stmt, params in stmts:
                cur.execute(stmt, params or ())
                if cur.description:
                    out.append(cur.fetchall())
            cur.execute("RESET ROLE")
            cur.execute("RELEASE SAVEPOINT rls_case")
            return out
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT rls_case")
            cur.execute("RELEASE SAVEPOINT rls_case")
            return "__blocked__"

    with psycopg.connect(DB) as c:
        try:
            with c.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS brief_cache (
                    tenant_id TEXT NOT NULL, org_id INT NOT NULL DEFAULT 0, data JSONB NOT NULL,
                    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (tenant_id, org_id))""")
                cur.execute(migration.read_text())
                cur.execute("GRANT agentos_app TO CURRENT_USER")
                cur.execute("GRANT agentos_audit_writer TO CURRENT_USER")
                cur.execute("""INSERT INTO tenants (tenant_id, name, api_token)
                               VALUES (%s,%s,%s),(%s,%s,%s)""",
                            (ta, f"RLS Smoke A {suffix}", f"tok-a-{suffix}",
                             tb, f"RLS Smoke B {suffix}", f"tok-b-{suffix}"))
                cur.execute("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s),(%s,%s)",
                            (pa, ta, pb, tb))
                cur.execute("""INSERT INTO ai_consent (tenant_id, provider, disclosure_version, accepted_at)
                               VALUES (%s,'rls-smoke','v1',now()),(%s,'rls-smoke','v1',now())""", (ta, tb))
                cur.execute("""INSERT INTO audit_log
                                  (actor, action, resource, decision, payload, prev_hash, entry_hash, tenant_id)
                               VALUES
                                  ('rls-smoke','Smoke','audit-a','allow','{}','p0','ha',%s),
                                  ('rls-smoke','Smoke','audit-b','allow','{}','ha','hb',%s),
                                  ('rls-smoke','Smoke','audit-platform','allow','{}','hb','hp',NULL)""",
                            (ta, tb))
                cur.execute("""INSERT INTO role_lessons (role, lesson, tenant_id)
                               VALUES ('rls-smoke',%s,NULL),('rls-smoke',%s,%s),('rls-smoke',%s,%s)""",
                            (f"shared-{suffix}", f"lesson-a-{suffix}", ta, f"lesson-b-{suffix}", tb))

                tp_a = app_case(cur, ta, [
                    ("SELECT product FROM tenant_products WHERE product LIKE %s ORDER BY product",
                     (f"rls-smoke-prod-%-{suffix}",)),
                ])
                tp_b = app_case(cur, tb, [
                    ("SELECT product FROM tenant_products WHERE product LIKE %s ORDER BY product",
                     (f"rls-smoke-prod-%-{suffix}",)),
                ])
                own_insert = app_case(cur, ta, [
                    ("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s)", (f"own-{pa}", ta)),
                ]) != "__blocked__"
                cross_insert_blocked = app_case(cur, ta, [
                    ("INSERT INTO tenant_products (product, tenant_id) VALUES (%s,%s)", (f"cross-{pb}", tb)),
                ]) == "__blocked__"
                consent_a = app_case(cur, ta, [
                    ("SELECT provider FROM ai_consent WHERE provider='rls-smoke' ORDER BY id", None),
                ])
                audit_app_a = app_case(cur, ta, [
                    ("SELECT resource FROM audit_log WHERE actor='rls-smoke' ORDER BY id", None),
                ])
                audit_app_insert_blocked = app_case(cur, ta, [
                    ("""INSERT INTO audit_log
                          (actor, action, resource, decision, payload, prev_hash, entry_hash, tenant_id)
                       VALUES ('rls-smoke','BadAppInsert','bad','deny','{}','x','y',%s)""", (ta,)),
                ]) == "__blocked__"
                audit_writer_tail = app_case(cur, None, [
                    ("SELECT resource FROM audit_log WHERE actor='rls-smoke' ORDER BY id DESC LIMIT 1", None),
                ], role="agentos_audit_writer")
                audit_writer_insert = app_case(cur, None, [
                    ("""INSERT INTO audit_log
                          (actor, action, resource, decision, payload, prev_hash, entry_hash, tenant_id)
                       VALUES ('rls-smoke','WriterInsert','writer-ok','allow','{}','hp','hw',%s)""", (ta,)),
                ], role="agentos_audit_writer") != "__blocked__"
                audit_writer_update_blocked = app_case(cur, None, [
                    ("UPDATE audit_log SET decision='mutated' WHERE actor='rls-smoke'", None),
                ], role="agentos_audit_writer") == "__blocked__"
                lessons_a = app_case(cur, ta, [
                    ("SELECT lesson FROM role_lessons WHERE role='rls-smoke' ORDER BY lesson", None),
                ])
                lessons_use_update = app_case(cur, ta, [
                    ("UPDATE role_lessons SET uses=uses+1 WHERE role='rls-smoke' AND lesson LIKE %s",
                     (f"%{suffix}",)),
                ]) != "__blocked__"
                lessons_text_update_blocked = app_case(cur, ta, [
                    ("UPDATE role_lessons SET lesson='mutated' WHERE role='rls-smoke'", None),
                ]) == "__blocked__"

                ok = (
                    tp_a == [[(pa,)]] and tp_b == [[(pb,)]]
                    and own_insert and cross_insert_blocked
                    and consent_a == [[("rls-smoke",)]]
                    and audit_app_a == [[("audit-a",)]]
                    and audit_app_insert_blocked
                    and audit_writer_tail == [[("audit-platform",)]]
                    and audit_writer_insert and audit_writer_update_blocked
                    and lessons_a == [[(f"lesson-a-{suffix}",), (f"shared-{suffix}",)]]
                    and lessons_use_update and lessons_text_update_blocked
                )
                print(f"tenant_products_a_only={tp_a == [[(pa,)]]} tenant_products_b_only={tp_b == [[(pb,)]]} "
                      f"own_insert={own_insert} cross_insert_blocked={cross_insert_blocked} "
                      f"consent_scoped={consent_a == [[('rls-smoke',)]]} "
                      f"audit_app_scoped={audit_app_a == [[('audit-a',)]]} "
                      f"audit_app_insert_blocked={audit_app_insert_blocked} "
                      f"audit_writer_tail={audit_writer_tail == [[('audit-platform',)]]} "
                      f"audit_writer_insert={audit_writer_insert} audit_writer_update_blocked={audit_writer_update_blocked} "
                      f"lessons_shared_and_own={lessons_a == [[(f'lesson-a-{suffix}',), (f'shared-{suffix}',)]]} "
                      f"lessons_use_update={lessons_use_update} "
                      f"lessons_text_update_blocked={lessons_text_update_blocked}")
                print("PASS: enforced real-table RLS smoke passes inside rollback transaction" if ok else "FAIL")
                return 0 if ok else 1
        finally:
            c.rollback()


def enforced_module_smoke() -> int:
    """Run real tenant-facing Python module APIs under enforced RLS in one rollback transaction.

    The migration and fixtures are applied on a single connection, then dbpool.connection is temporarily
    patched so imported modules use that same transaction. `AOS_DB_APP_ROLE=agentos_app` makes their normal
    `tenant_connection()` calls run as the non-owner app role. This proves actual app code paths, not just
    handcrafted SQL, can operate with policies enforced."""
    safe, _ = require_ddl_quiescence("enforced-module-smoke")
    if not safe:
        return 2
    migration = Path(__file__).resolve().parent.parent / "postgres" / "initdb" / "56-rls-policies.sql"
    suffix = uuid4().hex[:10]
    ta, tb = f"rls-module-a-{suffix}", f"rls-module-b-{suffix}"
    pa, pb = f"rls-module-prod-a-{suffix}", f"rls-module-prod-b-{suffix}"
    tha = 910000000 + int(suffix[:5], 16)
    thb = tha + 1
    rida, ridb = 720000000 + int(suffix[:5], 16), 730000000 + int(suffix[:5], 16)

    with psycopg.connect(DB) as c:
        try:
            with c.cursor() as cur:
                cur.execute(migration.read_text())
                cur.execute("GRANT agentos_app TO CURRENT_USER")
                cur.execute("GRANT agentos_audit_writer TO CURRENT_USER")
                cur.execute("""INSERT INTO tenants (tenant_id, name, api_token)
                               VALUES (%s,%s,%s),(%s,%s,%s)""",
                            (ta, f"RLS Module A {suffix}", f"tok-a-{suffix}",
                             tb, f"RLS Module B {suffix}", f"tok-b-{suffix}"))
                cur.execute("""INSERT INTO ai_consent (tenant_id, provider, disclosure_version, accepted_at)
                               VALUES (%s,'OpenAI','2026-06-v1',now()),
                                      (%s,'OpenAI','2026-06-v1',now())""", (ta, tb))
                cur.execute("""INSERT INTO notifications (tenant_id, channel, category, level, title, body)
                               VALUES (%s,'in_app','build','standard','A build','alpha'),
                                      (%s,'in_app','build','standard','B build','beta')""", (ta, tb))
                cur.execute("""INSERT INTO traces (run_id, product, stage, role, kind, rc, cost_usd,
                                                   tokens_in, tokens_out, elapsed_s, tenant_id)
                               VALUES (%s,%s,'BUILD','builder','agent',0,1.25,10,20,30,%s),
                                      (%s,%s,'BUILD','builder','agent',0,9.99,10,20,30,%s)""",
                            (f"run-a-{suffix}", pa, ta, f"run-b-{suffix}", pb, tb))
                cur.execute("""INSERT INTO controller_state
                               (thread_id, tenant_id, phase, awaiting, research_run_id,
                                job_kind, job_status, job_eta_min, job_started_at, execution_scope)
                               VALUES (%s,%s,'RESEARCH','fleet',%s,'research','Researching directions',10,
                                       now()-interval '2 min','test'),
                                      (%s,%s,'RESEARCH','fleet',%s,'research','Researching directions',10,
                                       now()-interval '3 min','test')""",
                            (tha, ta, rida, thb, tb, ridb))
                cur.execute("""INSERT INTO orchestra_runs (tenant_id, vision, status)
                               VALUES (%s,'tenant A research progress','running') RETURNING run_id""", (ta,))
                orca = cur.fetchone()[0]
                cur.execute("""INSERT INTO orchestra_runs (tenant_id, vision, status)
                               VALUES (%s,'tenant B research progress','running') RETURNING run_id""", (tb,))
                orcb = cur.fetchone()[0]
                for tenant, orc, rid, statuses in (
                    (ta, orca, rida, (("done", "competitor pricing"),
                                      ("working", "payment providers"),
                                      ("blocked", "regulatory edge cases"))),
                    (tb, orcb, ridb, (("working", "tenant b private research"),)),
                ):
                    cur.execute("""INSERT INTO orchestra_actors
                                   (run_id, tenant_id, name, role, kind, status, assignment, memory)
                                   VALUES (%s,%s,'research-coordinator','research-coordinator',
                                           'supervisor','working','coordinate research',%s::jsonb)""",
                                (orc, tenant, json.dumps({"research_run_id": rid})))
                    for i, (status, assignment) in enumerate(statuses, 1):
                        cur.execute("""INSERT INTO orchestra_actors
                                       (run_id, tenant_id, name, role, kind, status, assignment)
                                       VALUES (%s,%s,%s,'research-growth','worker',%s,%s)""",
                                    (orc, tenant, f"researcher-{i:02d}", status, assignment))

            import dbpool as _dbpool
            import tenancy as _tenancy
            import consent as _consent
            import notifications as _notifications
            import chiefofstaff as _chief
            import approvals as _approvals
            import cockpit as _cockpit
            import loopcontroller as _loop
            import audit as _audit

            real_env_role = os.environ.get("AOS_DB_APP_ROLE")
            real_env_audit_role = os.environ.get("AOS_DB_AUDIT_ROLE")
            real_dbpool_conn = _dbpool.connection
            real_tenancy_conn = _tenancy.connection
            real_consent_conn = _consent.connection
            real_notifications_conn = _notifications.connection
            real_chief_conn = _chief.connection
            real_approvals_inbox = _approvals.inbox
            real_cockpit_company_summary = _cockpit.company_summary
            real_cockpit_health = _cockpit.health
            real_audit_conn = _audit.connection

            @contextmanager
            def same_tx_connection(autocommit=False):
                try:
                    yield c
                finally:
                    try:
                        with c.cursor() as cur:
                            cur.execute("RESET ROLE")
                            cur.execute("SELECT set_config('app.tenant_id', '', true)")
                    except Exception:
                        pass

            try:
                os.environ["AOS_DB_APP_ROLE"] = "agentos_app"
                os.environ["AOS_DB_AUDIT_ROLE"] = "agentos_audit_writer"
                _dbpool.connection = same_tx_connection
                _tenancy.connection = same_tx_connection
                _consent.connection = same_tx_connection
                _notifications.connection = same_tx_connection
                _chief.connection = same_tx_connection
                _approvals.inbox = lambda tid: {"items": [], "count": 0}
                _cockpit.company_summary = lambda tid, org_id=0: {"verdict": "healthy"}
                _cockpit.health = lambda tid, org_id=0: {"verdict": "healthy"}
                _audit.connection = same_tx_connection

                _tenancy.register_product(pa, ta)
                _tenancy.register_product(pb, tb)
                a_products = _tenancy.products_of(ta)
                b_products = _tenancy.products_of(tb)
                a_owns_a = _tenancy.owns(ta, pa)
                a_owns_b = _tenancy.owns(ta, pb)
                b_owns_b = _tenancy.owns(tb, pb)
                a_consent = _consent.require_consent(ta, "OpenAI")
                b_consent = _consent.require_consent(tb, "OpenAI")
                a_state = _consent.state(ta, "OpenAI")
                _consent.revoke(ta, "OpenAI")
                revoked = not _consent.require_consent(ta, "OpenAI")
                _consent.record(ta, "OpenAI")
                rerecorded = _consent.require_consent(ta, "OpenAI")
                with c.cursor() as cur:
                    cur.execute("""SELECT action FROM audit_log
                                   WHERE actor='consent' AND tenant_id=%s
                                     AND action IN ('ConsentRevoked','ConsentAccepted')
                                   ORDER BY id DESC LIMIT 2""", (ta,))
                    consent_audit = [r[0] for r in cur.fetchall()]
                a_feed = _notifications.feed(ta)
                b_feed = _notifications.feed(tb)
                a_badge_before = _notifications.unread_count(ta)
                _notifications.mark_read(ta, a_feed[0]["id"])
                a_badge_after = _notifications.unread_count(ta)
                chief_facts = _chief._facts(ta, 0)
                _chief._cache_put(ta, 0, {"headline": "Scoped brief", "needs_you": [],
                                          "team_did": ["Build facts loaded"], "watch": [],
                                          "suggestion": "Keep going"})
                chief_cached = _chief._cache_get(ta, 0)
                chief_brief = _chief.brief(ta, 0, use_cache=True)
                live_a = _loop.live_status(tha, ta)
                live_a_from_b = _loop.live_status(tha, tb)
            finally:
                _dbpool.connection = real_dbpool_conn
                _tenancy.connection = real_tenancy_conn
                _consent.connection = real_consent_conn
                _notifications.connection = real_notifications_conn
                _chief.connection = real_chief_conn
                _approvals.inbox = real_approvals_inbox
                _cockpit.company_summary = real_cockpit_company_summary
                _cockpit.health = real_cockpit_health
                _audit.connection = real_audit_conn
                if real_env_role is None:
                    os.environ.pop("AOS_DB_APP_ROLE", None)
                else:
                    os.environ["AOS_DB_APP_ROLE"] = real_env_role
                if real_env_audit_role is None:
                    os.environ.pop("AOS_DB_AUDIT_ROLE", None)
                else:
                    os.environ["AOS_DB_AUDIT_ROLE"] = real_env_audit_role

            chief_portfolio = (chief_facts.get("portfolio") or {}) if isinstance(chief_facts, dict) else {}
            chief_spend = (chief_facts.get("spend") or {}) if isinstance(chief_facts, dict) else {}
            chief_facts_ok = (chief_portfolio.get("products_touched_30d") == 1
                              and chief_spend.get("cost_usd_30d") == 1.25)
            chief_cache_ok = bool(chief_cached and chief_cached.get("headline") == "Scoped brief"
                                  and chief_brief.get("_cached"))
            chief_live_overlay_ok = (
                "active workstream" in (chief_brief.get("headline") or "")
                and any("RESEARCH" in line for line in (chief_brief.get("team_did") or []))
            )
            live_progress_ok = (live_a.get("running") is True
                                and live_a.get("researchers_total") == 3
                                and live_a.get("researchers_done") == 1
                                and live_a.get("researchers_blocked") == 1
                                and "payment providers" in (live_a.get("progress_detail") or "")
                                and live_a_from_b.get("error") == "no such thread")

            ok = (a_products == [pa] and b_products == [pb] and a_owns_a and not a_owns_b and b_owns_b
                  and a_consent and b_consent and a_state.get("accepted") is True
                  and revoked and rerecorded and consent_audit == ["ConsentAccepted", "ConsentRevoked"]
                  and len(a_feed) == 1 and a_feed[0]["title"] == "A build"
                  and len(b_feed) == 1 and b_feed[0]["title"] == "B build"
                  and a_badge_before == 1 and a_badge_after == 0
                  and chief_facts_ok and chief_cache_ok and chief_live_overlay_ok
                  and live_progress_ok)
            print(f"tenancy_a_products={a_products == [pa]} tenancy_b_products={b_products == [pb]} "
                  f"owns_self={a_owns_a and b_owns_b} cross_owns_denied={not a_owns_b} "
                  f"consent_a={a_consent} consent_b={b_consent} state_accepted={a_state.get('accepted') is True} "
                  f"audited_consent_flow={revoked and rerecorded and consent_audit == ['ConsentAccepted', 'ConsentRevoked']} "
                  f"notifications_a_only={len(a_feed) == 1 and a_feed[0]['title'] == 'A build'} "
                  f"notifications_b_only={len(b_feed) == 1 and b_feed[0]['title'] == 'B build'} "
                  f"badge_read_flow={a_badge_before == 1 and a_badge_after == 0} "
                  f"chief_facts_scoped={chief_facts_ok} chief_cache_scoped={chief_cache_ok} "
                  f"chief_live_overlay={chief_live_overlay_ok} "
                  f"live_research_progress_scoped={live_progress_ok}")
            print("PASS: enforced module smoke proves real tenant APIs run under app-role RLS"
                  if ok else "FAIL")
            return 0 if ok else 1
        finally:
            c.rollback()


def rollout_gate() -> int:
    """One no-spend preflight gate for a DB-enforced RLS rollout.

    This deliberately does NOT apply RLS permanently. It answers the operator question: "is the current
    code/schema shape ready to try `56-rls-policies.sql` on a staging DB using the app-role path?" The live
    DB can still report red until the migration is intentionally applied; this gate fails only when the
    migration shape, enforced smokes, app-role primitive, or static app-path review is not clean enough for
    staging.
    """
    checks: list[tuple[str, bool]] = []

    print("== RLS rollout gate: migration dry-run ==")
    checks.append(("migration_dry_run", migration_dry_run() == 0))

    print("\n== RLS rollout gate: enforced real-table smoke ==")
    checks.append(("enforced_smoke", enforced_tables_smoke() == 0))

    print("\n== RLS rollout gate: enforced module smoke ==")
    checks.append(("enforced_module_smoke", enforced_module_smoke() == 0))

    print("\n== RLS rollout gate: dbpool app-role harness ==")
    checks.append(("app_role_harness", app_role_runtime_harness() == 0))

    print("\n== RLS rollout gate: static app-path review ==")
    try:
        import rls_app_paths
        scan = rls_app_paths.scan()
        allowed = {"scripts/rls_readiness.py", "scripts/rls_app_paths.py", "scripts/test_tenant_isolation.py"}
        direct_files = {r["file"] for r in scan["direct_connect_files"]}
        runtime_direct = sorted(direct_files - allowed)
        scanner_ok = not runtime_direct
        print(json.dumps({
            "direct_connect_refs": scan["summary"]["direct_connect_refs"],
            "files_with_direct_connect_refs": scan["summary"]["files_with_direct_connect_refs"],
            "runtime_direct_connect_files": runtime_direct,
            "allowed_tooling_direct_connect_files": sorted(direct_files & allowed),
            "tenant_connection_refs": scan["summary"]["tenant_connection_refs"],
            "pooled_connection_refs": scan["summary"]["pooled_connection_refs"],
        }, indent=2, sort_keys=True))
    except Exception as e:
        scanner_ok = False
        print(json.dumps({"error": str(e)[:300]}, indent=2, sort_keys=True))
    checks.append(("static_app_path_review", scanner_ok))

    print("\n== RLS rollout gate summary ==")
    for name, ok in checks:
        print(("PASS" if ok else "FAIL") + f": {name}")
    ok = all(v for _, v in checks)
    print("PASS: RLS rollout gate is green for a staging apply attempt"
          if ok else "FAIL: RLS rollout gate is not green; do not apply policies")
    return 0 if ok else 1


def _selftest() -> int:
    states = [
        TableState("tenant_products", {"product", "tenant_id"}, True, True, 1, True),
        TableState("audit_log", {"id", "tenant_id", "entry_hash"}, True, True, 1, True),
        TableState("task_board", {"id", "tenant", "title"}, True, True, 1, True),
        TableState("chat_messages", {"id", "thread_id", "tenant_id"}, True, True, 1, True),
        TableState("traces", {"id", "product", "run_id"}, False, False, 0, False),
        TableState("research_options", {"id", "run_id", "title"}, False, False, 0, False),
        TableState("schedules", {"name", "command"}, False, False, 0, False),
        TableState("accountability_sweep_state", {"name", "version"}, False, False, 0, False),
        TableState("agent_slots", {"slot_id", "holder", "owner_token"}, False, False, 0, False),
        TableState("app_spend_reservations", {"token", "app", "amount"}, False, False, 0, False),
        TableState("auth_rate_limits", {"action", "key_hash", "bucket"}, False, False, 0, False),
        TableState("factory_resume_claims", {"product", "claim_token"}, False, False, 0, False),
        TableState("factory_resume_sweep_state", {"singleton", "cursor_generation_ts", "cursor_run_id"},
                   False, False, 0, False),
        TableState("host_resource_leases", {"lease_id", "holder", "owner_token"}, False, False, 0, False),
        TableState("qa_evidence_encoding_jobs", {"id", "source_path", "status"}, False, False, 0, False),
        TableState("scheduler_claims", {"claim_token", "name", "status"}, False, False, 0, False),
        TableState("tenant_providers", {"tenant_id", "provider"}, True, False, 1, True),
        TableState("role_lessons", {"role", "lesson", "tenant_id"}, True, True, 1, True),
        TableState("mystery", {"id", "payload"}, False, False, 0, False),
    ]
    res = evaluate(states)
    audit_special_ok = TableState("audit_log", {"id", "tenant_id", "entry_hash"}, True, True, 2, True,
                                  {"audit_log_tenant_read", "audit_log_writer_append"})
    lessons_special_ok = TableState("role_lessons", {"role", "lesson", "tenant_id"}, True, True, 3, True,
                                    {"role_lessons_shared_read", "role_lessons_tenant_insert",
                                     "role_lessons_use_increment"})
    checks = {
        "good tenant table accepted": "tenant_products" not in res["missing_rls"],
        "tenant alias accepted": res["tenant_alias_tables"] == {"task_board": "tenant"},
        "force RLS required": res["missing_force_rls"] == ["tenant_providers"],
        "product-only table flagged": "traces" in res["indirect_scope_needs_tenant_id"],
        "parent-scoped child flagged": (
            res["parent_scope_needs_join_policy"].get("research_options")
            == "research_runs.id -> research_options.run_id"
        ),
        "special policy table flagged": "audit_log" in res["special_policy_needed"],
        "shared lesson special policy flagged": "role_lessons" in res["special_policy_needed"],
        "satisfied audit special policy accepted": _special_policy_satisfied(audit_special_ok),
        "satisfied shared lesson special policy accepted": _special_policy_satisfied(lessons_special_ok),
        "platform exemption honored": "schedules" not in res["unknown_unscoped_tables"],
        "global operational exemptions honored": not ({"accountability_sweep_state", "agent_slots",
        "app_spend_reservations", "auth_rate_limits", "factory_resume_claims",
                                                          "factory_resume_sweep_state", "forecast_sweep_state",
                                                          "host_resource_leases", "qa_evidence_encoding_jobs",
                                                          "scheduler_claims"}
                                                        & set(res["unknown_unscoped_tables"])),
        "global operational rationale reported": (
            set(res["global_operational_exemptions"]) == set(GLOBAL_OPERATIONAL_EXEMPTIONS)
            and all("owner-only" in reason
                    for reason in res["global_operational_exemptions"].values())
        ),
        "global operational exposure fails closed": (
            evaluate([TableState("agent_slots", {"slot_id"}, app_role_access=True)])["ok"] is False
        ),
        "unknown unscoped table flagged": res["unknown_unscoped_tables"] == ["mystery"],
        "overall fails closed": res["ok"] is False,
        "policy SQL contains FORCE RLS": any("FORCE ROW LEVEL SECURITY" in x
                                            for x in _policy_statements(states, "app_role")),
        "policy SQL omits audit_log generic policy": not any('"audit_log"' in x
                                                            for x in _policy_statements(states, "app_role")),
        "audit policy SQL grants writer insert only": (
            any("GRANT SELECT, INSERT" in x for x in _audit_policy_statements("app_role", "audit_writer"))
            and not any("GRANT UPDATE" in x for x in _audit_policy_statements("app_role", "audit_writer"))
        ),
        "role lessons policy SQL grants uses-only update": (
            any("GRANT UPDATE (uses)" in x for x in _role_lessons_policy_statements("app_role"))
            and not any("GRANT UPDATE ON" in x for x in _role_lessons_policy_statements("app_role"))
        ),
    }
    for label, ok in checks.items():
        print(("PASS" if ok else "FAIL") + f": {label}")
    all_ok = all(checks.values())
    print("PASS: RLS readiness checker classifies tenant, indirect, exempt, and unknown tables" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "selftest":
        sys.exit(_selftest())
    if cmd == "report":
        sys.exit(report())
    if cmd == "quiescence":
        sys.exit(quiescence())
    if cmd == "apply-indexes":
        sys.exit(apply_indexes())
    if cmd == "policy-sql":
        sys.exit(policy_sql(sys.argv[2] if len(sys.argv) > 2 else "agentos_app"))
    if cmd == "audit-policy-sql":
        sys.exit(audit_policy_sql(sys.argv[2] if len(sys.argv) > 2 else "agentos_app",
                                  sys.argv[3] if len(sys.argv) > 3 else "agentos_audit_writer"))
    if cmd == "role-lessons-policy-sql":
        sys.exit(role_lessons_policy_sql(sys.argv[2] if len(sys.argv) > 2 else "agentos_app"))
    if cmd == "harness":
        sys.exit(harness())
    if cmd == "audit-harness":
        sys.exit(audit_harness())
    if cmd == "role-lessons-harness":
        sys.exit(role_lessons_harness())
    if cmd == "app-role-harness":
        sys.exit(app_role_runtime_harness())
    if cmd == "migration-dry-run":
        sys.exit(migration_dry_run())
    if cmd == "enforced-smoke":
        sys.exit(enforced_tables_smoke())
    if cmd == "enforced-module-smoke":
        sys.exit(enforced_module_smoke())
    if cmd == "rollout-gate":
        sys.exit(rollout_gate())
    sys.exit("usage: rls_readiness.py report|quiescence|apply-indexes|policy-sql [app_role]|"
             "audit-policy-sql [app_role] [writer_role]|role-lessons-policy-sql [app_role]|"
             "harness|audit-harness|role-lessons-harness|app-role-harness|migration-dry-run|"
             "enforced-smoke|enforced-module-smoke|rollout-gate|selftest")
