import json
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from probe import jobs
from probe.config import target_apis
from probe.storage import Store


def sample_job():
    return jobs.make_job(target_apis("us-east-1", "openai.gpt-6-astra", "mantle", "regional")[0], 10, 600, 512)


def duplicate_table(old):
    table = Mock()
    table.put_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    table.get_item.return_value = {"Item": old}
    return table


def test_finished_duplicate_never_invokes_again(monkeypatch):
    store = Mock()
    store.claim.return_value = False
    monkeypatch.setattr(jobs, "Store", lambda: store)
    probe = Mock()
    monkeypatch.setattr(jobs, "run", probe)
    result = jobs.worker({"Records": [{"body": json.dumps(sample_job()), "messageId": "1"}]}, None)
    assert result == {"batchItemFailures": []}
    probe.assert_not_called()


def test_expired_claim_records_worker_timeout_without_second_inference(monkeypatch):
    monkeypatch.setattr("probe.storage.time.time", lambda: 1000)
    store = Store.__new__(Store)
    store.table = duplicate_table({"status": "running", "claimed_at": 600})
    assert store.claim(sample_job()) is False
    assert "worker_timeout" in store.table.update_item.call_args.kwargs["ExpressionAttributeValues"].values()


def test_stale_jobs_are_not_measured(monkeypatch):
    store = Mock()
    store.claim.return_value = True
    monkeypatch.setattr(jobs, "Store", lambda: store)
    monkeypatch.setattr(jobs.time, "time", lambda: 2000)
    probe = Mock()
    monkeypatch.setattr(jobs, "run", probe)
    result = jobs.worker({"Records": [{"body": json.dumps(sample_job()), "messageId": "1"}]}, None)
    assert result == {"batchItemFailures": []}
    assert store.finish.call_args.args[1]["status"] == "stale"
    probe.assert_not_called()


def test_storage_failure_returns_sqs_partial_batch_failure(monkeypatch):
    store = Mock()
    store.claim.side_effect = RuntimeError("DynamoDB unavailable")
    monkeypatch.setattr(jobs, "Store", lambda: store)
    result = jobs.worker({"Records": [{"body": json.dumps(sample_job()), "messageId": "1"}]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "1"}]}


@pytest.mark.parametrize("reasoning", ["model-default", "disabled"])
def test_worker_honors_queued_reasoning_configuration(monkeypatch, reasoning):
    target = target_apis("us-east-1", "global.anthropic.claude-opus-5", "runtime", "global")[0]
    job = jobs.make_job(target, 10, 600, 2048, reasoning)
    store = Mock()
    store.claim.return_value = True
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(jobs, "Store", lambda: store)
    monkeypatch.setattr(jobs.time, "time", lambda: 620)
    probe = Mock(return_value={"status": "ok"})
    monkeypatch.setattr(jobs, "run", probe)
    result = jobs.worker({"Records": [{"body": json.dumps(job), "messageId": "1"}]}, None)
    assert result == {"batchItemFailures": []}
    assert probe.call_args.kwargs["reasoning"] == reasoning
    assert store.finish.call_args.args[0]["config_id"] == job["config_id"]


@pytest.mark.parametrize("version", ["repeat-v1", "short-64-v2", None])
def test_worker_uses_the_queued_prompt_version(monkeypatch, version):
    job = sample_job()
    if version is None:
        job.pop("prompt_version")
    else:
        job["prompt_version"] = version
    store = Mock()
    store.claim.return_value = True
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(jobs, "Store", lambda: store)
    monkeypatch.setattr(jobs.time, "time", lambda: 620)
    probe = Mock(return_value={"status": "ok"})
    monkeypatch.setattr(jobs, "run", probe)
    assert jobs.worker({"Records": [{"body": json.dumps(job), "messageId": "1"}]}, None) == {"batchItemFailures": []}
    expected = ("Reply in exactly 64 words about cloud computing." if version == "short-64-v2"
                else "Write a story about a journey through outer space.")
    assert probe.call_args.args[1] == expected
