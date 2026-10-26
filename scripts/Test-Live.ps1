[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$local = Join-Path $root ".local"
$requests = Join-Path $local "requests"
New-Item -ItemType Directory -Path $requests -Force | Out-Null
$deploymentFile = Join-Path $local "deployment.json"
$deployment = Get-Content -LiteralPath $deploymentFile -Raw | ConvertFrom-Json
Push-Location $root
try {
    & python -m accelerator.gateway configure-models --deployment $deploymentFile --output (Join-Path $local "gateway.json")
    if ($LASTEXITCODE -ne 0) { throw "Gateway model configuration failed." }
    @{ task = "private-checks"; deployment = $deployment } | ConvertTo-Json -Depth 30 |
        Set-Content -LiteralPath (Join-Path $requests "private.json") -Encoding utf8NoBOM
    & (Join-Path $PSScriptRoot "Invoke-Runner.ps1") -RequestFile (Join-Path $requests "private.json") -OutputFile (Join-Path $local "private-verification.json")
    $projectFiles = @()
    foreach ($name in @("team-alpha", "team-beta")) {
        & (Join-Path $PSScriptRoot "Onboard-Team.ps1") -ProjectName $name -Apply
        $projectFile = Join-Path $local "teams\$name.json"
        & python -m accelerator.gateway prepare-team --deployment $deploymentFile --project $projectFile --output $projectFile
        if ($LASTEXITCODE -ne 0) { throw "Team connection configuration failed: $name" }
        $project = Get-Content -LiteralPath $projectFile -Raw | ConvertFrom-Json
        @{ task = "deploy-agent"; project = $project; name = "assistant"; mode = "ensure"; marker = "${name}_PRIVATE_READY" } |
            ConvertTo-Json -Depth 30 | Set-Content -LiteralPath (Join-Path $requests "$name-agent.json") -Encoding utf8NoBOM
        $resultFile = Join-Path $local "teams\$name-runner.json"
        & (Join-Path $PSScriptRoot "Invoke-Runner.ps1") -RequestFile (Join-Path $requests "$name-agent.json") -OutputFile $resultFile
        $result = Get-Content -LiteralPath $resultFile -Raw | ConvertFrom-Json
        $agentFile = Join-Path $local "teams\$name-agent.json"
        $result.agent | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $agentFile -Encoding utf8NoBOM
        & python -m accelerator.gateway register-agent --deployment $deploymentFile --project $projectFile --agent $agentFile --output $projectFile
        if ($LASTEXITCODE -ne 0) { throw "Gateway agent registration failed: $name" }
        & python (Join-Path $root "samples\consume_agent.py") --project $projectFile --question "Reply with exactly ${name}_GATEWAY_READY" --expected "${name}_GATEWAY_READY" --output (Join-Path $local "teams\$name-sample.json")
        if ($LASTEXITCODE -ne 0) { throw "Gateway-mediated consumption failed: $name" }
        if ($name -eq "team-alpha") {
            & python -m accelerator.gateway prepare-tools --deployment $deploymentFile --project $projectFile --output $projectFile
            if ($LASTEXITCODE -ne 0) { throw "Toolbox connection configuration failed." }
            $project = Get-Content -LiteralPath $projectFile -Raw | ConvertFrom-Json
            @{ task = "deploy-toolbox"; project = $project } | ConvertTo-Json -Depth 35 |
                Set-Content -LiteralPath (Join-Path $requests "$name-toolbox.json") -Encoding utf8NoBOM
            $toolboxFile = Join-Path $local "teams\$name-toolbox.json"
            & (Join-Path $PSScriptRoot "Invoke-Runner.ps1") -RequestFile (Join-Path $requests "$name-toolbox.json") -OutputFile $toolboxFile
            & python -m accelerator.gateway register-toolbox --deployment $deploymentFile --project $projectFile --toolbox $toolboxFile --output $projectFile
            if ($LASTEXITCODE -ne 0) { throw "Toolbox gateway registration failed." }
            & python (Join-Path $root "samples\consume_agent.py") --project $projectFile --toolbox --question "Reply with exactly TOOLBOX_GATEWAY_READY" --expected TOOLBOX_GATEWAY_READY --output (Join-Path $local "teams\$name-toolbox-sample.json")
            if ($LASTEXITCODE -ne 0) { throw "Toolbox gateway invocation failed." }
        }
        $verified = Get-Content -LiteralPath $projectFile -Raw | ConvertFrom-Json
        $verified | Add-Member -NotePropertyName gateway_registration_status -NotePropertyValue "runtime_verified" -Force
        $verified | ConvertTo-Json -Depth 35 | Set-Content -LiteralPath $projectFile -Encoding utf8NoBOM
        $projectFiles += $projectFile
    }
    $projects = @($projectFiles | ForEach-Object { Get-Content -LiteralPath $_ -Raw | ConvertFrom-Json })
    @{ task = "isolation"; projects = $projects } | ConvertTo-Json -Depth 35 |
        Set-Content -LiteralPath (Join-Path $requests "isolation.json") -Encoding utf8NoBOM
    & (Join-Path $PSScriptRoot "Invoke-Runner.ps1") -RequestFile (Join-Path $requests "isolation.json") -OutputFile (Join-Path $local "isolation-verification.json")
    @{ task = "model-probes"; deployment = $deployment; projects = $projects; count = 8 } |
        ConvertTo-Json -Depth 35 | Set-Content -LiteralPath (Join-Path $requests "model-probes.json") -Encoding utf8NoBOM
    $probeFile = Join-Path $local "project-probe-usage.json"
    & (Join-Path $PSScriptRoot "Invoke-Runner.ps1") -RequestFile (Join-Path $requests "model-probes.json") -OutputFile $probeFile
    & python -m accelerator.usage --deployment $deploymentFile --projects @projectFiles --probe-evidence $probeFile --output (Join-Path $local "usage-evidence.json")
    if ($LASTEXITCODE -ne 0) { throw "Native usage verification failed; inspect the recorded evidence." }
    Write-Host "Live verification passed. Resources are retained. Receipts: $local"
}
finally { Pop-Location }
