[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RequestFile,
    [Parameter(Mandatory)][string]$OutputFile
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$local = Join-Path $root ".local"
$deployment = Get-Content -LiteralPath (Join-Path $local "deployment.json") -Raw | ConvertFrom-Json
$archive = Join-Path $local "runner-code.zip"
if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive }
Compress-Archive -LiteralPath (Join-Path $root "accelerator"), (Join-Path $root "samples"), (Join-Path $root "requirements.txt") -DestinationPath $archive
$code = [Convert]::ToBase64String([IO.File]::ReadAllBytes($archive))
$request = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $RequestFile)))
$script = @"
set -eu
mkdir -p /opt/fsa
export DEBIAN_FRONTEND=noninteractive
if [ ! -x /opt/fsa/venv/bin/python ]; then
  apt-get update -qq
  apt-get install -y -qq python3-venv unzip
  python3 -m venv /opt/fsa/venv
fi
printf '%s' '$code' | base64 -d > /opt/fsa/code.zip
unzip -oq /opt/fsa/code.zip -d /opt/fsa
printf '%s' '$request' | base64 -d > /opt/fsa/request.json
cd /opt/fsa
venv/bin/python -m pip install --disable-pip-version-check --quiet -r requirements.txt
venv/bin/python -m accelerator.runner --request request.json
"@
$scriptFile = Join-Path $local "runner-command.sh"
$script.Replace("`r`n", "`n") | Set-Content -LiteralPath $scriptFile -Encoding utf8NoBOM
$raw = & az vm run-command invoke --resource-group $deployment.resource_group --name $deployment.runner.name --command-id RunShellScript --scripts "@$scriptFile" --output json
if ($LASTEXITCODE -ne 0) { throw "Azure VM Run Command failed." }
$response = ($raw -join "`n") | ConvertFrom-Json
$messages = @($response.value | ForEach-Object message) -join "`n"
$match = [regex]::Match($messages, '(?m)^FSA_RESULT:(.+)$')
if (-not $match.Success) { throw "Runner did not produce a success receipt. Azure output: $messages" }
$receipt = $match.Groups[1].Value | ConvertFrom-Json
$receipt | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $OutputFile -Encoding utf8NoBOM
Write-Host "Network-connected task succeeded. Nonsecret receipt: $OutputFile"
