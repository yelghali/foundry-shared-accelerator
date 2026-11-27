"""Tenant-pinned Entra verification for FastMCP and custom agent OBO."""

import asyncio
import os
from functools import lru_cache

import jwt


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} must be configured")
    return value


def values(name):
    return {item.strip() for item in os.environ.get(name, "").split(",") if item.strip()}


class EntraVerifier:
    def __init__(self, audience, clients, principals, *, delegated_scope=None, tenant=None):
        self.tenant = tenant or required("TENANT_ID")
        self.audience = audience
        self.clients = clients
        self.principals = principals
        self.delegated_scope = delegated_scope
        if not audience or not (clients or principals):
            raise ValueError("An audience and explicit caller allowlist are required")
        if delegated_scope and not (clients and principals):
            raise ValueError("Delegated access requires client and user allowlists")
        self.jwks = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{self.tenant}/discovery/v2.0/keys",
            cache_jwk_set=True,
            lifespan=3600,
            timeout=10,
        )

    def verify(self, token):
        key = self.jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=self.audience,
            options={"require": ["exp", "nbf", "iat", "iss", "aud", "tid", "oid"]},
        )
        issuers = {
            f"https://sts.windows.net/{self.tenant}/",
            f"https://login.microsoftonline.com/{self.tenant}/v2.0",
        }
        if claims["iss"] not in issuers or claims["tid"] != self.tenant:
            raise ValueError("Wrong tenant or issuer")
        client = claims.get("azp") or claims.get("appid")
        if self.clients and client not in self.clients:
            raise ValueError("Client is not allowed")
        if self.principals and claims["oid"] not in self.principals:
            raise ValueError("Principal is not allowed")
        # A delegated user token is not an APIM/workload application identity.
        if self.delegated_scope:
            if claims.get("idtyp") == "app" or self.delegated_scope not in str(claims.get("scp", "")).split():
                raise ValueError("Required delegated scope is missing")
        elif claims.get("scp"):
            raise ValueError("Delegated tokens are not accepted")
        return claims

    async def verify_async(self, token):
        return await asyncio.to_thread(self.verify, token)


@lru_cache
def verifier(prefix):
    return EntraVerifier(
        required(f"{prefix}_TOKEN_AUDIENCE"),
        values(f"{prefix}_ALLOWED_CLIENT_IDS"),
        values(f"{prefix}_ALLOWED_PRINCIPAL_IDS"),
    )


@lru_cache
def delegated_verifier(prefix):
    scope = os.environ.get(
        f"{prefix}_REQUIRED_SCOPE", "tools.read" if prefix == "MCP" else "access_as_user"
    ).strip()
    if not scope or len(scope.split()) != 1:
        raise ValueError("Exactly one delegated scope must be configured")
    audience = (
        os.environ.get("OBO_INPUT_AUDIENCE", "").strip()
        if prefix == "OBO" else ""
    ) or required(f"{prefix}_TOKEN_AUDIENCE")
    return EntraVerifier(
        audience,
        values(f"{prefix}_ALLOWED_CLIENT_IDS"),
        values(f"{prefix}_ALLOWED_PRINCIPAL_IDS"),
        delegated_scope=scope,
        tenant=os.environ.get("OBO_TENANT_ID") if prefix == "OBO" else None,
    )


def credential():
    from azure.identity.aio import ClientSecretCredential, WorkloadIdentityCredential

    mode = os.environ.get("IDENTITY_MODE", "workload")
    if mode == "service-principal":
        return ClientSecretCredential(
            required("AZURE_TENANT_ID"), required("AZURE_CLIENT_ID"), required("AZURE_CLIENT_SECRET")
        )
    if mode != "workload":
        raise ValueError("IDENTITY_MODE must be workload or service-principal")
    return WorkloadIdentityCredential()
