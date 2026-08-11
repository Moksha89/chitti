import json
import logging
import re
from dataclasses import dataclass
from math import sqrt

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


BELIEF_MATCH_THRESHOLD = 0.62


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def normalize_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    for prefix in ("preferred_stack_", "preferences_", "preferred_", "stack_"):
        if key.startswith(prefix):
            return key[len(prefix) :]
    return key


def belief_subject(key: str, value: str) -> str:
    return f"{normalize_key(key)}: {normalize(value)}"


def cosine_similarity(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = sqrt(sum(value * value for value in left))
    right_norm = sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


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
                "key": normalize_key(item.key),
            },
        )
        decision_id = int(result.scalar_one())
        await self._insert_embedding(session, decision_id, item.key, item.value)
        return decision_id

    async def _insert_embedding(
        self, session: AsyncSession, decision_id: int, key: str, value: str
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO decision_embeddings (decision_id, key_normalized, embedding) "
                "VALUES (:id, :key, CAST(:embedding AS vector)) "
                "ON CONFLICT (decision_id) DO NOTHING"
            ),
            {
                "id": decision_id,
                "key": normalize_key(key),
                "embedding": vector_literal(self.embedder.embed(belief_subject(key, value))),
            },
        )

    async def ensure_belief_embeddings(self, session: AsyncSession) -> None:
        result = await session.execute(
            text(
                "SELECT d.id, d.decision_key, d.decision FROM decisions d "
                "LEFT JOIN decision_embeddings e ON e.decision_id = d.id "
                "WHERE e.decision_id IS NULL"
            )
        )
        for row in result:
            await self._insert_embedding(
                session, int(row.id), str(row.decision_key), str(row.decision)
            )

    async def active_beliefs(self, session: AsyncSession) -> list[dict[str, object]]:
        result = await session.execute(
            text(
                "SELECT d.id, d.decision_key, d.decision FROM decisions d "
                "LEFT JOIN decision_forgets f ON f.decision_id = d.id "
                "WHERE d.superseded_by IS NULL AND f.id IS NULL ORDER BY d.id"
            ),
        )
        return [dict(row._mapping) for row in result]

    async def active_keys(self, session: AsyncSession) -> list[str]:
        return [str(item["decision_key"]) for item in await self.active_beliefs(session)]

    async def find_matching_belief(
        self, session: AsyncSession, memory: ExtractedMemory
    ) -> tuple[dict[str, object] | None, float]:
        await self.ensure_belief_embeddings(session)
        embedding = vector_literal(self.embedder.embed(belief_subject(memory.key, memory.value)))
        result = await session.execute(
            text(
                "WITH nearest AS ("
                "SELECT d.id, d.decision_key, d.decision, "
                "1 - (e.embedding <=> CAST(:embedding AS vector)) AS similarity "
                "FROM decision_embeddings e JOIN decisions d ON d.id = e.decision_id "
                "LEFT JOIN decision_forgets f ON f.decision_id = d.id "
                "WHERE d.superseded_by IS NULL AND f.id IS NULL "
                "ORDER BY e.embedding <=> CAST(:embedding AS vector) LIMIT 20"
                "), exact AS ("
                "SELECT d.id, d.decision_key, d.decision, 1.0 AS similarity "
                "FROM decision_embeddings e JOIN decisions d ON d.id = e.decision_id "
                "LEFT JOIN decision_forgets f ON f.decision_id = d.id "
                "WHERE d.superseded_by IS NULL AND f.id IS NULL "
                "AND e.key_normalized = :key"
                ") SELECT DISTINCT ON (id) id, decision_key, decision, similarity "
                "FROM (SELECT * FROM nearest UNION ALL SELECT * FROM exact) candidates "
                "ORDER BY id, similarity DESC"
            ),
            {"embedding": embedding, "key": normalize_key(memory.key)},
        )
        candidates = [dict(row._mapping) for row in result]
        canonical_key = normalize_key(memory.key)
        for item in candidates:
            if normalize_key(str(item["decision_key"])) == canonical_key:
                return item, 1.0
        if not candidates:
            return None, 0.0
        best = max(candidates, key=lambda item: float(item["similarity"]))
        similarity = float(best["similarity"])
        if similarity >= BELIEF_MATCH_THRESHOLD:
            return best, similarity
        return None, similarity

    def matching_belief(
        self, existing: list[dict[str, object]], memory: ExtractedMemory
    ) -> tuple[dict[str, object] | None, float]:
        proposed_key = normalize_key(memory.key)
        proposed_embedding = self.embedder.embed(belief_subject(memory.key, memory.value))
        best: tuple[dict[str, object] | None, float] = (None, 0.0)
        for item in existing:
            existing_key = str(item["decision_key"])
            if normalize_key(existing_key) == proposed_key:
                similarity = 1.0
            else:
                similarity = cosine_similarity(
                    proposed_embedding,
                    self.embedder.embed(belief_subject(existing_key, str(item["decision"]))),
                )
            if similarity > best[1]:
                best = (item, similarity)
        if best[1] >= BELIEF_MATCH_THRESHOLD:
            return best
        return None, best[1]

    async def record_memories(
        self, session: AsyncSession, memories: list[ExtractedMemory]
    ) -> list[Conflict]:
        conflicts: list[Conflict] = []
        for memory in memories:
            normalized = normalize(memory.value)
            match, _ = await self.find_matching_belief(session, memory)
            if match and normalize(str(match["decision"])) == normalized:
                continue
            if match:
                existing_key = str(match["decision_key"])
                result = await session.execute(
                    text(
                        "INSERT INTO memory_conflicts "
                        "(decision_key, existing_decision_id, proposed_value, proposed_rationale, "
                        "proposed_project, proposed_source) "
                        "VALUES (:key, :existing_id, :value, :rationale, :project, :source) "
                        "RETURNING id"
                    ),
                    {
                        "key": existing_key,
                        "existing_id": match["id"],
                        "value": memory.value,
                        "rationale": memory.rationale,
                        "project": memory.project,
                        "source": memory.source,
                    },
                )
                conflicts.append(
                    Conflict(
                        existing_key,
                        str(match["decision"]),
                        memory.value,
                        int(str(match["id"])),
                        int(result.scalar_one()),
                    )
                )
            else:
                canonical = ExtractedMemory(
                    normalize_key(memory.key),
                    memory.value,
                    memory.rationale,
                    memory.project,
                    memory.source,
                )
                await self.append_decision(session, canonical)
        return conflicts

    async def decisions(self, session: AsyncSession) -> list[dict[str, object]]:
        result = await session.execute(
            text(
                "SELECT d.id, d.decision_key, d.decision, d.rationale, d.project, d.source "
                "FROM decisions d LEFT JOIN decision_forgets f ON f.decision_id = d.id "
                "WHERE d.superseded_by IS NULL AND f.id IS NULL ORDER BY d.id DESC"
            )
        )
        return [dict(row._mapping) for row in result]

    async def conflicts(self, session: AsyncSession) -> list[dict[str, object]]:
        result = await session.execute(
            text(
                "SELECT c.id, c.decision_key, c.existing_decision_id, d.decision AS existing_value, "
                "c.proposed_value, c.proposed_rationale, c.proposed_project, c.proposed_source "
                "FROM memory_conflicts c JOIN decisions d ON d.id = c.existing_decision_id "
                "LEFT JOIN decision_forgets f ON f.decision_id = d.id "
                "WHERE c.resolution_decision_id IS NULL AND f.id IS NULL ORDER BY c.id DESC"
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

    async def forget_decision(self, session: AsyncSession, decision_id: int) -> None:
        result = await session.execute(
            text(
                "SELECT d.id FROM decisions d LEFT JOIN decision_forgets f "
                "ON f.decision_id = d.id WHERE d.id = :id AND d.superseded_by IS NULL "
                "AND f.id IS NULL"
            ),
            {"id": decision_id},
        )
        if result.scalar_one_or_none() is None:
            raise ValueError("invalid or already forgotten decision")
        await session.execute(
            text("INSERT INTO decision_forgets (decision_id) VALUES (:id)"),
            {"id": decision_id},
        )

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
