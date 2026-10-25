"""Member side of federation: joining a hub and the tenant connector.

The connector keeps one outbound long-poll open to the hub, so nothing in the
organisation's network has to accept inbound traffic. Each request it is
handed is checked against this member's ceiling (mode and exposed namespaces),
then run against the local API with the hub's assertion, where the member's
own RBAC decides. Answers go back on the next poll.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any

import httpx
from sqlalchemy import select

from shoal import __version__
from shoal.auth.rbac import Principal
from shoal.core import clock
from shoal.core.errors import Conflict, InvalidRequest, NotFound, Unauthorized
from shoal.core.ids import new_id
from shoal.federation import protocol as p
from shoal.store import tables as t

log = logging.getLogger(__name__)

NAMESPACE_PATH = re.compile(r"^/v1/namespaces/([^/]+)")
# Paths whose bodies carry run data; ``metadata`` visibility strips the payload fields.
RUN_DATA_PATH = re.compile(r"^/v1/(namespaces/[^/]+/)?(runs|eval-runs|campaign-runs)\b")
REDACTED_FIELDS = {"inputs", "outputs", "output", "context", "data", "logs", "transcript", "messages", "prompt"}
LINK_HEADER = "x-shoal-federation-link"


def _problem(status: int, code: str, detail: str) -> dict[str, Any]:
    body = {"type": f"https://shoal.dev/problems/{code}", "title": code.replace("_", " "), "status": status, "code": code, "detail": detail}
    return {"status": status, "headers": {"content-type": "application/problem+json"}, "body": base64.b64encode(json.dumps(body).encode()).decode()}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[hidden by federation visibility]" if k in REDACTED_FIELDS else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class FederationMember:
    def __init__(self, engine: Any):
        self.engine = engine
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self.app: Any = None  # the local API that forwarded calls run against
        self._running = False  # connectors run in the API process (tests drive poll_once themselves)
        self.connector_errors: dict[str, str] = {}  # membership id -> why the last poll failed (this process)

    @property
    def store(self) -> Any:
        return self.engine.store

    def _http(self, **kwargs: Any) -> httpx.AsyncClient:
        if self.engine.http_transport is not None:
            kwargs.setdefault("transport", self.engine.http_transport)
        return httpx.AsyncClient(**kwargs)

    # ------------------------------------------------------------------ membership

    async def membership(self, tenant_id: str) -> t.FederationMembership | None:
        async with self.store.session() as s:
            q = select(t.FederationMembership).where(t.FederationMembership.tenant_id == tenant_id)
            return (await s.execute(q)).scalar_one_or_none()

    async def memberships(self) -> list[t.FederationMembership]:
        async with self.store.session() as s:
            return list((await s.execute(select(t.FederationMembership))).scalars().all())

    async def join(self, tenant_id: str, hub_url: str, token: str, *, max_mode: str, namespaces: list[str], visibility: str,
                   grants: list[dict[str, Any]], actor: str) -> t.FederationMembership:
        if await self.membership(tenant_id) is not None:
            raise Conflict("this tenant already belongs to a hub; leave it first")
        if await self.engine.federation.link_for_tenant(tenant_id) is not None:
            raise Conflict("this tenant is federated from this deployment, so its data lives in a member; manage hub access there")
        if max_mode not in p.MODES or visibility not in p.VISIBILITIES:
            raise InvalidRequest(f"max_mode must be one of {p.MODES} and visibility one of {p.VISIBILITIES}")
        for ns in namespaces:
            await self.store.get_namespace(tenant_id, ns)
        for grant in grants:  # validate before enrolling, so a typo does not burn the token
            await self.engine.directory.get_role(tenant_id, grant["role"])
        hub_url = hub_url.rstrip("/")
        async with self._http(base_url=hub_url, timeout=30) as client:
            r = await client.post("/v1/federation/enroll", json={
                "token": token, "max_mode": max_mode, "namespaces": namespaces, "visibility": visibility,
                "member": {"version": __version__, "url": self.engine.settings.public_url},
            })
        if r.status_code >= 400:
            detail = r.json().get("detail") if r.headers.get("content-type", "").endswith("json") else r.text
            raise InvalidRequest(f"the hub refused enrollment ({r.status_code}): {detail}")
        enrolled = r.json()
        row = t.FederationMembership(
            id=new_id("mem"), tenant_id=tenant_id, hub_url=hub_url, link_id=enrolled["link_id"],
            secret_ciphertext=self.engine.secrets.cipher.encrypt(enrolled["secret"]), max_mode=max_mode,
            namespaces=namespaces, visibility=visibility, hub_mode=enrolled["effective_mode"], created_by=actor,
        )
        async with self.store.session() as s:
            s.add(row)
        for grant in grants:
            await self.engine.directory.bind(
                tenant_id, grant["role"], grant["subject_kind"], grant["subject_id"], namespace=grant.get("namespace"), actor=actor
            )
        if self._running:
            self.start_connector(row)
        return row

    async def update(self, tenant_id: str, **values: Any) -> t.FederationMembership:
        if values.get("max_mode") not in (None, *p.MODES) or values.get("visibility") not in (None, *p.VISIBILITIES):
            raise InvalidRequest("invalid max_mode or visibility")
        async with self.store.session() as s:
            row = (await s.execute(select(t.FederationMembership).where(t.FederationMembership.tenant_id == tenant_id))).scalar_one_or_none()
            if row is None:
                raise NotFound("this tenant is not a federation member")
            for key, value in values.items():
                if value is not None:
                    setattr(row, key, value)
        return row  # the hub hears about the new ceiling on the next poll

    async def leave(self, tenant_id: str) -> None:
        row = await self.membership(tenant_id)
        if row is None:
            raise NotFound("this tenant is not a federation member")
        task = self._tasks.pop(row.id, None)
        if task:
            task.cancel()
        async with self.store.session() as s:
            await s.delete(await s.get(t.FederationMembership, row.id))

    # ------------------------------------------------------------------ inbound authentication

    async def principal_for(self, link_id: str, assertion: str, *, method: str, path: str) -> tuple[Principal, dict[str, Any]]:
        """Turn a verified hub assertion into a principal that the member's own RBAC evaluates."""
        async with self.store.session() as s:
            q = select(t.FederationMembership).where(t.FederationMembership.link_id == link_id)
            row = (await s.execute(q)).scalar_one_or_none()
        if row is None:
            raise Unauthorized("unknown federation link")
        claims = p.verify_assertion(self.engine.secrets.cipher.decrypt(row.secret_ciphertext), assertion, method=method, path=path)
        subject = str(claims.get("sub"))
        mode = p.effective_mode(row.hub_mode or "read", row.max_mode)
        principal = Principal(
            tenant_id=row.tenant_id,
            actor=f"hub:user:{subject}",
            kind="hub",
            display_name=claims.get("name") or subject,
            email=subject if "@" in subject else None,
            key_scopes=("*:read",) if mode == "read" else (),
            key_namespaces=tuple(row.namespaces or ()),
            hub_subjects=(("hub_user", subject), *(("hub_group", g) for g in claims.get("groups") or ())),
        )
        return principal, claims

    # ------------------------------------------------------------------ connector

    def start(self, app: Any) -> None:
        """Called at API start-up: run a connector for every membership."""
        self.app = app
        self._running = True

        async def boot() -> None:
            for row in await self.memberships():
                self.start_connector(row)

        task = asyncio.create_task(boot())
        self.engine._tasks.add(task)  # noqa: SLF001 - engine owns background task lifetimes
        task.add_done_callback(self.engine._tasks.discard)  # noqa: SLF001

    def start_connector(self, row: t.FederationMembership) -> None:
        if row.id in self._tasks and not self._tasks[row.id].done():
            return
        task = asyncio.create_task(self._run(row.id))
        self._tasks[row.id] = task
        self.engine._tasks.add(task)  # noqa: SLF001
        task.add_done_callback(self.engine._tasks.discard)  # noqa: SLF001

    async def _run(self, membership_id: str) -> None:
        """Poll for requests and serve each one concurrently, answering as soon as it is done.

        Answers go back on their own short tunnel call, so a slow request (a long-polled event
        list, say) never holds up the others or the next poll.
        """
        backoff = 1.0
        unsent: list[dict[str, Any]] = []
        inflight: set[asyncio.Task[None]] = set()

        async def serve(row: t.FederationMembership, req: dict[str, Any]) -> None:
            answer = await self.handle(row, req)
            try:
                await self._tunnel(row, [answer], wait=0, limit=0)
            except Exception:  # sent with the next poll instead
                unsent.append(answer)

        try:
            while True:
                try:
                    responses, unsent[:] = list(unsent), []
                    row, body = await self._poll(membership_id, responses, wait=20)
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except NotFound:
                    self.connector_errors.pop(membership_id, None)
                    return  # left the hub
                except Exception as exc:
                    unsent[:0] = responses
                    if isinstance(exc, Unauthorized):  # revoked or replaced on the hub: no point retrying fast
                        backoff = 60
                    self.connector_errors[membership_id] = str(exc) or type(exc).__name__
                    log.warning("federation connector: %s; retrying in %.0fs", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                self.connector_errors.pop(membership_id, None)
                for req in body.get("requests", []):
                    task = asyncio.create_task(serve(row, req))
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
        finally:
            for task in inflight:
                task.cancel()

    async def poll_once(self, membership_id: str, responses: list[dict[str, Any]], *, wait: float = 0) -> list[dict[str, Any]]:
        """Send answers, receive requests, handle them. Returns the answers for the next poll."""
        row, body = await self._poll(membership_id, responses, wait=wait)
        return list(await asyncio.gather(*(self.handle(row, req) for req in body.get("requests", []))))

    async def _poll(self, membership_id: str, responses: list[dict[str, Any]], *, wait: float,
                    limit: int = 20) -> tuple[t.FederationMembership, dict[str, Any]]:
        async with self.store.session() as s:
            row = await s.get(t.FederationMembership, membership_id)
        if row is None:
            raise NotFound("membership removed")
        body = await self._tunnel(row, responses, wait=wait, limit=limit)
        async with self.store.session() as s:
            current = await s.get(t.FederationMembership, membership_id)
            if current is not None:
                current.hub_mode = body.get("requested_mode")
                current.last_connected_at = clock.now()
        row.hub_mode = body.get("requested_mode")
        return row, body

    async def _tunnel(self, row: t.FederationMembership, responses: list[dict[str, Any]], *, wait: float,
                      limit: int) -> dict[str, Any]:
        secret = self.engine.secrets.cipher.decrypt(row.secret_ciphertext)
        async with self._http(base_url=row.hub_url, timeout=wait + 15) as client:
            r = await client.post(
                "/v1/federation/tunnel",
                headers={"Authorization": f"Bearer {p.credential(row.link_id, secret)}"},
                json={"responses": responses, "wait": wait, "limit": limit, "report": await self._report(row)},
            )
        if r.status_code in (401, 403):
            raise Unauthorized("the hub rejected this member's link credential: the link was revoked, or replaced by a new enrollment")
        r.raise_for_status()
        return r.json()

    async def _report(self, row: t.FederationMembership) -> dict[str, Any]:
        return {
            "max_mode": row.max_mode, "namespaces": row.namespaces, "visibility": row.visibility,
            "member": {"version": __version__, "url": self.engine.settings.public_url, "protocol": p.PROTOCOL_VERSION},
        }

    async def handle(self, row: t.FederationMembership, req: dict[str, Any]) -> dict[str, Any]:
        """Apply this member's ceiling, then run the call against the local API.

        The ceiling is read afresh for every call, so lowering it applies to calls already
        handed out on the current poll too. A failure still answers, so the hub isn't left waiting.
        """
        answer = {"id": req["id"]}
        try:
            async with self.store.session() as s:
                current = await s.get(t.FederationMembership, row.id)
            if current is None:
                return answer | _problem(503, "federation_disconnected", "this deployment has left the hub")
            return await self._handle(current, req)
        except Exception:
            log.exception("federation connector: failed to serve %s %s", req.get("method"), req.get("path"))
            return answer | _problem(502, "federation_member_error", "the member deployment failed to handle this call")

    async def _handle(self, row: t.FederationMembership, req: dict[str, Any]) -> dict[str, Any]:
        method, path = req["method"], req["path"]
        answer = {"id": req["id"]}
        mode = p.effective_mode(row.hub_mode or "read", row.max_mode)
        if mode == "read" and not p.is_read(method, path):
            return answer | _problem(403, "federation_read_only", "this member allows the hub read-only access")
        match = NAMESPACE_PATH.match(path)
        if row.namespaces and match and match.group(1) not in row.namespaces:
            return answer | _problem(404, "not_found", f"namespace '{match.group(1)}' is not exposed to the hub")
        if "text/event-stream" in req.get("headers", {}).get("accept", ""):  # the hub streams by long-polling instead
            return answer | _problem(406, "not_acceptable", "the hub relays live streams by polling; ask for JSON")
        if self.app is None:
            return answer | _problem(503, "unavailable", "the member API is not running")
        headers = dict(req.get("headers") or {})
        headers[p.ASSERTION_HEADER] = req["assertion"]
        headers[LINK_HEADER] = row.link_id
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://member.local", timeout=120) as client:
            r = await client.request(
                method, path + (f"?{req['query']}" if req.get("query") else ""), headers=headers,
                content=base64.b64decode(req["body"]) if req.get("body") else None,
            )
        content = r.content
        if row.visibility == "metadata" and RUN_DATA_PATH.match(path) and r.headers.get("content-type", "").startswith("application/json"):
            content = json.dumps(redact(r.json())).encode()
        if row.namespaces and path in ("/v1/namespaces", "/v1/namespaces/") and r.status_code == 200:
            content = json.dumps([n for n in r.json() if n.get("name") in row.namespaces]).encode()
        keep = ("content-type", "location", "x-request-id")
        return answer | {
            "status": r.status_code,
            "headers": {k: v for k, v in r.headers.items() if k.lower() in keep},
            "body": base64.b64encode(content).decode(),
        }
