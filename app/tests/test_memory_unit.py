from chitti.embedding import FakeEmbedder
from chitti.memory import BELIEF_MATCH_THRESHOLD, MemoryStore, normalize, normalize_key
from chitti.provider import ExtractedMemory


def test_normalize_deduplicates_whitespace_and_case() -> None:
    assert normalize("  Dark   Mode ") == "dark mode"


def test_memory_item_is_explicitly_structured() -> None:
    item = ExtractedMemory("design_taste", "dark minimal", "user said so", None, "user_stated")
    assert item.source == "user_stated"
    assert len(FakeEmbedder().embed(item.value)) == 384


def test_normalize_key_collapses_namespace_drift() -> None:
    assert normalize_key("preferred_stack.frontend_framework") == "preferred_frontend_framework"
    assert normalize_key("preferred_frontend_framework") == "preferred_frontend_framework"


class SubjectEmbedder:
    dimensions = 2

    def embed(self, text: str) -> list[float]:
        if "frontend_framework" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]


def test_matching_belief_requires_threshold_for_unrelated_subjects() -> None:
    store = MemoryStore(SubjectEmbedder())
    related, score = store.matching_belief(
        [{"id": 1, "decision_key": "deployment_target", "decision": "VPS"}],
        ExtractedMemory("frontend_framework", "Next.js", None, None),
    )
    assert related is None
    assert score < BELIEF_MATCH_THRESHOLD
