from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .memory import normalize_namespace


async def append_entry(
    session: AsyncSession, namespace: str, role: str, content: str
) -> None:
    if role not in {"user", "assistant"}:
        raise ValueError("invalid transcript role")
    await session.execute(
        text(
            "INSERT INTO chat_transcript_entries (namespace, role, content) "
            "VALUES (:namespace, :role, :content)"
        ),
        {
            "namespace": normalize_namespace(namespace),
            "role": role,
            "content": content,
        },
    )


async def recent_entries(
    session: AsyncSession, namespace: str, limit: int = 20
) -> list[dict[str, object]]:
    result = await session.execute(
        text(
            "SELECT role, content "
            "FROM chat_transcript_entries "
            "WHERE namespace = :namespace "
            "ORDER BY id DESC LIMIT :limit"
        ),
        {"namespace": normalize_namespace(namespace), "limit": limit},
    )
    return [dict(row._mapping) for row in reversed(list(result))]
