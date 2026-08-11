from datetime import UTC, datetime

import pytest

from chitti.plans import (
    PlanApproval,
    PlanDocument,
    PlanRevision,
    PlanTask,
    plan_hash,
    validate_approval_binding,
)


def task(task_id: str, dependencies: list[str] | None = None) -> PlanTask:
    return PlanTask(
        id=task_id,
        title=f"Task {task_id}",
        description="Do the work.",
        dependencies=dependencies or [],
        done_condition="A test proves this is complete.",
    )


def test_plan_validator_rejects_dangling_dependency() -> None:
    with pytest.raises(ValueError, match="missing dependencies"):
        PlanDocument(title="Plan", summary="Summary", tasks=[task("a", ["missing"])])


def test_plan_validator_rejects_dependency_cycle() -> None:
    with pytest.raises(ValueError, match="cycle"):
        PlanDocument(title="Plan", summary="Summary", tasks=[task("a", ["b"]), task("b", ["a"])])


def test_approval_binding_is_to_exact_revision_content() -> None:
    document = PlanDocument(title="Plan", summary="Summary", tasks=[task("a")])
    digest = plan_hash(document)
    created_at = datetime.now(UTC)
    approval = PlanApproval(1, 1, "approved", None, digest, created_at)
    revision = PlanRevision(1, "demo", "Build the demo.", 1, document, digest, created_at, None)
    assert validate_approval_binding(revision, approval)
    changed = document.model_copy(update={"summary": "Changed"})
    changed_revision = PlanRevision(
        1, "demo", "Build the demo.", 1, changed, digest, created_at, None
    )
    assert not validate_approval_binding(changed_revision, approval)
