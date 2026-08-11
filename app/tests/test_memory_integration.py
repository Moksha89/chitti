import os
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from chitti.embedding import FakeEmbedder
from chitti.memory import MemoryStore
from chitti.provider import ExtractedMemory

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_DB_TESTS"), reason="set RUN_DB_TESTS=1 to run PostgreSQL integration tests"
)


@pytest.fixture
async def store():
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
        memory = MemoryStore(FakeEmbedder())
        yield engine, memory
        await engine.dispose()


async def test_append_only_and_retrieval(store) -> None:
    engine, memory = store
    async with engine.connect() as session:
        item = ExtractedMemory("stack", "FastAPI", "user stated", None, "user_stated")
        async with session.begin():
            first = await memory.append_decision(session, item)
            conflicts = await memory.record_memories(session, [item])
            assert first > 0
            assert conflicts == []
            conflicts = await memory.record_memories(
                session,
                [ExtractedMemory("stack", "Django", "conflicting preference", None, "user_stated")],
            )
            assert conflicts and conflicts[0].existing == "FastAPI"
            await memory.add_chunk(session, "FastAPI is preferred", "note", None, {})
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE decisions SET decision = 'forbidden' WHERE id = :id"),
                {"id": first},
            )
        await session.rollback()
        recalled = await memory.recall(session, "FastAPI", 1)
        assert recalled and "FastAPI" in recalled[0].content
