# Regional models and independent landing-zone simulation

Implemented and Azure-tested on **2026-10-23**.

## Default and opt-out

| Configuration | Model resources | Routing |
| --- | --- | --- |
| Secondary enabled, default | Two accounts in distinct regions | One native alias, equal priority and weight `1:1` |
| Secondary disabled before initial deployment | Primary account only | One backend |
| Native multi-backend API explicitly rejects support | Keep resources; use primary-only alias | Recorded `single_backend_fallback` reason |

The live environment accepted the multi-backend alias and both regional model
accounts accumulated real inference tokens. No single-backend fallback was
needed. The rejection/fallback branch is covered by focused tests; permission
or malformed-input failures do not silently trigger fallback.

The default lab regions are East US 2 and Sweden Central. They are synthetic
test choices, not a production residency recommendation. `GlobalStandard`
processing geography is not constrained merely by account location.
Platform inputs support a different model SKU, quota/capacity and approved regions.

## Logical model path

```text
Shared AGENT Foundry
  Team A / B / C projects
  prompt agents + curated Toolbox
             |
     project MI, Admin-connected BYOM
             |
             v
  Standalone APIM AI Gateway
  alias: approved-chat
  default: load balance
         /          \
       MI            MI
       PE            PE
       |              |
  MODEL A          MODEL B
  Region A         Region B
  ingress only     ingress only
                   optional
```

Clients keep one model reference. They do not select physical account endpoints.
Each backend has compatible model/version/API capability and independent quota.
Priorities/weights are verified from the live alias readback. Regional resource
token counters independently proved both resources were serving requests.

Equal request weights do not imply equal token counts, latency or cost.
N-1 capacity and automatic failure recovery need explicit production testing;
this accelerator does not claim a tested regional outage/circuit-breaker SLA.

## Network ownership

```text
CUSTOMER NETWORKS: already exist in production

REGION A VNET                         REGION B VNET
  agent PE subnet                       model PE subnet
    -> shared agent account                -> MODEL B
  delegated agent egress                  no agent egress
  gateway outbound subnet
  gateway frontend public in test
  model PE subnet
    -> MODEL A
  private validation subnet
       |
       +--- approved peering / transit ---+

Central DNS zones and forwarding resolve both model hostnames from the
gateway's actual outbound network. Model resources themselves are PaaS,
not VMs injected into these PE subnets.
```

Only the shared agent account has an agent-compute subnet.
Model accounts have no projects/agent injection; they require PE ingress and
gateway inference permissions. The Foundry module accepts a separate PE
location because the PE NIC belongs to its VNet's region.

The simulator uses:

- Primary VNet `10.73.0.0/16`, secondary `10.74.0.0/16`.
- Separate agent PE, model PE, agent egress, gateway egress and validation subnets.
- Optional dedicated team-gateway subnets for the retained isolation examples.
- Central Foundry private DNS zones linked to both VNets.
- Peering, NAT for required outbound dependencies, and model PE NSGs.

Production consumes IDs instead and does not create/take over the customer's
VNets, peering, DNS zones or subnet delegation.

## Simulator and real accelerator remain separate

```text
OPTIONAL SIMULATOR                     CUSTOMER PRODUCTION
landing-zone-simulator/                existing landing-zone IaC
  own Terraform state                  customer network team
            |                                   |
            +------ same network-ID contract ---+
                              |
                    infra/stacks/platform
                    private accounts / models
                    gateway / OTLP / test runner
                              |
                       infra/stacks/team
                       project + scoped authors
                              |
                       agent release modules
```

Network contract:

| Input | Meaning |
| --- | --- |
| `primary_location` | Agent/gateway network region |
| `agent_subnet_id`, `agent_pe_subnet_id` | Existing agent egress and PE subnets |
| `gateway_subnet_id` | Existing dedicated gateway outbound subnet |
| `runner_subnet_id` | Private validation runner subnet |
| `private_dns_zone_ids` | Existing central Foundry zones |
| `models.primary` | Model location, existing PE subnet and optional PE location |
| `models.secondary` | Same fields, omitted entirely for single-region mode |
| `search_pe_subnet_id`, `search_private_dns_zone_id` | Existing Search PE subnet and `privatelink.search.windows.net` zone; required only when `enable_search=true` |
| `shared_storage` | Optional customer-owned account ID/name, Blob endpoint/DNS zone and container ID/name; grants Search MI reader at **container**, not account scope |
| `storage_account_id` | Simulator or customer Storage account ARM ID passed through as `deployment.storage_account_id` for external onboarding |

## Optional Search and knowledge inputs

All additions are off by default, preserving existing resource addresses and
plans. In a fresh simulator, set `enable_search_network=true` and
`enable_shared_storage=true` to provision a Search PE subnet, linked Search and
Blob private DNS zones, private Blob PE, and one `knowledge` container. The
simulator returns these IDs under `network`; it owns only **test** Storage.
The independent platform state consumes the resulting network object and
sets `enable_search=true`, optionally `enable_embedding=true`. Customer
production supplies equivalent existing network/Storage IDs instead; the
platform never provisions or takes over customer Storage.

The Search module creates a **Basic** service with system-assigned MI, disabled
public access/local keys, an inbound `searchService` private endpoint and DNS
zone group. The Search service can live in a different region from the VNet:
the private endpoint must use the **VNet's region**, not the Search region.
The `2026-09-01-preview` Search ARM API supports
`semanticSearch="standard"` and `knowledgeRetrieval="standard"` (paid);
the stable `2025-05-01` API has no `knowledgeRetrieval` property. Preview
availability, regional support, Basic-tier eligibility and paid usage must be
checked before enabling. `deployment.search` contains `{id,name,endpoint,principal_id,sku}` (or null
when disabled). A separate, optional primary model deployment of
`text-embedding-3-small` has configurable deployment name, version, SKU and
capacity; it leaves the existing chat-model deployment address untouched.
Embedding output exposes its deployment name and primary model endpoint.
The Search MI receives `Cognitive Services User` on the primary model account
when both features are enabled.

For private-VM Search object releases, the runner system MI gets `Search
Service Contributor` and `Search Index Data Reader` **only on this Search
service**. The latter is needed for live knowledge-base retrieval validation;
it is not granted to project identities. If a `shared_storage`
contract is provided, Search MI gets `Storage Blob Data Reader` **only on its
container**. No access is granted to arbitrary customer Storage. Creation of
knowledge sources/bases, indexes, data ingestion and credentials are separate
data-plane release activities, not Terraform side effects.

Inbound Search PE/DNS does **not** make Search's own outbound indexing traffic
private. For private Blob and private model enrichment, configure/approve
Search shared private links (and the necessary network rules/DNS/region
capabilities) before ingestion; granting MI RBAC alone does not create a
network path. Validate from the private runner that the Search hostname resolves
to a private IP and check Search-to-Blob/model outbound access separately.
The retained `fsa1023a` test environment has a running Basic Search service
in West Europe (East US 2 private endpoint), a private Blob account and
approved Search shared private links. A new `fsa1023a-delta` project was
onboarded with a project-specific container, generated index, knowledge base,
and index-scoped RBAC. The private runner uploaded `verification.txt`, ran the
indexer (one item processed), and retrieved its unique marker through the
knowledge base. The platform runner's service-scoped reader permission was
used for this retrieval, **not** the project identity. A delta agent using
its project-managed-identity IQ connection was then tested: the Search MCP
dependency returned 403 (wrapped as a 424 by Foundry) despite the project's
index-scoped Data Contributor grant. The failed `iq-probe` test agent was
removed. Thus index-scoped grants alone do **not** make the shared-pool KB
usable by that project's agent. For this **dev** environment the user accepted
service-scoped Search Index Data Reader for project identities. After adding
that role, delta's project-identity agent retrieved its KB successfully.
The shared service **does not isolate Search reads across teams**; its project
identities can read other teams' indexes and knowledge bases. New teams reuse
the shared service by default:

```powershell
.\scripts\Onboard-Team.ps1 -ProjectName my-team `
  -UserPrincipalIds "<team-user-Entra-object-id>" -Apply
.\scripts\Verify-TeamKnowledge.ps1 -ProjectName my-team -VerifyAgent
```

For team isolation, choose `-DedicatedSearch -SearchLocation westeurope` when
first creating a team. The team state then owns an additional Search service,
its private endpoint,
Search-to-Blob and Search-to-model private links, and the team project.
The project MI receives `Search Index Data Contributor` on its generated
index plus `Search Index Data Reader` on **only its own Search service**.
Nominated team principals receive index-scoped Data Contributor and
service-scoped Search Service Contributor so they can create knowledge bases
within that isolated service; this also permits creating other indexes **in
that team's service**. The platform runner receives author and reader roles
for provisioning and testing. This has a separate Basic service charge per
team and is not a zero-cost shared-pool option. Existing shared-pool projects
cannot be flipped to dedicated in place: move their knowledge data and
connections using an explicit migration.

The retained `fsa1023a-epsilon` project exercises this dedicated option. Its
private Blob upload and indexer ran successfully; a Foundry agent used the
project-managed-identity MCP connection to retrieve the team marker. Separate
team-alpha and team-beta managed identities were each denied (HTTP 403) when
calling epsilon's KB on the same private runner. These checks establish that
the tested epsilon project works and the two tested identities cannot retrieve
its KB; they do not prove every possible cross-team authorization path.
`fsa1023a-dev-one` and `fsa1023a-dev-two` use the shared service. Nominated
team authors retain index-scoped contributor roles but cannot create arbitrary
knowledge bases on shared Search: request more KBs with a knowledge manifest
through the platform. Dedicated Search authors can create KBs within their
own service; those KBs still need Foundry project connections for agent use.
For repeatable ingestion checks, run
`.\scripts\Verify-TeamKnowledge.ps1 -ProjectName <team>` from a workstation
with Azure CLI access. It writes/overwrites `verification.txt` in that team's
`<team>-docs` container, triggers ingestion, and checks the marker in the KB
response; do not run it against an unrelated or non-test container.

The AI Gateway frontend currently resolves to a **public IP** from the
private runner. For this experimentation deployment, the user accepted
**authenticated public AI Gateway ingress**; the gateway remains protected by
Entra authentication and gateway RBAC, and its outbound backend integration
remains private. The signed-in AI Gateway portal's Networking page explicitly
says **"Private endpoints aren't supported for AI Gateway resources yet"** and
shows public access as enabled and read-only. This is consistent with the
`aigateways` RP returning `AIGatewayPrivateEndpointsNotEnabled` for this
gateway's `/privateLinkResources` endpoint and rejecting a Terraform PE
attempt. `Microsoft.Network/AllowPrivateEndpoints` registration was requested
and remained `Pending`, but approval of that generic feature alone would not
establish support by the AI Gateway RP. There is therefore **no inbound
private-endpoint option** in this accelerator today. The gateway's outbound
private-backend integration does not make its frontend private. Azure must
add inbound Private Link support for this dedicated resource type before this
optional future enhancement can be implemented and verified. Do not disable
the public frontend before a supported, tested private replacement exists.
Classic API Management
`Microsoft.ApiManagement/service` private-endpoint instructions do not apply
to `Microsoft.ApiManagement/aigateways`. Microsoft's
[resource-type migration guidance](https://learn.microsoft.com/azure/api-management/ai-gateway-resource#limitations)
explicitly lists unsupported private endpoints as a limitation of the new
resource and advises not migrating designs that require them.

References: [Search ARM API preview](https://learn.microsoft.com/azure/templates/microsoft.search/2026-09-01-preview/searchservices),
[Search private endpoint](https://learn.microsoft.com/azure/search/service-create-private-endpoint),
[Search shared private link](https://learn.microsoft.com/azure/search/search-indexer-howto-access-private),
[Search RBAC](https://learn.microsoft.com/azure/search/search-security-rbac).

The pinned gateway API requires a subnet delegated to
`Microsoft.App/environments`. Some portal networking text described
`Microsoft.Web/serverFarms`; the deployment follows the tested ARM contract
and never guesses a production subnet delegation.

## Modules and lifecycle

```text
landing-zone-simulator/       optional Terraform root/state
infra/modules/
  foundry/                   agent or model mode, private PE
  ai-gateway/                dedicated gateway resource
  project/                   project, capability host, author roles
  native-monitor/            DCE, DCR, AMW and telemetry roles
infra/stacks/
  platform/                  shared infrastructure
  team/                      independent state per team
accelerator/
  agent.py / toolbox.py      SDK releases, not Terraform provisioners
  gateway.py                 native catalog and publication workflow
```

Create networks first, platform second, projects third and agents last.
Separate states do not automatically express every cross-state dependency.
Never destroy a VNet while Foundry/gateway/PE resources reference it.
The supplied scripts reject any plan containing deletion/replacement.
Opting out after a secondary already exists therefore requires a separately
approved decommissioning workflow, not this retained-test script.

New teams normally reuse the shared gateway and need no new subnet.
`Onboard-Team.ps1 -GatewayMode dedicated -GatewaySubnetId ...` is an explicit
alternative for an independent gateway/network/administrative boundary.

## Chargeback and limits

Keep the originating project identity across routing. Actual backend region
comes from resource counters/available telemetry, not a presumed weight split.
Do not invent a 50/50 financial allocation.
See [the native token report](chargeback.md).

Model-backend regional diversity is not full HA of the agent resource, gateway
or customer network hub. Do not replay an already-started stream or entire
agent/tool workflow as an undocumented failover strategy.

References:

- [Current priority/weight routing](https://ai.gateway.azure.com/docs/releases/2026-10-16)
- [Dedicated gateway contract](https://learn.microsoft.com/azure/templates/microsoft.apimanagement/2026-09-01-preview/aigateways)
- [Foundry networking](https://learn.microsoft.com/azure/foundry/agents/concepts/networking-options)
- [Model processing geography](https://learn.microsoft.com/azure/foundry/foundry-models/concepts/deployment-types)
