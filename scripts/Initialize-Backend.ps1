[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Stack,
    [string]$BackendConfigFile,
    [string]$LocalStatePath
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$override = Join-Path $Stack "local_backend_override.tf"
if ($BackendConfigFile) {
    if (-not (Test-Path -LiteralPath $BackendConfigFile)) { throw "Backend configuration file is missing." }
    if (Test-Path -LiteralPath $override) {
        throw "Refusing remote initialization while the local backend override is present: $override"
    }
    & terraform "-chdir=$Stack" init -input=false -reconfigure "-backend-config=$BackendConfigFile"
} else {
    $content = "terraform {`n  backend `"local`" {}`n}`n"
    if (Test-Path -LiteralPath $override) {
        if ((Get-Content -LiteralPath $override -Raw) -ne $content) {
            throw "Unexpected local backend override: $override"
        }
    } else {
        Set-Content -LiteralPath $override -Value $content -NoNewline -Encoding utf8NoBOM
    }
    $arguments = @("-chdir=$Stack", "init", "-input=false", "-reconfigure")
    if ($LocalStatePath) { $arguments += "-backend-config=path=$LocalStatePath" }
    & terraform @arguments
}
if ($LASTEXITCODE -ne 0) { throw "Terraform backend initialization failed." }
