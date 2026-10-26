"""The generic webhook connection: signed, token and unsigned deliveries, and its setup guide."""

from __future__ import annotations

import hashlib
import hmac
import shutil
import subprocess

import httpx
import pytest
from pydantic import SecretStr

from shoal.api.app import create_app
from shoal.integrations.webhook import WebhookIntegration
from shoal.sdk.testing import IntegrationHarness

ADMIN = "webhook-admin"
BODY = b'{"message": "hello"}'


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
async def api(engine):
    engine.settings.admin_token = SecretStr(ADMIN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine=engine)), base_url="http://shoal.test") as c:
        r = await c.post("/v1/tenants", json={"slug": "acme"}, headers={"Authorization": f"Bearer {ADMIN}"})
        c.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        yield c


async def _connection(api, config: dict, *, secret: str | None = None) -> str:
    spec: dict = {"integration": "webhook", "config": config}
    if secret is not None:
        assert (await api.put("/v1/namespaces/default/secrets/hook-secret", json={"value": secret})).status_code < 300
        spec["secrets"] = {"secret": "hook-secret"}
    r = await api.put("/v1/namespaces/default/connections/hooks", json={"spec": spec})
    assert r.status_code < 300, r.text
    return (await api.get("/v1/namespaces/default/connections/hooks/webhook")).json()["url"].removeprefix("http://shoal.test")


@pytest.mark.parametrize(
    ("config", "headers", "ok"),
    [
        ({"secret": "s"}, {}, False),
        ({"secret": "s", "allow_unsigned": True}, {}, True),
        ({"secret": "s", "allow_unsigned": True}, {"X-Signature-256": "sha256=bad"}, False),
        ({"secret": "s", "allow_unsigned": True}, {"X-Shoal-Token": "wrong"}, False),
        ({"secret": "s", "allow_unsigned": True}, {"X-Signature-256": _sign("s", BODY)}, True),
        ({"secret": "s"}, {"X-Shoal-Token": "s"}, True),
        ({}, {}, False),
        ({"allow_unsigned": True}, {}, True),
    ],
)
async def test_verification(config, headers, ok):
    assert await IntegrationHarness(WebhookIntegration, config=config).verify_webhook(headers, BODY) is ok


async def test_allow_unsigned_accepts_unsigned_deliveries_even_with_a_secret_set(api):
    url = await _connection(api, {"allow_unsigned": True}, secret="s3cret")
    assert (await api.post(url, content=BODY)).status_code == 202
    assert (await api.post(url, content=BODY, headers={"X-Signature-256": "sha256=bad"})).status_code == 422
    assert (await api.post(url, content=BODY, headers={"X-Signature-256": _sign("s3cret", BODY)})).status_code == 202


async def test_unsigned_deliveries_are_rejected_unless_allowed(api):
    url = await _connection(api, {}, secret="s3cret")
    assert (await api.post(url, content=BODY)).status_code == 422
    assert (await api.post(url, content=BODY, headers={"X-Shoal-Token": "s3cret"})).status_code == 202
    assert (await api.post(f"{url}?token=s3cret", content=BODY)).status_code == 202


async def test_setup_guide_shows_signing_and_example_requests(api):
    await _connection(api, {"signature_header": "X-Hub-Sig"}, secret="s3cret")
    setup = (await api.get("/v1/namespaces/default/connections/hooks/webhook")).json()
    guide = setup["guide"]
    assert guide["signature_header"] == "X-Hub-Sig" and guide["secret_set"] and not guide["allow_unsigned"]
    assert "unsigned ones are rejected" in guide["accepts"]
    titles = [e["title"] for e in guide["examples"]]
    assert titles == ["Signed (HMAC-SHA256)", "Shared token"]
    assert all(setup["url"] in e["command"] for e in guide["examples"])
    assert '-H "X-Hub-Sig: $SIG"' in guide["examples"][0]["command"]

    await _connection(api, {"allow_unsigned": True})
    guide = (await api.get("/v1/namespaces/default/connections/hooks/webhook")).json()["guide"]
    assert guide["examples"][-1]["title"] == "Unsigned" and not guide["secret_set"]
    assert "nothing is checked" in guide["accepts"]


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_signed_example_computes_the_signature_shoal_expects():
    guide = WebhookIntegration().webhook_setup_guide("http://x/v1/hooks/wh_1", None, {"secret"})
    command = guide["examples"][0]["command"].replace("<the connection secret>", "s3cret")
    script = command.split("\ncurl", 1)[0] + '\nprintf "%s|%s" "$SIG" "$BODY"'
    sig, body = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True).stdout.split("|", 1)
    assert sig == _sign("s3cret", body.encode())
