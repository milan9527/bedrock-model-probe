import pytest

from probe.adapters import consume, sse
from probe.metrics import Measurement, aggregate, latest_health


def test_metadata_and_reasoning_are_not_first_visible_token():
    measure = Measurement(10)
    consume({"type": "response.created"}, "responses", measure, 10.1)
    consume({"type": "response.reasoning_text.delta", "delta": "thinking"}, "responses", measure, 10.2)
    consume({"type": "response.output_text.delta", "delta": ""}, "responses", measure, 10.3)
    assert measure.first_text is None
    consume({"type": "response.output_text.delta", "delta": "Hello world"}, "responses", measure, 11)
    consume({"type": "response.output_text.delta", "delta": "!"}, "responses", measure, 12)
    consume({"type": "response.completed", "response": {"status": "completed", "usage": {
        "input_tokens": 12, "output_tokens": 15, "output_tokens_details": {"reasoning_tokens": 5},
    }}}, "responses", measure, 13)
    result = measure.result(13)
    assert result["ttft_ms"] == 1000
    assert result["ttot_ms"] == 3000
    assert result["output_tps"] == 5
    assert result["decode_tps_estimate"] == 9
    assert result["text_chunks"] == 2
    assert measure.completed


def test_anthropic_usage_is_cumulative_not_summed():
    m = Measurement(0)
    consume({"type": "message_start", "message": {"usage": {"input_tokens": 8, "output_tokens": 1}}},
            "messages", m, .1)
    consume({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "reason"}},
            "messages", m, .2)
    consume({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "visible"}},
            "messages", m, 1)
    consume({"type": "message_delta", "usage": {"output_tokens": 25}, "delta": {"stop_reason": "end_turn"}},
            "messages", m, 2)
    consume({"type": "message_stop"}, "messages", m, 2.1)
    assert m.input_tokens == 8
    assert m.output_tokens == 25
    assert m.first_text == 1
    assert m.result(2.1)["decode_tps_estimate"] is None


def test_chat_usage_can_arrive_after_finish_chunk():
    m = Measurement(0)
    consume({"choices": [{"delta": {"role": "assistant", "content": ""}}]}, "chat", m, 1)
    consume({"choices": [{"delta": {"content": "Hello"}}]}, "chat", m, 2)
    consume({"choices": [{"delta": {}, "finish_reason": "stop"}]}, "chat", m, 3)
    consume({"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 1}}, "chat", m, 4)
    assert m.first_text == 2
    assert m.output_tokens == 1
    assert m.completed


def test_sse_handles_comments_multiline_usage_and_done():
    events = list(sse([": heartbeat", "event: response", 'data: {"type":', 'data: "response.created"}',
                       "", "data: [DONE]", ""]))
    assert events == [{"type": "response.created"}, {"type": "_done"}]


def test_stream_error_is_not_a_success():
    with pytest.raises(RuntimeError):
        consume({"type": "response.failed", "response": {"error": {"code": "overloaded"}}},
                "responses", Measurement(0), 1)


def test_missing_usage_and_zero_duration_stay_null():
    m = Measurement(0)
    m.text("hi", 0)
    assert m.result(0)["output_tps"] is None
    assert m.result(0)["decode_tps_estimate"] is None
    assert m.result(0)["ttft_ms"] == 0


def test_percentiles_exclude_failures_and_keep_dimension_boundaries():
    base = {"region": "us-east-1", "model_id": "test", "provider": "Anthropic", "endpoint": "runtime",
            "api": "messages", "routing": "global", "input_size": 10, "config_id": "v1",
            "scheduled_at": 600, "status": "ok"}
    samples = [{**base, "ttft_ms": 100}, {**base, "ttft_ms": 200},
               {**base, "status": "error", "ttft_ms": 99999},
               {**base, "status": "running"}, {**base, "input_size": 100, "ttft_ms": 900}]
    result = aggregate(samples, 600)
    row = next(row for row in result["summary"] if row["input_size"] == 10)
    assert row["count"] == 4
    assert row["success_rate"] == pytest.approx(2 / 3)
    assert row["pending"] == 1
    assert row["ttft_ms"]["mean"] == 150
    assert row["ttft_ms"]["p95"] == 195
    assert len(result["series"]) == 2


def test_empty_aggregation():
    assert aggregate([], 600) == {"summary": [], "series": []}


def test_missing_visible_text_and_retention_failures_explain_empty_metrics():
    base = {"region": "us-east-1", "model_id": "test", "scheduled_at": 600,
            "config_id": "old", "max_output": 512}
    rows = [
        {**base, "status": "no_text", "output_tokens": 512, "stop_reason": "incomplete"},
        {**base, "status": "error", "scheduled_at": 1200,
         "provider_error": '{"error":{"message":"data retention mode \'default\' is not available for this model"}}'},
        {**base, "status": "running", "scheduled_at": 1800},
    ]
    summary = aggregate(rows, 600)["summary"][0]
    assert summary["ttft_ms"]["n"] == 0
    assert summary["ttft_ms"]["p50"] is None
    assert summary["failure_counts"] == {"output_limit_no_text": 1, "data_retention_required": 1}
    assert summary["last_failure"]["reason"] == "data_retention_required"
    assert summary["pending"] == 1
    assert summary["max_output"] == 512


def test_new_output_budget_does_not_merge_with_old_configuration():
    from probe.config import target_apis
    from probe.jobs import make_job

    target = target_apis("us-east-1", "global.openai.gpt-6-sol", "runtime", "global")[0]
    old = make_job(target, 10, 600, 512)
    new = make_job(target, 10, 600, 2048)
    assert old["job_id"] != new["job_id"]
    summaries = aggregate([
        {**old, "status": "no_text", "output_tokens": 512, "stop_reason": "incomplete"},
        {**new, "status": "ok", "ttft_ms": 1200},
    ], 600)["summary"]
    assert len(summaries) == 2
    assert {s["max_output"] for s in summaries} == {512, 2048}
    assert next(s for s in summaries if s["max_output"] == 2048)["ttft_ms"]["p50"] == 1200


def test_selected_inputs_merge_raw_values_without_merging_reasoning_configs():
    from probe.config import target_apis
    from probe.jobs import make_job

    target = target_apis("us-east-1", "global.anthropic.claude-opus-5", "runtime", "global")[0]
    old = make_job(target, 10, 600, 2048, "model-default")
    new = make_job(target, 10, 600, 2048, "disabled")
    assert old["job_id"] != new["job_id"]
    rows = [
        {**old, "status": "ok", "ttft_ms": 9000},
        {**new, "status": "ok", "ttft_ms": 100},
        {**new, "input_size": 100, "status": "ok", "ttft_ms": 300},
        {**new, "input_size": 100, "status": "error"},
    ]
    result = aggregate(rows, 600, combine_inputs=True)
    assert len(result["summary"]) == 2
    merged = next(row for row in result["summary"] if row["reasoning"] == "disabled")
    assert merged["input_sizes"] == [10, 100]
    assert merged["ttft_ms"]["p95"] == 290
    assert merged["success_rate"] == pytest.approx(2 / 3)


def test_recovery_preserves_history_and_requires_the_same_input_and_configuration():
    base = {"region": "us-east-1", "model_id": "fable", "input_size": 10, "config_id": "same"}
    rows = [
        {**base, "scheduled_at": 600, "status": "error", "error": "data retention mode default"},
        {**base, "input_size": 100, "scheduled_at": 1200, "status": "ok"},
    ]
    assert latest_health(rows)["failing"] == 1
    rows.append({**base, "config_id": "new", "scheduled_at": 1800, "status": "ok"})
    assert latest_health(rows)["historical_cohorts"] == 1
    assert latest_health(rows)["recovered"] == 0
    rows.append({**base, "scheduled_at": 2400, "status": "ok", "ttft_ms": 12000})
    health = latest_health(rows)
    assert health["failing"] == 0 and health["recovered"] == 1
    assert health["active_failure_counts"] == {}
    summary = next(r for r in aggregate(rows, 600, combine_inputs=True)["summary"] if r["config_id"] == "same")
    assert summary["errors"] == 1
    assert summary["failure_counts"] == {"data_retention_required": 1}
    assert summary["success_rate"] == pytest.approx(2 / 3)
    assert summary["ttft_ms"]["p95"] == 12000


def test_pending_or_manual_checks_do_not_clear_a_scheduled_failure():
    rows = [
        {"scheduled_at": 600, "status": "error", "error": "timeout"},
        {"scheduled_at": 1200, "status": "running"},
        {"scheduled_at": 1800, "status": "ok", "verification": True},
    ]
    health = latest_health(rows)
    assert health["failing"] == 1
    assert health["pending_samples"] == 1
    assert health["active_failure_counts"] == {"timeout": 1}
    rows.append({"scheduled_at": 2400, "status": "ok"})
    assert latest_health(rows)["state"] == "recovered"


def test_a_recurrence_is_active_even_after_an_earlier_recovery():
    rows = [{"scheduled_at": slot, "status": status}
            for slot, status in [(600, "error"), (1200, "ok"), (1800, "error")]]
    assert latest_health(rows)["failing"] == 1
    assert latest_health(rows)["recovered"] == 0


def test_health_signature_changes_on_recovery_but_not_each_successful_refresh():
    rows = [{"scheduled_at": 600, "status": "error"}]
    failed = latest_health(rows)["signature"]
    rows.append({"scheduled_at": 1200, "status": "ok"})
    recovered = latest_health(rows)["signature"]
    assert failed != recovered
    rows.append({"scheduled_at": 1800, "status": "ok"})
    assert latest_health(rows)["signature"] == recovered


def test_retired_target_is_historical_without_rewriting_its_failure():
    rows = [{"region": "us-east-1", "model_id": "retired", "scheduled_at": 600, "status": "error"}]
    result = aggregate(rows, 600, active_targets=set())["summary"][0]
    assert result["errors"] == 1 and result["success_rate"] == 0
    assert result["latest_health"]["state"] == "historical"
    assert result["latest_health"]["failing"] == 0
