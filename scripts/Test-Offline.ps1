[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    foreach ($file in Get-ChildItem -Path (Join-Path $root "scripts") -Filter *.ps1) {
        $tokens = $null
        $errors = $null
        [Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors) | Out-Null
        if ($errors.Count) { throw "PowerShell syntax failed in $($file.Name): $($errors | Out-String)" }
    }
    foreach ($file in Get-ChildItem -Path (Join-Path $root "ci\examples") -Filter *.json) {
        $null = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
    }
    & python -m compileall -q accelerator samples tests
    if ($LASTEXITCODE -ne 0) { throw "Python syntax validation failed." }
    & python -m unittest discover -s tests -q
    if ($LASTEXITCODE -ne 0) { throw "Python tests failed." }
    & terraform "-chdir=$(Join-Path $root 'infra')" fmt -check -recursive
    if ($LASTEXITCODE -ne 0) { throw "Terraform formatting failed." }
    $previousDataDir = $env:TF_DATA_DIR
    try {
        foreach ($stackName in @("platform", "team", "foundry")) {
            $stack = if ($stackName -eq "foundry") {
                Join-Path $root "infra\modules\foundry"
            } else {
                Join-Path $root "infra\stacks\$stackName"
            }
            $env:TF_DATA_DIR = Join-Path $root ".local\tf-validation-$stackName"
            New-Item -ItemType Directory -Path $env:TF_DATA_DIR -Force | Out-Null
            & terraform "-chdir=$stack" init -backend=false -input=false -lockfile=readonly
            if ($LASTEXITCODE -ne 0) { throw "$stackName provider initialization failed." }
            & terraform "-chdir=$stack" validate
            if ($LASTEXITCODE -ne 0) { throw "$stackName Terraform validation failed." }
            if ($stackName -ne "team") {
                & terraform "-chdir=$stack" test -no-color
                if ($LASTEXITCODE -ne 0) { throw "$stackName Terraform tests failed." }
            }
        }
    }
    finally {
        $env:TF_DATA_DIR = $previousDataDir
    }
}
finally {
    Pop-Location
}
