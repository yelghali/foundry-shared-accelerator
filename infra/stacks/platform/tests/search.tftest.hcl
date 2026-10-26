mock_provider "azurerm" {}
mock_provider "azapi" {}

variables {
  subscription_id       = "00000000-0000-0000-0000-000000000001"
  prefix                = "fsatest"
  runner_ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHCbQ76ZURZrRl9AxkVfdpSc1DcBr8iT7Yp0WUu/kmpl test"
  network = {
    primary_location           = "eastus2"
    agent_subnet_id            = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.Network/virtualNetworks/test/subnets/agents"
    agent_pe_subnet_id         = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.Network/virtualNetworks/test/subnets/agent-pe"
    gateway_subnet_id          = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.Network/virtualNetworks/test/subnets/gateway"
    runner_subnet_id           = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.Network/virtualNetworks/test/subnets/runner"
    search_pe_subnet_id        = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.Network/virtualNetworks/test/subnets/search-pe"
    search_private_dns_zone_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.Network/privateDnsZones/privatelink.search.windows.net"
    private_dns_zone_ids       = ["/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.Network/privateDnsZones/privatelink.services.ai.azure.com"]
    models = {
      primary = {
        location     = "eastus2"
        pe_subnet_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.Network/virtualNetworks/test/subnets/model-pe"
      }
    }
  }
}

run "existing_platform_unchanged" {
  command = plan
  assert {
    condition     = length(module.search) == 0 && length(azapi_resource.embedding) == 0
    error_message = "New billable infrastructure must remain opt-in."
  }
}

run "search_and_embedding" {
  command = plan
  variables {
    enable_search    = true
    enable_embedding = true
  }
  assert {
    condition     = length(module.search) == 1 && length(azapi_resource.embedding) == 1 && length(azurerm_role_assignment.runner_search_author) == 1 && length(azurerm_role_assignment.search_model_user) == 1
    error_message = "Search, primary embedding and scoped MI grants must be created on opt-in."
  }
  assert {
    condition     = output.deployment.search.sku == "basic" && output.deployment.search.endpoint == "https://fsatest-search.search.windows.net"
    error_message = "Search output must expose the standardized Basic endpoint."
  }
}
