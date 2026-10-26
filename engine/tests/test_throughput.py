"""Behaviour that keeps Shoal standing under heavy load: idempotent starts, retries, retention, pooling."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from shoal.agents.providers.base import ModelRequest, ModelResponse, ProviderUnavailable, retry_after_seconds
from shoal.api.app import create_app
from shoal.core import clock
from shoal.core.errors import StepError
from shoal.llm.router import ModelClient, ModelTarget

ADMIN = "test-admin-token"
HELLO = """
apiVersion: shoal.dev/v1
kind: Workflow
metadata: {name: hello}
spec:
  inputs: {name: {type: string}}
  steps:
    - {id: greet, type: set, values: {text: "Hello {{ inputs.name }}"}}
---
apiVersion: shoal.dev/v1
kind: Workflow
metadata: {name: other}
spec:
  steps:
    - {id: a, type: set, values: {x: 1}}
"""


@pytest.fixture
async def api(engine):
    engine.settings.admin_token = SecretStr(ADMIN)
    app = create_app(engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shoal.test") as client:
        r = await client.post("/v1/tenants", json={"slug": "acme"}, headers={"Authorization": f"Bearer {ADMIN}"})
        assert r.status_code == 201, r.text
        client.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        r = await client.post("/v1/apply?namespace=default", content=HELLO, headers={"content-type": "application/yaml"})
        assert r.status_code == 200, r.text
        yield client


# --------------------------------------------------------------------------- idempotent run starts


async def test_idempotency_key_returns_the_first_run(api, engine):
    url = "/v1/namespaces/default/workflows/hello/runs"
    first = await api.post(url, json={"inputs": {"name": "a"}}, headers={"Idempotency-Key": "req-1"})
    assert first.status_code == 201, first.text
    again = await api.post(url, json={"inputs": {"name": "a"}}, headers={"Idempotency-Key": "req-1"})
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    other = await api.post(url, json={"inputs": {"name": "a"}}, headers={"Idempotency-Key": "req-2"})
    assert other.status_code == 201 and other.json()["id"] != first.json()["id"]
    plain = [await api.post(url, json={"inputs": {"name": "a"}}) for _ in range(2)]
    assert plain[0].json()["id"] != plain[1].json()["id"]

    clash = await api.post("/v1/namespaces/default/workflows/other/runs", json={}, headers={"Idempotency-Key": "req-1"})
    assert clash.status_code == 409


async def test_concurrent_starts_with_one_key_make_one_run(engine, tenant):
    from shoal.core.models import parse_resource

    await engine.store.apply_resource(
        tenant.id,
        parse_resource({"apiVersion": "shoal.dev/v1", "kind": "Workflow", "metadata": {"name": "w"},
                        "spec": {"steps": [{"id": "a", "type": "set", "values": {"x": 1}}]}}),
    )
    import asyncio

    runs = await asyncio.gather(*(engine.executor.start_run(tenant.id, "w", idempotency_key="same") for _ in range(5)))
    assert len({r.id for r in runs}) == 1
    assert await engine.store.count_runs(tenant.id) == 1


# --------------------------------------------------------------------------- model retries


class _Flaky:
    def __init__(self, failures: int, retry_after: float | None = None):
        self.failures, self.retry_after, self.calls = failures, retry_after, 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderUnavailable("429: slow down", retry_after=self.retry_after)
        return ModelResponse(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn", usage={}, model="m")


def _request() -> ModelRequest:
    return ModelRequest(model="m", system="", messages=[{"role": "user", "content": "hi"}], tools=[], max_tokens=10, config={})


async def test_model_client_waits_and_retries_when_every_candidate_is_rate_limited(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("shoal.llm.router.asyncio.sleep", fake_sleep)
    provider = _Flaky(failures=2, retry_after=5)
    client = ModelClient([ModelTarget("g", "p", provider, "m")], retries=3, backoff=1, max_backoff=30)  # type: ignore[arg-type]
    response = await client.complete(_request())
    assert response.content[0]["text"] == "ok" and provider.calls == 3
    assert len(slept) == 2 and all(4 <= s <= 6 for s in slept)  # Retry-After wins over the 1s backoff

    exhausted = ModelClient([ModelTarget("g", "p", _Flaky(failures=10), "m")], retries=2, backoff=0)  # type: ignore[arg-type]
    with pytest.raises(StepError, match="all model candidates failed"):
        await exhausted.complete(_request())


def test_retry_after_header_parsing():
    assert retry_after_seconds({"retry-after": "7"}) == 7.0
    assert retry_after_seconds({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}) is None
    assert retry_after_seconds({}) is None


# --------------------------------------------------------------------------- run event retention


async def test_old_events_of_finished_runs_are_pruned(api, engine):
    url = "/v1/namespaces/default/workflows/hello/runs"
    done = (await api.post(url, json={"inputs": {"name": "a"}})).json()["id"]
    await engine.run_until_idle()
    tenant_id = (await engine.store.get_run(None, done)).tenant_id
    waiting = await engine.store.get_run(None, (await api.post(url, json={"inputs": {"name": "b"}})).json()["id"])
    assert await engine.store.list_events(tenant_id, done)
    assert await engine.store.list_events(tenant_id, waiting.id)

    assert await engine.store.prune_run_events(clock.now() - timedelta(days=1)) == 0  # nothing old enough
    pruned = await engine.store.prune_run_events(clock.now() + timedelta(seconds=1))
    assert pruned > 0
    assert await engine.store.list_events(tenant_id, done) == []
    assert await engine.store.list_events(tenant_id, waiting.id)  # its run has not finished


# --------------------------------------------------------------------------- connection pool settings


def test_store_pool_settings_and_pgbouncer_mode():
    from shoal.config import Settings
    from shoal.store.store import store_from_settings

    settings = Settings(
        database_url="postgresql+asyncpg://u:p@pooler:6432/shoal",
        database_direct_url="postgresql+asyncpg://u:p@db:5432/shoal",
        db_pool_size=33, db_max_overflow=4, db_pgbouncer=True,
    )
    store = store_from_settings(settings)
    pool = store.engine.pool
    assert pool.size() == 33 and pool._max_overflow == 4  # noqa: SLF001
    assert store.direct_url.endswith("@db:5432/shoal")


# --------------------------------------------------------------------------- backpressure


async def test_requests_over_the_tenant_rate_get_429_with_retry_after(api, engine):
    engine.settings.rate_limit_requests_per_second = 1
    engine.settings.rate_limit_burst = 3
    engine.quotas._buckets.clear()  # noqa: SLF001
    engine.quotas._quotas.clear()  # noqa: SLF001
    codes = [(await api.get("/v1/me")).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[-1] == 429
    r = await api.get("/v1/me")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert r.json()["code"] == "quota_exceeded"


async def test_a_busy_process_sheds_requests_with_503(engine):
    engine.settings.api_max_concurrent_requests = 1
    engine.settings.api_queue_timeout_seconds = 0.05
    engine.settings.rate_limit_requests_per_second = 0
    engine.settings.admin_token = SecretStr(ADMIN)
    app = create_app(engine=engine)
    release, entered = asyncio.Event(), asyncio.Event()
    original = engine.quotas.check_request

    async def slow_first(tenant_id):
        if not entered.is_set():
            entered.set()
            await release.wait()
        await original(tenant_id)

    engine.quotas.check_request = slow_first
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shoal.test") as client:
        r = await client.post("/v1/tenants", json={"slug": "busy"}, headers={"Authorization": f"Bearer {ADMIN}"})
        client.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        held = asyncio.create_task(client.get("/v1/me"))
        await entered.wait()
        shed = await client.get("/v1/me")
        assert shed.status_code == 503 and shed.headers["retry-after"] == "1"
        assert shed.json()["code"] == "overloaded"
        assert (await client.get("/healthz")).status_code != 503  # outside /v1: never shed
        release.set()
        assert (await held).status_code == 200
        assert (await client.get("/v1/me")).status_code == 200


async def test_tenant_quotas_api_and_pending_run_admission(api, engine):
    admin = {"Authorization": f"Bearer {ADMIN}"}
    r = await api.put("/v1/tenants/acme/quotas", json={"max_pending_runs": 2}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["tenant"] == {"max_pending_runs": 2, "requests_per_second": None, "request_burst": None,
                                  "max_concurrent_runs": None}
    assert r.json()["effective"]["requests_per_second"] == engine.settings.rate_limit_requests_per_second
    assert (await api.get("/v1/quotas")).json()["effective"]["max_pending_runs"] == 2

    url = "/v1/namespaces/default/workflows/hello/runs"
    codes = [(await api.post(url, json={"inputs": {"name": "x"}})).status_code for _ in range(3)]
    assert codes == [201, 201, 429]
    await engine.run_until_idle()  # the queue drains, so new runs are admitted again
    engine.quotas._pending.clear()  # noqa: SLF001 - skip the 1s count cache
    assert (await api.post(url, json={"inputs": {"name": "x"}})).status_code == 201


async def test_claims_take_turns_across_tenants_and_respect_concurrency(engine):
    from shoal.core.models import parse_resource

    big = await engine.store.create_tenant("big")
    small = await engine.store.create_tenant("small")
    workflow = parse_resource({"apiVersion": "shoal.dev/v1", "kind": "Workflow", "metadata": {"name": "w"},
                               "spec": {"steps": [{"id": "a", "type": "set", "values": {"x": 1}}]}})
    for tenant in (big, small):
        await engine.store.apply_resource(tenant.id, workflow)
    big_runs = [await engine.executor.start_run(big.id, "w") for _ in range(10)]
    small_run = await engine.executor.start_run(small.id, "w")

    claimed = await engine.store.claim_runs("w1", limit=2, lease=timedelta(minutes=1),
                                            tenant_limits=engine.quotas.concurrency_limits)
    assert small_run.id in claimed  # not stuck behind the big tenant's backlog

    await engine.store.update_tenant_settings(big.id, quotas={"max_concurrent_runs": 3})
    engine.quotas.forget(big.id)
    more = await engine.store.claim_runs("w2", limit=10, lease=timedelta(minutes=1),
                                         tenant_limits=engine.quotas.concurrency_limits)
    leased_big = [r for r in claimed + more if r in {b.id for b in big_runs}]
    assert len(leased_big) == 3


async def test_gateway_concurrency_limit_is_shared_and_waits(engine, tenant):
    import asyncio

    from shoal.core.models import GatewayLimits

    inflight = peak = 0

    class Slow:
        async def complete(self, request):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.05)
            inflight -= 1
            return ModelResponse(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn",
                                 usage={"input_tokens": 1, "output_tokens": 1}, model="m")

    limits = GatewayLimits(max_concurrency=2, max_wait=timedelta(seconds=10))

    def client():
        target = ModelTarget("tier1", "p", Slow(), "m", limits=limits, limit_scope=tenant.id)  # type: ignore[arg-type]
        return ModelClient([target], limiter=engine.gateway_limiter)

    results = await asyncio.gather(*(client().complete(_request()) for _ in range(6)))
    assert len(results) == 6 and peak == 2
    from sqlalchemy import func, select

    from shoal.store import tables as t

    async with engine.store.session() as s:  # concurrency slots are handed back
        n = (await s.execute(select(func.count()).select_from(t.LimiterLease).where(t.LimiterLease.key.endswith(":conc")))).scalar_one()
    assert n == 0


async def test_gateway_requests_per_minute_falls_back_then_gives_up(engine, tenant, monkeypatch):
    from shoal.core.models import GatewayLimits

    async def no_wait(attempt: int) -> None:
        return None

    monkeypatch.setattr("shoal.limits.GatewayLimiter.pause", staticmethod(no_wait))
    limited = ModelTarget("tier1", "p", _Flaky(0), "m", limits=GatewayLimits(requests_per_minute=1, max_wait=timedelta(0)),  # type: ignore[arg-type]
                          limit_scope=tenant.id)
    backup = ModelTarget("backup", "p", _Flaky(0), "m2")  # type: ignore[arg-type]
    first = ModelClient([limited], limiter=engine.gateway_limiter)
    await first.complete(_request())
    with pytest.raises(StepError):  # the minute's one request is used up and there is nothing else
        await ModelClient([limited], limiter=engine.gateway_limiter).complete(_request())
    routed = ModelClient([limited, backup], limiter=engine.gateway_limiter)
    assert (await routed.complete(_request())).model == "m"  # the backup answered
    assert routed.index == 1


async def test_cached_principal_is_dropped_when_its_key_is_revoked(api, engine):
    r = await api.post("/v1/api-keys", json={"name": "temp"})
    assert r.status_code == 201, r.text
    key = r.json()
    headers = {"Authorization": f"Bearer {key['token']}"}
    assert (await api.get("/v1/me", headers=headers)).status_code == 200
    assert (await api.get("/v1/me", headers=headers)).status_code == 200  # served from the cache
    assert (await api.delete(f"/v1/api-keys/{key['key']['id']}")).status_code in (200, 204)
    assert (await api.get("/v1/me", headers=headers)).status_code == 401


# --------------------------------------------------------------------------- live updates


async def test_streams_wake_on_postgres_notifications_from_other_processes(engine, tenant):
    import asyncio

    from shoal.store.store import Store

    if engine.store.is_sqlite:
        pytest.skip("LISTEN/NOTIFY needs Postgres (set SHOAL_TEST_DATABASE_URL)")
    from shoal.core.models import parse_resource

    await engine.store.apply_resource(tenant.id, parse_resource(
        {"apiVersion": "shoal.dev/v1", "kind": "Workflow", "metadata": {"name": "w"},
         "spec": {"steps": [{"id": "a", "type": "set", "values": {"x": 1}}]}}))
    run = await engine.executor.start_run(tenant.id, "w")
    engine.events.start_listening()
    for _ in range(100):
        if engine.events.listening:
            break
        await asyncio.sleep(0.05)
    assert engine.events.listening and engine.events.poll_seconds == 15.0

    other = Store(engine.store.url)  # another process's store: its events never touch this bus directly
    try:
        waiting = asyncio.create_task(engine.events.wait(run.id, timeout=5))
        await asyncio.sleep(0.1)
        await other.add_event(tenant.id, run.id, "step.started", {})
        assert await asyncio.wait_for(waiting, 3) is True
    finally:
        await other.close()
        await engine.events.stop_listening()


# --------------------------------------------------------------------------- scale-out


async def test_runs_that_call_models_go_to_the_agent_lane(engine, tenant):
    from shoal.core.models import parse_resource

    def workflow(name, steps):
        return parse_resource({"apiVersion": "shoal.dev/v1", "kind": "Workflow", "metadata": {"name": name}, "spec": {"steps": steps}})

    await engine.store.apply_resource(tenant.id, workflow("plain", [{"id": "a", "type": "set", "values": {"x": 1}}]))
    await engine.store.apply_resource(tenant.id, workflow("nested", [
        {"id": "a", "type": "set", "values": {"x": 1}},
        {"id": "think", "type": "agent", "agent": "x", "prompt": "hi"},
    ]))
    plain = await engine.executor.start_run(tenant.id, "plain")
    nested = await engine.executor.start_run(tenant.id, "nested")
    assert (plain.lane, nested.lane) == ("standard", "agent")

    lease = timedelta(minutes=1)
    assert await engine.store.claim_runs("w", limit=10, lease=lease, lanes=["standard"]) == [plain.id]
    assert await engine.store.claim_runs("w", limit=10, lease=lease, lanes=["agent"]) == [nested.id]


async def test_a_pass_over_a_simple_run_uses_few_transactions(engine, tenant):
    from sqlalchemy import event

    from shoal.core.models import parse_resource

    await engine.store.apply_resource(tenant.id, parse_resource(
        {"apiVersion": "shoal.dev/v1", "kind": "Workflow", "metadata": {"name": "two"},
         "spec": {"steps": [{"id": "a", "type": "set", "values": {"x": 1}},
                            {"id": "b", "type": "set", "values": {"y": "{{ steps.a.output.x }}"}}]}}))
    run = await engine.executor.start_run(tenant.id, "two")
    commits = 0

    def count(_conn):
        nonlocal commits
        commits += 1

    event.listen(engine.store.engine.sync_engine, "commit", count)
    try:
        await engine.process_due()
    finally:
        event.remove(engine.store.engine.sync_engine, "commit", count)
    assert (await engine.store.get_run(tenant.id, run.id)).status == "succeeded"
    # claim, read run + steps, run.started, 2 x (started + succeeded), refreshes, finish (with the lease release)
    assert commits <= 14, commits
    assert (await engine.store.get_run(tenant.id, run.id)).lease_owner is None


async def test_a_run_whose_workers_keep_dying_fails_instead_of_looping(engine, tenant):
    from shoal.core.models import parse_resource

    await engine.store.apply_resource(tenant.id, parse_resource({
        "apiVersion": "shoal.dev/v1", "kind": "Workflow", "metadata": {"name": "w"},
        "spec": {"steps": [{"id": "a", "type": "set", "values": {"x": 1}}]},
    }))
    lease = timedelta(minutes=1)
    engine.settings.max_lost_leases = 3

    async def die_mid_pass(worker: str, run_id: str) -> None:
        """A worker takes the run and is killed before it releases the lease, which then expires."""
        assert await engine.store.claim_runs(worker, limit=1, lease=lease) == [run_id]
        await engine.store.update_run(run_id, lease_expires_at=clock.now() - timedelta(seconds=1))

    rid = (await engine.executor.start_run(tenant.id, "w")).id
    for worker in ("w0", "w1", "w2"):
        await die_mid_pass(worker, rid)
    assert (await engine.store.get_run(tenant.id, rid)).lease_losses == 2  # the first claim lost nothing

    assert await engine.store.claim_runs("w3", limit=1, lease=lease) == [rid]
    assert (await engine.store.get_run(tenant.id, rid)).lease_losses == 3
    await engine.executor.advance(rid, "w3")
    run = await engine.store.get_run(tenant.id, rid)
    assert run.status == "failed" and "3 times in a row" in run.error["message"]
    assert run.lease_owner is None

    # A pass that finishes normally forgets earlier losses.
    other = await engine.executor.start_run(tenant.id, "w")
    assert await engine.store.claim_runs("x1", limit=1, lease=lease) == [other.id]
    await engine.store.update_run(other.id, lease_expires_at=clock.now() - timedelta(seconds=1))
    assert await engine.store.claim_runs("x2", limit=1, lease=lease) == [other.id]
    await engine.executor.advance(other.id, "x2")
    done = await engine.store.get_run(tenant.id, other.id)
    assert done.status == "succeeded" and done.lease_losses == 0
