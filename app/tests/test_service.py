from unittest.mock import AsyncMock

import pytest

from chitti.embedding import FakeEmbedder
from chitti.memory import MemoryStore, Recall
from chitti.provider import FakeProvider
from chitti.service import ChittiService


def test_system_prompt_keeps_stable_profile_before_variable_recall() -> None:
    service = ChittiService(FakeProvider(), MemoryStore(FakeEmbedder()), "PROFILE")

    prompt = service.system_prompt([Recall("RECALLED", "semantic", 0.9)])

    assert prompt.index("PROFILE") < prompt.index("RECALLED")
    assert prompt.index("Relevant semantic recall:") < prompt.index("RECALLED")


@pytest.mark.asyncio
async def test_turn_passes_namespace_to_every_memory_read_and_write() -> None:
    memory = MemoryStore(FakeEmbedder())
    memory.recall = AsyncMock(return_value=[])
    memory.add_chunk = AsyncMock()
    memory.active_keys = AsyncMock(return_value=[])
    memory.record_memories = AsyncMock(return_value=[])
    provider = FakeProvider()
    provider.extract_memories = AsyncMock(return_value=[])
    service = ChittiService(provider, memory, "PROFILE")

    result = await service.turn(
        AsyncMock(),
        "hello",
        "PJ Digi",
        [],
        "pj-digi",
    )

    assert result.reply
    memory.recall.assert_awaited_once()
    assert memory.recall.await_args.args[2] == "pj-digi"
    assert memory.active_keys.await_args.args[1] == "pj-digi"
    assert memory.record_memories.await_args.args[2] == "pj-digi"
    assert all(call.args[-1] == "pj-digi" for call in memory.add_chunk.await_args_list)
