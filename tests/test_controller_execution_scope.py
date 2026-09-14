import uuid
import re
import sys
from pathlib import Path

import psycopg
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def test_jobd_production_discovery_cannot_see_test_controller_rows(monkeypatch):
    """The always-on daemon must exclude test work in SQL, not by process-local convention."""
    import jobd
    from aoscfg import DB

    schema = f"scope_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DB, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'''CREATE TABLE "{schema}".controller_state (
            thread_id bigint primary key, phase text, awaiting text,
            execution_scope text, updated_at timestamptz)''')
        cur.execute(f'''CREATE TABLE "{schema}".controller_jobs (
            id bigserial primary key, status text, execution_scope text)''')
        cur.execute(f'''INSERT INTO "{schema}".controller_state VALUES
            (11, 'IMPLEMENT', NULL, 'production', now()-interval '1 minute'),
            (12, 'IMPLEMENT', NULL, 'test', now()-interval '1 minute')''')
        # Test load must not consume the production controller capacity budget.
        cur.execute(f'''INSERT INTO "{schema}".controller_jobs(status,execution_scope)
                       VALUES ('running','test')''')

    def scoped_connection():
        return psycopg.connect(DB, options=f"-c search_path={schema}")

    monkeypatch.setattr(jobd, "connection", scoped_connection)
    monkeypatch.setattr(jobd, "_dispatch_limit", lambda active: max(0, 2 - active))
    try:
        assert jobd.runnable_threads("production") == [11]
        assert jobd.queued_runnables(execution_scope="production") == [11]
        assert jobd.runnable_threads("test") == [12]
        assert jobd.queued_runnables(execution_scope="test") == [12]
        with pytest.raises(ValueError):
            jobd.runnable_threads("unknown")
    finally:
        with psycopg.connect(DB, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_controller_rejects_unknown_execution_scope_before_any_write():
    import loopcontroller

    with pytest.raises(ValueError):
        loopcontroller.start("never-written", 1, execution_scope="diagnostic-ish")


def test_execution_scope_migration_defines_queue_and_job_boundaries():
    sql = (Path(__file__).resolve().parents[1]
           / "postgres/initdb/73-controller-execution-scope.sql").read_text()
    assert "controller_state_execution_scope_check" in sql
    assert "controller_jobs_execution_scope_check" in sql
    assert "controller_state_execution_queue_idx" in sql
    assert "controller_jobs_execution_active_idx" in sql


def test_every_direct_controller_test_fixture_declares_execution_scope():
    """A live production daemon may share this database while pytest runs.

    A test fixture that accepts the schema's production default is real daemon
    work, so the daemon can drive or reap it between two assertions. Keep the
    isolation boundary visible in every direct INSERT instead of relying on
    timing or on stopping production services for the test suite.
    """
    tests = Path(__file__).resolve().parent
    direct_insert = re.compile(
        r"INSERT\s+INTO\s+controller_(?:state|jobs)\s*\((.*?)\)\s*VALUES",
        re.IGNORECASE | re.DOTALL,
    )
    missing = []
    for path in sorted(tests.glob("test*.py")):
        source = path.read_text()
        for match in direct_insert.finditer(source):
            if "execution_scope" not in match.group(1).lower():
                line = source[:match.start()].count("\n") + 1
                missing.append(f"{path.name}:{line}")
    assert missing == [], f"controller fixtures defaulted into production scope: {missing}"


def test_production_management_claims_require_a_live_tenant_and_production_contract_scope():
    management = (SCRIPTS / "management.py").read_text()
    claim = management.split("def _claim(owner:", 1)[1].split("def _claim_one", 1)[0]
    assert "JOIN tenants t ON t.tenant_id=m.tenant_id" in claim
    assert "COALESCE(w.constraints->>'execution_scope','production')='production'" in management
    contracts = (SCRIPTS / "workcontracts.py").read_text()
    assert "JOIN tenants t ON t.tenant_id=w.tenant_id" in contracts
    assert "constraints->>'execution_scope'" in contracts
