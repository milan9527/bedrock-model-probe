import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass

REGIONS = {
    "us-east-1": "N. Virginia",
    "us-east-2": "Ohio",
    "us-west-2": "Oregon",
    "ap-northeast-1": "Tokyo",
    "ap-southeast-1": "Singapore",
    "eu-central-1": "Frankfurt",
}
SIZES = (1, 10, 100)
PROMPT_VERSION = "short-64-v2"
DEFAULT_MAX_OUTPUT = 2048


def probe_interval_seconds() -> int:
    interval = int(os.getenv("PROBE_INTERVAL_SECONDS", "3600"))
    if interval not in (600, 3600):
        raise ValueError("PROBE_INTERVAL_SECONDS must be 600 or 3600")
    return interval


def schedule_expression(interval: int) -> str:
    if interval not in (600, 3600):
        raise ValueError("schedule_seconds must be 600 or 3600")
    return "cron(0 * * * ? *)" if interval == 3600 else "cron(0/10 * * * ? *)"


def prompt(size: int, version: str = PROMPT_VERSION) -> str:
    """Nominal text-token presets; actual model usage is recorded separately."""
    if size not in SIZES:
        raise ValueError("Input preset must be 1, 10 or 100")
    if version not in ("repeat-v1", PROMPT_VERSION):
        raise ValueError("Unknown probe prompt version")
    if size == 1:
        return "Hello"
    # Exactly 10 and 100 text tokens under the cl100k_base reference encoding.
    # This is NOT a claim about any model's private tokenizer or billed message framing.
    # Retain the original text for jobs queued before the short-response release.
    seed = ("Write a story about a journey through outer space." if version == "repeat-v1"
            else "Reply in exactly 64 words about cloud computing.")
    return seed if size == 10 else " ".join([seed] * 10)


def eligible(model_id: str) -> bool:
    name = model_id.lower()
    # A dated Claude 4 ID (4-20250514) has no minor version; the date is not "4.20250514".
    match = re.search(r"anthropic\.claude-[a-z]+-(\d+)(?:[.-](\d{1,2})(?!\d))?", name)
    if match:
        return (int(match[1]), int(match[2] or 0)) >= (4, 8)
    match = re.search(r"openai\.gpt-(\d+)(?:[.-](\d+))?", name)
    return bool(match and (int(match[1]), int(match[2] or 0)) >= (5, 6))


@dataclass(frozen=True)
class Target:
    region: str
    model_id: str
    provider: str
    endpoint: str
    api: str
    route: str
    routing: str

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:20]


def runtime_targets(region: str, models: list, profiles: list) -> list[Target]:
    targets = []
    for model in models:
        mid = model["modelId"]
        if (eligible(mid) and "ON_DEMAND" in model.get("inferenceTypesSupported", [])
                and model.get("responseStreamingSupported", False)):
            targets.extend(target_apis(region, mid, "runtime", "regional"))
    for profile in profiles:
        mid = profile["inferenceProfileId"]
        if profile.get("status") == "ACTIVE" and eligible(mid):
            routing = "global" if mid.startswith("global.") else "geographic"
            targets.extend(target_apis(region, mid, "runtime", routing))
    return targets


def target_apis(region: str, mid: str, endpoint: str, routing: str) -> list[Target]:
    provider = "Anthropic" if "anthropic." in mid else "OpenAI"
    if provider == "Anthropic":
        if endpoint == "runtime":
            return [
                Target(region, mid, provider, endpoint, "converse", "", routing),
                Target(region, mid, provider, endpoint, "messages", "/anthropic/v1/messages", routing),
            ]
        return [Target(region, mid, provider, endpoint, "messages", "/anthropic/v1/messages", routing)]
    # Closed-weight GPT model cards use /openai/v1 on BOTH endpoint surfaces.
    return [
        Target(region, mid, provider, endpoint, "responses", "/openai/v1/responses", routing),
        Target(region, mid, provider, endpoint, "chat", "/openai/v1/chat/completions", routing),
    ]


def select_targets(targets: list[Target], model_pattern: str = "", apis: tuple = (),
                   routing: tuple = (), fable_global_only: bool = False) -> list[Target]:
    pattern = re.compile(model_pattern) if model_pattern else None
    return [
        t for t in targets
        if (not pattern or pattern.search(t.model_id))
        and (not apis or t.api in apis)
        and (not routing or t.routing in routing)
        and (not fable_global_only or not re.search(r"anthropic\.claude-fable-5(?:[.:-]|$)", t.model_id)
             or (t.endpoint == "runtime" and t.routing == "global"
                 and t.model_id.startswith("global.anthropic.")))
    ]
