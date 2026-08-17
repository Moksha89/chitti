from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import httpx
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
    snapshot_id: int | None = Field(default=None, ge=1)
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
    snapshot_id: int | None = Field(default=None, ge=1)


@dataclass(frozen=True)
class ResearchSnapshot:
    id: int
    url: str
    retrieved_at: datetime
    content_digest: str
    content: bytes


@dataclass(frozen=True)
class DiscoveredSource:
    url: str
    title: str
    snippet: str


async def fetch_source(
    session: AsyncSession,
    url: str,
    *,
    timeout_seconds: float = 30.0,
) -> int:
    retrieved_at = datetime.now(UTC)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout_seconds,
        headers={"User-Agent": "ChittiResearch/1.0"},
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        content = bytes(response.content)
        final_url = str(response.url)
    digest = hashlib.sha256(content).hexdigest()
    result = await session.execute(
        application_only_sql(text(
            "INSERT INTO research_source_snapshots "
            "(url, retrieved_at, content_digest, content) "
            "VALUES (:url, :retrieved_at, :digest, :content) "
            "RETURNING id"
        )),
        {
            "url": final_url,
            "retrieved_at": retrieved_at,
            "digest": digest,
            "content": content,
        },
    )
    return int(result.scalar_one())


async def snapshot_by_id(
    session: AsyncSession, snapshot_id: int
) -> ResearchSnapshot | None:
    result = await session.execute(
        application_only_sql(text(
            "SELECT id, url, retrieved_at, content_digest, content "
            "FROM research_source_snapshots WHERE id = :id"
        )),
        {"id": snapshot_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    content = bytes(row["content"])
    digest = hashlib.sha256(content).hexdigest()
    if digest != row["content_digest"]:
        raise ValueError("research source snapshot digest verification failed")
    return ResearchSnapshot(
        id=int(row["id"]),
        url=str(row["url"]),
        retrieved_at=row["retrieved_at"],
        content_digest=str(row["content_digest"]),
        content=content,
    )


async def discover_sources(
    urls: list[str],
    *,
    brave_api_key: str | None = None,
    openrouter_api_key: str | None = None,
    query: str | None = None,
) -> list[DiscoveredSource]:
    if brave_api_key:
        if not query:
            raise ValueError("query is required when using Brave Search")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": 10},
                headers={"X-Subscription-Token": brave_api_key},
            )
            response.raise_for_status()
            payload = response.json()
        return [
            DiscoveredSource(
                url=str(item["url"]),
                title=str(item.get("title", "")),
                snippet=str(item.get("description", "")),
            )
            for item in payload.get("web", {}).get("results", [])
            if item.get("url")
        ]
    if openrouter_api_key:
        if not query:
            raise ValueError("query is required when using OpenRouter Search")
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openrouter_api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://69-197-164-208.sslip.io",
                    "X-Title": "Chitti source discovery",
                },
                json={
                    "model": "perplexity/sonar-pro-search",
                    "messages": [{
                        "role": "user",
                        "content": (
                            "Find authoritative sources for this query. Return a "
                            "brief answer with citations: " + query
                        ),
                    }],
                    "max_tokens": 1200,
                },
            )
            response.raise_for_status()
            payload = response.json()
        annotations = payload.get("choices", [{}])[0].get(
            "message", {}
        ).get("annotations", [])
        citations = [
            annotation["url_citation"]["url"]
            for annotation in annotations
            if annotation.get("type") == "url_citation"
            and annotation.get("url_citation", {}).get("url")
        ]
        return [
            DiscoveredSource(url=str(url), title="", snippet="")
            for url in citations
            if url
        ]
    if not urls:
        return [
            DiscoveredSource(url=url, title="", snippet="")
            for url in urls
        ]
    return [
        DiscoveredSource(url=url, title="", snippet="")
        for url in urls
    ]


async def fact_from_snapshot(
    session: AsyncSession,
    snapshot_id: int,
    *,
    key: str,
    value: str,
    notes: str = "",
) -> ResearchFact:
    snapshot = await snapshot_by_id(session, snapshot_id)
    if snapshot is None:
        raise ValueError("research source snapshot not found")
    source_text = snapshot.content.decode("utf-8", errors="replace").casefold()
    if value.casefold() not in source_text:
        raise ValueError(
            "research fact value is not present in its fetched source snapshot"
        )
    return ResearchFact(
        key=key,
        value=value,
        source_url=cast(HttpUrl, snapshot.url),
        retrieved_at=snapshot.retrieved_at,
        content_digest=snapshot.content_digest,
        snapshot_id=snapshot.id,
        notes=notes,
    )


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
    source_by_id: dict[int, ResearchPackageSource] = {
        int(source.snapshot_id): source
        for source in sources
        if source.snapshot_id is not None
    }
    all_facts = [
        fact
        for group in (
            facts.fixture,
            facts.teams,
            facts.squads,
            facts.kit_colours,
            facts.design_references,
        )
        for fact in group
    ]
    if any(fact.snapshot_id is None for fact in all_facts):
        raise ValueError("every research fact must cite a fetched source snapshot")
    if any(source.snapshot_id is None for source in sources):
        raise ValueError("every research source must cite a fetched source snapshot")
    for fact in all_facts:
        snapshot_id = fact.snapshot_id
        assert snapshot_id is not None
        source = source_by_id.get(int(snapshot_id))
        if source is None or (
            str(fact.source_url) != str(source.url)
            or fact.retrieved_at != source.retrieved_at
            or fact.content_digest != source.content_digest
        ):
            raise ValueError("research fact provenance does not match its snapshot")
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
