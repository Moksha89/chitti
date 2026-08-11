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
                conflicts.append(Conflict(memory.key, existing[0][1], memory.value, existing[0][0]))
            else:
                to_append.append(memory)
        for memory in to_append:
            await self.append_decision(session, memory)
        return conflicts

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
