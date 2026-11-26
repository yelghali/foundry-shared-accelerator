terraform {
  required_version = ">= 1.9"
  backend "local" {
    path = "../../../../.local/apim-litellm/terraform.tfstate"
  }
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 5.9" }
  }
}

provider "azurerm" {
  features {}
  subscription_id                 = var.subscription_id
  tenant_id                       = var.tenant_id
  resource_provider_registrations = "none"
}
