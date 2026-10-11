from unittest.mock import Mock

from fastapi.testclient import TestClient

from probe import api as api_module
from probe.api import app


def test_api_requires_login_by_default(monkeypatch):
    monkeypatch.delenv("LOCAL_DEV", raising=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/catalog").status_code == 401
        assert client.get("/api/analysis?start=0&end=3600").status_code == 401


def test_local_preview_never_fabricates_measurements(monkeypatch):
    monkeypatch.setenv("LOCAL_DEV", "1")
    monkeypatch.delenv("TABLE_NAME", raising=False)
    with TestClient(app) as client:
        response = client.get("/api/metrics?start=0&end=3600")
        assert response.status_code == 200
        assert response.json()["samples"] == 0
        assert response.json()["summary"] == []
        analysis = client.get("/api/analysis?start=0&end=3600")
        assert analysis.status_code == 200
        assert analysis.json()["status"] == "insufficient_data"
        assert analysis.json()["ai"] is None


def test_invalid_ranges_and_regions_are_rejected(monkeypatch):
    monkeypatch.setenv("LOCAL_DEV", "1")
    with TestClient(app) as client:
        assert client.get("/api/metrics?start=100&end=1").status_code == 400
        assert client.get("/api/metrics?start=0&end=4000000").status_code == 400
        assert client.get("/api/metrics?start=0&end=100&region=moon-1").status_code == 400
        assert client.get("/api/metrics?start=0&end=100&input_size=1000").status_code == 400
        assert client.get("/api/analysis?start=0&end=3600&dimension=invalid").status_code == 400


def test_removed_sydney_region_is_not_advertised_or_queryable(monkeypatch):
    monkeypatch.setenv("LOCAL_DEV", "1")
    monkeypatch.delenv("TABLE_NAME", raising=False)
    with TestClient(app) as client:
        regions = client.get("/api/config").json()["regions"]
        assert len(regions) == 6
        assert "ap-southeast-2" not in regions
        assert "ap-southeast-2" not in client.get("/api/catalog").json()["regions"]
        for endpoint in ("metrics", "analysis"):
            assert client.get(f"/api/{endpoint}?start=0&end=3600&region=ap-southeast-2").status_code == 400
            assert client.get(
                f"/api/{endpoint}?start=0&end=3600&region=us-east-1&region=ap-southeast-2",
            ).status_code == 400


def test_multiple_filter_values_use_or_within_and_between_filters(monkeypatch):
    monkeypatch.setenv("LOCAL_DEV", "1")
    monkeypatch.setenv("TABLE_NAME", "test")
    base = {"status": "ok", "scheduled_at": 600, "config_id": "v1",
            "endpoint": "runtime", "api": "converse", "input_size": 10,
            "provider": "Anthropic", "routing": "global", "ttft_ms": 100}
    rows = [
        {**base, "region": "us-east-1", "model_id": "model-a"},
        {**base, "region": "us-west-2", "model_id": "model-b", "input_size": 100},
        {**base, "region": "us-west-2", "model_id": "model-c"},
        {**base, "region": "us-west-2", "model_id": "model-a", "endpoint": "mantle"},
        {**base, "region": "us-west-2", "model_id": "model-a", "input_size": 1},
    ]
    store = Mock()
    store.samples.return_value = rows
    monkeypatch.setattr(api_module, "Store", lambda: store)
    with TestClient(app) as client:
        response = client.get("/api/metrics", params=[
            ("start", "0"), ("end", "3600"), ("region", "us-east-1"), ("region", "us-west-2"),
            ("model", "model-a"), ("model", "model-b"), ("endpoint", "runtime"),
            ("input_size", "10"), ("input_size", "100"),
        ])
    assert response.status_code == 200
    assert response.json()["samples"] == 2
    assert {row["model_id"] for row in response.json()["summary"]} == {"model-a", "model-b"}
    store.samples.assert_called_once_with(0, 3600, ["us-east-1", "us-west-2"])


def test_analysis_filter_order_does_not_create_duplicate_cache_scope(monkeypatch):
    monkeypatch.setenv("LOCAL_DEV", "1")
    monkeypatch.setenv("TABLE_NAME", "test")
    store = Mock()
    store.samples.return_value = []
    monkeypatch.setattr(api_module, "Store", lambda: store)
    cached = Mock(return_value={"status": "ready"})
    monkeypatch.setattr(api_module, "cached_analysis", cached)
    with TestClient(app) as client:
        for regions in ["region=us-west-2&region=us-east-1", "region=us-east-1&region=us-west-2&region=us-east-1"]:
            response = client.get("/api/analysis?start=0&end=3600&model=b&model=a&" + regions)
            assert response.status_code == 200
    scopes = [call.args[5] for call in cached.call_args_list]
    assert scopes[0] == scopes[1]
    assert scopes[0]["region"] == ["us-east-1", "us-west-2"]
    assert scopes[0]["model_id"] == ["a", "b"]


def test_metrics_exclude_manual_verification_by_default_without_deleting_it(monkeypatch):
    monkeypatch.setenv("LOCAL_DEV", "1")
    monkeypatch.setenv("TABLE_NAME", "test")
    store = Mock()
    store.samples.return_value = [
        {"scheduled_at": 600, "status": "ok"},
        {"scheduled_at": 601, "status": "error", "verification": True},
    ]
    monkeypatch.setattr(api_module, "Store", lambda: store)
    with TestClient(app) as client:
        scheduled = client.get("/api/metrics?start=0&end=3600").json()
        diagnostic = client.get("/api/metrics?start=0&end=3600&include_verification=true").json()
    assert scheduled["samples"] == 1
    assert scheduled["excluded_verification_samples"] == 1
    assert scheduled["failures"] == []
    assert scheduled["summary"][0]["success_rate"] == 1
    assert diagnostic["samples"] == 2 and len(diagnostic["failures"]) == 1
    assert diagnostic["health"]["failing"] == 0
