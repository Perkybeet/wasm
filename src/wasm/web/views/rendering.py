# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The Jinja environment the panel renders through.

Two things here are load-bearing.

**Autoescaping is on.** The old panel built HTML by interpolating server data
into ``innerHTML`` in eighty-six places, with an escape helper that did not
escape quotes, so a process command line or an nginx error message could close
an attribute and inject script into a page that holds root over the machine.
Escaping by default moves that from a thing to remember to a thing the
templates cannot get wrong.

**Fragments and pages share one code path.** htmx asks for a piece of a page
with the same URL that a browser uses for the whole page. Rendering both from
the same template means the two can never disagree.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

#: Powers of 1024, because this is a systems tool and an operator comparing a
#: number here against `du -h` should see the same figure.
_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def filesize(value: float | int | None) -> str:
    """
    Render a byte count the way system tools do.

    Args:
        value: Number of bytes.

    Returns:
        A short human-readable size, or an em dash when unknown.
    """
    if value is None:
        return "—"
    size = float(value)
    for unit in _UNITS:
        if abs(size) < 1024 or unit == _UNITS[-1]:
            precision = 0 if unit == "B" or abs(size) >= 100 else 1
            return f"{size:.{precision}f} {unit}"
        size /= 1024
    return f"{size:.1f} {_UNITS[-1]}"


def duration(seconds: float | int | None) -> str:
    """
    Render a span of time compactly, in the style of ``systemctl status``.

    Args:
        seconds: Length of the span.

    Returns:
        A short string such as "3d 4h" or "12m", or an em dash when unknown.
    """
    if seconds is None:
        return "—"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _localised(moment: dt.datetime) -> dt.datetime:
    """
    Attach this machine's offset to a timestamp that was written without one.

    Everything WASM records is stamped with ``datetime.now()``: the store, the
    job manager and the backup manager all do it, and none of them attach an
    offset. Reading those back as UTC is how a deploy that finished a minute
    ago came out as "in 59m", an hour in the future, on any machine that is not
    in London.

    Args:
        moment: The timestamp, aware or naive.

    Returns:
        An aware timestamp.
    """
    return moment.astimezone() if moment.tzinfo is None else moment


def since(moment: dt.datetime | str | None) -> str:
    """
    Render how long ago something happened.

    Args:
        moment: When it happened, as a datetime or an ISO 8601 string. A
            timestamp with no offset is read as local time, which is how every
            part of WASM writes one.

    Returns:
        A relative description, or an em dash when unknown.
    """
    if moment is None:
        return "—"
    if isinstance(moment, str):
        try:
            parsed = dt.datetime.fromisoformat(moment)
        except ValueError:
            # An unparsable timestamp is shown as it was stored. Inventing a
            # date for it would be worse than admitting the record is odd.
            return moment
        moment = parsed
    moment = _localised(moment)
    delta = dt.datetime.now(dt.timezone.utc) - moment
    if delta.total_seconds() < 0:
        return f"in {duration(-delta.total_seconds())}"
    return f"{duration(delta.total_seconds())} ago"


def until(moment: dt.datetime | str | None) -> str:
    """
    Render how long remains before a deadline, such as certificate expiry.

    Args:
        moment: The deadline, as a datetime or an ISO 8601 string. A timestamp
            with no offset is read as local time.

    Returns:
        A relative description, or "expired" when it has passed.
    """
    if moment is None:
        return "—"
    if isinstance(moment, str):
        try:
            parsed = dt.datetime.fromisoformat(moment)
        except ValueError:
            return moment
        moment = parsed
    moment = _localised(moment)
    delta = moment - dt.datetime.now(dt.timezone.utc)
    if delta.total_seconds() <= 0:
        return "expired"
    return duration(delta.total_seconds())


def build_environment(template_dir: Path = TEMPLATE_DIR) -> Environment:
    """
    Create the panel's Jinja environment.

    Args:
        template_dir: Directory holding the templates.

    Returns:
        A configured environment with autoescaping enabled.
    """
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(default_for_string=True, default=True),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=_LoudUndefined,
    )
    env.filters["filesize"] = filesize
    env.filters["duration"] = duration
    env.filters["since"] = since
    env.filters["until"] = until
    return env


class _LoudUndefined(ChainableUndefined):
    """
    Placeholder that makes a missing template variable obvious.

    Jinja's default renders an empty string, which turns a typo in a context
    key into a silently blank column that nobody notices until a user asks
    where their data went. This renders a visible marker instead, so the
    mistake is caught the first time the page is looked at.
    """

    def __str__(self) -> str:
        return f"[missing: {self._undefined_name}]"

    def __html__(self) -> str:
        return f'<span class="faint">[missing: {self._undefined_name}]</span>'


templates = build_environment()


def page(
    request: Request, name: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    """
    Render a full page.

    Args:
        request: The incoming request, used for the shared context.
        name: Template path relative to the templates directory.
        context: Values the template needs, merged over the shared context.
        status_code: HTTP status to answer with. A page that reports something
            missing says so in its status too, so a proxy, a log and a person
            all agree on what happened.

    Returns:
        The rendered page.
    """
    from wasm.web.views.context import shared_context

    merged = shared_context(request)
    merged.update(context)
    return HTMLResponse(templates.get_template(name).render(**merged), status_code=status_code)


def fragment(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    """
    Render a fragment for an htmx swap.

    Args:
        request: The incoming request, used for the shared context.
        name: Template path relative to the templates directory.
        context: Values the template needs, merged over the shared context.

    Returns:
        The rendered fragment.
    """
    return page(request, name, context)
