import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chitti.previews import (
    MAX_PREVIEW_FILE_BYTES,
    build_manifest,
    preview_is_active,
    safe_preview_file,
    validate_result_binding,
)


def test_manifest_rejects_symlink_out(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (root / "link").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        build_manifest(root)


def test_manifest_rejects_symlinked_directory(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "assets").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        build_manifest(root)


def test_manifest_rejects_special_file(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    socket_path = root / "socket"
    sock = socket.socket(socket.AF_UNIX)
    try:
        sock.bind(str(socket_path))
        with pytest.raises(ValueError, match="regular"):
            build_manifest(root)
    finally:
        sock.close()


def test_manifest_rejects_oversized_file(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    (root / "large").write_bytes(b"x" * (MAX_PREVIEW_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        build_manifest(root)


def test_manifest_rejects_too_many_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "out"
    root.mkdir()
    monkeypatch.setattr("chitti.previews.MAX_PREVIEW_FILES", 2)
    for index in range(3):
        (root / f"{index}.txt").write_text("x")
    with pytest.raises(ValueError, match="file-count"):
        build_manifest(root)


@pytest.mark.parametrize("name", [".git", ".github", ".env", "node_modules", ".next"])
def test_manifest_rejects_denied_paths(tmp_path: Path, name: str) -> None:
    root = tmp_path / "out"
    root.mkdir()
    (root / name).mkdir()
    with pytest.raises(ValueError, match="denied"):
        build_manifest(root)


def test_preview_path_confinement_and_symlink_rejection(tmp_path: Path) -> None:
    preview = tmp_path / "preview"
    preview.mkdir()
    (preview / "index.html").write_text("ok")
    (preview / "escape").symlink_to(tmp_path / "outside")
    fd, _ = safe_preview_file(tmp_path, "preview", "index.html")
    try:
        assert Path(f"/proc/self/fd/{fd}").exists()
    finally:
        import os

        os.close(fd)
    with pytest.raises(ValueError):
        safe_preview_file(tmp_path, "preview", "../outside")
    with pytest.raises(OSError):
        safe_preview_file(tmp_path, "preview", "escape")


def test_result_binding_rejects_substitution() -> None:
    kwargs = {
        "revision_hash": "r",
        "manifest_revision_hash": "r",
        "approval_manifest_digest": "m",
        "manifest_digest": "m",
        "approval_reviewer_sha256": "review",
        "reviewer_sha256": "review",
        "approval_diff_sha256": "diff",
        "diff_sha256": "diff",
    }
    assert validate_result_binding(**kwargs)
    assert not validate_result_binding(**{**kwargs, "manifest_digest": "other"})


def test_expired_preview_is_not_active() -> None:
    now = datetime.now(UTC)
    assert preview_is_active(now + timedelta(minutes=1), now)
    assert not preview_is_active(now, now)
    assert not preview_is_active(now - timedelta(minutes=1), now)
