from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .brand_profiles import get_brand_profile
from .job_types import (
    WEBSITE_JOB,
    config_json,
    normalize_job_type,
    poster_config,
)
from .memory import normalize_namespace
from .namespaces import SHARED_NAMESPACE
from .runner_access import application_only_sql, runner_sql

if TYPE_CHECKING:
    from .db import Database
    from .memory import MemoryStore
    from .provider import ModelProvider


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
    brief: str
    revision: int
    document: PlanDocument
    content_hash: str
    created_at: datetime
    parent_revision_id: int | None
    namespace: str = SHARED_NAMESPACE
    job_type: str = WEBSITE_JOB
    job_config: dict[str, object] | None = None


@dataclass(frozen=True)
class PlanApproval:
    id: int
    revision_id: int
    decision: str
    reason: str | None
    content_hash: str
    created_at: datetime


def _document_from_row(value: object) -> PlanDocument:
    if isinstance(value, dict):
        return PlanDocument.model_validate(value)
    return PlanDocument.model_validate(json.loads(str(value)))


async def create_revision(
    session: AsyncSession,
    project: str,
    brief: str,
    document: PlanDocument,
    parent_revision_id: int | None = None,
    namespace: str = SHARED_NAMESPACE,
    job_type: str = WEBSITE_JOB,
    job_config: object | None = None,
) -> int:
    namespace = normalize_namespace(namespace)
    job_type = normalize_job_type(job_type)
    normalized_config = poster_config(job_config) if job_type == "poster" else {}
    content = document.model_dump(mode="json")
    digest = plan_hash(document)
    revision_result = await session.execute(
        application_only_sql(text(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM plan_revisions "
            "WHERE project = :project"
        )),
        {"project": project},
    )
    revision = int(revision_result.scalar_one())
    result = await session.execute(
        application_only_sql(text(
            "INSERT INTO plan_revisions "
            "(project, namespace, brief, revision, content, content_hash, parent_revision_id, "
            "job_type, job_config) "
            "VALUES (:project, :namespace, :brief, :revision, CAST(:content AS jsonb), "
            ":content_hash, :parent_revision_id, :job_type, CAST(:job_config AS jsonb)) "
            "RETURNING id"
        )),
        {
            "project": project,
            "namespace": namespace,
            "brief": brief,
            "revision": revision,
            "content": json.dumps(content),
            "content_hash": digest,
            "parent_revision_id": parent_revision_id,
            "job_type": job_type,
            "job_config": config_json(normalized_config),
        },
    )
    revision_id = int(result.scalar_one())
    for task in document.tasks:
        await session.execute(
            application_only_sql(text(
                "INSERT INTO plan_task_events "
                "(revision_id, task_id, event_type, status, detail) "
                "VALUES (:revision_id, :task_id, 'created', 'queued', :detail)"
            )),
            {
                "revision_id": revision_id,
                "task_id": task.id,
                "detail": task.done_condition,
            },
        )
    return revision_id


class PlanManager:
    def __init__(
        self, database: Database, provider: ModelProvider, memory: MemoryStore
    ) -> None:
        self.database = database
        self.provider = provider
        self.memory = memory
        self._jobs: set[asyncio.Task[None]] = set()

    async def enqueue(
        self,
        project: str,
        brief: str,
        parent_revision_id: int | None = None,
        rejection: str | None = None,
        namespace: str = SHARED_NAMESPACE,
        job_type: str = WEBSITE_JOB,
        job_config: object | None = None,
    ) -> int:
        namespace = normalize_namespace(namespace)
        job_type = normalize_job_type(job_type)
        normalized_config = poster_config(job_config) if job_type == "poster" else {}
        if job_type == "poster":
            async with self.database.sessions() as session:
                if await get_brand_profile(session, namespace) is None:
                    raise ValueError(
                        "poster plan refused: namespace "
                        f"'{namespace}' has no brand profile yet"
                    )
        async with self.database.sessions() as session:
            result = await session.execute(
                application_only_sql(text(
                    "INSERT INTO plan_jobs "
                    "(project, namespace, brief, parent_revision_id, rejection, job_type, job_config) "
                    "VALUES (:project, :namespace, :brief, :parent, :rejection, "
                    ":job_type, CAST(:job_config AS jsonb)) RETURNING id"
                )),
                {
                    "project": project,
                    "namespace": namespace,
                    "brief": brief,
                    "parent": parent_revision_id,
                    "rejection": rejection,
                    "job_type": job_type,
                    "job_config": config_json(normalized_config),
                },
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
                application_only_sql(text("SELECT id FROM plan_jobs WHERE status IN ('queued', 'running')"))
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
                    application_only_sql(text(
                        "SELECT project, namespace, brief, parent_revision_id, rejection, "
                        "job_type, job_config "
                        "FROM plan_jobs WHERE id = :id"
                    )),
                    {"id": job_id},
                )
            ).mappings().one_or_none()
            if job is None:
                return
            await session.execute(
                application_only_sql(text("UPDATE plan_jobs SET status = 'running', error = NULL WHERE id = :id")),
                {"id": job_id},
            )
            await session.commit()
        try:
            async with self.database.sessions() as session:
                beliefs = await self.memory.active_beliefs(session, str(job["namespace"]))
                profile = None
                if str(job["job_type"]) == "poster":
                    profile = await get_brand_profile(session, str(job["namespace"]))
            planner_feedback = str(job["rejection"]) if job["rejection"] else None
            document: PlanDocument | None = None
            for attempt in range(2):
                raw = await self.provider.plan(
                    str(job["brief"]),
                    str(job["project"]),
                    beliefs,
                    planner_feedback,
                    str(job["job_type"]),
                    job["job_config"],
                    profile,
                )
                match = re.search(r"\{[\s\S]*\}", raw)
                if not match:
                    raise ValueError("planner returned no JSON object")
                try:
                    document = PlanDocument.model_validate(
                        json.loads(match.group(0))
                    )
                except ValidationError as exc:
                    if attempt == 1:
                        raise
                    planner_feedback = (
                        (f"{planner_feedback}\n\n" if planner_feedback else "")
                        + "The previous planner response failed PlanDocument "
                        "validation. Correct these errors in the next strict JSON "
                        "response:\n"
                        + str(exc)
                    )
            if document is None:
                raise RuntimeError("planner produced no document")
            async with self.database.sessions() as session:
                revision_id = await create_revision(
                    session,
                    str(job["project"]),
                    str(job["brief"]),
                    document,
                    int(job["parent_revision_id"]) if job["parent_revision_id"] else None,
                    str(job["namespace"]),
                    str(job["job_type"]),
                    job["job_config"],
                )
                await session.execute(
                application_only_sql(text("UPDATE plan_jobs SET status = 'complete', revision_id = :revision WHERE id = :id")),
                    {"revision": revision_id, "id": job_id},
                )
                await session.commit()
        except Exception as exc:
            async with self.database.sessions() as session:
                await session.execute(
                application_only_sql(text("UPDATE plan_jobs SET status = 'failed', error = :error WHERE id = :id")),
                    {"error": str(exc)[:2000], "id": job_id},
                )
                await session.commit()

    async def job(self, job_id: int) -> dict[str, object] | None:
        async with self.database.sessions() as session:
            result = await session.execute(
                application_only_sql(text(
                    "SELECT id, project, namespace, brief, status, error, revision_id, "
                    "job_type, job_config, created_at "
                    "FROM plan_jobs WHERE id = :id"
                )),
                {"id": job_id},
            )
            row = result.mappings().one_or_none()
            return dict(row) if row else None


async def latest_revisions(
    session: AsyncSession, namespace: str = SHARED_NAMESPACE
) -> list[dict[str, object]]:
    namespace = normalize_namespace(namespace)
    result = await session.execute(
        application_only_sql(text(
            "SELECT DISTINCT ON (project) id, project, namespace, revision, content, "
            "content_hash, created_at, parent_revision_id, job_type, job_config FROM plan_revisions "
            "WHERE namespace IN (:namespace, :shared) "
            "ORDER BY project, namespace = :namespace DESC, revision DESC"
        )),
        {"namespace": namespace, "shared": SHARED_NAMESPACE},
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
            application_only_sql(text(
                "SELECT task_id, status FROM plan_task_events "
                "WHERE revision_id = :revision ORDER BY id"
            )),
            {"revision": plan["id"]},
        )
        plan["task_statuses"] = task_status_projection(
            [dict(row._mapping) for row in events]
        )
    return plans


async def revision_by_id(
    session: AsyncSession, revision_id: int, namespace: str = SHARED_NAMESPACE
) -> PlanRevision | None:
    namespace = normalize_namespace(namespace)
    result = await session.execute(
        runner_sql(text(
            "SELECT id, project, namespace, brief, revision, content, content_hash, "
            "created_at, parent_revision_id, job_type, job_config FROM plan_revisions "
            "WHERE id = :id AND namespace IN (:namespace, :shared)"
        )),
        {"id": revision_id, "namespace": namespace, "shared": SHARED_NAMESPACE},
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None
    return PlanRevision(
        id=int(row["id"]),
        project=str(row["project"]),
        namespace=str(row["namespace"]),
        brief=str(row["brief"]),
        revision=int(row["revision"]),
        document=_document_from_row(row["content"]),
        content_hash=str(row["content_hash"]),
        created_at=row["created_at"],
        parent_revision_id=int(row["parent_revision_id"]) if row["parent_revision_id"] else None,
        job_type=normalize_job_type(row["job_type"]),
        job_config=dict(row["job_config"]) if isinstance(row["job_config"], dict) else json.loads(str(row["job_config"])),
    )


async def approve_revision(
    session: AsyncSession,
    revision_id: int,
    reason: str | None = None,
    namespace: str = SHARED_NAMESPACE,
) -> PlanApproval:
    revision = await revision_by_id(session, revision_id, namespace)
    if revision is None:
        raise ValueError("plan revision not found")
    prior = await session.execute(
        application_only_sql(text("SELECT 1 FROM plan_approvals WHERE revision_id = :revision LIMIT 1")),
        {"revision": revision_id},
    )
    if prior.scalar_one_or_none() is not None:
        raise ValueError("plan revision already has an approval decision")
    result = await session.execute(
        application_only_sql(text(
            "INSERT INTO plan_approvals "
            "(revision_id, decision, reason, content_hash) "
            "VALUES (:revision_id, 'approved', :reason, :content_hash) "
            "RETURNING id, created_at"
        )),
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
    session: AsyncSession,
    revision_id: int,
    reason: str,
    namespace: str = SHARED_NAMESPACE,
) -> PlanApproval:
    if not reason.strip():
        raise ValueError("a rejection reason is required")
    revision = await revision_by_id(session, revision_id, namespace)
    if revision is None:
        raise ValueError("plan revision not found")
    prior = await session.execute(
        application_only_sql(text("SELECT 1 FROM plan_approvals WHERE revision_id = :revision LIMIT 1")),
        {"revision": revision_id},
    )
    if prior.scalar_one_or_none() is not None:
        raise ValueError("plan revision already has an approval decision")
    result = await session.execute(
        application_only_sql(text(
            "INSERT INTO plan_approvals "
            "(revision_id, decision, reason, content_hash) "
            "VALUES (:revision_id, 'rejected', :reason, :content_hash) "
            "RETURNING id, created_at"
        )),
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


def task_status_projection(events: Iterable[Mapping[str, object]]) -> dict[str, str]:
    projection: dict[str, str] = {}
    for event in events:
        projection[str(event["task_id"])] = str(event["status"])
    return projection
