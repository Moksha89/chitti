import os
import subprocess

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
from chitti.worker import approved_revision

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_DB_TESTS"), reason="set RUN_DB_TESTS=1 to run PostgreSQL integration tests"
)


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
