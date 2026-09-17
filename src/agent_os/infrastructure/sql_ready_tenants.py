"""Least-privilege discovery of tenants with ready V2 queue work."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from agent_os.application.ports import ReadyTenantSource
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
    FROM aos_v2_decision_responses
    WHERE (status = 'pending' AND available_at <= :now)
       OR (status = 'executing' AND lease_expires_at < :now)
)
SELECT tenant_id
FROM ready_tenants
ORDER BY CASE WHEN tenant_id > :after_tenant_id THEN 0 ELSE 1 END, tenant_id
LIMIT :limit
""")


class SQLReadyTenantSource(ReadyTenantSource):
    """Round-robin ready-tenant projection across lifecycle and graph queues."""

    def __init__(self, database_url: str) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        self._engine = create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)

    @contextmanager
    def _connection(self):
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL ROLE agentos_worker"))
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

    def close(self) -> None:
        self._engine.dispose()
