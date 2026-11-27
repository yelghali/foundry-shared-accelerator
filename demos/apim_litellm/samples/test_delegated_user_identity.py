"""Run from the demo folder: python -m samples.test_delegated_user_identity."""

import argparse
import os
from pathlib import Path

from litellm_demo.control import load_json, save_receipt
from litellm_demo.entra_setup import cached_user_token
from litellm_demo.gateway_keys import read_key
from litellm_demo.scenario_checks import ScenarioChecks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", default=".local/apim-litellm/deployment.json")
    parser.add_argument("--identity", default=".local/apim-litellm/entra-identity-user-consent.json")
    parser.add_argument("--user-token-cache", default=".local/apim-litellm/user-obo/user-token-cache.bin")
    parser.add_argument("--output", default=".local/apim-litellm/user-obo/scenario-tests.json")
    args = parser.parse_args()
    save_receipt(args.output, {"scenario": "Delegated signed-in user identity", "status": "running"})
    succeeded = False
    try:
        deployment_file = Path(args.deployment)
        deployment = load_json(deployment_file)
        identity = load_json(args.identity)
        if identity["tenant_id"] != deployment["tenant_id"]:
            raise ValueError("Deployment and delegated user identity belong to different tenants")
        os.environ["KUBECONFIG"] = str(deployment_file.with_name("kubeconfig").resolve())
        key = read_key("user-obo", "application")
        # No CLI/operator token fallback: the actual permitted user must sign in.
        token = cached_user_token(identity, args.user_token_cache)
        endpoint = deployment["apim"]["gateway_url"].rstrip("/") + "/ai-user-obo"
        print("Scenario: delegated user identity. MCP/agent invocation uses user OBO; direct models still use pod identity.")
        with ScenarioChecks(endpoint, token, key) as checks:
            checks.model("DELEGATED_USER_MODEL_OK")
            checks.mcp(expected_client_id=identity["gateway"]["app_id"],
                       expected_principal_ids=identity["permitted_user_object_ids"], delegated=True)
            checks.responses("prompt-agent-responses", "gateway-declarative", "DELEGATED_USER_PROMPT_RESPONSES_OK")
            checks.responses("hosted-agent-responses", "gateway-hosted-dual", "DELEGATED_USER_HOSTED_RESPONSES_OK")
            checks.hosted_private_mcp()
            checks.a2a_job("declarative", "DELEGATED_USER_PROMPT_A2A_OK")
            checks.a2a_job("hosted", "DELEGATED_USER_HOSTED_A2A_OK")
            save_receipt(args.output, {"scenario": "Delegated signed-in user identity",
                                      "status": "passed", "checks": checks.results})
        succeeded = True
        print("All delegated-user checks PASSED.")
    finally:
        if not succeeded:
            save_receipt(args.output, {"scenario": "Delegated signed-in user identity", "status": "failed"})


if __name__ == "__main__":
    main()
