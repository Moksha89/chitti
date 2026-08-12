from __future__ import annotations

from sqlalchemy import text

from .db import Database


async def recent_notifications(
    database: Database,
    namespace: str,
    limit: int = 50,
    include_acknowledged: bool = False,
) -> list[dict[str, object]]:
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT n.id, n.kind, n.title, n.body, n.created_at, "
                "a.acknowledged_at FROM notifications n "
                "LEFT JOIN notification_acknowledgements a ON a.notification_id = n.id "
                "WHERE n.namespace = :namespace "
                + (
                    ""
                    if include_acknowledged
                    else "AND a.notification_id IS NULL "
                )
                + "ORDER BY n.id DESC LIMIT :limit"
            ),
            {"namespace": namespace, "limit": limit},
        )
        return [dict(row) for row in result.mappings()]


async def notifications_after(
    database: Database,
    namespace: str,
    cursor: int,
    include_acknowledged: bool = False,
) -> list[dict[str, object]]:
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT n.id, n.kind, n.title, n.body, n.created_at, "
                "a.acknowledged_at FROM notifications n "
                "LEFT JOIN notification_acknowledgements a ON a.notification_id = n.id "
                "WHERE n.namespace = :namespace AND n.id > :cursor "
                + (
                    ""
                    if include_acknowledged
                    else "AND a.notification_id IS NULL "
                )
                + "ORDER BY n.id"
            ),
            {"namespace": namespace, "cursor": cursor},
        )
        return [dict(row) for row in result.mappings()]


async def acknowledge_notification(database: Database, namespace: str, notification_id: int) -> bool:
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "INSERT INTO notification_acknowledgements (notification_id) "
                "SELECT id FROM notifications WHERE id = :id AND namespace = :namespace "
                "ON CONFLICT DO NOTHING RETURNING notification_id"
            ),
            {"id": notification_id, "namespace": namespace},
        )
        await session.commit()
        return result.scalar_one_or_none() is not None
