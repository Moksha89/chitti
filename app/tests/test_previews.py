import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from chitti.auth import AuthManager
from chitti.main import app
from chitti.previews import (
    MAX_PREVIEW_FILE_BYTES,
    build_manifest,
    copy_export,
    preview_is_active,
    safe_preview_file,
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


def test_manifest_rejects_over_budget_total_before_recording_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "out"
    root.mkdir()
    monkeypatch.setattr("chitti.previews.MAX_PREVIEW_TOTAL_BYTES", 2)
    (root / "first.txt").write_text("ab")
    (root / "second.txt").write_text("c")
    with pytest.raises(ValueError, match="total size"):
        build_manifest(root)


def test_copy_export_returns_manifest_for_landed_tree(tmp_path: Path) -> None:
    source = tmp_path / "out"
    source.mkdir()
    (source / "index.html").write_text("home")
    (source / "assets").mkdir()
    (source / "assets" / "app.js").write_text("asset")
    destination = tmp_path / "published"
    expected = build_manifest(source)
    landed = copy_export(source, destination)
    assert landed == expected
    assert build_manifest(destination) == expected


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


def test_expired_preview_is_not_active() -> None:
    now = datetime.now(UTC)
    assert preview_is_active(now + timedelta(minutes=1), now)
    assert not preview_is_active(now, now)
    assert not preview_is_active(now - timedelta(minutes=1), now)


class _PreviewSession:
    async def __aenter__(self) -> "_PreviewSession":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, *_args: object, **_kwargs: object) -> "_PreviewResult":
        return _PreviewResult()


class _PreviewResult:
    def scalar_one_or_none(self) -> int:
        return 1


class _PreviewDatabase:
    def sessions(self) -> _PreviewSession:
        return _PreviewSession()


def test_authenticated_preview_serves_entry_and_nested_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = tmp_path / "site"
    (preview / "assets").mkdir(parents=True)
    (preview / "index.html").write_text("<h1>home</h1>")
    (preview / "assets" / "app.js").write_text("console.log('ok')")
    auth = AuthManager(
        "test-user",
        PasswordHasher().hash("x"),
        str(tmp_path / "state.json"),
        1,
    )
    auth.initialize()
    auth.must_change_password = False
    token, _ = auth.create_session("test-user")
    monkeypatch.setattr(app.state, "auth", auth, raising=False)
    monkeypatch.setattr(app.state, "database", _PreviewDatabase(), raising=False)
    monkeypatch.setattr(
        app.state,
        "settings",
        SimpleNamespace(preview_root=str(tmp_path)),
        raising=False,
    )
    client = TestClient(app)
    assert client.get("/previews/site").status_code == 401
    client.cookies.set("chitti_session", token)
    entry = client.get("/previews/site")
    directory = client.get("/previews/site/")
    asset = client.get("/previews/site/assets/app.js")
    assert entry.status_code == 200
    assert directory.status_code == 200
    assert entry.text == directory.text == "<h1>home</h1>"
    assert asset.status_code == 200
    assert asset.text == "console.log('ok')"
