"""Streaming adapters. SDK setup/signing is outside the timed request."""
import json
import time
from collections.abc import Iterable

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config

from probe.config import DEFAULT_MAX_OUTPUT, Target
from probe.metrics import Measurement


def host(target: Target) -> str:
    if target.endpoint == "runtime":
        return f"https://bedrock-runtime.{target.region}.amazonaws.com"
    return f"https://bedrock-mantle.{target.region}.api.aws"


def signed_headers(method: str, url: str, body: bytes, region: str, endpoint: str) -> dict:
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("AWS credentials are required")
    request = AWSRequest(method=method, url=url, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(credentials.get_frozen_credentials(),
              "bedrock" if endpoint == "runtime" else "bedrock-mantle", region).add_auth(request)
    return dict(request.headers)


def sse(lines: Iterable[str]):
    data = []
    for line in lines:
        if line == "":
            if data:
                value = "\n".join(data)
                if value == "[DONE]":
                    yield {"type": "_done"}
                else:
                    yield json.loads(value)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        value = "\n".join(data)
        yield {"type": "_done"} if value == "[DONE]" else json.loads(value)


def consume(event: dict, api: str, metric: Measurement, now: float):
    kind = event.get("type")
    if kind in ("error", "response.failed") or event.get("error"):
        raise RuntimeError(json.dumps(event)[:800])
    if api == "responses":
        if kind == "response.output_text.delta":
            metric.text(event.get("delta"), now)
        if kind in ("response.completed", "response.incomplete"):
            response = event["response"]
            usage = response.get("usage") or {}
            metric.input_tokens = usage.get("input_tokens")
            metric.output_tokens = usage.get("output_tokens")
            metric.reasoning_tokens = (usage.get("output_tokens_details") or {}).get("reasoning_tokens")
            metric.cache_read_tokens = (usage.get("input_tokens_details") or {}).get("cached_tokens")
            metric.stop_reason = response.get("status", kind)
            metric.extra["service_tier"] = response.get("service_tier", "default")
            metric.completed = True
    elif api == "chat":
        for choice in event.get("choices", []):
            metric.text(choice.get("delta", {}).get("content"), now)
            if choice.get("finish_reason"):
                metric.stop_reason = choice["finish_reason"]
                metric.completed = True
        usage = event.get("usage")
        if usage:
            metric.input_tokens = usage.get("prompt_tokens")
            metric.output_tokens = usage.get("completion_tokens")
            metric.reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            metric.cache_read_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    elif api == "messages":
        if kind == "message_start":
            usage = event["message"].get("usage", {})
            metric.input_tokens = usage.get("input_tokens")
            metric.cache_read_tokens = usage.get("cache_read_input_tokens")
            metric.extra["cache_write_tokens"] = usage.get("cache_creation_input_tokens")
        if kind == "content_block_delta" and event["delta"].get("type") == "text_delta":
            metric.text(event["delta"].get("text"), now)
        if kind == "message_delta":
            metric.output_tokens = event.get("usage", {}).get("output_tokens")
            metric.stop_reason = event.get("delta", {}).get("stop_reason")
        if kind == "message_stop":
            metric.completed = True


def payload(target: Target, text: str, max_output: int, reasoning="model-default") -> dict:
    value = {"model": target.model_id, "stream": True}
    if target.api == "responses":
        value.update(input=text, max_output_tokens=max_output, store=False, service_tier="default")
    else:
        value["messages"] = [{"role": "user", "content": text}]
        if target.api == "messages":
            value["max_tokens"] = max_output
            if reasoning == "disabled":
                value["thinking"] = {"type": "disabled"}
        else:
            value.update(max_completion_tokens=max_output, stream_options={"include_usage": True},
                         service_tier="default")
    return value


def run(target: Target, text: str, max_output: int = DEFAULT_MAX_OUTPUT, reasoning="model-default") -> dict:
    metric = Measurement(0)
    started = False
    try:
        if target.api == "converse":
            client = boto3.client("bedrock-runtime", region_name=target.region,
                                  config=Config(retries={"total_max_attempts": 1},
                                                connect_timeout=5, read_timeout=120))
            metric.start = time.perf_counter()
            metric.extra["request_started_at"] = time.time()
            started = True
            response = client.converse_stream(
                modelId=target.model_id, messages=[{"role": "user", "content": [{"text": text}]}],
                inferenceConfig={"maxTokens": max_output},
                **({"additionalModelRequestFields": {"thinking": {"type": "disabled"}}}
                   if reasoning == "disabled" else {}),
            )
            metric.extra["request_id"] = response["ResponseMetadata"].get("RequestId")
            stream = response["stream"]
            try:
                for event in stream:
                    now = time.perf_counter()
                    if now - metric.start > 120:
                        raise TimeoutError("Probe deadline exceeded")
                    for key in event:
                        if key.endswith("Exception"):
                            raise RuntimeError(json.dumps(event)[:800])
                    metric.text(event.get("contentBlockDelta", {}).get("delta", {}).get("text"), now)
                    if "messageStop" in event:
                        metric.stop_reason = event["messageStop"].get("stopReason")
                        metric.completed = True
                    if "metadata" in event:
                        usage = event["metadata"].get("usage", {})
                        metric.input_tokens = usage.get("inputTokens")
                        metric.output_tokens = usage.get("outputTokens")
                        metric.cache_read_tokens = usage.get("cacheReadInputTokens")
            finally:
                stream.close()
        else:
            body = json.dumps(payload(target, text, max_output, reasoning)).encode()
            url = host(target) + target.route
            headers = signed_headers("POST", url, body, target.region, target.endpoint)
            if target.api == "messages":
                headers["anthropic-version"] = "2023-06-01"
            with httpx.Client(timeout=httpx.Timeout(120, connect=5), follow_redirects=False) as client:
                metric.start = time.perf_counter()
                metric.extra["request_started_at"] = time.time()
                started = True
                with client.stream("POST", url, headers=headers, content=body) as response:
                    metric.extra["request_id"] = response.headers.get("x-amzn-requestid")
                    if response.is_error:
                        metric.extra["provider_error"] = response.read().decode(errors="replace")[:1000]
                    response.raise_for_status()
                    for event in sse(response.iter_lines()):
                        now = time.perf_counter()
                        if now - metric.start > 120:
                            raise TimeoutError("Probe deadline exceeded")
                        consume(event, target.api, metric, now)
        if not metric.completed:
            raise RuntimeError("Stream ended without a terminal event")
        result = metric.result(time.perf_counter())
        result["status"] = "ok" if metric.first_text is not None else "no_text"
        return result
    except Exception as exc:
        result = metric.result(time.perf_counter()) if started else {}
        code = getattr(exc, "response", None)
        if isinstance(code, httpx.Response):
            status = "throttled" if code.status_code == 429 else "access_denied" if code.status_code == 403 else "error"
            result["http_status"] = code.status_code
        else:
            error_code = code.get("Error", {}).get("Code") if isinstance(code, dict) else None
            status = {"ThrottlingException": "throttled", "AccessDeniedException": "access_denied"}.get(error_code, "error")
        return {**result, "status": status, "error": f"{type(exc).__name__}: {exc}"[:1000]}
