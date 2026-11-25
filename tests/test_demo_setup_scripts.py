"""Execute the real PowerShell wrappers with mocked cloud CLIs."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION = "00000000-0000-0000-0000-000000000001"
TENANT = "00000000-0000-0000-0000-000000000002"
ACCOUNT = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/demo/providers/Microsoft.CognitiveServices/accounts/demoaccount"
PROJECT = ACCOUNT + "/projects/demo-project"


def shell(code):
    executable = os.environ.get("TEST_POWERSHELL_EXE") or shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is required to execute deployment wrapper tests.")
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", code],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    if "about_Execution_Policies" in result.stderr and "UnauthorizedAccess" in result.stderr:
        pytest.skip("Host policy disables PowerShell scripts; wrapper execution requires an approved scripting environment.")
    return result


def test_hosted_plan_never_runs_azd_and_rejects_mismatched_project():
    command = (
        "& .\\scripts\\Deploy-DemoHostedAgent.ps1 "
        f"-SubscriptionId {SUBSCRIPTION} -ProjectId '{PROJECT}' "
        "-ProjectEndpoint https://demoaccount.services.ai.azure.com/api/projects/demo-project"
    )
    result = shell("function azd { throw 'Cloud CLI must not run for a plan' }; " + command)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["creates_project"] is False
    assert plan["writes_performed"] is False
    assert plan["protocols"] == ["a2a", "responses"]
    bad = shell(command.replace("/api/projects/demo-project", "/api/projects/another-project"))
    assert bad.returncode != 0
    assert "same project name" in bad.stderr


@pytest.mark.parametrize("replace", [False, True])
def test_model_plan_cannot_apply_or_replace_existing_deployment(replace):
    directory = ROOT / ".local" / "demo-foundry-model" / "demoaccount" / "test-model-script"
    try:
        code = r"""
$global:LASTEXITCODE = 0
function terraform {
    if ($args -contains 'apply') { throw 'Plan must not apply' }
    if ($args -contains 'show') { Write-Output '__PLAN__' }
    $global:LASTEXITCODE = 0
}
& .\scripts\Deploy-DemoModel.ps1 -SubscriptionId __SUB__ -TenantId __TENANT__ -ModelAccountId '__ACCOUNT__' -ModelEndpoint https://demoaccount.openai.azure.com/ -DeploymentName test-model-script
"""
        plan = {"resource_changes": [{"change": {"actions": ["delete", "create"]}}]} if replace else {}
        result = shell(code.replace("__PLAN__", json.dumps(plan)).replace("__SUB__", SUBSCRIPTION)
                       .replace("__TENANT__", TENANT).replace("__ACCOUNT__", ACCOUNT))
        if replace:
            assert result.returncode != 0
            assert "deletion/replacement" in result.stderr
        else:
            assert result.returncode == 0, result.stderr
            assert "Plan only" in result.stdout
        assert not (directory / "model.json").exists()
    finally:
        if directory.exists():
            shutil.rmtree(directory)


def test_failed_hosted_deployment_has_no_success_receipt_or_preview_env_leak():
    folder = ROOT / ".local" / "demo-script-tests"
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as directory:
        result = shell(r"""
$root = '__ROOT__'
$before = Get-Location
$env:AZD_ALPHA_ENABLE_AGENTS_A2A = 'previous'
function Push-Location { Microsoft.PowerShell.Management\Push-Location '__DIR__' }
function azd {
    if ($args[0] -eq 'deploy') { $global:LASTEXITCODE = 1 }
    else { $global:LASTEXITCODE = 0 }
}
try {
    & "$root\scripts\Deploy-DemoHostedAgent.ps1" -SubscriptionId __SUB__ -ProjectId '__PROJECT__' -ProjectEndpoint https://demoaccount.services.ai.azure.com/api/projects/demo-project -EnvironmentName test-hosted-script -Apply
    throw 'Expected failure'
}
catch {
    if ($_.Exception.Message -notlike '*deployment failed*') { throw }
}
if ($env:AZD_ALPHA_ENABLE_AGENTS_A2A -ne 'previous') { throw 'Preview environment leaked' }
if ((Get-Location).Path -ne $before.Path) { throw 'Working directory leaked' }
if (Test-Path "$root\.local\demo-hosted-agent\test-hosted-script\hosted-agent.json") { throw 'Success receipt leaked' }
Write-Output 'Failure cleanup verified'
""".replace("__ROOT__", str(ROOT)).replace("__DIR__", directory)
                       .replace("__SUB__", SUBSCRIPTION).replace("__PROJECT__", PROJECT))
    assert result.returncode == 0, result.stderr
    assert "Failure cleanup verified" in result.stdout


@pytest.mark.parametrize(("scenario", "auth_summary"), [
    ("shared-service", "shared gateway workload identity, not user OBO"),
    ("dedicated-application", "dedicated app-alpha gateway workload identity, not user OBO"),
    ("user-obo", "Hosted/prompt agents (A2A and Responses): custom user OBO"),
])
def test_consumer_loader_sets_current_session_without_printing_credentials(scenario, auth_summary):
    result = shell(r"""
$beforePath = $env:PATH
$env:KUBECONFIG = 'original-kubeconfig'
function fakepython {
    $global:LASTEXITCODE = 0
    '{"key":"sk-test-consumer-not-real","token":"test-entra-not-real","endpoint":"https://demo.azure-api.net/ai"}'
}
& .\scripts\Import-GatewayConsumerEnv.ps1 -Scenario __SCENARIO__ -PythonExecutable fakepython -UseAzd
if ($env:LITELLM_VIRTUAL_KEY -ne 'sk-test-consumer-not-real') { throw 'Application key not loaded' }
if ($env:GATEWAY_ACCESS_TOKEN -ne 'test-entra-not-real') { throw 'Caller token not loaded' }
if ($env:GATEWAY_ENDPOINT -ne 'https://demo.azure-api.net/ai') { throw 'Endpoint not loaded' }
if ($env:PATH -ne $beforePath -or $env:KUBECONFIG -ne 'original-kubeconfig') { throw 'Operator environment leaked' }
Write-Output 'Current-session credentials verified'
""".replace("__SCENARIO__", scenario))
    assert result.returncode == 0, result.stderr
    assert "Current-session credentials verified" in result.stdout
    assert "Setup only: no model, MCP or agent inference was called." in result.stdout
    assert "replaces the previous scenario" in result.stdout
    assert auth_summary in result.stdout
    assert "--kind hosted-agent --agent-name hosted" in result.stdout
    assert "sk-test-consumer-not-real" not in result.stdout
    assert "test-entra-not-real" not in result.stdout


def test_failed_consumer_loader_clears_stale_profile_and_restores_operator_environment():
    result = shell(r"""
$beforePath = $env:PATH
$env:KUBECONFIG = 'original-kubeconfig'
$env:LITELLM_VIRTUAL_KEY = 'stale-key'
$env:GATEWAY_ACCESS_TOKEN = 'stale-token'
$env:GATEWAY_ENDPOINT = 'https://stale.example/ai'
function fakepython { $global:LASTEXITCODE = 1 }
try {
    & .\scripts\Import-GatewayConsumerEnv.ps1 -Scenario user-obo -PythonExecutable fakepython
    throw 'Expected failure'
}
catch {
    if ($_.Exception.Message -notlike '*environment was not loaded*') { throw }
}
if ($env:LITELLM_VIRTUAL_KEY -or $env:GATEWAY_ACCESS_TOKEN -or $env:GATEWAY_ENDPOINT) { throw 'Stale credentials retained' }
if ($env:PATH -ne $beforePath -or $env:KUBECONFIG -ne 'original-kubeconfig') { throw 'Operator environment leaked' }
Write-Output 'Failed profile cannot fall back to stale credentials'
""")
    assert result.returncode == 0, result.stderr
    assert "cannot fall back" in result.stdout
