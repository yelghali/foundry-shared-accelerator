[CmdletBinding()]
param(
    [string]$Prefix,
    [string]$SubscriptionId,
    [Nullable[bool]]$EnableSecondary = $null,
    [string]$RunnerSize,
    [string]$SearchLocation,
    [string]$BackendConfigFile,
    [ValidateSet("all", "simulator", "platform")][string]$Stage = "all",
    [switch]$Apply
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$local = Join-Path $root ".local"
New-Item -ItemType Directory -Path $local -Force | Out-Null
$savedVars = Join-Path $local "simulator.tfvars.json"
$settings = if (Test-Path -LiteralPath $savedVars) { Get-Content -LiteralPath $savedVars -Raw | ConvertFrom-Json } else { $null }
if ([string]::IsNullOrWhiteSpace($Prefix)) {
    if ($null -eq $settings) { throw "The first deployment requires an explicit -Prefix." }
    $Prefix = $settings.prefix
}
if ($null -eq $EnableSecondary) {
    $EnableSecondary = if ($null -eq $settings) { $true } else { $settings.enable_secondary }
}

function Invoke-Checked {
    param([string]$Executable, [Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable failed (exit $LASTEXITCODE)." }
}
function Invoke-Json {
    param([string]$Executable, [Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    $text = & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable failed (exit $LASTEXITCODE)." }
    return ($text -join "`n") | ConvertFrom-Json
}
function Assert-NoDeletions {
    param([string]$Directory, [string]$Plan)
    $json = Invoke-Json terraform @("-chdir=$Directory", "show", "-json", $Plan)
    if ($null -eq $json.PSObject.Properties["planned_values"]) { throw "Terraform returned an invalid plan document." }
    $changes = @()
    if ($null -ne $json.PSObject.Properties["resource_changes"]) {
        $changes = @($json.resource_changes | Where-Object { $_.change.actions -contains "delete" })
    }
    if ($changes.Count) {
        $addresses = @($changes | ForEach-Object { $_.address })
        throw "Refusing a plan containing deletion/replacement: $($addresses -join ', '). Resources must be retained."
    }
}

$account = Invoke-Json az @("account", "show", "--output", "json")
if ([string]::IsNullOrWhiteSpace($SubscriptionId)) { $SubscriptionId = $account.id }
if ($null -ne $settings -and $settings.subscription_id -ne $SubscriptionId) {
    throw "This checkout already has a different subscription profile. Use a separate checkout/state."
}
if ($null -ne $settings -and $settings.prefix -ne $Prefix) {
    throw "This checkout already has a different deployment prefix. Use a separate checkout/state."
}
if ($account.id -ne $SubscriptionId) {
    throw "Select subscription '$SubscriptionId' explicitly with az account set first."
}
Write-Host "Deployment context: $($account.name) ($SubscriptionId), prefix $Prefix."
foreach ($namespace in @("Microsoft.Network", "Microsoft.CognitiveServices", "Microsoft.ApiManagement", "Microsoft.App", "Microsoft.Compute", "Microsoft.ManagedIdentity", "Microsoft.Insights", "Microsoft.Monitor", "Microsoft.OperationalInsights", "Microsoft.Search", "Microsoft.Storage")) {
    $provider = Invoke-Json az @("provider", "show", "--namespace", $namespace, "--query", "{state:registrationState}", "--output", "json")
    if ($provider.state -ne "Registered") {
        if (-not $Apply) { throw "Register $namespace before planning this environment." }
        Invoke-Checked az @("provider", "register", "--namespace", $namespace, "--wait")
    }
}

if ($Stage -ne "platform") {
    $simulator = Join-Path $root "landing-zone-simulator"
    $simulatorVars = Join-Path $local "simulator.tfvars.json"
    @{
        subscription_id   = $SubscriptionId
        prefix            = $Prefix
        enable_secondary  = $EnableSecondary
        enable_search_network = $true
        enable_shared_storage = $true
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $simulatorVars -Encoding utf8NoBOM
    $simulatorPlan = Join-Path $local "simulator.tfplan"
    Invoke-Checked terraform @("-chdir=$simulator", "init", "-input=false")
    Invoke-Checked terraform @("-chdir=$simulator", "validate")
    Invoke-Checked terraform @("-chdir=$simulator", "plan", "-input=false", "-var-file=$simulatorVars", "-out=$simulatorPlan")
    Assert-NoDeletions -Directory $simulator -Plan $simulatorPlan
    if (-not $Apply) {
        Write-Host "Simulator plan saved. Use -Apply to provision it before the platform."
        return
    }
    Invoke-Checked terraform @("-chdir=$simulator", "apply", "-input=false", $simulatorPlan)
    $network = Invoke-Json terraform @("-chdir=$simulator", "output", "-json", "network")
    $network | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $local "network.json") -Encoding utf8NoBOM
    if ($Stage -eq "simulator") {
        Write-Host "Simulated landing zone ready. Run Deploy-Platform.ps1 next."
        return
    }
}
$networkFile = Join-Path $local "network.json"
if (-not (Test-Path -LiteralPath $networkFile)) { throw "Supply .local/network.json from a customer landing zone or run Deploy-Simulated-LZ.ps1 first." }
$network = Get-Content -LiteralPath $networkFile -Raw | ConvertFrom-Json

$ssh = Join-Path $local "runner_ed25519"
if (-not (Test-Path -LiteralPath "$ssh.pub")) {
    Invoke-Checked ssh-keygen @("-q", "-t", "ed25519", "-N", "", "-f", $ssh)
}
if ($IsWindows) {
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    Invoke-Checked icacls @($ssh, "/inheritance:r", "/grant:r", "${owner}:(F)")
}
$platform = Join-Path $root "infra\stacks\platform"
$platformVars = Join-Path $local "platform.tfvars.json"
$platformSettings = @{
    subscription_id       = $SubscriptionId
    prefix                = $Prefix
    network               = $network
    runner_ssh_public_key  = (Get-Content -LiteralPath "$ssh.pub" -Raw).Trim()
    enable_search          = $true
    search_location        = $(if ($SearchLocation) { $SearchLocation } elseif ($null -ne $network.models.PSObject.Properties["secondary"]) { $network.models.secondary.location } else { $network.primary_location })
    enable_embedding       = $true
    allow_public_agent_access = $true
}
if ([string]::IsNullOrWhiteSpace($RunnerSize) -and (Test-Path -LiteralPath $platformVars)) {
    $priorPlatform = Get-Content -LiteralPath $platformVars -Raw | ConvertFrom-Json
    if ($null -ne $priorPlatform.PSObject.Properties["runner_size"]) { $RunnerSize = $priorPlatform.runner_size }
}
if (-not [string]::IsNullOrWhiteSpace($RunnerSize)) { $platformSettings.runner_size = $RunnerSize }
$platformSettings | ConvertTo-Json -Depth 25 | Set-Content -LiteralPath $platformVars -Encoding utf8NoBOM
$platformPlan = Join-Path $local "platform.tfplan"
& (Join-Path $PSScriptRoot "Initialize-Backend.ps1") -Stack $platform -BackendConfigFile $BackendConfigFile
Invoke-Checked terraform @("-chdir=$platform", "validate")
Invoke-Checked terraform @("-chdir=$platform", "plan", "-input=false", "-var-file=$platformVars", "-out=$platformPlan")
Assert-NoDeletions -Directory $platform -Plan $platformPlan
if (-not $Apply) {
    Write-Host "Platform plan saved. Use -Apply to provision."
    return
}
Invoke-Checked terraform @("-chdir=$platform", "apply", "-input=false", $platformPlan)
$deployment = Invoke-Json terraform @("-chdir=$platform", "output", "-json", "deployment")
$deployment | ConvertTo-Json -Depth 25 | Set-Content -LiteralPath (Join-Path $local "deployment.json") -Encoding utf8NoBOM
Write-Host "Platform deployed. Nonsecret receipt: $(Join-Path $local 'deployment.json')"
Write-Host "No resources will be deleted automatically. Continue with gateway configuration and project onboarding."
