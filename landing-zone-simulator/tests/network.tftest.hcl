mock_provider "azurerm" {}
mock_provider "azapi" {}
variables {
  subscription_id = "00000000-0000-0000-0000-000000000001"
  prefix          = "fsatest"
}
run "two_regions_default" {
  command = plan
  assert {
    condition     = length(azurerm_virtual_network.region) == 2 && length(azurerm_subnet.model_pe) == 2
    error_message = "The default simulator must provide two regional model ingress networks."
  }
  assert {
    condition     = length(azurerm_subnet.delegated) == 2 + length(var.team_gateway_subnets)
    error_message = "Primary agent, common gateway and team gateway subnets must stay separate."
  }
}
run "single_region_opt_out" {
  command = plan
  variables { enable_secondary = false }
  assert {
    condition     = length(azurerm_virtual_network.region) == 1 && length(azurerm_virtual_network_peering.to_secondary) == 0
    error_message = "Opting out must omit the secondary network and peerings."
  }
}
run "search_and_storage_opt_in" {
  command = plan
  variables {
    enable_search_network = true
    enable_shared_storage = true
  }
  assert {
    condition     = length(azurerm_subnet.search_pe) == 1 && length(azurerm_private_dns_zone.search) == 1 && length(azurerm_private_dns_zone.blob) == 1
    error_message = "Search and Blob must have dedicated DNS zones and private endpoint networking."
  }
  assert {
    condition     = length(azurerm_private_endpoint.blob) == 1 && length(azapi_resource.shared_container) == 1
    error_message = "Simulated Storage must use a private Blob endpoint and an ARM-created knowledge container."
  }
}
