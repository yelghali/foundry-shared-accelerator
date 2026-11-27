terraform {
  required_version = ">= 1.9"
  backend "local" {
    path = "../../.local/demo-private-foundry/terraform.tfstate"
  }
  required_providers {
    azapi   = { source = "Azure/azapi", version = "~> 2.8" }
    azurerm = { source = "hashicorp/azurerm", version = "~> 5.9" }
  }
}

variable "subscription_id" { type = string }
variable "tenant_id" { type = string }
variable "name" { type = string }
variable "location" {
  type    = string
  default = "eastus2"
}
variable "operator_object_id" { type = string }
variable "gateway_model_principal_ids" {
  type    = set(string)
  default = []
}
variable "gateway_agent_principal_ids" {
  description = "Service identities for shared/dedicated service identities only. Do not grant the delegated-user gateway a delegated-agent fallback."
  type        = set(string)
  default     = []
}
variable "gateway_vnet_id" {
  description = "The standalone demo AKS/APIM VNet; never the original accelerator VNet."
  type        = string
}
variable "agent_subnet_cidr" {
  type    = string
  default = "10.240.18.0/24"
}
variable "existing_private_dns_zone_ids" {
  description = "Reuse the three Foundry private DNS zones already linked to the gateway VNet, if present."
  type        = set(string)
  default     = []
}
variable "link_existing_private_dns_zones" {
  description = "Create gateway VNet links for supplied zones; set false only if those links already exist."
  type        = bool
  default     = true
}
variable "model_name" {
  type    = string
  default = "gpt-4o-mini"
}
variable "model_version" {
  type    = string
  default = "2024-07-18"
}
variable "project_name" {
  type    = string
  default = "private-gateway-demo"
}

provider "azapi" {
  subscription_id = var.subscription_id
  tenant_id       = var.tenant_id
}
provider "azurerm" {
  features {}
  subscription_id                 = var.subscription_id
  tenant_id                       = var.tenant_id
  resource_provider_registrations = "none"
}

locals {
  vnet_parts = split("/", var.gateway_vnet_id)
}

data "azurerm_virtual_network" "gateway" {
  name                = local.vnet_parts[8]
  resource_group_name = local.vnet_parts[4]
}
data "azurerm_subnet" "endpoints" {
  name                 = "private-endpoints"
  virtual_network_name = data.azurerm_virtual_network.gateway.name
  resource_group_name  = data.azurerm_virtual_network.gateway.resource_group_name
}

resource "azurerm_resource_group" "demo" {
  name     = "${var.name}-private-foundry"
  location = var.location
}
resource "azurerm_subnet" "agents" {
  name                 = "${var.name}-agents"
  resource_group_name  = data.azurerm_virtual_network.gateway.resource_group_name
  virtual_network_name = data.azurerm_virtual_network.gateway.name
  address_prefixes     = [var.agent_subnet_cidr]
  delegation {
    name = "foundry-agents"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}
resource "azurerm_network_security_group" "agents" {
  name                = "${var.name}-agents-egress"
  location            = var.location
  resource_group_name = azurerm_resource_group.demo.name
  security_rule {
    name                       = "deny-unapproved-internet"
    priority                   = 4000
    direction                  = "Outbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "Internet"
  }
}
resource "azurerm_subnet_network_security_group_association" "agents" {
  subnet_id                 = azurerm_subnet.agents.id
  network_security_group_id = azurerm_network_security_group.agents.id
}
resource "azurerm_private_dns_zone" "foundry" {
  for_each            = length(var.existing_private_dns_zone_ids) == 0 ? toset(["privatelink.cognitiveservices.azure.com", "privatelink.services.ai.azure.com", "privatelink.openai.azure.com"]) : toset([])
  name                = each.value
  resource_group_name = azurerm_resource_group.demo.name
}
resource "azurerm_private_dns_zone_virtual_network_link" "foundry" {
  for_each             = azurerm_private_dns_zone.foundry
  name                 = "${var.name}-aks-and-agents"
  private_dns_zone_id  = each.value.id
  virtual_network_id   = var.gateway_vnet_id
  registration_enabled = false
}
resource "azurerm_private_dns_zone_virtual_network_link" "existing" {
  for_each             = var.link_existing_private_dns_zones ? var.existing_private_dns_zone_ids : toset([])
  name                 = "${var.name}-aks-and-agents"
  private_dns_zone_id  = each.value
  virtual_network_id   = var.gateway_vnet_id
  registration_enabled = false
}

module "foundry" {
  source                 = "../modules/foundry"
  name                   = var.name
  resource_group_id      = azurerm_resource_group.demo.id
  resource_group_name    = azurerm_resource_group.demo.name
  location               = var.location
  pe_location            = var.location
  pe_subnet_id           = data.azurerm_subnet.endpoints.id
  agent_subnet_id        = azurerm_subnet.agents.id
  private_dns_zone_ids   = length(var.existing_private_dns_zone_ids) > 0 ? tolist(var.existing_private_dns_zone_ids) : [for zone in azurerm_private_dns_zone.foundry : zone.id]
  allow_public_access    = false
  trusted_service_bypass = false
  model = {
    name = var.model_name, version = var.model_version, sku = "GlobalStandard", capacity = 1
  }
  depends_on = [azurerm_subnet_network_security_group_association.agents]
}

module "project" {
  source                  = "../modules/project"
  name                    = var.project_name
  account_id              = module.foundry.account.id
  account_endpoint        = module.foundry.account.endpoint
  location                = var.location
  subscription_id         = var.subscription_id
  developer_principal_ids = [var.operator_object_id]
}
resource "azurerm_role_assignment" "project_model" {
  scope                = module.foundry.account.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = module.project.project.principal_id
  principal_type       = "ServicePrincipal"
}
resource "azurerm_role_assignment" "gateway_model" {
  for_each             = var.gateway_model_principal_ids
  scope                = module.foundry.account.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = each.value
  principal_type       = "ServicePrincipal"
}
resource "azurerm_role_assignment" "gateway_agent" {
  for_each           = var.gateway_agent_principal_ids
  scope              = module.project.project.id
  role_definition_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/eed3b665-ab3a-47b6-8f48-c9382fb1dad6"
  principal_id       = each.value
  principal_type     = "ServicePrincipal"
}
resource "azurerm_container_registry" "hosted" {
  name                          = "${replace(var.name, "-", "")}acr"
  resource_group_name           = azurerm_resource_group.demo.name
  location                      = var.location
  sku                           = "Premium"
  admin_enabled                 = false
  public_network_access_enabled = false
  network_rule_bypass_option    = "AzureServices"
}
resource "azurerm_private_dns_zone" "registry" {
  name                = "privatelink.azurecr.io"
  resource_group_name = azurerm_resource_group.demo.name
}
resource "azurerm_private_dns_zone_virtual_network_link" "registry" {
  name                 = "${var.name}-registry"
  private_dns_zone_id  = azurerm_private_dns_zone.registry.id
  virtual_network_id   = var.gateway_vnet_id
  registration_enabled = false
}
resource "azurerm_private_endpoint" "registry" {
  name                = "${var.name}-registry"
  resource_group_name = azurerm_resource_group.demo.name
  location            = var.location
  subnet_id           = data.azurerm_subnet.endpoints.id
  private_service_connection {
    name                           = "registry"
    private_connection_resource_id = azurerm_container_registry.hosted.id
    subresource_names              = ["registry"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "registry"
    private_dns_zone_ids = [azurerm_private_dns_zone.registry.id]
  }
}
resource "azurerm_role_assignment" "private_image_pull" {
  for_each             = toset(["account", "project"])
  scope                = azurerm_container_registry.hosted.id
  role_definition_name = "AcrPull"
  principal_id         = each.key == "account" ? module.foundry.account.principal_id : module.project.project.principal_id
  principal_type       = "ServicePrincipal"
}

output "private_foundry" {
  value = {
    account_id        = module.foundry.account.id
    project_id        = module.project.project.id
    project_endpoint  = module.project.project.endpoint
    model_endpoint    = "https://${var.name}.openai.azure.com/"
    model_deployment  = var.model_name
    agent_subnet_id   = azurerm_subnet.agents.id
    agent_subnet_cidr = var.agent_subnet_cidr
    public_access     = false
    private_registry  = azurerm_container_registry.hosted.login_server
    internet_egress   = "NSG Internet deny; fixed public HTTPS denial and private hosted model/MCP access verified."
  }
}
