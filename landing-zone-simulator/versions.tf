terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 5.9" }
    azapi   = { source = "Azure/azapi", version = "~> 2.13" }
  }
}
provider "azapi" {
  subscription_id = var.subscription_id
}
provider "azurerm" {
  features {}
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"
}
