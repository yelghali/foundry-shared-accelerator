variable "subscription_id" { type = string }
variable "project_name" { type = string }
variable "account_id" { type = string }
variable "account_endpoint" { type = string }
variable "location" { type = string }
variable "developer_principal_ids" { type = set(string) }
variable "user_principal_ids" {
  type    = set(string)
  default = []
}
variable "group_principal_ids" {
  type    = set(string)
  default = []
}
variable "service_principal_ids" {
  type    = set(string)
  default = []
}
variable "approved_model" {
  type = object({
    gateway_id       = string
    gateway_endpoint = string
    audience         = string
    model_version    = string
    chargeback_key   = string
  })
  default = null
}
variable "search_index_ids" {
  type    = set(string)
  default = []
}
variable "storage_account_id" {
  type    = string
  default = null
}
variable "search_principal_id" {
  type    = string
  default = null
}
variable "shared_search_id" {
  type    = string
  default = null
}
variable "search_location" {
  type    = string
  default = null
}
variable "agents_application_insights_id" {
  type    = string
  default = null
}
variable "dedicated_search" {
  type = object({
    location             = string
    pe_location          = string
    pe_subnet_id         = string
    private_dns_zone_id  = string
    embedding_account_id = string
  })
  default = null
}
variable "container_names" {
  type    = set(string)
  default = []
  validation {
    condition     = alltrue([for name in var.container_names : can(regex("^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])$", name))])
    error_message = "Blob container names must be 3-63 lowercase alphanumeric/hyphen characters."
  }
}
variable "gateway_subnet_id" {
  type    = string
  default = null
}
variable "shared_gateway" {
  type = object({
    id       = string, name = string, sku = string, principal_id = string,
    endpoint = string, public_frontend = bool
  })
  default = null
}
variable "runner_principal_id" { type = string }
locals {
  resource_group_id   = split("/providers/", var.account_id)[0]
  resource_group_name = element(split("/", local.resource_group_id), 4)
  search_principal_id = var.dedicated_search == null ? var.search_principal_id : module.dedicated_search[0].search.principal_id
  knowledge_search_id = var.shared_search_id != null ? var.shared_search_id : (var.dedicated_search != null && length(var.container_names) > 0 ? module.dedicated_search[0].search.id : null)
  team_authors        = setsubtract(setunion(var.user_principal_ids, var.group_principal_ids, var.service_principal_ids), toset([var.runner_principal_id]))
}
module "dedicated_search" {
  count               = var.dedicated_search == null ? 0 : 1
  source              = "../../modules/search-service"
  name                = "${var.project_name}-search"
  resource_group_id   = local.resource_group_id
  resource_group_name = local.resource_group_name
  location            = var.dedicated_search.location
  pe_location         = var.dedicated_search.pe_location
  pe_subnet_id        = var.dedicated_search.pe_subnet_id
  private_dns_zone_id = var.dedicated_search.private_dns_zone_id
}
resource "azurerm_search_shared_private_link_service" "dedicated_blob" {
  count              = var.dedicated_search == null ? 0 : 1
  name               = "${var.project_name}-blob"
  search_service_id  = module.dedicated_search[0].search.id
  target_resource_id = var.storage_account_id
  subresource_name   = "blob"
  request_message    = "Private team knowledge source ingestion"
}
resource "azurerm_search_shared_private_link_service" "dedicated_embedding" {
  count              = var.dedicated_search == null ? 0 : 1
  name               = "${var.project_name}-embedding"
  search_service_id  = module.dedicated_search[0].search.id
  target_resource_id = var.dedicated_search.embedding_account_id
  subresource_name   = "openai_account"
  request_message    = "Private team knowledge embedding"
}
resource "azurerm_role_assignment" "dedicated_model_user" {
  count                            = var.dedicated_search == null ? 0 : 1
  scope                            = var.dedicated_search.embedding_account_id
  role_definition_name             = "Cognitive Services User"
  principal_id                     = module.dedicated_search[0].search.principal_id
  skip_service_principal_aad_check = true
}
resource "azurerm_role_assignment" "dedicated_project_reader" {
  count                            = var.dedicated_search == null ? 0 : 1
  scope                            = module.dedicated_search[0].search.id
  role_definition_name             = "Search Index Data Reader"
  principal_id                     = module.project.project.principal_id
  skip_service_principal_aad_check = true
}
resource "azurerm_role_assignment" "shared_project_reader" {
  count                            = var.shared_search_id == null ? 0 : 1
  scope                            = var.shared_search_id
  role_definition_name             = "Search Index Data Reader"
  principal_id                     = module.project.project.principal_id
  skip_service_principal_aad_check = true
}
resource "azapi_resource" "knowledge_search_connection" {
  count                     = local.knowledge_search_id == null ? 0 : 1
  type                      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-10-01-preview"
  name                      = "platform-knowledge-search-${var.project_name}"
  parent_id                 = module.project.project.id
  schema_validation_enabled = false
  body = {
    properties = {
      category      = "CognitiveSearch"
      authType      = "ProjectManagedIdentity"
      target        = "https://${element(reverse(split("/", local.knowledge_search_id)), 0)}.search.windows.net"
      isSharedToAll = false
      metadata = {
        ApiType    = "Azure"
        ResourceId = local.knowledge_search_id
        location   = var.search_location
      }
    }
  }
  lifecycle {
    precondition {
      condition     = var.search_location != null
      error_message = "Search connections require the Search service location."
    }
  }
  depends_on = [azurerm_role_assignment.shared_project_reader, azurerm_role_assignment.dedicated_project_reader]
}
resource "azurerm_role_assignment" "dedicated_runner_author" {
  count                            = var.dedicated_search == null ? 0 : 1
  scope                            = module.dedicated_search[0].search.id
  role_definition_name             = "Search Service Contributor"
  principal_id                     = var.runner_principal_id
  skip_service_principal_aad_check = true
}
resource "azurerm_role_assignment" "dedicated_runner_reader" {
  count                            = var.dedicated_search == null ? 0 : 1
  scope                            = module.dedicated_search[0].search.id
  role_definition_name             = "Search Index Data Reader"
  principal_id                     = var.runner_principal_id
  skip_service_principal_aad_check = true
}
resource "azurerm_role_assignment" "dedicated_team_author" {
  for_each                         = var.dedicated_search == null ? toset([]) : local.team_authors
  scope                            = module.dedicated_search[0].search.id
  role_definition_name             = "Search Service Contributor"
  principal_id                     = each.value
  skip_service_principal_aad_check = contains(var.service_principal_ids, each.value)
}
moved {
  from = module.gateway
  to   = module.gateway[0]
}
moved {
  from = azurerm_role_assignment.runner_gateway
  to   = azurerm_role_assignment.runner_gateway[0]
}
module "gateway" {
  count             = var.shared_gateway == null ? 1 : 0
  source            = "../../modules/ai-gateway"
  name              = "gw-${substr(sha256("${var.account_id}/${var.project_name}"), 0, 16)}"
  resource_group_id = split("/providers/", var.account_id)[0]
  location          = var.location
  backend_subnet_id = var.gateway_subnet_id
}
resource "azurerm_role_assignment" "runner_gateway" {
  count                            = var.shared_gateway == null ? 1 : 0
  scope                            = module.gateway[0].gateway.id
  role_definition_name             = "Contributor"
  principal_id                     = var.runner_principal_id
  skip_service_principal_aad_check = true
}
module "project" {
  source                         = "../../modules/project"
  name                           = var.project_name
  account_id                     = var.account_id
  account_endpoint               = var.account_endpoint
  location                       = var.location
  subscription_id                = var.subscription_id
  developer_principal_ids        = var.developer_principal_ids
  user_principal_ids             = var.user_principal_ids
  group_principal_ids            = var.group_principal_ids
  service_principal_ids          = var.service_principal_ids
  approved_model                 = var.approved_model
  search_index_ids               = var.search_index_ids
  agents_application_insights_id = var.agents_application_insights_id
}
resource "azapi_resource" "container" {
  for_each  = var.container_names
  type      = "Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01"
  name      = each.value
  parent_id = "${coalesce(var.storage_account_id, "/missing-storage-account")}/blobServices/default"
  body      = { properties = { publicAccess = "None" } }
  lifecycle {
    precondition {
      condition     = var.storage_account_id != null && local.search_principal_id != null
      error_message = "Container onboarding requires both a Storage account ID and Search principal ID."
    }
  }
}
resource "azurerm_role_assignment" "search_blob_reader" {
  for_each           = var.container_names
  scope              = azapi_resource.container[each.key].id
  role_definition_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"
  principal_id       = local.search_principal_id
  principal_type     = "ServicePrincipal"
}
locals {
  container_writers = {
    for pair in setproduct(var.container_names, setunion(var.user_principal_ids, var.group_principal_ids, var.service_principal_ids)) :
    "${pair[0]}|${pair[1]}" => { container = pair[0], principal_id = pair[1] }
  }
}
resource "azurerm_role_assignment" "team_blob_writer" {
  for_each                         = local.container_writers
  scope                            = azapi_resource.container[each.value.container].id
  role_definition_id               = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/ba92f5b4-2d11-453d-a403-e96b0029c9fe"
  principal_id                     = each.value.principal_id
  skip_service_principal_aad_check = contains(var.service_principal_ids, each.value.principal_id)
}
output "project" {
  value = merge(module.project.project, {
    gateway    = var.shared_gateway == null ? module.gateway[0].gateway : var.shared_gateway
    containers = { for name, container in azapi_resource.container : name => container.id }
    search     = var.dedicated_search == null ? null : module.dedicated_search[0].search
  })
}
