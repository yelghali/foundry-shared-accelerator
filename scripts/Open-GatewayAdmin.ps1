<#
.SYNOPSIS
Opens a loopback-only AKS tunnel and exports the admin password to a protected local file.
.DESCRIPTION
The admin UI deliberately bypasses APIM's consumer API surface. This does not
expose administration publicly. Keep this command running while using the UI.
#>
[CmdletBinding()]
param(
    [ValidateSet("shared-service", "dedicated-application", "user-obo")][string]$Scenario = "shared-service",
    [ValidateRange(1024, 65535)][int]$Port = 4401,
    [ValidateRange(1024, 65535)][int]$TunnelPort = 4400,
    [ValidateSet("azurecli", "azd")][string]$KubernetesLoginMethod = "azd",
    [switch]$CopyAdminKey,
    [switch]$ExportKeyOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$namespace = @{ "shared-service" = "gateway"; "dedicated-application" = "gateway-app-alpha"; "user-obo" = "gateway-user-obo" }[$Scenario]
$kubeconfig = Join-Path $root ".local\apim-litellm\kubeconfig"
if (-not (Test-Path $kubeconfig)) { throw "Deploy the gateway workloads first; operator kubeconfig is missing." }
$previousKubeconfig = $env:KUBECONFIG
$tunnel = $null
function Protect-AdminPath([string]$Path, [switch]$Directory) {
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $otherGrants = @((Get-Acl -LiteralPath $Path).Access | Where-Object {
        $_.AccessControlType -eq "Allow" -and
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $currentSid
    } | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } | Sort-Object -Unique)
    $rights = if ($Directory) { "(OI)(CI)F" } else { "F" }
    # Change only the DACL; no audit/owner privilege is required.
    & icacls.exe $Path /inheritance:r /grant:r "*${currentSid}:$rights" /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Cannot restrict the admin credential path to the current account." }
    foreach ($otherSid in $otherGrants) {
        & icacls.exe $Path /remove:g "*$otherSid" /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Cannot remove an unrelated grant from the admin credential path." }
    }
}
try {
    if ($Port -eq $TunnelPort) { throw "The UI and TLS tunnel need different local ports." }
    $env:KUBECONFIG = $kubeconfig
    & kubelogin convert-kubeconfig -l $KubernetesLoginMethod
    if ($LASTEXITCODE -ne 0) { throw "Kubernetes login setup failed." }
    $encoded = & kubectl --request-timeout=60s -n $namespace get secret litellm-auth '-o=jsonpath={.data.master-key}'
    if ($LASTEXITCODE -ne 0 -or -not $encoded) { throw "Cannot read this scenario's admin credential; operator RBAC is required." }
    $masterKey = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))
    if (-not $masterKey.StartsWith("sk-")) { throw "The deployed master key is invalid." }

    $directory = Join-Path $root ".local\apim-litellm\admin"
    New-Item -ItemType Directory -Force $directory | Out-Null
    if ($IsWindows) {
        Protect-AdminPath -Path $directory -Directory
    }
    else {
        & chmod 700 $directory
        if ($LASTEXITCODE -ne 0) { throw "Cannot protect the admin credential directory." }
    }
    $keyFile = Join-Path $directory "$Scenario-admin.key"
    if ($IsWindows -and (Test-Path $keyFile)) { Protect-AdminPath -Path $keyFile }
    [IO.File]::WriteAllText($keyFile, $masterKey, [Text.UTF8Encoding]::new($false))
    if ($IsWindows) {
        Protect-AdminPath -Path $keyFile
    }
    else {
        & chmod 600 $keyFile
        if ($LASTEXITCODE -ne 0) { throw "Cannot protect the admin credential file." }
    }
    if ($CopyAdminKey) { Set-Clipboard -Value $masterKey }
    $masterKey = $null
    $encoded = $null
    Write-Host "UI: http://localhost:$Port/ui/login/"
    Write-Host "Username: admin"
    Write-Host "Password/admin key: $keyFile (not printed)"
    if ($ExportKeyOnly) { return }
    $deployment = Get-Content -LiteralPath (Join-Path $root ".local\apim-litellm\deployment.json") -Raw | ConvertFrom-Json
    $apiPath = if ($Scenario -eq "shared-service") { "ai" } else { "ai-$Scenario" }
    $publicBase = "$($deployment.apim.gateway_url)/$apiPath"
    $certificate = Join-Path $root ".local\apim-litellm\$Scenario\tls.crt"
    if (-not (Test-Path $certificate)) { throw "The matching gateway TLS certificate is missing; deploy workloads first." }
    foreach ($number in @($Port, $TunnelPort)) {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $number)
        try { $listener.Start() }
        catch [Net.Sockets.SocketException] { throw "Local port $number is occupied. Choose different ports; do not stop an unrelated process." }
        finally { $listener.Stop() }
    }
    $tunnel = Start-Process -FilePath (Get-Command kubectl).Source -ArgumentList @(
        "--request-timeout=0", "-n", $namespace, "port-forward", "--address=127.0.0.1",
        "service/litellm", "${TunnelPort}:443"
    ) -NoNewWindow -PassThru
    Write-Host "Loopback-only development UI; upstream AKS traffic remains TLS protected."
    Write-Host "Ctrl+C closes access. This panel tests LiteLLM directly, not APIM admission."
    Push-Location $root
    try {
        & python -m accelerator.admin_ui_proxy --port $Port --tunnel-port $TunnelPort --certificate $certificate --public-base $publicBase
        if ($LASTEXITCODE -ne 0) { throw "The local UI bridge stopped unexpectedly." }
    }
    finally { Pop-Location }
}
finally {
    if ($null -ne $tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue }
    $masterKey = $null
    $encoded = $null
    $env:KUBECONFIG = $previousKubeconfig
}
