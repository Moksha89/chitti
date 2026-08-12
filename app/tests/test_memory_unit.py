import pytest

from chitti.embedding import FakeEmbedder
from chitti.memory import MemoryStore, normalize, normalize_key
from chitti.provider import ExtractedMemory


def test_normalize_deduplicates_whitespace_and_case() -> None:
    assert normalize("  Dark   Mode ") == "dark mode"


def test_memory_item_is_explicitly_structured() -> None:
    item = ExtractedMemory("design_taste", "dark minimal", "user said so", None, "user_stated")
    assert item.source == "user_stated"
    assert len(FakeEmbedder().embed(item.value)) == 384


def test_normalize_key_collapses_namespace_drift() -> None:
    expected = "frontend_framework"
    for key in (
        "preferred_stack.frontend_framework",
        "preferred_frontend_framework",
        "preferences.frontend_framework",
        "stack.frontend_framework",
        "frontend_framework",
    ):
        assert normalize_key(key) == expected
    assert normalize_key("project.frontend_framework") == "project_frontend_framework"


class SubjectEmbedder:
    dimensions = 2

    def embed(self, text: str) -> list[float]:
        if "frontend_framework" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]


def test_matching_belief_uses_only_normalized_keys() -> None:
    store = MemoryStore(SubjectEmbedder())
    related, score = store.matching_belief(
        [{"id": 1, "decision_key": "deployment_target", "decision": "VPS"}],
        ExtractedMemory("frontend_framework", "Next.js", None, None),
    )
    assert related is None
    assert score == 0.0
    related, score = store.matching_belief(
        [{"id": 1, "decision_key": "deployment_target", "decision": "VPS"}],
        ExtractedMemory("preferred_deployment_target", "managed cloud", None, None),
    )
    assert related is not None
    assert score == 1.0


def test_retrieval_requires_an_explicit_namespace_argument() -> None:
    store = MemoryStore(FakeEmbedder())
    with pytest.raises(TypeError):
        store.recall(None, "query")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        store.active_beliefs(None)  # type: ignore[arg-type]
