"""Schema migrations (Alembic), run on a connection from the store's own engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from shoal.store import tables

MIGRATIONS = Path(__file__).parent / "migrations"


def config(connection: Any = None) -> Config:
    cfg = Config()
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("version_locations", str(MIGRATIONS / "versions"))
    cfg.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    cfg.attributes["connection"] = connection
    return cfg


def upgrade(connection: Any, revision: str = "head") -> None:
    command.upgrade(config(connection), revision)


def stamp(connection: Any, revision: str) -> None:
    command.stamp(config(connection), revision)


def current(connection: Any) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def pending_changes(connection: Any) -> list[Any]:
    """Differences between the models and the migrated database (empty when a migration is not missing)."""
    ctx = MigrationContext.configure(connection, opts={"compare_type": True})
    return compare_metadata(ctx, tables.Base.metadata)


def revision(connection: Any, message: str) -> None:
    """Autogenerate a new migration from the models (developers: `shoal db revision -m ...`)."""
    command.revision(config(connection), message=message, autogenerate=True)
