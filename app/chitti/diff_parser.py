from __future__ import annotations

import re
from pathlib import Path

GENERATED_DIFF_ROOTS = {"out", "dist", "build", ".next"}
GENERATED_DIFF_FILENAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}


def diff_file_role(path: str) -> str:
    parts = Path(path).parts
    if parts and (
        parts[0] in GENERATED_DIFF_ROOTS
        or Path(path).name in GENERATED_DIFF_FILENAMES
    ):
        return "generated"
    return "authored"


def parse_diff(payload: bytes) -> list[dict[str, object]]:
    text_payload = payload.decode("utf-8", errors="replace")
    lines = text_payload.splitlines(keepends=True)
    entries: list[dict[str, object]] = []
    current: list[str] = []
    current_path: str | None = None

    def append_entry() -> None:
        if current_path is None:
            return
        body = "".join(current)
        additions = sum(
            1 for line in current if line.startswith("+") and not line.startswith("+++")
        )
        deletions = sum(
            1 for line in current if line.startswith("-") and not line.startswith("---")
        )
        entries.append(
            {
                "index": len(entries),
                "kind": "diff",
                "path": current_path,
                "role": diff_file_role(current_path),
                "additions": additions,
                "deletions": deletions,
                "body_bytes": len(body.encode("utf-8")),
                "summary": f"+{additions} / -{deletions}",
            }
        )

    for line in lines:
        if line.startswith("diff --git "):
            append_entry()
            current = [line]
            match = re.match(r"diff --git a/(.+) b/(.+)\n?$", line.rstrip("\n"))
            current_path = match.group(2) if match else None
        elif current_path is not None:
            current.append(line)
    append_entry()
    return entries
