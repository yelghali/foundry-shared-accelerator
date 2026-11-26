"""Consume APIM/LiteLLM without Kubernetes access or an administrative master key."""

import argparse
import json
import os
import sys
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import requests

from accelerator.control import load_json
from accelerator.entra_setup import cached_user_token
from accelerator.mcp import MCPClient, parse_rpc_response


DEMO_AGENTS = {
    "prompt-agent-responses": "gateway-declarative",
    "hosted-agent-responses": "gateway-hosted-dual",
    "declarative": "gateway-declarative",
    "hosted": "gateway-hosted-dual",
    "echo": "gateway-a2a-echo",
}


def diagnostic(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def post_request(url: str, *, headers: dict[str, str], json: dict, timeout: int | float) -> requests.Response:
    started = time.perf_counter()
    try:
        response = requests.post(url, headers=headers, json=json, timeout=timeout)
    except requests.RequestException:
        diagnostic(f"HTTP request failed after {time.perf_counter() - started:.3f}s; credentials omitted.")
        raise
    diagnostic(
        f"HTTP status: {response.status_code}; client round trip: {time.perf_counter() - started:.3f}s "
        "(excludes credential loading)"
    )
    backend = response.headers.get("x-litellm-model-api-base")
    if isinstance(backend, str) and backend:
        try:
            parsed = urlsplit(backend)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname:
                raise ValueError("Invalid backend URL")
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            if parsed.port is not None:
                host += f":{parsed.port}"
            safe_url = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
            diagnostic(f"Server-reported backend base (header): {safe_url}")
        except ValueError:
            diagnostic("Server-reported backend base: invalid URL omitted.")
    for name in (
        "request-id", "x-request-id", "apim-request-id", "x-ms-request-id", "x-litellm-call-id",
        "llm_provider-request-id", "llm_provider-apim-request-id", "llm_provider-x-request-id",
        "x-litellm-response-duration-ms", "x-litellm-overhead-duration-ms", "x-litellm-callback-duration-ms",
        "llm_provider-x-agent-version-resolved",
        "llm_provider-x-ms-debug-latency-session-start-type",
        "llm_provider-x-ms-debug-latency-infra-setup-ms",
        "llm_provider-x-ms-debug-latency-container-readiness-ms",
        "llm_provider-x-ms-debug-latency-platform-preprocessing-ms",
        "llm_provider-x-ms-debug-latency-response-begin-ms",
        "llm_provider-x-ms-debug-latency-container-response-ms",
    ):
        value = response.headers.get(name)
        if isinstance(value, str) and value:
            diagnostic(f"Response header {name}: {value}")
    return response


def report_agent(payload: dict, alias: str, protocol: str) -> None:
    diagnostic(f"Requested gateway alias: {alias}")
    diagnostic(
        f"Expected demo mapping (client-side convention, not server attestation): {DEMO_AGENTS[alias]}"
    )
    references = [payload.get("agent_reference")]
    references.extend(
        item.get("agent_reference") for item in payload.get("output", [])
        if isinstance(item, dict) and item.get("role") == "assistant"
    )
    seen = set()
    for reference in references:
        if not isinstance(reference, dict) or not isinstance(reference.get("name"), str):
            continue
        version = reference.get("version")
        version = str(version) if isinstance(version, (str, int)) else "not provided"
        identity = (reference["name"], version)
        if identity not in seen:
            seen.add(identity)
            diagnostic(f"Server agent_reference: name={identity[0]}; version={identity[1]}")
            if identity[0] != DEMO_AGENTS[alias]:
                diagnostic("WARNING: server agent name differs from the client-side demo convention.")
    if not seen:
        diagnostic(
            "Server agent_reference: not returned. A2A Tasks do not require this field; "
            "no identity metadata has been invented." if protocol == "A2A" else
            "Server agent_reference: not returned; the expected demo mapping is not verified by this response."
        )
    session = payload.get("agent_session_id")
    if isinstance(session, str) and session:
        diagnostic(f"Hosted session ID (server): {session}")
    if protocol == "A2A":
        task = payload.get("task")
        if isinstance(task, dict):
            diagnostic(f"A2A Task ID (server): {task.get('id', 'not provided')}")
            diagnostic(f"A2A context ID (server): {task.get('contextId', 'not provided')}")
            diagnostic(f"A2A Task state: {task.get('status', {}).get('state', 'not provided')}")
            artifact_ids = [artifact["artifactId"] for artifact in task.get("artifacts", [])
                            if isinstance(artifact, dict) and artifact.get("artifactId")]
            diagnostic(f"A2A artifact IDs (server): {artifact_ids}")
        else:
            diagnostic("A2A result is not a Task envelope; inspect the unchanged JSON result.")
    else:
        diagnostic(f"Responses status: {payload.get('status', 'not provided')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="Public APIM base including /ai or its named scenario suffix.")
    parser.add_argument("--kind", choices=[
        "model", "mcp", "hosted-agent", "declarative-agent",
        "hosted-agent-responses", "prompt-agent-responses",
    ], required=True)
    parser.add_argument("--agent-name", choices=["echo", "hosted"],
                        help="A2A hosted alias; use hosted for the private demo, echo for the legacy transport sample.")
    parser.add_argument("--prompt", default="Reply exactly HELLO")
    parser.add_argument("--identity")
    parser.add_argument("--user-token-cache")
    parser.add_argument("--background", action="store_true", help="A2A: submit returnImmediately=true, then poll GetTask.")
    parser.add_argument("--poll-timeout", type=int, default=180, help="Maximum A2A polling seconds after submission.")
    args = parser.parse_args()
    if args.agent_name and args.kind != "hosted-agent":
        raise ValueError("--agent-name applies only to --kind hosted-agent")
    if args.background and args.kind not in {"hosted-agent", "declarative-agent"}:
        parser.error("--background applies only to A2A agent calls")
    if args.poll_timeout <= 0:
        parser.error("--poll-timeout must be positive")
    parsed = urlsplit(args.endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Provide an HTTPS APIM base URL without credentials, query or fragment.")
    key = os.environ.get("LITELLM_VIRTUAL_KEY", "").strip()
    if not key:
        raise ValueError("LITELLM_VIRTUAL_KEY must contain a consumer virtual key, never the administrative master key.")
    if args.user_token_cache:
        if not args.identity:
            raise ValueError("--user-token-cache requires its matching --identity receipt.")
        token = cached_user_token(load_json(args.identity), args.user_token_cache)
    else:
        token = os.environ.get("GATEWAY_ACCESS_TOKEN", "").strip()
        if not token:
            raise ValueError("Provide GATEWAY_ACCESS_TOKEN or explicitly select an encrypted user token cache.")
    base = args.endpoint.rstrip("/")
    headers = {"Authorization": "Bearer " + token, "x-litellm-api-key": "Bearer " + key}
    if args.kind == "mcp":
        url = base + "/blob/mcp"
        print(f"POST {url} [MCP initialize / tools/list]", file=sys.stderr, flush=True)
        client = MCPClient(url, token)
        client.headers["x-litellm-api-key"] = "Bearer " + key
        try:
            client.initialize()
            print(json.dumps(client.call("tools/list"), indent=2))
        finally:
            client.session.close()
        return
    if args.kind == "model":
        url = base + "/v1/chat/completions"
        print(f"POST {url} [model: enterprise-chat]", file=sys.stderr, flush=True)
        response = post_request(
            url, headers=headers,
            json={"model": "enterprise-chat", "messages": [{"role": "user", "content": args.prompt}]},
            timeout=180,
        )
        response.raise_for_status()
        print(response.json()["choices"][0]["message"]["content"])
        return
    if args.kind in {"hosted-agent-responses", "prompt-agent-responses"}:
        url = base + "/v1/responses"
        print(f"POST {url} [Responses: {args.kind}]", file=sys.stderr, flush=True)
        response = post_request(
            url, headers=headers,
            json={"model": args.kind, "input": args.prompt}, timeout=240,
        )
        response.raise_for_status()
        payload = response.json()
        report_agent(payload, args.kind, "Responses")
        print(json.dumps(payload, indent=2))
        return
    agent = (args.agent_name or "echo") if args.kind == "hosted-agent" else "declarative"
    url = base + "/a2a/" + agent
    print(f"POST {url} [A2A SendMessage]", file=sys.stderr, flush=True)
    message_id = str(uuid.uuid4())
    diagnostic(f"Protocol: A2A 1.0; A2A-Version: 1.0; JSON-RPC: 2.0; method: SendMessage; request ID: 1; message ID: {message_id}")
    response = post_request(
        url, headers={**headers, "A2A-Version": "1.0"},
        json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
            "message": {"messageId": message_id, "role": "ROLE_USER", "parts": [{"text": args.prompt}]},
            "configuration": {"returnImmediately": args.background},
        }},
        timeout=240,
    )
    payload = parse_rpc_response(response, 1)
    if args.background:
        task = payload.get("task")
        if not isinstance(task, dict) or not task.get("id"):
            raise RuntimeError("Background A2A submission returned no Task ID")
        task_id = task["id"]
        diagnostic(f"Background submitted: task={task_id}; state={task.get('status', {}).get('state')}")
        deadline = time.monotonic() + args.poll_timeout
        request_id = 1
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Background Task {task_id} did not complete before the polling deadline")
            request_id += 1
            diagnostic(f"POST {url} [A2A GetTask; A2A-Version: 1.0; task={task_id}]")
            response = post_request(
                url, headers={**headers, "A2A-Version": "1.0"},
                json={"jsonrpc": "2.0", "id": request_id, "method": "GetTask", "params": {"id": task_id}},
                timeout=max(1, min(60, remaining)),
            )
            result = parse_rpc_response(response, request_id)
            task = result.get("task", result)
            if task.get("id") != task_id:
                raise RuntimeError("GetTask returned a different Task ID")
            state = task.get("status", {}).get("state")
            diagnostic(f"Background poll: task={task_id}; state={state}")
            if state in {"completed", "TASK_STATE_COMPLETED"}:
                payload = {"task": task}
                break
            if state not in {"submitted", "working", "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
                raise RuntimeError(f"Background Task stopped without completion: {state}")
            time.sleep(min(3, max(0, deadline - time.monotonic())))
    report_agent(payload, agent, "A2A")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
