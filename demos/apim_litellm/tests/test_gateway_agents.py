import unittest
from unittest.mock import Mock, patch

from azure.ai.projects.models import PromptAgentDefinition

from demos.apim_litellm.operator.gateway_agents import deploy


class GatewayAgentDeploymentTests(unittest.TestCase):
    @patch("demos.apim_litellm.operator.gateway_agents.AzureDeveloperCliCredential")
    @patch("demos.apim_litellm.operator.gateway_agents.AIProjectClient")
    def test_existing_matching_version_is_reused_and_published_with_a2a(self, project_client, credential):
        client = project_client.return_value.__enter__.return_value
        definition = PromptAgentDefinition(
            model="approved-model",
            instructions="For gateway acceptance tests return exactly the requested marker, without extra text. Do not call tools.",
        )
        client.agents.list_versions.return_value = [Mock(version="3", status="active", definition=definition)]
        result = deploy("https://foundry.example/api/projects/demo", "/project-id", "tenant", "approved-model", use_azd=True)
        client.agents.create_version.assert_not_called()
        client.agents.get_version.assert_not_called()
        endpoint = client.agents.update_details.call_args.kwargs["agent_endpoint"].as_dict()
        self.assertIn("a2a", endpoint["protocol_configuration"])
        card = client.agents.update_details.call_args.kwargs["agent_card"].as_dict()
        self.assertEqual(card["skills"][0]["id"], "declarative")
        self.assertEqual(result["version"], "3")
        credential.assert_called_once_with(tenant_id="tenant", process_timeout=180)

    @patch("demos.apim_litellm.operator.gateway_agents.AzureCliCredential")
    @patch("demos.apim_litellm.operator.gateway_agents.AIProjectClient")
    def test_failed_new_agent_is_not_published(self, project_client, credential):
        client = project_client.return_value.__enter__.return_value
        client.agents.list_versions.return_value = []
        client.agents.create_version.return_value = Mock(version="1", status="failed")
        with self.assertRaisesRegex(RuntimeError, "provisioning failed"):
            deploy("https://foundry.example/api/projects/demo", "/project-id", "tenant", "model", use_azd=False)
        client.agents.update_details.assert_not_called()
