"""In-process Foundry compatibility for the pinned LiteLLM A2A gateway.

Workload authentication is native LiteLLM OIDC. User OBO remains an explicit
custom integration. Both paths call Foundry directly, without a sidecar.
"""

import os
from functools import wraps

import httpx


def install():
    from litellm.proxy.agent_endpoints import a2a_endpoints

    original = a2a_endpoints._forward_jsonrpc
    if getattr(original, "_foundry_direct", False):
        return
    targets = frozenset(
        os.environ[name].rstrip("/")
        for name in ("FOUNDRY_A2A_URL", "FOUNDRY_DECLARATIVE_A2A_URL", "FOUNDRY_HOSTED_A2A_URL")
        if os.environ.get(name)
    )

    @wraps(original)
    async def forward(agent_url, body, extra_headers=None):
        # v1.110.2 normalizes incoming GetTask to the legacy wire method.
        # Correct only configured Foundry targets; all other agents are untouched.
        if agent_url.rstrip("/") in targets and body.get("method") == "tasks/get":
            body = {**body, "method": "GetTask"}
            extra_headers = {
                **{k: v for k, v in (extra_headers or {}).items() if k.lower() != "a2a-version"},
                "a2a-version": "1.0",
            }
        return await original(agent_url, body, extra_headers=extra_headers)

    forward._foundry_direct = True
    a2a_endpoints._forward_jsonrpc = forward
    if os.environ.get("AUTHENTICATION_SCENARIO") != "user-obo":
        return

    from fastapi import HTTPException
    from litellm.proxy.agent_endpoints.databricks_oauth import AgentBackendAuth
    from obo import OnBehalfOf

    exchange = OnBehalfOf(None)
    resolve = a2a_endpoints._resolve_backend_auth

    @wraps(resolve)
    async def resolve_auth(litellm_params, custom_llm_provider, agent_extra_headers=None):
        if not litellm_params.get("foundry_user_obo"):
            return await resolve(litellm_params, custom_llm_provider, agent_extra_headers)
        if custom_llm_provider:
            raise HTTPException(500, "Foundry OBO requires a plain A2A agent")
        authorization = next(
            (value for name, value in (agent_extra_headers or {}).items() if name.lower() == "authorization"),
            None,
        )
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            token = await exchange.get_token(
                authorization, "https://ai.azure.com/.default", client=client,
            )
        # Native discovery and invocation both receive the exchanged bearer.
        # Nothing is persisted to the registry and there is no workload fallback.
        return AgentBackendAuth(
            litellm_params={**litellm_params, "azure_ad_token": token},
            header={"Authorization": f"Bearer {token}"},
        )

    a2a_endpoints._resolve_backend_auth = resolve_auth
