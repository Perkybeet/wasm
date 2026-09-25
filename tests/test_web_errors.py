"""
Tests for the API's one error contract.

Every endpoint under ``/api`` answers a failure as
``{"error": str, "detail": str, "hint": str | None, "fields": dict[str, str] | None}``,
never Starlette's bare ``{"detail": ...}`` and never an unhandled 500. Three
defect classes are pinned here:

- **Three routers had no error boundary.** ``services``, ``config`` and
  ``monitor`` were built as plain ``APIRouter()``, so a manager error crashed
  the request with a bare 500 instead of answering with the status the error
  actually implies.
- **A validation failure did not name the field.** FastAPI's own 422 body is
  a list of ``{"loc": [...], "msg": ...}`` entries a client has to parse; the
  contract instead keys them by field name.
- **A login failure was a string a client had to pattern-match.** "Invalid
  token", "include totp_code" and "invalid code" all answered 401 with no way
  to tell them apart short of grepping the message. Each now carries its own
  ``error`` value.

HTML routes are untouched: the JSON reshaping only applies under ``/api``,
which is checked directly against the login page and the missing-page screen.
"""

from __future__ import annotations

import ast
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from tests.test_web_auth import build_client, enable_totp, login, make_config
from wasm.core.exceptions import ConfigError
from wasm.web.auth import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from wasm.web.server import create_app, get_token_manager

#: Every module under this package that declares an ``APIRouter``. Walked by
#: :func:`test_every_api_router_uses_the_error_route` rather than imported,
#: so the guard does not itself require importing (and therefore partially
#: initialising) every router module.
API_PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "wasm" / "web" / "api"


@pytest.fixture
def client(sandbox: Path, runner: object) -> TestClient:
    """
    A signed-in client against the real application.

    Depends on ``runner`` (a :class:`~wasm.core.runner.FakeRunner`) because
    the missing-page screen renders the machine strip, which shells out to
    ``systemctl``; without it a real subprocess would be attempted and the
    test would fail on that, not on the behaviour under test.

    Returns:
        A client already carrying a valid session and CSRF cookie.
    """
    test_client = build_client(sandbox)
    token = get_token_manager().generate_master_token()
    login(test_client, token)
    return test_client


def csrf(client: TestClient) -> dict[str, str]:
    """
    Build the header a mutation needs, from the session's own CSRF cookie.

    Args:
        client: A signed-in client.

    Returns:
        The header to attach to a ``POST``, ``PUT``, ``PATCH`` or ``DELETE``.
    """
    return {CSRF_HEADER_NAME: client.cookies[CSRF_COOKIE_NAME]}


class TestValidationErrors:
    """A 422 names every field that failed, not just the first."""

    def test_validation_errors_name_each_field(self, client: TestClient) -> None:
        """
        Two independently-invalid query parameters both show up in ``fields``.

        ``/api/apps`` validates its ``domain`` deep inside the handler (a
        ``DomainError``, not a pydantic failure), so it cannot pin the
        multi-field case; ``/api/monitor/processes`` fails both parameters at
        the pydantic layer in one request, which is what this defect is
        actually about.
        """
        response = client.get(
            "/api/monitor/processes", params={"limit": "not-a-number", "sort_by": "bogus"}
        )

        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "validation_error"
        assert set(body["fields"]) >= {"limit", "sort_by"}
        assert body["hint"] is None

    def test_a_single_field_error_still_uses_the_contract(self, client: TestClient) -> None:
        """The common case - one bad field - is not a special case of the shape."""
        response = client.get("/api/monitor/processes", params={"limit": "not-a-number"})

        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "validation_error"
        assert "limit" in body["fields"]


class TestManagerErrors:
    """A WASMError answers with its own status, not a bare 500."""

    def test_a_config_error_is_json_not_a_bare_500(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``config.py`` had no ``WASMErrorRoute``; a ``ConfigError`` crashed the request."""

        def raise_config_error(self: object, *_args: object, **_kwargs: object) -> None:
            raise ConfigError("bad key", details="use apps.directory")

        monkeypatch.setattr("wasm.core.config.Config.replace", raise_config_error)

        response = client.put("/api/config", json={"config": {"x": 1}}, headers=csrf(client))

        assert response.status_code == 400
        assert response.json() == {
            "error": "configerror",
            "detail": "bad key",
            "hint": "use apps.directory",
            "fields": None,
        }

    def test_a_service_not_found_uses_the_contract(self, client: TestClient) -> None:
        """``services.py`` had no ``WASMErrorRoute`` either; this is a plain ``HTTPException``."""
        response = client.get("/api/services/does-not-exist", headers=csrf(client))

        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "not_found"
        assert "does-not-exist" in body["detail"]
        assert body["fields"] is None

    def test_an_unsafe_service_name_is_a_validation_error_not_a_500(
        self, client: TestClient
    ) -> None:
        """
        A traversal payload is refused by the validator, caught by the new route class.

        ``%2f`` decodes to a literal ``/`` and stops matching the single-segment
        ``{name}`` route entirely (a 404, exercised in ``tests/test_web_services_api.py``
        against every traversal payload); a name that fails validation while
        staying inside one path segment is what exercises the route class here.
        """
        response = client.get(f"/api/services/{quote('evil name', safe='')}", headers=csrf(client))

        assert response.status_code == 400
        body = response.json()
        assert body["error"] in {"validationerror", "securityerror"}


class TestLoginFailuresAreMachineReadable:
    """Every reason ``/api/auth/login`` can fail carries its own ``error``."""

    def test_wrong_master_token_is_machine_readable(self, sandbox: Path) -> None:
        test_client = build_client(sandbox)

        response = test_client.post("/api/auth/login", json={"token": "wrong"})

        assert response.status_code == 401
        assert response.json()["error"] == "invalid_token"

    def test_missing_second_factor_is_machine_readable(self, sandbox: Path) -> None:
        test_client = build_client(sandbox)
        master = get_token_manager().generate_master_token()
        body = login(test_client, master)
        enable_totp(test_client, body["csrf_token"])

        response = test_client.post("/api/auth/login", json={"token": master})

        assert response.status_code == 401
        assert response.json()["error"] == "totp_required"

    def test_wrong_second_factor_is_machine_readable(self, sandbox: Path) -> None:
        test_client = build_client(sandbox)
        master = get_token_manager().generate_master_token()
        body = login(test_client, master)
        enable_totp(test_client, body["csrf_token"])

        response = test_client.post(
            "/api/auth/login", json={"token": master, "totp_code": "000000"}
        )

        assert response.status_code == 401
        assert response.json()["error"] == "invalid_totp"

    def test_a_locked_out_client_is_machine_readable(self, sandbox: Path) -> None:
        test_client = build_client(sandbox, max_failed_attempts=3, lockout_duration=60)

        for _ in range(3):
            test_client.post("/api/auth/login", json={"token": "wrong"})
        response = test_client.post("/api/auth/login", json={"token": "wrong"})

        assert response.status_code == 429
        assert response.json()["error"] == "locked_out"


class TestHtmlRoutesAreUntouched:
    """The JSON reshaping is scoped to ``/api``; server-rendered pages do not change."""

    def test_the_login_page_still_renders_html(self, sandbox: Path) -> None:
        app = create_app(make_config(sandbox))
        test_client = TestClient(app, client=("testclient", 50000))

        response = test_client.get("/login")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_a_missing_api_route_uses_the_contract(self, client: TestClient) -> None:
        """A mistyped API address is a 404 in the same shape as any other."""
        response = client.get("/api/does-not-exist")

        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_a_missing_page_still_renders_the_panels_own_screen(self, client: TestClient) -> None:
        """A mistyped panel address for a signed-in browser is unaffected."""
        response = client.get("/no-such-page", headers={"Accept": "text/html"})

        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]


class TestErrorRouteGuard:
    """Every router under ``wasm.web.api`` must install the error boundary."""

    def test_every_api_router_uses_the_error_route(self) -> None:
        """
        A router built without ``route_class=WASMErrorRoute`` answers Starlette's
        shape, not ours, for anything raised inside its own handlers.

        Walked with ``ast`` instead of imported: a module that constructs its
        router at import time with the wrong class should fail this test
        without needing the rest of the package to import cleanly.
        """
        offenders = []
        for path in sorted(API_PACKAGE_DIR.glob("*.py")):
            if path.name in {"__init__.py", "deps.py", "router.py"}:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "APIRouter"
                ):
                    continue
                has_error_route = any(
                    keyword.arg == "route_class"
                    and isinstance(keyword.value, ast.Name)
                    and keyword.value.id == "WASMErrorRoute"
                    for keyword in node.keywords
                )
                if not has_error_route:
                    offenders.append(f"{path.name}:{node.lineno}")

        assert offenders == [], f"routers missing route_class=WASMErrorRoute: {offenders}"
