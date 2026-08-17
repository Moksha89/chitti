from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .memory import normalize_namespace
from .namespaces import SHARED_NAMESPACE
from .runner_access import application_only_sql


class ResearchFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=2000)
    source_url: HttpUrl
    retrieved_at: datetime
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    notes: str = Field(default="", max_length=2000)


class ResearchPackageDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture: list[ResearchFact] = Field(min_length=1)
    teams: list[ResearchFact] = Field(min_length=1)
    squads: list[ResearchFact] = Field(default_factory=list)
    kit_colours: list[ResearchFact] = Field(default_factory=list)
    design_references: list[ResearchFact] = Field(default_factory=list)


class ResearchPackageSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    retrieved_at: datetime
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def canonical_package(
    facts: ResearchPackageDocument,
    sources: list[ResearchPackageSource],
) -> str:
    return json.dumps(
        {
            "facts": facts.model_dump(mode="json"),
            "sources": [source.model_dump(mode="json") for source in sources],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class ResearchPackage:
    id: int
    namespace: str
    title: str
    facts: ResearchPackageDocument
    sources: tuple[ResearchPackageSource, ...]
    content_digest: str
    created_at: datetime
    approved_at: datetime | None
    approved_by: str | None

    @property
    def approved(self) -> bool:
        return self.approved_at is not None


async def create_package(
    session: AsyncSession,
    title: str,
    facts: ResearchPackageDocument,
    sources: list[ResearchPackageSource],
    namespace: str = SHARED_NAMESPACE,
) -> int:
    namespace = normalize_namespace(namespace)
    canonical = canonical_package(facts, sources)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    result = await session.execute(
        application_only_sql(text(
            "INSERT INTO research_packages "
            "(namespace, title, facts, sources, content_digest, created_at) "
            "VALUES (:namespace, :title, CAST(:facts AS json), CAST(:sources AS json), "
            ":digest, :created_at) RETURNING id"
        )),
        {
            "namespace": namespace,
            "title": title,
            "facts": facts.model_dump_json(),
            "sources": json.dumps(
                [source.model_dump(mode="json") for source in sources]
            ),
            "digest": digest,
            "created_at": datetime.now(UTC),
        },
    )
    return int(result.scalar_one())


async def approve_package(
    session: AsyncSession, package_id: int, actor: str
) -> None:
    result = await session.execute(
        application_only_sql(text(
            "UPDATE research_packages SET approved_at = COALESCE(approved_at, :now), "
            "approved_by = COALESCE(approved_by, :actor) WHERE id = :id "
            "RETURNING id"
        )),
        {"id": package_id, "actor": actor, "now": datetime.now(UTC)},
    )
    if result.scalar_one_or_none() is None:
        raise ValueError("research package not found")


async def package_by_id(
    session: AsyncSession, package_id: int, namespace: str
) -> ResearchPackage | None:
    result = await session.execute(
        application_only_sql(text(
            "SELECT id, namespace, title, facts, sources, content_digest, created_at, "
            "approved_at, approved_by FROM research_packages "
            "WHERE id = :id AND namespace = :namespace"
        )),
        {"id": package_id, "namespace": normalize_namespace(namespace)},
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    facts = ResearchPackageDocument.model_validate(row["facts"])
    sources = tuple(
        ResearchPackageSource.model_validate(item)
        for item in row["sources"]
    )
    if hashlib.sha256(
        canonical_package(facts, list(sources)).encode()
    ).hexdigest() != row["content_digest"]:
        raise ValueError("research package digest verification failed")
    return ResearchPackage(
        id=int(row["id"]),
        namespace=str(row["namespace"]),
        title=str(row["title"]),
        facts=facts,
        sources=sources,
        content_digest=str(row["content_digest"]),
        created_at=row["created_at"],
        approved_at=row["approved_at"],
        approved_by=row["approved_by"],
    )
