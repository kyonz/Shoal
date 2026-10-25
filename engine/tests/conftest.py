from __future__ import annotations

import pytest

from shoal.config import Settings
from shoal.core import clock
from shoal.engine import Engine


@pytest.fixture
async def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOAL_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/shoal.db", public_url="http://shoal.test")
    eng = Engine(settings)
    await eng.start()
    yield eng
    await eng.close()
    clock.reset()


@pytest.fixture
async def tenant(engine):
    return await engine.store.create_tenant("acme", "Acme Corp")
