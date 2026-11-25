"""Direct Responses authentication for fixed administrator-registered Foundry agents."""

import os
from functools import wraps
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import HTTPException

from identity import credential, required

_exchange = None


def install():
    """Keep the required Foundry endpoint version when v1.110.2 drops extra_query."""
    from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig

    original = OpenAIResponsesAPIConfig.get_complete_url
    if getattr(original, "_foundry_responses_version", False):
        return
    targets = frozenset(
        os.environ[name].rstrip("/")
        for name in ("FOUNDRY_PROMPT_RESPONSES_BASE", "FOUNDRY_HOSTED_RESPONSES_BASE")
        if os.environ.get(name)
    )

    @wraps(original)
    def endpoint(self, api_base, litellm_params):
        url = original(self, api_base, litellm_params)
        if api_base and api_base.rstrip("/") in targets:
            parsed = urlsplit(url)
            query = dict(parse_qsl(parsed.query))
            query["api-version"] = "2025-11-15-preview"
            return urlunsplit(parsed._replace(query=urlencode(query)))
        return url

    endpoint._foundry_responses_version = True
    OpenAIResponsesAPIConfig.get_complete_url = endpoint


async def authenticate(data, body, *, authorization=None):
    global _exchange
    aliases = {
        "prompt-agent-responses": "FOUNDRY_PROMPT_RESPONSES_BASE",
        "hosted-agent-responses": "FOUNDRY_HOSTED_RESPONSES_BASE",
    }
    variable = aliases.get(body.get("model"))
    if variable is None or not os.environ.get(variable):
        raise HTTPException(403, "The agent Responses alias is not configured")
    if os.environ.get("AUTHENTICATION_SCENARIO") == "user-obo":
        from obo import OnBehalfOf

        if _exchange is None:
            _exchange = OnBehalfOf(None)
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            token = await _exchange.get_token(authorization, "https://ai.azure.com/.default", client=client)
    else:
        async with credential() as identity:
            token = (await identity.get_token("https://ai.azure.com/.default")).token
    # These are deployment-owned destinations. Incoming overrides are denied
    # before this hook, and ordinary native-key model permissions still apply.
    data["api_key"] = token
    data["api_base"] = required(variable)
    return data
