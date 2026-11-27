"""Demo-local MCP client; rejects HTTP and JSON-RPC failures, including SSE errors."""

from __future__ import annotations

import json
from typing import Any

import requests


class MCPProtocolError(RuntimeError):
    def __init__(self, error: dict[str, Any]) -> None:
        self.error = error
        super().__init__(f"MCP error: {json.dumps(error)[:1200]}")


def parse_rpc_response(response: requests.Response, request_id: int) -> dict[str, Any]:
    response.raise_for_status()
    if "text/event-stream" in response.headers.get("Content-Type", "").lower():
        candidates = []
        for block in response.text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            if data and data != "[DONE]":
                value = json.loads(data)
                if isinstance(value, dict):
                    candidates.append(value)
        matching = [item for item in candidates if item.get("id") == request_id]
        if not matching:
            raise RuntimeError("MCP SSE response omitted the matching JSON-RPC result.")
        value = matching[-1]
    else:
        value = response.json()
    if not isinstance(value, dict) or value.get("id") != request_id:
        raise RuntimeError("MCP returned an invalid or mismatched JSON-RPC response.")
    if "error" in value:
        if not isinstance(value["error"], dict):
            raise RuntimeError("MCP returned an invalid JSON-RPC error object.")
        raise MCPProtocolError(value["error"])
    result = value.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("MCP response did not contain an object result.")
    return result


class MCPClient:
    def __init__(self, endpoint: str, bearer_token: str) -> None:
        if not endpoint.startswith("https://"):
            raise ValueError("MCP consumer calls require an HTTPS gateway endpoint.")
        self.endpoint = endpoint
        self.session = requests.Session()
        self.headers = {"Authorization": "Bearer " + bearer_token, "Accept": "application/json, text/event-stream"}
        self.next_id = 1

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        response = self.session.post(
            self.endpoint, headers=self.headers,
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
            timeout=240,
        )
        if response.headers.get("Mcp-Session-Id"):
            self.headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
        return parse_rpc_response(response, request_id)

    def initialize(self) -> None:
        result = self.call("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "standalone-litellm-demo", "version": "1.0.0"},
        })
        self.headers["MCP-Protocol-Version"] = str(result["protocolVersion"])
        response = self.session.post(
            self.endpoint, headers=self.headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=60,
        )
        if response.status_code not in {200, 202, 204}:
            response.raise_for_status()
