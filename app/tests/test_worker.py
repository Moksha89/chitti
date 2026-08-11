from datetime import UTC, datetime

import pytest

from chitti.plans import PlanDocument, PlanRevision
from chitti.worker import FixedOperation, WorkerLimits, fixed_operations


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
    assert values["memory"] == "512m"
    assert values["pids"] == 128
    assert values["non_root_uid"] == 65532
    assert values["artifact_bytes"] > 0


def test_fixed_operations_are_deterministic_and_include_preview() -> None:
    operations = fixed_operations(revision())
    assert all(isinstance(operation, FixedOperation) for operation in operations)
    assert [operation.name for operation in operations] == [
        "git-init",
        "write-fixture",
        "install-fixture-dependency",
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
