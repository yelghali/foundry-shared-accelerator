"""Emit a Kubernetes List as JSON (accepted by kubectl apply -f).

Run with the variables in deployment.env.example; no Azure/Kubernetes writes.
"""

import json
import os
import ipaddress
import hashlib
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be configured")
    return value


def render():
    exposure = required("EXPOSURE")
    if exposure not in {"public", "private"}:
        raise ValueError("EXPOSURE must be public or private")
    source_ranges = [
        str(ipaddress.ip_network(value.strip(), strict=True))
        for value in os.environ.get("LOAD_BALANCER_SOURCE_RANGES", "").split(",")
        if value.strip()
    ]
    scenario = os.environ.get("AUTHENTICATION_SCENARIO", "shared-service")
    if scenario not in {"shared-service", "dedicated-application", "user-obo"}:
        raise ValueError("AUTHENTICATION_SCENARIO must be shared-service, dedicated-application or user-obo")
    namespace = os.environ.get("GATEWAY_NAMESPACE", "gateway")
    instance = os.environ.get("GATEWAY_INSTANCE", "litellm")
    service_account = os.environ.get("GATEWAY_SERVICE_ACCOUNT", "litellm")
    for name, value in [("GATEWAY_NAMESPACE", namespace), ("GATEWAY_INSTANCE", instance),
                        ("GATEWAY_SERVICE_ACCOUNT", service_account)]:
        if len(value) > 50 or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value):
            raise ValueError(f"{name} must be a DNS label of at most 50 characters")
    if scenario == "dedicated-application" and namespace == "gateway":
        raise ValueError("dedicated-application requires an isolated GATEWAY_NAMESPACE")
    if scenario == "user-obo" and namespace == "gateway":
        raise ValueError("user-obo requires an isolated GATEWAY_NAMESPACE")
    tenant = required("TENANT_ID")
    litellm_client = required("LITELLM_CLIENT_ID")
    fastmcp_client = required("FASTMCP_CLIENT_ID")
    config = Path(__file__).with_name("config.yaml").read_text(encoding="utf-8")
    # Native registry names are kept deterministic to match APIM route policy.
    common = {"TENANT_ID": tenant, "AUTHENTICATION_SCENARIO": scenario}
    gateway_env = {
        **common,
        "GATEWAY_NAMESPACE": namespace,
        "MODEL_ALIAS": os.environ.get("MODEL_ALIAS", "enterprise-chat"),
        "AZURE_MODEL": os.environ.get("AZURE_MODEL", "azure/gpt-4o-mini"),
        "AZURE_API_BASE": required("AZURE_API_BASE"),
        "AZURE_API_VERSION": os.environ.get("AZURE_API_VERSION", "2024-10-21"),
        "LITELLM_TELEMETRY": "False",
        "KEEPALIVE_TIMEOUT": "300",
        "LITELLM_WORKER_STARTUP_HOOKS": "foundry_a2a:install,agent_responses:install",
        "FOUNDRY_A2A_URL": required("FOUNDRY_A2A_URL"),
    }
    if os.environ.get("PROXY_BASE_URL", "").strip():
        gateway_env["PROXY_BASE_URL"] = required("PROXY_BASE_URL")
    gateway_env.update({
        "FASTMCP_URL": f"http://fastmcp.{namespace}.svc.cluster.local:8000/mcp",
        "MCP_TOKEN_SCOPE": required("MCP_TOKEN_SCOPE"),
    })
    mcp_env = {
        **common,
        "MCP_TOKEN_AUDIENCE": required("MCP_TOKEN_AUDIENCE"),
        "MCP_ALLOWED_CLIENT_IDS": os.environ.get("MCP_ALLOWED_CLIENT_IDS", "").strip() or litellm_client,
        "MCP_ALLOWED_PRINCIPAL_IDS": os.environ.get("MCP_ALLOWED_PRINCIPAL_IDS", "").strip() or required("LITELLM_PRINCIPAL_ID"),
        "BLOB_ACCOUNT_URL": required("BLOB_ACCOUNT_URL"),
        "BLOB_CONTAINER": required("BLOB_CONTAINER"),
    }
    for field in ("MCP_ALLOWED_CLIENT_IDS", "MCP_ALLOWED_PRINCIPAL_IDS"):
        additional = os.environ.get(field.replace("ALLOWED", "FOUNDRY"), "").strip()
        if additional:
            mcp_env[field] = ",".join(dict.fromkeys((mcp_env[field] + "," + additional).split(",")))
    if os.environ.get("FOUNDRY_DECLARATIVE_A2A_URL", "").strip():
        gateway_env["FOUNDRY_DECLARATIVE_A2A_URL"] = required("FOUNDRY_DECLARATIVE_A2A_URL")
    if scenario == "user-obo":
        obo_env = {
            "BACKEND_AUTH_MODE": "user-obo",
            "OBO_TENANT_ID": os.environ.get("OBO_TENANT_ID", tenant),
            "OBO_CLIENT_ID": required("OBO_CLIENT_ID"),
            "OBO_CLIENT_SECRET_FILE": "/mnt/secrets-store/obo-client-secret",
            "OBO_INPUT_AUDIENCE": os.environ.get("OBO_INPUT_AUDIENCE", "").strip() or required("OBO_TOKEN_AUDIENCE"),
            "OBO_ALLOWED_CLIENT_IDS": required("OBO_ALLOWED_CLIENT_IDS"),
            "OBO_ALLOWED_PRINCIPAL_IDS": required("OBO_ALLOWED_PRINCIPAL_IDS"),
            "OBO_REQUIRED_SCOPE": os.environ.get("OBO_REQUIRED_SCOPE", "access_as_user"),
        }
        gateway_env.update(obo_env)
        gateway_env["LITELLM_WORKER_STARTUP_HOOKS"] += ",request_policy:install_mcp_env_resolution"
        mcp_env.update({"MCP_AUTH_MODE": "user-obo",
                        "MCP_ALLOWED_CLIENT_IDS": os.environ.get("MCP_ALLOWED_CLIENT_IDS", "").strip() or obo_env["OBO_CLIENT_ID"],
                        "MCP_ALLOWED_PRINCIPAL_IDS": os.environ.get("MCP_ALLOWED_PRINCIPAL_IDS", "").strip() or obo_env["OBO_ALLOWED_PRINCIPAL_IDS"],
                        "MCP_REQUIRED_SCOPE": os.environ.get("MCP_REQUIRED_SCOPE", "tools.read")})
        config = config.replace(
            "    allow_all_keys: false",
            "    allow_all_keys: false\n"
            "    auth_type: oauth2_token_exchange\n"
            "    token_exchange_profile: entra_obo\n"
            f"    token_exchange_endpoint: https://login.microsoftonline.com/{obo_env['OBO_TENANT_ID']}/oauth2/v2.0/token\n"
            "    client_id: os.environ/OBO_CLIENT_ID\n"
            "    client_secret: os.environ/OBO_CLIENT_SECRET\n"
            f"    scopes: [{json.dumps(required('MCP_TOKEN_SCOPE'))}]",
        )
    else:
        gateway_env["LITELLM_WORKER_STARTUP_HOOKS"] += ",mcp_workload:install"
    settings = yaml.safe_load(config)
    settings["mcp_servers"]["blob"]["url"] = gateway_env["FASTMCP_URL"]
    agents = []
    for agent in settings["agents"]:
        variable = agent["agent_card_params"]["url"].removeprefix("os.environ/")
        endpoint = gateway_env.get(variable)
        if not endpoint:
            continue
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"{variable} must be an HTTPS agent endpoint without credentials, query or fragment")
        agent["agent_card_params"]["url"] = endpoint.rstrip("/")
        if scenario == "user-obo":
            agent["extra_headers"] = ["Authorization"]
            agent["litellm_params"] = {
                "agent_card_path": "agentCard/v1.0", "foundry_user_obo": True,
            }
        agents.append(agent)
    settings["agents"] = agents
    if os.environ.get("ENABLE_AGENT_RESPONSES", "").lower() == "true":
        for alias, variable, model in (
            ("prompt-agent-responses", "FOUNDRY_PROMPT_RESPONSES_BASE",
             "openai/" + (os.environ.get("PROMPT_AGENT_MODEL", "").strip()
                          or gateway_env["AZURE_MODEL"].removeprefix("azure/"))),
            ("hosted-agent-responses", "FOUNDRY_HOSTED_RESPONSES_BASE", "openai/gateway-hosted-dual"),
        ):
            target = required(variable).rstrip("/")
            parsed = urlsplit(target)
            if (parsed.scheme != "https" or not parsed.hostname
                    or not parsed.hostname.endswith(".services.ai.azure.com")
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError(f"{variable} must be a fixed Foundry HTTPS base without credentials or query")
            gateway_env[variable] = target
            settings["model_list"].append({
                "model_name": alias,
                "litellm_params": {"model": model, "api_base": target, "drop_params": True},
            })
        hosted = deepcopy(settings["agents"][0])
        hosted["agent_name"] = "hosted"
        hosted["agent_card_params"].update(
            name="Hosted model-backed agent",
            description="Private model-backed hosted A2A agent",
            url=gateway_env["FOUNDRY_HOSTED_RESPONSES_BASE"].removesuffix("/openai") + "/a2a",
            skills=[{"id": "chat", "name": "Chat", "description": "Model and explicit MCP read", "tags": ["chat"]}],
        )
        gateway_env["FOUNDRY_HOSTED_A2A_URL"] = hosted["agent_card_params"]["url"]
        settings["agents"].append(hosted)
        if os.environ.get("PRIVATE_FOUNDRY_DEMO", "").lower() == "true":
            settings["agents"] = [agent for agent in settings["agents"] if agent["agent_name"] != "echo"]
    config = yaml.safe_dump(settings, sort_keys=False)
    resources = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}}]
    for name, client in [
        (service_account, litellm_client), ("fastmcp", fastmcp_client),
        ("blob-seed", required("BLOB_SEED_CLIENT_ID")),
    ]:
        resources.append({
            "apiVersion": "v1", "kind": "ServiceAccount",
            "metadata": {"name": name, "namespace": namespace,
                         "annotations": {"azure.workload.identity/client-id": client,
                                         "azure.workload.identity/tenant-id": tenant}},
        })
    resources.append({
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": f"{instance}-config", "namespace": namespace},
        "data": {"config.yaml": config},
    })

    def container(name, image, port, env):
        return {
            "name": name, "image": image,
            "ports": [{"containerPort": port}],
            "env": [{"name": key, "value": value} for key, value in env.items()],
            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"},
                          "limits": {"cpu": "2", "memory": "2Gi"}},
        }

    gateway_image = required("LITELLM_IMAGE")
    gateway = container("litellm", gateway_image, 4000, gateway_env)
    gateway["env"].extend([
        {"name": name, "valueFrom": {"secretKeyRef": {"name": "litellm-auth", "key": key}}}
        for name, key in [("DATABASE_URL", "database-url"), ("LITELLM_MASTER_KEY", "master-key")]
    ])
    gateway["volumeMounts"] = [{
        "name": "config", "mountPath": "/app/scenario/config.yaml", "subPath": "config.yaml",
    }, {"name": "tls", "mountPath": "/app/tls", "readOnly": True}]
    gateway["args"] = [
        "--config", "/app/scenario/config.yaml", "--host", "0.0.0.0", "--port", "4000",
        "--ssl_keyfile_path", "/app/tls/tls.key",
        "--ssl_certfile_path", "/app/tls/tls.crt",
    ]
    gateway["readinessProbe"] = {
        "httpGet": {"path": "/health/liveness", "port": 4000, "scheme": "HTTPS"},
        "initialDelaySeconds": 10, "periodSeconds": 10,
    }
    gateway["startupProbe"] = {
        "httpGet": {"path": "/health/liveness", "port": 4000, "scheme": "HTTPS"},
        "failureThreshold": 60, "periodSeconds": 10,
    }
    if scenario == "user-obo":
        gateway["volumeMounts"].append({
            "name": "obo-secret", "mountPath": "/mnt/secrets-store", "readOnly": True,
        })
    mcp = container("fastmcp", required("FASTMCP_IMAGE"), 8000, mcp_env)
    mcp["readinessProbe"] = {"tcpSocket": {"port": 8000}, "initialDelaySeconds": 5}
    gateway_containers = [gateway]
    for name, containers in [(instance, gateway_containers), ("fastmcp", [mcp])]:
        spec = {"serviceAccountName": service_account if name == instance else name,
                "enableServiceLinks": False, "containers": containers}
        if name == instance:
            spec["volumes"] = [
                {"name": "config", "configMap": {"name": f"{instance}-config"}},
                {"name": "tls", "secret": {"secretName": os.environ.get("TLS_SECRET_NAME", "gateway-tls")}},
            ]
            if scenario == "user-obo":
                spec["volumes"].append({
                    "name": "obo-secret", "csi": {
                        "driver": "secrets-store.csi.k8s.io", "readOnly": True,
                        "volumeAttributes": {"secretProviderClass": "litellm-obo"},
                    },
                })
        resources.append({
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {
                         "labels": {"app": name, "azure.workload.identity/use": "true"},
                         "annotations": {"checksum/config": hashlib.sha256(config.encode("utf-8")).hexdigest()} if name == instance else {},
                     },
                                  "spec": spec}},
        })
        service = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"selector": {"app": name}, "ports": [
                {"port": 443 if name == instance else 8000, "targetPort": 4000 if name == instance else 8000}
            ]},
        }
        if name == instance:
            service["metadata"]["annotations"] = {
                "service.beta.kubernetes.io/azure-load-balancer-internal": str(exposure == "private").lower()
            }
            service["spec"]["type"] = "LoadBalancer"
            if exposure == "public" and os.environ.get("PUBLIC_DNS_LABEL", "").strip():
                service["metadata"]["annotations"]["service.beta.kubernetes.io/azure-dns-label-name"] = required("PUBLIC_DNS_LABEL")
            if source_ranges:
                service["spec"]["loadBalancerSourceRanges"] = source_ranges
        resources.append(service)
        if name == instance and exposure == "public":
            resources.append({
                "apiVersion": "v1", "kind": "Service",
                "metadata": {
                    "name": f"{instance}-internal", "namespace": namespace,
                    "annotations": {"service.beta.kubernetes.io/azure-load-balancer-internal": "true"},
                },
                "spec": {
                    "type": "LoadBalancer", "selector": {"app": instance},
                    "ports": [{"port": 443, "targetPort": 4000}],
                },
            })
    seed_hash = hashlib.sha256(json.dumps([
        required("FASTMCP_IMAGE"), required("BLOB_ACCOUNT_URL"), required("BLOB_CONTAINER"),
    ]).encode()).hexdigest()[:12]
    resources.append({
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": f"blob-seed-{seed_hash}", "namespace": namespace,
                     "labels": {"app": "blob-seed"}},
        "spec": {
            "backoffLimit": 4,
            "activeDeadlineSeconds": 600,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {"app": "blob-seed", "azure.workload.identity/use": "true"}},
                "spec": {
                    "serviceAccountName": "blob-seed", "enableServiceLinks": False,
                    "restartPolicy": "Never",
                    "containers": [{
                        "name": "blob-seed", "image": required("FASTMCP_IMAGE"),
                        "command": ["python", "/app/scenario/blob_seed.py"],
                        "env": [
                            {"name": "BLOB_ACCOUNT_URL", "value": required("BLOB_ACCOUNT_URL")},
                            {"name": "BLOB_CONTAINER", "value": required("BLOB_CONTAINER")},
                        ],
                        "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                      "limits": {"cpu": "1", "memory": "512Mi"}},
                    }],
                },
            },
        },
    })
    resources.append({
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": "fastmcp-only-from-litellm", "namespace": namespace},
        "spec": {"podSelector": {"matchLabels": {"app": "fastmcp"}}, "policyTypes": ["Ingress"],
                 "ingress": [{"from": [{"podSelector": {"matchLabels": {"app": instance}}}],
                              "ports": [{"protocol": "TCP", "port": 8000}]}]},
    })
    if os.environ.get("FOUNDRY_AGENT_SUBNET_CIDR", "").strip():
        agent_network = ipaddress.ip_network(required("FOUNDRY_AGENT_SUBNET_CIDR"), strict=True)
        if agent_network.version != 4 or not agent_network.is_private:
            raise ValueError("FOUNDRY_AGENT_SUBNET_CIDR must be the private delegated IPv4 subnet")
        resources[-1]["spec"]["ingress"].append({
            "from": [{"ipBlock": {"cidr": str(agent_network)}}],
            "ports": [{"protocol": "TCP", "port": 8000}],
        })
        resources.append({
            "apiVersion": "v1", "kind": "Service",
            "metadata": {
                "name": "fastmcp-foundry-internal", "namespace": namespace,
                "annotations": {"service.beta.kubernetes.io/azure-load-balancer-internal": "true"},
            },
            "spec": {
                "type": "LoadBalancer", "selector": {"app": "fastmcp"},
                "externalTrafficPolicy": "Local",
                "loadBalancerSourceRanges": [str(agent_network)],
                "ports": [{"port": 8000, "targetPort": 8000}],
            },
        })
    return {"apiVersion": "v1", "kind": "List", "items": resources}


if __name__ == "__main__":
    print(json.dumps(render(), indent=2))
