import json
from unittest.mock import Mock, patch

import pytest
import requests
from azure.core.pipeline.transport import RequestsTransport

from litellm_demo.private_foundry import PrivateFoundryAdapter


def test_private_operator_transport_fixes_host_and_keeps_tokens_in_stdin():
    adapter = PrivateFoundryAdapter("https://demo.services.ai.azure.com/api/projects/demo", "gateway")
    request = requests.Request(
        "POST", "https://demo.services.ai.azure.com/api/projects/demo/agents/test/versions",
        headers={"Authorization": "Bearer private-operator"}, json={"definition": {"kind": "prompt"}},
    ).prepare()
    with patch("litellm_demo.private_foundry.subprocess.run") as run:
        run.return_value = Mock(returncode=0, stdout=json.dumps({
            "status": 200, "headers": {"content-type": "application/json", "content-encoding": "gzip"},
            "body": '{"version":"1"}',
        }))
        response = adapter.send(request)
    assert response.json() == {"version": "1"}
    assert "content-encoding" not in response.headers
    response.raw.enforce_content_length = True
    assert "private-operator" not in repr(run.call_args.args[0])
    assert json.loads(run.call_args.kwargs["input"])["headers"]["Authorization"] == "Bearer private-operator"
    other = requests.Request("GET", "https://other.example/agents").prepare()
    with pytest.raises(ValueError, match="outside"):
        adapter.send(other)


def test_private_operator_transport_supports_azure_sdk_response_contract():
    session = requests.Session()
    session.mount("https://", PrivateFoundryAdapter("https://demo.services.ai.azure.com/api/projects/demo", "gateway"))
    transport = RequestsTransport(session=session)
    from azure.core.pipeline.transport import HttpRequest
    with patch("litellm_demo.private_foundry.subprocess.run") as run:
        run.return_value = Mock(returncode=0, stdout=json.dumps({
            "status": 200, "headers": {"content-type": "application/json"}, "body": '{"name":"agent"}',
        }))
        response = transport.send(HttpRequest("GET", "https://demo.services.ai.azure.com/api/projects/demo/agents"))
    assert response.status_code == 200
    assert json.loads(response.body()) == {"name": "agent"}


def test_private_operator_transport_failure_is_redacted():
    adapter = PrivateFoundryAdapter("https://demo.services.ai.azure.com/api/projects/demo", "gateway")
    with patch("litellm_demo.private_foundry.subprocess.run", return_value=Mock(
        returncode=1, stderr="private-token", stdout="private-secret",
    )):
        with pytest.raises(RuntimeError, match="output suppressed") as error:
            adapter.send(requests.Request("GET", "https://demo.services.ai.azure.com/agents").prepare())
    assert "private-token" not in str(error.value)
