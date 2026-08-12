from unittest.mock import AsyncMock

import pytest

from chitti.embedding import FakeEmbedder
from chitti.memory import MemoryStore, Recall
from chitti.provider import FakeProvider
from chitti.run_context import RunEvidence, _diff_summary, bound_context
from chitti.service import ChittiService


def test_run_prompt_requires_evidence_grounding_and_names_context() -> None:
    service = ChittiService(FakeProvider(), MemoryStore(FakeEmbedder()), "PROFILE")
    evidence = RunEvidence("[failed operation]\nexit 1: missing module", ("failed operation",), False)

    prompt = service.system_prompt([Recall("memory", "semantic", 0.9)], evidence)

    assert "Answer only from this evidence" in prompt
    assert "say that the evidence does not contain an answer" in prompt
    assert "missing module" in prompt


@pytest.mark.asyncio
async def test_run_question_reply_reports_the_supplied_evidence() -> None:
    provider = FakeProvider()
    provider.chat = AsyncMock(return_value="Task 4 failed with the missing module.")
    provider.extract_memories = AsyncMock(return_value=[])
    memory = MemoryStore(FakeEmbedder())
    memory.recall = AsyncMock(return_value=[])
    memory.add_chunk = AsyncMock()
    memory.active_keys = AsyncMock(return_value=[])
    memory.record_memories = AsyncMock(return_value=[])
    service = ChittiService(provider, memory, "PROFILE")
    evidence = RunEvidence(
        "[failed operation]\nmissing module",
        ("failed operation",),
        False,
    )

    result = await service.turn(
        AsyncMock(),
        "Why did task 4 fail?",
        None,
        [],
        "general",
        evidence,
    )

    assert "missing module" in provider.chat.await_args.args[0]
    assert "Evidence used: failed operation." in result.reply


def test_run_context_prioritizes_failures_and_marks_byte_clipping() -> None:
    evidence = bound_context(
        [
            ("run", "Run 22"),
            ("failed operation", "failure output"),
            ("successful operations", "success output"),
            ("screenshots", "must never be selected"),
            ("model prompts", "must never be selected"),
        ],
        max_bytes=80,
    )

    assert len(evidence.context.encode("utf-8")) <= 80
    assert evidence.clipped
    assert "context clipped" in evidence.context
    assert evidence.context.index("failed operation") < evidence.context.index("failure output")
    assert "screenshots" not in evidence.context
    assert "model prompts" not in evidence.context


def test_diff_summary_reports_files_and_line_changes_without_body() -> None:
    diff = b"diff --git a/page.js b/page.js\n+new line\n-old line\n"

    summary = _diff_summary(diff)

    assert "page.js" in summary
    assert "+1/-1" in summary
