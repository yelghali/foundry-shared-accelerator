"""Small synchronous MCP client for the retained gateway sample."""

from __future__ import annotations

import json
from typing import Any

import requests


def parse_rpc_response(response: requests.Response, request_id: int) -> dict[str, Any]:
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/event-stream" in content_type:
        candidates: list[dict[str, Any]] = []
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
        raise RuntimeError(f"MCP error: {json.dumps(value['error'])[:1200]}")
    result = value.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("MCP response did not contain an object result.")
    return result


class MCPClient:
    def __init__(self, endpoint: str, bearer_token: str) -> None:
        if not endpoint.startswith("https://"):
            raise ValueError("MCP sample requires an HTTPS endpoint.")
        self.endpoint = endpoint
        self.session = requests.Session()
        self.headers = {"Authorization": "Bearer " + bearer_token, "Accept": "application/json, text/event-stream"}
        self.next_id = 1

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        response = self.session.post(
            self.endpoint,
            headers=self.headers,
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
            timeout=240,
        )
        if response.headers.get("Mcp-Session-Id"):
            self.headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
        return parse_rpc_response(response, request_id)

    def initialize(self) -> None:
        result = self.call("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "foundry-shared-accelerator", "version": "1.0.0"},
        })
        self.headers["MCP-Protocol-Version"] = str(result["protocolVersion"])
        response = self.session.post(
            self.endpoint,
            headers=self.headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            timeout=60,
        )
        if response.status_code not in {200, 202, 204}:
            response.raise_for_status()


def agent_arguments(schema: dict[str, Any], question: str) -> dict[str, Any]:
    properties = schema.get("properties", {})
    result: dict[str, Any] = {}
    for name, definition in properties.items():
        normalized = name.lower().replace("_", "").replace("-", "")
        if normalized in {"input", "question", "message"}:
            result[name] = question
        elif normalized in {"body", "requestbody"} and definition.get("type") == "object":
            result[name] = agent_arguments(definition, question)
        elif normalized in {"store", "stream"}:
            result[name] = False
        elif "default" in definition:
            result[name] = definition["default"]
        elif len(definition.get("enum", [])) == 1:
            result[name] = definition["enum"][0]
        elif normalized == "apiversion":
            result[name] = "v1"
        elif name in schema.get("required", []):
            raise ValueError(f"Unsupported required MCP argument: {name}")
    if not result:
        raise ValueError("Agent tool input schema exposes no supported arguments.")
    return result


def completed_agent_output(result: dict[str, Any]) -> dict[str, str]:
    responses: list[dict[str, str]] = []

    def visit(node: Any, depth: int = 0) -> None:
        if depth > 12:
            raise ValueError("MCP response nesting exceeded the supported limit.")
        if isinstance(node, str):
            try:
                decoded = json.loads(node)
            except json.JSONDecodeError:
                return
            visit(decoded, depth + 1)
        elif isinstance(node, list):
            for item in node:
                visit(item, depth + 1)
        elif isinstance(node, dict):
            if node.get("isError"):
                raise RuntimeError("Nested MCP result reported a backend failure.")
            if node.get("object") == "response":
                if node.get("status") != "completed" or node.get("error") is not None:
                    raise RuntimeError("The Foundry response did not complete successfully.")
                text = "".join(
                    part["text"] for message in node.get("output", [])
                    if message.get("type") == "message" and message.get("role") == "assistant"
                    for part in message.get("content", [])
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str)
                )
                if text:
                    responses.append({"response_id": node["id"], "output_text": text})
            else:
                for value in node.values():
                    if isinstance(value, (str, list, dict)):
                        visit(value, depth + 1)

    visit(result)
    if len(responses) != 1:
        raise RuntimeError(f"Expected one completed Foundry agent response; found {len(responses)}.")
    return responses[0]
