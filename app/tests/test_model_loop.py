from pathlib import Path

import pytest

from chitti.worker import WorkerLimits, _confined_path, _parse_tool_call


def test_model_limits_round_trip() -> None:
    limits = WorkerLimits(model_iterations=3, model_tool_calls=7, model_write_bytes=1234)
    assert WorkerLimits.from_json(limits.as_json()) == limits


def test_model_tool_parser_rejects_malformed_and_unknown_shape() -> None:
    with pytest.raises(ValueError, match="valid JSON"):
        _parse_tool_call("not json")
    with pytest.raises(ValueError, match="one tool call"):
        _parse_tool_call('{"arguments": {}}')
    with pytest.raises(ValueError, match="arguments"):
        _parse_tool_call('{"tool": "write_file", "arguments": []}')


def test_model_paths_reject_traversal_and_symlink_escape(tmp_path: Path) -> None:
    assert _confined_path(tmp_path, "app/page.js") == tmp_path / "app/page.js"
    with pytest.raises(ValueError, match="escapes"):
        _confined_path(tmp_path, "../outside")
    outside = tmp_path.parent / "model-loop-outside"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        _confined_path(tmp_path, "link/file.txt")
