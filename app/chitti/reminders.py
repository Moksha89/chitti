from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from .db import Database

logger = logging.getLogger("chitti.reminders")


def next_due(due_at: datetime, recurrence: str | None) -> datetime | None:
    if recurrence == "daily":
        return due_at + timedelta(days=1)
    if recurrence == "weekly":
        return due_at + timedelta(days=7)
    return None


async def sweep_reminders(database: Database, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    fired = 0
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT id, namespace, text, due_at, recurrence "
                "FROM reminders WHERE active = true AND "
                "(recurrence IS NOT NULL OR NOT EXISTS ("
                "  SELECT 1 FROM reminder_occurrences o WHERE o.reminder_id = reminders.id"
                ")) AND due_at <= :now ORDER BY id"
            ),
            {"now": now},
        )
        reminders = list(result.mappings())
    for reminder in reminders:
        try:
            due_at = reminder["due_at"]
            recurrence = str(reminder["recurrence"]) if reminder["recurrence"] else None
            while due_at <= now:
                async with database.sessions() as session:
                    inserted = await session.execute(
                        text(
                            "INSERT INTO reminder_occurrences (reminder_id, due_at) "
                            "VALUES (:reminder, :due_at) ON CONFLICT DO NOTHING "
                            "RETURNING id"
                        ),
                        {"reminder": int(reminder["id"]), "due_at": due_at},
                    )
                    occurrence_id = inserted.scalar_one_or_none()
                    if occurrence_id is not None:
                        await session.execute(
                            text(
                                "INSERT INTO notifications "
                                "(namespace, kind, title, body, reminder_occurrence_id) "
                                "VALUES (:namespace, 'reminder', 'Reminder', :body, :occurrence)"
                            ),
                            {
                                "namespace": str(reminder["namespace"]),
                                "body": str(reminder["text"]),
                                "occurrence": int(occurrence_id),
                            },
                        )
                        await session.commit()
                        fired += 1
                    else:
                        await session.rollback()
                next_value = next_due(due_at, recurrence)
                if next_value is None:
                    break
                due_at = next_value
        except Exception:
            logger.exception("reminder sweep failed for reminder %s", reminder["id"])
    return fired


async def create_reminder(
    database: Database,
    namespace: str,
    text_value: str,
    due_at: datetime,
    recurrence: str | None,
) -> int:
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "INSERT INTO reminders (namespace, text, due_at, recurrence) "
                "VALUES (:namespace, :text, :due_at, :recurrence) RETURNING id"
            ),
            {
                "namespace": namespace,
                "text": text_value,
                "due_at": due_at,
                "recurrence": recurrence,
            },
        )
        reminder_id = int(result.scalar_one())
        await session.commit()
        return reminder_id


async def recent_reminders(database: Database, namespace: str) -> list[dict[str, object]]:
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT id, text, due_at, recurrence, active FROM reminders "
                "WHERE namespace = :namespace AND active = true "
                "ORDER BY due_at, id LIMIT 50"
            ),
            {"namespace": namespace},
        )
        return [dict(row) for row in result.mappings()]
