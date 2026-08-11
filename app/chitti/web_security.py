def safe_next_path(value: str | None) -> str:
    candidate = value or "/"
    if "\\" in candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate
