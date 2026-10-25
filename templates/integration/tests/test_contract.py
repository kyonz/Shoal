"""The AWM integration contract: schemas, naming, secrets and webhook signing."""

from awm.sdk.testing import IntegrationContract, signed_webhook

from awm_starter import StarterIntegration


class TestStarterContract(IntegrationContract):
    integration = StarterIntegration
    sample_config = {"api_token": "tok_test", "webhook_secret": "whsec_test"}
    sample_webhook = signed_webhook(
        "whsec_test",
        {"action": "created", "ticket": {"id": "OPS-1", "status": "open"}},
        header="X-Starter-Signature",
    )
