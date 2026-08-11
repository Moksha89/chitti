from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_PREVIEW_FILE_BYTES = 10 * 1024 * 1024
MAX_PREVIEW_TOTAL_BYTES = 50 * 1024 * 1024
MAX_PREVIEW_FILES = 500
MAX_PREVIEW_DEPTH = 8
DENIED_NAMES = {
    ".git",
    ".github",
    ".env",
    ".env.local",
    ".env.production",
    "node_modules",
    ".next",
    ".npm",
    ".npm-cache",
    ".cache",
}


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ExportManifest:
    entries: tuple[ManifestEntry, ...]
    total_bytes: int
    max_depth: int
    digest: str

    def as_json(self) -> list[dict[str, object]]:
        return [
            {"path": item.path, "size": item.size, "sha256": item.sha256}
            for item in self.entries
        ]


def validate_result_binding(
    *,
    revision_hash: str,
    manifest_revision_hash: str,
    approval_manifest_digest: str,
    manifest_digest: str,
    approval_reviewer_sha256: str,
    reviewer_sha256: str,
    approval_diff_sha256: str,
    diff_sha256: str,
) -> bool:
    return (
        revision_hash == manifest_revision_hash
        and approval_manifest_digest == manifest_digest
        and approval_reviewer_sha256 == reviewer_sha256
        and approval_diff_sha256 == diff_sha256
    )


def preview_is_active(expires_at: datetime, now: datetime | None = None) -> bool:
    current = now or datetime.now(UTC)
    return expires_at > current


def manifest_from_json(value: object, digest: str) -> ExportManifest:
    if not isinstance(value, list):
        raise ValueError("stored preview manifest is invalid")
    entries = tuple(
        ManifestEntry(
            path=str(item["path"]),
            size=int(item["size"]),
            sha256=str(item["sha256"]),
        )
        for item in value
        if isinstance(item, dict)
    )
    canonical = json.dumps(
        [entry.__dict__ for entry in entries], sort_keys=True, separators=(",", ":")
    ).encode()
    if hashlib.sha256(canonical).hexdigest() != digest:
        raise ValueError("stored preview manifest digest mismatch")
    return ExportManifest(
        entries,
        sum(entry.size for entry in entries),
        max((entry.path.count("/") for entry in entries), default=0),
        digest,
    )


def _check_name(name: str) -> None:
    lowered = name.lower()
    if (
        name in {"", ".", ".."}
        or lowered in DENIED_NAMES
        or lowered.startswith(".env")
        or lowered in {"credentials", "secrets"}
    ):
        raise ValueError(f"preview path is denied: {name}")


def _open_directory(path: str | bytes, parent: int | None = None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(path, flags, dir_fd=parent)


def _read_regular(fd: int, name: str, maximum: int) -> tuple[int, str]:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    file_fd = os.open(name, flags, dir_fd=fd)
    try:
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError(f"preview entry is not a regular file: {name}")
        if file_stat.st_size > maximum:
            raise ValueError(f"preview file exceeds size limit: {name}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ValueError(f"preview file exceeds size limit: {name}")
            digest.update(chunk)
        return total, digest.hexdigest()
    finally:
        os.close(file_fd)


def build_manifest(root: Path) -> ExportManifest:
    root_fd = _open_directory(str(root))
    entries: list[ManifestEntry] = []
    total_bytes = 0
    max_depth = 0

    def walk(directory_fd: int, relative: str, depth: int) -> None:
        nonlocal total_bytes, max_depth
        if depth > MAX_PREVIEW_DEPTH:
            raise ValueError("preview directory depth limit exceeded")
        max_depth = max(max_depth, depth)
        with os.scandir(directory_fd) as iterator:
            for item in iterator:
                _check_name(item.name)
                path = f"{relative}/{item.name}" if relative else item.name
                if item.is_symlink():
                    raise ValueError(f"preview symlink is denied: {path}")
                if item.is_dir(follow_symlinks=False):
                    child_fd = _open_directory(item.name, directory_fd)
                    try:
                        walk(child_fd, path, depth + 1)
                    finally:
                        os.close(child_fd)
                    continue
                if not item.is_file(follow_symlinks=False):
                    raise ValueError(f"preview entry is not a regular file: {path}")
                size, digest = _read_regular(directory_fd, item.name, MAX_PREVIEW_FILE_BYTES)
                total_bytes += size
                if total_bytes > MAX_PREVIEW_TOTAL_BYTES:
                    raise ValueError("preview total size limit exceeded")
                entries.append(ManifestEntry(path, size, digest))
                if len(entries) > MAX_PREVIEW_FILES:
                    raise ValueError("preview file-count limit exceeded")

    try:
        walk(root_fd, "", 0)
    finally:
        os.close(root_fd)
    entries.sort(key=lambda entry: entry.path)
    canonical = json.dumps(
        [entry.__dict__ for entry in entries], sort_keys=True, separators=(",", ":")
    ).encode()
    return ExportManifest(tuple(entries), total_bytes, max_depth, hashlib.sha256(canonical).hexdigest())


def copy_export(root: Path, destination: Path) -> ExportManifest:
    manifest = build_manifest(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o750)
    for entry in manifest.entries:
        source_parts = entry.path.split("/")
        source_fd = _open_directory(str(root))
        try:
            for part in source_parts[:-1]:
                next_fd = _open_directory(part, source_fd)
                os.close(source_fd)
                source_fd = next_fd
            input_fd = os.open(source_parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
            try:
                output = destination / entry.path
                output.parent.mkdir(parents=True, exist_ok=True)
                output_fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
                try:
                    while True:
                        chunk = os.read(input_fd, 1024 * 1024)
                        if not chunk:
                            break
                        os.write(output_fd, chunk)
                finally:
                    os.close(output_fd)
            finally:
                os.close(input_fd)
        finally:
            os.close(source_fd)
    return manifest


def preview_id() -> str:
    return uuid.uuid4().hex


def safe_preview_file(root: Path, preview: str, requested: str) -> tuple[int, str]:
    if not preview or "/" in preview or preview in {".", ".."}:
        raise ValueError("invalid preview id")
    parts = Path(requested).parts
    if not requested or Path(requested).is_absolute() or ".." in parts:
        raise ValueError("preview path escapes root")
    directory_fd = _open_directory(str(root))
    try:
        preview_fd = _open_directory(preview, directory_fd)
        try:
            current_fd = preview_fd
            for part in parts[:-1]:
                _check_name(part)
                next_fd = _open_directory(part, current_fd)
                if current_fd != preview_fd:
                    os.close(current_fd)
                current_fd = next_fd
            _check_name(parts[-1])
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current_fd)
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                os.close(file_fd)
                raise ValueError("preview target is not a regular file")
            return file_fd, parts[-1]
        finally:
            if "current_fd" in locals() and current_fd != preview_fd:
                os.close(current_fd)
            os.close(preview_fd)
    finally:
        os.close(directory_fd)


def remove_preview(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
