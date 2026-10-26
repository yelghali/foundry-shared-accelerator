mock_provider "azurerm" {}
mock_provider "azapi" {
  mock_resource "azapi_resource" {
    defaults = {
      id     = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.CognitiveServices/accounts/test"
      output = { identity = { principalId = "00000000-0000-0000-0000-000000000002" } }
    }
  }
}
variables {
  name                 = "foundry-test"
  resource_group_id    = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test"
  resource_group_name  = "test"
  location             = "eastus2"
  pe_location          = "eastus2"
  pe_subnet_id         = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/net/providers/Microsoft.Network/virtualNetworks/vnet/subnets/pe"
  private_dns_zone_ids = ["/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/net/providers/Microsoft.Network/privateDnsZones/privatelink.services.ai.azure.com"]
}
run "model_ingress_only" {
  command = plan
  variables {
    model = { name = "gpt-4o-mini", version = "2024-07-18", sku = "GlobalStandard", capacity = 1 }
  }
  assert {
    condition     = azapi_resource.account.body.properties.publicNetworkAccess == "Disabled" && azapi_resource.account.body.properties.disableLocalAuth
    error_message = "Foundry public/key access must stay disabled."
  }
  assert {
    condition     = !contains(keys(azapi_resource.account.body.properties), "networkInjections") && length(azapi_resource.deployment) == 1
    error_message = "Model accounts must have inference but no agent network injection."
  }
}
run "agent_no_native_models" {
  command = plan
  variables {
    agent_subnet_id = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/net/providers/Microsoft.Network/virtualNetworks/vnet/subnets/agents"
  }
  assert {
    condition     = azapi_resource.account.body.properties.networkInjections[0].scenario == "agent" && length(azapi_resource.deployment) == 0
    error_message = "The shared agent account must have network injection and no native inference deployment."
  }
}
