"""Run from the demo folder: python -m samples.test_dedicated_service_identity --use-azd."""

import argparse
import os
from pathlib import Path

from azure.identity import AzureCliCredential, AzureDeveloperCliCredential

from litellm_demo.control import load_json, save_receipt
from litellm_demo.gateway_keys import read_key
from litellm_demo.scenario_checks import ScenarioChecks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", default=".local/apim-litellm/deployment.json")
    parser.add_argument("--output", default=".local/apim-litellm/dedicated-application/scenario-tests.json")
    parser.add_argument("--use-azd", action="store_true")
    args = parser.parse_args()
    save_receipt(args.output, {"scenario": "Dedicated LiteLLM service identity", "status": "running"})
    succeeded = False
    try:
        deployment_file = Path(args.deployment)
        deployment = load_json(deployment_file)
        profiles = load_json(deployment_file.with_name("terraform-outputs.json"))["identity_scenarios"]["value"]
        # The deployed identifier stays stable; it is NOT consumer-app passthrough.
        expected = profiles["dedicated-application"]
        os.environ["KUBECONFIG"] = str(deployment_file.with_name("kubeconfig").resolve())
        key = read_key("dedicated-application", "application")
        credential_type = AzureDeveloperCliCredential if args.use_azd else AzureCliCredential
        with credential_type(tenant_id=deployment["tenant_id"], process_timeout=180) as identity:
            token = identity.get_token(deployment["apim"]["caller_audience"].rstrip("/") + "/.default").token
        endpoint = deployment["apim"]["gateway_url"].rstrip("/") + "/ai-dedicated-application"
        print("Scenario: dedicated LiteLLM service identity. Backend sees the dedicated pod identity, not the consumer app.")
        with ScenarioChecks(endpoint, token, key) as checks:
            checks.model("DEDICATED_SERVICE_MODEL_OK")
            checks.mcp(expected_client_id=expected["client_id"],
                       expected_principal_ids=[expected["principal_id"]], delegated=False)
            checks.responses("prompt-agent-responses", "gateway-declarative", "DEDICATED_SERVICE_PROMPT_RESPONSES_OK")
            checks.responses("hosted-agent-responses", "gateway-hosted-dual", "DEDICATED_SERVICE_HOSTED_RESPONSES_OK")
            checks.hosted_private_mcp()
            checks.a2a_job("declarative", "DEDICATED_SERVICE_PROMPT_A2A_OK")
            checks.a2a_job("hosted", "DEDICATED_SERVICE_HOSTED_A2A_OK")
            save_receipt(args.output, {"scenario": "Dedicated LiteLLM service identity",
                                      "status": "passed", "checks": checks.results})
        succeeded = True
        print("All dedicated-service checks PASSED.")
    finally:
        if not succeeded:
            save_receipt(args.output, {"scenario": "Dedicated LiteLLM service identity", "status": "failed"})


if __name__ == "__main__":
    main()
