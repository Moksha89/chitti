from __future__ import annotations

from typing import Any

from .runner_access import derived_sync_grants, runner_source_texts


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
            columns = await conn.fetch(
                "SELECT a.attname AS column_name FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = $1 "
                "AND a.attnum > 0 AND NOT a.attisdropped",
                table,
            )
            for column in columns:
                sequence = await conn.fetchval(
                    "SELECT pg_get_serial_sequence('public.' || $1, $2)",
                    table,
                    column["column_name"],
                )
                if sequence:
                    await conn.execute(f'GRANT USAGE, SELECT ON SEQUENCE "{sequence}" TO "{role}"')
