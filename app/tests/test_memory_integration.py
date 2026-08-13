import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from chitti.embedding import FakeEmbedder
from chitti.memory import MemoryStore, proposal_fingerprint
from chitti.provider import ExtractedMemory

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_DB_TESTS"), reason="set RUN_DB_TESTS=1 to run PostgreSQL integration tests"
)
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def store():
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
        memory = MemoryStore(FakeEmbedder())
        yield engine, memory
        await engine.dispose()


async def test_append_only_and_retrieval(store) -> None:
    engine, memory = store
    async with engine.connect() as session:
        item = ExtractedMemory("stack", "FastAPI", "user stated", None, "user_stated")
        async with session.begin():
            first = await memory.append_decision(session, item, "general")
            conflicts = await memory.record_memories(session, [item], "general")
            assert first > 0
            assert conflicts == []
            conflicts = await memory.record_memories(
                session,
                [ExtractedMemory("stack", "Django", "conflicting preference", None, "user_stated")],
                "general",
            )
            assert conflicts and conflicts[0].existing == "FastAPI"
            await memory.add_chunk(session, "FastAPI is preferred", "note", None, {}, "general")
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE decisions SET decision = 'forbidden' WHERE id = :id"),
                {"id": first},
            )
        await session.rollback()
        recalled = await memory.recall(session, "FastAPI", "general", 1)
        assert recalled and "FastAPI" in recalled[0].content


async def test_rephrased_key_conflicts_and_forget_hides_without_deleting(store) -> None:
    engine, memory = store
    async with engine.begin() as session:
        first = await memory.append_decision(
            session,
            ExtractedMemory(
                "preferred_stack.frontend_framework", "SvelteKit", "user stated", None, "user_stated"
            ),
            namespace="general",
        )
        conflicts = await memory.record_memories(
            session,
            [ExtractedMemory("preferred_frontend_framework", "Next.js", "replacement", None, "user_stated")],
            "general",
        )
        assert conflicts and conflicts[0].key == "frontend_framework"
        await memory.forget_decision(session, first)
        assert await memory.decisions(session, "general") == []
        result = await session.execute(text("SELECT id FROM decisions WHERE id = :id"), {"id": first})
        assert result.scalar_one() == first


async def test_conflicting_extractions_in_one_batch_use_real_decision_ids(store) -> None:
    engine, memory = store
    async with engine.begin() as session:
        conflicts = await memory.record_memories(
            session,
            [
                ExtractedMemory("deployment_target", "VPS", "first", None, "user_stated"),
                ExtractedMemory("deployment_target", "managed cloud", "second", None, "user_stated"),
            ],
            "general",
        )
        assert len(conflicts) == 1
        assert conflicts[0].decision_id > 0
        result = await session.execute(
            text("SELECT COUNT(*) FROM decisions WHERE decision_key = 'deployment_target'")
        )
        assert result.scalar_one() == 1


async def test_equivalent_conflict_recurrence_updates_one_open_row(store) -> None:
    engine, memory = store
    async with engine.begin() as session:
        await memory.append_decision(
            session,
            ExtractedMemory(
                "worker_caps",
                "Keep worker caps at $0.75 and 300,000 model tokens per run.",
                None,
                None,
                "user_stated",
            ),
            "general",
        )
        first = await memory.record_memories(
            session,
            [
                ExtractedMemory(
                    "worker_caps",
                    "Worker caps are $0.75 per run and 300,000 model tokens.",
                    None,
                    None,
                    "user_stated",
                )
            ],
            "general",
        )
        second = await memory.record_memories(
            session,
            [
                ExtractedMemory(
                    "worker_caps",
                    "Worker caps are $0.75 per run and 300,000 model tokens.",
                    None,
                    None,
                    "user_stated",
                )
            ],
            "general",
        )
        assert len(first) == len(second) == 1
        row = (
            await session.execute(
                text(
                    "SELECT COUNT(*) AS count, MAX(recurrence_count) AS recurrence_count "
                    "FROM memory_conflicts WHERE decision_key = 'worker_caps' "
                    "AND closed_at IS NULL"
                )
            )
        ).mappings().one()
        assert row["count"] == 1
        assert row["recurrence_count"] == 2
        visible = await memory.conflicts(session, "general")
        assert visible[0]["proposed_value"] == (
            "Worker caps are $0.75 per run and 300,000 model tokens."
        )
        latest = (
            await session.execute(
                text(
                    "SELECT latest_proposed_value FROM memory_conflicts "
                    "WHERE decision_key = 'worker_caps' AND closed_at IS NULL"
                )
            )
        ).scalar_one()
        assert latest == "Worker caps are $0.75 per run and 300,000 model tokens."


async def test_different_conflicts_under_one_key_stay_open(store) -> None:
    engine, memory = store
    async with engine.begin() as session:
        await memory.append_decision(
            session,
            ExtractedMemory("style", "Tailwind CSS", None, None, "user_stated"),
            "general",
        )
        first = await memory.record_memories(
            session,
            [ExtractedMemory("style", "CSS modules", None, None, "user_stated")],
            "general",
        )
        second = await memory.record_memories(
            session,
            [ExtractedMemory("style", "Vanilla CSS", None, None, "user_stated")],
            "general",
        )
        assert first and second
        rows = (
            await session.execute(
                text(
                    "SELECT proposed_value, closed_at, closure_reason, superseded_by_conflict_id "
                    "FROM memory_conflicts WHERE decision_key = 'style' ORDER BY id"
                )
            )
        ).mappings().all()
        assert [row["proposed_value"] for row in rows] == ["CSS modules", "Vanilla CSS"]
        assert all(row["closed_at"] is None for row in rows)
        assert all(row["closure_reason"] is None for row in rows)
        assert all(row["superseded_by_conflict_id"] is None for row in rows)


async def test_resolution_records_actor_and_namespace_conflicts_are_private(store) -> None:
    engine, memory = store
    async with engine.begin() as session:
        await memory.append_decision(
            session,
            ExtractedMemory("shared_preference", "Existing", None, None, "user_stated"),
            "general",
        )
        conflicts = await memory.record_memories(
            session,
            [ExtractedMemory("shared_preference", "PJ proposal", None, None, "user_stated")],
            "pj-digi",
        )
        assert len(await memory.conflicts(session, "pj-digi")) == 1
        assert await memory.conflicts(session, "jsv-fashion") == []
        await memory.resolve_conflict(session, conflicts[0].conflict_id, "existing", "akirah")
        row = (
            await session.execute(
                text(
                    "SELECT resolution_actor, resolved_at, closure_reason "
                    "FROM memory_conflicts WHERE id = :id"
                ),
                {"id": conflicts[0].conflict_id},
            )
        ).mappings().one()
        assert row["resolution_actor"] == "akirah"
        assert row["resolved_at"] is not None
        assert row["closure_reason"] == "owner"


async def test_resolution_reconciles_open_sibling_conflicts_without_deleting(store) -> None:
    engine, memory = store
    async with engine.begin() as session:
        await memory.append_decision(
            session,
            ExtractedMemory(
                "worker_caps",
                "Keep worker caps at $0.75 and 300,000 model tokens per run.",
                None,
                None,
                "user_stated",
            ),
            "general",
        )
        agreeing = await memory.record_memories(
            session,
            [
                ExtractedMemory(
                    "worker_caps",
                    "Keep worker caps at $0.75 and 300,000 model tokens.",
                    None,
                    None,
                    "user_stated",
                )
            ],
            "general",
        )
        disagreeing = await memory.record_memories(
            session,
            [
                ExtractedMemory(
                    "worker_caps",
                    "Cap worker cost at $1.00 per run and 400,000 model tokens per run.",
                    None,
                    None,
                    "user_stated",
                )
            ],
            "general",
        )
        decision_id = (
            await session.execute(
                text(
                    "SELECT id FROM decisions WHERE decision_key = 'worker_caps' "
                    "AND superseded_by IS NULL ORDER BY id LIMIT 1"
                )
            )
        ).scalar_one()
        await session.execute(text("DROP INDEX memory_conflicts_one_open_per_proposal"))
        duplicate = (
            await session.execute(
                text(
                    "INSERT INTO memory_conflicts "
                    "(decision_key, existing_decision_id, proposed_value, proposed_source, "
                    "namespace, last_seen_at, latest_proposed_value, proposal_fingerprint) "
                    "VALUES ('worker_caps', :decision_id, :value, 'user_stated', 'general', "
                    "now(), :value, :fingerprint) RETURNING id"
                ),
                {
                    "decision_id": decision_id,
                    "value": "Keep worker caps at $0.75 and 300,000 model tokens.",
                    "fingerprint": proposal_fingerprint(
                        "Keep worker caps at $0.75 and 300,000 model tokens."
                    ),
                },
            )
        ).scalar_one()
        await memory.append_decision(
            session,
            ExtractedMemory("style", "Tailwind CSS", None, None, "user_stated"),
            "general",
        )
        unrelated = await memory.record_memories(
            session,
            [ExtractedMemory("style", "CSS modules", None, None, "user_stated")],
            "general",
        )
        before = (
            await session.execute(text("SELECT COUNT(*) FROM memory_conflicts"))
        ).scalar_one()
        new_id = await memory.resolve_conflict(
            session, agreeing[0].conflict_id, "proposed", "akirah"
        )
        after = (
            await session.execute(text("SELECT COUNT(*) FROM memory_conflicts"))
        ).scalar_one()
        assert after == before
        rows = (
            await session.execute(
                text(
                    "SELECT id, existing_decision_id, resolution_decision_id, "
                    "closed_at, closure_reason FROM memory_conflicts "
                    "WHERE id IN (:agreeing, :duplicate, :disagreeing, :unrelated) ORDER BY id"
                ),
                {
                    "agreeing": agreeing[0].conflict_id,
                    "duplicate": duplicate,
                    "disagreeing": disagreeing[0].conflict_id,
                    "unrelated": unrelated[0].conflict_id,
                },
            )
        ).mappings().all()
        by_id = {row["id"]: row for row in rows}
        selected = by_id[agreeing[0].conflict_id]
        reconciled = by_id[duplicate]
        remaining = by_id[disagreeing[0].conflict_id]
        untouched = by_id[unrelated[0].conflict_id]
        assert selected["resolution_decision_id"] == new_id
        assert selected["closure_reason"] == "owner"
        assert selected["closed_at"] is not None
        assert reconciled["resolution_decision_id"] == new_id
        assert reconciled["closure_reason"] == "owner_reconciled"
        assert reconciled["closed_at"] is not None
        assert remaining["existing_decision_id"] == new_id
        assert remaining["closed_at"] is None
        assert remaining["closure_reason"] is None
        assert untouched["existing_decision_id"] != new_id
        visible = await memory.conflicts(session, "general")
        remaining = next(
            row for row in visible if row["id"] == disagreeing[0].conflict_id
        )
        assert remaining["existing_value"] == (
            "Keep worker caps at $0.75 and 300,000 model tokens."
        )


async def test_conflict_repair_groups_by_equivalent_proposal_without_deleting(store) -> None:
    engine, _ = store
    env = {
        **os.environ,
        "DATABASE_URL": engine.url.render_as_string(hide_password=False).replace(
            "+asyncpg", "+psycopg"
        ),
    }
    subprocess.run(
        ["python", "-m", "alembic", "-c", "alembic.ini", "downgrade", "0021_runner_health_success"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )
    async with engine.begin() as session:
        decision = await session.execute(
            text(
                "INSERT INTO decisions "
                "(project, decision, rationale, source, decision_key, namespace) "
                "VALUES (NULL, 'Tailwind CSS', NULL, 'user_stated', 'styling_framework', 'general') "
                "RETURNING id"
            )
        )
        decision_id = decision.scalar_one()
        for value in (
            "The user writes plain CSS modules for styling on every project and has moved off Tailwind completely.",
            "Use Next.js App Router with React Three Fiber, Drei, and Three.js for generated websites.",
            "Use Next.js App Router with React Three Fiber, Drei, and Three.js for generated websites.",
        ):
            await session.execute(
                text(
                    "INSERT INTO memory_conflicts "
                    "(decision_key, existing_decision_id, proposed_value, proposed_source, namespace) "
                    "VALUES ('styling_framework', :decision_id, :value, 'user_stated', 'general')"
                ),
                {"decision_id": decision_id, "value": value},
            )
        before = await session.execute(text("SELECT COUNT(*) FROM memory_conflicts"))
        before_count = before.scalar_one()
    subprocess.run(
        ["python", "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )
    async with engine.begin() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, proposed_value, latest_proposed_value, closed_at, "
                    "closure_reason, superseded_by_conflict_id "
                    "FROM memory_conflicts WHERE decision_key = 'styling_framework' "
                    "ORDER BY id"
                )
            )
        ).mappings().all()
        after = await session.execute(text("SELECT COUNT(*) FROM memory_conflicts"))
        assert after.scalar_one() == before_count
        assert len(rows) == 3
        assert rows[0]["closed_at"] is None
        assert rows[0]["proposed_value"].startswith("The user writes plain CSS")
        assert rows[0]["latest_proposed_value"].startswith("The user writes plain CSS")
        assert rows[1]["closed_at"] is None
        assert rows[1]["latest_proposed_value"].startswith("Use Next.js")
        assert rows[2]["closure_reason"] == "deduplicated"
        assert rows[2]["superseded_by_conflict_id"] == rows[1]["id"]
        latest = (
            await session.execute(
                text(
                    "SELECT latest_proposed_value FROM memory_conflicts "
                    "WHERE id = :id"
                ),
                {"id": rows[0]["id"]},
            )
        ).scalar_one()
        assert latest.startswith("The user writes plain CSS")


async def test_memory_namespaces_isolate_business_data_and_share_general_data(store) -> None:
    engine, memory = store
    async with engine.begin() as session:
        await memory.add_chunk(session, "PJ private context", "note", None, {}, "pj-digi")
        await memory.add_chunk(session, "JSV private context", "note", None, {}, "jsv-fashion")
        await memory.add_chunk(session, "Owner shared context", "note", None, {}, "general")
        await memory.append_decision(
            session,
            ExtractedMemory("pj_rule", "PJ only", None, "PJ Digi", "user_stated"),
            "pj-digi",
        )
        await memory.append_decision(
            session,
            ExtractedMemory("jsv_rule", "JSV only", None, "JSV Fashion", "user_stated"),
            "jsv-fashion",
        )
        await memory.append_decision(
            session,
            ExtractedMemory("shared_rule", "Owner-wide", None, None, "user_stated"),
            "general",
        )
        pj_recall = await memory.recall(session, "context", "pj-digi", 10)
        jsv_recall = await memory.recall(session, "context", "jsv-fashion", 10)
        assert any(item.content == "PJ private context" for item in pj_recall)
        assert not any(item.content == "JSV private context" for item in pj_recall)
        assert any(item.content == "Owner shared context" for item in pj_recall)
        assert any(item.content == "JSV private context" for item in jsv_recall)
        assert not any(item.content == "PJ private context" for item in jsv_recall)
        pj_beliefs = await memory.active_beliefs(session, "pj-digi")
        jsv_beliefs = await memory.active_beliefs(session, "jsv-fashion")
        assert {str(item["decision_key"]) for item in pj_beliefs} == {"pj_rule", "shared_rule"}
        assert {str(item["decision_key"]) for item in jsv_beliefs} == {"jsv_rule", "shared_rule"}


async def test_memory_retrieval_rejects_unknown_namespace_instead_of_global_read(store) -> None:
    engine, memory = store
    async with engine.begin() as session:
        with pytest.raises(ValueError, match="unknown memory namespace"):
            await memory.recall(session, "anything", "not-a-business")


async def test_namespace_migration_preserves_legacy_rows_and_backfills_honestly(store) -> None:
    engine, _ = store
    env = {
        **os.environ,
        "DATABASE_URL": engine.url.render_as_string(hide_password=False).replace(
            "+asyncpg", "+psycopg"
        ),
    }
    subprocess.run(
        ["python", "-m", "alembic", "-c", "alembic.ini", "downgrade", "0015_approval_actor"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )
    embedding = "[" + ",".join(["0"] * 384) + "]"
    async with engine.begin() as session:
        await session.execute(
            text(
                "INSERT INTO decisions "
                "(project, decision, rationale, source, decision_key) "
                "VALUES ('PJ Digi', 'legacy belief', NULL, 'user_stated', 'legacy_belief')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO memory_chunks "
                "(content, source_type, source_id, metadata, embedding) "
                "VALUES ('explicit namespace', 'note', NULL, "
                "CAST('{\"namespace\":\"pj-digi\"}' AS json), CAST(:embedding AS vector))"
            ),
            {"embedding": embedding},
        )
        await session.execute(
            text(
                "INSERT INTO memory_chunks "
                "(content, source_type, source_id, metadata, embedding) "
                "VALUES ('undetermined namespace', 'note', NULL, "
                "CAST('{}' AS json), CAST(:embedding AS vector))"
            ),
            {"embedding": embedding},
        )
    subprocess.run(
        ["python", "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )
    async with engine.connect() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT content, namespace FROM memory_chunks "
                    "ORDER BY content"
                )
            )
        ).all()
        decision_namespace = (
            await session.execute(
                text("SELECT namespace FROM decisions WHERE decision_key = 'legacy_belief'")
            )
        ).scalar_one()
    assert rows == [
        ("explicit namespace", "pj-digi"),
        ("undetermined namespace", "general"),
    ]
    assert decision_namespace == "general"
