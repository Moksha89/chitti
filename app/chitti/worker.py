from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .plans import (
    PlanApproval,
    PlanRevision,
    revision_by_id,
    validate_approval_binding,
)

if TYPE_CHECKING:
    from .db import Database


@dataclass(frozen=True)
class WorkerLimits:
    cpus: float = 1.0
    memory: str = "512m"
    pids: int = 128
    timeout_seconds: int = 300
    nofile: int = 256
    artifact_bytes: int = 50 * 1024 * 1024
    shm_size: str = "64m"

    def as_json(self) -> dict[str, object]:
        return {
            "cpus": self.cpus,
            "memory": self.memory,
            "pids": self.pids,
            "timeout_seconds": self.timeout_seconds,
            "nofile": self.nofile,
            "artifact_bytes": self.artifact_bytes,
            "shm_size": self.shm_size,
            "network_policy": "public_egress_default_bridge",
            "non_root_uid": 65532,
        }


@dataclass(frozen=True)
class FixedOperation:
    task_id: str
    name: str
    command: tuple[str, ...]
    network: str = "none"


class WorkerDispatcher(Protocol):
    async def dispatch(
        self, revision: PlanRevision, run_id: int, limits: WorkerLimits
    ) -> None: ...

    async def cancel(self, run_id: int) -> None: ...


class DockerSandboxDispatcher:
    """Host-side cage controller; the worker container receives no Docker socket."""

    def __init__(
        self,
        database: Database,
        image: str = "chitti-sandbox:latest",
        workspace_root: Path = Path("/var/lib/chitti-worker/runs"),
    ) -> None:
        self.database = database
        self.image = image
        self.workspace_root = workspace_root
        self._containers: dict[int, str] = {}
        self._processes: dict[int, subprocess.Popen[bytes]] = {}
        self._cancelled: set[int] = set()
        self._slot = asyncio.Semaphore(1)

    async def cancel(self, run_id: int) -> None:
        self._cancelled.add(run_id)
        process = self._processes.get(run_id)
        if process and process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
            except TimeoutError:
                pass
        container = self._containers.get(run_id)
        if container:
            await self._remove_container(container)

    async def dispatch(
        self, revision: PlanRevision, run_id: int, limits: WorkerLimits
    ) -> None:
        async with self._slot:
            await self._dispatch_one(revision, run_id, limits)

    async def _dispatch_one(
        self, revision: PlanRevision, run_id: int, limits: WorkerLimits
    ) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = Path(
            tempfile.mkdtemp(prefix=f"chitti-run-{run_id}-", dir=self.workspace_root)
        )
        mounted = False
        try:
            await self._mount_workspace(workspace, limits)
            mounted = True
            await self._event(run_id, "running", "run started")
            for index, operation in enumerate(fixed_operations(revision)):
                if run_id in self._cancelled:
                    await self._event(run_id, "cancelled", "cancelled before operation")
                    return
                await self._event(
                    run_id, "operation_running", operation.name,
                    operation_index=index, task_id=operation.task_id,
                )
                await self._task_event(run_id, operation.task_id, "running", operation.name)
                started = datetime.now(UTC)
                command = self._docker_command(operation, workspace, run_id, limits)
                try:
                    result, stdout, stderr = await self._run_container(
                        run_id, command, limits
                    )
                except TimeoutError:
                    await self.cancel(run_id)
                    await self._operation(
                        run_id, operation, index, "failed", "",
                        "worker exceeded wall-clock timeout", 124, started,
                    )
                    await self._event(run_id, "failed", "worker exceeded wall-clock timeout")
                    return
                if run_id in self._cancelled:
                    await self._event(run_id, "cancelled", "worker stopped by cancellation")
                    return
                if result.returncode == 137:
                    stderr += "\nworker exceeded memory limit"
                status = "passed" if result.returncode == 0 else "failed"
                await self._operation(
                    run_id, operation, index, status, stdout, stderr,
                    result.returncode, started,
                )
                await self._task_event(run_id, operation.task_id, status, operation.name)
                if status == "failed":
                    await self._event(run_id, "failed", f"operation failed: {operation.name}")
                    return
                if _directory_size(workspace) > limits.artifact_bytes:
                    await self._task_event(
                        run_id, operation.task_id, "failed", "artifact quota exceeded"
                    )
                    await self._event(run_id, "failed", "artifact quota exceeded")
                    return
            await self._capture_workspace_artifacts(run_id, workspace, limits)
            await self._event(run_id, "passed", "all fixed operations passed")
        finally:
            if mounted:
                await self._unmount_workspace(workspace)
            shutil.rmtree(workspace, ignore_errors=True)

    async def _mount_workspace(self, workspace: Path, limits: WorkerLimits) -> None:
        await asyncio.to_thread(
            subprocess.run,
            [
                "mount",
                "-t",
                "tmpfs",
                "-o",
                f"size={limits.artifact_bytes},uid=65532,gid=65532,mode=0770",
                "tmpfs",
                str(workspace),
            ],
            check=True,
        )

    async def _unmount_workspace(self, workspace: Path) -> None:
        await asyncio.to_thread(
            subprocess.run, ["umount", str(workspace)], check=False
        )

    async def _run_container(
        self, run_id: int, command: list[str], limits: WorkerLimits
    ) -> tuple[subprocess.CompletedProcess[str], str, str]:
        container = f"chitti-worker-{run_id}"
        self._containers[run_id] = container
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._processes[run_id] = process
        try:
            stdout_task = asyncio.create_task(
                asyncio.to_thread(
                    self._read_limited, process.stdout, max(1, limits.artifact_bytes // 2)
                )
            )
            stderr_task = asyncio.create_task(
                asyncio.to_thread(
                    self._read_limited, process.stderr, max(1, limits.artifact_bytes // 2)
                )
            )
            try:
                output = await asyncio.wait_for(
                    self._collect_outputs(stdout_task, stderr_task),
                    timeout=limits.timeout_seconds,
                )
            except TimeoutError:
                await self.cancel(run_id)
                raise
            (stdout, stdout_exceeded), (stderr, stderr_exceeded) = output
            if stdout_exceeded or stderr_exceeded:
                await self.cancel(run_id)
                return (
                    subprocess.CompletedProcess(command, 125),
                    stdout,
                    stderr + "\nworker output quota exceeded",
                )
            await asyncio.to_thread(process.wait)
            await self._remove_container(container)
            return (
                subprocess.CompletedProcess(command, process.returncode or 0),
                stdout,
                stderr,
            )
        finally:
            self._processes.pop(run_id, None)
            self._containers.pop(run_id, None)

    async def _collect_outputs(
        self,
        stdout_task: asyncio.Task[tuple[str, bool]],
        stderr_task: asyncio.Task[tuple[str, bool]],
    ) -> tuple[tuple[str, bool], tuple[str, bool]]:
        pending: set[asyncio.Task[tuple[str, bool]]] = {
            stdout_task,
            stderr_task,
        }
        results: dict[asyncio.Task[tuple[str, bool]], tuple[str, bool]] = {}
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                result = task.result()
                results[task] = result
                if result[1]:
                    for remaining in pending:
                        remaining.cancel()
                    empty = ("", False)
                    return (
                        results.get(stdout_task, empty),
                        results.get(stderr_task, empty),
                    )
        return results[stdout_task], results[stderr_task]

    async def _remove_container(self, container: str) -> None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    subprocess.run,
                    ["docker", "kill", "--signal", "KILL", container],
                    capture_output=True,
                    check=False,
                ),
                timeout=5,
            )
            await asyncio.wait_for(
                asyncio.to_thread(
                    subprocess.run,
                    ["docker", "rm", "--force", container],
                    capture_output=True,
                    check=False,
                ),
                timeout=5,
            )
        except TimeoutError:
            return

    def _read_limited(
        self, stream: IO[bytes] | None, limit: int
    ) -> tuple[str, bool]:
        if stream is None:
            return "", False
        chunks: list[bytes] = []
        total = 0
        exceeded = False
        while True:
            chunk = stream.read(min(65536, limit + 1))
            if not chunk:
                break
            remaining = limit - total
            if remaining <= 0:
                exceeded = True
                break
            chunks.append(chunk[:remaining])
            total += len(chunk)
            if len(chunk) >= remaining:
                exceeded = True
                break
        return b"".join(chunks).decode("utf-8", errors="replace"), exceeded

    def _docker_command(
        self, operation: FixedOperation, workspace: Path,
        run_id: int, limits: WorkerLimits,
    ) -> list[str]:
        return [
            "docker", "run", "--name", f"chitti-worker-{run_id}",
            "--network", operation.network, "--cpus", str(limits.cpus),
            "--memory", limits.memory, "--pids-limit", str(limits.pids),
            "--ulimit", f"nofile={limits.nofile}:{limits.nofile}",
            "--shm-size", limits.shm_size, "--user", "65532:65532",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "--mount", f"type=bind,src={workspace},dst=/workspace",
            self.image, *operation.command,
        ]

    async def _event(
        self, run_id: int, status: str, detail: str,
        operation_index: int | None = None, task_id: str | None = None,
    ) -> None:
        async with self.database.sessions() as session:
            await session.execute(
                text(
                    "INSERT INTO worker_run_events "
                    "(run_id, status, detail, operation_index, task_id) "
                    "VALUES (:run_id, :status, :detail, :operation_index, :task_id)"
                ),
                {
                    "run_id": run_id, "status": status, "detail": detail,
                    "operation_index": operation_index, "task_id": task_id,
                },
            )
            await session.commit()

    async def _task_event(
        self, run_id: int, task_id: str, status: str, detail: str
    ) -> None:
        async with self.database.sessions() as session:
            await session.execute(
                text(
                    "INSERT INTO plan_task_events "
                    "(revision_id, task_id, event_type, status, detail) "
                    "SELECT revision_id, :task_id, 'worker', :status, :detail "
                    "FROM worker_runs WHERE id = :run_id"
                ),
                {
                    "run_id": run_id, "task_id": task_id,
                    "status": status, "detail": detail,
                },
            )
            await session.commit()

    async def _operation(
        self, run_id: int, operation: FixedOperation, index: int,
        status: str, stdout: str, stderr: str, exit_code: int, started: datetime,
    ) -> None:
        async with self.database.sessions() as session:
            result = await session.execute(
                text(
                    "INSERT INTO worker_operations "
                    "(run_id, task_id, operation_index, name, status, stdout, stderr, "
                    "exit_code, started_at, finished_at) VALUES "
                    "(:run_id, :task_id, :operation_index, :name, :status, :stdout, "
                    ":stderr, :exit_code, :started_at, now()) RETURNING id"
                ),
                {
                    "run_id": run_id, "task_id": operation.task_id,
                    "operation_index": index, "name": operation.name,
                    "status": status, "stdout": stdout, "stderr": stderr,
                    "exit_code": exit_code, "started_at": started,
                },
            )
            operation_id = int(result.scalar_one())
            for kind, content in (("stdout", stdout), ("stderr", stderr)):
                artifact = await session.execute(
                    text(
                        "INSERT INTO worker_artifacts "
                        "(run_id, operation_id, kind, path, sha256, byte_size) "
                        "VALUES (:run_id, :operation_id, :kind, :path, :sha256, "
                        ":size) RETURNING id"
                    ),
                    {
                        "run_id": run_id, "operation_id": operation_id,
                        "kind": kind,
                        "path": f"operations/{index}/{operation.name}/{kind}",
                        "sha256": hashlib.sha256(content.encode()).hexdigest(),
                        "size": len(content.encode()),
                    },
                )
                artifact_id = int(artifact.scalar_one())
                await session.execute(
                    text(
                        "INSERT INTO worker_artifact_payloads "
                        "(artifact_id, content) VALUES (:artifact_id, :content)"
                    ),
                    {"artifact_id": artifact_id, "content": content.encode()},
                )
            await session.commit()

    async def _capture_workspace_artifacts(
        self, run_id: int, workspace: Path, limits: WorkerLimits
    ) -> None:
        for path in workspace.rglob("*"):
            if not path.is_file() or path.stat().st_size > limits.artifact_bytes:
                continue
            content = path.read_bytes()
            kind = "screenshot" if path.suffix == ".png" else "diff"
            async with self.database.sessions() as session:
                artifact = await session.execute(
                    text(
                        "INSERT INTO worker_artifacts "
                        "(run_id, kind, path, sha256, byte_size) "
                        "VALUES (:run_id, :kind, :path, :sha256, :byte_size) "
                        "RETURNING id"
                    ),
                    {
                        "run_id": run_id, "kind": kind,
                        "path": str(path.relative_to(workspace)),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "byte_size": len(content),
                    },
                )
                artifact_id = int(artifact.scalar_one())
                await session.execute(
                    text(
                        "INSERT INTO worker_artifact_payloads "
                        "(artifact_id, content) VALUES (:artifact_id, :content)"
                    ),
                    {"artifact_id": artifact_id, "content": content},
                )
                await session.commit()


class WorkerRunManager:
    def __init__(
        self, database: Database, dispatcher: WorkerDispatcher | None = None
    ) -> None:
        self.database = database
        self.dispatcher = dispatcher
    async def enqueue(self, revision_id: int, limits: WorkerLimits | None = None) -> int:
        chosen = limits or WorkerLimits()
        async with self.database.sessions() as session:
            revision = await approved_revision(session, revision_id)
            result = await session.execute(
                text(
                    "INSERT INTO worker_runs (revision_id, limits, workspace_id) "
                    "VALUES (:revision_id, CAST(:limits AS json), :workspace_id) RETURNING id"
                ),
                {
                    "revision_id": revision.id,
                    "limits": json.dumps(chosen.as_json()),
                    "workspace_id": f"run-{revision.id}",
                },
            )
            run_id = int(result.scalar_one())
            await session.execute(
                text(
                    "INSERT INTO worker_run_events (run_id, status, detail) "
                    "VALUES (:run_id, 'queued', 'awaiting sandbox slot')"
                ),
                {"run_id": run_id},
            )
            await session.commit()
        return run_id

    async def cancel(self, run_id: int) -> None:
        await self._record(run_id, "cancel_requested", "cancellation requested by owner")

    async def _record(self, run_id: int, status: str, detail: str) -> None:
        async with self.database.sessions() as session:
            await session.execute(
                text(
                    "INSERT INTO worker_run_events (run_id, status, detail) "
                    "VALUES (:run_id, :status, :detail)"
                ),
                {"run_id": run_id, "status": status, "detail": detail},
            )
            await session.commit()

    async def detail(self, run_id: int) -> dict[str, object] | None:
        async with self.database.sessions() as session:
            run_result = await session.execute(
                text(
                    "SELECT id, revision_id, limits, workspace_id, created_at "
                    "FROM worker_runs WHERE id = :run_id"
                ),
                {"run_id": run_id},
            )
            run = run_result.mappings().one_or_none()
            if run is None:
                return None
            events = await session.execute(
                text(
                    "SELECT status, detail, operation_index, task_id, created_at "
                    "FROM worker_run_events WHERE run_id = :run_id ORDER BY id"
                ),
                {"run_id": run_id},
            )
            operations = await session.execute(
                text(
                    "SELECT id, task_id, operation_index, name, status, stdout, stderr, "
                    "exit_code, started_at, finished_at FROM worker_operations "
                    "WHERE run_id = :run_id ORDER BY operation_index"
                ),
                {"run_id": run_id},
            )
            artifacts = await session.execute(
                text(
                    "SELECT id, operation_id, kind, path, sha256, byte_size "
                    "FROM worker_artifacts WHERE run_id = :run_id ORDER BY id"
                ),
                {"run_id": run_id},
            )
            return {
                "run": dict(run),
                "events": [dict(row._mapping) for row in events],
                "operations": [dict(row._mapping) for row in operations],
                "artifacts": [dict(row._mapping) for row in artifacts],
            }


def fixed_operations(revision: PlanRevision) -> tuple[FixedOperation, ...]:
    first = revision.document.tasks[0]
    return (
        FixedOperation(
            first.id,
            "git-init",
            (
                "sh",
                "-c",
                "git init -q /workspace",
            ),
        ),
        FixedOperation(
            first.id, "write-fixture", (
                "sh", "-c",
                "printf '%s\\n' '<!doctype html><title>Chitti preview</title>' "
                "> /workspace/index.html",
            ),
        ),
        FixedOperation(
            first.id,
            "install-fixture-dependency",
            (
                "sh",
                "-c",
                "python -m pip install --no-cache-dir --target /workspace/.deps "
                "colorama==0.4.6",
            ),
            network="bridge",
        ),
        FixedOperation(first.id, "browser-preview", (
            "python", "/opt/screenshot.py",
        )),
        FixedOperation(first.id, "run-tests", (
            "python", "-m", "compileall", "-q", "/workspace",
        )),
        FixedOperation(first.id, "git-diff", (
            "sh", "-c", "cd /workspace && git -c safe.directory=/workspace "
            "add -N index.html && git -c safe.directory=/workspace diff "
            "--no-ext-diff > workspace.diff || true",
        )),
    )


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


async def approved_revision(
    session: AsyncSession, revision_id: int
) -> PlanRevision:
    revision = await revision_by_id(session, revision_id)
    if revision is None:
        raise ValueError("plan revision not found")
    result = await session.execute(
        text(
            "SELECT revision_id, content_hash FROM plan_approvals "
            "WHERE revision_id = :revision AND decision = 'approved' "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"revision": revision_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise ValueError("plan revision is not approved")
    approval = PlanApproval(
        id=0,
        revision_id=int(row["revision_id"]),
        decision="approved",
        reason=None,
        content_hash=str(row["content_hash"]),
        created_at=revision.created_at,
    )
    if not validate_approval_binding(revision, approval):
        raise ValueError("plan approval no longer matches immutable content")
    return revision
