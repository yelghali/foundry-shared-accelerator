"""Measure native gateway inference and inspect exported metrics without inventing attribution."""

from __future__ import annotations

import argparse
import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .control import GATEWAY_SCOPE, ControlPlane, credential, load_json, save_receipt


def model_probes(
    control: ControlPlane, deployment: dict[str, Any], project: dict[str, Any], count: int, *,
    header_project_key: str | None = None,
) -> dict[str, Any]:
    token = control.credential.get_token(GATEWAY_SCOPE).token
    encoded = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    if claims.get("oid") != project["sample_caller"]["principal_id"]:
        raise ValueError("Project probes must run under the project's mapped managed identity.")
    calls: list[dict[str, Any]] = []
    for index in range(count):
        correlation = str(uuid.uuid4())
        for attempt in range(5):
            response = requests.post(
                deployment["gateway"]["endpoint"] + "/default/models/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + token, "x-correlation-id": correlation,
                         "X-Chargeback-Project-Key": header_project_key or project["chargeback_project_key"]},
                json={"model": "approved-chat", "messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 4},
                timeout=120,
            )
            if response.status_code != 429 or attempt == 4:
                break
            retry = response.headers.get("Retry-After", "15")
            delay = min(int(retry), 60) if retry.isdigit() else 15
            print(f"Gateway throttled a probe; waiting {delay}s before retry.", flush=True)
            time.sleep(delay)
        response.raise_for_status()
        body = response.json()
        usage = body.get("usage")
        if not isinstance(usage, dict) or not isinstance(usage.get("total_tokens"), int):
            raise RuntimeError("Successful gateway probe omitted actual token counts.")
        calls.append({
            "index": index, "correlation_id": correlation, "response_id": body.get("id"),
            "usage": usage, "observed_region_header": response.headers.get("x-ms-region"),
        })
    return {
        "project_id": project["id"],
        "chargeback_project_key": project["chargeback_project_key"],
        "caller_principal_id": claims["oid"],
        "tenant_id": claims["tid"],
        "sent_project_header": header_project_key or project["chargeback_project_key"],
        "calls": calls,
        "measured_input_tokens": sum(item["usage"]["prompt_tokens"] for item in calls),
        "measured_output_tokens": sum(item["usage"]["completion_tokens"] for item in calls),
        "measured_total_tokens": sum(item["usage"]["total_tokens"] for item in calls),
        "source": "controlled Entra gateway response usage; corroborate with native identity metrics",
    }


def backend_metrics(control: ControlPlane, deployment: dict[str, Any]) -> dict[str, Any]:
    start = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result: dict[str, Any] = {}
    for region, account in deployment["models"].items():
        uri = account["id"] + (
            "/providers/Microsoft.Insights/metrics"
            f"?metricnames=TokenTransaction,ProcessedPromptTokens,GeneratedTokens&timespan={start}/{end}"
            "&interval=PT1M&aggregation=Total"
        )
        body = control.request("GET", uri, version="2023-10-01")
        result[region] = {
            "resource_id": account["id"],
            "location": account["location"],
            "counters": {
                item["name"]["value"]: sum(
                    point.get("total", 0) or 0
                    for series in item.get("timeseries", [])
                    for point in series.get("data", [])
                )
                for item in body.get("value", [])
            },
        }
    return result


def exported_metrics(control: ControlPlane, deployment: dict[str, Any]) -> dict[str, Any]:
    response = requests.get(
        deployment["otlp"]["query_endpoint"].rstrip("/") + "/api/v1/query",
        headers={"Authorization": "Bearer " + control.credential.get_token("https://prometheus.monitor.azure.com/.default").token},
        params={"query": 'last_over_time({__name__="azure.ai_gateway.client.token.usage"}[4h])'},
        timeout=120,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Native metric query failed: {body.get('error', 'unknown')}")
    rows: list[dict[str, Any]] = []
    for item in body.get("data", {}).get("result", []):
        metric = item["metric"]
        rows.append({
            "name": metric["__name__"],
            "timestamp": item["value"][0],
            "value": float(item["value"][1]),
            "dimensions": {name: control.redact(str(value)) for name, value in metric.items() if name != "__name__"},
        })
    return {
        "metrics_workspace_id": deployment["otlp"]["metrics_workspace_id"],
        "measurement_kind": "native Prometheus cumulative snapshot, not a billing-period delta",
        "lookback_hours": 4,
        "timestamp_semantics": "query evaluation time; selected values are the latest observations within the lookback",
        "rows": rows,
    }


def project_showback(
    native: dict[str, Any], deployment: dict[str, Any], projects: list[dict[str, Any]],
) -> dict[str, Any]:
    owners: dict[str, dict[str, Any]] = {}
    totals: dict[str, dict[str, Any]] = {}
    for project in projects:
        totals[project["id"]] = {
            "project_id": project["id"],
            "project_name": project["name"],
            "chargeback_project_key": project["chargeback_project_key"],
            "input_tokens": None, "output_tokens": None, "total_tokens": None,
            "observed_principals": [],
        }
        for principal in (project["principal_id"], project.get("sample_caller", {}).get("principal_id")):
            if principal:
                if principal.lower() in owners and owners[principal.lower()]["id"] != project["id"]:
                    raise ValueError("A metering principal cannot belong to two projects.")
                owners[principal.lower()] = project
    unassigned: list[dict[str, Any]] = []
    types = {"prompt_tokens": "input_tokens", "completion_tokens": "output_tokens", "total_tokens": "total_tokens"}
    for row in native["rows"]:
        labels = row["dimensions"]
        token_type = labels.get("gen_ai.token.type")
        if token_type not in types:
            continue
        principal = labels.get("azure.ai_gateway.identity.claims.oid", "").lower()
        tenant = labels.get("azure.ai_gateway.identity.claims.tid", "").lower()
        owner = owners.get(principal)
        if (
            owner is None or tenant != deployment["tenant_id"].lower()
            or labels.get("azure.ai_gateway.api_key_id") != "unknown"
            or labels.get("gen_ai.request.model") != "approved-chat"
        ):
            unassigned.append(row)
            continue
        target = totals[owner["id"]]
        category = types[token_type]
        target[category] = row["value"] if target[category] is None else target[category] + row["value"]
        if principal not in target["observed_principals"]:
            target["observed_principals"].append(principal)
    for target in totals.values():
        target["data_status"] = "unavailable" if not target["observed_principals"] else "partial"
        if any(target[key] is None for key in ("input_tokens", "output_tokens", "total_tokens")):
            continue
        if target["total_tokens"] != target["input_tokens"] + target["output_tokens"]:
            raise RuntimeError("Native token categories do not reconcile; refusing a misleading showback total.")
        target["data_status"] = "complete"
    return {
        "projects": list(totals.values()),
        "unassigned_core_series": unassigned,
        "quality": "cumulative native snapshot for the verified Entra-only test population",
        "trust_boundary": "JWT claim enrichment alone is not financial proof for mixed API-key/Bearer traffic.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--projects", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--probe-count", type=int, default=8)
    parser.add_argument("--probe-evidence")
    parser.add_argument("--wait-seconds", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.probe_count <= 100:
        parser.error("--probe-count must be from 1 through 100.")
    control = ControlPlane(credential())
    deployment = load_json(args.deployment)
    projects = [load_json(path) for path in args.projects]
    probes = load_json(args.probe_evidence)["projects"] if args.probe_evidence else []
    deadline = time.monotonic() + args.wait_seconds
    while True:
        native = exported_metrics(control, deployment)
        regional = backend_metrics(control, deployment)
        both_used = all(item["counters"].get("TokenTransaction", 0) > 0 for item in regional.values())
        showback = project_showback(native, deployment, projects)
        attributed = all(row["data_status"] == "complete" and row["total_tokens"] > 0 for row in showback["projects"])
        if (native["rows"] and both_used and attributed) or time.monotonic() >= deadline:
            break
        print("Waiting for native gateway/backend metrics ingestion.", flush=True)
        time.sleep(20)
    receipt = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_probe_usage": probes,
        "native_gateway_metrics": native,
        "backend_account_metrics": regional,
        "all_configured_regions_observed": both_used,
        "native_project_showback": showback,
        "financial_chargeback_status": "showback_ready_for_verified_Entra_test_population; rates_and_billing_reconciliation_required",
    }
    save_receipt(args.output, receipt)
    if not native["rows"]:
        raise RuntimeError("No native gateway token metric was observed; usage receipt records the gap.")
    if not both_used:
        raise RuntimeError("Configured backend regions were not all observed in authoritative resource counters.")
    if not attributed:
        raise RuntimeError("Native project attribution is missing or incomplete; receipt records the gap.")
    print(f"Native token export and regional inference observed; evidence: {args.output}.")


if __name__ == "__main__":
    main()
