variable "name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.name))
    error_message = "Use a 3-40 character lowercase project name."
  }
}
variable "account_id" { type = string }
variable "account_endpoint" { type = string }
variable "location" { type = string }
variable "subscription_id" { type = string }
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
variable "agents_application_insights_id" {
  type    = string
  default = null
}
locals {
  authors = setunion(var.developer_principal_ids, var.user_principal_ids, var.group_principal_ids, var.service_principal_ids)
  index_grants = {
    for pair in setproduct(var.search_index_ids, setunion(var.user_principal_ids, var.group_principal_ids, var.service_principal_ids)) :
    "${pair[0]}|${pair[1]}" => { index_id = pair[0], principal_id = pair[1] }
  }
}
resource "azapi_resource" "project" {
  type      = "Microsoft.CognitiveServices/accounts/projects@2025-06-01"
  name      = var.name
  parent_id = var.account_id
  location  = var.location
  identity { type = "SystemAssigned" }
  body                   = { properties = { displayName = var.name } }
  response_export_values = ["identity.principalId", "properties.internalId"]
}
resource "azurerm_role_assignment" "developer" {
  for_each                         = local.authors
  scope                            = azapi_resource.project.id
  role_definition_id               = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/53ca6127-db72-4b80-b1b0-d745d6d5456d"
  principal_id                     = each.value
  skip_service_principal_aad_check = contains(var.service_principal_ids, each.value)
}
resource "azurerm_role_assignment" "approved_model" {
  count              = var.approved_model == null ? 0 : 1
  scope              = "${var.approved_model.gateway_id}/workspaces/default/aliases/approved-chat"
  role_definition_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/a6b68e3d-a1f0-4c60-8678-237c050cf4ea"
  principal_id       = azapi_resource.project.output.identity.principalId
  principal_type     = "ServicePrincipal"
}
resource "azurerm_role_assignment" "project_index" {
  for_each           = var.search_index_ids
  scope              = each.value
  role_definition_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/8ebe5a00-799e-43f5-93ac-243d3dce84a7"
  principal_id       = azapi_resource.project.output.identity.principalId
  principal_type     = "ServicePrincipal"
}
resource "azurerm_role_assignment" "team_index" {
  for_each                         = local.index_grants
  scope                            = each.value.index_id
  role_definition_id               = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/8ebe5a00-799e-43f5-93ac-243d3dce84a7"
  principal_id                     = each.value.principal_id
  skip_service_principal_aad_check = contains(var.service_principal_ids, each.value.principal_id)
}
resource "azapi_resource" "model_connection" {
  count                     = var.approved_model == null ? 0 : 1
  type                      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-10-01-preview"
  name                      = "platform-models-${var.name}"
  parent_id                 = azapi_resource.project.id
  schema_validation_enabled = false
  body = {
    properties = {
      category      = "ApiManagement"
      authType      = "ProjectManagedIdentity"
      audience      = var.approved_model.audience
      target        = "${var.approved_model.gateway_endpoint}/default/models/openai/v1"
      isSharedToAll = false
      credentials   = {}
      metadata = {
        audience            = var.approved_model.audience
        models              = jsonencode([{ name = "approved-chat", properties = { model = { name = "approved-chat", format = "OpenAI", version = var.approved_model.model_version } } }])
        deploymentInPath    = "false"
        inferenceAPIVersion = "v1"
        authConfig          = "{}"
        customHeaders       = jsonencode({ "X-Chargeback-Project-Key" = var.approved_model.chargeback_key })
      }
    }
  }
  depends_on = [azurerm_role_assignment.approved_model]
}
resource "azurerm_role_assignment" "agent_tracing" {
  count                = var.agents_application_insights_id == null ? 0 : 1
  scope                = var.agents_application_insights_id
  role_definition_name = "Monitoring Metrics Publisher"
  principal_id         = azapi_resource.project.output.identity.principalId
  principal_type       = "ServicePrincipal"
}
data "azurerm_application_insights" "agents" {
  count               = var.agents_application_insights_id == null ? 0 : 1
  name                = split("/", var.agents_application_insights_id)[8]
  resource_group_name = split("/", var.agents_application_insights_id)[4]
}
resource "azapi_resource" "agent_tracing_connection" {
  count                     = var.agents_application_insights_id == null ? 0 : 1
  type                      = "Microsoft.CognitiveServices/accounts/projects/connections@2025-10-01-preview"
  name                      = "platform-agent-tracing-${var.name}"
  parent_id                 = azapi_resource.project.id
  schema_validation_enabled = false
  body = {
    properties = {
      category      = "AppInsights"
      authType      = "ProjectManagedIdentity"
      target        = var.agents_application_insights_id
      isSharedToAll = false
      metadata = {
        ApiType                             = "Azure"
        ResourceId                          = var.agents_application_insights_id
        ApplicationInsightsConnectionString = data.azurerm_application_insights.agents[0].connection_string
      }
    }
  }
  depends_on = [azurerm_role_assignment.agent_tracing]
}
resource "azapi_resource" "agents" {
  type                      = "Microsoft.CognitiveServices/accounts/projects/capabilityHosts@2025-04-01-preview"
  parent_id                 = azapi_resource.project.id
  name                      = "agents"
  schema_validation_enabled = false
  body                      = { properties = { capabilityHostKind = "Agents" } }
  depends_on                = [azurerm_role_assignment.developer]
}
output "project" {
  value = {
    id                  = azapi_resource.project.id
    name                = var.name
    principal_id        = azapi_resource.project.output.identity.principalId
    endpoint            = "${var.account_endpoint}/api/projects/${var.name}"
    model_reference     = var.approved_model == null ? null : "platform-models-${var.name}/approved-chat"
    model_connection_id = var.approved_model == null ? null : azapi_resource.model_connection[0].id
    model_alias_id      = var.approved_model == null ? null : "${var.approved_model.gateway_id}/workspaces/default/aliases/approved-chat"
  }
}
