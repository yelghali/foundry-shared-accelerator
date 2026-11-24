[CmdletBinding()]
param(
    [switch]$Live,
    [string]$DeploymentFile = ".local\apim-litellm\deployment.json",
    [string]$OutputFile = ".local\apim-litellm\acceptance.json"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    & python -m pytest scenarios\apim-litellm\tests tests\test_apim_litellm_acceptance.py tests\test_gateway_keys.py tests\test_entra_setup.py tests\test_gateway_agents.py tests\test_consume_gateway.py -q
    if ($LASTEXITCODE -ne 0) { throw "Scenario runtime/acceptance regression tests failed." }
    $stack = Join-Path $root "infra\stacks\apim-litellm"
    & terraform "-chdir=$stack" fmt -check
    if ($LASTEXITCODE -ne 0) { throw "Scenario Terraform formatting failed." }
    & terraform "-chdir=$stack" validate -no-color
    if ($LASTEXITCODE -ne 0) { throw "Scenario Terraform validation failed; run terraform init first." }
    & terraform "-chdir=$stack" test -no-color
    if ($LASTEXITCODE -ne 0) { throw "Scenario Terraform tests failed." }
    if ($Live) {
        & python -m accelerator.apim_litellm verify --deployment $DeploymentFile --output $OutputFile
        if ($LASTEXITCODE -ne 0) { throw "Azure scenario acceptance failed; no success receipt was written." }
    }
}
finally {
    Pop-Location
}
