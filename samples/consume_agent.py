"""Invoke an agent exclusively through the public dedicated gateway MCP endpoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from accelerator.control import GATEWAY_SCOPE, ControlPlane, credential, load_json, save_receipt
from accelerator.mcp import MCPClient, agent_arguments, completed_agent_output


def consume(project: dict, question: str, control: ControlPlane, *, toolbox: bool = False, agent_name: str | None = None) -> dict:
    token = control.credential.get_token(GATEWAY_SCOPE).token
    if toolbox and agent_name:
        raise ValueError("Select a toolbox or an agent, not both.")
    endpoint = project["toolbox_mcp_endpoint"] if toolbox else project["agent_mcp_endpoint"]
    if agent_name:
        if agent_name not in project.get("agents", {}):
            raise ValueError("The requested agent is not registered in this project receipt.")
        endpoint = project["agents"][agent_name]["mcp_endpoint"]
    client = MCPClient(endpoint, token)
    client.initialize()
    listing = client.call("tools/list")
    tools = listing.get("tools", [])
    matches = [item for item in tools if item["name"].endswith("ask_agent")]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one registered ask_agent tool; found {len(matches)}.")
    tool = matches[0]
    arguments = agent_arguments(tool["inputSchema"], question)
    result = client.call("tools/call", {"name": tool["name"], "arguments": arguments})
    if result.get("isError"):
        raise RuntimeError(f"Gateway tool reported a backend failure: {control.redact(json.dumps(result))[:1500]}")
    response = completed_agent_output(result)
    return {
        "project_id": project["id"],
        "gateway_id": project["gateway"]["id"],
        "tool": tool["name"],
        "protocol": "MCP through dedicated AIGateway",
        **response,
        "result": result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Nonsecret registered team receipt.")
    parser.add_argument("--question", default="Reply with exactly GATEWAY_AGENT_READY.")
    parser.add_argument("--expected", help="Optional exact smoke-test output; omit for normal questions.")
    parser.add_argument("--output")
    parser.add_argument("--toolbox", action="store_true")
    parser.add_argument("--agent-name")
    args = parser.parse_args()
    result = consume(
        load_json(args.project), args.question, ControlPlane(credential()),
        toolbox=args.toolbox, agent_name=args.agent_name,
    )
    if args.expected is not None and result["output_text"].strip() != args.expected:
        raise RuntimeError("The gateway-mediated agent response omitted the expected marker.")
    if args.output:
        save_receipt(args.output, result)
    print(json.dumps({key: value for key, value in result.items() if key != "result"}, indent=2))


if __name__ == "__main__":
    main()
