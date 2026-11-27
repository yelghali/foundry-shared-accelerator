<#
.SYNOPSIS
Runs local demo tests. Does not authenticate to Azure or make live model/tool calls.
#>
[CmdletBinding()]
param([string]$PythonExecutable, [switch]$Terraform)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $PythonExecutable) {
    $venv = Join-Path $root ".venv\Scripts\python.exe"
    $PythonExecutable = if (Test-Path $venv) { $venv } else { "python" }
}
Push-Location $root
try {
    & $PythonExecutable -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Demo offline tests failed." }
    if ($Terraform) {
        foreach ($name in @("apim-litellm", "demo-foundry-model", "demo-private-foundry")) {
            $stack = Join-Path $root "infra\$name"
            & terraform "-chdir=$stack" validate -no-color
            if ($LASTEXITCODE -ne 0) { throw "$name validation failed. Initialize its providers first." }
        }
        & terraform "-chdir=$(Join-Path $root 'infra\apim-litellm')" test -no-color
        if ($LASTEXITCODE -ne 0) { throw "Demo gateway Terraform tests failed." }
    }
}
finally { Pop-Location }
