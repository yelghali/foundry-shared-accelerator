"""Loopback-only UI bridge; leaves deployed gateway and APIM configuration unchanged."""

from __future__ import annotations

import argparse
import json
import ssl
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response, StreamingResponse

HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "content-length"}


def local_ui_url(url: str, public_base: str, local_base: str, upstream_base: str = "") -> str:
    for base in (public_base.rstrip("/"), upstream_base.rstrip("/")):
        if base and (url == base or url.startswith(base + "/") or url.startswith(base + "?")):
            return local_base.rstrip("/") + url[len(base):]
    return url


def legacy_agent_request(path: str, method: str, body: bytes) -> bool:
    if method != "POST" or not path.startswith("/a2a/") or not path.endswith("/message/send"):
        return False
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(value, dict) and value.get("method") == "message/send"


def ui_agent_response(value: dict) -> dict:
    result = value.get("result")
    if not isinstance(result, dict):
        return value
    task, message = result.get("task"), result.get("message")
    if isinstance(task, dict):
        status = dict(task.get("status", {}))
        if isinstance(status.get("state"), str) and status["state"].startswith("TASK_STATE_"):
            status["state"] = status["state"].removeprefix("TASK_STATE_").lower()
        converted = {**task, "kind": "task", "status": status, "artifacts": [
            {**artifact, "parts": [
                {**part, "kind": "text"} if isinstance(part.get("text"), str) else part
                for part in artifact.get("parts", [])
            ]}
            for artifact in task.get("artifacts", [])
        ]}
    elif isinstance(message, dict):
        converted = {**message, "kind": "message", "parts": [
            {**part, "kind": "text"} if isinstance(part.get("text"), str) else part
            for part in message.get("parts", [])
        ]}
        converted["role"] = {"ROLE_AGENT": "agent", "ROLE_USER": "user"}.get(message.get("role"), message.get("role"))
    else:
        return value
    return {**value, "result": converted}


def create_app(upstream: str, certificate: str, public_base: str, local_base: str) -> FastAPI:
    target, local = urlsplit(upstream), urlsplit(local_base)
    if target.scheme != "https" or target.hostname not in {"localhost", "127.0.0.1"} or target.username or target.password:
        raise ValueError("The UI bridge upstream must be the fixed HTTPS loopback AKS tunnel.")
    if local.scheme != "http" or local.hostname not in {"localhost", "127.0.0.1"} or not local.port:
        raise ValueError("The development UI origin must be an explicit HTTP loopback port.")
    tls = ssl.create_default_context(cafile=certificate)
    # The tunneled certificate authenticates the gateway DNS name, not localhost.
    # Keep certificate-chain validation, relaxing hostname matching only on this loopback hop.
    tls.check_hostname = False

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            base_url=upstream.rstrip("/"), verify=tls, timeout=httpx.Timeout(30, read=240),
            follow_redirects=False, trust_env=False,
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def forward(request: Request, path: str):
        if request.url.hostname not in {"localhost", "127.0.0.1"}:
            raise HTTPException(403, "Loopback Host required")
        origin = request.headers.get("origin")
        if origin and origin not in {local_base.rstrip("/"), f"http://127.0.0.1:{local.port}"}:
            raise HTTPException(403, "Cross-origin admin requests are not allowed")
        headers = {
            key: value for key, value in request.headers.items()
            if key.lower() not in HOP_HEADERS | {"host", "accept-encoding"}
        }
        headers["accept-encoding"] = "identity"
        # A leading slash fixes the destination at the configured tunnel; never
        # interpret an incoming absolute URL as a different upstream host.
        route = "/" + path.lstrip("/")
        body = await request.body()
        legacy_agent = legacy_agent_request(route, request.method, body)
        client = request.app.state.client
        upstream_request = client.build_request(
            request.method, route, params=request.query_params, headers=headers, content=body,
        )
        try:
            response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as error:
            raise HTTPException(502, "The private AKS admin tunnel is unavailable; reconnect it.") from error
        response_headers = {
            key: value for key, value in response.headers.items()
            if key.lower() not in HOP_HEADERS | {"set-cookie"}
        }
        if response_headers.get("location"):
            response_headers["location"] = local_ui_url(response_headers["location"], public_base, local_base, upstream)
        if response.is_success and (
            legacy_agent or route.rstrip("/") in {"/litellm/.well-known/litellm-ui-config", "/v2/login"}
        ):
            try:
                await response.aread()
                data = response.json()
                if legacy_agent:
                    # The pinned UI renders 0.3-shaped results; the deployed
                    # gateway and Foundry remain native A2A 1.0.
                    data = ui_agent_response(data)
                elif route.rstrip("/") == "/v2/login":
                    data["redirect_url"] = local_ui_url(data["redirect_url"], public_base, local_base, upstream)
                else:
                    data["proxy_base_url"] = local_base.rstrip("/")
                    data["gateway_base_url"] = local_base.rstrip("/")
                result: Response = JSONResponse(
                    data, status_code=response.status_code,
                    headers={key: value for key, value in response_headers.items()
                             if key.lower() not in {"content-type", "content-encoding"}},
                )
            finally:
                await response.aclose()
        else:
            result = StreamingResponse(
                response.aiter_raw(), status_code=response.status_code, headers=response_headers,
                background=BackgroundTask(response.aclose),
            )
        for cookie in response.headers.get_list("set-cookie"):
            # HTTP is allowed only on local loopback. Backend TLS remains verified;
            # these development cookies must never be exposed on a remote listener.
            cookie = "; ".join(part for part in cookie.split("; ") if part.lower() != "secure")
            result.headers.append("set-cookie", cookie)
        return result

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tunnel-port", type=int, default=4400)
    parser.add_argument("--port", type=int, default=4401)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--public-base", required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not 1024 <= args.tunnel_port <= 65535 or args.port == args.tunnel_port:
        raise ValueError("Use distinct nonprivileged local UI and tunnel ports.")
    app = create_app(
        f"https://127.0.0.1:{args.tunnel_port}", args.certificate,
        args.public_base, f"http://localhost:{args.port}",
    )
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
