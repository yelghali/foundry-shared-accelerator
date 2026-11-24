# Standalone APIM + LiteLLM demo infrastructure

Entry point: [demo README and auth diagrams](../../../scenarios/apim-litellm/README.md).
This root is not wired into the main platform deployment or team-onboarding
scripts; keep its state and deployment lifecycle independent.

Independent Terraform root; does not change or consume the native `aigateway` scenario's state. Creates its own resource group, AKS, ACR, Standard v2 APIM, workload identities, and private-access blob container. Existing Foundry/model resources are reused, not created or reconfigured. Role assignments, optional peerings, and DNS links on existing resources are owned by this root.

## Provision

Copy `terraform.tfvars.example` to `terraform.tfvars`, supply real IDs, then:

```powershell
terraform init
terraform fmt -check -recursive
terraform validate
terraform test
terraform plan -out gateway.tfplan
# Review before applying; no cloud apply is performed by repository validation.
terraform apply gateway.tfplan
terraform output -json deployment_contract
```

Default state is local and **separate** from existing roots. Configure an independently secured remote backend before shared use; never reuse platform/team state. Do not commit state, plans, or real tfvars.

The deploying principal needs resource creation privileges plus role-assignment write permission at the existing model account and Foundry project. Pre-register Microsoft.ContainerService, ContainerRegistry, Network, Storage, ManagedIdentity, ApiManagement and Web (automatic registration is disabled). Optional peering/DNS inputs require write access on both VNets and the existing DNS zones.

## Inputs and deployment contract

Required: `subscription_id`, `tenant_id`, globally unique lowercase `name`, `publisher_email`, `operator_object_id`, `caller_audience`, `allowed_caller_client_ids`, `mcp_service_token_audience`, existing `model_account_id` and `foundry_project_id`. `litellm_token_audience` remains a deprecated alias for the MCP service audience; it no longer describes an APIM-to-LiteLLM token. See variable descriptions and example for optional node sizing, location, tags and network CIDRs. Default pod/service CIDRs are 10.244.0.0/16 and 10.245.0.0/16; VNet/peer address spaces must not overlap these or each other.

`deployment_contract` contains:

* `resource_group_name`, `location`, `tenant_id`.
* `aks`: `name`, `id`, `oidc_issuer_url`, `subnet_id`, `subnet_cidr`, `namespace`.
* `acr`: `name`, `id`, `login_server`. Deployment publisher separately needs AcrPush.
* `workload_identities.litellm`, `.fastmcp`, and `["blob-seed"]`: `client_id`, `principal_id`, `resource_id`, `service_account`, `namespace`. The last identity belongs only to the private-network seed Job.
* `apim`: `name`, `id`, `gateway_url`, `principal_id`, `caller_audience`, `litellm_token_audience`, `foundry_token_audience`.
* `model_account_id`, `foundry_project_id`, and `blob.account_name`, `.endpoint`, `.container`.
* `mcp_service_token_audience`, used only for A/B service authentication to FastMCP.

`enable_identity_scenarios=true` adds `identity_scenarios` and `obo_key_vault`
outputs, separate B/C gateway managed identities, namespace-specific federated
credentials and a private Key Vault with CSI integration. See the
[identity scenario runbook](../../../docs/gateway-identity-scenarios.md).

Runtime release creates namespace `gateway` and service accounts `litellm`/`fastmcp`, annotates each with `azure.workload.identity/client-id` from the corresponding output, and labels pods `azure.workload.identity/use: "true"`. LiteLLM gets Cognitive Services OpenAI User on the model account and Foundry Agent Consumer on the project; FastMCP gets **read-only, container-scoped** Storage Blob Data Reader. The nominated operator gets container-scoped Blob Data Contributor to upload acceptance content and AKS Azure RBAC Cluster Admin to release workloads.

## Authentication and API release boundary

APIM owns exposure and caller JWT validation. LiteLLM OSS owns native virtual-key
admission and backend permissions. APIM preserves the caller Authorization
header and forwards `x-litellm-api-key`; no APIM managed-identity token replaces
the caller. The explicit `allow_development_management_audience` flag permits
the management audience for A/B tests. Production uses a dedicated API audience.
The separate Entra setup script owns registrations and explicit consent; this
Terraform root does not store app passwords or grant tenant consent.

Global APIM policy validates tenant, audience and allowed client application IDs. Every released API must inherit `<base />` in inbound policy; do not override this protection. This is a client-application allowlist, not per-user authorization. Add user scopes/roles where required.

No API/backend URL is fabricated before workloads exist. Set
`litellm_backend_url` for A's `/ai` API and `scenario_backend_urls` for B/C's named
APIs after deployment. Policies inherit JWT validation, require a virtual-key
header and block administrative routes. C additionally requires
`user_obo_gateway_audience` and delegated `access_as_user`. HTTPS is mandatory.
`development_self_signed_tls` is A's explicit test bypass;
`scenario_development_self_signed_tls` supports independent B/C TLS migration.
Production uses trusted matching certificates and disables each bypass.

FastMCP and A2A are always routed through LiteLLM. A/B use native workload
federation directly for Foundry agents; only their MCP calls use the HTTP
adapter. C uses native MCP Entra OBO and custom in-process A2A OBO, with no
adapter container. All agent URLs point directly to Foundry. C's gateway MI has
model/Vault access, not fallback agent access. APIM
has no direct Foundry grant. Endpoint URLs remain explicit runtime inputs.

## Network and limitations

Standard v2 uses **outbound VNet integration**, delegated `Microsoft.Web/serverFarms` subnet and associated NSG, not classic internal APIM injection. Public APIM inbound remains enabled; default NSG outbound rules permit VNet, Key Vault and public dependencies. AKS uses an explicit Standard LoadBalancer outbound path for image/package/Entra dependencies, Azure CNI overlay, Entra RBAC, OIDC and workload identity. Local AKS accounts and ACR admin credentials are disabled. AKS control plane and ACR endpoints are public for this demo; this is not full private-link hardening. Blob uses a private endpoint, a linked private DNS zone and disabled public ingress. Its separate seed identity has container-scoped contributor access; FastMCP retains container-scoped reader access.

For private-only model/Foundry accounts supply `existing_vnet_id` and all relevant `private_dns_zone_ids`. The root creates reciprocal same-subscription peering and links zones to its VNet; it does not change existing DNS records, endpoint ACLs, firewall rules, custom DNS forwarding, or build transitive hub routes. Existing public-enabled accounts need neither input, but their firewall must permit the new outbound path. Do not assume the existing native account allows public access (its module defaults to private-only).

AKS access uses the nominated operator's Azure RBAC assignment or supplied administrator groups and `kubelogin`; no admin kubeconfig is output. Regional SKU availability, quota, RBAC propagation, Entra consent and actual private DNS/backend reachability require a reviewed cloud deployment and end-to-end smoke test. Public mode exposes LiteLLM over TLS while FastMCP remains ClusterIP-only; the private profile uses an internal LoadBalancer. APIM provisioning is billable. Destroy removes this root's role assignments and links, not the existing model/project/VNet.

References: [Standard v2 outbound integration](https://learn.microsoft.com/azure/api-management/integrate-vnet-outbound), [AzureRM APIM](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/api_management).
