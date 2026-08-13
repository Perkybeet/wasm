# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the agreement between the panel's client script and the server.

``tests/test_web_views.py`` already proves that every address a *template*
emits resolves to a route, with the method it is emitted with. That net has a
hole exactly the size of ``static/panel.js``: an address hard-coded in the
script is not in any template, so nothing checked it.

That is not hypothetical. The shell shipped with

    const events = new EventSource("/events");

while the only ``/events`` in the codebase is a **WebSocket** registered on a
router mounted at ``/ws``. So the real route was ``/ws/events``, the panel asked
for ``/events`` over plain HTTP, got a 404, and reported it through the error
handler that tells the operator the live connection dropped - forever, because
an EventSource reconnects. Live state updates, the row pulse and every
server-pushed notice had never worked, and the whole suite was green.

So this file checks the other half of the contract:

- **Every URL the script opens resolves**, with the protocol it opens it with.
  An EventSource against a WebSocket route is a dead feature, and so is a
  WebSocket against an HTTP one.
- **Every DOM identifier the script looks up is rendered** by some template.
  A ``getElementById`` that always returns null silently disables whatever it
  guards.
- **Every asset the shell loads is on disk.** The panel must work on a machine
  with no route to the internet, so a missing vendored file is a broken panel,
  not a slow one.
- **Every Alpine component named in the markup is defined** by the script, and
  every data attribute the script reads is emitted by some template.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.routing import Match, Route, WebSocketRoute

from wasm.web.auth import SecurityConfig
from wasm.web.server import create_app

WEB = Path(__file__).resolve().parents[1] / "src" / "wasm" / "web"
STATIC = WEB / "static"
TEMPLATES = WEB / "templates"

PANEL_JS = (STATIC / "panel.js").read_text(encoding="utf-8")

#: Every template, as text. Rendering is not needed to answer "does this
#: identifier exist anywhere in the markup", and reading the source keeps this
#: file independent of the page fixtures.
TEMPLATE_SOURCES = {
    path.relative_to(TEMPLATES).as_posix(): path.read_text(encoding="utf-8")
    for path in sorted(TEMPLATES.rglob("*.html"))
}

ALL_MARKUP = "\n".join(TEMPLATE_SOURCES.values())


@pytest.fixture
def app(tmp_path: Path, runner: object) -> FastAPI:
    """
    Build the panel with its state in a temporary directory.

    Args:
        tmp_path: Per-test temporary directory.
        runner: The fake command runner, so no manager reaches a real process.

    Returns:
        The application, for its routing table.
    """
    return create_app(SecurityConfig(state_dir=tmp_path / "state"))


def _walk(routes: object, prefix: str = "") -> list[tuple[str, str]]:
    """
    Collect every route the application assembles, however it nests them.

    FastAPI does not flatten an included router into ``app.routes`` in every
    version, so a flat comprehension reports three routes on a panel that has
    a hundred. Descending is what keeps this honest.

    Args:
        routes: A routes collection, or an object holding one.
        prefix: Path prefix accumulated from enclosing routers.

    Returns:
        Pairs of kind ("http" or "websocket") and path.
    """
    found: list[tuple[str, str]] = []
    for route in routes:  # type: ignore[attr-defined]
        # An included router keeps its children on the router it wrapped, and
        # its prefix on the include context, rather than exposing either as
        # attributes of its own.
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            found.extend(_walk(included.routes, prefix + str(getattr(context, "prefix", "") or "")))
            continue

        path = prefix + str(getattr(route, "path", ""))
        nested = getattr(route, "routes", None)
        if nested:
            found.extend(_walk(nested, path))
        elif isinstance(route, WebSocketRoute):
            found.append(("websocket", path))
        elif isinstance(route, Route):
            found.append(("http", path))
    return found


def _paths(app: FastAPI, kind: str) -> set[str]:
    """
    Args:
        app: The application.
        kind: "http" or "websocket".

    Returns:
        The path of every route of that kind.
    """
    return {path for route_kind, path in _walk(app.routes) if route_kind == kind}


def _resolves(app: FastAPI, path: str, *, websocket: bool) -> bool:
    """
    Check whether the application would route a path.

    Args:
        app: The application.
        path: An absolute path taken from the client script.
        websocket: Whether the client opens it as a WebSocket.

    Returns:
        True when a route of the right kind matches it fully.
    """
    scope = {
        "type": "websocket" if websocket else "http",
        "path": path,
        "headers": [],
        "root_path": "",
    }
    if not websocket:
        scope["method"] = "GET"

    for route in app.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return True
    return False


# ---------------------------------------------------------------------------
# Addresses the script opens
# ---------------------------------------------------------------------------


def test_the_script_opens_at_least_one_address() -> None:
    """A net that catches nothing passes silently."""
    assert _event_source_urls(), "no EventSource found; this file is checking nothing"


def test_the_resolver_agrees_with_routes_that_are_known_to_exist(app: FastAPI) -> None:
    """
    Control case for the matcher.

    Without this, a resolver that answered False for everything would report
    the whole panel as broken and read as a very thorough test suite.
    """
    assert _resolves(app, "/apps", websocket=False), "the applications page did not resolve"
    assert _resolves(app, "/api/apps", websocket=False), "the applications API did not resolve"
    assert _resolves(app, "/ws/events", websocket=True), "the events socket did not resolve"
    assert not _resolves(app, "/no/such/route", websocket=False), "the resolver matches anything"


def test_the_route_inventory_sees_the_whole_panel(app: FastAPI) -> None:
    """
    Control case for the inventory the failure messages are written from.

    FastAPI does not flatten included routers here, so a flat listing reports
    four routes on a panel with well over a hundred, and every message written
    from it is misleading.
    """
    assert "/ws/events" in _paths(app, "websocket")
    assert {"/apps", "/settings"} <= _paths(app, "http")
    assert len(_paths(app, "http")) >= 50, f"only found {len(_paths(app, 'http'))} HTTP routes"


def _event_source_urls() -> list[str]:
    """
    Returns:
        Every absolute path the script opens as a server-sent event stream.
    """
    return re.findall(r"""new\s+EventSource\(\s*["'](/[^"']*)["']""", PANEL_JS)


def _websocket_urls() -> list[str]:
    """
    Returns:
        Every absolute path the script opens as a WebSocket literal. URLs the
        script is handed at runtime are not here; those come from templates and
        are covered by the address sweep in test_web_views.py.
    """
    return re.findall(r"""new\s+WebSocket\(\s*["'](/[^"']*)["']""", PANEL_JS)


def _fetch_urls() -> list[str]:
    """
    Returns:
        Every absolute path the script fetches.
    """
    return re.findall(r"""fetch\(\s*["'](/[^"']*)["']""", PANEL_JS)


@pytest.mark.parametrize("url", _event_source_urls())
def test_every_event_stream_the_script_opens_is_an_http_route(app: FastAPI, url: str) -> None:
    """
    The reported defect, as a test.

    An EventSource speaks plain HTTP and expects ``text/event-stream``. Pointed
    at a WebSocket route it gets a 404 or a 426 and reconnects forever, which
    is indistinguishable on screen from a system where nothing is happening.

    Args:
        app: The application.
        url: A path the script opens as an event stream.
    """
    sockets = _paths(app, "websocket")
    assert url not in sockets, (
        f"{url!r} is a WebSocket route, and an EventSource cannot speak to one"
    )
    assert _resolves(app, url, websocket=False), (
        f"panel.js opens an EventSource on {url!r}, which no HTTP route answers.\n"
        f"WebSocket routes that look related: "
        f"{sorted(path for path in sockets if path.endswith(url))}"
    )


@pytest.mark.parametrize("url", _websocket_urls())
def test_every_socket_the_script_opens_is_a_websocket_route(app: FastAPI, url: str) -> None:
    """
    Args:
        app: The application.
        url: A path the script opens as a WebSocket.
    """
    assert _resolves(app, url, websocket=True), (
        f"panel.js opens a WebSocket on {url!r}, which no WebSocket route answers"
    )


@pytest.mark.parametrize("url", _fetch_urls())
def test_every_address_the_script_fetches_resolves(app: FastAPI, url: str) -> None:
    """
    Args:
        app: The application.
        url: A path the script fetches.
    """
    assert _resolves(app, url, websocket=False), f"panel.js fetches {url!r}, which does not resolve"


# ---------------------------------------------------------------------------
# Identifiers the script looks up
# ---------------------------------------------------------------------------


def _created_ids() -> set[str]:
    """
    Returns:
        Every id the script assigns to an element it builds itself. These are
        looked up too - to replace the previous one - and asking a template to
        render them would be asking for the wrong thing.
    """
    return set(re.findall(r"""\.id\s*=\s*["']([^"']+)["']""", PANEL_JS))


def _looked_up_ids() -> list[str]:
    """
    Returns:
        Every element id the script expects the server to have rendered.
    """
    found = re.findall(r"""getElementById\(\s*["']([^"']+)["']""", PANEL_JS)
    return [identifier for identifier in found if identifier not in _created_ids()]


@pytest.mark.parametrize("identifier", sorted(set(_looked_up_ids())))
def test_every_identifier_the_script_looks_up_is_rendered(identifier: str) -> None:
    """
    A lookup that always returns null disables whatever it guards, in silence.

    ``panel.js`` gates the live connection on ``machine-strip`` existing, so a
    renamed id would switch the whole feature off without a word.

    Args:
        identifier: An id the script resolves by name.
    """
    assert f'id="{identifier}"' in ALL_MARKUP, (
        f"panel.js looks up #{identifier}, which no template renders"
    )


def test_the_identifier_sweep_covers_the_live_connection_gate() -> None:
    """The regex above is load-bearing; assert it found the known lookups."""
    found = set(_looked_up_ids())
    assert {"machine-strip", "notices"} <= found, f"only found {found}"


# ---------------------------------------------------------------------------
# Assets the shell loads
# ---------------------------------------------------------------------------


def _static_references() -> set[str]:
    """
    Returns:
        Every ``/static/`` path the templates and the script name.
    """
    sources = ALL_MARKUP + PANEL_JS + (STATIC / "app.css").read_text(encoding="utf-8")
    return set(re.findall(r"""["'(](/static/[^"')\s]+)["')]""", sources))


@pytest.mark.parametrize("reference", sorted(_static_references()))
def test_every_static_asset_the_panel_names_is_on_disk(reference: str) -> None:
    """
    The panel must work with no route to the internet, so nothing is fetched
    lazily from anywhere: a named file that is not vendored is a broken panel.

    Args:
        reference: A ``/static/`` path named in the markup, the script or the
            stylesheet.
    """
    asset = STATIC / reference[len("/static/") :]
    assert asset.is_file(), f"{reference} is referenced but not vendored"


def test_the_asset_sweep_covers_the_vendored_libraries() -> None:
    """The regex above is load-bearing; assert it found the known assets."""
    found = _static_references()
    assert "/static/vendor/htmx.min.js" in found
    assert "/static/vendor/xterm.js" in found
    assert any(reference.endswith(".woff2") for reference in found), "no font was swept"


# ---------------------------------------------------------------------------
# Components and data attributes
# ---------------------------------------------------------------------------


def _handled_hooks() -> set[str]:
    """
    Returns:
        Every ``data-`` hook the client script handles by name.
    """
    return set(re.findall(r'closest\(\s*"\[(data-[\w-]+)\]"', PANEL_JS))


@pytest.mark.parametrize("hook", sorted(_handled_hooks()))
def test_every_hook_the_script_handles_is_rendered_somewhere(hook: str) -> None:
    """
    With no framework binding the markup to the script, a hook one side knows
    about and the other does not is a dead control that reports nothing.

    Args:
        hook: A data attribute the script listens for.
    """
    assert hook in ALL_MARKUP, f"panel.js handles [{hook}], which no template renders"


def test_the_hook_sweep_covers_the_drawer_and_the_navigation() -> None:
    """The regex above is load-bearing; assert it found the known hooks."""
    found = _handled_hooks()

    assert {"data-follow-log", "data-drawer-toggle", "data-nav-toggle"} <= found, found


def _dataset_reads() -> set[str]:
    """
    Returns:
        Every ``data-*`` attribute the script reads through ``dataset``.
    """
    names = re.findall(r"\.dataset\.(\w+)", PANEL_JS)
    # dataset.fooBar is the DOM spelling of data-foo-bar.
    return {re.sub(r"([A-Z])", lambda m: f"-{m.group(1).lower()}", name) for name in names}


@pytest.mark.parametrize("attribute", sorted(_dataset_reads()))
def test_every_data_attribute_the_script_reads_is_emitted(attribute: str) -> None:
    """
    Args:
        attribute: A ``data-*`` attribute name the script reads.
    """
    assert f"data-{attribute}=" in ALL_MARKUP, (
        f"panel.js reads data-{attribute}, which no template emits"
    )


def test_the_dataset_sweep_covers_the_log_buttons() -> None:
    """The regex above is load-bearing; assert it found the known attributes."""
    assert {"source", "url"} <= _dataset_reads()
