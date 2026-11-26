"""Read-only, container-scoped Blob tools using a separate workload identity."""

import os
import logging
from contextlib import asynccontextmanager

from azure.storage.blob.aio import BlobServiceClient
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from jwt import PyJWTError

from identity import credential, delegated_verifier, required, verifier

logger = logging.getLogger(__name__)


def auth_mode():
    mode = os.environ.get("MCP_AUTH_MODE", "service")
    if mode not in {"service", "delegated", "user-obo"}:
        raise ValueError("MCP_AUTH_MODE must be service or user-obo")
    return "service" if mode == "service" else "user-obo"


def token_verifier():
    return verifier("MCP") if auth_mode() == "service" else delegated_verifier("MCP")


class GatewayTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str):
        try:
            claims = await token_verifier().verify_async(token)
            if claims is None:
                logger.warning("MCP identity rejected (no verified claims)")
                return None
            return AccessToken(
                token=token, client_id=claims.get("azp") or claims.get("appid") or claims["oid"],
                scopes=claims.get("scp", "").split(), expires_at=claims["exp"],
                claims=claims,
            )
        except (PyJWTError, ValueError) as exc:
            logger.warning("MCP identity rejected (%s)", type(exc).__name__)
            return None


@asynccontextmanager
async def lifespan(server):
    del server
    async with credential() as identity:
        async with BlobServiceClient(required("BLOB_ACCOUNT_URL"), credential=identity) as client:
            global container
            container = client.get_container_client(required("BLOB_CONTAINER"))
            yield


container = None
mcp = FastMCP("Dedicated Blob reader", auth=GatewayTokenVerifier(), lifespan=lifespan)


@mcp.tool
async def whoami() -> dict:
    """Return only verified caller identity; Blob access still uses server identity."""
    access = get_access_token()
    if access is None or not access.claims:
        raise ValueError("Verified identity is required")
    claims = access.claims
    return {
        "tid": claims["tid"], "oid": claims["oid"],
        "appid": claims.get("azp") or claims.get("appid"),
        "scp": claims.get("scp", ""),
        "authentication_mode": auth_mode(),
    }


@mcp.tool
async def list_blobs(prefix: str = "", limit: int = 20) -> list[str]:
    """List at most 100 blob names in this server's dedicated container."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    result = []
    async for blob in container.list_blobs(name_starts_with=prefix):
        result.append(blob.name)
        if len(result) >= limit:
            break
    return result


@mcp.tool
async def read_blob(name: str) -> str:
    """Read one UTF-8 blob, limited to 1 MiB, from the dedicated container."""
    if not name or len(name) > 1024:
        raise ValueError("A valid blob name is required")
    blob = container.get_blob_client(name)
    properties = await blob.get_blob_properties()
    if properties.size > 1048576:
        raise ValueError("Blob exceeds the 1 MiB sample limit")
    data = await (await blob.download_blob(offset=0, length=1048577)).readall()
    if len(data) > 1048576:
        raise ValueError("Blob exceeds the 1 MiB sample limit")
    return data.decode("utf-8")


if __name__ == "__main__":
    token_verifier()
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), stateless_http=True)
