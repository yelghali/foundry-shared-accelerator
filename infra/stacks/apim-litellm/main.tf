resource "azurerm_resource_group" "gateway" {
  name     = "${var.name}-apim-litellm"
  location = var.location
  tags     = var.tags
}

resource "azurerm_virtual_network" "gateway" {
  name                = "${var.name}-vnet"
  resource_group_name = azurerm_resource_group.gateway.name
  location            = var.location
  address_space       = var.vnet_address_space
  tags                = var.tags
}

resource "azurerm_subnet" "aks" {
  name                 = "aks"
  resource_group_name  = azurerm_resource_group.gateway.name
  virtual_network_name = azurerm_virtual_network.gateway.name
  address_prefixes     = [var.aks_subnet_cidr]
}

resource "azurerm_subnet" "apim" {
  name                 = "apim-outbound"
  resource_group_name  = azurerm_resource_group.gateway.name
  virtual_network_name = azurerm_virtual_network.gateway.name
  address_prefixes     = [var.apim_subnet_cidr]
  delegation {
    name = "apim-standard-v2"
    service_delegation {
      name    = "Microsoft.Web/serverFarms"
      actions = ["Microsoft.Network/virtualNetworks/subnets/action"]
    }
  }
}

resource "azurerm_network_security_group" "apim" {
  name                = "${var.name}-apim"
  resource_group_name = azurerm_resource_group.gateway.name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_subnet_network_security_group_association" "apim" {
  subnet_id                 = azurerm_subnet.apim.id
  network_security_group_id = azurerm_network_security_group.apim.id
}

locals {
  existing_vnet_parts = var.existing_vnet_id == null ? [] : split("/", var.existing_vnet_id)
}

resource "azurerm_virtual_network_peering" "to_existing" {
  count                     = var.existing_vnet_id == null ? 0 : 1
  name                      = "${var.name}-to-existing"
  resource_group_name       = azurerm_resource_group.gateway.name
  virtual_network_name      = azurerm_virtual_network.gateway.name
  remote_virtual_network_id = var.existing_vnet_id
}

resource "azurerm_virtual_network_peering" "from_existing" {
  count                     = var.existing_vnet_id == null ? 0 : 1
  name                      = "to-${var.name}"
  resource_group_name       = local.existing_vnet_parts[4]
  virtual_network_name      = local.existing_vnet_parts[8]
  remote_virtual_network_id = azurerm_virtual_network.gateway.id
  depends_on                = [azurerm_virtual_network_peering.to_existing]
}

resource "azurerm_private_dns_zone_virtual_network_link" "existing" {
  for_each             = var.private_dns_zone_ids
  name                 = "${var.name}-${each.key}"
  private_dns_zone_id  = each.value
  virtual_network_id   = azurerm_virtual_network.gateway.id
  registration_enabled = false
}

resource "azurerm_user_assigned_identity" "aks" {
  name                = "${var.name}-aks"
  resource_group_name = azurerm_resource_group.gateway.name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_role_assignment" "aks_network" {
  scope                = azurerm_subnet.aks.id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_user_assigned_identity.aks.principal_id
}

resource "azurerm_kubernetes_cluster" "gateway" {
  name                      = "${var.name}-aks"
  resource_group_name       = azurerm_resource_group.gateway.name
  location                  = var.location
  dns_prefix                = "${var.name}-aks"
  oidc_issuer_enabled       = true
  workload_identity_enabled = true
  dynamic "key_vault_secrets_provider" {
    for_each = var.enable_identity_scenarios ? [true] : []
    content {
      secret_rotation_enabled = true
    }
  }
  local_account_disabled            = true
  role_based_access_control_enabled = true
  node_provisioning_profile {
    mode = "Manual"
  }
  azure_active_directory_role_based_access_control {
    tenant_id              = var.tenant_id
    azure_rbac_enabled     = true
    admin_group_object_ids = var.aks_admin_group_object_ids
  }
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.aks.id]
  }
  default_node_pool {
    name           = "system"
    node_count     = var.node_count
    vm_size        = var.node_vm_size
    vnet_subnet_id = azurerm_subnet.aks.id
    upgrade_settings {
      max_surge                     = "10%"
      drain_timeout_in_minutes      = 0
      node_soak_duration_in_minutes = 0
    }
  }
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    pod_cidr            = "10.244.0.0/16"
    service_cidr        = "10.245.0.0/16"
    dns_service_ip      = "10.245.0.10"
    load_balancer_sku   = "standard"
    outbound_type       = "loadBalancer"
    network_policy      = "calico"
  }
  tags       = var.tags
  depends_on = [azurerm_role_assignment.aks_network]
  lifecycle {
    # Subscription policy owns this add-on; reruns must not remove its protection.
    ignore_changes = [microsoft_defender]
  }
}

resource "azurerm_container_registry" "gateway" {
  name                = "${replace(var.name, "-", "")}acr"
  resource_group_name = azurerm_resource_group.gateway.name
  location            = var.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = var.tags
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                            = azurerm_container_registry.gateway.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_kubernetes_cluster.gateway.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}

resource "azurerm_user_assigned_identity" "runtime" {
  for_each            = toset(["litellm", "fastmcp", "blob-seed"])
  name                = "${var.name}-${each.key}"
  resource_group_name = azurerm_resource_group.gateway.name
  location            = var.location
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "runtime" {
  for_each                  = azurerm_user_assigned_identity.runtime
  name                      = each.key
  user_assigned_identity_id = each.value.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = azurerm_kubernetes_cluster.gateway.oidc_issuer_url
  subject                   = "system:serviceaccount:gateway:${each.key}"
}

resource "azurerm_role_assignment" "model" {
  scope                = var.model_account_id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = azurerm_user_assigned_identity.runtime["litellm"].principal_id
}

resource "azurerm_storage_account" "demo" {
  name                            = "${replace(var.name, "-", "")}mcp"
  resource_group_name             = azurerm_resource_group.gateway.name
  location                        = var.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  public_network_access           = "Disabled"
  tags                            = var.tags
}

resource "azurerm_storage_container" "demo" {
  name                  = "demo"
  storage_account_id    = azurerm_storage_account.demo.id
  container_access_type = "private"
}

resource "azurerm_role_assignment" "mcp_blob" {
  scope                = "${azurerm_storage_account.demo.id}/blobServices/default/containers/${azurerm_storage_container.demo.name}"
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.runtime["fastmcp"].principal_id
}

resource "azurerm_role_assignment" "seed_blob" {
  scope                = "${azurerm_storage_account.demo.id}/blobServices/default/containers/${azurerm_storage_container.demo.name}"
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.runtime["blob-seed"].principal_id
}

resource "azurerm_subnet" "private_endpoints" {
  name                 = "private-endpoints"
  resource_group_name  = azurerm_resource_group.gateway.name
  virtual_network_name = azurerm_virtual_network.gateway.name
  address_prefixes     = [var.private_endpoint_subnet_cidr]
}

resource "azurerm_private_dns_zone" "blob" {
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = azurerm_resource_group.gateway.name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "blob" {
  name                = "${var.name}-blob"
  private_dns_zone_id = azurerm_private_dns_zone.blob.id
  virtual_network_id  = azurerm_virtual_network.gateway.id
}

resource "azurerm_private_endpoint" "blob" {
  name                = "${var.name}-blob"
  resource_group_name = azurerm_resource_group.gateway.name
  location            = var.location
  subnet_id           = azurerm_subnet.private_endpoints.id
  private_service_connection {
    name                           = "blob"
    private_connection_resource_id = azurerm_storage_account.demo.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "blob"
    private_dns_zone_ids = [azurerm_private_dns_zone.blob.id]
  }
}

resource "azurerm_api_management" "gateway" {
  name                 = "${var.name}-apim"
  resource_group_name  = azurerm_resource_group.gateway.name
  location             = var.location
  publisher_name       = var.publisher_name
  publisher_email      = var.publisher_email
  sku_name             = "StandardV2_1"
  virtual_network_type = "External"
  virtual_network_configuration {
    subnet_id = azurerm_subnet.apim.id
  }
  identity { type = "SystemAssigned" }
  tags       = var.tags
  depends_on = [azurerm_subnet_network_security_group_association.apim]
}

resource "azurerm_role_assignment" "litellm_foundry" {
  scope              = var.foundry_project_id
  role_definition_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Authorization/roleDefinitions/eed3b665-ab3a-47b6-8f48-c9382fb1dad6"
  principal_id       = azurerm_user_assigned_identity.runtime["litellm"].principal_id
}

resource "azurerm_role_assignment" "operator_aks" {
  scope                = azurerm_kubernetes_cluster.gateway.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = var.operator_object_id
}

resource "azurerm_role_assignment" "operator_blob" {
  scope                = "${azurerm_storage_account.demo.id}/blobServices/default/containers/${azurerm_storage_container.demo.name}"
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = var.operator_object_id
}

resource "azurerm_api_management_policy" "authentication" {
  api_management_id = azurerm_api_management.gateway.id
  xml_content = templatefile("${path.module}/inbound-policy.xml.tftpl", {
    tenant_id          = var.tenant_id
    audiences          = sort(tolist(setunion(toset([var.caller_audience]), var.additional_caller_audiences)))
    allowed_client_ids = sort(tolist(var.allowed_caller_client_ids))
  })
}
