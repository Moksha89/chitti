from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol, TypedDict, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .brand_colors import split_brand_color
from .brand_profiles import BrandProfile, available_font_families, get_brand_profile
from .image_generation import (
    ImageBudgetExceeded,
    ImageManifestRefused,
    ImageProviderFailure,
    ImageProviderUnavailable,
    generate_manifest_images,
    verify_export_assets,
)
from .job_types import (
    WEBSITE_POLICY,
    JobTypePolicy,
    config_json,
    policy_for,
    poster_config,
    poster_config_within_ceiling,
)
from .memory import normalize_namespace
from .model_tools import model_tool_names, model_tool_schemas
from .namespaces import SHARED_NAMESPACE
from .plans import (
    PlanApproval,
    PlanRevision,
    revision_by_id,
    validate_approval_binding,
)
from .previews import copy_export, remove_preview
from .provider import (
    CODER_FALLBACK_ROUTE,
    CODER_MAX_OUTPUT_TOKENS,
    CODER_ROUTE,
    CODER_ROUTES,
    REVIEWER_ROUTE,
    VISION_FALLBACK_ROUTE,
    VISION_ROUTE,
    ModelCompletion,
    ModelProvider,
    ModelProviderError,
    ModelToolCall,
    ModelTransportError,
)
from .run_status import validate_run_event_status
from .runner_access import application_only_sql, runner_sql
from .settings import get_settings

MAX_CAPTURE_ARTIFACTS_PER_RUN = 32
LIVE_OUTPUT_FLUSH_BYTES = 16 * 1024
LIVE_OUTPUT_FLUSH_SECONDS = 0.25
LIVE_OUTPUT_TAIL_BYTES = 256 * 1024

if TYPE_CHECKING:
    from .db import Database

logger = logging.getLogger(__name__)

NONPRODUCTIVE_TURN_LIMIT = 3
MAX_TURNS_WITHOUT_WORKSPACE_CHANGE = 8
FINISH_HINT_AFTER_NONPRODUCTIVE_TURNS = 3
MAX_PROGRESS_LEDGER_ENTRIES = 24
MAX_PROGRESS_LEDGER_ITEM_CHARS = 160
MAX_FILE_REWRITES_WITHOUT_COMMAND = 4
MODEL_TOOL_CALL_BUDGET = 240
MAX_FILE_WRITES_WITHOUT_COMMAND = 24
IDENTICAL_TOOL_FAILURE_LIMIT = 3
REQUIRED_GATE_COMMANDS = ("build", "test", "export")
VISUAL_REVIEW_MAX_CYCLES = 4
VISUAL_REVIEW_SPEND_CAP_USD = 0.20


class RunBudgetExceeded(RuntimeError):
    """A run-level budget ended execution and must not be retried by the model."""

    def __init__(self, budget: str, detail: str | None = None) -> None:
        self.budget = budget
        super().__init__(detail or f"{budget} budget exceeded")


class RunCancelled(RuntimeError):
    """The owner cancelled a run while it was executing."""


class ModelProgressError(RuntimeError):
    """The model loop stopped because it was not making useful progress."""


class VisualReviewInconclusive(RuntimeError):
    """The poster could not reach a trustworthy visual verdict."""


VISUAL_REVIEW_INSTRUCTION = "\n\n".join(
    (
        "You are judging one captured poster PNG. Describe before you judge: "
        "an unexamined pass is worse than a false fail, because a pass ends the work.",
        "Return exactly one JSON object with exactly these top-level fields: "
        "observations, verdict, image_sha256, criteria, findings, summary, "
        "evidence_limitations.",
        "observations must be an object with exactly these keys, each a plain-language "
        "description of what the image actually shows: imagery_edges, background, "
        "imagery, text_blocks, colour_use. imagery_edges must describe, for "
        "every placed photographic or illustrated element, only boundaries visibly "
        "present against the surrounding design. Distinguish a visible seam, halo, "
        "background rectangle or panel from an intentional crop at the canvas edge; "
        "do not call a clean subject contour or canvas crop a straight edge. State "
        "where a real boundary is visible, or explicitly say no visible boundary "
        "exists.",
        "criteria must contain exactly these keys, each pass or fail: fixture_text, "
        "visual_hierarchy, readability, generated_imagery, composite_integrity, "
        "brand_constraints, text_contrast, cinematic_treatment, subject_scale, "
        "text_redundancy, asset_sharpness, kit_fidelity, research_copy.",
        "composite_integrity fails when any placed image reads as a pasted rectangle: "
        "a visible box, panel or seam around it, a background inside it that differs "
        "from the poster background, or a washed-out block where the element sits. "
        "Deliberate framing lines that belong to the design's own geometry are not "
        "failures; an element's own leftover background is. If you are unsure whether "
        "an edge is deliberate, describe it and fail composite_integrity.",
        "generated_imagery fails when imagery is absent, or so dark, faint or "
        "low-contrast that the poster would read as flat colour without it.",
        "subject_scale fails when focal subjects are small or soft against large "
        "empty areas, especially when the composition leaves a substantial dead "
        "middle or lower third. Fail when the focal figures occupy only a minor "
        "portion of the canvas rather than carrying the composition; estimate "
        "their visible height and reject figures that are plainly undersized. "
        "Fail when a subject floats in mid-air, is cropped across the body with "
        "a visible hard edge, lacks full-body grounding with feet, or leaves a "
        "large empty middle band. Subjects should be anchored to the lower "
        "composition beneath a defined title and fixture-information zone.",
        "text_redundancy fails when the same fixture, matchup, date, venue, or "
        "other information is stated twice in materially duplicated text blocks. "
        "Treat case, punctuation, separators, and a versus marker as irrelevant: "
        "'INDIA PAKISTAN' and 'India v Pakistan' are the same matchup and must fail "
        "when both are visible.",
        "asset_sharpness fails when placed imagery is visibly blurry, soft, pixelated, "
        "upscaled, illustrated, or cartoonish where photographic realism was "
        "requested. If the imagery is described as illustrated or cartoonish, fail "
        "this criterion even if it is not visibly blurry.",
        "kit_fidelity fails when approved research facts describe kit colours and "
        "the visible figures contradict those colours; compare the image to the "
        "approved facts, not to invented palette names.",
        "research_copy fails when a descriptive research value such as 'rich blue "
        "foundation with striking orange panels' or 'traditional light green "
        "colour scheme' is rendered as visible poster copy. Research descriptions "
        "may guide imagery and styling, but only fixture facts—teams, competition, "
        "date, time, and venue—may appear as text.",
        "text_contrast fails when any required text block is low-contrast against "
        "its immediate background, including a title or team name that becomes "
        "difficult to read because its colour is too close to the background. "
        "Judge every text block, not only the smallest text.",
        "cinematic_treatment fails when the composition is flat or evenly lit and "
        "lacks visible directional or rim lighting, foreground/background depth "
        "separation, and atmospheric treatment such as haze, light spill, particles "
        "or grain. A dark colour wash or gradient alone is not cinematic treatment.",
        "brand_constraints must judge the likeness policy stated in the poster brief: "
        "the policy is an owner decision carried in the run configuration. Generic "
        "figures are the default; real likenesses are permitted only when the "
        "recorded policy says so. Fabricated board "
        "logos and fabricated trophy imagery remain prohibited regardless of likeness "
        "policy.",
        "verdict must be pass or fail, and must be fail if any criterion is fail. "
        "image_sha256 must exactly match the supplied digest. findings must be a list "
        "of objects with criterion, issue and action fields; every failed criterion "
        "needs at least one finding whose action names a concrete change. "
        "evidence_limitations must be a list of strings; use an empty list if you "
        "have none. Do not use markdown fences. Do not claim anything the image does "
        "not show.",
    )
)


class VisualState(TypedDict):
    cycles: int
    cost: float
    tokens: int
    accounted_cost: float
    accounted_tokens: int
    last_failed_digest: str | None
    source_digest: str | None
    review_passed: bool
    review_task_id: str | None


def _poster_likeness_policy(brief: str) -> str:
    normalized = brief.lower()
    if re.search(
        r"\b(?:no|without|not)\s+(?:real\s+)?(?:player|players|likeness|faces?|kits?)\b",
        normalized,
    ):
        return (
            "Likeness policy: generic figures are required; real player likenesses "
            "are not permitted by this brief."
        )
    if re.search(
        r"\breal\s+(?:players?|likeness(?:es)?|faces?|kits?)\b",
        normalized,
    ):
        return (
            "Likeness policy: this brief explicitly permits real player likenesses, "
            "real kits, and real faces. Do not fail brand constraints merely because "
            "the figures look like real players."
        )
    return (
        "Likeness policy: the brief is silent, so use generic figures and do not "
        "introduce real player likenesses."
    )


class GateEvidenceContradiction(ModelProgressError):
    """The loop stopped because gate evidence reached an impossible state."""


def _model_call_failure_detail(route: str, exc: Exception) -> str:
    if isinstance(exc, ModelProviderError):
        retry_detail = (
            f" retry_failures={','.join(exc.retry_failures)}"
            if exc.retry_failures
            else ""
        )
        detail = (
            f"model provider failure on route {route}: "
            f"class={exc.failure_class} attempts={exc.attempts}{retry_detail}: {exc}"
        )
        if exc.response_diagnostics:
            detail += " diagnostics=" + " | ".join(exc.response_diagnostics)
        return detail
    return f"model response processing failed on route {route}: {exc}"


def _retry_evidence(
    attempts: int,
    failures: tuple[str, ...],
    total_tokens: int = 0,
) -> tuple[str, ...]:
    if attempts <= 1:
        return ()
    evidence = (f"retry_attempts={attempts}",) + tuple(
        f"retry_failure={failure_class}" for failure_class in failures
    )
    if total_tokens:
        evidence += (f"retry_total_tokens={total_tokens}",)
    return evidence


def _model_failure_completion(route: str, exc: Exception) -> ModelCompletion:
    provider = exc if isinstance(exc, ModelProviderError) else None
    return ModelCompletion(
        content=_model_call_failure_detail(route, exc)[:1000],
        model=route,
        prompt_tokens=provider.prompt_tokens if provider else 0,
        completion_tokens=provider.completion_tokens if provider else 0,
        total_tokens=provider.total_tokens if provider else 0,
        cost_usd=provider.cost_usd if provider else 0.0,
        message_fields=(
            _retry_evidence(
                provider.attempts,
                provider.retry_failures,
                provider.total_tokens,
            )
            if provider
            else ()
        ),
        response_diagnostics=provider.response_diagnostics if provider else (),
    )


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


def _identical_tool_failure_detail(
    task_id: str,
    tool: str,
    detail: str,
    failure_counts: dict[tuple[str, str], int],
) -> tuple[str, int]:
    failure = (tool, detail)
    count = failure_counts.get(failure, 0) + 1
    failure_counts[failure] = count
    if count >= IDENTICAL_TOOL_FAILURE_LIMIT:
        raise ModelProgressError(
            f"task {task_id} stopped after {count} identical "
            f"failures from {tool}: {detail}"
        )
    if count == IDENTICAL_TOOL_FAILURE_LIMIT - 1:
        detail = (
            f"{detail} This identical failure has occurred {count} "
            "times in this run; repeating it once more will end the run."
        )
    return detail, count


def _remember_progress_ledger(
    ledger: list[str], entry: str, *, maximum: int = MAX_PROGRESS_LEDGER_ENTRIES
) -> None:
    bounded = entry[:MAX_PROGRESS_LEDGER_ITEM_CHARS]
    if bounded in ledger:
        ledger.remove(bounded)
    ledger.append(bounded)
    del ledger[:-maximum]


def _model_progress_context(
    nonproductive_turns: int,
    completed_commands: set[str],
    inspected_paths: list[str],
    command_outcomes: list[str],
    required_gates: tuple[str, ...] = REQUIRED_GATE_COMMANDS,
) -> str:
    remaining = max(0, MAX_TURNS_WITHOUT_WORKSPACE_CHANGE - nonproductive_turns)
    lines = [
        "PROGRESS STATUS (system fact):",
        (
            f"This task stops after {MAX_TURNS_WITHOUT_WORKSPACE_CHANGE} model "
            f"turns without workspace changes. {nonproductive_turns} have been "
            f"used; {remaining} nonproductive turn(s) remain before that stop."
        ),
        (
            "Read-only inspection turns do not reset this counter. Workspace "
            "writes, captures, and successful project commands do."
        ),
    ]
    if remaining <= 2:
        lines.append(
            "The remaining allowance is short; another inspection-only turn "
            "without a workspace change will consume one of the turns above."
        )
    lines.append(_gate_evidence_status(completed_commands, required_gates))
    if nonproductive_turns >= FINISH_HINT_AFTER_NONPRODUCTIVE_TURNS and _task_done_checks(
        completed_commands, required_gates
    ):
        lines.append(
            "The done condition appears satisfiable from that evidence; the "
            "expected next action is to call `finish` with a truthful summary. "
            "The completion gate will independently accept or refuse it."
        )
    lines.append(
        f"Rewriting the same file without running a command is bounded at "
        f"{MAX_FILE_REWRITES_WITHOUT_COMMAND} rewrites. A write made only to reset "
        "the progress counter still reaches that bound and stops the task."
    )
    if inspected_paths:
        lines.append("Already inspected paths: " + ", ".join(inspected_paths))
    if command_outcomes:
        lines.append("Commands already run: " + ", ".join(command_outcomes))
    return "\n".join(lines)


def _replace_model_progress_status(
    messages: list[dict[str, object]], status: str
) -> None:
    messages[:] = [
        message
        for message in messages
        if not (
            message.get("role") == "user"
            and str(message.get("content", "")).startswith(
                "PROGRESS STATUS (system fact):"
            )
        )
    ]
    messages.append({"role": "user", "content": status})


def _tool_counts_as_progress(tool: str, command: str | None = None) -> bool:
    """Successful workspace-affecting tools reset the model stall budget."""
    if tool in {"write_file", "capture_screenshot"}:
        return True
    return tool == "run_command" and command in MODEL_COMMANDS


def _missing_gate_evidence(
    completed_commands: set[str],
    required_gates: tuple[str, ...] = REQUIRED_GATE_COMMANDS,
) -> list[str]:
    return [name for name in required_gates if name not in completed_commands]


def _gate_evidence_status(
    completed_commands: set[str],
    required_gates: tuple[str, ...] = REQUIRED_GATE_COMMANDS,
) -> str:
    missing = _missing_gate_evidence(completed_commands, required_gates)
    if missing:
        return "Current successful gate evidence is missing: " + ", ".join(missing) + "."
    return (
        "Current successful gate evidence is complete ("
        + ", ".join(required_gates)
        + ")."
    )


def _gate_refusal(
    completed_commands: set[str],
    stale_reason: str | None,
    required_gates: tuple[str, ...] = REQUIRED_GATE_COMMANDS,
) -> str:
    missing = _missing_gate_evidence(completed_commands, required_gates)
    if not missing:
        raise GateEvidenceContradiction(
            "gate refusal requested with complete required gate evidence"
        )
    detail = (
        "missing current successful gates: "
        + ", ".join(missing)
        if missing
        else "current successful gates are incomplete"
    )
    if stale_reason:
        detail += f"; {stale_reason}"
    return f"TOOL FAILURE: done condition requires {detail}; run those gates next"


def _gate_refusal_progress(
    completed_commands: set[str],
    stale_reason: str | None,
    previous_missing: frozenset[str] | None,
    required_gates: tuple[str, ...] = REQUIRED_GATE_COMMANDS,
) -> tuple[str, bool, frozenset[str]]:
    missing = frozenset(_missing_gate_evidence(completed_commands, required_gates))
    refusal = _gate_refusal(completed_commands, stale_reason, required_gates)
    repeated = previous_missing == missing
    return refusal, not repeated, missing


def _model_response_failure(completion: ModelCompletion) -> str | None:
    if completion.finish_reason == "length":
        detail = (
            "model response exceeded the output limit before a complete tool call; "
            f"the {CODER_MAX_OUTPUT_TOKENS}-token ceiling was reached, so split "
            "large file writes across multiple tool calls and be brief"
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


def _run_command_was_executed(
    tool: str, arguments: Mapping[str, object]
) -> bool:
    return (
        tool == "run_command"
        and str(arguments.get("name", "")) in MODEL_COMMANDS
        and arguments.get("args", []) in ([], None)
    )


def _reset_file_write_counters(
    tool: str,
    arguments: Mapping[str, object],
    total: int,
    per_file: dict[str, int],
) -> tuple[int, dict[str, int]]:
    if not _run_command_was_executed(tool, arguments):
        return total, per_file
    return 0, {}


def _model_tool_progress_detail(tool: str, arguments: Mapping[str, object]) -> str:
    if tool in {"read_file", "write_file", "list_files"}:
        target = str(arguments.get("path", "."))
    elif tool == "run_command":
        target = str(arguments.get("name", ""))
    elif tool == "capture_screenshot":
        target = str(arguments.get("route", "/"))
    else:
        target = str(arguments.get("summary", ""))
    return f"{tool}: {target}"[:1000]


def _utf8_safe_prefix(content: bytes) -> int:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data" and exc.end == len(content):
            return exc.start
        return len(content)
    return len(content)


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
    model_tool_calls: int = MODEL_TOOL_CALL_BUDGET
    model_tokens: int = 500000
    model_write_bytes: int = 2 * 1024 * 1024
    model_spend_usd: float = 0.75
    image_spend_usd: float = 0.10
    image_request_count: int = 12
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
            "image_spend_usd": self.image_spend_usd,
            "image_request_count": self.image_request_count,
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
            model_tool_calls=int(
                cast(int, values.get("model_tool_calls", MODEL_TOOL_CALL_BUDGET))
            ),
            model_tokens=int(cast(int, values.get("model_tokens", 500000))),
            model_write_bytes=int(cast(int, values.get("model_write_bytes", 2 * 1024 * 1024))),
            model_spend_usd=float(cast(float, values.get("model_spend_usd", 0.75))),
            image_spend_usd=float(cast(float, values.get("image_spend_usd", 0.10))),
            image_request_count=int(cast(int, values.get("image_request_count", 12))),
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
    "poster-export": (
        "poster-export",
        (
            "python3",
            "/opt/validate_poster.py",
        ),
        "none",
    ),
    "generate-images": (
        "generate-images",
        ("sh", "-c", "test -f /workspace/image_manifest.json"),
        "none",
    ),
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
        self._live_output_degraded: set[int] = set()
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

    def _raise_if_cancelled(self, run_id: int) -> None:
        if self._is_cancelled(run_id):
            raise RunCancelled

    def _is_cancelled(self, run_id: int) -> bool:
        return run_id in self._cancelled

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
            await self._event(run_id, "running", "run started")
            if self._is_cancelled(run_id):
                await self._event(run_id, "cancelled", "cancelled before operation")
                return
            async with self.database.sessions() as session:
                run_result = await session.execute(
                    runner_sql(text("SELECT job_type, job_config FROM worker_runs WHERE id = :run_id")),
                    {"run_id": run_id},
                )
                run_row = run_result.mappings().one()
            job_type = str(run_row["job_type"])
            policy = policy_for(job_type)
            job_config = (
                poster_config(run_row["job_config"]) if policy.is_poster else {}
            )
            brand_profile = None
            if policy.is_poster:
                async with self.database.sessions() as session:
                    brand_profile = await get_brand_profile(session, revision.namespace)
                if brand_profile is None:
                    raise RuntimeError(
                        "poster run refused during dispatch: no brand profile is "
                        "recorded for this namespace"
                    )
            await self._mount_workspace(workspace, limits)
            if self.model_provider is not None:
                await self._dispatch_model_one(
                    revision, run_id, limits, workspace, policy, job_config, brand_profile
                )
                return
            for index, operation in enumerate(
                fixed_operations(revision, policy, job_config)
            ):
                if self._is_cancelled(run_id):
                    await self._event(run_id, "cancelled", "cancelled before operation")
                    return
                await self._event(
                    run_id, "operation_running", operation.name,
                    operation_index=index, task_id=operation.task_id,
                )
                await self._task_event(run_id, operation.task_id, "running", operation.name)
                started = datetime.now(UTC)
                command = self._docker_command(
                    operation, workspace, run_id, limits, job_config, brand_profile
                )
                try:
                    result, stdout, stderr = await self._run_container(
                        run_id, command, limits, operation_index=index
                    )
                except TimeoutError:
                    await self.cancel(run_id)
                    await self._operation(
                        run_id, operation, index, "failed", "",
                        "worker exceeded wall-clock timeout", 124, started,
                    )
                    await self._event(run_id, "failed", "worker exceeded wall-clock timeout")
                    return
                if self._is_cancelled(run_id):
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
        except RunCancelled:
            await self._event(run_id, "cancelled", "worker stopped by cancellation")
            raise
        except Exception as exc:
            detail = str(exc).strip()[:2000] or exc.__class__.__name__
            await self._event(run_id, "failed", detail)
            raise
        finally:
            try:
                await self._cleanup_workspace(workspace)
            except Exception as exc:
                await self._record_cleanup_failure(
                    str(run_id),
                    f"workspace cleanup failed: {str(exc)[:1000]}",
                )

    async def _dispatch_model_one(
        self,
        revision: PlanRevision,
        run_id: int,
        limits: WorkerLimits,
        workspace: Path,
        policy: JobTypePolicy,
        job_config: dict[str, object],
        brand_profile: BrandProfile | None,
    ) -> None:
        assert self.model_provider is not None
        started = time.monotonic()
        (workspace / "artifacts").mkdir(parents=True, exist_ok=True)
        init = FixedOperation("runner", "git-init", ("sh", "-c", "git init -q /workspace"))
        init_result, _init_out, init_err = await self._run_container(
            run_id, self._docker_command(init, workspace, run_id, limits), limits,
            operation_index=0,
        )
        if init_result.returncode:
            raise RuntimeError(init_err[-1000:] or "git initialization failed")
        await self._operation(
            run_id, init, 0, "passed", _init_out, init_err,
            init_result.returncode, datetime.now(UTC),
        )
        fixture_command = (
            "mkdir -p /workspace/out /workspace/artifacts"
            if policy.is_poster
            else "cp -r /opt/fixture/. /workspace/ && mkdir -p /workspace/artifacts"
        )
        fixture = FixedOperation(
            "runner",
            "write-fixture",
            ("sh", "-c", fixture_command),
        )
        fixture_result, fixture_out, fixture_err = await self._run_container(
            run_id, self._docker_command(fixture, workspace, run_id, limits), limits,
            operation_index=1,
        )
        if fixture_result.returncode:
            raise RuntimeError(fixture_err[-1000:] or "fixture initialization failed")
        await self._operation(
            run_id, fixture, 1, "passed", fixture_out, fixture_err,
            fixture_result.returncode, datetime.now(UTC),
        )
        self._raise_if_cancelled(run_id)
        starter_context = _starter_context(workspace)
        async with self.database.sessions() as session:
            result = await session.execute(
                runner_sql(text(
                    "SELECT d.decision_key, d.decision FROM decisions d "
                    "LEFT JOIN decision_forgets f ON f.decision_id = d.id "
                    "WHERE d.superseded_by IS NULL AND f.id IS NULL ORDER BY d.id"
                )),
            )
            beliefs = [dict(row._mapping) for row in result]
        stable = _model_system_prompt(policy, brand_profile, job_config)
        spent = 0.0
        spent_tokens = 0
        calls = 0
        tool_calls_used = 0
        writes = 0
        operation_index = 1
        completed_commands: set[str] = set()
        visual_state: VisualState = {
            "cycles": 0,
            "cost": 0.0,
            "tokens": 0,
            "accounted_cost": 0.0,
            "accounted_tokens": 0,
            "last_failed_digest": None,
            "source_digest": None,
            "review_passed": False,
            "review_task_id": None,
        }
        visual_brief = "\n".join(
            [revision.document.title]
            + [f"{item.title}: {item.description}" for item in revision.document.tasks]
        )

        def account_visual_spend() -> None:
            nonlocal spent, spent_tokens
            visual_cost = visual_state["cost"] - visual_state["accounted_cost"]
            visual_tokens = visual_state["tokens"] - visual_state["accounted_tokens"]
            if not visual_cost and not visual_tokens:
                return
            spent += visual_cost
            spent_tokens += visual_tokens
            visual_state["accounted_cost"] = visual_state["cost"]
            visual_state["accounted_tokens"] = visual_state["tokens"]
            if spent > limits.model_spend_usd:
                raise RunBudgetExceeded("model spend")
            if spent_tokens > limits.model_tokens:
                raise RunBudgetExceeded("model token")

        for task in revision.document.tasks:
            await self._task_event(run_id, task.id, "running", task.title)
            file_write_counts: dict[str, int] = {}
            file_writes_without_command = 0
            nonproductive_turns = 0
            gate_stale_reason: str | None = None
            last_refused_missing_gates: frozenset[str] | None = None
            inspected_paths: list[str] = []
            command_outcomes: list[str] = []
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

            async def record_nonproductive(
                detail: str,
                *,
                workspace_changed: bool = False,
                task_id: str = task_id,
            ) -> None:
                nonlocal failures, nonproductive_turns
                failures, nonproductive_turns = _progress_counters(
                    failures,
                    nonproductive_turns,
                    workspace_changed=workspace_changed,
                    failure=not workspace_changed,
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

            def remember_tool_result(
                tool: str,
                arguments: dict[str, object],
                *,
                succeeded: bool,
                inspected_paths: list[str] = inspected_paths,
                command_outcomes: list[str] = command_outcomes,
            ) -> None:
                if tool in {"read_file", "list_files"}:
                    path = str(arguments.get("path", ""))
                    if path:
                        _remember_progress_ledger(
                            inspected_paths,
                            f"{tool} {path} ({'passed' if succeeded else 'failed'})",
                        )
                elif tool == "run_command":
                    command = str(arguments.get("name", ""))
                    if command:
                        _remember_progress_ledger(
                            command_outcomes,
                            f"{command} ({'passed' if succeeded else 'failed'})",
                        )

            tool_failure_counts: dict[tuple[str, str], int] = {}

            def repeated_tool_failure_detail(
                tool: str,
                detail: str,
                task_id: str = task.id,
                failure_counts: dict[tuple[str, str], int] = tool_failure_counts,
            ) -> str:
                (
                    detail,
                    _count,
                ) = _identical_tool_failure_detail(
                    task_id,
                    tool,
                    detail,
                    failure_counts,
                )
                return detail

            def reset_tool_failure_streak(
                failure_counts: dict[tuple[str, str], int] = tool_failure_counts,
            ) -> None:
                failure_counts.clear()

            for iteration in range(1, limits.model_iterations + 1):
                self._raise_if_cancelled(run_id)
                if time.monotonic() - started > limits.run_timeout_seconds:
                    raise RunBudgetExceeded("model run wall-clock")
                _replace_model_progress_status(
                    messages,
                    _model_progress_context(
                        nonproductive_turns,
                        completed_commands,
                        inspected_paths,
                        command_outcomes,
                        policy.required_gates,
                    ),
                )
                try:
                    completion = await self.model_provider.agent_completion(
                        messages,
                        route,
                        tools=model_tool_schemas() if route in CODER_ROUTES else None,
                        tool_choice="required" if route in CODER_ROUTES else None,
                    )
                except Exception as exc:
                    if self._is_cancelled(run_id):
                        raise RunCancelled from exc
                    detail = _model_call_failure_detail(route, exc)
                    failure = _model_failure_completion(route, exc)
                    if isinstance(exc, ModelProviderError):
                        calls += exc.attempts
                        spent += exc.cost_usd
                        spent_tokens += exc.total_tokens
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
                self._raise_if_cancelled(run_id)
                if spent_tokens > limits.model_tokens:
                    raise RunBudgetExceeded("model token")
                if spent > limits.model_spend_usd:
                    raise RunBudgetExceeded("model spend")
                response_failure = _model_response_failure(completion)
                if response_failure is not None:
                    detail = response_failure
                    await record_nonproductive(detail)
                    if failures >= 2 and route == CODER_ROUTE:
                        route = CODER_FALLBACK_ROUTE
                        await self._event(
                            run_id,
                            "model_route_switched",
                            "switched to coder fallback after two malformed responses",
                            task_id=task.id,
                        )
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
                    batch_refusal_progress = True
                    for call_index, native_call in enumerate(completion.tool_calls):
                        self._raise_if_cancelled(run_id)
                        tool, arguments = native_call.name, native_call.arguments
                        if tool not in model_tool_names():
                            result_text = f"TOOL FAILURE: unknown model tool: {tool}"
                            batch_failure = result_text
                            messages.append(_tool_result_message(native_call, result_text))
                        elif tool_calls_used >= limits.model_tool_calls:
                            raise RunBudgetExceeded("model tool-call")
                        else:
                            tool_calls_used += 1
                            if tool == "finish" and _task_done_checks(
                                completed_commands, policy.required_gates
                            ):
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
                                (
                                    result_text,
                                    refusal_progress,
                                    last_refused_missing_gates,
                                ) = _gate_refusal_progress(
                                    completed_commands,
                                    gate_stale_reason,
                                    last_refused_missing_gates,
                                    policy.required_gates,
                                )
                                batch_refusal_progress = refusal_progress
                                batch_failure = result_text
                                messages.append(_tool_result_message(native_call, result_text))
                            else:
                                try:
                                    await self._event(
                                        run_id,
                                        "model_tool_running",
                                        _model_tool_progress_detail(tool, arguments),
                                        task_id=task.id,
                                    )
                                    result_text, written, operation_index = (
                                        await self._execute_model_tool(
                                            run_id, task.id, operation_index, tool,
                                            arguments, workspace, limits, route,
                                            policy, job_config, brand_profile,
                                            visual_state, visual_brief,
                                        )
                                    )
                                    account_visual_spend()
                                    self._raise_if_cancelled(run_id)
                                    writes += written
                                    if writes > limits.model_write_bytes:
                                        raise RunBudgetExceeded("model write-byte")
                                    remember_tool_result(tool, arguments, succeeded=True)
                                    reset_tool_failure_streak()
                                    if tool == "run_command":
                                        command_name = str(arguments.get("name", ""))
                                        _record_gate_command(completed_commands, command_name)
                                        if command_name == "sync-lockfile":
                                            gate_stale_reason = (
                                                "previous gate evidence was invalidated "
                                                "by sync-lockfile"
                                            )
                                            last_refused_missing_gates = None
                                        elif not _missing_gate_evidence(completed_commands, policy.required_gates):
                                            gate_stale_reason = None
                                        (
                                            file_writes_without_command,
                                            file_write_counts,
                                        ) = _reset_file_write_counters(
                                            tool,
                                            arguments,
                                            file_writes_without_command,
                                            file_write_counts,
                                        )
                                    elif tool == "capture_screenshot":
                                        completed_commands.add("capture_screenshot")
                                    elif tool == "visual_critique":
                                        if result_text.startswith("VISUAL_REVIEW_PASS"):
                                            completed_commands.add("visual-review")
                                            await self._task_event(
                                                run_id, task.id, "passed",
                                                result_text[19:2019],
                                            )
                                            done = True
                                        else:
                                            completed_commands.discard("visual-review")
                                    elif tool == "write_file":
                                        path = str(arguments.get("path", ""))
                                        if _source_path_invalidates_gates(path):
                                            completed_commands.clear()
                                            gate_stale_reason = (
                                                f"previous gate evidence was invalidated "
                                                f"by source change at {path}"
                                            )
                                            last_refused_missing_gates = None
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
                                    if _tool_counts_as_progress(
                                        tool, str(arguments.get("name", ""))
                                    ):
                                        batch_workspace_changed = True
                                    messages.append(
                                        _tool_result_message(
                                            native_call, result_text[:16000]
                                        )
                                    )
                                except Exception as exc:
                                    account_visual_spend()
                                    if self._is_cancelled(run_id):
                                        raise RunCancelled from exc
                                    if isinstance(
                                        exc,
                                        ModelProgressError
                                        | RunBudgetExceeded
                                        | VisualReviewInconclusive,
                                    ):
                                        await self._task_event(
                                            run_id, task.id, "failed", str(exc)[:1000]
                                        )
                                        raise
                                    (
                                        file_writes_without_command,
                                        file_write_counts,
                                    ) = _reset_file_write_counters(
                                        tool,
                                        arguments,
                                        file_writes_without_command,
                                        file_write_counts,
                                    )
                                    remember_tool_result(tool, arguments, succeeded=False)
                                    result_text = repeated_tool_failure_detail(
                                        tool,
                                        f"TOOL FAILURE: {tool}: {str(exc)[:1000]}",
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
                        await record_nonproductive(
                            batch_failure,
                            workspace_changed=(
                                batch_workspace_changed and batch_refusal_progress
                            ),
                        )
                        if failures >= 2 and route == CODER_ROUTE:
                            if "model response was not valid JSON" in batch_failure:
                                route = CODER_FALLBACK_ROUTE
                                await self._event(
                                    run_id,
                                    "model_route_switched",
                                    "switched to coder fallback after two malformed responses",
                                    task_id=task.id,
                                )
                            else:
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
                    raise RunBudgetExceeded("model tool-call")
                tool_calls_used += 1
                if tool == "finish" and _task_done_checks(
                    completed_commands, policy.required_gates
                ):
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
                    (
                        result_text,
                        refusal_progress,
                        last_refused_missing_gates,
                    ) = _gate_refusal_progress(
                        completed_commands,
                        gate_stale_reason,
                        last_refused_missing_gates,
                        policy.required_gates,
                    )
                    await record_nonproductive(
                        result_text, workspace_changed=refusal_progress
                    )
                    messages.extend(_tool_exchange(completion, result_text, native_call))
                    await compact_history()
                    continue
                try:
                    await self._event(
                        run_id,
                        "model_tool_running",
                        _model_tool_progress_detail(tool, arguments),
                        task_id=task.id,
                    )
                    result_text, written, operation_index = await self._execute_model_tool(
                        run_id, task.id, operation_index, tool, arguments,
                        workspace, limits, route, policy, job_config, brand_profile,
                        visual_state, visual_brief,
                    )
                    account_visual_spend()
                    self._raise_if_cancelled(run_id)
                    writes += written
                    if writes > limits.model_write_bytes:
                        raise RunBudgetExceeded("model write-byte")
                    remember_tool_result(tool, arguments, succeeded=True)
                    reset_tool_failure_streak()
                    if tool == "run_command":
                        command_name = str(arguments.get("name", ""))
                        _record_gate_command(completed_commands, command_name)
                        if command_name == "sync-lockfile":
                            gate_stale_reason = (
                                "previous gate evidence was invalidated by sync-lockfile"
                            )
                            last_refused_missing_gates = None
                        elif not _missing_gate_evidence(completed_commands, policy.required_gates):
                            gate_stale_reason = None
                        (
                            file_writes_without_command,
                            file_write_counts,
                        ) = _reset_file_write_counters(
                            tool,
                            arguments,
                            file_writes_without_command,
                            file_write_counts,
                        )
                    elif tool == "capture_screenshot":
                        completed_commands.add("capture_screenshot")
                    elif tool == "visual_critique":
                        if result_text.startswith("VISUAL_REVIEW_PASS"):
                            completed_commands.add("visual-review")
                            await self._task_event(
                                run_id, task.id, "passed", result_text[19:2019]
                            )
                            done = True
                        else:
                            completed_commands.discard("visual-review")
                    elif tool == "write_file":
                        path = str(arguments.get("path", ""))
                        if _source_path_invalidates_gates(path):
                            completed_commands.clear()
                            gate_stale_reason = (
                                f"previous gate evidence was invalidated "
                                f"by source change at {path}"
                            )
                            last_refused_missing_gates = None
                        file_writes_without_command += 1
                        file_write_counts[path] = file_write_counts.get(path, 0) + 1
                        stall = _file_write_stall(
                            path, file_write_counts[path], file_writes_without_command
                        )
                        if stall is not None:
                            raise ModelProgressError(
                                f"task {task.id} stopped: {stall}"
                            )
                    if _tool_counts_as_progress(
                        tool, str(arguments.get("name", ""))
                    ):
                        reset_progress_counters()
                    else:
                        await record_inspection_turn()
                except Exception as exc:
                    account_visual_spend()
                    if self._is_cancelled(run_id):
                        raise RunCancelled from exc
                    if isinstance(
                        exc,
                        ModelProgressError
                        | RunBudgetExceeded
                        | VisualReviewInconclusive,
                    ):
                        await self._task_event(
                            run_id, task.id, "failed", str(exc)[:1000]
                        )
                        raise
                    (
                        file_writes_without_command,
                        file_write_counts,
                    ) = _reset_file_write_counters(
                        tool,
                        arguments,
                        file_writes_without_command,
                        file_write_counts,
                    )
                    remember_tool_result(tool, arguments, succeeded=False)
                    result_text = repeated_tool_failure_detail(
                        tool, f"TOOL FAILURE: {tool}: {str(exc)[:1000]}"
                    )
                    await record_nonproductive(result_text)
                    if failures >= 2 and route == CODER_ROUTE:
                        if "model response was not valid JSON" in result_text:
                            route = CODER_FALLBACK_ROUTE
                            await self._event(
                                run_id,
                                "model_route_switched",
                                "switched to coder fallback after two malformed responses",
                                task_id=task.id,
                            )
                        else:
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
        operation_index += 1
        diff_result, _diff_out, diff_err = await self._run_container(
            run_id, self._docker_command(diff, workspace, run_id, limits), limits,
            operation_index=operation_index,
        )
        if diff_result.returncode:
            raise RuntimeError(diff_err[-1000:] or "git diff failed")
        if policy.is_poster:
            verify_operation = FixedOperation(
                "runner",
                "poster-export-assets",
                (),
            )
            await self._verify_poster_assets(
                run_id, workspace, verify_operation, operation_index + 1
            )
        await self._operation(
            run_id, diff, operation_index, "passed", _diff_out, diff_err,
            diff_result.returncode, datetime.now(UTC),
        )
        await self._capture_workspace_artifacts(run_id, workspace, limits)
        await self._review_run(
            run_id, revision, limits, spent, spent_tokens, calls, workspace, policy
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
            if revision.job_type == "poster":
                verify_operation = FixedOperation(
                    "runner",
                    "poster-preview-assets",
                    (),
                )
                await self._verify_poster_assets(
                    run_id, workspace, verify_operation, 0
                )
            manifest = await asyncio.to_thread(copy_export, export_root, staging)
            async with self.database.sessions() as session:
                artifacts = await session.execute(
                    runner_sql(text(
                        "SELECT id, kind, sha256 FROM worker_artifacts "
                        "WHERE run_id = :run_id AND kind IN ('diff', 'reviewer_report') "
                        "ORDER BY id"
                    )),
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
                    runner_sql(text(
                        "INSERT INTO export_manifests "
                        "(run_id, revision_id, revision_content_hash, "
                        "reviewer_artifact_id, diff_artifact_id, manifest, digest, "
                        "total_bytes, file_count, max_depth, staging_path) VALUES "
                        "(:run_id, :revision_id, :revision_hash, :reviewer, :diff, "
                        "CAST(:manifest AS json), :digest, :total_bytes, :file_count, "
                        ":max_depth, :staging_path)"
                    )),
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
        policy: JobTypePolicy = WEBSITE_POLICY,
        job_config: dict[str, object] | None = None,
        brand_profile: BrandProfile | None = None,
        visual_state: VisualState | None = None,
        visual_brief: str = "",
    ) -> tuple[str, int, int]:
        job_config = job_config or {}
        if tool == "list_files":
            path = _confined_path(workspace, str(arguments.get("path", ".")))
            return json.dumps(sorted(item.name for item in path.iterdir())[:200]), 0, operation_index
        if tool == "read_file":
            path = _confined_path(workspace, str(arguments.get("path", "")))
            maximum = min(int(cast(int, arguments.get("max_bytes", 65536))), 65536)
            return path.read_bytes()[:maximum].decode("utf-8", errors="replace"), 0, operation_index
        if tool == "visual_critique":
            if not policy.is_poster:
                raise ValueError("visual critique is only available to poster jobs")
            if route != CODER_ROUTE:
                raise ValueError("only the coder route may request visual critique")
            if visual_state is None:
                raise ValueError("visual critique state was not initialized")
            return await self._visual_critique(
                run_id,
                task_id,
                operation_index,
                workspace,
                limits,
                visual_state,
                visual_brief,
                brand_profile,
                job_config,
            )
        if tool == "write_file":
            if route != CODER_ROUTE:
                raise ValueError("reviewer route cannot write files")
            if (
                policy.is_poster
                and visual_state is not None
                and visual_state.get("review_passed", False)
            ):
                raise ValueError(
                    "poster workspace is frozen after a passing visual critique; "
                    "run poster-export and capture_screenshot to deliberately start "
                    "a new repair cycle before editing"
                )
            path = _confined_path(workspace, str(arguments.get("path", "")))
            content = str(arguments.get("content", ""))
            encoded = content.encode()
            if len(encoded) > limits.model_write_bytes:
                raise ValueError(
                    "single write exceeds model write-byte budget; "
                    "split the file into smaller writes"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encoded)
            return f"wrote {len(encoded)} bytes", len(encoded), operation_index
        if tool == "capture_screenshot":
            route_value = str(arguments.get("route", "/"))
            width = int(cast(int, arguments.get("width", 390)))
            height = int(cast(int, arguments.get("height", 900)))
            scale = int(cast(int, arguments.get("scale", 1)))
            _validate_screenshot_request(
                policy, route_value, width, height, scale, job_config
            )
            if policy.is_poster and visual_state is not None:
                _, _, operation_index = await self._execute_model_tool(
                    run_id,
                    task_id,
                    operation_index,
                    "run_command",
                    {"name": "poster-export", "args": []},
                    workspace,
                    limits,
                    route,
                    policy,
                    job_config,
                    brand_profile,
                    visual_state,
                    visual_brief,
                )
            if policy.is_poster:
                requested = poster_config(
                    {**job_config, "width": width, "height": height, "scale": scale}
                )
                capture_args = (
                    f"--width {requested['width']} --height {requested['height']} "
                    f"--scale {requested['scale']} "
                )
                capture_command = (
                    "test -f \"out/$CHITTI_POSTER_ARTIFACT\" && "
                    "if [ \"$CHITTI_POSTER_ARTIFACT\" = \"index.html\" ] || "
                    "grep -F -- \"$CHITTI_POSTER_ARTIFACT\" out/index.html; then "
                    "python3 /opt/next_screenshot.py "
                    + capture_args
                    + f"--poster --artifact {shlex.quote(str(requested['artifact']))}; "
                    "else echo \"poster capture requires out/index.html to reference "
                    "the declared artifact $CHITTI_POSTER_ARTIFACT\" >&2; exit 1; fi"
                )
            else:
                capture_command = "python3 /opt/next_screenshot.py"
            operation = FixedOperation(
                task_id, "capture-screenshot", ("sh", "-c", capture_command)
            )
            result, stdout, stderr = await self._run_container(
                run_id,
                self._docker_command(
                    operation, workspace, run_id, limits, job_config, brand_profile
                ),
                limits,
                operation_index=operation_index + 1,
            )
            operation_index += 1
            if policy.is_poster and result.returncode == 0:
                await self._verify_poster_assets(
                    run_id, workspace, operation, operation_index, stdout
                )
            await self._operation(
                run_id, operation, operation_index,
                "passed" if result.returncode == 0 else "failed",
                stdout, stderr, result.returncode, datetime.now(UTC),
            )
            if result.returncode:
                await self._capture_workspace_artifacts(run_id, workspace, limits)
                raise RuntimeError(stderr[-1000:] or "screenshot failed")
            await self._capture_workspace_artifacts(
                run_id, workspace, limits, include_diff=False
            )
            if policy.is_poster and visual_state is not None:
                artifact = str(poster_config(job_config)["artifact"])
                source_path = workspace / "out" / artifact
                visual_state["source_digest"] = hashlib.sha256(
                    source_path.read_bytes()
                ).hexdigest()
            return stdout[-4000:] or "screenshots captured in artifacts/", 0, operation_index
        if tool == "run_command":
            name = str(arguments.get("name", ""))
            if arguments.get("args", []) not in ([], None):
                raise ValueError("arbitrary command arguments are not allowed")
            if name not in policy.model_commands:
                raise ValueError("unknown allowlisted command")
            if name == "generate-images":
                if not policy.is_poster:
                    raise ValueError("generated images are only available to poster jobs")
                started = datetime.now(UTC)
                operation = FixedOperation(task_id, "generate-images", MODEL_COMMANDS[name][1])
                await self._event(
                    run_id, "operation_running", operation.name,
                    operation_index=operation_index + 1, task_id=task_id,
                )
                try:
                    detail = await generate_manifest_images(
                        self.database, run_id, workspace, limits
                    )
                except ImageManifestRefused as exc:
                    await self._operation(
                        run_id, operation, operation_index + 1, "failed", "",
                        str(exc)[:2000], 1, started,
                    )
                    raise
                except (
                    ImageBudgetExceeded,
                    ImageProviderFailure,
                    ImageProviderUnavailable,
                ) as exc:
                    await self._operation(
                        run_id, operation, operation_index + 1, "failed", "",
                        str(exc)[:2000], 1, started,
                    )
                    raise RunBudgetExceeded("image", str(exc)) from exc
                except Exception as exc:
                    await self._operation(
                        run_id, operation, operation_index + 1, "failed", "",
                        str(exc)[:2000], 1, started,
                    )
                    raise
                operation_index += 1
                await self._operation(
                    run_id, operation, operation_index, "passed", detail, "",
                    0, started,
                )
                await self._capture_generated_images(run_id, workspace, limits)
                return detail, 0, operation_index
            op_name, command, network = MODEL_COMMANDS[name]
            operation = FixedOperation(task_id, op_name, command, network=network)
            if name == "poster-export":
                await self._verify_poster_assets(
                    run_id, workspace, operation, operation_index + 1
                )
            result, stdout, stderr = await self._run_container(
                run_id,
                self._docker_command(
                    operation, workspace, run_id, limits, job_config, brand_profile
                ),
                limits,
                operation_index=operation_index + 1,
            )
            operation_index += 1
            status = "passed" if result.returncode == 0 else "failed"
            if name == "poster-export" and result.returncode == 0:
                await self._verify_poster_assets(
                    run_id, workspace, operation, operation_index, stdout
                )
                if visual_state is not None:
                    visual_state["review_passed"] = False
                    visual_state["review_task_id"] = None
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

    async def _visual_critique(
        self,
        run_id: int,
        task_id: str,
        iteration: int,
        workspace: Path,
        limits: WorkerLimits,
        visual_state: VisualState,
        brief: str,
        brand_profile: BrandProfile | None,
        job_config: dict[str, object] | None = None,
    ) -> tuple[str, int, int]:
        job_config = job_config or {}
        if visual_state["cycles"] >= VISUAL_REVIEW_MAX_CYCLES:
            raise VisualReviewInconclusive(
                f"visual review exceeded {VISUAL_REVIEW_MAX_CYCLES} critique cycles"
            )
        image_path = workspace / "artifacts" / "poster.png"
        if not image_path.is_file():
            raise ValueError("visual critique requires a captured artifacts/poster.png")
        image = image_path.read_bytes()
        if not image:
            raise ValueError("visual critique requires a non-empty poster PNG")
        if len(image) > 5 * 1024 * 1024:
            raise VisualReviewInconclusive("visual critique PNG exceeds provider size limit")
        image_digest = hashlib.sha256(image).hexdigest()
        artifact = str(poster_config(job_config)["artifact"])
        source_path = workspace / "out" / artifact
        source_digest = visual_state.get("source_digest")
        if source_digest is not None and source_digest != hashlib.sha256(
            source_path.read_bytes()
        ).hexdigest():
            raise ValueError(
                "visual critique requires a fresh capture after the poster source "
                "changed; run poster-export and capture_screenshot again"
            )
        width, height = _png_dimensions(image)
        generated_asset_count = len(list((workspace / "out" / "generated").glob("*.png")))
        profile_fields: dict[str, object] = (
            {
                "brand_colors": brand_profile.brand_colors,
                "typography": brand_profile.typography,
                "poster_formats": brand_profile.poster_formats,
                "do_not_use": brand_profile.do_not_use,
            }
            if brand_profile is not None
            else {}
        )
        profile = json.dumps(profile_fields)
        job_config = job_config or {}
        likeness_policy = (
            "Likeness policy: real player likenesses, real kits, and real faces "
            "are permitted by the owner."
            if job_config.get("likeness_policy") == "real_likeness_permitted"
            else "Likeness policy: use generic figures; real player likenesses, "
            "real kits, and real faces are not permitted."
        )
        research_facts = job_config.get("research_facts")
        review_instruction = VISUAL_REVIEW_INSTRUCTION
        messages: list[dict[str, object]] = [
            {"role": "system", "content": review_instruction},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"POSTER BRIEF:\n{brief}\n\nBRAND PROFILE:\n{profile}\n\n"
                            f"IMAGE SHA-256: {image_digest}\n"
                            f"IMAGE DIMENSIONS: {width}x{height}\n"
                            f"GENERATED ASSET COUNT: {generated_asset_count}\n"
                            f"APPROVED RESEARCH FACTS:\n"
                            f"{json.dumps(research_facts, sort_keys=True)}\n"
                            "When approved research facts are supplied, judge "
                            "fixture_text against those facts exactly; do not use "
                            "the prose brief as a substitute source of truth.\n"
                            f"{likeness_policy}\n"
                            "Judge this exact captured PNG against the brief."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": (
                                "data:image/png;base64,"
                                + base64.b64encode(image).decode("ascii")
                            )
                        },
                    },
                ],
            },
        ]
        persisted_messages = [
            messages[0],
            {
                "role": "user",
                "content": (
                    f"POSTER BRIEF:\n{brief}\n\nBRAND PROFILE:\n{profile}\n\n"
                    f"IMAGE PATH: artifacts/poster.png\n"
                    f"IMAGE SHA-256: {image_digest}\n"
                    f"IMAGE BYTE SIZE: {len(image)}\n"
                    f"IMAGE DIMENSIONS: {width}x{height}\n"
                    f"GENERATED ASSET COUNT: {generated_asset_count}\n"
                    f"{likeness_policy}\n"
                    "The PNG bytes were sent ephemerally and are not stored in this prompt."
                ),
            },
        ]
        assert self.model_provider is not None
        compliance_reask = False
        review_routes = (VISION_ROUTE, VISION_FALLBACK_ROUTE)
        route_index = 0
        prompt_messages = persisted_messages
        while True:
            try:
                completion = await self.model_provider.agent_completion(
                    messages, review_routes[route_index]
                )
            except ModelTransportError as exc:
                raise VisualReviewInconclusive(
                    f"visual critique provider accounting failed: {exc}"
                ) from exc
            visual_state["cost"] += completion.cost_usd
            visual_state["tokens"] += completion.total_tokens
            if visual_state["cost"] > VISUAL_REVIEW_SPEND_CAP_USD:
                raise RunBudgetExceeded("visual model spend")
            if "data:image" in completion.content.lower() or "base64" in completion.content.lower():
                raise VisualReviewInconclusive(
                    "visual critique response contained image data"
                )
            await self._record_model_call(
                run_id,
                task_id,
                iteration,
                review_routes[route_index],
                completion,
                kind="visual_critique",
                prompt=json.dumps(prompt_messages, separators=(",", ":")),
            )
            if completion.finish_reason == "length" or any(
                item == "response_failure_class=output limit"
                for item in completion.message_fields
            ):
                diagnostics = "; ".join(completion.response_diagnostics) or "none retained"
                raise VisualReviewInconclusive(
                    "visual critique response exceeded the output limit before a "
                    "complete JSON verdict; increase concision and return exactly "
                    "the required rubric fields; diagnostics: "
                    f"{diagnostics}"
                )
            try:
                verdict = json.loads(completion.content)
                parsed = _parse_visual_verdict(verdict, image_digest)
            except json.JSONDecodeError as exc:
                parse_error = VisualReviewInconclusive(
                    f"visual critique returned invalid JSON: {exc}"
                )
            except VisualReviewInconclusive as exc:
                parse_error = exc
            else:
                break
            if route_index == 0:
                route_index = 1
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "The primary vision response was empty or structurally "
                            "unparseable. Return one complete JSON verdict through "
                            "the independent fallback route."
                        ),
                    }
                )
                prompt_messages = [*persisted_messages, *messages[2:]]
                continue
            if compliance_reask:
                raise parse_error
            compliance_reask = True
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous visual verdict was structurally non-compliant: "
                        f"{parse_error}. Re-ask: return exactly one compliant JSON "
                        "object with every required field, every failed criterion "
                        "paired with a concrete finding, and no markdown."
                    ),
                }
            )
            prompt_messages = [*persisted_messages, *messages[2:]]
        visual_state["cycles"] += 1
        if generated_asset_count == 0 and parsed["verdict"] == "pass":
            parsed = {
                **parsed,
                "verdict": "fail",
                "criteria": {
                    **cast(dict[str, str], parsed["criteria"]),
                    "generated_imagery": "fail",
                },
                "findings": [
                    *cast(list[dict[str, str]], parsed["findings"]),
                    {
                        "criterion": "generated_imagery",
                        "issue": "No authoritative generated image asset was present.",
                        "action": "Use generate-images and compose one of its assets into the poster.",
                    },
                ],
                "summary": "Generated imagery is required but no generated asset was present.",
            }
        if parsed["verdict"] == "fail":
            if visual_state["last_failed_digest"] == image_digest:
                raise VisualReviewInconclusive(
                    "visual critique failed without a new rendered image digest"
                )
            visual_state["last_failed_digest"] = image_digest
            if visual_state["cycles"] >= VISUAL_REVIEW_MAX_CYCLES:
                raise VisualReviewInconclusive(
                    "visual critique remained failing after the maximum repair cycles"
                )
            await self._event(
                run_id,
                "visual_review_failed",
                json.dumps(parsed, separators=(",", ":"))[:4000],
                task_id=task_id,
            )
            return "VISUAL_REVIEW_FAIL\n" + json.dumps(parsed), 0, iteration
        await self._event(
            run_id,
            "visual_review_passed",
            json.dumps(parsed, separators=(",", ":"))[:4000],
            task_id=task_id,
        )
        visual_state["review_passed"] = True
        visual_state["review_task_id"] = task_id
        return "VISUAL_REVIEW_PASS\n" + json.dumps(parsed), 0, iteration

    async def _review_run(
        self, run_id: int, revision: PlanRevision, limits: WorkerLimits,
        spent: float, spent_tokens: int, calls: int, workspace: Path,
        policy: JobTypePolicy = WEBSITE_POLICY,
    ) -> None:
        assert self.model_provider is not None
        evidence = await self._review_evidence(run_id, workspace)
        if policy.is_poster:
            async with self.database.sessions() as session:
                profile = await get_brand_profile(session, revision.namespace)
            evidence += "\nAUTHORITATIVE BRAND PROFILE:\n" + json.dumps(
                getattr(profile, "__dict__", {}), default=str
            )
        review_messages: list[dict[str, object]] = [
            {"role": "system", "content": _reviewer_system_prompt(policy)},
            {
                "role": "user",
                "content": (
                    f"Review completed run for {revision.document.title}.\n"
                    "Review only the evidence below. Return a structured verdict "
                    "with verdict (pass or fail), findings (specific observations), "
                    "evidence_limitations, and summary. Do not claim to inspect "
                    "pixels or image contents; image dimensions and browser errors "
                    "are the available screenshot facts. For a poster, explicitly "
                    "state that visual quality was not assessed and that owner "
                    "approval is required for visual quality.\n\n"
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
            raise RunBudgetExceeded("model token")
        if spent + completion.cost_usd > limits.model_spend_usd:
            raise RunBudgetExceeded("model spend")
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
                runner_sql(text(
                    "SELECT name, status, stdout, stderr, exit_code "
                    "FROM worker_operations WHERE run_id = :run_id "
                    "ORDER BY operation_index"
                )),
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
                    "poster-export",
                    "capture-screenshot",
                    "git-diff",
                }
            ][-20:]
        facts: list[dict[str, object]] = []
        for name in (
            "phone.png",
            "desktop.png",
            "poster.png",
            "browser-errors.json",
            "workspace.diff",
        ):
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
        generated_root = workspace / "out" / "generated"
        if generated_root.is_dir():
            for source in sorted(generated_root.glob("*")):
                if not source.is_file():
                    continue
                raw = source.read_bytes()
                item = {
                    "path": str(source.relative_to(workspace)),
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
                if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
                    item["dimensions"] = {
                        "width": int.from_bytes(raw[16:20], "big"),
                        "height": int.from_bytes(raw[20:24], "big"),
                    }
                facts.append(item)
        for source in sorted((workspace / "out").rglob("*")) if (workspace / "out").is_dir() else ():
            if source.is_file() and source.suffix.lower() in {".html", ".css", ".svg"}:
                facts.append(
                    {
                        "path": str(source.relative_to(workspace)),
                        "source_excerpt": source.read_text(
                            encoding="utf-8", errors="replace"
                        )[:12000],
                    }
                )
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
        if completion.response_diagnostics:
            response_content = json.dumps(
                {
                    "content": completion.content,
                    "diagnostics": list(completion.response_diagnostics),
                },
                separators=(",", ":"),
            )
        content, content_size, content_truncated = _bounded_artifact(response_content)
        async with self.database.sessions() as session:
            result = await session.execute(
                runner_sql(text(
                    "INSERT INTO worker_model_calls "
                    "(run_id, task_id, iteration, route, model, prompt_tokens, "
                    "completion_tokens, total_tokens, reasoning_tokens, cost_usd, "
                    "finish_reason, "
                    "message_fields) VALUES "
                    "(:run_id, :task_id, :iteration, :route, :model, :prompt_tokens, "
                    ":completion_tokens, :total_tokens, :reasoning_tokens, :cost_usd, "
                    ":finish_reason, "
                    "CAST(:message_fields AS jsonb)) RETURNING id"
                )),
                {
                    "run_id": run_id, "task_id": task_id, "iteration": iteration,
                    "route": route, "model": completion.model,
                    "prompt_tokens": completion.prompt_tokens,
                    "completion_tokens": completion.completion_tokens,
                    "total_tokens": completion.total_tokens,
                    "reasoning_tokens": completion.reasoning_tokens,
                    "cost_usd": completion.cost_usd,
                    "finish_reason": completion.finish_reason,
                    "message_fields": json.dumps(
                        completion.message_fields
                        + _retry_evidence(
                            completion.attempts,
                            completion.retry_failures,
                            completion.retry_total_tokens,
                        )
                    ),
                },
            )
            call_id = int(result.scalar_one())
            artifact = await session.execute(
                runner_sql(text(
                    "INSERT INTO worker_artifacts "
                    "(run_id, kind, path, sha256, byte_size, original_byte_size, truncated) "
                    "VALUES (:run_id, :kind, :path, :sha256, :byte_size, "
                    ":original_byte_size, :truncated) RETURNING id"
                )),
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
                runner_sql(text(
                    "INSERT INTO worker_artifact_payloads (artifact_id, content) "
                    "VALUES (:artifact_id, :content)"
                )),
                {"artifact_id": int(artifact.scalar_one()), "content": content},
            )
            prompt_artifact = await session.execute(
                runner_sql(text(
                    "INSERT INTO worker_artifacts "
                    "(run_id, kind, path, sha256, byte_size, original_byte_size, truncated) "
                    "VALUES (:run_id, 'model_prompt', :path, :sha256, :byte_size, "
                    ":original_byte_size, :truncated) "
                    "RETURNING id"
                )),
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
                runner_sql(text(
                    "INSERT INTO worker_artifact_payloads (artifact_id, content) "
                    "VALUES (:artifact_id, :content)"
                )),
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
                runner_sql(text("SELECT staging_path, created_at FROM export_manifests"))
            )
            cutoff = datetime.now(UTC).timestamp() - self.preview_ttl_hours * 3600
            known_staging = {
                Path(str(row.staging_path))
                for row in manifests
                if row.created_at.timestamp() >= cutoff
            }
            previews = await session.execute(
                runner_sql(text("SELECT preview_id, expires_at FROM previews"))
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
                    runner_sql(text(
                        "INSERT INTO worker_run_events (run_id, status, detail) "
                        "VALUES (:run_id, 'failed', :detail)"
                    )),
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
                runner_sql(text("SELECT preview_id FROM previews WHERE expires_at <= now()"))
            )
            expired = [str(row.preview_id) for row in result]
        for identifier in expired:
            await asyncio.to_thread(remove_preview, self.preview_root / identifier)

    async def _run_container(
        self,
        run_id: int,
        command: list[str],
        limits: WorkerLimits,
        *,
        operation_index: int,
    ) -> tuple[subprocess.CompletedProcess[str], str, str]:
        container = f"chitti-worker-{run_id}"
        self._containers[run_id] = container
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._processes[run_id] = process
        try:
            stdout_task = asyncio.create_task(
                self._read_stream_live_async(
                    run_id,
                    operation_index,
                    "stdout",
                    process.stdout,
                    max(1, limits.output_bytes // 2),
                )
            )
            stderr_task = asyncio.create_task(
                self._read_stream_live_async(
                    run_id,
                    operation_index,
                    "stderr",
                    process.stderr,
                    max(1, limits.output_bytes // 2),
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

    async def _read_stream_live_async(
        self,
        run_id: int,
        operation_index: int,
        stream_name: str,
        stream: IO[bytes] | None,
        limit: int,
    ) -> tuple[str, bool]:
        if stream is None:
            return "", False
        chunks: list[bytes] = []
        buffer = bytearray()
        total = 0
        offset = 0
        sequence = 0
        exceeded = False

        async def flush_buffer(final: bool = False) -> None:
            nonlocal offset, sequence
            if not buffer:
                return
            candidate = bytes(buffer)
            safe_length = len(candidate) if final else _utf8_safe_prefix(candidate)
            if safe_length == 0:
                return
            payload = candidate[:safe_length]
            try:
                await self._append_output_chunk(
                    run_id,
                    operation_index,
                    stream_name,
                    sequence,
                    offset,
                    payload,
                )
            except Exception:
                await self._mark_live_output_degraded(run_id)
            sequence += 1
            offset += len(payload)
            del buffer[:safe_length]

        read_task = asyncio.create_task(
            asyncio.to_thread(stream.read, min(65536, limit + 1))
        )
        last_flush = time.monotonic()
        while True:
            done, _ = await asyncio.wait(
                {read_task},
                timeout=max(0.01, LIVE_OUTPUT_FLUSH_SECONDS - (time.monotonic() - last_flush)),
            )
            if not done:
                await flush_buffer()
                last_flush = time.monotonic()
                continue
            chunk = read_task.result()
            if not chunk:
                await flush_buffer(final=True)
                break
            remaining = limit - total
            if remaining <= 0:
                exceeded = True
                break
            retained = chunk[:remaining]
            chunks.append(retained)
            buffer.extend(retained)
            total += len(retained)
            if len(chunk) > remaining:
                exceeded = True
            if len(buffer) >= LIVE_OUTPUT_FLUSH_BYTES:
                await flush_buffer()
                last_flush = time.monotonic()
            if exceeded:
                await flush_buffer(final=True)
                break
            read_task = asyncio.create_task(
                asyncio.to_thread(stream.read, min(65536, limit - total + 1))
            )
        return b"".join(chunks).decode("utf-8", errors="replace"), exceeded

    async def _mark_live_output_degraded(self, run_id: int) -> None:
        if run_id in self._live_output_degraded:
            return
        self._live_output_degraded.add(run_id)
        try:
            await self._event(
                run_id,
                "live_output_degraded",
                "live output is temporarily unavailable; final artifacts remain authoritative",
            )
        except Exception:
            return

    async def _append_output_chunk(
        self,
        run_id: int,
        operation_index: int,
        stream_name: str,
        sequence: int,
        byte_offset: int,
        content: bytes,
    ) -> None:
        if not content:
            return
        async with self.database.sessions() as session:
            await session.execute(
                runner_sql(text(
                    "INSERT INTO worker_operation_output_chunks "
                    "(run_id, operation_index, stream, sequence, byte_offset, content) "
                    "VALUES (:run_id, :operation_index, :stream, :sequence, "
                    ":byte_offset, :content)"
                )),
                {
                    "run_id": run_id,
                    "operation_index": operation_index,
                    "stream": stream_name,
                    "sequence": sequence,
                    "byte_offset": byte_offset,
                    "content": content,
                },
            )
            await session.execute(
                runner_sql(text(
                    "WITH retained AS ("
                    "SELECT id, COALESCE(SUM(octet_length(content)) OVER ("
                    "PARTITION BY run_id, operation_index ORDER BY id DESC"
                    "), 0) AS retained_bytes "
                    "FROM worker_operation_output_chunks "
                    "WHERE run_id = :run_id AND operation_index = :operation_index"
                    ") DELETE FROM worker_operation_output_chunks c "
                    "USING retained r WHERE c.id = r.id "
                    "AND r.retained_bytes > :maximum"
                )),
                {
                    "run_id": run_id,
                    "operation_index": operation_index,
                    "maximum": LIVE_OUTPUT_TAIL_BYTES,
                },
            )
            await session.commit()

    async def _prune_output_chunks(self, run_id: int, operation_index: int) -> None:
        async with self.database.sessions() as session:
            await session.execute(
                runner_sql(text(
                    "DELETE FROM worker_operation_output_chunks "
                    "WHERE run_id = :run_id AND operation_index = :operation_index"
                )),
                {"run_id": run_id, "operation_index": operation_index},
            )
            await session.commit()

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
        job_config: dict[str, object] | None = None,
        brand_profile: BrandProfile | None = None,
    ) -> list[str]:
        command = [
            "docker", "run", "--name", f"chitti-worker-{run_id}",
            "--network", operation.network, "--cpus", str(limits.cpus),
            "--memory", limits.memory, "--pids-limit", str(limits.pids),
            "--ulimit", f"nofile={limits.nofile}:{limits.nofile}",
            "--shm-size", limits.shm_size, "--user", "65532:65532",
            "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--mount", f"type=bind,src={workspace},dst=/workspace",
            self.image, *operation.command,
        ]
        if job_config and "artifact" in job_config:
            image_index = command.index(self.image)
            env = ["--env", f"CHITTI_POSTER_ARTIFACT={job_config['artifact']}"]
            if brand_profile is not None:
                env.extend(
                    ["--env", f"CHITTI_POSTER_COLORS={'|'.join(brand_profile.brand_colors)}"]
                )
                env.extend(["--env", f"CHITTI_POSTER_FONT={brand_profile.typography}"])
            command[image_index:image_index] = env
        return command

    async def _event(
        self, run_id: int, status: str, detail: str,
        operation_index: int | None = None, task_id: str | None = None,
    ) -> None:
        validate_run_event_status(status)
        async with self.database.sessions() as session:
            await session.execute(
                runner_sql(text(
                    "INSERT INTO worker_run_events "
                    "(run_id, status, detail, operation_index, task_id) "
                    "VALUES (:run_id, :status, :detail, :operation_index, :task_id)"
                )),
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
                runner_sql(text(
                    "INSERT INTO plan_task_events "
                    "(revision_id, task_id, event_type, status, detail) "
                    "SELECT revision_id, :task_id, 'worker', :status, :detail "
                    "FROM worker_runs WHERE id = :run_id"
                )),
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
                runner_sql(text(
                    "INSERT INTO worker_operations "
                    "(run_id, task_id, operation_index, name, status, stdout, stderr, "
                    "exit_code, started_at, finished_at) VALUES "
                    "(:run_id, :task_id, :operation_index, :name, :status, :stdout, "
                    ":stderr, :exit_code, :started_at, now()) RETURNING id"
                )),
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
                    runner_sql(text(
                        "INSERT INTO worker_artifacts "
                        "(run_id, operation_id, kind, path, sha256, byte_size) "
                        "VALUES (:run_id, :operation_id, :kind, :path, :sha256, "
                        ":size) RETURNING id"
                    )),
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
                    runner_sql(text(
                        "INSERT INTO worker_artifact_payloads "
                        "(artifact_id, content) VALUES (:artifact_id, :content)"
                    )),
                    {"artifact_id": artifact_id, "content": content.encode()},
                )
            await session.commit()
        await self._prune_output_chunks(run_id, index)
        await self._event(
            run_id,
            "operation_complete",
            f"{operation.name} · {status} · exit {exit_code}",
            operation_index=index,
            task_id=operation.task_id,
        )

    async def _capture_workspace_artifacts(
        self,
        run_id: int,
        workspace: Path,
        limits: WorkerLimits,
        *,
        include_diff: bool = True,
    ) -> None:
        artifact_root = workspace / "artifacts"
        if not artifact_root.is_dir():
            return
        for path in artifact_root.iterdir():
            if not path.is_file() or (
                path.suffix != ".png"
                and path.name not in {"workspace.diff", "browser-errors.json"}
            ) or (not include_diff and path.name == "workspace.diff"):
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
                if kind in {"screenshot", "browser_evidence"}:
                    capture_count = await session.execute(
                        runner_sql(text(
                            "SELECT COUNT(*) FROM worker_artifacts "
                            "WHERE run_id = :run_id "
                            "AND kind IN ('screenshot', 'browser_evidence')"
                        )),
                        {"run_id": run_id},
                    )
                    if int(capture_count.scalar_one()) >= MAX_CAPTURE_ARTIFACTS_PER_RUN:
                        continue
                artifact = await session.execute(
                    runner_sql(text(
                        "INSERT INTO worker_artifacts "
                        "(run_id, kind, path, sha256, byte_size) "
                        "VALUES (:run_id, :kind, :path, :sha256, :byte_size) "
                        "RETURNING id"
                    )),
                    {
                        "run_id": run_id,
                        "kind": kind,
                        "path": str(path.relative_to(workspace)),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "byte_size": len(content),
                    },
                )
                artifact_id = int(artifact.scalar_one())
                await session.execute(
                    runner_sql(text(
                        "INSERT INTO worker_artifact_payloads "
                        "(artifact_id, content) VALUES (:artifact_id, :content)"
                    )),
                    {"artifact_id": artifact_id, "content": content},
                )
                await session.commit()

    async def _capture_generated_images(
        self, run_id: int, workspace: Path, limits: WorkerLimits
    ) -> None:
        root = workspace / "out" / "generated"
        if not root.is_dir():
            return
        for path in sorted(root.glob("*.png")):
            if path.stat().st_size > limits.artifact_bytes:
                continue
            content = path.read_bytes()
            async with self.database.sessions() as session:
                artifact = await session.execute(
                    runner_sql(text(
                        "INSERT INTO worker_artifacts "
                        "(run_id, kind, path, sha256, byte_size) "
                        "VALUES (:run_id, 'generated_image', :path, :sha256, :size) "
                        "RETURNING id"
                    )),
                    {
                        "run_id": run_id,
                        "path": str(path.relative_to(workspace)),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    },
                )
                await session.execute(
                    runner_sql(text(
                        "INSERT INTO worker_artifact_payloads (artifact_id, content) "
                        "VALUES (:artifact_id, :content)"
                    )),
                    {"artifact_id": int(artifact.scalar_one()), "content": content},
                )
                await session.commit()

    async def _verify_poster_assets(
        self,
        run_id: int,
        workspace: Path,
        operation: FixedOperation,
        operation_index: int,
        stdout: str = "",
    ) -> None:
        try:
            await verify_export_assets(self.database, run_id, workspace)
        except ImageManifestRefused as exc:
            await self._operation(
                run_id, operation, operation_index, "failed", stdout,
                str(exc)[:2000], 1, datetime.now(UTC),
            )
            raise RuntimeError(str(exc)) from exc


class WorkerRunManager:
    def __init__(
        self, database: Database, dispatcher: WorkerDispatcher | None = None
    ) -> None:
        self.database = database
        self.dispatcher = dispatcher
    async def enqueue(
        self,
        revision_id: int,
        limits: WorkerLimits | None = None,
        namespace: str = SHARED_NAMESPACE,
        job_type: str = "website",
        job_config: object | None = None,
    ) -> int:
        chosen = limits or WorkerLimits()
        namespace = normalize_namespace(namespace)
        policy = policy_for(job_type)
        async with self.database.sessions() as session:
            revision = await approved_revision(session, revision_id, namespace)
            revision_job_type = revision.job_type
            revision_job_config = revision.job_config
            if policy.name != revision_job_type:
                raise ValueError(
                    "run job type does not match approved plan revision: "
                    f"submitted {policy.name}, approved {revision_job_type}"
                )
            normalized_config = (
                poster_config_within_ceiling(
                    job_config if job_config is not None else revision_job_config,
                    revision_job_config,
                )
                if policy.is_poster
                else {}
            )
            if policy.is_poster:
                settings = get_settings()
                if not settings.runpod_api_key or not settings.runpod_endpoint_id:
                    raise ValueError(
                        "poster run not started: image generation is unavailable; "
                        "RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID are not configured"
                    )
                profile = await get_brand_profile(session, revision.namespace)
                if profile is None:
                    raise ValueError(
                        "poster run not started: namespace "
                        f"'{revision.namespace}' has no brand profile yet. "
                        "Open Brand profile for this namespace and save its "
                        "colours, typography, formats, audience, voice, and "
                        "do-not-use rules first."
                    )
            result = await session.execute(
                        application_only_sql(text(
                            "INSERT INTO worker_runs "
                    "(revision_id, limits, workspace_id, job_type, job_config) "
                    "VALUES (:revision_id, CAST(:limits AS json), :workspace_id, "
                    ":job_type, CAST(:job_config AS json)) RETURNING id"
                        )),
                {
                    "revision_id": revision.id,
                    "limits": json.dumps(chosen.as_json()),
                    "workspace_id": f"run-{revision.id}",
                    "job_type": policy.name,
                    "job_config": config_json(normalized_config),
                },
            )
            run_id = int(result.scalar_one())
            await session.execute(
                runner_sql(text(
                    "INSERT INTO worker_run_events (run_id, status, detail) "
                    "VALUES (:run_id, 'queued', 'awaiting sandbox slot')"
                )),
                {"run_id": run_id},
            )
            await session.commit()
        return run_id

    async def cancel(self, run_id: int) -> None:
        await self._record(run_id, "cancel_requested", "cancellation requested by owner")

    async def _record(self, run_id: int, status: str, detail: str) -> None:
        async with self.database.sessions() as session:
            await session.execute(
                runner_sql(text(
                    "INSERT INTO worker_run_events (run_id, status, detail) "
                    "VALUES (:run_id, :status, :detail)"
                )),
                {"run_id": run_id, "status": status, "detail": detail},
            )
            await session.commit()

    async def detail(self, run_id: int) -> dict[str, object] | None:
        async with self.database.sessions() as session:
            run_result = await session.execute(
                runner_sql(text(
                    "SELECT r.id, r.revision_id, r.limits, r.workspace_id, r.job_type, "
                    "r.job_config, r.created_at, "
                    "p.namespace "
                    "FROM worker_runs r JOIN plan_revisions p ON p.id = r.revision_id "
                    "WHERE r.id = :run_id"
                )),
                {"run_id": run_id},
            )
            run = run_result.mappings().one_or_none()
            if run is None:
                return None
            events = await session.execute(
                runner_sql(text(
                    "SELECT id, status, detail, operation_index, task_id, created_at "
                    "FROM worker_run_events WHERE run_id = :run_id ORDER BY id"
                )),
                {"run_id": run_id},
            )
            operations = await session.execute(
                runner_sql(text(
                    "SELECT id, task_id, operation_index, name, status, stdout, stderr, "
                    "exit_code, started_at, finished_at FROM worker_operations "
                    "WHERE run_id = :run_id ORDER BY operation_index"
                )),
                {"run_id": run_id},
            )
            artifacts = await session.execute(
                runner_sql(text(
                    "SELECT id, operation_id, kind, path, sha256, byte_size, "
                    "original_byte_size, truncated "
                    "FROM worker_artifacts WHERE run_id = :run_id ORDER BY id"
                )),
                {"run_id": run_id},
            )
            model_calls = await session.execute(
                runner_sql(text(
                    "SELECT id, task_id, iteration, route, model, prompt_tokens, "
                    "completion_tokens, total_tokens, reasoning_tokens, cost_usd, created_at "
                    "FROM worker_model_calls WHERE run_id = :run_id ORDER BY id"
                )),
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
                runner_sql(text(
                    "SELECT status FROM worker_run_events "
                    "WHERE run_id = :run_id ORDER BY id DESC LIMIT 1"
                )),
                {"run_id": run_id},
            )
            status = result.scalar_one_or_none()
            return str(status) if status is not None else None

    async def events_after(self, run_id: int, event_id: int) -> list[dict[str, object]]:
        async with self.database.sessions() as session:
            result = await session.execute(
                runner_sql(text(
                    "SELECT id, status, detail, operation_index, task_id, created_at "
                    "FROM worker_run_events "
                    "WHERE run_id = :run_id AND id > :event_id ORDER BY id"
                )),
                {"run_id": run_id, "event_id": event_id},
            )
            return [dict(row._mapping) for row in result]

    async def output_chunks_after(
        self, run_id: int, chunk_id: int
    ) -> list[dict[str, object]]:
        async with self.database.sessions() as session:
            result = await session.execute(
                runner_sql(text(
                    "SELECT id, operation_index, stream, sequence, byte_offset, "
                    "content, created_at "
                    "FROM worker_operation_output_chunks "
                    "WHERE run_id = :run_id AND id > :chunk_id ORDER BY id"
                )),
                {"run_id": run_id, "chunk_id": chunk_id},
            )
            return [
                {
                    **dict(row._mapping),
                    "content": bytes(row._mapping["content"]).decode(
                        "utf-8", errors="replace"
                    ),
                }
                for row in result
            ]

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


def _task_done_checks(
    completed_commands: set[str],
    required_gates: tuple[str, ...] = REQUIRED_GATE_COMMANDS,
) -> bool:
    return not _missing_gate_evidence(completed_commands, required_gates)


def _png_dimensions(content: bytes) -> tuple[int, int]:
    if content[:8] != b"\x89PNG\r\n\x1a\n" or len(content) < 24:
        raise VisualReviewInconclusive("visual critique requires a valid PNG")
    return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")


def _parse_visual_verdict(value: object, image_digest: str) -> dict[str, object]:
    criteria_names = (
        "fixture_text",
        "visual_hierarchy",
        "readability",
        "generated_imagery",
        "composite_integrity",
        "brand_constraints",
        "text_contrast",
        "cinematic_treatment",
        "subject_scale",
        "text_redundancy",
        "asset_sharpness",
        "kit_fidelity",
        "research_copy",
    )
    if not isinstance(value, dict):
        raise VisualReviewInconclusive("visual critique returned a non-object verdict")
    expected = {
        "observations",
        "verdict",
        "image_sha256",
        "criteria",
        "findings",
        "summary",
        "evidence_limitations",
    }
    if set(value) != expected:
        raise VisualReviewInconclusive("visual critique returned incomplete fields")
    if value.get("image_sha256") != image_digest:
        raise VisualReviewInconclusive("visual critique image digest did not match")
    observations = value.get("observations")
    observation_names = (
        "imagery_edges",
        "background",
        "imagery",
        "text_blocks",
        "colour_use",
    )
    if (
        not isinstance(observations, dict)
        or set(observations) != set(observation_names)
        or any(
            not isinstance(observations[name], str) or not observations[name].strip()
            for name in observation_names
        )
    ):
        raise VisualReviewInconclusive("visual critique observations were incomplete")
    verdict = value.get("verdict")
    if verdict not in {"pass", "fail"}:
        raise VisualReviewInconclusive("visual critique returned an ambiguous verdict")
    criteria = value.get("criteria")
    if (
        not isinstance(criteria, dict)
        or set(criteria) != set(criteria_names)
        or any(criteria[name] not in {"pass", "fail"} for name in criteria_names)
    ):
        raise VisualReviewInconclusive("visual critique criteria were incomplete")
    findings = value.get("findings")
    if not isinstance(findings, list):
        raise VisualReviewInconclusive("visual critique findings were not a list")
    normalized_findings: list[dict[str, str]] = []
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {"criterion", "issue", "action"}:
            raise VisualReviewInconclusive("visual critique finding was incomplete")
        criterion = finding["criterion"]
        issue = finding["issue"]
        action = finding["action"]
        if (
            criterion not in criteria_names
            or not isinstance(issue, str)
            or not issue.strip()
            or not isinstance(action, str)
            or not action.strip()
        ):
            raise VisualReviewInconclusive("visual critique finding was not actionable")
        normalized_findings.append(
            {"criterion": criterion, "issue": issue, "action": action}
        )
    failed_criteria = {name for name in criteria_names if criteria[name] == "fail"}
    if failed_criteria and verdict != "fail":
        raise VisualReviewInconclusive(
            "visual critique passed with failed criteria"
        )
    if verdict == "fail":
        finding_criteria = {item["criterion"] for item in normalized_findings}
        if not failed_criteria.issubset(finding_criteria):
            raise VisualReviewInconclusive(
                "visual critique failure had no actionable failed criterion"
            )
    summary = value.get("summary")
    limitations = value.get("evidence_limitations")
    if not isinstance(summary, str) or not summary.strip():
        raise VisualReviewInconclusive("visual critique summary was missing")
    if isinstance(limitations, str):
        limitations = [limitations]
    if not isinstance(limitations, list) or any(
        not isinstance(item, str) for item in limitations
    ):
        raise VisualReviewInconclusive("visual critique limitations were invalid")
    return {
        "observations": {
            name: observations[name] for name in observation_names
        },
        "verdict": verdict,
        "image_sha256": image_digest,
        "criteria": {name: criteria[name] for name in criteria_names},
        "findings": normalized_findings,
        "summary": summary,
        "evidence_limitations": limitations,
    }


def _record_gate_command(evidence: set[str], command: str) -> None:
    if command == "sync-lockfile":
        evidence.clear()
    elif command in {
        "build",
        "test",
        "export",
        "poster-export",
        "capture_screenshot",
        "visual-review",
    }:
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


def _validate_screenshot_request(
    policy: JobTypePolicy,
    route: str,
    width: int,
    height: int,
    scale: int,
    job_config: dict[str, object],
) -> None:
    if policy.is_poster:
        _validate_poster_screenshot_request(width, height, scale, job_config)
        return
    if not route.startswith("/"):
        raise ValueError(f"invalid screenshot route: {route}")
    if width not in {390, 1440}:
        raise ValueError(
            f"invalid screenshot width: {width}; "
            "website screenshots only support 390 or 1440"
        )
    if height != 900:
        raise ValueError(
            f"invalid screenshot height: {height}; "
            "website screenshots require height 900"
        )
    if scale != 1:
        raise ValueError(
            f"invalid screenshot scale: {scale}; "
            "website screenshots require scale 1"
        )


def _validate_poster_screenshot_request(
    width: int,
    height: int,
    scale: int,
    job_config: dict[str, object],
) -> None:
    approved = poster_config(job_config)
    if (
        width > int(cast(int, approved["width"]))
        or height > int(cast(int, approved["height"]))
        or scale > int(cast(int, approved["scale"]))
    ):
        raise ValueError(
            "poster screenshot exceeds approved capture: "
            f"requested {width}x{height} at scale {scale}, "
            f"approved {approved['width']}x{approved['height']} "
            f"at scale {approved['scale']}"
        )


def _model_system_prompt(
    policy: JobTypePolicy = WEBSITE_POLICY,
    profile: object | None = None,
    job_config: dict[str, object] | None = None,
) -> str:
    if policy.is_poster:
        profile_data = getattr(profile, "__dict__", {})
        manifest = "\n".join(available_font_families()) or "(empty)"
        approved = poster_config(job_config or {})
        color_values = []
        for color in getattr(profile, "brand_colors", ()):
            label, value = split_brand_color(str(color))
            color_values.append(f"{label}: {value}" if label else value)
        return (
            "You are producing one offline poster artifact, not a website. "
            "Write only HTML, CSS, and SVG; do not use npm, package.json, lockfiles, "
            "JavaScript frameworks, remote URLs, runtime fetches, or external fonts. "
            "The poster validator rejects http://, https://, data:, and other "
            "network-bearing CSS url(...) values unless they are same-document "
            "fragment references, rejects fetch(...), and permits examples such as "
            "url(#gradient). Use only "
            "a font family from this supplied sandbox manifest:\n"
            f"{manifest}\n"
            "The owner brand profile is authoritative and must be reflected exactly; "
            "do not invent colours, voice, audience, or typography. The declared "
            f"browser-renderable brand colours are {json.dumps(color_values)}. "
            "Use each colour value in visible CSS or a CSS variable used by the "
            "poster; a label is metadata, not a CSS colour. Include the declared "
            "typography in an explicit font-family declaration. Create the declared "
            "artifact under out/. "
            f"The recorded owner likeness policy is "
            f"{approved['likeness_policy']}; generic figures by default, and "
            "follow the recorded policy exactly. "
            "Fabricated board "
            "logos and fabricated trophies remain prohibited in all cases. "
            + (
                "The approved research package facts are authoritative; use them "
                "for all fixture, team, squad, kit-colour, and venue text and do "
                "not invent replacements:\n"
                f"{json.dumps(approved.get('research_facts', {}), sort_keys=True)}\n"
                if approved.get("research_facts")
                else ""
            )
            +
            "Treat cinematic treatment as required: use directional or rim lighting, "
            "visible depth separation between foreground subjects and the background, "
            "and atmosphere such as haze, light spill, particles, or grain. A flat "
            "colour wash or gradient is not enough, and every text block must maintain "
            "clear contrast against its immediate background. Make the focal figures "
            "large enough to carry the composition; do not leave a dead middle third. "
            "Do not repeat the same matchup or fixture information in multiple title "
            "blocks. Use sharp, photographic-looking generated assets rather than "
            "small cartoon figures, and never scale an asset beyond the dimensions "
            "reported by image_manifest.resolved.json; request a larger asset instead. "
            "When research states kit colours, make the visible figures match those "
            "colours exactly and do not substitute invented panels or accents. "
            "Research descriptions are design inputs, not poster copy: render only "
            "fixture facts as text—teams, competition, date, time, and venue. Never "
            "print descriptive kit values such as 'RICH BLUE & ORANGE' or "
            "'TRADITIONAL LIGHT GREEN' under a team name. State the matchup only "
            "once; 'INDIA PAKISTAN' and 'India v Pakistan' are duplicate matchup "
            "copy even when capitalization or separators differ. "
            f"The coder output ceiling is {CODER_MAX_OUTPUT_TOKENS} tokens; split "
            "large file writes across multiple tool calls rather than emitting one "
            "enormous tool call. Create an out/index.html entry that references and "
            "renders the declared artifact for the cage capture. The capture opens "
            "the declared artifact itself, not the directory root; if the declared "
            "artifact or its index entry is missing, capture is refused. Run "
            "poster-export, then capture_screenshot using exactly the approved poster "
            f"artifact {approved['artifact']} at width {approved['width']}, height "
            f"{approved['height']}, and device scale {approved['scale']}. These "
            "capture numbers are binding; do not raise the scale. A poster capture "
            "does not need a route argument. After capture, call visual_critique. "
            "A visual critique is required before finish; if it fails, make a real "
            "workspace edit, rerun poster-export, capture_screenshot, and call "
            "visual_critique again. The host critique is bound to the exact PNG "
            "digest and cannot be reused after a render change. The reviewer can "
            "inspect source facts and dimensions, not visual quality, which still "
            "requires owner approval.\n"
            "Generated imagery is available through the host-only generate-images "
            "allowlisted operation. First write /workspace/image_manifest.json "
            "(the workspace root, never out/) with only these "
            "fields per image: id, purpose, prompt, negative_prompt, width, height, "
            "optional seed, optional denoise, and optional reference to an existing "
            "generated asset; then run generate-images. The host owns the ComfyUI "
            "workflow and provider credentials, writes assets under out/generated, "
            "and resolves /workspace/image_manifest.resolved.json with real dimensions. The "
            "manifest may set cutout=true for a subject asset; the host runs an "
            "offline segmentation model and returns RGBA with feathered transparency, "
            "so do not fake transparency with opacity or screen blending. If a figure "
            "cannot be matted, compose it as an intentional full-bleed or framed panel "
            "with its own treatment, never as a pasted box. A background plate should "
            "remain visible and well-lit: do not make a stadium plate so dark that it "
            "reads as flat navy. "
            "The cutout field is opt-in per image; omit it for background plates. "
            "generated asset width and height must each be between 64 and 1024 "
            "pixels; choose the largest useful source aspect ratio the endpoint "
            "supports for each subject. The approved 1080x1350 size is the final composed poster "
            "canvas. Subject figures must be full-body with visible feet, anchored "
            "to the lower composition beneath a defined title and fixture-information "
            "zone; do not float portraits in open space or crop a figure across the "
            "body with a hard edge. Subject figures must not be enlarged beyond their resolved "
            "generated pixels; request a larger subject asset instead. Full-bleed "
            "background plates are exempt from that subject rule and should be "
            "judged against the final canvas using cropping, background-size cover, "
            "or layout treatment. "
            "image budget is "
            f"{WorkerLimits().image_request_count} requests and "
            f"${WorkerLimits().image_spend_usd:.2f}; cache hits do not rebill. "
            "When research states kit colours, copy each approved kit-colour "
            "description literally into that figure's image_manifest prompt; do not "
            "paraphrase or invert it. Request plain unbranded kits with no sponsor "
            "marks, board logos, crests, badges, or trophies. Undeclared local "
            "assets, remote URLs, data URLs, and runtime fetches "
            "are refused. Never write workflow JSON or provider credentials.\n"
            "The out/ directory is the publishable export only: keep it limited "
            "to the approved HTML artifact, its index entry, CSS/SVG, and "
            "manifest-declared raster assets. Put working notes, evidence JSON, "
            "and other scratch files under artifacts/ (or another workspace-root "
            "path), never under out/; notes are not export assets.\n"
            f"BRAND PROFILE:\n{json.dumps(profile_data, default=str)}"
        )
    return (
        "Stable worker rules come first. Use the provided native function tools and "
        "return exactly one tool call per response; never emit shell commands. A JSON "
        "tool object in visible content is accepted only as a compatibility fallback. "
        "All paths are relative to the disposable workspace. No .env, secrets, "
        "credentials, arbitrary argv, shell passthrough, or network tool exists. "
        "Write useful code early, then iterate using build and test feedback; do not "
        "read the entire workspace before making a first change."
        f" The coder output ceiling is {CODER_MAX_OUTPUT_TOKENS} tokens; "
        "if a response would exceed it, split large file writes across multiple "
        "tool calls instead of emitting one enormous call."
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


def _reviewer_system_prompt(policy: JobTypePolicy = WEBSITE_POLICY) -> str:
    poster_rule = (
        " For poster jobs, judge artifact existence, declared dimensions, browser "
        "and layout errors, and source-level brand colour/font facts only. State "
        "plainly that visual quality was not assessed and requires owner approval."
        if policy.is_poster
        else ""
    )
    return (
        "You are the reviewer route. Return one strict JSON object with exactly "
        'the fields {"verdict":"pass|fail","findings":[],"evidence_limitations":[],'
        '"summary":"..."}. Findings must be specific observations grounded in the '
        "provided operation output and artifact facts. An unresolved browser error, "
        "page exception, failed request, failed build/test, or missing artifact "
        "requires verdict fail. A failed attempt followed by a successful retry is "
        "a finding but not an unresolved failure. Do not claim to inspect screenshot "
        "pixels: the prompt only contains screenshot dimensions and browser evidence "
        f"facts. Do not write files or propose shell commands.{poster_rule}"
    )


def fixed_operations(
    revision: PlanRevision,
    policy: JobTypePolicy = WEBSITE_POLICY,
    job_config: dict[str, object] | None = None,
) -> tuple[FixedOperation, ...]:
    first = revision.document.tasks[0]
    if policy.is_poster:
        config = poster_config(job_config)
        return (
            FixedOperation(first.id, "git-init", ("sh", "-c", "git init -q /workspace")),
            FixedOperation(
                first.id,
                "write-fixture",
                ("sh", "-c", "mkdir -p /workspace/out /workspace/artifacts"),
            ),
            FixedOperation(
                first.id,
                "poster-export",
                (
                    "sh",
                    "-c",
                    "test -f \"out/$CHITTI_POSTER_ARTIFACT\"",
                ),
            ),
                FixedOperation(
                    first.id,
                    "browser-preview",
                    (
                        "sh",
                        "-c",
                        "test -f \"out/$CHITTI_POSTER_ARTIFACT\" && "
                        "if [ \"$CHITTI_POSTER_ARTIFACT\" = \"index.html\" ] || "
                        "grep -F -- \"$CHITTI_POSTER_ARTIFACT\" out/index.html; then "
                        "python3 /opt/next_screenshot.py "
                        f"--width {config['width']} --height {config['height']} "
                        f"--scale {config['scale']} --poster "
                        f"--artifact {shlex.quote(str(config['artifact']))}; "
                        "else echo \"poster capture requires out/index.html to "
                        "reference the declared artifact $CHITTI_POSTER_ARTIFACT\" "
                        ">&2; exit 1; fi",
                    ),
                ),
            FixedOperation(
                first.id,
                "git-diff",
                (
                    "sh",
                    "-c",
                    "cd /workspace && git add -A -f -- . "
                    "':(exclude)artifacts' && git diff --cached --no-ext-diff "
                    "> artifacts/workspace.diff",
                ),
            ),
        )
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
    session: AsyncSession,
    revision_id: int,
    namespace: str = SHARED_NAMESPACE,
) -> PlanRevision:
    revision = await revision_by_id(session, revision_id, namespace)
    if revision is None:
        raise ValueError("plan revision not found")
    result = await session.execute(
        runner_sql(text(
            "SELECT revision_id, content_hash FROM plan_approvals "
            "WHERE revision_id = :revision AND decision = 'approved' "
            "ORDER BY id DESC LIMIT 1"
        )),
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
