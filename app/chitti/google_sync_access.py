from __future__ import annotations

from typing import Any

from .runner_access import derived_sync_grants, owned_sequences, runner_source_texts


def sync_source_texts() -> list[str]:
    return runner_source_texts("chitti.google_sync")


def sync_grants(known_tables: set[str]) -> dict[str, set[str]]:
    return derived_sync_grants(sync_source_texts(), known_tables)


async def reconcile_sync_privileges(conn: Any, role: str = "chitti_google_sync") -> None:
    tables = {
        str(row["table_name"])
        for row in await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }
    grants = sync_grants(tables)
    await conn.execute(f'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{role}"')
    await conn.execute(f'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{role}"')
    for table, privileges in grants.items():
        for privilege in privileges:
            await conn.execute(f'GRANT {privilege} ON TABLE "{table}" TO "{role}"')
        if "INSERT" in privileges:
            for sequence in await owned_sequences(conn, table):
                await conn.execute(f'GRANT USAGE, SELECT ON {sequence} TO "{role}"')
