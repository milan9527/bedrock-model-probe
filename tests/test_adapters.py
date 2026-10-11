import httpx

from probe import adapters
from probe.config import target_apis


def mocked_run(monkeypatch, content, status=200):
    transport = httpx.MockTransport(lambda req: httpx.Response(status, text=content))
    client = httpx.Client(transport=transport)
    monkeypatch.setattr(adapters.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(adapters, "signed_headers", lambda *args: {})
    target = target_apis("us-east-1", "openai.gpt-6-astra", "mantle", "regional")[0]
    return adapters.run(target, "Hello", 512)


def test_connection_end_without_terminal_event_is_a_failed_probe(monkeypatch):
    result = mocked_run(monkeypatch, 'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n')
    assert result["status"] == "error"
    assert "terminal event" in result["error"]
    assert result["ttft_ms"] is not None


def test_missing_usage_is_preserved_as_unknown(monkeypatch):
    result = mocked_run(monkeypatch,
                        'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
                        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n')
    assert result["status"] == "ok"
    assert result["output_tps"] is None


def test_throttle_keeps_provider_error_and_has_no_fake_ttft(monkeypatch):
    result = mocked_run(monkeypatch, '{"error":{"message":"quota exceeded"}}', status=429)
    assert result["status"] == "throttled"
    assert result["ttft_ms"] is None
    assert "quota exceeded" in result["provider_error"]


def test_reasoning_only_response_is_reported_separately(monkeypatch):
    result = mocked_run(monkeypatch,
                        'data: {"type":"response.reasoning_text.delta","delta":"Thinking"}\n\n'
                        'data: {"type":"response.incomplete","response":{"status":"incomplete",'
                        '"usage":{"input_tokens":8,"output_tokens":512}}}\n\n')
    assert result["status"] == "no_text"
    assert result["ttft_ms"] is None
