import json
import unittest
from unittest.mock import Mock, patch

from accelerator.gateway_keys import bootstrap_database, provision_keys


class GatewayKeyTests(unittest.TestCase):
    def test_existing_database_credentials_are_preserved(self):
        kube = Mock(namespace="gateway")
        kube.secret.return_value = {"master-key": "sk-existing", "postgres-password": "existing", "database-url": "existing-url"}
        bootstrap_database(kube)
        kube.store_secret.assert_not_called()
        kube.run.assert_called_once_with("rollout", "status", "statefulset/litellm-postgres", "--timeout=300s")

    def test_missing_secret_with_retained_pvc_cannot_invent_new_password(self):
        kube = Mock(namespace="gateway")
        kube.secret.return_value = None
        kube.run.side_effect = [json.dumps({"items": []}), "persistentvolumeclaim/data-litellm-postgres-0"]
        with self.assertRaisesRegex(RuntimeError, "restore the original Secret"):
            bootstrap_database(kube)
        kube.store_secret.assert_not_called()

    @patch("accelerator.gateway_keys.private_api")
    def test_virtual_key_rerun_updates_permissions_without_replacing_keys(self, api):
        kube = Mock(namespace="gateway")
        kube.secret.return_value = {"application": "sk-one", "models-only": "sk-two"}
        api.side_effect = [
            [{"server_name": "blob", "server_id": "blob-id"}],
            [{"agent_name": "echo", "agent_id": "hosted-id"},
             {"agent_name": "declarative", "agent_id": "prompt-id"}],
            {}, {},
        ]
        result = provision_keys(kube, "shared-service")
        calls = api.call_args_list
        self.assertEqual([call.args[2] for call in calls[2:]], ["/key/update", "/key/update"])
        self.assertEqual(calls[2].args[3]["object_permission"], {
            "mcp_servers": ["blob-id"], "agents": ["hosted-id", "prompt-id"],
        })
        self.assertEqual(calls[3].args[3]["object_permission"], {"mcp_servers": ["no-mcp-servers"], "agents": []})
        self.assertNotIn("sk-one", json.dumps(result))

    @patch("accelerator.gateway_keys.private_api")
    def test_failed_catalog_cannot_mint_unrestricted_key(self, api):
        api.side_effect = [[], []]
        kube = Mock(namespace="gateway")
        with self.assertRaisesRegex(ValueError, "not ready"):
            provision_keys(kube, "shared-service")
        self.assertEqual(api.call_count, 2)
        kube.store_secret.assert_not_called()
