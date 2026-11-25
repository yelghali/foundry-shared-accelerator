"""Verify both protocol handlers locally without Azure credentials or model calls."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).parent / "src"))
from main import create_host


async def main():
    async def fixed_response(text):
        return "VERIFIED: " + text

    with patch("main.respond", side_effect=fixed_response):
        host = create_host()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=host), base_url="http://localhost:8088",
        ) as client:
            response = await client.post("/responses", json={"input": "local-check", "model": "demo"})
            response.raise_for_status()
            payload = response.json()
            texts = [
                content["text"] for item in payload["output"]
                for content in item.get("content", []) if content.get("type") == "output_text"
            ]
            assert texts == ["VERIFIED: local-check"], payload
            print("Local Responses handler: PASS")
            response = await client.post(
                "/a2a", headers={"a2a-version": "1.0"},
                json={
                    "jsonrpc": "2.0", "id": "local-a2a", "method": "SendMessage",
                    "params": {"message": {
                        "messageId": "local-message", "role": "ROLE_USER",
                        "parts": [{"text": "a2a-check"}],
                    }},
                },
            )
            response.raise_for_status()
            payload = response.json()
            assert "error" not in payload, payload
            assert "VERIFIED: a2a-check" in str(payload), payload
            print("Local A2A handler: PASS")


if __name__ == "__main__":
    asyncio.run(main())
