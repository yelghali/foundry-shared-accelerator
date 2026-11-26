<#
.SYNOPSIS
Deploys the demo's hosted A2A Echo into an existing Foundry project; never provisions a project.
.DESCRIPTION
Without -Apply prints the intended deployment only. Requires the azd and agent
extension versions declared in the sample azure.yaml. The operator needs project
deployment permissions and the subscription's hosted A2A preview enabled.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][guid]$SubscriptionId,
    [Parameter(Mandatory)][string]$ProjectId,
    [Parameter(Mandatory)][ValidatePattern('^https://[a-zA-Z0-9-]+\.services\.ai\.azure\.com/api/projects/[a-zA-Z0-9_-]+/?$')][string]$ProjectEndpoint,
    [ValidateSet("eastus2", "swedencentral")][string]$Location = "eastus2",
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9-]{0,63}$')][string]$EnvironmentName = "gateway-demo",
    [switch]$Apply
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($ProjectId -notmatch "^/subscriptions/$SubscriptionId/resourceGroups/[^/]+/providers/Microsoft.CognitiveServices/accounts/[^/]+/projects/([^/]+)$") {
    throw "ProjectId must identify an existing Foundry project in SubscriptionId."
}
$projectName = $Matches[1]
if (([uri]$ProjectEndpoint).AbsolutePath.TrimEnd('/').Split('/')[-1] -ne $projectName) {
    throw "ProjectEndpoint and ProjectId must identify the same project name."
}
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$sample = Join-Path $root "demos\apim_litellm\samples\hosted-a2a-echo"
$directory = Join-Path $root ".local\demo-hosted-agent\$EnvironmentName"
$output = Join-Path $directory "hosted-agent.json"
if (-not $Apply) {
    @{
        mode = "plan"; existing_project_id = $ProjectId
        project_endpoint = $ProjectEndpoint.TrimEnd('/')
        environment = $EnvironmentName; agent_name = "gateway-a2a-echo"
        protocols = @("a2a", "responses"); creates_project = $false
        output = $output; writes_performed = $false
    } | ConvertTo-Json
    return
}
$previousPreview = $env:AZD_ALPHA_ENABLE_AGENTS_A2A
Push-Location $sample
try {
    $env:AZD_ALPHA_ENABLE_AGENTS_A2A = "true"
    $environmentFile = Join-Path $sample ".azure\$EnvironmentName\.env"
    if (Test-Path -LiteralPath $environmentFile) {
        $line = Get-Content -LiteralPath $environmentFile | Where-Object { $_ -match '^FOUNDRY_PROJECT_ID=' }
        if (-not $line -or ($line -replace '^FOUNDRY_PROJECT_ID=', '').Trim('"') -ne $ProjectId) {
            throw "Existing azd environment targets another/unknown project. Choose a new EnvironmentName."
        }
        & azd env select $EnvironmentName
    }
    else {
        & azd env new $EnvironmentName --subscription "$SubscriptionId" --location $Location --no-prompt
    }
    if ($LASTEXITCODE -ne 0) { throw "Cannot initialize/select the hosted demo azd environment." }
    foreach ($item in @(@("FOUNDRY_PROJECT_ID", $ProjectId), @("FOUNDRY_PROJECT_ENDPOINT", $ProjectEndpoint.TrimEnd('/')))) {
        & azd env set $item[0] $item[1]
        if ($LASTEXITCODE -ne 0) { throw "Cannot configure the existing Foundry target." }
    }
    & azd deploy echo --no-prompt
    if ($LASTEXITCODE -ne 0) { throw "Hosted A2A deployment failed; no success receipt written." }
    $details = & azd ai agent show echo --output json
    if ($LASTEXITCODE -ne 0 -or -not ($details | ConvertFrom-Json)) {
        throw "Cannot verify the deployed hosted agent; no success receipt written."
    }
    New-Item -ItemType Directory -Force $directory | Out-Null
    $endpoint = "$($ProjectEndpoint.TrimEnd('/'))/agents/gateway-a2a-echo/endpoint/protocols/a2a"
    @{
        status = "deployed"; existing_project_id = $ProjectId
        project_endpoint = $ProjectEndpoint.TrimEnd('/'); agent_name = "gateway-a2a-echo"
        a2a_endpoint = $endpoint; agent_card_endpoint = "$endpoint/agentCard/v1.0"
        protocols = @("a2a", "responses"); environment = $EnvironmentName
        invocation_tested = $false
    } | ConvertTo-Json | Set-Content -LiteralPath $output -Encoding utf8
    Write-Host "Hosted agent deployed to existing Foundry. Receipt: $output"
    Write-Host "FoundryA2AEndpoint: $endpoint"
    Write-Host "Run Test-GatewayScenarios.ps1 after gateway deployment to verify invocation."
}
finally {
    $env:AZD_ALPHA_ENABLE_AGENTS_A2A = $previousPreview
    Pop-Location
}
