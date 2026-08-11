import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .embedding import EmbedderProtocol, vector_literal
from .provider import ExtractedMemory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Conflict:
    key: str
    existing: str
    proposed: str
    decision_id: int
    conflict_id: int = 0


@dataclass(frozen=True)
class Recall:
    content: str
    source_type: str
    similarity: float


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


class MemoryStore:
    def __init__(self, embedder: EmbedderProtocol) -> None:
        self.embedder = embedder

    async def append_decision(self, session: AsyncSession, item: ExtractedMemory) -> int:
        result = await session.execute(
            text(
                "INSERT INTO decisions (project, decision, rationale, source, decision_key) "
                "VALUES (:project, :decision, :rationale, :source, :key) RETURNING id"
            ),
            {
                "project": item.project,
                "decision": item.value,
                "rationale": item.rationale,
                "source": item.source,
                "key": item.key,
            },
        )
        return int(result.scalar_one())

    async def active_decisions(self, session: AsyncSession, key: str) -> list[tuple[int, str]]:
        result = await session.execute(
            text(
                "SELECT id, decision FROM decisions "
                "WHERE decision_key = :key AND superseded_by IS NULL ORDER BY id"
            ),
            {"key": key},
        )
        return [(int(row.id), str(row.decision)) for row in result]

    async def record_memories(
        self, session: AsyncSession, memories: list[ExtractedMemory]
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        to_append: list[ExtractedMemory] = []
        for memory in memories:
            existing = await self.active_decisions(session, memory.key)
            normalized = normalize(memory.value)
            if any(normalize(value) == normalized for _, value in existing):
                continue
            if existing:
                result = await session.execute(
                    text(
                        "INSERT INTO memory_conflicts "
                        "(decision_key, existing_decision_id, proposed_value, proposed_rationale, "
                        "proposed_project, proposed_source) "
                        "VALUES (:key, :existing_id, :value, :rationale, :project, :source) "
                        "RETURNING id"
                    ),
                    {
                        "key": memory.key,
                        "existing_id": existing[0][0],
                        "value": memory.value,
                        "rationale": memory.rationale,
                        "project": memory.project,
                        "source": memory.source,
                    },
                )
                conflicts.append(
                    Conflict(memory.key, existing[0][1], memory.value, existing[0][0], int(result.scalar_one()))
                )
            else:
                to_append.append(memory)
        for memory in to_append:
            await self.append_decision(session, memory)
        return conflicts

    async def decisions(self, session: AsyncSession) -> list[dict[str, object]]:
        result = await session.execute(
            text(
                "SELECT id, decision_key, decision, rationale, project, source "
                "FROM decisions WHERE superseded_by IS NULL ORDER BY id DESC"
            )
        )
        return [dict(row._mapping) for row in result]

    async def conflicts(self, session: AsyncSession) -> list[dict[str, object]]:
        result = await session.execute(
            text(
                "SELECT c.id, c.decision_key, c.existing_decision_id, d.decision AS existing_value, "
                "c.proposed_value, c.proposed_rationale, c.proposed_project, c.proposed_source "
                "FROM memory_conflicts c JOIN decisions d ON d.id = c.existing_decision_id "
                "WHERE c.resolution_decision_id IS NULL ORDER BY c.id DESC"
            )
        )
        return [dict(row._mapping) for row in result]

    async def resolve_conflict(
        self, session: AsyncSession, conflict_id: int, choice: str
    ) -> int:
        result = await session.execute(
            text(
                "SELECT decision_key, existing_decision_id, proposed_value, proposed_rationale, "
                "proposed_project, proposed_source FROM memory_conflicts "
                "WHERE id = :id AND resolution_decision_id IS NULL"
            ),
            {"id": conflict_id},
        )
        conflict = result.mappings().one_or_none()
        if conflict is None or choice not in {"existing", "proposed"}:
            raise ValueError("invalid or already resolved conflict")
        if choice == "existing":
            value = await session.execute(
                text("SELECT decision, rationale, project, source FROM decisions WHERE id = :id"),
                {"id": conflict["existing_decision_id"]},
            )
            existing = value.mappings().one()
            replacement = ExtractedMemory(
                str(conflict["decision_key"]),
                str(existing["decision"]),
                str(existing["rationale"]) if existing["rationale"] else None,
                str(existing["project"]) if existing["project"] else None,
                str(existing["source"]),
            )
        else:
            replacement = ExtractedMemory(
                str(conflict["decision_key"]),
                str(conflict["proposed_value"]),
                str(conflict["proposed_rationale"]) if conflict["proposed_rationale"] else None,
                str(conflict["proposed_project"]) if conflict["proposed_project"] else None,
                str(conflict["proposed_source"]),
            )
        new_id = await self.append_decision(session, replacement)
        await session.execute(
            text("UPDATE decisions SET superseded_by = :new_id WHERE id = :old_id"),
            {"new_id": new_id, "old_id": conflict["existing_decision_id"]},
        )
        await session.execute(
            text("UPDATE memory_conflicts SET resolution_decision_id = :new_id WHERE id = :id"),
            {"new_id": new_id, "id": conflict_id},
        )
        return new_id

    async def add_chunk(
        self,
        session: AsyncSession,
        content: str,
        source_type: str,
        source_id: str | None,
        metadata: dict[str, object],
    ) -> None:
        embedding = vector_literal(self.embedder.embed(content))
        await session.execute(
            text(
                "INSERT INTO memory_chunks (content, source_type, source_id, metadata, embedding) "
                "VALUES (:content, :source_type, :source_id, CAST(:metadata AS jsonb), "
                "CAST(:embedding AS vector))"
            ),
            {
                "content": content,
                "source_type": source_type,
                "source_id": source_id,
                "metadata": json.dumps(metadata),
                "embedding": embedding,
            },
        )

    async def recall(self, session: AsyncSession, query: str, limit: int = 5) -> list[Recall]:
        embedding = vector_literal(self.embedder.embed(query))
        result = await session.execute(
            text(
                "SELECT content, source_type, 1 - "
                "(embedding <=> CAST(:embedding AS vector)) AS similarity "
                "FROM memory_chunks ORDER BY embedding <=> CAST(:embedding AS vector) "
                "LIMIT CAST(:limit AS integer)"
            ),
            {"embedding": embedding, "limit": limit},
        )
        return [
            Recall(str(row.content), str(row.source_type), float(row.similarity))
            for row in result
        ]

    async def supersede(
        self, session: AsyncSession, old_id: int, replacement: ExtractedMemory
    ) -> int:
        new_id = await self.append_decision(session, replacement)
        await session.execute(
            text("UPDATE decisions SET superseded_by = :new_id WHERE id = :old_id"),
            {"new_id": new_id, "old_id": old_id},
        )
        return new_id
