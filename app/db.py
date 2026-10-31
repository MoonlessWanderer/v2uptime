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
    """Drop stored subscription links. Servers that came from one stay, as ordinary servers."""
    tables = set(database.get_tables())
    if "servers" in tables:
        columns = {column.name for column in database.get_columns("servers")}
        if "subscription_id" in columns:
            database.execute_sql("UPDATE servers SET subscription_id = NULL")
            database.execute_sql("PRAGMA foreign_keys = OFF")
            try:
                database.execute_sql("ALTER TABLE servers DROP COLUMN subscription_id")
            except Exception:
                log.info("migration: subscription_id left unused; the model no longer reads it")
            finally:
                database.execute_sql("PRAGMA foreign_keys = ON")
    if "subscriptions" in set(database.get_tables()):
        database.execute_sql("PRAGMA foreign_keys = OFF")
        try:
            database.execute_sql("DROP TABLE subscriptions")
        finally:
            database.execute_sql("PRAGMA foreign_keys = ON")
        log.info("migration: removed subscriptions")


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
