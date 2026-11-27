# Standalone AI gateway demo

**This folder is the demo, not the accelerator's platform/team lifecycle.**
It includes its own Python package, dependencies, Terraform modules, scripts,
tests, docs and diagrams. It does not import the accelerator package or call
Day 0/Day 1 scripts. Copy the folder to a separate repository when ready.

```text
Consumer -> APIM Standard v2 -> LiteLLM on AKS
                                 |-> private model
                                 |-> private FastMCP -> private Blob
                                 `-> private Foundry prompt / hosted agent
                                      Responses and A2A 1.0 jobs
```

No authentication sidecars are deployed. Some authentication is native
LiteLLM; the remaining gaps use clearly labeled custom Python inside its
process. Hosted agents use preview capabilities validated on the lab
subscription; this is not a production HA or exhaustive security certification.

## Choose one identity scenario

| Scenario | What the backend sees | Run its explicit test script |
|---|---|---|
| **Shared service identity** | Shared LiteLLM pod's managed identity | [Test-SharedServiceIdentity.ps1](scripts/Test-SharedServiceIdentity.ps1) |
| **Dedicated service identity** | Separate LiteLLM pod's managed identity, not the consuming app's identity | [Test-DedicatedServiceIdentity.ps1](scripts/Test-DedicatedServiceIdentity.ps1) |
| **Delegated user identity** | Signed-in user for MCP/agent invocation through OBO; direct models still see the pod identity | [Test-DelegatedUserIdentity.ps1](scripts/Test-DelegatedUserIdentity.ps1) |

Each script loads its own credentials and checks **model output, MCP caller
identity and real Blob access, prompt/hosted Responses, hosted private MCP, and prompt/hosted
background A2A jobs with GetTask polling**. No prior environment-loader command
is needed. No script silently skips a failed backend or falls back to another user.

## Start here

1. [Setup and run each scenario](docs/lab-guide.md).
2. [Understand native versus custom code, with source/config links](docs/design.md).
3. [Deployment and local-state guidance](docs/deployment.md).
4. [Hosted agent implementation](samples/hosted-dual-agent/README.md).

**Run all commands from this folder**, whether it is still under
`demos/apim_litellm` or has become its own repository:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Existing deployment and intended Azure login required for these live tests:
.\scripts\Test-SharedServiceIdentity.ps1 -UseAzd
.\scripts\Test-DedicatedServiceIdentity.ps1 -UseAzd
.\scripts\Test-DelegatedUserIdentity.ps1
```

These are real Azure calls and incur usage. The delegated-user script needs
the permitted user's encrypted login cache and completed consent/RBAC.
See the lab before running it for the first time.

## Where to look

```text
README.md                    Entry point and scenario names
docs/                        Tutorial, design, deployment, SVG/Excalidraw diagrams
samples/test_*_identity.py    One readable Python test flow per identity scenario
samples/consume_gateway.py   Individual model/MCP/agent calls and background jobs
samples/hosted-dual-agent/   Actual model-backed Foundry container
gateway/                     LiteLLM config, reviewed custom auth, FastMCP, images
litellm_demo/                Local operator utilities and shared HTTP assertions
scripts/                     Independent deployment, consent, test and UI commands
infra/                       Demo stacks and their folder-local resource modules
tests/ and gateway/tests/    Offline behavioral tests
requirements.txt             Self-contained dependency manifest
.local/                      Ignored state, receipts, kubeconfig and encrypted cache
```

The three scenario Python files deliberately spell out their own identity
selection and seven checks. Only HTTP/protocol assertions are shared in
[scenario_checks.py](litellm_demo/scenario_checks.py); there is no hidden
scenario-routing framework.

The deployed identifiers `shared-service`, `dedicated-application` and
`user-obo` remain unchanged to preserve Azure resources, URLs, key stores and
state. These are implementation identifiers, not additional identity scenarios.

## Diagrams

[![Private architecture](docs/diagrams/apim-litellm-private-demo.svg)](docs/diagrams/apim-litellm-private-demo.svg)

- [Identity and native/custom authentication](docs/diagrams/gateway-identity-scenarios.svg)
- [Registrations and consent](docs/diagrams/apim-litellm-demo-consent.svg)
- [Editable diagram sources](docs/diagrams)

## Offline validation

```powershell
.\scripts\Test-Offline.ps1
# After initializing the Terraform roots:
.\scripts\Test-Offline.ps1 -Terraform
```

Offline tests use mocks where appropriate and do not claim Azure acceptance.
The named live scripts write explicit running/passed/failed receipts under
`.local/apim-litellm/<deployed-identifier>/scenario-tests.json`.
Never commit `.local`, tokens, passwords or Terraform state.

Validation on 2026-11-27: **174 offline tests passed** (two native-schema
checks skipped), **six Terraform tests passed**, and all **21 named live
checks passed** against the reviewed runtime. A source-only copy outside
the accelerator also installed from this manifest and passed its tests with
no accelerator package available.

The root accelerator's native agent consumer and lifecycle are separate;
none of this demo's hosted samples are used for team onboarding.
