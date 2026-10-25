"""Behaviour tests against a mocked Starter API."""

import pytest
from awm.sdk.testing import IntegrationHarness, MockHTTP, engine_harness, signed_webhook

from awm_starter import StarterIntegration

CONFIG = {"base_url": "https://api.starter.example", "api_token": "tok_test", "webhook_secret": "whsec_test"}


@pytest.fixture
def api() -> MockHTTP:
    return MockHTTP(host="api.starter.example")


async def test_create_ticket(api):
    route = api.post(
        "/tickets", json={"id": "OPS-7", "url": "https://starter.example/OPS-7", "status": "open"}
    )
    harness = IntegrationHarness(StarterIntegration, config=CONFIG, http=api)

    out = await harness.call("create_ticket", {"project": "OPS", "title": "Flaky test"})

    assert out == {"id": "OPS-7", "url": "https://starter.example/OPS-7", "status": "open"}
    assert route.last.json["title"] == "Flaky test"
    assert route.last.headers["authorization"] == "Bearer tok_test"


async def test_list_tickets_paginates(api):
    def tickets(request):
        page = int(request.url.params["page"])
        return {"items": [{"id": f"OPS-{page}"}], "has_more": page < 3}

    api.get("/tickets", handler=tickets)
    harness = IntegrationHarness(StarterIntegration, config=CONFIG, http=api)

    items = await harness.query_all("ticket", {"project": "OPS"})

    assert [i["id"] for i in items] == ["OPS-1", "OPS-2", "OPS-3"]


async def test_webhook_events(api):
    harness = IntegrationHarness(StarterIntegration, config=CONFIG, http=api)
    headers, body = signed_webhook(
        "whsec_test", {"action": "updated", "ticket": {"id": "OPS-1"}}, header="X-Starter-Signature"
    )

    [event] = await harness.webhook(headers, body)

    assert event.type == "ticket.updated"
    assert event.wake_keys == ["starter:ticket:OPS-1"]


WORKFLOW = """
apiVersion: awm.dev/v1
kind: Workflow
metadata: { name: triage }
spec:
  inputs: { ticket: { type: string } }
  steps:
    - { id: note, type: set, values: { ticket: "{{ inputs.ticket }}" } }
"""

TRIGGER = """
apiVersion: awm.dev/v1
kind: Trigger
metadata: { name: on-ticket }
spec:
  connection: starter
  event: ticket.created
  workflow: triage
  inputs: { ticket: "{{ event.ticket.id }}" }
"""


async def test_webhook_fires_a_trigger_in_the_engine(api):
    async with engine_harness(StarterIntegration, http=api) as world:
        await world.connect(
            "starter",
            "starter",
            config={"base_url": CONFIG["base_url"]},
            secrets={"api_token": "tok_test", "webhook_secret": "whsec_test"},
        )
        await world.apply(WORKFLOW, TRIGGER)
        headers, body = signed_webhook(
            "whsec_test", {"action": "created", "ticket": {"id": "OPS-9"}}, header="X-Starter-Signature"
        )

        result = await world.webhook("starter", headers, body)

        assert [t["trigger"] for t in result["triggers"]] == ["on-ticket"]
        assert len(await world.runs()) == 1
