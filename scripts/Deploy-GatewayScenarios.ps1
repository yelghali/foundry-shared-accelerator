[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$VariablesFile,
    [Parameter(Mandatory)][string]$FoundryA2AEndpoint,
    [Parameter(Mandatory)][string]$DeclarativeA2AEndpoint,
    [Parameter(Mandatory)][string]$ModelEndpoint,
    [string]$ModelDeployment = "gpt-4o-mini",
    [string]$IdentityFile,
    [ValidateSet("shared-service", "dedicated-application", "user-obo")]
    [string[]]$Scenarios = @("shared-service", "dedicated-application", "user-obo"),
    [string]$ImageTag = "20261124h",
    [ValidateSet("public", "private")][string]$Exposure = "public",
    [ValidateSet("azurecli", "azd")][string]$KubernetesLoginMethod = "azurecli",
    [switch]$SkipBuild,
    [switch]$Apply
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$variablesPath = (Resolve-Path $VariablesFile).Path
$settings = Get-Content -LiteralPath $variablesPath -Raw | ConvertFrom-Json
$local = Join-Path $root ".local\apim-litellm"
New-Item -ItemType Directory -Force $local | Out-Null
$scenarioVariables = Join-Path $local "identity-scenarios.tfvars.json"
$extra = if (Test-Path $scenarioVariables) {
    Get-Content -LiteralPath $scenarioVariables -Raw | ConvertFrom-Json -AsHashtable
} else { @{} }
$extra.enable_identity_scenarios = $true
if ($IdentityFile) {
    $IdentityFile = (Resolve-Path $IdentityFile).Path
    $identity = Get-Content -LiteralPath $IdentityFile -Raw | ConvertFrom-Json
    if ($identity.tenant_id -ne $settings.tenant_id) { throw "Identity receipt and infrastructure tenant differ." }
    $previousAudiences = if ($extra.ContainsKey("additional_caller_audiences")) { @($extra.additional_caller_audiences) } else { @() }
    $previousClients = if ($extra.ContainsKey("allowed_caller_client_ids")) { @($extra.allowed_caller_client_ids) } else { @() }
    $extra.additional_caller_audiences = @($previousAudiences + $identity.gateway.app_id + $identity.gateway.audience | Sort-Object -Unique)
    $extra.user_obo_gateway_audience = $identity.gateway.app_id
    $extra.allowed_caller_client_ids = @($previousClients + $settings.allowed_caller_client_ids + $identity.test_client.app_id | Sort-Object -Unique)
}
elseif ("user-obo" -in $Scenarios -and $Apply) {
    throw "user-obo requires -IdentityFile from Initialize-GatewayIdentity.ps1. Consent is a separate explicit administrator action."
}
if ($Exposure -eq "private" -and $Apply) {
    throw "For private migration, use Deploy-APIMLiteLLM.ps1 per scenario with trusted TLS and -BackendHostname; do not infer private DNS."
}
$extra | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $scenarioVariables -Encoding utf8
$common = @{
    VariablesFile = $variablesPath; ScenarioVariablesFile = $scenarioVariables
    FoundryA2AEndpoint = $FoundryA2AEndpoint; DeclarativeA2AEndpoint = $DeclarativeA2AEndpoint
    ModelEndpoint = $ModelEndpoint; ModelDeployment = $ModelDeployment
    IdentityFile = $IdentityFile; ImageTag = $ImageTag; Exposure = $Exposure
    KubernetesLoginMethod = $KubernetesLoginMethod
}
& (Join-Path $PSScriptRoot "Deploy-APIMLiteLLM.ps1") @common -Stage infrastructure -Apply:$Apply
if (-not $?) { throw "Identity-scenario infrastructure failed." }
if (-not $Apply) { return }
$reuseImages = $SkipBuild
foreach ($scenario in $Scenarios) {
    & (Join-Path $PSScriptRoot "Deploy-APIMLiteLLM.ps1") @common -Scenario $scenario -Stage workloads -Apply -SkipBuild:$reuseImages
    if (-not $?) { throw "$scenario workload deployment failed." }
    $reuseImages = $true
    & (Join-Path $PSScriptRoot "Deploy-APIMLiteLLM.ps1") @common -Scenario $scenario -Stage release -Apply
    if (-not $?) { throw "$scenario APIM release failed." }
}
