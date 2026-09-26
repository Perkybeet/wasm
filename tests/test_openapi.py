# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for the API's OpenAPI contract.

``panel/openapi.json`` is the one input of the console's generated types
(``npm run gen:api``), so three things have to hold or the console is either
locked out of the schema, silently typed as ``unknown`` for something it
actually gets a shape for, or built against a contract the server has since
moved past:

- The schema is only served to an authenticated caller - an anonymous client
  gets nothing, the same way the rest of the API answers nothing useful
  without a session or a token.
- Every JSON response the API declares actually has a shape. FastAPI answers
  a route with no ``response_model`` and no return annotation with an empty
  ``{}`` schema, which ``openapi-typescript`` turns into ``unknown`` -
  useless to the panel and indistinguishable, in the generated types, from a
  route nobody has looked at yet.
- ``scripts/export_openapi.py`` writes a deterministic file, and its
  ``--check`` mode fails loudly the moment the committed
  ``panel/openapi.json`` stops matching the API that generates it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI

from tests.test_web_auth import build_client, login, make_config
from wasm.core.runner import FakeRunner
from wasm.web.server import create_app, get_token_manager


@pytest.fixture
def app(sandbox: Path, runner: FakeRunner) -> FastAPI:
    """
    Build the application the way the panel and the export script do.

    Args:
        sandbox: Per-test temporary directory.
        runner: Fake process runner, installed so nothing this touches shells
            out for real.

    Returns:
        The configured application.
    """
    return create_app(make_config(sandbox))


class TestSchemaIsAuthenticated:
    """The schema of an API that runs systemd as root is not free to read."""

    def test_an_anonymous_client_is_refused(self, sandbox: Path, runner: FakeRunner) -> None:
        client = build_client(sandbox)

        response = client.get("/api/openapi.json")

        assert response.status_code == 401

    def test_a_signed_in_client_reads_the_schema(self, sandbox: Path, runner: FakeRunner) -> None:
        client = build_client(sandbox)
        token = get_token_manager().generate_master_token()
        login(client, token)

        response = client.get("/api/openapi.json")

        assert response.status_code == 200
        body = response.json()
        assert body["openapi"].startswith("3.")
        # A real document, not an empty shell: it names at least the route
        # that served it.
        assert "/api/apps" in body["paths"]

    def test_docs_and_redoc_stay_disabled(self, app: FastAPI) -> None:
        """
        The interactive docs UIs serve the schema to whoever can reach them
        with no session at all, which is exactly what serving the schema
        itself behind ``require_auth`` is meant to prevent. Checked on the
        application object rather than by requesting ``/docs``: the SPA's
        catch-all route answers every unmatched path with 200 and the shell
        page, by design, so a status code there would not tell docs being
        off from docs never having been reachable in the first place.
        """
        assert app.docs_url is None
        assert app.redoc_url is None


class TestEveryEndpointIsTyped:
    """A dict without a model is an ``unknown`` in TypeScript."""

    def test_no_json_response_has_an_empty_schema(self, app: FastAPI) -> None:
        """
        Scoped to ``/api``: the webhook surface mounted at ``/hooks`` is a
        git forge calling in with an HMAC signature, not a JSON client of
        this product, and Task 1.2 deliberately leaves it out of the
        reshaped error and response contract the rest of the API carries.
        """
        schema = app.openapi()

        untyped = [
            f"{method.upper()} {path} -> {status}"
            for path, operations in schema["paths"].items()
            if path.startswith("/api")
            for method, operation in operations.items()
            if method in {"get", "post", "put", "patch", "delete"}
            for status, response in operation.get("responses", {}).items()
            if status.isdigit()
            and int(status) < 300
            and "application/json" in response.get("content", {})
            and response["content"]["application/json"]["schema"] == {}
        ]

        assert untyped == []

    def test_the_new_health_and_diagnose_endpoints_are_typed(self, app: FastAPI) -> None:
        schema = app.openapi()

        health_schema = schema["paths"]["/api/system/health"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        diagnose_schema = schema["paths"]["/api/apps/{domain}/diagnose"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]

        assert health_schema != {}
        assert diagnose_schema != {}


class TestDefaultsStayOptional:
    """
    A field with a default must not come out required just because FastAPI
    generates a separate input and output schema for a model used as both a
    request and a response body.
    """

    def test_no_component_marks_a_defaulted_field_as_required(self, app: FastAPI) -> None:
        schema = app.openapi()
        offenders = []

        for name, definition in schema.get("components", {}).get("schemas", {}).items():
            properties = definition.get("properties", {})
            required = set(definition.get("required", []))
            for field_name, spec in properties.items():
                if "default" in spec and field_name in required:
                    offenders.append(f"{name}.{field_name}")

        assert offenders == []


class TestTheSchemaCarriesTheRealVersion:
    """
    The reported defect: ``info.version`` was the literal ``1.0.0`` whatever
    release was running, so the schema an operator fetched from their panel
    named a version that never existed.
    """

    def test_the_served_schema_names_the_running_release(self, app: FastAPI) -> None:
        from wasm import __version__

        assert app.openapi()["info"]["version"] == __version__

    def test_the_export_does_not_depend_on_the_installed_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A committed file stamped with the version would go stale on every
        release bump, and would differ between a development install and a
        release build of the same API, so ``--check`` could never pass for
        both. The export writes a placeholder instead.
        """
        from scripts.export_openapi import EXPORTED_VERSION, export_openapi

        first = export_openapi(tmp_path / "a.json")
        monkeypatch.setattr("wasm.web.server.__version__", "99.0.0.dev7")
        second = export_openapi(tmp_path / "b.json")

        assert first.read_bytes() == second.read_bytes()
        assert json.loads(first.read_text())["info"]["version"] == EXPORTED_VERSION


class TestExportIsDeterministic:
    """``scripts/export_openapi.py`` is the console's one source of truth."""

    def test_two_exports_of_the_same_api_are_byte_identical(self, tmp_path: Path) -> None:
        from scripts.export_openapi import export_openapi

        first = export_openapi(tmp_path / "a.json")
        second = export_openapi(tmp_path / "b.json")

        assert first.read_bytes() == second.read_bytes()

    def test_the_file_is_sorted_indented_json_with_one_trailing_newline(
        self, tmp_path: Path
    ) -> None:
        from scripts.export_openapi import export_openapi

        destination = export_openapi(tmp_path / "schema.json")
        text = destination.read_text()

        assert text.endswith("\n")
        assert not text.endswith("\n\n")
        parsed = json.loads(text)
        assert text == json.dumps(parsed, sort_keys=True, indent=2) + "\n"

    def test_check_passes_against_what_export_just_wrote(self, tmp_path: Path) -> None:
        from scripts.export_openapi import export_openapi, main

        destination = tmp_path / "openapi.json"
        export_openapi(destination)

        assert main(["--check", "--output", str(destination)]) == 0

    def test_check_detects_a_stale_file(self, tmp_path: Path) -> None:
        from scripts.export_openapi import export_openapi, main

        destination = tmp_path / "openapi.json"
        export_openapi(destination)
        destination.write_text('{"stale": true}\n')

        assert main(["--check", "--output", str(destination)]) == 1

    def test_check_reports_a_missing_file(self, tmp_path: Path) -> None:
        from scripts.export_openapi import main

        assert main(["--check", "--output", str(tmp_path / "missing.json")]) == 1
