# CI/CD releases (GitHub Actions and GitLab CI)

These workflows support **new deployments** from an existing customer landing
zone. They have not been run against a connected GitHub/GitLab repository yet.
Do **not** point them at the retained `fsa1023a` environment: that deployment
uses local Terraform state. The CI wrapper refuses a pre-existing platform
resource group or team project without the matching remote state/receipt.
Migrating existing states is a separate operator-reviewed exercise.

The three release lanes are:

| Lane | Reviewed input | Plan/approval | Apply and verification |
| --- | --- | --- | --- |
| Platform | `ci/network.json` (customer LZ contract) | Terraform plan, protected approval | Platform Terraform; approved gateway model alias and telemetry |
| Team | `ci/teams/<team>.json` | Terraform plan, protected approval | Project, Search KB and RBAC; private KB/agent/gateway acceptance |
| Operations | `ci/knowledge/<team>.json`, `ci/agents/<team>/<agent>.json`, or consumer object ID | Reviewed change and protected approval; Terraform plan for KB extension | Add KB, version/publish a prompt agent, or grant one app a gateway consumer role |

The **Teams-based human approval/front agent is future work**. Until then,
protect the apply environments and require human review of the configuration
change/plan. Do not configure an unprotected apply environment. The scripts
do not execute the landing-zone simulator.
GitHub pushes and pull requests and GitLab default-branch and merge-request
pipelines run [offline validation](../scripts/Test-Offline.ps1) for Terraform
format, provider validation and mock tests, PowerShell/Python syntax and
Python tests without logging in to Azure. This is separate from the original
lab's Bicep workflow and does not validate or deploy the LZ simulator.

## One-time Azure and repository setup

1. Create an **independent Azure Storage account/container** for Terraform
   state and CI receipts before running a workflow. Protect the container:
   Terraform state may contain sensitive connection metadata. Give the CI
   deployment identity **Storage Blob Data Contributor** on that container.
   The CI runner must be able to reach the state endpoint. If the state
   account is private-only, use a self-hosted runner with private access
   instead of an Internet-hosted runner.
2. Create a deployment app/service principal with **federated workload
   credentials** for your trusted CI ref/environment. It needs resource
   provisioning, role-assignment, gateway release, and Azure VM Run Command
   rights for the chosen subscription/scopes. Scope it according to the
   customer policy; it must not use a long-lived client secret. In GitHub,
   allow the `main` branch subject for plan jobs and the `fsa-platform` and
   `fsa-team` environment subjects for apply jobs. In GitLab, configure
   federation for the protected default branch with audience
   `api://AzureADTokenExchange`.
3. Configure these repository/project variables for **both** providers:
   `FSA_PREFIX`, `AZURE_SUBSCRIPTION_ID`, `AZURE_TENANT_ID`,
   `AZURE_CLIENT_ID`, `TF_STATE_RESOURCE_GROUP`,
   `TF_STATE_STORAGE_ACCOUNT`, `TF_STATE_CONTAINER`.
   Set `RUNNER_SSH_PRIVATE_KEY_B64` as a GitHub Actions secret or a protected,
   masked GitLab CI variable: the base64 encoding of one persistent,
   unencrypted SSH private key for the private runner VM. Retain this key
   securely for every release; a new key changes the VM login and can cause
   a Terraform replacement. Do not commit the key or its encoding.
   The Azure state container must already exist. The workflow sets
   `ARM_USE_OIDC=true` and `ARM_USE_AZUREAD=true`; GitLab also forwards
   its ID token as `ARM_OIDC_TOKEN`.
4. GitHub: use `main` as the trusted deployment branch (or adjust the
   workflow ref guards). Configure **required reviewers** for both GitHub
   environments `fsa-platform` and `fsa-team`, and restrict their branches.
   GitLab: use a runner tagged `fsa-azure` with Azure CLI, Terraform 1.16,
   PowerShell 7, Python 3.10+, `ssh-keygen`, and private state access where
   needed. Protect the default branch and environments `fsa-platform` and
   `fsa-team`; allow only designated platform approvers to run manual jobs.
   Use one CI provider per deployment prefix: GitHub concurrency and GitLab
   resource groups serialize releases within each provider, not across both.
5. Copy [the LZ example](../ci/examples/network.json) to `ci/network.json`
   and replace every placeholder with the customer landing-zone IDs.
   Review and commit it before requesting the platform release. No simulator
   state or network resources are created by these pipelines.

Remote state keys are `fsa/<prefix>/platform.tfstate` and
`fsa/<prefix>/teams/<team>.tfstate`. Nonsecret deployment/team receipts and
team Terraform variable snapshots live under adjacent blob keys. CI checks
for state/receipt mismatch and stops rather than applying with an empty state.
Existing local scripts still use git-ignored `local_backend_override.tf`;
do **not** copy that override or `.local/` into CI. `terraform init -reconfigure`
selects the backend for each job; never run `-migrate-state` in CI.

## Release sequence

1. **Platform:** Run **Release shared platform** in GitHub Actions, or a
   GitLab web pipeline with `FSA_ACTION=platform`. Review the plan, then
   approve the protected apply job. It writes the deployment receipt and
   configures the gateway alias; no separate `configure-models` CI job is
   needed.
2. **Team:** Copy [the team example](../ci/examples/team.json) to
   `ci/teams/<team>.json` and replace its placeholders. Explicitly choose
   `search_mode: shared` or `dedicated`; dedicated also needs
   `search_location`. Merge the reviewed change. Run **Onboard Foundry team**
   with `project_name`, or a GitLab web pipeline with
   `FSA_ACTION=onboard-team` and `PROJECT_NAME`. Approve only after reviewing
   the plan. Acceptance uses the private VM and the default KB names.
3. **Extend KB:** Copy [the complete manifest example](../ci/examples/knowledge.json)
   to `ci/knowledge/<team>.json`; include **all existing** team sources/KBs,
   not just additions. The release refuses omissions. Merge it and run
   **Release team operation** (`operation=extend-kb`) or set
   `FSA_ACTION=extend-kb` and `PROJECT_NAME` in GitLab. Its plan covers
   Terraform-managed containers/RBAC; Search knowledge objects are created
   at apply and do not appear as Terraform resources in the plan.
4. **Publish agent:** Commit a reviewed prompt template at
   `ci/agents/<team>/<agent>.json` (see [the template example](../ci/examples/agent.json)).
   Run `publish-agent` with `PROJECT_NAME` and `AGENT_NAME`. The private
   runner creates/ensures the agent from that template, verifies it, then
   registers and invokes its authenticated gateway MCP asset. A manually
   authored portal agent isn't imported by this automated template flow;
   use the [operator release process](agent-deployment.md) for that case.
5. **Grant an app consumption:** Run `grant-consumer` with team, published
   agent name and the app's **service-principal object ID**
   (`CONSUMER_PRINCIPAL_ID` in GitLab). It grants AI Gateway Tools User on
   that agent asset only, not Foundry authoring rights.

GitHub Actions files are under [`.github/workflows/`](../.github/workflows/).
GitLab uses [`.gitlab-ci.yml`](../.gitlab-ci.yml). Both call the same
[release wrapper](../scripts/Invoke-CIRelease.ps1) and onboarding scripts;
GitLab's protected jobs are manual, while GitHub waits for protected
environment approval. A branch/merge-request review is not, by itself, a
substitute for protecting those apply environments.
The apply job recalculates a Terraform plan against current remote state and
refuses deletion/replacement; it does not replay the saved plan from the
separate review job. Approval therefore does not pin the exact set of additions
and updates. Use a single protected release lane per prefix; exact plan
artifact approval/binding requires a separate enhancement before using this
for change-controlled production releases.
