"""Model-backed hosted agent with native A2A and Responses protocol handlers."""

import asyncio
import os
import ipaddress
import socket
from urllib.parse import urlsplit

from a2a.helpers import get_message_text, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils.errors import TaskNotCancelableError
from azure.ai.agentserver.a2a import A2AAgentServerHost
from azure.ai.agentserver.core import AgentConfig
from azure.ai.agentserver.responses import CreateResponse, ResponseContext, ResponsesAgentServerHost, TextResponse
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncOpenAI
import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be configured")
    return value


async def respond(text: str) -> str:
    endpoint = required("MODEL_ENDPOINT").rstrip("/")
    if urlsplit(endpoint).scheme != "https":
        raise ValueError("MODEL_ENDPOINT must use HTTPS")
    if text.strip() == "/network-check":
        addresses = await asyncio.to_thread(socket.getaddrinfo, "example.com", 443, socket.AF_INET)
        address = addresses[0][4][0]
        if not ipaddress.ip_address(address).is_global:
            raise RuntimeError("The fixed outbound probe did not resolve to a public IP")
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
                await client.get("https://example.com/")
        except (httpx.ConnectTimeout, httpx.ReadTimeout):
            return "INTERNET_EGRESS_BLOCKED"
        raise RuntimeError("Unapproved public HTTPS egress succeeded; private demo acceptance must fail")
    async with DefaultAzureCredential() as identity:
        if text.startswith("/mcp-read "):
            name = text.removeprefix("/mcp-read ").strip()
            if not name or "\n" in name:
                raise ValueError("Use /mcp-read <blob-name>")
            target = required("PRIVATE_MCP_URL")
            parsed = urlsplit(target)
            private_http = False
            if parsed.scheme == "http" and parsed.hostname:
                try:
                    address = ipaddress.ip_address(parsed.hostname)
                    private_http = any(address in ipaddress.ip_network(network)
                                       for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
                except ValueError:
                    private_http = False
            if (parsed.scheme != "https" and not private_http) or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("PRIVATE_MCP_URL must use HTTPS or an explicitly configured private IP over HTTP")
            token = await identity.get_token(required("MCP_TOKEN_SCOPE"))
            async with Client(StreamableHttpTransport(
                target, headers={"Authorization": "Bearer " + token.token},
            )) as mcp:
                result = await mcp.call_tool("read_blob", {"name": name})
                if result.is_error:
                    raise RuntimeError("Private MCP read_blob failed")
                contents = "\n".join(part.text for part in result.content if part.type == "text")
                if not contents:
                    raise RuntimeError("Private MCP returned no text")
                return contents
        provider = get_bearer_token_provider(identity, "https://cognitiveservices.azure.com/.default")
        async with AsyncOpenAI(base_url=endpoint + "/openai/v1", api_key=provider) as client:
            response = await client.responses.create(
                model=required("MODEL_DEPLOYMENT"), input=text,
            )
            if not response.output_text:
                raise RuntimeError("The configured model returned no output text")
            return response.output_text


class ModelExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.message is None:
            raise ValueError("An A2A message is required")
        task = new_task_from_user_message(context.message)
        await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        text = await respond(get_message_text(context.message))
        await updater.add_artifact(parts=[new_text_part(text, media_type="text/plain")])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise TaskNotCancelableError(message="This demo does not implement cancellation")


class DualHost(A2AAgentServerHost, ResponsesAgentServerHost):
    pass


def create_host() -> DualHost:
    config = AgentConfig.from_env()
    card = AgentCard(
        name="Gateway model-backed hosted agent",
        description="A2A and Responses handlers invoking an explicitly configured model.",
        version="1.0.0", capabilities=AgentCapabilities(),
        default_input_modes=["text/plain"], default_output_modes=["text/plain"],
        supported_interfaces=[AgentInterface(
            protocol_binding="JSONRPC", protocol_version="1.0",
            url=f"http://localhost:{config.port}/a2a",
        )],
        skills=[AgentSkill(id="chat", name="Chat", description="Model-backed chat", tags=["chat"])],
    )
    host = DualHost(agent_card=card, agent_executor=ModelExecutor())

    @host.response_handler
    async def handle_response(request: CreateResponse, context: ResponseContext, cancellation_signal: asyncio.Event):
        if cancellation_signal.is_set():
            raise asyncio.CancelledError
        return TextResponse(context, request, text=await respond(await context.get_input_text()))

    return host


if __name__ == "__main__":
    create_host().run(host="0.0.0.0" if AgentConfig.from_env().is_hosted else "127.0.0.1")
