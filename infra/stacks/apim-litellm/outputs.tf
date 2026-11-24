output "deployment_contract" {
  description = "Non-secret contract consumed by image, Kubernetes, and APIM API/policy release steps."
  value = {
    resource_group_name        = azurerm_resource_group.gateway.name
    location                   = var.location
    tenant_id                  = var.tenant_id
    mcp_service_token_audience = coalesce(var.mcp_service_token_audience, var.litellm_token_audience)
    aks = {
      name            = azurerm_kubernetes_cluster.gateway.name
      id              = azurerm_kubernetes_cluster.gateway.id
      oidc_issuer_url = azurerm_kubernetes_cluster.gateway.oidc_issuer_url
      subnet_id       = azurerm_subnet.aks.id
      subnet_cidr     = var.aks_subnet_cidr
      namespace       = "gateway"
    }
    acr = {
      id           = azurerm_container_registry.gateway.id
      name         = azurerm_container_registry.gateway.name
      login_server = azurerm_container_registry.gateway.login_server
    }
    workload_identities = {
      for name, identity in azurerm_user_assigned_identity.runtime : name => {
        client_id       = identity.client_id
        principal_id    = identity.principal_id
        resource_id     = identity.id
        service_account = name
        namespace       = "gateway"
      }
    }
    apim = {
      id                     = azurerm_api_management.gateway.id
      name                   = azurerm_api_management.gateway.name
      gateway_url            = azurerm_api_management.gateway.gateway_url
      principal_id           = azurerm_api_management.gateway.identity[0].principal_id
      caller_audience        = var.caller_audience
      litellm_token_audience = coalesce(var.mcp_service_token_audience, var.litellm_token_audience)
      foundry_token_audience = "https://ai.azure.com"
    }
    model_account_id   = var.model_account_id
    foundry_project_id = var.foundry_project_id
    blob = {
      account_name = azurerm_storage_account.demo.name
      endpoint     = azurerm_storage_account.demo.primary_blob_endpoint
      container    = azurerm_storage_container.demo.name
    }
  }
}
