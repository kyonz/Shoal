"""Alembic environment: runs on the connection Shoal hands it (see shoal.store.migrate)."""

from __future__ import annotations

from alembic import context

from shoal.store import tables

target_metadata = tables.Base.metadata


def run() -> None:
    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError("run migrations through `shoal db upgrade` or Store.migrate()")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite alters tables by copying them
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run()
