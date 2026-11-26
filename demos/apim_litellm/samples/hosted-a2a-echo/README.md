# Hosted A2A Echo for the standalone demo

This is a custom Python agent container deployed on **Foundry**, not AKS.
It returns a completed native A2A Task whose artifact is `Echo: <input>`.
It has no LLM, Toolbox, MCP or Blob dependency. It is intentionally deterministic
so gateway tests verify transport, authentication and saved-task retrieval.

## Deploy to an existing Foundry project

From the repository root:

```powershell
.\demos\apim_litellm\scripts\Deploy-DemoHostedAgent.ps1 `
  -SubscriptionId "<subscription>" `
  -ProjectId "<existing-Foundry-project-ARM-ID>" `
  -ProjectEndpoint "https://<account>.services.ai.azure.com/api/projects/<project>" `
  -Location eastus2 -EnvironmentName my-demo
# Plan only. Repeat with -Apply to deploy.
```

The script uses this folder's checked-in [azure.yaml](azure.yaml) with `azd`;
it does **not** run platform deployment, team onboarding, `azd up` or provision
a new project. Use a distinct environment name for a different project; an
existing environment pointing elsewhere is rejected rather than retargeted.
It deploys the service `echo`, named `gateway-a2a-echo`, and writes a nonsecret
receipt at `.local\demo-hosted-agent\<environment>\hosted-agent.json`.
Use its `a2a_endpoint` as `-FoundryA2AEndpoint` for the gateway deployment.

Required:

* Existing Foundry account/project supporting hosted agents, and correct
  project ID/endpoint; the script checks subscription and matching project name.
* Subscription enabled for the hosted A2A private preview in the selected
  region. East US 2 was tested; Sweden Central requires its preview enablement.
  The region argument configures the CLI environment, not a project migration.
* `azd >= 1.37.2`, agent extension `azure.ai.agents >= 1.0.0-beta.23`,
  and operator login/permissions to deploy into that existing project.
  Install/sign in beforehand; the script does not silently install tools.
* Network access from the operator/build/runtime to required endpoints.
  For private-only Foundry, configure private DNS/reachability explicitly.

Both `a2a` and `responses` protocols are enabled, as in the upstream sample.
Protected A2A discovery is at `agentCard/v1.0`. A successful deploy is **not**
an invocation test: run the demo's live acceptance after wiring LiteLLM/APIM.
The script marks `invocation_tested: false` in its deployment receipt.

## What exactly does "adapted" mean?

Upstream reference, pinned for reproducibility:
[A2A Echo](https://github.com/Hameedkunkanoor/foundry-samples/tree/45c9459e8214038fbe5a00ae60a29cfa138aaae1/samples/python/hosted-agents/bring-your-own/a2a/echo).

The demo keeps the upstream **core execution pattern**:

1. `A2AAgentServerHost` hosts an `EchoExecutor`.
2. Extract user text with `get_message_text`.
3. Create/enqueue a Task using `new_task_from_user_message`.
4. Add a `text/plain` artifact with the `Echo:` prefix.
5. Complete it with `TaskUpdater`; cancellation reports not cancelable.
6. Bind the hosted container on `0.0.0.0`; use loopback outside hosting.

The local changes are small and visible:

| Local adaptation | Why |
|---|---|
| Service/project names and source layout | `a2a-echo` -> `gateway-a2a-echo`; source under `src` instead of upstream `src/echo-a2a` |
| Agent-card names/descriptions and error/log wording | Identify gateway acceptance and its text-only limitations; omit sample skill examples |
| Pinned local Python manifests | Reproducible preview dependencies; explicit pip requirements prevent the tested build from misidentifying the manifest as Poetry |
| Deployment wrapper and nonsecret receipt | Reuse an existing project and make its A2A endpoint easy to pass into the standalone demo |

**Not an adaptation:** enabling both endpoint protocols and disabling the
specified telemetry instrumentation already appear in the pinned upstream
manifest. The demo does not add a new reasoning workflow, change Echo into an
LLM agent, or deploy the upstream richer planner.

The LiteLLM workload/OBO authentication and `GetTask` compatibility code are
**gateway-side integrations**, not modifications to the hosted Echo algorithm.
See the [standalone demo README](../../README.md).

Known limits: no streaming/file processing and no LLM reasoning. Same-user saved
Task retrieval was tested through Foundry/LiteLLM/APIM. Cross-user isolation and
long token-expiry endurance remain separately unverified.
