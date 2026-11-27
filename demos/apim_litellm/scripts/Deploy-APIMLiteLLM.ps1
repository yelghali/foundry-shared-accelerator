[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$VariablesFile,
    [ValidateSet("infrastructure", "workloads", "release", "all")][string]$Stage = "all",
    [string]$FoundryA2AEndpoint,
    [string]$ModelEndpoint,
    [string]$ModelDeployment = "gpt-4o-mini",
    [string]$ImageTag = "20261127a",
    [ValidateSet("shared-service", "dedicated-application", "user-obo")][string]$Scenario = "shared-service",
    [string]$IdentityFile,
    [string]$ScenarioVariablesFile,
    [string]$DeclarativeA2AEndpoint,
    [ValidateSet("public", "private")][string]$Exposure = "public",
    [string]$BackendHostname,
    [ValidateSet("azurecli", "azd")][string]$KubernetesLoginMethod = "azurecli",
    [switch]$SkipBuild,
    [switch]$EnableAgentResponses,
    [string]$AgentResponsesProjectEndpoint,
    [switch]$PrivateDemo,
    [switch]$Apply
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$variables = (Resolve-Path $VariablesFile).Path
$settings = Get-Content -LiteralPath $variables -Raw | ConvertFrom-Json
$stack = Join-Path $root "infra\apim-litellm"
$local = Join-Path $root ".local\apim-litellm"
New-Item -ItemType Directory -Force $local | Out-Null
$workloadsLocal = Join-Path $local $Scenario
New-Item -ItemType Directory -Force $workloadsLocal | Out-Null
$namespace = @{ "shared-service" = "gateway"; "dedicated-application" = "gateway-app-alpha"; "user-obo" = "gateway-user-obo" }[$Scenario]
$contractFile = Join-Path $local "deployment.json"
$planFile = Join-Path $local "deployment.tfplan"
$releaseFile = Join-Path $local "release.tfvars.json"
$backendFile = Join-Path $workloadsLocal "workload-backend.json"
$previousKubeconfig = $env:KUBECONFIG
Push-Location $root
try {
    function Invoke-ScenarioPlan([switch]$Release) {
        $arguments = @("-chdir=$stack", "plan", "-input=false", "-no-color", "-var-file=$variables", "-out=$planFile")
        if ($Release) { $arguments += "-var-file=$releaseFile" }
        if ($ScenarioVariablesFile) { $arguments += "-var-file=$((Resolve-Path $ScenarioVariablesFile).Path)" }
        & terraform @arguments
        if ($LASTEXITCODE -ne 0) { throw "Scenario Terraform plan failed." }
        $plan = & terraform "-chdir=$stack" show -json $planFile | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw "Cannot inspect saved scenario plan." }
        if (@($plan.resource_changes | Where-Object { "delete" -in $_.change.actions }).Count) {
            throw "Deletion/replacement detected. Review explicitly rather than applying through this script."
        }
        if ($Apply) {
            & terraform "-chdir=$stack" apply -input=false -no-color $planFile
            if ($LASTEXITCODE -ne 0) { throw "Scenario Terraform apply failed." }
            $contract = & terraform "-chdir=$stack" output -json deployment_contract
            if ($LASTEXITCODE -ne 0) { throw "Cannot read deployment contract." }
            $contract | Set-Content -LiteralPath $contractFile -Encoding utf8
            & terraform "-chdir=$stack" output -json | Set-Content -LiteralPath (Join-Path $local "terraform-outputs.json") -Encoding utf8
            if ($LASTEXITCODE -ne 0) { throw "Cannot export identity scenario outputs." }
        }
    }

    if ($Stage -ne "workloads") {
        & terraform "-chdir=$stack" init -input=false
        if ($LASTEXITCODE -ne 0) { throw "Terraform initialization failed." }
    }
    if ($Stage -in @("infrastructure", "all")) {
        # Preserve a previously published backend during an infrastructure rerun.
        Invoke-ScenarioPlan -Release:(Test-Path $releaseFile)
    }
    if (-not $Apply) {
        if ($Stage -notin @("infrastructure", "all")) { throw "Workloads/release require -Apply; infrastructure plans are the dry-run surface." }
        return
    }
    if ($Stage -eq "infrastructure") { return }
    if (-not (Test-Path $contractFile)) { throw "Deploy infrastructure first." }
    $deployment = Get-Content -LiteralPath $contractFile -Raw | ConvertFrom-Json
    if ($Stage -in @("workloads", "all")) {
        if (-not $FoundryA2AEndpoint -or -not $ModelEndpoint) { throw "Workloads require -FoundryA2AEndpoint and -ModelEndpoint." }
        $env:KUBECONFIG = Join-Path $local "kubeconfig"
        & az aks get-credentials --subscription $settings.subscription_id --resource-group $deployment.resource_group_name --name $deployment.aks.name --file $env:KUBECONFIG --overwrite-existing
        if ($LASTEXITCODE -ne 0) { throw "AKS credentials failed." }
        & kubelogin convert-kubeconfig -l $KubernetesLoginMethod
        if ($LASTEXITCODE -ne 0) { throw "Install kubelogin and retry." }
        $runtime = Join-Path $root "gateway"
        if (-not $SkipBuild) {
            foreach ($image in @(@("apim-litellm", "Dockerfile.litellm"), @("fastmcp", "Dockerfile.fastmcp"))) {
                & az acr build --subscription $settings.subscription_id --registry $deployment.acr.name --image "$($image[0]):$ImageTag" --file (Join-Path $runtime $image[1]) $runtime --no-logs --output none
                if ($LASTEXITCODE -ne 0) { throw "ACR build failed for $($image[0]). Inspect az acr task logs." }
            }
        }
        $dnsLabel = if ($Scenario -eq "shared-service") { $settings.name } else { "$($settings.name)-$Scenario" }
        $prepareArgs = @("-m", "litellm_demo.apim_litellm", "prepare", "--deployment", $contractFile,
            "--a2a-endpoint", $FoundryA2AEndpoint, "--model-endpoint", $ModelEndpoint,
            "--model-deployment", $ModelDeployment, "--image-tag", $ImageTag,
            "--dns-label", $dnsLabel, "--exposure", $Exposure, "--scenario", $Scenario, "--output", $workloadsLocal)
        if ($IdentityFile) { $prepareArgs += @("--identity", (Resolve-Path $IdentityFile).Path) }
        if ($DeclarativeA2AEndpoint) { $prepareArgs += @("--declarative-endpoint", $DeclarativeA2AEndpoint) }
        if ($EnableAgentResponses -or $PrivateDemo) { $prepareArgs += "--enable-agent-responses" }
        if ($PrivateDemo) { $prepareArgs += "--private-demo" }
        if ($AgentResponsesProjectEndpoint) { $prepareArgs += @("--agent-responses-project-endpoint", $AgentResponsesProjectEndpoint) }
        & python @prepareArgs
        if ($LASTEXITCODE -ne 0) { throw "Workload manifest generation failed." }
        & python -m litellm_demo.gateway_keys database --scenario $Scenario
        if ($LASTEXITCODE -ne 0) { throw "Persistent LiteLLM virtual-key database bootstrap failed." }
        if ($Scenario -eq "user-obo") {
            $outputs = Get-Content -LiteralPath (Join-Path $local "terraform-outputs.json") -Raw | ConvertFrom-Json
            $clientId = $outputs.identity_scenarios.value.'user-obo'.client_id
            & python -m litellm_demo.gateway_keys secret-provider --scenario user-obo --identity $IdentityFile --client-id $clientId
            if ($LASTEXITCODE -ne 0) { throw "OBO Key Vault secret provider setup failed." }
        }
        if (-not (Test-Path (Join-Path $workloadsLocal "tls.crt")) -or -not (Test-Path (Join-Path $workloadsLocal "tls.key"))) {
            throw "Provide tls.crt and tls.key in $workloadsLocal. Public development creates a self-signed pair; private mode requires your trusted certificate."
        }
        & kubectl -n $namespace create secret tls gateway-tls "--cert=$(Join-Path $workloadsLocal 'tls.crt')" "--key=$(Join-Path $workloadsLocal 'tls.key')" --dry-run=client -o json | & kubectl apply -f -
        if ($LASTEXITCODE -ne 0) { throw "TLS secret creation failed." }
        & kubectl apply -f (Join-Path $workloadsLocal "workloads.json")
        if ($LASTEXITCODE -ne 0) { throw "Workload apply failed." }
        foreach ($name in @("fastmcp", "litellm")) {
            & kubectl -n $namespace rollout status "deployment/$name" --timeout=600s
            if ($LASTEXITCODE -ne 0) { throw "$name is not ready; inspect its container logs." }
        }
        if ($Exposure -eq "public") {
            & kubectl -n $namespace wait '--for=jsonpath={.status.loadBalancer.ingress[0].ip}' service/litellm-internal --timeout=300s
            if ($LASTEXITCODE -ne 0) { throw "APIM's private AKS load balancer is not ready." }
            $backendIP = & kubectl -n $namespace get service litellm-internal '-o=jsonpath={.status.loadBalancer.ingress[0].ip}'
            if ($LASTEXITCODE -ne 0 -or -not $backendIP) { throw "Cannot resolve APIM's private backend." }
            @{ hostname = "$backendIP" } | ConvertTo-Json | Set-Content -LiteralPath $backendFile -Encoding utf8
        }
        $manifest = Get-Content -LiteralPath (Join-Path $workloadsLocal "workloads.json") -Raw | ConvertFrom-Json
        $seedJobs = @($manifest.items | Where-Object { $_.kind -eq "Job" })
        if ($seedJobs.Count -ne 1) { throw "Expected one private-network Blob seed Job." }
        & kubectl -n $namespace wait --for=condition=complete "job/$($seedJobs[0].metadata.name)" --timeout=300s
        if ($LASTEXITCODE -ne 0) { throw "Private acceptance Blob seeding failed; inspect the seed Job logs." }
        $keyArgs = @("-m", "litellm_demo.gateway_keys", "keys", "--scenario", $Scenario)
        if ($EnableAgentResponses -or $PrivateDemo) { $keyArgs += "--agent-responses" }
        & python @keyArgs
        if ($LASTEXITCODE -ne 0) { throw "Native LiteLLM scoped virtual-key provisioning failed." }
    }
    if ($Stage -in @("release", "all")) {
        if ($Exposure -eq "private" -and -not $BackendHostname) {
            throw "Private release requires -BackendHostname with private DNS and a trusted certificate matching it."
        }
        if (-not $BackendHostname -and $Exposure -eq "public" -and (Test-Path $backendFile)) {
            $BackendHostname = (Get-Content -LiteralPath $backendFile -Raw | ConvertFrom-Json).hostname
        }
        if (-not $BackendHostname) { $BackendHostname = "$($settings.name).$($deployment.location).cloudapp.azure.com" }
        $release = if (Test-Path $releaseFile) { Get-Content -LiteralPath $releaseFile -Raw | ConvertFrom-Json -AsHashtable } else { @{} }
        if ($Scenario -eq "shared-service") {
            # Preserve the other profiles' current TLS mode during a single-profile migration.
            if (-not $release.ContainsKey("scenario_development_self_signed_tls")) { $release.scenario_development_self_signed_tls = @{} }
            if ($release.ContainsKey("scenario_backend_urls")) {
                foreach ($profile in $release.scenario_backend_urls.Keys) {
                    if (-not $release.scenario_development_self_signed_tls.ContainsKey($profile)) {
                        $release.scenario_development_self_signed_tls[$profile] = $release.development_self_signed_tls
                    }
                }
            }
            $release.litellm_backend_url = "https://${BackendHostname}"
            $release.development_self_signed_tls = ($Exposure -eq "public")
        }
        else {
            if (-not $release.ContainsKey("scenario_backend_urls")) { $release.scenario_backend_urls = @{} }
            $release.scenario_backend_urls[$Scenario] = "https://${BackendHostname}"
            if (-not $release.ContainsKey("scenario_development_self_signed_tls")) { $release.scenario_development_self_signed_tls = @{} }
            $release.scenario_development_self_signed_tls[$Scenario] = ($Exposure -eq "public")
        }
        $release | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $releaseFile -Encoding utf8
        Invoke-ScenarioPlan -Release
        $apiPath = if ($Scenario -eq "shared-service") { "ai" } else { "ai-$Scenario" }
        Write-Host "Published $($deployment.apim.gateway_url)/$apiPath"
    }
}
finally {
    $env:KUBECONFIG = $previousKubeconfig
    Pop-Location
}
