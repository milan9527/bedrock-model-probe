# Deployment and operation

This project deploys directly with AWS CLI and Boto3. It does not use
CloudFormation or Terraform. Resource names and account IDs are resolved from
the deployment configuration and the active AWS credentials.

## Prerequisites

- Python 3.12 or newer, AWS CLI v2, Docker, and Docker Buildx.
- AWS credentials authorized to manage the services in the architecture diagram.
- An existing default VPC in each of the six regions listed in the README.
- Account access to the Bedrock models and inference profiles you intend to use.
- An available analysis model in the configured `analysis_region`.

Model availability, Marketplace access, and account data-retention requirements
vary by region and endpoint. Deployment does not accept model agreements or
change regional account data-retention settings.

## Configure

Review `infra/config.json` before deployment:

| Setting | Purpose |
|---|---|
| `home_region` | DynamoDB, frontend storage, and Cognito region |
| `api_region` | ECS API and internal load balancer region |
| `api_image_tag` | Immutable ECR image tag to deploy |
| `api_architecture` | `ARM64` or `X86_64` for the API image |
| `schedule_enabled` | Enable or pause regional ten-minute schedules |
| `input_sizes` | Scheduled presets: a subset of `1`, `10`, and `100` |
| `max_output_tokens` | Per-request output budget |
| `worker_concurrency` | Probe worker concurrency in each region |
| `max_targets_per_region` | Discovery budget; excess targets stop that regional run |
| `model_id_regex`, `apis`, `routing_modes` | Optional collection restrictions |
| `fable_global_only` | Optional Fable restriction; `false` enables all discovered routes |
| `retention_days` | Raw measurement expiration |
| `analysis_model_id` | Model or profile used for AI observations |
| `analysis_max_calls_per_10_minutes` | Shared analysis inference budget |

The region list is defined in `probe/config.py`. With schedules enabled, each
selected target and input preset produces one real inference request per
ten-minute cycle. See the README for capacity and cost calculations.

## Install and deploy

Run commands from the repository root using credentials for the intended AWS
account. You can select an AWS CLI profile with `AWS_PROFILE`.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
aws sts get-caller-identity

bash scripts/package_lambda.sh
probe_image_tag=$(.venv/bin/python -c 'import json; print(json.load(open("infra/config.json"))["api_image_tag"])')
probe_api_region=$(.venv/bin/python -c 'import json; print(json.load(open("infra/config.json"))["api_region"])')
AWS_REGION="$probe_api_region" bash scripts/push_api.sh "$probe_image_tag"
.venv/bin/python -m infra.aws_deploy --part all --update-code
```

Deployment reuses NAT connectivity in each default VPC when available. Otherwise,
it creates application-specific private subnets and NAT egress in that VPC.
It creates regional Lambda, Scheduler, SQS and monitoring resources, the shared
table, and the private frontend/API infrastructure.

The frontend uses a private S3 bucket with CloudFront OAC. The ECS service uses
private Fargate tasks behind an internal ALB exposed through a CloudFront VPC
origin. Cognito self-registration is disabled.

Deployment writes these local files:

- `.build/dashboard-deployment.json`: dashboard URL and resource identifiers.
- `.build/default-networks.json`: regional network identifiers.

Generated output, local credentials, and private deployment records are excluded
from version control.

## Create dashboard users

Create users administratively in the deployed Cognito pool. You can use the AWS
console or an administrative workflow appropriate for your account. Passwords
and access tokens do not belong in this repository.

Users must sign in before accessing dashboard data. Reading the dashboard
requires a valid session. Changing collection settings or requesting discovery
also requires membership in the `probe-admins` group:

```bash
aws cognito-idp admin-add-user-to-group --region YOUR_HOME_REGION \
  --user-pool-id YOUR_USER_POOL_ID \
  --username YOUR_USER_EMAIL \
  --group-name probe-admins
```

Sign out and back in after a group change to obtain an updated access token.

## Updates

For Python worker changes, rebuild the Lambda package and publish it:

```bash
bash scripts/package_lambda.sh
.venv/bin/python -m infra.aws_deploy --part regions --update-code
```

For API changes, choose a new `api_image_tag`, build and push that tag, then
reconcile the dashboard:

```bash
probe_image_tag=$(.venv/bin/python -c 'import json; print(json.load(open("infra/config.json"))["api_image_tag"])')
probe_api_region=$(.venv/bin/python -c 'import json; print(json.load(open("infra/config.json"))["api_region"])')
AWS_REGION="$probe_api_region" bash scripts/push_api.sh "$probe_image_tag"
.venv/bin/python -m infra.aws_deploy --part dashboard
```

ECR tags are immutable. Do not reuse an existing tag for a changed image.
Dashboard reconciliation also uploads frontend assets with cache revalidation.

## Collection controls and verification

Use **Management** to discover current targets, enable or pause probes, and
control automatic addition of new models. Saved choices apply to each region's
next scheduled run. Dashboard filters only change the displayed measurements.

Check the latest scheduled sample, regional discovery status, and the Issues
column after deployment. Probe execution uses the regional Lambda logs; API
execution uses the ECS log group. Queue age, dead letters, and Lambda failures
have CloudWatch alarms.

To pause future collection, set `schedule_enabled` to `false` and reconcile:

```bash
.venv/bin/python -m infra.aws_deploy --part regions
```

Already queued jobs may finish until their ten-minute freshness limit. This
pauses collection but leaves infrastructure running.
