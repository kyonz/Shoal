"""Remote agents over the Agent2Agent (A2A) protocol (JSON-RPC 2.0 over HTTP).

Register a remote agent as an ``Agent`` with ``runtime: a2a`` and
``runtime_config``:

* ``url`` - the agent's A2A endpoint (or ``card_url`` to discover it from the
  Agent Card's ``url`` field);
* ``auth`` - ``{"type": "bearer", "secret": "NAME"}`` or
  ``{"type": "header", "header": "X-API-Key", "secret": "NAME"}``;
* ``poll_interval`` / ``timeout`` - seconds.

The prompt is sent with ``message/send``; non-terminal tasks are polled with
``tasks/get``. Text parts of the final artifacts/messages become ``summary``;
the first JSON data part is validated against the step's declared outputs.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx

from shoal.agents.runtime import AgentInvocation, AgentResult, AgentRuntime
from shoal.core.errors import MissingConfiguration, StepError
from shoal.core.models import ParamValidationError, validate_params

TERMINAL = {"completed", "failed", "canceled", "rejected"}
BLOCKED = {"input-required", "auth-required"}


class A2ARuntime(AgentRuntime):
    name = "a2a"

    async def run(self, inv: AgentInvocation) -> AgentResult:
        cfg = inv.agent.runtime_config
        headers = await self._auth_headers(inv, cfg.get("auth") or {})
        transport = inv.ctx.engine.http_transport
        async with httpx.AsyncClient(timeout=60, headers=headers, transport=transport) as client:
            url = cfg.get("url") or await self._discover(client, cfg)
            message = {
                "role": "user",
                "messageId": str(uuid.uuid4()),
                "parts": [{"kind": "text", "text": inv.prompt}],
                "metadata": {"shoal_run_id": inv.ctx.run.id, "shoal_step_id": inv.ctx.step.id},
            }
            if inv.outputs:
                message["parts"].append(
                    {"kind": "data", "data": {"expected_outputs": {k: v.json_schema() for k, v in inv.outputs.items()}}}
                )
            result = await self._rpc(client, url, "message/send", {"message": message})
            await inv.ctx.emit("agent.a2a.sent", {"agent": inv.agent_name, "url": url})

            deadline = asyncio.get_running_loop().time() + float(cfg.get("timeout") or 3600)
            while result.get("kind") == "task" and result.get("status", {}).get("state") not in TERMINAL:
                state = result.get("status", {}).get("state")
                if state in BLOCKED:
                    raise StepError(f"remote agent requires {state}", details={"task_id": result.get("id")})
                if asyncio.get_running_loop().time() > deadline:
                    raise StepError("remote agent timed out", retryable=True)
                await asyncio.sleep(float(cfg.get("poll_interval") or 5))
                result = await self._rpc(client, url, "tasks/get", {"id": result["id"]})

        state = result.get("status", {}).get("state", "completed") if result.get("kind") == "task" else "completed"
        if state != "completed":
            raise StepError(f"remote agent task {state}", details={"task": _brief(result)})
        texts, data = _collect_parts(result)
        try:
            outputs = validate_params(inv.outputs, data)
        except ParamValidationError as exc:
            raise StepError(f"remote agent outputs invalid: {exc}", details={"data": data}) from exc
        return AgentResult(outputs=outputs, summary="\n".join(texts)[:8000])

    async def _discover(self, client: httpx.AsyncClient, cfg: dict[str, Any]) -> str:
        card_url = cfg.get("card_url")
        if not card_url:
            raise StepError("a2a agents need runtime_config.url or card_url")
        response = await client.get(card_url)
        response.raise_for_status()
        return response.json()["url"]

    async def _auth_headers(self, inv: AgentInvocation, auth: dict[str, Any]) -> dict[str, str]:
        if not auth:
            return {}
        value = await inv.ctx.engine.secrets.get(inv.ctx.tenant_id, auth["secret"])
        if value is None:
            raise MissingConfiguration(f"secret '{auth['secret']}' for A2A auth is not set", kind="Secret", name=auth["secret"])
        inv.ctx.redactor.add(value)
        if auth.get("type", "bearer") == "bearer":
            return {"Authorization": f"Bearer {value}"}
        return {auth["header"]: value}

    async def _rpc(self, client: httpx.AsyncClient, url: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await client.post(
            url, json={"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        )
        if response.status_code >= 500:
            raise StepError(f"A2A {method} failed: HTTP {response.status_code}", retryable=True)
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise StepError(f"A2A {method} error: {body['error'].get('message')}", details=body["error"])
        return body.get("result") or {}


def _collect_parts(result: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if result.get("kind") == "message":
        parts = result.get("parts", [])
    else:
        for artifact in result.get("artifacts") or []:
            parts += artifact.get("parts", [])
        message = (result.get("status") or {}).get("message") or {}
        parts += message.get("parts", [])
    texts = [p["text"] for p in parts if p.get("kind") == "text" and p.get("text")]
    data: dict[str, Any] = {}
    for part in parts:
        if part.get("kind") == "data" and isinstance(part.get("data"), dict):
            data = part["data"]
            break
    else:
        for text in texts:  # tolerate agents that return JSON as text
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    data = parsed
                    break
            except ValueError:
                continue
    return texts, data


def _brief(result: dict[str, Any]) -> dict[str, Any]:
    return {"id": result.get("id"), "status": result.get("status")}
