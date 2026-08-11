import pytest

from chitti.plans import PlanDocument, PlanTask, plan_hash, validate_approval_binding


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
    approval = type("Approval", (), {"revision_id": 1, "content_hash": digest})()
    revision = type("Revision", (), {"id": 1, "document": document, "content_hash": digest})()
    assert validate_approval_binding(revision, approval)
    changed = document.model_copy(update={"summary": "Changed"})
    changed_revision = type("Revision", (), {"id": 1, "document": changed, "content_hash": digest})()
    assert not validate_approval_binding(changed_revision, approval)
