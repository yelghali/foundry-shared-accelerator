# Lab: models, MCP and agents by identity scenario

[Demo overview](../README.md) | [Auth/code map](design.md) | [Deploy](deployment.md)

## Prerequisites

Run from the **demo folder**, not the parent accelerator repository.
Use PowerShell with the intended tenant login and an environment installed
from [requirements.txt](../requirements.txt).

The operator needs `az`, `kubectl` and `kubelogin`; `azd` is needed only for
`-UseAzd`. Install these tools or put them on PATH. An optional demo-local
`.local/tools` directory can hold them. The scripts do not look into the
parent accelerator for tools, dependencies or credentials.

For an existing deployment, the demo-local files must include:

```text
.local/apim-litellm/deployment.json
.local/apim-litellm/terraform-outputs.json
.local/apim-litellm/kubeconfig
```

The named scripts read the scenario's **consumer virtual key** through
operator AKS access and acquire a fresh caller token. They never use the
administrative master key as a consumer credential. Normal application
consumers should receive their issued key through an approved secret-delivery
mechanism, not Kubernetes access.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## What every live script checks

| Check | Evidence required, not just HTTP 200 |
|---|---|
| Model | Exact assistant output marker |
| MCP | Actual `whoami` identity/client/mode matches the selected scenario; real `read_blob` returns the seeded Blob marker |
| Prompt Responses | Completed assistant output and server reference `gateway-declarative` |
| Hosted Responses | Completed assistant output and server reference `gateway-hosted-dual` |
| Hosted private MCP | The actual hosted agent reads the Blob through private MCP, not just a successful direct gateway tool call |
| Prompt A2A job | Submit `returnImmediately=true`; poll the same Task ID with native `GetTask`; exact completed artifact |
| Hosted A2A job | Same job/polling sequence against the actual hosted agent |

A failed check stops the script and leaves a **failed**, not stale passed,
receipt. User-message history, echoed input, a JSON-RPC error inside HTTP 200,
or an LLM claiming its own identity does not satisfy a check.

### Verified live on 2026-11-27

All three named PowerShell scripts were run against the reviewed
`20261127a` deployment and passed **seven checks each (21 total)**:
actual model output, direct MCP identity/Blob, both agent Responses,
hosted-to-private-MCP Blob access, and both background A2A jobs.
Every job was polled to a completed artifact. Responses returned the actual
prompt version `1` and hosted version `5` in this run.

The separate deep verifier also passed all three profiles, including
anonymous/key/override denials and the private-network diagnostic. These
are observations for that deployment, not a guarantee of other subscriptions,
cross-user job isolation or production durability.

## Shared service identity

```text
Caller -> APIM /ai -> shared LiteLLM pod managed identity -> model / MCP / agents
```

```powershell
.\scripts\Test-SharedServiceIdentity.ps1 -UseAzd
```

Or run its readable Python source directly:

```powershell
.\.venv\Scripts\python.exe -m samples.test_shared_service_identity --use-azd
```

Read [the seven explicit checks and credential selection](../samples/test_shared_service_identity.py).
The caller token admits the client to APIM. It is not the workload token
used by LiteLLM to call the backend. MCP must report the shared managed
identity from the deployment's Terraform output.

Receipt: `.local/apim-litellm/shared-service/scenario-tests.json`.

## Dedicated service identity

```text
Caller -> APIM /ai-dedicated-application
          -> dedicated LiteLLM pod managed identity -> model / MCP / agents
```

```powershell
.\scripts\Test-DedicatedServiceIdentity.ps1 -UseAzd
```

Python source/command:

```powershell
.\.venv\Scripts\python.exe -m samples.test_dedicated_service_identity --use-azd
```

Read [the dedicated identity selection and checks](../samples/test_dedicated_service_identity.py).
This is **not consumer-app passthrough** and not one identity per agent.
The dedicated LiteLLM deployment invokes both agents with its own identity.
Separate namespace, identity and key store do not themselves isolate data
when the approved backend resources are shared.

Receipt: `.local/apim-litellm/dedicated-application/scenario-tests.json`.

## Delegated user identity

```text
Signed-in user -> APIM /ai-user-obo -> LiteLLM
                                      |-> model with pod identity
                                      |-> native MCP user OBO
                                      `-> custom agent user OBO
```

The operator must first configure the registrations, downstream delegated
permissions, consent, user/client allowlists, Key Vault credential and user
Foundry RBAC. Registration is not consent, and consent is not RBAC.

Sign in as the actual permitted user:

```powershell
.\.venv\Scripts\python.exe -m litellm_demo.entra_setup login `
  --identity .local\apim-litellm\entra-identity-user-consent.json `
  --token-cache .local\apim-litellm\user-obo\user-token-cache.bin

.\scripts\Test-DelegatedUserIdentity.ps1
```

Python source/command:

```powershell
.\.venv\Scripts\python.exe -m samples.test_delegated_user_identity
```

Read [the explicit cached-user selection and checks](../samples/test_delegated_user_identity.py).
It never substitutes an operator token or pod identity for a failed user
assertion. MCP must report the permitted user, the confidential gateway app
and delegated `tools.read`.

Receipt: `.local/apim-litellm/user-obo/scenario-tests.json`.

The hosted agent's own model/MCP calls still use its hosted runtime identity.
User invocation OBO does not imply user delegation into those downstream calls.

## Individual calls and visible agent jobs

The scenario scripts above are the recommended complete tests. For interactive
calls, load **one** profile in the same terminal:

```powershell
$py = ".\.venv\Scripts\python.exe"
.\scripts\Import-GatewayConsumerEnv.ps1 -Scenario shared-service -UseAzd
# Alternatively:
# .\scripts\Import-GatewayConsumerEnv.ps1 -Scenario dedicated-application -UseAzd
# .\scripts\Import-GatewayConsumerEnv.ps1 -Scenario user-obo
```

The loader only sets credentials/endpoint. It does not run inference.
Loading another profile replaces the prior terminal selection.

```powershell
# Model
& $py -m samples.consume_gateway --endpoint $env:GATEWAY_ENDPOINT --kind model --prompt "Reply exactly MODEL_OK"

# MCP discovery only (the named scripts also call whoami/read_blob)
& $py -m samples.consume_gateway --endpoint $env:GATEWAY_ENDPOINT --kind mcp

# Prompt / Responses
& $py -m samples.consume_gateway --endpoint $env:GATEWAY_ENDPOINT --kind prompt-agent-responses --prompt "Reply exactly PROMPT_RESPONSES_OK"

# Hosted / Responses
& $py -m samples.consume_gateway --endpoint $env:GATEWAY_ENDPOINT --kind hosted-agent-responses --prompt "Reply exactly HOSTED_RESPONSES_OK"

# Prompt / A2A background job and polling
& $py -m samples.consume_gateway --endpoint $env:GATEWAY_ENDPOINT --kind declarative-agent --background --poll-timeout 180 --prompt "Calculate 17 multiplied by 23. Return only the integer, no explanation."

# Hosted / A2A background job and polling
& $py -m samples.consume_gateway --endpoint $env:GATEWAY_ENDPOINT --kind hosted-agent --background --poll-timeout 180 --prompt "Calculate 17 multiplied by 23. Return only the integer, no explanation."
```

Expected arithmetic answer: **391** in completed Task artifacts. Omitting
`--background` keeps the blocking SendMessage behavior.

### Background A2A jobs: submit and poll

Every request must reach Foundry with `A2A-Version: 1.0`.

Submission:

```json
{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{"message":{"messageId":"<new-uuid>","role":"ROLE_USER","parts":[{"text":"Calculate 17 multiplied by 23."}]},"configuration":{"returnImmediately":true}}}
```

Poll the same endpoint using the actual returned Task ID:

```json
{"jsonrpc":"2.0","id":2,"method":"GetTask","params":{"id":"<actual-task-id>"}}
```

The scripts display initial and polled states. Fast jobs can already be
completed at submission; a submitted state is never fabricated. The HTTP
client is synchronous; the **agent job** is asynchronous because submission
and result retrieval are separate operations.

An absent A2A `agent_reference` is normal. Responses returns actual resource
references; A2A Tasks return Task/context/artifact data. The expected demo
name printed by the individual consumer is labeled a client-side convention,
not independent hosting attestation.

`HostedAgentNotSupported` / `-32099` mentioning **translated requests** can
indicate the older protocol path. Check the actual request leaving LiteLLM,
not only the caller's header: the pinned LiteLLM needs our
[GetTask compatibility patch](../gateway/foundry_a2a.py).
The native v1.0 method is `GetTask`, not `tasks/get`.

## Troubleshooting without inventing success

- **401:** refresh the selected caller login/token. If it persists, inspect
  the authentication boundary; expiry is not proven by a screenshot alone.
- **403 after changing the MCP backend URL:** reconcile native virtual-key
  object grants with `python -m litellm_demo.gateway_keys keys --scenario <deployed-identifier> --agent-responses`.
  Do not grant all servers as a workaround.
- **Wrong MCP identity:** stop. A correct-looking model answer does not prove
  delegated identity or the intended service identity.
- **Hosted latency:** cold runtime/session readiness and the container's own
  model call add work. Provider debug timings may overlap; do not sum them
  blindly. Background submission can still take seconds.
- **Timeout:** fail explicitly. A client timeout does not cancel the server
  job. Cancellation, crash durability and cross-user Task isolation are not
  certified by this demo.

Diagnostics never print credentials. Do not copy tokens, cookies, admin
passwords or full request headers into logs or customer messages.
