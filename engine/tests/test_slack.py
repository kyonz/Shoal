"""The built-in Slack integration: messages, interactive approvals, inputs, events and the app install."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr

from shoal.api.app import create_app
from shoal.core import clock
from shoal.core.errors import InvalidRequest
from shoal.core.loader import parse_resource
from shoal.integrations.base import IntegrationError
from shoal.integrations.slack import SlackIntegration
from shoal.sdk.testing import IntegrationContract, IntegrationHarness, MockSlack, slack_request

SECRET = "slack-signing-secret"
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "slack-approval"


class TestSlackContract(IntegrationContract):
    integration = SlackIntegration
    sample_config = {"bot_token": "xoxb-sample", "signing_secret": SECRET}
    sample_webhook = MockSlack(signing_secret=SECRET).event({"type": "app_mention", "user": "U1", "channel": "C1",
                                                              "text": "<@U0BOT> hi", "ts": "1.1"})


def doc(kind: str, name: str, spec: dict) -> object:
    return parse_resource({"apiVersion": "shoal.dev/v1", "kind": kind, "metadata": {"name": name}, "spec": spec})


@pytest.fixture
def slack() -> MockSlack:
    slack = MockSlack(signing_secret=SECRET)
    slack.user("U_DANA", "dana", email="dana@acme.test", real_name="Dana Scully")
    slack.user("U_FOX", "fox", email="fox@acme.test")
    slack.user("U_NOBODY", "nobody", email="nobody@elsewhere.test")
    slack.channel("#deploys", "C_DEPLOYS")
    return slack


@pytest.fixture
async def world(engine, tenant, slack):
    """A tenant with a Slack connection, Dana (operator, linked by slack_id) and Fox (viewer, matched by email)."""
    engine.http_transport = slack.transport
    secrets = engine.secrets.writable()
    await secrets.put(tenant.id, "slack-bot", slack.bot_token)
    await secrets.put(tenant.id, "slack-signing", SECRET)
    await engine.store.apply_resource(tenant.id, doc("Connection", "slack", {
        "integration": "slack", "config": {"default_channel": "#deploys"},
        "secrets": {"bot_token": "slack-bot", "signing_secret": "slack-signing"},
    }))
    d = engine.directory
    dana = await d.create_user(tenant.id, "dana", email="dana@acme.test", display_name="Dana Scully")
    fox = await d.create_user(tenant.id, "fox", email="fox@acme.test", display_name="Fox Mulder")
    await d.bind(tenant.id, "operator", "user", dana.id)
    await d.bind(tenant.id, "viewer", "user", fox.id)
    await engine.store.apply_resource(tenant.id, doc("Identity", "dana", {"slack_id": "U_DANA", "actors": ["user:dana"]}))
    return engine, tenant


async def deliver(engine, tenant, delivery: tuple[dict, bytes]) -> dict:
    headers, body = delivery
    url = await engine.integrations.webhook_url(tenant.id, "slack")
    return await engine.integrations.handle_webhook(url.rsplit("/", 1)[-1], headers, body)


async def start(engine, tenant, steps: list[dict], name: str = "gated", inputs: dict | None = None):
    await engine.store.apply_resource(tenant.id, doc("Workflow", name, {"steps": steps}))
    run = await engine.executor.start_run(tenant.id, name, inputs or {})
    await engine.run_until_idle()
    return run


APPROVAL = {"id": "ok", "type": "approval", "message": "Deploy *api* to prod?",
            "notify": [{"integration": "slack", "to": "#deploys"}]}


# --------------------------------------------------------------------------- actions


async def test_post_message_dm_by_email_and_signal_buttons_need_a_run(slack):
    harness = IntegrationHarness(SlackIntegration, config={"bot_token": slack.bot_token}, http=slack)
    out = await harness.call("post_message", {"channel": "fox@acme.test", "text": "hello"})
    assert out["channel"] == "D_FOX" and out["thread_key"] == f"slack:thread:D_FOX:{out['ts']}"
    assert slack.last_message("D_FOX").text == "hello"
    with pytest.raises(IntegrationError, match="only works in a run"):
        await harness.call("post_message", {"channel": "#deploys", "text": "pick", "buttons": [{"label": "Go", "signal": "go"}]})
    with pytest.raises(IntegrationError, match="no bot token"):
        await IntegrationHarness(SlackIntegration, config={}, http=slack).call("post_message", {"channel": "C1", "text": "x"})


async def test_slack_errors_are_integration_errors(slack):
    harness = IntegrationHarness(SlackIntegration, config={"bot_token": "xoxb-wrong"}, http=slack)
    with pytest.raises(IntegrationError, match="invalid_auth") as exc:
        await harness.test_connection()
    assert not exc.value.retryable


async def test_signature_is_required_and_checked(world, slack):
    engine, tenant = world
    headers, body = slack.url_verification()
    with pytest.raises(InvalidRequest):
        await deliver(engine, tenant, ({**headers, "X-Slack-Signature": "v0=bad"}, body))
    stale = slack_request(SECRET, {"type": "url_verification", "challenge": "c"}, timestamp=int(clock.now().timestamp()) - 600)
    with pytest.raises(InvalidRequest):
        await deliver(engine, tenant, stale)
    result = await deliver(engine, tenant, (headers, body))
    assert result["reply"].body == {"challenge": "challenge-token"}


# --------------------------------------------------------------------------- approvals


async def test_approval_in_slack_checks_who_clicked(world, slack):
    engine, tenant = world
    run = await start(engine, tenant, [APPROVAL, {"id": "done", "type": "set", "values": {"approved": "{{ steps.ok.output.approved }}"}}])
    message = slack.last_message("#deploys")
    assert "Deploy *api* to prod?" in message.all_text()
    assert {b["action_id"] for b in message.buttons()} == {"shoal_approve", "shoal_reject", "shoal_open"}

    # someone Shoal does not know
    result = await deliver(engine, tenant, slack.click(message, "shoal_approve", user="U_NOBODY"))
    assert not result["interactions"][0]["ok"] and "does not know who you are" in result["interactions"][0]["error"]
    assert "does not know who you are" in slack.responses[-1]["text"]
    # Fox maps by email but only has viewer
    result = await deliver(engine, tenant, slack.click(message, "shoal_approve", user="U_FOX"))
    assert "lacks 'runs:approve'" in result["interactions"][0]["error"]
    assert (await engine.store.get_step(run.id, "ok")).status == "waiting"
    # Dana is linked by slack_id and may approve
    result = await deliver(engine, tenant, slack.click(message, "shoal_approve", user="U_DANA"))
    assert result["interactions"][0] == {"kind": "approval", "run_id": run.id, "ok": True, "by": "Dana Scully",
                                         "approved": True, "step_id": "ok"}
    await engine.run_until_idle()
    assert (await engine.store.get_step(run.id, "ok")).output["by"] == "Dana Scully"
    assert (await engine.store.get_run(tenant.id, run.id)).status == "succeeded"
    assert "Approved by Dana Scully" in message.all_text() and not message.buttons()
    # a late click is refused with who decided
    result = await deliver(engine, tenant, slack.click(
        type(message)(message.channel, message.ts, "", message.updates[0]["blocks"]), "shoal_reject", user="U_DANA"))
    assert "already approved by Dana Scully" in result["interactions"][0]["error"]


async def test_approvers_list_applies_to_slack(world, slack):
    engine, tenant = world
    await start(engine, tenant, [{**APPROVAL, "approvers": ["group:release-managers"]}])
    result = await deliver(engine, tenant, slack.click(slack.last_message("#deploys"), "shoal_approve", user="U_DANA"))
    assert "not one of this step's approvers" in result["interactions"][0]["error"]
    group = await engine.directory.create_group(tenant.id, "release-managers")
    dana = await engine.directory.find_user(tenant.id, username="dana")
    await engine.directory.add_group_members(tenant.id, group.id, [dana.id])
    result = await deliver(engine, tenant, slack.click(slack.last_message("#deploys"), "shoal_approve", user="U_DANA"))
    assert result["interactions"][0]["ok"]


async def test_decisions_elsewhere_and_timeouts_update_the_message(world, slack):
    engine, tenant = world
    run = await start(engine, tenant, [APPROVAL])
    await engine.executor.submit_approval(tenant.id, run.id, "ok", approved=False, by="Walter", comment="not today")
    await engine.run_until_idle()
    assert "Rejected by Walter: not today" in slack.last_message("#deploys").all_text()

    await start(engine, tenant, [{**APPROVAL, "max_wait": "1h"}], name="timed")
    clock.advance(timedelta(hours=1, seconds=1))
    await engine.run_until_idle()
    assert "Timed out, rejected automatically" in slack.last_message("#deploys").all_text()


async def test_reject_with_a_comment_through_a_modal(world, slack):
    engine, tenant = world
    run = await start(engine, tenant, [{**APPROVAL, "notify": [{"integration": "slack", "with": {"ask_comment": True}}]}])
    await deliver(engine, tenant, slack.click(slack.last_message("#deploys"), "shoal_reject", user="U_DANA"))
    assert (await engine.store.get_step(run.id, "ok")).status == "waiting"
    view = slack.views[-1]
    assert view["callback_id"] == "shoal_approval_comment"
    result = await deliver(engine, tenant, slack.submit(view, {"comment": "tests are red"}, user="U_DANA"))
    assert result["reply"].body is None and result["interactions"][0]["ok"]
    await engine.run_until_idle()
    output = (await engine.store.get_step(run.id, "ok")).output
    assert output["approved"] is False and output["comment"] == "tests are red"
    # a refused modal submission shows the error in the form
    run2 = await start(engine, tenant, [{**APPROVAL, "notify": [{"integration": "slack", "with": {"ask_comment": True}}]}], name="g2")
    await deliver(engine, tenant, slack.click(slack.last_message("#deploys"), "shoal_approve", user="U_FOX"))
    result = await deliver(engine, tenant, slack.submit(slack.views[-1], {"comment": "lgtm"}, user="U_FOX"))
    assert result["reply"].body["response_action"] == "errors"
    assert (await engine.store.get_step(run2.id, "ok")).status == "waiting"


async def test_notify_failure_retries_without_posting_twice(world, slack):
    engine, tenant = world
    run = await start(engine, tenant, [{**APPROVAL, "notify": [
        {"integration": "slack", "to": "#deploys"}, {"connection": "nope", "to": "#x"},
    ]}])
    step = await engine.store.get_step(run.id, "ok")
    assert step.status == "failed" and "nope" in json.dumps(step.error)
    assert len(slack.channel_messages("#deploys")) == 1


# --------------------------------------------------------------------------- inputs and signals


async def test_request_input_modal_answers_reach_the_run(world, slack):
    engine, tenant = world
    run = await start(engine, tenant, [
        {"id": "ask", "type": "action", "action": "slack.request_input", "with": {
            "text": "Which version should we ship?", "signal": "release",
            "fields": [{"name": "version", "label": "Version"},
                       {"name": "env", "label": "Environment", "type": "select", "options": ["staging", "prod"]}]}},
        {"id": "wait", "type": "wait", "signal": {"name": "release"}},
        {"id": "done", "type": "set", "values": {"v": "{{ steps.wait.output.payload['values']['version'] }}"}},
    ])
    await deliver(engine, tenant, slack.click(slack.last_message("#deploys"), "shoal_input", user="U_FOX"))
    view = slack.views[-1]
    assert [b["block_id"] for b in view["blocks"]] == ["f_version", "f_env"]
    # Fox lacks runs:signal (viewer): the form shows why
    result = await deliver(engine, tenant, slack.submit(view, {"version": "1.4.2", "env": "prod"}, user="U_FOX"))
    assert "lacks 'runs:signal'" in result["reply"].body["errors"]["f_version"]
    result = await deliver(engine, tenant, slack.submit(view, {"version": "1.4.2", "env": "prod"}, user="U_DANA"))
    assert result["interactions"][0]["ok"]
    await engine.run_until_idle()
    wait = await engine.store.get_step(run.id, "wait")
    assert wait.output["payload"] == {"values": {"version": "1.4.2", "env": "prod"}, "user": {"id": "U_DANA", "name": "dana"}}
    assert wait.output["source"] == "slack" and wait.output["sender"] == "user:dana"
    assert (await engine.store.get_step(run.id, "done")).output == {"v": "1.4.2"}


async def test_buttons_that_signal_the_run(world, slack):
    engine, tenant = world
    await engine.directory.bind(tenant.id, "operator", "user", (await engine.directory.find_user(tenant.id, username="fox")).id)
    run = await start(engine, tenant, [
        {"id": "ask", "type": "action", "action": "slack.post_message", "with": {
            "text": "Canary looks healthy. Continue?",
            "buttons": [{"label": "Promote", "signal": "choice", "payload": "promote", "style": "primary"},
                        {"label": "Roll back", "signal": "choice", "payload": "rollback", "style": "danger"}]}},
        {"id": "wait", "type": "wait", "signal": {"name": "choice"}},
    ])
    message = slack.last_message("#deploys")
    await deliver(engine, tenant, slack.click(message, "Roll back", user="U_FOX"))
    await engine.run_until_idle()
    assert (await engine.store.get_step(run.id, "wait")).output["payload"]["value"] == "rollback"
    replaced = slack.responses[-1]
    assert replaced["replace_original"] and "chose *Roll back*" in json.dumps(replaced["blocks"])


# --------------------------------------------------------------------------- events and commands


async def test_mentions_and_slash_commands_fire_triggers(world, slack):
    engine, tenant = world
    await engine.store.apply_resource(tenant.id, doc("Workflow", "echo", {
        "inputs": {"text": {"type": "string"}}, "steps": [{"id": "s", "type": "set", "values": {"text": "{{ inputs.text }}"}}]}))
    await engine.store.apply_resource(tenant.id, doc("Trigger", "mention", {
        "connection": "slack", "event": "app_mention", "workflow": "echo", "inputs": {"text": "{{ event.command_text }}"}}))
    await engine.store.apply_resource(tenant.id, doc("Trigger", "cmd", {
        "connection": "slack", "event": "slash_command", "filter": "{{ event.args[0] == 'echo' }}",
        "workflow": "echo", "inputs": {"text": "{{ event.text }}"}}))

    result = await deliver(engine, tenant, slack.event({"type": "app_mention", "user": "U_DANA", "channel": "C_DEPLOYS",
                                                        "text": "<@U0BOT> ship it", "ts": "5.5"}))
    assert result["triggers"][0]["status"] == "fired" and result["reply"].status == 200
    run = await engine.store.get_run(tenant.id, result["triggers"][0]["run_id"])
    assert run.inputs == {"text": "ship it"} and run.initiator["identity"] == "dana"

    # the app's own messages never trigger anything
    assert (await deliver(engine, tenant, slack.event({"type": "message", "bot_id": "B1", "channel": "C1", "text": "x", "ts": "6"})))["events"] == 0

    result = await deliver(engine, tenant, slack.command("echo hello there", user="U_FOX"))
    reply = result["reply"].body
    assert reply["response_type"] == "ephemeral" and "cmd started run" in reply["text"]
    assert f"/ns/default/runs/{result['triggers'][0]['run_id']}" in reply["text"]
    result = await deliver(engine, tenant, slack.command("dance", user="U_FOX"))
    assert result["reply"].body["text"] == "No Shoal trigger handles `/shoal dance`."


async def test_thread_replies_wake_a_waiting_step(world, slack):
    engine, tenant = world
    run = await start(engine, tenant, [
        {"id": "post", "type": "action", "action": "slack.post_message", "with": {"text": "Reply in thread with the ticket id"}},
        {"id": "reply", "type": "wait", "signal": {"event": "{{ steps.post.output.thread_key }}"}},
    ])
    post = (await engine.store.get_step(run.id, "post")).output
    await deliver(engine, tenant, slack.event({"type": "message", "user": "U_FOX", "channel": post["channel"], "text": "OPS-12",
                                               "ts": "9.9", "thread_ts": post["ts"]}))
    await engine.run_until_idle()
    assert (await engine.store.get_step(run.id, "reply")).output["payload"]["data"]["text"] == "OPS-12"


# --------------------------------------------------------------------------- setup and install


async def test_setup_manifest_and_add_to_slack(engine, tenant, slack):
    engine.http_transport = slack.transport
    engine.settings.admin_token = SecretStr("admin")
    await engine.secrets.writable().put(tenant.id, "slack-client-secret", "shh")
    await engine.store.apply_resource(tenant.id, doc("Connection", "slack", {
        "integration": "slack", "config": {"client_id": "123.456"}, "secrets": {"client_secret": "slack-client-secret"}}))
    app = create_app(engine=engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shoal.test",
                                 headers={"Authorization": "Bearer admin", "X-Shoal-Tenant": "acme"}) as api:
        setup = (await api.get("/v1/namespaces/default/connections/slack/webhook")).json()
        assert setup["install"] is True
        assert setup["manifest"]["settings"]["interactivity"]["request_url"] == setup["url"]
        assert setup["manifest"]["oauth_config"]["redirect_urls"] == ["http://shoal.test/v1/install/callback"]

        r = await api.post("/v1/namespaces/default/connections/slack/install")
        assert r.status_code == 200, r.text
        url = urlparse(r.json()["authorize_url"])
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        assert url.netloc == "slack.com" and query["client_id"] == "123.456" and "chat:write" in query["scope"]

        r = await api.get("/v1/install/callback", params={"code": "install-code", "state": query["state"]})
        assert r.status_code == 303 and r.headers["location"].endswith("/ns/default/connections/slack?installed=1")
        r = await api.get("/v1/install/callback", params={"code": "install-code", "state": "forged"})
        assert r.status_code == 401
    connection = await engine.store.get_resource(tenant.id, "Connection", "slack")
    assert connection.spec.secrets["bot_token"] == "slack-bot-token"
    assert await engine.secrets.get(tenant.id, "slack-bot-token") == slack.bot_token
    ctx = await engine.integrations.connection(tenant.id, "slack")
    assert (await ctx.integration.test_connection(ctx))["team"] == "Acme"


async def test_webhook_endpoint_answers_slack(world, slack):
    engine, tenant = world
    app = create_app(engine=engine)
    url = await engine.integrations.webhook_url(tenant.id, "slack")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shoal.test") as client:
        headers, body = slack.url_verification("abc")
        r = await client.post(urlparse(url).path, content=body, headers=headers)
        assert r.status_code == 200 and r.json() == {"challenge": "abc"}
        headers, body = slack.event({"type": "reaction_added", "user": "U_FOX", "reaction": "eyes", "item": {"channel": "C1", "ts": "1.2"}})
        r = await client.post(urlparse(url).path, content=body, headers=headers)
        assert r.status_code == 200 and r.content == b""


async def test_example_ship_workflow(world, slack):
    """examples/slack-approval end to end: /shoal ship, a form answer, an approval with a comment, the announcement."""
    from shoal.core.loader import load_path

    engine, tenant = world
    for resource in load_path(EXAMPLE):
        if resource.to_document()["kind"] != "Connection":  # keep the test connection
            await engine.store.apply_resource(tenant.id, resource)
    group = await engine.directory.create_group(tenant.id, "release-managers")
    await engine.directory.add_group_members(tenant.id, group.id, [(await engine.directory.find_user(tenant.id, username="dana")).id])

    result = await deliver(engine, tenant, slack.command("ship api", user="U_DANA"))
    run_id = result["triggers"][0]["run_id"]
    await engine.run_until_idle()
    await deliver(engine, tenant, slack.click(slack.last_message("#deploys"), "shoal_input", user="U_DANA"))
    await deliver(engine, tenant, slack.submit(slack.views[-1], {"version": "2.0.1"}, user="U_DANA"))
    await engine.run_until_idle()
    approval = slack.last_message("#deploys")
    assert "Deploy *api* 2.0.1 to production?" in approval.all_text()
    await deliver(engine, tenant, slack.click(approval, "shoal_approve", user="U_DANA"))
    await deliver(engine, tenant, slack.submit(slack.views[-1], {"comment": "go"}, user="U_DANA"))
    await engine.run_until_idle()
    assert (await engine.store.get_run(tenant.id, run_id)).status == "succeeded"
    assert slack.last_message("#deploys").text == ":rocket: api 2.0.1 approved by Dana Scully"
    assert "Approved by Dana Scully: go" in approval.all_text()
