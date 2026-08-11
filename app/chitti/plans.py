from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class PlanTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=2000)
    dependencies: list[str] = Field(default_factory=list)
    done_condition: str = Field(min_length=1, max_length=1000)


class MemoryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_key: str = Field(min_length=1, max_length=255)
    influence: str = Field(min_length=1, max_length=1000)


class PlanDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=4000)
    tasks: list[PlanTask] = Field(min_length=1, max_length=100)
    memory_decisions: list[MemoryDecision] = Field(default_factory=list)

    @field_validator("tasks")
    @classmethod
    def validate_dependencies(cls, tasks: list[PlanTask]) -> list[PlanTask]:
        ids = [task.id for task in tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("plan task ids must be unique")
        known = set(ids)
        for task in tasks:
            missing = set(task.dependencies) - known
            if missing:
                raise ValueError(
                    f"task {task.id!r} references missing dependencies: {sorted(missing)}"
                )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("plan task dependencies contain a cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            task = next(item for item in tasks if item.id == task_id)
            for dependency in task.dependencies:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in ids:
            visit(task_id)
        return tasks


def canonical_plan(document: PlanDocument) -> str:
    return json.dumps(document.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def plan_hash(document: PlanDocument) -> str:
    return hashlib.sha256(canonical_plan(document).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanRevision:
    id: int
    project: str
    revision: int
    document: PlanDocument
    content_hash: str
    created_at: datetime
    parent_revision_id: int | None


@dataclass(frozen=True)
class PlanApproval:
    id: int
    revision_id: int
    decision: str
    reason: str | None
    content_hash: str
    created_at: datetime


def _document_from_row(value: Any) -> PlanDocument:
    return PlanDocument.model_validate(value if isinstance(value, dict) else json.loads(value))


async def create_revision(
    session: AsyncSession,
    project: str,
    document: PlanDocument,
    parent_revision_id: int | None = None,
) -> int:
    content = document.model_dump(mode="json")
    digest = plan_hash(document)
    revision_result = await session.execute(
        text(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM plan_revisions "
            "WHERE project = :project"
        ),
        {"project": project},
    )
    revision = int(revision_result.scalar_one())
    result = await session.execute(
        text(
            "INSERT INTO plan_revisions "
            "(project, revision, content, content_hash, parent_revision_id) "
            "VALUES (:project, :revision, CAST(:content AS jsonb), :content_hash, "
            ":parent_revision_id) RETURNING id"
        ),
        {
            "project": project,
            "revision": revision,
            "content": json.dumps(content),
            "content_hash": digest,
            "parent_revision_id": parent_revision_id,
        },
    )
    revision_id = int(result.scalar_one())
    for task in document.tasks:
        await session.execute(
            text(
                "INSERT INTO plan_task_events "
                "(revision_id, task_id, event_type, status, detail) "
                "VALUES (:revision_id, :task_id, 'created', 'queued', :detail)"
            ),
            {
                "revision_id": revision_id,
                "task_id": task.id,
                "detail": task.done_condition,
            },
        )
    return revision_id


class PlanManager:
    def __init__(self, database: Any, provider: Any, memory: Any) -> None:
        self.database = database
        self.provider = provider
        self.memory = memory
        self._jobs: set[asyncio.Task[None]] = set()

    async def enqueue(
        self, project: str, brief: str, parent_revision_id: int | None = None
    ) -> int:
        async with self.database.sessions() as session:
            result = await session.execute(
                text(
                    "INSERT INTO plan_jobs (project, brief, parent_revision_id) "
                    "VALUES (:project, :brief, :parent) RETURNING id"
                ),
                {"project": project, "brief": brief, "parent": parent_revision_id},
            )
            job_id = int(result.scalar_one())
            await session.commit()
        task = asyncio.create_task(self._run(job_id))
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)
        return job_id

    async def resume_queued(self) -> None:
        async with self.database.sessions() as session:
            result = await session.execute(
                text("SELECT id FROM plan_jobs WHERE status IN ('queued', 'running')")
            )
            job_ids = [int(row.id) for row in result]
        for job_id in job_ids:
            task = asyncio.create_task(self._run(job_id))
            self._jobs.add(task)
            task.add_done_callback(self._jobs.discard)

    async def _run(self, job_id: int) -> None:
        async with self.database.sessions() as session:
            job = (
                await session.execute(
                    text(
                        "SELECT project, brief, parent_revision_id FROM plan_jobs WHERE id = :id"
                    ),
                    {"id": job_id},
                )
            ).mappings().one_or_none()
            if job is None:
                return
            await session.execute(
                text("UPDATE plan_jobs SET status = 'running', error = NULL WHERE id = :id"),
                {"id": job_id},
            )
            await session.commit()
        try:
            async with self.database.sessions() as session:
                beliefs = await self.memory.active_beliefs(session)
            raw = await self.provider.plan(str(job["brief"]), str(job["project"]), beliefs)
            match = re.search(r"\{[\s\S]*\}", raw)
            if not match:
                raise ValueError("planner returned no JSON object")
            document = PlanDocument.model_validate(json.loads(match.group(0)))
            async with self.database.sessions() as session:
                revision_id = await create_revision(
                    session,
                    str(job["project"]),
                    document,
                    int(job["parent_revision_id"]) if job["parent_revision_id"] else None,
                )
                await session.execute(
                    text("UPDATE plan_jobs SET status = 'complete', revision_id = :revision WHERE id = :id"),
                    {"revision": revision_id, "id": job_id},
                )
                await session.commit()
        except Exception as exc:
            async with self.database.sessions() as session:
                await session.execute(
                    text("UPDATE plan_jobs SET status = 'failed', error = :error WHERE id = :id"),
                    {"error": str(exc)[:2000], "id": job_id},
                )
                await session.commit()

    async def job(self, job_id: int) -> dict[str, Any] | None:
        async with self.database.sessions() as session:
            result = await session.execute(
                text(
                    "SELECT id, project, brief, status, error, revision_id, created_at "
                    "FROM plan_jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
            row = result.mappings().one_or_none()
            return dict(row) if row else None


async def latest_revisions(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT DISTINCT ON (project) id, project, revision, content, content_hash, "
            "created_at, parent_revision_id FROM plan_revisions ORDER BY project, revision DESC"
        )
    )
    plans = [
        {
            **dict(row._mapping),
            "document": _document_from_row(row.content),
        }
        for row in result
    ]
    for plan in plans:
        events = await session.execute(
            text(
                "SELECT task_id, status FROM plan_task_events "
                "WHERE revision_id = :revision ORDER BY id"
            ),
            {"revision": plan["id"]},
        )
        plan["task_statuses"] = task_status_projection(
            [dict(row._mapping) for row in events]
        )
    return plans


async def revision_by_id(session: AsyncSession, revision_id: int) -> PlanRevision | None:
    result = await session.execute(
        text(
            "SELECT id, project, revision, content, content_hash, created_at, parent_revision_id "
            "FROM plan_revisions WHERE id = :id"
        ),
        {"id": revision_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    return PlanRevision(
        id=int(row["id"]),
        project=str(row["project"]),
        revision=int(row["revision"]),
        document=_document_from_row(row["content"]),
        content_hash=str(row["content_hash"]),
        created_at=row["created_at"],
        parent_revision_id=int(row["parent_revision_id"]) if row["parent_revision_id"] else None,
    )


async def approve_revision(
    session: AsyncSession, revision_id: int, reason: str | None = None
) -> PlanApproval:
    revision = await revision_by_id(session, revision_id)
    if revision is None:
        raise ValueError("plan revision not found")
    prior = await session.execute(
        text("SELECT 1 FROM plan_approvals WHERE revision_id = :revision LIMIT 1"),
        {"revision": revision_id},
    )
    if prior.scalar_one_or_none() is not None:
        raise ValueError("plan revision already has an approval decision")
    result = await session.execute(
        text(
            "INSERT INTO plan_approvals "
            "(revision_id, decision, reason, content_hash) "
            "VALUES (:revision_id, 'approved', :reason, :content_hash) "
            "RETURNING id, created_at"
        ),
        {
            "revision_id": revision_id,
            "reason": reason,
            "content_hash": revision.content_hash,
        },
    )
    row = result.mappings().one()
    return PlanApproval(
        int(row["id"]),
        revision_id,
        "approved",
        reason,
        revision.content_hash,
        row["created_at"],
    )


async def reject_revision(
    session: AsyncSession, revision_id: int, reason: str
) -> PlanApproval:
    if not reason.strip():
        raise ValueError("a rejection reason is required")
    revision = await revision_by_id(session, revision_id)
    if revision is None:
        raise ValueError("plan revision not found")
    prior = await session.execute(
        text("SELECT 1 FROM plan_approvals WHERE revision_id = :revision LIMIT 1"),
        {"revision": revision_id},
    )
    if prior.scalar_one_or_none() is not None:
        raise ValueError("plan revision already has an approval decision")
    result = await session.execute(
        text(
            "INSERT INTO plan_approvals "
            "(revision_id, decision, reason, content_hash) "
            "VALUES (:revision_id, 'rejected', :reason, :content_hash) "
            "RETURNING id, created_at"
        ),
        {
            "revision_id": revision_id,
            "reason": reason.strip(),
            "content_hash": revision.content_hash,
        },
    )
    row = result.mappings().one()
    return PlanApproval(
        int(row["id"]),
        revision_id,
        "rejected",
        reason.strip(),
        revision.content_hash,
        row["created_at"],
    )


def validate_approval_binding(revision: PlanRevision, approval: PlanApproval) -> bool:
    return approval.revision_id == revision.id and approval.content_hash == plan_hash(
        revision.document
    )


def task_status_projection(events: Iterable[dict[str, Any]]) -> dict[str, str]:
    projection: dict[str, str] = {}
    for event in events:
        projection[str(event["task_id"])] = str(event["status"])
    return projection
