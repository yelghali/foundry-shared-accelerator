<#
.SYNOPSIS
Tests the dedicated LiteLLM service identity, not passthrough of the consuming application's identity.
#>
[CmdletBinding()]
param(
    [string]$DeploymentFile = ".local\apim-litellm\deployment.json",
    [string]$PythonExecutable,
    [switch]$UseAzd
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
    $arguments = @("-m", "samples.test_dedicated_service_identity", "--deployment", $DeploymentFile)
    if ($UseAzd) { $arguments += "--use-azd" }
    & $PythonExecutable @arguments
    if ($LASTEXITCODE -ne 0) { throw "Dedicated service identity tests FAILED. Inspect the explicit failing check." }
}
finally { Pop-Location }
