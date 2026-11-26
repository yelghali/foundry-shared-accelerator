"""OSS LiteLLM custom auth: validate APIM identity and deny non-sample routes."""

import logging
import uuid

from jwt import PyJWTError
from fastapi import HTTPException, Request
from litellm.proxy._types import LiteLLM_ObjectPermissionTable, UserAPIKeyAuth

from identity import required, verifier

logger = logging.getLogger(__name__)


def route_path(request: Request, mcp: str) -> str:
    def relative(path):
        root = request.scope.get("app_root_path", request.scope.get("root_path", "")).rstrip("/")
        if root and (path == root or path.startswith(root + "/")):
            path = path[len(root):]
        return path.rstrip("/")

    path = relative(request.scope["path"])
    original = request.scope.get("_original_path")
    if original is not None:
        # LiteLLM's named route rewrites /blob/mcp to /mcp/blob internally.
        original = relative(original)
        if original != f"/{mcp}/mcp" or path != f"/mcp/{mcp}":
            raise HTTPException(403, "MCP route is not allowlisted")
        return original
    return path


def has_override(value):
    if isinstance(value, dict):
        forbidden = {
            "api_base", "base_url", "api_key", "azure_ad_token",
            "azure_ad_token_provider", "custom_llm_provider", "litellm_params",
            "model_list", "fallbacks", "mock_response", "headers", "extra_headers",
            "azure_scope", "client_id", "client_secret", "tenant_id",
            "server_url", "server_label", "connector_id",
        }
        return bool(forbidden.intersection(value)) or value.get("type") == "mcp" or any(
            has_override(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(has_override(item) for item in value)
    return False


def object_permissions(mcp, agent, *, mcp_route, agent_route):
    mcp_ids, agent_ids = [], []
    if mcp_route:
        from litellm.proxy.mcp_server.mcp_server_manager import global_mcp_server_manager

        server = global_mcp_server_manager.get_mcp_server_by_name(mcp)
        if server is None:
            raise HTTPException(503, "Configured MCP server unavailable")
        mcp_ids.append(server.server_id)
    if agent_route:
        from litellm.proxy.agent_endpoints.agent_registry import global_agent_registry

        registered = global_agent_registry.get_agent_by_name(agent)
        if registered is None:
            raise HTTPException(503, "Configured agent unavailable")
        agent_ids.append(registered.agent_id)
    permission_id = uuid.uuid5(uuid.NAMESPACE_URL, f"apim:{','.join(mcp_ids)}:{','.join(agent_ids)}")
    return LiteLLM_ObjectPermissionTable(
        object_permission_id=str(permission_id), mcp_servers=mcp_ids, agents=agent_ids
    )


async def user_api_key_auth(request: Request, api_key: str) -> UserAPIKeyAuth:
    del api_key
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "APIM bearer token required")
    try:
        claims = await verifier("APIM").verify_async(header[7:])
    except (PyJWTError, ValueError) as exc:
        logger.warning("APIM identity rejected (%s)", type(exc).__name__)
        raise HTTPException(401, "Invalid APIM identity") from exc

    model = required("MODEL_ALIAS")
    agent = required("A2A_AGENT_NAME")
    mcp = required("MCP_SERVER_NAME")
    path = route_path(request, mcp)
    mcp_route = path == f"/{mcp}/mcp"
    agent_route = path.startswith(f"/a2a/{agent}/") or path in {
        f"/a2a/{agent}", f"/v1/a2a/{agent}/message/send",
    }
    allowed = False
    if request.method == "GET":
        allowed = path == "/v1/models" or path in {
            f"/a2a/{agent}/.well-known/agent.json",
            f"/a2a/{agent}/.well-known/agent-card.json",
        }
    if request.method == "POST":
        if path in {"/v1/chat/completions", "/chat/completions", "/v1/responses", "/responses"}:
            try:
                body = await request.json()
                # Remote MCP tools and nested provider overrides bypass this profile's registry.
                allowed = isinstance(body, dict) and body.get("model") == model and not has_override(body)
            except ValueError:
                allowed = False
        else:
            allowed = path in {
                f"/a2a/{agent}",
                f"/a2a/{agent}/message/send",
                f"/v1/a2a/{agent}/message/send",
            }
    # MCP is native Streamable HTTP, including GET SSE and DELETE session cleanup.
    if mcp_route and request.method in {"GET", "POST", "DELETE"}:
        allowed = True
    if not allowed:
        raise HTTPException(403, "Route or model is not allowlisted")
    return UserAPIKeyAuth(
        user_id=claims["oid"],
        models=[model],
        user_role="internal_user",
        object_permission=object_permissions(mcp, agent, mcp_route=mcp_route, agent_route=agent_route),
    )
