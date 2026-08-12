from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .diff_parser import parse_diff
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
    clipped_sections: tuple[str, ...] = ()


def _fit_text(value: str, limit: int, from_end: bool = False) -> str:
    encoded = value.encode()
    if len(encoded) <= limit:
        return value
    retained = encoded[-limit:] if from_end else encoded[:limit]
    return retained.decode("utf-8", errors="ignore")


def _trim_failed_operation(section: str, limit: int) -> str:
    stderr_marker = "\nstderr tail:\n"
    stdout_marker = "\nstdout tail:\n"
    prefix, _, stderr_and_stdout = section.partition(stderr_marker)
    if not _:
        heading, separator, body = section.partition("\n")
        if not separator:
            return _fit_text(section, limit, from_end=True)
        heading = heading + separator
        return heading + _fit_text(body, max(0, limit - len(heading.encode())), from_end=True)
    stderr, _, stdout = stderr_and_stdout.partition(stdout_marker)
    prefix_text = prefix + stderr_marker
    retained = _fit_text(prefix_text, limit)
    remaining = limit - len(retained.encode())
    if remaining <= 0:
        return retained
    retained_stderr = _fit_text(stderr, remaining, from_end=True)
    retained += retained_stderr
    remaining -= len(retained_stderr.encode())
    if stdout and remaining > len(stdout_marker.encode()):
        retained += stdout_marker
        retained += _fit_text(stdout, remaining - len(stdout_marker.encode()), from_end=True)
    return retained


def _trim_section(label: str, section: str, limit: int) -> str:
    if label == "failed operation":
        return _trim_failed_operation(section, limit)
    if label in {"run", "task states"}:
        heading, separator, body = section.partition("\n")
        if separator:
            heading += separator
            return heading + _fit_text(
                body, max(0, limit - len(heading.encode())), from_end=True
            )
    return _fit_text(section, limit)


def bound_context(
    sections: list[tuple[str, str]], max_bytes: int = MAX_RUN_CONTEXT_BYTES
) -> RunEvidence:
    used: list[str] = []
    chunks: list[str] = []
    remaining = max_bytes
    clipped = False
    clipped_sections: list[str] = []
    reserve_each = min(768, max(16, max_bytes // 20))
    reserve_marker = min(128, max(16, max_bytes // 20))
    for index, (label, section) in enumerate(sections):
        rendered = f"[{label}]\n{section}\n"
        encoded = rendered.encode("utf-8")
        later_labels = {
            later_label for later_label, _ in sections[index + 1 :] if later_label != label
        }
        later_reserve = len(later_labels) * reserve_each
        available = max(0, remaining - later_reserve - reserve_marker)
        if len(encoded) <= available:
            chunks.append(rendered)
            used.append(label)
            remaining -= len(encoded)
            continue
        clipped = True
        if label not in clipped_sections:
            clipped_sections.append(label)
        if available > 0 and label == "failed operation":
            partial = _trim_section(label, rendered, available)
            chunks.append(partial)
            if label not in used:
                used.append(label)
            remaining -= len(partial.encode("utf-8"))
    if clipped:
        compact_labels = {
            "failed operation": "failed output",
            "successful operations": "success",
            "task states": "tasks",
            "event tail": "events",
            "publication": "publication state",
        }
        labels = ", ".join(
            compact_labels.get(label, label) for label in clipped_sections
        )
        marker = f"\n[context clipped: {labels} omitted or trimmed]\n"
        if len("".join(chunks).encode()) + len(marker.encode()) > max_bytes:
            first = compact_labels.get(clipped_sections[0], clipped_sections[0])
            marker = f"[context clipped: {first}]"
        marker = _fit_text(marker, max_bytes, from_end=False)
        context = _fit_text(
            "".join(chunks),
            max(0, max_bytes - len(marker.encode())),
        ) + marker
    else:
        context = "".join(chunks)
    return RunEvidence(
        context, tuple(used), clipped, tuple(clipped_sections)
    )


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
    entries = parse_diff(payload_bytes)
    if not entries:
        return "no changed-file summary available"
    authored = [entry for entry in entries if entry["role"] == "authored"]
    generated = [entry for entry in entries if entry["role"] == "generated"]

    def summarize(items: list[dict[str, object]]) -> str:
        additions = sum(int(str(item["additions"])) for item in items)
        deletions = sum(int(str(item["deletions"])) for item in items)
        paths = ", ".join(str(item["path"]) for item in items[:80])
        return f"{len(items)} files (+{additions}/-{deletions}): {paths}"

    parts = []
    if authored:
        parts.append(f"authored {summarize(authored)}")
    if generated:
        parts.append(f"generated {summarize(generated)}")
    return (
        "changed files: " + "; ".join(parts)
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
                f"stderr tail:\n{_tail(operation['stderr'])}\n"
                f"stdout tail:\n{_tail(operation['stdout'])}",
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
