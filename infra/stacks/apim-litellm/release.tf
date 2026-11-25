variable "litellm_backend_url" {
  description = "HTTPS AKS endpoint; set after the workload LoadBalancer is ready."
  type        = string
  default     = null
  validation {
    condition     = var.litellm_backend_url == null ? true : startswith(var.litellm_backend_url, "https://")
    error_message = "LiteLLM must use HTTPS, including the public development profile."
  }
}

variable "development_self_signed_tls" {
  description = "Explicit development-only bypass of backend certificate trust/name validation."
  type        = bool
  default     = false
}

resource "azurerm_api_management_backend" "litellm" {
  count               = var.litellm_backend_url == null ? 0 : 1
  name                = "litellm"
  resource_group_name = azurerm_resource_group.gateway.name
  api_management_name = azurerm_api_management.gateway.name
  protocol            = "http"
  url                 = var.litellm_backend_url
  tls {
    validate_certificate_chain = !var.development_self_signed_tls
    validate_certificate_name  = !var.development_self_signed_tls
  }
}

resource "azurerm_api_management_api" "litellm" {
  count                 = var.litellm_backend_url == null ? 0 : 1
  name                  = "litellm"
  resource_group_name   = azurerm_resource_group.gateway.name
  api_management_name   = azurerm_api_management.gateway.name
  revision              = "1"
  display_name          = "LiteLLM models MCP and A2A"
  path                  = "ai"
  protocols             = ["https"]
  subscription_required = false
  service_url           = var.litellm_backend_url
}

resource "azurerm_api_management_api_operation" "passthrough" {
  for_each            = var.litellm_backend_url == null ? toset([]) : toset(["GET", "POST", "DELETE"])
  operation_id        = lower(each.key)
  api_name            = azurerm_api_management_api.litellm[0].name
  api_management_name = azurerm_api_management.gateway.name
  resource_group_name = azurerm_resource_group.gateway.name
  display_name        = "${each.key} protocol passthrough"
  method              = each.key
  url_template        = "/*"
}

resource "azurerm_api_management_api_policy" "litellm" {
  count               = var.litellm_backend_url == null ? 0 : 1
  api_name            = azurerm_api_management_api.litellm[0].name
  api_management_name = azurerm_api_management.gateway.name
  resource_group_name = azurerm_resource_group.gateway.name
  xml_content         = <<-XML
    <policies>
      <inbound>
        <base />
        <choose>
          <when condition='@(!System.Text.RegularExpressions.Regex.IsMatch(context.Request.OriginalUrl.Path, "^/ai/(v1/(chat/completions|responses|models)|blob/mcp|a2a/(echo|declarative|hosted)(/\\.well-known/agent(-card)?\\.json)?)/?$"))'>
            <return-response>
              <set-status code="403" reason="Only published AI routes are allowed" />
            </return-response>
          </when>
          <when condition='@(!context.Request.Headers.ContainsKey("x-litellm-api-key"))'>
            <return-response>
              <set-status code="401" reason="LiteLLM virtual key required" />
            </return-response>
          </when>
        </choose>
        <set-backend-service backend-id="${azurerm_api_management_backend.litellm[0].name}" />
        <set-header name="x-litellm-user-id" exists-action="delete" />
        <set-header name="x-litellm-team-id" exists-action="delete" />
        <set-header name="x-litellm-user-role" exists-action="delete" />
        <set-header name="x-a2a-echo-authorization" exists-action="delete" />
        <set-header name="x-a2a-declarative-authorization" exists-action="delete" />
        <set-header name="x-a2a-hosted-authorization" exists-action="delete" />
      </inbound>
      <backend><forward-request timeout="240" buffer-response="false" /></backend>
      <outbound><base /></outbound>
      <on-error><base /></on-error>
    </policies>
  XML
}
