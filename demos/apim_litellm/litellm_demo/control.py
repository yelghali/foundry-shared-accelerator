"""Demo-local Azure ARM requests and atomic, nonsecret validation receipts."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential


class AzureRequestError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        super().__init__(f"Azure HTTP {status} {code}: {message}")


def credential() -> TokenCredential:
    return AzureCliCredential(process_timeout=120)


def load_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    return value


def save_receipt(path: str | Path, value: dict[str, Any]) -> None:
    def check(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key.lower() in {
                    "primarykey", "secondarykey", "api-key", "authorization", "credentials",
                    "access_token", "refresh_token", "client_secret",
                }:
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

    def request(
        self, method: str, resource_id: str, body: dict[str, Any] | None = None,
        *, version: str, extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not resource_id.startswith("/subscriptions/"):
            raise ValueError("Azure operations require an explicit subscription resource ID.")
        token = self.credential.get_token("https://management.azure.com/.default").token
        for attempt in range(5):
            response = self.session.request(
                method, "https://management.azure.com" + resource_id,
                params={"api-version": version},
                headers={"Authorization": "Bearer " + token, **(extra_headers or {})},
                json=body, timeout=180,
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
            raise AzureRequestError(response.status_code, code, message.replace(token, "[REDACTED]")[:2000])
        if not response.content:
            return {}
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError(f"Azure {method} returned a non-object response.")
        return value

    def put(self, resource_id: str, body: dict[str, Any], *, version: str) -> dict[str, Any]:
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

    def grant(self, principal_id: str, scope: str, role: str, *, principal_type: str = "ServicePrincipal") -> str:
        subscription = scope.split("/")[2]
        name = uuid.uuid5(uuid.NAMESPACE_URL, f"{scope.lower()}|{principal_id.lower()}|{role.lower()}")
        resource_id = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}"
        self.put(resource_id, {"properties": {
            "principalId": principal_id, "principalType": principal_type,
            "roleDefinitionId": f"/subscriptions/{subscription}/providers/Microsoft.Authorization/roleDefinitions/{role}",
        }}, version="2022-04-01")
        return resource_id
