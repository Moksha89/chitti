from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlalchemy import text

from .db import Database
from .previews import (
    ExportManifest,
    build_manifest,
    copy_export,
    manifest_from_json,
    preview_id,
    remove_preview,
)
from .provider import (
    FakeProvider,
    GatewayMisconfigurationError,
    GatewayTransientError,
    GatewayValidationError,
    LiteLLMProvider,
    ModelProvider,
)
from .reminders import sweep_reminders
from .run_status import TERMINAL_RUN_STATUSES
from .runner_health import record_runner_health_failure, record_runner_health_success
from .runtime_identity import write_loaded_code_identity
from .settings import Settings, get_settings
from .worker import (
    DockerSandboxDispatcher,
    RunBudgetExceeded,
    RunCancelled,
    VisualReviewInconclusive,
    WorkerLimits,
    approved_revision,
)

logger = logging.getLogger("chitti.worker_runner")
POLL_SECONDS = 2
RUN_HEARTBEAT_SECONDS = 2
RUN_HEARTBEAT_STALE_SECONDS = 10
PREVIEW_APPROVAL_BINDING_FAILURE = "preview approval binding failed"
PREVIEW_EVIDENCE_SUBSTITUTION = "preview evidence substitution detected"
PREVIEW_STAGING_MISSING = "approved preview staging output is missing"
TERMINAL_PREVIEW_FAILURES = frozenset(
    {
        PREVIEW_APPROVAL_BINDING_FAILURE,
        PREVIEW_EVIDENCE_SUBSTITUTION,
        PREVIEW_STAGING_MISSING,
    }
)
REMINDER_HEALTH_WRITE_INTERVAL_SECONDS = 30.0
_last_reminder_health_success = 0.0

def _copy_and_verify_export(
    source: Path, destination: Path, approved: ExportManifest
) -> ExportManifest:
    copy_export(source, destination)
    landed = build_manifest(destination)
    if landed != approved:
        raise RuntimeError("preview export changed while publishing")
    return landed


async def best_effort_reminder_sweep(
    database: Database, timezone_name: str = "UTC"
) -> None:
    global _last_reminder_health_success
    try:
        await sweep_reminders(database, timezone_name=timezone_name)
        now = asyncio.get_running_loop().time()
        if (
            _last_reminder_health_success == 0.0
            or now - _last_reminder_health_success
            >= REMINDER_HEALTH_WRITE_INTERVAL_SECONDS
        ):
            await record_runner_health_success(database, "reminder_sweep")
            _last_reminder_health_success = now
    except Exception as exc:
        _last_reminder_health_success = 0.0
        logger.exception("reminder sweep failed")
        try:
            await record_runner_health_failure(
                database, "reminder_sweep", f"{type(exc).__name__}: {exc}"
            )
        except Exception:
            logger.exception("reminder sweep health reporting failed")


async def next_queued_run(
    database: Database, runner_id: str = "legacy-runner"
) -> Mapping[str, object] | None:
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
        await session.execute(
            text(
                "INSERT INTO worker_run_heartbeats (run_id, runner_id, heartbeat_at) "
                "VALUES (:run_id, :runner_id, now()) "
                "ON CONFLICT (run_id) DO UPDATE SET "
                "runner_id = EXCLUDED.runner_id, heartbeat_at = EXCLUDED.heartbeat_at"
            ),
            {"run_id": int(row["id"]), "runner_id": runner_id},
        )
        await session.commit()
        return cast(Mapping[str, object], row)


async def record_run_heartbeat(
    database: Database, run_id: int, runner_id: str
) -> None:
    async with database.sessions() as session:
        await session.execute(
            text(
                "UPDATE worker_run_heartbeats "
                "SET runner_id = :runner_id, heartbeat_at = now() "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id, "runner_id": runner_id},
        )
        await session.commit()


async def _run_heartbeat_loop(
    database: Database, run_id: int, runner_id: str
) -> None:
    while True:
        await asyncio.sleep(RUN_HEARTBEAT_SECONDS)
        try:
            await record_run_heartbeat(database, run_id, runner_id)
        except Exception:
            logger.exception("worker run heartbeat failed for run %s", run_id)


async def reconcile_interrupted_runs(
    database: Database, now: datetime | None = None
) -> list[int]:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=RUN_HEARTBEAT_STALE_SECONDS)
    interrupted: list[int] = []
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT r.id, latest.status, heartbeat.run_id AS heartbeat_run_id "
                "FROM worker_runs r "
                "JOIN LATERAL ("
                "  SELECT status FROM worker_run_events "
                "  WHERE run_id = r.id ORDER BY id DESC LIMIT 1"
                ") latest ON latest.status NOT IN ('queued', 'cancel_requested') "
                "LEFT JOIN worker_run_heartbeats heartbeat "
                "ON heartbeat.run_id = r.id "
                "WHERE heartbeat.heartbeat_at IS NULL OR heartbeat.heartbeat_at < :cutoff "
                "ORDER BY r.id FOR UPDATE OF r SKIP LOCKED"
            ),
            {"cutoff": cutoff},
        )
        for row in result.mappings():
            if str(row["status"]) in TERMINAL_RUN_STATUSES:
                continue
            run_id = int(row["id"])
            detail = (
                "interrupted by runner restart; no execution heartbeat was recorded"
                if row["heartbeat_run_id"] is None
                else "interrupted by runner restart; execution heartbeat expired"
            )
            await session.execute(
                text(
                    "INSERT INTO worker_run_events (run_id, status, detail) "
                    "VALUES (:run_id, 'interrupted', :detail)"
                ),
                {"run_id": run_id, "detail": detail},
            )
            await session.execute(
                text("DELETE FROM worker_run_heartbeats WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            interrupted.append(run_id)
        await session.commit()
    return interrupted


async def reconcile_cancelled_run(database: Database) -> int | None:
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT r.id "
                "FROM worker_runs r "
                "JOIN LATERAL ("
                "  SELECT status FROM worker_run_events "
                "  WHERE run_id = r.id ORDER BY id DESC LIMIT 1"
                ") latest ON latest.status = 'cancel_requested' "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM worker_run_events claimed "
                "  WHERE claimed.run_id = r.id "
                "    AND claimed.status = 'running'"
                ") "
                "ORDER BY r.id "
                "LIMIT 1 FOR UPDATE OF r SKIP LOCKED"
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        run_id = int(row["id"])
        await session.execute(
            text(
                "INSERT INTO worker_run_events (run_id, status, detail) "
                "VALUES (:run_id, 'cancelled', 'cancelled before it started')"
            ),
            {"run_id": run_id},
        )
        await session.commit()
        return run_id


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


async def latest_status(database: Database, run_id: int) -> str | None:
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT status FROM worker_run_events "
                "WHERE run_id = :run_id ORDER BY id DESC LIMIT 1"
            ),
            {"run_id": run_id},
        )
        status = result.scalar_one_or_none()
        return str(status) if status is not None else None


async def record_cancelled_if_requested(database: Database, run_id: int) -> bool:
    if not await cancellation_requested(database, run_id):
        return False
    status = await latest_status(database, run_id)
    if status in TERMINAL_RUN_STATUSES:
        return True
    await record_event(database, run_id, "cancelled", "cancelled by owner")
    return True


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


async def publish_approved_previews(database: Database, settings: Settings) -> None:
    preview_root = Path(str(settings.preview_root))
    preview_root.mkdir(parents=True, exist_ok=True)
    await _evict_expired_preview_directories(database, preview_root)
    async with database.sessions() as session:
        rows = await session.execute(
            text(
                "SELECT a.id AS approval_id, a.run_id "
                "FROM promotion_approvals a "
                "LEFT JOIN previews p ON p.run_id = a.run_id "
                "LEFT JOIN LATERAL ("
                "  SELECT status, detail FROM worker_run_events "
                "  WHERE run_id = a.run_id "
                "    AND status IN ('preview_failed', 'preview_blocked') "
                "  ORDER BY id DESC LIMIT 1"
                ") latest ON true "
                "WHERE a.decision = 'approved' AND p.run_id IS NULL"
            )
        )
        approvals = list(rows.mappings())
    for row in approvals:
        if _promotion_failure_is_terminal(row.get("status"), row.get("detail")):
            continue
        try:
            await _publish_one_preview(
                database, settings, preview_root, cast(Mapping[str, object], row)
            )
        except PreviewBlockedError as exc:
            await _record_preview_event(
                database, int(cast(int, row["run_id"])), "preview_blocked", str(exc)
            )
        except Exception as exc:
            await _record_preview_event(
                database,
                int(cast(int, row["run_id"])),
                "preview_failed",
                str(exc)[:2000],
            )


class PreviewBlockedError(RuntimeError):
    pass


def _promotion_failure_is_terminal(status: object, detail: object) -> bool:
    if status != "preview_failed":
        return False
    return str(detail) in TERMINAL_PREVIEW_FAILURES


async def _record_preview_event(
    database: Database, run_id: int, status: str, detail: str
) -> None:
    try:
        bounded_detail = detail[:2000]
        async with database.sessions() as session:
            latest = await session.execute(
                text(
                    "SELECT status, detail FROM worker_run_events "
                    "WHERE run_id = :run_id "
                    "AND status IN ('preview_failed', 'preview_blocked') "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"run_id": run_id},
            )
            previous = latest.mappings().one_or_none()
            if (
                previous is not None
                and previous["status"] == status
                and previous["detail"] == bounded_detail
            ):
                return
            await session.execute(
                text(
                    "INSERT INTO worker_run_events (run_id, status, detail) "
                    "VALUES (:run_id, :status, :detail)"
                ),
                {"run_id": run_id, "status": status, "detail": bounded_detail},
            )
            await session.commit()
    except Exception:
        logger.exception("could not record preview event for run %s", run_id)


async def _evict_expired_preview_directories(database: Database, preview_root: Path) -> None:
    async with database.sessions() as session:
        result = await session.execute(
            text("SELECT preview_id FROM previews WHERE expires_at <= now()")
        )
        expired = [str(row.preview_id) for row in result]
    for identifier in expired:
        await asyncio.to_thread(remove_preview, preview_root / identifier)


async def _publish_one_preview(
    database: Database,
    settings: Settings,
    preview_root: Path,
    approval: Mapping[str, object],
) -> int:
    async with database.sessions() as session:
        result = await session.execute(
            text(
                "SELECT a.id AS approval_id, a.run_id, a.manifest_id, "
                "a.revision_content_hash, a.reviewer_artifact_id, a.reviewer_sha256, "
                "a.diff_artifact_id, a.diff_sha256, a.manifest_digest, "
                "m.revision_content_hash AS manifest_revision_content_hash, "
                "m.manifest, m.digest, m.staging_path, m.total_bytes, m.file_count "
                "FROM promotion_approvals a JOIN export_manifests m "
                "ON m.id = a.manifest_id WHERE a.id = :approval_id"
            ),
            {"approval_id": int(cast(int, approval["approval_id"]))},
        )
        row = result.mappings().one()
        existing = await session.execute(
            text(
                "SELECT COALESCE(SUM(total_bytes), 0) AS total, COUNT(*) AS count "
                "FROM previews WHERE expires_at > now()"
            )
        )
        totals = existing.mappings().one()
        total_bytes = int(totals["total"])
        preview_count = int(totals["count"])
        manifest = manifest_from_json(row["manifest"], str(row["digest"]))
        if (
            str(row["revision_content_hash"])
            != str(row["manifest_revision_content_hash"])
            or str(row["manifest_digest"]) != manifest.digest
            or manifest.total_bytes != int(row["total_bytes"])
            or len(manifest.entries) != int(row["file_count"])
        ):
            raise RuntimeError(PREVIEW_APPROVAL_BINDING_FAILURE)
        if preview_count >= int(settings.preview_max_count):
            raise PreviewBlockedError("preview count quota exhausted")
        if total_bytes + manifest.total_bytes > int(settings.preview_max_bytes):
            raise PreviewBlockedError("preview size quota exhausted")
        reviewer = await session.execute(
            text(
                "SELECT sha256 FROM worker_artifacts "
                "WHERE id = :id AND run_id = :run_id"
            ),
            {"id": row["reviewer_artifact_id"], "run_id": row["run_id"]},
        )
        diff = await session.execute(
            text(
                "SELECT sha256 FROM worker_artifacts "
                "WHERE id = :id AND run_id = :run_id"
            ),
            {"id": row["diff_artifact_id"], "run_id": row["run_id"]},
        )
        if (
            reviewer.scalar_one_or_none() != row["reviewer_sha256"]
            or diff.scalar_one_or_none() != row["diff_sha256"]
        ):
            raise RuntimeError(PREVIEW_EVIDENCE_SUBSTITUTION)
        staging = Path(str(row["staging_path"]))
        if not staging.is_dir():
            raise RuntimeError(PREVIEW_STAGING_MISSING)
        current = await asyncio.to_thread(build_manifest, staging)
        if current.digest != manifest.digest:
            raise RuntimeError("preview export changed after approval")
        identifier = preview_id()
        destination = preview_root / identifier
        try:
            landed = await asyncio.to_thread(
                _copy_and_verify_export, staging, destination, manifest
            )
            expires_at = datetime.now(UTC) + timedelta(
                hours=int(settings.preview_ttl_hours)
            )
            await session.execute(
                text(
                    "INSERT INTO previews "
                    "(preview_id, expires_at, run_id, manifest_id, approval_id, "
                    "total_bytes, file_count) VALUES "
                    "(:preview_id, :expires_at, :run_id, :manifest_id, "
                    ":approval_id, :total_bytes, :file_count)"
                ),
                {
                    "preview_id": identifier,
                    "expires_at": expires_at,
                    "run_id": row["run_id"],
                    "manifest_id": row["manifest_id"],
                    "approval_id": row["approval_id"],
                    "total_bytes": landed.total_bytes,
                    "file_count": len(landed.entries),
                },
            )
            await session.commit()
        except Exception:
            await session.rollback()
            await asyncio.to_thread(remove_preview, destination)
            raise
    await asyncio.to_thread(remove_preview, staging)
    return manifest.total_bytes


async def execute_run(
    database: Database,
    dispatcher: DockerSandboxDispatcher,
    row: Mapping[str, object],
    provider: ModelProvider,
    runner_id: str = "legacy-runner",
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
    try:
        await provider.validate_gateway()
    except GatewayMisconfigurationError as exc:
        await record_event(database, run_id, "failed", f"gateway misconfiguration: {exc}")
        return
    except GatewayTransientError as exc:
        await record_event(database, run_id, "failed", f"gateway temporarily unavailable: {exc}")
        return
    if await record_cancelled_if_requested(database, run_id):
        return
    async with database.sessions() as session:
        revision = await approved_revision(session, revision_id)

    heartbeat_task = (
        asyncio.create_task(_run_heartbeat_loop(database, run_id, runner_id))
        if runner_id != "legacy-runner"
        else None
    )
    task = asyncio.create_task(dispatcher.dispatch(revision, run_id, limits))
    try:
        deadline = asyncio.get_running_loop().time() + limits.run_timeout_seconds
        while not task.done():
            await asyncio.sleep(1)
            if asyncio.get_running_loop().time() > deadline:
                await dispatcher.cancel(run_id)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                if not await record_cancelled_if_requested(database, run_id):
                    await record_event(
                        database, run_id, "failed",
                        "model run wall-clock budget exceeded",
                    )
                return
            if await cancellation_requested(database, run_id):
                await dispatcher.cancel(run_id)
        await task
        await record_cancelled_if_requested(database, run_id)
    except RunCancelled:
        await record_event(database, run_id, "cancelled", "cancelled by owner")
    except asyncio.CancelledError:
        await dispatcher.cancel(run_id)
        await record_event(database, run_id, "cancelled", "runner interrupted")
        raise
    except RunBudgetExceeded as exc:
        if not await record_cancelled_if_requested(database, run_id):
            await record_event(database, run_id, "failed", str(exc))
    except VisualReviewInconclusive as exc:
        if not await record_cancelled_if_requested(database, run_id):
            await record_event(database, run_id, "visual_review_inconclusive", str(exc))
    except Exception as exc:
        if not await record_cancelled_if_requested(database, run_id):
            await record_event(database, run_id, "failed", str(exc)[:2000])
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            async with database.sessions() as session:
                await session.execute(
                    text("DELETE FROM worker_run_heartbeats WHERE run_id = :run_id"),
                    {"run_id": run_id},
                )
                await session.commit()
        await trim_payloads(database)


async def run_forever() -> None:
    identity = write_loaded_code_identity()
    logger.info(
        "runner loaded code identity digest=%s pid=%s",
        identity["digest"],
        identity["pid"],
    )
    database = Database(get_settings())
    settings = get_settings()
    runner_id = uuid.uuid4().hex
    provider = (
        FakeProvider()
        if settings.chitti_provider == "fake"
        else LiteLLMProvider(settings.litellm_base_url, settings.litellm_master_key)
    )
    dispatcher = DockerSandboxDispatcher(
        database,
        preview_root=Path(settings.preview_root),
        preview_staging_root=Path(settings.preview_staging_root),
        preview_ttl_hours=settings.preview_ttl_hours,
        model_provider=provider,
    )
    try:
        try:
            await provider.validate_gateway(probe_routes=True)
        except GatewayValidationError as exc:
            logger.error("gateway startup preflight failed: %s", exc)
        await dispatcher.cleanup_stale_workspaces()
        interrupted = await reconcile_interrupted_runs(database)
        if interrupted:
            logger.warning("marked interrupted worker runs after restart: %s", interrupted)
        while True:
            await dispatcher.cleanup_expired_previews()
            await publish_approved_previews(database, settings)
            await reconcile_cancelled_run(database)
            await best_effort_reminder_sweep(database, settings.display_timezone)
            row = await next_queued_run(database, runner_id)
            if row is None:
                await asyncio.sleep(POLL_SECONDS)
                continue
            try:
                await execute_run(database, dispatcher, row, provider, runner_id)
            except Exception:
                logger.exception("worker run failed outside durable event handling")
    finally:
        await database.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
