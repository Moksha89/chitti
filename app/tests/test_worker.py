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
        "browser-preview",
        "run-tests",
        "git-diff",
    ]
    assert operations[2].network == "bridge"
    assert all(operation.network == "none" for operation in operations[:2])
    assert all(operation.network == "none" for operation in operations[3:])


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
