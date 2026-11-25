terraform {
  required_version = ">= 1.9"
  backend "local" {}
  required_providers {
    azapi = { source = "Azure/azapi", version = "~> 2.8" }
  }
}

variable "subscription_id" { type = string }
variable "tenant_id" { type = string }
variable "model_account_id" {
  type = string
  validation {
    condition     = can(regex("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.CognitiveServices/accounts/[^/]+$", var.model_account_id))
    error_message = "Supply the ARM ID of an existing Foundry model account."
  }
}
variable "model_endpoint" {
  type = string
  validation {
    condition     = can(regex("^https://[a-zA-Z0-9-]+\\.openai\\.azure\\.com/?$", var.model_endpoint))
    error_message = "Supply the existing account's Azure OpenAI HTTPS endpoint."
  }
}
variable "deployment_name" {
  type    = string
  default = "gpt-4o-mini"
}
variable "model_name" {
  type    = string
  default = "gpt-4o-mini"
}
variable "model_version" {
  type    = string
  default = "2024-07-18"
}
variable "model_sku" {
  type    = string
  default = "GlobalStandard"
}
variable "capacity" {
  type    = number
  default = 1
  validation {
    condition     = var.capacity >= 1 && floor(var.capacity) == var.capacity
    error_message = "Capacity must be a positive integer supported by the selected model SKU."
  }
}

provider "azapi" {
  subscription_id = var.subscription_id
  tenant_id       = var.tenant_id
}

resource "azapi_resource" "model" {
  type      = "Microsoft.CognitiveServices/accounts/deployments@2025-06-01"
  parent_id = var.model_account_id
  name      = var.deployment_name
  body = {
    sku = { name = var.model_sku, capacity = var.capacity }
    properties = {
      model                = { format = "OpenAI", name = var.model_name, version = var.model_version }
      versionUpgradeOption = "NoAutoUpgrade"
    }
  }
  lifecycle {
    precondition {
      condition     = split("/", var.model_account_id)[2] == var.subscription_id
      error_message = "The model account must belong to the explicitly selected subscription."
    }
  }
}

output "model_contract" {
  value = {
    model_account_id = var.model_account_id
    model_endpoint   = var.model_endpoint
    model_deployment = var.deployment_name
    model_name       = var.model_name
    model_version    = var.model_version
    model_sku        = var.model_sku
  }
}
