"""Shared limits: tenant quotas, request rate limits and gateway pacing.

* **Request rate limits** are token buckets per tenant, kept in each API process
  (``SHOAL_RATE_LIMIT_*`` or a tenant's ``quotas``). Over the limit, the API
  answers 429 with ``Retry-After``; the effective rate for a tenant is the
  per-process rate times the number of API processes.
* **Run admission** refuses new runs (429) once a tenant has too many runs
  waiting for a worker (``max_pending_runs``); workers stop claiming a tenant's
  runs once it has ``max_concurrent_runs`` being advanced.
* **Gateway pacing** shares a Gateway's ``limits`` (concurrency, requests and
  tokens per minute) across every worker through ``limiter_leases`` rows. On
  Postgres each check holds a transaction-scoped advisory lock on the key.

Tenant quotas live in ``tenants.settings["quotas"]`` and are set by the system
admin with ``PUT /v1/tenants/{slug}/quotas``.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text

from shoal.core import clock
from shoal.core.errors import QuotaExceeded
from shoal.core.ids import new_id
from shoal.core.models import RunStatus
from shoal.store import tables as t

if TYPE_CHECKING:
    from shoal.engine import Engine
    from shoal.llm.router import ModelTarget


class TenantQuotas(BaseModel):
    """Per-tenant overrides of the server-wide defaults. Unset fields use the defaults; 0 means unlimited."""

    requests_per_second: float | None = Field(None, ge=0, description="API requests per second, per API process.")
    request_burst: int | None = Field(None, ge=0, description="Requests allowed in a burst above the rate.")
    max_pending_runs: int | None = Field(None, ge=0, description="Runs waiting for a worker before new runs get 429.")
    max_concurrent_runs: int | None = Field(None, ge=0, description="Runs workers advance at once for this tenant.")


class TokenBucket:
    def __init__(self, rate: float, burst: float):
        self.rate, self.burst = rate, max(burst, 1.0)
        self.tokens, self.updated = self.burst, time.monotonic()

    def take(self) -> float:
        """Take a token. Returns 0 when allowed, else the seconds until one is free."""
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate


class _Cached:
    def __init__(self, value: Any, ttl: float):
        self.value, self.expires = value, time.monotonic() + ttl

    @property
    def fresh(self) -> bool:
        return time.monotonic() < self.expires


class Quotas:
    """Tenant quotas and the checks that enforce them."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self._quotas: dict[str, _Cached] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._pending: dict[str, _Cached] = {}

    def defaults(self) -> TenantQuotas:
        s = self.engine.settings
        return TenantQuotas(
            requests_per_second=s.rate_limit_requests_per_second,
            request_burst=s.rate_limit_burst,
            max_pending_runs=s.max_pending_runs_per_tenant,
            max_concurrent_runs=s.max_concurrent_runs_per_tenant,
        )

    async def for_tenant(self, tenant_id: str) -> TenantQuotas:
        cached = self._quotas.get(tenant_id)
        if cached is not None and cached.fresh:
            return cached.value
        tenant = await self.engine.store.get_tenant(tenant_id)
        own = TenantQuotas.model_validate((tenant.settings or {}).get("quotas") or {})
        merged = self.defaults().model_copy(update=own.model_dump(exclude_none=True))
        self._quotas[tenant_id] = _Cached(merged, self.engine.settings.quota_cache_seconds)
        return merged

    def forget(self, tenant_id: str) -> None:
        self._quotas.pop(tenant_id, None)
        self._buckets.pop(tenant_id, None)

    async def check_request(self, tenant_id: str | None) -> None:
        """Rate-limit an API request (429 with Retry-After when over)."""
        if not tenant_id:
            return
        quotas = await self.for_tenant(tenant_id)
        rate = quotas.requests_per_second or 0
        if not rate:
            return
        bucket = self._buckets.get(tenant_id)
        burst = float(quotas.request_burst or rate)
        if bucket is None or bucket.rate != rate or bucket.burst != max(burst, 1.0):
            bucket = self._buckets[tenant_id] = TokenBucket(rate, burst)
        wait = bucket.take()
        if wait:
            raise QuotaExceeded(
                f"rate limit of {rate:g} requests/s exceeded; retry later",
                details={"retry_after": max(1, round(wait + 0.5))},
            )

    async def check_webhook(self, webhook_id: str) -> None:
        """Rate-limit deliveries to one webhook URL (a flood from one sender cannot crowd out the rest)."""
        rate = self.engine.settings.webhook_rate_limit_per_second
        if not rate:
            return
        key = f"webhook:{webhook_id}"
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = TokenBucket(rate, max(rate * 2, self.engine.settings.webhook_rate_limit_burst))
        wait = bucket.take()
        if wait:
            raise QuotaExceeded("too many deliveries to this webhook; retry later", details={"retry_after": max(1, round(wait + 0.5))})

    async def admit_run(self, tenant_id: str) -> None:
        """Refuse a new run while the tenant already has too many waiting for a worker."""
        quotas = await self.for_tenant(tenant_id)
        limit = quotas.max_pending_runs or 0
        if not limit:
            return
        cached = self._pending.get(tenant_id)
        if cached is None or not cached.fresh:
            count = await self.engine.store.count_runs(tenant_id, statuses=[RunStatus.pending.value])
            cached = self._pending[tenant_id] = _Cached(count, 1.0)
        if cached.value >= limit:
            raise QuotaExceeded(
                f"tenant has {cached.value} runs waiting to start (limit {limit}); retry later",
                details={"retry_after": 5, "pending_runs": cached.value, "limit": limit},
            )
        cached.value += 1  # count this one until the next refresh

    async def concurrency_limits(self, tenant_ids: set[str]) -> dict[str, int]:
        """``max_concurrent_runs`` of each tenant that has one (claim loop)."""
        out = {}
        for tenant_id in tenant_ids:
            try:
                limit = (await self.for_tenant(tenant_id)).max_concurrent_runs or 0
            except Exception:  # a tenant deleted mid-claim
                limit = 0
            if limit:
                out[tenant_id] = limit
        return out


# --------------------------------------------------------------------------- gateway pacing


@dataclass
class Permit:
    keys: list[str]
    lease_id: str | None = None
    tokens_key: str | None = None


class GatewayLimiter:
    """Concurrency and per-minute limits shared through the database."""

    MINUTE = timedelta(minutes=1)

    def __init__(self, engine: Engine):
        self.engine = engine
        self.store = engine.store
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def key(target: ModelTarget) -> str | None:
        return f"gw:{target.limit_scope}:{target.gateway}" if target.limits else None

    async def try_acquire(self, target: ModelTarget) -> Permit | None:
        """Take capacity for one call, or None when the gateway is at a limit right now."""
        limits, base = target.limits, self.key(target)
        if limits is None or base is None:
            return Permit([])
        now = clock.now()
        call_ttl = timedelta(seconds=self.engine.settings.model_call_lease_seconds)
        async with self._lock(base), self.store.session() as s:
            if not self.store.is_sqlite:
                await s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": base})
            await s.execute(delete(t.LimiterLease).where(t.LimiterLease.key.startswith(base), t.LimiterLease.expires_at < now))

            async def used(key: str) -> int:
                q = select(func.coalesce(func.sum(t.LimiterLease.weight), 0)).where(t.LimiterLease.key == key)
                return int((await s.execute(q)).scalar_one())

            checks = [
                (f"{base}:conc", limits.max_concurrency),
                (f"{base}:rpm", limits.requests_per_minute),
                (f"{base}:tpm", limits.tokens_per_minute),
            ]
            for key, limit in checks:
                if limit and await used(key) >= limit:
                    return None
            permit = Permit([base], tokens_key=f"{base}:tpm" if limits.tokens_per_minute else None)
            if limits.max_concurrency:
                permit.lease_id = new_id("lim")
                s.add(t.LimiterLease(id=permit.lease_id, key=f"{base}:conc", weight=1, expires_at=now + call_ttl))
            if limits.requests_per_minute:
                s.add(t.LimiterLease(id=new_id("lim"), key=f"{base}:rpm", weight=1, expires_at=now + self.MINUTE))
            return permit

    async def release(self, permit: Permit, tokens: int = 0) -> None:
        if permit.lease_id is None and not (permit.tokens_key and tokens):
            return
        async with self.store.session() as s:
            if permit.lease_id is not None:
                await s.execute(delete(t.LimiterLease).where(t.LimiterLease.id == permit.lease_id))
            if permit.tokens_key and tokens:
                s.add(t.LimiterLease(id=new_id("lim"), key=permit.tokens_key, weight=tokens, expires_at=clock.now() + self.MINUTE))

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    @staticmethod
    async def pause(attempt: int) -> None:
        await asyncio.sleep(min(2.0, 0.05 * 2**attempt) * random.uniform(0.5, 1.5))
