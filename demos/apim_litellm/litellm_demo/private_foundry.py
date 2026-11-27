"""Operator-only SDK transport through a connected AKS pod to one private Foundry host."""

import json
import io
import subprocess
from urllib.parse import urlsplit

import requests
from azure.core.pipeline.transport import RequestsTransport
from requests.adapters import BaseAdapter
from urllib3.response import HTTPResponse


_REQUEST = r"""
import json,sys,httpx
p=json.load(sys.stdin)
from urllib.parse import urlsplit
u=urlsplit(p["url"])
if u.scheme!="https" or u.hostname!=p["host"] or u.username or u.password:
    raise ValueError("Only the configured private Foundry HTTPS host is allowed")
with httpx.Client(timeout=240,trust_env=False,follow_redirects=False) as client:
    response=client.request(p["method"],p["url"],headers=p["headers"],content=p["body"])
    print(json.dumps({"status":response.status_code,"headers":dict(response.headers),"body":response.text}))
"""


class PrivateFoundryAdapter(BaseAdapter):
    def __init__(self, endpoint: str, namespace: str):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname
                or not parsed.hostname.endswith(".services.ai.azure.com")
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Provide the fixed Foundry project HTTPS endpoint.")
        if namespace not in {"gateway", "gateway-app-alpha", "gateway-user-obo"}:
            raise ValueError("Choose an existing demo gateway namespace.")
        self.host = parsed.hostname
        self.namespace = namespace

    def send(self, request, **kwargs):
        parsed = urlsplit(request.url)
        if parsed.scheme != "https" or parsed.hostname != self.host or parsed.username or parsed.password:
            raise ValueError("Request is outside the configured private Foundry host.")
        body = request.body
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        if body is not None and not isinstance(body, str):
            raise ValueError("Private Foundry transport supports JSON request bodies only.")
        process = subprocess.run(
            ["kubectl", "-n", self.namespace, "exec", "-i", "deployment/fastmcp", "--", "python", "-c", _REQUEST],
            input=json.dumps({
                "host": self.host, "method": request.method, "url": request.url,
                "headers": dict(request.headers), "body": body,
            }), text=True, capture_output=True, timeout=300, check=False,
        )
        if process.returncode:
            raise RuntimeError("Private Foundry network hop failed; credentials and pod output suppressed.")
        try:
            value = json.loads(process.stdout)
            response = requests.Response()
            response.status_code = value["status"]
            response.headers.update({
                key: item for key, item in value["headers"].items()
                if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
            })
            response._content = value["body"].encode("utf-8")
            response.raw = HTTPResponse(
                body=io.BytesIO(response._content), status=response.status_code,
                headers=dict(response.headers), preload_content=False,
            )
            response.request = request
            response.url = request.url
            return response
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("Private Foundry network hop returned an invalid response; output suppressed.") from None

    def close(self):
        pass


def sdk_transport(endpoint: str, namespace: str) -> RequestsTransport:
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", PrivateFoundryAdapter(endpoint, namespace))
    return RequestsTransport(session=session, session_owner=True)
