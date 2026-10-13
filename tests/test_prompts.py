import pytest
import tiktoken

from probe.config import PROMPT_VERSION, SIZES, probe_interval_seconds, prompt, schedule_expression


@pytest.mark.parametrize("version", ["repeat-v1", PROMPT_VERSION])
def test_reference_prompt_sizes(version):
    encoding = tiktoken.get_encoding("cl100k_base")
    for size in SIZES:
        assert len(encoding.encode(prompt(size, version))) == size


def test_short_response_and_legacy_prompts_are_versioned():
    assert prompt(10) == "Reply in exactly 64 words about cloud computing."
    assert prompt(10, "repeat-v1") == "Write a story about a journey through outer space."
    with pytest.raises(ValueError, match="version"):
        prompt(10, "unknown")


def test_hourly_schedule_default_and_legacy_override(monkeypatch):
    monkeypatch.delenv("PROBE_INTERVAL_SECONDS", raising=False)
    assert probe_interval_seconds() == 3600
    assert schedule_expression(probe_interval_seconds()) == "cron(0 * * * ? *)"
    monkeypatch.setenv("PROBE_INTERVAL_SECONDS", "600")
    assert schedule_expression(probe_interval_seconds()) == "cron(0/10 * * * ? *)"
    monkeypatch.setenv("PROBE_INTERVAL_SECONDS", "601")
    with pytest.raises(ValueError):
        probe_interval_seconds()
