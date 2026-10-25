"""The migrations must produce exactly the schema the models describe."""

from __future__ import annotations

from shoal.store import migrate


async def test_migrations_match_the_models(engine):
    async with engine.store.engine.connect() as conn:
        assert await conn.run_sync(migrate.current) == "0007"
        diff = await conn.run_sync(migrate.pending_changes)
    assert diff == [], f"models changed without a migration: run `shoal db revision -m ...` ({diff})"


async def test_existing_database_without_migrations_is_adopted(engine):
    """A database created before migrations existed has the 0001 schema: it is stamped, then upgraded."""
    from alembic import command

    async with engine.store.engine.begin() as conn:
        await conn.run_sync(lambda c: command.downgrade(migrate.config(c), "0001"))
        await conn.exec_driver_sql("DROP TABLE alembic_version")
    assert await engine.store.migrate() == "0007"
