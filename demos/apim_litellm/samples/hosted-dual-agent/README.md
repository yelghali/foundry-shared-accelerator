# Model-backed hosted A2A + Responses demo agent

**Deployed and live-tested on private Foundry on 2026-11-25.** The original
hosted Echo remains unchanged as the known-working A2A transport test.

This sample composes `A2AAgentServerHost` and `ResponsesAgentServerHost`, with a
shared `respond` function. Normal text invokes the configured model using the
hosted runtime's Azure identity. `/mcp-read <blob-name>` explicitly calls the
private MCP `read_blob` tool using that runtime identity; it is a deterministic
tool command, not a claim of autonomous LLM tool planning or user OBO into MCP.
The hosted agent's own runtime identity authorizes both its model and MCP
calls, even when its inbound invocation uses user OBO.

`/network-check` probes only the fixed `https://example.com/` destination. It
requires public DNS resolution, returns `INTERNET_EGRESS_BLOCKED` on a connection/
read timeout, and fails if an HTTP response is received. It never accepts a
caller-selected probe URL. This is a demo diagnostic, not general proof of all
possible egress paths.

The image publisher declares **container protocols** A2A `1.0.0` and Responses
`2.0.0`, as well as both published endpoint protocols. Merely declaring a published
Responses endpoint on the old Echo container is insufficient.

## Actual Foundry API distinction

Live probes on 2026-11-25 established:

| Agent type | Endpoint | Result |
|---|---|---|
| Prompt agent | Project `/openai/v1/responses`, with `agent_reference` | HTTP 200 and expected output |
| Hosted Echo | Same project Responses endpoint | HTTP 400: hosted agents require their dedicated endpoint |
| Hosted Echo | `/agents/<name>/endpoint/protocols/openai/responses?api-version=2025-11-15-preview` | HTTP 400: container must declare Responses `2.0.0` |

Consequently, a LiteLLM named-agent entry that works for the prompt agent must
not be assumed to work for the hosted agent unchanged. Native provider routing,
authentication renewal, preview query parameters and output shape need separate
validation before exposing a hosted Responses alias.

## Deployment inputs

Use an existing, connected Foundry project. The tested SDK publisher can use
Azure CLI or an explicitly selected azd operator login.
The publisher supplies these runtime environment values:

* `MODEL_ENDPOINT`: base Azure OpenAI HTTPS endpoint, without `/openai/v1`;
* `MODEL_DEPLOYMENT`: the approved model deployment;
* `PRIVATE_MCP_URL`: optional fixed MCP endpoint; trusted HTTPS is preferred,
  while the current demo supports HTTP only to an explicit RFC1918 IP;
* `MCP_TOKEN_SCOPE`: that MCP API's token scope when MCP is enabled.

The hosted runtime identity needs model inference RBAC and explicit MCP
authorization. Private DNS, routing and Foundry/model TLS must work before
runtime tests. The sample does not copy an incoming user's token to MCP.

The tested private deployment uses the [image-hosted publisher](../../operator/demo_hosted.py)
and this sample's Dockerfile. The project endpoint and tenant are explicit CLI
arguments; there is no separate code-upload manifest to maintain. The publisher
declares `protocol_versions` for image hosting; the service rejects the
code-hosting-only `container_protocol_versions` field with an image definition.

The MCP target is the authenticated internal AKS listener, restricted to the
delegated subnet. It currently uses HTTP over that private path; use trusted
HTTPS for production. HTTP is accepted only for an explicitly configured
RFC1918 IP, not arbitrary public URLs.

LiteLLM exposes `hosted-agent-responses` at `/v1/responses` and `hosted` at
`/a2a/hosted`. Both call the dedicated private Foundry endpoint directly.
Prompt-agent MCP binding was attempted but is not part of the published
model-only prompt baseline; its native private-tool path remains unverified.

Only Responses POST generation is certified here. Streaming, response
retrieval/deletion, multi-turn memory integration and crash recovery are not
covered by this demo's protocol checks.

## Local protocol check

Install the declared requirements into an isolated environment, then run
`python demos\apim_litellm\demos\apim_litellm\samples\hosted-dual-agent\verify_local.py` from the repository root.
It uses the actual SDK A2A/Responses handlers with a deterministic mocked backend
and requires no Azure credentials. Both handlers passed this check on
2026-11-25. This does not test model inference, MCP networking or hosted runtime
deployment. Dependencies should remain isolated from the pinned working
LiteLLM scenario environment.

Live checks additionally exercised real model inference, saved A2A Tasks,
private MCP Blob reads and the public HTTPS denial diagnostic. Positive
single-user OBO invocation does not imply user delegation from this agent
into its downstream model/MCP calls.

SDK patterns:
[Responses getting started](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/agentserver/azure-ai-agentserver-responses/samples/sample_01_getting_started.py),
[multi-protocol composition](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/agentserver/azure-ai-agentserver-a2a/samples/02-multi-protocol/multi_protocol.py).
