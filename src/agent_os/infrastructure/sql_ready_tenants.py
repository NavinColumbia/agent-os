"""Least-privilege discovery of tenants with ready V2 queue work."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from agent_os.application.ports import ReadyTenantSource, ReadyWorkSummary
from agent_os.infrastructure.dbos_lifecycle import sqlalchemy_url


_READY_TENANTS = text("""
WITH ready_tenants AS (
    SELECT tenant_id
    FROM aos_v2_lifecycle_commands
    WHERE (status = 'pending' AND available_at <= :now)
       OR (status = 'executing' AND lease_expires_at < :now)
    UNION
    SELECT tenant_id
    FROM aos_v2_workflow_actions
    WHERE (status = 'pending' AND available_at <= :now)
       OR (status = 'executing' AND lease_expires_at < :now)
    UNION
    SELECT tenant_id
    FROM aos_v2_management_watches
    WHERE (status = 'pending' AND next_check_at <= :now)
       OR (status = 'executing' AND lease_expires_at < :now)
    UNION
    SELECT tenant_id
    FROM aos_v2_notification_deliveries
    WHERE (status = 'pending' AND available_at <= :now)
       OR (status = 'executing' AND lease_expires_at < :now)
    UNION
    SELECT tenant_id
    FROM aos_v2_web_push_deliveries
    WHERE (status = 'pending' AND available_at <= :now)
       OR (status = 'executing' AND lease_expires_at < :now)
    UNION
    SELECT tenant_id
    FROM aos_v2_decision_responses
    WHERE (status = 'pending' AND available_at <= :now)
       OR (status = 'executing' AND lease_expires_at < :now)
)
SELECT tenant_id
FROM ready_tenants
ORDER BY CASE WHEN tenant_id > :after_tenant_id THEN 0 ELSE 1 END, tenant_id
LIMIT :limit
""")


def _ready_work_query() -> str:
    branches: list[str] = []
    queues = (
        ("lifecycle", "aos_v2_lifecycle_commands", "available_at"),
        ("workflow", "aos_v2_workflow_actions", "available_at"),
        ("management", "aos_v2_management_watches", "next_check_at"),
        ("notification", "aos_v2_notification_deliveries", "available_at"),
        ("web_push", "aos_v2_web_push_deliveries", "available_at"),
        ("decision", "aos_v2_decision_responses", "available_at"),
    )
    for kind, table_name, ready_column in queues:
        branches.extend((
            f"""SELECT '{kind}' AS queue_kind, due_at
                  FROM (SELECT {ready_column} AS due_at
                          FROM {table_name}
                         WHERE status = 'pending' AND {ready_column} <= :now
                         ORDER BY {ready_column}
                         LIMIT :branch_limit) AS {kind}_pending""",
            f"""SELECT '{kind}' AS queue_kind, due_at
                  FROM (SELECT lease_expires_at AS due_at
                          FROM {table_name}
                         WHERE status = 'executing' AND lease_expires_at < :now
                         ORDER BY lease_expires_at
                         LIMIT :branch_limit) AS {kind}_expired""",
        ))
    return """
WITH bounded AS (
    {branches}
), sample AS (
    SELECT queue_kind, due_at
      FROM bounded
     ORDER BY due_at, queue_kind
     LIMIT :sample_limit
)
SELECT COUNT(*) AS sampled_count,
       MIN(due_at) AS oldest_ready_at,
       (SELECT queue_kind FROM sample ORDER BY due_at, queue_kind LIMIT 1)
           AS oldest_queue_kind
  FROM sample
""".format(branches="\n    UNION ALL\n    ".join(branches))


_READY_WORK = text(_ready_work_query())


class SQLReadyTenantSource(ReadyTenantSource):
    """Round-robin ready-tenant projection across lifecycle and graph queues."""

    def __init__(self, database_url: str, *, statement_timeout_seconds: int = 10) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        if not 1 <= statement_timeout_seconds <= 60:
            raise ValueError("statement_timeout_seconds must be between 1 and 60")
        self._statement_timeout_ms = statement_timeout_seconds * 1_000
        normalized_url = sqlalchemy_url(database_url)
        connect_args = (
            {"connect_timeout": min(statement_timeout_seconds, 10)}
            if normalized_url.startswith("postgresql") else {}
        )
        self._engine = create_engine(
            normalized_url, pool_pre_ping=True, connect_args=connect_args,
        )

    @contextmanager
    def _connection(self):
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_worker"))
                connection.execute(
                    text("SELECT set_config('statement_timeout', :timeout, true)"),
                    {"timeout": str(self._statement_timeout_ms)},
                )
            yield connection

    def list_ready_tenants(
        self,
        *,
        after_tenant_id: str | None = None,
        limit: int = 128,
    ) -> tuple[str, ...]:
        if limit < 1 or limit > 1_000:
            raise ValueError("ready tenant discovery limit must be between 1 and 1000")
        cursor = after_tenant_id or ""
        if len(cursor) > 128 or "\0" in cursor:
            raise ValueError("ready tenant cursor is invalid")
        with self._connection() as connection:
            rows = connection.execute(
                _READY_TENANTS,
                {
                    "now": datetime.now(timezone.utc),
                    "after_tenant_id": cursor,
                    "limit": limit,
                },
            ).scalars().all()
        return tuple(str(item) for item in rows)

    def summarize_ready_work(self, *, limit: int = 1_000) -> ReadyWorkSummary:
        if limit < 100 or limit > 10_000:
            raise ValueError("ready work sample limit must be between 100 and 10000")
        observed_at = datetime.now(timezone.utc)
        with self._connection() as connection:
            row = connection.execute(
                _READY_WORK,
                {
                    "now": observed_at,
                    "branch_limit": limit + 1,
                    "sample_limit": limit + 1,
                },
            ).mappings().one()
        sampled_count = int(row["sampled_count"])
        oldest = row["oldest_ready_at"]
        if isinstance(oldest, str):
            oldest = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
        if oldest is not None and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        return ReadyWorkSummary(
            observed_at=observed_at,
            ready_count_capped=min(sampled_count, limit),
            truncated=sampled_count > limit,
            oldest_ready_at=oldest,
            oldest_queue_kind=(
                str(row["oldest_queue_kind"])
                if row["oldest_queue_kind"] is not None else None
            ),
        )

    def close(self) -> None:
        self._engine.dispose()
