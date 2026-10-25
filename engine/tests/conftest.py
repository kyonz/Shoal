from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from shoal.config import Settings
from shoal.core import clock
from shoal.engine import Engine

# Run the suite against Postgres with e.g.
#   SHOAL_TEST_DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/postgres uv run pytest
TEST_DATABASE_URL = os.environ.get("SHOAL_TEST_DATABASE_URL")
# Run the suite with runs driven by Temporal (needs the temporal extra) with e.g.
#   temporal server start-dev &  SHOAL_TEST_TEMPORAL_ADDRESS=localhost:7233 uv run pytest
TEST_TEMPORAL_ADDRESS = os.environ.get("SHOAL_TEST_TEMPORAL_ADDRESS")


@pytest.fixture(autouse=True)
def _temporal_orchestrator(monkeypatch):
    """With SHOAL_TEST_TEMPORAL_ADDRESS, every engine in the suite uses Temporal and serves its own task queue."""
    if not TEST_TEMPORAL_ADDRESS:
        return
    monkeypatch.setenv("SHOAL_ORCHESTRATOR", "temporal")
    monkeypatch.setenv("SHOAL_TEMPORAL_ADDRESS", TEST_TEMPORAL_ADDRESS)
    start = Engine.start

    async def start_and_serve(self: Engine) -> None:
        # Installations sharing a Temporal namespace need their own task queues (here: the hub and member
        # engines of the federation tests), or one installation's workers pick up the other's runs.
        self.settings.temporal_task_queue = f"shoal-test-{uuid.uuid4().hex[:12]}"
        await start(self)
        if self.orchestrator.name == "temporal":
            task = asyncio.create_task(self.orchestrator.serve())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    monkeypatch.setattr(Engine, "start", start_and_serve)


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
