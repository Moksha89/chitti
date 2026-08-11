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

from .model_tools import model_tool_names, model_tool_schemas
from .plans import (
    PlanApproval,
    PlanRevision,
    revision_by_id,
    validate_approval_binding,
)
from .previews import copy_export, remove_preview
from .provider import (
    CODER_ROUTE,
    REVIEWER_ROUTE,
    ModelCompletion,
    ModelProvider,
    ModelToolCall,
    ModelTransportError,
)

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)

NONPRODUCTIVE_TURN_LIMIT = 3
MAX_TURNS_WITHOUT_WORKSPACE_CHANGE = 8
MAX_FILE_REWRITES_WITHOUT_COMMAND = 4
MAX_FILE_WRITES_WITHOUT_COMMAND = 24


class ModelProgressError(RuntimeError):
    """The model loop stopped because it was not making useful progress."""


def _model_call_failure_detail(route: str, exc: Exception) -> str:
    if isinstance(exc, ModelTransportError):
        return f"model transport failure on route {route}: {exc}"
    return f"model response processing failed on route {route}: {exc}"


def _progress_counters(
    failures: int,
    nonproductive_turns: int,
    *,
    workspace_changed: bool,
    failure: bool = False,
) -> tuple[int, int]:
    if workspace_changed:
        return 0, 0
    return failures + int(failure), nonproductive_turns + 1


def _model_response_failure(completion: ModelCompletion) -> str | None:
    if completion.finish_reason == "length":
        detail = (
            "model response was truncated before a complete tool call; "
            "be brief and split large file writes across multiple tool calls"
        )
    elif not completion.content.strip() and not completion.tool_calls:
        detail = "model response had no visible content"
    else:
        return None
    return detail


def _file_write_stall(path: str, path_writes: int, total_writes: int) -> str | None:
    if path_writes >= MAX_FILE_REWRITES_WITHOUT_COMMAND:
        return (
            f"{path} was rewritten {path_writes} times without running a command"
        )
    if total_writes >= MAX_FILE_WRITES_WITHOUT_COMMAND:
        return f"stopped after {total_writes} file writes without running a command"
    return None


def _reset_file_write_counter(tool: str, current: int) -> int:
    return 0 if tool == "run_command" else current


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
    model_tokens: int = 500000
    model_write_bytes: int = 2 * 1024 * 1024
    model_spend_usd: float = 0.75
    run_timeout_seconds: int = 7200

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
            model_tokens=int(cast(int, values.get("model_tokens", 500000))),
            model_write_bytes=int(cast(int, values.get("model_write_bytes", 2 * 1024 * 1024))),
            model_spend_usd=float(cast(float, values.get("model_spend_usd", 0.75))),
            run_timeout_seconds=int(cast(int, values.get("run_timeout_seconds", 7200))),
        )


@dataclass(frozen=True)
class FixedOperation:
    task_id: str
    name: str
    command: tuple[str, ...]
    network: str = "none"


MODEL_COMMANDS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "sync-lockfile": (
        "sync-lockfile",
        (
            "sh",
            "-c",
            "npm install --package-lock-only --ignore-scripts "
            "--no-audit --no-fund",
        ),
        "bridge",
    ),
    "install": (
        "npm-install",
        ("sh", "-c", "npm ci --ignore-scripts --no-audit --no-fund"),
        "bridge",
    ),
    "build": ("next-build", ("sh", "-c", "npm run build"), "none"),
    "test": (
        "run-tests",
        ("sh", "-c", "CHITTI_MODEL_LOOP=1 npm test"),
        "none",
    ),
    "export": ("static-export", ("sh", "-c", "test -f out/index.html"), "none"),
}


class WorkerDispatcher(Protocol):
    async def dispatch(
        self, revision: PlanRevision, run_id: int, limits: WorkerLimits
    ) -> None: ...

    async def cancel(self, run_id: int) -> None: ...


class DockerSandboxDispatcher:
    """Host-side cage controller; the worker container receives no Docker socket."""

    _HOST_MOUNT_NAMESPACE = "/proc/1/ns/mnt"

    def __init__(
        self,
        database: Database,
        image: str = "chitti-sandbox:latest",
        workspace_root: Path = Path("/var/lib/chitti-worker/runs"),
        preview_root: Path = Path("/var/lib/chitti-previews"),
        preview_staging_root: Path = Path("/var/lib/chitti-preview-staging"),
        preview_ttl_hours: int = 72,
        model_provider: ModelProvider | None = None,
    ) -> None:
        self.database = database
        self.image = image
        self.workspace_root = workspace_root
        self.preview_root = preview_root
        self.preview_staging_root = preview_staging_root
        self.preview_ttl_hours = preview_ttl_hours
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
        tool_calls_used = 0
        writes = 0
        operation_index = 1
        completed_commands: set[str] = set()
        for task in revision.document.tasks:
            await self._task_event(run_id, task.id, "running", task.title)
            file_write_counts: dict[str, int] = {}
            file_writes_without_command = 0
            nonproductive_turns = 0
            route = CODER_ROUTE
            failures = 0
            messages: list[dict[str, object]] = [
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

            async def record_nonproductive(detail: str, task_id: str = task_id) -> None:
                nonlocal failures, nonproductive_turns
                failures, nonproductive_turns = _progress_counters(
                    failures, nonproductive_turns, workspace_changed=False, failure=True
                )
                await self._event(run_id, "model_tool_failed", detail, task_id=task_id)
                if failures >= NONPRODUCTIVE_TURN_LIMIT:
                    raise RuntimeError(
                        f"task {task_id} stopped after {failures} "
                        f"consecutive model failures: {detail}"
                    )
                if nonproductive_turns >= MAX_TURNS_WITHOUT_WORKSPACE_CHANGE:
                    raise RuntimeError(
                        f"task {task_id} stopped after {nonproductive_turns} "
                        f"model turns without workspace changes: {detail}"
                    )

            async def record_inspection_turn(task_id: str = task_id) -> None:
                nonlocal failures, nonproductive_turns
                failures, nonproductive_turns = _progress_counters(
                    failures, nonproductive_turns, workspace_changed=False
                )
                if nonproductive_turns >= MAX_TURNS_WITHOUT_WORKSPACE_CHANGE:
                    raise RuntimeError(
                        f"task {task_id} stopped after {nonproductive_turns} "
                        "model turns without workspace changes"
                    )

            def reset_progress_counters() -> None:
                nonlocal failures, nonproductive_turns
                failures, nonproductive_turns = _progress_counters(
                    failures, nonproductive_turns, workspace_changed=True
                )

            for iteration in range(1, limits.model_iterations + 1):
                if time.monotonic() - started > limits.run_timeout_seconds:
                    await self._task_event(
                        run_id, task.id, "failed",
                        "model run wall-clock budget exceeded",
                    )
                    raise RuntimeError("model run wall-clock budget exceeded")
                try:
                    completion = await self.model_provider.agent_completion(
                        messages,
                        route,
                        tools=model_tool_schemas() if route == CODER_ROUTE else None,
                        tool_choice="required" if route == CODER_ROUTE else None,
                    )
                except Exception as exc:
                    detail = _model_call_failure_detail(route, exc)
                    failure = ModelCompletion(
                        content=detail[:1000],
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
                        detail[:1000],
                        task_id=task.id,
                    )
                    await self._task_event(run_id, task.id, "failed", detail[:1000])
                    raise
                calls += 1
                spent += completion.cost_usd
                spent_tokens += completion.total_tokens
                await self._record_model_call(
                    run_id, task.id, iteration, route, completion,
                    prompt=json.dumps(messages, separators=(",", ":")),
                )
                if spent_tokens > limits.model_tokens:
                    await self._task_event(
                        run_id, task.id, "failed", "model token budget exceeded"
                    )
                    raise RuntimeError("model token budget exceeded")
                if spent > limits.model_spend_usd:
                    await self._task_event(
                        run_id, task.id, "failed", "model spend budget exceeded"
                    )
                    raise RuntimeError("model spend budget exceeded")
                response_failure = _model_response_failure(completion)
                if response_failure is not None:
                    detail = response_failure
                    await record_nonproductive(detail)
                    messages.extend(
                        _tool_rejection_exchange(completion, f"TOOL FAILURE: {detail}")
                    )
                    await compact_history()
                    continue
                if route == REVIEWER_ROUTE:
                    diagnosis = completion.content.strip()
                    messages = [
                        {"role": "system", "content": stable},
                        {
                            "role": "user",
                            "content": (
                                f"STARTER WORKSPACE:\n{starter_context}\n"
                                f"PLAN:\n{revision.brief}\n{revision.document.summary}\n"
                                f"BELIEFS:\n{json.dumps(beliefs)}\n"
                                f"TASK {task.id}: {task.title}\n{task.description}\n"
                                f"DONE CONDITION: {task.done_condition}\n"
                                "A reviewer diagnosed the previous failure:\n"
                                f"{diagnosis or 'No diagnosis was returned.'}\n"
                                "Make one corrective attempt using the available coder "
                                "tool."
                            ),
                        },
                    ]
                    route = CODER_ROUTE
                    await record_inspection_turn()
                    continue
                if completion.tool_calls:
                    messages.append(_assistant_tool_message(completion))
                    batch_failure: str | None = None
                    batch_completed = True
                    batch_workspace_changed = False
                    for call_index, native_call in enumerate(completion.tool_calls):
                        tool, arguments = native_call.name, native_call.arguments
                        if tool not in model_tool_names():
                            result_text = f"TOOL FAILURE: unknown model tool: {tool}"
                            batch_failure = result_text
                            messages.append(_tool_result_message(native_call, result_text))
                        elif tool_calls_used >= limits.model_tool_calls:
                            result_text = "TOOL FAILURE: model tool-call budget exceeded"
                            batch_failure = result_text
                            messages.append(_tool_result_message(native_call, result_text))
                        else:
                            tool_calls_used += 1
                            if tool == "finish" and _task_done_checks(completed_commands):
                                await self._event(
                                    run_id, "task_finished",
                                    str(arguments.get("summary", ""))[:2000],
                                    task_id=task.id,
                                )
                                await self._task_event(
                                    run_id, task.id, "passed",
                                    str(arguments.get("summary", ""))[:2000],
                                )
                                messages.append(
                                    _tool_result_message(native_call, "task finished")
                                )
                                done = True
                                batch_completed = False
                            elif tool == "finish":
                                result_text = (
                                    "TOOL FAILURE: done condition requires "
                                    "current successful build, test, and export commands"
                                )
                                batch_failure = result_text
                                messages.append(_tool_result_message(native_call, result_text))
                            else:
                                try:
                                    result_text, written, operation_index = (
                                        await self._execute_model_tool(
                                            run_id, task.id, operation_index, tool,
                                            arguments, workspace, limits, route,
                                        )
                                    )
                                    writes += written
                                    if writes > limits.model_write_bytes:
                                        await self._task_event(
                                            run_id, task.id, "failed",
                                            "model write-byte budget exceeded",
                                        )
                                        raise RuntimeError(
                                            "model write-byte budget exceeded"
                                        )
                                    if tool == "run_command":
                                        command_name = str(arguments.get("name", ""))
                                        _record_gate_command(
                                            completed_commands, command_name
                                        )
                                        file_writes_without_command = (
                                            _reset_file_write_counter(
                                                tool, file_writes_without_command
                                            )
                                        )
                                    elif tool == "capture_screenshot":
                                        completed_commands.add("capture_screenshot")
                                    elif tool == "write_file":
                                        path = str(arguments.get("path", ""))
                                        if _source_path_invalidates_gates(path):
                                            completed_commands.clear()
                                        file_writes_without_command += 1
                                        file_write_counts[path] = (
                                            file_write_counts.get(path, 0) + 1
                                        )
                                        stall = _file_write_stall(
                                            path, file_write_counts[path],
                                            file_writes_without_command,
                                        )
                                        if stall is not None:
                                            raise ModelProgressError(
                                                f"task {task.id} stopped: {stall}"
                                            )
                                    if tool in {
                                        "write_file",
                                        "run_command",
                                        "capture_screenshot",
                                    }:
                                        batch_workspace_changed = True
                                    messages.append(
                                        _tool_result_message(
                                            native_call, result_text[:16000]
                                        )
                                    )
                                except Exception as exc:
                                    if isinstance(exc, ModelProgressError):
                                        await self._task_event(
                                            run_id, task.id, "failed", str(exc)[:1000]
                                        )
                                        raise
                                    result_text = (
                                        f"TOOL FAILURE: {tool}: {str(exc)[:1000]}"
                                    )
                                    batch_failure = result_text
                                    messages.append(
                                        _tool_result_message(native_call, result_text)
                                    )
                        if batch_failure is not None:
                            messages.extend(
                                _unexecuted_tool_results(
                                    completion.tool_calls[call_index + 1:],
                                    "TOOL FAILURE: not executed because an "
                                    "earlier tool call in this batch failed",
                                )
                            )
                            batch_completed = False
                            break
                        if done:
                            messages.extend(
                                _unexecuted_tool_results(
                                    completion.tool_calls[call_index + 1:],
                                    "TOOL FAILURE: not executed because the "
                                    "task was already finished",
                                )
                            )
                            break
                    if batch_failure is not None:
                        if batch_workspace_changed:
                            reset_progress_counters()
                        await record_nonproductive(batch_failure)
                        if failures >= 2 and route == CODER_ROUTE:
                            route = REVIEWER_ROUTE
                            messages = _reviewer_diagnosis_messages(
                                task.title, task.description,
                                completion.tool_calls[0].name, batch_failure,
                            )
                            await self._event(
                                run_id, "model_route_switched",
                                "switched to reviewer after two failures on the same task",
                                task_id=task.id,
                            )
                            continue
                    elif batch_completed:
                        if batch_workspace_changed:
                            reset_progress_counters()
                        else:
                            await record_inspection_turn()
                    await compact_history()
                    if done:
                        break
                    continue
                else:
                    native_call = None
                    try:
                        tool, arguments = _parse_tool_call(completion.content)
                    except ValueError as exc:
                        detail = str(exc)[:1000]
                        await record_nonproductive(detail)
                        messages.append(
                            {"role": "user", "content": f"TOOL FAILURE: {detail}"}
                        )
                        await compact_history()
                        continue
                if tool not in model_tool_names():
                    detail = f"unknown model tool: {tool}"
                    await record_nonproductive(detail)
                    messages.extend(
                        _tool_rejection_exchange(
                            completion, f"TOOL FAILURE: {detail}"
                        )
                    )
                    await compact_history()
                    continue
                if tool_calls_used >= limits.model_tool_calls:
                    await self._task_event(
                        run_id, task.id, "failed",
                        "model tool-call budget exceeded",
                    )
                    raise RuntimeError("model tool-call budget exceeded")
                tool_calls_used += 1
                if tool == "finish" and _task_done_checks(completed_commands):
                    await self._event(
                        run_id, "task_finished",
                        str(arguments.get("summary", ""))[:2000], task_id=task.id,
                    )
                    await self._task_event(
                        run_id, task.id, "passed",
                        str(arguments.get("summary", ""))[:2000],
                    )
                    done = True
                    break
                if tool == "finish":
                    result_text = (
                        "TOOL FAILURE: done condition requires current successful "
                        "build, test, and export commands"
                    )
                    await record_nonproductive(result_text)
                    messages.extend(_tool_exchange(completion, result_text, native_call))
                    await compact_history()
                    continue
                try:
                    result_text, written, operation_index = await self._execute_model_tool(
                        run_id, task.id, operation_index, tool, arguments,
                        workspace, limits, route,
                    )
                    writes += written
                    if writes > limits.model_write_bytes:
                        await self._task_event(
                            run_id, task.id, "failed",
                            "model write-byte budget exceeded",
                        )
                        raise RuntimeError("model write-byte budget exceeded")
                    if tool == "run_command":
                        command_name = str(arguments.get("name", ""))
                        _record_gate_command(completed_commands, command_name)
                        file_writes_without_command = _reset_file_write_counter(
                            tool, file_writes_without_command
                        )
                    elif tool == "capture_screenshot":
                        completed_commands.add("capture_screenshot")
                    elif tool == "write_file":
                        path = str(arguments.get("path", ""))
                        if _source_path_invalidates_gates(path):
                            completed_commands.clear()
                        file_writes_without_command += 1
                        file_write_counts[path] = file_write_counts.get(path, 0) + 1
                        stall = _file_write_stall(
                            path, file_write_counts[path], file_writes_without_command
                        )
                        if stall is not None:
                            raise ModelProgressError(
                                f"task {task.id} stopped: {stall}"
                            )
                    if tool in {"write_file", "run_command", "capture_screenshot"}:
                        reset_progress_counters()
                    else:
                        await record_inspection_turn()
                except Exception as exc:
                    if isinstance(exc, ModelProgressError):
                        await self._task_event(
                            run_id, task.id, "failed", str(exc)[:1000]
                        )
                        raise
                    result_text = f"TOOL FAILURE: {tool}: {str(exc)[:1000]}"
                    await record_nonproductive(result_text)
                    if failures >= 2 and route == CODER_ROUTE:
                        route = REVIEWER_ROUTE
                        messages = _reviewer_diagnosis_messages(
                            task.title, task.description, tool, result_text
                        )
                        await self._event(
                            run_id, "model_route_switched",
                            "switched to reviewer after two failures on the same task",
                            task_id=task.id,
                        )
                        continue
                messages.extend(
                    _tool_exchange(completion, result_text[:16000], native_call)
                )
                await compact_history()
            if not done:
                await self._task_event(
                    run_id, task.id, "failed",
                    f"task {task.id} exceeded model iteration budget",
                )
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
        await self._create_export_manifest(run_id, revision, workspace)
        await self._event(run_id, "passed", "model tasks and reviewer passed")

    async def _create_export_manifest(
        self, run_id: int, revision: PlanRevision, workspace: Path
    ) -> None:
        export_root = workspace / "out"
        if not export_root.is_dir():
            raise RuntimeError(
                "run is not promotable: static export output is missing"
            )
        staging = self.preview_staging_root / str(run_id)
        try:
            manifest = await asyncio.to_thread(copy_export, export_root, staging)
            async with self.database.sessions() as session:
                artifacts = await session.execute(
                    text(
                        "SELECT id, kind, sha256 FROM worker_artifacts "
                        "WHERE run_id = :run_id AND kind IN ('diff', 'reviewer_report') "
                        "ORDER BY id"
                    ),
                    {"run_id": run_id},
                )
                rows = list(artifacts.mappings())
                reviewer = next(
                    (row for row in reversed(rows) if row["kind"] == "reviewer_report"),
                    None,
                )
                diff = next(
                    (row for row in reversed(rows) if row["kind"] == "diff"), None
                )
                if reviewer is None or diff is None:
                    raise RuntimeError(
                        "run is not promotable: reviewer or diff evidence is missing"
                    )
                await session.execute(
                    text(
                        "INSERT INTO export_manifests "
                        "(run_id, revision_id, revision_content_hash, "
                        "reviewer_artifact_id, diff_artifact_id, manifest, digest, "
                        "total_bytes, file_count, max_depth, staging_path) VALUES "
                        "(:run_id, :revision_id, :revision_hash, :reviewer, :diff, "
                        "CAST(:manifest AS json), :digest, :total_bytes, :file_count, "
                        ":max_depth, :staging_path)"
                    ),
                    {
                        "run_id": run_id,
                        "revision_id": revision.id,
                        "revision_hash": revision.content_hash,
                        "reviewer": int(reviewer["id"]),
                        "diff": int(diff["id"]),
                        "manifest": json.dumps(manifest.as_json()),
                        "digest": manifest.digest,
                        "total_bytes": manifest.total_bytes,
                        "file_count": len(manifest.entries),
                        "max_depth": manifest.max_depth,
                        "staging_path": str(staging),
                    },
                )
                await session.commit()
        except Exception:
            await asyncio.to_thread(remove_preview, staging)
            raise

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
            if route != CODER_ROUTE:
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
            if name not in MODEL_COMMANDS:
                raise ValueError("unknown allowlisted command")
            op_name, command, network = MODEL_COMMANDS[name]
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
                detail = (stderr or stdout)[-2000:] or f"{name} failed"
                raise RuntimeError(_install_failure_detail(name, detail))
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
        review_messages: list[dict[str, object]] = [
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
            completion = await self.model_provider.agent_completion(
                review_messages, REVIEWER_ROUTE
            )
        except Exception as exc:
            failure = ModelCompletion(
                content=f"reviewer call failed: {str(exc)[:1000]}",
                model=REVIEWER_ROUTE,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cost_usd=0.0,
            )
            await self._record_model_call(
                run_id, "review", calls + 1, REVIEWER_ROUTE, failure,
                kind="reviewer_report",
                prompt=json.dumps(review_messages, separators=(",", ":")),
            )
            raise
        if spent_tokens + completion.total_tokens > limits.model_tokens:
            raise RuntimeError("model token budget exceeded during review")
        if spent + completion.cost_usd > limits.model_spend_usd:
            raise RuntimeError("model spend budget exceeded during review")
        await self._record_model_call(
            run_id, "review", calls + 1, REVIEWER_ROUTE, completion,
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
        response_content = completion.content
        if completion.tool_calls:
            response_content = json.dumps(
                {
                    "content": completion.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                        for call in completion.tool_calls
                    ],
                },
                separators=(",", ":"),
            )
        content, content_size, content_truncated = _bounded_artifact(response_content)
        async with self.database.sessions() as session:
            result = await session.execute(
                text(
                    "INSERT INTO worker_model_calls "
                    "(run_id, task_id, iteration, route, model, prompt_tokens, "
                    "completion_tokens, total_tokens, reasoning_tokens, cost_usd, "
                    "finish_reason, "
                    "message_fields) VALUES "
                    "(:run_id, :task_id, :iteration, :route, :model, :prompt_tokens, "
                    ":completion_tokens, :total_tokens, :reasoning_tokens, :cost_usd, "
                    ":finish_reason, "
                    "CAST(:message_fields AS jsonb)) RETURNING id"
                ),
                {
                    "run_id": run_id, "task_id": task_id, "iteration": iteration,
                    "route": route, "model": completion.model,
                    "prompt_tokens": completion.prompt_tokens,
                    "completion_tokens": completion.completion_tokens,
                    "total_tokens": completion.total_tokens,
                    "reasoning_tokens": completion.reasoning_tokens,
                    "cost_usd": completion.cost_usd,
                    "finish_reason": completion.finish_reason,
                    "message_fields": json.dumps(completion.message_fields),
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
                self._host_command(
                    [
                        "mount",
                        "-o",
                        "loop,nodev,nosuid",
                        str(image),
                        str(workspace),
                    ]
                ),
                check=True,
            )
            await asyncio.to_thread(
                subprocess.run,
                ["chown", "65532:65532", str(workspace)],
                check=True,
            )
            artifacts = workspace / "artifacts"
            artifacts.mkdir(mode=0o700)
            await asyncio.to_thread(
                subprocess.run,
                ["chown", "65532:65532", str(artifacts)],
                check=True,
            )
            source, filesystem, options = await asyncio.to_thread(
                self._mounted_details, workspace
            )
            self._assert_quota_mount(source, filesystem, options)
            await asyncio.to_thread(self._verify_worker_mount, workspace)
        except Exception:
            await self._cleanup_workspace(workspace)
            raise

    async def _unmount_workspace(self, workspace: Path) -> None:
        image = self._workspace_image(workspace)
        source = await asyncio.to_thread(self._mounted_source, workspace)
        unmount_result: subprocess.CompletedProcess[str] | None = None
        if source is not None:
            unmount_result = await asyncio.to_thread(
                subprocess.run,
                self._host_command(["umount", str(workspace)]),
                capture_output=True,
                text=True,
                check=False,
            )
        for _ in range(20):
            if await asyncio.to_thread(self._mounted_source, workspace) is None:
                break
            await asyncio.sleep(0.1)
        else:
            detail = ""
            if unmount_result is not None:
                detail = (
                    f" (umount exit={unmount_result.returncode}, "
                    f"stderr={unmount_result.stderr.strip()!r})"
                )
            raise RuntimeError(f"workspace mount remains active: {workspace}{detail}")
        if unmount_result is not None and unmount_result.returncode != 0:
            logger.warning(
                "workspace unmount reported exit=%s stderr=%r, "
                "but the mount is gone: %s",
                unmount_result.returncode,
                unmount_result.stderr.strip(),
                workspace,
            )

        loops = await asyncio.to_thread(self._workspace_loops, image)
        if source is not None and source.startswith("/dev/loop") and source not in loops:
            loops = (source, *loops)
        detach_failures: list[str] = []
        for loop_device in loops:
            result = await asyncio.to_thread(
                subprocess.run,
                self._host_command(["losetup", "--detach", loop_device]),
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                detach_failures.append(
                    f"{loop_device}: exit={result.returncode}, "
                    f"stderr={result.stderr.strip()!r}"
                )
        for _ in range(20):
            remaining = await asyncio.to_thread(self._workspace_loops, image)
            if not remaining:
                return
            await asyncio.sleep(0.1)
        detail = f"; detach errors={'; '.join(detach_failures)}" if detach_failures else ""
        raise RuntimeError(f"workspace loop device remains active: {image}{detail}")

    @staticmethod
    def _host_command(command: list[str]) -> list[str]:
        return [
            "nsenter",
            f"--mount={DockerSandboxDispatcher._HOST_MOUNT_NAMESPACE}",
            "--",
            *command,
        ]

    @staticmethod
    def _assert_quota_mount(
        source: str | None, filesystem: str, options: str
    ) -> None:
        if (
            source is None
            or not source.startswith("/dev/loop")
            or filesystem != "ext4"
            or "nodev" not in options.split(",")
            or "nosuid" not in options.split(",")
        ):
            raise RuntimeError(
                "workspace quota mount verification failed: "
                f"source={source!r} filesystem={filesystem!r} options={options!r}"
            )

    @classmethod
    def _mounted_details(cls, workspace: Path) -> tuple[str | None, str, str]:
        result = subprocess.run(
            cls._host_command(
                [
                    "findmnt",
                    "--noheadings",
                    "--output",
                    "SOURCE,FSTYPE,OPTIONS",
                    "--mountpoint",
                    str(workspace),
                ]
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        fields = result.stdout.strip().split()
        if len(fields) < 3:
            return None, "", ""
        return fields[0], fields[1], fields[2]

    @classmethod
    def _mounted_source(cls, workspace: Path) -> str | None:
        return cls._mounted_details(workspace)[0]

    @classmethod
    def _verify_worker_mount(cls, workspace: Path) -> None:
        probe = (
            "from pathlib import Path\n"
            "artifacts = Path('/workspace/artifacts')\n"
            "if not artifacts.is_dir():\n"
            "    raise SystemExit('worker artifacts directory is missing')\n"
            "write_probe = artifacts / '.write-probe'\n"
            "write_probe.write_bytes(b'probe')\n"
            "write_probe.unlink()\n"
            "for line in open('/proc/self/mountinfo', encoding='utf-8'):\n"
            "    fields = line.rstrip().split(' - ', 1)\n"
            "    if len(fields) != 2:\n"
            "        continue\n"
            "    mount = fields[0].split()\n"
            "    source = fields[1].split()\n"
            "    if len(mount) > 4 and mount[4] == '/workspace':\n"
            "        if source[0] != 'ext4' or not source[1].startswith('/dev/loop'):\n"
            "            raise SystemExit(f'unexpected worker mount: {line.strip()}')\n"
            "        print(line.strip())\n"
            "        raise SystemExit(0)\n"
            "raise SystemExit('worker mount was not visible')\n"
        )
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--user",
                "65532:65532",
                "--mount",
                f"type=bind,src={workspace},dst=/workspace",
                "chitti-sandbox:latest",
                "python3",
                "-c",
                probe,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "worker quota mount verification failed: "
                f"exit={result.returncode}, stderr={result.stderr.strip()!r}"
            )

    @staticmethod
    def _associated_loops(image: Path) -> tuple[str, ...]:
        result = subprocess.run(
            DockerSandboxDispatcher._host_command(
                ["losetup", "--associated", str(image)]
            ),
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
            DockerSandboxDispatcher._host_command(
                ["losetup", "--list", "--noheadings", "--output", "NAME,BACK-FILE"]
            ),
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
            DockerSandboxDispatcher._host_command(
                ["findmnt", "--noheadings", "--output", "TARGET,SOURCE"]
            ),
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

        self.preview_root.mkdir(parents=True, exist_ok=True)
        self.preview_staging_root.mkdir(parents=True, exist_ok=True)
        async with self.database.sessions() as session:
            manifests = await session.execute(
                text("SELECT staging_path, created_at FROM export_manifests")
            )
            cutoff = datetime.now(UTC).timestamp() - self.preview_ttl_hours * 3600
            known_staging = {
                Path(str(row.staging_path))
                for row in manifests
                if row.created_at.timestamp() >= cutoff
            }
            previews = await session.execute(
                text("SELECT preview_id, expires_at FROM previews")
            )
            now = datetime.now(UTC)
            known_previews = {
                str(row.preview_id)
                for row in previews
                if row.expires_at > now
            }
        for child in self.preview_staging_root.iterdir():
            if child not in known_staging:
                await asyncio.to_thread(remove_preview, child)
        for child in self.preview_root.iterdir():
            if child.name not in known_previews:
                await asyncio.to_thread(remove_preview, child)

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

    async def cleanup_expired_previews(self) -> None:
        if not self.preview_root.exists():
            return
        async with self.database.sessions() as session:
            result = await session.execute(
                text("SELECT preview_id FROM previews WHERE expires_at <= now()")
            )
            expired = [str(row.preview_id) for row in result]
        for identifier in expired:
            await asyncio.to_thread(remove_preview, self.preview_root / identifier)

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
                    "SELECT id, status, detail, operation_index, task_id, created_at "
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
                    "completion_tokens, total_tokens, reasoning_tokens, cost_usd, created_at "
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
                "reasoning_token_totals": sum(
                    int(row["reasoning_tokens"]) for row in model_call_rows
                ),
                "cost_total_usd": sum(float(row["cost_usd"]) for row in model_call_rows),
            }

    async def latest_status(self, run_id: int) -> str | None:
        async with self.database.sessions() as session:
            result = await session.execute(
                text(
                    "SELECT status FROM worker_run_events "
                    "WHERE run_id = :run_id ORDER BY id DESC LIMIT 1"
                ),
                {"run_id": run_id},
            )
            status = result.scalar_one_or_none()
            return str(status) if status is not None else None

    async def events_after(self, run_id: int, event_id: int) -> list[dict[str, object]]:
        async with self.database.sessions() as session:
            result = await session.execute(
                text(
                    "SELECT id, status, detail, operation_index, task_id, created_at "
                    "FROM worker_run_events "
                    "WHERE run_id = :run_id AND id > :event_id ORDER BY id"
                ),
                {"run_id": run_id, "event_id": event_id},
            )
            return [dict(row._mapping) for row in result]

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


def _assistant_tool_message(completion: ModelCompletion) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": completion.content[:16000],
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, separators=(",", ":")),
                },
            }
            for call in completion.tool_calls
        ],
    }


def _tool_result_message(call: ModelToolCall, result_text: str) -> dict[str, object]:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": result_text,
    }


def _unexecuted_tool_results(
    calls: tuple[ModelToolCall, ...], reason: str
) -> list[dict[str, object]]:
    return [_tool_result_message(call, reason) for call in calls]


def _tool_exchange(
    completion: ModelCompletion,
    result_text: str,
    native_call: ModelToolCall | None,
) -> list[dict[str, object]]:
    if native_call is None:
        return [
            {"role": "assistant", "content": completion.content[:16000]},
            {"role": "user", "content": result_text},
        ]
    return [
        _assistant_tool_message(completion),
        _tool_result_message(native_call, result_text),
    ]


def _tool_rejection_exchange(
    completion: ModelCompletion, result_text: str
) -> list[dict[str, object]]:
    if not completion.tool_calls:
        return [
            {"role": "assistant", "content": completion.content[:16000]},
            {"role": "user", "content": result_text},
        ]
    exchange: list[dict[str, object]] = [_assistant_tool_message(completion)]
    exchange.extend(
        _tool_result_message(call, result_text)
        for call in completion.tool_calls
    )
    return exchange


def _task_done_checks(completed_commands: set[str]) -> bool:
    return {"build", "test", "export"} <= completed_commands


def _record_gate_command(evidence: set[str], command: str) -> None:
    if command == "sync-lockfile":
        evidence.clear()
    elif command in {"build", "test", "export", "capture_screenshot"}:
        evidence.add(command)


_NONTRIVIAL_COMPACTION_CHARS = 256
_NON_PROJECT_PATHS = {
    "artifacts",
    "node_modules",
    ".next",
    ".npm-cache",
    ".home",
    ".cache",
    ".npm",
    "out",
}


def _source_path_invalidates_gates(path: str) -> bool:
    normalized: list[str] = []
    for part in Path(path).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if normalized:
                normalized.pop()
            else:
                return True
        else:
            normalized.append(part)
    return not normalized or normalized[0] not in _NON_PROJECT_PATHS


def _bounded_artifact(value: str, maximum: int = 16000) -> tuple[bytes, int, bool]:
    raw = value.encode()
    return raw[:maximum], len(raw), len(raw) > maximum


def _compact_model_messages(
    messages: list[dict[str, object]], recent_turns: int = 8, max_preserved: int = 4
) -> tuple[list[dict[str, object]], bool, int]:
    if len(messages) <= recent_turns + 3:
        return messages, False, 0
    prefix: list[dict[str, object]] = messages[:2]
    units = _message_units(messages[2:])
    recent_units: list[list[dict[str, object]]] = []
    recent_count = 0
    for unit in reversed(units):
        recent_units.insert(0, unit)
        recent_count += len(unit)
        if recent_count >= recent_turns:
            break
    older_units = units[: len(units) - len(recent_units)]
    important_units = [
        unit
        for unit in older_units
        if any(
            marker in str(item.get("content", "")).lower()
            for item in unit
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
    important = [
        {**item, "content": str(item.get("content", ""))[:3000]}
        for unit in important_units
        for item in unit
    ]
    recent = [item for unit in recent_units for item in unit]
    removed_chars = sum(
        len(str(item.get("content", "")))
        for unit in older_units
        for item in unit
    ) - sum(len(str(item.get("content", ""))) for item in important)
    if removed_chars < _NONTRIVIAL_COMPACTION_CHARS:
        return messages, False, 0
    summary: dict[str, object] = {
        "role": "user",
        "content": (
            "COMPACTION: older exploratory turns and superseded file contents were "
            f"removed ({removed_chars} characters). The current workspace is authoritative; "
            "retain the task contract, recent turns, and preserved build/test feedback."
        ),
    }
    return prefix + [summary, *important, *recent], True, removed_chars


def _message_units(messages: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    units: list[list[dict[str, object]]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        unit = [message]
        if message.get("role") == "assistant" and message.get("tool_calls"):
            raw_calls = message.get("tool_calls")
            call_ids = {
                str(call.get("id"))
                for call in raw_calls
                if isinstance(call, dict) and call.get("id")
            } if isinstance(raw_calls, list) else set()
            index += 1
            while index < len(messages):
                following = messages[index]
                if (
                    following.get("role") == "tool"
                    and str(following.get("tool_call_id")) in call_ids
                ):
                    unit.append(following)
                    index += 1
                    continue
                break
            units.append(unit)
            continue
        units.append(unit)
        index += 1
    return units


def _model_system_prompt() -> str:
    return (
        "Stable worker rules come first. Use the provided native function tools and "
        "return exactly one tool call per response; never emit shell commands. A JSON "
        "tool object in visible content is accepted only as a compatibility fallback. "
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
        "inherited fixture text as authored work. The workspace starts with a "
        "package.json and matching package-lock.json. If you add or change "
        "dependencies in package.json, run the allowlisted sync-lockfile "
        "operation before install; it derives the lockfile from package.json. "
        "Then run install, which is the strict reproducible npm ci gate. Run "
        "install before build or test so dependency failures are avoided. If "
        "install reports that package.json and package-lock.json are out of "
        "sync, run sync-lockfile and retry install. Run export after build and "
        "confirm that out/index.html exists; a project that cannot produce a "
        "complete static export is not promotable."
    )


def _reviewer_diagnosis_messages(
    task_title: str, task_description: str, tool: str, failure: str
) -> list[dict[str, object]]:
    return [
        {
            "role": "system",
            "content": (
                "Diagnose the worker failure in plain text. Do not call tools and do "
                "not return JSON. Explain the likely correction briefly so the coder "
                "can make one corrective attempt."
            ),
        },
        {
            "role": "user",
            "content": (
                f"TASK: {task_title}\n{task_description}\n"
                f"ATTEMPTED TOOL: {tool}\nFAILURE:\n{failure}"
            ),
        },
    ]


def _starter_context(workspace: Path) -> str:
    listing = sorted(item.name for item in workspace.iterdir())[:200]
    files = ("package.json", "app/page.js", "app/layout.js", "app/globals.css", "next.config.mjs")
    sections = [f"FILES:\n{json.dumps(listing)}"]
    package_path = workspace / "package.json"
    if package_path.is_file():
        try:
            package = json.loads(package_path.read_text())
        except (OSError, json.JSONDecodeError):
            package = {}
        dependencies = package.get("dependencies", {})
        dev_dependencies = package.get("devDependencies", {})
        if isinstance(dependencies, dict) and isinstance(dev_dependencies, dict):
            direct_dependencies = {
                **{str(key): str(value) for key, value in dependencies.items()},
                **{
                    str(key): str(value)
                    for key, value in dev_dependencies.items()
                },
            }
            sections.append(
                "LOCKED DIRECT DEPENDENCIES:\n"
                + json.dumps(dict(sorted(direct_dependencies.items())))
            )
    for relative in files:
        path = workspace / relative
        if path.is_file():
            content = path.read_bytes()[:12000].decode("utf-8", errors="replace")
            sections.append(f"FILE {relative}:\n{content}")
    return "\n\n".join(sections)


def _is_lockfile_mismatch(detail: str) -> bool:
    lowered = detail.lower()
    return (
        "package.json and package-lock.json" in lowered
        and "sync" in lowered
    ) or (
        "missing:" in lowered
        and "lock file" in lowered
    ) or (
        "invalid:" in lowered
        and "lock file" in lowered
    )


def _install_failure_detail(name: str, detail: str) -> str:
    if name == "install" and _is_lockfile_mismatch(detail):
        return (
            f"{detail}\nDependency manifest mismatch: run the "
            "`sync-lockfile` operation, then run `install` again."
        )
    return detail


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
        FixedOperation(first.id, "static-export", (
            "sh", "-c", "test -f out/index.html",
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
