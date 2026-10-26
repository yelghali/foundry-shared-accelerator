"""Network-connected validation tasks executed on the retained private VM."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
from typing import Any
from urllib.parse import urlsplit

import requests
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import HttpResponseError
from azure.identity import ManagedIdentityCredential

from .agent import deploy_agent, verify_direct
from .control import GATEWAY_SCOPE, ControlPlane, credential, load_json


def verify_isolation(projects: list[dict[str, Any]]) -> dict[str, Any]:
    from samples.consume_agent import consume

    if len(projects) != 2:
        raise ValueError("Isolation validation requires exactly two test projects.")
    results: dict[str, Any] = {}
    for index, project in enumerate(projects):
        other = projects[1 - index]
        cred = ManagedIdentityCredential(client_id=project["sample_caller"]["client_id"])
        with AIProjectClient(endpoint=project["endpoint"], credential=cred, allow_preview=True) as client:
            own = client.agents.get(agent_name=project["agent"]["name"])
            if own.id != project["agent"]["id"]:
                raise RuntimeError("Own-project agent identity did not match the release receipt.")
        try:
            with AIProjectClient(endpoint=other["endpoint"], credential=cred, allow_preview=True) as client:
                client.agents.get(agent_name=other["agent"]["name"])
        except HttpResponseError as error:
            if error.status_code != 403:
                raise
            cross_project_status = error.status_code
        else:
            raise RuntimeError("A team caller unexpectedly read the other project's agent.")
        control = ControlPlane(cred)
        marker = project["name"].replace("-", "_").upper() + "_GATEWAY_READY"
        output = consume(project, f"Reply with exactly {marker}", control)
        if marker not in output["output_text"]:
            raise RuntimeError("The gateway-mediated sample omitted its expected team marker.")
        token = control.credential.get_token(GATEWAY_SCOPE).token
        wrong_gateway = requests.post(
            other["agent_mcp_endpoint"],
            headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout=90,
        )
        if wrong_gateway.status_code not in {401, 403}:
            raise RuntimeError("A team's Entra identity unexpectedly reached the other team's gateway tool server.")
        results[project["name"]] = {
            "own_agent_read": True,
            "gateway_agent_marker": marker,
            "cross_project_status": cross_project_status,
            "cross_gateway_status": wrong_gateway.status_code,
            "authentication": "managed_identity_no_keys",
        }
    return results


def private_checks(deployment: dict[str, Any]) -> dict[str, Any]:
    cred = credential(managed_identity=True)
    addresses: dict[str, str] = {}
    for account in [deployment["agent_foundry"], *deployment["models"].values()]:
        hostname = urlsplit(account["endpoint"]).hostname
        if hostname is None:
            raise ValueError("Account endpoint has no hostname.")
        address = socket.gethostbyname(hostname)
        if not ipaddress.ip_address(address).is_private:
            raise RuntimeError(f"{hostname} did not resolve privately.")
        addresses[hostname] = address
    denied: dict[str, Any] = {}
    for region, account in deployment["models"].items():
        response = requests.post(
            account["model_endpoint"] + "openai/v1/chat/completions",
            headers={"Authorization": "Bearer " + cred.get_token("https://cognitiveservices.azure.com/.default").token},
            json={"model": deployment["model_name"], "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 1},
            timeout=90,
        )
        error = response.json().get("error", {})
        if response.status_code not in {401, 403} or error.get("code") != "PermissionDenied":
            raise RuntimeError(
                f"Unprivileged validation identity expected an explicit permission denial in {region}, got {response.status_code}; "
                f"code={error.get('code')}; message={error.get('message')}"
            )
        denied[region] = {"status": response.status_code, "code": error["code"]}
    response = requests.post(
        deployment["gateway"]["endpoint"] + "/default/models/openai/v1/chat/completions",
        headers={"Authorization": "Bearer " + cred.get_token(GATEWAY_SCOPE).token},
        json={"model": "approved-chat", "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 4},
        timeout=120,
    )
    if not response.ok:
        raise RuntimeError(f"Managed-identity gateway inference failed: {response.status_code} {response.text[:1200]}")
    return {"private_dns": addresses, "direct_model_denials": denied, "gateway_mi_inference_usage": response.json().get("usage")}


def verify_cross_access(caller: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    cred = ManagedIdentityCredential(client_id=caller["sample_caller"]["client_id"])
    try:
        with AIProjectClient(endpoint=target["endpoint"], credential=cred, allow_preview=True) as client:
            client.agents.get(agent_name=target["agent"]["name"])
    except HttpResponseError as error:
        if error.status_code != 403:
            raise
    else:
        raise RuntimeError("Caller unexpectedly read the foreign private agent.")
    response = requests.post(
        target["agent_mcp_endpoint"],
        headers={"Authorization": "Bearer " + cred.get_token(GATEWAY_SCOPE).token,
                 "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, timeout=90,
    )
    if response.status_code != 403:
        raise RuntimeError(f"Expected foreign shared-gateway asset denial, received {response.status_code}.")
    return {"caller_project_id": caller["id"], "target_project_id": target["id"],
            "foreign_private_agent_status": 403, "foreign_shared_gateway_asset_status": response.status_code}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    request = load_json(args.request)
    task = request["task"]
    if task == "private-checks":
        receipt = private_checks(request["deployment"])
    elif task == "isolation":
        receipt = verify_isolation(request["projects"])
    elif task == "cross-access":
        receipt = verify_cross_access(request["caller"], request["target"])
    elif task == "model-probes":
        from .usage import model_probes
        projects = []
        for project in request["projects"]:
            cred = ManagedIdentityCredential(client_id=project["sample_caller"]["client_id"])
            evidence = model_probes(
                ControlPlane(cred), request["deployment"], project, request.get("count", 8),
                header_project_key=request.get("header_project_key"),
            )
            projects.append({key: value for key, value in evidence.items() if key != "calls"} | {"call_count": len(evidence["calls"])})
        receipt = {"projects": projects}
    elif task == "deploy-toolbox":
        from .toolbox import deploy_toolbox
        receipt = deploy_toolbox(request["project"], credential(managed_identity=True))
    elif task == "prepare-iq":
        from .knowledge import provision
        receipt = provision(request["deployment"], request["project"], request["manifest"],
                            credential(managed_identity=True))
    elif task == "verify-iq":
        from .knowledge import verify_ingestion
        receipt = verify_ingestion(request["deployment"], request["project"],
                                   credential(managed_identity=True))
    elif task == "verify-iq-agent":
        from .knowledge import verify_project_agent
        receipt = verify_project_agent(request["project"], credential(managed_identity=True))
    elif task == "verify-iq-isolation":
        from .knowledge import verify_foreign_denial
        receipt = verify_foreign_denial(request["project"], request["callers"])
    elif task == "deploy-agent":
        project = request["project"]
        cred = credential(managed_identity=True)
        agent = deploy_agent(
            project, cred, name=request.get("name", "assistant"), mode=request.get("mode", "ensure"),
            expected_agent_id=request.get("agent_id"),
            template=request.get("template"),
        )
        verification = verify_direct(project, agent, cred, request.get("marker", "PRIVATE_AGENT_READY"))
        receipt = {"agent": agent, "direct_verification": verification}
    else:
        raise ValueError(f"Unknown runner task: {task}")
    print("FSA_RESULT:" + json.dumps(receipt, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
