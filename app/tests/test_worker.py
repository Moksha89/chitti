import asyncio
from datetime import UTC, datetime
from io import BytesIO

import pytest

from chitti.plans import PlanDocument, PlanRevision
from chitti.worker import DockerSandboxDispatcher, FixedOperation, WorkerLimits, fixed_operations


def revision() -> PlanRevision:
    document = PlanDocument(
        title="Sandbox fixture",
        summary="Deterministic fixture",
        tasks=[
            {
                "id": "one",
                "title": "Write fixture",
                "description": "Write a fixture.",
                "done_condition": "Fixture exists.",
            }
        ],
    )
    return PlanRevision(
        id=1,
        project="sandbox",
        brief="Build fixture.",
        revision=1,
        document=document,
        content_hash="a" * 64,
        created_at=datetime.now(UTC),
        parent_revision_id=None,
    )


def test_worker_limits_are_recorded_as_explicit_sandbox_contract() -> None:
    limits = WorkerLimits()
    values = limits.as_json()
    assert values["cpus"] == 1.0
    assert values["memory"] == "2g"
    assert values["pids"] == 512
    assert values["non_root_uid"] == 65532
    assert values["workspace_bytes"] == 4 * 1024 * 1024 * 1024
    assert values["output_bytes"] > 0
    assert WorkerLimits.from_json(values) == limits


def test_fixed_operations_are_deterministic_and_include_preview() -> None:
    operations = fixed_operations(revision())
    assert all(isinstance(operation, FixedOperation) for operation in operations)
    assert [operation.name for operation in operations] == [
        "git-init",
        "write-fixture",
        "install-node-dependencies",
        "next-build",
        "static-export",
        "browser-preview",
        "run-tests",
        "git-diff",
    ]
    assert operations[2].network == "bridge"
    assert all(operation.network == "none" for operation in operations[:2])
    assert all(operation.network == "none" for operation in operations[3:])
    assert "/workspace/artifacts" in operations[1].command[-1]
    assert "node_modules" in operations[-1].command[-1]
    assert ".next" in operations[-1].command[-1]
    assert ".npm-cache" in operations[-1].command[-1]


@pytest.mark.parametrize("network", ["chitti_net", "host"])
def test_fixed_operations_never_join_application_network(network: str) -> None:
    assert network not in {operation.network for operation in fixed_operations(revision())}


def test_output_reader_stops_at_budget_without_buffering_unbounded_output() -> None:
    async def exercise() -> tuple[str, bool]:
        dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]
        return dispatcher._read_limited(BytesIO(b"x" * 4096), 1024)

    output, exceeded = asyncio.run(exercise())
    assert len(output) == 1024
    assert exceeded


def test_failed_unmount_keeps_workspace_image(monkeypatch, tmp_path) -> None:
    dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]
    workspace = tmp_path / "chitti-run-1"
    image = dispatcher._workspace_image(workspace)
    image.write_bytes(b"backing image")

    async def fail_unmount(_workspace):
        raise RuntimeError("workspace mount remains active")

    monkeypatch.setattr(dispatcher, "_unmount_workspace", fail_unmount)

    with pytest.raises(RuntimeError, match="workspace mount remains active"):
        asyncio.run(dispatcher._cleanup_workspace(workspace))

    assert image.exists()


def test_stale_cleanup_attempts_every_workspace_before_failing(monkeypatch, tmp_path) -> None:
    dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]
    dispatcher.workspace_root = tmp_path
    workspaces = {tmp_path / "chitti-run-proof-a", tmp_path / "chitti-run-proof-b"}
    attempted: list[str] = []
    recorded: list[tuple[str, str]] = []

    monkeypatch.setattr(dispatcher, "_backing_loops", lambda _root: {})
    monkeypatch.setattr(dispatcher, "_mounted_workspaces", lambda _root: workspaces)

    async def remove_container(_container):
        return None

    async def fail_cleanup(workspace):
        attempted.append(workspace.name)
        raise RuntimeError("mount remains")

    async def record_failure(run_id, detail):
        recorded.append((run_id, detail))

    monkeypatch.setattr(dispatcher, "_remove_container", remove_container)
    monkeypatch.setattr(dispatcher, "_cleanup_workspace", fail_cleanup)
    monkeypatch.setattr(dispatcher, "_record_cleanup_failure", record_failure)

    with pytest.raises(RuntimeError, match="proof-a|proof-b"):
        asyncio.run(dispatcher.cleanup_stale_workspaces())

    assert set(attempted) == {workspace.name for workspace in workspaces}
    assert {run_id for run_id, _detail in recorded} == {"proof-a", "proof-b"}


def test_cleanup_failure_recorder_logs_non_run_id_without_raising(caplog) -> None:
    dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]

    with caplog.at_level("ERROR"):
        asyncio.run(
            dispatcher._record_cleanup_failure(
                "proof-quota", "workspace mount remains active"
            )
        )

    assert "proof-quota" in caplog.text
    assert "workspace mount remains active" in caplog.text
