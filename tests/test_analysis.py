import json
from unittest.mock import Mock

from botocore.exceptions import ClientError

from probe import analysis


def sample(slot, latency=100, **overrides):
    return {
        "region": "us-east-1", "model_id": "global.anthropic.claude-haiku-5-5",
        "provider": "Anthropic", "endpoint": "runtime", "api": "converse", "routing": "global",
        "input_size": 10, "config_id": "same", "max_output": 2048,
        "scheduled_at": slot, "status": "ok", "ttft_ms": latency, "ttot_ms": latency * 10,
        "output_tps": 50, **overrides,
    }


def test_pending_and_policy_failures_are_distinguished_from_success():
    rows = [sample(0), sample(600, status="running"), sample(
        1200, status="error", error="data retention mode 'default' is not available for this model")]
    evidence = analysis.build_evidence(rows, 0, 1800, 600, "region", {})
    assert evidence["overall"]["completed"] == 2
    assert evidence["overall"]["pending"] == 1
    assert evidence["overall"]["success_rate"] == .5
    assert evidence["overall"]["policy_failures"] == 1
    assert evidence["overall"]["ttft_ms"]["n"] == 1


def test_trend_compares_the_same_configuration_across_time():
    rows = [sample(i * 600, 100 if i < 3 else 400) for i in range(6)]
    evidence = analysis.build_evidence(rows, 0, 3600, 600, "model_id", {})
    assert evidence["comparable_cohorts"] == 1
    trend = evidence["comparisons"][0]
    assert trend["ttft_p50_change_pct"] == 300
    assert trend["sample_strength"] == "limited"
    assert trend["before"]["completed"] == 3
    assert trend["after"]["completed"] == 3


def test_output_budget_changes_cannot_be_mistaken_for_a_latency_regression():
    rows = [sample(i * 600, 100 if i < 3 else 900,
                   config_id="old" if i < 3 else "new", max_output=512 if i < 3 else 2048)
            for i in range(6)]
    evidence = analysis.build_evidence(rows, 0, 3600, 600, "region", {})
    assert evidence["comparisons"] == []
    assert evidence["output_budgets"] == [512, 2048]
    assert any("different output budgets" in note for note in evidence["observations"])


def test_other_inputs_and_models_are_not_paired_for_trend():
    rows = [sample(i * 600, input_size=10 if i < 3 else 100) for i in range(6)]
    assert not analysis.build_evidence(rows, 0, 3600, 600, "region", {})["comparisons"]


def test_gaps_only_cover_observed_span_and_exclude_manual_verification():
    rows = [sample(1200), sample(1800, verification=True), sample(2400)]
    evidence = analysis.build_evidence(rows, 0, 3600, 600, "region", {})
    assert evidence["overall"]["samples"] == 2
    assert evidence["coverage"]["internal_missing_slots"] == 1
    assert evidence["coverage"]["gap_to_window_end_ms"] == 600000


def test_hourly_samples_do_not_create_ten_minute_gaps():
    rows = [sample(slot, schedule_seconds=3600, prompt_version="short-64-v2") for slot in (0, 3600, 7200)]
    evidence = analysis.build_evidence(rows, 0, 10800, 3600, "region", {})
    assert evidence["coverage"]["internal_missing_slots"] == 0
    assert evidence["coverage"]["gap_to_window_end_ms"] == 0
    missing = analysis.build_evidence([rows[0], rows[2]], 0, 10800, 3600, "region", {})
    assert missing["coverage"]["internal_missing_slots"] == 1


def test_story_and_short_response_samples_are_not_paired_as_performance_trends():
    rows = [sample(i * 3600, config_id="old" if i < 3 else "short",
                   prompt_version="repeat-v1" if i < 3 else "short-64-v2", schedule_seconds=3600)
            for i in range(6)]
    evidence = analysis.build_evidence(rows, 0, 21600, 3600, "region", {})
    assert not evidence["comparisons"]
    assert any("story and short-response" in note for note in evidence["observations"])


def test_no_data_does_not_report_stability_or_invoke_model(monkeypatch):
    store = Mock()
    store.table.get_item.return_value = {}
    summarize = Mock()
    monkeypatch.setattr(analysis, "summarize_with_model", summarize)
    result = analysis.cached_analysis(store, 0, 3600, 600, "region", {}, lambda: [])
    assert result["status"] == "insufficient_data"
    assert result["ai"] is None
    assert result["evidence"]["overall"]["success_rate"] is None
    summarize.assert_not_called()
    store.table.update_item.assert_not_called()


def test_cache_and_shared_lease_prevent_duplicate_model_calls(monkeypatch):
    monkeypatch.setattr(analysis.time, "time", lambda: 1000)
    store = Mock()
    store.table.get_item.return_value = {}
    summarize = Mock(return_value={"text": "Conclusion grounded in observed statistics", "model_id": "test", "partial": False})
    monkeypatch.setattr(analysis, "summarize_with_model", summarize)
    rows = [sample(i * 600) for i in range(6)]
    result = analysis.cached_analysis(store, 0, 3600, 600, "region", {}, lambda: rows)
    assert result["status"] == "ready"
    store.table.get_item.return_value = {"Item": {"expires_at": 1600, "payload": json.dumps(result)}}
    cached = analysis.cached_analysis(store, 0, 3600, 600, "region", {}, Mock())
    assert cached["cached"] is True
    summarize.assert_called_once()
    store.table.get_item.return_value = {"Item": {"state": "building", "lease_until": 1100}}
    pending = analysis.cached_analysis(store, 0, 3600, 600, "region", {}, Mock())
    assert pending["status"] == "pending"
    summarize.assert_called_once()


def test_shared_call_budget_preserves_statistics_without_fake_ai(monkeypatch):
    store = Mock()
    store.table.get_item.return_value = {}
    store.table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    summarize = Mock()
    monkeypatch.setattr(analysis, "summarize_with_model", summarize)
    result = analysis.cached_analysis(
        store, 0, 3600, 600, "region", {}, lambda: [sample(i * 600) for i in range(6)])
    assert result["status"] == "limited"
    assert result["evidence"]["overall"]["successes"] == 6
    assert result["ai"] is None
    summarize.assert_not_called()


def test_ai_failure_leaves_honest_statistical_observations(monkeypatch):
    store = Mock()
    store.table.get_item.return_value = {}
    monkeypatch.setattr(analysis, "summarize_with_model", Mock(side_effect=RuntimeError("unavailable")))
    result = analysis.cached_analysis(
        store, 0, 3600, 600, "region", {}, lambda: [sample(i * 600) for i in range(6)])
    assert result["status"] == "unavailable"
    assert result["ai"] is None
    assert result["evidence"]["observations"]


def test_different_model_defaults_do_not_claim_reasoning_changed_over_time():
    rows = [sample(600, model_id="opus", reasoning="disabled"),
            sample(600, model_id="fable", reasoning="model-default")]
    evidence = analysis.build_evidence(rows, 0, 1800, 600, "model_id", {})
    assert not any("Reasoning settings changed" in note for note in evidence["observations"])
    rows.append(sample(1200, model_id="opus", config_id="new", reasoning="model-default"))
    evidence = analysis.build_evidence(rows, 0, 1800, 600, "model_id", {})
    assert any("Reasoning settings changed" in note for note in evidence["observations"])


def test_recent_recovery_is_separate_from_historical_window_statistics():
    old = [sample(600, status="error", error="data retention mode default")]
    current = analysis.latest_health([*old, sample(1800)])
    evidence = analysis.build_evidence(old, 0, 1800, 600, "region", {}, current)
    assert evidence["overall"]["policy_failures"] == 1
    assert evidence["current_health"]["failing"] == 0
    assert evidence["current_health"]["recovered"] == 1
    assert any("historical analysis window" in note for note in evidence["observations"])


def test_recovery_invalidates_ai_cache_within_the_same_time_window(monkeypatch):
    monkeypatch.setattr(analysis, "summarize_with_model",
                        Mock(return_value={"text": "Observed recovery", "partial": False}))
    store = Mock()
    store.table.get_item.return_value = {}
    rows = [sample(slot, status="error") for slot in (0, 600, 1200)]
    for current_rows in (rows, [*rows, sample(1800)]):
        analysis.cached_analysis(store, 0, 1800, 600, "region", {}, lambda: rows,
                                 current_health=analysis.latest_health(current_rows))
    keys = [call.kwargs["Key"] for call in store.table.get_item.call_args_list]
    assert keys[0] != keys[1]
