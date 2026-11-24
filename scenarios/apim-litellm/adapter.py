"""Loopback-only renewable identity adapter; never an arbitrary URL proxy."""

from contextlib import asynccontextmanager
import json
import os
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from identity import credential, required

CARD_PATHS = {
    "/.well-known/agent-card.json",
    "/.well-known/agent.json",
    "/a2a/.well-known/agent-card.json",
    "/a2a/.well-known/agent.json",
    "/a2a/hosted/.well-known/agent-card.json",
    "/a2a/hosted/.well-known/agent.json",
    "/a2a/declarative/.well-known/agent-card.json",
    "/a2a/declarative/.well-known/agent.json",
}

def target_for(path):
    if path in {"/a2a", "/a2a/hosted", "/a2a/declarative"} or path in CARD_PATHS:
        is_card = path in CARD_PATHS
        prefix = "FOUNDRY_DECLARATIVE_A2A" if path.startswith("/a2a/declarative") else "FOUNDRY_A2A"
        url = required(f"{prefix}_CARD_URL" if is_card else f"{prefix}_URL")
        if urlsplit(url).scheme != "https":
            raise ValueError("Foundry endpoints must use HTTPS")
        return url, "https://ai.azure.com/.default"
    if path == "/mcp":
        url = required("FASTMCP_URL")
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
            raise ValueError("Invalid FastMCP endpoint")
        return url, required("MCP_TOKEN_SCOPE")
    raise HTTPException(404)


@asynccontextmanager
async def lifespan(app):
    app.state.credential = credential()
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(120, read=None), follow_redirects=False, trust_env=False
    )
    mode = os.environ.get("BACKEND_AUTH_MODE", "service")
    if mode not in {"service", "user-obo"}:
        raise ValueError("BACKEND_AUTH_MODE must be service or user-obo")
    app.state.obo = None
    if mode == "user-obo":
        from obo import OnBehalfOf
        app.state.obo = OnBehalfOf(app.state.client)
    yield
    await app.state.client.aclose()
    await app.state.credential.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
async def forward(request: Request, path: str):
    del path
    url, scope = target_for(request.url.path)
    if request.url.path != "/mcp" and request.method == "DELETE":
        raise HTTPException(405)
    if request.url.path in CARD_PATHS and request.method != "GET":
        raise HTTPException(405)
    if os.environ.get("BACKEND_AUTH_MODE", "service") == "user-obo":
        if request.url.path == "/mcp":
            # Profile C MCP uses LiteLLM's native Entra OBO, never this adapter.
            raise HTTPException(404)
        token_value = await request.app.state.obo.get_token(
            request.headers.get("authorization"), scope
        )
    else:
        token = await request.app.state.credential.get_token(scope)
        token_value = token.token
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in {
            "accept", "content-type", "mcp-session-id", "mcp-protocol-version",
            "last-event-id", "a2a-version", "traceparent", "tracestate",
        }
    }
    headers["Authorization"] = f"Bearer {token_value}"
    if request.url.path != "/mcp":
        # LiteLLM task forwarding omits this; Foundry otherwise selects legacy translation.
        headers["a2a-version"] = "1.0"
    body = await request.body()
    if request.url.path in {"/a2a", "/a2a/hosted", "/a2a/declarative"} and request.method == "POST":
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "Invalid A2A JSON-RPC payload") from exc
        # LiteLLM normalizes GetTask to its legacy wire name before raw forwarding.
        if isinstance(payload, dict) and payload.get("method") == "tasks/get":
            payload["method"] = "GetTask"
            body = json.dumps(payload).encode("utf-8")
    client = request.app.state.client
    upstream = client.build_request(
        request.method, url, headers=headers,
        params=request.query_params, content=body,
    )
    try:
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Identity upstream unavailable") from exc
    response_headers = {
        key: value for key, value in response.headers.items()
        if key.lower() in {
            "content-type", "content-encoding", "mcp-session-id", "mcp-protocol-version",
            "cache-control", "retry-after", "www-authenticate",
        }
    }
    # LiteLLM must discover the loopback route, never bypass token refresh by
    # following the public URL advertised by the upstream card.
    if request.url.path in CARD_PATHS and response.is_success:
        try:
            await response.aread()
            card = response.json()
            route = "/a2a/declarative" if request.url.path.startswith("/a2a/declarative") else "/a2a"
            card["url"] = f"http://127.0.0.1:4001{route}"
            for field in ("additionalInterfaces", "supportedInterfaces"):
                for interface in card.get(field, []):
                    interface["url"] = card["url"]
            from fastapi.responses import JSONResponse
            return JSONResponse(card)
        finally:
            await response.aclose()
    if response.status_code in {204, 304}:
        await response.aclose()
        return Response(status_code=response.status_code, headers=response_headers)
    return StreamingResponse(
        response.aiter_raw(), status_code=response.status_code,
        headers=response_headers, background=BackgroundTask(response.aclose),
    )
