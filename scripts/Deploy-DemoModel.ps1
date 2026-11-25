<#
.SYNOPSIS
Plans or deploys the demo model in an existing Foundry account using separate Terraform state.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][guid]$TenantId,
    [Parameter(Mandatory)][string]$ModelAccountId,
    [Parameter(Mandatory)][ValidatePattern('^https://[a-zA-Z0-9-]+\.openai\.azure\.com/?$')][string]$ModelEndpoint,
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$')][string]$DeploymentName = "gpt-4o-mini",
    [string]$ModelName = "gpt-4o-mini",
    [string]$ModelVersion = "2024-07-18",
    [string]$ModelSku = "GlobalStandard",
    [ValidateRange(1, 100000)][int]$Capacity = 1,
    [switch]$Apply
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($ModelAccountId -notmatch "^/subscriptions/$SubscriptionId/resourceGroups/[^/]+/providers/Microsoft.CognitiveServices/accounts/[^/]+$") {
    throw "ModelAccountId must identify an existing account in SubscriptionId."
}
$root = Split-Path -Parent $PSScriptRoot
$stack = Join-Path $root "infra\stacks\demo-foundry-model"
$accountName = $ModelAccountId.Split('/')[-1]
$directory = Join-Path $root ".local\demo-foundry-model\$accountName\$DeploymentName"
New-Item -ItemType Directory -Force $directory | Out-Null
$variables = Join-Path $directory "model.tfvars.json"
$state = Join-Path $directory "terraform.tfstate"
$plan = Join-Path $directory "model.tfplan"
@{
    subscription_id = "$SubscriptionId"; tenant_id = "$TenantId"
    model_account_id = $ModelAccountId; model_endpoint = $ModelEndpoint
    deployment_name = $DeploymentName; model_name = $ModelName
    model_version = $ModelVersion; model_sku = $ModelSku; capacity = $Capacity
} | ConvertTo-Json | Set-Content -LiteralPath $variables -Encoding utf8
$previousData = $env:TF_DATA_DIR
try {
    $env:TF_DATA_DIR = Join-Path $directory ".terraform"
    & terraform "-chdir=$stack" init -input=false "-backend-config=path=$state"
    if ($LASTEXITCODE -ne 0) { throw "Demo model Terraform initialization failed." }
    & terraform "-chdir=$stack" plan -input=false -no-color "-var-file=$variables" "-out=$plan"
    if ($LASTEXITCODE -ne 0) { throw "Demo model plan failed; check model availability, quota and account permissions." }
    $changes = & terraform "-chdir=$stack" show -json $plan
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect the demo model plan." }
    $parsed = $changes | ConvertFrom-Json
    if ($null -ne $parsed.PSObject.Properties["resource_changes"] -and
        @($parsed.resource_changes | Where-Object { "delete" -in $_.change.actions }).Count) {
        throw "Model deletion/replacement is not applied automatically. Review an explicit migration."
    }
    if (-not $Apply) { Write-Host "Plan only. Review before repeating with -Apply."; return }
    & terraform "-chdir=$stack" apply -input=false -no-color $plan
    if ($LASTEXITCODE -ne 0) { throw "Demo model deployment failed." }
    $contract = & terraform "-chdir=$stack" output -json model_contract
    if ($LASTEXITCODE -ne 0) { throw "Cannot export the deployed model contract." }
    $contract | Set-Content -LiteralPath (Join-Path $directory "model.json") -Encoding utf8
    Write-Host "Model deployed. Nonsecret inputs for Deploy-GatewayScenarios.ps1: $(Join-Path $directory 'model.json')"
}
finally { $env:TF_DATA_DIR = $previousData }
