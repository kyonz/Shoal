"""Test kit for integration authors.

* :class:`MockHTTP` - mock the provider's API (route templates, recorded calls);
* :class:`IntegrationHarness` - call actions, queries and webhook handlers with no database;
* :func:`engine_harness` - a real throwaway engine, for trigger and workflow tests;
* :func:`signed_webhook` / :func:`github_webhook` - signed deliveries;
* :class:`IntegrationContract` / :func:`check_integration` / :func:`check_remote_service` -
  the contract every integration (Python or remote) should pass.
"""

from awm.sdk.testing.contract import (
    IntegrationContract,
    check_config,
    check_integration,
    check_manifest,
    check_remote_service,
)
from awm.sdk.testing.harness import EngineHarness, IntegrationHarness, engine_harness
from awm.sdk.testing.http import MockHTTP, RecordedCall, Route, UnmatchedRequest
from awm.sdk.testing.webhooks import github_webhook, signed_webhook, webhook_body

__all__ = [
    "EngineHarness",
    "IntegrationContract",
    "IntegrationHarness",
    "MockHTTP",
    "RecordedCall",
    "Route",
    "UnmatchedRequest",
    "check_config",
    "check_integration",
    "check_manifest",
    "check_remote_service",
    "engine_harness",
    "github_webhook",
    "signed_webhook",
    "webhook_body",
]
