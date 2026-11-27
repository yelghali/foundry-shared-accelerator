<#
.SYNOPSIS
Plans Entra identity setup; -Apply creates registrations and stores OBO credentials only in Key Vault.
.DESCRIPTION
Requires Graph Application.ReadWrite.All. -GrantAdminConsent additionally requires
DelegatedPermissionGrant.ReadWrite.All and an authorized tenant administrator.
Key Vault requires Secrets Officer data-plane access. No tokens or passwords enter the receipt.
For a private vault, -KeyVaultProxyNamespace gateway explicitly sends vault requests
through deployment/fastmcp using the operator's token over kubectl stdin, never pod identity.
Login: python -m litellm_demo.entra_setup login --identity <receipt>
Device login requires a real user; it cannot be bootstrapped unattended.
New custom API scopes default to admin consent. To opt existing development apps
into user consent after provisioning, run:
python -m litellm_demo.entra_setup user-consent --identity <receipt> --use-azd --apply
This changes app-owned scopes and combined consent, never tenant consent policy.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$TenantId,
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][string]$ProjectId,
    [Parameter(Mandatory)][string]$Prefix,
    [Parameter(Mandatory)][string]$KeyVaultUrl,
    [string]$KeyVaultSubscriptionId,
    [string]$KeyVaultProxyNamespace,
    [Parameter(Mandatory)][string]$Output,
    [ValidateSet("shared-service", "dedicated-application", "user-obo")][string]$Profile = "user-obo",
    [string[]]$PermittedUserObjectId = @(),
    [switch]$Apply,
    [switch]$GrantAdminConsent,
    [switch]$RotateOboSecret,
    [switch]$PreauthorizeAzureCli,
    [switch]$UseAzd
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$arguments = @("-m", "litellm_demo.entra_setup", "provision",
    "--tenant-id", $TenantId, "--subscription-id", $SubscriptionId,
    "--project-id", $ProjectId, "--prefix", $Prefix, "--key-vault-url", $KeyVaultUrl,
    "--profile", $Profile, "--output", $Output)
if ($KeyVaultSubscriptionId) { $arguments += @("--key-vault-subscription-id", $KeyVaultSubscriptionId) }
if ($KeyVaultProxyNamespace) { $arguments += @("--key-vault-proxy-namespace", $KeyVaultProxyNamespace) }
foreach ($id in $PermittedUserObjectId) { $arguments += @("--permitted-user-object-id", $id) }
if ($Apply) { $arguments += "--apply" }
if ($GrantAdminConsent) { $arguments += "--grant-admin-consent" }
if ($RotateOboSecret) { $arguments += "--rotate-obo-secret" }
if ($PreauthorizeAzureCli) { $arguments += "--preauthorize-azure-cli" }
if ($UseAzd) { $arguments += "--use-azd" }
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    & python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Entra identity setup failed; see sanitized diagnostic above." }
}
finally { Pop-Location }
