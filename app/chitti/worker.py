from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .plans import (
    PlanApproval,
    PlanRevision,
    revision_by_id,
    validate_approval_binding,
)
from .provider import ModelCompletion, ModelProvider

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerLimits:
    cpus: float = 1.0
    memory: str = "2g"
    pids: int = 512
    timeout_seconds: int = 900
    nofile: int = 1024
    artifact_bytes: int = 100 * 1024 * 1024
    output_bytes: int = 100 * 1024 * 1024
    workspace_bytes: int = 4 * 1024 * 1024 * 1024
    shm_size: str = "256m"
    model_iterations: int = 40
    model_tool_calls: int = 120
    model_tokens: int = 300000
    model_write_bytes: int = 2 * 1024 * 1024
    model_spend_usd: float = 0.75
    run_timeout_seconds: int = 1800

    def as_json(self) -> dict[str, object]:
        return {
            "cpus": self.cpus,
            "memory": self.memory,
            "pids": self.pids,
            "timeout_seconds": self.timeout_seconds,
            "nofile": self.nofile,
            "artifact_bytes": self.artifact_bytes,
            "output_bytes": self.output_bytes,
            "workspace_bytes": self.workspace_bytes,
            "shm_size": self.shm_size,
            "model_iterations": self.model_iterations,
            "model_tool_calls": self.model_tool_calls,
            "model_tokens": self.model_tokens,
            "model_write_bytes": self.model_write_bytes,
            "model_spend_usd": self.model_spend_usd,
            "run_timeout_seconds": self.run_timeout_seconds,
            "network_policy": "public_egress_default_bridge",
            "non_root_uid": 65532,
        }

    @classmethod
    def from_json(cls, values: Mapping[str, object]) -> WorkerLimits:
        artifact_bytes = int(cast(int, values["artifact_bytes"]))
        return cls(
            cpus=float(cast(float, values["cpus"])),
            memory=str(values["memory"]),
            pids=int(cast(int, values["pids"])),
            timeout_seconds=int(cast(int, values["timeout_seconds"])),
            nofile=int(cast(int, values["nofile"])),
            artifact_bytes=artifact_bytes,
            output_bytes=int(cast(int, values.get("output_bytes", artifact_bytes))),
            workspace_bytes=int(cast(int, values.get("workspace_bytes", artifact_bytes))),
            shm_size=str(values["shm_size"]),
            model_iterations=int(cast(int, values.get("model_iterations", 40))),
            model_tool_calls=int(cast(int, values.get("model_tool_calls", 120))),
            model_tokens=int(cast(int, values.get("model_tokens", 300000))),
            model_write_bytes=int(cast(int, values.get("model_write_bytes", 2 * 1024 * 1024))),
            model_spend_usd=float(cast(float, values.get("model_spend_usd", 0.75))),
            run_timeout_seconds=int(cast(int, values.get("run_timeout_seconds", 1800))),
        )


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
        model_provider: ModelProvider | None = None,
    ) -> None:
        self.database = database
        self.image = image
        self.workspace_root = workspace_root
        self.model_provider = model_provider
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
        workspace = self.workspace_root / f"chitti-run-{run_id}"
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            await self._mount_workspace(workspace, limits)
            await self._event(run_id, "running", "run started")
            if self.model_provider is not None:
                await self._dispatch_model_one(revision, run_id, limits, workspace)
                return
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
                if _directory_size(workspace) > limits.workspace_bytes:
                    await self._task_event(
                        run_id, operation.task_id, "failed", "artifact quota exceeded"
                    )
                    await self._event(run_id, "failed", "artifact quota exceeded")
                    return
            await self._capture_workspace_artifacts(run_id, workspace, limits)
            await self._event(run_id, "passed", "all fixed operations passed")
        finally:
            await self._cleanup_workspace(workspace)

    async def _dispatch_model_one(
        self, revision: PlanRevision, run_id: int, limits: WorkerLimits, workspace: Path
    ) -> None:
        assert self.model_provider is not None
        started = time.monotonic()
        (workspace / "artifacts").mkdir(parents=True, exist_ok=True)
        init = FixedOperation("runner", "git-init", ("sh", "-c", "git init -q /workspace"))
        init_result, _init_out, init_err = await self._run_container(
            run_id, self._docker_command(init, workspace, run_id, limits), limits
        )
        if init_result.returncode:
            raise RuntimeError(init_err[-1000:] or "git initialization failed")
        await self._operation(
            run_id, init, 0, "passed", _init_out, init_err,
            init_result.returncode, datetime.now(UTC),
        )
        fixture = FixedOperation(
            "runner",
            "write-fixture",
            ("sh", "-c", "cp -r /opt/fixture/. /workspace/ && mkdir -p /workspace/artifacts"),
        )
        fixture_result, fixture_out, fixture_err = await self._run_container(
            run_id, self._docker_command(fixture, workspace, run_id, limits), limits
        )
        if fixture_result.returncode:
            raise RuntimeError(fixture_err[-1000:] or "fixture initialization failed")
        await self._operation(
            run_id, fixture, 1, "passed", fixture_out, fixture_err,
            fixture_result.returncode, datetime.now(UTC),
        )
        starter_context = _starter_context(workspace)
        async with self.database.sessions() as session:
            result = await session.execute(
                text(
                    "SELECT d.decision_key, d.decision FROM decisions d "
                    "LEFT JOIN decision_forgets f ON f.decision_id = d.id "
                    "WHERE d.superseded_by IS NULL AND f.id IS NULL ORDER BY d.id"
                )
            )
            beliefs = [dict(row._mapping) for row in result]
        stable = _model_system_prompt()
        spent = 0.0
        spent_tokens = 0
        calls = 0
        writes = 0
        operation_index = 1
        for task in revision.document.tasks:
            completed_commands: set[str] = set()
            route = "coder"
            failures = 0
            messages = [
                {"role": "system", "content": stable},
                {
                    "role": "user",
                    "content": (
                        f"STARTER WORKSPACE:\n{starter_context}\n"
                        f"PLAN:\n{revision.brief}\n{revision.document.summary}\n"
                        f"BELIEFS:\n{json.dumps(beliefs)}\n"
                        f"TASK {task.id}: {task.title}\n{task.description}\n"
                        f"DONE CONDITION: {task.done_condition}"
                    ),
                },
            ]
            done = False
            task_id = task.id

            async def compact_history(task_id: str = task_id) -> None:
                nonlocal messages
                compacted, changed, removed_chars = _compact_model_messages(messages)
                if changed:
                    messages = compacted
                    await self._event(
                        run_id,
                        "model_context_compacted",
                        f"compacted model history: removed {removed_chars} characters",
                        task_id=task_id,
                    )

            for iteration in range(1, limits.model_iterations + 1):
                if time.monotonic() - started > limits.run_timeout_seconds:
                    raise RuntimeError("model run wall-clock budget exceeded")
                if calls >= limits.model_tool_calls:
                    raise RuntimeError("model tool-call budget exceeded")
                try:
                    completion = await self.model_provider.agent_completion(messages, route)
                except Exception as exc:
                    failure = ModelCompletion(
                        content=f"model call failed: {str(exc)[:1000]}",
                        model=route,
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        cost_usd=0.0,
                    )
                    await self._record_model_call(
                        run_id, task.id, iteration, route, failure,
                        prompt=json.dumps(messages, separators=(",", ":")),
                    )
                    await self._event(
                        run_id, "model_tool_failed",
                        f"model call failed on route {route}: {str(exc)[:1000]}",
                        task_id=task.id,
                    )
                    raise
                calls += 1
                spent += completion.cost_usd
                spent_tokens += completion.total_tokens
                await self._record_model_call(
                    run_id, task.id, iteration, route, completion,
                    prompt=json.dumps(messages, separators=(",", ":")),
                )
                if spent_tokens > limits.model_tokens:
                    raise RuntimeError("model token budget exceeded")
                if spent > limits.model_spend_usd:
                    raise RuntimeError("model spend budget exceeded")
                if route == "reviewer":
                    messages.extend(
                        [
                            {"role": "assistant", "content": completion.content[:16000]},
                            {
                                "role": "user",
                                "content": (
                                    "Diagnosis received. Return to the coder route and make "
                                    "one corrective attempt using that diagnosis."
                                ),
                            },
                        ]
                    )
                    route = "coder"
                    continue
                try:
                    tool, arguments = _parse_tool_call(completion.content)
                except ValueError as exc:
                    await self._event(
                        run_id, "model_tool_failed", str(exc)[:1000], task_id=task.id
                    )
                    messages.append(
                        {"role": "user", "content": f"TOOL FAILURE: {str(exc)[:1000]}"}
                    )
                    await compact_history()
                    continue
                if tool == "finish" and _task_done_checks(completed_commands):
                    await self._event(
                        run_id, "task_finished",
                        str(arguments.get("summary", ""))[:2000], task_id=task.id,
                    )
                    done = True
                    break
                if tool == "finish":
                    failures += 1
                    result_text = "TOOL FAILURE: done condition requires successful build and test commands"
                    await self._event(run_id, "model_tool_failed", result_text, task_id=task.id)
                    messages.extend(
                        [
                            {"role": "assistant", "content": completion.content[:16000]},
                            {"role": "user", "content": result_text},
                        ]
                    )
                    await compact_history()
                    continue
                try:
                    result_text, written, operation_index = await self._execute_model_tool(
                        run_id, task.id, operation_index, tool, arguments,
                        workspace, limits, route,
                    )
                    writes += written
                    if writes > limits.model_write_bytes:
                        raise RuntimeError("model write-byte budget exceeded")
                    if tool == "run_command":
                        completed_commands.add(str(arguments.get("name", "")))
                    failures = 0
                except Exception as exc:
                    failures += 1
                    result_text = f"TOOL FAILURE: {tool}: {str(exc)[:1000]}"
                    await self._event(run_id, "model_tool_failed", result_text, task_id=task.id)
                    if failures >= 2 and route == "coder":
                        route = "reviewer"
                        await self._event(
                            run_id, "model_route_switched",
                            "switched to reviewer after two failures on the same task",
                            task_id=task.id,
                        )
                messages.extend(
                    [
                        {"role": "assistant", "content": completion.content[:16000]},
                        {"role": "user", "content": result_text[:16000]},
                    ]
                )
                await compact_history()
            if not done:
                raise RuntimeError(f"task {task.id} exceeded model iteration budget")
        diff = FixedOperation(
            "runner",
            "git-diff",
            (
                "sh", "-c",
                "cd /workspace && git -c safe.directory=/workspace add -A -f -- . "
                "':(exclude)node_modules' ':(exclude)node_modules/**' "
                "':(exclude).next' ':(exclude).next/**' "
                "':(exclude).npm-cache' ':(exclude).npm-cache/**' "
                "':(exclude).home' ':(exclude).home/**' "
                "':(exclude).cache' ':(exclude).cache/**' "
                "':(exclude).npm' ':(exclude).npm/**' "
                "':(exclude)artifacts' ':(exclude)artifacts/**' && "
                "git -c safe.directory=/workspace diff --cached --no-ext-diff "
                "> artifacts/workspace.diff",
            ),
        )
        diff_result, _diff_out, diff_err = await self._run_container(
            run_id, self._docker_command(diff, workspace, run_id, limits), limits
        )
        if diff_result.returncode:
            raise RuntimeError(diff_err[-1000:] or "git diff failed")
        await self._operation(
            run_id, diff, operation_index, "passed", _diff_out, diff_err,
            diff_result.returncode, datetime.now(UTC),
        )
        await self._capture_workspace_artifacts(run_id, workspace, limits)
        await self._review_run(
            run_id, revision, limits, spent, spent_tokens, calls, workspace
        )
        await self._event(run_id, "passed", "model tasks and reviewer passed")

    async def _execute_model_tool(
        self, run_id: int, task_id: str, operation_index: int, tool: str,
        arguments: dict[str, object], workspace: Path, limits: WorkerLimits, route: str,
    ) -> tuple[str, int, int]:
        if tool == "list_files":
            path = _confined_path(workspace, str(arguments.get("path", ".")))
            return json.dumps(sorted(item.name for item in path.iterdir())[:200]), 0, operation_index
        if tool == "read_file":
            path = _confined_path(workspace, str(arguments.get("path", "")))
            maximum = min(int(cast(int, arguments.get("max_bytes", 65536))), 65536)
            return path.read_bytes()[:maximum].decode("utf-8", errors="replace"), 0, operation_index
        if tool == "write_file":
            if route != "coder":
                raise ValueError("reviewer route cannot write files")
            path = _confined_path(workspace, str(arguments.get("path", "")))
            content = str(arguments.get("content", ""))
            encoded = content.encode()
            if len(encoded) > limits.model_write_bytes:
                raise ValueError("single write exceeds model write budget")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encoded)
            return f"wrote {len(encoded)} bytes", len(encoded), operation_index
        if tool == "capture_screenshot":
            route_value = str(arguments.get("route", "/"))
            width = int(cast(int, arguments.get("width", 390)))
            if not route_value.startswith("/") or width not in {390, 1440}:
                raise ValueError("invalid screenshot route or width")
            operation = FixedOperation(task_id, "capture-screenshot", ("python3", "/opt/next_screenshot.py"))
            result, stdout, stderr = await self._run_container(
                run_id, self._docker_command(operation, workspace, run_id, limits), limits
            )
            operation_index += 1
            await self._operation(
                run_id, operation, operation_index,
                "passed" if result.returncode == 0 else "failed",
                stdout, stderr, result.returncode, datetime.now(UTC),
            )
            if result.returncode:
                await self._capture_workspace_artifacts(run_id, workspace, limits)
                raise RuntimeError(stderr[-1000:] or "screenshot failed")
            return stdout[-4000:] or "screenshots captured in artifacts/", 0, operation_index
        if tool == "run_command":
            name = str(arguments.get("name", ""))
            if arguments.get("args", []) not in ([], None):
                raise ValueError("arbitrary command arguments are not allowed")
            commands = {
                "install": ("npm-install", ("sh", "-c", "npm ci --ignore-scripts --no-audit --no-fund"), "bridge"),
                "build": ("next-build", ("sh", "-c", "npm run build"), "none"),
                "test": (
                    "run-tests",
                    ("sh", "-c", "CHITTI_MODEL_LOOP=1 npm test"),
                    "none",
                ),
            }
            if name not in commands:
                raise ValueError("unknown allowlisted command")
            op_name, command, network = commands[name]
            operation = FixedOperation(task_id, op_name, command, network=network)
            result, stdout, stderr = await self._run_container(
                run_id, self._docker_command(operation, workspace, run_id, limits), limits
            )
            operation_index += 1
            status = "passed" if result.returncode == 0 else "failed"
            await self._operation(
                run_id, operation, operation_index, status, stdout, stderr,
                result.returncode, datetime.now(UTC),
            )
            if result.returncode:
                raise RuntimeError((stderr or stdout)[-2000:] or f"{name} failed")
            return (stdout or "command passed")[-4000:], 0, operation_index
        if tool == "finish":
            return str(arguments.get("summary", "")), 0, operation_index
        raise ValueError(f"unknown model tool: {tool}")

    async def _review_run(
        self, run_id: int, revision: PlanRevision, limits: WorkerLimits,
        spent: float, spent_tokens: int, calls: int, workspace: Path,
    ) -> None:
        assert self.model_provider is not None
        evidence = await self._review_evidence(run_id, workspace)
        review_messages = [
            {"role": "system", "content": _reviewer_system_prompt()},
            {
                "role": "user",
                "content": (
                    f"Review completed run for {revision.document.title}.\n"
                    "Review only the evidence below. Return a structured verdict "
                    "with verdict (pass or fail), findings (specific observations), "
                    "evidence_limitations, and summary. Do not claim to inspect "
                    "pixels or image contents; image dimensions and browser errors "
                    "are the available screenshot facts.\n\n"
                    f"{evidence}"
                ),
            },
        ]
        try:
            completion = await self.model_provider.agent_completion(review_messages, "reviewer")
        except Exception as exc:
            failure = ModelCompletion(
                content=f"reviewer call failed: {str(exc)[:1000]}",
                model="reviewer",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cost_usd=0.0,
            )
            await self._record_model_call(
                run_id, "review", calls + 1, "reviewer", failure,
                kind="reviewer_report",
                prompt=json.dumps(review_messages, separators=(",", ":")),
            )
            raise
        if spent_tokens + completion.total_tokens > limits.model_tokens:
            raise RuntimeError("model token budget exceeded during review")
        if spent + completion.cost_usd > limits.model_spend_usd:
            raise RuntimeError("model spend budget exceeded during review")
        await self._record_model_call(
            run_id, "review", calls + 1, "reviewer", completion,
            kind="reviewer_report",
            prompt=json.dumps(review_messages, separators=(",", ":")),
        )
        try:
            verdict = json.loads(completion.content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"reviewer returned invalid JSON: {exc}") from exc
        if (
            not isinstance(verdict, dict)
            or verdict.get("verdict") not in {"pass", "fail"}
            or not isinstance(verdict.get("findings"), list)
            or not isinstance(verdict.get("evidence_limitations"), list)
            or not isinstance(verdict.get("summary"), str)
        ):
            raise RuntimeError("reviewer returned an incomplete structured verdict")
        await self._event(run_id, "review_complete", json.dumps(verdict)[:4000])
        if verdict["verdict"] == "fail":
            raise RuntimeError(f"reviewer verdict fail: {verdict['summary'][:1000]}")

    async def _review_evidence(self, run_id: int, workspace: Path) -> str:
        async with self.database.sessions() as session:
            operations = await session.execute(
                text(
                    "SELECT name, status, stdout, stderr, exit_code "
                    "FROM worker_operations WHERE run_id = :run_id "
                    "ORDER BY operation_index"
                ),
                {"run_id": run_id},
            )
            rows = [
                {
                    **dict(row._mapping),
                    "stdout": str(row._mapping["stdout"])[-4000:],
                    "stderr": str(row._mapping["stderr"])[-4000:],
                }
                for row in operations
                if row._mapping["name"] in {
                    "npm-install",
                    "next-build",
                    "run-tests",
                    "capture-screenshot",
                    "git-diff",
                }
            ][-20:]
        facts: list[dict[str, object]] = []
        for name in ("phone.png", "desktop.png", "browser-errors.json", "workspace.diff"):
            path = workspace / "artifacts" / name
            if not path.is_file():
                continue
            item: dict[str, object] = {"path": f"artifacts/{name}"}
            if name.endswith(".png"):
                raw = path.read_bytes()
                if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
                    item["dimensions"] = {
                        "width": int.from_bytes(raw[16:20], "big"),
                        "height": int.from_bytes(raw[20:24], "big"),
                    }
            else:
                item["bytes"] = path.stat().st_size
                if name == "browser-errors.json":
                    item["content"] = path.read_text(encoding="utf-8")[:8000]
            facts.append(item)
        return json.dumps({"operations": rows, "artifacts": facts}, default=str)

    async def _record_model_call(
        self, run_id: int, task_id: str, iteration: int, route: str,
        completion: ModelCompletion, kind: str = "model_response",
        prompt: str = "",
    ) -> None:
        prompt_bytes, prompt_size, prompt_truncated = _bounded_artifact(prompt)
        content, content_size, content_truncated = _bounded_artifact(completion.content)
        async with self.database.sessions() as session:
            result = await session.execute(
                text(
                    "INSERT INTO worker_model_calls "
                    "(run_id, task_id, iteration, route, model, prompt_tokens, "
                    "completion_tokens, total_tokens, cost_usd) VALUES "
                    "(:run_id, :task_id, :iteration, :route, :model, :prompt_tokens, "
                    ":completion_tokens, :total_tokens, :cost_usd) RETURNING id"
                ),
                {
                    "run_id": run_id, "task_id": task_id, "iteration": iteration,
                    "route": route, "model": completion.model,
                    "prompt_tokens": completion.prompt_tokens,
                    "completion_tokens": completion.completion_tokens,
                    "total_tokens": completion.total_tokens,
                    "cost_usd": completion.cost_usd,
                },
            )
            call_id = int(result.scalar_one())
            artifact = await session.execute(
                text(
                    "INSERT INTO worker_artifacts "
                    "(run_id, kind, path, sha256, byte_size, original_byte_size, truncated) "
                    "VALUES (:run_id, :kind, :path, :sha256, :byte_size, "
                    ":original_byte_size, :truncated) RETURNING id"
                ),
                {
                    "run_id": run_id, "kind": kind,
                    "path": f"model_calls/{call_id}/response.json",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "byte_size": len(content),
                    "original_byte_size": content_size,
                    "truncated": content_truncated,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO worker_artifact_payloads (artifact_id, content) "
                    "VALUES (:artifact_id, :content)"
                ),
                {"artifact_id": int(artifact.scalar_one()), "content": content},
            )
            prompt_artifact = await session.execute(
                text(
                    "INSERT INTO worker_artifacts "
                    "(run_id, kind, path, sha256, byte_size, original_byte_size, truncated) "
                    "VALUES (:run_id, 'model_prompt', :path, :sha256, :byte_size, "
                    ":original_byte_size, :truncated) "
                    "RETURNING id"
                ),
                {
                    "run_id": run_id,
                    "path": f"model_calls/{call_id}/prompt.json",
                    "sha256": hashlib.sha256(prompt_bytes).hexdigest(),
                    "byte_size": len(prompt_bytes),
                    "original_byte_size": prompt_size,
                    "truncated": prompt_truncated,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO worker_artifact_payloads (artifact_id, content) "
                    "VALUES (:artifact_id, :content)"
                ),
                {
                    "artifact_id": int(prompt_artifact.scalar_one()),
                    "content": prompt_bytes,
                },
            )
            await session.commit()

    async def _mount_workspace(self, workspace: Path, limits: WorkerLimits) -> None:
        image = self._workspace_image(workspace)
        await asyncio.to_thread(
            subprocess.run,
            [
                "truncate",
                "-s",
                str(limits.workspace_bytes),
                str(image),
            ],
            check=True,
        )
        await asyncio.to_thread(
            subprocess.run,
            ["mkfs.ext4", "-q", "-F", "-m", "0", str(image)],
            check=True,
        )
        try:
            await asyncio.to_thread(
                subprocess.run,
                [
                    "mount",
                    "-o",
                    "loop,nodev,nosuid",
                    str(image),
                    str(workspace),
                ],
                check=True,
            )
            await asyncio.to_thread(
                subprocess.run,
                ["chown", "65532:65532", str(workspace)],
                check=True,
            )
        except Exception:
            await self._cleanup_workspace(workspace)
            raise

    async def _unmount_workspace(self, workspace: Path) -> None:
        image = self._workspace_image(workspace)
        source = await asyncio.to_thread(self._mounted_source, workspace)
        if source is not None:
            await asyncio.to_thread(
                subprocess.run, ["umount", str(workspace)], check=False
            )
        for _ in range(20):
            if await asyncio.to_thread(self._mounted_source, workspace) is None:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError(f"workspace mount remains active: {workspace}")

        loops = await asyncio.to_thread(self._workspace_loops, image)
        if source is not None and source.startswith("/dev/loop") and source not in loops:
            loops = (source, *loops)
        for loop_device in loops:
            await asyncio.to_thread(
                subprocess.run, ["losetup", "--detach", loop_device], check=False
            )
        for _ in range(20):
            remaining = await asyncio.to_thread(self._workspace_loops, image)
            if not remaining:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"workspace loop device remains active: {image}")

    @staticmethod
    def _mounted_source(workspace: Path) -> str | None:
        result = subprocess.run(
            [
                "findmnt",
                "--noheadings",
                "--output",
                "SOURCE",
                "--mountpoint",
                str(workspace),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        source = result.stdout.strip()
        return source or None

    @staticmethod
    def _associated_loops(image: Path) -> tuple[str, ...]:
        result = subprocess.run(
            ["losetup", "--associated", str(image)],
            capture_output=True,
            text=True,
            check=False,
        )
        return tuple(
            line.split(":", 1)[0].strip()
            for line in result.stdout.splitlines()
            if line.split(":", 1)[0].strip()
        )

    @classmethod
    def _workspace_loops(cls, image: Path) -> tuple[str, ...]:
        loops = cls._associated_loops(image)
        backing = cls._backing_loops(image.parent).get(image, ())
        return tuple(dict.fromkeys((*loops, *backing)))

    @staticmethod
    def _backing_loops(root: Path) -> dict[Path, tuple[str, ...]]:
        result = subprocess.run(
            ["losetup", "--list", "--noheadings", "--output", "NAME,BACK-FILE"],
            capture_output=True,
            text=True,
            check=False,
        )
        loops: dict[Path, list[str]] = {}
        for line in result.stdout.splitlines():
            fields = line.split(None, 1)
            if len(fields) != 2:
                continue
            device, backing = fields
            backing_path = Path(backing.removesuffix(" (deleted)"))
            if backing_path.parent == root and backing_path.name.startswith("chitti-run-"):
                loops.setdefault(backing_path, []).append(device)
        return {image: tuple(devices) for image, devices in loops.items()}

    @staticmethod
    def _mounted_workspaces(root: Path) -> set[Path]:
        result = subprocess.run(
            ["findmnt", "--noheadings", "--output", "TARGET,SOURCE"],
            capture_output=True,
            text=True,
            check=False,
        )
        workspaces: set[Path] = set()
        for line in result.stdout.splitlines():
            fields = line.split(None, 1)
            if len(fields) != 2:
                continue
            target = Path(fields[0])
            if target.parent == root and target.name.startswith("chitti-run-"):
                workspaces.add(target)
        return workspaces

    async def _cleanup_workspace(self, workspace: Path) -> None:
        await self._unmount_workspace(workspace)
        shutil.rmtree(workspace, ignore_errors=True)
        self._workspace_image(workspace).unlink(missing_ok=True)

    def _workspace_image(self, workspace: Path) -> Path:
        return workspace.with_name(f"{workspace.name}.img")

    async def cleanup_stale_workspaces(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        images = set(self.workspace_root.glob("chitti-run-*.img"))
        images.update(self._backing_loops(self.workspace_root))
        workspaces = {image.with_suffix("") for image in images}
        workspaces.update(self._mounted_workspaces(self.workspace_root))
        failures: list[str] = []
        for workspace in workspaces:
            run_id = workspace.name.removeprefix("chitti-run-")
            await self._remove_container(f"chitti-worker-{run_id}")
            try:
                await self._cleanup_workspace(workspace)
            except Exception as exc:
                detail = f"stale workspace cleanup failed: {str(exc)[:1000]}"
                failures.append(f"{workspace}: {detail}")
                await self._record_cleanup_failure(run_id, detail)
        if failures:
            raise RuntimeError("; ".join(failures))

    async def _record_cleanup_failure(self, run_id: str, detail: str) -> None:
        try:
            numeric_run_id = int(run_id)
        except (TypeError, ValueError):
            logger.error("workspace cleanup failure for non-run %s: %s", run_id, detail)
            return
        if self.database is None:
            logger.error("workspace cleanup failure for run %s: %s", run_id, detail)
            return
        try:
            async with self.database.sessions() as session:
                await session.execute(
                    text(
                        "INSERT INTO worker_run_events (run_id, status, detail) "
                        "VALUES (:run_id, 'failed', :detail)"
                    ),
                    {"run_id": numeric_run_id, "detail": detail},
                )
                await session.commit()
        except Exception:
            logger.exception("could not record workspace cleanup failure for run %s", run_id)

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
                    self._read_limited, process.stdout, max(1, limits.output_bytes // 2)
                )
            )
            stderr_task = asyncio.create_task(
                asyncio.to_thread(
                    self._read_limited, process.stderr, max(1, limits.output_bytes // 2)
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
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
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
        artifact_root = workspace / "artifacts"
        if not artifact_root.is_dir():
            return
        for path in artifact_root.iterdir():
            if not path.is_file() or (
                path.suffix != ".png"
                and path.name not in {"workspace.diff", "browser-errors.json"}
            ):
                continue
            if path.stat().st_size > limits.artifact_bytes:
                continue
            content = path.read_bytes()
            kind = (
                "screenshot" if path.suffix == ".png"
                else "browser_evidence" if path.name == "browser-errors.json"
                else "diff"
            )
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
                    "SELECT id, operation_id, kind, path, sha256, byte_size, "
                    "original_byte_size, truncated "
                    "FROM worker_artifacts WHERE run_id = :run_id ORDER BY id"
                ),
                {"run_id": run_id},
            )
            model_calls = await session.execute(
                text(
                    "SELECT id, task_id, iteration, route, model, prompt_tokens, "
                    "completion_tokens, total_tokens, cost_usd, created_at "
                    "FROM worker_model_calls WHERE run_id = :run_id ORDER BY id"
                ),
                {"run_id": run_id},
            )
            model_call_rows = [dict(row._mapping) for row in model_calls]
            return {
                "run": dict(run),
                "events": [dict(row._mapping) for row in events],
                "operations": [dict(row._mapping) for row in operations],
                "artifacts": [dict(row._mapping) for row in artifacts],
                "model_calls": model_call_rows,
                "token_totals": sum(int(row["total_tokens"]) for row in model_call_rows),
                "cost_total_usd": sum(float(row["cost_usd"]) for row in model_call_rows),
            }

def _confined_path(workspace: Path, requested: str) -> Path:
    if not requested or "\x00" in requested:
        raise ValueError("invalid workspace path")
    relative = Path(requested)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("workspace path escapes sandbox")
    if any(part.lower() in {".env", ".env.local", "credentials", "secrets"} for part in relative.parts):
        raise ValueError("sensitive workspace path is not accessible")
    root = workspace.resolve()
    candidate = (root / relative).resolve(strict=False)
    if candidate != root and root not in candidate.parents:
        raise ValueError("workspace path escapes sandbox")
    return candidate


def _parse_tool_call(content: str) -> tuple[str, dict[str, object]]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("model response was not valid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("tool"), str):
        raise ValueError("model response did not contain one tool call")
    arguments = value.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    return str(value["tool"]), {str(key): item for key, item in arguments.items()}


def _task_done_checks(completed_commands: set[str]) -> bool:
    return {"build", "test"} <= completed_commands


def _bounded_artifact(value: str, maximum: int = 16000) -> tuple[bytes, int, bool]:
    raw = value.encode()
    return raw[:maximum], len(raw), len(raw) > maximum


def _compact_model_messages(
    messages: list[dict[str, str]], recent_turns: int = 8, max_preserved: int = 4
) -> tuple[list[dict[str, str]], bool, int]:
    if len(messages) <= recent_turns + 3:
        return messages, False, 0
    prefix = messages[:2]
    older = messages[2:-recent_turns]
    recent = messages[-recent_turns:]
    important = [
        {"role": item["role"], "content": item["content"][:3000]}
        for item in older
        if any(
            marker in item["content"].lower()
            for marker in (
                "tool failure",
                "next-build",
                "run-tests",
                "npm-install",
                "screenshot",
                "worker output",
            )
        )
    ][-max_preserved:]
    removed_chars = sum(len(item["content"]) for item in older) - sum(
        len(item["content"]) for item in important
    )
    summary = {
        "role": "user",
        "content": (
            "COMPACTION: older exploratory turns and superseded file contents were "
            f"removed ({removed_chars} characters). The current workspace is authoritative; "
            "retain the task contract, recent turns, and preserved build/test feedback."
        ),
    }
    return prefix + [summary, *important, *recent], True, removed_chars


def _model_system_prompt() -> str:
    return (
        "Stable worker rules and tool schemas come first. Emit exactly one strict JSON "
        "object per response and never emit shell commands. Tools:\n"
        '{"tool":"list_files","arguments":{"path":"."}}\n'
        '{"tool":"read_file","arguments":{"path":"app/page.js","max_bytes":65536}}\n'
        '{"tool":"write_file","arguments":{"path":"app/page.js","content":"..."}}\n'
        '{"tool":"run_command","arguments":{"name":"install|build|test","args":[]}}\n'
        '{"tool":"capture_screenshot","arguments":{"route":"/","width":390}}\n'
        '{"tool":"finish","arguments":{"summary":"done"}}\n'
        "All paths are relative to the disposable workspace. No .env, secrets, "
        "credentials, arbitrary argv, shell passthrough, or network tool exists. "
        "Write useful code early, then iterate using build and test feedback; do not "
        "read the entire workspace before making a first change."
        " Browser capture runs with no network: do not use remote assets, Drei "
        "Environment presets, Drei Text, remote fonts, external URLs, or runtime "
        "fetches. "
        "Use local geometry, lights, CSS, and ASCII text so the page renders "
        "offline inside the cage. Replace the starter's fixture copy and "
        "placeholder content with original task-specific copy; do not claim "
        "inherited fixture text as authored work. Run npm install before build "
        "or test so dependency failures are avoided."
    )


def _starter_context(workspace: Path) -> str:
    listing = sorted(item.name for item in workspace.iterdir())[:200]
    files = ("package.json", "app/page.js", "app/layout.js", "app/globals.css", "next.config.mjs")
    sections = [f"FILES:\n{json.dumps(listing)}"]
    for relative in files:
        path = workspace / relative
        if path.is_file():
            content = path.read_bytes()[:12000].decode("utf-8", errors="replace")
            sections.append(f"FILE {relative}:\n{content}")
    return "\n\n".join(sections)


def _reviewer_system_prompt() -> str:
    return (
        "You are the reviewer route. Return one strict JSON object with exactly "
        'the fields {"verdict":"pass|fail","findings":[],"evidence_limitations":[],'
        '"summary":"..."}. Findings must be specific observations grounded in the '
        "provided operation output and artifact facts. An unresolved browser error, "
        "page exception, failed request, failed build/test, or missing artifact "
        "requires verdict fail. A failed attempt followed by a successful retry is "
        "a finding but not an unresolved failure. Do not claim to inspect screenshot "
        "pixels: the prompt only contains screenshot dimensions and browser evidence "
        "facts. Do not write files or propose shell commands."
    )


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
                "cp -r /opt/fixture/. /workspace/ && mkdir -p /workspace/artifacts",
            ),
        ),
        FixedOperation(
            first.id,
            "install-node-dependencies",
            (
                "sh",
                "-c",
                "npm ci --ignore-scripts --no-audit --no-fund",
            ),
            network="bridge",
        ),
        FixedOperation(first.id, "next-build", (
            "sh", "-c", "npm run build",
        )),
        FixedOperation(first.id, "browser-preview", (
            "python3", "/opt/next_screenshot.py",
        )),
        FixedOperation(first.id, "run-tests", (
            "npm", "test",
        )),
        FixedOperation(first.id, "git-diff", (
                "sh", "-c", "cd /workspace && git -c safe.directory=/workspace "
                "add -A -f -- . ':(exclude)node_modules' "
                "':(exclude)node_modules/**' ':(exclude)**/node_modules/**' "
                "':(exclude).next' ':(exclude).next/**' "
                "':(exclude)**/.next/**' ':(exclude).npm-cache' "
                "':(exclude).npm-cache/**' ':(exclude)**/.npm-cache/**' "
                "':(exclude).home' ':(exclude).home/**' "
                "':(exclude)**/.home/**' ':(exclude).cache' "
                "':(exclude).cache/**' ':(exclude)**/.cache/**' "
                "':(exclude).npm' ':(exclude).npm/**' "
                "':(exclude)**/.npm/**' "
                "':(exclude)artifacts' ':(exclude)artifacts/**' && "
            "git -c safe.directory=/workspace diff --cached --no-ext-diff "
            "> artifacts/workspace.diff",
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
