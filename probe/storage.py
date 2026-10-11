import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event, Lock

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from probe.selection import default_policy


def decimalize(value):
    return json.loads(json.dumps(value), parse_float=Decimal)


def plain(value):
    return json.loads(json.dumps(value, default=lambda x: float(x) if isinstance(x, Decimal) else str(x)))


class Store:
    def __init__(self):
        self.table = boto3.resource(
            "dynamodb", region_name=os.environ.get("HOME_REGION", "us-east-1"),
        ).Table(os.environ["TABLE_NAME"])

    def catalog(self, region, value=None):
        key = {"pk": f"CATALOG#{region}", "sk": "LATEST"}
        if value is not None:
            self.table.put_item(Item=decimalize({**key, **value}))
        else:
            return plain(self.table.get_item(Key=key).get("Item", {}))

    def discovery(self, region, value=None):
        key = {"pk": f"DISCOVERY#{region}", "sk": "LATEST"}
        if value is not None:
            self.table.put_item(Item=decimalize({**key, **value}))
        else:
            return plain(self.table.get_item(Key=key, ConsistentRead=True).get("Item", {}))

    def policy(self):
        item = self.table.get_item(
            Key={"pk": "MANAGEMENT#POLICY", "sk": "LATEST"}, ConsistentRead=True,
        ).get("Item")
        value = plain(item) if item else default_policy()
        # Keep the revision an integer on the wire so non-JavaScript clients can
        # round-trip GET responses into the strictly validated PATCH request.
        value["revision"] = int(value["revision"])
        return value

    def save_policy(self, value, revision):
        self.table.put_item(
            Item=decimalize({"pk": "MANAGEMENT#POLICY", "sk": "LATEST", **value}),
            ConditionExpression="attribute_not_exists(pk)" if revision == 0 else "revision = :revision",
            **({} if revision == 0 else {"ExpressionAttributeValues": {":revision": revision}}),
        )

    def claim_discovery_refresh(self, now, actor):
        self.table.put_item(
            Item={"pk": "MANAGEMENT#DISCOVERY", "sk": "LATEST", "requested_at": now,
                  "next_allowed_at": now + 60, "requested_by": actor},
            ConditionExpression="attribute_not_exists(pk) OR next_allowed_at <= :now",
            ExpressionAttributeValues={":now": now},
        )

    @staticmethod
    def key(job):
        date = datetime.fromtimestamp(job["scheduled_at"], UTC).strftime("%Y-%m-%d")
        return {"pk": f"SAMPLES#{job['region']}#{date}",
                "sk": f"{job['scheduled_at']:010d}#{job['job_id']}"}

    def claim(self, job):
        now = int(time.time())
        item = {**job, **self.key(job), "status": "running", "claimed_at": now,
                "expires_at": now + int(os.getenv("RETENTION_DAYS", "90")) * 86400}
        try:
            self.table.put_item(Item=decimalize(item),
                                ConditionExpression="attribute_not_exists(pk)")
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            old = self.table.get_item(Key=self.key(job), ConsistentRead=True).get("Item", {})
            if old.get("status") == "running":
                if now - int(old["claimed_at"]) < 180:
                    raise RuntimeError("An earlier delivery still owns this probe")
                # A process died after claiming. Preserve one measured attempt; never repeat inference.
                self.finish(job, {"status": "worker_timeout", "error": "Worker exited without a final result"})
            return False

    def finish(self, job, result):
        # Update preserves claim, TTL and job fields.
        names = {f"#k{i}": k for i, k in enumerate(result)}
        values = {f":v{i}": decimalize(v) for i, v in enumerate(result.values())}
        self.table.update_item(
            Key=self.key(job),
            UpdateExpression="SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(result))),
            ExpressionAttributeNames=names, ExpressionAttributeValues=values,
        )

    def samples(self, start: int, end: int, regions: list[str], limit=100_000):
        days = []
        day = datetime.fromtimestamp(start, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        while day.timestamp() < end:
            days.extend((region, day.strftime("%Y-%m-%d")) for region in regions)
            day += timedelta(days=1)
        stopped = Event()
        lock = Lock()
        read_count = 0

        def partition(pair):
            nonlocal read_count
            region, date = pair
            items, token = [], None
            while True:
                if stopped.is_set():
                    return []
                response = self.table.query(
                    KeyConditionExpression=Key("pk").eq(f"SAMPLES#{region}#{date}")
                    & Key("sk").between(f"{start:010d}#", f"{end - 1:010d}#~"),
                    Limit=1000,
                    **({"ExclusiveStartKey": token} if token else {}),
                )
                with lock:
                    read_count += len(response["Items"])
                    if read_count > limit:
                        stopped.set()
                        raise ValueError("Query exceeds 100,000 samples; select a shorter range or one region")
                items.extend(response["Items"])
                token = response.get("LastEvaluatedKey")
                if not token:
                    return items

        rows = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            for items in executor.map(partition, days):
                rows.extend(items)
        return plain(rows)
