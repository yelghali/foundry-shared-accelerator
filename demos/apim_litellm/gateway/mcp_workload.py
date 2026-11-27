"""Pinned in-process MCP workload auth; custom integration, not native LiteLLM auth."""

import asyncio
import atexit
import inspect
import logging
import os
from functools import wraps
from importlib.metadata import version
from threading import Lock

import httpx2
from azure.identity import WorkloadIdentityCredential
from azure.core.exceptions import AzureError

from identity import required

logger = logging.getLogger(__name__)


class WorkloadAuth(httpx2.Auth):
    header_name = "Authorization"

    def __init__(self, target, scope):
        self.target = target
        self.scope = scope
        self.identity = WorkloadIdentityCredential()
        self.lock = Lock()
        atexit.register(self.identity.close)

    def token(self):
        # Reuse the credential's token cache and expiry-aware renewal across requests.
        with self.lock:
            return self.identity.get_token(self.scope).token

    async def async_auth_flow(self, request):
        if str(request.url).split("?", 1)[0] != self.target:
            raise ValueError("MCP workload credential destination mismatch")
        try:
            token = await asyncio.to_thread(self.token)
        except AzureError:
            logger.error("MCP workload token acquisition failed; no credential fallback")
            raise
        request.headers["Authorization"] = "Bearer " + token
        yield request


def install():
    # Profile C must retain LiteLLM's native delegated MCP OAuth/OBO pipeline.
    if os.environ.get("AUTHENTICATION_SCENARIO") == "user-obo":
        return
    if version("litellm") != "1.110.2":
        raise RuntimeError("MCP workload integration requires validated LiteLLM 1.110.2")
    from litellm.mcp_client.client import MCPClient

    original = MCPClient.__init__
    if getattr(original, "_scenario_workload_auth", False):
        return
    target = required("FASTMCP_URL")
    expected = f"http://fastmcp.{required('GATEWAY_NAMESPACE')}.svc.cluster.local:8000/mcp"
    if target != expected:
        raise ValueError("MCP workload auth requires the deployment-owned FastMCP endpoint")
    auth = WorkloadAuth(target, required("MCP_TOKEN_SCOPE"))
    signature = inspect.signature(original)
    if "resolved_auth" not in signature.parameters:
        raise RuntimeError("Pinned MCP HTTP authentication interface is unavailable")

    @wraps(original)
    def initialize(self, *args, **kwargs):
        # Scope auth to this client instance, not a process-wide HTTP transport:
        # unrelated registry servers must never receive the workload bearer.
        bound = signature.bind(self, *args, **kwargs)
        if bound.arguments.get("server_url") == target:
            bound.arguments["resolved_auth"] = auth
            bound.arguments["auth_value"] = None
            bound.arguments["auth_type"] = None
            bound.arguments["auth_header_name"] = "Authorization"
        original(*bound.args, **bound.kwargs)

    initialize._scenario_workload_auth = True
    MCPClient.__init__ = initialize
