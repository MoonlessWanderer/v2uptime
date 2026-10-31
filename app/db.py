"""SQLite database setup (WAL, busy_timeout) and async helpers.

Peewee is synchronous.  Code running inside the event loop must never touch
the database directly; it calls :func:`run_db`, which executes the function in
a worker thread.  Peewee keeps one connection per thread, so this is safe.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, TypeVar

from peewee import DatabaseProxy, SqliteDatabase
from playhouse.migrate import SqliteMigrator, migrate

log = logging.getLogger(__name__)

db = DatabaseProxy()

PRAGMAS: dict[str, Any] = {
    "journal_mode": "wal",
    "busy_timeout": 10000,
    "synchronous": "normal",
    "foreign_keys": 1,
    "cache_size": -16000,
    "temp_store": "memory",
}

T = TypeVar("T")


def init_db(path: str | Path) -> SqliteDatabase:
    """Open the database, create tables and add columns missing after updates."""
    from app.models import ALL_MODELS

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    database = SqliteDatabase(str(path), pragmas=PRAGMAS, timeout=10)
    db.initialize(database)
    database.connect(reuse_if_open=True)
    database.create_tables(ALL_MODELS, safe=True)
    _add_missing_columns(database, ALL_MODELS)
    _retire_subscriptions(database)
    _drop_retired_settings()
    _migrate_legacy_defaults()
    return database


def _retire_subscriptions(database: SqliteDatabase) -> None:
    """Drop stored subscription links. Servers that came from one stay, as ordinary servers.

    SQLite will not ``DROP COLUMN subscription_id`` while the table's foreign key
    still names that column. A failed drop used to be ignored and ``subscriptions``
    was removed anyway. The next start then ran ``UPDATE servers SET
    subscription_id = NULL`` with foreign keys on, and SQLite aborted with
    ``no such table: main.subscriptions``. The parent table is dropped only after
    the column is gone. When ``DROP COLUMN`` cannot do it, ``servers`` is rebuilt
    without that column and the rows are copied across.
    """
    tables = set(database.get_tables())
    has_column = "servers" in tables and _has_column(database, "servers", "subscription_id")
    has_table = "subscriptions" in tables
    if not has_column and not has_table:
        return
    if database.in_transaction():
        database.commit()
    database.execute_sql("PRAGMA foreign_keys = OFF")
    try:
        state = database.execute_sql("PRAGMA foreign_keys").fetchone()
        if state is None or state[0]:
            raise RuntimeError("could not disable foreign keys")
        if has_column:
            _drop_column(database, "servers", "subscription_id")
        if has_table:
            database.execute_sql('DROP TABLE "subscriptions"')
        log.info("migration: removed subscriptions")
    finally:
        database.execute_sql("PRAGMA foreign_keys = ON")


def _has_column(database: SqliteDatabase, table: str, column: str) -> bool:
    return any(item.name == column for item in database.get_columns(table))


def _drop_column(database: SqliteDatabase, table: str, column: str) -> None:
    table_sql, column_sql = _quote(_ident(table)), _quote(_ident(column))
    try:
        database.execute_sql(f"ALTER TABLE {table_sql} DROP COLUMN {column_sql}")
        return
    except Exception as exc:
        log.info(
            "migration: drop column failed; rebuilding the table",
            extra={"table": table, "column": column, "error": str(exc)},
        )
        if database.in_transaction():
            database.rollback()
    _rebuild_without_column(database, table, column)


def _rebuild_without_column(database: SqliteDatabase, table: str, column: str) -> None:
    """Copy ``table`` to a new table that has no ``column``, then rename it into place."""
    table = _ident(table)
    column = _ident(column)
    info = list(database.execute_sql(f"PRAGMA table_info({table})"))
    keep = [row for row in info if row[1] != column]
    if len(keep) == len(info):
        return
    indexes = []
    for name, statement in database.execute_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
            (table,)):
        cols = [item[2] for item in database.execute_sql(f"PRAGMA index_info({_ident(name)})")]
        if column in cols:
            continue
        if not statement or ";" in statement:
            raise RuntimeError(f"cannot rebuild {table}; index {name} is not a single command")
        indexes.append(statement)
    pieces, names, primary = [], [], []
    for _cid, name, typ, notnull, default, pk in keep:
        name = _ident(name)
        names.append(name)
        piece = f"{_quote(name)} {_column_type(typ)}"
        if notnull:
            piece += " NOT NULL"
        piece += _default_clause(default)
        if pk and sum(1 for row in keep if row[5]) == 1:
            piece += " PRIMARY KEY"
        pieces.append(piece)
        if pk:
            primary.append((pk, name))
    if len(primary) > 1:
        ordered = ", ".join(_quote(name) for _pk, name in sorted(primary))
        pieces.append(f"PRIMARY KEY ({ordered})")
    fresh = _ident(f"{table}__new")
    quoted = ", ".join(_quote(name) for name in names)
    with database.atomic():
        database.execute_sql(f"DROP TABLE IF EXISTS {_quote(fresh)}")
        database.execute_sql(f"CREATE TABLE {_quote(fresh)} ({', '.join(pieces)})")
        database.execute_sql(
            f"INSERT INTO {_quote(fresh)} ({quoted}) SELECT {quoted} FROM {_quote(table)}")
        database.execute_sql(f"DROP TABLE {_quote(table)}")
        database.execute_sql(f"ALTER TABLE {_quote(fresh)} RENAME TO {_quote(table)}")
        for statement in indexes:
            database.execute_sql(statement)


def _ident(name: str) -> str:
    if not name or not (name[0].isalpha() or name[0] == "_") or not all(ch.isalnum() or ch == "_" for ch in name):
        raise RuntimeError(f"cannot rebuild servers; unsupported name {name!r}")
    return name


def _quote(name: str) -> str:
    return f'"{name}"'


def _column_type(typ: str) -> str:
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_()")
    if not typ or any(ch not in allowed for ch in typ):
        raise RuntimeError(f"cannot rebuild servers; unsupported type {typ!r}")
    return typ


def _default_clause(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    body = value[1:] if value.startswith("-") else value
    numeric = bool(body) and body.count(".") <= 1 and any(ch.isdigit() for ch in body) and all(
        ch.isdigit() or ch == "." for ch in body)
    quoted = len(value) >= 2 and value.startswith("'") and value.endswith("'") and ";" not in value
    if value == "NULL" or numeric or quoted:
        return f" DEFAULT {value}"
    raise RuntimeError(f"cannot rebuild servers; unsupported default {value!r}")


def _drop_retired_settings() -> None:
    """Forget settings this version no longer reads. Unknown keys are ignored."""
    from app.models import Setting

    removed = Setting.delete().where(Setting.key == "use_jalali").execute()
    if removed:
        log.info("migration: removed retired setting", extra={"key": "use_jalali"})


def _migrate_legacy_defaults() -> None:
    """Replace untouched built-in copy. Custom admin values are left alone."""
    from app.legacy_defaults import LEGACY_DEFAULTS
    from app.models import Setting

    for key, old, new in LEGACY_DEFAULTS:
        updated = Setting.update(value=new).where((Setting.key == key) & (Setting.value == old)).execute()
        if updated:
            log.info("migration: replaced legacy default", extra={"key": key})


def _add_missing_columns(database: SqliteDatabase, models: list[type]) -> None:
    """Tiny forward-only migration: add columns that new code expects."""
    migrator = SqliteMigrator(database)
    operations = []
    for model in models:
        table = model._meta.table_name
        existing = {col.name for col in database.get_columns(table)}
        for field in model._meta.sorted_fields:
            if field.column_name not in existing:
                if not field.null and field.default is None:
                    field.null = True  # SQLite cannot add NOT NULL without default
                operations.append(migrator.add_column(table, field.column_name, field))
                log.info("migration: adding column", extra={"table": table, "column": field.column_name})
    if operations:
        with database.atomic():
            migrate(*operations)


async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking database function without blocking the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)
