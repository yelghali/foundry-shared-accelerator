<#
.SYNOPSIS
Tests actual user OBO for MCP and agent invocation. No operator-token or managed-identity fallback.
#>
[CmdletBinding()]
param(
    [string]$DeploymentFile = ".local\apim-litellm\deployment.json",
    [string]$IdentityFile = ".local\apim-litellm\entra-identity-user-consent.json",
    [string]$UserTokenCache = ".local\apim-litellm\user-obo\user-token-cache.bin",
    [string]$PythonExecutable
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $PythonExecutable) {
    $venv = Join-Path $root ".venv\Scripts\python.exe"
    $PythonExecutable = if (Test-Path $venv) { $venv } else { "python" }
}
Push-Location $root
try {
    & $PythonExecutable -m samples.test_delegated_user_identity --deployment $DeploymentFile `
        --identity $IdentityFile --user-token-cache $UserTokenCache
    if ($LASTEXITCODE -ne 0) { throw "Delegated user identity tests FAILED. Inspect consent/login and the explicit failing check." }
}
finally { Pop-Location }
