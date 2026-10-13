import hashlib
import json
import os
import signal
import time
from dataclasses import asdict
from datetime import datetime

import boto3

from probe.adapters import run
from probe.config import (
    DEFAULT_MAX_OUTPUT,
    PROMPT_VERSION,
    SIZES,
    Target,
    probe_interval_seconds,
    prompt,
    select_targets,
)
from probe.discovery import discover, snapshot
from probe.selection import enabled
from probe.storage import Store


def make_job(target: Target, size: int, slot: int, max_output: int, anthropic_thinking="disabled"):
    # Only these affected model versions have verified opt-out support. Opus 5.5,
    # Sonnet 5.5 and Fable require adaptive thinking; future versions keep defaults.
    model_name = target.model_id.split("anthropic.")[-1].split(":")[0]
    supports_disabled = model_name in ("claude-opus-5", "claude-haiku-5-5")
    config = {"max_output": max_output, "prompt_version": PROMPT_VERSION,
              "schedule_seconds": probe_interval_seconds(),
              "reasoning": anthropic_thinking if target.provider == "Anthropic" and supports_disabled else "model-default",
              "service_tier": "default"}
    config_id = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
    job_id = hashlib.sha256(f"{slot}/{target.key}/{size}/{config_id}".encode()).hexdigest()
    return {**asdict(target), **config, "config_id": config_id, "input_size": size,
            "scheduled_at": slot, "job_id": job_id}


def planner(event, context):
    region = os.environ["AWS_REGION"]
    scheduled = event.get("scheduled_time")
    # Some Scheduler deliveries retain the context token literally. Such a token
    # contains no timestamp; use the invocation's UTC slot and record its source.
    unresolved = scheduled == "<aws.scheduler.scheduled-time>"
    source = "invocation" if not scheduled or unresolved else "scheduler"
    now = (int(datetime.fromisoformat(scheduled.replace("Z", "+00:00")).timestamp())
           if source == "scheduler" else int(time.time()))
    if unresolved:
        print(json.dumps({"warning": "Scheduler time token was not expanded",
                          "schedule_time_source": source}))
    interval = probe_interval_seconds()
    slot = now // interval * interval
    targets, issues = discover(region)
    store = Store()
    # Preserve known targets only on a surface whose discovery failed. A partial
    # list must not silently remove existing probes or management selections.
    if issues:
        previous = store.discovery(region) or store.catalog(region)
        failed = {issue["surface"] for issue in issues}
        for value in previous.get("targets", []):
            target = Target(**value)
            surface = ("mantle/v1/models" if target.endpoint == "mantle" else
                       "runtime/models" if target.routing == "regional" else "runtime/profiles")
            if surface in failed:
                targets.append(target)
        targets = list({target.key: target for target in targets}.values())
    targets = select_targets(
        targets, os.getenv("MODEL_ID_REGEX", ""),
        tuple(json.loads(os.getenv("APIS", "[]"))),
        tuple(json.loads(os.getenv("ROUTING_MODES", "[]"))),
        fable_global_only=os.getenv("FABLE_GLOBAL_ONLY", "false").lower() == "true",
    )
    discovered = snapshot(targets, issues, int(time.time()))
    store.discovery(region, discovered)
    if event.get("action") == "discover":
        return {"targets": len(targets), "issues": issues, "discovered_at": discovered["discovered_at"]}
    policy = store.policy()
    targets = [target for target in targets if enabled(target, policy)]
    max_targets = int(os.getenv("MAX_TARGETS", "100"))
    if len(targets) > max_targets:
        issues.append({"surface": "planner", "error": f"{len(targets)} targets exceeds MAX_TARGETS={max_targets}; no jobs sent"})
        store.catalog(region, snapshot(targets, issues, int(time.time())))
        raise ValueError("Target budget exceeded; raise MAX_TARGETS or narrow discovery")
    sizes = json.loads(os.getenv("INPUT_SIZES", "[10]"))
    if not sizes or any(size not in SIZES for size in sizes):
        raise ValueError("INPUT_SIZES must be a nonempty subset of [1,10,100]")
    max_output = int(os.getenv("MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT)))
    thinking = os.getenv("ANTHROPIC_THINKING", "disabled")
    if thinking not in ("disabled", "model-default"):
        raise ValueError("ANTHROPIC_THINKING must be disabled or model-default")
    jobs = [make_job(target, size, slot, max_output, thinking) for target in targets for size in sizes]
    for job in jobs:
        job["schedule_time_source"] = source
    catalog = snapshot(targets, issues, int(time.time()))
    catalog.update(scheduled_at=slot, schedule_time_source=source, planned_jobs=len(jobs), enqueued_jobs=0,
                   policy_revision=policy["revision"], discovered_targets=len(discovered["targets"]),
                   schedule_seconds=interval, input_sizes=sizes, prompt_version=PROMPT_VERSION)
    store.catalog(region, catalog)
    sqs = boto3.client("sqs", region_name=region)
    for offset in range(0, len(jobs), 10):
        batch = jobs[offset:offset + 10]
        result = sqs.send_message_batch(
            QueueUrl=os.environ["QUEUE_URL"],
            Entries=[{"Id": str(i), "MessageBody": json.dumps(job)} for i, job in enumerate(batch)],
        )
        if result.get("Failed"):
            raise RuntimeError(f"SQS rejected jobs: {result['Failed']}")
        catalog["enqueued_jobs"] += len(batch)
    store.catalog(region, catalog)
    print(json.dumps({"region": region, "scheduled_at": slot, "jobs": len(jobs), "issues": len(issues)}))
    return {"jobs": len(jobs), "issues": issues}


def deadline(signum, frame):
    raise TimeoutError("120-second probe deadline exceeded")


def worker(event, context):
    store = Store()
    failures = []
    for record in event["Records"]:
        try:
            job = json.loads(record["body"])
            if not store.claim(job):
                continue
            if time.time() - job["scheduled_at"] > 600:
                store.finish(job, {"status": "stale", "error": "Queue delay exceeded the ten-minute freshness limit"})
                continue
            target = Target(**{name: job[name] for name in Target.__dataclass_fields__})
            previous = signal.signal(signal.SIGALRM, deadline)
            signal.alarm(120)
            try:
                started_at = time.time()
                result = run(target, prompt(job["input_size"], job.get("prompt_version", "repeat-v1")), job["max_output"],
                             reasoning=job.get("reasoning", "model-default"))
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous)
            started_at = result.get("request_started_at", started_at)
            result.update(started_at=started_at, queue_delay_ms=(started_at - job["scheduled_at"]) * 1000,
                          worker_region=os.environ["AWS_REGION"])
            store.finish(job, result)
            print(json.dumps({"job_id": job["job_id"], "status": result["status"],
                              "region": target.region, "endpoint": target.endpoint}))
        except Exception as exc:
            print(json.dumps({"delivery_error": type(exc).__name__, "message_id": record["messageId"]}))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
