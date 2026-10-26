"""Amazon SQS as the dispatch queue (``SHOAL_DISPATCH_QUEUE=sqs``).

Each lane can have its own standard queue (``SHOAL_DISPATCH_SQS_LANE_QUEUE_URLS``),
so an agent worker pool never receives standard runs' messages; lanes without
one use ``SHOAL_DISPATCH_SQS_QUEUE_URL``.

- Wake-ups for later go out with ``DelaySeconds`` (at most 15 minutes). A run
  waiting longer gets its message early, and the worker that receives it sends
  it again for the rest of the wait.
- Messages carry the tenant as their ``MessageGroupId``, which turns on SQS fair
  queues: one tenant's backlog does not hold back the others' messages. Workers
  still take turns across tenants and respect their concurrency caps when they
  claim, as they do with the database queue.
- FIFO queues are not supported: they allow no per-message delay, and ordering
  buys nothing when the database decides what is due.

The calls are SQS's JSON protocol over httpx, signed with SigV4 and the
deployment's own AWS credentials (environment, IRSA, EKS Pod Identity, ECS or
instance role; see ``shoal.cloud.aws``), so no AWS SDK is needed and
``SHOAL_DISPATCH_SQS_ENDPOINT`` can point at LocalStack or a VPC endpoint.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from shoal.cloud.aws import AmbientCredentials, sigv4_headers
from shoal.core import clock
from shoal.core.errors import StepError
from shoal.runtime.queues import Delivery, QueueError, Wakeup

if TYPE_CHECKING:
    from shoal.engine import Engine

log = logging.getLogger(__name__)

MAX_DELAY_SECONDS = 900
MAX_WAIT_SECONDS = 20
VISIBILITY_SECONDS = 30  # a received message is acked or retried within one claim, well inside this
_REGION = re.compile(r"(?:^|\.)sqs[.-]([a-z0-9-]+)\.amazonaws\.com")


def message_body(wakeup: Wakeup) -> str:
    return json.dumps({"v": 1, "run": wakeup.run_id, "tenant": wakeup.tenant_id, "lane": wakeup.lane,
                       "due": wakeup.due_at.isoformat()}, separators=(",", ":"))


def parse_body(body: str) -> Wakeup:
    data = json.loads(body)
    return Wakeup(run_id=data["run"], tenant_id=data["tenant"], lane=data["lane"], due_at=datetime.fromisoformat(data["due"]))


class SQSQueue:
    """``shoal.dispatch_queues`` plugin ``sqs``."""

    name = "sqs"
    external = True
    batch_size = 10

    def setup(self, engine: Engine) -> None:
        self.engine = engine
        settings = engine.settings
        self.default_url = settings.dispatch_sqs_queue_url
        self.lane_urls = dict(settings.dispatch_sqs_lane_queue_urls)
        self.endpoint = settings.dispatch_sqs_endpoint
        self.fair = settings.dispatch_sqs_fair_queues
        self.region = settings.dispatch_sqs_region
        self.credentials = AmbientCredentials(engine)
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        urls = {self.default_url, *self.lane_urls.values()} - {None}
        if not urls:
            raise QueueError("SHOAL_DISPATCH_QUEUE=sqs needs SHOAL_DISPATCH_SQS_QUEUE_URL (or a queue per lane)")
        fifo = sorted(u for u in urls if u and u.endswith(".fifo"))
        if fifo:
            raise QueueError(f"use standard SQS queues, not FIFO ones: {', '.join(fifo)}")
        self._client = httpx.AsyncClient(timeout=MAX_WAIT_SECONDS + 10, transport=self.engine.http_transport)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def url_for(self, lane: str) -> str:
        url = self.lane_urls.get(lane) or self.default_url
        if not url:
            raise QueueError(f"no SQS queue for lane '{lane}': set SHOAL_DISPATCH_SQS_QUEUE_URL")
        return url

    def groups(self, lanes: Sequence[str]) -> list[tuple[str, ...]]:
        by_url: dict[str, list[str]] = {}
        for lane in lanes:
            by_url.setdefault(self.url_for(lane), []).append(lane)
        return [tuple(group) for group in by_url.values()]

    # ------------------------------------------------------------------ producer

    async def publish(self, wakeups: Sequence[Wakeup]) -> None:
        by_url: dict[str, list[Wakeup]] = {}
        for wakeup in wakeups:
            by_url.setdefault(self.url_for(wakeup.lane), []).append(wakeup)
        now = clock.now()
        failed: list[str] = []
        for url, items in by_url.items():
            for chunk in _chunks(items, self.batch_size):
                entries = []
                for i, wakeup in enumerate(chunk):
                    delay = min(MAX_DELAY_SECONDS, max(0, math.ceil((wakeup.due_at - now).total_seconds())))
                    entry: dict[str, Any] = {"Id": str(i), "MessageBody": message_body(wakeup), "DelaySeconds": delay}
                    if self.fair:
                        entry["MessageGroupId"] = wakeup.tenant_id
                    entries.append(entry)
                result = await self._call("SendMessageBatch", {"QueueUrl": url, "Entries": entries})
                failed += [f"{f.get('Code')}: {f.get('Message', '')}" for f in result.get("Failed") or []]
        if failed:
            raise QueueError(f"SQS refused {len(failed)} wake-up(s): {failed[0]}")

    # ------------------------------------------------------------------ consumer

    async def receive(self, lanes: Sequence[str], max_messages: int, wait: float) -> list[Delivery]:
        url = self.url_for(lanes[0])  # groups() put every lane of a group on one queue
        result = await self._call("ReceiveMessage", {
            "QueueUrl": url,
            "MaxNumberOfMessages": max(1, min(self.batch_size, max_messages)),
            "WaitTimeSeconds": max(0, min(MAX_WAIT_SECONDS, int(wait))),
            "VisibilityTimeout": VISIBILITY_SECONDS,
            "MessageSystemAttributeNames": ["ApproximateReceiveCount"],
        }, timeout=wait + 10)
        deliveries: list[Delivery] = []
        poison: list[Delivery] = []
        for message in result.get("Messages") or []:
            handle = (url, message["ReceiptHandle"])
            attempts = int((message.get("Attributes") or {}).get("ApproximateReceiveCount") or 1)
            try:
                deliveries.append(Delivery(parse_body(message["Body"]), handle, attempts))
            except (ValueError, KeyError, TypeError):
                log.warning("dropping an SQS message that is not a Shoal wake-up: %.200s", message.get("Body"))
                poison.append(Delivery(Wakeup("", "", "", clock.now()), handle))
        if poison:
            await self.ack(poison)
        return deliveries

    async def ack(self, deliveries: Sequence[Delivery]) -> None:
        await self._batch("DeleteMessageBatch", deliveries, {})

    async def retry(self, deliveries: Sequence[Delivery], delay: float) -> None:
        await self._batch("ChangeMessageVisibilityBatch", deliveries, {"VisibilityTimeout": max(0, math.ceil(delay))})

    async def _batch(self, action: str, deliveries: Sequence[Delivery], extra: dict[str, Any]) -> None:
        by_url: dict[str, list[str]] = {}
        for delivery in deliveries:
            url, receipt = delivery.handle
            by_url.setdefault(url, []).append(receipt)
        for url, receipts in by_url.items():
            for chunk in _chunks(receipts, self.batch_size):
                entries = [{"Id": str(i), "ReceiptHandle": r, **extra} for i, r in enumerate(chunk)]
                result = await self._call(action, {"QueueUrl": url, "Entries": entries})
                for failure in result.get("Failed") or []:  # e.g. the receipt expired: the message comes back anyway
                    log.info("SQS %s: %s %s", action, failure.get("Code"), failure.get("Message", ""))

    # ------------------------------------------------------------------ wire

    def _region(self, url: str) -> str:
        if self.region:
            return self.region
        match = _REGION.search(urlsplit(url).hostname or "")
        if match:
            return match.group(1)
        return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

    async def _call(self, action: str, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        if self._client is None:
            raise QueueError("the SQS dispatch queue is not started")
        queue_url = payload["QueueUrl"]
        parts = urlsplit(self.endpoint or queue_url)
        endpoint = f"{parts.scheme}://{parts.netloc}/"
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/x-amz-json-1.0", "X-Amz-Target": f"AmazonSQS.{action}"}
        try:
            creds = await self.credentials.get()
        except StepError as exc:
            raise QueueError(str(exc)) from exc
        headers = sigv4_headers("POST", endpoint, body, creds, self._region(queue_url), "sqs", clock.now(), headers)
        try:
            response = await self._client.post(endpoint, content=body, headers=headers,
                                               timeout=timeout or MAX_WAIT_SECONDS + 10)
        except httpx.HTTPError as exc:
            raise QueueError(f"SQS {action} failed: {type(exc).__name__}: {exc}") from exc
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        if response.status_code >= 400:
            code = str(data.get("__type") or response.status_code).rsplit("#", 1)[-1]
            message = data.get("message") or data.get("Message") or response.text[:200]
            raise QueueError(f"SQS {action} failed: {code}: {message}")
        return data


def _chunks(items: Sequence[Any], size: int) -> list[Sequence[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]
