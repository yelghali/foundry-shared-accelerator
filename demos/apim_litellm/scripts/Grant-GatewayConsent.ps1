<#
.SYNOPSIS
Plans or grants only the delegated Entra consent recorded in an identity receipt.
.DESCRIPTION
Run as an authorized tenant administrator. No Key Vault access, AKS access,
application password generation or secret export is performed by this command.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$IdentityFile,
    [string]$Output,
    [switch]$Apply,
    [switch]$IncludeAzureCli,
    [switch]$UseAzd
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$arguments = @("-m", "demos.apim_litellm.operator.entra_setup", "consent", "--identity", (Resolve-Path $IdentityFile).Path)
if ($Output) { $arguments += @("--output", $Output) }
if ($Apply) { $arguments += "--apply" }
if ($IncludeAzureCli) { $arguments += "--include-azure-cli" }
if ($UseAzd) { $arguments += "--use-azd" }
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    & python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Tenant admin consent failed; no positive OBO result is claimed." }
}
finally { Pop-Location }
