# 1. Deploy the shared platform

This step is performed once by the platform operator. Start here **after** the
customer landing zone exists; do not deploy the simulator over customer VNets.
The platform deploys the shared agent Foundry, private model backends, APIM AI
Gateway, private Search, monitoring and the private validation runner.

## Before running

- Install Azure CLI, Terraform >= 1.9, PowerShell 7+, Python 3.10+,
  `ssh-keygen`, and the Python dependencies in `requirements.txt`.
- Sign in to Azure, select the intended subscription, and have resource
  creation, RBAC assignment and provider registration rights.
- Verify available gateway SKU, model quota, runner VM SKU, private DNS,
  routing, and permission to join the existing delegated and PE subnets.
- Provide `.local/network.json` matching the
  [network-ID contract](regional-deployment.md#simulator-and-real-accelerator-remain-separate).
  It must include the agent, gateway, runner and Search PE subnet IDs, private
  DNS zone IDs, model-region/PE details, and the customer Storage account ID.
  The operator owns this file; the platform stack consumes the supplied IDs.

From the repository root in PowerShell:

```powershell
az login
az account set --subscription "<subscription-id>"
python -m pip install -r .\requirements.txt
```

If **testing without a customer landing zone**, the independent simulator
generates that contract:

```powershell
.\scripts\Deploy-Simulated-LZ.ps1 -Prefix myfsa01
.\scripts\Deploy-Simulated-LZ.ps1 -Prefix myfsa01 -Apply
```

Do not run the simulator if `.local/network.json` represents a customer
landing zone. Pick a unique prefix for a fresh deployment; do not change it or
the subscription for an existing state.

## Deploy and configure

```powershell
.\scripts\Deploy-Platform.ps1 -Prefix myfsa01
# Review the Terraform plan for the platform stack.
.\scripts\Deploy-Platform.ps1 -Prefix myfsa01 -Apply
python -m accelerator.gateway configure-models `
  --deployment .local\deployment.json --output .local\gateway.json
```

The first platform command plans without applying. The second provisions the
resources and writes `.local/deployment.json`; the final command configures
the approved gateway model alias. Terraform alone does **not** perform this
gateway release. Both platform and simulator scripts reject plans that delete
or replace resources. The optional `-SearchLocation` selects a supported
Search region, and `-RunnerSize` selects an eligible validation VM SKU.
The dedicated `Microsoft.ApiManagement/aigateways` gateway currently uses the
`Premium` wire SKU, not the older `AIGateway` literal or a conventional APIM
service instance.

Check `.local/deployment.json` for the account, gateway and Search references
and `.local/gateway.json` for the configured alias. A private validation runner
is provisioned without a public IP. `Test-Live.ps1` is an **extended lab test**:
it also creates the `team-alpha` and `team-beta` sample projects, so do not
mistake it for a prerequisite to onboarding a customer team.

This dev profile uses Entra-authenticated **public gateway ingress** and
private model backends. The shared agent Foundry also permits public authoring
in the lab; local keys are disabled. See [networking and regional limitations](regional-deployment.md)
and [usage measurement](chargeback.md) before using another environment.

For a broader lab verification, `.\scripts\Test-Live.ps1` tests private DNS,
model access, agent/Toolbox consumption, isolation checks and usage attribution;
it also creates sample teams. Offline checks are
`python -m unittest discover -s tests -v`,
`terraform fmt -check -recursive`,
`terraform -chdir=landing-zone-simulator test`, and
`terraform -chdir=infra/modules/foundry test`. Keep `.local/` state and runner
SSH material protected. The local backend and dev identities should be replaced
with approved production controls before production deployment.

Next: [onboard a team](onboard-team.md).
For a new deployment automated from an existing landing zone, see
[GitHub/GitLab CI/CD](ci-cd.md). It deliberately does not run the simulator
or adopt this lab's existing local Terraform state.
