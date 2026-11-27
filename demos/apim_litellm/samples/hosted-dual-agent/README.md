# Model-backed hosted agent: A2A and Responses

This is the active hosted sample for the [standalone demo](../../README.md).
It is not a deterministic Echo server and is not used by accelerator team
onboarding.

## Three separate protocol layers

1. [Container code](src/main.py) composes `A2AAgentServerHost` and
   `ResponsesAgentServerHost`, both using the same model-backed response logic.
2. [Publisher](../../litellm_demo/demo_hosted.py) declares image-hosted
   container protocols `a2a: 1.0.0` and `responses: 2.0.0`.
3. The publisher enables A2A/Responses on the agent's published endpoint and
   selects the version receiving traffic. LiteLLM routes to those endpoints;
   it does not implement protocols missing from the container.

Use `protocol_versions` for this image-hosted definition. The platform
rejected the code-hosting-only `container_protocol_versions` field with an
image definition in the tested environment.

## Runtime inputs and identity

| Input | Meaning |
|---|---|
| `MODEL_ENDPOINT` | Approved Azure OpenAI HTTPS base, without `/openai/v1` |
| `MODEL_DEPLOYMENT` | Actual deployed model name |
| `PRIVATE_MCP_URL` | Optional fixed private MCP target |
| `MCP_TOKEN_SCOPE` | Scope for that MCP API |

The hosted **runtime identity** needs model RBAC and explicit MCP authorization.
Inbound delegated-user OBO does not automatically propagate to those downstream
calls.

Normal text invokes the model. `/mcp-read gateway-check.txt` performs an
explicit authenticated tool read, not autonomous tool planning. `/network-check`
probes only a fixed public HTTPS destination; it is not exhaustive egress
certification.

## Deploy and test

Run from the demo folder:

```powershell
.\.venv\Scripts\python.exe -m litellm_demo.demo_hosted --help

# Keep agent-server SDK dependencies isolated from the operator environment.
python -m venv .local\hosted-agent-venv
.\.local\hosted-agent-venv\Scripts\python.exe -m pip install -r samples\hosted-dual-agent\src\requirements.txt
.\.local\hosted-agent-venv\Scripts\python.exe samples\hosted-dual-agent\verify_local.py
```

The publisher requires an existing project, container image, explicit
tenant/model inputs and `--apply` for writes. Without it, it prints a plan.
Use the [deployment sequence](../../docs/deployment.md) for private ACR,
image-pull permissions, network prerequisites and runtime roles.

Local protocol validation uses real SDK handlers with a mocked model backend;
it is not Azure acceptance. The [named live scenarios](../../docs/lab-guide.md)
exercise actual model answers and background A2A submission/GetTask polling.
Dedicated hosted Responses uses
`/agents/<name>/endpoint/protocols/openai/responses` with the required preview
API version, not the project-wide Responses route.

Hosted capabilities are preview-dependent. Cancellation, crash recovery,
cross-user job isolation and streaming are not certified. Private MCP is
currently HTTP inside the VNet; use trusted HTTPS for production.

Official references:
[Hosted deployment](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent),
[incoming A2A](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint),
[SDK multi-protocol example](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/agentserver/azure-ai-agentserver-a2a/samples/02-multi-protocol/multi_protocol.py).
