import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from probe.auth import administrator, authorize, can_manage
from probe.config import REGIONS, Target, eligible, probe_interval_seconds, select_targets
from probe.selection import default_policy, enabled
from probe.storage import Store

router = APIRouter(prefix="/api/management")


def snapshots(store):
    def load(region):
        scheduled = store.catalog(region)
        discovered = store.discovery(region) or scheduled
        return region, {"discovery": discovered, "schedule": scheduled}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(pool.map(load, REGIONS))


def candidates(regional):
    result = {}
    for region, value in regional.items():
        for data in value["discovery"].get("targets", []):
            target = Target(**{key: data[key] for key in Target.__dataclass_fields__})
            if target.region == region and eligible(target.model_id):
                # Apply the same optional routing restriction as the planners.
                if select_targets([target], fable_global_only=os.getenv("FABLE_GLOBAL_ONLY", "false").lower() == "true"):
                    result[target.key] = target
    return result


@router.get("")
def management(claims=Depends(authorize)):
    local = os.getenv("LOCAL_DEV") == "1" and not os.getenv("TABLE_NAME")
    store = None if local else Store()
    regional = snapshots(store) if store else {r: {"discovery": {}, "schedule": {}} for r in REGIONS}
    policy = store.policy() if store else default_policy()
    targets = []
    for key, target in candidates(regional).items():
        value = regional[target.region]
        scheduled = {Target(**t).key for t in value["schedule"].get("targets", [])}
        targets.append({**asdict(target), "key": key, "enabled": enabled(target, policy),
                        "scheduled": key in scheduled, "discovered_at": value["discovery"].get("discovered_at"),
                        "scheduled_at": value["schedule"].get("scheduled_at")})
    return {"can_manage": can_manage(claims) and not local, "policy": policy, "regions": regional,
            "targets": sorted(targets, key=lambda t: (t["model_id"], t["region"], t["endpoint"], t["api"])),
            "next_schedule_at": (int(time.time()) // probe_interval_seconds() + 1) * probe_interval_seconds(),
            "schedule_seconds": probe_interval_seconds(),
            "max_targets_per_region": int(os.getenv("MAX_TARGETS", "100")),
            "input_sizes": json.loads(os.getenv("INPUT_SIZES", "[10]"))}


class PolicyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    changes: dict[str, StrictBool] = Field(default_factory=dict, max_length=2000)
    auto_add: StrictBool | None = None


@router.patch("/policy")
def change_policy(change: PolicyChange, claims=Depends(administrator)):
    if not os.getenv("TABLE_NAME"):
        raise HTTPException(503, "Management changes require a deployed database")
    store = Store()
    policy = store.policy()
    if change.revision != policy["revision"]:
        raise HTTPException(409, "Selections changed in another session. Reload Management and try again.")
    known = candidates(snapshots(store))
    if any(key not in known for key in change.changes):
        raise HTTPException(400, "Unknown or unavailable probe target. Discover the latest models and try again.")
    overrides = dict(policy["overrides"])
    auto_add = policy["auto_add"]
    if change.auto_add is not None and change.auto_add != auto_add:
        # This setting affects future discoveries, not existing probe membership.
        overrides.update({key: enabled(target, policy) for key, target in known.items()})
        auto_add = change.auto_add
    overrides.update(change.changes)
    updated = {"revision": policy["revision"] + 1, "auto_add": auto_add, "overrides": overrides,
               "updated_at": int(time.time()), "updated_by": claims["sub"]}
    limit = int(os.getenv("MAX_TARGETS", "100"))
    for region in REGIONS:
        if sum(t.region == region and enabled(t, updated) for t in known.values()) > limit:
            raise HTTPException(400, f"The selection exceeds the {limit}-target limit in {region}")
    try:
        store.save_policy(updated, change.revision)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(409, "Selections changed in another session. Reload Management and try again.") from None
        raise
    interval = probe_interval_seconds()
    return {"policy": updated, "next_schedule_at": (int(time.time()) // interval + 1) * interval}


@router.post("/discover", status_code=202)
def discover_latest(claims=Depends(administrator)):
    if not os.getenv("TABLE_NAME"):
        raise HTTPException(503, "Discovery requires a deployed database")
    now = int(time.time())
    try:
        Store().claim_discovery_refresh(now, claims["sub"])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(429, "Discovery was requested recently. Wait one minute before requesting again.") from None
        raise

    def invoke(region):
        try:
            client = boto3.client("lambda", region_name=region, config=Config(
                connect_timeout=3, read_timeout=8, retries={"total_max_attempts": 1},
            ))
            client.invoke(FunctionName=os.getenv("PLANNER_FUNCTION", "bedrock-probe-planner"),
                          InvocationType="Event", Payload=json.dumps({"action": "discover"}).encode())
            return region, {"accepted": True}
        except Exception:
            return region, {"accepted": False, "error": "Could not start discovery. Retry after one minute."}
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = dict(pool.map(invoke, REGIONS))
    return {"requested_at": now, "regions": results}
