variable "name" { type = string }
variable "resource_group_id" { type = string }
variable "location" { type = string }
variable "backend_subnet_id" { type = string }
variable "sku_name" {
  type    = string
  default = "Premium"
  validation {
    condition     = contains(["Premium", "Consumption"], var.sku_name)
    error_message = "The current dedicated AIGateway RP accepts Premium or Consumption, not the retired AIGateway SKU name."
  }
}
resource "azapi_resource" "gateway" {
  type                      = "Microsoft.ApiManagement/aigateways@2026-09-01-preview"
  name                      = var.name
  parent_id                 = var.resource_group_id
  location                  = var.location
  schema_validation_enabled = false
  identity { type = "SystemAssigned" }
  body = {
    sku        = { name = var.sku_name }
    properties = { backend = { subnet = { id = var.backend_subnet_id } } }
  }
  response_export_values = ["identity.principalId", "properties.frontend.defaultHostname"]
}
output "gateway" {
  value = {
    id              = azapi_resource.gateway.id
    name            = var.name
    sku             = var.sku_name
    principal_id    = azapi_resource.gateway.output.identity.principalId
    endpoint        = "https://${azapi_resource.gateway.output.properties.frontend.defaultHostname}"
    public_frontend = true
  }
}
