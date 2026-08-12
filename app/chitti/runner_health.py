from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from sqlalchemy import text

from .db import Database


async def record_runner_health_failure(
    database: Database, component: str, detail: str
) -> None:
    async with database.sessions() as session:
        await session.execute(
            text(
                "INSERT INTO runner_health "
                "(component, status, detail, first_failed_at, last_failed_at, "
                "consecutive_failures, resolved_at) "
                "VALUES (:component, 'failed', :detail, now(), now(), 1, NULL) "
                "ON CONFLICT (component) DO UPDATE SET "
                "status = 'failed', detail = EXCLUDED.detail, "
                "last_failed_at = EXCLUDED.last_failed_at, "
                "consecutive_failures = runner_health.consecutive_failures + 1, "
                "resolved_at = NULL"
            ),
            {"component": component, "detail": detail[:2000]},
        )
        await session.commit()


async def clear_runner_health(database: Database, component: str) -> None:
    async with database.sessions() as session:
        await session.execute(
            text(
                "UPDATE runner_health SET status = 'healthy', "
                "resolved_at = now(), consecutive_failures = 0 "
                "WHERE component = :component AND status = 'failed'"
            ),
            {"component": component},
        )
        await session.commit()


async def recent_runner_health(database: Database) -> list[Mapping[str, object]]:
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT component, status, detail, first_failed_at, last_failed_at, "
                "consecutive_failures FROM runner_health "
                "WHERE status = 'failed' ORDER BY last_failed_at DESC"
            )
        )
        return cast(list[Mapping[str, object]], list(result.mappings()))
