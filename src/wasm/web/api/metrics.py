# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Metrics history endpoints.

The charts get their live points over the ``/events`` stream; this is where
they load the past from. It is a thin read of
:class:`~wasm.monitor.timeseries.MetricsStore` - the collector writes it, this
translates a window name into seconds and hands the points back.

The windows are a fixed vocabulary rather than a free ``seconds`` parameter
because the store's retention tiers are fixed too: an hour of raw samples, a
day of minute means, thirty days of hour means. A window the store cannot
honour would come back misleadingly sparse, so it cannot be asked for.

``7d`` is not a fourth tier: it is the same hour-mean tier ``30d`` reads,
asked for a shorter stretch of it. :func:`~wasm.monitor.timeseries.MetricsStore.query`
already takes an arbitrary ``window_s`` and unions whichever tiers it
reaches into, so there is nothing to add there - only a name for callers to
ask by.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from wasm.monitor.timeseries import resolution_label
from wasm.web import metrics_collector
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute

router = APIRouter(route_class=WASMErrorRoute)

#: The windows the panel offers, mapped onto the store's retention tiers.
#: "7d" is filtered from the same hour-mean tier "30d" reads, not a tier of
#: its own.
WINDOWS: dict[str, int] = {
    "1h": 3_600,
    "24h": 86_400,
    "7d": 7 * 86_400,
    "30d": 30 * 86_400,
}

#: The authentication dependency. Reading metrics needs nothing beyond the
#: ``read`` scope the chokepoint already enforces for every GET.
Session = Annotated[dict, Depends(get_current_session)]


class MetricsListResponse(BaseModel):
    """Every metric name the store has data for, and the windows it can be read over."""

    metrics: list[str]
    windows: list[str]


class MetricHistoryResponse(BaseModel):
    """One metric's points over a window, oldest first."""

    metric: str
    window: str
    resolution: str
    points: list[tuple[int, float]]


@router.get("", response_model=MetricsListResponse)
def list_metrics(session: Session) -> MetricsListResponse:
    """
    Name every metric that has data.

    Args:
        session: Authenticated session, injected.

    Returns:
        The metric names and the windows they can be asked over.
    """
    store = metrics_collector.get_metrics_store()
    return MetricsListResponse(metrics=store.list_metrics(), windows=sorted(WINDOWS))


@router.get("/{metric:path}", response_model=MetricHistoryResponse)
def metric_history(
    metric: str,
    session: Session,
    window: Annotated[Literal["1h", "24h", "7d", "30d"], Query()] = "1h",
) -> MetricHistoryResponse:
    """
    Read one metric over a named window, oldest point first.

    Args:
        metric: Metric name, e.g. ``cpu.percent`` or
            ``app.example.com.mem.bytes``.
        session: Authenticated session, injected.
        window: Which window to read. Anything outside the fixed vocabulary
            is refused by validation before this runs.

    Returns:
        The metric, the window, the resolution the points are spaced at, and
        ``[ts, value]`` pairs. A metric nothing has recorded returns an empty
        list rather than a 404: "no data yet" is a normal chart state, not a
        missing resource.
    """
    window_s = WINDOWS[window]
    store = metrics_collector.get_metrics_store()
    points = store.query(metric, window_s=window_s)
    return MetricHistoryResponse(
        metric=metric,
        window=window,
        resolution=resolution_label(window_s),
        points=[(ts, value) for ts, value in points],
    )
