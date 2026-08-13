import json
import logging
import re
from dataclasses import dataclass
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .embedding import EmbedderProtocol, vector_literal
from .namespaces import MEMORY_NAMESPACES, SHARED_NAMESPACE
from .provider import ExtractedMemory
from .runner_access import application_only_sql

logger = logging.getLogger(__name__)
_PROPOSAL_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "in",
        "is",
        "keep",
        "must",
        "of",
        "on",
        "per",
        "the",
        "to",
        "use",
        "with",
    }
)


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


def _proposal_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", normalize(value))
        if token not in _PROPOSAL_STOP_WORDS
    }


def proposal_fingerprint(value: str) -> str:
    return " ".join(sorted(_proposal_tokens(value)))


def equivalent_proposal(existing: str, proposed: str) -> bool:
    """Conservatively recognize restatements without semantic classification."""
    if normalize(existing) == normalize(proposed):
        return True
    return proposal_fingerprint(existing) == proposal_fingerprint(proposed)


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
        self, session: AsyncSession, item: ExtractedMemory, namespace: str
    ) -> int:
        namespace = normalize_namespace(namespace)
        result = await session.execute(
            application_only_sql(text(
                "INSERT INTO decisions "
                "(project, decision, rationale, source, decision_key, namespace) "
                "VALUES (:project, :decision, :rationale, :source, :key, :namespace) "
                "RETURNING id"
            )),
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
            {
                "key": normalize_key(memory.key),
                "namespace": namespace,
                "shared": SHARED_NAMESPACE,
            },
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
                open_conflicts = await session.execute(
                    text(
                        "SELECT id, proposed_value, proposal_fingerprint FROM memory_conflicts "
                        "WHERE namespace = :namespace AND decision_key = :key "
                        "AND resolution_decision_id IS NULL AND closed_at IS NULL "
                        "AND proposal_fingerprint = :fingerprint "
                        "ORDER BY id DESC"
                    ),
                    {
                        "namespace": namespace,
                        "key": existing_key,
                        "fingerprint": proposal_fingerprint(memory.value),
                    },
                )
                for open_conflict in open_conflicts.mappings():
                    if equivalent_proposal(str(open_conflict["proposed_value"]), memory.value):
                        await session.execute(
                            application_only_sql(text(
                                "UPDATE memory_conflicts SET recurrence_count = recurrence_count + 1, "
                                "last_seen_at = now(), latest_proposed_value = :value, "
                                "latest_proposed_rationale = :rationale, "
                                "latest_proposed_project = :project, "
                                "latest_proposed_source = :source WHERE id = :id"
                            )),
                            {
                                "id": open_conflict["id"],
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
                                int(str(open_conflict["id"])),
                            )
                        )
                        break
                else:
                    result = await session.execute(
                        application_only_sql(text(
                            "INSERT INTO memory_conflicts "
                            "(decision_key, existing_decision_id, proposed_value, proposed_rationale, "
                            "proposed_project, proposed_source, namespace, last_seen_at, "
                            "latest_proposed_value, latest_proposed_rationale, "
                            "latest_proposed_project, latest_proposed_source, proposal_fingerprint) "
                            "VALUES (:key, :existing_id, :value, :rationale, :project, :source, "
                            ":namespace, now(), :value, :rationale, :project, :source, :fingerprint) "
                            "RETURNING id"
                        )),
                        {
                            "key": existing_key,
                            "existing_id": match["id"],
                            "value": memory.value,
                            "rationale": memory.rationale,
                            "project": memory.project,
                            "source": memory.source,
                            "namespace": namespace,
                            "fingerprint": proposal_fingerprint(memory.value),
                        },
                    )
                    new_conflict_id = int(result.scalar_one())
                    conflicts.append(
                        Conflict(
                            existing_key,
                            str(match["decision"]),
                            memory.value,
                            int(str(match["id"])),
                            new_conflict_id,
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
                "COALESCE(c.latest_proposed_value, c.proposed_value) AS proposed_value, "
                "COALESCE(c.latest_proposed_rationale, c.proposed_rationale) AS proposed_rationale, "
                "COALESCE(c.latest_proposed_project, c.proposed_project) AS proposed_project, "
                "COALESCE(c.latest_proposed_source, c.proposed_source) AS proposed_source, "
                "c.recurrence_count, c.closed_at, c.closure_reason, c.resolution_actor, c.resolved_at "
                "FROM memory_conflicts c JOIN decisions d ON d.id = c.existing_decision_id "
                "LEFT JOIN decision_forgets f ON f.decision_id = d.id "
                "WHERE c.resolution_decision_id IS NULL AND c.closed_at IS NULL AND f.id IS NULL "
                "AND c.namespace IN (:namespace, :shared) "
                "AND d.namespace IN (:namespace, :shared) ORDER BY c.id DESC"
            ),
            {"namespace": namespace, "shared": SHARED_NAMESPACE},
        )
        return [dict(row._mapping) for row in result]

    @staticmethod
    def group_conflicts(
        conflicts: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        grouped: dict[tuple[str, int], dict[str, object]] = {}
        for conflict in conflicts:
            key = (
                str(conflict["decision_key"]),
                int(str(conflict["existing_decision_id"])),
            )
            group = grouped.setdefault(
                key,
                {
                    "id": int(str(conflict["id"])),
                    "decision_key": conflict["decision_key"],
                    "existing_decision_id": int(str(conflict["existing_decision_id"])),
                    "existing_value": conflict["existing_value"],
                    "recurrence_count": 0,
                    "proposals": [],
                },
            )
            group["recurrence_count"] = max(
                int(str(group["recurrence_count"])),
                int(str(conflict["recurrence_count"])),
            )
            proposals = cast(list[dict[str, object]], group["proposals"])
            proposals.append(conflict)
        return list(grouped.values())

    async def resolve_conflict(
        self,
        session: AsyncSession,
        conflict_id: int,
        choice: str,
        actor: str | None = None,
    ) -> int:
        result = await session.execute(
            text(
                "SELECT decision_key, existing_decision_id, "
                "COALESCE(latest_proposed_value, proposed_value) AS proposed_value, "
                "COALESCE(latest_proposed_rationale, proposed_rationale) AS proposed_rationale, "
                "COALESCE(latest_proposed_project, proposed_project) AS proposed_project, "
                "COALESCE(latest_proposed_source, proposed_source) AS proposed_source "
                "FROM memory_conflicts "
                "WHERE id = :id AND resolution_decision_id IS NULL AND closed_at IS NULL"
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
            application_only_sql(text("UPDATE decisions SET superseded_by = :new_id WHERE id = :old_id")),
            {"new_id": new_id, "old_id": conflict["existing_decision_id"]},
        )
        await session.execute(
            application_only_sql(text(
                "UPDATE memory_conflicts SET resolution_decision_id = :new_id, "
                "closed_at = now(), closure_reason = 'owner', "
                "resolution_actor = :actor, resolved_at = now() WHERE id = :id"
            )),
            {"new_id": new_id, "id": conflict_id, "actor": actor},
        )
        if choice == "existing":
            await session.execute(
                application_only_sql(text(
                    "UPDATE memory_conflicts SET resolution_decision_id = :new_id, "
                    "closed_at = now(), closure_reason = 'declined', "
                    "resolution_actor = :actor, resolved_at = now() "
                    "WHERE existing_decision_id = :old_id AND id <> :id "
                    "AND resolution_decision_id IS NULL AND closed_at IS NULL"
                )),
                {
                    "new_id": new_id,
                    "old_id": conflict["existing_decision_id"],
                    "id": conflict_id,
                    "actor": actor,
                },
            )
            return new_id
        siblings = await session.execute(
            text(
                "SELECT id, proposed_value, latest_proposed_value, "
                "COALESCE(latest_proposed_value, proposed_value) AS effective_value "
                "FROM memory_conflicts "
                "WHERE existing_decision_id = :old_id "
                "AND id <> :id AND resolution_decision_id IS NULL AND closed_at IS NULL"
            ),
            {"old_id": conflict["existing_decision_id"], "id": conflict_id},
        )
        for sibling in siblings.mappings():
            if equivalent_proposal(str(sibling["effective_value"]), replacement.value):
                await session.execute(
                    application_only_sql(text(
                        "UPDATE memory_conflicts SET resolution_decision_id = :new_id, "
                        "closed_at = now(), closure_reason = 'owner_reconciled', "
                        "resolution_actor = :actor, resolved_at = now() WHERE id = :id"
                    )),
                    {"new_id": new_id, "id": sibling["id"], "actor": actor},
                )
            else:
                await session.execute(
                    application_only_sql(text(
                        "UPDATE memory_conflicts SET existing_decision_id = :new_id "
                        "WHERE id = :id"
                    )),
                    {"new_id": new_id, "id": sibling["id"]},
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
            application_only_sql(text("INSERT INTO decision_forgets (decision_id) VALUES (:id)")),
            {"id": decision_id},
        )

    async def add_chunk(
        self,
        session: AsyncSession,
        content: str,
        source_type: str,
        source_id: str | None,
        metadata: dict[str, object],
        namespace: str,
    ) -> None:
        namespace = normalize_namespace(namespace)
        metadata = {**metadata, "namespace": namespace}
        embedding = vector_literal(self.embedder.embed(content))
        await session.execute(
                application_only_sql(text(
                    "INSERT INTO memory_chunks "
                "(content, source_type, source_id, metadata, embedding, namespace) "
                "VALUES (:content, :source_type, :source_id, CAST(:metadata AS jsonb), "
                "CAST(:embedding AS vector), :namespace)"
                )),
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
            {
                "embedding": embedding,
                "namespace": namespace,
                "shared": SHARED_NAMESPACE,
                "limit": limit,
            },
        )
        return [
            Recall(str(row.content), str(row.source_type), float(row.similarity))
            for row in result
        ]

    async def supersede(
        self, session: AsyncSession, old_id: int, replacement: ExtractedMemory,
        namespace: str,
    ) -> int:
        new_id = await self.append_decision(session, replacement, namespace)
        await session.execute(
            application_only_sql(text("UPDATE decisions SET superseded_by = :new_id WHERE id = :old_id")),
            {"new_id": new_id, "old_id": old_id},
        )
        return new_id
