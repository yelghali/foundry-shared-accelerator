# Deploy and operate the standalone demo

[Overview](../README.md) | [Design/code map](design.md) | [Live tests](lab-guide.md)

## Folder boundary

All commands run from this demo folder. The Python package is `litellm_demo`;
the dependency manifest, gateway sources and Terraform modules are local.
The accelerator repository, its Day 0/Day 1 scripts, and its state are not
runtime dependencies.

Required external tools/services remain explicit: Python, Azure CLI (or azd
for supported operator login), Terraform, kubectl/kubelogin, Azure subscription
permissions, Foundry/model capacity and hosted-agent preview enablement.
Self-contained source does not mean zero Azure prerequisites.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Infrastructure roots

| Root | Purpose | Resource modules |
|---|---|---|
| [infra/apim-litellm](../infra/apim-litellm) | APIM Standard v2, AKS, image registry, workload identities, Blob, profiles | Local root resources |
| [infra/demo-private-foundry](../infra/demo-private-foundry) | Demo-owned private Foundry/project/model/ACR and networking | [Local Foundry](../infra/modules/foundry), [local project](../infra/modules/project) |
| [infra/demo-foundry-model](../infra/demo-foundry-model) | Optional model deployment into an existing account | Local root resources |

The private Foundry root expects the standalone gateway VNet and explicit
nonoverlapping subnets/DNS inputs. Bootstrap the gateway with explicitly
supplied existing Foundry/model inputs when necessary, then create the
demo-private backend and update the routes. This is not a one-command
empty-subscription deployment.

Review the root's example inputs and Terraform plan before applying.
PowerShell deployment helpers do not apply without `-Apply` and reject
automatic resource deletion/replacement.

## Deployment order

1. Configure [gateway variables](../infra/apim-litellm/terraform.tfvars.example)
   and deploy the independent APIM/AKS infrastructure with
   [Deploy-APIMLiteLLM.ps1](../scripts/Deploy-APIMLiteLLM.ps1).
2. Deploy [private Foundry prerequisites](../infra/demo-private-foundry/README.md):
   project, model, private ACR, delegated agent subnet, private endpoints/DNS,
   gateway model/agent roles and the actual user's runtime RBAC.
3. Publish the model-only prompt agent using
   `python -m litellm_demo.gateway_agents --help`; its operator
   [publisher](../litellm_demo/gateway_agents.py) can use a connected private
   transport from an approved AKS namespace.
4. Build/import the [hosted dual-protocol image](../samples/hosted-dual-agent/README.md),
   publish it with [demo_hosted.py](../litellm_demo/demo_hosted.py), then grant
   its runtime identity model access and explicit MCP authorization.
5. Configure [Entra registrations/consent](#delegated-user-prerequisites),
   private MCP inputs and the named gateway profiles, then run each named
   live test separately.

For the gateway profiles, use current deployment-owned inputs, not copied
lab resource IDs:

```powershell
$env:FOUNDRY_AGENT_SUBNET_CIDR = "<delegated-agent-subnet-CIDR>"

# Confirm the receipt describes the active published instance in Foundry.
# Do not substitute the project managed identity for the agent instance.
$hosted = Get-Content .local\demo-private-foundry\hosted-dual.json -Raw | ConvertFrom-Json
@{
    namespace = "gateway"
    client_ids = @($hosted.runtime_client_id)
    principal_ids = @($hosted.runtime_principal_id)
} | ConvertTo-Json | Set-Content .local\apim-litellm\hosted-mcp-authorization.json

.\scripts\Deploy-GatewayScenarios.ps1 -PrivateDemo `
  -VariablesFile .local\apim-litellm.tfvars.json `
  -FoundryA2AEndpoint "<private-project>/agents/gateway-hosted-dual/endpoint/protocols/a2a" `
  -DeclarativeA2AEndpoint "<private-project>/agents/gateway-declarative/endpoint/protocols/a2a" `
  -ModelEndpoint "https://<private-model-account>.openai.azure.com/" `
  -IdentityFile .local\apim-litellm\entra-identity-user-consent.json -Apply
```

The script builds gateway/FastMCP images, renders the pod manifests,
reconciles native virtual-key grants and publishes APIM routes. It does not
create a gateway per team or run accelerator onboarding.

The renderer's operator reads the approved hosted MCP caller policy beside
the deployment receipt; `prepare --mcp-authorization <file>` can explicitly
select another policy. The policy adds only the verified caller IDs to the
named service namespace, not to delegated-user MCP. Missing private-hosted
authorization is an explicit preparation error rather than a silently
broken tool path. Explicit `MCP_FOUNDRY_CLIENT_IDS` /
`MCP_FOUNDRY_PRINCIPAL_IDS` operator inputs remain supported.

During cleanup, a stale local manifest initially dropped the hosted caller
allowlist. Live private-tool validation caught the 401/hosted 500. The active
agent instance was checked against live Foundry metadata, the narrow
allowlist was restored, and actual hosted private Blob reads passed again.
Obsolete project-managed-identity entries were removed rather than retained
as an unverified fallback.

## Delegated-user prerequisites

Use [Initialize-GatewayIdentity.ps1](../scripts/Initialize-GatewayIdentity.ps1)
and, when applicable, [Grant-GatewayConsent.ps1](../scripts/Grant-GatewayConsent.ps1).
Use each command's PowerShell help to provide the explicit tenant,
subscription, project scope and permitted-user inputs.

Three custom registrations have distinct roles:

| Registration | Purpose |
|---|---|
| Public test client | Interactive login; no client secret |
| Gateway resource/confidential app | Exposes `access_as_user`; performs downstream OBO |
| MCP resource API | Exposes `tools.read` |

The gateway app needs configured downstream MCP and Azure AI delegated
permissions. Tenant admin consent or the demo's explicit per-user consent
workflow must actually complete. Tenant policy can still require admin
approval; subscription Owner alone does not grant Graph consent authority.

The confidential app secret goes to private Key Vault and CSI, never YAML,
Git or a plaintext consumer env file. Complete real user login with the
[lab command](lab-guide.md#delegated-user-identity) before running user tests.

## Local state and extraction

All local files are ignored under this folder's `.local/`. Existing live
deployment receipts, encrypted cache and state must be transferred through
an approved secure channel when moving to another machine/repository.
They are not generated credentials for a new deployment.

- Gateway backend: `.local/apim-litellm/terraform.tfstate`.
- Private Foundry backend: `.local/demo-private-foundry/terraform.tfstate`.
- Existing-account model state: helper-selected path under
  `.local/demo-foundry-model/`.
- Never reuse saved plans generated before a folder/backend move.
- Existing copies in the parent repository's `.local/` are historical
  operator data, not a second environment to apply independently.

After source/state relocation, initialize the intended backend explicitly
and inspect it before planning. Do not accept an empty state for an existing
deployment, overwrite a differing state, or automatically approve migration
prompts without checking lineage and resource addresses.

```powershell
terraform -chdir=infra\apim-litellm init -reconfigure -input=false -lockfile=readonly
terraform -chdir=infra\apim-litellm state list
terraform -chdir=infra\demo-private-foundry init -reconfigure -input=false -lockfile=readonly
```

There is no automatic cloud resource migration merely because the source
folder moved. Stable deployed profile IDs/namespace names remain unchanged.

## Private admin UI

```powershell
.\scripts\Open-GatewayAdmin.ps1 -Scenario shared-service
```

Keep it running. The loopback UI bridge uses `http://localhost:4401/ui/login/`;
the upstream AKS tunnel is TLS-protected. Username is `admin`; the helper
exports the password only to a protected ignored file and can optionally
copy it to the clipboard. Never use that password as a consumer virtual key.
The UI bypasses APIM admission and is not the delegated-user scenario test.

## Validation and release checks

```powershell
.\scripts\Test-Offline.ps1
.\scripts\Test-Offline.ps1 -Terraform
.\scripts\Test-SharedServiceIdentity.ps1 -UseAzd
.\scripts\Test-DedicatedServiceIdentity.ps1 -UseAzd
.\scripts\Test-DelegatedUserIdentity.ps1
```

For deeper anonymous/key/override denial and private-network regression,
the operator CLI remains available:
`python -m litellm_demo.apim_litellm verify --help`.
Its deployed `--scenario` identifiers remain stable for state/key isolation;
the three named scripts are the clear first-line scenario examples.
