"""Hub side of federation: links, enrollment, the request queue and forwarding.

A forwarded call is queued as a ``FederationRequest`` row. The member's tenant
connector long-polls ``POST /v1/federation/tunnel``, which hands it queued
requests and carries back the answers to earlier ones. The queue lives in the
database, so any hub replica can accept the API call and any replica can serve
the connector's poll; within one replica, waiters are woken directly.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update

from shoal.auth.rbac import Principal
from shoal.core import clock
from shoal.core.errors import Conflict, InvalidRequest, NotFound, ShoalError, Unauthorized
from shoal.core.ids import new_id
from shoal.federation import protocol as p
from shoal.store import tables as t

ENROLLMENT_TTL = timedelta(days=7)
CONNECTED_WITHIN = timedelta(seconds=45)  # a connector polls at least this often while connected
FORWARD_TIMEOUT = 30.0
MAX_WAIT = 25.0
FORWARDED_HEADERS = ("content-type", "accept", "x-shoal-client", "if-match")


class FederationReadOnly(ShoalError):
    status_code = 403
    code = "federation_read_only"


class FederationUnavailable(ShoalError):
    status_code = 503
    code = "federation_disconnected"


class FederationTimeout(ShoalError):
    status_code = 504
    code = "federation_timeout"


class FederationHub:
    def __init__(self, engine: Any):
        self.engine = engine
        self._pollers: dict[str, set[asyncio.Event]] = {}  # link id -> connector polls waiting on this replica
        self._responses: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._federated: dict[str, str | None] = {}  # tenant id -> link id (None: hosted), cached

    @property
    def store(self) -> Any:
        return self.engine.store

    # ------------------------------------------------------------------ links

    async def link_for_tenant(self, tenant_id: str) -> t.FederationLink | None:
        async with self.store.session() as s:
            return (await s.execute(select(t.FederationLink).where(t.FederationLink.tenant_id == tenant_id))).scalar_one_or_none()

    async def federated_link_id(self, tenant_id: str) -> str | None:
        """The link to forward a tenant's calls through, or None for hosted tenants (cached)."""
        if tenant_id not in self._federated:
            link = await self.link_for_tenant(tenant_id)
            self._federated[tenant_id] = link.id if link and link.status != "revoked" else None
        return self._federated[tenant_id]

    async def links(self) -> list[t.FederationLink]:
        async with self.store.session() as s:
            return list((await s.execute(select(t.FederationLink).order_by(t.FederationLink.created_at))).scalars().all())

    async def create_link(self, tenant_id: str, *, mode: str, actor: str) -> tuple[t.FederationLink, str]:
        if mode not in p.MODES:
            raise InvalidRequest(f"mode must be one of {p.MODES}")
        if await self.link_for_tenant(tenant_id) is not None:
            raise Conflict("this tenant is already federated; issue a new enrollment token instead")
        async with self.store.session() as s:
            in_use = (await s.execute(select(func.count()).select_from(t.Resource).where(t.Resource.tenant_id == tenant_id))).scalar()
        if in_use:
            raise Conflict("only an empty tenant can be federated: its data lives in the member deployment")
        token = p.ENROLLMENT_PREFIX + secrets.token_urlsafe(32)
        link = t.FederationLink(
            id=new_id("fed"), tenant_id=tenant_id, requested_mode=mode, created_by=actor,
            enrollment_hash=p.hash_token(token), enrollment_expires_at=clock.now() + ENROLLMENT_TTL,
        )
        async with self.store.session() as s:
            s.add(link)
        self._federated.pop(tenant_id, None)
        return link, token

    async def reissue_enrollment(self, tenant_id: str) -> tuple[t.FederationLink, str]:
        token = p.ENROLLMENT_PREFIX + secrets.token_urlsafe(32)
        link = await self._update(tenant_id, enrollment_hash=p.hash_token(token), enrollment_expires_at=clock.now() + ENROLLMENT_TTL)
        return link, token

    async def set_mode(self, tenant_id: str, mode: str) -> t.FederationLink:
        if mode not in p.MODES:
            raise InvalidRequest(f"mode must be one of {p.MODES}")
        return await self._update(tenant_id, requested_mode=mode)

    async def revoke(self, tenant_id: str) -> t.FederationLink:
        link = await self._update(tenant_id, status="revoked", secret_ciphertext=None, enrollment_hash=None)
        self._federated.pop(tenant_id, None)
        return link

    async def _update(self, tenant_id: str, **values: Any) -> t.FederationLink:
        async with self.store.session() as s:
            link = (await s.execute(select(t.FederationLink).where(t.FederationLink.tenant_id == tenant_id))).scalar_one_or_none()
            if link is None:
                raise NotFound("this tenant is not federated")
            for key, value in values.items():
                setattr(link, key, value)
        return link

    @staticmethod
    def connected(link: t.FederationLink) -> bool:
        return link.status == "active" and link.last_seen_at is not None and clock.now() - link.last_seen_at < CONNECTED_WITHIN

    # ------------------------------------------------------------------ enrollment and tunnel

    async def enroll(self, token: str, *, max_mode: str, namespaces: list[str], visibility: str, member: dict[str, Any]) -> dict[str, Any]:
        if max_mode not in p.MODES or visibility not in p.VISIBILITIES:
            raise InvalidRequest("invalid max_mode or visibility")
        secret = secrets.token_urlsafe(32)
        async with self.store.session() as s:
            link = (
                await s.execute(select(t.FederationLink).where(t.FederationLink.enrollment_hash == p.hash_token(token)))
            ).scalar_one_or_none()
            if link is None or link.status == "revoked" or (link.enrollment_expires_at and link.enrollment_expires_at < clock.now()):
                raise Unauthorized("invalid or expired enrollment token")
            link.enrollment_hash = None
            link.status = "active"
            link.member_mode = max_mode
            link.namespaces = namespaces
            link.visibility = visibility
            link.member = member
            link.secret_ciphertext = self.engine.secrets.cipher.encrypt(secret)
            link.last_seen_at = clock.now()
            tenant = await s.get(t.Tenant, link.tenant_id)
        self._federated.pop(link.tenant_id, None)
        await self.store.audit(link.tenant_id, "federation:member", "federation.enrolled", link.id, max_mode=max_mode)
        return {
            "link_id": link.id, "secret": secret, "tenant": tenant.slug if tenant else None,
            "requested_mode": link.requested_mode, "effective_mode": p.effective_mode(link.requested_mode, max_mode),
            "protocol": p.PROTOCOL_VERSION,
        }

    async def authenticate_link(self, credential: str) -> t.FederationLink:
        link_id, secret = p.parse_credential(credential)
        async with self.store.session() as s:
            link = await s.get(t.FederationLink, link_id)
        if link is None or link.status != "active" or not link.secret_ciphertext:
            raise Unauthorized("unknown or revoked federation link")
        if not secrets.compare_digest(self.engine.secrets.cipher.decrypt(link.secret_ciphertext), secret):
            raise Unauthorized("invalid federation link credential")
        return link

    async def tunnel(self, link: t.FederationLink, *, responses: list[dict[str, Any]], wait: float, limit: int,
                     report: dict[str, Any]) -> dict[str, Any]:
        """One connector poll: deliver answers, update the member's report, then hand out queued requests."""
        now = clock.now()
        async with self.store.session() as s:
            row = await s.get(t.FederationLink, link.id)
            row.last_seen_at = now
            for key in ("max_mode", "namespaces", "visibility"):
                if key in report:
                    setattr(row, "member_mode" if key == "max_mode" else key, report[key])
            if "member" in report:
                row.member = report["member"]
            for answer in responses:
                req = await s.get(t.FederationRequest, answer.get("id"))
                if req is None or req.link_id != link.id or req.status == "done":
                    continue
                req.status = "done"
                req.response = {k: answer.get(k) for k in ("status", "headers", "body")}
            link = row
        for answer in responses:
            future = self._responses.get(answer.get("id", ""))
            if future is not None and not future.done():
                future.set_result({k: answer.get(k) for k in ("status", "headers", "body")})
        event = asyncio.Event()  # registered before the first claim, so a request queued in between still wakes us
        pollers = self._pollers.setdefault(link.id, set())
        pollers.add(event)
        try:
            requests = await self._claim(link.id, limit)
            deadline = asyncio.get_running_loop().time() + min(wait, MAX_WAIT)
            while not requests and (remaining := deadline - asyncio.get_running_loop().time()) > 0:
                try:  # woken directly by forwards on this replica; re-check the queue for other replicas' forwards
                    await asyncio.wait_for(event.wait(), timeout=min(remaining, 1.0))
                except TimeoutError:
                    pass
                event.clear()
                requests = await self._claim(link.id, limit)
        finally:
            pollers.discard(event)
        return {
            "requests": requests,
            "requested_mode": link.requested_mode,
            "effective_mode": p.effective_mode(link.requested_mode, link.member_mode),
        }

    async def _claim(self, link_id: str, limit: int) -> list[dict[str, Any]]:
        stale = clock.now() - timedelta(seconds=FORWARD_TIMEOUT)
        async with self.store.session() as s:
            q = (
                select(t.FederationRequest)
                .where(t.FederationRequest.link_id == link_id, t.FederationRequest.status == "pending",
                       t.FederationRequest.created_at > stale)
                .order_by(t.FederationRequest.created_at)
                .limit(limit)
            )
            rows = list((await s.execute(q)).scalars().all())
            claimed = []
            for row in rows:  # compare-and-set so two polling replicas never hand out the same request
                result = await s.execute(
                    update(t.FederationRequest)
                    .where(t.FederationRequest.id == row.id, t.FederationRequest.status == "pending")
                    .values(status="claimed", claimed_at=clock.now())
                )
                if result.rowcount:
                    claimed.append({"id": row.id, **row.request})
        return claimed

    # ------------------------------------------------------------------ forwarding

    async def forward(self, link_id: str, principal: Principal, *, method: str, path: str, query: str,
                      headers: dict[str, str], body: bytes, request_id: str) -> dict[str, Any]:
        async with self.store.session() as s:
            link = await s.get(t.FederationLink, link_id)
        if link is None or link.status != "active" or not link.secret_ciphertext:
            raise FederationUnavailable("this federated tenant has not enrolled a member deployment yet")
        if not self.connected(link):
            raise FederationUnavailable("the member deployment for this tenant is not connected")
        mode = p.effective_mode(link.requested_mode, link.member_mode)
        if mode == "read" and not p.is_read(method, path):
            raise FederationReadOnly("this federated tenant is linked read-only")
        secret = self.engine.secrets.cipher.decrypt(link.secret_ciphertext)
        assertion = p.sign_assertion(secret, {
            "link": link.id, "sub": principal.email or principal.actor, "actor": principal.actor,
            "name": principal.display_name, "groups": await self._group_names(principal),
            "rid": request_id, "method": method.upper(), "path": path,
        })
        req = t.FederationRequest(
            id=new_id("fwd"), link_id=link.id,
            request={
                "method": method.upper(), "path": path, "query": query, "assertion": assertion,
                "headers": {k: v for k, v in headers.items() if k.lower() in FORWARDED_HEADERS} | {"x-request-id": request_id},
                "body": base64.b64encode(body).decode() if body else "",
            },
        )
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._responses[req.id] = future
        try:
            async with self.store.session() as s:
                s.add(req)
            for poller in self._pollers.get(link.id, ()):
                poller.set()
            return await self._await_response(req.id, future)
        finally:
            self._responses.pop(req.id, None)

    async def _await_response(self, request_id: str, future: asyncio.Future[dict[str, Any]]) -> dict[str, Any]:
        """Woken directly when this replica served the poll; otherwise the answer shows up in the database."""
        deadline = asyncio.get_running_loop().time() + FORWARD_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise FederationTimeout("the member deployment did not answer in time")
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=min(0.5, remaining))
            except TimeoutError:
                async with self.store.session() as s:
                    row = await s.get(t.FederationRequest, request_id)
                if row is not None and row.status == "done" and row.response is not None:
                    return row.response

    async def _group_names(self, principal: Principal) -> list[str]:
        if not principal.user_id or not principal.tenant_id:
            return []
        directory = self.engine.directory
        names = []
        for group_id in await directory.groups_of(principal.user_id):
            try:
                names.append((await directory.get_group(principal.tenant_id, group_id)).display_name)
            except NotFound:
                continue
        return sorted(names)

    async def prune(self, older_than: datetime) -> None:
        async with self.store.session() as s:
            await s.execute(delete(t.FederationRequest).where(t.FederationRequest.created_at < older_than))
