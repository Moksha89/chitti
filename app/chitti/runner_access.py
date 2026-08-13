from __future__ import annotations

import ast
import re
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Any

RUNNER_ENTRYPOINT = "chitti.runner"
RUNNER_ACCESS_EXCLUSIONS = {
    # These helpers also serve the application process, but the runner never
    # calls their application-only mutation paths.
    ("brand_profiles", "INSERT"),
    ("brand_profile_history", "INSERT"),
    ("decision_forgets", "INSERT"),
    ("decisions", "INSERT"),
    ("decisions", "UPDATE"),
    ("memory_chunks", "INSERT"),
    ("memory_conflicts", "INSERT"),
    ("memory_conflicts", "UPDATE"),
    ("plan_approvals", "INSERT"),
    ("plan_jobs", "INSERT"),
    ("plan_jobs", "UPDATE"),
    ("plan_revisions", "INSERT"),
    ("worker_runs", "INSERT"),
    ("reminders", "INSERT"),
    ("reminders", "UPDATE"),
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


def _imported_local_modules(module_name: str, source: str) -> set[str]:
    tree = ast.parse(source)
    package = module_name.rpartition(".")[0]
    prefix = module_name.split(".", 1)[0]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(
                alias.name
                for alias in node.names
                if alias.name == prefix or alias.name.startswith(f"{prefix}.")
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                if node.level > len(base) + 1:
                    continue
                base = base[: len(base) + 1 - node.level]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            if not module or not (
                module == prefix or module.startswith(f"{prefix}.")
            ):
                continue
            imported.add(module)
            if node.module is None:
                imported.update(
                    f"{module}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
    return imported


def runner_source_texts(entrypoint: str = RUNNER_ENTRYPOINT) -> list[str]:
    """Read every local Python module reachable from the runner entrypoint."""
    pending = [entrypoint]
    visited: set[str] = set()
    sources: list[str] = []
    while pending:
        module_name = pending.pop()
        if module_name in visited:
            continue
        visited.add(module_name)
        try:
            module = import_module(module_name)
        except Exception as exc:
            raise SystemExit(
                f"runner privilege source is not importable: {module_name}"
            ) from exc
        module_file = getattr(module, "__file__", None)
        if not module_file:
            raise SystemExit(f"runner privilege source has no file: {module_name}")
        try:
            source = Path(module_file).resolve().read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(
                f"runner privilege source cannot be read: {module_name}"
            ) from exc
        if not source.strip():
            raise SystemExit(f"runner privilege source is empty: {module_name}")
        sources.append(source)
        for imported_name in _imported_local_modules(module_name, source):
            try:
                spec = find_spec(imported_name)
            except (ImportError, ModuleNotFoundError, ValueError):
                continue
            if spec is not None and spec.origin and spec.origin.endswith(".py"):
                pending.append(imported_name)
    return sources


async def owned_sequences(conn: Any, table: str) -> list[str]:
    try:
        columns = await conn.fetch(
            "SELECT a.attname AS column_name "
            "FROM pg_attribute AS a "
            "JOIN pg_class AS c ON c.oid = a.attrelid "
            "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = $1 "
            "AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY a.attnum",
            table,
        )
        sequences: list[str] = []
        for row in columns:
            sequence = await conn.fetchval(
                "SELECT pg_get_serial_sequence('public.' || $1, $2)",
                table,
                row["column_name"],
            )
            if sequence is not None and sequence not in sequences:
                sequences.append(str(sequence))
        return sequences
    except Exception as exc:
        raise SystemExit(
            f"runner sequence discovery failed for {table}: {exc}"
        ) from exc


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
    if source_texts is None:
        source_texts = runner_source_texts()
    if not source_texts:
        raise SystemExit("runner privilege source derivation produced no sources")
    required = required_privileges(source_texts, tables)
    if not required:
        raise SystemExit("runner privilege derivation produced no table expectations")
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
            for sequence in await owned_sequences(conn, table):
                if not await conn.fetchval(
                    "SELECT has_sequence_privilege(current_user, $1, 'USAGE')",
                    sequence,
                ):
                    raise SystemExit(f"runner lacks sequence usage on {sequence}")

    if await conn.fetchval(
        "SELECT has_table_privilege(current_user, 'worker_runs', 'INSERT')"
    ):
        raise SystemExit("runner unexpectedly has INSERT on worker_runs")
    for worker_runs_sequence in await owned_sequences(conn, "worker_runs"):
        if await conn.fetchval(
            "SELECT has_sequence_privilege(current_user, $1, 'USAGE')",
            worker_runs_sequence,
        ):
            raise SystemExit(
                "runner unexpectedly has sequence usage on "
                f"{worker_runs_sequence}"
            )
    if "brand_profiles" in tables:
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            if await conn.fetchval(
                "SELECT has_table_privilege(current_user, 'brand_profiles', $1)",
                privilege,
            ):
                raise SystemExit(
                    f"runner unexpectedly has {privilege} on brand_profiles"
                )
