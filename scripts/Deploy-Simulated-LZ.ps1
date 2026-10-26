[CmdletBinding()]
param(
    [string]$Prefix,
    [string]$SubscriptionId,
    [Nullable[bool]]$EnableSecondary = $null,
    [switch]$Apply
)
& (Join-Path $PSScriptRoot "Deploy-Lab.ps1") -Prefix $Prefix -SubscriptionId $SubscriptionId -EnableSecondary $EnableSecondary -Stage simulator -Apply:$Apply
