"""Explicit, tenant-scoped Entra provisioning. Planning never authenticates or writes.

Run ``python -m demos.apim_litellm.operator.entra_setup --help``. Admin consent and password
rotation are separate opt-ins. Receipts contain identifiers, never credentials.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from azure.identity import AzureCliCredential, AzureDeveloperCliCredential

from accelerator.control import save_receipt

GRAPH = "https://graph.microsoft.com/v1.0"
CLI_APP_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
PROFILES = ("shared-service", "dedicated-application", "user-obo")
VAULT_PROXY_SCRIPT = (
    "import json,sys,urllib.request,urllib.error\n"
    "class NoRedirect(urllib.request.HTTPRedirectHandler):\n"
    " def redirect_request(self,*args,**kwargs): return None\n"
    "opener=urllib.request.build_opener(NoRedirect);"
    "p=json.load(sys.stdin);"
    "r=urllib.request.Request(p['url'],data=json.dumps(p['body']).encode() if p['body'] is not None else None,"
    "headers={**p['headers'],'Content-Type':'application/json'},method=p['method']);"
    "\ntry:\n"
    " with opener.open(r,timeout=120) as v:"
    " print(json.dumps({'status':v.status,'body':v.read().decode(),'headers':dict(v.headers)}))\n"
    "except urllib.error.HTTPError as e:\n"
    " print(json.dumps({'status':e.code,'body':e.read().decode(),'headers':dict(e.headers)}))\n"
)


class IdentityError(RuntimeError):
    def __init__(self, status: int, code: str, operation: str = ""):
        self.status = status
        super().__init__(f"Identity request failed: HTTP {status}, {code}"
                         f"{' (' + operation + ')' if operation else ''}. "
                         "Check tenant, Graph permissions, admin consent and Key Vault data-plane RBAC.")


class GraphClient:
    def __init__(self, credential, vault_url: str, key_vault_proxy_namespace: str | None = None):
        self.credential = credential
        self.vault_url = vault_url.rstrip("/")
        self.session = requests.Session()
        if key_vault_proxy_namespace and not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", key_vault_proxy_namespace):
            raise ValueError("Key Vault proxy namespace must be a Kubernetes namespace name.")
        self.key_vault_proxy_namespace = key_vault_proxy_namespace

    def _vault_proxy(self, method, url, body, token):
        # The command is constant: bearer and secret values travel through stdin only.
        command = ["kubectl", "-n", self.key_vault_proxy_namespace, "exec", "-i",
                   "deployment/fastmcp", "--", "python", "-c", VAULT_PROXY_SCRIPT]
        try:
            process = subprocess.run(
                command, input=json.dumps({"method": method, "url": url, "body": body,
                                           "headers": {"Authorization": "Bearer " + token}}),
                capture_output=True, text=True, timeout=150, check=False)
            if process.returncode:
                raise RuntimeError("Proxy execution failed.")
            value = json.loads(process.stdout)
            if not isinstance(value.get("status"), int) or not isinstance(value.get("body"), str):
                raise ValueError("Invalid proxy response.")
            response = requests.Response()
            response.status_code = value["status"]
            response._content = value["body"].encode("utf-8")
            response.headers.update(value.get("headers", {}))
            return response
        except Exception:
            raise RuntimeError("Key Vault private-network proxy failed; output suppressed. "
                               "Check kubectl context, namespace, fastmcp readiness and private DNS.") from None

    def request(self, method, path, body=None):
        url = GRAPH + path if path.startswith("/") else path
        parsed = urlparse(url)
        if not (url.startswith(GRAPH + "/") or
                (self.vault_url and url.startswith(self.vault_url + "/"))):
            raise ValueError("Request target must be Microsoft Graph or the configured Key Vault.")
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("HTTPS without embedded credentials is required.")
        scope = ("https://graph.microsoft.com/.default" if url.startswith(GRAPH + "/")
                 else "https://vault.azure.net/.default")
        try:
            token = self.credential.get_token(scope).token
        except Exception:
            raise RuntimeError("Operator authentication failed; sign in to the explicit tenant.") from None
        for attempt in range(5):
            try:
                if self.key_vault_proxy_namespace and not url.startswith(GRAPH + "/"):
                    response = self._vault_proxy(method, url, body, token)
                else:
                    response = self.session.request(
                        method, url, json=body, headers={"Authorization": "Bearer " + token}, timeout=120)
            except requests.RequestException:
                raise RuntimeError("Identity request transport failed; credentials and response bodies suppressed.") from None
            if response.status_code not in (429, 502, 503, 504) or method != "GET" or attempt == 4:
                break
            retry = response.headers.get("Retry-After", "")
            time.sleep(min(int(retry), 60) if retry.isdigit() else 2 ** attempt)
        if not response.ok:
            # Server messages may echo addPassword responses, request bodies or tokens.
            code = "RequestDenied" if response.status_code == 403 else "RequestFailed"
            try:
                error = response.json().get("error", {})
                candidates = [error.get("innererror", {}).get("code"), error.get("code")]
                # Only known service codes may be emitted; never arbitrary server text.
                allowed = {"Forbidden", "ForbiddenByConnection", "ForbiddenByRbac", "AccessDenied",
                           "Authorization_RequestDenied", "InsufficientPrivileges", "SecretNotFound"}
                code = next((candidate for candidate in candidates if candidate in allowed), code)
            except (ValueError, AttributeError, TypeError):
                pass
            raise IdentityError(response.status_code, code,
                                method + " " + parsed.netloc + parsed.path)
        return response.json() if response.content else {}

    def list(self, path):
        values = []
        while path:
            result = self.request("GET", path)
            values.extend(result.get("value", []))
            path = result.get("@odata.nextLink")
        return values


def single(values, description):
    if len(values) > 1:
        raise ValueError(f"Ambiguous {description}; resolve duplicate registrations before retrying.")
    return values[0] if values else None


def ensure_sp(graph, app_id):
    existing = single(graph.list(f"/servicePrincipals?$filter=appId eq '{app_id}'"), "service principal")
    return existing or graph.request("POST", "/servicePrincipals", {"appId": app_id})


def ensure_application(graph, name, scope_name=None, public=False):
    app = single(graph.list(f"/applications?$filter=displayName eq '{name}'"), name)
    if not app:
        app = graph.request("POST", "/applications", {"displayName": name, "signInAudience": "AzureADMyOrg"})
    body = {}
    if scope_name:
        api = dict(app.get("api") or {})
        api["requestedAccessTokenVersion"] = 2
        scopes = list(api.get("oauth2PermissionScopes") or [])
        scope = single([s for s in scopes if s["value"] == scope_name], scope_name)
        if not scope:
            scope = {
                "id": str(uuid.uuid4()), "value": scope_name, "type": "Admin", "isEnabled": True,
                "adminConsentDisplayName": f"Access {name}",
                "adminConsentDescription": f"Access {name} on behalf of the signed-in user.",
            }
            scopes.append(scope)
        elif not scope.get("isEnabled"):
            raise ValueError(f"{name}/{scope_name} is disabled; enable explicitly before provisioning.")
        api["oauth2PermissionScopes"] = scopes
        uris = list(app.get("identifierUris") or [])
        audience = "api://" + app["appId"]
        if audience not in uris:
            uris.append(audience)
        body.update(api=api, identifierUris=uris)
    if public:
        body["isFallbackPublicClient"] = True
        native = dict(app.get("publicClient") or {})
        native["redirectUris"] = list(dict.fromkeys([
            *(native.get("redirectUris") or []), "https://login.microsoftonline.com/common/oauth2/nativeclient",
        ]))
        body["publicClient"] = native
    if body:
        graph.request("PATCH", "/applications/" + app["id"], body)
        app = {**app, **body}
    sp = ensure_sp(graph, app["appId"])
    result = {"app_id": app["appId"], "object_id": app["id"], "sp_id": sp["id"]}
    if scope_name:
        result.update(audience=audience, scope=audience + "/" + scope_name, scope_id=scope["id"],
                      consent_type=scope.get("type", "Admin"))
    return app, result


def configure_user_consent(graph, identity):
    """Change only app-owned scopes and bundle the existing client/OBO permissions."""
    applications = {}
    for name in ("gateway", "mcp", "test_client"):
        registration = identity[name]
        app = graph.request(
            "GET", "/applications/" + registration["object_id"]
            + "?$select=id,appId,displayName,api,requiredResourceAccess",
        )
        if app["appId"] != registration["app_id"]:
            raise ValueError(f"{name} application does not match the identity receipt.")
        applications[name] = app
    foundry = graph.request(
        "GET", "/servicePrincipals/" + identity["foundry"]["sp_id"] + "?$select=oauth2PermissionScopes",
    )
    permission = single([
        scope for scope in foundry["oauth2PermissionScopes"]
        if scope["value"] == identity["foundry"]["scope"].rsplit("/", 1)[-1]
    ], "Foundry delegated scope")
    if not permission or not permission.get("isEnabled") or permission.get("type") != "User":
        raise ValueError("The Foundry delegated scope is not user-consent eligible; an administrator is required.")
    for client, resource_id, scope_id in (
        ("test_client", identity["gateway"]["app_id"], identity["gateway"]["scope_id"]),
        ("gateway", identity["mcp"]["app_id"], identity["mcp"]["scope_id"]),
        ("gateway", identity["foundry"]["resource_app_id"], permission["id"]),
    ):
        if not any(
            access["resourceAppId"] == resource_id
            and {"id": scope_id, "type": "Scope"} in access.get("resourceAccess", [])
            for access in applications[client].get("requiredResourceAccess", [])
        ):
            raise ValueError(f"{client} is missing a required delegated API permission; run identity provisioning first.")
    for name in ("mcp", "gateway"):
        registration, app = identity[name], applications[name]
        scope = single([
            scope for scope in app["api"]["oauth2PermissionScopes"]
            if scope["id"] == registration["scope_id"]
            and scope["value"] == registration["scope"].rsplit("/", 1)[-1]
        ], name + " scope")
        if not scope or not scope.get("isEnabled"):
            raise ValueError(f"{name} scope is absent or disabled; resolve its configuration before migration.")
    for name in ("mcp", "gateway"):
        registration, app = identity[name], applications[name]
        original_api = deepcopy(app["api"])
        desired = deepcopy(original_api)
        scope = next(scope for scope in desired["oauth2PermissionScopes"] if scope["id"] == registration["scope_id"])
        changed_scope = scope["type"] != "User"
        if changed_scope:
            scope.update(
                type="User", userConsentDisplayName=f"Access {app['displayName']}",
                userConsentDescription=f"Allow this application to access {app['displayName']} on your behalf.",
            )
        if name == "gateway":
            desired["knownClientApplications"] = list(dict.fromkeys([
                *(desired.get("knownClientApplications") or []), identity["test_client"]["app_id"],
            ]))
        if desired == original_api:
            continue
        path = "/applications/" + app["id"]
        if changed_scope:
            disabled = deepcopy(original_api)
            next(scope for scope in disabled["oauth2PermissionScopes"]
                 if scope["id"] == registration["scope_id"])["isEnabled"] = False
            graph.request("PATCH", path, {"api": disabled})
        try:
            graph.request("PATCH", path, {"api": desired})
        except RuntimeError:
            if changed_scope:
                try:
                    graph.request("PATCH", path, {"api": original_api})
                except RuntimeError as restore_error:
                    raise RuntimeError(
                        f"{name} scope migration failed and restoration failed; inspect and restore its enabled state."
                    ) from restore_error
            raise


def user_consent_from_receipt(args):
    identity = json.loads(Path(args.identity).read_text(encoding="utf-8-sig"))
    uuid.UUID(identity["tenant_id"])
    for name in ("gateway", "mcp", "test_client"):
        for field in ("app_id", "object_id"):
            uuid.UUID(identity[name][field])
    for name in ("gateway", "mcp"):
        uuid.UUID(identity[name]["scope_id"])
    uuid.UUID(identity["foundry"]["sp_id"])
    if not args.apply:
        print(json.dumps({
            "mode": "plan", "tenant_id": identity["tenant_id"],
            "scope_changes": ["gateway/access_as_user: User", "mcp/tools.read: User"],
            "known_client_application": identity["test_client"]["app_id"],
            "tenant_policy_changes": False, "permission_grants_created": False,
            "writes_performed": False,
        }, indent=2))
        return
    credential_type = AzureDeveloperCliCredential if args.use_azd else AzureCliCredential
    graph = GraphClient(credential_type(tenant_id=identity["tenant_id"], process_timeout=120), "")
    configure_user_consent(graph, identity)
    for name in ("gateway", "mcp"):
        actual = graph.request("GET", "/applications/" + identity[name]["object_id"] + "?$select=api")
        scope = next(scope for scope in actual["api"]["oauth2PermissionScopes"]
                     if scope["id"] == identity[name]["scope_id"])
        if scope["type"] != "User" or not scope["isEnabled"]:
            raise RuntimeError(f"{name} user-consent configuration was not confirmed.")
        if name == "gateway" and identity["test_client"]["app_id"] not in actual["api"].get("knownClientApplications", []):
            raise RuntimeError("Combined client/OBO consent configuration was not confirmed.")
        identity[name]["consent_type"] = "User"
    identity.update(consent_mode="user", user_consent_configured=True)
    output = args.output or args.identity
    save_receipt(output, identity)
    print(json.dumps({
        "receipt": output, "user_consent_configured": True, "consent_performed": False,
        "next_step": "Run login with this identity receipt and an OS-encrypted --token-cache; tenant policy still applies.",
    }, indent=2))


def require_scope(graph, app, resource_app_id, scope_id):
    access = [dict(item) for item in app.get("requiredResourceAccess", [])]
    target = next((r for r in access if r["resourceAppId"] == resource_app_id), None)
    if target is None:
        target = {"resourceAppId": resource_app_id, "resourceAccess": []}
        access.append(target)
    permission = {"id": scope_id, "type": "Scope"}
    if permission not in target["resourceAccess"]:
        target["resourceAccess"] = [*target["resourceAccess"], permission]
        graph.request("PATCH", "/applications/" + app["id"], {"requiredResourceAccess": access})
        app["requiredResourceAccess"] = access


def grant_consent(graph, client_sp_id, resource_sp_id, scope):
    grants = graph.list(
        f"/oauth2PermissionGrants?$filter=clientId eq '{client_sp_id}' and resourceId eq '{resource_sp_id}'")
    existing = single([g for g in grants if g["consentType"] == "AllPrincipals"], "tenant-wide consent")
    scopes = sorted(set((existing or {}).get("scope", "").split()) | {scope})
    if existing:
        if existing["scope"] != " ".join(scopes):
            graph.request("PATCH", "/oauth2PermissionGrants/" + existing["id"], {"scope": " ".join(scopes)})
        return existing["id"]
    result = graph.request("POST", "/oauth2PermissionGrants", {
        "clientId": client_sp_id, "resourceId": resource_sp_id,
        "consentType": "AllPrincipals", "principalId": None, "scope": " ".join(scopes),
    })
    return result["id"]


def consent_from_receipt(args):
    """Grant only delegated Graph consent; no application or vault writes."""
    identity = json.loads(Path(args.identity).read_text(encoding="utf-8-sig"))
    if not isinstance(identity, dict):
        raise ValueError("Identity receipt must contain a JSON object.")
    uuid.UUID(identity["tenant_id"])
    for resource in ("gateway", "mcp", "test_client", "foundry"):
        uuid.UUID(identity[resource]["sp_id"])
    scopes = []
    for resource in ("gateway", "mcp", "foundry"):
        # Receipts expose full scopes, while permission grants require scope values.
        scope = identity[resource]["scope"].rsplit("/", 1)[-1]
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", scope) or scope == ".default":
            raise ValueError("Receipt must identify an explicit delegated scope value.")
        scopes.append(scope)
    gateway_scope, mcp_scope, foundry_scope = scopes
    pairs = [
        (identity["gateway"]["sp_id"], identity["foundry"]["sp_id"], foundry_scope),
        (identity["gateway"]["sp_id"], identity["mcp"]["sp_id"], mcp_scope),
        (identity["test_client"]["sp_id"], identity["gateway"]["sp_id"], gateway_scope),
    ]
    if not args.apply:
        print(json.dumps({"mode": "plan", "tenant_id": identity["tenant_id"],
                          "grants": [{"client_sp_id": client, "resource_sp_id": resource, "scope": scope}
                                     for client, resource, scope in pairs],
                          "include_azure_cli": args.include_azure_cli, "writes_performed": False}, indent=2))
        return
    credential_type = AzureDeveloperCliCredential if args.use_azd else AzureCliCredential
    graph = GraphClient(credential_type(tenant_id=identity["tenant_id"], process_timeout=120), "")
    if args.include_azure_cli:
        cli_sp = single(graph.list(f"/servicePrincipals?$filter=appId eq '{CLI_APP_ID}'"), "Azure CLI principal")
        if not cli_sp:
            raise ValueError("Azure CLI service principal is absent; operator must provision it explicitly.")
        uuid.UUID(cli_sp["id"])
        pairs.append((cli_sp["id"], identity["gateway"]["sp_id"], gateway_scope))
    # Do not publish a success receipt if any individual grant fails.
    grant_ids = [grant_consent(graph, *pair) for pair in pairs]
    identity.update(consent_grant_ids=grant_ids, admin_consent_requested=True)
    output = args.output or args.identity
    save_receipt(output, identity)
    print(json.dumps({"receipt": output, "consent_grant_ids": grant_ids}))


def ensure_obo_secret(graph, gateway, *, rotate=False):
    url = graph.vault_url + "/secrets/litellm-obo-client-secret?api-version=7.4"
    try:
        existing = graph.request("GET", url)
    except IdentityError as error:
        if error.status != 404:
            raise
    else:
        if existing.get("tags", {}).get("entra-app-id") != gateway["app_id"]:
            raise ValueError("Existing OBO secret belongs to another application; refusing to overwrite.")
        if not rotate:
            if not existing.get("value") or not existing.get("attributes", {}).get("enabled", True):
                raise ValueError("Existing OBO secret is empty or disabled; rotate explicitly.")
            expires = existing.get("attributes", {}).get("exp")
            if expires is not None and expires <= time.time():
                raise ValueError("Existing OBO secret has expired; rotate explicitly.")
            return existing["id"]
    password = graph.request("POST", "/applications/" + gateway["object_id"] + "/addPassword", {
        "passwordCredential": {"displayName": "litellm-obo-keyvault"},
    })
    try:
        stored = graph.request("PUT", url, {
            "value": password["secretText"], "contentType": "application/x-entra-client-secret",
            "tags": {"entra-app-id": gateway["app_id"], "entra-key-id": password["keyId"]},
            **({"attributes": {"exp": int(datetime.fromisoformat(
                password["endDateTime"].replace("Z", "+00:00")).timestamp())}}
               if password.get("endDateTime") else {}),
        })
    except Exception:
        # A failed vault write must not leave an untracked active credential.
        graph.request("POST", "/applications/" + gateway["object_id"] + "/removePassword",
                      {"keyId": password["keyId"]})
        raise
    return stored["id"]


def provision(graph, args):
    gateway_app, gateway = ensure_application(graph, args.prefix + "-gateway", "access_as_user")
    _, mcp = ensure_application(graph, args.prefix + "-mcp", "tools.read")
    test_app, test_client = ensure_application(graph, args.prefix + "-test-client", public=True)
    resources = graph.list("/servicePrincipals?$filter=servicePrincipalNames/any(n:n eq 'https://ai.azure.com')")
    foundry = single(resources, "Azure AI resource service principal")
    if not foundry:
        raise ValueError("Azure AI service principal https://ai.azure.com was not found in this tenant.")
    permission = single([s for s in foundry.get("oauth2PermissionScopes", [])
                         if s.get("isEnabled") and s.get("value") == "user_impersonation"],
                        "Azure AI delegated user_impersonation permission")
    if not permission:
        raise ValueError("Azure AI does not expose enabled user_impersonation; inspect tenant metadata.")
    require_scope(graph, gateway_app, foundry["appId"], permission["id"])
    require_scope(graph, gateway_app, mcp["app_id"], mcp["scope_id"])
    require_scope(graph, test_app, gateway["app_id"], gateway["scope_id"])
    consent_pairs = [(gateway["sp_id"], foundry["id"], permission["value"]),
                     (gateway["sp_id"], mcp["sp_id"], "tools.read"),
                     (test_client["sp_id"], gateway["sp_id"], "access_as_user")]
    if args.preauthorize_azure_cli:
        cli_sp = single(graph.list(f"/servicePrincipals?$filter=appId eq '{CLI_APP_ID}'"), "Azure CLI principal")
        if not cli_sp:
            raise ValueError("Azure CLI service principal is absent; operator must provision it explicitly.")
        api = dict(gateway_app["api"])
        preauth = list(api.get("preAuthorizedApplications") or [])
        target = next((p for p in preauth if p["appId"] == CLI_APP_ID), None)
        if target is None:
            target = {"appId": CLI_APP_ID, "delegatedPermissionIds": []}
            preauth.append(target)
        target["delegatedPermissionIds"] = list(dict.fromkeys([
            *target["delegatedPermissionIds"], gateway["scope_id"]]))
        api["preAuthorizedApplications"] = preauth
        graph.request("PATCH", "/applications/" + gateway["object_id"], {"api": api})
        consent_pairs.append((cli_sp["id"], gateway["sp_id"], "access_as_user"))
    grants = [grant_consent(graph, *pair) for pair in consent_pairs] if args.grant_admin_consent else []
    secret_uri = ensure_obo_secret(graph, gateway, rotate=args.rotate_obo_secret)
    return {
        "tenant_id": args.tenant_id, "subscription_id": args.subscription_id,
        "project_id": args.project_id, "profile": args.profile,
        "gateway": gateway, "mcp": mcp, "test_client": test_client,
        "foundry": {"resource_app_id": foundry["appId"], "sp_id": foundry["id"],
                    "scope": "https://ai.azure.com/" + permission["value"]},
        "permitted_user_object_ids": args.permitted_user_object_id,
        "obo_client_secret_uri": secret_uri, "consent_grant_ids": grants,
        "admin_consent_requested": args.grant_admin_consent,
        "consent_mode": "user" if (
            gateway.get("consent_type") == mcp.get("consent_type") == "User"
            and test_client["app_id"] in (gateway_app.get("api", {}).get("knownClientApplications") or [])
        ) else "admin",
        "user_consent_configured": (
            gateway.get("consent_type") == mcp.get("consent_type") == "User"
            and test_client["app_id"] in (gateway_app.get("api", {}).get("knownClientApplications") or [])
        ),
        "key_vault_name": urlparse(graph.vault_url).hostname.split(".")[0],
        "key_vault_subscription_id": args.key_vault_subscription_id or args.subscription_id,
    }


def encrypted_token_cache(cache_path: str):
    try:
        from msal_extensions import PersistedTokenCache, build_encrypted_persistence
        return PersistedTokenCache(build_encrypted_persistence(cache_path))
    except Exception:
        raise RuntimeError("Encrypted MSAL cache unavailable; install msal-extensions and configure OS encryption. "
                           "Run login --identity <receipt> --token-cache <protected-cache-path>.") from None


def cached_user_token(identity: dict, cache_path: str) -> str:
    """Read one explicitly cached public-client user token without prompting."""
    import msal

    instruction = ("Run login --identity <receipt> --token-cache <protected-cache-path> "
                   "with the intended user after the required user or administrator consent.")
    if not Path(cache_path).is_file():
        raise RuntimeError("Encrypted user token cache is missing. " + instruction)
    cache = encrypted_token_cache(cache_path)
    try:
        app = msal.PublicClientApplication(
            identity["test_client"]["app_id"],
            authority="https://login.microsoftonline.com/" + identity["tenant_id"],
            token_cache=cache)
        accounts = app.get_accounts()
    except Exception:
        raise RuntimeError("Silent user authentication failed. " + instruction) from None
    if len(accounts) != 1:
        raise RuntimeError("User token cache must contain exactly one account. " + instruction)
    try:
        result = app.acquire_token_silent([identity["gateway"]["scope"]], account=accounts[0])
    except Exception:
        raise RuntimeError("Silent user authentication failed. " + instruction) from None
    token = result.get("access_token") if isinstance(result, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("Cached user token is unavailable or requires consent. " + instruction)
    return token


def login(args):
    import msal

    receipt = json.loads(Path(args.identity).read_text(encoding="utf-8-sig"))
    options = {}
    if args.token_cache:
        # Never silently downgrade to a plaintext cache.
        options["token_cache"] = encrypted_token_cache(args.token_cache)
    app = msal.PublicClientApplication(receipt["test_client"]["app_id"],
                                      authority="https://login.microsoftonline.com/" + receipt["tenant_id"], **options)
    scope = (receipt["gateway"]["audience"].rstrip("/") + "/.default"
             if receipt.get("consent_mode") == "user" else receipt["gateway"]["scope"])
    flow = app.initiate_device_flow(scopes=[scope])
    if "user_code" not in flow:
        raise RuntimeError("Device login could not start; verify public-client registration and required consent.")
    print(flow["message"], flush=True)
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError("Interactive user login failed or requires administrator approval under tenant policy; "
                           "no successful login is claimed.") from None
    print(json.dumps({"authenticated": True, "user_object_id": result.get("id_token_claims", {}).get("oid")}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("provision", help="Plan by default; --apply performs tenant writes.")
    for name in ("tenant-id", "subscription-id", "project-id", "prefix", "key-vault-url", "output"):
        setup.add_argument("--" + name, required=True)
    setup.add_argument("--key-vault-subscription-id")
    setup.add_argument("--key-vault-proxy-namespace",
                       help="Explicit vault-only AKS network hop via deployment/fastmcp; operator identity is retained.")
    setup.add_argument("--profile", choices=PROFILES, default="user-obo")
    setup.add_argument("--permitted-user-object-id", action="append", default=[])
    for name in ("apply", "grant-admin-consent", "rotate-obo-secret", "preauthorize-azure-cli", "use-azd"):
        setup.add_argument("--" + name, action="store_true")
    auth = commands.add_parser("login", help="Interactive public-client device login; never prints bearer tokens.")
    auth.add_argument("--identity", required=True)
    auth.add_argument("--token-cache", help="Optional OS-encrypted MSAL cache path; never plaintext.")
    consent = commands.add_parser("consent", help="Plan or apply Graph-only admin consent from an identity receipt.")
    consent.add_argument("--identity", required=True)
    consent.add_argument("--output", help="Updated nonsecret receipt path; defaults to --identity.")
    for name in ("apply", "include-azure-cli", "use-azd"):
        consent.add_argument("--" + name, action="store_true")
    user_consent = commands.add_parser(
        "user-consent", help="Opt in existing registrations to per-user combined OBO consent; never changes tenant policy.",
    )
    user_consent.add_argument("--identity", required=True)
    user_consent.add_argument("--output")
    for name in ("apply", "use-azd"):
        user_consent.add_argument("--" + name, action="store_true")
    args = parser.parse_args(argv)
    if args.command == "user-consent":
        user_consent_from_receipt(args)
        return
    if args.command == "consent":
        consent_from_receipt(args)
        return
    if args.command == "login":
        login(args)
        return
    for identifier in (args.tenant_id, args.subscription_id, *args.permitted_user_object_id):
        uuid.UUID(identifier)
    if args.key_vault_subscription_id:
        uuid.UUID(args.key_vault_subscription_id)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,63}", args.prefix):
        raise ValueError("Prefix must contain only letters, digits and hyphens (maximum 64 characters).")
    if not re.fullmatch(r"https://[a-zA-Z0-9-]+\.vault\.azure\.net/?", args.key_vault_url):
        raise ValueError("Key Vault URL must be an explicit public Azure vault HTTPS URL.")
    if not args.apply:
        print(json.dumps({"mode": "plan", "tenant_id": args.tenant_id, "profile": args.profile,
                          "applications": [args.prefix + suffix for suffix in ("-gateway", "-mcp", "-test-client")],
                          "grant_admin_consent": args.grant_admin_consent,
                          "rotate_obo_secret": args.rotate_obo_secret,
                          "writes_performed": False}, indent=2))
        return
    credential = (AzureDeveloperCliCredential(tenant_id=args.tenant_id, process_timeout=120) if args.use_azd
                  else AzureCliCredential(tenant_id=args.tenant_id, process_timeout=120))
    result = provision(GraphClient(credential, args.key_vault_url, args.key_vault_proxy_namespace), args)
    save_receipt(args.output, result)
    print(json.dumps({"receipt": args.output, "gateway_app_id": result["gateway"]["app_id"],
                      "consent_grant_ids": result["consent_grant_ids"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Only our errors are safe to display; third-party errors can contain bodies.
        safe = str(error) if isinstance(error, (IdentityError, ValueError, RuntimeError)) else type(error).__name__
        print("Entra setup failed: " + safe, file=sys.stderr)
        sys.exit(1)
