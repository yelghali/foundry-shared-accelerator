"""Constrain inference to administrator-registered backends, after native key auth."""

import os
import json
import importlib
import sys
from copy import deepcopy
from functools import wraps
from enum import Enum

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

class _CaptureOwner(Enum):
    framework = "framework"


_CAPTURE_OWNER = _CaptureOwner.framework


def install_request_capture():
    """Capture original ASGI bytes before v1.110.2 enriches its body snapshot."""
    global _CAPTURE_OWNER
    setup = importlib.import_module("litellm.proxy.litellm_pre_call_utils")
    original = setup.add_litellm_data_to_request
    if getattr(original, "_scenario_raw_capture", False):
        _CAPTURE_OWNER = original._scenario_capture_owner
        return

    @wraps(original)
    async def capture(data, request, *args, **kwargs):
        if request.scope.get("path", "").rstrip("/") not in {
            "/v1/chat/completions", "/chat/completions", "/v1/responses", "/responses",
        }:
            return await original(data, request, *args, **kwargs)
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            body = None
        result = await original(data, request, *args, **kwargs)
        # Native logging excludes secret_fields. Do not copy raw credentials
        # into proxy_server_request, which is an auditable, enriched snapshot.
        result.setdefault("secret_fields", {})["_scenario_request"] = (_CAPTURE_OWNER, body)
        result["secret_fields"]["_scenario_authorization"] = (
            _CAPTURE_OWNER, request.headers.get("authorization"),
        )
        return result

    capture._scenario_raw_capture = True
    capture._scenario_capture_owner = _CAPTURE_OWNER
    setup.add_litellm_data_to_request = capture
    processing = sys.modules.get("litellm.proxy.common_request_processing")
    if processing is not None and getattr(processing, "add_litellm_data_to_request", None) is original:
        processing.add_litellm_data_to_request = capture


def registered_mcp_tool(value):
    allowed_fields = {"type", "server_url", "server_label", "require_approval", "allowed_tools", "headers"}
    if set(value) - allowed_fields or value.get("headers") not in (None, {}):
        return False
    if value.get("server_label") is not None and not isinstance(value["server_label"], str):
        return False
    if value.get("require_approval") is not None and value["require_approval"] not in ("never", "always"):
        return False
    tools = value.get("allowed_tools")
    if tools is not None and (not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools)):
        return False
    target = value.get("server_url")
    if target == "litellm_proxy":
        return True
    if not isinstance(target, str) or not target.startswith("litellm_proxy/"):
        return False
    from litellm.proxy.mcp_server.mcp_server_manager import global_mcp_server_manager

    server = global_mcp_server_manager.get_mcp_server_by_name("blob")
    if server is None:
        return False
    return target in {
        f"litellm_proxy/mcp/{identifier}" for identifier in ("blob", server.server_id)
    } | {f"litellm_proxy/{identifier}" for identifier in ("blob", server.server_id)}


def has_override(value, *, agent_selection=False):
    if isinstance(value, dict):
        if value.get("type") == "mcp":
            # This sentinel selects the gateway registry, not a caller URL.
            # Native virtual-key MCP/object permissions still authorize access.
            return not registered_mcp_tool(value)
        forbidden = {
            "api_base", "base_url", "api_key", "api_version", "azure_endpoint",
            "azure_ad_token", "azure_ad_token_provider", "custom_llm_provider",
            "litellm_params", "litellm_settings", "router_settings",
            "model_list", "deployment_id", "api_type", "aws_access_key_id",
            "aws_secret_access_key", "aws_session_token", "vertex_credentials",
            "credentials", "extra_headers", "extra_query", "headers", "proxy", "http_client",
            "fallbacks", "context_window_fallbacks", "mock_response", "azure_scope",
            "client_id", "client_secret", "tenant_id", "server_url", "server_label",
            "connector_id", "model_info", "num_retries",
        }
        if agent_selection:
            forbidden |= {"agent_reference", "agent", "agent_id", "agent_name"}
        return bool(forbidden.intersection(value)) or any(
            has_override(item, agent_selection=agent_selection) for item in value.values()
        )
    if isinstance(value, list):
        return any(has_override(item, agent_selection=agent_selection) for item in value)
    return False


class RegisteredBackendPolicy(CustomLogger):
    enforces_request_content = True

    def __init__(self):
        super().__init__()
        install_request_capture()

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # A2A/MCP use fixed registry targets and synthetic hook data, not an
        # inference request body. Their native key/object checks remain active.
        if call_type in {"asend_message", "list_mcp_tools", "call_mcp_tool"}:
            return data
        captured = data.get("secret_fields", {}).get("_scenario_request")
        if not isinstance(captured, tuple) or len(captured) != 2 or captured[0] is not _CAPTURE_OWNER:
            raise HTTPException(status_code=400, detail="Original request body is unavailable")
        body = captured[1]
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid request body")
        agent_response = body.get("model") in {"prompt-agent-responses", "hosted-agent-responses"}
        if has_override(body, agent_selection=agent_response):
            raise HTTPException(status_code=403, detail="Backend overrides are not permitted")
        if agent_response:
            from agent_responses import authenticate

            authorization = data.get("secret_fields", {}).get("_scenario_authorization")
            value = (
                authorization[1] if isinstance(authorization, tuple) and len(authorization) == 2
                and authorization[0] is _CAPTURE_OWNER else None
            )
            return await authenticate(data, body, authorization=value)
        if body.get("model") and body["model"] != os.environ.get("MODEL_ALIAS", "enterprise-chat"):
            raise HTTPException(status_code=403, detail="Only the registered model alias is permitted")
        return data


policy = RegisteredBackendPolicy()


def install_mcp_env_resolution():
    """Bridge v1.110.2 YAML's flat OAuth fields to native MCP credential loading.

    Unlike model parameters, this version's MCP YAML loader does not expand
    os.environ references. Resolve only administrator-owned OAuth fields in a
    copy, leaving the published config and native authentication untouched.
    """
    from litellm.proxy.mcp_server.mcp_server_manager import global_mcp_server_manager

    manager = global_mcp_server_manager
    if getattr(manager, "_scenario_env_resolution", False):
        return
    original = manager.load_servers_from_config

    async def load_with_env(mcp_servers_config, mcp_aliases=None):
        resolved = deepcopy(mcp_servers_config)
        for server in resolved.values():
            for field in ("client_id", "client_secret"):
                value = server.get(field)
                if isinstance(value, str) and value.startswith("os.environ/"):
                    variable = value.removeprefix("os.environ/")
                    if not os.environ.get(variable, "").strip():
                        raise RuntimeError(f"MCP OAuth environment variable {variable} is required")
                    server[field] = os.environ[variable]
        return await original(resolved, mcp_aliases)

    manager.load_servers_from_config = load_with_env
    manager._scenario_env_resolution = True
