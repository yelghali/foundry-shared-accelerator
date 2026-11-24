variable "enable_identity_scenarios" {
  description = "Add isolated dedicated-application and user-obo gateways and a Key Vault for OBO credentials."
  type        = bool
  default     = false
}

variable "dedicated_application_model_account_id" {
  description = "Optional narrower model account for the app-alpha identity; defaults to the shared test account."
  type        = string
  default     = null
}

variable "dedicated_application_foundry_scope_id" {
  description = "Optional agent/project ARM scope for app-alpha; defaults to the test project."
  type        = string
  default     = null
}

locals {
  identity_scenarios = var.enable_identity_scenarios ? {
    dedicated-application = {
      namespace = "gateway-app-alpha"
      model     = coalesce(var.dedicated_application_model_account_id, var.model_account_id)
      foundry   = coalesce(var.dedicated_application_foundry_scope_id, var.foundry_project_id)
    }
    user-obo = {
      namespace = "gateway-user-obo"
      model     = var.model_account_id
      foundry   = var.foundry_project_id
    }
  } : {}
}

resource "azurerm_user_assigned_identity" "scenario" {
  for_each            = local.identity_scenarios
  name                = "${var.name}-${each.key}"
  resource_group_name = azurerm_resource_group.gateway.name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "scenario" {
  for_each                  = local.identity_scenarios
  name                      = each.key
  user_assigned_identity_id = azurerm_user_assigned_identity.scenario[each.key].id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = azurerm_kubernetes_cluster.gateway.oidc_issuer_url
  subject                   = "system:serviceaccount:${each.value.namespace}:litellm"
}

resource "azurerm_federated_identity_credential" "scenario_mcp" {
  for_each                  = local.identity_scenarios
  name                      = each.key
  user_assigned_identity_id = azurerm_user_assigned_identity.runtime["fastmcp"].id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = azurerm_kubernetes_cluster.gateway.oidc_issuer_url
  subject                   = "system:serviceaccount:${each.value.namespace}:fastmcp"
}

resource "azurerm_federated_identity_credential" "scenario_seed" {
  for_each                  = local.identity_scenarios
  name                      = each.key
  user_assigned_identity_id = azurerm_user_assigned_identity.runtime["blob-seed"].id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = azurerm_kubernetes_cluster.gateway.oidc_issuer_url
  subject                   = "system:serviceaccount:${each.value.namespace}:blob-seed"
}

resource "azurerm_role_assignment" "scenario_models" {
  for_each             = local.identity_scenarios
  scope                = each.value.model
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.scenario[each.key].principal_id
}

resource "azurerm_role_assignment" "scenario_agents" {
  for_each           = { for key, value in local.identity_scenarios : key => value if key != "user-obo" }
  scope              = each.value.foundry
  role_definition_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/eed3b665-ab3a-47b6-8f48-c9382fb1dad6"
  principal_id       = azurerm_user_assigned_identity.scenario[each.key].principal_id
}

resource "azurerm_key_vault" "obo" {
  count                         = var.enable_identity_scenarios ? 1 : 0
  name                          = "${replace(var.name, "-", "")}obo"
  resource_group_name           = azurerm_resource_group.gateway.name
  location                      = var.location
  tenant_id                     = var.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  soft_delete_retention_days    = 7
  public_network_access_enabled = false
  tags                          = var.tags
}

resource "azurerm_private_dns_zone" "obo_vault" {
  count               = var.enable_identity_scenarios ? 1 : 0
  name                = "privatelink.vaultcore.azure.net"
  resource_group_name = azurerm_resource_group.gateway.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "obo_vault" {
  count               = var.enable_identity_scenarios ? 1 : 0
  name                = "${var.name}-obo"
  private_dns_zone_id = azurerm_private_dns_zone.obo_vault[0].id
  virtual_network_id  = azurerm_virtual_network.gateway.id
}

resource "azurerm_private_endpoint" "obo_vault" {
  count               = var.enable_identity_scenarios ? 1 : 0
  name                = "${var.name}-obo-vault"
  resource_group_name = azurerm_resource_group.gateway.name
  location            = var.location
  subnet_id           = azurerm_subnet.private_endpoints.id
  private_service_connection {
    name                           = "vault"
    private_connection_resource_id = azurerm_key_vault.obo[0].id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "vault"
    private_dns_zone_ids = [azurerm_private_dns_zone.obo_vault[0].id]
  }
}

resource "azurerm_role_assignment" "obo_secret_reader" {
  count                = var.enable_identity_scenarios ? 1 : 0
  scope                = azurerm_key_vault.obo[0].id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.scenario["user-obo"].principal_id
}

resource "azurerm_role_assignment" "obo_secret_operator" {
  count                = var.enable_identity_scenarios ? 1 : 0
  scope                = azurerm_key_vault.obo[0].id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = var.operator_object_id
}

output "identity_scenarios" {
  value = merge({
    shared-service = {
      namespace    = "gateway"
      client_id    = azurerm_user_assigned_identity.runtime["litellm"].client_id
      principal_id = azurerm_user_assigned_identity.runtime["litellm"].principal_id
    }
    }, {
    for key, value in local.identity_scenarios : key => {
      namespace    = value.namespace
      client_id    = azurerm_user_assigned_identity.scenario[key].client_id
      principal_id = azurerm_user_assigned_identity.scenario[key].principal_id
    }
  })
}

output "obo_key_vault" {
  value = var.enable_identity_scenarios ? {
    id       = azurerm_key_vault.obo[0].id
    name     = azurerm_key_vault.obo[0].name
    endpoint = azurerm_key_vault.obo[0].vault_uri
  } : null
}
