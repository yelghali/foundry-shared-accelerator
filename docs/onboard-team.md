# 2. Onboard a team

The platform operator repeats this step for each team **after**
`.local/deployment.json` exists and the approved gateway model alias is
configured. Team names must be unique, lowercase and 3-40 characters.

## Choose the Search boundary first

The default reuses the shared Basic Search service. It is cheaper, but the
Foundry **Knowledge** view lists KBs from the whole connected service and
project identities have service-wide Search Index Data Reader. Other teams'
Search content is **not isolated** by the project name, index author grants,
or the team-specific MCP tool connection.

For a team-only Knowledge view and a Search data-plane boundary, use
`-DedicatedSearch -SearchLocation <region>` **at initial onboarding**.
This provisions another Basic Search service and incurs another service
charge. Switching an already-created shared team to dedicated Search is not
an automatic flag change: it requires a planned migration of sources, index,
KB, connections and identity grants. The existing gateway remains shared
unless explicitly configured otherwise.

## Plan, apply and accept

From the repository root in PowerShell:

```powershell
.\scripts\Onboard-Team.ps1 -ProjectName my-team `
  -UserPrincipalIds "<team-user-Entra-object-id>"
# Review the team Terraform plan.
.\scripts\Onboard-Team.ps1 -ProjectName my-team `
  -UserPrincipalIds "<team-user-Entra-object-id>" -Apply
.\scripts\Accept-Team.ps1 -ProjectName my-team
```

Add `-DedicatedSearch -SearchLocation <region>` to **both** onboarding
commands when choosing the dedicated profile. The script also accepts
`-GroupPrincipalIds` and `-ServicePrincipalIds` for **authors**; these are
Entra object IDs, not email addresses or application client IDs. Reruns keep
previously recorded principals. Review a deliberate revocation separately.

The default onboarding creates:

1. A project in the shared agent Foundry with named team members and an
   approved managed-identity model connection through the AI Gateway.
2. A `<team>-docs` Blob container, a Search knowledge source and generated
   index, and a real `<team>-kb` Foundry IQ knowledge base on Search.
3. A project Search connected resource for Foundry **Knowledge**, a separate
   `<team>-kb-iq` MCP connection for scripted agents, scoped author grants,
   and an agent tracing connection. The KB exists **once** on Search; these
   are two views/connections to it, not two copies.

`Accept-Team.ps1` uploads a test document, verifies private ingestion and
project-identity retrieval, versions and publishes a test `assistant`, calls
it through the authenticated gateway, and checks attributed token usage.
It assumes the default KB/container. For a custom `-KnowledgeManifest`,
provide tailored validation; the default acceptance script does not cover
arbitrary names. A separate team member should still sign in and check their
own portal and network access. Receipts are under `.local/teams/`.

**Additional KBs:** an approved knowledge manifest can name multiple sources
and KBs, but shared-Search teams request this through the operator. Index
Data Contributor does not grant knowledge-object creation rights. Dedicated
Search authors can manage knowledge objects on their own service under their
assigned Search Service Contributor role. An existing Search index is not
automatically adopted by passing its ID for author RBAC; making it a
knowledge source needs a separate approved workflow and a compatible index.

For the precise RBAC boundaries and the measured index-scoped retrieval
failure, see [architecture and responsibilities](architecture-plan.md).
Next: [what teams can do](team-guide.md).
For protected, operator-approved GitHub/GitLab releases, see
[CI/CD setup](ci-cd.md); Teams approvals are not yet implemented.
