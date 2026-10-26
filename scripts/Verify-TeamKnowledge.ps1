[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern("^[a-z][a-z0-9-]{2,39}$")][string]$ProjectName,
    [switch]$VerifyAgent
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$local = Join-Path $root ".local"
$deployment = Get-Content -LiteralPath (Join-Path $local "deployment.json") -Raw | ConvertFrom-Json
$project = Get-Content -LiteralPath (Join-Path $local "teams\$ProjectName.json") -Raw | ConvertFrom-Json
if ($project.name -ne $ProjectName) { throw "The project receipt does not match the requested team." }
$hasDedicatedSearch = $null -ne $project.PSObject.Properties["search"] -and $null -ne $project.search
if ($hasDedicatedSearch) {
    $deployment.search = $project.search
}
$request = Join-Path $local "teams\$ProjectName-verify-request.json"
$receipt = Join-Path $local "teams\$ProjectName-verify.json"
@{
    task       = "verify-iq"
    deployment = $deployment
    project    = $project
} | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $request -Encoding utf8NoBOM
& (Join-Path $PSScriptRoot "Invoke-Runner.ps1") -RequestFile $request -OutputFile $receipt
Get-Content -LiteralPath $receipt -Raw
if ($VerifyAgent) {
    $agentRequest = Join-Path $local "teams\$ProjectName-verify-agent-request.json"
    $agentReceipt = Join-Path $local "teams\$ProjectName-verify-agent.json"
    @{ task = "verify-iq-agent"; project = $project } |
        ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $agentRequest -Encoding utf8NoBOM
    & (Join-Path $PSScriptRoot "Invoke-Runner.ps1") -RequestFile $agentRequest -OutputFile $agentReceipt
    Get-Content -LiteralPath $agentReceipt -Raw
    if ($hasDedicatedSearch -and @($deployment.callers.PSObject.Properties).Count -gt 0) {
        $isolationRequest = Join-Path $local "teams\$ProjectName-verify-isolation-request.json"
        $isolationReceipt = Join-Path $local "teams\$ProjectName-verify-isolation.json"
        @{
            task     = "verify-iq-isolation"
            project  = $project
            callers  = $deployment.callers
        } | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $isolationRequest -Encoding utf8NoBOM
        & (Join-Path $PSScriptRoot "Invoke-Runner.ps1") -RequestFile $isolationRequest -OutputFile $isolationReceipt
        Get-Content -LiteralPath $isolationReceipt -Raw
    } elseif (-not $hasDedicatedSearch) {
        Write-Warning "Shared Search grants project identities service-wide read access; cross-team KB isolation is not expected or tested."
    }
}
