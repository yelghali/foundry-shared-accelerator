variable "subscription_id" { type = string }
variable "prefix" { type = string }
variable "runner_ssh_public_key" { type = string }
variable "network" {
  type = object({
    primary_location           = string
    agent_subnet_id            = string
    agent_pe_subnet_id         = string
    gateway_subnet_id          = string
    runner_subnet_id           = string
    private_dns_zone_ids       = list(string)
    search_pe_subnet_id        = optional(string)
    search_private_dns_zone_id = optional(string)
    storage_account_id         = optional(string)
    shared_storage = optional(object({
      id               = string
      name             = string
      blob_endpoint    = string
      blob_dns_zone_id = string
      container_id     = string
      container_name   = string
    }))
    models = map(object({
      location     = string
      pe_subnet_id = string
      pe_location  = optional(string)
    }))
  })
  validation {
    condition = (
      contains(keys(var.network.models), "primary") &&
      alltrue([for key in keys(var.network.models) : contains(["primary", "secondary"], key)]) &&
      length(distinct([for model in values(var.network.models) : lower(model.location)])) == length(var.network.models)
    )
    error_message = "Supply primary and optional secondary model backends in distinct Azure regions."
  }
  validation {
    condition     = !var.enable_search || (var.network.search_pe_subnet_id != null && var.network.search_private_dns_zone_id != null)
    error_message = "Search requires customer-owned search PE subnet and privatelink.search.windows.net DNS zone IDs."
  }
}
variable "enable_search" {
  type    = bool
  default = false
}
variable "search_location" {
  type    = string
  default = null
}
variable "enable_embedding" {
  type    = bool
  default = false
}
variable "allow_public_agent_access" {
  type    = bool
  default = false
}
variable "embedding_deployment_name" {
  type    = string
  default = "text-embedding-3-small"
}
variable "embedding_model_version" {
  type    = string
  default = "1"
}
variable "embedding_sku_name" {
  type    = string
  default = "GlobalStandard"
}
variable "embedding_capacity" {
  type    = number
  default = 1
  validation {
    condition     = var.embedding_capacity > 0 && floor(var.embedding_capacity) == var.embedding_capacity
    error_message = "Embedding capacity must be a positive integer."
  }
}
variable "model_name" {
  type    = string
  default = "gpt-4o-mini"
}
variable "model_version" {
  type    = string
  default = "2024-07-18"
}
variable "model_capacity" {
  type    = number
  default = 1
}
variable "model_sku_name" {
  type    = string
  default = "GlobalStandard"
}
variable "runner_size" {
  type    = string
  default = "Standard_D2as_v7"
}
variable "validation_projects" {
  type    = set(string)
  default = ["team-alpha", "team-beta"]
}
data "azurerm_client_config" "current" {}
resource "azurerm_resource_group" "platform" {
  name     = "rg-${var.prefix}"
  location = var.network.primary_location
  tags     = { application = "foundry-shared-accelerator", lifecycle = "retained-live-test" }
}
module "agents" {
  source               = "../../modules/foundry"
  name                 = "${var.prefix}-agents"
  resource_group_id    = azurerm_resource_group.platform.id
  resource_group_name  = azurerm_resource_group.platform.name
  location             = var.network.primary_location
  pe_location          = var.network.primary_location
  pe_subnet_id         = var.network.agent_pe_subnet_id
  agent_subnet_id      = var.network.agent_subnet_id
  allow_public_access  = var.allow_public_agent_access
  private_dns_zone_ids = var.network.private_dns_zone_ids
}
module "models" {
  for_each               = var.network.models
  source                 = "../../modules/foundry"
  name                   = "${var.prefix}-model-${each.key == "primary" ? "a" : "b"}"
  resource_group_id      = azurerm_resource_group.platform.id
  resource_group_name    = azurerm_resource_group.platform.name
  location               = each.value.location
  pe_location            = coalesce(each.value.pe_location, each.value.location)
  pe_subnet_id           = each.value.pe_subnet_id
  private_dns_zone_ids   = var.network.private_dns_zone_ids
  trusted_service_bypass = var.enable_embedding && each.key == "primary"
  model = {
    name     = var.model_name
    version  = var.model_version
    sku      = var.model_sku_name
    capacity = var.model_capacity
  }
}
resource "azapi_resource" "embedding" {
  count     = var.enable_embedding ? 1 : 0
  type      = "Microsoft.CognitiveServices/accounts/deployments@2025-06-01"
  parent_id = module.models["primary"].account.id
  name      = var.embedding_deployment_name
  body = {
    sku = { name = var.embedding_sku_name, capacity = var.embedding_capacity }
    properties = {
      model                = { format = "OpenAI", name = "text-embedding-3-small", version = var.embedding_model_version }
      versionUpgradeOption = "NoAutoUpgrade"
    }
  }
}
module "search" {
  count               = var.enable_search ? 1 : 0
  source              = "../../modules/search-service"
  name                = "${var.prefix}-search"
  resource_group_id   = azurerm_resource_group.platform.id
  resource_group_name = azurerm_resource_group.platform.name
  location            = coalesce(var.search_location, var.network.primary_location)
  pe_location         = var.network.primary_location
  pe_subnet_id        = var.network.search_pe_subnet_id
  private_dns_zone_id = var.network.search_private_dns_zone_id
}
resource "azurerm_search_shared_private_link_service" "blob" {
  count              = var.enable_search && var.network.storage_account_id != null ? 1 : 0
  name               = "shared-blob"
  search_service_id  = module.search[0].search.id
  target_resource_id = var.network.storage_account_id
  subresource_name   = "blob"
  request_message    = "Shared Foundry IQ private Blob ingestion"
}
resource "azurerm_search_shared_private_link_service" "embedding" {
  count              = var.enable_search && var.enable_embedding ? 1 : 0
  name               = "shared-embedding"
  search_service_id  = module.search[0].search.id
  target_resource_id = module.models["primary"].account.id
  subresource_name   = "openai_account"
  request_message    = "Shared Foundry IQ private embedding access"
}
resource "azurerm_role_assignment" "search_model_user" {
  count                            = var.enable_search && var.enable_embedding ? 1 : 0
  scope                            = module.models["primary"].account.id
  role_definition_name             = "Cognitive Services User"
  principal_id                     = module.search[0].search.principal_id
  skip_service_principal_aad_check = true
}
resource "azurerm_role_assignment" "runner_search_author" {
  count                            = var.enable_search ? 1 : 0
  scope                            = module.search[0].search.id
  role_definition_name             = "Search Service Contributor"
  principal_id                     = azurerm_linux_virtual_machine.runner.identity[0].principal_id
  skip_service_principal_aad_check = true
}
resource "azurerm_role_assignment" "runner_search_reader" {
  count                            = var.enable_search ? 1 : 0
  scope                            = module.search[0].search.id
  role_definition_name             = "Search Index Data Reader"
  principal_id                     = azurerm_linux_virtual_machine.runner.identity[0].principal_id
  skip_service_principal_aad_check = true
}
resource "azurerm_role_assignment" "search_blob_reader" {
  count                            = var.enable_search && var.network.shared_storage != null ? 1 : 0
  scope                            = var.network.shared_storage.container_id
  role_definition_name             = "Storage Blob Data Reader"
  principal_id                     = module.search[0].search.principal_id
  skip_service_principal_aad_check = true
}
module "gateway" {
  source            = "../../modules/ai-gateway"
  name              = "${var.prefix}-gw"
  resource_group_id = azurerm_resource_group.platform.id
  location          = var.network.primary_location
  backend_subnet_id = var.network.gateway_subnet_id
}
resource "azurerm_role_assignment" "model_inference" {
  for_each                         = var.network.models
  scope                            = module.models[each.key].account.id
  role_definition_id               = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/53ca6127-db72-4b80-b1b0-d745d6d5456d"
  principal_id                     = module.gateway.gateway.principal_id
  skip_service_principal_aad_check = true
}
resource "azurerm_network_interface" "runner" {
  name                = "nic-${var.prefix}-runner"
  resource_group_name = azurerm_resource_group.platform.name
  location            = var.network.primary_location
  ip_configuration {
    name                          = "private"
    subnet_id                     = var.network.runner_subnet_id
    private_ip_address_allocation = "Dynamic"
  }
}
resource "azurerm_linux_virtual_machine" "runner" {
  name                                                   = "vm-${var.prefix}-runner"
  resource_group_name                                    = azurerm_resource_group.platform.name
  location                                               = var.network.primary_location
  size                                                   = var.runner_size
  bypass_platform_safety_checks_on_user_schedule_enabled = true
  admin_username                                         = "azureuser"
  network_interface_ids                                  = [azurerm_network_interface.runner.id]
  identity {
    type         = "SystemAssigned, UserAssigned"
    identity_ids = [for caller in azurerm_user_assigned_identity.caller : caller.id]
  }
  admin_ssh_key {
    username   = "azureuser"
    public_key = var.runner_ssh_public_key
  }
  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = 32
  }
  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts-gen2"
    version   = "latest"
  }
}
resource "azurerm_user_assigned_identity" "caller" {
  for_each            = var.validation_projects
  name                = "id-${var.prefix}-${each.value}"
  resource_group_name = azurerm_resource_group.platform.name
  location            = var.network.primary_location
}
resource "azurerm_role_definition" "key_reader" {
  name        = "${var.prefix} gateway runtime key reader"
  scope       = azurerm_resource_group.platform.id
  description = "Read only the explicitly scoped gateway runtime key for a sample consumer."
  permissions {
    actions = [
      "Microsoft.ApiManagement/aigateways/apiKeys/read",
      "Microsoft.ApiManagement/aigateways/apiKeys/listSecrets/action"
    ]
  }
  assignable_scopes = [azurerm_resource_group.platform.id]
}
resource "azurerm_role_assignment" "runner_gateway" {
  scope                            = module.gateway.gateway.id
  role_definition_name             = "Contributor"
  principal_id                     = azurerm_linux_virtual_machine.runner.identity[0].principal_id
  skip_service_principal_aad_check = true
}
resource "azurerm_log_analytics_workspace" "usage" {
  name                = "log-${var.prefix}"
  resource_group_name = azurerm_resource_group.platform.name
  location            = var.network.primary_location
  sku                 = "PerGB2018"
  retention_in_days   = 30
}
resource "azurerm_application_insights" "gateway" {
  name                = "appi-${var.prefix}"
  resource_group_name = azurerm_resource_group.platform.name
  location            = var.network.primary_location
  workspace_id        = azurerm_log_analytics_workspace.usage.id
  application_type    = "web"
}
resource "azurerm_application_insights" "agents" {
  name                         = "appi-${var.prefix}-agents"
  resource_group_name          = azurerm_resource_group.platform.name
  location                     = var.network.primary_location
  workspace_id                 = azurerm_log_analytics_workspace.usage.id
  application_type             = "web"
  local_authentication_enabled = false
}
module "monitor" {
  source                  = "../../modules/native-monitor"
  prefix                  = var.prefix
  resource_group_id       = azurerm_resource_group.platform.id
  location                = var.network.primary_location
  application_insights_id = azurerm_application_insights.gateway.id
  application_id          = azurerm_application_insights.gateway.app_id
  log_analytics_id        = azurerm_log_analytics_workspace.usage.id
  gateway_principal_id    = module.gateway.gateway.principal_id
  reader_principal_id     = data.azurerm_client_config.current.object_id
}
output "deployment" {
  value = {
    subscription_id       = var.subscription_id
    tenant_id             = data.azurerm_client_config.current.tenant_id
    deployer_principal_id = data.azurerm_client_config.current.object_id
    resource_group        = azurerm_resource_group.platform.name
    location              = var.network.primary_location
    agent_foundry         = module.agents.account
    models                = { for key, instance in module.models : key => instance.account }
    search                = var.enable_search ? module.search[0].search : null
    storage_account_id    = var.network.storage_account_id
    embedding = var.enable_embedding ? {
      id         = azapi_resource.embedding[0].id
      name       = var.embedding_deployment_name
      endpoint   = module.models["primary"].account.model_endpoint
      model_name = "text-embedding-3-small"
    } : null
    shared_storage = var.network.shared_storage
    gateway        = module.gateway.gateway
    runner = {
      name         = azurerm_linux_virtual_machine.runner.name
      principal_id = azurerm_linux_virtual_machine.runner.identity[0].principal_id
    }
    model_name                     = var.model_name
    model_version                  = var.model_version
    model_sku_name                 = var.model_sku_name
    application_insights_id        = azurerm_application_insights.gateway.id
    agents_application_insights_id = azurerm_application_insights.agents.id
    otlp                           = module.monitor.otlp
    gateway_key_reader_role_id     = azurerm_role_definition.key_reader.role_definition_id
    callers = { for name, caller in azurerm_user_assigned_identity.caller : name => {
      principal_id = caller.principal_id
      client_id    = caller.client_id
    } }
  }
}
