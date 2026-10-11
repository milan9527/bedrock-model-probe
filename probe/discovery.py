import json
from dataclasses import asdict

import boto3
import httpx
from botocore.config import Config

from probe.adapters import signed_headers
from probe.config import Target, eligible, runtime_targets, target_apis


def discover(region: str) -> tuple[list[Target], list[dict]]:
    targets, issues = [], []
    client = boto3.client("bedrock", region_name=region,
                          config=Config(connect_timeout=5, read_timeout=15,
                                        retries={"total_max_attempts": 2}))
    models, profiles = [], []
    try:
        models = client.list_foundation_models()["modelSummaries"]
    except Exception as exc:
        issues.append({"surface": "runtime/models", "error": str(exc)[:800]})
    try:
        token = None
        while True:
            page = client.list_inference_profiles(typeEquals="SYSTEM_DEFINED", **({"nextToken": token} if token else {}))
            profiles.extend(page.get("inferenceProfileSummaries", []))
            token = page.get("nextToken")
            if not token:
                break
    except Exception as exc:
        issues.append({"surface": "runtime/profiles", "error": str(exc)[:800]})
    targets.extend(runtime_targets(region, models, profiles))
    # The catalog lives at /v1/models even for inference routes under /openai/v1.
    for prefix in ("/v1",):
        try:
            url = f"https://bedrock-mantle.{region}.api.aws{prefix}/models"
            seen = set()
            with httpx.Client(timeout=15) as http:
                while True:
                    headers = signed_headers("GET", url, b"", region, "mantle")
                    response = http.get(url, headers=headers)
                    response.raise_for_status()
                    page = response.json()
                    for model in page.get("data", []):
                        mid = model["id"]
                        if eligible(mid):
                            targets.extend(target_apis(region, mid, "mantle", "regional"))
                    if not page.get("has_more"):
                        break
                    last = page.get("last_id") or page["data"][-1]["id"]
                    if last in seen:
                        raise RuntimeError("Model catalog pagination did not advance")
                    seen.add(last)
                    url = str(httpx.URL(
                        f"https://bedrock-mantle.{region}.api.aws{prefix}/models",
                        params={"after": last},
                    ))
        except Exception as exc:
            issues.append({"surface": f"mantle{prefix}/models", "error": str(exc)[:800]})
    return sorted({t.key: t for t in targets}.values(), key=lambda t: t.key), issues


def snapshot(targets: list[Target], issues: list[dict], now: int):
    return {"discovered_at": now, "targets": [asdict(t) for t in targets], "issues": issues}


if __name__ == "__main__":
    import argparse
    import time
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    targets, issues = discover(args.region)
    print(json.dumps(snapshot(targets, issues, int(time.time())), indent=2))
