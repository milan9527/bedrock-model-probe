"""Direct AWS SDK deployment. No Terraform or CloudFormation operations."""
import argparse
import json
import mimetypes
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from infra.aws_network import allow_ingress, default_network, security_group, tags
from probe.config import REGIONS

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "infra/config.json").read_text())
NAME = CONFIG["name"]
HOME = CONFIG["home_region"]
ACCOUNT = boto3.client("sts").get_caller_identity()["Account"]
TABLE = f"{NAME}-samples"
TABLE_ARN = f"arn:aws:dynamodb:{HOME}:{ACCOUNT}:table/{TABLE}"
IAM = boto3.client("iam")


def log(message):
    print(message, flush=True)


def policy(statements):
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def role(name, service, statements, managed=()):
    trust = policy([{"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}])
    try:
        value = IAM.get_role(RoleName=name)["Role"]
    except IAM.exceptions.NoSuchEntityException:
        value = IAM.create_role(RoleName=name, AssumeRolePolicyDocument=trust,
                                Tags=tags(name))["Role"]
    if statements:
        IAM.put_role_policy(RoleName=name, PolicyName="bedrock-probe-access", PolicyDocument=policy(statements))
    for arn in managed:
        IAM.attach_role_policy(RoleName=name, PolicyArn=arn)
    # Retire only policies created by the abandoned deployment on these application roles.
    for old in IAM.list_role_policies(RoleName=name)["PolicyNames"]:
        if old.startswith("terraform-"):
            IAM.delete_role_policy(RoleName=name, PolicyName=old)
    IAM.tag_role(RoleName=name, Tags=tags(name))
    return value["Arn"]


def log_group(region, name):
    logs = boto3.client("logs", region_name=region)
    try:
        logs.create_log_group(logGroupName=name, tags={"Application": NAME, "ManagedBy": "aws-cli"})
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=name, retentionInDays=30)


def queue(client, suffix, **attributes):
    desired = {"SqsManagedSseEnabled": "true", **{k: str(v) for k, v in attributes.items()}}
    try:
        response = client.get_queue_url(QueueName=f"{NAME}-{suffix}")
    except client.exceptions.QueueDoesNotExist:
        response = client.create_queue(
            QueueName=f"{NAME}-{suffix}", Attributes=desired,
            tags={"Application": NAME, "ManagedBy": "aws-cli"})
    url = response["QueueUrl"]
    client.set_queue_attributes(QueueUrl=url, Attributes=desired)
    arn = client.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    client.set_queue_attributes(QueueUrl=url, Attributes={"Policy": policy([{
        "Effect": "Deny", "Principal": "*", "Action": "sqs:*", "Resource": arn,
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }])})
    return url, arn


def lambda_wait(client, name):
    client.get_waiter("function_updated_v2").wait(
        FunctionName=name, WaiterConfig={"Delay": 5, "MaxAttempts": 120})


def configure_lambda(client, parameters):
    # IAM permissions can take a few seconds to propagate to Lambda's ENI preflight.
    for attempt in range(12):
        try:
            return client.update_function_configuration(**parameters)
        except ClientError as exc:
            message = exc.response["Error"].get("Message", "")
            if attempt == 11 or not (
                "execution role" in message and "CreateNetworkInterface" in message
            ):
                raise
            time.sleep(5)


def regional(region, network, update_code=False):
    sqs = boto3.client("sqs", region_name=region)
    lam = boto3.client("lambda", region_name=region)
    dead_url, dead_arn = queue(sqs, "dead", MessageRetentionPeriod=1209600)
    schedule_dead_url, schedule_dead_arn = queue(sqs, "schedule-dead", MessageRetentionPeriod=1209600)
    job_url, job_arn = queue(
        sqs, "jobs", VisibilityTimeout=900, MessageRetentionPeriod=172800,
        RedrivePolicy=json.dumps({"deadLetterTargetArn": dead_arn, "maxReceiveCount": 3}),
    )
    env = {
        "TABLE_NAME": TABLE, "HOME_REGION": HOME, "QUEUE_URL": job_url,
        "INPUT_SIZES": json.dumps(CONFIG["input_sizes"]),
        "MAX_OUTPUT_TOKENS": str(CONFIG["max_output_tokens"]),
        "ANTHROPIC_THINKING": CONFIG.get("anthropic_thinking", "disabled"),
        "FABLE_GLOBAL_ONLY": str(CONFIG.get("fable_global_only", False)).lower(),
        "MAX_TARGETS": str(CONFIG["max_targets_per_region"]),
        "RETENTION_DAYS": str(CONFIG["retention_days"]),
        "MODEL_ID_REGEX": CONFIG["model_id_regex"],
        "APIS": json.dumps(CONFIG["apis"]), "ROUTING_MODES": json.dumps(CONFIG["routing_modes"]),
    }
    functions = {}
    # Upgrade the worker before a planner can enqueue a new job configuration.
    for kind in ("worker", "planner"):
        function_name = f"{NAME}-{kind}"
        log_group(region, f"/aws/lambda/{function_name}")
        statements = [
            {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"],
             "Resource": TABLE_ARN},
            {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
             "Resource": f"arn:aws:logs:{region}:{ACCOUNT}:log-group:/aws/lambda/{function_name}:*"},
            {"Effect": "Allow", "Action": [
                "ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface",
                "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses", "ec2:DescribeSubnets",
            ], "Resource": "*"},
        ]
        if kind == "planner":
            statements.extend([
                {"Effect": "Allow", "Action": ["bedrock:ListFoundationModels", "bedrock:ListInferenceProfiles",
                                             "bedrock-mantle:ListModels"], "Resource": "*"},
                {"Effect": "Allow", "Action": ["sqs:SendMessage"], "Resource": [job_arn, schedule_dead_arn]},
            ])
        else:
            statements.extend([
                {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                 "Resource": [f"arn:aws:bedrock:*::foundation-model/{p}.*" for p in ("anthropic", "openai")]
                 + [f"arn:aws:bedrock:*:{ACCOUNT}:inference-profile/*",
                    f"arn:aws:bedrock:*:{ACCOUNT}:project/default"]},
                {"Effect": "Allow", "Action": ["bedrock-mantle:CreateInference"], "Resource": "*"},
                {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
                 "Resource": job_arn},
            ])
        arn = role(f"{NAME}-{region}-{kind}", "lambda.amazonaws.com", statements)
        parameters = {
            "FunctionName": function_name, "Role": arn, "Runtime": "python3.12",
            "Handler": f"probe.jobs.{kind}", "Timeout": 180 if kind == "planner" else 150,
            "MemorySize": 512 if kind == "planner" else 1024, "Environment": {"Variables": env},
            "VpcConfig": {"SubnetIds": network["subnets"],
                          "SecurityGroupIds": [network["lambda_security_group"]]},
        }
        if kind == "planner":
            parameters["DeadLetterConfig"] = {"TargetArn": schedule_dead_arn}
        try:
            lam.get_function(FunctionName=function_name)
        except lam.exceptions.ResourceNotFoundException:
            lam.create_function(**parameters, Architectures=["x86_64"],
                                Code={"ZipFile": (ROOT / ".build/lambda.zip").read_bytes()},
                                Tags={"Application": NAME, "ManagedBy": "aws-cli"})
            lam.get_waiter("function_active_v2").wait(
                FunctionName=function_name, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
        else:
            if update_code:
                lam.update_function_code(FunctionName=function_name,
                                         ZipFile=(ROOT / ".build/lambda.zip").read_bytes())
                lambda_wait(lam, function_name)
            configure_lambda(lam, parameters)
            lambda_wait(lam, function_name)
        functions[kind] = f"arn:aws:lambda:{region}:{ACCOUNT}:function:{function_name}"
        lam.tag_resource(Resource=functions[kind], Tags={"Application": NAME, "ManagedBy": "aws-cli"})
        log(f"{region}: {kind} attached to default VPC {network['vpc_id']}")
    lam.put_function_event_invoke_config(
        FunctionName=f"{NAME}-planner", MaximumEventAgeInSeconds=300, MaximumRetryAttempts=0)
    mappings = lam.list_event_source_mappings(FunctionName=f"{NAME}-worker", EventSourceArn=job_arn)[
        "EventSourceMappings"]
    mapping = {"BatchSize": 1, "FunctionResponseTypes": ["ReportBatchItemFailures"],
               "ScalingConfig": {"MaximumConcurrency": CONFIG["worker_concurrency"]}, "Enabled": True}
    if mappings:
        lam.update_event_source_mapping(UUID=mappings[0]["UUID"], **mapping)
    else:
        lam.create_event_source_mapping(FunctionName=f"{NAME}-worker", EventSourceArn=job_arn, **mapping)
    scheduler_role = role(f"{NAME}-{region}-scheduler", "scheduler.amazonaws.com", [
        {"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": functions["planner"]},
        {"Effect": "Allow", "Action": ["sqs:SendMessage"], "Resource": schedule_dead_arn},
    ])
    scheduler = boto3.client("scheduler", region_name=region)
    schedule = {
        "Name": NAME, "ScheduleExpression": "cron(0/10 * * * ? *)", "ScheduleExpressionTimezone": "UTC",
        "State": "ENABLED" if CONFIG["schedule_enabled"] else "DISABLED", "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {"Arn": functions["planner"], "RoleArn": scheduler_role,
                   "Input": json.dumps({"scheduled_time": "<aws.scheduler.scheduled-time>"}),
                   "RetryPolicy": {"MaximumEventAgeInSeconds": 300, "MaximumRetryAttempts": 2},
                   "DeadLetterConfig": {"Arn": schedule_dead_arn}},
    }
    try:
        scheduler.get_schedule(Name=NAME)
    except scheduler.exceptions.ResourceNotFoundException:
        scheduler.create_schedule(**schedule)
    else:
        scheduler.update_schedule(**schedule)
    cw = boto3.client("cloudwatch", region_name=region)
    alarms = [
        (f"{k}-errors", "AWS/Lambda", "Errors", "Sum", 1, "FunctionName", f"{NAME}-{k}")
        for k in ("planner", "worker")
    ] + [
        ("queue-lag", "AWS/SQS", "ApproximateAgeOfOldestMessage", "Maximum", 600, "QueueName", f"{NAME}-jobs"),
        ("jobs-dead-letters", "AWS/SQS", "ApproximateNumberOfMessagesVisible", "Maximum", 1,
         "QueueName", f"{NAME}-dead"),
        ("scheduler-dead-letters", "AWS/SQS", "ApproximateNumberOfMessagesVisible", "Maximum", 1,
         "QueueName", f"{NAME}-schedule-dead"),
    ]
    for suffix, namespace, metric, statistic, threshold, dimension, value in alarms:
        cw.put_metric_alarm(
            AlarmName=f"{NAME}-{region}-{suffix}", Namespace=namespace, MetricName=metric,
            Statistic=statistic, Period=60 if suffix.endswith("dead-letters") else 600,
            EvaluationPeriods=1, Threshold=threshold,
            ComparisonOperator="GreaterThanOrEqualToThreshold", TreatMissingData="notBreaching",
            Dimensions=[{"Name": dimension, "Value": value}],
        )
    return {"region": region, "vpc_id": network["vpc_id"], "planner": functions["planner"],
            "worker": functions["worker"]}


def storage_and_users():
    ddb = boto3.client("dynamodb", region_name=HOME)
    try:
        ddb.describe_table(TableName=TABLE)
    except ddb.exceptions.ResourceNotFoundException:
        ddb.create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in ("pk", "sk")],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            Tags=tags(TABLE),
        )
        ddb.get_waiter("table_exists").wait(TableName=TABLE)
    ddb.update_continuous_backups(TableName=TABLE,
                                  PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True})
    if ddb.describe_time_to_live(TableName=TABLE)["TimeToLiveDescription"]["TimeToLiveStatus"] == "DISABLED":
        ddb.update_time_to_live(TableName=TABLE, TimeToLiveSpecification={
            "Enabled": True, "AttributeName": "expires_at"})
    bucket = f"{NAME}-{ACCOUNT}-{HOME}"
    s3 = boto3.client("s3", region_name=HOME)
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=ACCOUNT)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
        s3.create_bucket(Bucket=bucket, **(
            {"CreateBucketConfiguration": {"LocationConstraint": HOME}} if HOME != "us-east-1" else {}))
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True})
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    cognito = boto3.client("cognito-idp", region_name=HOME)
    pools = []
    for page in cognito.get_paginator("list_user_pools").paginate(MaxResults=60):
        pools.extend(page["UserPools"])
    pool = next((p for p in pools if p["Name"] == NAME), None)
    if pool is None:
        pool = cognito.create_user_pool(
            PoolName=NAME, UsernameAttributes=["email"], AutoVerifiedAttributes=["email"],
            AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
            Policies={"PasswordPolicy": {"MinimumLength": 12, "RequireUppercase": True,
                                         "RequireLowercase": True, "RequireNumbers": True, "RequireSymbols": True}},
            UserPoolTags={"Application": NAME, "ManagedBy": "aws-cli"},
        )["UserPool"]
    pool_id = pool["Id"]
    try:
        cognito.get_group(UserPoolId=pool_id, GroupName="probe-admins")
    except cognito.exceptions.ResourceNotFoundException:
        cognito.create_group(UserPoolId=pool_id, GroupName="probe-admins",
                             Description="Manage model discovery and scheduled probe selections")
    detail = cognito.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    if not detail.get("AdminCreateUserConfig", {}).get("AllowAdminCreateUserOnly"):
        allowed = cognito.meta.service_model.operation_model("UpdateUserPool").input_shape.members
        parameters = {key: value for key, value in detail.items() if key in allowed}
        parameters.update(UserPoolId=pool_id, AdminCreateUserConfig={"AllowAdminCreateUserOnly": True})
        cognito.update_user_pool(**parameters)
    domain = f"{NAME}-{ACCOUNT}-{HOME}"
    existing = cognito.describe_user_pool_domain(Domain=domain).get("DomainDescription", {})
    if not existing.get("UserPoolId"):
        cognito.create_user_pool_domain(Domain=domain, UserPoolId=pool_id)
    elif existing["UserPoolId"] != pool_id:
        raise RuntimeError("Cognito domain belongs to another user pool")
    return bucket, pool_id, domain


def dashboard(network):
    region = CONFIG["api_region"]
    if region != network["region"]:
        raise ValueError("API network region mismatch")
    bucket, pool_id, login_domain = storage_and_users()
    cognito = boto3.client("cognito-idp", region_name=HOME)
    clients = cognito.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)["UserPoolClients"]
    web_client = next((c for c in clients if c["ClientName"] == "dashboard"), None)
    if web_client is None:
        web_client = cognito.create_user_pool_client(
            UserPoolId=pool_id, ClientName="dashboard", GenerateSecret=False,
            PreventUserExistenceErrors="ENABLED", AccessTokenValidity=1,
        )["UserPoolClient"]
    client_id = web_client["ClientId"]
    ec2 = boto3.client("ec2", region_name=region)
    elb = boto3.client("elbv2", region_name=region)
    ecs = boto3.client("ecs", region_name=region)
    cf = boto3.client("cloudfront")
    alb_sg = security_group(ec2, network["vpc_id"], f"{NAME}-alb")
    api_sg = security_group(ec2, network["vpc_id"], f"{NAME}-api")
    allow_ingress(ec2, alb_sg, {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
                              "IpRanges": [{"CidrIp": network["cidr"], "Description": "CloudFront VPC origin"}]})
    allow_ingress(ec2, api_sg, {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000,
                              "UserIdGroupPairs": [{"GroupId": alb_sg, "Description": "Internal ALB"}]})
    if len(network["subnets"]) < 2:
        raise RuntimeError("The API requires private subnets in at least two availability zones")
    try:
        lb = elb.describe_load_balancers(Names=[f"{NAME}-api"])["LoadBalancers"][0]
    except elb.exceptions.LoadBalancerNotFoundException:
        lb = elb.create_load_balancer(
            Name=f"{NAME}-api", Subnets=network["subnets"], SecurityGroups=[alb_sg], Scheme="internal",
            Type="application", IpAddressType="ipv4", Tags=tags(f"{NAME}-api"),
        )["LoadBalancers"][0]
    if lb["VpcId"] != network["vpc_id"]:
        raise RuntimeError("Existing API load balancer is outside the default VPC")
    try:
        target = elb.describe_target_groups(Names=[f"{NAME}-api"])["TargetGroups"][0]
    except elb.exceptions.TargetGroupNotFoundException:
        target = elb.create_target_group(
            Name=f"{NAME}-api", Protocol="HTTP", Port=8000, VpcId=network["vpc_id"], TargetType="ip",
            HealthCheckProtocol="HTTP", HealthCheckPath="/health", Matcher={"HttpCode": "200"},
            Tags=tags(f"{NAME}-api"),
        )["TargetGroups"][0]
    listeners = elb.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"])["Listeners"]
    if not listeners:
        elb.create_listener(LoadBalancerArn=lb["LoadBalancerArn"], Protocol="HTTP", Port=80,
                            DefaultActions=[{"Type": "forward", "TargetGroupArn": target["TargetGroupArn"]}])
    ecs.create_cluster(clusterName=NAME, settings=[{"name": "containerInsights", "value": "enabled"}],
                       tags=[{"key": "Application", "value": NAME}, {"key": "ManagedBy", "value": "aws-cli"}])
    execution_role = role(f"{NAME}-ecs-execution", "ecs-tasks.amazonaws.com", [], managed=[
        "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"])
    analysis_model = CONFIG["analysis_model_id"]
    foundation_id = (analysis_model.split(".", 1)[1]
                     if analysis_model.startswith(("global.", "us.", "eu.", "au.", "jp.")) else analysis_model)
    api_role = role(f"{NAME}-api", "ecs-tasks.amazonaws.com", [
        {"Effect": "Allow", "Action": ["dynamodb:Query", "dynamodb:GetItem"], "Resource": TABLE_ARN},
        {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:UpdateItem"], "Resource": TABLE_ARN,
         "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["ANALYSIS#*", "MANAGEMENT#*"]}}},
        {"Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": [
            f"arn:aws:lambda:{r}:{ACCOUNT}:function:{NAME}-planner" for r in REGIONS
        ]},
        {"Effect": "Allow", "Action": ["bedrock:InvokeModel"], "Resource": [
            f"arn:aws:bedrock:*::foundation-model/{foundation_id}*",
            f"arn:aws:bedrock:*:{ACCOUNT}:inference-profile/{analysis_model}",
        ]},
    ])
    log_group(region, f"/ecs/{NAME}-api")
    task = ecs.register_task_definition(
        family=f"{NAME}-api", requiresCompatibilities=["FARGATE"], networkMode="awsvpc", cpu="512", memory="1024",
        executionRoleArn=execution_role, taskRoleArn=api_role,
        runtimePlatform={"cpuArchitecture": CONFIG["api_architecture"], "operatingSystemFamily": "LINUX"},
        containerDefinitions=[{
            "name": "api", "image": f"{ACCOUNT}.dkr.ecr.{HOME}.amazonaws.com/{NAME}-api:{CONFIG['api_image_tag']}",
            "essential": True, "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
            "environment": [
                {"name": "TABLE_NAME", "value": TABLE}, {"name": "HOME_REGION", "value": HOME},
                {"name": "JWT_ISSUER", "value": f"https://cognito-idp.{HOME}.amazonaws.com/{pool_id}"},
                {"name": "JWT_CLIENT_ID", "value": client_id},
                {"name": "PLANNER_FUNCTION", "value": f"{NAME}-planner"},
                {"name": "FABLE_GLOBAL_ONLY", "value": str(CONFIG.get("fable_global_only", False)).lower()},
                {"name": "MAX_TARGETS", "value": str(CONFIG["max_targets_per_region"])},
                {"name": "INPUT_SIZES", "value": json.dumps(CONFIG["input_sizes"])},
                {"name": "ANALYSIS_MODEL_ID", "value": analysis_model},
                {"name": "ANALYSIS_REGION", "value": CONFIG["analysis_region"]},
                {"name": "ANALYSIS_MAX_CALLS", "value": str(CONFIG["analysis_max_calls_per_10_minutes"])},
            ],
            "logConfiguration": {"logDriver": "awslogs", "options": {
                "awslogs-group": f"/ecs/{NAME}-api", "awslogs-region": region, "awslogs-stream-prefix": "api"}},
            "readonlyRootFilesystem": True, "linuxParameters": {"initProcessEnabled": True},
        }],
    )["taskDefinition"]["taskDefinitionArn"]
    service = {
        "cluster": NAME, "taskDefinition": task, "desiredCount": 2,
        "networkConfiguration": {"awsvpcConfiguration": {
            "subnets": network["subnets"], "securityGroups": [api_sg], "assignPublicIp": "DISABLED"}},
        "loadBalancers": [{"targetGroupArn": target["TargetGroupArn"], "containerName": "api", "containerPort": 8000}],
        "healthCheckGracePeriodSeconds": 60,
        "deploymentConfiguration": {"minimumHealthyPercent": 50, "maximumPercent": 200,
                                    "deploymentCircuitBreaker": {"enable": True, "rollback": True}},
    }
    services = ecs.describe_services(cluster=NAME, services=[f"{NAME}-api"])["services"]
    if any(s["status"] == "ACTIVE" for s in services):
        ecs.update_service(service=f"{NAME}-api", **service)
    else:
        ecs.create_service(serviceName=f"{NAME}-api", launchType="FARGATE", **service)
    log(f"{region}: ECS API started in default VPC {network['vpc_id']}")
    elb.get_waiter("load_balancer_available").wait(
        LoadBalancerArns=[lb["LoadBalancerArn"]], WaiterConfig={"Delay": 10, "MaxAttempts": 60})
    vpc_origins = cf.list_vpc_origins().get("VpcOriginList", {}).get("Items", [])
    origin = next((o for o in vpc_origins if o["Name"] == f"{NAME}-api-default"), None)
    if origin is None:
        origin = cf.create_vpc_origin(VpcOriginEndpointConfig={
            "Name": f"{NAME}-api-default", "Arn": lb["LoadBalancerArn"], "HTTPPort": 80, "HTTPSPort": 443,
            "OriginProtocolPolicy": "http-only", "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
        })["VpcOrigin"]
    for _ in range(90):
        current = cf.get_vpc_origin(Id=origin["Id"])["VpcOrigin"]
        if current["Status"] == "Deployed":
            break
        time.sleep(10)
    else:
        raise TimeoutError("CloudFront VPC origin did not become ready")
    origin_groups = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [network["vpc_id"]]},
        {"Name": "group-name", "Values": ["CloudFront-VPCOrigins-Service-SG"]},
    ])["SecurityGroups"]
    if len(origin_groups) != 1:
        raise RuntimeError("CloudFront VPC origin service security group was not found")
    allow_ingress(ec2, alb_sg, {
        "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
        "UserIdGroupPairs": [{"GroupId": origin_groups[0]["GroupId"],
                             "Description": "CloudFront VPC origin service"}],
    })
    ec2.revoke_security_group_ingress(GroupId=alb_sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
        "IpRanges": [{"CidrIp": network["cidr"]}],
    }])
    controls = cf.list_origin_access_controls().get("OriginAccessControlList", {}).get("Items", [])
    control = next((c for c in controls if c["Name"] == f"{NAME}-s3"), None)
    if control is None:
        control = cf.create_origin_access_control(OriginAccessControlConfig={
            "Name": f"{NAME}-s3", "SigningProtocol": "sigv4", "SigningBehavior": "always",
            "OriginAccessControlOriginType": "s3",
        })["OriginAccessControl"]
    cache_id = next(i["CachePolicy"]["Id"] for i in cf.list_cache_policies(Type="managed")[
        "CachePolicyList"]["Items"] if i["CachePolicy"]["CachePolicyConfig"]["Name"] == "Managed-CachingDisabled")
    request_id = next(i["OriginRequestPolicy"]["Id"] for i in cf.list_origin_request_policies(Type="managed")[
        "OriginRequestPolicyList"]["Items"] if i["OriginRequestPolicy"]["OriginRequestPolicyConfig"]["Name"]
        == "Managed-AllViewerExceptHostHeader")
    security_id = next(i["ResponseHeadersPolicy"]["Id"] for i in cf.list_response_headers_policies(Type="managed")[
        "ResponseHeadersPolicyList"]["Items"] if i["ResponseHeadersPolicy"]["ResponseHeadersPolicyConfig"]["Name"]
        == "Managed-SecurityHeadersPolicy")
    behavior = {
        "TargetOriginId": "frontend", "ViewerProtocolPolicy": "redirect-to-https",
        "AllowedMethods": {"Quantity": 3, "Items": ["GET", "HEAD", "OPTIONS"],
                           "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
        "CachePolicyId": cache_id, "ResponseHeadersPolicyId": security_id, "Compress": True,
    }
    distro = {
        "CallerReference": f"{NAME}-{ACCOUNT}", "Comment": NAME, "Enabled": True,
        "DefaultRootObject": "index.html", "IsIPV6Enabled": True, "HttpVersion": "http2and3",
        "Origins": {"Quantity": 2, "Items": [
            {"Id": "frontend", "DomainName": f"{bucket}.s3.{HOME}.amazonaws.com",
             "OriginAccessControlId": control["Id"], "S3OriginConfig": {"OriginAccessIdentity": ""}},
            {"Id": "api", "DomainName": lb["DNSName"], "VpcOriginConfig": {
                "VpcOriginId": origin["Id"], "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5}},
        ]},
        "DefaultCacheBehavior": behavior,
        "CacheBehaviors": {"Quantity": 1, "Items": [{
            **behavior, "PathPattern": "/api/*", "TargetOriginId": "api", "ViewerProtocolPolicy": "https-only",
            "AllowedMethods": {"Quantity": 7, "Items": ["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
            "OriginRequestPolicyId": request_id,
        }]},
        "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
        "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
    }
    distributions = []
    for page in cf.get_paginator("list_distributions").paginate():
        distributions.extend(page.get("DistributionList", {}).get("Items", []))
    existing = next((d for d in distributions if d.get("Comment") == NAME), None)
    if existing:
        previous = cf.get_distribution_config(Id=existing["Id"])
        distro["CallerReference"] = previous["DistributionConfig"]["CallerReference"]
        distribution = cf.update_distribution(Id=existing["Id"], IfMatch=previous["ETag"],
                                                DistributionConfig=distro)["Distribution"]
    else:
        distribution = cf.create_distribution(DistributionConfig=distro)["Distribution"]
    url = f"https://{distribution['DomainName']}/"
    oauth = {
        "UserPoolId": pool_id, "AllowedOAuthFlowsUserPoolClient": True, "AllowedOAuthFlows": ["code"],
        "AllowedOAuthScopes": ["openid", "email"], "SupportedIdentityProviders": ["COGNITO"],
        "CallbackURLs": [url], "LogoutURLs": [url], "PreventUserExistenceErrors": "ENABLED",
        "AccessTokenValidity": 1,
    }
    cognito.update_user_pool_client(ClientId=client_id, **oauth)
    s3 = boto3.client("s3", region_name=HOME)
    s3.put_bucket_policy(Bucket=bucket, Policy=policy([
        {"Effect": "Allow", "Principal": {"Service": "cloudfront.amazonaws.com"}, "Action": "s3:GetObject",
         "Resource": f"arn:aws:s3:::{bucket}/*",
         "Condition": {"StringEquals": {"AWS:SourceArn": distribution["ARN"]}}},
        {"Effect": "Deny", "Principal": "*", "Action": "s3:*",
         "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
         "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
    ]))
    for file in (ROOT / "frontend").rglob("*"):
        if file.is_file():
            s3.put_object(Bucket=bucket, Key=str(file.relative_to(ROOT / "frontend")), Body=file.read_bytes(),
                          ContentType=mimetypes.guess_type(file.name)[0] or "application/octet-stream",
                          CacheControl="no-cache")
    s3.put_object(Bucket=bucket, Key="config.json", ContentType="application/json", CacheControl="no-store",
                  Body=json.dumps({"cognito_domain": f"https://{login_domain}.auth.{HOME}.amazoncognito.com",
                                   "client_id": client_id, "redirect_uri": url}).encode())
    result = {
        "dashboard_url": url, "distribution_id": distribution["Id"], "user_pool_id": pool_id,
        "client_id": client_id, "bucket": bucket, "api_region": region, "vpc_id": network["vpc_id"],
        "load_balancer_arn": lb["LoadBalancerArn"], "target_group_arn": target["TargetGroupArn"],
    }
    (ROOT / ".build/dashboard-deployment.json").write_text(json.dumps(result, indent=2))
    log(f"Dashboard assets uploaded: {url}; waiting for CloudFront and ECS")
    cf.get_waiter("distribution_deployed").wait(
        Id=distribution["Id"], WaiterConfig={"Delay": 15, "MaxAttempts": 80})
    ecs.get_waiter("services_stable").wait(
        cluster=NAME, services=[f"{NAME}-api"], WaiterConfig={"Delay": 10, "MaxAttempts": 80})
    log(f"Dashboard deployed: {url}")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--part", choices=["all", "regions", "dashboard"], default="all")
    parser.add_argument("--update-code", action="store_true")
    args = parser.parse_args()
    saved = ROOT / ".build/default-networks.json"
    saved.parent.mkdir(parents=True, exist_ok=True)
    networks = json.loads(saved.read_text()) if saved.exists() else {}
    needed = list(REGIONS) if args.part in ("all", "regions") else [CONFIG["api_region"]]
    with ThreadPoolExecutor(max_workers=7) as executor:
        futures = {executor.submit(default_network, region): region for region in needed}
        for future in as_completed(futures):
            networks[futures[future]] = future.result()
            saved.write_text(json.dumps(networks, indent=2))
    if args.part in ("all", "regions"):
        storage_and_users()
        with ThreadPoolExecutor(max_workers=7) as executor:
            futures = [executor.submit(regional, region, networks[region], args.update_code) for region in REGIONS]
            for future in as_completed(futures):
                log(json.dumps(future.result()))
    if args.part in ("all", "dashboard"):
        dashboard(networks[CONFIG["api_region"]])


if __name__ == "__main__":
    main()
