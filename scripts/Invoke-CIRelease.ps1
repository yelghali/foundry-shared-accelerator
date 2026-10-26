[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet("platform-plan", "platform-apply", "team-plan", "team-apply", "kb-plan", "kb-apply", "publish-agent", "grant-consumer")][string]$Action,
    [ValidatePattern("^[a-z][a-z0-9-]{2,39}$")][string]$ProjectName,
    [ValidatePattern("^[a-z][a-z0-9-]{2,39}$")][string]$AgentName,
    [string]$ConsumerPrincipalId
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$local = Join-Path $root ".local"
$teamDir = Join-Path $local "teams"
New-Item -ItemType Directory -Path $local, $teamDir -Force | Out-Null

foreach ($name in @("FSA_PREFIX", "AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_ID",
        "TF_STATE_RESOURCE_GROUP", "TF_STATE_STORAGE_ACCOUNT", "TF_STATE_CONTAINER")) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "Set the required CI variable $name."
    }
}
$prefix = $env:FSA_PREFIX
if ($prefix -notmatch "^[a-z][a-z0-9]{4,11}$") { throw "FSA_PREFIX must be 5-12 lowercase letters/digits." }
if ((& az account show --query id -o tsv) -ne $env:AZURE_SUBSCRIPTION_ID -or $LASTEXITCODE -ne 0) {
    throw "Azure CLI must be authenticated to the configured subscription."
}
if ((& az account show --query tenantId -o tsv) -ne $env:AZURE_TENANT_ID -or $LASTEXITCODE -ne 0) {
    throw "Azure CLI tenant does not match the CI configuration."
}
if ((& az account show --query user.type -o tsv) -ne "servicePrincipal" -or $LASTEXITCODE -ne 0) {
    throw "CI releases require a federated service-principal identity, not a user login."
}
if ($env:ARM_USE_OIDC -ne "true" -or $env:ARM_USE_AZUREAD -ne "true") {
    throw "Configure Terraform for Azure AD and workload-identity federation."
}
if ([string]::IsNullOrWhiteSpace($env:RUNNER_SSH_PRIVATE_KEY_B64)) {
    throw "Set RUNNER_SSH_PRIVATE_KEY_B64 to the persistent base64-encoded private key used by the platform runner."
}

function Invoke-AzJson {
    param([string[]]$Arguments)
    $result = & az @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Azure CLI operation failed: az $($Arguments[0..[Math]::Min(2, $Arguments.Count - 1)] -join ' ')" }
    return ($result -join "`n") | ConvertFrom-Json
}
function Test-Blob {
    param([string]$Name)
    $exists = Invoke-AzJson -Arguments @("storage", "blob", "exists", "--account-name", $env:TF_STATE_STORAGE_ACCOUNT,
        "--container-name", $env:TF_STATE_CONTAINER, "--name", $Name, "--auth-mode", "login", "-o", "json")
    return [bool]$exists.exists
}
function Get-Blob {
    param([string]$Name, [string]$Path)
    if (-not (Test-Blob -Name $Name)) { return $false }
    & az storage blob download --account-name $env:TF_STATE_STORAGE_ACCOUNT --container-name $env:TF_STATE_CONTAINER `
        --name $Name --file $Path --auth-mode login --overwrite true -o none
    if ($LASTEXITCODE -ne 0) { throw "Could not download CI receipt $Name." }
    return $true
}
function Set-Blob {
    param([string]$Name, [string]$Path)
    & az storage blob upload --account-name $env:TF_STATE_STORAGE_ACCOUNT --container-name $env:TF_STATE_CONTAINER `
        --name $Name --file $Path --auth-mode login --overwrite true -o none
    if ($LASTEXITCODE -ne 0) { throw "Could not store CI receipt $Name." }
}
function New-BackendConfig {
    param([string]$Key, [string]$Path)
    @(
        "resource_group_name=$env:TF_STATE_RESOURCE_GROUP"
        "storage_account_name=$env:TF_STATE_STORAGE_ACCOUNT"
        "container_name=$env:TF_STATE_CONTAINER"
        "key=$Key"
        "subscription_id=$env:AZURE_SUBSCRIPTION_ID"
        "tenant_id=$env:AZURE_TENANT_ID"
        "client_id=$env:AZURE_CLIENT_ID"
        "use_azuread_auth=true"
        "use_oidc=true"
    ) | Set-Content -LiteralPath $Path -Encoding utf8NoBOM
}
$networkFile = Join-Path $root "ci\network.json"
if (-not (Test-Path -LiteralPath $networkFile)) { throw "Provide the reviewed ci/network.json landing-zone contract." }
Copy-Item -LiteralPath $networkFile -Destination (Join-Path $local "network.json") -Force
$platformState = "fsa/$prefix/platform.tfstate"
$platformBackend = Join-Path $local "platform.backend.hcl"
New-BackendConfig -Key $platformState -Path $platformBackend
$stateExists = Test-Blob -Name $platformState
if (-not $stateExists) {
    $groupExists = & az group exists --name "rg-$prefix" -o tsv
    if ($LASTEXITCODE -ne 0) { throw "Could not check whether the target platform already exists." }
    if ($groupExists -eq "true") {
        throw "rg-$prefix exists without CI remote state. Migrate the existing local state before enabling CI apply."
    }
}

Push-Location $root
try {
    if ($Action -like "platform-*") {
        $ssh = Join-Path $local "runner_ed25519"
        [IO.File]::WriteAllBytes($ssh, [Convert]::FromBase64String($env:RUNNER_SSH_PRIVATE_KEY_B64))
        if (-not $IsWindows) {
            & chmod 600 $ssh
            if ($LASTEXITCODE -ne 0) { throw "Could not protect the runner SSH key." }
        }
        $publicKey = & ssh-keygen -y -P "" -f $ssh
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($publicKey)) {
            throw "RUNNER_SSH_PRIVATE_KEY_B64 must contain a valid unencrypted SSH private key."
        }
        $publicKey.Trim() | Set-Content -LiteralPath "$ssh.pub" -Encoding utf8NoBOM
        & (Join-Path $PSScriptRoot "Deploy-Platform.ps1") -Prefix $prefix `
            -SubscriptionId $env:AZURE_SUBSCRIPTION_ID -BackendConfigFile $platformBackend `
            -Apply:($Action -eq "platform-apply")
        if ($Action -eq "platform-apply") {
            $deploymentPath = Join-Path $local "deployment.json"
            $deployment = Get-Content -LiteralPath $deploymentPath -Raw | ConvertFrom-Json
            $deployment | Add-Member -NotePropertyName deployer_principal_type -NotePropertyValue ServicePrincipal -Force
            $deployment | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $deploymentPath -Encoding utf8NoBOM
            Set-Blob -Name "fsa/$prefix/deployment.json" -Path $deploymentPath
            & python -m accelerator.gateway configure-models --deployment $deploymentPath --output (Join-Path $local "gateway.json")
            if ($LASTEXITCODE -ne 0) { throw "Gateway model/telemetry release failed." }
            Set-Blob -Name "fsa/$prefix/gateway.json" -Path (Join-Path $local "gateway.json")
        }
        return
    }
    if (-not $ProjectName) { throw "ProjectName is required for team operations." }
    $deploymentPath = Join-Path $local "deployment.json"
    if (-not (Get-Blob -Name "fsa/$prefix/deployment.json" -Path $deploymentPath)) {
        throw "Deploy and configure the platform before running team CI."
    }
    $deployment = Get-Content -LiteralPath $deploymentPath -Raw | ConvertFrom-Json
    if ($deployment.subscription_id -ne $env:AZURE_SUBSCRIPTION_ID) { throw "The platform receipt targets another subscription." }
    $projectPath = Join-Path $teamDir "$ProjectName.json"
    $varsPath = Join-Path $teamDir "$ProjectName.tfvars.json"
    $hasReceipt = Get-Blob -Name "fsa/$prefix/teams/$ProjectName.json" -Path $projectPath
    $null = Get-Blob -Name "fsa/$prefix/teams/$ProjectName.tfvars.json" -Path $varsPath
    $teamState = "fsa/$prefix/teams/$ProjectName.tfstate"
    $teamBackend = Join-Path $local "$ProjectName.backend.hcl"
    New-BackendConfig -Key $teamState -Path $teamBackend
    $hasState = Test-Blob -Name $teamState
    if ($hasState -ne $hasReceipt) {
        throw "Team state and receipt disagree. Restore the missing receipt or migrate state before proceeding."
    }
    if (-not $hasState) {
        $projectId = "$($deployment.agent_foundry.id)/projects/$ProjectName"
        $items = Invoke-AzJson -Arguments @("resource", "list", "--resource-group", $deployment.resource_group,
            "--resource-type", "Microsoft.CognitiveServices/accounts/projects", "-o", "json")
        if (@($items | Where-Object { $_.id -eq $projectId }).Count) {
            throw "Project $ProjectName exists outside CI state. Import it before a CI release."
        }
    }
    if ($Action -in @("team-plan", "team-apply", "kb-plan", "kb-apply")) {
        if ($Action -like "kb-*" -and -not $hasReceipt) { throw "Onboard the team before extending its knowledge." }
        $configPath = Join-Path $root "ci\teams\$ProjectName.json"
        if (-not (Test-Path -LiteralPath $configPath)) { throw "Commit the reviewed team configuration: ci/teams/$ProjectName.json" }
        $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        if ($config.name -ne $ProjectName -or $config.search_mode -notin @("shared", "dedicated")) {
            throw "Team configuration must match the project and explicitly choose shared or dedicated Search."
        }
        $params = @{
            ProjectName = $ProjectName
            BackendConfigFile = $teamBackend
            UserPrincipalIds = @($config.user_principal_ids | Where-Object { $_ })
            GroupPrincipalIds = @($config.group_principal_ids | Where-Object { $_ })
            ServicePrincipalIds = @($config.service_principal_ids | Where-Object { $_ })
            Apply = ($Action -like "*-apply")
        }
        if ($config.search_mode -eq "dedicated") {
            if ([string]::IsNullOrWhiteSpace($config.search_location)) { throw "Dedicated Search needs a location." }
            $params.DedicatedSearch = $true
            $params.SearchLocation = $config.search_location
        } else { $params.SharedSearch = $true }
        if ($Action -like "kb-*") {
            $manifestPath = Join-Path $root "ci\knowledge\$ProjectName.json"
            if (-not (Test-Path -LiteralPath $manifestPath)) { throw "Commit ci/knowledge/$ProjectName.json before extending knowledge." }
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
            $prior = Get-Content -LiteralPath $projectPath -Raw | ConvertFrom-Json
            $previousBases = @($prior.knowledge.knowledge_bases.PSObject.Properties | ForEach-Object { $_.Name })
            $nextBases = @($manifest.knowledge_bases | ForEach-Object { $_.name })
            if (@($previousBases | Where-Object { $_ -notin $nextBases }).Count) {
                throw "Knowledge manifests must retain every existing team KB; removing a KB is a separate reviewed migration."
            }
            $params.KnowledgeManifest = $manifestPath
        }
        try {
            & (Join-Path $PSScriptRoot "Onboard-Team.ps1") @params
            if ($Action -eq "team-apply") {
                & (Join-Path $PSScriptRoot "Accept-Team.ps1") -ProjectName $ProjectName
            }
        }
        finally {
            if ($Action -like "*-apply" -and (Test-Path -LiteralPath $projectPath) -and (Test-Blob -Name $teamState)) {
                Set-Blob -Name "fsa/$prefix/teams/$ProjectName.json" -Path $projectPath
                Set-Blob -Name "fsa/$prefix/teams/$ProjectName.tfvars.json" -Path $varsPath
            }
        }
        return
    }
    if (-not $hasReceipt) { throw "Onboard the team before releasing an agent or consumer role." }
    if (-not $AgentName) { throw "AgentName is required for this operation." }
    if ($Action -eq "publish-agent") {
        $templatePath = Join-Path $root "ci\agents\$ProjectName\$AgentName.json"
        if (-not (Test-Path -LiteralPath $templatePath)) {
            throw "Commit the reviewed agent template: ci/agents/$ProjectName/$AgentName.json"
        }
        $requestPath = Join-Path $teamDir "$ProjectName-$AgentName-ci-request.json"
        $resultPath = Join-Path $teamDir "$ProjectName-$AgentName-ci-result.json"
        $agentPath = Join-Path $teamDir "$ProjectName-$AgentName-ci-agent.json"
        @{
            task = "deploy-agent"
            project = (Get-Content -LiteralPath $projectPath -Raw | ConvertFrom-Json)
            name = $AgentName
            mode = "ensure"
            marker = "${ProjectName}_PRIVATE_READY"
            template = (Get-Content -LiteralPath $templatePath -Raw | ConvertFrom-Json)
        } | ConvertTo-Json -Depth 35 | Set-Content -LiteralPath $requestPath -Encoding utf8NoBOM
        & (Join-Path $PSScriptRoot "Invoke-Runner.ps1") -RequestFile $requestPath -OutputFile $resultPath
        $result = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
        $result.agent | ConvertTo-Json -Depth 25 | Set-Content -LiteralPath $agentPath -Encoding utf8NoBOM
        & python -m accelerator.gateway register-agent --deployment $deploymentPath --project $projectPath `
            --agent $agentPath --output $projectPath
        if ($LASTEXITCODE -ne 0) { throw "Gateway agent publication failed." }
        $marker = ($ProjectName -replace "-", "_").ToUpperInvariant() + "_GATEWAY_READY"
        & python (Join-Path $root "samples\consume_agent.py") --project $projectPath --agent-name $AgentName `
            --question "Reply with exactly $marker" --expected $marker `
            --output (Join-Path $teamDir "$ProjectName-$AgentName-ci-consumption.json")
        if ($LASTEXITCODE -ne 0) { throw "Published agent runtime acceptance failed." }
        Set-Blob -Name "fsa/$prefix/teams/$ProjectName.json" -Path $projectPath
    } else {
        [Guid]$parsed = [Guid]::Empty
        if (-not [Guid]::TryParse($ConsumerPrincipalId, [ref]$parsed)) {
            throw "ConsumerPrincipalId must be an Entra object ID."
        }
        & python -m accelerator.gateway grant-agent-consumer --deployment $deploymentPath --project $projectPath `
            --agent-name $AgentName --consumer-principal-id $ConsumerPrincipalId `
            --output (Join-Path $teamDir "$ProjectName-$AgentName-consumer-grant.json")
        if ($LASTEXITCODE -ne 0) { throw "Gateway consumer grant failed." }
    }
}
finally {
    Pop-Location
}
