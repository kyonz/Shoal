"""The Shoal integrations SDK.

Everything needed to build an integration (triggers via webhook events,
actions, and metadata resources) against the same public contract the
built-in GitHub and Jira integrations use::

    from shoal.sdk import BaseModel, ConnectionContext, Field, Integration, SecretStr, action

    class LinearConnection(BaseModel):
        api_key: SecretStr

    class CreateIssue(BaseModel):
        team_id: str
        title: str

    class LinearIntegration(Integration):
        key = "linear"
        title = "Linear"
        connection_model = LinearConnection

        @action(description="Create an issue.", input=CreateIssue)
        async def create_issue(self, ctx: ConnectionContext, params: CreateIssue) -> dict:
            ...

Ship it as a package with an ``shoal.integrations`` entry point, or serve it
over HTTP with :mod:`shoal.sdk.server`. Test it with :mod:`shoal.sdk.testing`.

Skill sources (marketplaces that skills are imported from) use the same
pattern: subclass :class:`SkillSource`, implement ``list`` (and optionally
``search``) and ``fetch``, and ship it with a ``shoal.skill_sources`` entry
point or serve it with :mod:`shoal.sdk.skill_server`.
"""

from pydantic import BaseModel, Field, SecretStr

from shoal.integrations.base import (
    ActionDef,
    ConnectionContext,
    EventDef,
    Integration,
    IntegrationError,
    IntegrationEvent,
    Page,
    ResourceDef,
    action,
    raise_for_status,
    resource,
)
from shoal.integrations.remote import PROTOCOL, RemoteManifest, register_remote_integration
from shoal.sdk.helpers import (
    actor,
    decode_cursor,
    encode_cursor,
    hmac_sha256,
    json_body,
    verify_hmac_sha256,
    verify_token,
)
from shoal.sdk.manifest import manifest_for
from shoal.skills.base import (
    SkillBundle,
    SkillPage,
    SkillSource,
    SkillSourceContext,
    SkillSourceError,
    SkillSummary,
    parse_skill_md,
)

SDK_VERSION = "1.0"

__all__ = [
    "PROTOCOL",
    "SDK_VERSION",
    "ActionDef",
    "BaseModel",
    "ConnectionContext",
    "EventDef",
    "Field",
    "Integration",
    "IntegrationError",
    "IntegrationEvent",
    "Page",
    "RemoteManifest",
    "ResourceDef",
    "SecretStr",
    "SkillBundle",
    "SkillPage",
    "SkillSource",
    "SkillSourceContext",
    "SkillSourceError",
    "SkillSummary",
    "action",
    "actor",
    "decode_cursor",
    "encode_cursor",
    "hmac_sha256",
    "json_body",
    "manifest_for",
    "parse_skill_md",
    "raise_for_status",
    "register_remote_integration",
    "resource",
    "verify_hmac_sha256",
    "verify_token",
]
