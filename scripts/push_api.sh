#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
probe_region="${AWS_REGION:-$(.venv/bin/python -c 'import json; print(json.load(open("infra/config.json"))["home_region"])')}"
probe_tag="${1:?Usage: AWS_REGION=us-east-1 bash scripts/push_api.sh IMAGE_TAG}"
probe_repo="$(AWS_REGION="$probe_region" .venv/bin/python - <<'PY'
import json, os
import boto3
config = json.load(open("infra/config.json"))
client = boto3.client("ecr", region_name=os.environ["AWS_REGION"])
name = config["name"] + "-api"
try:
    repo = client.describe_repositories(repositoryNames=[name])["repositories"][0]
except client.exceptions.RepositoryNotFoundException:
    repo = client.create_repository(repositoryName=name, imageTagMutability="IMMUTABLE",
                                    imageScanningConfiguration={"scanOnPush": True})["repository"]
print(repo["repositoryUri"])
PY
)"
probe_platform="$(.venv/bin/python -c 'import json; print("linux/arm64" if json.load(open("infra/config.json"))["api_architecture"] == "ARM64" else "linux/amd64")')"
probe_registry="${probe_repo%%/*}"
aws ecr get-login-password --region "$probe_region" |
  docker login --username AWS --password-stdin "$probe_registry"
docker buildx build --platform "$probe_platform" --tag "${probe_repo}:${probe_tag}" --push .
