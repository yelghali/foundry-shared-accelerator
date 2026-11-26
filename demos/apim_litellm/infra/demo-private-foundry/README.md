# Private Foundry extension for the standalone demo

**Deployed on 2026-11-25.** Private model inference, prompt/hosted Responses,
both agents over A2A, and hosted-agent access to private AKS MCP were exercised
live. The standalone gateway remains public-facing; Foundry public access is
disabled. This does not integrate with Day 0 or team onboarding.

This independent root adds a demo-owned Foundry account/project/model to the
standalone AKS/APIM VNet. It reuses the repository's infrastructure modules
without calling platform deployment or team onboarding.

```text
Public consumer -> public APIM -> LiteLLM on AKS
                                    |
                            private endpoint + DNS
                                    |
                      demo-owned private Foundry
                       models / prompt / hosted
                                    |
                      delegated agent VNet subnet
                       internet egress denied
                                    |
                       private HTTP MCP on AKS
```

The root configures `publicNetworkAccess: Disabled`, an account private
endpoint, a separate `Microsoft.App/environments` delegated agent subnet and
an NSG denying Internet outbound. It also creates a private Premium ACR and
its private endpoint for hosted image pulls. It does not enable public Foundry
access for bootstrap. ACR permits Azure's authorized import service; its public
network access and admin account remain disabled.

Inputs include subscription/tenant, operator object ID, unique account name,
the standalone gateway VNet ID, a nonoverlapping agent-subnet CIDR, and optional
existing private DNS zone IDs. The root links supplied zones to the gateway
VNet by default. If those exact links already exist under another owner, set
`link_existing_private_dns_zones=false`. Azure cannot link overlapping zones
of the same name to one VNet.

Use independent state and review the plan before applying. Do not mix this
root's state with the original platform, team or gateway root. The root currently
expects the standalone gateway VNet and its `private-endpoints` subnet to exist.
It is **not yet a single-command empty-subscription bootstrap**.

## Required acceptance before changing gateway targets

* Account public access disabled; inbound DNS from AKS resolves private IPs.
* Prompt and hosted agent private networking/capability dependencies provision.
* Agent runtime has private MCP DNS, routing, HTTPS trust and authorization.
* VNet-injected agents cannot reach an unapproved public destination.
* The hosted agent actually invokes the private MCP; network policy alone is
  not evidence of connectivity. Native prompt-agent MCP is a separate follow-up,
  not a capability of the published model-only prompt baseline.
* Responses and A2A aliases invoke actual agents through LiteLLM, with key
  isolation and the selected service/delegated identity.

Do not treat a successful Terraform plan or a private endpoint as proof of
these runtime properties. In this deployment, the hosted agent's explicit MCP
read passed. The attempted native prompt-agent MCP binding returned
`tool_user_error`; it is not enabled on the published prompt version. That
version is model-only and passes A2A/Responses tests. Do not claim native prompt
MCP access or full BYO Storage/Cosmos DB/Search data residency was validated.

## Deployment and runtime contract

Supply real inputs in an ignored JSON file, then review and apply:

Start from [the JSON example](private.tfvars.example.json). Fill in the
standalone gateway VNet and operator IDs; configure gateway principal lists
only for RBAC this root owns. Do not manage identical role assignments in both
the gateway and private-Foundry states.

```powershell
terraform -chdir=demos\apim_litellm\infra\demo-private-foundry init
terraform -chdir=demos\apim_litellm\infra\demo-private-foundry plan `
  -var-file="$PWD\.local\demo-private-foundry\private.tfvars.json" `
  -out="$PWD\.local\demo-private-foundry\private.tfplan"
terraform -chdir=demos\apim_litellm\infra\demo-private-foundry apply `
  "$PWD\.local\demo-private-foundry\private.tfplan"
terraform -chdir=demos\apim_litellm\infra\demo-private-foundry output -json private_foundry
```

The backend explicitly stores state at
`.local\demo-private-foundry\terraform.tfstate`. Do not pass the deprecated
`-state` planning flag or delete state to recover from an authentication error.
The initial `AADSTS50020` login blocker and state-path mismatch were resolved;
the account, model, project, capability host, private endpoints, DNS links,
runtime RBAC and private registry were subsequently deployed.

The `private_foundry` output supplies project/model endpoints and private
registry. Optional gateway principal sets grant model inference to A/B/C and
agent invocation only to A/B. C receives no workload-identity agent fallback.

Build the hosted image with the demo build registry, import it into the private
registry using `az acr import`, then run:

```powershell
python -m demos.apim_litellm.operator.demo_hosted `
  --project-endpoint "<private-project-endpoint>" --tenant-id "<tenant>" `
  --image "<private-registry>/hosted-dual:<tested-tag>" `
  --model-endpoint "<private-model-endpoint>" `
  --model-account-id "<private-model-account-ARM-ID>" --grant-model-access `
  --mcp-url "http://<internal-MCP-IP>:8000/mcp" `
  --mcp-scope "https://management.azure.com/.default" `
  --private-proxy-namespace gateway --use-azd `
  --output .local\demo-private-foundry\hosted-dual.json --apply
```

Without `--apply`, the publisher prints a plan without authentication/writes.
The operator-only network hop runs HTTPS requests through an existing connected
AKS pod, carrying credentials in stdin, not command arguments. It adds no
runtime intermediary between LiteLLM and Foundry. A VPN or connected runner is
an alternative.

## Measured boundaries

* AKS resolves `fsapriv1125.openai.azure.com` and
  `fsapriv1125.services.ai.azure.com` to private endpoint addresses.
* The authorized operator's public-network request returns HTTP 403:
  `Public access is disabled. Please configure private endpoint.`
* Private model and agent requests succeed through AKS.
* The hosted agent reads the actual Blob marker through private AKS MCP.
* A fixed unapproved public HTTPS request times out while the private positive
  controls succeed. The diagnostic requires public DNS resolution and checks
  HTTPS, not just a TCP socket accepted by a managed-hosting network layer.
  This is measured denial of that destination, not exhaustive egress certification.
* MCP uses an internal load balancer with source range restricted to the
  delegated agent subnet and `externalTrafficPolicy: Local`. This preserves
  source addresses for Calico and supplies appropriate LB health checking.
* **The MCP hop currently uses HTTP inside the private VNet.** It is not
  end-to-end TLS. Production must supply trusted private HTTPS before treating
  this as a hardened deployment. No public MCP listener was created.

The private ACR, model deployment and running AKS incur charges. Nothing is
automatically destroyed.

See [Foundry private networking](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/virtual-networks).
