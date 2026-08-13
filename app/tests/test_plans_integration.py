import asyncio
import hashlib
import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from chitti.brand_profiles import save_brand_profile
from chitti.briefings import compose_briefing
from chitti.db import Database
from chitti.embedding import FakeEmbedder
from chitti.main import record_promotion_approval
from chitti.memory import MemoryStore
from chitti.notifications import (
    acknowledge_notification,
    notifications_after,
    recent_notifications,
)
from chitti.plans import (
    PlanDocument,
    PlanManager,
    PlanTask,
    approve_revision,
    create_revision,
    reject_revision,
    revision_by_id,
    validate_approval_binding,
)
from chitti.provider import FakeProvider
from chitti.reminders import (
    cancel_reminder,
    create_reminder,
    next_due,
    recent_reminders,
    sweep_reminders,
)
from chitti.run_context import RunContextError, build_run_evidence
from chitti.runner import (
    cancellation_requested,
    next_queued_run,
    reconcile_cancelled_run,
    reconcile_interrupted_runs,
)
from chitti.runner_access import assert_runner_privileges, reconcile_runner_privileges
from chitti.runner_health import (
    recent_runner_health,
    record_runner_health_failure,
    record_runner_health_success,
)
from chitti.settings import Settings
from chitti.transcripts import append_entry, recent_entries
from chitti.worker import (
    MAX_CAPTURE_ARTIFACTS_PER_RUN,
    DockerSandboxDispatcher,
    FixedOperation,
    WorkerLimits,
    WorkerRunManager,
    approved_revision,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_DB_TESTS"), reason="set RUN_DB_TESTS=1 to run PostgreSQL integration tests"
)
REPO_ROOT = Path(__file__).resolve().parents[2]


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
            cwd=REPO_ROOT,
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


async def test_plan_project_keeps_namespace_as_a_separate_scope(database) -> None:
    async with database.begin() as session:
        revision_id = await create_revision(
            session,
            "animated-3d",
            "Build the site.",
            document(),
            namespace="pj-digi",
        )
        revision = await revision_by_id(session, revision_id, "pj-digi")
        hidden = await revision_by_id(session, revision_id, "jsv-fashion")
        assert revision is not None
        assert revision.project == "animated-3d"
        assert revision.namespace == "pj-digi"
        assert hidden is None


async def test_poster_planning_requires_brand_profile_and_generates_poster_revision(
    database,
) -> None:
    manager = PlanManager(_DatabaseAdapter(database), FakeProvider(), MemoryStore(FakeEmbedder()))
    with pytest.raises(ValueError, match="namespace 'general' has no brand profile"):
        await manager.enqueue(
            "trial-poster",
            "Create a trial poster.",
            namespace="general",
            job_type="poster",
            job_config={"artifact": "poster.html", "width": 1080, "height": 1350, "scale": 1},
        )
    async with database.begin() as session:
        await save_brand_profile(
            session,
            "general",
            brand_colors=["#111111"],
            typography="FreeSans",
            poster_formats=["1080x1350 trial"],
            audience="trial audience",
            voice="trial voice",
            do_not_use=["real brands"],
            actor="owner",
        )
    job_id = await manager.enqueue(
        "trial-poster",
        "Create a trial poster.",
        namespace="general",
        job_type="poster",
        job_config={"artifact": "poster.html", "width": 1080, "height": 1350, "scale": 1},
    )
    await asyncio.gather(*manager._jobs)
    async with database.begin() as session:
        job = await manager.job(job_id)
        revision = await revision_by_id(session, int(job["revision_id"]))
    assert job["job_type"] == "poster"
    assert revision is not None
    assert revision.job_type == "poster"
    assert revision.job_config == {
        "artifact": "poster.html",
        "width": 1080,
        "height": 1350,
        "scale": 1,
    }
    assert {task.id for task in revision.document.tasks} == {"brief", "review"}
    assert "poster" in revision.document.tasks[0].title.lower()


async def test_planner_retries_once_after_plan_document_validation_error(
    database, monkeypatch
) -> None:
    calls: list[str | None] = []
    invalid = (
        '{"title":"Trial","summary":"Trial poster","tasks":['
        '{"id":1,"title":"Author","description":"Write it.",'
        '"dependencies":[],"done_condition":"Written."}]}'
    )
    valid = (
        '{"title":"Trial","summary":"Trial poster","tasks":['
        '{"id":"T1","title":"Author","description":"Write it.",'
        '"dependencies":[],"done_condition":"Written."}]}'
    )

    async def plan(_self, _brief, _project, _beliefs, rejection=None, *_args):
        calls.append(rejection)
        return invalid if len(calls) == 1 else valid

    monkeypatch.setattr(FakeProvider, "plan", plan)
    manager = PlanManager(
        _DatabaseAdapter(database), FakeProvider(), MemoryStore(FakeEmbedder())
    )
    job_id = await manager.enqueue("retry-plan", "Create a website.")
    await asyncio.gather(*manager._jobs)
    job = await manager.job(job_id)
    assert job is not None
    assert job["status"] == "complete"
    assert job["revision_id"] is not None
    assert len(calls) == 2
    assert calls[0] is None
    assert calls[1] is not None
    assert "validation" in calls[1]
    assert "valid string" in calls[1]


async def test_plan_revision_job_type_is_required_by_worker_run(database) -> None:
    async with database.begin() as session:
        revision_id = await create_revision(
            session,
            "poster-project",
            "Create a poster.",
            document(),
            job_type="poster",
            job_config={"artifact": "poster.html", "width": 1080, "height": 1350, "scale": 1},
        )
        await approve_revision(session, revision_id)
    with pytest.raises(ValueError, match="does not match approved plan revision"):
        await WorkerRunManager(_DatabaseAdapter(database)).enqueue(
            revision_id,
            job_type="website",
        )


async def test_run_evidence_is_namespace_scoped_and_failure_first(database) -> None:
    async with database.begin() as session:
        revision_id = await create_revision(
            session,
            "animated-3d",
            "Build the site.",
            document(),
            namespace="pj-digi",
        )
        run_id = int(
            (
                await session.execute(
                    text(
                        "INSERT INTO worker_runs (revision_id, limits, workspace_id) "
                        "VALUES (:revision, '{}'::json, 'run-context') RETURNING id"
                    ),
                    {"revision": revision_id},
                )
            ).scalar_one()
        )
        now = datetime.now(UTC)
        await session.execute(
            text(
                "INSERT INTO worker_run_events "
                "(run_id, status, detail, operation_index, task_id) "
                "VALUES (:run, 'failed', 'task failed', 1, 'scene')"
            ),
            {"run": run_id},
        )
        await session.execute(
            text(
                "INSERT INTO worker_operations "
                "(run_id, task_id, operation_index, name, status, stdout, stderr, "
                "exit_code, started_at, finished_at) "
                "VALUES (:run, 'scene', 1, 'build', 'failed', "
                "'failure output', 'missing module', 1, :now, :now)"
            ),
            {"run": run_id, "now": now},
        )
        await session.execute(
            text(
                "INSERT INTO worker_operations "
                "(run_id, task_id, operation_index, name, status, stdout, stderr, "
                "exit_code, started_at, finished_at) "
                "VALUES (:run, 'scene', 2, 'test', 'passed', "
                "'success output', '', 0, :now, :now)"
            ),
            {"run": run_id, "now": now},
        )
        reviewer_id = int(
            (
                await session.execute(
                    text(
                        "INSERT INTO worker_artifacts "
                        "(run_id, kind, path, content, sha256, byte_size) "
                        "VALUES (:run, 'reviewer_report', 'reviewer.json', NULL, :sha, 20) "
                        "RETURNING id"
                    ),
                    {"run": run_id, "sha": "r" * 64},
                )
            ).scalar_one()
        )
        diff_id = int(
            (
                await session.execute(
                    text(
                        "INSERT INTO worker_artifacts "
                        "(run_id, kind, path, content, sha256, byte_size) "
                        "VALUES (:run, 'diff', 'change.diff', NULL, :sha, 50) "
                        "RETURNING id"
                    ),
                    {"run": run_id, "sha": "d" * 64},
                )
            ).scalar_one()
        )
        await session.execute(
            text(
                "INSERT INTO worker_artifact_payloads (artifact_id, content) "
                "VALUES (:reviewer, :reviewer_content), (:diff, :diff_content)"
            ),
            {
                "reviewer": reviewer_id,
                "reviewer_content": b'{"verdict":"fail","summary":"missing module"}',
                "diff": diff_id,
                "diff_content": b"diff --git a/page.js b/page.js\n+new\n-old\n",
            },
        )
        await session.execute(
            text(
                "INSERT INTO plan_task_events "
                "(revision_id, task_id, event_type, status, detail) "
                "VALUES (:revision, 'scene', 'completed', 'failed', 'missing module')"
            ),
            {"revision": revision_id},
        )

        evidence = await build_run_evidence(session, run_id, "pj-digi")

        assert "missing module" in evidence.context
        assert evidence.context.index("[failed operation]") < evidence.context.index(
            "[successful operations]"
        )
        assert "page.js" in evidence.context
        assert "screenshot" not in evidence.context.lower()
        assert "model prompt" not in evidence.context.lower()
        with pytest.raises(RunContextError):
            await build_run_evidence(session, run_id, "jsv-fashion")
        with pytest.raises(RunContextError):
            await build_run_evidence(session, 999999, "pj-digi")


async def test_transcript_is_append_only_and_namespace_scoped(database) -> None:
    async with database.begin() as session:
        await append_entry(session, "pj-digi", "user", "PJ question")
        await append_entry(session, "pj-digi", "assistant", "PJ answer")
        await append_entry(session, "jsv-fashion", "user", "JSV question")

        pj_entries = await recent_entries(session, "pj-digi")
        jsv_entries = await recent_entries(session, "jsv-fashion")

        assert [entry["content"] for entry in pj_entries] == ["PJ question", "PJ answer"]
        assert [entry["content"] for entry in jsv_entries] == ["JSV question"]
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE chat_transcript_entries SET content = 'rewritten' "
                    "WHERE namespace = 'pj-digi'"
                )
            )


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


@pytest.mark.parametrize("actor", ["owner", "agent", "system"])
async def test_promotion_approval_records_actor_and_reason(database, actor) -> None:
    async with database.begin() as session:
        revision_id = await create_revision(session, "sandbox", "Build fixture.", document())
        run_id = int(
            (
                await session.execute(
                    text(
                        "INSERT INTO worker_runs (revision_id, limits, workspace_id) "
                        "VALUES (:revision, '{}'::json, :workspace) RETURNING id"
                    ),
                    {"revision": revision_id, "workspace": f"approval-{actor}"},
                )
            ).scalar_one()
        )
        artifact_ids = []
        for kind in ("reviewer_report", "diff"):
            artifact_ids.append(
                int(
                    (
                        await session.execute(
                            text(
                                "INSERT INTO worker_artifacts "
                                "(run_id, kind, path, sha256, byte_size) "
                                "VALUES (:run, :kind, :path, :sha, 4) RETURNING id"
                            ),
                            {
                                "run": run_id,
                                "kind": kind,
                                "path": f"{kind}.json",
                                "sha": kind[0] * 64,
                            },
                        )
                    ).scalar_one()
                )
            )
        manifest_id = int(
            (
                await session.execute(
                    text(
                        "INSERT INTO export_manifests "
                        "(run_id, revision_id, revision_content_hash, "
                        "reviewer_artifact_id, diff_artifact_id, manifest, digest, "
                        "total_bytes, file_count, max_depth, staging_path) "
                        "VALUES (:run, :revision, :revision_hash, :reviewer, :diff, "
                        "'{}'::json, :digest, 4, 1, 1, :staging) RETURNING id"
                    ),
                    {
                        "run": run_id,
                        "revision": revision_id,
                        "revision_hash": "a" * 64,
                        "reviewer": artifact_ids[0],
                        "diff": artifact_ids[1],
                        "digest": "b" * 64,
                        "staging": f"staging-{actor}",
                    },
                )
            ).scalar_one()
        )
        await record_promotion_approval(
            session,
            {
                "run_id": run_id,
                "revision_id": revision_id,
                "revision_content_hash": "a" * 64,
                "manifest_id": manifest_id,
                "reviewer_artifact_id": artifact_ids[0],
                "reviewer_sha256": "r" * 64,
                "diff_artifact_id": artifact_ids[1],
                "diff_sha256": "d" * 64,
                "digest": "b" * 64,
            },
            actor=actor,
            reason=f"{actor} note",
        )
        row = (
            await session.execute(
                text(
                    "SELECT approved_by, reason FROM promotion_approvals "
                    "WHERE run_id = :run"
                ),
                {"run": run_id},
            )
        ).mappings().one()
        assert row["approved_by"] == actor
        assert row["reason"] == f"{actor} note"


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


async def test_queued_cancellation_is_reconciled_without_dispatch(database) -> None:
    async with database.begin() as session:
        revision_id = await create_revision(session, "runner", "Build fixture.", document())
        result = await session.execute(
            text(
                "INSERT INTO worker_runs (revision_id, limits, workspace_id) "
                "VALUES (:revision, CAST(:limits AS json), 'cancel-test') RETURNING id"
            ),
            {"revision": revision_id, "limits": '{"cpus": 1.0}'},
        )
        run_id = int(result.scalar_one())
        await session.execute(
            text(
                "INSERT INTO worker_run_events (run_id, status, detail) VALUES "
                "(:run, 'queued', 'test'), "
                "(:run, 'cancel_requested', 'owner requested cancellation')"
            ),
            {"run": run_id},
        )

    adapter = _DatabaseAdapter(database)
    assert await reconcile_cancelled_run(adapter) == run_id  # type: ignore[arg-type]
    assert await next_queued_run(adapter) is None  # type: ignore[arg-type]
    async with database.begin() as session:
        result = await session.execute(
            text(
                "SELECT status, detail FROM worker_run_events "
                "WHERE run_id = :run ORDER BY id DESC LIMIT 1"
            ),
            {"run": run_id},
        )
        latest = result.mappings().one()
    assert latest["status"] == "cancelled"
    assert latest["detail"] == "cancelled before it started"


async def test_claimed_cancellation_is_not_reconciled(database) -> None:
    async with database.begin() as session:
        revision_id = await create_revision(session, "runner", "Build fixture.", document())
        result = await session.execute(
            text(
                "INSERT INTO worker_runs (revision_id, limits, workspace_id) "
                "VALUES (:revision, CAST(:limits AS json), 'claimed-cancel-test') RETURNING id"
            ),
            {"revision": revision_id, "limits": '{"cpus": 1.0}'},
        )
        run_id = int(result.scalar_one())
        await session.execute(
            text(
                "INSERT INTO worker_run_events (run_id, status, detail) VALUES "
                "(:run, 'queued', 'test')"
            ),
            {"run": run_id},
        )
    adapter = _DatabaseAdapter(database)
    assert await next_queued_run(adapter) is not None  # type: ignore[arg-type]
    async with database.begin() as session:
        await session.execute(
            text(
                "INSERT INTO worker_run_events (run_id, status, detail) "
                "VALUES (:run, 'cancel_requested', 'owner requested cancellation')"
            ),
            {"run": run_id},
        )
    assert await reconcile_cancelled_run(adapter) is None  # type: ignore[arg-type]


async def test_restart_reconciliation_marks_only_stale_runs_interrupted(database) -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    async with database.begin() as session:
        revision_id = await create_revision(session, "runner", "Build fixture.", document())
        run_ids: dict[str, int] = {}
        for name, status in (
            ("stale", "running"),
            ("preexisting", "running"),
            ("live", "running"),
            ("unknown_in_flight", "model_context_compacted"),
            ("terminal", "passed"),
        ):
            result = await session.execute(
                text(
                    "INSERT INTO worker_runs (revision_id, limits, workspace_id) "
                    "VALUES (:revision, '{}'::json, :workspace) RETURNING id"
                ),
                {"revision": revision_id, "workspace": f"restart-{name}"},
            )
            run_id = int(result.scalar_one())
            run_ids[name] = run_id
            await session.execute(
                text(
                    "INSERT INTO worker_run_events (run_id, status, detail) "
                    "VALUES (:run, :status, :detail)"
                ),
                {"run": run_id, "status": status, "detail": "test state"},
            )
        await session.execute(
            text(
                "INSERT INTO worker_run_heartbeats (run_id, runner_id, heartbeat_at) "
                "VALUES (:run, 'dead-runner', :heartbeat)"
            ),
            {
                "run": run_ids["stale"],
                "heartbeat": now - timedelta(seconds=30),
            },
        )
        await session.execute(
            text(
                "INSERT INTO worker_run_heartbeats (run_id, runner_id, heartbeat_at) "
                "VALUES (:run, 'live-runner', :heartbeat)"
            ),
            {
                "run": run_ids["live"],
                "heartbeat": now - timedelta(seconds=1),
            },
        )

    adapter = _DatabaseAdapter(database)
    assert await reconcile_interrupted_runs(adapter, now) == [
        run_ids["stale"],
        run_ids["preexisting"],
        run_ids["unknown_in_flight"],
    ]  # type: ignore[arg-type]
    assert await reconcile_interrupted_runs(adapter, now) == []  # type: ignore[arg-type]

    manager = WorkerRunManager(adapter)  # type: ignore[arg-type]
    stale_detail = await manager.detail(run_ids["stale"])
    assert stale_detail is not None
    assert stale_detail["events"][-1]["status"] == "interrupted"
    assert "execution heartbeat expired" in stale_detail["events"][-1]["detail"]
    preexisting_detail = await manager.detail(run_ids["preexisting"])
    assert preexisting_detail is not None
    assert "no execution heartbeat was recorded" in preexisting_detail["events"][-1]["detail"]
    assert await manager.latest_status(run_ids["live"]) == "running"
    assert await manager.latest_status(run_ids["terminal"]) == "passed"


async def test_live_output_chunks_are_cursorable_bounded_and_pruned_after_artifact(
    database,
) -> None:
    async with database.begin() as session:
        revision_id = await create_revision(session, "runner", "Build fixture.", document())
        result = await session.execute(
            text(
                "INSERT INTO worker_runs (revision_id, limits, workspace_id) "
                "VALUES (:revision, '{}'::json, 'live-output-test') RETURNING id"
            ),
            {"revision": revision_id},
        )
        run_id = int(result.scalar_one())

    adapter = _DatabaseAdapter(database)
    dispatcher = DockerSandboxDispatcher(adapter)  # type: ignore[arg-type]
    await dispatcher._append_output_chunk(
        run_id, 0, "stdout", 0, 0, b"first\n"
    )
    await dispatcher._append_output_chunk(
        run_id, 2, "stdout", 0, 0, b"build output\n"
    )
    await dispatcher._append_output_chunk(
        run_id, 3, "stdout", 0, 0, b"test output\n"
    )
    manager = WorkerRunManager(adapter)  # type: ignore[arg-type]
    chunks = await manager.output_chunks_after(run_id, 0)
    assert [chunk["content"] for chunk in chunks if chunk["operation_index"] == 0] == [
        "first\n"
    ]
    all_chunks = await manager.output_chunks_after(run_id, -1)
    assert {
        (chunk["operation_index"], chunk["content"]) for chunk in all_chunks
    } >= {(2, "build output\n"), (3, "test output\n")}
    resumed = await manager.output_chunks_after(run_id, int(chunks[0]["id"]))
    assert all(chunk["operation_index"] != 0 for chunk in resumed)
    assert {
        (chunk["operation_index"], chunk["content"]) for chunk in resumed
    } >= {(2, "build output\n"), (3, "test output\n")}

    await dispatcher._append_output_chunk(
        run_id, 0, "stdout", 1, 6, b"x" * (256 * 1024)
    )
    async with database.begin() as session:
        count = await session.execute(
            text(
                "SELECT COALESCE(SUM(octet_length(content)), 0) "
                "FROM worker_operation_output_chunks "
                "WHERE run_id = :run AND operation_index = 0"
            ),
            {"run": run_id},
        )
        assert int(count.scalar_one()) <= 256 * 1024

    operation = FixedOperation("scene", "build", ("true",))
    await dispatcher._operation(
        run_id,
        operation,
        0,
        "passed",
        "authoritative output\n",
        "",
        0,
        datetime.now(UTC),
    )
    async with database.begin() as session:
        chunks_left = await session.execute(
            text(
                "SELECT COUNT(*) FROM worker_operation_output_chunks "
                "WHERE run_id = :run AND operation_index = 0"
            ),
            {"run": run_id},
        )
        artifact = await session.execute(
            text(
                "SELECT a.sha256, p.content FROM worker_artifacts a "
                "JOIN worker_artifact_payloads p ON p.artifact_id = a.id "
                "WHERE a.run_id = :run AND a.operation_id = "
                "(SELECT id FROM worker_operations WHERE run_id = :run "
                "AND operation_index = 0) AND a.kind = 'stdout'"
            ),
            {"run": run_id},
        )
        assert chunks_left.scalar_one() == 0
        row = artifact.one()
        assert row[0] == hashlib.sha256(b"authoritative output\n").hexdigest()
        assert bytes(row[1]) == b"authoritative output\n"


def test_recurrence_advances_from_scheduled_instant() -> None:
    due = datetime(2026, 1, 1, 9, tzinfo=UTC)
    assert next_due(due, "daily") == datetime(2026, 1, 2, 9, tzinfo=UTC)
    assert next_due(due, "weekly") == datetime(2026, 1, 8, 9, tzinfo=UTC)
    assert next_due(due, None) is None


async def test_reminders_are_exactly_once_and_namespace_scoped(database):
    now = datetime(2026, 1, 5, 12, tzinfo=UTC)
    async with database.begin() as session:
        await session.execute(
            text(
                "INSERT INTO reminders (namespace, text, due_at, recurrence) "
                "VALUES ('pj-digi', 'check launch', :due, 'daily')"
            ),
            {"due": now - timedelta(days=2)},
        )
    adapter = _DatabaseAdapter(database)
    assert await sweep_reminders(adapter, now) == 1
    assert await sweep_reminders(_DatabaseAdapter(database), now) == 0
    async with database.begin() as session:
        count = await session.execute(
            text(
                "SELECT COUNT(*) FROM reminder_occurrences o "
                "JOIN reminders r ON r.id = o.reminder_id "
                "WHERE r.namespace = 'pj-digi'"
            )
        )
        assert count.scalar_one() == 3
    assert await recent_notifications(adapter, "jsv-fashion") == []
    assert await notifications_after(adapter, "jsv-fashion", 0) == []
    assert len(await notifications_after(adapter, "pj-digi", 0)) == 1
    notifications = await recent_notifications(adapter, "pj-digi")
    assert len(notifications) == 1
    original = notifications[0]
    assert await acknowledge_notification(
        adapter, "jsv-fashion", int(notifications[0]["id"])
    ) is False
    assert await acknowledge_notification(
        adapter, "pj-digi", int(notifications[0]["id"])
    ) is True
    assert await acknowledge_notification(
        adapter, "pj-digi", int(notifications[0]["id"])
    ) is False
    assert await recent_notifications(adapter, "pj-digi") == []
    assert len(
        await recent_notifications(adapter, "pj-digi", include_acknowledged=True)
    ) == 1
    assert await notifications_after(adapter, "pj-digi", 0) == []
    assert len(
        await notifications_after(
            adapter, "pj-digi", 0, include_acknowledged=True
        )
    ) == 1
    after = (
        await recent_notifications(
            adapter, "pj-digi", include_acknowledged=True
        )
    )[0]
    assert after["body"] == original["body"]
    assert after["created_at"] == original["created_at"]


async def test_cancelled_recurring_reminder_stays_auditable_but_never_fires(database):
    adapter = _DatabaseAdapter(database)
    reminder_id = await create_reminder(
        adapter,
        "general",
        "cancel this",
        datetime(2026, 1, 1, 9, tzinfo=UTC),
        "daily",
    )
    assert await sweep_reminders(adapter, datetime(2026, 1, 1, 9, tzinfo=UTC)) == 1
    assert await cancel_reminder(adapter, "general", reminder_id)
    assert await sweep_reminders(adapter, datetime(2026, 1, 5, 9, tzinfo=UTC)) == 0
    async with database.begin() as session:
        reminder = await session.execute(
            text("SELECT active FROM reminders WHERE id = :id"),
            {"id": reminder_id},
        )
        assert reminder.scalar_one() is False
        occurrences = await session.execute(
            text(
                "SELECT COUNT(*) FROM reminder_occurrences WHERE reminder_id = :id"
            ),
            {"id": reminder_id},
        )
        assert occurrences.scalar_one() == 1
    assert await recent_reminders(adapter, "general") == []
    assert len(await recent_notifications(adapter, "general")) == 1


async def test_reminder_local_time_and_empty_briefing_are_deterministic(database):
    from zoneinfo import ZoneInfo

    local_due = datetime.fromisoformat("2026-01-02T00:30").replace(
        tzinfo=ZoneInfo("Asia/Kolkata")
    )
    reminder_id = await create_reminder(
        _DatabaseAdapter(database),
        "general",
        "start the day",
        local_due.astimezone(UTC),
        None,
    )
    assert reminder_id > 0
    async with database.begin() as session:
        stored = await session.execute(
            text("SELECT due_at FROM reminders WHERE id = :id"),
            {"id": reminder_id},
        )
        assert stored.scalar_one() == datetime(2026, 1, 1, 19, 0, tzinfo=UTC)
    briefing = await compose_briefing(
        _DatabaseAdapter(database),
        "general",
        "Asia/Kolkata",
        datetime(2026, 1, 1, 19, tzinfo=UTC),
    )
    assert briefing["local_date"].isoformat() == "2026-01-02"
    assert "Reminders due today" in str(briefing["content"])
    empty = await compose_briefing(
        _DatabaseAdapter(database),
        "vsports",
        "Asia/Kolkata",
        datetime(2026, 1, 1, 19, tzinfo=UTC),
    )
    assert empty["content"] == "Nothing needs your attention today."


async def test_today_briefing_refreshes_but_past_briefing_stays_frozen(database):
    now = datetime.now(UTC).replace(microsecond=0)
    today = now.date()
    yesterday = today - timedelta(days=1)
    async with database.begin() as session:
        await session.execute(
            text(
                "INSERT INTO daily_briefings "
                "(namespace, local_date, generated_at, content) "
                "VALUES ('general', :today, :generated, 'frozen today')"
            ),
            {"today": today, "generated": now - timedelta(hours=1)},
        )
        await session.execute(
            text(
                "INSERT INTO daily_briefings "
                "(namespace, local_date, generated_at, content) "
                "VALUES ('general', :yesterday, :generated, 'frozen past')"
            ),
            {"yesterday": yesterday, "generated": now - timedelta(days=1)},
        )
    today_briefing = await compose_briefing(
        _DatabaseAdapter(database), "general", "UTC", now
    )
    assert today_briefing["content"] != "frozen today"
    past_briefing = await compose_briefing(
        _DatabaseAdapter(database), "general", "UTC", now - timedelta(days=1)
    )
    assert past_briefing["content"] == "frozen past"


async def test_briefing_never_invokes_model_provider(database, monkeypatch):
    chat = AsyncMock()
    plan = AsyncMock()
    monkeypatch.setattr(FakeProvider, "chat", chat)
    monkeypatch.setattr(FakeProvider, "plan", plan)
    briefing = await compose_briefing(
        _DatabaseAdapter(database),
        "general",
        "Asia/Kolkata",
        datetime(2026, 1, 1, 19, tzinfo=UTC),
    )
    assert briefing["content"] == "Nothing needs your attention today."
    chat.assert_not_awaited()
    plan.assert_not_awaited()


async def test_late_recurring_reminder_emits_one_notification_with_skip_count(database):
    now = datetime(2026, 1, 8, 9, tzinfo=UTC)
    async with database.begin() as session:
        await session.execute(
            text(
                "INSERT INTO reminders (namespace, text, due_at, recurrence) "
                "VALUES ('general', 'stand up', :due, 'daily')"
            ),
            {"due": datetime(2026, 1, 1, 9, tzinfo=UTC)},
        )
    adapter = _DatabaseAdapter(database)
    assert await sweep_reminders(adapter, now) == 1
    notifications = await recent_notifications(adapter, "general")
    assert len(notifications) == 1
    assert "skipped 7 scheduled occurrences" in str(notifications[0]["body"])
    async with database.begin() as session:
        occurrences = await session.execute(
            text(
                "SELECT COUNT(*), MAX(due_at) FROM reminder_occurrences "
                "WHERE reminder_id = (SELECT id FROM reminders WHERE text = 'stand up')"
            )
        )
        count, latest = occurrences.one()
        assert count == 8
        assert latest == now


async def test_persistent_runner_failure_is_visible_in_durable_health(database):
    adapter = _DatabaseAdapter(database)
    await record_runner_health_failure(
        adapter, "reminder_sweep", "permission denied for table reminders"
    )
    health = await recent_runner_health(adapter)
    assert len(health) == 1
    assert health[0]["component"] == "reminder_sweep"
    assert health[0]["status"] == "failed"
    assert "permission denied" in str(health[0]["detail"])


async def test_runner_health_distinguishes_never_recent_and_failed(database):
    adapter = _DatabaseAdapter(database)
    assert await recent_runner_health(adapter) == []
    await record_runner_health_success(adapter, "reminder_sweep")
    health = await recent_runner_health(adapter)
    assert len(health) == 1
    assert health[0]["status"] == "healthy"
    assert health[0]["last_succeeded_at"] is not None
    await record_runner_health_failure(adapter, "reminder_sweep", "database unavailable")
    assert (await recent_runner_health(adapter))[0]["status"] == "failed"
    await record_runner_health_success(adapter, "reminder_sweep")
    recovered = await recent_runner_health(adapter)
    assert recovered[0]["status"] == "healthy"
    assert recovered[0]["last_succeeded_at"] is not None


async def test_runner_privilege_assertion_handles_non_id_primary_key(database):
    database_url = database.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    parsed = urlsplit(database_url)
    admin = await asyncpg.connect(database_url)
    role = f"runner_test_{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex
    try:
        await admin.execute(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\'')
        await admin.execute(
            f'GRANT CONNECT ON DATABASE "{parsed.path.lstrip("/")}" TO "{role}"'
        )
        await admin.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        await admin.execute(
            f'GRANT SELECT, INSERT ON worker_run_heartbeats TO "{role}"'
        )
        runner_database_url = urlunsplit(
            (
                parsed.scheme,
                f"{role}:{password}@{parsed.hostname}:{parsed.port}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
        connection = await asyncpg.connect(runner_database_url)
        try:
            await assert_runner_privileges(
                connection,
                ["INSERT INTO worker_run_heartbeats (run_id) VALUES (1)"],
            )
        finally:
            await connection.close()
    finally:
        await admin.execute(f'DROP OWNED BY "{role}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


async def test_runner_brand_profile_access_is_read_only(database):
    database_url = database.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    parsed = urlsplit(database_url)
    admin = await asyncpg.connect(database_url)
    role = f"runner_brand_{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex
    try:
        await admin.execute(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\'')
        await admin.execute(
            f'GRANT CONNECT ON DATABASE "{parsed.path.lstrip("/")}" TO "{role}"'
        )
        await admin.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        await admin.execute(
            f'GRANT SELECT ON decisions, brand_profiles TO "{role}"'
        )
        runner_database_url = urlunsplit(
            (
                parsed.scheme,
                f"{role}:{password}@{parsed.hostname}:{parsed.port}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
        non_granting_connection = await asyncpg.connect(runner_database_url)
        try:
            with pytest.raises(
                SystemExit,
                match="requires a database role that can grant and revoke privileges",
            ):
                await reconcile_runner_privileges(non_granting_connection, role)
        finally:
            await non_granting_connection.close()
        await reconcile_runner_privileges(admin, role)
        connection = await asyncpg.connect(runner_database_url)
        try:
            with pytest.raises(
                SystemExit,
                match="reached sensitive tables: chat_transcript_entries",
            ):
                await reconcile_runner_privileges(
                    admin,
                    role,
                    [
                        "SELECT content FROM chat_transcript_entries",
                        "SELECT id FROM decisions",
                    ],
                )
            assert not await admin.fetchval(
                "SELECT has_table_privilege($1, 'chat_transcript_entries', 'SELECT')",
                role,
            )
            with pytest.raises(
                SystemExit, match="runner lacks INSERT on decisions"
            ):
                await assert_runner_privileges(
                    connection,
                    [
                        "INSERT INTO decisions (decision) VALUES ('unclassified')",
                        "SELECT namespace FROM brand_profiles",
                    ],
                )
            await admin.execute(f'GRANT INSERT ON brand_profiles TO "{role}"')
            with pytest.raises(
                SystemExit, match="runner unexpectedly has INSERT on brand_profiles"
            ):
                await assert_runner_privileges(
                    connection,
                    [
                        "application_only_sql(text(\"INSERT INTO decisions "
                        "(decision) VALUES ('classified')\"))",
                        "SELECT namespace FROM brand_profiles",
                    ],
                )
            await admin.execute(f'REVOKE INSERT ON brand_profiles FROM "{role}"')
            await assert_runner_privileges(
                connection,
                [
                    "application_only_sql(text(\"INSERT INTO decisions "
                    "(decision) VALUES ('classified')\"))",
                    "SELECT namespace FROM brand_profiles",
                ],
            )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(
                    "DELETE FROM brand_profiles WHERE namespace = 'general'"
                )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(
                    "UPDATE decisions SET decision = decision WHERE id = 1"
                )
        finally:
            await connection.close()
    finally:
        await admin.execute(f'DROP OWNED BY "{role}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()


async def test_runner_health_upsert_requires_update_privilege(database):
    database_url = database.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    parsed = urlsplit(database_url)
    admin = await asyncpg.connect(database_url)
    role = f"runner_health_{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex
    component = f"test_{uuid.uuid4().hex[:8]}"
    try:
        await admin.execute(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\'')
        await admin.execute(
            f'GRANT CONNECT ON DATABASE "{parsed.path.lstrip("/")}" TO "{role}"'
        )
        await admin.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        await admin.execute(
            f"INSERT INTO runner_health "
            f"(component, status, detail, first_failed_at, last_failed_at, "
            f"consecutive_failures, resolved_at, last_succeeded_at) "
            f"VALUES ('{component}', 'failed', 'seed', now(), now(), 1, NULL, NULL)"
        )
        await admin.execute(f'GRANT INSERT, SELECT ON runner_health TO "{role}"')
        role_url = urlunsplit(
            (
                "postgresql+asyncpg",
                f"{role}:{password}@{parsed.hostname}:{parsed.port}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
        role_database = Database(Settings(database_url=role_url))
        try:
            with pytest.raises(DBAPIError):
                await record_runner_health_success(role_database, component)
            await admin.execute(f'GRANT UPDATE ON runner_health TO "{role}"')
            await record_runner_health_success(role_database, component)
        finally:
            await role_database.close()
    finally:
        await admin.execute(f"DELETE FROM runner_health WHERE component = '{component}'")
        await admin.execute(f'DROP OWNED BY "{role}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()
