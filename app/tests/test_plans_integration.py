import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from chitti.plans import (
    PlanDocument,
    PlanTask,
    approve_revision,
    create_revision,
    reject_revision,
    revision_by_id,
    validate_approval_binding,
)
from chitti.runner import cancellation_requested, next_queued_run
from chitti.worker import (
    MAX_CAPTURE_ARTIFACTS_PER_RUN,
    DockerSandboxDispatcher,
    WorkerLimits,
    approved_revision,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_DB_TESTS"), reason="set RUN_DB_TESTS=1 to run PostgreSQL integration tests"
)


class _DatabaseAdapter:
    def __init__(self, engine) -> None:
        self.engine = engine

    @asynccontextmanager
    async def sessions(self) -> AsyncIterator[object]:
        async with self.engine.begin() as session:
            yield session


@pytest.fixture
async def database():
    with PostgresContainer("pgvector/pgvector:pg16") as postgres:
        url = postgres.get_connection_url().replace("postgresql+psycopg2", "postgresql+asyncpg")
        env = {**os.environ, "DATABASE_URL": url}
        subprocess.run(
            ["python", "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            cwd="..",
            env=env,
            check=True,
        )
        engine = create_async_engine(url)
        yield engine
        await engine.dispose()


def document(summary: str = "Build the site.") -> PlanDocument:
    return PlanDocument(
        title="VSports landing page",
        summary=summary,
        tasks=[
            PlanTask(
                id="scene",
                title="Build the scene",
                description="Create the hero scene.",
                done_condition="The scene renders in Chromium.",
            )
        ],
    )


async def test_rejection_and_approval_are_append_only_and_hash_bound(database) -> None:
    async with database.begin() as session:
        first_id = await create_revision(session, "vsports", "Build the site.", document())
        rejection = await reject_revision(session, first_id, "Add a mobile acceptance criterion.")
        with pytest.raises(ValueError, match="already has an approval decision"):
            await approve_revision(session, first_id)
        second_id = await create_revision(
            session,
            "vsports",
            "Build the site with mobile acceptance criteria.",
            document("Build the site with mobile acceptance criteria."),
            first_id,
        )
        approval = await approve_revision(session, second_id)
        assert rejection.revision_id == first_id
        assert second_id != first_id
        assert approval.revision_id == second_id
        revision = await revision_by_id(session, second_id)
        assert revision is not None
        assert validate_approval_binding(revision, approval)
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE plan_revisions SET content = '{}'::jsonb WHERE id = :id"),
                {"id": first_id},
            )
        await session.rollback()


async def test_worker_approval_gate_rechecks_exact_content_hash(database) -> None:
    async with database.begin() as session:
        revision_id = await create_revision(
            session, "sandbox", "Build fixture.", document()
        )
        with pytest.raises(ValueError, match="not approved"):
            await approved_revision(session, revision_id)
        await session.execute(
            text(
                "INSERT INTO plan_approvals "
                "(revision_id, decision, content_hash) "
                "VALUES (:revision, 'approved', :content_hash)"
            ),
            {"revision": revision_id, "content_hash": "0" * 64},
        )
        with pytest.raises(ValueError, match="no longer matches"):
            await approved_revision(session, revision_id)
        await session.rollback()


async def test_worker_artifact_payload_retention_preserves_audit_record(database) -> None:
    async with database.begin() as session:
        revision_id = await create_revision(session, "sandbox", "Build fixture.", document())
        await session.execute(
            text(
                "INSERT INTO worker_runs (revision_id, limits, workspace_id) "
                "VALUES (:revision, '{}'::json, 'run-test') RETURNING id"
            ),
            {"revision": revision_id},
        )
        run_id = int((await session.execute(text("SELECT currval(pg_get_serial_sequence('worker_runs', 'id'))"))).scalar_one())
        await session.execute(
            text(
                "INSERT INTO worker_run_events (run_id, status, detail) "
                "VALUES (:run, 'cancel_requested', 'owner requested cancellation')"
            ),
            {"run": run_id},
        )
        await session.execute(
            text(
                "INSERT INTO worker_run_events (run_id, status, detail) "
                "VALUES (:run, 'operation_running', 'later event must not erase intent')"
            ),
            {"run": run_id},
        )
        artifact_result = await session.execute(
            text(
                "INSERT INTO worker_artifacts "
                "(run_id, kind, path, sha256, byte_size) "
                "VALUES (:run, 'diff', 'workspace.diff', :sha, 4) RETURNING id"
            ),
            {"run": run_id, "sha": "a" * 64},
        )
        artifact_id = int(artifact_result.scalar_one())
        await session.execute(
            text(
                "INSERT INTO worker_artifact_payloads (artifact_id, content) "
                "VALUES (:artifact, 'data')"
            ),
            {"artifact": artifact_id},
        )
        await session.execute(
            text("DELETE FROM worker_artifact_payloads WHERE artifact_id = :artifact"),
            {"artifact": artifact_id},
        )
        record = await session.execute(
            text("SELECT sha256, byte_size FROM worker_artifacts WHERE id = :artifact"),
            {"artifact": artifact_id},
        )
        assert record.one() == ("a" * 64, 4)
    assert await cancellation_requested(_DatabaseAdapter(database), run_id)


async def test_captured_screenshot_survives_later_run_failure_cleanup(
    database, tmp_path
) -> None:
    async with database.begin() as session:
        revision_id = await create_revision(session, "sandbox", "Build fixture.", document())
        result = await session.execute(
            text(
                "INSERT INTO worker_runs (revision_id, limits, workspace_id) "
                "VALUES (:revision, '{}'::json, 'run-capture') RETURNING id"
            ),
            {"revision": revision_id},
        )
        run_id = int(result.scalar_one())

    workspace = tmp_path / "workspace"
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "phone.png").write_bytes(b"phone-image")
    (artifacts / "desktop.png").write_bytes(b"desktop-image")
    (artifacts / "browser-errors.json").write_text("[]")
    dispatcher = DockerSandboxDispatcher(
        _DatabaseAdapter(database),
        workspace_root=tmp_path / "runs",
        preview_root=tmp_path / "previews",
        preview_staging_root=tmp_path / "staging",
    )

    await dispatcher._capture_workspace_artifacts(
        run_id, workspace, WorkerLimits(artifact_bytes=1024)
    )
    (artifacts / "phone.png").write_bytes(b"new-phone-image")
    await dispatcher._capture_workspace_artifacts(
        run_id, workspace, WorkerLimits(artifact_bytes=1024)
    )
    shutil.rmtree(workspace)

    async with database.begin() as session:
        rows = await session.execute(
            text(
                "SELECT a.id, a.kind, a.path, p.content "
                "FROM worker_artifacts a "
                "JOIN worker_artifact_payloads p ON p.artifact_id = a.id "
                "WHERE a.run_id = :run AND a.kind = 'screenshot' "
                "ORDER BY a.path, a.id"
            ),
            {"run": run_id},
        )
        screenshot_rows = list(rows)
        assert len(screenshot_rows) == 4
        assert [
            (row[1], row[2], bytes(row[3])) for row in screenshot_rows
        ] == [
            ("screenshot", "artifacts/desktop.png", b"desktop-image"),
            ("screenshot", "artifacts/desktop.png", b"desktop-image"),
            ("screenshot", "artifacts/phone.png", b"phone-image"),
            ("screenshot", "artifacts/phone.png", b"new-phone-image"),
        ]
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE worker_artifacts SET sha256 = :sha "
                    "WHERE id = :artifact"
                ),
                {"sha": "f" * 64, "artifact": screenshot_rows[-1][0]},
            )


async def test_capture_artifact_count_is_bounded(database, tmp_path) -> None:
    async with database.begin() as session:
        revision_id = await create_revision(session, "sandbox", "Build fixture.", document())
        result = await session.execute(
            text(
                "INSERT INTO worker_runs (revision_id, limits, workspace_id) "
                "VALUES (:revision, '{}'::json, 'run-capture-cap') RETURNING id"
            ),
            {"revision": revision_id},
        )
        run_id = int(result.scalar_one())

    workspace = tmp_path / "workspace-cap"
    artifacts = workspace / "artifacts"
    artifacts.mkdir(parents=True)
    screenshot = artifacts / "desktop.png"
    dispatcher = DockerSandboxDispatcher(
        _DatabaseAdapter(database),
        workspace_root=tmp_path / "runs",
        preview_root=tmp_path / "previews",
        preview_staging_root=tmp_path / "staging",
    )
    for index in range(40):
        screenshot.write_bytes(f"capture-{index}".encode())
        await dispatcher._capture_workspace_artifacts(
            run_id, workspace, WorkerLimits(artifact_bytes=1024)
        )

    async with database.begin() as session:
        result = await session.execute(
            text(
                "SELECT a.id, p.content "
                "FROM worker_artifacts a "
                "JOIN worker_artifact_payloads p ON p.artifact_id = a.id "
                "WHERE a.run_id = :run AND a.kind = 'screenshot' "
                "ORDER BY a.id"
            ),
            {"run": run_id},
        )
        rows = list(result)
        assert len(rows) == MAX_CAPTURE_ARTIFACTS_PER_RUN
        assert bytes(rows[-1][1]) == b"capture-31"


async def test_runner_claim_is_atomic_and_cancellation_is_sticky(database) -> None:
    async with database.begin() as session:
        revision_id = await create_revision(session, "runner", "Build fixture.", document())
        result = await session.execute(
            text(
                "INSERT INTO worker_runs (revision_id, limits, workspace_id) "
                "VALUES (:revision, CAST(:limits AS json), 'runner-test') RETURNING id"
            ),
            {"revision": revision_id, "limits": '{"cpus": 1.0}'},
        )
        run_id = int(result.scalar_one())
        await session.execute(
            text(
                "INSERT INTO worker_run_events (run_id, status, detail) "
                "VALUES (:run, 'queued', 'test')"
            ),
            {"run": run_id},
        )
    adapter = _DatabaseAdapter(database)
    claimed = await next_queued_run(adapter)  # type: ignore[arg-type]
    assert claimed is not None
    assert int(claimed["id"]) == run_id
    async with database.begin() as session:
        await session.execute(
            text(
                "INSERT INTO worker_run_events (run_id, status, detail) "
                "VALUES (:run, 'cancel_requested', 'test')"
            ),
            {"run": run_id},
        )
        await session.execute(
            text(
                "INSERT INTO worker_run_events (run_id, status, detail) "
                "VALUES (:run, 'operation_running', 'later event')"
            ),
            {"run": run_id},
        )
    assert await cancellation_requested(adapter, run_id)  # type: ignore[arg-type]
    assert await next_queued_run(adapter) is None  # type: ignore[arg-type]
