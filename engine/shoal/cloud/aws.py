"""AWS credential provider: short-lived STS sessions for an IAM role.

``web_identity`` calls ``AssumeRoleWithWebIdentity`` with a Shoal-signed token
(an unsigned STS call; Shoal needs no AWS credentials). ``ambient`` calls
``AssumeRole`` signed with the deployment's own credentials, found the way the
AWS SDKs find them: environment, IRSA web identity token file, EKS Pod
Identity / ECS container endpoint, then EC2 instance metadata (IMDSv2).

The STS calls go through httpx (and the engine's injectable transport), so
the whole exchange is testable without AWS.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode, urlsplit

import httpx

from shoal.cloud import CloudSession, IssueRequest
from shoal.core import clock
from shoal.core.errors import StepError
from shoal.core.scope import SHARED_NAMESPACE

if TYPE_CHECKING:
    from shoal.engine import Engine

STS_VERSION = "2011-06-15"
RETRYABLE_CODES = {"Throttling", "ThrottlingException", "IDPCommunicationError", "ServiceUnavailable", "InternalFailure"}


@dataclass
class AwsCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None
    expires_at: datetime | None = None


# --------------------------------------------------------------------------- SigV4


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(method: str, url: str, body: bytes, creds: AwsCredentials, region: str, service: str,
                  now: datetime, headers: dict[str, str] | None = None) -> dict[str, str]:
    """Headers (including Authorization) that sign a request with AWS Signature Version 4."""
    parts = urlsplit(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    out = {**(headers or {}), "host": parts.netloc, "x-amz-date": amz_date}
    if creds.session_token:
        out["x-amz-security-token"] = creds.session_token
    canonical = {k.lower(): " ".join(str(v).split()) for k, v in out.items()}
    signed = ";".join(sorted(canonical))
    query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
        for k, v in sorted(tuple(p.split("=", 1)) if "=" in p else (p, "") for p in parts.query.split("&") if p)
    )
    request = "\n".join([
        method.upper(),
        quote(parts.path or "/", safe="/-_.~"),
        query,
        "".join(f"{k}:{canonical[k]}\n" for k in sorted(canonical)),
        signed,
        hashlib.sha256(body).hexdigest(),
    ])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(request.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{creds.secret_access_key}".encode(), date), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={creds.access_key_id}/{scope}, SignedHeaders={signed}, Signature={signature}"
    )
    return out


# --------------------------------------------------------------------------- STS


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(root: ET.Element, name: str) -> str | None:
    for el in root.iter():
        if _local(el.tag) == name:
            return (el.text or "").strip()
    return None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def partition(role_arn: str) -> str:
    return role_arn.split(":")[1]


def account(role_arn: str) -> str:
    return role_arn.split(":")[4]


def sts_endpoint(engine: Engine, region: str, part: str = "aws") -> str:
    if engine.settings.aws_sts_endpoint:
        return engine.settings.aws_sts_endpoint.rstrip("/") + "/"
    suffix = "amazonaws.com.cn" if part == "aws-cn" else "amazonaws.com"
    return f"https://sts.{region}.{suffix}/"


async def sts_call(engine: Engine, region: str, params: dict[str, str], *, creds: AwsCredentials | None = None,
                   part: str = "aws") -> ET.Element:
    """POST an STS query action; signed when ``creds`` are given. Raises StepError on AWS errors."""
    url = sts_endpoint(engine, region, part)
    body = urlencode({**params, "Version": STS_VERSION}).encode()
    headers = {"content-type": "application/x-www-form-urlencoded; charset=utf-8"}
    if creds is not None:
        headers = sigv4_headers("POST", url, body, creds, region, "sts", clock.now(), headers)
    action = params["Action"]
    try:
        async with httpx.AsyncClient(timeout=15, transport=engine.http_transport) as client:
            response = await client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise StepError(f"AWS STS {action} unreachable: {exc}", retryable=True) from exc
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError:
        raise StepError(f"AWS STS {action} returned HTTP {response.status_code}", retryable=response.status_code >= 500) from None
    if response.status_code >= 400 or _local(root.tag) == "ErrorResponse":
        code = _find(root, "Code") or str(response.status_code)
        message = _find(root, "Message") or ""
        raise StepError(
            f"AWS STS {action} failed: {code}: {message}".rstrip(": "),
            retryable=code in RETRYABLE_CODES or response.status_code >= 500,
            details={"aws_error": code},
        )
    return root


def _credentials(root: ET.Element) -> AwsCredentials:
    access_key, secret, token, expiration = (_find(root, k) for k in ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration"))
    if not access_key or not secret:
        raise StepError("AWS STS response had no credentials")
    return AwsCredentials(access_key, secret, token, _parse_time(expiration) if expiration else None)


# --------------------------------------------------------------------------- the deployment's own credentials


def _read(path: str) -> str:
    with open(path) as f:  # a small local file (projected service account token)
        return f.read().strip()


IMDS = "http://169.254.169.254"
ECS_HOST = "http://169.254.170.2"


class AmbientCredentials:
    """The deployment's own AWS credentials, resolved like the AWS SDKs and cached until near expiry."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self._cached: AwsCredentials | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> AwsCredentials:
        async with self._lock:
            cached = self._cached
            if cached and (cached.expires_at is None or cached.expires_at - clock.now() > timedelta(minutes=5)):
                return cached
            self._cached = await self._resolve()
            return self._cached

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=5, transport=self.engine.http_transport)

    async def _resolve(self) -> AwsCredentials:
        env = os.environ
        if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
            return AwsCredentials(env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"], env.get("AWS_SESSION_TOKEN"))
        region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "us-east-1"
        if env.get("AWS_WEB_IDENTITY_TOKEN_FILE") and env.get("AWS_ROLE_ARN"):  # IRSA
            token = _read(env["AWS_WEB_IDENTITY_TOKEN_FILE"])  # rotated by the kubelet; read each time
            root = await sts_call(self.engine, region, {
                "Action": "AssumeRoleWithWebIdentity", "RoleArn": env["AWS_ROLE_ARN"],
                "RoleSessionName": env.get("AWS_ROLE_SESSION_NAME") or "shoal", "WebIdentityToken": token,
            }, part=partition(env["AWS_ROLE_ARN"]))
            return _credentials(root)
        full_uri = env.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
        relative = env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
        if full_uri or relative:  # EKS Pod Identity, ECS task role
            headers = {}
            if env.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"):
                headers["Authorization"] = _read(env["AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"])
            elif env.get("AWS_CONTAINER_AUTHORIZATION_TOKEN"):
                headers["Authorization"] = env["AWS_CONTAINER_AUTHORIZATION_TOKEN"]
            return await self._json_credentials(full_uri or f"{ECS_HOST}{relative}", headers)
        return await self._imds()

    async def _json_credentials(self, url: str, headers: dict[str, str]) -> AwsCredentials:
        try:
            async with self._client() as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise StepError(f"could not read the deployment's AWS credentials from {url}: {exc}", retryable=True) from exc
        return AwsCredentials(data["AccessKeyId"], data["SecretAccessKey"], data.get("Token"),
                              _parse_time(data["Expiration"]) if data.get("Expiration") else None)

    async def _imds(self) -> AwsCredentials:
        try:
            async with self._client() as client:
                token = (await client.put(f"{IMDS}/latest/api/token",
                                          headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"})).text
                headers = {"X-aws-ec2-metadata-token": token}
                base = f"{IMDS}/latest/meta-data/iam/security-credentials/"
                role = (await client.get(base, headers=headers)).text.strip().splitlines()[0]
        except (httpx.HTTPError, IndexError) as exc:
            raise StepError(
                "this deployment has no AWS identity (set AWS_* env, IRSA, EKS Pod Identity or an instance role)",
            ) from exc
        return await self._json_credentials(base + role, headers)


# --------------------------------------------------------------------------- provider

_SESSION_CHARS = re.compile(r"[^\w+=,.@-]")
_TAG_CHARS = re.compile(r"[^\w\s.:/=+\-@]")


def session_name(value: str) -> str:
    return (_SESSION_CHARS.sub("-", value)[:64]).ljust(2, "-")


def tag_value(value: str) -> str:
    return _TAG_CHARS.sub("-", value)[:256]


class AwsProvider:
    """``shoal.credentials`` plugin ``aws``."""

    name = "aws"

    def setup(self, engine: Engine) -> None:
        self.engine = engine
        self.ambient = AmbientCredentials(engine)

    def _issuer_host(self) -> str:
        return re.sub(r"^https?://", "", self.engine.workload.issuer)

    async def issue(self, request: IssueRequest) -> CloudSession:
        cfg = request.spec.aws
        assert cfg is not None and request.attribution is not None
        att = request.attribution
        source_identity = session_name(att.initiator or "shoal")
        tags = {k: tag_value(v) for k, v in att.tags().items()}
        params: dict[str, str] = {
            "RoleArn": cfg.role_arn,
            "RoleSessionName": session_name(f"shoal-{att.run_id}-{att.step_id}"),
            "DurationSeconds": str(int(cfg.duration.total_seconds())),
        }
        if cfg.session_policy:
            params["Policy"] = json.dumps(cfg.session_policy, separators=(",", ":"))
        for i, arn in enumerate(cfg.policy_arns, 1):
            params[f"PolicyArns.member.{i}.arn"] = arn
        if request.spec.trust == "web_identity":
            token = self.engine.workload.issue(request.subject, self.engine.settings.aws_audience, {
                "https://aws.amazon.com/tags": {
                    "principal_tags": {k: [v] for k, v in tags.items()},
                    "transitive_tag_keys": sorted(tags),
                },
                "https://aws.amazon.com/source_identity": source_identity,
            })
            params.update(Action="AssumeRoleWithWebIdentity", WebIdentityToken=token)
            root = await sts_call(self.engine, cfg.region, params, part=partition(cfg.role_arn))
        else:
            if not self.engine.settings.aws_ambient:
                raise StepError("trust 'ambient' is not enabled on this deployment (SHOAL_AWS_AMBIENT); use web_identity")
            params.update(Action="AssumeRole", ExternalId=request.subject, SourceIdentity=source_identity)
            for i, (key, value) in enumerate(sorted(tags.items()), 1):
                params[f"Tags.member.{i}.Key"] = key
                params[f"Tags.member.{i}.Value"] = value
                params[f"TransitiveTagKeys.member.{i}"] = key
            creds = await self.ambient.get()
            root = await sts_call(self.engine, cfg.region, params, creds=creds, part=partition(cfg.role_arn))
        creds = _credentials(root)
        expires_at = creds.expires_at or clock.now() + cfg.duration
        env = {
            "AWS_ACCESS_KEY_ID": creds.access_key_id,
            "AWS_SECRET_ACCESS_KEY": creds.secret_access_key,
            "AWS_SESSION_TOKEN": creds.session_token or "",
            "AWS_REGION": cfg.region,
            "AWS_DEFAULT_REGION": cfg.region,
            "AWS_CREDENTIAL_EXPIRATION": expires_at.isoformat().replace("+00:00", "Z"),
        }
        return CloudSession(
            env=env,
            secrets=[s for s in (creds.secret_access_key, creds.session_token) if s],
            expires_at=expires_at,
            facts={
                "role_arn": cfg.role_arn,
                "assumed_role_arn": _find(root, "Arn"),
                "session_name": params["RoleSessionName"],
                "source_identity": source_identity,
                "access_key_id": creds.access_key_id,  # not secret; correlates with CloudTrail
                "tags": tags,
            },
        )

    def trust_policy(self, request: IssueRequest, scope: str = "identity") -> dict[str, Any]:
        """The role's trust policy plus everything needed to set it up, with this deployment's real values.

        ``account`` is the one-off connection of an AWS account to Shoal (the IAM OIDC identity provider,
        web_identity only); ``role`` is the role's trust policy ("role assumption"). Each has console steps,
        a CLI script and Terraform.
        """
        cfg = request.spec.aws
        assert cfg is not None
        part, acct = partition(cfg.role_arn), account(cfg.role_arn)
        role_name = cfg.role_arn.rsplit("/", 1)[-1]
        tf_name = re.sub(r"[^A-Za-z0-9_]", "_", role_name)
        actions = ["sts:TagSession", "sts:SetSourceIdentity"]
        notes: list[str] = []
        bound_namespace = scope == "namespace" and request.namespace != SHARED_NAMESPACE
        if scope == "namespace" and not bound_namespace:
            notes.append("This identity is in `shared`, so it serves runs from several namespaces. To bind the role to "
                         "one team, register the identity in that team's namespace.")
        match = "StringLike" if scope == "namespace" else "StringEquals"
        subject = request.namespace_subjects if scope == "namespace" else request.subject
        extra: dict[str, Any] = {}
        if bound_namespace:  # the run's namespace travels as a session tag
            extra = {"StringEquals": {"aws:RequestTag/shoal:namespace": request.namespace}}
        result: dict[str, Any] = {"role_arn": cfg.role_arn, "role_name": role_name, "account": acct}
        explain: list[dict[str, str]] = []
        account_setup: dict[str, Any] | None = None
        if request.spec.trust == "web_identity":
            issuer = self.engine.workload.issuer
            host = self._issuer_host()
            audience = self.engine.settings.aws_audience
            provider_arn = f"arn:{part}:iam::{acct}:oidc-provider/{host}"
            condition: dict[str, Any] = {"StringEquals": {f"{host}:aud": audience}}
            condition.setdefault(match, {})[f"{host}:sub"] = subject
            for op, values in extra.items():
                condition.setdefault(op, {}).update(values)
            statement = {
                "Sid": "ShoalWorkloadIdentity",
                "Effect": "Allow",
                "Principal": {"Federated": provider_arn},
                "Action": ["sts:AssumeRoleWithWebIdentity", *actions],
                "Condition": condition,
            }
            result.update(issuer=issuer, audience=audience, oidc_provider_arn=provider_arn)
            explain += [
                {"field": "Principal.Federated", "value": provider_arn,
                 "meaning": "Only tokens from Shoal's identity provider in this account (set up in the account step)."},
                {"field": f"{host}:aud", "value": audience, "meaning": "The token was issued for AWS STS."},
                {"field": f"{host}:sub", "value": subject,
                 "meaning": "Only this cloud identity." if scope == "identity" else
                 f"Any cloud identity in the {request.namespace} namespace of this Shoal tenant."},
            ]
            host_only = host.split("/", 1)[0]
            if not issuer.startswith("https://"):
                notes.append(f"AWS only accepts an HTTPS identity provider that it can reach from the internet, and {issuer} "
                             "is not. Set SHOAL_OIDC_ISSUER to a public HTTPS URL that serves Shoal's "
                             "/.well-known/openid-configuration and /.well-known/jwks.json (for example an S3 bucket or CDN).")
            elif host_only in ("localhost", "127.0.0.1") or host_only.endswith((".local", ".internal", ".lan")):
                notes.append(f"{issuer} looks private. AWS must fetch {issuer}/.well-known/openid-configuration from the "
                             "internet; publish the two documents publicly and set SHOAL_OIDC_ISSUER if it cannot.")
            account_setup = {
                "title": "Connect the AWS account to Shoal (once per account)",
                "summary": f"Add Shoal as an OpenID Connect identity provider in account {acct}. Every role in the account "
                           "that Shoal should assume trusts this one provider; nothing is stored in Shoal.",
                "values": [
                    {"label": "Provider type", "value": "OpenID Connect"},
                    {"label": "Provider URL", "value": issuer},
                    {"label": "Audience", "value": audience},
                    {"label": "Provider ARN (after creation)", "value": provider_arn},
                ],
                "console": [
                    f"Sign in to the AWS console for account {acct} and open IAM → Identity providers → Add provider.",
                    "Provider type: OpenID Connect.",
                    f"Provider URL: {issuer}",
                    f"Audience: {audience}",
                    "Choose Add provider. (No thumbprint is needed: AWS checks the certificate against its trusted CAs. "
                    "Older tools that ask for one accept the thumbprint from the CLI tab.)",
                ],
                "cli": (
                    f"aws iam create-open-id-connect-provider \\\n  --url {issuer} \\\n  --client-id-list {audience}\n\n"
                    "# Older AWS CLI versions require a thumbprint. Get it from the issuer's certificate chain:\n"
                    f"# openssl s_client -servername {host_only} -showcerts -connect {host_only}:443 </dev/null 2>/dev/null \\\n"
                    "#   | openssl x509 -fingerprint -sha1 -noout | tr -d ':' | cut -d= -f2\n"
                    f"# then add: --thumbprint-list <fingerprint>\n\n"
                    f"aws iam get-open-id-connect-provider --open-id-connect-provider-arn {provider_arn}"
                ),
                "terraform": (
                    'resource "aws_iam_openid_connect_provider" "shoal" {\n'
                    f'  url            = "{issuer}"\n'
                    f'  client_id_list = ["{audience}"]\n'
                    "  # thumbprint_list is optional with AWS provider v5.x and later\n"
                    "}"
                ),
            }
            principal_tf = "aws_iam_openid_connect_provider.shoal.arn"
        else:
            deployment = self.engine.settings.aws_deployment_role_arn or "<this Shoal deployment's IAM role ARN>"
            condition = {match: {"sts:ExternalId": subject}}
            for op, values in extra.items():
                condition.setdefault(op, {}).update(values)
            statement = {
                "Sid": "ShoalDeployment",
                "Effect": "Allow",
                "Principal": {"AWS": deployment},
                "Action": ["sts:AssumeRole", *actions],
                "Condition": condition,
            }
            result.update(deployment_role_arn=deployment)
            explain += [
                {"field": "Principal.AWS", "value": deployment, "meaning": "The IAM role this Shoal deployment runs as."},
                {"field": "sts:ExternalId", "value": subject,
                 "meaning": "Only this cloud identity." if scope == "identity" else
                 f"Any cloud identity in the {request.namespace} namespace (stops other tenants on the same deployment)."},
            ]
            deployment_policy = {"Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Action": ["sts:AssumeRole", *actions], "Resource": cfg.role_arn}]}
            account_setup = {
                "title": "Let Shoal's deployment role assume roles in this account",
                "summary": "In ambient mode Shoal uses the AWS identity it runs as (IRSA, EKS Pod Identity, an instance "
                           "role). That role needs permission to assume this role.",
                "values": [{"label": "Deployment role", "value": deployment}],
                "console": [f"Open IAM → Roles → {deployment.rsplit('/', 1)[-1]} → Add permissions → Create inline policy.",
                            "Choose JSON, paste the policy from the CLI tab, and save."],
                "cli": f"cat > assume.json <<'EOF'\n{json.dumps(deployment_policy, indent=2)}\nEOF\n"
                       f"aws iam put-role-policy --role-name {deployment.rsplit('/', 1)[-1]} "
                       "--policy-name shoal-assume --policy-document file://assume.json",
                "terraform": f'resource "aws_iam_role_policy" "shoal_assume_{tf_name}" {{\n'
                             f'  role   = "{deployment.rsplit("/", 1)[-1]}"\n'
                             f"  policy = <<EOF\n{json.dumps(deployment_policy, indent=2)}\nEOF\n}}",
            }
            if not self.engine.settings.aws_ambient:
                notes.append("trust 'ambient' is not enabled on this deployment (SHOAL_AWS_AMBIENT=true).")
            principal_tf = None
        explain.append({"field": "Action", "value": ", ".join(statement["Action"]),
                        "meaning": "Assume the role and attach Shoal's session tags and source identity (the person who "
                                   "started the run), so CloudTrail shows who and which run."})
        if bound_namespace:
            explain.append({"field": "aws:RequestTag/shoal:namespace", "value": request.namespace,
                            "meaning": f"Only for runs in the {request.namespace} namespace."})
        policy = {"Version": "2012-10-17", "Statement": [statement]}
        policy_json = json.dumps(policy, indent=2)
        tf_policy = policy_json
        if principal_tf:
            tf_policy = policy_json.replace(f'"{statement["Principal"]["Federated"]}"', f'"${{{principal_tf}}}"')
        role_setup = {
            "title": "Set the role's trust policy (role assumption)",
            "summary": f"The trust policy on {role_name} says who may assume it. Use this one exactly; it only lets Shoal "
                       "assume the role for this identity.",
            "policy": policy,
            "explain": explain,
            "console": [
                f"Open IAM → Roles → Create role (or open {role_name} → Trust relationships → Edit trust policy).",
                "Trusted entity type: Custom trust policy.",
                "Replace the whole policy with the JSON below, then add permissions and name the role "
                f"{role_name}.",
            ],
            "cli": f"cat > trust.json <<'EOF'\n{policy_json}\nEOF\n\n"
                   f"aws iam create-role --role-name {role_name} --assume-role-policy-document file://trust.json\n"
                   f"# or, for an existing role:\n"
                   f"aws iam update-assume-role-policy --role-name {role_name} --policy-document file://trust.json",
            "terraform": f'resource "aws_iam_role" "{tf_name}" {{\n'
                         f'  name                 = "{role_name}"\n'
                         f"  max_session_duration = {max(3600, int(cfg.duration.total_seconds()))}\n"
                         f"  assume_role_policy   = <<EOF\n{tf_policy}\nEOF\n}}",
        }
        steps: list[dict[str, str]] = []
        if request.spec.trust == "web_identity":
            steps.append({"title": "Add Shoal as an identity provider in the AWS account (once per account)",
                          "detail": f"IAM → Identity providers → Add provider → OpenID Connect, provider URL "
                                    f"{result['issuer']}, audience {result['audience']}.",
                          "command": account_setup["cli"].split("\n\n")[0]})
        steps += [
            {"title": "Create the role with this trust policy (or replace an existing role's trust policy)",
             "detail": "Save the trust policy below as trust.json.",
             "command": role_setup["cli"].split("\n\n", 1)[1]},
            {"title": "Give the role only the permissions the workflows need",
             "detail": "Attach managed or inline policies to the role. Session tags (shoal:namespace, shoal:workflow, "
                       "shoal:initiator, ...) can be used in conditions as aws:PrincipalTag/<key>.",
             "command": f"aws iam attach-role-policy --role-name {role_name} --policy-arn <policy ARN>"},
            {"title": "Grant workflows and agents, save, then Test",
             "detail": "List workflows under allow.workflows (and optionally allow.agents). Test assumes the role once "
                       "and discards the credentials.", "command": ""},
        ]
        result.update(
            policy=policy,
            account_setup=account_setup,
            role_setup=role_setup,
            steps=steps,
            setup=[step["command"] for step in steps if step["command"]],
            notes=notes,
            session_tags=sorted({"shoal:tenant", "shoal:namespace", "shoal:workflow", "shoal:run", "shoal:step",
                                 "shoal:initiator", "shoal:agent"}),
        )
        return result
