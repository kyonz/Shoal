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
    async with engine.store.engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE alembic_version")
        await conn.exec_driver_sql("DROP TABLE run_signals")  # added by 0002
        await conn.exec_driver_sql("DROP TABLE user_credentials")  # added by 0004
        await conn.exec_driver_sql("ALTER TABLE runs DROP COLUMN orchestrator")  # added by 0005
        await conn.exec_driver_sql("DROP TABLE limiter_leases")  # added by 0007
        await conn.exec_driver_sql("DROP TABLE webhook_inbox")
        await conn.exec_driver_sql("DROP INDEX ix_runs_tenant_status")
        await conn.exec_driver_sql("DROP INDEX ix_runs_lease")
        await conn.exec_driver_sql("ALTER TABLE runs DROP COLUMN lane")
        await conn.exec_driver_sql("DROP INDEX ix_runs_idempotency")  # added by 0006
        await conn.exec_driver_sql("ALTER TABLE runs DROP COLUMN idempotency_key")
        await conn.exec_driver_sql("DROP INDEX ix_run_events_created")
    assert await engine.store.migrate() == "0007"
