# Standalone demo model prerequisite

This independent Terraform root deploys **one model into an existing Foundry
account**. It does not create/reconfigure a Foundry account, project, network,
team or gateway. It is not used by the main Day 0/team-onboarding workflow.

Use [Deploy-DemoModel.ps1](../../../scripts/Deploy-DemoModel.ps1) from the
repository root. Supply the existing account ARM ID, actual Azure OpenAI
endpoint, subscription and tenant:

```powershell
.\scripts\Deploy-DemoModel.ps1 `
  -SubscriptionId "<subscription>" -TenantId "<tenant>" `
  -ModelAccountId "<existing-account-ARM-ID>" `
  -ModelEndpoint "https://<account>.openai.azure.com/"
# Review, then repeat with -Apply.
```

Defaults are `gpt-4o-mini`, version `2024-07-18`, deployment `gpt-4o-mini`,
`GlobalStandard`, capacity 1. Confirm regional model/version availability,
quota, organizational policy and the correct account endpoint. Override the
model/version/SKU/capacity explicitly as needed. The script does not select a
different model silently or change public/private network configuration.

The operator needs model-deployment write access. Callers separately need
`Cognitive Services OpenAI User`. Hosted Echo does not need a model, but model
calls and the declarative agent do. If that agent uses a different model account
than LiteLLM's model route, provision/authorize its project model separately.

State and the nonsecret `model.json` receipt are under
`.local\demo-foundry-model\<account>\<deployment>\`. Do not share this state with
the platform, team or gateway stack. Configure a reviewed remote backend before
shared production use; the wrapper deliberately uses local demo state.

**Existing model deployment:** reuse its name/endpoint without running this
provisioner, or explicitly import it into this demo's Terraform state before
applying. Never attempt to take ownership of a model managed by another stack.
The wrapper rejects deletion/replacement plans and does not import automatically.

The receipt supplies `model_account_id`, `model_endpoint` and `model_deployment`
for the demo infrastructure inputs and `Deploy-GatewayScenarios.ps1`.
