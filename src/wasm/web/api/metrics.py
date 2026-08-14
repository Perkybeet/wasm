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
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from wasm.web import metrics_collector
from wasm.web.api.auth import get_current_session
from wasm.web.api.deps import WASMErrorRoute

router = APIRouter(route_class=WASMErrorRoute)

#: The windows the panel offers, mapped onto the store's retention tiers.
WINDOWS: dict[str, int] = {
    "1h": 3_600,
    "24h": 86_400,
    "30d": 30 * 86_400,
}

#: The authentication dependency. Reading metrics needs nothing beyond the
#: ``read`` scope the chokepoint already enforces for every GET.
Session = Annotated[dict, Depends(get_current_session)]


@router.get("")
def list_metrics(session: Session) -> dict[str, Any]:
    """
    Name every metric that has data.

    Args:
        session: Authenticated session, injected.

    Returns:
        The metric names and the windows they can be asked over.
    """
    store = metrics_collector.get_metrics_store()
    return {"metrics": store.list_metrics(), "windows": sorted(WINDOWS)}


@router.get("/{metric:path}")
def metric_history(
    metric: str,
    session: Session,
    window: Annotated[Literal["1h", "24h", "30d"], Query()] = "1h",
) -> dict[str, Any]:
    """
    Read one metric over a named window, oldest point first.

    Args:
        metric: Metric name, e.g. ``cpu.percent`` or
            ``app.example.com.mem.bytes``.
        session: Authenticated session, injected.
        window: Which window to read. Anything outside the fixed vocabulary
            is refused by validation before this runs.

    Returns:
        The metric, the window, and ``[ts, value]`` pairs. A metric nothing
        has recorded returns an empty list rather than a 404: "no data yet"
        is a normal chart state, not a missing resource.
    """
    store = metrics_collector.get_metrics_store()
    points = store.query(metric, window_s=WINDOWS[window])
    return {
        "metric": metric,
        "window": window,
        "points": [[ts, value] for ts, value in points],
    }
