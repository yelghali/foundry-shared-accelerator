[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern("^[a-z][a-z0-9-]{2,39}$")][string]$ProjectName,
    [string[]]$DeveloperPrincipalIds = @(),
    [string[]]$UserPrincipalIds = @(),
    [string[]]$GroupPrincipalIds = @(),
    [string[]]$ServicePrincipalIds = @(),
    [string[]]$SearchIndexIds = @(),
    [string]$KnowledgeManifest,
    [switch]$SkipKnowledge,
    [switch]$DedicatedSearch,
    [switch]$SharedSearch,
    [string]$SearchLocation,
    [string]$GatewaySubnetId,
    [ValidateSet("auto", "shared", "dedicated")][string]$GatewayMode = "auto",
    [string]$BackendConfigFile,
    [switch]$Apply
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$local = Join-Path $root ".local"
$deployment = Get-Content -LiteralPath (Join-Path $local "deployment.json") -Raw | ConvertFrom-Json
$existingReceipt = Join-Path $local "teams\$ProjectName.json"
$priorProject = if (Test-Path -LiteralPath $existingReceipt) { Get-Content -LiteralPath $existingReceipt -Raw | ConvertFrom-Json } else { $null }
if ($DedicatedSearch -and $SharedSearch) { throw "Choose either dedicated or shared Search, not both." }
if ($SkipKnowledge -and ($null -eq $priorProject -or $KnowledgeManifest)) {
    throw "-SkipKnowledge only applies to existing projects without a new knowledge manifest."
}
$priorDedicated = $null -ne $priorProject -and
    $null -ne $priorProject.PSObject.Properties["search"] -and $null -ne $priorProject.search
$useDedicatedSearch = if ($null -ne $priorProject) { $priorDedicated } else { [bool]$DedicatedSearch }
if (-not $useDedicatedSearch) {
    Write-Warning "When a project uses shared Search knowledge, its identity receives service-wide read access to all indexes and knowledge bases there. Use -DedicatedSearch for team isolation."
}
if ($null -ne $priorProject -and $DedicatedSearch -and
    -not $priorDedicated) {
    throw "Migrating an existing project's knowledge to a dedicated Search service requires an explicit migration plan."
}
if ($null -ne $priorProject -and $SharedSearch -and $priorDedicated) {
    throw "Migrating an existing dedicated project to shared Search requires an explicit migration plan."
}
$hasSearch = $null -ne $deployment.search -and -not [string]::IsNullOrWhiteSpace($deployment.storage_account_id)
$prepareKnowledge = -not $SkipKnowledge -and $hasSearch -and ($null -eq $priorProject -or
    $null -eq $priorProject.PSObject.Properties["knowledge"] -or
    -not [string]::IsNullOrWhiteSpace($KnowledgeManifest))
if (-not [string]::IsNullOrWhiteSpace($KnowledgeManifest) -and -not $hasSearch) {
    throw "A Search service and Storage account must be deployed before knowledge onboarding."
}
$manifest = $null
if ($prepareKnowledge) {
    $manifest = if ($KnowledgeManifest) {
        Get-Content -LiteralPath $KnowledgeManifest -Raw | ConvertFrom-Json
    } else {
        @{
            sources = @(@{ name = "$ProjectName-docs"; container = "$ProjectName-docs" })
            knowledge_bases = @(@{ name = "$ProjectName-kb"; sources = @("$ProjectName-docs") })
        }
    }
    if (@($manifest.sources).Count -eq 0 -or @($manifest.knowledge_bases).Count -eq 0) {
        throw "Knowledge onboarding requires sources and knowledge bases."
    }
}
if ($GatewayMode -eq "auto") {
    $GatewayMode = "shared"
    if ($null -ne $priorProject) {
        if ($priorProject.gateway.id -ne $deployment.gateway.id) { $GatewayMode = "dedicated" }
    }
}
if ($GatewayMode -eq "dedicated" -and [string]::IsNullOrWhiteSpace($GatewaySubnetId)) {
    $network = Get-Content -LiteralPath (Join-Path $local "network.json") -Raw | ConvertFrom-Json
    $subnet = $network.team_gateway_subnet_ids.PSObject.Properties[$ProjectName]
    if ($null -eq $subnet) { throw "Supply -GatewaySubnetId for this team's pre-created dedicated gateway subnet." }
    $GatewaySubnetId = $subnet.Value
}
$teamDir = Join-Path $local "teams"
$stateDir = Join-Path $local "states"
New-Item -ItemType Directory -Path $teamDir, $stateDir -Force | Out-Null
$lock = [IO.File]::Open((Join-Path $local "onboarding.lock"), "OpenOrCreate", "ReadWrite", "None")
$oldDataDir = $env:TF_DATA_DIR
try {
    $env:TF_DATA_DIR = Join-Path $local "tf-$ProjectName"
    $varsPath = Join-Path $teamDir "$ProjectName.tfvars.json"
    if (Test-Path -LiteralPath $varsPath) {
        $priorVars = Get-Content -LiteralPath $varsPath -Raw | ConvertFrom-Json
        $DeveloperPrincipalIds = @(@($priorVars.developer_principal_ids) + @($DeveloperPrincipalIds) | Where-Object { $_ } | Sort-Object -Unique)
        if ($null -ne $priorVars.PSObject.Properties["user_principal_ids"]) {
            $UserPrincipalIds = @(@($priorVars.user_principal_ids) + @($UserPrincipalIds) | Where-Object { $_ } | Sort-Object -Unique)
        }
        if ($null -ne $priorVars.PSObject.Properties["group_principal_ids"]) {
            $GroupPrincipalIds = @(@($priorVars.group_principal_ids) + @($GroupPrincipalIds) | Where-Object { $_ } | Sort-Object -Unique)
        }
        if ($null -ne $priorVars.PSObject.Properties["service_principal_ids"]) {
            $ServicePrincipalIds = @(@($priorVars.service_principal_ids) + @($ServicePrincipalIds) | Where-Object { $_ } | Sort-Object -Unique)
        }
    }
    $principals = @(@($DeveloperPrincipalIds) + @($deployment.deployer_principal_id, $deployment.runner.principal_id))
    $servicePrincipals = @(@($ServicePrincipalIds) + @($deployment.runner.principal_id))
    $caller = $deployment.callers.PSObject.Properties[$ProjectName]
    if ($null -ne $caller) {
        $principals += $caller.Value.principal_id
        $servicePrincipals += $caller.Value.principal_id
    }
    $principals += $ServicePrincipalIds
    $existingIndexes = @()
    if ($null -ne $priorProject -and $null -ne $priorProject.PSObject.Properties["search_index_ids"]) {
        $existingIndexes = @($priorProject.search_index_ids)
    }
    $indexIds = @(@($existingIndexes) + @($SearchIndexIds) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object -Unique)
    $containerNames = @()
    if ($null -ne $priorProject -and $null -ne $priorProject.PSObject.Properties["containers"]) {
        $containerNames = @($priorProject.containers.PSObject.Properties | ForEach-Object { $_.Name })
    }
    if ($prepareKnowledge) {
        $containerNames = @(@($containerNames) + @($manifest.sources | ForEach-Object { $_.container }) | Sort-Object -Unique)
    }
    $dedicated = $null
    if ($useDedicatedSearch) {
        $network = Get-Content -LiteralPath (Join-Path $local "network.json") -Raw | ConvertFrom-Json
        $location = if ($null -ne $priorProject -and $null -ne $priorProject.search) {
            $priorProject.search.location
        } elseif ($SearchLocation) {
            $SearchLocation
        } else {
            $deployment.search.location
        }
        if ([string]::IsNullOrWhiteSpace($location)) {
            throw "Supply -SearchLocation for a dedicated Search service."
        }
        $dedicated = @{
            location             = $location
            pe_location          = $network.primary_location
            pe_subnet_id         = $network.search_pe_subnet_id
            private_dns_zone_id  = $network.search_private_dns_zone_id
            embedding_account_id = $deployment.models.primary.id
        }
    }
    @{
        subscription_id          = $deployment.subscription_id
        project_name             = $ProjectName
        account_id               = $deployment.agent_foundry.id
        account_endpoint         = $deployment.agent_foundry.endpoint
        location                 = $deployment.location
        developer_principal_ids  = @($principals | Sort-Object -Unique)
        user_principal_ids       = @($UserPrincipalIds | Sort-Object -Unique)
        group_principal_ids      = @($GroupPrincipalIds | Sort-Object -Unique)
        service_principal_ids    = @($servicePrincipals | Sort-Object -Unique)
        approved_model           = @{
            gateway_id       = $deployment.gateway.id
            gateway_endpoint = $deployment.gateway.endpoint
            audience         = "https://azure-api.net/aigateway"
            model_version    = $deployment.model_version
            chargeback_key   = "$($deployment.resource_group):$ProjectName"
        }
        search_index_ids         = $indexIds
        storage_account_id       = $(if ($containerNames.Count) { $deployment.storage_account_id } else { $null })
        search_principal_id      = $(if ($containerNames.Count) { $deployment.search.principal_id } else { $null })
        shared_search_id               = $(if ($hasSearch -and -not $useDedicatedSearch -and $containerNames.Count) { $deployment.search.id } else { $null })
        search_location                = $(if ($containerNames.Count) { if ($useDedicatedSearch) { $dedicated.location } else { $deployment.search.location } } else { $null })
        agents_application_insights_id = $(if ($null -ne $deployment.PSObject.Properties["agents_application_insights_id"]) { $deployment.agents_application_insights_id } else { $null })
        dedicated_search         = $dedicated
        container_names          = $containerNames
        gateway_subnet_id        = $GatewaySubnetId
        runner_principal_id      = $deployment.runner.principal_id
        shared_gateway           = $(if ($GatewayMode -eq "shared") { $deployment.gateway } else { $null })
    } | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $varsPath -Encoding utf8NoBOM
    $stack = Join-Path $root "infra\stacks\team"
    $state = Join-Path $stateDir "$ProjectName.tfstate"
    $plan = Join-Path $teamDir "$ProjectName.tfplan"
    & (Join-Path $PSScriptRoot "Initialize-Backend.ps1") -Stack $stack -BackendConfigFile $BackendConfigFile `
        -LocalStatePath $(if ($BackendConfigFile) { "" } else { $state })
    $owned = @()
    if ($BackendConfigFile -or (Test-Path -LiteralPath $state)) {
        $owned = @(& terraform "-chdir=$stack" state list)
        if ($LASTEXITCODE -ne 0) { throw "Could not inspect team Terraform state." }
    }
    $connectionAddress = 'module.project.azapi_resource.model_connection[0]'
    $connectionId = "$($deployment.agent_foundry.id)/projects/$ProjectName/connections/platform-models-$ProjectName"
    if ($owned -notcontains $connectionAddress) {
        $existingConnection = & az resource show --ids $connectionId --api-version 2025-10-01-preview -o json 2>$null
        if ($LASTEXITCODE -eq 0) {
            $existing = ($existingConnection -join "`n") | ConvertFrom-Json
            $expectedTarget = "$($deployment.gateway.endpoint)/default/models/openai/v1"
            if ($existing.properties.category -ne "ApiManagement" -or
                $existing.properties.authType -ne "ProjectManagedIdentity" -or
                $existing.properties.target -ne $expectedTarget -or
                $existing.properties.audience -ne "https://azure-api.net/aigateway") {
                throw "Existing model connection differs from the approved platform configuration. Refusing adoption."
            }
            & terraform "-chdir=$stack" import -input=false "-var-file=$varsPath" $connectionAddress $connectionId
            if ($LASTEXITCODE -ne 0) { throw "Could not adopt existing model connection into team state." }
        }
    }
    $roleAddress = 'module.project.azurerm_role_assignment.approved_model[0]'
    if ($owned -notcontains $roleAddress -and $null -ne $priorProject) {
        $aliasId = "$($deployment.gateway.id)/workspaces/default/aliases/approved-chat"
        $modelRole = "a6b68e3d-a1f0-4c60-8678-237c050cf4ea"
        $assignments = & az role assignment list --scope $aliasId --assignee-object-id $priorProject.principal_id --fill-principal-name false -o json
        if ($LASTEXITCODE -ne 0) { throw "Could not inspect existing model alias assignments." }
        $matching = @((($assignments -join "`n") | ConvertFrom-Json) | Where-Object { $_.roleDefinitionId -like "*/$modelRole" -and $_.scope -eq $aliasId })
        if ($matching.Count -gt 1) { throw "Multiple alias grants exist for the project identity; resolve before adoption." }
        if ($matching.Count -eq 1) {
            & terraform "-chdir=$stack" import -input=false "-var-file=$varsPath" $roleAddress $matching[0].id
            if ($LASTEXITCODE -ne 0) { throw "Could not adopt existing model alias grant into team state." }
        }
    }
    & terraform "-chdir=$stack" plan -input=false "-var-file=$varsPath" "-out=$plan"
    if ($LASTEXITCODE -ne 0) { throw "Team Terraform planning failed." }
    $planJson = & terraform "-chdir=$stack" show -json $plan
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect the team plan." }
    $planned = ($planJson -join "`n") | ConvertFrom-Json
    if ($null -eq $planned.PSObject.Properties["planned_values"]) { throw "Terraform returned an invalid plan document." }
    $deletions = @()
    if ($null -ne $planned.PSObject.Properties["resource_changes"]) {
        $deletions = @($planned.resource_changes | Where-Object { $_.change.actions -contains "delete" })
    }
    if ($deletions.Count) {
        throw "Refusing a team plan containing deletion or replacement."
    }
    if (-not $Apply) { return }
    & terraform "-chdir=$stack" apply -input=false $plan
    if ($LASTEXITCODE -ne 0) { throw "Team provisioning failed; no success receipt was written." }
    $output = & terraform "-chdir=$stack" output -json project
    if ($LASTEXITCODE -ne 0) { throw "Could not read project outputs." }
    $project = ($output -join "`n") | ConvertFrom-Json
    if ($null -ne $priorProject -and $priorProject.id -eq $project.id -and $priorProject.gateway.id -eq $project.gateway.id) {
        foreach ($property in $priorProject.PSObject.Properties) {
            if ($null -eq $project.PSObject.Properties[$property.Name]) {
                $project | Add-Member -NotePropertyName $property.Name -NotePropertyValue $property.Value
            }
        }
    }
    if ($null -ne $caller) { $project | Add-Member -NotePropertyName sample_caller -NotePropertyValue $caller.Value -Force }
    if ($null -eq $project.PSObject.Properties["chargeback_project_key"]) {
        $project | Add-Member -NotePropertyName chargeback_project_key -NotePropertyValue "$($deployment.resource_group):$ProjectName"
    }
    $project | Add-Member -NotePropertyName search_index_ids -NotePropertyValue $indexIds -Force
    $project | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $teamDir "$ProjectName.json") -Encoding utf8NoBOM
    if ($prepareKnowledge) {
        $knowledgeDeployment = $deployment | ConvertTo-Json -Depth 30 | ConvertFrom-Json
        if ($useDedicatedSearch) {
            $knowledgeDeployment.search = $project.search
            foreach ($target in @(
                @{ id = $deployment.storage_account_id; version = "2023-05-01"; link = "$ProjectName-blob" },
                @{ id = $deployment.models.primary.id; version = "2025-06-01"; link = "$ProjectName-embedding" }
            )) {
                $url = "https://management.azure.com$($target.id)/privateEndpointConnections?api-version=$($target.version)"
                $connectionsJson = & az rest --method get --url $url -o json
                if ($LASTEXITCODE -ne 0) { throw "Could not inspect private link connections for $($target.link)." }
                $connections = @((($connectionsJson -join "`n") | ConvertFrom-Json).value |
                    Where-Object { $_.properties.privateEndpoint.id -like "*/$($target.link)" })
                if ($connections.Count -ne 1) { throw "Expected exactly one private link connection for $($target.link)." }
                $status = $connections[0].properties.privateLinkServiceConnectionState.status
                if ($status -eq "Pending") {
                    & az network private-endpoint-connection approve --id $connections[0].id --description "Approved isolated team Search ingestion" -o none
                    if ($LASTEXITCODE -ne 0) { throw "Could not approve private link $($target.link)." }
                } elseif ($status -ne "Approved") {
                    throw "Private link $($target.link) is $status; cannot ingest privately."
                }
            }
        }
        $requestFile = Join-Path $teamDir "$ProjectName-knowledge-request.json"
        $sourceReceipt = Join-Path $teamDir "$ProjectName-knowledge-source.json"
        $iqReceipt = Join-Path $teamDir "$ProjectName-knowledge.json"
        @{
            task = "prepare-iq"
            deployment = $knowledgeDeployment
            project = $project
            manifest = $manifest
        } | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $requestFile -Encoding utf8NoBOM
        & (Join-Path $PSScriptRoot "Invoke-Runner.ps1") -RequestFile $requestFile -OutputFile $sourceReceipt
        $knowledge = Get-Content -LiteralPath $sourceReceipt -Raw | ConvertFrom-Json
        $indexIds = @(@($indexIds) + @($knowledge.index_ids) | Sort-Object -Unique)
        $vars = Get-Content -LiteralPath $varsPath -Raw | ConvertFrom-Json
        $vars.search_index_ids = $indexIds
        $vars | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $varsPath -Encoding utf8NoBOM
        & terraform "-chdir=$stack" plan -input=false "-var-file=$varsPath" "-out=$plan"
        if ($LASTEXITCODE -ne 0) { throw "Index-scoped RBAC plan failed." }
        $second = (& terraform "-chdir=$stack" show -json $plan) -join "`n" | ConvertFrom-Json
        if (@($second.resource_changes | Where-Object { $_.change.actions -contains "delete" }).Count) {
            throw "Refusing index RBAC plan containing deletion or replacement."
        }
        & terraform "-chdir=$stack" apply -input=false $plan
        if ($LASTEXITCODE -ne 0) { throw "Index-scoped RBAC apply failed." }
        Push-Location $root
        try {
            & python -m accelerator.knowledge bind --project $existingReceipt --receipt $sourceReceipt --output $iqReceipt
            if ($LASTEXITCODE -ne 0) { throw "Foundry IQ connection binding failed." }
        }
        finally {
            Pop-Location
        }
        $project | Add-Member -NotePropertyName search_index_ids -NotePropertyValue $indexIds -Force
        $project | Add-Member -NotePropertyName knowledge -NotePropertyValue (Get-Content -LiteralPath $iqReceipt -Raw | ConvertFrom-Json) -Force
        $project | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $existingReceipt -Encoding utf8NoBOM
    }
    Write-Host "Project infrastructure ready: $ProjectName. Agent releases remain team-owned."
}
finally {
    $lock.Dispose()
    $env:TF_DATA_DIR = $oldDataDir
}
