import json
from dataclasses import asdict
from unittest.mock import Mock

import pytest

from probe import jobs
from probe.config import target_apis
from probe.selection import default_policy


@pytest.mark.parametrize(("event", "expected_slot", "source"), [
    ({"scheduled_time": "<aws.scheduler.scheduled-time>"}, 1200, "invocation"),
    ({}, 1200, "invocation"),
    ({"scheduled_time": "1970-01-01T00:10:00Z"}, 600, "scheduler"),
])
def test_scheduler_delivery_creates_correctly_aligned_jobs(monkeypatch, event, expected_slot, source):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("QUEUE_URL", "https://example.invalid/jobs")
    monkeypatch.setenv("INPUT_SIZES", "[1,10,100]")
    monkeypatch.setattr(jobs.time, "time", lambda: 1215)
    targets = target_apis("us-east-1", "openai.gpt-6-astra", "mantle", "regional")[:1]
    monkeypatch.setattr(jobs, "discover", lambda region: (targets, []))
    store = Mock()
    store.policy.return_value = default_policy()
    monkeypatch.setattr(jobs, "Store", lambda: store)
    sqs = Mock()
    sqs.send_message_batch.return_value = {"Successful": [{"Id": str(i)} for i in range(3)]}
    monkeypatch.setattr(jobs.boto3, "client", lambda *args, **kwargs: sqs)

    assert jobs.planner(event, None)["jobs"] == 3
    sent = [json.loads(entry["MessageBody"]) for entry in sqs.send_message_batch.call_args.kwargs["Entries"]]
    assert {job["input_size"] for job in sent} == {1, 10, 100}
    assert {job["scheduled_at"] for job in sent} == {expected_slot}
    assert {job["schedule_time_source"] for job in sent} == {source}
    assert store.catalog.call_args.args[1]["enqueued_jobs"] == 3


@pytest.mark.parametrize("model", [
    "global.anthropic.claude-fable-5", "global.anthropic.claude-fable-5-1",
    "global.anthropic.claude-opus-5-5", "global.anthropic.claude-sonnet-5-5",
    "global.anthropic.claude-future-6", "global.openai.gpt-6-astra",
])
def test_mandatory_or_unverified_reasoning_is_not_disabled(model):
    target = target_apis("us-east-1", model, "runtime", "global")[0]
    job = jobs.make_job(target, 10, 600, 2048)
    assert job["reasoning"] == "model-default"


@pytest.mark.parametrize(("global_only", "expected_count"), [("true", 2), ("false", 5), (None, 5)])
def test_planner_applies_configured_fable_routing_restriction(monkeypatch, global_only, expected_count):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("QUEUE_URL", "https://example.invalid/jobs")
    monkeypatch.setenv("INPUT_SIZES", "[10]")
    if global_only is None:
        monkeypatch.delenv("FABLE_GLOBAL_ONLY", raising=False)
    else:
        monkeypatch.setenv("FABLE_GLOBAL_ONLY", global_only)
    monkeypatch.setattr(jobs.time, "time", lambda: 1215)
    targets = [
        *target_apis("us-east-1", "global.anthropic.claude-fable-5", "runtime", "global"),
        *target_apis("us-east-1", "us.anthropic.claude-fable-5", "runtime", "geographic"),
        *target_apis("us-east-1", "anthropic.claude-fable-5", "mantle", "regional"),
    ]
    monkeypatch.setattr(jobs, "discover", lambda region: (targets, []))
    store, sqs = Mock(), Mock()
    store.policy.return_value = default_policy()
    monkeypatch.setattr(jobs, "Store", lambda: store)
    sqs.send_message_batch.return_value = {"Successful": [{"Id": "0"}, {"Id": "1"}]}
    monkeypatch.setattr(jobs.boto3, "client", lambda *args, **kwargs: sqs)
    assert jobs.planner({}, None)["jobs"] == expected_count
    sent = [json.loads(entry["MessageBody"]) for entry in sqs.send_message_batch.call_args.kwargs["Entries"]]
    assert all(job["reasoning"] == "model-default" and job["region"] == "us-east-1" for job in sent)
    if global_only == "true":
        assert all(job["model_id"] == "global.anthropic.claude-fable-5" for job in sent)
    else:
        assert {job["routing"] for job in sent} == {"regional", "geographic", "global"}
        assert {job["endpoint"] for job in sent} == {"runtime", "mantle"}
    assert len(store.catalog.call_args.args[1]["targets"]) == expected_count


def test_paused_targets_remain_discoverable_but_do_not_enqueue(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("QUEUE_URL", "https://example.invalid/jobs")
    monkeypatch.setenv("INPUT_SIZES", "[1,10,100]")
    targets = target_apis("us-east-1", "openai.gpt-6-astra", "mantle", "regional")
    monkeypatch.setattr(jobs, "discover", lambda region: (targets, []))
    store, sqs = Mock(), Mock()
    store.policy.return_value = {**default_policy(), "revision": 1, "overrides": {targets[0].key: False}}
    sqs.send_message_batch.return_value = {}
    monkeypatch.setattr(jobs, "Store", lambda: store)
    monkeypatch.setattr(jobs.boto3, "client", lambda *args, **kwargs: sqs)
    assert jobs.planner({}, None)["jobs"] == 3
    sent = [json.loads(e["MessageBody"]) for e in sqs.send_message_batch.call_args.kwargs["Entries"]]
    assert {job["api"] for job in sent} == {targets[1].api}
    assert len(store.discovery.call_args.args[1]["targets"]) == 2
    assert store.catalog.call_args.args[1]["policy_revision"] == 1


def test_discovery_only_refresh_preserves_failed_surface_without_enqueuing(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    previous = [
        *target_apis("us-east-1", "openai.gpt-6-astra", "mantle", "regional"),
        *target_apis("us-east-1", "global.openai.gpt-6-astra", "runtime", "global"),
    ]
    store = Mock()
    store.discovery.return_value = {"targets": [asdict(t) for t in previous]}
    monkeypatch.setattr(jobs, "Store", lambda: store)
    monkeypatch.setattr(jobs, "discover", lambda r: ([], [{"surface": "mantle/v1/models", "error": "timeout"}]))
    client = Mock()
    monkeypatch.setattr(jobs.boto3, "client", client)
    result = jobs.planner({"action": "discover"}, None)
    assert result["targets"] == 2
    assert all(t["endpoint"] == "mantle" for t in store.discovery.call_args.args[1]["targets"])
    store.catalog.assert_not_called()
    store.policy.assert_not_called()
    client.assert_not_called()
