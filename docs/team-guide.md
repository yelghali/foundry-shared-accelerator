# 3. Build and use a team's agents

After [onboarding](onboard-team.md), sign in to the assigned Foundry project.
The team has **Foundry User** for agent authoring and an approved gateway
model connection; it does not create shared model deployments or change the
platform's gateway/model catalog. The operator supplies additional approved
connections and shared-Search KBs. Team authors can write their allocated
Blob container and generated Search index. Dedicated Search grants broader
Search object management **only on that team's service**.

## Use the team's knowledge

Foundry IQ knowledge bases live on Azure AI Search. In Foundry **Build >
Knowledge**, select the project's connected Search service and the team's
`<team>-kb`. Shared Search also shows other teams' KBs in that service;
visibility is not a team filter or an access boundary. With dedicated Search,
the list contains only that service's knowledge objects.

The same KB appears under **Tools** as `<team>-kb-iq`: this is its
managed-identity MCP endpoint, not a second KB or a second index. A team can
attach the KB while authoring an agent in Foundry, or a script can attach the
MCP connection to an agent definition. The project connection is reusable,
but only agents explicitly configured with it call the KB. Avoid attaching
both routes to the **same KB on the same agent** unless intentionally testing
two retrieval paths. The validated SDK example uses the MCP route; native
portal authoring is a team workflow, not a separate deployed KB.

The default KB uses `minimal` reasoning and extractive results: it does
**not** enable LLM query planning. Upgrading to low/medium reasoning and an
approved KB model is a separate platform change. Agent instructions/model
can still reason over the retrieved passages. See [the architecture](architecture-plan.md)
for the current configuration.

## Author, publish and consume

Teams can create and version prompt agents in their own project, including
from the supplied [templates](../agent-templates/). For SDK releases from a
network with project access, follow the [agent deployment guide](agent-deployment.md).
`Accept-Team.ps1` already publishes a smoke-test `assistant`; other
production-intended agents need an explicit gateway registration and runtime
test. Merely creating an agent in Foundry does **not** publish it to the
gateway. The operator uses `accelerator.gateway register-agent` with the
actual agent release receipt, as described in the deployment guide.

After an agent is published, the signed-in operator can test it:

```powershell
python .\samples\consume_agent.py `
  --project .local\teams\my-team.json `
  --question "Reply with exactly READY" --expected READY
```

Use `--agent-name` for another registered agent or `--toolbox` for an
approved Toolbox. For a consuming application, the operator grants access to
**one published agent asset**, not project authoring:

```powershell
python -m accelerator.gateway grant-agent-consumer `
  --deployment .local\deployment.json --project .local\teams\my-team.json `
  --agent-name assistant --consumer-principal-id "<app-service-principal-object-id>" `
  --output .local\teams\my-team-consumer-grant.json
```

The app authenticates with Entra to the gateway audience
`https://azure-api.net/aigateway/.default`. This consumer role is distinct
from onboarding's `-ServicePrincipalIds` author grant; it does not require
Foundry User just to call the gateway. The gateway currently has authenticated
public ingress, while model backends are private.

## Operations and limits

The operator can inspect project-attributed gateway token usage through the
[showback workflow](chargeback.md). Foundry agent traces go to a separate
Application Insights component in the same Log Analytics workspace; team
authors are not automatically monitoring readers. Ask the operator for an
approved reader assignment and treat traces as potentially sensitive.

The current workflow does not provide a self-service onboarding form,
automatic KB request approvals, or automatic publication of every agent.
Request a new KB, model or external connection from the platform operator.
