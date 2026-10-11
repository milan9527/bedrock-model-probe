import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field


@dataclass
class Measurement:
    start: float
    first_text: float | None = None
    last_text: float | None = None
    completed: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    stop_reason: str | None = None
    text_chunks: int = 0
    extra: dict = field(default_factory=dict)

    def text(self, value: str | None, now: float):
        # Metadata, empty deltas, reasoning, and role events are not visible tokens.
        if value:
            if self.first_text is None:
                self.first_text = now
            self.last_text = now
            self.text_chunks += 1

    def result(self, end: float) -> dict:
        duration = end - self.start
        visible = None
        if self.output_tokens is not None and self.reasoning_tokens is not None:
            visible = max(0, self.output_tokens - self.reasoning_tokens)
        generation = (
            self.last_text - self.first_text
            if self.last_text is not None and self.first_text is not None else 0
        )
        return {
            "ttft_ms": (self.first_text - self.start) * 1000 if self.first_text is not None else None,
            "ttot_ms": duration * 1000,
            "last_text_ms": (self.last_text - self.start) * 1000 if self.last_text is not None else None,
            # Usage-based throughput includes reasoning when the provider includes it.
            "output_tps": self.output_tokens / duration if self.output_tokens is not None and duration > 0 else None,
            # Approximate: streaming chunks can contain multiple tokens. Never count chunks as tokens.
            "decode_tps_estimate": (visible - 1) / generation if visible and visible > 1 and generation > 0 else None,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "stop_reason": self.stop_reason,
            "text_chunks": self.text_chunks,
            **self.extra,
        }


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * p
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


DIMENSIONS = ("region", "model_id", "provider", "endpoint", "api", "routing", "input_size", "config_id")
METRICS = ("ttft_ms", "ttot_ms", "output_tps", "decode_tps_estimate", "input_tokens", "output_tokens")
TARGET_DIMENSIONS = tuple(k for k in DIMENSIONS if k not in ("input_size", "config_id"))


def failure_reason(row: dict) -> str:
    message = f"{row.get('provider_error', '')} {row.get('error', '')}".lower()
    if "data retention mode" in message:
        return "data_retention_required"
    if row["status"] == "no_text":
        cap = row.get("max_output")
        if row.get("stop_reason") in ("max_tokens", "length") or (
            cap and row.get("output_tokens") is not None and row["output_tokens"] >= cap
        ):
            return "output_limit_no_text"
        return "no_visible_text"
    if "timeout" in message or "deadline" in message:
        return "timeout"
    return row["status"]


def latest_configurations(samples):
    latest = {}
    for row in samples:
        if row.get("verification"):
            continue
        key = tuple(row.get(k) for k in DIMENSIONS if k != "config_id")
        slot = row["scheduled_at"]
        if key not in latest or slot > latest[key][0]:
            latest[key] = (slot, {row.get("config_id")})
        elif slot == latest[key][0]:
            latest[key][1].add(row.get("config_id"))
    return latest


def latest_health(samples: list[dict], active_targets=None, configurations=None) -> dict:
    """Latest completed scheduled result per exact input/configuration cohort."""
    cohorts = defaultdict(list)
    configurations = latest_configurations(samples) if configurations is None else configurations
    for row in samples:
        if not row.get("verification"):
            cohorts[tuple(row.get(key) for key in DIMENSIONS)].append(row)
    states, causes, signatures = Counter(), Counter(), []
    pending, history = 0, 0
    success_times, failure_times, completed_times, scheduled_times = [], [], [], []
    for identity, rows in cohorts.items():
        completed = [r for r in rows if r["status"] != "running"]
        failed = [r for r in completed if r["status"] != "ok"]
        pending += sum(r["status"] == "running" for r in rows)
        history += len(failed)
        scheduled_times.extend(r["scheduled_at"] for r in rows)
        success_times.extend(r["scheduled_at"] for r in completed if r["status"] == "ok")
        failure_times.extend(r["scheduled_at"] for r in failed)
        # Prefer failure on an ambiguous tie; a same-slot success cannot prove recovery.
        latest = max(completed, key=lambda r: (r["scheduled_at"], r["status"] != "ok"), default=None)
        example = rows[0]
        config_key = tuple(example.get(k) for k in DIMENSIONS if k != "config_id")
        config_ids = configurations.get(config_key, (None, {example.get("config_id")}))[1]
        inactive = (
            example.get("config_id") not in config_ids
            or (active_targets is not None
                and tuple(example.get(k) for k in TARGET_DIMENSIONS) not in active_targets)
        )
        reason = None
        if inactive:
            state = "historical"
        elif latest is None:
            state = "pending"
        elif latest["status"] != "ok":
            state = "failing"
            reason = failure_reason(latest)
            causes[reason] += 1
        else:
            state = "recovered" if failed else "healthy"
        states[state] += 1
        if latest:
            completed_times.append(latest["scheduled_at"])
        signatures.append((identity, state, reason))
    state = ("unknown" if not cohorts else "historical" if states["historical"] == len(cohorts)
             else "failing" if states["failing"] else
             "pending" if states["pending"] else "recovered" if states["recovered"] else "healthy")
    return {
        "state": state, "cohorts": len(cohorts), "healthy": states["healthy"],
        "recovered": states["recovered"], "failing": states["failing"],
        "historical_cohorts": states["historical"],
        "awaiting_first_result": states["pending"], "pending_samples": pending,
        "active_failure_counts": dict(causes), "historical_failures": history,
        "last_success_at": max(success_times, default=None),
        "last_failure_at": max(failure_times, default=None),
        "latest_scheduled_at": max(scheduled_times, default=None),
        "oldest_completed_at": min(completed_times, default=None),
        # Stable during ordinary successful collection; changes when failure/recovery changes.
        "signature": hashlib.sha256(json.dumps(sorted(signatures, key=str)).encode()).hexdigest()[:20],
    }


def aggregate(samples: list[dict], period: int, combine_inputs=False, active_targets=None) -> dict:
    series, summary = defaultdict(list), defaultdict(list)
    configurations = latest_configurations(samples)
    dimensions = tuple(key for key in DIMENSIONS if not combine_inputs or key != "input_size")
    for sample in samples:
        key = tuple(sample.get(k) for k in dimensions)
        summary[key].append(sample)
        series[(int(sample["scheduled_at"]) // period * period, key)].append(sample)

    def stats(rows, details=False):
        ok = [r for r in rows if r["status"] == "ok"]
        failed = [r for r in rows if r["status"] not in ("ok", "running")]
        pending = sum(r["status"] == "running" for r in rows)
        result = {
            "count": len(rows), "successes": len(ok), "pending": pending,
            "errors": len(rows) - len(ok) - pending,
            "success_rate": len(ok) / (len(rows) - pending) if len(rows) > pending else None,
        }
        if details:
            latest = max(failed, key=lambda r: r["scheduled_at"], default=None)
            result.update(
                latest_health=latest_health(rows, active_targets, configurations),
                max_output=rows[0].get("max_output"),
                reasoning=rows[0].get("reasoning", "model-default"),
                input_sizes=sorted({r["input_size"] for r in rows if r.get("input_size") is not None}),
                status_counts=dict(Counter(r["status"] for r in rows)),
                failure_counts=dict(Counter(failure_reason(r) for r in failed)),
                last_failure=({
                    "reason": failure_reason(latest), "status": latest["status"],
                    "scheduled_at": latest["scheduled_at"], "stop_reason": latest.get("stop_reason"),
                    "max_output": latest.get("max_output"), "output_tokens": latest.get("output_tokens"),
                    "message": latest.get("provider_error") or latest.get("error"),
                } if latest else None),
            )
        for metric in METRICS:
            values = [float(r[metric]) for r in ok if r.get(metric) is not None]
            result[metric] = {
                "n": len(values),
                "mean": sum(values) / len(values) if values else None,
                "p50": percentile(values, .5), "p95": percentile(values, .95),
                "p99": percentile(values, .99),
            }
        return result

    return {
        "summary": [{**dict(zip(dimensions, k)), **stats(v, details=True)} for k, v in summary.items()],
        "series": [
            {"timestamp": ts, **dict(zip(dimensions, k)), **stats(v)}
            for (ts, k), v in sorted(series.items())
        ],
    }
