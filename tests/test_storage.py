from decimal import Decimal
from unittest.mock import Mock

import pytest

from probe.storage import Store


def test_queries_follow_dynamodb_pagination():
    store = Store.__new__(Store)
    store.table = Mock()
    store.table.query.side_effect = [
        {"Items": [{"value": 1}], "LastEvaluatedKey": {"pk": "p", "sk": "s"}},
        {"Items": [{"value": 2}]},
    ]
    assert store.samples(0, 100, ["us-east-1"]) == [{"value": 1}, {"value": 2}]
    assert store.table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"pk": "p", "sk": "s"}


def test_sample_cap_is_global_across_partitions():
    store = Store.__new__(Store)
    store.table = Mock()
    store.table.query.return_value = {"Items": [{"value": 1}, {"value": 2}]}
    with pytest.raises(ValueError, match="shorter range"):
        store.samples(0, 100, ["us-east-1", "us-east-2"], limit=3)


def test_policy_reads_are_consistent_and_writes_compare_revision():
    store = Store.__new__(Store)
    store.table = Mock()
    store.table.get_item.return_value = {}
    assert store.policy() == {"revision": 0, "auto_add": True, "overrides": {}}
    assert store.table.get_item.call_args.kwargs["ConsistentRead"] is True
    store.save_policy({"revision": 1}, 0)
    assert store.table.put_item.call_args.kwargs["ConditionExpression"] == "attribute_not_exists(pk)"
    store.save_policy({"revision": 2}, 1)
    assert store.table.put_item.call_args.kwargs["ExpressionAttributeValues"] == {":revision": 1}


def test_persisted_policy_revision_can_round_trip_into_strict_api_request():
    from probe.management import PolicyChange
    store = Store.__new__(Store)
    store.table = Mock()
    store.table.get_item.return_value = {"Item": {"revision": Decimal(2), "auto_add": True, "overrides": {}}}
    policy = store.policy()
    assert type(policy["revision"]) is int
    assert PolicyChange(revision=policy["revision"]).revision == 2
