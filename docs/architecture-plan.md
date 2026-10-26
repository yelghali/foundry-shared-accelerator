# Verified shared Foundry architecture

Implementation and Azure validation date: **2026-10-23**.
This document replaces the initial capability assumptions with measured behavior.

## Resource boundaries

```text
CUSTOMER FOUNDATION: VNets / subnets / private DNS / routing / identities
                                  |
                         PLATFORM TERRAFORM
                                  |
      +---------------------------+-----------------------------+
      |                           |                             |
AGENT Foundry (PE + public) APIM AI Gateway              Native OTLP stack
  team projects             independent resource         DCE + DCR + AMW
  prompt agents             model alias + MCP             + Log Analytics
  curated Toolbox                |
      |                          +--MI + PE--> MODEL A / Region A
      +----BYOM, project MI----->+--MI + PE--> MODEL B / Region B
                                               secondary optional

Entra application -> gateway MCP asset -> private agent / Toolbox
```

The default contains three Foundry accounts: one shared agent account and two
model-only accounts. Models have private ingress but no agent subnet injection.
Only the agent account has `networkInjections[scenario=agent]`.
Both model accounts deny public/local-key access and have no team projects.

The independently managed gateway is
`Microsoft.ApiManagement/aigateways@2026-09-01-preview`.
Its current RP wire SKU is `Premium`. It is **not**
`Microsoft.ApiManagement/service`, and Foundry does not manage or attach it.

## Verified current capabilities

Older overview documentation lagged the current portal release. Azure tests and
the portal's September/October release documentation established:

- Native aliases with multiple backend model resource IDs, priorities and weights.
- Entra runtime authentication and asset-scoped Azure RBAC.
- Managed-identity Admin-connected BYOM/project connections.
- Native OpenAPI-to-MCP agent exposure and MCP federation for a private Toolbox.
- Native OTLP metrics/logs/traces and configurable token-claim enrichment.

The deployed API version is registered `2026-09-01-preview`; a future published
`2026-11-01` schema was reference material, not the deployed API version.

## Authentication and RBAC

Gateway token audience: **`https://azure-api.net/aigateway`**.
Managed identities use `https://azure-api.net/aigateway/.default`.
Delegated user tokens must include `Data.All`; the user's Azure CLI token was
verified to have the correct audience/scope.

| Principal | Scope | Role |
| --- | --- | --- |
| Model gateway MI | Each model-only Foundry | Foundry User for inference; no admin assignment |
| Project MI | Approved gateway alias | AI Gateway Models User |
| Test developer/caller MI | Own Foundry project | Foundry User; no sibling-project access |
| Consumer identity | Own published MCP server | AI Gateway Tools User |
| Gateway MI invoking an agent | Specific private agent | Foundry Agent Consumer |
| Gateway MI invoking Toolbox | Toolbox's project | Foundry User, the documented invocation requirement |
| Gateway MI exporting OTLP | DCR | Monitoring Metrics Publisher |
| Shared-Search project MI | Shared Search service | Search Index Data Reader; **cross-team reads possible** |
| Project MI | Own generated Search index | Search Index Data Contributor |
| Nominated team people/apps | Own Search index + Blob container | Index Data Contributor + Blob Data Contributor; no shared service author role |
| Project MI exporting agent traces | Agents Application Insights | Monitoring Metrics Publisher |
| Monitoring operator | Azure Monitor workspace | Monitoring Data Reader |

The private validation VM's system identity is trusted deployment automation.
The two attached user-assigned identities simulate separate team developers/
consumers and were used for actual own/foreign access tests.
Production agent-only consumers need the gateway asset role, not Foundry User.

Stable role IDs:

| Role | ID |
| --- | --- |
| Foundry User | `53ca6127-db72-4b80-b1b0-d745d6d5456d` |
| Foundry Agent Consumer | `eed3b665-ab3a-47b6-8f48-c9382fb1dad6` |
| AI Gateway Models User | `a6b68e3d-a1f0-4c60-8678-237c050cf4ea` |
| AI Gateway Tools User | `dce03f8d-857f-4e40-a90d-67770fa6ecd2` |
| Monitoring Metrics Publisher | `3913510d-42f4-4e42-8a64-420c390055eb` |
| Monitoring Data Reader | `b0d8363b-8ddd-447d-831f-62ca05bff136` |

User/group author assignments are not forced to `ServicePrincipal`.
Known managed identities use the service-principal replication bypass; other
principal types use normal Azure resolution.

## Connections, model administration and team autonomy

Platform administrators own native model deployments, quota, network bindings,
gateway catalog/roles and telemetry. Teams author/version agents in their own
projects and request approved connection/backend access through the workflow.
They do not receive model-account roles or shared-account Owner/Contributor.

Admin-connected model connection:

```text
category: ApiManagement
authType: ProjectManagedIdentity
audience: https://azure-api.net/aigateway
target: <actual-gateway>/default/models/openai/v1
model reference: platform-models-<project>/approved-chat
```

Connection metadata uses a JSON-string model catalog and `deploymentInPath=false`.
The connection is project-owned and contains no runtime key.
Connection names include the project name because the live service rejected a
reused short name across projects in the same shared account.

RemoteTool connections for curated tools similarly use project MI and the
native gateway audience. A connection record alone does not grant backend access.
Foundry User is not a secret-blind role; keeping connections credential-free
avoids using them to hide broad static keys.

## Three distinct gateway paths

1. **Models:** project BYOM connection -> logical alias -> selected regional
   deployment, authenticated by gateway MI.
2. **Agents:** consumer Entra token -> native OpenAPI-derived MCP tool ->
   selected private agent's stable Responses endpoint. The backend MI has
   agent-scoped consumer access.
3. **Toolbox:** consumer -> gateway MCP federation -> private project Toolbox ->
   RemoteTool MI connection -> gateway's approved agent tool -> private agent.

The Toolbox path deliberately uses different endpoints on each hop; it does
not route a toolbox back to itself. Its contents are versioned and allowlisted.

Native MCP servers set `subscriptionRequired=false` for Entra-only use.
The legacy `true` setting demanded a subscription key even when a valid Entra
token was present. Publication also checks that anonymous calls are rejected
and blocks a server if that check fails.

Gateway backend identity replacement is service authentication, not end-user
OBO. No claim is made that the backend authorizes as the original human.

## State and module ownership

| Surface | Controller |
| --- | --- |
| Test networks/DNS/peering | Optional simulator Terraform root/state |
| Customer production network | Customer's existing landing-zone controller |
| Accounts, model deployments, gateway, runner, OTLP resources | Platform Terraform |
| Project and initial author assignments | Reusable team Terraform root; separate state per team |
| Model/MCP catalog configuration | Explicit idempotent gateway release workflow |
| Agent/Toolbox definitions and selected versions | Python SDK release modules |
| Dynamic asset and agent consumer roles | Release workflow after resource IDs exist |

No SDK work is hidden in Terraform provisioners. Runtime receipts contain IDs,
endpoints, hashes and verification results, not credentials.
Project reruns preserve independently managed runtime fields when the project
and gateway IDs are unchanged.

New teams reuse the shared gateway by default. Optional dedicated gateway
profiles require a pre-created subnet. Retained alpha/beta examples preserve
their earlier dedicated profiles; Terraform `moved` blocks prevent replacement
when migrating the module addressing.

New teams also reuse shared Search for the accepted dev profile. Project
identities receive Search Index Data Reader at service scope because the
index-only grant failed IQ MCP retrieval. Shared Search does **not** isolate
team knowledge at the Search data plane; nominated authors retain index-scoped
grants and request KB creation from the platform. `-DedicatedSearch` at first
onboarding provides a separate service when isolation is required. Each
project gets a keyless AppInsights connection and publisher grant to
`appi-<prefix>-agents`. Gateway OTLP continues to reference the separate
`appi-<prefix>` component; both use one Log Analytics workspace.

Foundry IQ knowledge sources and knowledge bases live on Azure AI Search, not
as project-owned Foundry resources. The default source is the team's Blob
container; Search creates its index, and a knowledge base references that
source. Onboarding also connects the existing Search service to the project
as a keyless `CognitiveSearch` connected resource, so it can be selected under
Foundry **Build > Knowledge**. The separate `RemoteTool` connection exposes the
knowledge base's MCP endpoint to agents under **Tools**; seeing only that tool
does not mean the Search service is connected in the portal. Portal visibility
still depends on the signed-in user's permissions and private-network reachability.
For an already maintained Search index, the platform can instead provision a
search-index knowledge source and attach it to a knowledge base; this is not the
default Blob ingestion workflow. Index-scoped data roles govern direct index
operations, not the tested knowledge-base MCP retrieval path.

## Networking and operational controls

- Local Foundry authentication is disabled; the retained lab enables public
  Foundry authoring for experimentation alongside the private endpoint.
- Model PE NSGs permit gateway/validation paths and deny other subnet callers.
- The gateway frontend is public only for the authorized test profile.
- Private DNS and regional peering are customer inputs in production.
- The private validation VM has no public IP and is reached through Azure Run Command.
- Model PE creation follows deployment creation to avoid the observed account
  `Accepted`-state race.
- Saved Terraform plans are checked for deletion/replacement before apply.
- Azure-stamped public-IP `FirstPartyUsage` metadata is preserved; removing it
  caused an otherwise unnecessary IP replacement plan.

## Metering

The native collector requires DCE, DCR, Azure Monitor workspace and publisher
RBAC, not just an App Insights connection string.
The tested metric is **`azure.ai_gateway.client.token.usage`** and is queried
with an exact quoted selector because its name contains dots.

Project token showback joins observed tenant/object claims to the immutable
project identity registry. A live forged-header test charged the actual
alpha identity, not the beta project named in the header.
See [chargeback boundaries and metric queries](chargeback.md).

## Measured versus future scope

Verified: private resources, two active regional model backends, keyless BYOM,
project/agent creation and update, gateway-mediated consumption, Toolbox
federation, own/foreign authorization and native token attribution.

Future work: hosted-agent templates, onboarding form, production policy presets,
remote state bootstrap and full accounting reconciliation.
The MCP agent sample uses a nonstreaming Responses operation; it is not a
general raw HTTP/SSE proxy.
Two model regions do not provide full regional HA for the one agent/gateway region.

## Primary references

- [Current gateway Entra audience and asset roles](https://ai.gateway.azure.com/docs/security-identity)
- [Priority and weighted routing release](https://ai.gateway.azure.com/docs/releases/2026-10-16)
- [Entra and claim telemetry release](https://ai.gateway.azure.com/docs/releases/2026-09-24)
- [Native observability](https://ai.gateway.azure.com/docs/observability)
- [Admin-connected BYOM](https://learn.microsoft.com/azure/foundry/agents/how-to/ai-gateway)
- [Foundry roles and agent-scoped consumption](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry)
- [Regional and simulator contracts](regional-deployment.md)
- [Agent deployment contract](agent-deployment.md)
