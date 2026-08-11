from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import cast

from sqlalchemy import text

from .db import Database
from .settings import get_settings
from .worker import (
    DockerSandboxDispatcher,
    WorkerLimits,
    approved_revision,
)

logger = logging.getLogger("chitti.worker_runner")
POLL_SECONDS = 2


async def next_queued_run(database: Database) -> Mapping[str, object] | None:
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT r.id, r.revision_id, r.limits "
                "FROM worker_runs r "
                "JOIN LATERAL ("
                "  SELECT status FROM worker_run_events "
                "  WHERE run_id = r.id ORDER BY id DESC LIMIT 1"
                ") latest ON latest.status = 'queued' "
                "ORDER BY r.id "
                "LIMIT 1 FOR UPDATE OF r SKIP LOCKED"
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        await session.execute(
            text(
                "INSERT INTO worker_run_events (run_id, status, detail) "
                "VALUES (:run_id, 'running', 'claimed by host runner')"
            ),
            {"run_id": int(row["id"])},
        )
        await session.commit()
        return cast(Mapping[str, object], row)


async def cancellation_requested(database: Database, run_id: int) -> bool:
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM worker_run_events "
                "  WHERE run_id = :run_id AND status = 'cancel_requested'"
                ")"
            ),
            {"run_id": run_id},
        )
        return bool(result.scalar_one())


async def record_event(database: Database, run_id: int, status: str, detail: str) -> None:
    async with database.sessions() as session:
        await session.execute(
            text(
                "INSERT INTO worker_run_events (run_id, status, detail) "
                "VALUES (:run_id, :status, :detail)"
            ),
            {"run_id": run_id, "status": status, "detail": detail},
        )
        await session.commit()


async def trim_payloads(database: Database) -> None:
    async with database.sessions() as session:
        policy = await session.execute(
            text("SELECT max_payload_bytes FROM worker_retention_policy WHERE id = 1")
        )
        maximum = int(policy.scalar_one())
        while True:
            result = await session.execute(
                text(
                    "SELECT COALESCE(SUM(octet_length(content)), 0) "
                    "FROM worker_artifact_payloads"
                )
            )
            total = int(result.scalar_one())
            if total <= maximum:
                break
            oldest = await session.execute(
                text(
                    "SELECT artifact_id FROM worker_artifact_payloads "
                    "ORDER BY created_at, artifact_id LIMIT 1"
                )
            )
            artifact_id = oldest.scalar_one_or_none()
            if artifact_id is None:
                break
            await session.execute(
                text(
                    "DELETE FROM worker_artifact_payloads WHERE artifact_id = :artifact_id"
                ),
                {"artifact_id": int(artifact_id)},
            )
        await session.commit()


async def execute_run(
    database: Database,
    dispatcher: DockerSandboxDispatcher,
    row: Mapping[str, object],
) -> None:
    run_id = int(cast(int, row["id"]))
    revision_id = int(cast(int, row["revision_id"]))
    raw_limits = row["limits"]
    limits_data = (
        raw_limits
        if isinstance(raw_limits, Mapping)
        else json.loads(str(raw_limits))
    )
    limits = WorkerLimits.from_json(limits_data)
    async with database.sessions() as session:
        revision = await approved_revision(session, revision_id)

    task = asyncio.create_task(dispatcher.dispatch(revision, run_id, limits))
    try:
        while not task.done():
            await asyncio.sleep(1)
            if await cancellation_requested(database, run_id):
                await dispatcher.cancel(run_id)
        await task
    except asyncio.CancelledError:
        await dispatcher.cancel(run_id)
        await record_event(database, run_id, "cancelled", "runner interrupted")
        raise
    except Exception as exc:
        await record_event(database, run_id, "failed", str(exc)[:2000])
    finally:
        await trim_payloads(database)


async def run_forever() -> None:
    database = Database(get_settings())
    dispatcher = DockerSandboxDispatcher(database)
    try:
        while True:
            row = await next_queued_run(database)
            if row is None:
                await asyncio.sleep(POLL_SECONDS)
                continue
            try:
                await execute_run(database, dispatcher, row)
            except Exception:
                logger.exception("worker run failed outside durable event handling")
    finally:
        await database.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
