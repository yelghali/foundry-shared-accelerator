import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from unittest.mock import Mock, patch

from accelerator.entra_setup import (
    GraphClient, IdentityError, ensure_application, ensure_obo_secret,
    grant_consent, main, provision, VAULT_PROXY_SCRIPT, cached_user_token, configure_user_consent,
)


class EntraSetupTests(unittest.TestCase):
    def consent_receipt(self):
        return {
            "tenant_id": "5dc82be3-90ab-4f72-a0f2-b2557ba694e3",
            "gateway": {"sp_id": "00000000-0000-0000-0000-000000000001",
                        "scope": "api://gateway/access_as_user"},
            "mcp": {"sp_id": "00000000-0000-0000-0000-000000000002",
                    "scope": "api://mcp/tools.read"},
            "test_client": {"sp_id": "00000000-0000-0000-0000-000000000003"},
            "foundry": {"sp_id": "00000000-0000-0000-0000-000000000004",
                        "scope": "https://ai.azure.com/user_impersonation"},
            "admin_consent_requested": False, "consent_grant_ids": [],
        }

    def user_consent_fixture(self):
        receipt = self.consent_receipt()
        apps = {}
        for index, name in enumerate(("gateway", "mcp", "test_client"), 1):
            entry = receipt[name]
            entry.update(app_id=f"10000000-0000-0000-0000-{index:012d}",
                         object_id=f"20000000-0000-0000-0000-{index:012d}")
            entry["scope_id"] = f"30000000-0000-0000-0000-{index:012d}"
            scopes = ([] if name == "test_client" else [{
                "id": entry["scope_id"], "value": entry["scope"].rsplit("/", 1)[-1],
                "type": "Admin", "isEnabled": True, "adminConsentDisplayName": "Original",
            }])
            apps[name] = {
                "id": entry["object_id"], "appId": entry["app_id"], "displayName": name,
                "api": {"requestedAccessTokenVersion": 2, "oauth2PermissionScopes": scopes,
                        "knownClientApplications": ["existing-client"]},
            }
        foundry = {"oauth2PermissionScopes": [{
            "id": "foundry-scope", "value": "user_impersonation", "type": "User", "isEnabled": True,
        }]}
        receipt["foundry"]["resource_app_id"] = "40000000-0000-0000-0000-000000000001"
        for client, resource_id, scope_id in (
            ("test_client", receipt["gateway"]["app_id"], receipt["gateway"]["scope_id"]),
            ("gateway", receipt["mcp"]["app_id"], receipt["mcp"]["scope_id"]),
            ("gateway", receipt["foundry"]["resource_app_id"], "foundry-scope"),
        ):
            apps[client].setdefault("requiredResourceAccess", []).append({
                "resourceAppId": resource_id, "resourceAccess": [{"id": scope_id, "type": "Scope"}],
            })
        return receipt, apps, foundry

    def test_user_consent_plan_has_no_authentication_or_writes(self):
        receipt, _, _ = self.user_consent_fixture()
        with patch("accelerator.entra_setup.Path.read_text", return_value=json.dumps(receipt)), \
             patch("accelerator.entra_setup.AzureCliCredential") as credential, \
             patch("accelerator.entra_setup.GraphClient") as graph, \
             patch("accelerator.entra_setup.save_receipt") as save, redirect_stdout(io.StringIO()) as output:
            main(["user-consent", "--identity", "receipt.json"])
        credential.assert_not_called()
        graph.assert_not_called()
        save.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["tenant_policy_changes"])

    def test_user_consent_preserves_ids_and_other_configuration_and_is_idempotent(self):
        receipt, apps, foundry = self.user_consent_fixture()
        original = deepcopy(apps)
        graph = Mock()

        def request(method, path, body=None):
            if path.startswith("/servicePrincipals/"):
                return deepcopy(foundry)
            app = next(app for app in apps.values() if path.split("?")[0].endswith(app["id"]))
            if method == "PATCH":
                app["api"] = deepcopy(body["api"])
                return {}
            return deepcopy(app)

        graph.request.side_effect = request
        configure_user_consent(graph, receipt)
        self.assertEqual(len([c for c in graph.request.call_args_list if c.args[0] == "PATCH"]), 4)
        patches = [c for c in graph.request.call_args_list if c.args[0] == "PATCH"]
        for before, after in zip(patches[::2], patches[1::2]):
            self.assertFalse(before.args[2]["api"]["oauth2PermissionScopes"][0]["isEnabled"])
            scope = after.args[2]["api"]["oauth2PermissionScopes"][0]
            self.assertTrue(scope["isEnabled"])
            self.assertEqual(scope["type"], "User")
            self.assertEqual(scope["id"], before.args[2]["api"]["oauth2PermissionScopes"][0]["id"])
        self.assertEqual(apps["gateway"]["api"]["requestedAccessTokenVersion"], 2)
        self.assertEqual(apps["gateway"]["api"]["knownClientApplications"],
                         ["existing-client", receipt["test_client"]["app_id"]])
        self.assertEqual(original["gateway"]["api"]["oauth2PermissionScopes"][0]["type"], "Admin")
        graph.request.reset_mock()
        configure_user_consent(graph, receipt)
        self.assertFalse(any(c.args[0] != "GET" for c in graph.request.call_args_list))

    def test_user_consent_preflights_external_scope_and_restores_failed_scope_update(self):
        receipt, apps, foundry = self.user_consent_fixture()
        graph = Mock()
        foundry["oauth2PermissionScopes"][0]["type"] = "Admin"
        graph.request.side_effect = [*apps.values(), foundry]
        with self.assertRaisesRegex(ValueError, "administrator is required"):
            configure_user_consent(graph, receipt)
        self.assertFalse(any(c.args[0] == "PATCH" for c in graph.request.call_args_list))
        foundry["oauth2PermissionScopes"][0]["type"] = "User"
        graph.reset_mock()
        graph.request.side_effect = [*apps.values(), foundry, {}, IdentityError(403, "Denied"), {}]
        with self.assertRaises(IdentityError):
            configure_user_consent(graph, receipt)
        graph.request.assert_called_with(
            "PATCH", "/applications/" + apps["mcp"]["id"], {"api": apps["mcp"]["api"]},
        )

    def test_user_consent_apply_records_configuration_not_an_admin_or_user_grant(self):
        receipt, apps, _ = self.user_consent_fixture()
        for app in apps.values():
            for scope in app["api"]["oauth2PermissionScopes"]:
                scope["type"] = "User"
        apps["gateway"]["api"]["knownClientApplications"].append(receipt["test_client"]["app_id"])
        with patch("accelerator.entra_setup.Path.read_text", return_value=json.dumps(receipt)), \
             patch("accelerator.entra_setup.AzureDeveloperCliCredential"), \
             patch("accelerator.entra_setup.GraphClient") as graph, \
             patch("accelerator.entra_setup.configure_user_consent") as configure, \
             patch("accelerator.entra_setup.save_receipt") as save, redirect_stdout(io.StringIO()) as output:
            graph.return_value.request.side_effect = [apps["gateway"], apps["mcp"]]
            main(["user-consent", "--identity", "receipt.json", "--apply", "--use-azd"])
        configure.assert_called_once()
        self.assertIs(configure.call_args.args[0], graph.return_value)
        saved = save.call_args.args[1]
        self.assertEqual(saved["consent_mode"], "user")
        self.assertTrue(saved["user_consent_configured"])
        self.assertFalse(saved["admin_consent_requested"])
        self.assertFalse(json.loads(output.getvalue())["consent_performed"])

    def test_user_consent_login_requests_default_for_combined_permissions(self):
        receipt, _, _ = self.user_consent_fixture()
        receipt.update(consent_mode="user", user_consent_configured=True)
        receipt["gateway"]["audience"] = "api://" + receipt["gateway"]["app_id"]
        with patch("accelerator.entra_setup.Path.read_text", return_value=json.dumps(receipt)), \
             patch("msal.PublicClientApplication") as public_client, redirect_stdout(io.StringIO()) as output:
            app = public_client.return_value
            app.initiate_device_flow.return_value = {"user_code": "code", "message": "Complete user consent"}
            app.acquire_token_by_device_flow.return_value = {"access_token": "must-not-be-printed"}
            main(["login", "--identity", "receipt.json"])
        app.initiate_device_flow.assert_called_once_with(scopes=[receipt["gateway"]["audience"] + "/.default"])
        self.assertNotIn("must-not-be-printed", output.getvalue())

    def test_consent_plan_never_authenticates_or_writes(self):
        with patch("accelerator.entra_setup.Path.read_text", return_value=json.dumps(self.consent_receipt())), \
             patch("accelerator.entra_setup.AzureCliCredential") as cli, \
             patch("accelerator.entra_setup.AzureDeveloperCliCredential") as azd, \
             patch("accelerator.entra_setup.GraphClient") as graph, \
             patch("accelerator.entra_setup.save_receipt") as save, \
             redirect_stdout(io.StringIO()) as output:
            main(["consent", "--identity", "receipt.json", "--include-azure-cli"])
        cli.assert_not_called()
        azd.assert_not_called()
        graph.assert_not_called()
        save.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["writes_performed"])

    def test_consent_apply_is_graph_only_and_uses_explicit_receipt_tenant(self):
        receipt = self.consent_receipt()
        with patch("accelerator.entra_setup.Path.read_text", return_value=json.dumps(receipt)), \
             patch("accelerator.entra_setup.AzureDeveloperCliCredential") as azd, \
             patch("accelerator.entra_setup.GraphClient") as graph, \
             patch("accelerator.entra_setup.grant_consent", side_effect=["one", "two", "three", "cli"]) as grant, \
             patch("accelerator.entra_setup.ensure_application") as registration, \
             patch("accelerator.entra_setup.ensure_obo_secret") as secret, \
             patch("accelerator.entra_setup.subprocess.run") as subprocess_run, \
             patch("accelerator.entra_setup.save_receipt") as save, redirect_stdout(io.StringIO()):
            graph.return_value.list.return_value = [{"id": "00000000-0000-0000-0000-000000000005"}]
            main(["consent", "--identity", "receipt.json", "--apply", "--include-azure-cli", "--use-azd"])
        azd.assert_called_once_with(tenant_id=receipt["tenant_id"], process_timeout=120)
        graph.assert_called_once_with(azd.return_value, "")
        self.assertEqual(grant.call_args_list[0].args[1:], (
            receipt["gateway"]["sp_id"], receipt["foundry"]["sp_id"], "user_impersonation"))
        self.assertEqual(grant.call_args_list[1].args[1:], (
            receipt["gateway"]["sp_id"], receipt["mcp"]["sp_id"], "tools.read"))
        self.assertEqual(grant.call_args_list[2].args[1:], (
            receipt["test_client"]["sp_id"], receipt["gateway"]["sp_id"], "access_as_user"))
        graph.return_value.request.assert_not_called()
        registration.assert_not_called()
        secret.assert_not_called()
        subprocess_run.assert_not_called()
        self.assertEqual(save.call_args.args[0], "receipt.json")
        self.assertTrue(save.call_args.args[1]["admin_consent_requested"])
        self.assertEqual(save.call_args.args[1]["consent_grant_ids"], ["one", "two", "three", "cli"])

    def test_failed_consent_does_not_replace_receipt(self):
        with patch("accelerator.entra_setup.Path.read_text", return_value=json.dumps(self.consent_receipt())), \
             patch("accelerator.entra_setup.AzureCliCredential"), \
             patch("accelerator.entra_setup.GraphClient"), \
             patch("accelerator.entra_setup.grant_consent", side_effect=["first", IdentityError(403, "Denied")]), \
             patch("accelerator.entra_setup.save_receipt") as save:
            with self.assertRaises(IdentityError):
                main(["consent", "--identity", "receipt.json", "--apply"])
        save.assert_not_called()

    def test_consent_rejects_non_uuid_principal_before_authentication(self):
        receipt = self.consent_receipt()
        receipt["mcp"]["sp_id"] = "unsafe'principal"
        with patch("accelerator.entra_setup.Path.read_text", return_value=json.dumps(receipt)), \
             patch("accelerator.entra_setup.AzureCliCredential") as credential:
            with self.assertRaises(ValueError):
                main(["consent", "--identity", "receipt.json", "--apply"])
        credential.assert_not_called()

    def test_cached_user_token_uses_exact_scope_account_and_encrypted_cache_without_logging(self):
        identity = {"tenant_id": "tenant", "test_client": {"app_id": "public-client"},
                    "gateway": {"scope": "api://gateway/access_as_user"}}
        with patch("accelerator.entra_setup.Path.is_file", return_value=True), \
             patch("accelerator.entra_setup.encrypted_token_cache", return_value="encrypted-cache") as cache, \
             patch("msal.PublicClientApplication") as public_client, \
             redirect_stdout(io.StringIO()) as output:
            app = public_client.return_value
            app.get_accounts.return_value = [{"home_account_id": "unique-account"}]
            app.acquire_token_silent.return_value = {"access_token": "private-user-token"}
            self.assertEqual(cached_user_token(identity, "protected-cache"), "private-user-token")
        cache.assert_called_once_with("protected-cache")
        public_client.assert_called_once_with(
            "public-client", authority="https://login.microsoftonline.com/tenant", token_cache="encrypted-cache")
        app.acquire_token_silent.assert_called_once_with(
            ["api://gateway/access_as_user"], account={"home_account_id": "unique-account"})
        self.assertEqual(output.getvalue(), "")
        app.initiate_device_flow.assert_not_called()

    def test_cached_user_token_rejects_missing_or_ambiguous_accounts(self):
        identity = {"tenant_id": "tenant", "test_client": {"app_id": "public-client"},
                    "gateway": {"scope": "api://gateway/access_as_user"}}
        for accounts in ([], [{"id": "one"}, {"id": "two"}]):
            with self.subTest(accounts=accounts), \
                 patch("accelerator.entra_setup.Path.is_file", return_value=True), \
                 patch("accelerator.entra_setup.encrypted_token_cache"), \
                 patch("msal.PublicClientApplication") as public_client:
                public_client.return_value.get_accounts.return_value = accounts
                with self.assertRaisesRegex(RuntimeError, "exactly one account"):
                    cached_user_token(identity, "protected-cache")
                public_client.return_value.acquire_token_silent.assert_not_called()
        with patch("accelerator.entra_setup.Path.is_file", return_value=False), \
             patch("accelerator.entra_setup.encrypted_token_cache") as cache:
            with self.assertRaisesRegex(RuntimeError, "cache is missing.*Run login"):
                cached_user_token(identity, "missing")
            cache.assert_not_called()

    def test_cached_user_token_errors_are_actionable_and_redacted(self):
        identity = {"tenant_id": "tenant", "test_client": {"app_id": "public-client"},
                    "gateway": {"scope": "api://gateway/access_as_user"}}
        for result in (None, {"error_description": "private-refresh-token"}, {"access_token": ""}):
            with self.subTest(result=result), \
                 patch("accelerator.entra_setup.Path.is_file", return_value=True), \
                 patch("accelerator.entra_setup.encrypted_token_cache"), \
                 patch("msal.PublicClientApplication") as public_client:
                app = public_client.return_value
                app.get_accounts.return_value = [{"id": "one"}]
                app.acquire_token_silent.return_value = result
                with self.assertRaisesRegex(RuntimeError, "Run login") as caught:
                    cached_user_token(identity, "protected-cache")
                self.assertNotIn("private-refresh-token", str(caught.exception))
        with patch("accelerator.entra_setup.Path.is_file", return_value=True), \
             patch("accelerator.entra_setup.encrypted_token_cache"), \
             patch("msal.PublicClientApplication", side_effect=RuntimeError("private-refresh-token")):
            with self.assertRaisesRegex(RuntimeError, "Run login") as caught:
                cached_user_token(identity, "protected-cache")
            self.assertNotIn("private-refresh-token", str(caught.exception))

    def test_consent_uses_sp_object_ids_and_scope_values(self):
        graph = Mock()
        graph.list.return_value = []
        graph.request.return_value = {"id": "grant-object"}
        self.assertEqual(grant_consent(graph, "client-sp", "resource-sp", "tools.read"), "grant-object")
        body = graph.request.call_args.args[2]
        self.assertEqual(body, {
            "clientId": "client-sp", "resourceId": "resource-sp",
            "consentType": "AllPrincipals", "principalId": None, "scope": "tools.read",
        })

    def test_existing_consent_is_merged_without_duplicate_creation(self):
        graph = Mock()
        graph.list.return_value = [{"id": "existing", "consentType": "AllPrincipals", "scope": "other"}]
        self.assertEqual(grant_consent(graph, "client", "resource", "tools.read"), "existing")
        graph.request.assert_called_once_with(
            "PATCH", "/oauth2PermissionGrants/existing", {"scope": "other tools.read"})

    def test_rerun_reuses_app_scope_and_service_principal(self):
        graph = Mock()
        graph.list.side_effect = [[{
            "id": "app-object", "appId": "app-id", "identifierUris": ["api://app-id"],
            "api": {"oauth2PermissionScopes": [{"id": "scope-guid", "value": "access_as_user", "isEnabled": True}]},
        }], [{"id": "sp-object"}]]
        _, receipt = ensure_application(graph, "gateway", "access_as_user")
        self.assertEqual(receipt["scope_id"], "scope-guid")
        self.assertEqual(receipt["sp_id"], "sp-object")
        self.assertFalse(any(c.args[0] == "POST" for c in graph.request.call_args_list))

    def test_secret_reuse_never_adds_password_or_returns_value(self):
        graph = Mock(vault_url="https://vault.vault.azure.net")
        graph.request.return_value = {
            "id": "https://vault/secrets/name/version", "value": "never-persist",
            "tags": {"entra-app-id": "app"},
        }
        receipt = ensure_obo_secret(graph, {"app_id": "app", "object_id": "object"})
        self.assertNotIn("never-persist", receipt)
        self.assertEqual(graph.request.call_count, 1)

    def test_secret_refuses_cross_application_reuse_even_for_rotation(self):
        graph = Mock(vault_url="https://vault.vault.azure.net")
        graph.request.return_value = {"tags": {"entra-app-id": "other"}}
        with self.assertRaisesRegex(ValueError, "another application"):
            ensure_obo_secret(graph, {"app_id": "app"}, rotate=True)

    def test_rotation_puts_secret_in_vault_only(self):
        graph = Mock(vault_url="https://vault.vault.azure.net")
        graph.request.side_effect = [
            {"tags": {"entra-app-id": "app"}},
            {"secretText": "secret-text", "keyId": "key-id"},
            {"id": "https://vault/secrets/name/version"},
        ]
        result = ensure_obo_secret(graph, {"app_id": "app", "object_id": "object"}, rotate=True)
        self.assertNotIn("secret-text", result)
        put = graph.request.call_args_list[2]
        self.assertEqual(put.args[0], "PUT")
        self.assertEqual(put.args[2]["value"], "secret-text")

    def test_failed_vault_write_removes_new_password(self):
        graph = Mock(vault_url="https://vault.vault.azure.net")
        graph.request.side_effect = [
            IdentityError(404, "Missing"), {"secretText": "private", "keyId": "key"},
            IdentityError(403, "Denied"), {},
        ]
        with self.assertRaises(IdentityError):
            ensure_obo_secret(graph, {"app_id": "app", "object_id": "object"})
        graph.request.assert_called_with("POST", "/applications/object/removePassword", {"keyId": "key"})

    def test_http_errors_never_echo_credentials_or_server_body(self):
        graph = GraphClient(Mock(), "https://vault.vault.azure.net")
        graph.credential.get_token.return_value.token = "bearer-secret"
        graph.session = Mock()
        graph.session.request.return_value = Mock(
            ok=False, status_code=403, json=Mock(return_value={"error": {"message": "bearer-secret private"}}))
        with self.assertRaises(IdentityError) as caught:
            graph.request("POST", "/applications?opaque=do-not-log")
        self.assertNotIn("bearer-secret", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))
        self.assertIn("POST graph.microsoft.com/v1.0/applications", str(caught.exception))
        self.assertNotIn("do-not-log", str(caught.exception))

    def test_pagination_cannot_exfiltrate_graph_bearer(self):
        graph = GraphClient(Mock(), "https://vault.vault.azure.net")
        with self.assertRaises(ValueError):
            graph.request("GET", "https://attacker.example/next")
        graph.credential.get_token.assert_not_called()

    def test_vault_proxy_sends_operator_credentials_and_secret_only_in_stdin(self):
        graph = GraphClient(Mock(), "https://vault.vault.azure.net", "gateway")
        graph.credential.get_token.return_value.token = "operator-private-token"
        graph.session = Mock()
        with patch("accelerator.entra_setup.subprocess.run") as run:
            run.return_value = Mock(returncode=0, stdout=json.dumps({
                "status": 200, "body": '{"id":"secret-uri"}', "headers": {},
            }))
            result = graph.request("PUT", "https://vault.vault.azure.net/secrets/name?api-version=7.4",
                                   {"value": "password-private"})
        self.assertEqual(result, {"id": "secret-uri"})
        command = run.call_args.args[0]
        self.assertNotIn("operator-private-token", repr(command))
        self.assertNotIn("password-private", repr(command))
        self.assertEqual(command[:7], ["kubectl", "-n", "gateway", "exec", "-i", "deployment/fastmcp", "--"])
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["headers"]["Authorization"], "Bearer operator-private-token")
        self.assertEqual(payload["body"]["value"], "password-private")
        graph.credential.get_token.assert_called_once_with("https://vault.azure.net/.default")
        graph.session.request.assert_not_called()
        compile(VAULT_PROXY_SCRIPT, "<proxy>", "exec")

    def test_proxy_is_never_used_for_graph_or_unconfigured_vault(self):
        graph = GraphClient(Mock(), "https://vault.vault.azure.net", "gateway")
        graph.credential.get_token.return_value.token = "operator-token"
        graph.session = Mock()
        graph.session.request.return_value = Mock(ok=True, content=b"{}", json=Mock(return_value={}))
        with patch("accelerator.entra_setup.subprocess.run") as run:
            graph.request("GET", "/me")
            with self.assertRaises(ValueError):
                graph.request("GET", "https://other.vault.azure.net/secrets/name")
        run.assert_not_called()
        self.assertEqual(graph.session.request.call_count, 1)

    def test_proxy_failure_does_not_log_output_or_fallback(self):
        graph = GraphClient(Mock(), "https://vault.vault.azure.net", "gateway")
        graph.credential.get_token.return_value.token = "operator-token"
        graph.session = Mock()
        with patch("accelerator.entra_setup.subprocess.run") as run:
            run.return_value = Mock(returncode=1, stdout="secret", stderr="token")
            with self.assertRaisesRegex(RuntimeError, "output suppressed") as caught:
                graph.request("GET", "https://vault.vault.azure.net/secrets/name")
        self.assertNotIn("secret", str(caught.exception))
        graph.session.request.assert_not_called()

    def test_known_vault_connection_policy_code_is_reported_without_message(self):
        graph = GraphClient(Mock(), "https://vault.vault.azure.net")
        graph.credential.get_token.return_value.token = "operator-token"
        graph.session = Mock()
        graph.session.request.return_value = Mock(ok=False, status_code=403, json=Mock(return_value={
            "error": {"code": "Forbidden", "message": "secret-private",
                      "innererror": {"code": "ForbiddenByConnection"}}}))
        with self.assertRaisesRegex(IdentityError, "ForbiddenByConnection") as caught:
            graph.request("GET", "https://vault.vault.azure.net/secrets/name")
        self.assertNotIn("secret-private", str(caught.exception))

    def test_plan_never_authenticates_or_writes_receipt(self):
        with patch("accelerator.entra_setup.AzureCliCredential") as credential, \
             patch("accelerator.entra_setup.save_receipt") as save, \
             redirect_stdout(io.StringIO()) as output:
            main(["provision", "--tenant-id", "5dc82be3-90ab-4f72-a0f2-b2557ba694e3",
                  "--subscription-id", "292e62e6-54a8-4c6a-b996-0c83f8cc29d0",
                  "--project-id", "project", "--prefix", "test",
                  "--key-vault-url", "https://vault.vault.azure.net", "--output", "unused.json"])
        credential.assert_not_called()
        save.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["writes_performed"])

    def test_consent_is_explicit_and_foundry_scope_is_discovered(self):
        args = argparse.Namespace(prefix="demo", preauthorize_azure_cli=False, grant_admin_consent=False,
                                  rotate_obo_secret=False, tenant_id="tenant", subscription_id="sub",
                                  project_id="project", profile="user-obo", permitted_user_object_id=[],
                                  key_vault_subscription_id=None)
        graph = Mock(vault_url="https://vault.vault.azure.net")
        graph.list.return_value = [{
            "id": "foundry-sp", "appId": "discovered-resource-app",
            "oauth2PermissionScopes": [{"id": "discovered-scope-id", "value": "user_impersonation", "isEnabled": True}],
        }]
        registrations = [
            ({"id": "gateway-object"}, {"app_id": "gateway-app", "sp_id": "gateway-sp", "scope_id": "gateway-scope"}),
            ({}, {"app_id": "mcp-app", "sp_id": "mcp-sp", "scope_id": "mcp-scope"}),
            ({"id": "test-object"}, {"app_id": "test-app", "sp_id": "test-sp"}),
        ]
        with patch("accelerator.entra_setup.ensure_application", side_effect=registrations), \
             patch("accelerator.entra_setup.require_scope") as require, \
             patch("accelerator.entra_setup.grant_consent") as consent, \
             patch("accelerator.entra_setup.ensure_obo_secret", return_value="secret-uri"):
            receipt = provision(graph, args)
        consent.assert_not_called()
        require.assert_any_call(graph, {"id": "gateway-object"}, "discovered-resource-app", "discovered-scope-id")
        self.assertEqual(receipt["consent_grant_ids"], [])
        self.assertEqual(receipt["subscription_id"], "sub")


if __name__ == "__main__":
    unittest.main()
