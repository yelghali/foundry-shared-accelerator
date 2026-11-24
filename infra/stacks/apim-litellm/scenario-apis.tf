variable "scenario_backend_urls" {
  description = "Additional isolated LiteLLM profile endpoints, populated after workload deployment."
  type        = map(string)
  default     = {}
  validation {
    condition = alltrue([
      for profile, url in var.scenario_backend_urls :
      contains(["dedicated-application", "user-obo"], profile) && startswith(url, "https://")
    ])
    error_message = "Use dedicated-application/user-obo profile names and HTTPS backend URLs."
  }
}
variable "scenario_development_self_signed_tls" {
  description = "Per-profile development TLS overrides, allowing independent trusted-certificate migration."
  type        = map(bool)
  default     = {}
}
variable "user_obo_gateway_audience" {
  description = "Actual delegated gateway JWT audience (app GUID for the v2 Entra application)."
  type        = string
  default     = null
  validation {
    condition     = !contains(keys(var.scenario_backend_urls), "user-obo") || try(length(var.user_obo_gateway_audience) > 0, false)
    error_message = "Publishing user-obo requires its dedicated user_obo_gateway_audience from the Entra receipt."
  }
}

resource "azurerm_api_management_backend" "scenario" {
  for_each            = var.scenario_backend_urls
  name                = "litellm-${each.key}"
  resource_group_name = azurerm_resource_group.gateway.name
  api_management_name = azurerm_api_management.gateway.name
  protocol            = "http"
  url                 = each.value
  tls {
    validate_certificate_chain = !lookup(var.scenario_development_self_signed_tls, each.key, var.development_self_signed_tls)
    validate_certificate_name  = !lookup(var.scenario_development_self_signed_tls, each.key, var.development_self_signed_tls)
  }
}

resource "azurerm_api_management_api" "scenario" {
  for_each              = var.scenario_backend_urls
  name                  = "litellm-${each.key}"
  resource_group_name   = azurerm_resource_group.gateway.name
  api_management_name   = azurerm_api_management.gateway.name
  revision              = "1"
  display_name          = "LiteLLM ${each.key}"
  path                  = "ai-${each.key}"
  protocols             = ["https"]
  subscription_required = false
  service_url           = each.value
}

resource "azurerm_api_management_api_operation" "scenario" {
  for_each = {
    for pair in setproduct(keys(var.scenario_backend_urls), ["GET", "POST", "DELETE"]) :
    "${pair[0]}-${pair[1]}" => { profile = pair[0], method = pair[1] }
  }
  operation_id        = lower(each.value.method)
  api_name            = azurerm_api_management_api.scenario[each.value.profile].name
  api_management_name = azurerm_api_management.gateway.name
  resource_group_name = azurerm_resource_group.gateway.name
  display_name        = "${each.value.method} protocol passthrough"
  method              = each.value.method
  url_template        = "/*"
}

resource "azurerm_api_management_api_policy" "scenario" {
  for_each            = var.scenario_backend_urls
  api_name            = azurerm_api_management_api.scenario[each.key].name
  api_management_name = azurerm_api_management.gateway.name
  resource_group_name = azurerm_resource_group.gateway.name
  xml_content = templatefile("${path.module}/scenario-policy.xml.tftpl", {
    path              = "ai-${each.key}"
    backend_id        = azurerm_api_management_backend.scenario[each.key].name
    user_obo          = each.key == "user-obo"
    tenant_id         = var.tenant_id
    user_obo_audience = var.user_obo_gateway_audience != null ? var.user_obo_gateway_audience : ""
  })
}
