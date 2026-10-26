variable "name" { type = string }
variable "resource_group_id" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "pe_location" { type = string }
variable "pe_subnet_id" { type = string }
variable "private_dns_zone_ids" { type = list(string) }
variable "agent_subnet_id" {
  type    = string
  default = null
}
variable "trusted_service_bypass" {
  type    = bool
  default = false
}
variable "allow_public_access" {
  type    = bool
  default = false
}
variable "model" {
  type = object({
    name     = string
    version  = string
    sku      = string
    capacity = number
  })
  default = null
  validation {
    condition     = var.model == null ? true : var.model.capacity > 0 && floor(var.model.capacity) == var.model.capacity
    error_message = "Model capacity must be a positive integer."
  }
}
resource "azapi_resource" "account" {
  type      = "Microsoft.CognitiveServices/accounts@2026-05-01"
  name      = var.name
  parent_id = var.resource_group_id
  location  = var.location
  identity { type = "SystemAssigned" }
  body = {
    kind = "AIServices"
    sku  = { name = "S0" }
    properties = merge({
      allowProjectManagement = var.agent_subnet_id != null
      customSubDomainName    = var.name
      disableLocalAuth       = true
      publicNetworkAccess    = var.allow_public_access ? "Enabled" : "Disabled"
      networkAcls            = merge({ defaultAction = var.allow_public_access ? "Allow" : "Deny", ipRules = [], virtualNetworkRules = [] }, var.trusted_service_bypass ? { bypass = "AzureServices" } : {})
      }, var.agent_subnet_id == null ? {} : {
      networkInjections = [{
        scenario                   = "agent"
        subnetArmId                = var.agent_subnet_id
        useMicrosoftManagedNetwork = false
      }]
    })
  }
  response_export_values = ["identity.principalId", "properties.endpoint"]
}
resource "azapi_resource" "deployment" {
  count     = var.model == null ? 0 : 1
  type      = "Microsoft.CognitiveServices/accounts/deployments@2025-06-01"
  parent_id = azapi_resource.account.id
  name      = var.model.name
  body = {
    sku = { name = var.model.sku, capacity = var.model.capacity }
    properties = {
      model                = { format = "OpenAI", name = var.model.name, version = var.model.version }
      versionUpgradeOption = "NoAutoUpgrade"
    }
  }
}
resource "azurerm_private_endpoint" "account" {
  depends_on          = [azapi_resource.deployment]
  name                = "${var.name}-pe"
  resource_group_name = var.resource_group_name
  location            = var.pe_location
  subnet_id           = var.pe_subnet_id
  private_service_connection {
    name                           = var.name
    private_connection_resource_id = azapi_resource.account.id
    subresource_names              = ["account"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "foundry"
    private_dns_zone_ids = var.private_dns_zone_ids
  }
}
output "account" {
  value = {
    id             = azapi_resource.account.id
    name           = var.name
    location       = var.location
    principal_id   = azapi_resource.account.output.identity.principalId
    endpoint       = "https://${var.name}.services.ai.azure.com"
    model_endpoint = "https://${var.name}.cognitiveservices.azure.com/"
    deployment_id  = var.model == null ? null : azapi_resource.deployment[0].id
  }
}
