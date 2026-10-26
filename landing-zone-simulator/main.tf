variable "subscription_id" { type = string }
variable "prefix" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{4,11}$", var.prefix))
    error_message = "Use 5-12 lowercase alphanumeric characters, starting with a letter."
  }
}
variable "primary_location" {
  type    = string
  default = "eastus2"
}
variable "secondary_location" {
  type    = string
  default = "swedencentral"
}
variable "enable_secondary" {
  type    = bool
  default = true
}
variable "enable_search_network" {
  type    = bool
  default = false
}
variable "enable_shared_storage" {
  type    = bool
  default = false
  validation {
    condition     = !var.enable_shared_storage || var.enable_search_network
    error_message = "Shared Storage requires the search network (private endpoint subnet and Blob DNS)."
  }
}
variable "team_gateway_subnets" {
  type = map(string)
  default = {
    team-alpha = "10.73.5.0/24"
    team-beta  = "10.73.6.0/24"
  }
  validation {
    condition     = !contains(keys(var.team_gateway_subnets), "agents") && !contains(keys(var.team_gateway_subnets), "gateway")
    error_message = "Team gateway subnet names cannot replace the platform agent/gateway subnets."
  }
}
locals {
  regions = merge(
    { primary = { location = var.primary_location, address = "10.73.0.0/16" } },
    var.enable_secondary ? { secondary = { location = var.secondary_location, address = "10.74.0.0/16" } } : {}
  )
  dns_names = toset([
    "privatelink.cognitiveservices.azure.com",
    "privatelink.openai.azure.com",
    "privatelink.services.ai.azure.com"
  ])
  tags = { application = "foundry-shared-accelerator", purpose = "customer-network-simulation" }
}
resource "azurerm_resource_group" "network" {
  name     = "rg-${var.prefix}-network"
  location = var.primary_location
  tags     = local.tags
}
resource "azurerm_virtual_network" "region" {
  for_each            = local.regions
  name                = "vnet-${var.prefix}-${each.key}"
  location            = each.value.location
  resource_group_name = azurerm_resource_group.network.name
  address_space       = [each.value.address]
  tags                = local.tags
}
resource "azurerm_subnet" "model_pe" {
  for_each                          = local.regions
  name                              = "model-private-endpoints"
  resource_group_name               = azurerm_resource_group.network.name
  virtual_network_name              = azurerm_virtual_network.region[each.key].name
  address_prefixes                  = [each.key == "primary" ? "10.73.4.0/24" : "10.74.4.0/24"]
  private_endpoint_network_policies = "NetworkSecurityGroupEnabled"
}
resource "azurerm_network_security_group" "models" {
  for_each            = local.regions
  name                = "nsg-${var.prefix}-models-${each.key}"
  resource_group_name = azurerm_resource_group.network.name
  location            = each.value.location
  tags                = local.tags
  security_rule {
    name                       = "GatewayAndValidationRunner"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefixes    = ["10.73.2.0/24", "10.73.3.0/24"]
    destination_address_prefix = "*"
  }
  security_rule {
    name                       = "DenyOtherModelCallers"
    priority                   = 200
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}
resource "azurerm_subnet_network_security_group_association" "models" {
  for_each                  = local.regions
  subnet_id                 = azurerm_subnet.model_pe[each.key].id
  network_security_group_id = azurerm_network_security_group.models[each.key].id
}
resource "azurerm_subnet" "agent_pe" {
  name                 = "agent-private-endpoints"
  resource_group_name  = azurerm_resource_group.network.name
  virtual_network_name = azurerm_virtual_network.region["primary"].name
  address_prefixes     = ["10.73.0.0/24"]
}
resource "azurerm_subnet" "search_pe" {
  count                = var.enable_search_network ? 1 : 0
  name                 = "search-private-endpoints"
  resource_group_name  = azurerm_resource_group.network.name
  virtual_network_name = azurerm_virtual_network.region["primary"].name
  address_prefixes     = ["10.73.7.0/24"]
}
resource "azurerm_private_dns_zone" "search" {
  count               = var.enable_search_network ? 1 : 0
  name                = "privatelink.search.windows.net"
  resource_group_name = azurerm_resource_group.network.name
  tags                = local.tags
}
resource "azurerm_private_dns_zone_virtual_network_link" "search" {
  for_each             = var.enable_search_network ? local.regions : {}
  name                 = "${var.prefix}-${each.key}"
  private_dns_zone_id  = azurerm_private_dns_zone.search[0].id
  virtual_network_id   = azurerm_virtual_network.region[each.key].id
  registration_enabled = false
}
resource "azurerm_storage_account" "shared" {
  count                           = var.enable_shared_storage ? 1 : 0
  name                            = "${var.prefix}shared"
  resource_group_name             = azurerm_resource_group.network.name
  location                        = var.primary_location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  account_kind                    = "StorageV2"
  min_tls_version                 = "TLS1_2"
  public_network_access           = "Disabled"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
  tags                            = local.tags
}
resource "azapi_resource" "shared_container" {
  count     = var.enable_shared_storage ? 1 : 0
  type      = "Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01"
  name      = "knowledge"
  parent_id = "${azurerm_storage_account.shared[0].id}/blobServices/default"
  body = {
    properties = {
      publicAccess = "None"
    }
  }
}
resource "azurerm_private_dns_zone" "blob" {
  count               = var.enable_shared_storage ? 1 : 0
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = azurerm_resource_group.network.name
  tags                = local.tags
}
resource "azurerm_private_dns_zone_virtual_network_link" "blob" {
  for_each             = var.enable_shared_storage ? local.regions : {}
  name                 = "${var.prefix}-${each.key}"
  private_dns_zone_id  = azurerm_private_dns_zone.blob[0].id
  virtual_network_id   = azurerm_virtual_network.region[each.key].id
  registration_enabled = false
}
resource "azurerm_private_endpoint" "blob" {
  count               = var.enable_shared_storage ? 1 : 0
  name                = "${var.prefix}-blob-pe"
  resource_group_name = azurerm_resource_group.network.name
  location            = var.primary_location
  subnet_id           = azurerm_subnet.search_pe[0].id
  private_service_connection {
    name                           = "${var.prefix}-blob"
    private_connection_resource_id = azurerm_storage_account.shared[0].id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "blob"
    private_dns_zone_ids = [azurerm_private_dns_zone.blob[0].id]
  }
}
resource "azurerm_subnet" "delegated" {
  for_each             = merge({ agents = "10.73.1.0/24", gateway = "10.73.2.0/24" }, var.team_gateway_subnets)
  name                 = each.key
  resource_group_name  = azurerm_resource_group.network.name
  virtual_network_name = azurerm_virtual_network.region["primary"].name
  address_prefixes     = [each.value]
  delegation {
    name = "container-apps"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}
resource "azurerm_subnet" "runner" {
  name                 = "validation-runner"
  resource_group_name  = azurerm_resource_group.network.name
  virtual_network_name = azurerm_virtual_network.region["primary"].name
  address_prefixes     = ["10.73.3.0/24"]
}
resource "azurerm_public_ip" "egress" {
  name                = "pip-${var.prefix}-egress"
  resource_group_name = azurerm_resource_group.network.name
  location            = var.primary_location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.tags
  lifecycle {
    # Azure stamps FirstPartyUsage; removing that RP-owned tag forces IP replacement.
    ignore_changes = [ip_tags]
  }
}
resource "azurerm_nat_gateway" "egress" {
  name                = "nat-${var.prefix}"
  resource_group_name = azurerm_resource_group.network.name
  location            = var.primary_location
  tags                = local.tags
}
resource "azurerm_nat_gateway_public_ip_association" "egress" {
  nat_gateway_id       = azurerm_nat_gateway.egress.id
  public_ip_address_id = azurerm_public_ip.egress.id
}
resource "azurerm_subnet_nat_gateway_association" "egress" {
  for_each       = merge({ runner = azurerm_subnet.runner.id }, { for key, subnet in azurerm_subnet.delegated : key => subnet.id })
  subnet_id      = each.value
  nat_gateway_id = azurerm_nat_gateway.egress.id
}
resource "azurerm_virtual_network_peering" "to_secondary" {
  count                     = var.enable_secondary ? 1 : 0
  name                      = "primary-to-secondary"
  resource_group_name       = azurerm_resource_group.network.name
  virtual_network_name      = azurerm_virtual_network.region["primary"].name
  remote_virtual_network_id = azurerm_virtual_network.region["secondary"].id
}
resource "azurerm_virtual_network_peering" "to_primary" {
  count                     = var.enable_secondary ? 1 : 0
  name                      = "secondary-to-primary"
  resource_group_name       = azurerm_resource_group.network.name
  virtual_network_name      = azurerm_virtual_network.region["secondary"].name
  remote_virtual_network_id = azurerm_virtual_network.region["primary"].id
}
resource "azurerm_private_dns_zone" "foundry" {
  for_each            = local.dns_names
  name                = each.value
  resource_group_name = azurerm_resource_group.network.name
  tags                = local.tags
}
locals {
  dns_links = { for pair in setproduct(keys(local.regions), local.dns_names) : "${pair[0]}:${pair[1]}" => { region = pair[0], zone = pair[1] } }
}
resource "azurerm_private_dns_zone_virtual_network_link" "foundry" {
  for_each             = local.dns_links
  name                 = "${var.prefix}-${each.value.region}"
  private_dns_zone_id  = azurerm_private_dns_zone.foundry[each.value.zone].id
  virtual_network_id   = azurerm_virtual_network.region[each.value.region].id
  registration_enabled = false
}
output "network" {
  value = {
    primary_location           = var.primary_location
    agent_subnet_id            = azurerm_subnet.delegated["agents"].id
    agent_pe_subnet_id         = azurerm_subnet.agent_pe.id
    gateway_subnet_id          = azurerm_subnet.delegated["gateway"].id
    team_gateway_subnet_ids    = { for name in keys(var.team_gateway_subnets) : name => azurerm_subnet.delegated[name].id }
    runner_subnet_id           = azurerm_subnet.runner.id
    private_dns_zone_ids       = [for zone in azurerm_private_dns_zone.foundry : zone.id]
    search_pe_subnet_id        = var.enable_search_network ? azurerm_subnet.search_pe[0].id : null
    search_private_dns_zone_id = var.enable_search_network ? azurerm_private_dns_zone.search[0].id : null
    storage_account_id         = var.enable_shared_storage ? azurerm_storage_account.shared[0].id : null
    shared_storage = var.enable_shared_storage ? {
      id               = azurerm_storage_account.shared[0].id
      name             = azurerm_storage_account.shared[0].name
      blob_endpoint    = azurerm_storage_account.shared[0].primary_blob_endpoint
      blob_dns_zone_id = azurerm_private_dns_zone.blob[0].id
      container_id     = azapi_resource.shared_container[0].id
      container_name   = azapi_resource.shared_container[0].name
    } : null
    models = { for key, region in local.regions : key => {
      location     = region.location
      pe_subnet_id = azurerm_subnet.model_pe[key].id
    } }
  }
}
