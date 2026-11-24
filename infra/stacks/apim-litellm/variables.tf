variable "subscription_id" { type = string }
variable "tenant_id" { type = string }
variable "name" {
  description = "Globally unique lowercase prefix (6-16 characters; hyphens allowed)."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,14}[a-z0-9]$", var.name))
    error_message = "Use 6-16 lowercase letters, digits or hyphens, starting with a letter and ending with a letter or digit."
  }
}
variable "location" {
  type    = string
  default = "swedencentral"
}
variable "publisher_email" { type = string }
variable "publisher_name" {
  type    = string
  default = "Gateway demo"
}
variable "aks_admin_group_object_ids" {
  description = "Entra groups granted AKS cluster administrator access; kubelogin is required."
  type        = list(string)
  default     = []
}
variable "operator_object_id" {
  description = "Deployment operator granted AKS Azure RBAC Cluster Admin and demo Blob upload access."
  type        = string
}
variable "caller_audience" {
  description = "Dedicated Entra API audience accepted by APIM, e.g. api://<gateway-app-id>."
  type        = string
  validation {
    condition     = startswith(var.caller_audience, "api://") || (var.allow_development_management_audience && var.caller_audience == "https://management.azure.com")
    error_message = "Use a dedicated api:// application audience, or explicitly enable the development management audience."
  }
}
variable "allowed_caller_client_ids" {
  description = "Entra client application IDs permitted to call APIM."
  type        = set(string)
  validation {
    condition     = length(var.allowed_caller_client_ids) > 0
    error_message = "At least one allowed caller application ID is required."
  }
}
variable "additional_caller_audiences" {
  description = "Additional dedicated API audiences, including the user-obo app registration."
  type        = set(string)
  default     = []
}
variable "litellm_token_audience" {
  description = "Deprecated alias for mcp_service_token_audience. APIM no longer replaces caller tokens."
  type        = string
  default     = null
}
variable "mcp_service_token_audience" {
  description = "Resource/App ID URI accepted by FastMCP for service-identity calls in A/B."
  type        = string
  default     = null
  validation {
    condition     = var.mcp_service_token_audience != null || var.litellm_token_audience != null
    error_message = "Supply mcp_service_token_audience (or its deprecated litellm_token_audience alias)."
  }
}
variable "allow_development_management_audience" {
  description = "DEVELOPMENT ONLY: accept Azure management audience at APIM instead of a dedicated application audience."
  type        = bool
  default     = false
}
variable "model_account_id" {
  description = "Existing Microsoft.CognitiveServices/accounts resource ID. No model resources are changed."
  type        = string
  validation {
    condition     = can(regex("(?i)/providers/Microsoft.CognitiveServices/accounts/[^/]+$", var.model_account_id))
    error_message = "Supply a Cognitive Services account ARM ID."
  }
}
variable "foundry_project_id" {
  description = "Existing Foundry Microsoft.CognitiveServices/accounts/projects resource ID."
  type        = string
  validation {
    condition     = can(regex("(?i)/providers/Microsoft.CognitiveServices/accounts/[^/]+/projects/[^/]+$", var.foundry_project_id))
    error_message = "Supply a Foundry account project ARM ID."
  }
}
variable "vnet_address_space" {
  type    = list(string)
  default = ["10.240.0.0/16"]
}
variable "aks_subnet_cidr" {
  type    = string
  default = "10.240.0.0/20"
}
variable "apim_subnet_cidr" {
  type    = string
  default = "10.240.16.0/24"
}
variable "private_endpoint_subnet_cidr" {
  type    = string
  default = "10.240.17.0/24"
}
variable "existing_vnet_id" {
  description = "Optional same-subscription VNet containing model/Foundry private endpoints. Creates both peerings."
  type        = string
  default     = null
}
variable "private_dns_zone_ids" {
  description = "Existing private DNS zones to link to this VNet, keyed by stable labels."
  type        = map(string)
  default     = {}
}
variable "node_count" {
  type    = number
  default = 2
}
variable "node_vm_size" {
  type    = string
  default = "Standard_D4s_v7"
}
variable "tags" {
  type    = map(string)
  default = {}
}
