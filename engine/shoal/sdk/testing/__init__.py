"""Test kit for integration authors.

* :class:`MockHTTP` - mock the provider's API (route templates, recorded calls);
* :class:`IntegrationHarness` - call actions, queries and webhook handlers with no database;
* :func:`engine_harness` - a real throwaway engine, for trigger and workflow tests;
* :func:`signed_webhook` / :func:`github_webhook` - signed deliveries;
* :class:`IntegrationContract` / :func:`check_integration` / :func:`check_remote_service` -
  the contract every integration (Python or remote) should pass;
* :func:`check_sandbox_backend`, :func:`check_model_provider`, :func:`check_secret_provider`,
  :func:`check_decision_provider`, :func:`check_eval_sink`, :func:`check_skill_source`, :func:`check_entry_point` - the
  contracts for the other plugin types.
"""

from shoal.sdk.testing.contract import (
    IntegrationContract,
    check_config,
    check_integration,
    check_manifest,
    check_remote_service,
)
from shoal.sdk.testing.harness import EngineHarness, IntegrationHarness, engine_harness
from shoal.sdk.testing.http import MockHTTP, RecordedCall, Route, UnmatchedRequest
from shoal.sdk.testing.plugins import (
    check_decision_provider,
    check_entry_point,
    check_eval_sink,
    check_model_provider,
    check_sandbox_backend,
    check_secret_provider,
    check_skill_source,
)
from shoal.sdk.testing.webhooks import github_webhook, signed_webhook, webhook_body

__all__ = [
    "EngineHarness",
    "IntegrationContract",
    "IntegrationHarness",
    "MockHTTP",
    "RecordedCall",
    "Route",
    "UnmatchedRequest",
    "check_config",
    "check_decision_provider",
    "check_entry_point",
    "check_eval_sink",
    "check_integration",
    "check_manifest",
    "check_model_provider",
    "check_remote_service",
    "check_sandbox_backend",
    "check_secret_provider",
    "check_skill_source",
    "engine_harness",
    "github_webhook",
    "signed_webhook",
    "webhook_body",
]
