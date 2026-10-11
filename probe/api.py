import os
import time

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from probe.analysis import ANALYSIS_DIMENSIONS, THRESHOLDS, build_evidence, cached_analysis
from probe.auth import authorize
from probe.config import REGIONS
from probe.management import router as management_router
from probe.metrics import TARGET_DIMENSIONS, aggregate, latest_health
from probe.storage import Store

app = FastAPI(title="Bedrock Probe", docs_url=None, redoc_url=None)
app.include_router(management_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/config", dependencies=[Depends(authorize)])
def config():
    return {"regions": REGIONS, "input_sizes": [1, 10, 100], "schedule_seconds": 600,
            "max_range_days": 31, "max_samples": 100000, "anomaly_thresholds": THRESHOLDS}


@app.get("/api/catalog", dependencies=[Depends(authorize)])
def catalog():
    if os.getenv("LOCAL_DEV") == "1" and not os.getenv("TABLE_NAME"):
        return {"regions": {region: {} for region in REGIONS}, "local": True}
    store = Store()
    return {"regions": {region: {
        **store.catalog(region), "available_targets": store.discovery(region).get("targets", []),
    } for region in REGIONS}}


def validate_selection(start, end, region, input_size):
    if end <= start or end - start > 31 * 86400:
        raise HTTPException(400, "Choose a time range between 1 second and 31 days")
    if region and any(value not in REGIONS for value in region):
        raise HTTPException(400, "Unknown region")
    if input_size and any(value not in (1, 10, 100) for value in input_size):
        raise HTTPException(400, "Unknown input preset")


def selection_filters(provider, endpoint, model, api, input_size):
    return {key: sorted(set(values)) if values else None for key, values in {
        "provider": provider, "endpoint": endpoint, "model_id": model,
        "api": api, "input_size": input_size,
    }.items()}


def selected_rows(start, end, region, filters, store=None):
    if os.getenv("LOCAL_DEV") == "1" and not os.getenv("TABLE_NAME"):
        rows = []
    else:
        rows = (store or Store()).samples(start, end, sorted(set(region)) if region else list(REGIONS))
    rows = [row for row in rows if all(not values or row.get(key) in values for key, values in filters.items())]
    # Report abandoned claims even if their redelivery has not happened yet.
    now = time.time()
    for row in rows:
        if row["status"] == "running" and now - row.get("claimed_at", now) > 180:
            row["status"] = "worker_timeout"
    return rows


def active_targets(store, regions):
    if store is None:
        return None
    targets = set()
    for region in regions or REGIONS:
        snapshot = store.catalog(region)
        # A missing, stale or partial discovery cannot establish that a target was retired.
        if (not isinstance(snapshot, dict) or "targets" not in snapshot or snapshot.get("issues")
                or time.time() - snapshot.get("scheduled_at", 0) > 1200):
            return None
        targets.update(tuple(target.get(key) for key in TARGET_DIMENSIONS) for target in snapshot["targets"])
    return targets


@app.get("/api/metrics", dependencies=[Depends(authorize)])
def metrics(
    start: int = Query(ge=0), end: int = Query(ge=1),
    period: int = Query(3600, ge=600, le=86400),
    region: list[str] | None = Query(None), provider: list[str] | None = Query(None),
    endpoint: list[str] | None = Query(None), model: list[str] | None = Query(None),
    api: list[str] | None = Query(None), input_size: list[int] | None = Query(None),
    include_verification: bool = False,
):
    validate_selection(start, end, region, input_size)
    filters = selection_filters(provider, endpoint, model, api, input_size)
    store = None if os.getenv("LOCAL_DEV") == "1" and not os.getenv("TABLE_NAME") else Store()
    try:
        rows = selected_rows(start, end, region, filters, store)
    except ValueError as exc:
        raise HTTPException(413, str(exc)) from None
    verification_count = sum(bool(row.get("verification")) for row in rows)
    if not include_verification:
        rows = [row for row in rows if not row.get("verification")]
    targets = active_targets(store, region)
    result = aggregate(rows, period, combine_inputs=True, active_targets=targets)
    result.update(start=start, end=end, period=period, samples=len(rows),
                  health=latest_health(rows, targets), include_verification=include_verification,
                  excluded_verification_samples=verification_count if not include_verification else 0,
                  latest_sample=max((r["scheduled_at"] for r in rows), default=None),
                  failures=sorted(
                      [r for r in rows if r["status"] not in ("ok", "running")],
                      key=lambda r: r["scheduled_at"], reverse=True,
                  )[:50])
    return result


@app.get("/api/analysis", dependencies=[Depends(authorize)])
def analysis(
    start: int = Query(ge=0), end: int = Query(ge=1),
    period: int = Query(3600, ge=600, le=86400), dimension: str = "model_id",
    region: list[str] | None = Query(None), provider: list[str] | None = Query(None),
    endpoint: list[str] | None = Query(None), model: list[str] | None = Query(None),
    api: list[str] | None = Query(None), input_size: list[int] | None = Query(None),
):
    validate_selection(start, end, region, input_size)
    if dimension not in ANALYSIS_DIMENSIONS:
        raise HTTPException(400, "Unknown analysis dimension")
    span = end - start
    requested_end = min(end, int(time.time()))
    end = requested_end // 600 * 600
    start = max(0, end - span)
    filters = selection_filters(provider, endpoint, model, api, input_size)
    scope = {**filters, "region": sorted(set(region)) if region else None}
    if os.getenv("LOCAL_DEV") == "1" and not os.getenv("TABLE_NAME"):
        return {"status": "insufficient_data", "generated_at": int(time.time()), "ai": None, "cached": False,
                "evidence": build_evidence([], start, end, period, dimension, scope)}
    store = Store()
    try:
        rows = selected_rows(start, requested_end, region, filters, store)
    except ValueError as exc:
        return {"status": "query_limit", "message": str(exc), "ai": None}
    return cached_analysis(store, start, end, period, dimension, scope, lambda: rows,
                           current_health=latest_health(rows, active_targets(store, region)))


if os.getenv("LOCAL_DEV") == "1":
    @app.get("/config.json")
    def local_config():
        return {"local": True}

    @app.get("/")
    def index():
        return FileResponse("frontend/index.html")

    app.mount("/static", StaticFiles(directory="frontend/static"), name="static")
