variable "name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,58}[a-z0-9]$", var.name))
    error_message = "Search service names must be 2-60 lowercase letters, digits or hyphens, starting with a letter and ending with a letter or digit."
  }
}
variable "resource_group_id" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "pe_location" { type = string }
variable "pe_subnet_id" { type = string }
variable "private_dns_zone_id" { type = string }

resource "azapi_resource" "search" {
  type      = "Microsoft.Search/searchServices@2026-09-01-preview"
  name      = var.name
  parent_id = var.resource_group_id
  location  = var.location
  identity { type = "SystemAssigned" }
  body = {
    sku = { name = "basic" }
    properties = {
      replicaCount        = 1
      partitionCount      = 1
      publicNetworkAccess = "Disabled"
      disableLocalAuth    = true
      semanticSearch      = "standard"
      knowledgeRetrieval  = "standard"
    }
  }
  response_export_values = ["identity.principalId"]
}

resource "azurerm_private_endpoint" "search" {
  name                = "${var.name}-pe"
  resource_group_name = var.resource_group_name
  location            = var.pe_location
  subnet_id           = var.pe_subnet_id
  private_service_connection {
    name                           = var.name
    private_connection_resource_id = azapi_resource.search.id
    subresource_names              = ["searchService"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "search"
    private_dns_zone_ids = [var.private_dns_zone_id]
  }
}

output "search" {
  value = {
    id           = azapi_resource.search.id
    name         = var.name
    endpoint     = "https://${var.name}.search.windows.net"
    principal_id = azapi_resource.search.output.identity.principalId
    sku          = "basic"
    location     = var.location
  }
}
