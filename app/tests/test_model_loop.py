from pathlib import Path

import pytest

from chitti.provider import ModelCompletion, ModelToolCall
from chitti.worker import (
    DockerSandboxDispatcher,
    WorkerLimits,
    _assistant_tool_message,
    _bounded_artifact,
    _compact_model_messages,
    _confined_path,
    _file_write_stall,
    _model_response_failure,
    _parse_tool_call,
    _task_done_checks,
    _tool_exchange,
)


def test_model_limits_round_trip() -> None:
    limits = WorkerLimits(model_iterations=3, model_tool_calls=7, model_write_bytes=1234)
    assert WorkerLimits.from_json(limits.as_json()) == limits


def test_model_token_budget_round_trip() -> None:
    limits = WorkerLimits(model_iterations=40, model_tool_calls=120, model_tokens=30000)
    encoded = limits.as_json()
    assert encoded["model_tokens"] == 30000
    assert WorkerLimits.from_json(encoded).model_tokens == 30000


def test_done_condition_commands_are_scoped_to_each_task() -> None:
    first_task_commands = {"build", "test", "export"}
    second_task_commands: set[str] = set()
    assert _task_done_checks(first_task_commands)
    assert not _task_done_checks(second_task_commands)


def test_bounded_artifact_preserves_original_size_and_truncation() -> None:
    payload, original_size, truncated = _bounded_artifact("é" * 9000, maximum=16000)
    assert len(payload) == 16000
    assert original_size == 18000
    assert truncated


def test_model_history_compaction_keeps_prefix_recent_and_feedback() -> None:
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "task contract"},
        *[
            {"role": "assistant", "content": f"old {index}"}
            for index in range(10)
        ],
        {"role": "user", "content": "next-build passed"},
        {"role": "assistant", "content": "recent"},
    ]
    compacted, changed, removed = _compact_model_messages(messages, recent_turns=2)
    assert changed
    assert removed > 0
    assert compacted[0] == messages[0]
    assert compacted[1] == messages[1]
    assert any("next-build passed" in item["content"] for item in compacted)
    assert any("COMPACTION:" in item["content"] for item in compacted)


def test_model_history_compaction_keeps_native_tool_exchange_together() -> None:
    completion = ModelCompletion(
        content="",
        model="coder",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost_usd=0,
        tool_calls=(
            ModelToolCall(
                id="call-1", name="list_files", arguments={"path": "."}
            ),
        ),
    )
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "old"},
        *_tool_exchange(completion, "[\"package.json\"]", completion.tool_calls[0]),
        {"role": "assistant", "content": "recent"},
    ]
    compacted, changed, _ = _compact_model_messages(messages, recent_turns=2)
    assert changed
    assistant_index = next(
        index for index, item in enumerate(compacted) if item.get("tool_calls")
    )
    assert compacted[assistant_index + 1]["role"] == "tool"
    assert compacted[assistant_index + 1]["tool_call_id"] == "call-1"


def test_native_tool_call_message_is_represented_for_provider_history() -> None:
    completion = ModelCompletion(
        content="",
        model="coder",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost_usd=0,
        tool_calls=(
            ModelToolCall(
                id="call-1", name="finish", arguments={"summary": "done"}
            ),
        ),
    )
    message = _assistant_tool_message(completion)
    assert message["role"] == "assistant"
    assert message["tool_calls"][0]["function"]["name"] == "finish"
    assert _model_response_failure(completion) is None

def test_model_tool_parser_rejects_malformed_and_unknown_shape() -> None:
    with pytest.raises(ValueError, match="valid JSON"):
        _parse_tool_call("not json")
    with pytest.raises(ValueError, match="one tool call"):
        _parse_tool_call('{"arguments": {}}')
    with pytest.raises(ValueError, match="arguments"):
        _parse_tool_call('{"tool": "write_file", "arguments": []}')


def test_truncated_model_response_is_reported_distinctly() -> None:
    completion = ModelCompletion(
        content="",
        model="coder",
        prompt_tokens=10,
        completion_tokens=8192,
        total_tokens=8202,
        cost_usd=0.01,
        finish_reason="length",
    )
    assert _model_response_failure(completion) == (
        "model response truncated at the output limit"
    )
    assert not completion.message_fields


def test_empty_content_with_reasoning_but_no_tool_call_is_nonproductive() -> None:
    completion = ModelCompletion(
        content="",
        model="coder",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        cost_usd=0.01,
        message_fields=("reasoning_content",),
    )
    assert _model_response_failure(completion) == "model response had no visible content"


def test_circular_file_rewrites_are_stopped() -> None:
    assert _file_write_stall("app/page.js", 4, 4) == (
        "app/page.js was rewritten 4 times without running a command"
    )
    assert _file_write_stall("app/page.js", 2, 24) == (
        "stopped after 24 file writes without running a command"
    )
    assert _file_write_stall("app/page.js", 3, 15) is None


def test_many_file_scaffold_writes_can_reach_a_command() -> None:
    for index in range(20):
        assert _file_write_stall(f"app/file-{index}.js", 1, index + 1) is None
    assert _file_write_stall("app/file-20.js", 1, 21) is None
    assert _file_write_stall("app/file-23.js", 1, 24) == (
        "stopped after 24 file writes without running a command"
    )


def test_model_paths_reject_traversal_and_symlink_escape(tmp_path: Path) -> None:
    assert _confined_path(tmp_path, "app/page.js") == tmp_path / "app/page.js"
    with pytest.raises(ValueError, match="escapes"):
        _confined_path(tmp_path, "../outside")
    outside = tmp_path.parent / "model-loop-outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        _confined_path(tmp_path, "link/file.txt")


@pytest.mark.asyncio
async def test_model_write_budget_and_command_allowlist(tmp_path: Path) -> None:
    dispatcher = object.__new__(DockerSandboxDispatcher)
    limits = WorkerLimits(model_write_bytes=4)
    with pytest.raises(ValueError, match="single write"):
        await dispatcher._execute_model_tool(
            1, "task", 0, "write_file",
            {"path": "app.js", "content": "too big"},
            tmp_path, limits, "coder",
        )
    with pytest.raises(ValueError, match="unknown allowlisted"):
        await dispatcher._execute_model_tool(
            1, "task", 0, "run_command",
            {"name": "sh", "args": []},
            tmp_path, limits, "coder",
        )
    with pytest.raises(ValueError, match="arbitrary command"):
        await dispatcher._execute_model_tool(
            1, "task", 0, "run_command",
            {"name": "build", "args": ["--unsafe"]},
            tmp_path, limits, "coder",
        )
