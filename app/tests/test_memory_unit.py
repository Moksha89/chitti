from chitti.embedding import FakeEmbedder
from chitti.memory import normalize
from chitti.provider import ExtractedMemory


def test_normalize_deduplicates_whitespace_and_case() -> None:
    assert normalize("  Dark   Mode ") == "dark mode"


def test_memory_item_is_explicitly_structured() -> None:
    item = ExtractedMemory("design_taste", "dark minimal", "user said so", None, "user_stated")
    assert item.source == "user_stated"
    assert len(FakeEmbedder().embed(item.value)) == 384
