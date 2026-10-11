from dataclasses import asdict
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from probe import management
from probe.api import app
from probe.auth import authorize
from probe.config import REGIONS, target_apis
from probe.selection import default_policy, enabled


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "test")
    monkeypatch.delenv("LOCAL_DEV", raising=False)
    store = Mock()
    store.policy.return_value = default_policy()
    targets = target_apis("us-east-1", "global.openai.gpt-6-astra", "runtime", "global")
    store.catalog.return_value = {}
    store.discovery.side_effect = lambda r: {"targets": [asdict(t) for t in targets if t.region == r]}
    monkeypatch.setattr(management, "Store", lambda: store)
    app.dependency_overrides[authorize] = lambda: {"sub": "admin", "cognito:groups": ["probe-admins"]}
    with TestClient(app) as client:
        yield client, store, targets
    app.dependency_overrides.clear()


def test_management_requires_login_and_writes_require_admin(setup):
    client, store, _ = setup
    app.dependency_overrides.clear()
    assert client.get("/api/management").status_code == 401
    assert client.post("/api/management/discover").status_code == 401
    app.dependency_overrides[authorize] = lambda: {"sub": "viewer"}
    assert client.get("/api/management").json()["can_manage"] is False
    assert client.patch("/api/management/policy", json={"revision": 0}).status_code == 403
    assert client.post("/api/management/discover").status_code == 403
    store.save_policy.assert_not_called()


def test_change_selection_and_reject_stale_revision(setup):
    client, store, targets = setup
    response = client.patch("/api/management/policy", json={"revision": 0, "changes": {targets[0].key: False}})
    assert response.status_code == 200
    policy = response.json()["policy"]
    assert not enabled(targets[0], policy) and enabled(targets[1], policy)
    assert policy["revision"] == 1 and policy["updated_by"] == "admin"
    store.policy.return_value = policy
    assert client.patch("/api/management/policy", json={"revision": 0}).status_code == 409
    store.save_policy.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    assert client.patch("/api/management/policy", json={"revision": 1}).status_code == 409


def test_auto_add_only_changes_future_discoveries(setup):
    client, store, targets = setup
    store.policy.return_value["overrides"] = {targets[0].key: False}
    future = target_apis("us-east-1", "global.openai.gpt-7-astra", "runtime", "global")[0]
    policy = client.patch("/api/management/policy", json={"revision": 0, "auto_add": False}).json()["policy"]
    assert not enabled(targets[0], policy) and enabled(targets[1], policy)
    assert not enabled(future, policy)
    store.policy.return_value = policy
    policy = client.patch("/api/management/policy", json={"revision": 1, "auto_add": True}).json()["policy"]
    assert not enabled(targets[0], policy) and enabled(targets[1], policy) and enabled(future, policy)


def test_only_discovered_permitted_targets_can_be_enabled(setup, monkeypatch):
    monkeypatch.setenv("FABLE_GLOBAL_ONLY", "true")
    client, store, targets = setup
    forbidden = target_apis("us-east-1", "anthropic.claude-fable-5", "mantle", "regional")[0]
    targets.append(forbidden)
    value = client.get("/api/management").json()
    assert forbidden.key not in {t["key"] for t in value["targets"]}
    assert "ap-southeast-2" not in value["regions"]
    for key in ("unknown", forbidden.key):
        assert client.patch("/api/management/policy", json={"revision": 0, "changes": {key: True}}).status_code == 400
    assert client.patch("/api/management/policy",
                        json={"revision": 0, "changes": {targets[0].key: "false"}}).status_code == 422
    store.save_policy.assert_not_called()


def test_all_discovered_routing_modes_are_enabled_by_default(setup, monkeypatch):
    client, _, targets = setup
    monkeypatch.delenv("FABLE_GLOBAL_ONLY", raising=False)
    targets.extend([
        *target_apis("us-east-1", "anthropic.claude-fable-5", "mantle", "regional"),
        *target_apis("us-east-1", "us.anthropic.claude-fable-5", "runtime", "geographic"),
        *target_apis("us-east-1", "global.anthropic.claude-fable-5", "runtime", "global"),
    ])
    rows = client.get("/api/management").json()["targets"]
    assert {t["key"] for t in rows} == {t.key for t in targets}
    assert all(t["enabled"] for t in rows)
    assert {t["routing"] for t in rows} == {"regional", "geographic", "global"}
    response = client.patch("/api/management/policy", json={
        "revision": 0, "changes": {t.key: True for t in targets},
    })
    assert response.status_code == 200


def test_per_region_target_budget_cannot_be_bypassed(setup, monkeypatch):
    client, store, _ = setup
    monkeypatch.setenv("MAX_TARGETS", "1")
    assert client.patch("/api/management/policy", json={"revision": 0}).status_code == 400
    store.save_policy.assert_not_called()


def test_refresh_invokes_only_discovery_in_six_regions_and_rate_limits(setup, monkeypatch):
    client, store, _ = setup
    lambdas = {region: Mock() for region in REGIONS}
    monkeypatch.setattr(management.boto3, "client", lambda _, region_name, **kw: lambdas[region_name])
    value = client.post("/api/management/discover")
    assert value.status_code == 202
    assert all(r["accepted"] for r in value.json()["regions"].values())
    for function in lambdas.values():
        assert function.invoke.call_args.kwargs["Payload"] == b'{"action": "discover"}'
        assert function.invoke.call_args.kwargs["InvocationType"] == "Event"
    store.claim_discovery_refresh.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    assert client.post("/api/management/discover").status_code == 429
    assert all(function.invoke.call_count == 1 for function in lambdas.values())


def test_partial_dispatch_is_reported_per_region(setup, monkeypatch):
    client, _, _ = setup
    function = Mock()
    function.invoke.side_effect = RuntimeError("unavailable")
    monkeypatch.setattr(management.boto3, "client", lambda *a, **kw: function)
    value = client.post("/api/management/discover").json()
    assert len(value["regions"]) == 6
    assert all(not region["accepted"] and region["error"] for region in value["regions"].values())
