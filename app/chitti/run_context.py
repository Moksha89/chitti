from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .memory import SHARED_NAMESPACE, normalize_namespace

MAX_RUN_CONTEXT_BYTES = 24_000
OUTPUT_TAIL_BYTES = 3_000
EVENT_TAIL_LIMIT = 12


class RunContextError(ValueError):
    pass


@dataclass(frozen=True)
class RunEvidence:
    context: str
    evidence_used: tuple[str, ...]
    clipped: bool


def bound_context(
    sections: list[tuple[str, str]], max_bytes: int = MAX_RUN_CONTEXT_BYTES
) -> RunEvidence:
    used: list[str] = []
    chunks: list[str] = []
    remaining = max_bytes
    clipped = False
    for label, section in sections:
        rendered = f"[{label}]\n{section}\n"
        encoded = rendered.encode("utf-8")
        if len(encoded) <= remaining:
            chunks.append(rendered)
            used.append(label)
            remaining -= len(encoded)
            continue
        clipped = True
        marker = "\n[context clipped: remaining run evidence omitted]\n"
        marker_bytes = marker.encode("utf-8")
        if remaining < len(marker_bytes):
            marker = "[context clipped]"
            marker_bytes = marker.encode("utf-8")
            current = "".join(chunks).encode("utf-8")
            keep = max(0, max_bytes - len(marker_bytes))
            chunks = [current[:keep].decode("utf-8", errors="ignore")]
            remaining = max_bytes - len(chunks[0].encode("utf-8"))
        if remaining >= len(marker_bytes):
            chunks.append(marker)
        break
    return RunEvidence("".join(chunks), tuple(used), clipped)


def _tail(value: object, limit: int = OUTPUT_TAIL_BYTES) -> str:
    text_value = str(value or "")
    return text_value[-limit:]


def _diff_summary(payload: object) -> str:
    if payload is None:
        return "diff body unavailable"
    if isinstance(payload, str):
        payload_bytes = payload.encode("utf-8")
    elif isinstance(payload, bytes | bytearray | memoryview):
        payload_bytes = bytes(payload)
    else:
        payload_bytes = str(payload).encode("utf-8")
    lines = payload_bytes.decode("utf-8", errors="replace").splitlines()
    files: list[str] = []
    additions = 0
    deletions = 0
    for line in lines:
        if line.startswith("diff --git a/"):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                files.append(parts[1])
        elif line.startswith("+++ ") or line.startswith("--- "):
            continue
        elif line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    if not files:
        return "no changed-file summary available"
    return (
        f"changed files ({len(files)}): {', '.join(files[:80])}; "
        f"line changes: +{additions}/-{deletions}"
    )


async def build_run_evidence(
    session: AsyncSession,
    run_id: int,
    namespace: str,
    max_bytes: int = MAX_RUN_CONTEXT_BYTES,
) -> RunEvidence:
    namespace = normalize_namespace(namespace)
    run_result = await session.execute(
        text(
            "SELECT r.id, r.revision_id, p.project, p.revision, p.namespace "
            "FROM worker_runs r JOIN plan_revisions p ON p.id = r.revision_id "
            "WHERE r.id = :run_id "
            "AND p.namespace IN (:namespace, :shared)"
        ),
        {"run_id": run_id, "namespace": namespace, "shared": SHARED_NAMESPACE},
    )
    run = run_result.mappings().one_or_none()
    if run is None:
        raise RunContextError("run not found in the active namespace")

    event_result = await session.execute(
        text(
            "SELECT status, detail, operation_index, task_id, created_at "
            "FROM worker_run_events WHERE run_id = :run_id "
            "ORDER BY id DESC LIMIT :limit"
        ),
        {"run_id": run_id, "limit": EVENT_TAIL_LIMIT},
    )
    events = list(reversed([dict(row._mapping) for row in event_result]))
    operation_result = await session.execute(
        text(
            "SELECT id, task_id, operation_index, name, status, stdout, stderr, exit_code "
            "FROM worker_operations WHERE run_id = :run_id "
            "ORDER BY CASE WHEN status = 'failed' THEN 0 ELSE 1 END, operation_index"
        ),
        {"run_id": run_id},
    )
    operations = [dict(row._mapping) for row in operation_result]
    failed = [item for item in operations if str(item.get("status")) == "failed"]
    successful = [item for item in operations if item not in failed]

    task_result = await session.execute(
        text(
            "SELECT task_id, status FROM plan_task_events "
            "WHERE revision_id = :revision_id ORDER BY id"
        ),
        {"revision_id": run["revision_id"]},
    )
    task_states: dict[str, str] = {}
    for row in task_result:
        task_states[str(row.task_id)] = str(row.status)

    reviewer_result = await session.execute(
        text(
            "SELECT p.content FROM worker_artifacts a "
            "JOIN worker_artifact_payloads p ON p.artifact_id = a.id "
            "WHERE a.run_id = :run_id AND a.kind = 'reviewer_report' "
            "ORDER BY a.id DESC LIMIT 1"
        ),
        {"run_id": run_id},
    )
    reviewer = reviewer_result.scalar_one_or_none()

    diff_result = await session.execute(
        text(
            "SELECT p.content FROM worker_artifacts a "
            "LEFT JOIN worker_artifact_payloads p ON p.artifact_id = a.id "
            "WHERE a.run_id = :run_id AND a.kind = 'diff' "
            "ORDER BY a.id DESC LIMIT 1"
        ),
        {"run_id": run_id},
    )
    diff = diff_result.scalar_one_or_none()

    manifest_result = await session.execute(
        text(
            "SELECT m.manifest, m.digest, m.file_count, m.total_bytes, "
            "a.decision AS approval_decision, p.preview_id, p.expires_at "
            "FROM export_manifests m "
            "LEFT JOIN promotion_approvals a ON a.manifest_id = m.id "
            "LEFT JOIN previews p ON p.manifest_id = m.id "
            "WHERE m.run_id = :run_id ORDER BY m.id DESC LIMIT 1"
        ),
        {"run_id": run_id},
    )
    manifest = manifest_result.mappings().one_or_none()

    sections: list[tuple[str, str]] = [
        (
            "run",
            f"Run {run['id']} / project {run['project']} / "
            f"revision {run['revision']} / namespace {run['namespace']}",
        ),
    ]
    for operation in failed:
        sections.append(
            (
                "failed operation",
                f"FAILED operation {operation['operation_index']} "
                f"task {operation['task_id']} {operation['name']} "
                f"(exit {operation['exit_code']}):\n"
                f"stdout tail:\n{_tail(operation['stdout'])}\n"
                f"stderr tail:\n{_tail(operation['stderr'])}",
            )
        )
    if reviewer is not None:
        try:
            reviewer_text = json.dumps(json.loads(str(reviewer)), indent=2)
        except (TypeError, ValueError):
            reviewer_text = str(reviewer)
        sections.append(("reviewer", f"Reviewer report:\n{reviewer_text}"))
    if task_states:
        sections.append(
            (
                "task states",
                "Task states: "
                + ", ".join(f"{key}={value}" for key, value in task_states.items()),
            )
        )
    if events:
        sections.append(
            (
                "event tail",
                "Recent run events:\n"
                + "\n".join(
                    f"- {item['status']}: {item['detail']}" for item in events
                ),
            )
        )
    if successful:
        sections.append(
            (
                "successful operations",
                "Successful operations: "
                + ", ".join(
                    f"{item['operation_index']}:{item['name']}" for item in successful
                ),
            )
        )
    sections.append(("files", _diff_summary(diff)))
    if manifest is not None:
        sections.append(
            (
                "publication",
                f"Export manifest digest {manifest['digest']}, "
                f"{manifest['file_count']} files / {manifest['total_bytes']} bytes; "
                f"approval={manifest['approval_decision'] or 'none'}; "
                f"preview={manifest['preview_id'] or 'none'} "
                f"expires={manifest['expires_at'] or 'n/a'}",
            )
        )

    return bound_context(sections, max_bytes)
