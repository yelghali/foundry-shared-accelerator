[CmdletBinding()]
param(
    [ValidateSet("shared-service", "dedicated-application", "user-obo")]
    [string[]]$Scenarios = @("shared-service", "dedicated-application", "user-obo"),
    [string]$DeploymentFile = ".local\apim-litellm\deployment.json",
    [string]$IdentityFile,
    [string]$UserTokenCache,
    [switch]$Live,
    [switch]$UseAzd,
    [switch]$PrivateDemo
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$previousKubeconfig = $env:KUBECONFIG
Push-Location $root
try {
    & python -m pytest demos\apim_litellm\tests demos\apim_litellm\gateway\tests -q
    if ($LASTEXITCODE -ne 0) { throw "Identity-scenario unit tests failed." }
    $stack = Join-Path $root "demos\apim_litellm\infra\apim-litellm"
    & terraform "-chdir=$stack" validate -no-color
    if ($LASTEXITCODE -ne 0) { throw "Identity-scenario infrastructure validation failed." }
    & terraform "-chdir=$stack" test -no-color
    if ($LASTEXITCODE -ne 0) { throw "Identity-scenario infrastructure tests failed." }
    if ($Live) {
        $env:KUBECONFIG = Join-Path $root ".local\apim-litellm\kubeconfig"
        foreach ($scenario in $Scenarios) {
            $arguments = @("-m", "demos.apim_litellm.operator.apim_litellm", "verify", "--deployment", $DeploymentFile,
                "--scenario", $scenario, "--permissions", "--declarative",
                "--output", ".local\apim-litellm\$scenario\acceptance.json")
            if ($IdentityFile) { $arguments += @("--identity", $IdentityFile) }
            if ($UserTokenCache -and $scenario -eq "user-obo") { $arguments += @("--user-token-cache", $UserTokenCache) }
            if ($UseAzd) { $arguments += "--use-azd" }
            if ($PrivateDemo) { $arguments += "--private-demo" }
            if ($scenario -eq "shared-service" -and "dedicated-application" -in $Scenarios) {
                $arguments += @("--reject-key-from", "dedicated-application")
            }
            elseif ($scenario -eq "dedicated-application" -and "shared-service" -in $Scenarios) {
                $arguments += @("--reject-key-from", "shared-service")
            }
            & python @arguments
            if ($LASTEXITCODE -ne 0) {
                throw "$scenario failed live acceptance. No success is claimed; inspect the explicit error and required admin actions."
            }
        }
    }
}
finally {
    $env:KUBECONFIG = $previousKubeconfig
    Pop-Location
}
