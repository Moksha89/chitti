from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from sqlalchemy import text

from .db import Database
from .reminders import next_due


def _format_section(title: str, values: list[str]) -> str:
    if not values:
        return f"{title}: none."
    return f"{title}:\n" + "\n".join(f"- {value}" for value in values)


async def compose_briefing(
    database: Database, namespace: str, timezone_name: str, now: datetime | None = None
) -> dict[str, object]:
    now = now or datetime.now(UTC)
    zone = ZoneInfo(timezone_name)
    local_now = now.astimezone(zone)
    local_date = local_now.date()
    start_local = datetime.combine(local_date, time.min, tzinfo=zone)
    start_utc = start_local.astimezone(UTC)
    end_utc = (start_local + timedelta(days=1)).astimezone(UTC)
    async with database.sessions() as session:
        previous = await session.execute(
            text(
                "SELECT generated_at FROM daily_briefings "
                "WHERE namespace = :namespace AND local_date < :local_date "
                "ORDER BY local_date DESC LIMIT 1"
            ),
            {"namespace": namespace, "local_date": local_date},
        )
        previous_row = previous.mappings().one_or_none()
        since = (
            previous_row["generated_at"]
            if previous_row is not None
            else start_utc
        )
        runs_result = await session.execute(
            text(
                "SELECT r.id, COALESCE(latest.status, 'queued') AS status "
                "FROM worker_runs r JOIN plan_revisions p ON p.id = r.revision_id "
                "LEFT JOIN LATERAL (SELECT status FROM worker_run_events "
                "WHERE run_id = r.id ORDER BY id DESC LIMIT 1) latest ON true "
                "WHERE p.namespace = :namespace AND r.created_at >= :since "
                "ORDER BY r.id"
            ),
            {"namespace": namespace, "since": since},
        )
        runs = [f"Run {row.id}: {row.status}" for row in runs_result]
        approvals_result = await session.execute(
            text(
                "SELECT r.id FROM worker_runs r "
                "JOIN plan_revisions p ON p.id = r.revision_id "
                "JOIN LATERAL (SELECT status FROM worker_run_events "
                "WHERE run_id = r.id ORDER BY id DESC LIMIT 1) latest "
                "ON latest.status = 'passed' "
                "LEFT JOIN promotion_approvals a ON a.run_id = r.id "
                "AND a.decision = 'approved' "
                "WHERE p.namespace = :namespace AND a.id IS NULL ORDER BY r.id"
            ),
            {"namespace": namespace},
        )
        approvals = [f"Run {row.id} is waiting for approval" for row in approvals_result]
        conflicts_result = await session.execute(
            text(
                "SELECT COUNT(*) FROM memory_conflicts c "
                "JOIN decisions d ON d.id = c.existing_decision_id "
                "WHERE d.namespace = :namespace AND c.resolution_decision_id IS NULL"
            ),
            {"namespace": namespace},
        )
        conflicts = int(conflicts_result.scalar_one())
        previews_result = await session.execute(
            text(
                "SELECT preview_id, expires_at FROM previews p "
                "JOIN export_manifests m ON m.id = p.manifest_id "
                "JOIN worker_runs r ON r.id = m.run_id "
                "JOIN plan_revisions pr ON pr.id = r.revision_id "
                "WHERE pr.namespace = :namespace AND p.expires_at > :now "
                "AND p.expires_at <= :soon ORDER BY p.expires_at"
            ),
            {"namespace": namespace, "now": now, "soon": now + timedelta(days=1)},
        )
        previews = [
            f"{row.preview_id} expires {row.expires_at.astimezone(zone).isoformat(timespec='minutes')}"
            for row in previews_result
        ]
        reminders_result = await session.execute(
            text(
                "SELECT text, due_at, recurrence FROM reminders "
                "WHERE namespace = :namespace AND active = true AND due_at < :end "
                "ORDER BY due_at"
            ),
            {"namespace": namespace, "end": end_utc},
        )
        reminders: list[str] = []
        for row in reminders_result:
            due_at = row.due_at
            recurrence = str(row.recurrence) if row.recurrence else None
            while due_at < start_utc:
                due_at = next_due(due_at, recurrence)
                if due_at is None:
                    break
            if due_at is not None and start_utc <= due_at < end_utc:
                reminders.append(
                    f"{due_at.astimezone(zone).strftime('%H:%M')} — {row.text}"
                )
        sections = [
            _format_section("Runs since the last briefing", runs),
            _format_section("Results waiting for approval", approvals),
            f"Unresolved memory contradictions: {conflicts}.",
            _format_section("Previews expiring within 24 hours", previews),
            _format_section("Reminders due today", reminders),
        ]
        content = "\n\n".join(sections)
        if not runs and not approvals and conflicts == 0 and not previews and not reminders:
            content = "Nothing needs your attention today."
        result = await session.execute(
            text(
                "INSERT INTO daily_briefings (namespace, local_date, generated_at, content) "
                "VALUES (:namespace, :local_date, :generated_at, :content) "
                "ON CONFLICT (namespace, local_date) DO UPDATE SET content = daily_briefings.content "
                "RETURNING content, generated_at"
            ),
            {
                "namespace": namespace,
                "local_date": local_date,
                "generated_at": now,
                "content": content,
            },
        )
        briefing_row = cast(Mapping[str, object], result.mappings().one())
        await session.commit()
        return {
            "content": str(briefing_row["content"]),
            "generated_at": briefing_row["generated_at"],
            "timezone": timezone_name,
            "local_date": local_date,
        }
