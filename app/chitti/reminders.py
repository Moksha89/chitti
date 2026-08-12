from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from .db import Database

logger = logging.getLogger("chitti.reminders")


def next_due(
    due_at: datetime, recurrence: str | None, timezone_name: str = "UTC"
) -> datetime | None:
    if recurrence is None:
        return None
    local_due = due_at.astimezone(ZoneInfo(timezone_name))
    if recurrence == "daily":
        return (local_due + timedelta(days=1)).astimezone(UTC)
    if recurrence == "weekly":
        return (local_due + timedelta(days=7)).astimezone(UTC)
    return None


async def sweep_reminders(
    database: Database,
    now: datetime | None = None,
    timezone_name: str = "UTC",
) -> int:
    now = now or datetime.now(UTC)
    fired = 0
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT r.id, r.namespace, r.text, r.due_at, r.recurrence, "
                "MAX(o.due_at) AS last_due "
                "FROM reminders r LEFT JOIN reminder_occurrences o "
                "ON o.reminder_id = r.id "
                "WHERE r.active = true GROUP BY r.id "
                "HAVING (r.recurrence IS NOT NULL OR MAX(o.due_at) IS NULL) "
                "AND r.due_at <= :now ORDER BY r.id"
            ),
            {"now": now},
        )
        reminders = list(result.mappings())
    for reminder in reminders:
        try:
            last_due = reminder["last_due"]
            recurrence = str(reminder["recurrence"]) if reminder["recurrence"] else None
            due_at = (
                next_due(last_due, recurrence, timezone_name)
                if last_due is not None
                else reminder["due_at"]
            )
            if due_at is None or due_at > now:
                continue
            scheduled = [due_at]
            while True:
                next_value = next_due(scheduled[-1], recurrence, timezone_name)
                if next_value is None or next_value > now:
                    break
                scheduled.append(next_value)
            latest_due = scheduled[-1]
            skipped = len(scheduled) - 1
            async with database.sessions() as session:
                latest_occurrence_id: int | None = None
                for scheduled_due in scheduled:
                    inserted = await session.execute(
                        text(
                            "INSERT INTO reminder_occurrences (reminder_id, due_at) "
                            "VALUES (:reminder, :due_at) ON CONFLICT DO NOTHING "
                            "RETURNING id"
                        ),
                        {"reminder": int(reminder["id"]), "due_at": scheduled_due},
                    )
                    occurrence_id = inserted.scalar_one_or_none()
                    if scheduled_due == latest_due and occurrence_id is not None:
                        latest_occurrence_id = int(occurrence_id)
                if latest_occurrence_id is not None:
                    suffix = (
                        f" Fired late; skipped {skipped} scheduled occurrence"
                        f"{'' if skipped == 1 else 's'}."
                        if skipped
                        else ""
                    )
                    await session.execute(
                        text(
                            "INSERT INTO notifications "
                            "(namespace, kind, title, body, reminder_occurrence_id) "
                            "VALUES (:namespace, 'reminder', 'Reminder', :body, :occurrence)"
                        ),
                        {
                            "namespace": str(reminder["namespace"]),
                            "body": f"{reminder['text']}{suffix}",
                            "occurrence": latest_occurrence_id,
                        },
                    )
                    fired += 1
                await session.commit()
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
