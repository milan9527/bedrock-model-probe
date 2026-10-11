"""Use each region's existing default VPC; reuse NAT routes or add isolated probe subnets."""
import ipaddress
import time

import boto3
from botocore.exceptions import ClientError

NAME = "bedrock-probe"


def tags(name):
    return [{"Key": "Name", "Value": name}, {"Key": "Application", "Value": NAME},
            {"Key": "ManagedBy", "Value": "aws-cli"}]


def security_group(ec2, vpc_id, name):
    groups = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "group-name", "Values": [name]},
    ])["SecurityGroups"]
    if groups:
        return groups[0]["GroupId"]
    return ec2.create_security_group(
        GroupName=name, Description=f"{NAME} managed access", VpcId=vpc_id,
        TagSpecifications=[{"ResourceType": "security-group", "Tags": tags(name)}],
    )["GroupId"]


def allow_ingress(ec2, group, permission):
    try:
        ec2.authorize_security_group_ingress(GroupId=group, IpPermissions=[permission])
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise


def default_network(region):
    ec2 = boto3.client("ec2", region_name=region)
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    if len(vpcs) != 1:
        raise RuntimeError(f"{region}: exactly one existing default VPC is required")
    vpc = vpcs[0]
    vpc_id = vpc["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    tables = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
    main = next(t for t in tables if any(a.get("Main") for a in t["Associations"]))
    associations = {
        a["SubnetId"]: t for t in tables for a in t["Associations"] if a.get("SubnetId")
    }
    nat = ec2.describe_nat_gateways(Filter=[
        {"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "state", "Values": ["available", "pending"]},
    ])["NatGateways"]
    nat_ids = {n["NatGatewayId"] for n in nat if n["State"] == "available"}

    def has_route(subnet, key, allowed=None):
        table = associations.get(subnet["SubnetId"], main)
        return any(r.get("DestinationCidrBlock") == "0.0.0.0/0"
                   and r.get("State") == "active" and r.get(key)
                   and (allowed is None or r[key] in allowed) for r in table["Routes"])

    public = sorted([s for s in subnets if has_route(s, "GatewayId")], key=lambda s: s["AvailabilityZone"])
    private = sorted([s for s in subnets if has_route(s, "NatGatewayId", nat_ids)],
                     key=lambda s: s["AvailabilityZone"])
    if not public:
        raise RuntimeError(f"{region}: the default VPC has no Internet Gateway route")
    if not private:
        own_nat = next((n for n in nat if any(
            t["Key"] == "Application" and t["Value"] == NAME for t in n.get("Tags", []))), None)
        chosen_nat = own_nat or next(iter(nat), None)
        if chosen_nat is None:
            allocation = ec2.allocate_address(
                Domain="vpc", TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": tags(f"{NAME}-nat")}])
            chosen_nat = ec2.create_nat_gateway(
                SubnetId=public[0]["SubnetId"], AllocationId=allocation["AllocationId"],
                TagSpecifications=[{"ResourceType": "natgateway", "Tags": tags(f"{NAME}-nat")}],
            )["NatGateway"]
            print(f"{region}: created NAT {chosen_nat['NatGatewayId']} in default VPC", flush=True)
        nat_id = chosen_nat["NatGatewayId"]
        for _ in range(60):
            current = ec2.describe_nat_gateways(NatGatewayIds=[nat_id])["NatGateways"][0]
            if current["State"] == "available":
                break
            if current["State"] in ("failed", "deleted"):
                raise RuntimeError(f"{region}: NAT failed: {current.get('FailureMessage', current['State'])}")
            time.sleep(5)
        else:
            raise TimeoutError(f"{region}: NAT was not ready in five minutes")
        own_tables = [t for t in tables if any(
            tag["Key"] == "Name" and tag["Value"] == f"{NAME}-private" for tag in t.get("Tags", []))]
        route_table = own_tables[0] if own_tables else ec2.create_route_table(
            VpcId=vpc_id, TagSpecifications=[{"ResourceType": "route-table", "Tags": tags(f"{NAME}-private")}],
        )["RouteTable"]
        table_id = route_table["RouteTableId"]
        try:
            ec2.create_route(RouteTableId=table_id, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat_id)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "RouteAlreadyExists":
                raise
        networks = [ipaddress.ip_network(s["CidrBlock"]) for s in subnets]
        available = [n for n in ipaddress.ip_network(vpc["CidrBlock"]).subnets(new_prefix=24)
                     if not any(n.overlaps(other) for other in networks)]
        zones = list(dict.fromkeys(s["AvailabilityZone"] for s in public))[:2]
        for index, zone in enumerate(zones):
            label = f"{NAME}-private-{index + 1}"
            existing = next((s for s in subnets if any(
                t["Key"] == "Name" and t["Value"] == label for t in s.get("Tags", []))), None)
            subnet = existing or ec2.create_subnet(
                VpcId=vpc_id, CidrBlock=str(available.pop(0)), AvailabilityZone=zone,
                TagSpecifications=[{"ResourceType": "subnet", "Tags": tags(label)}],
            )["Subnet"]
            if subnet["SubnetId"] not in associations:
                ec2.associate_route_table(RouteTableId=table_id, SubnetId=subnet["SubnetId"])
            private.append(subnet)
    # Keep at most one subnet in each AZ; Lambda shares an ENI across these functions.
    selected = {}
    for subnet in private:
        selected.setdefault(subnet["AvailabilityZone"], subnet["SubnetId"])
    group = security_group(ec2, vpc_id, f"{NAME}-lambda")
    result = {
        "region": region, "vpc_id": vpc_id, "cidr": vpc["CidrBlock"],
        "subnets": list(selected.values())[:3], "lambda_security_group": group,
        "public_subnets": [s["SubnetId"] for s in public],
    }
    print(f"{region}: default VPC {vpc_id}, {len(result['subnets'])} private subnets ready", flush=True)
    return result
