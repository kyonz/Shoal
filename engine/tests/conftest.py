from __future__ import annotations

import os

import pytest

from shoal.config import Settings
from shoal.core import clock
from shoal.engine import Engine

# Run the suite against Postgres with e.g.
#   SHOAL_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres uv run pytest
TEST_DATABASE_URL = os.environ.get("SHOAL_TEST_DATABASE_URL")


async def _reset_postgres(url: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await admin.dispose()


@pytest.fixture
async def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOAL_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    url = TEST_DATABASE_URL or f"sqlite+aiosqlite:///{tmp_path}/shoal.db"
    if TEST_DATABASE_URL:
        await _reset_postgres(TEST_DATABASE_URL)
    settings = Settings(database_url=url, public_url="http://shoal.test")
    eng = Engine(settings)
    await eng.start()
    yield eng
    await eng.close()
    clock.reset()


@pytest.fixture
async def tenant(engine):
    return await engine.store.create_tenant("acme", "Acme Corp")
