from __future__ import annotations

import pytest

from awm.config import Settings
from awm.core import clock
from awm.engine import Engine


@pytest.fixture
async def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/awm.db", public_url="http://awm.test")
    eng = Engine(settings)
    await eng.start()
    yield eng
    await eng.close()
    clock.reset()


@pytest.fixture
async def tenant(engine):
    return await engine.store.create_tenant("acme", "Acme Corp")
