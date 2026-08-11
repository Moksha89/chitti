from chitti.embedding import FakeEmbedder
from chitti.memory import MemoryStore, Recall
from chitti.provider import FakeProvider
from chitti.service import ChittiService


def test_system_prompt_keeps_stable_profile_before_variable_recall() -> None:
    service = ChittiService(FakeProvider(), MemoryStore(FakeEmbedder()), "PROFILE")

    prompt = service.system_prompt([Recall("RECALLED", "semantic", 0.9)])

    assert prompt.index("PROFILE") < prompt.index("RECALLED")
    assert prompt.index("Relevant semantic recall:") < prompt.index("RECALLED")
