from __future__ import annotations

from sqlalchemy import text

from .db import Database
from .runner_access import runner_sql


async def record_runner_health_failure(
    database: Database, component: str, detail: str
) -> None:
    async with database.sessions() as session:
        await session.execute(
            runner_sql(text(
                "INSERT INTO runner_health "
                "(component, status, detail, first_failed_at, last_failed_at, "
                "consecutive_failures, resolved_at) "
                "VALUES (:component, 'failed', :detail, now(), now(), 1, NULL) "
                "ON CONFLICT (component) DO UPDATE SET "
                "status = 'failed', detail = EXCLUDED.detail, "
                "last_failed_at = EXCLUDED.last_failed_at, "
                "consecutive_failures = runner_health.consecutive_failures + 1, "
                "resolved_at = NULL"
            )),
            {"component": component, "detail": detail[:2000]},
        )
        await session.commit()


async def clear_runner_health(database: Database, component: str) -> None:
    await record_runner_health_success(database, component)


async def record_runner_health_success(database: Database, component: str) -> None:
    async with database.sessions() as session:
        await session.execute(
            runner_sql(text(
                "INSERT INTO runner_health "
                "(component, status, detail, first_failed_at, last_failed_at, "
                "consecutive_failures, resolved_at, last_succeeded_at) "
                "VALUES (:component, 'healthy', 'reminder sweep completed', "
                "now(), now(), 0, NULL, now()) "
                "ON CONFLICT (component) DO UPDATE SET "
                "status = 'healthy', detail = 'reminder sweep completed', "
                "consecutive_failures = 0, resolved_at = now(), "
                "last_succeeded_at = now()"
            )),
            {"component": component},
        )
        await session.commit()


async def recent_runner_health(database: Database) -> list[dict[str, object]]:
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT component, status, detail, first_failed_at, last_failed_at, "
                "consecutive_failures, last_succeeded_at FROM runner_health "
                "ORDER BY COALESCE(last_succeeded_at, last_failed_at) DESC"
            )
        )
        return [dict(row) for row in result.mappings()]
