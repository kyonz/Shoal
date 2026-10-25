"""The migrations must produce exactly the schema the models describe."""

from __future__ import annotations

from shoal.store import migrate


async def test_migrations_match_the_models(engine):
    async with engine.store.engine.connect() as conn:
        assert await conn.run_sync(migrate.current) == "0001"
        diff = await conn.run_sync(migrate.pending_changes)
    assert diff == [], f"models changed without a migration: run `shoal db revision -m ...` ({diff})"


async def test_existing_database_without_migrations_is_adopted(engine):
    async with engine.store.engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE alembic_version")
    assert await engine.store.migrate() == "0001"
