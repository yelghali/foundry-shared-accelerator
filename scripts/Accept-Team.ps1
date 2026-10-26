[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern("^[a-z][a-z0-9-]{2,39}$")][string]$ProjectName,
    [ValidatePattern("^[a-z][a-z0-9-]{2,39}$")][string]$AgentName = "assistant"
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$local = Join-Path $root ".local"
$deploymentFile = Join-Path $local "deployment.json"
$projectFile = Join-Path $local "teams\$ProjectName.json"
if (-not (Test-Path -LiteralPath $projectFile)) {
    throw "Onboard $ProjectName before running acceptance."
}
$project = Get-Content -LiteralPath $projectFile -Raw | ConvertFrom-Json
if ($project.name -ne $ProjectName) { throw "The project receipt does not match the requested team." }
if ($null -eq $project.PSObject.Properties["knowledge"] -or
    $null -eq $project.knowledge.PSObject.Properties["knowledge_bases"] -or
    $null -eq $project.knowledge.knowledge_bases.PSObject.Properties["$ProjectName-kb"]) {
    throw "Acceptance requires the default $ProjectName-kb knowledge base and test container."
}
$teamDir = Join-Path $local "teams"
Push-Location $root
try {
    & (Join-Path $PSScriptRoot "Verify-TeamKnowledge.ps1") -ProjectName $ProjectName -VerifyAgent
    & python -m accelerator.gateway prepare-team --deployment $deploymentFile --project $projectFile --output $projectFile
    if ($LASTEXITCODE -ne 0) { throw "Approved gateway model connection verification failed." }
    $project = Get-Content -LiteralPath $projectFile -Raw | ConvertFrom-Json
    $marker = ($ProjectName -replace "-", "_").ToUpperInvariant() + "_GATEWAY_READY"
    $requestFile = Join-Path $teamDir "$ProjectName-$AgentName-accept-request.json"
    $runnerFile = Join-Path $teamDir "$ProjectName-$AgentName-accept-runner.json"
    $agentFile = Join-Path $teamDir "$ProjectName-$AgentName-agent.json"
    @{
        task    = "deploy-agent"
        project = $project
        name    = $AgentName
        mode    = "ensure"
        marker  = "${ProjectName}_PRIVATE_READY"
    } | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $requestFile -Encoding utf8NoBOM
    & (Join-Path $PSScriptRoot "Invoke-Runner.ps1") -RequestFile $requestFile -OutputFile $runnerFile
    $runner = Get-Content -LiteralPath $runnerFile -Raw | ConvertFrom-Json
    $runner.agent | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $agentFile -Encoding utf8NoBOM
    & python -m accelerator.gateway register-agent --deployment $deploymentFile --project $projectFile --agent $agentFile --output $projectFile
    if ($LASTEXITCODE -ne 0) { throw "Agent publication to the AI Gateway failed." }
    $sampleFile = Join-Path $teamDir "$ProjectName-$AgentName-accept-sample.json"
    & python (Join-Path $root "samples\consume_agent.py") --project $projectFile --agent-name $AgentName `
        --question "Reply with exactly $marker" --expected $marker --output $sampleFile
    if ($LASTEXITCODE -ne 0) { throw "Authenticated gateway agent consumption failed." }
    $usageFile = Join-Path $teamDir "$ProjectName-accept-usage.json"
    & python -m accelerator.usage --deployment $deploymentFile --projects $projectFile `
        --wait-seconds 300 --output $usageFile
    if ($LASTEXITCODE -ne 0) { throw "Native gateway token usage was not attributed to this project." }
    $project = Get-Content -LiteralPath $projectFile -Raw | ConvertFrom-Json
    $project | Add-Member -NotePropertyName gateway_registration_status -NotePropertyValue "runtime_verified" -Force
    $project | ConvertTo-Json -Depth 35 | Set-Content -LiteralPath $projectFile -Encoding utf8NoBOM
    Write-Host "Team $ProjectName accepted: private IQ, agent, authenticated gateway and attributed tokens."
    Write-Warning "The private runner and signed-in platform operator performed these checks. A named team user's own portal/network access still needs a human sign-in check."
}
finally {
    Pop-Location
}
