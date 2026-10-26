"""Provision project-scoped Foundry IQ bindings on an approved Search pool."""

from __future__ import annotations

import argparse

import re
import time
from typing import Any

import requests
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.core.credentials import TokenCredential
from azure.identity import ManagedIdentityCredential

from .control import ControlPlane, FOUNDRY_API, credential, load_json, save_receipt

SEARCH_API = "2026-08-01-preview"
SEARCH_SCOPE = "https://search.azure.com/.default"
SEARCH_NAME = re.compile(r"^[a-z][a-z0-9-]{2,39}$")


class SearchClient:
    def __init__(self, endpoint: str, identity: TokenCredential) -> None:
        if not endpoint.startswith("https://") or not endpoint.endswith(".search.windows.net"):
            raise ValueError("Expected an approved Azure AI Search endpoint.")
        self.endpoint = endpoint
        self.identity = identity

    def list_names(self, collection: str) -> set[str]:
        if collection not in {"indexes", "knowledgesources", "knowledgebases"}:
            raise ValueError("Unexpected Search collection.")
        response = requests.get(
            f"{self.endpoint}/{collection}",
            params={"api-version": SEARCH_API},
            headers={"Authorization": "Bearer " + self.identity.get_token(SEARCH_SCOPE).token},
            timeout=180,
        )
        if not response.ok:
            raise RuntimeError(f"Search {collection} inventory returned HTTP {response.status_code}: {response.text[:900]}")
        page = response.json()
        if page.get("@odata.nextLink"):
            raise RuntimeError("Search inventory has more than one page; cannot safely allocate capacity.")
        return {item["name"] for item in page["value"]}

    def request(self, method: str, collection: str, name: str) -> dict[str, Any]:
        if collection not in {"knowledgesources", "knowledgebases"} or not SEARCH_NAME.fullmatch(name):
            raise ValueError("Invalid Search knowledge object.")
        response = requests.request(
            method,
            f"{self.endpoint}/{collection}/{name}",
            params={"api-version": SEARCH_API},
            headers={"Authorization": "Bearer " + self.identity.get_token(SEARCH_SCOPE).token},
            timeout=180,
        )
        if response.status_code == 404:
            raise LookupError(f"Search {collection}/{name} does not exist.")
        if not response.ok:
            raise RuntimeError(f"Search {collection}/{name} returned HTTP {response.status_code}: {response.text[:900]}")
        return response.json()

    def put(self, collection: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        response = requests.put(
            f"{self.endpoint}/{collection}/{name}",
            params={"api-version": SEARCH_API},
            headers={"Authorization": "Bearer " + self.identity.get_token(SEARCH_SCOPE).token},
            json=body,
            timeout=180,
        )
        if not response.ok:
            raise RuntimeError(f"Search {collection}/{name} creation returned HTTP {response.status_code}: {response.text[:900]}")
        return response.json()


def provision(
    deployment: dict[str, Any], project: dict[str, Any],
    manifest: dict[str, Any], identity: TokenCredential,
) -> dict[str, Any]:
    search = deployment["search"]
    storage_id = deployment["storage_account_id"]
    embedding = deployment["embedding"]
    if not project["id"].lower().startswith(deployment["agent_foundry"]["id"].lower() + "/projects/"):
        raise ValueError("Project belongs to a different Foundry account.")
    sources = manifest["sources"]
    bases = manifest["knowledge_bases"]
    if not sources or not bases:
        raise ValueError("At least one source and knowledge base is required.")
    if len({source["name"] for source in sources}) != len(sources):
        raise ValueError("Knowledge source names must be distinct.")
    if len({base["name"] for base in bases}) != len(bases):
        raise ValueError("Knowledge base names must be distinct.")
    for item in [*sources, *bases]:
        if not item["name"].startswith(project["name"] + "-"):
            raise ValueError("Knowledge object names must start with the project name.")
    client = SearchClient(search["endpoint"], identity)
    if search["sku"].lower() != "basic":
        raise ValueError("Capacity allocation needs an explicit limit for this Search SKU.")
    present_sources = client.list_names("knowledgesources")
    present_bases = client.list_names("knowledgebases")
    present_indexes = client.list_names("indexes")
    needed_sources = {source["name"] for source in sources} - present_sources
    needed_bases = {base["name"] for base in bases} - present_bases
    if (len(present_sources) + len(needed_sources) > 15
            or len(present_bases) + len(needed_bases) > 15
            or len(present_indexes) + len(needed_sources) > 15):
        raise RuntimeError("The Basic Search pool lacks capacity for the requested indexes, sources or knowledge bases.")
    allocated: dict[str, str] = {}
    for source in sources:
        name, container = source["name"], source["container"]
        if not SEARCH_NAME.fullmatch(name) or container not in project["containers"]:
            raise ValueError(f"Source {name} must reference an allocated project container.")
        try:
            value = client.request("GET", "knowledgesources", name)
        except LookupError:
            value = client.put("knowledgesources", name, {
                "name": name,
                "kind": "azureBlob",
                "azureBlobParameters": {
                    "connectionString": f"ResourceId={storage_id}",
                    "containerName": container,
                    "isADLSGen2": False,
                    "ingestionParameters": {
                        "networkAccessMode": "private",
                        "contentExtractionMode": "minimal",
                        "embeddingModel": {
                            "kind": "azureOpenAI",
                            "azureOpenAIParameters": {
                                "resourceUri": embedding["endpoint"],
                                "deploymentId": embedding["name"],
                                "modelName": embedding["name"],
                            },
                        },
                    },
                },
            })
        parameters = value.get("azureBlobParameters", {})
        if parameters.get("containerName") != container or parameters.get("connectionString", "").rstrip(";") != f"ResourceId={storage_id}":
            raise ValueError(f"Knowledge source {name} targets an unexpected storage container.")
        index = parameters.get("createdResources", {}).get("index")
        if not index:
            for _ in range(12):
                time.sleep(5)
                value = client.request("GET", "knowledgesources", name)
                index = value.get("azureBlobParameters", {}).get("createdResources", {}).get("index")
                if index:
                    break
        if not index:
            raise RuntimeError(f"Search has not reported the generated index for {name}.")
        allocated[name] = index
    bindings: dict[str, dict[str, str]] = {}
    for base in bases:
        name = base["name"]
        selected = base["sources"]
        if not SEARCH_NAME.fullmatch(name) or not selected or any(source not in allocated for source in selected):
            raise ValueError(f"Knowledge base {name} must use allocated knowledge sources.")
        try:
            existing = client.request("GET", "knowledgebases", name)
        except LookupError:
            existing = client.put("knowledgebases", name, {
                "name": name,
                "knowledgeSources": [{"name": source} for source in selected],
                "models": [],
                "outputMode": "extractiveData",
                "retrievalReasoningEffort": {"kind": "minimal"},
            })
        if {item["name"] for item in existing.get("knowledgeSources", [])} != set(selected):
            raise ValueError(f"Existing knowledge base {name} has different source ownership.")
        url = f"{search['endpoint']}/knowledgebases/{name}/mcp?api-version={SEARCH_API}"
        bindings[name] = {"mcp_endpoint": url}
    return {
        "search_id": search["id"],
        "search_endpoint": search["endpoint"],
        "knowledge_sources": allocated,
        "knowledge_bases": bindings,
        "index_ids": [f"{search['id']}/indexes/{name}" for name in sorted(set(allocated.values()))],
    }


def bind(project: dict[str, Any], receipt: dict[str, Any], control: ControlPlane) -> dict[str, Any]:
    bindings = {}
    for name, item in receipt["knowledge_bases"].items():
        if not name.startswith(project["name"] + "-"):
            raise ValueError("Refusing a foreign knowledge base binding.")
        connection_id = f"{project['id']}/connections/{name}-iq"
        control.put(connection_id, {"properties": {
            "authType": "ProjectManagedIdentity",
            "category": "RemoteTool",
            "target": item["mcp_endpoint"],
            "isSharedToAll": False,
            "audience": "https://search.azure.com/",
            "metadata": {"ApiType": "Azure"},
        }}, version=FOUNDRY_API)
        bindings[name] = {**item, "connection_id": connection_id}
    return {**receipt, "knowledge_bases": bindings}


def verify_ingestion(deployment: dict[str, Any], project: dict[str, Any],
                     identity: TokenCredential) -> dict[str, Any]:
    source = f"{project['name']}-docs"
    kb = f"{project['name']}-kb"
    if source not in project["containers"] or kb not in project["knowledge"]["knowledge_bases"]:
        raise ValueError("The project must own its test container and knowledge base.")
    marker = f"{project['name'].upper()} KNOWLEDGE INGESTION VERIFIED"
    storage = deployment["shared_storage"]["blob_endpoint"].rstrip("/")
    upload = requests.put(
        f"{storage}/{source}/verification.txt",
        headers={
            "Authorization": "Bearer " + identity.get_token("https://storage.azure.com/.default").token,
            "x-ms-version": "2023-11-03",
            "x-ms-blob-type": "BlockBlob",
            "Content-Type": "text/plain",
        },
        data=marker.encode(),
        timeout=90,
    )
    if upload.status_code != 201:
        raise RuntimeError(f"Private Blob upload failed: HTTP {upload.status_code}: {upload.text[:800]}")
    search = deployment["search"]["endpoint"]
    token = identity.get_token(SEARCH_SCOPE).token
    indexer = source + "-indexer"
    trigger = requests.post(
        f"{search}/indexers/{indexer}/run",
        params={"api-version": SEARCH_API},
        headers={"Authorization": "Bearer " + token},
        timeout=90,
    )
    if trigger.status_code not in {202, 204}:
        raise RuntimeError(f"Knowledge indexer trigger failed: HTTP {trigger.status_code}: {trigger.text[:800]}")
    for _ in range(30):
        time.sleep(10)
        status = requests.get(
            f"{search}/indexers/{indexer}/status",
            params={"api-version": SEARCH_API},
            headers={"Authorization": "Bearer " + identity.get_token(SEARCH_SCOPE).token},
            timeout=90,
        )
        if not status.ok:
            raise RuntimeError(f"Knowledge indexer status failed: HTTP {status.status_code}: {status.text[:800]}")
        result = status.json().get("lastResult") or {}
        if result.get("status") == "success" and result.get("itemsProcessed", 0) > 0:
            break
        if result.get("status") in {"transientFailure", "persistentFailure"}:
            raise RuntimeError(f"Knowledge indexer failed: {result.get('errorMessage', result)}")
    else:
        raise RuntimeError("Knowledge indexer did not finish processing the test blob within five minutes.")
    retrieve = requests.post(
        f"{search}/knowledgebases/{kb}/retrieve",
        params={"api-version": SEARCH_API},
        headers={"Authorization": "Bearer " + identity.get_token(SEARCH_SCOPE).token},
        json={
            "intents": [{"type": "semantic", "search": f"What does verification.txt say for {project['name']}?"}],
            "knowledgeSourceParams": [{"knowledgeSourceName": source, "kind": "azureBlob"}],
        },
        timeout=180,
    )
    if not retrieve.ok:
        raise RuntimeError(
            f"Knowledge retrieval failed: HTTP {retrieve.status_code}; "
            f"error={retrieve.headers.get('x-ms-error-code')}; "
            f"request-id={retrieve.headers.get('request-id')}: {retrieve.text[:1000]}"
        )
    body = retrieve.json()
    if marker not in str(body):
        raise RuntimeError("Knowledge retrieval did not return the uploaded team marker.")
    return {"project": project["name"], "indexer_status": result["status"],
            "items_processed": result["itemsProcessed"], "grounded_marker_found": True}


def verify_project_agent(project: dict[str, Any], identity: TokenCredential) -> dict[str, Any]:
    bases = project["knowledge"]["knowledge_bases"]
    if len(bases) != 1:
        raise ValueError("Agent verification requires exactly one team knowledge base.")
    kb = next(iter(bases.values()))
    marker = f"{project['name'].upper()} KNOWLEDGE INGESTION VERIFIED"
    name = "iq-probe"
    with AIProjectClient(endpoint=project["endpoint"], credential=identity, allow_preview=True) as client:
        version = client.agents.create_version(
            agent_name=name,
            definition=PromptAgentDefinition(
                model=project["model_reference"],
                instructions="Always use the knowledge base tool to answer. Quote the relevant document; never guess.",
                tools=[MCPTool(
                    server_label="knowledge-base",
                    server_url=kb["mcp_endpoint"],
                    require_approval="never",
                    allowed_tools=["knowledge_base_retrieve"],
                    project_connection_id=kb["connection_id"].rsplit("/", 1)[-1],
                )],
            ),
        )
        for _ in range(60):
            if str(version.status).lower() not in {"creating", "agentversionstatus.creating"}:
                break
            time.sleep(5)
            version = client.agents.get_version(agent_name=name, agent_version=version.version)
        if str(version.status).lower() in {
            "creating", "agentversionstatus.creating", "failed", "agentversionstatus.failed",
        }:
            raise RuntimeError(f"Knowledge agent version did not become ready: {version.status}")
        response = client.get_openai_client().responses.create(
            input=f"What does verification.txt say for {project['name']}? Use the knowledge base tool.",
            store=False,
            max_output_tokens=300,
            extra_body={"agent_reference": {"name": name, "version": version.version, "type": "agent_reference"}},
        )
    if marker not in response.output_text:
        raise RuntimeError(f"Knowledge agent did not return its team marker: {response.output_text[:900]}")
    return {"project": project["name"], "agent": name, "version": version.version,
            "project_identity_knowledge_retrieval": True}


def verify_foreign_denial(project: dict[str, Any], callers: dict[str, Any]) -> dict[str, Any]:
    search = project["search"]
    bases = project["knowledge"]["knowledge_bases"]
    if len(bases) != 1 or not callers:
        raise ValueError("Isolation verification requires one KB and at least one foreign caller.")
    kb = next(iter(bases))
    results = {}
    for name, caller in callers.items():
        if name == project["name"]:
            raise ValueError("Isolation caller must belong to another project.")
        identity = ManagedIdentityCredential(client_id=caller["client_id"])
        response = requests.post(
            f"{search['endpoint']}/knowledgebases/{kb}/retrieve",
            params={"api-version": SEARCH_API},
            headers={"Authorization": "Bearer " + identity.get_token(SEARCH_SCOPE).token},
            json={"intents": [{"type": "semantic", "search": "Read verification.txt"}]},
            timeout=90,
        )
        if response.status_code != 403:
            raise RuntimeError(f"Foreign caller {name} unexpectedly received HTTP {response.status_code} from {kb}.")
        results[name] = response.status_code
    return {"project": project["name"], "foreign_knowledge_denials": results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["bind"])
    parser.add_argument("--project", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    save_receipt(args.output, bind(load_json(args.project), load_json(args.receipt),
                                   ControlPlane(credential())))


if __name__ == "__main__":
    main()
