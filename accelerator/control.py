"""Checked Azure management requests with redacted errors and nonsecret receipts."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential, ManagedIdentityCredential

GATEWAY_API = "2026-09-01-preview"
FOUNDRY_API = "2025-10-01-preview"
FOUNDRY_USER = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
AGENT_CONSUMER = "eed3b665-ab3a-47b6-8f48-c9382fb1dad6"
GATEWAY_MODELS_USER = "a6b68e3d-a1f0-4c60-8678-237c050cf4ea"
GATEWAY_TOOLS_USER = "dce03f8d-857f-4e40-a90d-67770fa6ecd2"
GATEWAY_AUDIENCE = "https://azure-api.net/aigateway"
GATEWAY_SCOPE = GATEWAY_AUDIENCE + "/.default"


class AzureRequestError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        super().__init__(f"Azure HTTP {status} {code}: {message}")


def credential(*, managed_identity: bool = False) -> TokenCredential:
    return ManagedIdentityCredential() if managed_identity else AzureCliCredential(process_timeout=120)


def load_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def save_receipt(path: str | Path, value: dict[str, Any]) -> None:
    def check(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key.lower() in {"primarykey", "secondarykey", "api-key", "authorization", "credentials"}:
                    raise ValueError(f"Credential field is forbidden in a receipt: {key}")
                check(item)
        elif isinstance(node, list):
            for item in node:
                check(item)

    check(value)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


class ControlPlane:
    def __init__(self, token_credential: TokenCredential) -> None:
        self.credential = token_credential
        self.session = requests.Session()
        self._secrets: set[str] = set()

    def protect(self, value: str) -> str:
        if not value:
            raise ValueError("An empty runtime credential is not usable.")
        self._secrets.add(value)
        return value

    def redact(self, message: str) -> str:
        for secret in self._secrets:
            message = message.replace(secret, "[REDACTED]")
        return message

    def request(
        self,
        method: str,
        resource_id: str,
        body: dict[str, Any] | None = None,
        *,
        version: str = GATEWAY_API,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not resource_id.startswith("/subscriptions/"):
            raise ValueError("Azure operations must target an explicit subscription resource ID.")
        headers = {
            "Authorization": "Bearer " + self.credential.get_token("https://management.azure.com/.default").token,
            **(extra_headers or {}),
        }
        for attempt in range(5):
            response = self.session.request(
                method,
                "https://management.azure.com" + resource_id,
                params={"api-version": version},
                headers=headers,
                json=body,
                timeout=180,
            )
            if response.status_code not in {429, 502, 503, 504} or method not in {"GET", "PUT"} or attempt == 4:
                break
            retry = response.headers.get("Retry-After", "")
            delay = min(int(retry), 60) if retry.isdigit() else 5 * (attempt + 1)
            print(f"Transient Azure {response.status_code}; retrying {method} after {delay}s.", flush=True)
            time.sleep(delay)
        if not response.ok:
            try:
                error = response.json().get("error", {})
                code = str(error.get("code", "RequestFailed"))
                message = str(error.get("message", response.reason))
                if error.get("details"):
                    message += "; details=" + json.dumps(error["details"])
            except (ValueError, AttributeError):
                code, message = "RequestFailed", response.reason
            raise AzureRequestError(response.status_code, code, self.redact(message[:2000]))
        if not response.content:
            return {}
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError(f"Azure {method} returned a non-object response for {resource_id}.")
        return value

    def put(self, resource_id: str, body: dict[str, Any], *, version: str = GATEWAY_API) -> dict[str, Any]:
        headers: dict[str, str] = {}
        try:
            existing = self.request("GET", resource_id, version=version)
        except AzureRequestError as error:
            if error.status != 404:
                raise
        else:
            headers["If-Match"] = str(existing.get("etag", "*"))
        result = self.request("PUT", resource_id, body, version=version, extra_headers=headers)
        deadline = time.monotonic() + 900
        while result.get("properties", {}).get("provisioningState") in {"Creating", "Updating", "Accepted", "InProgress"}:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Azure did not finish provisioning {resource_id}.")
            time.sleep(5)
            result = self.request("GET", resource_id, version=version)
        if result.get("properties", {}).get("provisioningState") in {"Failed", "Canceled"}:
            raise RuntimeError(f"Azure resource did not succeed: {resource_id}.")
        return result

    def runtime_key(self, key_id: str) -> str:
        value = self.request("POST", key_id + "/listSecrets")
        properties = value.get("properties", value)
        key = properties.get("primaryKey")
        if not isinstance(key, str):
            raise ValueError("The API-key secrets response omitted primaryKey.")
        return self.protect(key)

    def grant(self, principal_id: str, scope: str, role: str, *, principal_type: str = "ServicePrincipal") -> str:
        subscription = scope.split("/")[2]
        name = uuid.uuid5(uuid.NAMESPACE_URL, f"{scope.lower()}|{principal_id.lower()}|{role.lower()}")
        resource_id = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}"
        self.put(
            resource_id,
            {"properties": {
                "principalId": principal_id,
                "principalType": principal_type,
                "roleDefinitionId": f"/subscriptions/{subscription}/providers/Microsoft.Authorization/roleDefinitions/{role}",
            }},
            version="2022-04-01",
        )
        return resource_id
