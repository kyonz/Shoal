"""Cloud identities: a namespace brings an AWS IAM role, grants workflows/agents use of it, and steps get
short-lived STS credentials that start from the deployment's own identity (§7.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from pydantic import SecretStr, ValidationError

from shoal.api.app import create_app
from shoal.cloud.aws import AwsCredentials, sigv4_headers
from shoal.core.models import parse_resource
from shoal.core.scope import use_namespace

ROLE = "arn:aws:iam::123456789012:role/deploy"
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY-session"
TOKEN = "FwoGZXIvYXdzEXAMPLE-session-token"


def doc(kind: str, name: str, spec: dict) -> dict:
    return {"apiVersion": "shoal.dev/v1", "kind": kind, "metadata": {"name": name}, "spec": spec}


def identity(name: str = "aws-prod", **spec) -> dict:
    return doc("CloudIdentity", name, {"aws": {"role_arn": ROLE, "region": "eu-west-1"},
                                       "allow": {"workflows": ["deploy"]}, **spec})


class FakeSTS:
    def __init__(self, error: str | None = None):
        self.requests: list[httpx.Request] = []
        self.error = error

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error:
            return httpx.Response(403, text=f"<ErrorResponse><Error><Type>Sender</Type><Code>{self.error}</Code>"
                                            "<Message>Not authorized to perform sts:AssumeRoleWithWebIdentity</Message>"
                                            "</Error></ErrorResponse>")
        action = self.form(request)["Action"]
        return httpx.Response(200, text=f"""<{action}Response xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <{action}Result>
    <Credentials><AccessKeyId>ASIAEXAMPLE</AccessKeyId><SecretAccessKey>{SECRET}</SecretAccessKey>
      <SessionToken>{TOKEN}</SessionToken><Expiration>2030-01-01T00:00:00Z</Expiration></Credentials>
    <AssumedRoleUser><Arn>arn:aws:sts::123456789012:assumed-role/deploy/s</Arn><AssumedRoleId>AROA:s</AssumedRoleId></AssumedRoleUser>
  </{action}Result>
</{action}Response>""")

    @staticmethod
    def form(request: httpx.Request) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


@pytest.fixture
def sts(engine):
    fake = FakeSTS()
    engine.http_transport = httpx.MockTransport(fake)
    return fake


async def deploy_workflow(engine, tenant, name: str = "deploy", **step) -> None:
    await engine.store.apply_resource(tenant.id, parse_resource(doc("Workflow", name, {"steps": [
        {"id": "aws", "type": "script", "cloud_identities": ["aws-prod"],
         "run": "echo $AWS_ACCESS_KEY_ID $AWS_REGION; echo $AWS_SECRET_ACCESS_KEY $AWS_SESSION_TOKEN", **step},
    ]})))


async def run(engine, tenant, workflow: str = "deploy"):
    started = await engine.executor.start_run(
        tenant.id, workflow, {}, started_by="user:dana@acme.com",
        initiator={"actor": "user:dana@acme.com", "email": "dana@acme.com", "source": "api"},
    )
    await engine.run_until_idle()
    return await engine.store.get_run(tenant.id, started.id), {s.step_id: s for s in await engine.store.get_steps(started.id)}


def test_spec_validation():
    parse_resource(identity())
    with pytest.raises(ValidationError, match="role_arn"):
        parse_resource(doc("CloudIdentity", "x", {"aws": {"role_arn": "arn:aws:iam::1:user/bob"}}))
    with pytest.raises(ValidationError, match="needs an 'aws' block"):
        parse_resource(doc("CloudIdentity", "x", {}))
    with pytest.raises(ValidationError, match="between 15m and 12h"):
        parse_resource(identity(aws={"role_arn": ROLE, "duration": "13h"}))
    with pytest.raises(ValidationError, match="credentials"):
        parse_resource(identity(provider="gcp", config={"private_key": "x"}))


def test_sigv4_matches_the_aws_test_suite():
    creds = AwsCredentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    headers = sigv4_headers("GET", "https://example.amazonaws.com/", b"", creds, "us-east-1", "service",
                            datetime(2015, 8, 30, 12, 36, tzinfo=UTC))
    assert headers["Authorization"].endswith(
        "SignedHeaders=host;x-amz-date, Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")


async def test_web_identity_session_is_injected_attributed_and_redacted(engine, tenant, sts):
    await engine.store.apply_resource(tenant.id, parse_resource(identity()))
    await deploy_workflow(engine, tenant)
    result, steps = await run(engine, tenant)
    assert result.status == "succeeded", result.error

    out = steps["aws"].output["stdout"]
    assert "ASIAEXAMPLE eu-west-1" in out
    assert SECRET not in out and TOKEN not in out and "*** ***" in out

    (request,) = sts.requests
    assert str(request.url) == "https://sts.eu-west-1.amazonaws.com/"
    assert "authorization" not in request.headers  # web identity needs no AWS credentials in Shoal
    form = FakeSTS.form(request)
    assert form["Action"] == "AssumeRoleWithWebIdentity" and form["RoleArn"] == ROLE
    assert form["RoleSessionName"].startswith(f"shoal-{result.id}-aws")
    claims = jwt.decode(form["WebIdentityToken"], options={"verify_signature": False})
    subject = f"tenant:{tenant.id}:namespace:default:cloud-identity:aws-prod"
    assert claims["sub"] == subject and claims["aud"] == "sts.amazonaws.com" and claims["iss"] == "http://shoal.test"
    assert claims["https://aws.amazon.com/source_identity"] == "dana@acme.com"
    tags = claims["https://aws.amazon.com/tags"]["principal_tags"]
    assert tags["shoal:run"] == [result.id] and tags["shoal:workflow"] == ["deploy"] and tags["shoal:initiator"] == ["dana@acme.com"]
    engine.workload.verify(form["WebIdentityToken"], "sts.amazonaws.com")  # signed by the published key

    events = {e.type: e.data for e in await engine.store.list_events(tenant.id, result.id)}
    issued = events["cloud_identity.issued"]
    assert issued["identity"] == "default/aws-prod" and issued["access_key_id"] == "ASIAEXAMPLE"
    assert SECRET not in str(events)
    (entry,) = await engine.store.list_audit(tenant.id, action="cloud-identity.issued")
    assert entry.actor == f"run:{result.id}" and entry.data["role_arn"] == ROLE and entry.data["source_identity"] == "dana@acme.com"


async def test_grants_are_enforced(engine, tenant, sts):
    await engine.store.apply_resource(tenant.id, parse_resource(identity()))
    await deploy_workflow(engine, tenant, name="other")
    result, _ = await run(engine, tenant, "other")
    assert result.status == "failed" and "allow.workflows" in str(result.error)
    assert not sts.requests
    (entry,) = await engine.store.list_audit(tenant.id, action="cloud-identity.denied")
    assert entry.outcome == "denied"

    # allow.agents limits use to agent steps running those agents, so a script step is refused
    await engine.store.apply_resource(tenant.id, parse_resource(identity(allow={"workflows": ["dep*"], "agents": ["infra"]})))
    await deploy_workflow(engine, tenant)
    result, _ = await run(engine, tenant)
    assert result.status == "failed" and "only agent steps" in str(result.error)


async def test_agent_steps_get_credentials_when_the_agent_is_granted(engine, tenant, sts):
    await engine.store.apply_resource(tenant.id, parse_resource(identity(allow={"workflows": ["deploy"], "agents": ["infra"]})))
    await engine.store.apply_resource(tenant.id, parse_resource(doc("Agent", "infra", {
        "provider": "scripted", "cloud_identities": ["aws-prod"],
        "provider_config": {
            "script": [{"tool_calls": [{"name": "shell", "input": {"command": "echo key=$AWS_ACCESS_KEY_ID"}}]}],
            "final_outputs": {"key": "{{ last_result.stdout }}"},
        },
    })))
    await engine.store.apply_resource(tenant.id, parse_resource(doc("Workflow", "deploy", {"steps": [
        {"id": "agent", "type": "agent", "agent": "infra", "prompt": "deploy", "outputs": {"key": {"type": "string"}}}]})))
    result, steps = await run(engine, tenant)
    assert result.status == "succeeded", result.error
    assert "key=ASIAEXAMPLE" in steps["agent"].output["key"]
    tags = jwt.decode(FakeSTS.form(sts.requests[0])["WebIdentityToken"], options={"verify_signature": False})
    assert tags["https://aws.amazon.com/tags"]["principal_tags"]["shoal:agent"] == ["infra"]


async def test_shared_identities_need_a_namespace_qualified_grant(engine, tenant, sts):
    with use_namespace("shared"):
        await engine.store.apply_resource(tenant.id, parse_resource(identity()))
    with use_namespace("team-a"):
        await deploy_workflow(engine, tenant)
        result, _ = await run(engine, tenant)
        assert result.status == "failed" and "team-a/deploy" in str(result.error)
    with use_namespace("shared"):
        await engine.store.apply_resource(tenant.id, parse_resource(identity(allow={"workflows": ["team-a/deploy"]})))
    with use_namespace("team-a"):
        result, _ = await run(engine, tenant)
    assert result.status == "succeeded", result.error
    claims = jwt.decode(FakeSTS.form(sts.requests[-1])["WebIdentityToken"], options={"verify_signature": False})
    assert claims["sub"].endswith(":namespace:shared:cloud-identity:aws-prod")
    assert claims["https://aws.amazon.com/tags"]["principal_tags"]["shoal:namespace"] == ["team-a"]


async def test_sts_errors_fail_the_step_clearly(engine, tenant):
    engine.http_transport = httpx.MockTransport(FakeSTS(error="AccessDenied"))
    await engine.store.apply_resource(tenant.id, parse_resource(identity()))
    await deploy_workflow(engine, tenant)
    result, _ = await run(engine, tenant)
    assert result.status == "failed" and "AccessDenied" in str(result.error)
    assert (await engine.store.list_audit(tenant.id, action="cloud-identity.failed"))


async def test_ambient_trust_uses_the_deployment_identity_with_external_id(engine, tenant, sts, monkeypatch):
    await engine.store.apply_resource(tenant.id, parse_resource(identity(trust="ambient")))
    await deploy_workflow(engine, tenant)
    result, _ = await run(engine, tenant)
    assert result.status == "failed" and "SHOAL_AWS_AMBIENT" in str(result.error)

    engine.settings.aws_ambient = True
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIADEPLOYMENT")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "deployment-secret")
    result, _ = await run(engine, tenant)
    assert result.status == "succeeded", result.error
    request = sts.requests[-1]
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIADEPLOYMENT/")
    form = FakeSTS.form(request)
    assert form["Action"] == "AssumeRole" and form["SourceIdentity"] == "dana@acme.com"
    assert form["ExternalId"] == f"tenant:{tenant.id}:namespace:default:cloud-identity:aws-prod"
    tags = {form[f"Tags.member.{i}.Key"]: form[f"Tags.member.{i}.Value"] for i in range(1, 7)}
    assert tags["shoal:run"] == result.id and tags["shoal:initiator"] == "dana@acme.com"


async def test_ambient_credentials_from_eks_pod_identity(engine, tenant, monkeypatch, tmp_path):
    fake = FakeSTS()
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "169.254.170.23":
            seen.append(request)
            return httpx.Response(200, json={"AccessKeyId": "ASIAPOD", "SecretAccessKey": "pod-secret",
                                             "Token": "pod-token", "Expiration": "2030-01-01T00:00:00Z"})
        return fake(request)

    engine.http_transport = httpx.MockTransport(transport)
    engine.settings.aws_ambient = True
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_WEB_IDENTITY_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "token").write_text("pod-identity-token\n")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://169.254.170.23/v1/credentials")
    monkeypatch.setenv("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", str(tmp_path / "token"))
    await engine.store.apply_resource(tenant.id, parse_resource(identity(trust="ambient")))
    await deploy_workflow(engine, tenant)
    result, _ = await run(engine, tenant)
    assert result.status == "succeeded", result.error
    assert seen[0].headers["authorization"] == "pod-identity-token"
    assert "Credential=ASIAPOD/" in fake.requests[-1].headers["authorization"]
    assert fake.requests[-1].headers["x-amz-security-token"] == "pod-token"


ADMIN = "cloud-admin"


@pytest.fixture
async def admin(engine):
    engine.settings.admin_token = SecretStr(ADMIN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine=engine)), base_url="http://shoal.test") as c:
        r = await c.post("/v1/tenants", json={"slug": "acme"}, headers={"Authorization": f"Bearer {ADMIN}"})
        c.headers["Authorization"] = f"Bearer {r.json()['api_key']}"
        yield c


async def test_api_trust_policy_issuer_and_permissions(engine, admin):
    spec = identity()["spec"]
    assert (await admin.put("/v1/namespaces/default/cloud-identities/aws-prod", json={"spec": spec})).status_code == 201
    r = await admin.get("/v1/namespaces/default/cloud-identities/aws-prod/trust-policy")
    assert r.status_code == 200, r.text
    body = r.json()
    (statement,) = body["policy"]["Statement"]
    assert statement["Principal"] == {"Federated": "arn:aws:iam::123456789012:oidc-provider/shoal.test"}
    assert statement["Condition"]["StringEquals"]["shoal.test:sub"] == body["subject"]
    assert "sts:TagSession" in statement["Action"] and body["notes"]  # http issuer: AWS cannot reach it

    spec["trust"] = "ambient"
    engine.settings.aws_deployment_role_arn = "arn:aws:iam::999999999999:role/shoal"
    await admin.put("/v1/namespaces/default/cloud-identities/aws-prod", json={"spec": spec})
    (statement,) = (await admin.get("/v1/namespaces/default/cloud-identities/aws-prod/trust-policy")).json()["policy"]["Statement"]
    assert statement["Principal"] == {"AWS": "arn:aws:iam::999999999999:role/shoal"}
    assert statement["Condition"]["StringEquals"]["sts:ExternalId"].endswith(":cloud-identity:aws-prod")

    # editors can see identities but not register roles or change grants
    sa = (await admin.put("/v1/service-accounts/dev", json={})).json()
    await admin.post("/v1/role-bindings", json={"role": "editor", "subject_kind": "user", "subject_id": sa["id"], "namespace": None})
    token = (await admin.post("/v1/api-keys", json={"name": "k", "service_account": "dev"})).json()["token"]
    editor = {"Authorization": f"Bearer {token}"}
    assert (await admin.get("/v1/namespaces/default/cloud-identities/aws-prod", headers=editor)).status_code == 200
    assert (await admin.put("/v1/namespaces/default/cloud-identities/mine", json={"spec": spec}, headers=editor)).status_code == 403
    assert (await admin.delete("/v1/namespaces/default/cloud-identities/aws-prod", headers=editor)).status_code == 403

    # the issuer documents are public, and its tokens verify against the published JWKS
    discovery = (await admin.get("/.well-known/openid-configuration", headers={"Authorization": ""})).json()
    assert discovery["issuer"] == "http://shoal.test" and discovery["id_token_signing_alg_values_supported"] == ["ES256"]
    jwks = (await admin.get("/.well-known/jwks.json", headers={"Authorization": ""})).json()
    token = engine.workload.issue("sub-1", "sts.amazonaws.com")
    key = jwt.PyJWK(jwks["keys"][0]).key
    assert jwt.decode(token, key, algorithms=["ES256"], audience="sts.amazonaws.com")["sub"] == "sub-1"


async def test_trust_policy_can_bind_the_role_to_the_namespace(engine, admin):
    spec = identity()["spec"]
    await admin.put("/v1/namespaces/default/cloud-identities/aws-prod", json={"spec": spec})
    body = (await admin.get("/v1/namespaces/default/cloud-identities/aws-prod/trust-policy?scope=namespace")).json()
    (statement,) = body["policy"]["Statement"]
    cond = statement["Condition"]
    assert cond["StringLike"]["shoal.test:sub"].endswith(":namespace:default:cloud-identity:*")
    assert cond["StringEquals"] == {"shoal.test:aud": "sts.amazonaws.com", "aws:RequestTag/shoal:namespace": "default"}
    assert body["scope"] == "namespace" and body["oidc_provider_arn"].endswith(":oidc-provider/shoal.test")
    assert any("create-open-id-connect-provider" in s["command"] for s in body["steps"])

    # ambient: ExternalId pattern plus the run's namespace tag
    spec["trust"] = "ambient"
    body = (await admin.post("/v1/namespaces/default/cloud-identities/draft/trust-policy?scope=namespace",
                             json={"spec": spec})).json()  # preview of an unsaved draft
    cond = body["policy"]["Statement"][0]["Condition"]
    assert cond["StringLike"]["sts:ExternalId"].endswith(":namespace:default:cloud-identity:*")
    assert cond["StringEquals"]["aws:RequestTag/shoal:namespace"] == "default"
    assert (await admin.get("/v1/namespaces/default/cloud-identities/draft")).status_code == 404

    # a shared identity cannot be bound to one team this way, and says so
    await admin.put("/v1/namespaces/shared/cloud-identities/org", json={"spec": identity()["spec"]})
    body = (await admin.get("/v1/namespaces/shared/cloud-identities/org/trust-policy?scope=namespace")).json()
    assert "aws:RequestTag/shoal:namespace" not in str(body["policy"]) and body["notes"]
    assert (await admin.get("/v1/namespaces/shared/cloud-identities/org/trust-policy?scope=team")).status_code == 422


async def test_test_button_assumes_the_role_and_explains_failures(engine, admin):
    fake = FakeSTS()
    engine.http_transport = httpx.MockTransport(fake)
    await admin.put("/v1/namespaces/default/cloud-identities/aws-prod", json={"spec": identity()["spec"]})
    r = await admin.post("/v1/namespaces/default/cloud-identities/aws-prod/test")
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    assert r.json()["assumed_role_arn"].startswith("arn:aws:sts::123456789012:assumed-role/")
    assert SECRET not in r.text and TOKEN not in r.text
    tags = jwt.decode(FakeSTS.form(fake.requests[-1])["WebIdentityToken"], options={"verify_signature": False})
    assert tags["https://aws.amazon.com/tags"]["principal_tags"]["shoal:workflow"] == ["shoal-test"]

    engine.http_transport = httpx.MockTransport(FakeSTS(error="InvalidIdentityToken"))
    body = (await admin.post("/v1/namespaces/default/cloud-identities/aws-prod/test")).json()
    assert body["ok"] is False and body["code"] == "InvalidIdentityToken" and "OIDC provider" in body["hint"]
    tenant = (await engine.store.get_tenant_by_slug("acme")).id
    outcomes = [e.outcome for e in await engine.store.list_audit(tenant, action="cloud-identity.tested")]
    assert sorted(outcomes) == ["failure", "success"]
