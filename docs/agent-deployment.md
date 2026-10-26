# Agent and Toolbox deployment contract

Implemented and exercised in Azure on **2026-10-23**.

## Inputs: a receipt carries the real project identity

Do not confuse:

- Project ARM resource ID: Azure ownership and RBAC scope.
- Project endpoint URL: the SDK's data-plane target.
- Agent name/ID: a particular object inside that project.
- Agent version: a release, not a managed identity.
- Agent identity principal ID: an authorization subject, not an endpoint.
- MCP server resource ID: the gateway consumer authorization scope.

`accelerator.agent.validate_project` requires the account/project in the ARM ID
to match the endpoint and project name. The supplied account module uses the
account name as its custom subdomain.
The wrapper will not accept an arbitrary unrelated URL.

The team receipt supplies:

```text
id, name, endpoint, principal_id, chargeback_project_key
model_reference, model_connection_id, model_alias_id
gateway.id / endpoint / principal_id
```

Model/Toolbox URLs and IDs are generated from approved onboarding outputs,
not hard-coded into template instructions.

## Agent modes

| Mode | Behavior |
| --- | --- |
| `create` | Fail if the named agent already exists |
| `new-version` | Require the existing agent; optional expected ID must match |
| `ensure` | Reuse a matching definition hash; a changed definition requires explicit release |

An error/404 during an update never becomes silent creation in another project.
Definition hashes include the approved model reference and instructions.
The selected version is set to 100% on the stable Responses endpoint.
Previous versions are retained.

Actual validation included creating agents in separate projects and updating
alpha using its existing agent ID without deleting version 1.

## Direct SDK module

Run only from a network that reaches the project's private endpoint:

```powershell
python -m accelerator.agent `
  --project .local\teams\my-team.json `
  --name assistant --mode ensure `
  --template-file agent-templates\prompt-basic\template.json `
  --output .local\teams\my-team-agent.json
```

Supported deployment-process environment variables:

| Variable | Actual wrapper use |
| --- | --- |
| `ACCEL_AGENT_NAME` | Default for `--name` |
| `ACCEL_DEPLOY_MODE` | Default for `--mode` |
| `ACCEL_AGENT_ID` | Expected existing object for update validation |
| `ACCEL_TEMPLATE_FILE` | Optional version-1 prompt template JSON |

Other proposed environment names are not automatically SDK inputs.
Project identity/endpoint come from the explicit receipt, not from an opaque
project ID accidentally passed as a URL.

The prompt templates support:

```json
{
  "schema_version": 1,
  "kind": "prompt",
  "instructions": "You are the assistant for project ${PROJECT_NAME}."
}
```

Unknown fields and empty instructions are rejected. The model comes from the
approved project configuration, not a template-supplied physical backend.

## Private validation runner

For a newly onboarded team with the default knowledge manifest, run
`.\scripts\Accept-Team.ps1 -ProjectName <team>` to execute private indexing,
project-MI KB retrieval, an idempotent smoke-test agent release, gateway
publication/consumption, and the native token-usage check. On shared Search,
this deliberately does **not** assert cross-team KB denial: project identities
have service-wide Search Reader. The signed-in operator performs gateway
consumption; an actual team member should separately sign in and verify
their own project and private data-network access. Custom knowledge manifests
without `<team>-docs` and `<team>-kb` require tailored validation instead.

The lab VM has no public IP. Azure Run Command uploads source/configuration
without runtime secrets and uses its managed identity:

```powershell
$project = Get-Content .local\teams\my-team.json -Raw | ConvertFrom-Json
@{
  task = "deploy-agent"
  project = $project
  name = "assistant"
  mode = "ensure"
  marker = "PRIVATE_AGENT_READY"
} | ConvertTo-Json -Depth 30 |
  Set-Content .local\requests\my-team-agent.json -Encoding utf8NoBOM

.\scripts\Invoke-Runner.ps1 `
  -RequestFile .local\requests\my-team-agent.json `
  -OutputFile .local\teams\my-team-runner.json
```

For `new-version`, the request may include `agent_id` from the previous receipt.
The request may also include the parsed template object.

Dependencies explicitly include HTTPX/OpenAI because the first real Linux
bootstrap exposed a missing HTTPX dependency. No package installation is
hidden in Terraform; the lab runner bootstrap is a separate test workflow.

## Native gateway publication and consumption

```text
agent SDK release -> actual ID/version/identity
        |
stable private Responses endpoint
        |
native OpenAPI-backed gateway MCP server
        |
asset-scoped Entra consumer role
        |
samples/consume_agent.py
```

The native MCP endpoint is returned in the team receipt.
The sample initializes MCP, lists the approved tool, binds the pinned
`api-version=v1` when the generator omits its default, and calls it.
It validates a completed Foundry Response and assistant output, not an echoed
marker in an error message.

```powershell
python .\samples\consume_agent.py `
  --project .local\teams\my-team.json `
  --question "Reply with exactly READY" --expected READY
```

Additional registered agents have separate MCP bindings. `--agent-name`
selects their binding without replacing the project's existing default agent.
`--toolbox` selects a registered curated Toolbox.
New backend IDs/namespaces are unique per published agent; existing bindings
retain their names. Reusing a backend namespace caused a real 502 during the
multi-agent test and is now covered by regression tests.

After publication, `python -m accelerator.gateway grant-agent-consumer
--deployment .local\deployment.json --project .local\teams\<team>.json
--agent-name assistant --consumer-principal-id "<app-service-principal-object-id>"
--output .local\teams\<team>-consumer-grant.json` gives an app **AI Gateway
Tools User** only at that active agent tool-server scope. This is a separate
grant from project authoring (`Onboard-Team.ps1 -ServicePrincipalIds`).
The supplied sample tests with the signed-in operator; a real app must obtain
an Entra token for `https://azure-api.net/aigateway/.default` and pass its own
grant check after RBAC propagation.

## Curated Toolbox

```text
consumer -> gateway toolbox MCP facade
          -> private Foundry Toolbox, gateway MI
          -> project-owned RemoteTool connection, project MI
          -> approved gateway agent tool
          -> private agent -> BYOM model alias
```

`prepare-tools`, the SDK Toolbox release and `register-toolbox` are explicit
steps in the workflow. The toolbox is versioned by definition hash and contains
an allowlisted read-only team agent tool.
Gateway MI receives Foundry User only on that Toolbox's project.
No self-referential endpoint or user OBO claim is introduced.

## Output and partial failure

Nonsecret receipts record actual IDs, version, selected endpoint, definition
hash, model reference, gateway binding and verification result.
An active agent with failed gateway publication is not reported as overall
success. Publication receipts remain pending until runtime consumption passes.
No direct private endpoint is exposed as an undocumented fallback.
No failure cleanup deletes previous versions or Azure resources.

Hosted-agent containers are not implemented by these prompt templates.
If added later, consume platform-injected `FOUNDRY_*`/`AGENT_*` values rather
than overwriting them; keep deployment-process configuration separate.

References:

- [Agent endpoint and identity lifecycle](https://learn.microsoft.com/azure/foundry/agents/how-to/migrate-agent-applications)
- [Hosted environment reserved values](https://learn.microsoft.com/azure/foundry/agents/how-to/configure-hosted-agent-env-variables)
- [Toolbox authentication](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/tool-authentication)
- [Keyless Admin-connected model shape](https://github.com/microsoft-foundry/foundry-samples/blob/main/infrastructure/infrastructure-setup-terraform/46-bring-your-own-models/code/development-connections.tf)
