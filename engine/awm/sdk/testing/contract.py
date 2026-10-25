"""Contract tests every integration should pass.

For a Python integration, subclass :class:`IntegrationContract` in a test
module and pytest runs the checks::

    class TestLinearContract(IntegrationContract):
        integration = LinearIntegration
        sample_config = {"api_key": "lin_test"}
        sample_webhook = signed_webhook("whsec", {"type": "Issue", "action": "create"}, header="Linear-Signature")

For a service in any language, run the remote checks against it::

    python -m awm.sdk.testing.contract https://linear-integration.internal --token $TOKEN
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import inspect
import json
import re
import sys
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from awm.integrations.base import Integration, IntegrationEvent
from awm.integrations.remote import PROTOCOL, RemoteClient, RemoteManifest
from awm.sdk.manifest import manifest_data

KEY = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
SECRETISH = re.compile(r"(^|_)(token|secret|password|api_key|apikey|private_key|client_secret)($|_)")


def _json_ok(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _object_schema(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("type", "object") == "object"


def _event_declared(event_type: str, declared: list[str]) -> bool:
    return any(fnmatch.fnmatch(event_type, pattern) or event_type == pattern for pattern in declared)


def check_manifest(manifest: RemoteManifest) -> list[str]:
    """Problems with a manifest, from any source; empty means it is fine."""
    problems: list[str] = []
    if manifest.protocol != PROTOCOL:
        problems.append(f"protocol is {manifest.protocol!r}, expected {PROTOCOL!r}")
    if not KEY.match(manifest.key):
        problems.append(f"key {manifest.key!r} must match {KEY.pattern} (no dots: actions are '<key>.<action>')")
    if not manifest.title.strip():
        problems.append("title is empty")
    if not _object_schema(manifest.connection_schema):
        problems.append("connection_schema must be a JSON object schema")
    properties = manifest.connection_schema.get("properties", {})
    for field in manifest.secret_fields:
        if properties and field not in properties:
            problems.append(f"secret field {field!r} is not in connection_schema")
    for name in properties:
        if SECRETISH.search(name.lower()) and name not in manifest.secret_fields:
            problems.append(f"connection field {name!r} looks like a credential but is not a secret field (use SecretStr)")
    for kind, items in (("action", manifest.actions), ("resource", manifest.resources), ("event", manifest.events)):
        names = [i.name for i in items]
        for dup in sorted({n for n in names if names.count(n) > 1}):
            problems.append(f"duplicate {kind} {dup!r}")
    for action in manifest.actions:
        if not NAME.match(action.name):
            problems.append(f"action {action.name!r} must match {NAME.pattern} (it is also an agent tool name)")
        if len(f"{manifest.key}__{action.name}") > 64:
            problems.append(f"tool name {manifest.key}__{action.name} is longer than 64 characters")
        if not action.description.strip():
            problems.append(f"action {action.name!r} has no description (agents choose tools by it)")
        if not _object_schema(action.input_schema):
            problems.append(f"action {action.name!r} input_schema must be an object schema")
        if action.output_schema is not None and not _object_schema(action.output_schema):
            problems.append(f"action {action.name!r} output_schema must be an object schema")
    for resource in manifest.resources:
        if not NAME.match(resource.name):
            problems.append(f"resource {resource.name!r} must match {NAME.pattern}")
        if not resource.description.strip():
            problems.append(f"resource {resource.name!r} has no description")
        if not _object_schema(resource.filter_schema):
            problems.append(f"resource {resource.name!r} filter_schema must be an object schema")
    for event in manifest.events:
        if not event.name.strip():
            problems.append("an event has an empty name")
        if not _json_ok(event.example):
            problems.append(f"event {event.name!r} example is not JSON")
    if manifest.events and not manifest.webhooks:
        problems.append("events are declared but the integration does not parse webhooks")
    return problems


def check_integration(integration: Integration | type[Integration]) -> list[str]:
    """Static problems with a Python integration class; empty means it passes."""
    cls = integration if isinstance(integration, type) else type(integration)
    problems: list[str] = []
    for attr in ("key", "title", "connection_model"):
        if not hasattr(cls, attr):
            problems.append(f"{cls.__name__} does not set {attr}")
    if problems:
        return problems
    if not (isinstance(cls.connection_model, type) and issubclass(cls.connection_model, BaseModel)):
        return [f"{cls.__name__}.connection_model must be a pydantic model"]
    for definition in cls.actions.values():
        fn = getattr(cls, definition.method, None)
        if not inspect.iscoroutinefunction(fn):
            problems.append(f"action {definition.name!r} ({definition.method}) must be async")
        if not (isinstance(definition.input_model, type) and issubclass(definition.input_model, BaseModel)):
            problems.append(f"action {definition.name!r} input must be a pydantic model")
    for definition in cls.resources.values():
        if not inspect.iscoroutinefunction(getattr(cls, definition.method, None)):
            problems.append(f"resource {definition.name!r} ({definition.method}) must be async")
    try:
        catalog = cls.catalog()
    except Exception as exc:  # schema generation failures are the usual cause
        return [*problems, f"catalog() failed: {exc}"]
    if not _json_ok(catalog):
        problems.append("catalog() is not JSON serialisable")
    data = manifest_data(cls)
    if not KEY.match(str(cls.key)):
        problems.append(f"key {cls.key!r} must match {KEY.pattern} (no dots: actions are '<key>.<action>')")
        data["key"] = "invalid"  # keep checking everything else
    try:
        manifest = RemoteManifest.model_validate(data)
    except ValidationError as exc:
        return [*problems, f"cannot be described as a remote manifest: {exc}"]
    return [*problems, *check_manifest(manifest)]


def check_config(integration: Integration | type[Integration], config: dict[str, Any]) -> list[str]:
    """A sample config validates and its secrets do not leak through repr/catalog."""
    cls = integration if isinstance(integration, type) else type(integration)
    try:
        model = cls.connection_model.model_validate(config)
    except ValidationError as exc:
        return [f"sample_config does not validate: {exc}"]
    problems = []
    rendered = repr(model) + json.dumps(cls.catalog())
    for name in cls.secret_fields():
        value = getattr(model, name, None)
        if isinstance(value, SecretStr) and value.get_secret_value() and value.get_secret_value() in rendered:
            problems.append(f"secret field {name!r} leaks into repr or catalog")
    return problems


class IntegrationContract:
    """Pytest mixin: subclass as ``TestXContract`` and set the class attributes."""

    integration: ClassVar[type[Integration]]
    sample_config: ClassVar[dict[str, Any] | None] = None
    #: ``(headers, body)`` of a genuine, correctly signed delivery.
    sample_webhook: ClassVar[tuple[dict[str, str], bytes] | None] = None
    #: Set False for integrations whose webhooks are deliberately unauthenticated.
    webhooks_are_signed: ClassVar[bool] = True

    def test_contract(self) -> None:
        problems = check_integration(self.integration)
        assert not problems, "integration contract:\n  - " + "\n  - ".join(problems)

    def test_sample_config(self) -> None:
        if self.sample_config is None:
            return
        problems = check_config(self.integration, self.sample_config)
        assert not problems, "\n".join(problems)

    async def test_sample_webhook(self) -> None:
        if self.sample_webhook is None:
            return
        from awm.sdk.testing.harness import IntegrationHarness

        harness = IntegrationHarness(self.integration, config=self.sample_config or {})
        headers, body = self.sample_webhook
        assert await harness.verify_webhook(headers, body), "sample_webhook should verify"
        events = await harness.parse_webhook(headers, body)
        assert all(isinstance(e, IntegrationEvent) for e in events)
        declared = [e.name for e in self.integration.events]
        undeclared = [e.type for e in events if declared and not _event_declared(e.type, declared)]
        assert not undeclared, f"events {undeclared} are not declared in {self.integration.__name__}.events"
        if self.webhooks_are_signed:
            tampered = body + b" "
            assert not await harness.verify_webhook(headers, tampered), "a tampered body must fail verification"
            unsigned = {k: v for k, v in headers.items() if "sign" not in k.lower() and "token" not in k.lower()}
            assert not await harness.verify_webhook(unsigned, body), "a delivery without a signature must fail"


# --------------------------------------------------------------------------- remote services


async def check_remote_service(
    url: str, token: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[str]:
    """Contract checks against a running remote integration service (any language)."""

    def http(**kwargs: Any) -> httpx.AsyncClient:
        if transport is not None:
            kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)

    client = RemoteClient(url, token, http=http)
    try:
        payload = await client.request("GET", "/manifest")
    except Exception as exc:
        return [f"GET /manifest failed: {exc}"]
    try:
        manifest = RemoteManifest.model_validate(payload)
    except ValidationError as exc:
        return [f"invalid manifest: {exc}"]
    problems = check_manifest(manifest)
    if token:
        anonymous = RemoteClient(url, None, http=http)
        for method, path in (("GET", "/manifest"), ("POST", "/webhooks/parse")):
            try:
                await anonymous.request(method, path, {})
                problems.append(f"{method} {path} accepted a request without the bearer token")
            except Exception as exc:
                if getattr(exc, "status", None) not in (401, 403):
                    problems.append(f"{method} {path} without a token should be 401/403, got: {exc}")
    body = {"protocol": PROTOCOL, "connection": {}, "context": {"tenant_id": "contract", "connection": "contract"}}
    try:
        await client.request("POST", "/actions/__awm_contract_missing__", {**body, "params": {}})
        problems.append("an unknown action should return 404")
    except Exception as exc:
        if getattr(exc, "status", None) != 404:
            problems.append(f"an unknown action should return 404 with an error body, got: {exc}")
    if manifest.webhooks:
        try:
            result = await client.request("POST", "/webhooks/verify", {**body, "headers": {}, "body_b64": ""})
            if not isinstance(result.get("valid"), bool):
                problems.append("POST /webhooks/verify must answer {\"valid\": true|false}")
        except Exception as exc:
            if getattr(exc, "status", None) not in (400, 401, 422):
                problems.append(f"POST /webhooks/verify failed: {exc}")
    return problems


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wrapper
    parser = argparse.ArgumentParser(description="Run AWM contract checks against a remote integration service.")
    parser.add_argument("url")
    parser.add_argument("--token")
    args = parser.parse_args(argv)
    problems = asyncio.run(check_remote_service(args.url, args.token))
    for problem in problems:
        print(f"FAIL {problem}")
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
