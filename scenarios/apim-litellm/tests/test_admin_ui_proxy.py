import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from accelerator.admin_ui_proxy import create_app, legacy_agent_request, ui_agent_response
from accelerator.apim_litellm import create_development_certificate


class AdminUiBridgeTests(unittest.TestCase):
    def test_pinned_agent_ui_can_render_native_tasks_without_changing_consumer_protocol(self):
        body = {"jsonrpc": "2.0", "id": "ui-1", "result": {"task": {
            "id": "task-1", "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "artifact-1", "parts": [{"text": "Echo: hello"}]}],
        }}}
        converted = ui_agent_response(body)
        self.assertEqual(converted["id"], "ui-1")
        self.assertEqual(converted["result"]["kind"], "task")
        self.assertEqual(converted["result"]["status"]["state"], "completed")
        self.assertEqual(converted["result"]["artifacts"][0]["parts"][0], {"kind": "text", "text": "Echo: hello"})
        self.assertIn("task", body["result"])
        self.assertNotIn("kind", body["result"]["task"])
        self.assertTrue(legacy_agent_request("/a2a/echo/message/send", "POST", b'{"method":"message/send"}'))
        self.assertFalse(legacy_agent_request("/a2a/echo", "POST", b'{"method":"SendMessage"}'))
        self.assertFalse(legacy_agent_request("/a2a/echo/message/send", "POST", b'{"method":"SendMessage"}'))
        error = {"jsonrpc": "2.0", "id": "ui-1", "error": {"code": -32001, "message": "Unauthorized"}}
        self.assertEqual(ui_agent_response(error), error)

    def test_ui_discovery_and_login_stay_local_without_changing_the_gateway(self):
        async def run(certificate):
            async def upstream(request):
                self.assertEqual(request.url.host, "127.0.0.1")
                if request.url.path.endswith("litellm-ui-config"):
                    return httpx.Response(200, json={
                        "proxy_base_url": "https://api.example/ai",
                        "gateway_base_url": "https://api.example/ai",
                        "server_root_path": "",
                    })
                return httpx.Response(200, json={
                    "token": "test-ui-session", "redirect_url": "https://api.example/ai/ui?login=success",
                }, headers={"set-cookie": "token=test-ui-session; Secure; SameSite=lax"})

            app = create_app("https://127.0.0.1:4400", certificate, "https://api.example/ai", "http://localhost:4401")
            async with httpx.AsyncClient(base_url="https://127.0.0.1:4400", transport=httpx.MockTransport(upstream)) as backend:
                app.state.client = backend
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:4401") as client:
                    discovery = await client.get("/litellm/.well-known/litellm-ui-config")
                    self.assertEqual(discovery.json()["proxy_base_url"], "http://localhost:4401")
                    self.assertEqual(discovery.json()["gateway_base_url"], "http://localhost:4401")
                    login = await client.post("/v2/login", json={"username": "admin", "password": "test"})
                    self.assertEqual(login.json()["redirect_url"], "http://localhost:4401/ui?login=success")
                    self.assertEqual(login.json()["token"], "test-ui-session")
                    self.assertNotIn("secure", login.headers["set-cookie"].lower())

        with tempfile.TemporaryDirectory() as directory:
            create_development_certificate("gateway.example", Path(directory))
            asyncio.run(run(str(Path(directory) / "tls.crt")))

    def test_remote_origin_and_rebound_host_cannot_reach_the_admin_backend(self):
        async def run(certificate):
            async def forbidden(request):
                self.fail("An untrusted browser origin reached the private admin gateway")

            app = create_app("https://127.0.0.1:4400", certificate, "https://api.example/ai", "http://localhost:4401")
            async with httpx.AsyncClient(base_url="https://127.0.0.1:4400", transport=httpx.MockTransport(forbidden)) as backend:
                app.state.client = backend
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:4401") as client:
                    response = await client.post("/key/generate", headers={"origin": "https://untrusted.example"}, json={})
                    self.assertEqual(response.status_code, 403)
                    response = await client.get("/ui/", headers={"host": "untrusted.example"})
                    self.assertEqual(response.status_code, 403)

        with tempfile.TemporaryDirectory() as directory:
            create_development_certificate("gateway.example", Path(directory))
            asyncio.run(run(str(Path(directory) / "tls.crt")))

    def test_inference_headers_and_streaming_are_forwarded_without_origin_rewrite(self):
        async def run(certificate):
            async def upstream(request):
                self.assertEqual(request.headers["x-litellm-api-key"], "Bearer test-consumer-key")
                self.assertEqual(json.loads(request.content)["model"], "enterprise-chat")
                class EventStream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b"data: [DONE]\n\n"
                return httpx.Response(200, stream=EventStream(), headers={"content-type": "text/event-stream"})

            app = create_app("https://127.0.0.1:4400", certificate, "https://api.example/ai", "http://localhost:4401")
            async with httpx.AsyncClient(base_url="https://127.0.0.1:4400", transport=httpx.MockTransport(upstream)) as backend:
                app.state.client = backend
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:4401") as client:
                    response = await client.post(
                        "/v1/chat/completions", headers={"x-litellm-api-key": "Bearer test-consumer-key"},
                        json={"model": "enterprise-chat"},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.text, "data: [DONE]\n\n")

        with tempfile.TemporaryDirectory() as directory:
            create_development_certificate("gateway.example", Path(directory))
            asyncio.run(run(str(Path(directory) / "tls.crt")))
