import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from accelerator.demo_hosted import main


ARGS = [
    "demo_hosted", "--project-endpoint", "https://demo.services.ai.azure.com/api/projects/demo",
    "--tenant-id", "00000000-0000-0000-0000-000000000001",
    "--image", "private.azurecr.io/hosted-dual:tested",
    "--model-endpoint", "https://demo.openai.azure.com/",
    "--output", ".local/hosted-plan.json",
]


def test_hosted_image_plan_never_authenticates_deploys_or_writes_a_receipt():
    with patch("sys.argv", ARGS), patch("accelerator.demo_hosted.deploy") as deploy, \
         patch("accelerator.demo_hosted.save_receipt") as save, redirect_stdout(io.StringIO()) as output:
        main()
    deploy.assert_not_called()
    save.assert_not_called()
    assert json.loads(output.getvalue())["writes_performed"] is False


def test_hosted_image_failure_never_publishes_a_success_receipt():
    with patch("sys.argv", ARGS + ["--apply"]), \
         patch("accelerator.demo_hosted.deploy", side_effect=RuntimeError("Hosted provisioning failed")), \
         patch("accelerator.demo_hosted.save_receipt") as save:
        with pytest.raises(RuntimeError, match="Hosted provisioning failed"):
            main()
    save.assert_not_called()


def test_hosted_plan_rejects_non_foundry_endpoint_before_deploy():
    bad = [value.replace("https://demo.services.ai.azure.com/api/projects/demo", "https://another.example/project")
           for value in ARGS]
    with patch("sys.argv", bad), patch("accelerator.demo_hosted.deploy") as deploy:
        with pytest.raises(ValueError, match="Foundry project HTTPS"):
            main()
    deploy.assert_not_called()
