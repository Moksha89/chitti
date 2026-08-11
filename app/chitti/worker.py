from typing import Protocol


class WorkerDispatcher(Protocol):
    """Phase 2 seam; Phase 1 intentionally has no worker implementation."""

    async def dispatch(self, task: str, project: str) -> str: ...
