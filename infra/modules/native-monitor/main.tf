terraform {
  required_providers {
    azurerm = { source = "hashicorp/azurerm" }
    azapi   = { source = "Azure/azapi" }
  }
}
variable "prefix" { type = string }
variable "resource_group_id" { type = string }
variable "location" { type = string }
variable "application_insights_id" { type = string }
variable "application_id" { type = string }
variable "log_analytics_id" { type = string }
variable "gateway_principal_id" { type = string }
variable "reader_principal_id" { type = string }
resource "azapi_resource" "metrics" {
  type                      = "Microsoft.Monitor/accounts@2023-04-03"
  name                      = "amw-${var.prefix}"
  parent_id                 = var.resource_group_id
  location                  = var.location
  schema_validation_enabled = false
  body                      = { properties = { publicNetworkAccess = "Enabled" } }
  response_export_values    = ["properties.metrics.prometheusQueryEndpoint"]
}
resource "azapi_resource" "endpoint" {
  type                      = "Microsoft.Insights/dataCollectionEndpoints@2023-03-11"
  name                      = "dce-${var.prefix}"
  parent_id                 = var.resource_group_id
  location                  = var.location
  schema_validation_enabled = false
  body                      = { properties = { networkAcls = { publicNetworkAccess = "Enabled" } } }
  response_export_values    = ["properties.metricsIngestion.endpoint", "properties.logsIngestion.endpoint"]
}
resource "azapi_resource" "rule" {
  type                      = "Microsoft.Insights/dataCollectionRules@2025-05-11"
  name                      = "dcr-${var.prefix}"
  parent_id                 = var.resource_group_id
  location                  = var.location
  schema_validation_enabled = false
  body = {
    properties = {
      dataCollectionEndpointId = azapi_resource.endpoint.id
      references = {
        applicationInsights = [{ name = "application", resourceId = var.application_insights_id }]
      }
      directDataSources = {
        otelMetrics = [{
          name                         = "metrics"
          streams                      = ["Microsoft-OtelMetrics"]
          enrichWithReference          = "application"
          enrichWithResourceAttributes = ["cloud.region", "service.name", "service.version", "service.instance.id", "service.namespace"]
        }]
        otelLogs = [{
          name                           = "logs"
          streams                        = ["Microsoft-OTel-Logs"]
          enrichWithReference            = "application"
          replaceResourceIdWithReference = true
        }]
        otelTraces = [{
          name                           = "traces"
          streams                        = ["Microsoft-OTel-Traces-Events", "Microsoft-OTel-Traces-Spans", "Microsoft-OTel-Traces-Resources"]
          enrichWithReference            = "application"
          replaceResourceIdWithReference = true
        }]
      }
      destinations = {
        monitoringAccounts = [{ name = "metrics", accountResourceId = azapi_resource.metrics.id }]
        logAnalytics       = [{ name = "logs", workspaceResourceId = var.log_analytics_id }]
      }
      dataFlows = [
        { streams = ["Microsoft-OtelMetrics"], destinations = ["metrics"] },
        { streams = ["Microsoft-OTel-Logs", "Microsoft-OTel-Traces-Events", "Microsoft-OTel-Traces-Spans", "Microsoft-OTel-Traces-Resources"], destinations = ["logs"] }
      ]
    }
  }
  response_export_values = ["properties.immutableId"]
}
resource "azurerm_role_assignment" "publisher" {
  scope                            = azapi_resource.rule.id
  role_definition_name             = "Monitoring Metrics Publisher"
  principal_id                     = var.gateway_principal_id
  skip_service_principal_aad_check = true
}
resource "azurerm_role_assignment" "reader" {
  scope                = azapi_resource.metrics.id
  role_definition_name = "Monitoring Data Reader"
  principal_id         = var.reader_principal_id
}
output "otlp" {
  value = {
    rule_id              = azapi_resource.rule.id
    metrics_workspace_id = azapi_resource.metrics.id
    query_endpoint       = azapi_resource.metrics.output.properties.metrics.prometheusQueryEndpoint
    metrics_endpoint     = "${azapi_resource.endpoint.output.properties.metricsIngestion.endpoint}/dataCollectionRules/${azapi_resource.rule.output.properties.immutableId}/streams/Microsoft-OtelMetrics/otlp/v1/metrics"
    logs_endpoint        = "${azapi_resource.endpoint.output.properties.logsIngestion.endpoint}/dataCollectionRules/${azapi_resource.rule.output.properties.immutableId}/streams/Microsoft-OTLP-Logs/otlp/v1/logs"
    traces_endpoint      = "${azapi_resource.endpoint.output.properties.logsIngestion.endpoint}/dataCollectionRules/${azapi_resource.rule.output.properties.immutableId}/streams/Microsoft-OTLP-Traces/otlp/v1/traces"
  }
}
