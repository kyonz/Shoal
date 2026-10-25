"""An in-memory Amazon SQS (JSON protocol) for httpx.MockTransport: delays, visibility timeouts, long polling.

Time is Shoal's clock, so ``clock.advance`` makes delayed messages visible; long polls wait in real time.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from shoal.core import clock

ACCOUNT = "123456789012"


@dataclass
class Message:
    id: str
    body: str
    group: str | None
    visible_at: datetime
    receipt: str = ""
    receives: int = 0


@dataclass
class FakeSQS:
    region: str = "eu-west-1"
    queues: dict[str, list[Message]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any], httpx.Headers]] = field(default_factory=list)
    fail: bool = False
    _ids: Any = field(default_factory=itertools.count)

    def __post_init__(self) -> None:
        self._arrived = asyncio.Event()

    def create(self, name: str) -> str:
        url = f"https://sqs.{self.region}.amazonaws.com/{ACCOUNT}/{name}"
        self.queues.setdefault(url, [])
        return url

    def messages(self, url: str) -> list[dict[str, Any]]:
        return [json.loads(m.body) | {"_delay": (m.visible_at - clock.now()).total_seconds(), "_group": m.group}
                for m in self.queues[url]]

    def actions(self) -> list[str]:
        return [action for action, _, _ in self.calls]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        action = request.headers.get("x-amz-target", "").removeprefix("AmazonSQS.")
        payload = json.loads(request.content or b"{}")
        self.calls.append((action, payload, request.headers))
        if self.fail:
            return httpx.Response(500, json={"__type": "com.amazonaws.sqs#InternalError", "message": "down"})
        queue = self.queues.get(payload.get("QueueUrl", ""))
        if queue is None:
            return httpx.Response(400, json={"__type": "com.amazonaws.sqs#QueueDoesNotExist",
                                             "message": "The specified queue does not exist."})
        handler = getattr(self, f"_{action}", None)
        if handler is None:
            return httpx.Response(400, json={"__type": "com.amazonaws.sqs#InvalidAction", "message": action})
        return httpx.Response(200, json=await handler(queue, payload))

    async def _SendMessageBatch(self, queue: list[Message], payload: dict[str, Any]) -> dict[str, Any]:
        entries = payload["Entries"]
        assert 1 <= len(entries) <= 10
        for entry in entries:
            assert 0 <= entry.get("DelaySeconds", 0) <= 900, entry
            queue.append(Message(str(next(self._ids)), entry["MessageBody"], entry.get("MessageGroupId"),
                                 clock.now() + timedelta(seconds=entry.get("DelaySeconds", 0))))
        self._arrived.set()
        return {"Successful": [{"Id": e["Id"], "MessageId": "m"} for e in entries], "Failed": []}

    async def _ReceiveMessage(self, queue: list[Message], payload: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + payload.get("WaitTimeSeconds", 0)
        while True:
            now = clock.now()
            visible = [m for m in queue if m.visible_at <= now][: payload.get("MaxNumberOfMessages", 1)]
            if visible or loop.time() >= deadline:
                break
            self._arrived.clear()
            try:
                await asyncio.wait_for(self._arrived.wait(), timeout=min(0.05, max(0.0, deadline - loop.time())))
            except TimeoutError:
                pass
        out = []
        for m in visible:
            m.receives += 1
            m.receipt = f"{m.id}-{m.receives}"
            m.visible_at = now + timedelta(seconds=payload.get("VisibilityTimeout", 30))
            out.append({"MessageId": m.id, "ReceiptHandle": m.receipt, "Body": m.body,
                        "Attributes": {"ApproximateReceiveCount": str(m.receives)}})
        return {"Messages": out} if out else {}

    async def _DeleteMessageBatch(self, queue: list[Message], payload: dict[str, Any]) -> dict[str, Any]:
        ok, failed = [], []
        for entry in payload["Entries"]:
            found = [m for m in queue if m.receipt == entry["ReceiptHandle"]]
            if found:
                queue.remove(found[0])
                ok.append({"Id": entry["Id"]})
            else:
                failed.append({"Id": entry["Id"], "Code": "ReceiptHandleIsInvalid", "SenderFault": True})
        return {"Successful": ok, "Failed": failed}

    async def _ChangeMessageVisibilityBatch(self, queue: list[Message], payload: dict[str, Any]) -> dict[str, Any]:
        for entry in payload["Entries"]:
            for m in queue:
                if m.receipt == entry["ReceiptHandle"]:
                    m.visible_at = clock.now() + timedelta(seconds=entry["VisibilityTimeout"])
        self._arrived.set()
        return {"Successful": [{"Id": e["Id"]} for e in payload["Entries"]], "Failed": []}
