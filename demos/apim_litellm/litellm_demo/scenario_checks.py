"""HTTP assertions shared by the three explicit, runnable scenario examples."""

import json
import time
import uuid
from urllib.parse import urlsplit

import requests

from .apim_litellm import completed_a2a_text, completed_response_text
from .mcp import MCPClient, parse_rpc_response


class ScenarioChecks:
    def __init__(self, endpoint, caller_token, consumer_key):
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Provide the approved HTTPS gateway base without credentials/query/fragment")
        self.endpoint = endpoint.rstrip("/")
        self.headers = {
            "Authorization": "Bearer " + caller_token,
            "x-litellm-api-key": "Bearer " + consumer_key,
        }
        self.caller_token = caller_token
        self.consumer_key = consumer_key
        self.session = requests.Session()
        self.results = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.session.close()

    def post(self, path, body, *, a2a=False, timeout=240):
        url = self.endpoint + path
        label = body.get("method") or body.get("model")
        print(f"POST {url} [{label}]", flush=True)
        response = self.session.post(
            url, headers={**self.headers, **({"A2A-Version": "1.0"} if a2a else {})},
            json=body, timeout=timeout,
        )
        response.raise_for_status()
        return response

    def passed(self, name, **evidence):
        self.results.append({"check": name, "passed": True, **evidence})
        print(f"PASS {name}: {json.dumps(evidence)}", flush=True)

    def model(self, marker):
        response = self.post("/v1/chat/completions", {
            "model": "enterprise-chat", "messages": [{"role": "user", "content": "Reply exactly " + marker}],
            "temperature": 0, "max_tokens": 40,
        })
        answer = response.json()["choices"][0]["message"]["content"]
        if answer.strip() != marker:
            raise RuntimeError("Model did not return the exact assistant marker")
        self.passed("model", actual=answer.strip())

    def mcp(self, *, expected_client_id, expected_principal_ids, delegated):
        url = self.endpoint + "/blob/mcp"
        print(f"POST {url} [initialize, tools/list, whoami, read_blob]", flush=True)
        client = MCPClient(url, self.caller_token)
        client.headers["x-litellm-api-key"] = "Bearer " + self.consumer_key
        try:
            client.initialize()
            names = [tool["name"] for tool in client.call("tools/list")["tools"]]
            tools = {}
            for suffix in ("whoami", "read_blob"):
                matches = [name for name in names if name.endswith(suffix)]
                if len(matches) != 1:
                    raise RuntimeError(f"Expected exactly one registered {suffix} tool")
                tools[suffix] = matches[0]
            identity = client.call("tools/call", {"name": tools["whoami"], "arguments": {}})
            if identity.get("isError"):
                raise RuntimeError("MCP whoami failed")
            claims = identity.get("structuredContent")
            if not isinstance(claims, dict):
                claims = json.loads("".join(p["text"] for p in identity.get("content", []) if p.get("type") == "text"))
            expected_mode = "user-obo" if delegated else "service"
            if (claims.get("oid") not in expected_principal_ids
                    or (claims.get("azp") or claims.get("appid")) != expected_client_id
                    or claims.get("authentication_mode") != expected_mode):
                raise RuntimeError("MCP backend identity does not match the selected scenario")
            if delegated and "tools.read" not in claims.get("scp", "").split():
                raise RuntimeError("MCP did not receive the delegated tools.read scope")
            result = client.call("tools/call", {
                "name": tools["read_blob"], "arguments": {"name": "gateway-check.txt"},
            })
            text = "".join(p["text"] for p in result.get("content", []) if p.get("type") == "text")
            if result.get("isError") or "MCP_WORKLOAD_IDENTITY_OK" not in text:
                raise RuntimeError("MCP did not return the actual seeded Blob marker")
            self.passed("mcp", identity_matched=True, authentication_mode=expected_mode,
                        actual_blob_marker="MCP_WORKLOAD_IDENTITY_OK")
        finally:
            client.session.close()

    def responses(self, alias, expected_agent, marker):
        payload = self.post("/v1/responses", {"model": alias, "input": "Reply exactly " + marker}).json()
        if completed_response_text(payload).strip() != marker:
            raise RuntimeError(f"{alias} did not return the exact assistant marker")
        reference = payload.get("agent_reference", {})
        if reference.get("name") != expected_agent:
            raise RuntimeError(f"{alias} returned an unexpected/missing server agent reference")
        self.passed(alias, actual=marker, server_agent=reference["name"], version=reference.get("version"))

    def a2a_job(self, alias, marker):
        path = "/a2a/" + alias
        response = self.post(path, {
            "jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
                "message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": "Reply exactly " + marker}]},
                "configuration": {"returnImmediately": True},
            },
        }, a2a=True)
        task = parse_rpc_response(response, 1).get("task")
        if not isinstance(task, dict) or not task.get("id"):
            raise RuntimeError("Background submission returned no native Task")
        task_id = task["id"]
        initial = task.get("status", {}).get("state")
        print(f"Submitted {alias}: Task={task_id}; state={initial}; returnImmediately=true", flush=True)
        deadline = time.monotonic() + 180
        request_id = 1
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("A2A job did not complete before the polling deadline")
            request_id += 1
            response = self.post(path, {
                "jsonrpc": "2.0", "id": request_id, "method": "GetTask", "params": {"id": task_id},
            }, a2a=True, timeout=max(1, min(60, remaining)))
            result = parse_rpc_response(response, request_id)
            task = result.get("task", result)
            if task.get("id") != task_id:
                raise RuntimeError("GetTask returned a different Task ID")
            state = task.get("status", {}).get("state")
            print(f"Poll {alias}: state={state}", flush=True)
            if state in {"completed", "TASK_STATE_COMPLETED"}:
                if completed_a2a_text({"task": task}).strip() != marker:
                    raise RuntimeError("Completed A2A artifacts did not contain the exact marker")
                break
            if state not in {"submitted", "working", "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
                raise RuntimeError(f"A2A Task ended without success: {state}")
            time.sleep(min(3, max(0, deadline - time.monotonic())))
        self.passed(alias + "-a2a-job", task_id=task_id, initial_state=initial,
                    final_state=state, actual_artifact=marker, polled=True)

    def hosted_private_mcp(self):
        # This is the hosted runtime's private tool path, not direct gateway MCP.
        payload = self.post("/v1/responses", {
            "model": "hosted-agent-responses", "input": "/mcp-read gateway-check.txt",
        }).json()
        if completed_response_text(payload).strip() != "MCP_WORKLOAD_IDENTITY_OK":
            raise RuntimeError("Hosted agent did not read the actual private MCP Blob")
        if payload.get("agent_reference", {}).get("name") != "gateway-hosted-dual":
            raise RuntimeError("Private tool response omitted the expected hosted agent reference")
        self.passed("hosted-private-mcp", actual_blob_marker="MCP_WORKLOAD_IDENTITY_OK")
