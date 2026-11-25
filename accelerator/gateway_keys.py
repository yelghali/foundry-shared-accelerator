"""Private Kubernetes bootstrap for LiteLLM's native PostgreSQL-backed virtual keys."""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

NAMESPACES = {
    "shared-service": "gateway",
    "dedicated-application": "gateway-app-alpha",
    "user-obo": "gateway-user-obo",
}


class Kubernetes:
    def __init__(self, namespace: str) -> None:
        self.namespace = namespace

    def run(self, *args: str, data: str | None = None) -> str:
        result = subprocess.run(
            ["kubectl", "-n", self.namespace, *args],
            input=data, text=True, capture_output=True, check=False,
        )
        if result.returncode:
            # Never include subprocess input/output: Secret manifests and management replies contain keys.
            raise RuntimeError(f"kubectl {args[0]} failed in {self.namespace}; inspect resource events (credentials omitted).")
        return result.stdout

    def apply(self, value: dict[str, Any]) -> None:
        self.run("apply", "-f", "-", data=json.dumps(value))

    def secret(self, name: str) -> dict[str, str] | None:
        value = self.run("get", "secret", name, "--ignore-not-found", "-o", "json").strip()
        if not value:
            return None
        return {
            key: base64.b64decode(item).decode("utf-8")
            for key, item in json.loads(value).get("data", {}).items()
        }

    def store_secret(self, name: str, value: dict[str, str]) -> None:
        self.apply({
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": name, "namespace": self.namespace},
            "type": "Opaque", "data": {
                key: base64.b64encode(item.encode("utf-8")).decode("ascii") for key, item in value.items()
            },
        })


def database_resources(namespace: str) -> dict[str, Any]:
    labels = {"app": "litellm-postgres"}
    return {"apiVersion": "v1", "kind": "List", "items": [
        {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "litellm-postgres", "namespace": namespace},
            "spec": {"clusterIP": "None", "selector": labels, "ports": [{"port": 5432}]},
        },
        {
            "apiVersion": "apps/v1", "kind": "StatefulSet",
            "metadata": {"name": "litellm-postgres", "namespace": namespace},
            "spec": {
                "serviceName": "litellm-postgres", "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {"metadata": {"labels": labels}, "spec": {
                    "enableServiceLinks": False,
                    "containers": [{
                        "name": "postgres", "image": "postgres:16-bookworm",
                        "env": [
                            {"name": "POSTGRES_DB", "value": "litellm"},
                            {"name": "POSTGRES_USER", "value": "litellm"},
                            {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                            {"name": "POSTGRES_PASSWORD", "valueFrom": {"secretKeyRef": {
                                "name": "litellm-auth", "key": "postgres-password",
                            }}},
                        ],
                        "ports": [{"containerPort": 5432}],
                        "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"}],
                        "readinessProbe": {"exec": {"command": ["pg_isready", "-U", "litellm"]},
                                           "initialDelaySeconds": 5, "periodSeconds": 5},
                        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                      "limits": {"cpu": "1", "memory": "512Mi"}},
                    }],
                }},
                "volumeClaimTemplates": [{
                    "metadata": {"name": "data"},
                    "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "managed-csi",
                             "resources": {"requests": {"storage": "4Gi"}}},
                }],
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "postgres-only-from-litellm", "namespace": namespace},
            "spec": {"podSelector": {"matchLabels": labels}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": "litellm"}}}],
                                  "ports": [{"port": 5432, "protocol": "TCP"}]}]},
        },
    ]}


def bootstrap_database(kube: Kubernetes) -> None:
    kube.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": kube.namespace}})
    existing = kube.secret("litellm-auth")
    if existing is None:
        # A retained PVC must never be attached to a newly invented database password.
        pvcs = json.loads(kube.run("get", "pvc", "-l", "app=litellm-postgres", "-o", "json"))
        named = kube.run("get", "pvc", "data-litellm-postgres-0", "--ignore-not-found", "-o", "name").strip()
        if named or pvcs.get("items"):
            raise RuntimeError("PostgreSQL PVC exists but its credentials are missing; restore the original Secret.")
        password = secrets.token_urlsafe(48)
        existing = {
            "master-key": "sk-" + secrets.token_urlsafe(48),
            "postgres-password": password,
            "database-url": f"postgresql://litellm:{quote(password, safe='')}@litellm-postgres.{kube.namespace}.svc.cluster.local:5432/litellm",
        }
        kube.store_secret("litellm-auth", existing)
    if not all(existing.get(key) for key in ("master-key", "postgres-password", "database-url")):
        raise ValueError("The existing litellm-auth Secret is incomplete; refusing credential replacement.")
    kube.apply(database_resources(kube.namespace))
    kube.run("rollout", "status", "statefulset/litellm-postgres", "--timeout=300s")


_PRIVATE_API = r'''
import json, os, ssl, sys
import httpx
request = json.load(sys.stdin)
tls = ssl.create_default_context(cafile="/app/tls/tls.crt")
tls.check_hostname = False
key = os.environ["LITELLM_MASTER_KEY"]
with httpx.Client(base_url="https://127.0.0.1:4000", verify=tls, trust_env=False, timeout=120) as client:
    response = client.request(request["method"], request["path"],
        headers={"x-litellm-api-key": "Bearer " + key, "Authorization": "Bearer " + key},
        json=request.get("body"))
    if not response.is_success:
        print(json.dumps({"error_status": response.status_code, "operation": request["path"]}))
    else:
        print(response.text)
'''


def private_api(kube: Kubernetes, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    output = kube.run(
        "exec", "-i", "deployment/litellm", "-c", "litellm", "--", "python", "-c", _PRIVATE_API,
        data=json.dumps({"method": method, "path": path, "body": body}),
    )
    value = json.loads(output)
    if isinstance(value, dict) and value.get("error_status"):
        raise RuntimeError(f"Private LiteLLM {method} {path} returned HTTP {value['error_status']}; inspect gateway logs.")
    return value


def catalog_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("data", "mcp_servers", "servers", "agents"):
            if isinstance(value.get(key), list):
                return value[key]
    raise ValueError("Unrecognized LiteLLM catalog response; no keys were generated.")


def provision_keys(kube: Kubernetes, scenario: str, *, agent_responses: bool = False) -> dict[str, Any]:
    servers = catalog_rows(private_api(kube, "GET", "/v1/mcp/server"))
    agents = catalog_rows(private_api(kube, "GET", "/v1/agents"))
    server_ids = [str(server["server_id"]) for server in servers if server.get("server_name", server.get("name")) == "blob"]
    allowed_agents = {"echo", "declarative", "hosted"} if agent_responses else {"echo", "declarative"}
    agent_ids = [str(agent["agent_id"]) for agent in agents if agent.get("agent_name") in allowed_agents]
    if len(server_ids) != 1 or not agent_ids:
        raise ValueError("The expected Blob MCP server and agent registrations are not ready.")
    stored = kube.secret("litellm-client-keys") or {}
    created: dict[str, str] = {}
    for alias, permissions in {
        "application": {"mcp_servers": server_ids, "agents": agent_ids},
        "models-only": {"mcp_servers": ["no-mcp-servers"], "agents": []},
    }.items():
        existing_key = stored.get(alias)
        body = {
            "key_alias": f"{scenario}-{alias}",
            "models": ["enterprise-chat"], "metadata": {"scenario": scenario},
            "object_permission": permissions,
        }
        if agent_responses and alias == "application":
            body["models"] += ["prompt-agent-responses", "hosted-agent-responses"]
        if existing_key:
            # Reconcile a previously minted key without replacing a distributed credential.
            private_api(kube, "POST", "/key/update", {"key": existing_key, **body})
            created[alias] = existing_key
        else:
            result = private_api(kube, "POST", "/key/generate", body)
            key = result.get("key")
            if not isinstance(key, str) or not key.startswith("sk-"):
                raise ValueError("LiteLLM key generation did not return a usable virtual key.")
            created[alias] = key
            # Persist after each successful mint, so a later failure does not orphan a disclosed key.
            kube.store_secret("litellm-client-keys", {**stored, **created})
    kube.store_secret("litellm-client-keys", created)
    return {"scenario": scenario, "namespace": kube.namespace, "key_secret": "litellm-client-keys",
            "aliases": list(created), "mcp_server_ids": server_ids, "agent_ids": agent_ids}


def read_key(scenario: str, alias: str = "application") -> str:
    value = Kubernetes(NAMESPACES[scenario]).secret("litellm-client-keys")
    if value is None or not value.get(alias):
        raise ValueError("Provision native virtual keys before calling this scenario.")
    return value[alias]

def secret_provider(namespace: str, identity: dict[str, Any], workload_client_id: str) -> dict[str, Any]:
    return {
        "apiVersion": "secrets-store.csi.x-k8s.io/v1", "kind": "SecretProviderClass",
        "metadata": {"name": "litellm-obo", "namespace": namespace},
        "spec": {"provider": "azure", "parameters": {
            "usePodIdentity": "false", "useVMManagedIdentity": "false",
            "clientID": workload_client_id,
            "keyvaultName": identity["key_vault_name"], "tenantId": identity["tenant_id"],
            "objects": "array:\n  - |\n    objectName: litellm-obo-client-secret\n    objectType: secret\n    objectAlias: obo-client-secret\n",
        }},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["database", "keys", "export-key", "secret-provider"])
    parser.add_argument("--scenario", choices=list(NAMESPACES), required=True)
    parser.add_argument("--alias", choices=["application", "models-only"], default="application")
    parser.add_argument("--output")
    parser.add_argument("--identity")
    parser.add_argument("--client-id")
    parser.add_argument("--agent-responses", action="store_true")
    args = parser.parse_args()
    kube = Kubernetes(NAMESPACES[args.scenario])
    if args.command == "database":
        bootstrap_database(kube)
        print(f"Persistent PostgreSQL and durable native-key credentials ready in {kube.namespace}.")
    elif args.command == "keys":
        print(json.dumps(provision_keys(kube, args.scenario, agent_responses=args.agent_responses), indent=2))
    elif args.command == "secret-provider":
        if args.scenario != "user-obo" or not args.identity or not args.client_id:
            raise ValueError("secret-provider requires user-obo, --identity and --client-id.")
        identity = json.loads(Path(args.identity).read_text(encoding="utf-8-sig"))
        kube.apply(secret_provider(kube.namespace, identity, args.client_id))
        print("Key Vault workload-identity secret provider configured; no secret values exported.")
    else:
        if not args.output:
            raise ValueError("export-key requires an explicit protected output path.")
        target = Path(args.output).resolve()
        if ".local" not in target.parts:
            raise ValueError("Export keys only to the ignored .local directory; never into repository source.")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation avoids replacing an existing distributed credential by accident.
        with target.open("x", encoding="utf-8") as handle:
            handle.write(read_key(args.scenario, args.alias))
        target.chmod(0o600)
        print(f"Key exported to {target}; protect its Windows ACL and never commit or log it.")


if __name__ == "__main__":
    main()
