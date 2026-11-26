<#
.SYNOPSIS
Loads demo consumer credentials into the current PowerShell process without printing them.
.DESCRIPTION
Operator convenience: reads only the application's native virtual key from AKS.
Scenario C uses the explicitly selected encrypted user cache, never an operator
identity fallback. Run in your existing terminal, not a separate pwsh -File process.
#>
[CmdletBinding()]
param(
    [ValidateSet("shared-service", "dedicated-application", "user-obo")]
    [string]$Scenario = "shared-service",
    [string]$DeploymentFile = ".local\apim-litellm\deployment.json",
    [string]$IdentityFile = ".local\apim-litellm\entra-identity-user-consent.json",
    [string]$UserTokenCache = ".local\apim-litellm\user-obo\user-token-cache.bin",
    [string]$PythonExecutable,
    [switch]$UseAzd
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $PythonExecutable) {
    $localPython = Join-Path $root ".local\scenario-venv\Scripts\python.exe"
    $PythonExecutable = if (Test-Path -LiteralPath $localPython) { $localPython } else { "python" }
}
$previousPath = $env:PATH
$previousKubeconfig = $env:KUBECONFIG
$raw = $null
$bundle = $null
foreach ($name in @("LITELLM_VIRTUAL_KEY", "GATEWAY_ACCESS_TOKEN", "GATEWAY_ENDPOINT")) {
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
}
$code = @'
import json,sys
from pathlib import Path
from azure.identity import AzureCliCredential,AzureDeveloperCliCredential
from accelerator.control import load_json
from accelerator.entra_setup import cached_user_token
from accelerator.gateway_keys import read_key

try:
    deployment_path,scenario,identity_path,cache_path,use_azd=sys.argv[1:]
    deployment=load_json(deployment_path)
    if not Path(deployment_path).with_name("kubeconfig").is_file():
        raise ValueError("The demo operator kubeconfig is missing")
    key=read_key(scenario,"application")
    if scenario=="user-obo":
        receipt=load_json(identity_path)
        if receipt["tenant_id"]!=deployment["tenant_id"]:
            raise ValueError("The user cache and deployment tenants differ")
        token=cached_user_token(receipt,cache_path)
    else:
        credential_type=AzureDeveloperCliCredential if use_azd=="true" else AzureCliCredential
        with credential_type(tenant_id=deployment["tenant_id"],process_timeout=180) as identity:
            token=identity.get_token(deployment["apim"]["caller_audience"].rstrip("/")+"/.default").token
    suffix="ai" if scenario=="shared-service" else "ai-"+scenario
    endpoint=deployment["apim"]["gateway_url"].rstrip("/")+"/"+suffix
    print(json.dumps({"key":key,"token":token,"endpoint":endpoint}))
except Exception as error:
    print("Consumer credentials could not be loaded ("+type(error).__name__+
          "). Check the selected tenant login, AKS/operator access and kubeconfig. "
          "For user-obo, complete login with the selected identity receipt and encrypted cache. "
          "Credential details suppressed; no fallback is used.",file=sys.stderr)
    sys.exit(1)
'@
Push-Location $root
try {
    $tools = @(Join-Path $root ".local\tools"; Join-Path $root ".local\tools\azd")
    $env:PATH = ($tools -join [IO.Path]::PathSeparator) + [IO.Path]::PathSeparator + $previousPath
    $deploymentPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DeploymentFile)
    $env:KUBECONFIG = Join-Path (Split-Path -Parent $deploymentPath) "kubeconfig"
    $mode = if ($UseAzd) { "true" } else { "false" }
    $raw = & $PythonExecutable -c $code $deploymentPath $Scenario $IdentityFile $UserTokenCache $mode
    if ($LASTEXITCODE -ne 0) { throw "Consumer environment was not loaded. No previous consumer credentials were retained." }
    try { $bundle = ($raw -join "`n") | ConvertFrom-Json }
    catch { throw "Credential helper returned invalid data; contents suppressed." }
    foreach ($field in @("key", "token", "endpoint")) {
        if ($null -eq $bundle.PSObject.Properties[$field] -or
            $bundle.$field -isnot [string] -or [string]::IsNullOrWhiteSpace($bundle.$field)) {
            throw "Credential helper returned incomplete data; contents suppressed."
        }
    }
    $uri = $null
    if (-not $bundle.key.StartsWith("sk-") -or
        -not [uri]::TryCreate($bundle.endpoint, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne "https" -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) {
        throw "Credential helper returned invalid consumer configuration; contents suppressed."
    }
    $env:LITELLM_VIRTUAL_KEY = $bundle.key.Trim()
    $env:GATEWAY_ACCESS_TOKEN = $bundle.token.Trim()
    $env:GATEWAY_ENDPOINT = $bundle.endpoint.TrimEnd("/")
    Write-Host "Consumer environment loaded for $Scenario in this PowerShell session."
    Write-Host "Endpoint: $env:GATEWAY_ENDPOINT"
    Write-Host "Application virtual key and Entra token loaded (not printed). Rerun to refresh."
    Write-Host "Setup only: no model, MCP or agent inference was called."
    Write-Host "This replaces the previous scenario's credentials/endpoint in this terminal."
    if ($Scenario -eq "user-obo") {
        Write-Host "MCP: native LiteLLM Entra user OBO. Hosted/prompt agents (A2A and Responses): custom user OBO."
        Write-Host "Agent downstream model/MCP calls use the hosted runtime identity, not user OBO."
    }
    else {
        $identityDescription = if ($Scenario -eq "shared-service") { "shared gateway" } else { "dedicated app-alpha gateway" }
        Write-Host "MCP and agent calls use the $identityDescription workload identity, not user OBO."
    }
    Write-Host "Release 20261126a: no auth sidecar. A/B MCP auth is custom Python inside LiteLLM; C MCP uses native OBO."
    Write-Host ('Next (from repository root): & "' + $PythonExecutable + '" -m samples.consume_gateway --endpoint $env:GATEWAY_ENDPOINT --kind hosted-agent --agent-name hosted')
}
finally {
    $raw = $null
    $bundle = $null
    $env:PATH = $previousPath
    $env:KUBECONFIG = $previousKubeconfig
    Pop-Location
}
