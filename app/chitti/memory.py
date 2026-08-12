import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .embedding import EmbedderProtocol, vector_literal
from .provider import ExtractedMemory

logger = logging.getLogger(__name__)
SHARED_NAMESPACE = "general"
MEMORY_NAMESPACES = {
    SHARED_NAMESPACE: "Shared / general",
    "pj-digi": "PJ Digi",
    "jsv-fashion": "JSV Fashion",
    "andhrawala": "Andhrawala",
    "vsports": "VSports",
}


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


def normalize_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    for prefix in ("preferred_stack_", "preferences_", "preferred_", "stack_"):
        if key.startswith(prefix):
            return key[len(prefix) :]
    return key


def belief_subject(key: str) -> str:
    return normalize_key(key)


def normalize_namespace(namespace: str | None) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", (namespace or "").strip().lower()).strip("-")
    if not value:
        return SHARED_NAMESPACE
    if value not in MEMORY_NAMESPACES:
        raise ValueError(f"unknown memory namespace: {namespace}")
    return value


def namespace_options() -> list[dict[str, str]]:
    return [{"slug": slug, "display_name": name} for slug, name in MEMORY_NAMESPACES.items()]


class MemoryStore:
    def __init__(self, embedder: EmbedderProtocol) -> None:
        self.embedder = embedder

    async def append_decision(
        self, session: AsyncSession, item: ExtractedMemory, namespace: str = SHARED_NAMESPACE
    ) -> int:
        namespace = normalize_namespace(namespace)
        result = await session.execute(
            text(
                "INSERT INTO decisions "
                "(project, decision, rationale, source, decision_key, namespace) "
                "VALUES (:project, :decision, :rationale, :source, :key, :namespace) "
                "RETURNING id"
            ),
            {
                "project": item.project,
                "decision": item.value,
                "rationale": item.rationale,
                "source": item.source,
                "key": normalize_key(item.key),
                "namespace": namespace,
            },
        )
        return int(result.scalar_one())

    async def active_beliefs(
        self, session: AsyncSession, namespace: str
    ) -> list[dict[str, object]]:
        namespace = normalize_namespace(namespace)
        result = await session.execute(
            text(
                "SELECT d.id, d.decision_key, d.decision, d.namespace FROM decisions d "
                "LEFT JOIN decision_forgets f ON f.decision_id = d.id "
                "WHERE d.superseded_by IS NULL AND f.id IS NULL "
                "AND d.namespace IN (:namespace, :shared) ORDER BY d.id"
            ),
            {"namespace": namespace, "shared": SHARED_NAMESPACE},
        )
        return [dict(row._mapping) for row in result]

    async def active_keys(self, session: AsyncSession, namespace: str) -> list[str]:
        return [
            str(item["decision_key"])
            for item in await self.active_beliefs(session, namespace)
        ]

    async def find_matching_belief(
        self, session: AsyncSession, memory: ExtractedMemory, namespace: str
    ) -> tuple[dict[str, object] | None, float]:
        namespace = normalize_namespace(namespace)
        result = await session.execute(
            text(
                "SELECT d.id, d.decision_key, d.decision, 1.0 AS similarity "
                "FROM decisions d "
                "LEFT JOIN decision_forgets f ON f.decision_id = d.id "
                "WHERE d.superseded_by IS NULL AND f.id IS NULL "
                "AND d.decision_key = :key "
                "AND d.namespace IN (:namespace, :shared) "
                "ORDER BY CASE WHEN d.namespace = :namespace THEN 0 ELSE 1 END, d.id "
                "LIMIT 1"
            ),
            {"key": normalize_key(memory.key), "namespace": namespace, "shared": SHARED_NAMESPACE},
        )
        item = result.mappings().one_or_none()
        return (dict(item), 1.0) if item is not None else (None, 0.0)

    def matching_belief(
        self, existing: list[dict[str, object]], memory: ExtractedMemory
    ) -> tuple[dict[str, object] | None, float]:
        proposed_key = normalize_key(memory.key)
        for item in existing:
            existing_key = str(item["decision_key"])
            if normalize_key(existing_key) == proposed_key:
                return item, 1.0
        return None, 0.0

    async def record_memories(
        self, session: AsyncSession, memories: list[ExtractedMemory], namespace: str
    ) -> list[Conflict]:
        namespace = normalize_namespace(namespace)
        conflicts: list[Conflict] = []
        for memory in memories:
            normalized = normalize(memory.value)
            match, _ = await self.find_matching_belief(session, memory, namespace)
            if match and normalize(str(match["decision"])) == normalized:
                continue
            if match:
                existing_key = str(match["decision_key"])
                result = await session.execute(
                    text(
                        "INSERT INTO memory_conflicts "
                        "(decision_key, existing_decision_id, proposed_value, proposed_rationale, "
                        "proposed_project, proposed_source, namespace) "
                        "VALUES (:key, :existing_id, :value, :rationale, :project, :source, :namespace) "
                        "RETURNING id"
                    ),
                    {
                        "key": existing_key,
                        "existing_id": match["id"],
                        "value": memory.value,
                        "rationale": memory.rationale,
                        "project": memory.project,
                        "source": memory.source,
                        "namespace": namespace,
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
                await self.append_decision(session, canonical, namespace)
        return conflicts

    async def decisions(
        self, session: AsyncSession, namespace: str
    ) -> list[dict[str, object]]:
        namespace = normalize_namespace(namespace)
        result = await session.execute(
            text(
                "SELECT d.id, d.decision_key, d.decision, d.rationale, d.project, "
                "d.source, d.namespace "
                "FROM decisions d LEFT JOIN decision_forgets f ON f.decision_id = d.id "
                "WHERE d.superseded_by IS NULL AND f.id IS NULL "
                "AND d.namespace IN (:namespace, :shared) ORDER BY d.id DESC"
            ),
            {"namespace": namespace, "shared": SHARED_NAMESPACE},
        )
        return [dict(row._mapping) for row in result]

    async def conflicts(
        self, session: AsyncSession, namespace: str
    ) -> list[dict[str, object]]:
        namespace = normalize_namespace(namespace)
        result = await session.execute(
            text(
                "SELECT c.id, c.decision_key, c.existing_decision_id, d.decision AS existing_value, "
                "c.proposed_value, c.proposed_rationale, c.proposed_project, c.proposed_source "
                "FROM memory_conflicts c JOIN decisions d ON d.id = c.existing_decision_id "
                "LEFT JOIN decision_forgets f ON f.decision_id = d.id "
                "WHERE c.resolution_decision_id IS NULL AND f.id IS NULL "
                "AND d.namespace IN (:namespace, :shared) ORDER BY c.id DESC"
            ),
            {"namespace": namespace, "shared": SHARED_NAMESPACE},
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
        namespace_result = await session.execute(
            text("SELECT namespace FROM decisions WHERE id = :id"),
            {"id": conflict["existing_decision_id"]},
        )
        namespace = normalize_namespace(str(namespace_result.scalar_one()))
        new_id = await self.append_decision(session, replacement, namespace)
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
        namespace: str = SHARED_NAMESPACE,
    ) -> None:
        namespace = normalize_namespace(namespace)
        metadata = {**metadata, "namespace": namespace}
        embedding = vector_literal(self.embedder.embed(content))
        await session.execute(
            text(
                "INSERT INTO memory_chunks "
                "(content, source_type, source_id, metadata, embedding, namespace) "
                "VALUES (:content, :source_type, :source_id, CAST(:metadata AS jsonb), "
                "CAST(:embedding AS vector), :namespace)"
            ),
            {
                "content": content,
                "source_type": source_type,
                "source_id": source_id,
            "metadata": json.dumps(metadata),
            "embedding": embedding,
            "namespace": namespace,
        },
    )

    async def recall(
        self, session: AsyncSession, query: str, namespace: str, limit: int = 5
    ) -> list[Recall]:
        namespace = normalize_namespace(namespace)
        embedding = vector_literal(self.embedder.embed(query))
        result = await session.execute(
            text(
                "SELECT content, source_type, 1 - "
                "(embedding <=> CAST(:embedding AS vector)) AS similarity "
                "FROM memory_chunks "
                "WHERE namespace IN (:namespace, :shared) "
                "ORDER BY embedding <=> CAST(:embedding AS vector) "
                "LIMIT CAST(:limit AS integer)"
            ),
        {"embedding": embedding, "namespace": namespace, "shared": SHARED_NAMESPACE, "limit": limit},
        )
        return [
            Recall(str(row.content), str(row.source_type), float(row.similarity))
            for row in result
        ]

    async def supersede(
        self, session: AsyncSession, old_id: int, replacement: ExtractedMemory,
        namespace: str = SHARED_NAMESPACE,
    ) -> int:
        new_id = await self.append_decision(session, replacement, namespace)
        await session.execute(
            text("UPDATE decisions SET superseded_by = :new_id WHERE id = :old_id"),
            {"new_id": new_id, "old_id": old_id},
        )
        return new_id
