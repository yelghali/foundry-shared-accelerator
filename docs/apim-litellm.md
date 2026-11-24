# Alternative scenario: APIM Standard v2 + LiteLLM on AKS

**Current authentication profiles:** use the
[identity-scenario guide](gateway-identity-scenarios.md) for native OSS virtual
keys, persistent key storage, application isolation and user OBO. The November
20 evidence below describes the earlier APIM-MI/custom-auth profile. The current
guide records the separate November 24 live user-OBO acceptance after actual
interactive user consent, including delegated MCP and hosted/declarative A2A.

Release `20261124h` also replaces the historical A2A HTTP adapter below with
**direct LiteLLM -> Foundry HTTPS** routing, native workload-federation auth,
and an explicit in-process GetTask compatibility patch. Follow the current
guide for deployable configuration; the adapter remains only on A/B MCP paths.

This is an **additive scenario**, not a replacement for the tested native
`Microsoft.ApiManagement/aigateways` platform. It uses conventional
`Microsoft.ApiManagement/service` **StandardV2**, a separate Terraform root and
state, and separate runtime/release commands. Do not point the existing native
gateway publication or showback commands at this gateway.

## Architecture

```text
Entra consumer / external A2A client
                    |
          HTTPS + Entra access token
                    v
         APIM Standard v2 /ai
         validate tenant/audience/client
                    |
          HTTPS + APIM managed identity
                    v
         AKS: LiteLLM OSS
           |         |                  |
           |         |                  +--> Foundry hosted Echo, native A2A
           |         +--> FastMCP on AKS       / SendMessage and GetTask
           |              |
           v              v
  Shared Azure models   Dedicated Blob container
  LiteLLM workload MI   FastMCP workload MI
```

LiteLLM owns model routing, MCP federation and A2A registration. APIM owns
exposure and validates the incoming consumer token; it replaces that token with
its own managed-identity token for LiteLLM. This is **service identity**, not
human identity passthrough or OBO.

The LiteLLM pod includes a loopback-only identity adapter. It obtains renewable
Entra tokens for the configured Foundry/MCP endpoints using the pod's workload
identity. This avoids assuming LiteLLM's model credential chain is also used
by its A2A/MCP registry. The adapter has fixed targets, not a caller-controlled
URL. FastMCP uses a different workload identity to read its own Blob container.
No Azure API keys, consumer virtual keys, or SP client secrets are required by the
default workload-identity deployment. LiteLLM also requires a strong
administrative bootstrap key: the launcher generates one in memory on every
start, never logs or persists it, and continues to require the custom Entra
authentication handler for clients. No weak/unset-master-key bypass is enabled.
The TLS private key is necessarily a
Kubernetes Secret; "keyless" here refers to Azure authentication, not TLS.

## Public test profile and private target

| Surface | Initial public test | Private target |
| --- | --- | --- |
| APIM frontend | Public HTTPS, Entra required | Public or private according to consumer network design |
| LiteLLM | Public TLS test LoadBalancer plus an internal TLS LoadBalancer used by APIM; only APIM MI authorized | Internal TLS LoadBalancer and private DNS |
| FastMCP | ClusterIP only; callable through APIM/LiteLLM | Remains private |
| Shared Foundry/models | Reuse an explicitly selected public-enabled test account; Entra/RBAC required | Private endpoints, linked DNS and VNet reachability |
| Blob data plane | Private endpoint + private DNS, public ingress disabled | Already private |
| AKS API/ACR | Development defaults | Harden separately for a fully private enterprise platform |

Public mode does **not** make LiteLLM anonymous. Test applications use APIM;
direct consumer tokens are rejected by LiteLLM's APIM-identity allowlist.
The adapter port is loopback-only and is never exposed by the Service.
FastMCP has no public IP. Optional LoadBalancer source ranges can further restrict
LiteLLM's public surface.
APIM uses its outbound VNet integration and the additional internal Service even
in public test mode. External consumers still need only the public APIM URL.
Direct traffic to the raw public LiteLLM IP timed out in the test environment;
the public IP is retained for environments that permit it, not presented as a
verified alternative client entrypoint.

For easy development, an explicit Terraform switch permits the
`https://management.azure.com` audience and Azure CLI client ID at APIM.
LiteLLM additionally requires the APIM MI object ID, and FastMCP requires the
LiteLLM MI client and object IDs. **Do not carry this development audience into
production.** Use separate dedicated Entra API audiences and explicit consumer
application permissions. Built-in LiteLLM enterprise JWT/SSO features are not
required: the sample uses the OSS custom-auth extension with explicit route
and model allowlists. It is not a full multi-team authorization/billing system.

## Prerequisites

- Azure CLI signed in to the intended subscription; Terraform >= 1.9.
- Python dependencies:
  `python -m pip install -r samples\apim-litellm-requirements.txt`.
- `kubectl` and `kubelogin`; AKS local accounts are disabled.
- `azd >= 1.37.2` and `azure.ai.agents` extension `1.0.0-beta.23`
  for the demonstrated preview hosted-agent workflow.
- Permission to create resources and role assignments, plus access to the
  selected Foundry project and shared model account.
- A2A/hosted-agent preview enablement in the selected region. East US 2 and
  Sweden Central are the intended candidates; capacity must still be checked.

APIM, AKS nodes, ACR, Storage and hosted execution incur charges. Nothing is
automatically destroyed. Terraform state and generated certificates under
`.local/` must be protected. Use an approved remote backend for production.

## Deploy

### 1. Select the existing enterprise backends

Copy [the example inputs](../infra/stacks/apim-litellm/terraform.tfvars.example)
to a protected local JSON/HCL variables file. Supply the actual subscription,
tenant, globally unique name, publisher details, operator object ID, existing
model account and Foundry project IDs. The operator receives AKS Azure RBAC
Cluster Admin and container-scoped Blob Data Contributor for connected maintenance.
An independent `blob-seed` workload identity and short-lived AKS Job upload the
acceptance marker through the private endpoint. The running FastMCP identity
remains read-only.
The existing model/project resources are not imported, resized or recreated.

Choose a supported AKS node SKU; quota alone does not establish SKU availability.
The tested subscription rejected D4s v5 in East US 2; D4s v7 was used instead.

```powershell
.\scripts\Deploy-APIMLiteLLM.ps1 `
  -VariablesFile .local\apim-litellm.tfvars.json -Stage infrastructure

# Review the creation-only plan, then explicitly apply.
.\scripts\Deploy-APIMLiteLLM.ps1 `
  -VariablesFile .local\apim-litellm.tfvars.json -Stage infrastructure -Apply
```

The wrapper refuses deletion/replacement plans. Use an explicitly reviewed
Terraform workflow for intentional destructive changes.

### 2. Deploy the hosted Echo agent

Run from the sample directory. Using an existing manifest/environment avoids
template-copy initialization issues in ignored working directories.

```powershell
Push-Location samples\hosted-a2a-echo
azd env new gateway-demo --subscription "<subscription-id>" --location eastus2
azd env set FOUNDRY_PROJECT_ID "<project-ARM-ID>"
azd env set FOUNDRY_PROJECT_ENDPOINT "https://<account>.services.ai.azure.com/api/projects/<project>"
$env:AZD_ALPHA_ENABLE_AGENTS_A2A = "true"
azd deploy echo --no-prompt
azd ai agent show echo --output json
Pop-Location
```

The sample deliberately enables **both `a2a` and `responses` endpoint
protocols**: the tested preview rejected protected A2A card discovery when
Responses was not enabled, even though CLI A2A invocation worked.
Its explicit pip requirements avoid Oryx interpreting the PEP 621 manifest as
a Poetry project. Keep the current `azd` executable first on PATH: the extension
invokes `azd` again when acquiring credentials.

Echo returns a completed Task containing exactly `Echo: hello`. It has no LLM
dependency and does not implement file processing or streaming. Adapted from
the [upstream A2A Echo sample](https://github.com/Hameedkunkanoor/foundry-samples/tree/45c9459e8214038fbe5a00ae60a29cfa138aaae1/samples/python/hosted-agents/bring-your-own/a2a/echo).
The richer travel-planner sample is not deployed by this scenario.

### 3. Build and deploy AKS workloads

Use the actual A2A endpoint from the deployment (base path without its
`?api-version=v1` suffix), and the actual Azure OpenAI model endpoint.

```powershell
.\scripts\Deploy-APIMLiteLLM.ps1 `
  -VariablesFile .local\apim-litellm.tfvars.json -Stage workloads -Apply `
  -Exposure public -ImageTag 20261120h `
  -FoundryA2AEndpoint "https://<account>.services.ai.azure.com/api/projects/<project>/agents/gateway-a2a-echo/endpoint/protocols/a2a" `
  -ModelEndpoint "https://<account>.openai.azure.com/" `
  -ModelDeployment gpt-4o-mini
```

ACR Tasks builds the pinned runtime images; local Docker is not required.
`-SkipBuild` reuses a tag you have already built. Use a new immutable tag for
changed source; do not assume reapplying a Deployment pulls an overwritten tag.
The wrapper creates namespace/service-account federation bindings, TLS Secret,
ConfigMap, Deployments, Services and a FastMCP ingress NetworkPolicy. It uploads
the read-only tool's marker document through the seed Job and waits for completion.
The seed identity has only container-scoped data-contributor access; its Job uses
a separate service account and is garbage-collected after completion.

Public development creates a 30-day self-signed certificate for
`<name>.<region>.cloudapp.azure.com`. Its key is stored under the ignored
`.local\apim-litellm` directory and in the Kubernetes TLS Secret. Rotate it
explicitly; it is not an enterprise certificate lifecycle.

### 4. Publish APIM routes

```powershell
.\scripts\Deploy-APIMLiteLLM.ps1 `
  -VariablesFile .local\apim-litellm.tfvars.json -Stage release -Apply `
  -Exposure public
```

Terraform publishes `/ai` with GET/POST/DELETE passthrough operations and
managed-identity backend authentication. The public development release
The wrapper reads the non-secret `workload-backend.json` receipt generated by
the workload stage and uses the internal load-balancer IP for APIM. An explicit
`-BackendHostname` overrides that selection. The public test listener and private
APIM backend both use HTTPS port 443; the container listens on 4000. The public development release
explicitly disables APIM backend certificate chain/name validation for the
self-signed test certificate. Traffic is still encrypted; production must use
a trusted matching certificate and restore validation.

| Endpoint below the APIM `/ai` base | Purpose |
| --- | --- |
| `/v1/chat/completions` | Approved `enterprise-chat` model alias |
| `/blob/mcp` | Native Streamable HTTP MCP |
| `/a2a/echo` | Native A2A JSON-RPC |

Do not use a chat-completion wrapper as evidence that A2A Tasks work.

### 5. Run live acceptance

```powershell
python -m accelerator.apim_litellm verify `
  --deployment .local\apim-litellm\deployment.json `
  --output .local\apim-litellm\acceptance.json
```

Acceptance rejects anonymous calls on all three surfaces and unapproved model
aliases, checks the exact model marker and actual usage, initializes MCP and
reads the Blob marker, sends native A2A 1.0 and retrieves the same completed
Task/artifacts. HTTP 200 containing a JSON-RPC error is a failure.

Run `.\scripts\Test-APIMLiteLLM.ps1` for the focused offline suite, or add
`-Live` to run both offline checks and Azure acceptance. This is separate from
the default scenario's offline/live scripts.

Repeat after token expiry to test renewal without restarting pods. A single
A signed-in current `azd` can be selected explicitly with `--use-azd` if the
local Azure CLI credential process is unavailable. For AKS access the deployment
wrapper similarly accepts `-KubernetesLoginMethod azd`. A single
The selected caller credential's application ID must be in APIM's
`allowed_caller_client_ids`; an Azure CLI-only allowlist does not implicitly
authorize the separate Azure Developer CLI application. A single
successful short run does not prove token renewal, cross-user Task isolation,
streaming, file passthrough, or private-network operation.

## Private migration (later, not implied by public acceptance)

1. Establish Foundry/model private endpoints and private DNS resolution from AKS;
   verify reachability before disabling shared public access. Coordinate shared
   resources with their owner; this scenario does not change their network policy.
2. Link existing private DNS zones and optionally peer the gateway VNet using
   the Terraform inputs. Supply enterprise routing/firewall rules as needed.
3. Issue a trusted certificate for the private LiteLLM DNS name; replace
   `gateway-tls` and provide the matching private DNS record.
4. Re-render workloads with `-Exposure private`; the LiteLLM Service becomes
   an internal LoadBalancer. APIM Standard v2 retains outbound VNet integration.
5. Release with `-Exposure private -BackendHostname <private-DNS-name>`.
   This restores backend certificate validation. Verify APIM routing, anonymous
   denials, models, MCP and A2A again from authorized network locations.
6. Separately harden AKS API access, ACR networking, outbound egress,
   certificate rotation, HA, monitoring and deployment-runner connectivity.

Changing the LoadBalancer annotation can replace its IP. Plan DNS and APIM
release sequencing; do not switch shared Foundry ingress off prematurely.

## Service-principal alternative and limitations

Runtime credential helpers support `IDENTITY_MODE=service-principal` with
`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`. This is an explicit
alternative, not an automatic fallback on workload-identity failure. Bind
short-lived credentials using your approved secret-delivery mechanism; never
put them in tfvars, ConfigMaps, image layers or receipts. The default renderer
is workload-identity-only; SP Secret references require a deliberate manifest
overlay. Azure model authentication must be configured consistently as well.

Foundry sees the gateway service principal, not the original human. Do not
assume per-user storage isolation from the travel-planner sample survives this
identity boundary. Streaming is not advertised for the hosted preview agent.
File support, cancellation, registry discovery and full protocol parity are
not claimed.

## Live deployment and implementation findings

Validated in **East US 2 on 2026-11-20**, using APIM StandardV2, LiteLLM
`v1.110.2`, FastMCP `2.14.2`, release images tagged `20261120h`, and hosted Echo
version `2`. The separate resource group is `fsa-lit1120-apim-litellm`.

Public consumer base URL: `https://fsa-lit1120-apim.azure-api.net/ai`.
The original native Azure AI Gateway deployment was not replaced.

The successful non-secret receipt is `.local\apim-litellm\acceptance.json`:

| Live check | Result |
| --- | --- |
| Anonymous model, MCP and A2A calls | Rejected |
| Approved Azure model, Entra workload identity | Exact `GATEWAY_OK`, actual token usage returned |
| Unapproved model alias | Rejected |
| MCP initialize/list/call | Both Blob tools listed; `read_blob` returned `MCP_WORKLOAD_IDENTITY_OK` |
| Native A2A 1.0 SendMessage | Completed Task with exact `Echo: hello` |
| Native A2A GetTask | Same saved Task ID and artifact content retrieved |
| Bootstrap document upload | Separate workload-identity Job through Blob private endpoint |
| APIM backend connection reuse | Five successful requests, six seconds apart; no retry fallback |
| Public A2A agent card | Advertises APIM URL, protocol 1.0, streaming false |

Final validation: **89 Python tests and 7 subtests passed**, four Terraform tests
passed, PowerShell syntax checks passed, and a live Terraform rerun reported
**no changes**. The measured connection/card receipt is
`.local\apim-litellm\stability.json`. These short-run checks do not certify
token-expiry endurance.

Important details found by live iteration:

- Kubernetes service-link variables collided with FastMCP's port setting.
  Workload pods disable service links; DNS service discovery still works.
- Async Azure credentials require the explicit `aiohttp` dependency.
- LiteLLM's pinned release requires a compatible PyJWT and a strong bootstrap
  master key even when custom authentication is configured. The launcher
  generates the latter only in memory; no weak-key bypass is used.
- LiteLLM normalizes `GetTask` to legacy `tasks/get` and omits the native version
  header on raw task forwarding. The fixed-target adapter restores `GetTask`
  and sends exactly one `A2A-Version: 1.0` header. The catalog is pinned to 1.0
  with streaming disabled. Other untested protocol primitives are not claimed.
- LiteLLM's SendMessage serialization omits the optional default text media type;
  GetTask retains `text/plain`. Acceptance compares artifacts after normalizing
  only that default, still checking task ID, artifact IDs, text and other fields.
  This is not a claim of byte-for-byte transparent serialization.
- APIM tracing identified intermittent closed backend connections with Uvicorn's
  short default keep-alive. `KEEPALIVE_TIMEOUT=300` configures the supported
  LiteLLM server option; no retries of potentially side-effecting calls were added.
- Subscription-managed AKS Defender configuration is preserved on Terraform
  reruns rather than removed when its block was added outside this root.

The public APIM route is the supported external test entrypoint. Raw public
LiteLLM connectivity was not established; APIM uses its private VNet backend
instead. Full private Foundry/model ingress, token-expiry endurance, multipart
files and streaming remain separate, explicitly unverified follow-up checks.
Resources remain running and billable.

## Sources

- [AKS Workload Identity](https://learn.microsoft.com/azure/aks/workload-identity-overview)
- [LiteLLM A2A](https://docs.litellm.ai/docs/a2a)
- [Foundry A2A registration](https://docs.litellm.ai/docs/providers/azure_ai_agents#foundry-agents-over-a2a)
- [LiteLLM MCP configuration](https://docs.litellm.ai/docs/mcp_config_reference)
- [OSS custom authentication](https://docs.litellm.ai/docs/proxy/custom_auth)
