"""Explicit custom Entra OBO integration for Foundry A2A agents (not native MCP)."""

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import HTTPException
from jwt import PyJWTError

from identity import delegated_verifier, required

logger = logging.getLogger(__name__)


class OnBehalfOf:
    def __init__(self, client):
        self.client = client
        self.tenant = required("OBO_TENANT_ID")
        self.client_id = required("OBO_CLIENT_ID")
        # Validate configuration at startup, rather than silently using workload identity.
        self.verifier = delegated_verifier("OBO")
        self.cache = {}
        self.lock = asyncio.Lock()

    def secret(self):
        value = os.environ.get("OBO_CLIENT_SECRET", "").strip()
        if not value:
            value = Path(os.environ.get(
                "OBO_CLIENT_SECRET_FILE", "/mnt/secrets-store/obo-client-secret"
            )).read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError("OBO client secret is empty")
        return value

    async def get_token(self, authorization, scope, *, client=None):
        scheme, _, assertion = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not assertion.strip():
            raise HTTPException(401, "A delegated user bearer is required")
        assertion = assertion.strip()
        try:
            await self.verifier.verify_async(assertion)
        except (PyJWTError, ValueError):
            logger.warning("OBO subject rejected")
            raise HTTPException(401, "Invalid delegated user bearer") from None
        key = (hashlib.sha256(assertion.encode()).digest(), scope)
        async with self.lock:
            now = time.time()
            self.cache = {k: v for k, v in self.cache.items() if v[1] > now + 60}
            cached = self.cache.get(key)
            if cached:
                return cached[0]
            try:
                response = await (client or self.client).post(
                    f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
                    data={
                        "client_id": self.client_id, "client_secret": self.secret(),
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "requested_token_use": "on_behalf_of",
                        "assertion": assertion, "scope": scope,
                    },
                )
                if response.status_code != 200:
                    raise ValueError("Exchange rejected")
                payload = response.json()
                token = payload["access_token"]
                ttl = int(payload["expires_in"])
                if not isinstance(token, str) or not token or ttl <= 60:
                    raise ValueError("Invalid token response")
            except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError):
                logger.warning("OBO exchange failed; no workload fallback")
                raise HTTPException(502, "Delegated token exchange failed") from None
            if len(self.cache) >= 1024:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (token, time.time() + ttl)
            return token
