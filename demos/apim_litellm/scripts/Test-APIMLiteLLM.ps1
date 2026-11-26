[CmdletBinding()]
param(
    [switch]$Live,
    [string]$DeploymentFile = ".local\apim-litellm\deployment.json",
    [string]$OutputFile = ".local\apim-litellm\acceptance.json"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
Push-Location $root
try {
    & python -m pytest demos\apim_litellm\gateway\tests demos\apim_litellm\tests\test_apim_litellm_acceptance.py demos\apim_litellm\tests\test_gateway_keys.py demos\apim_litellm\tests\test_entra_setup.py demos\apim_litellm\tests\test_gateway_agents.py demos\apim_litellm\tests\test_consume_gateway.py -q
    if ($LASTEXITCODE -ne 0) { throw "Scenario runtime/acceptance regression tests failed." }
    $stack = Join-Path $root "demos\apim_litellm\infra\apim-litellm"
    & terraform "-chdir=$stack" fmt -check
    if ($LASTEXITCODE -ne 0) { throw "Scenario Terraform formatting failed." }
    & terraform "-chdir=$stack" validate -no-color
    if ($LASTEXITCODE -ne 0) { throw "Scenario Terraform validation failed; run terraform init first." }
    & terraform "-chdir=$stack" test -no-color
    if ($LASTEXITCODE -ne 0) { throw "Scenario Terraform tests failed." }
    if ($Live) {
        & python -m demos.apim_litellm.operator.apim_litellm verify --deployment $DeploymentFile --output $OutputFile
        if ($LASTEXITCODE -ne 0) { throw "Azure scenario acceptance failed; no success receipt was written." }
    }
}
finally {
    Pop-Location
}
