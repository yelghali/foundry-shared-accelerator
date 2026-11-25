"""Consume APIM/LiteLLM without Kubernetes access or an administrative master key."""

import argparse
import json
import os
import uuid
from urllib.parse import urlsplit

import requests

from accelerator.control import load_json
from accelerator.entra_setup import cached_user_token
from accelerator.mcp import MCPClient, parse_rpc_response


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
    args = parser.parse_args()
    if args.agent_name and args.kind != "hosted-agent":
        raise ValueError("--agent-name applies only to --kind hosted-agent")
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
        client = MCPClient(base + "/blob/mcp", token)
        client.headers["x-litellm-api-key"] = "Bearer " + key
        try:
            client.initialize()
            print(json.dumps(client.call("tools/list"), indent=2))
        finally:
            client.session.close()
        return
    if args.kind == "model":
        response = requests.post(
            base + "/v1/chat/completions", headers=headers,
            json={"model": "enterprise-chat", "messages": [{"role": "user", "content": args.prompt}]},
            timeout=180,
        )
        response.raise_for_status()
        print(response.json()["choices"][0]["message"]["content"])
        return
    if args.kind in {"hosted-agent-responses", "prompt-agent-responses"}:
        response = requests.post(
            base + "/v1/responses", headers=headers,
            json={"model": args.kind, "input": args.prompt}, timeout=240,
        )
        response.raise_for_status()
        print(json.dumps(response.json(), indent=2))
        return
    agent = (args.agent_name or "echo") if args.kind == "hosted-agent" else "declarative"
    response = requests.post(
        base + "/a2a/" + agent, headers={**headers, "A2A-Version": "1.0"},
        json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
            "message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": args.prompt}]},
            "configuration": {"returnImmediately": False},
        }},
        timeout=240,
    )
    print(json.dumps(parse_rpc_response(response, 1), indent=2))


if __name__ == "__main__":
    main()
