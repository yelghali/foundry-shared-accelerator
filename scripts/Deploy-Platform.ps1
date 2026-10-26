[CmdletBinding()]
param(
    [string]$Prefix,
    [string]$SubscriptionId,
    [string]$RunnerSize,
    [string]$SearchLocation,
    [string]$BackendConfigFile,
    [switch]$Apply
)
& (Join-Path $PSScriptRoot "Deploy-Lab.ps1") -Prefix $Prefix -SubscriptionId $SubscriptionId -RunnerSize $RunnerSize -SearchLocation $SearchLocation -BackendConfigFile $BackendConfigFile -Stage platform -Apply:$Apply
