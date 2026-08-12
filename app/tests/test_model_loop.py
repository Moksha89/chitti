from pathlib import Path

import pytest

from chitti.provider import ModelCompletion, ModelToolCall
from chitti.worker import (
    DockerSandboxDispatcher,
    RunBudgetExceeded,
    WorkerLimits,
    _assistant_tool_message,
    _bounded_artifact,
    _compact_model_messages,
    _confined_path,
    _file_write_stall,
    _gate_evidence_status,
    _gate_refusal_progress,
    _install_failure_detail,
    _is_lockfile_mismatch,
    _model_call_failure_detail,
    _model_progress_context,
    _model_response_failure,
    _model_system_prompt,
    _parse_tool_call,
    _progress_counters,
    _record_gate_command,
    _remember_progress_ledger,
    _replace_model_progress_status,
    _reset_file_write_counter,
    _reviewer_diagnosis_messages,
    _source_path_invalidates_gates,
    _starter_context,
    _task_done_checks,
    _tool_counts_as_progress,
    _tool_exchange,
    _tool_rejection_exchange,
    _tool_result_message,
    _unexecuted_tool_results,
)


def test_lockfile_sync_operation_is_allowlisted_and_has_no_arguments() -> None:
    from chitti.model_tools import model_tool_schemas
    from chitti.worker import MODEL_COMMANDS

    operation, command, network = MODEL_COMMANDS["sync-lockfile"]
    assert operation == "sync-lockfile"
    assert command == (
        "sh",
        "-c",
        "npm install --package-lock-only --ignore-scripts --no-audit --no-fund",
    )
    assert network == "bridge"
    schema = next(
        tool for tool in model_tool_schemas()
        if tool["function"]["name"] == "run_command"
    )
    assert "sync-lockfile" in schema["function"]["parameters"]["properties"]["name"]["enum"]


def test_install_mismatch_feedback_names_lockfile_sync_operation() -> None:
    detail = (
        "npm ci can only install packages when your package.json and "
        "package-lock.json are in sync."
    )
    assert _is_lockfile_mismatch(detail)
    assert "sync-lockfile" in _install_failure_detail("install", detail)
    assert _install_failure_detail("build", detail) == detail
    assert "sync-lockfile" in _model_system_prompt()


def test_starter_context_summarizes_direct_dependencies(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"next":"14.2.35"},'
        '"devDependencies":{"tailwindcss":"3.4.17"}}'
    )
    context = _starter_context(tmp_path)
    assert "LOCKED DIRECT DEPENDENCIES" in context
    assert '"next": "14.2.35"' in context
    assert '"tailwindcss": "3.4.17"' in context


def test_model_limits_round_trip() -> None:
    limits = WorkerLimits(model_iterations=3, model_tool_calls=7, model_write_bytes=1234)
    assert WorkerLimits.from_json(limits.as_json()) == limits
    assert WorkerLimits().run_timeout_seconds == 7200
    assert WorkerLimits().model_tokens == 500000
    assert WorkerLimits().model_tool_calls == 240


@pytest.mark.parametrize(
    "budget",
    ["model tool-call", "model token", "model spend", "model write-byte", "model run wall-clock"],
)
def test_run_budget_failure_names_the_exhausted_budget(budget: str) -> None:
    error = RunBudgetExceeded(budget)
    assert str(error) == f"{budget} budget exceeded"


def test_model_call_failure_detail_distinguishes_transport_and_response_errors() -> None:
    from chitti.provider import ModelTransportError

    assert "transport failure" in _model_call_failure_detail(
        "coder", ModelTransportError("gateway request timed out")
    )
    assert "response processing failed" in _model_call_failure_detail(
        "coder", ValueError("invalid model response")
    )


def test_inspection_does_not_clear_stall_but_write_progress_does() -> None:
    failures, turns = _progress_counters(
        1, 2, workspace_changed=False
    )
    assert (failures, turns) == (1, 3)
    assert _progress_counters(
        failures, turns, workspace_changed=True
    ) == (0, 0)


def test_model_progress_context_warns_more_sharply_as_turns_run_out() -> None:
    early = _model_progress_context(0, set(), [], [])
    late = _model_progress_context(7, set(), [], [])
    assert "8 nonproductive turn(s) remain" in early
    assert "1 nonproductive turn(s) remain" in late
    assert "another inspection-only turn" in late


def test_model_progress_context_only_hints_finish_with_current_gates() -> None:
    incomplete = _model_progress_context(1, {"build", "test"}, [], [])
    opening = _model_progress_context(0, {"build", "test", "export"}, [], [])
    complete = _model_progress_context(3, {"build", "test", "export"}, [], [])
    assert "expected next action is to call `finish`" not in incomplete
    assert "expected next action is to call `finish`" not in opening
    assert "Current successful gate evidence is missing: export." in incomplete
    assert "Current successful gate evidence is complete (build, test, export)." in opening
    assert "expected next action is to call `finish`" in complete
    assert "gate will independently accept or refuse it" in complete
    assert "Current successful gate evidence is complete (build, test, export)." in complete


def test_gate_status_never_claims_missing_evidence_with_empty_list() -> None:
    assert _gate_evidence_status({"build", "test", "export"}) == (
        "Current successful gate evidence is complete (build, test, export)."
    )
    for evidence in (set(), {"build"}, {"build", "test"}):
        status = _gate_evidence_status(evidence)
        assert "missing evidence with an empty list" not in status
        assert not status.endswith("missing: .")


def test_progress_context_states_same_file_rewrite_bound() -> None:
    context = _model_progress_context(0, {"build", "test", "export"}, [], [])
    assert "bounded at 4 rewrites" in context
    assert "write made only to reset the progress counter" in context


def test_model_progress_status_replaces_stale_copy_and_survives_compaction() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "task"},
    ]
    for turn in range(5):
        _replace_model_progress_status(messages, f"PROGRESS STATUS (system fact): {turn}")
        messages.append({"role": "assistant", "content": "x" * 500})
    compacted, _, _ = _compact_model_messages(messages, recent_turns=2)
    _replace_model_progress_status(compacted, "PROGRESS STATUS (system fact): newest")
    statuses = [
        message for message in compacted
        if str(message.get("content", "")).startswith(
            "PROGRESS STATUS (system fact):"
        )
    ]
    assert statuses == [
        {"role": "user", "content": "PROGRESS STATUS (system fact): newest"}
    ]


def test_progress_ledger_is_bounded_and_kept_outside_compacted_file_bodies() -> None:
    ledger: list[str] = []
    for index in range(40):
        _remember_progress_ledger(ledger, f"read_file app/file-{index}.js")
    assert len(ledger) == 24
    assert "app/file-0.js" not in "\n".join(ledger)
    assert "app/file-39.js" in ledger[-1]


def test_successful_required_gate_command_counts_as_progress() -> None:
    assert _tool_counts_as_progress("run_command", "build")
    assert _progress_counters(2, 7, workspace_changed=True, failure=True) == (0, 0)


def test_done_refusal_names_missing_gates_and_stale_cause() -> None:
    from chitti.worker import _gate_refusal

    refusal = _gate_refusal(
        {"build"},
        "previous gate evidence was invalidated by sync-lockfile",
    )
    assert "missing current successful gates: test, export" in refusal
    assert "invalidated by sync-lockfile" in refusal
    assert "run those gates next" in refusal


def test_gate_refusal_surfaces_complete_evidence_contradiction() -> None:
    from chitti.worker import GateEvidenceContradiction, _gate_refusal

    with pytest.raises(GateEvidenceContradiction, match="complete required gate evidence"):
        _gate_refusal({"build", "test", "export"}, None)


def test_repeated_gate_refusal_is_nonproductive_but_shrinking_set_progresses() -> None:
    refusal, progress, missing = _gate_refusal_progress(
        set(), "previous gate evidence was invalidated by sync-lockfile", None
    )
    assert "build, test, export" in refusal
    assert progress
    refusal, progress, repeated = _gate_refusal_progress(
        set(), "previous gate evidence was invalidated by sync-lockfile", missing
    )
    assert not progress
    assert repeated == missing
    _, progress, smaller = _gate_refusal_progress(
        {"build"}, "previous gate evidence was invalidated by sync-lockfile", repeated
    )
    assert progress
    assert smaller == frozenset({"test", "export"})


def test_reviewer_return_does_not_reset_progress_counters() -> None:
    assert _progress_counters(
        2, 3, workspace_changed=False
    ) == (2, 4)


def test_repeated_failures_survive_inspection_until_failure_limit() -> None:
    failures, turns = _progress_counters(
        1, 1, workspace_changed=False, failure=False
    )
    assert (failures, turns) == (1, 2)
    assert _progress_counters(
        failures, turns, workspace_changed=False, failure=True
    ) == (2, 3)


def test_model_token_budget_round_trip() -> None:
    limits = WorkerLimits(model_iterations=40, model_tool_calls=120, model_tokens=30000)
    encoded = limits.as_json()
    assert encoded["model_tokens"] == 30000
    assert WorkerLimits.from_json(encoded).model_tokens == 30000


def test_done_condition_evidence_survives_task_boundaries() -> None:
    run_gate_evidence = {"build", "test", "export"}
    _record_gate_command(run_gate_evidence, "build")
    _record_gate_command(run_gate_evidence, "test")
    _record_gate_command(run_gate_evidence, "export")
    assert _task_done_checks(run_gate_evidence)
    assert _task_done_checks(run_gate_evidence)


def test_lockfile_sync_invalidates_gate_evidence() -> None:
    run_gate_evidence = {"build", "test", "export"}
    _record_gate_command(run_gate_evidence, "sync-lockfile")
    assert not run_gate_evidence


def test_source_changes_invalidate_gate_evidence_but_generated_paths_do_not() -> None:
    assert _source_path_invalidates_gates("app/page.js")
    assert _source_path_invalidates_gates("package-lock.json")
    assert not _source_path_invalidates_gates("artifacts/desktop.png")
    assert not _source_path_invalidates_gates("out/index.html")
    assert not _source_path_invalidates_gates("node_modules/three/index.js")
    evidence = {"build", "test", "export", "capture_screenshot"}
    _record_gate_command(evidence, "capture_screenshot")
    assert "capture_screenshot" in evidence
    evidence.clear()
    _record_gate_command(evidence, "build")
    _record_gate_command(evidence, "test")
    _record_gate_command(evidence, "export")
    _record_gate_command(evidence, "capture_screenshot")
    assert _task_done_checks(evidence)


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
            {"role": "assistant", "content": f"old {index} " + ("x" * 200)}
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


def test_model_history_compaction_skips_negligible_savings() -> None:
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "task contract"},
        *[
            {"role": "assistant", "content": f"old {index}"}
            for index in range(10)
        ],
        {"role": "assistant", "content": "recent"},
    ]
    compacted, changed, removed = _compact_model_messages(messages, recent_turns=2)
    assert compacted == messages
    assert not changed
    assert removed == 0


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
        {"role": "assistant", "content": "old " + ("x" * 300)},
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


def test_rejected_native_calls_are_answered_with_tool_results() -> None:
    completion = ModelCompletion(
        content="",
        model="coder",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost_usd=0,
        tool_calls=(
            ModelToolCall(id="call-1", name="unknown", arguments={}),
            ModelToolCall(id="call-2", name="also-unknown", arguments={}),
        ),
    )
    exchange = _tool_rejection_exchange(completion, "TOOL FAILURE: rejected")
    assert exchange[0]["role"] == "assistant"
    assert [item["tool_call_id"] for item in exchange[1:]] == ["call-1", "call-2"]
    assert all(item["role"] == "tool" for item in exchange[1:])


def test_native_batch_history_can_answer_each_call_and_unexecuted_remainder() -> None:
    completion = ModelCompletion(
        content="",
        model="coder",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost_usd=0,
        tool_calls=(
            ModelToolCall(id="call-1", name="list_files", arguments={}),
            ModelToolCall(id="call-2", name="write_file", arguments={}),
            ModelToolCall(id="call-3", name="run_command", arguments={}),
        ),
    )
    results = [
        _tool_result_message(completion.tool_calls[0], "listed"),
        _tool_result_message(completion.tool_calls[1], "TOOL FAILURE: write_file failed"),
        *_unexecuted_tool_results(
            completion.tool_calls[2:],
            "TOOL FAILURE: not executed because an earlier tool call in "
            "this batch failed",
        ),
    ]
    assert [item["tool_call_id"] for item in results] == [
        "call-1", "call-2", "call-3"
    ]
    assert results[2]["role"] == "tool"
    assert "not executed" in results[2]["content"]


def test_reviewer_diagnosis_request_contains_no_tool_shaped_turns() -> None:
    messages = _reviewer_diagnosis_messages(
        "Capture page",
        "Capture phone and desktop screenshots.",
        "capture_screenshot",
        "TOOL FAILURE: static export server exited",
    )
    assert all(message["role"] in {"system", "user"} for message in messages)
    assert all("tool_calls" not in message for message in messages)
    assert all(message["role"] != "tool" for message in messages)
    assert "capture_screenshot" in messages[1]["content"]


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
    detail = _model_response_failure(completion)
    assert detail is not None
    assert "be brief" in detail
    assert not completion.message_fields


def test_truncated_native_tool_call_is_rejected_not_executed() -> None:
    completion = ModelCompletion(
        content="",
        model="coder",
        prompt_tokens=10,
        completion_tokens=8192,
        total_tokens=8202,
        cost_usd=0.01,
        finish_reason="length",
        tool_calls=(
            ModelToolCall(id="partial", name="write_file", arguments={}),
        ),
    )
    detail = _model_response_failure(completion)
    assert detail is not None
    assert "be brief" in detail
    assert "split large file writes" in detail


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


def test_successful_command_resets_write_stall_counter_but_capture_does_not() -> None:
    writes_without_command = 3
    writes_without_command = _reset_file_write_counter(
        "run_command", writes_without_command
    )
    assert writes_without_command == 0
    assert _file_write_stall("app/page.js", 1, writes_without_command) is None
    assert _reset_file_write_counter("capture_screenshot", 3) == 3


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
    with pytest.raises(ValueError, match="single write exceeds model write-byte budget"):
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
