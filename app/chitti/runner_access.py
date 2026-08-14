from __future__ import annotations

import ast
import re
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Any

RUNNER_ENTRYPOINT = "chitti.runner"
_TABLE_REFERENCE = re.compile(
    r"\b(DELETE\s+FROM|FROM|JOIN|INTO|UPDATE)\s+([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_FOR_UPDATE = re.compile(r"\bFOR\s+UPDATE\s+OF\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
_FOR_UPDATE_TABLE = re.compile(
    r"\bFROM\s+([a-z_][a-z0-9_]*)(?:\s+[a-z_][a-z0-9_]*)?\s+FOR\s+UPDATE\b",
    re.IGNORECASE,
)
_TABLE_ALIAS = re.compile(
    r"\bFROM\s+([a-z_][a-z0-9_]*)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE
)
_WRITE_TARGET = re.compile(
    r"\b(?:INSERT\s+INTO|(?<!DO )UPDATE|DELETE\s+FROM)\s+"
    r"([a-z_][a-z0-9_]*)\b",
    re.IGNORECASE,
)
_DELETE_USING = re.compile(
    r"\bDELETE\s+FROM\s+[a-z_][a-z0-9_]*\s+USING\s+"
    r"([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_RETURNING = re.compile(r"\bRETURNING\b", re.IGNORECASE)
_WRITE_PRIVILEGES = frozenset({"INSERT", "UPDATE", "DELETE"})
SENSITIVE_RUNNER_TABLES = frozenset(
    {
        "auth_sessions",
        "auth_users",
        "chat_transcript_entries",
        "credential_store",
        "decision_embeddings",
        "memory_chunks",
        "memory_conflicts",
        "memory_namespaces",
        "provider_credentials",
        "provider_keys",
        "google_oauth_credentials",
        "google_provider_accounts",
        "google_sync_state",
        "google_gmail_messages",
        "google_calendar_events",
        "google_account_audit",
        "google_email_actions",
        "google_email_action_approvals",
        "session_store",
    }
)


def application_only_sql(statement: Any) -> Any:
    return statement


def runner_sql(statement: Any) -> Any:
    return statement


def sync_sql(statement: Any) -> Any:
    return statement


def _mask_application_only_sql(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    lines = source.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    masked = list(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (
            isinstance(function, ast.Name)
            and function.id == "application_only_sql"
        ):
            continue
        if node.end_lineno is None or node.end_col_offset is None:
            continue
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        masked[start:end] = " " * (end - start)
    return "".join(masked)


def _scan_privileges(
    source: str, known_tables: set[str] | None = None
) -> dict[str, set[str]]:
    privileges: dict[str, set[str]] = {}
    aliases: dict[str, str] = {}
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
        privileges.setdefault(table, set()).add(privilege)
    for match in _FOR_UPDATE.finditer(source):
        table = aliases.get(match.group(1).lower())
        if table is not None and (
            known_tables is None or table in known_tables
        ):
            privileges.setdefault(table, set()).add("UPDATE")
    for match in _FOR_UPDATE_TABLE.finditer(source):
        privileges.setdefault(match.group(1).lower(), set()).add("UPDATE")
    write_targets = list(_WRITE_TARGET.finditer(source))
    for index, match in enumerate(write_targets):
        end = (
            write_targets[index + 1].start()
            if index + 1 < len(write_targets)
            else len(source)
        )
        statement = source[match.start() : end]
        table = match.group(1).lower()
        if _RETURNING.search(statement):
            privileges.setdefault(table, set()).add("SELECT")
        if re.search(r"\bON\s+CONFLICT\b[\s\S]*?\bDO\s+UPDATE\b", statement, re.I):
            privileges.setdefault(table, set()).add("UPDATE")
    for match in _DELETE_USING.finditer(source):
        privileges.setdefault(match.group(1).lower(), set()).add("SELECT")
    return privileges


def _declared_segments(source: str, marker: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    segments: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Name)
            and function.id == marker
        ):
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                segments.append(segment)
    return segments


def _declared_privileges(
    source_texts: list[str], marker: str, known_tables: set[str] | None = None
) -> dict[str, set[str]]:
    declared: dict[str, set[str]] = {}
    for source in source_texts:
        for segment in _declared_segments(source, marker):
            for table, privileges in _scan_privileges(segment, known_tables).items():
                declared.setdefault(table, set()).update(privileges)
    return declared


def application_only_privileges(
    source_texts: list[str], known_tables: set[str] | None = None
) -> dict[str, set[str]]:
    return _declared_privileges(source_texts, "application_only_sql", known_tables)


def runner_privileges(
    source_texts: list[str], known_tables: set[str] | None = None
) -> dict[str, set[str]]:
    return _declared_privileges(source_texts, "runner_sql", known_tables)


def _validate_runner_write_boundary(
    required: dict[str, set[str]],
    application_only: dict[str, set[str]],
    runner_declared: dict[str, set[str]],
) -> None:
    for table, privileges in required.items():
        overlap = (
            (privileges & _WRITE_PRIVILEGES)
            & application_only.get(table, set())
            - runner_declared.get(table, set())
        )
        if overlap:
            values = ", ".join(sorted(overlap))
            raise SystemExit(
                f"runner privilege derivation would widen application-only "
                f"writes on {table}: {values}"
            )


def required_privileges(
    source_texts: list[str], known_tables: set[str] | None = None
) -> dict[str, set[str]]:
    # This deliberately stays a small regex scan, so dynamic table names and
    # unusual SQL shapes can be missed. False positives fail deployment safely;
    # the durable runner health surface is the backstop for missed references.
    privileges: dict[str, set[str]] = {}
    for source in source_texts:
        for table, values in _scan_privileges(
            _mask_application_only_sql(source), known_tables
        ).items():
            privileges.setdefault(table, set()).update(values)
    return privileges


def derived_grants(
    source_texts: list[str], known_tables: set[str]
) -> dict[str, set[str]]:
    required = required_privileges(source_texts, known_tables)
    if not required:
        raise SystemExit("runner privilege derivation produced no table expectations")
    sensitive = sorted(set(required) & SENSITIVE_RUNNER_TABLES)
    if sensitive:
        raise SystemExit(
            "runner privilege derivation reached sensitive tables: "
            + ", ".join(sensitive)
        )
    _validate_runner_write_boundary(
        required,
        application_only_privileges(source_texts, known_tables),
        runner_privileges(source_texts, known_tables),
    )
    return required


def derived_sync_grants(
    source_texts: list[str], known_tables: set[str]
) -> dict[str, set[str]]:
    required = _declared_privileges(source_texts, "sync_sql", known_tables)
    if not required:
        raise SystemExit("Google sync privilege derivation produced no table expectations")
    allowed = {
        "google_provider_accounts",
        "google_oauth_credentials",
        "google_sync_state",
        "google_gmail_messages",
        "google_calendar_events",
        "runner_health",
        "google_email_actions",
        "google_email_action_approvals",
    }
    unexpected = sorted(set(required) - allowed)
    if unexpected:
        raise SystemExit("Google sync reached unexpected tables: " + ", ".join(unexpected))
    return required


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
    required = derived_grants(source_texts, tables)
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
    if "decisions" in tables:
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            if await conn.fetchval(
                "SELECT has_table_privilege(current_user, 'decisions', $1)",
                privilege,
            ):
                raise SystemExit(
                    f"runner unexpectedly has {privilege} on decisions"
                )


def _quoted_identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
        raise ValueError(f"unsafe PostgreSQL identifier: {value}")
    return f'"{value}"'


async def reconcile_runner_privileges(
    conn: Any,
    grantee: str = "chitti_runner",
    source_texts: list[str] | None = None,
) -> None:
    can_reconcile = await conn.fetchval(
        "SELECT EXISTS ("
        "SELECT 1 FROM information_schema.role_table_grants "
        "WHERE grantee = current_user AND table_schema = 'public' "
        "AND is_grantable = 'YES'"
        ")"
    )
    if not can_reconcile:
        raise SystemExit(
            "runner privilege reconciliation requires a database role "
            "that can grant and revoke privileges; current_user cannot"
        )
    tables = {
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
    required = derived_grants(source_texts, tables)
    grantee_sql = _quoted_identifier(grantee)
    await conn.execute(
        f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {grantee_sql}"
    )
    await conn.execute(
        f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {grantee_sql}"
    )
    for table, privileges in sorted(required.items()):
        values = ", ".join(sorted(privileges))
        print(f"runner derived grant {table}: {values}")
        await conn.execute(
            f"GRANT {values} ON {_quoted_identifier(table)} TO {grantee_sql}"
        )
        if "INSERT" in privileges:
            for sequence in await owned_sequences(conn, table):
                print(f"runner derived sequence grant {sequence}: USAGE, SELECT")
                await conn.execute(
                    f"GRANT USAGE, SELECT ON {sequence} TO {grantee_sql}"
                )
