mock_provider "azurerm" {}

variables {
  subscription_id            = "00000000-0000-0000-0000-000000000001"
  tenant_id                  = "00000000-0000-0000-0000-000000000002"
  operator_object_id         = "00000000-0000-0000-0000-000000000003"
  name                       = "gatewaytest"
  publisher_email            = "operator@example.com"
  aks_admin_group_object_ids = ["00000000-0000-0000-0000-000000000003"]
  caller_audience            = "api://gateway-caller"
  mcp_service_token_audience = "api://mcp-backend"
  litellm_backend_url        = "https://gateway.example.com:4000"
  allowed_caller_client_ids  = ["00000000-0000-0000-0000-000000000004"]
  model_account_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/existing/providers/Microsoft.CognitiveServices/accounts/models"
  foundry_project_id         = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/existing/providers/Microsoft.CognitiveServices/accounts/models/projects/demo"
}

run "identity_and_network_contract" {
  command = plan
  plan_options {
    target = [azurerm_api_management_policy.authentication, azurerm_role_assignment.mcp_blob, azurerm_container_registry.gateway]
  }
  assert {
    condition     = azurerm_api_management.gateway.sku_name == "StandardV2_1" && azurerm_api_management.gateway.virtual_network_type == "External"
    error_message = "APIM must retain Standard v2 outbound integration."
  }
  assert {
    condition     = strcontains(azurerm_api_management_policy.authentication.xml_content, "<audience>api://gateway-caller</audience>") && strcontains(azurerm_api_management_policy.authentication.xml_content, "<application-id>00000000-0000-0000-0000-000000000004</application-id>")
    error_message = "Inbound policy must validate both the dedicated audience and client allowlist."
  }
  assert {
    condition     = !azurerm_container_registry.gateway.admin_enabled && !azurerm_storage_account.demo.shared_access_key_enabled && azurerm_role_assignment.mcp_blob.role_definition_name == "Storage Blob Data Reader"
    error_message = "Image and blob access must remain identity-based and least privilege."
  }
}

run "reject_unknown_identity_scenario" {
  command = plan
  plan_options {
    target = [azurerm_api_management_backend.scenario]
  }
  variables {
    scenario_backend_urls = { unknown-profile = "https://gateway.example.com" }
  }
  expect_failures = [var.scenario_backend_urls]
}

run "reject_empty_caller_allowlist" {
  command = plan
  plan_options {
    target = [azurerm_api_management_policy.authentication]
  }
  variables {
    allowed_caller_client_ids = []
  }

  expect_failures = [var.allowed_caller_client_ids]
}

run "reject_plaintext_backend" {
  command = plan
  plan_options {
    target = [azurerm_api_management_backend.litellm]
  }
  variables {
    litellm_backend_url = "http://example.com:4000"
  }
  expect_failures = [var.litellm_backend_url]
}

run "reject_obo_without_dedicated_audience" {
  command = plan
  plan_options {
    target = [azurerm_api_management_api_policy.scenario]
  }
  variables {
    scenario_backend_urls = { user-obo = "https://gateway.example.com" }
  }
  expect_failures = [var.user_obo_gateway_audience]
}

run "independent_profile_tls_migration" {
  command = plan
  plan_options {
    target = [azurerm_api_management_backend.scenario]
  }
  variables {
    scenario_backend_urls = {
      dedicated-application = "https://app.example.com"
      user-obo              = "https://obo.example.com"
    }
    user_obo_gateway_audience   = "00000000-0000-0000-0000-000000000005"
    development_self_signed_tls = true
    scenario_development_self_signed_tls = {
      dedicated-application = false
      user-obo              = true
    }
  }
  assert {
    condition     = azurerm_api_management_backend.scenario["dedicated-application"].tls[0].validate_certificate_chain && !azurerm_api_management_backend.scenario["user-obo"].tls[0].validate_certificate_chain
    error_message = "Migrating one profile to trusted TLS must not change another profile's test-certificate mode."
  }
}
