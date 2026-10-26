# Native token attribution and chargeback boundaries

Azure-tested on **2026-10-23**. The implementation provides verified token
**showback** for the Entra-only test population, not a completed invoicing system.

## Gateway usage versus agent traces

The shared development environment has two workspace-based Application Insights
components in the **same** `log-<prefix>` Log Analytics workspace:

- `appi-<prefix>` is referenced by the AI Gateway's native OTLP exporter.
  Request and dependency telemetry appears here. Actual gateway token usage
  is measured through `azure.ai_gateway.client.token.usage` in the Azure Monitor
  workspace, **not** inferred from counting Application Insights requests.
- `appi-<prefix>-agents` is connected to each newly onboarded Foundry project
  using its managed identity. The project identity has Monitoring Metrics
  Publisher on this component. Foundry server-side agent traces/dependencies
  appear here after ingestion delay. Its local-authentication ingestion is
  disabled; no connection-string credential is distributed to teams.

The project connection must contain Application Insights connection metadata
even when its authentication is `ProjectManagedIdentity`. Terraform reads
that value as sensitive provider data and stores it in the **local team state**;
protect/migrate those state files before sharing the accelerator. The gateway
and agent telemetry use separate components to simplify filtering while
sharing workspace retention and ingestion billing. The default 30-day
workspace retention applies to both.
Team project authoring roles do **not** automatically grant permission to query
the shared agent Application Insights component. The signed-in platform
operator can inspect it; granting Log Analytics Reader to every team member
on that shared component would expose other teams' traces. Use a separate
per-team telemetry boundary if teams must inspect only their own traces.

See Microsoft's [Foundry tracing setup](https://learn.microsoft.com/azure/foundry/observability/how-to/trace-agent-setup)
and [managed-identity trace ingestion](https://learn.microsoft.com/azure/foundry/observability/how-to/trace-ingestion-entra-authentication)
for connection and query permissions.

For a new default-manifest team, `.\scripts\Accept-Team.ps1 -ProjectName
<team>` writes `<team>-accept-usage.json` after checking gateway inference and
identity-attributed native token metrics. Run the report again after new
traffic using `python -m accelerator.usage --deployment .local\deployment.json
--projects .local\teams\<team>.json --output .local\teams\<team>-usage.json`.
These are cumulative observations, not a per-month bill.

## Native pipeline

```text
Project MI / mapped application MI
              |
     Entra-authenticated model request
              v
       APIM AI Gateway
              |
       MI-authenticated OTLP
              v
    Data Collection Endpoint + Rule
          /                 \
Azure Monitor workspace    Log Analytics / App Insights
native token metrics       request logs and traces
          |
 tenant/object identity registry join
          |
 per-project input / output / total snapshot
```

An App Insights identifier alone did not configure native metric ingestion.
The working implementation provisions DCE/DCR/AMW, grants the gateway
Monitoring Metrics Publisher on the DCR, and configures complete OTLP endpoints.
The gateway uses `https://monitor.azure.com` as its export MI audience.
Payload capture is disabled.

## Exact tested metric and labels

The observed metric name is:

```text
azure.ai_gateway.client.token.usage
```

It retains dots. This exact selector worked:

```promql
{__name__="azure.ai_gateway.client.token.usage"}
```

Inactive identities drop out of an instant query's default lookback. The report
therefore uses the latest observed value within an explicit four-hour window:

```promql
last_over_time({__name__="azure.ai_gateway.client.token.usage"}[4h])
```

Returned timestamps are query evaluation times, not a fabricated last-request
time. This avoids losing an inactive team's measurements while preserving the
distinction between a snapshot and billing-period deltas.

The older `gen_ai_client_token_usage` example returned no data, and a broad
metric-name regex query was not implemented by the tested endpoint.
The report uses the exact observed metric rather than treating missing data
as zero.

Observed labels include:

- `azure.ai_gateway.identity.claims.tid`
- `azure.ai_gateway.identity.claims.oid`
- `azure.ai_gateway.api_key_id`
- `gen_ai.request.model`, `gen_ai.response.model`
- `gen_ai.operation.name`, `gen_ai.token.type`

`tid` and `oid` enrichment is explicitly configured on the native exporter.
The native metric also exports detail categories, including cache/reasoning and
audio/prediction details. The report sums core `prompt_tokens`,
`completion_tokens` and `total_tokens` separately; it does not add nested
details a second time.

## Stable ownership, not a trusted billing header

Onboarding receipts hold:

- Full project ARM resource ID and stable `chargeback_project_key`.
- Tenant and project MI principal ID.
- Optional mapped application/test-caller MI principal ID.
- Model alias, gateway and connection resource IDs.

Project names are display labels. Identity ownership must remain one-to-one
within the registry; the report rejects an identity assigned to two projects.
Keep effective-dated ownership history when moving cost centers.

`X-Chargeback-Project-Key` is context only. The live negative test sent beta's
header using alpha's authenticated managed identity:

```text
alpha caller total: 80 -> 90 tokens
beta caller total:  80 -> 80 tokens
measured response:       10 tokens
```

The native identity remained alpha. A caller cannot transfer ownership merely
by changing that header.

## Important claim-enrichment trust boundary

Claim enrichment is an **observed-claim telemetry feature**, not an
authentication/billing decision by itself. Do not accept arbitrary observed
claims from mixed API-key/Bearer traffic as financial authority.

The supplied showback is bounded to the verified test population:

- Applications and project connections use native Entra authentication.
- Native alias/tool roles and negative access tests were exercised.
- No application runtime keys are issued by the current workflow.
- Only the configured tenant, approved alias, registered identities and
  `api_key_id="unknown"` test records are assigned.
- Other/key-associated/unknown-identity core series remain unassigned.

The unused legacy key probe was suspended. The platform's administrative
built-in credentials are not distributed to teams.
Production must independently enforce/audit its authentication population
before promoting observed claims to financial chargeback evidence.
Privileged platform administrators remain part of the trusted control plane.

## Report and data quality

```powershell
python -m accelerator.usage `
  --deployment .local\deployment.json `
  --projects .local\teams\team-alpha.json .local\teams\team-beta.json `
  --probe-evidence .local\project-probe-usage.json `
  --output .local\usage-evidence.json
```

The report includes native rows, project totals, unassigned series, controlled
response-usage evidence and each model account's authoritative regional counters.
Controlled probes are not silently substituted for exported native metrics.

The metric was empirically cumulative for the tested series: alpha's extra
10-token request changed its native value from 80 to 90, not from 80 to 10.
The generated report is a **cumulative snapshot**, not a monthly billing-period
delta. Process restarts, resets, late ingestion and collection gaps require a
proper temporal accounting policy.

Missing project measurements are `unavailable`/`partial` with null counts,
not zero-spend success. Complete core categories must reconcile
`input + output == total`.
The live verifier fails if native attribution is incomplete or a configured
regional model account was never observed serving inference.

The real streaming client check used
`stream_options={"include_usage":true}`. Without that explicit option, the tested
client path did not receive a final usage object. Do not rely on release-note
claims of automatic usage injection for every client/backend combination.

## Regional and monetary allocation

One alias can route to both model regions. Project ownership does not change.
The actual backend resource/region is not inferred from the requested alias,
gateway region or configured routing weights.
The report reads each model resource's `TokenTransaction`,
`ProcessedPromptTokens` and `GeneratedTokens`.

Monetary chargeback additionally needs:

- Effective contractual rates, billing unit, currency and model/version/SKU.
- Regional attribution when rates differ.
- Cache/reasoning category semantics without double-counting.
- Capacity/PTU allocation where pricing is not pay-per-token.
- Gateway, network, runner and monitoring cost allocation rules.
- Azure Cost Management/provider billing reconciliation.

No prices are invented. MCP request volume is not model token usage.
The current output is suitable for a tested showback pilot; it is not a claim
that token estimates equal the complete Azure invoice.

References:

- [Current native claim telemetry and Entra release](https://ai.gateway.azure.com/docs/releases/2026-09-24)
- [Gateway observability](https://ai.gateway.azure.com/docs/observability)
- [Gateway Entra roles and token audience](https://ai.gateway.azure.com/docs/security-identity)
- [Regional design](regional-deployment.md)
