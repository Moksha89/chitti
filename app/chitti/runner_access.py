from __future__ import annotations

import re
from importlib import import_module
from pathlib import Path
from typing import Any

RUNNER_ACCESS_MODULES = (
    "chitti.runner",
    "chitti.worker",
    "chitti.reminders",
    "chitti.runner_health",
)
RUNNER_ACCESS_EXCLUSIONS = {
    # These helpers also serve the application process, but the runner never
    # calls their application-only mutation paths.
    ("worker_runs", "INSERT"),
    ("reminders", "INSERT"),
}
_TABLE_REFERENCE = re.compile(
    r"\b(DELETE\s+FROM|FROM|JOIN|INTO|UPDATE)\s+([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_FOR_UPDATE = re.compile(r"\bFOR\s+UPDATE\s+OF\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
_TABLE_ALIAS = re.compile(
    r"\bFROM\s+([a-z_][a-z0-9_]*)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE
)


def required_privileges(
    source_texts: list[str], known_tables: set[str] | None = None
) -> dict[str, set[str]]:
    # This deliberately stays a small regex scan, so dynamic table names and
    # unusual SQL shapes can be missed. False positives fail deployment safely;
    # the durable runner health surface is the backstop for missed references.
    privileges: dict[str, set[str]] = {}
    aliases: dict[str, str] = {}
    for source in source_texts:
        for match in _TABLE_ALIAS.finditer(source):
            aliases[match.group(2).lower()] = match.group(1).lower()
        for match in _TABLE_REFERENCE.finditer(source):
            verb, table = match.groups()
            table = table.lower()
            if known_tables is not None and table not in known_tables:
                continue
            privilege = "DELETE" if verb.upper().startswith("DELETE") else {
                "FROM": "SELECT",
                "JOIN": "SELECT",
                "INTO": "INSERT",
                "UPDATE": "UPDATE",
            }[verb.upper()]
            if (table, privilege) in RUNNER_ACCESS_EXCLUSIONS:
                continue
            privileges.setdefault(table, set()).add(privilege)
        for match in _FOR_UPDATE.finditer(source):
            table = aliases.get(match.group(1).lower())
            if table is not None and (
                known_tables is None or table in known_tables
            ):
                if (table, "UPDATE") in RUNNER_ACCESS_EXCLUSIONS:
                    continue
                privileges.setdefault(table, set()).add("UPDATE")
    return privileges


async def assert_runner_privileges(
    conn: Any,
    source_texts: list[str] | None = None,
    known_tables: set[str] | None = None,
) -> None:
    tables = known_tables or {
        str(row["table_name"])
        for row in await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
    }
    loaded_runtime_sources = source_texts is None
    if loaded_runtime_sources:
        source_texts = []
        for module_name in RUNNER_ACCESS_MODULES:
            try:
                module = import_module(module_name)
            except Exception as exc:
                raise SystemExit(
                    f"runner privilege source is not importable: {module_name}"
                ) from exc
            module_file = getattr(module, "__file__", None)
            if not module_file:
                raise SystemExit(
                    f"runner privilege source has no file: {module_name}"
                )
            try:
                source = Path(module_file).resolve().read_text(encoding="utf-8")
            except OSError as exc:
                raise SystemExit(
                    f"runner privilege source cannot be read: {module_name}"
                ) from exc
            if not source.strip():
                raise SystemExit(f"runner privilege source is empty: {module_name}")
            source_texts.append(source)
    assert source_texts is not None
    if loaded_runtime_sources and len(source_texts) != len(RUNNER_ACCESS_MODULES):
        raise SystemExit("runner privilege source derivation is incomplete")
    required = required_privileges(source_texts, tables)
    if len(required) < len(source_texts):
        raise SystemExit("runner privilege source derivation found too few tables")
    for table, privileges in required.items():
        for privilege in privileges:
            allowed = await conn.fetchval(
                "SELECT has_table_privilege(current_user, $1, $2)",
                table,
                privilege,
            )
            if not allowed:
                raise SystemExit(f"runner lacks {privilege} on {table}")
        if "INSERT" in privileges:
            sequence = await conn.fetchval(
                "SELECT pg_get_serial_sequence($1, 'id')", table
            )
            if sequence is not None and not await conn.fetchval(
                "SELECT has_sequence_privilege(current_user, $1, 'USAGE')",
                sequence,
            ):
                raise SystemExit(f"runner lacks sequence usage on {sequence}")

    if await conn.fetchval(
        "SELECT has_table_privilege(current_user, 'worker_runs', 'INSERT')"
    ):
        raise SystemExit("runner unexpectedly has INSERT on worker_runs")
    worker_runs_sequence = await conn.fetchval(
        "SELECT pg_get_serial_sequence('worker_runs', 'id')"
    )
    if worker_runs_sequence and await conn.fetchval(
        "SELECT has_sequence_privilege(current_user, $1, 'USAGE')",
        worker_runs_sequence,
    ):
        raise SystemExit(
            f"runner unexpectedly has sequence usage on {worker_runs_sequence}"
        )
