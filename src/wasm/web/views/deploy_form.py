# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The deployment form and the git webhook section.

Own module so concurrent panel work never edits the aggregate router. Unlike
the other sub-view modules it is included *above* ``/apps/{domain}`` in
``views/router.py``, not at the bottom: routes match in declaration order, and
``/apps/new`` declared after the parametrised route would be read as an
application called "new".

Handlers follow the same contract as the rest of the views: synchronous,
session-guarded, rendering Jinja fragments over the managers. Every mutation
here is an adapter over the JSON API - :func:`wasm.web.api.apps.create_app`
for the form, :mod:`wasm.web.api.hooks` for the webhook secret - because a
second implementation of "deploy an application" or "mint a webhook secret"
is exactly what the panel must never grow.

Three surfaces live here:

- The deployment form at ``/apps/new``: the page, the per-field checks htmx
  posts to as the operator types, and the type-specific advanced options
  fragment that follows the type select.
- The submission, which queues the same job the JSON API queues and sends the
  operator to the activity screen to watch it.
- The application page's git webhook section: mint a secret (shown once),
  rotate it, disable it, and the recent deliveries that actually deployed.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from wasm.core.exceptions import DomainError, WASMError
from wasm.core.store import DeploymentTrigger, get_store
from wasm.validators.environment import EnvironmentValidationError, validate_environment
from wasm.web.views.rendering import page
from wasm.web.views.router import WEBSERVERS, PageErrorRoute, require_page_session

# Annotated explicitly: this module and router.py import each other, and inside
# that cycle mypy cannot infer the type of a module-level variable another
# module reads.
router: APIRouter = APIRouter(
    include_in_schema=False,
    dependencies=[Depends(require_page_session)],
    route_class=PageErrorRoute,
)

#: How many webhook-triggered deployments the webhook section lists. The full
#: history is one click away on the domain's history page.
WEBHOOK_DELIVERIES = 5


def _form_values(fields: dict[str, list[str]]) -> dict[str, Any]:
    """
    Shape a parsed form body into what the templates and handlers read.

    Every field the form can carry is present, so a refusal re-renders the
    form with everything the operator typed still in place, and the advanced
    fields exist even when the browser did not send them.

    Args:
        fields: The parsed urlencoded body, one list of values per name.

    Returns:
        First value per field, stripped; checkboxes as booleans.
    """

    def field(name: str, default: str = "") -> str:
        """
        Args:
            name: Field name.
            default: Value to use when the field was not submitted.

        Returns:
            The submitted value, stripped.
        """
        return fields.get(name, [default])[0].strip()

    return {
        "domain": field("domain"),
        "source": field("source"),
        "app_type": field("app_type", "auto"),
        "branch": field("branch"),
        "port": field("port"),
        "webserver": field("webserver", "nginx"),
        "ssl": "ssl" in fields,
        "env": field("env"),
        "subdomains": field("subdomains"),
        "workspaces": field("workspaces"),
        "no_database": "no_database" in fields,
        "compose_file": field("compose_file"),
        "compose_profiles": field("compose_profiles"),
    }


def _parse_body(body: bytes) -> dict[str, Any]:
    """
    Parse an urlencoded form submission.

    ``parse_qs`` rather than ``request.form()`` so the panel does not acquire
    python-multipart, which would have to be declared in four packaging files
    and exist on every target distribution.

    Args:
        body: The raw request body.

    Returns:
        The shaped form values.
    """
    return _form_values(parse_qs(body.decode("utf-8", errors="replace")))


def _parse_subdomain_mappings(text: str) -> dict[str, str]:
    """
    Parse the "workspace:subdomain per line" textarea into a mapping.

    The same ``app:subdomain`` shape ``wasm create --subdomain`` accepts. A
    blank line or one starting with ``#`` is skipped; a malformed line is
    refused rather than silently dropped, because a mapping the deployer never
    saw is a workspace served on a subdomain the operator did not choose.

    Args:
        text: Raw textarea content.

    Returns:
        Workspace name to subdomain mapping.

    Raises:
        ValueError: When a line is not a ``workspace:subdomain`` pair.
    """
    mappings: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, subdomain = line.partition(":")
        if not separator or not name.strip() or not subdomain.strip():
            raise ValueError(f"Not a workspace:subdomain mapping: {line}")
        mappings[name.strip()] = subdomain.strip()
    return mappings


def _parse_names(text: str) -> list[str]:
    """
    Parse a comma or whitespace separated list of names.

    Args:
        text: Raw field content.

    Returns:
        The names, in the order typed.
    """
    return [name for chunk in text.split(",") for name in chunk.split()]


def _advanced_options(submitted: dict[str, Any]) -> dict[str, Any]:
    """
    Parse the advanced section into the options the deployers accept.

    Only what :meth:`MonorepoDeployer.configure` and
    :meth:`DockerComposeDeployer.configure` already read is produced here:
    ``subdomain_overrides``, ``workspace_filter`` and ``skip_database`` for a
    monorepo, ``compose_file`` and ``compose_profiles`` for a compose stack,
    and ``env_vars`` for everything. The deployers ignore the options that do
    not concern them, which is the interface's own contract.

    Args:
        submitted: The shaped form values.

    Returns:
        Keyword arguments for :class:`~wasm.web.api.apps.CreateAppRequest`.

    Raises:
        ValueError: When a subdomain mapping is malformed.
        EnvironmentValidationError: When a variable cannot be safely written.
    """
    from wasm.web.views.router import _parse_env_text

    return {
        "env_vars": validate_environment(_parse_env_text(submitted["env"])),
        "subdomain_overrides": _parse_subdomain_mappings(submitted["subdomains"]),
        "workspace_filter": _parse_names(submitted["workspaces"]) or None,
        "skip_database": submitted["no_database"],
        "compose_file": submitted["compose_file"] or None,
        "compose_profiles": _parse_names(submitted["compose_profiles"]) or None,
    }


def _deploy_form(
    request: Request, submitted: dict[str, Any], problem: dict[str, str] | None = None
) -> HTMLResponse:
    """
    Render the deployment form.

    A refusal answers 400 to a plain client and 200 to an htmx one: htmx does
    not swap an error status, so a boosted submission answered 400 would leave
    the operator on a form that looks like it did nothing, with the reason
    delivered nowhere.

    Args:
        request: The incoming request.
        submitted: What the operator typed, so a refusal does not empty the
            form they have to correct.
        problem: A ``fix``/``output`` mapping when a submission was refused.

    Returns:
        The deployment page.
    """
    from wasm.deployers.registry import available_types

    refused = 200 if request.headers.get("HX-Request") else 400
    return page(
        request,
        "pages/deploy.html",
        {
            "app_types": available_types(),
            "webservers": WEBSERVERS,
            "submitted": submitted,
            "problem": problem,
        },
        status_code=refused if problem else 200,
    )


@router.get("/apps/new", response_class=HTMLResponse)
def deploy_form(request: Request) -> HTMLResponse:
    """
    Render the form that deploys an application.

    Args:
        request: The incoming request.

    Returns:
        The deployment page.
    """
    return _deploy_form(
        request, _form_values({"ssl": ["yes"], "app_type": ["auto"], "webserver": ["nginx"]})
    )


def _check_field(name: str, submitted: dict[str, Any]) -> str | None:
    """
    Validate one field the way the chokepoint will, and say so early.

    These checks are a courtesy, not the guard: the domain is validated again
    by :func:`~wasm.web.api.apps.create_app`, the environment by the same
    :func:`validate_environment` on submission, the port by the port
    validator. A field this function does not know is a 404, so a template
    pointing a check at a field nobody validates fails loudly instead of
    reporting "fine" about a value nothing looked at.

    Args:
        name: The field being checked.
        submitted: The shaped form values.

    Returns:
        The refusal in plain words, or None when the value passes.

    Raises:
        HTTPException: 404 when no inline check exists for the field.
    """
    value = str(submitted.get(name, ""))

    if name == "domain":
        from wasm.web.api.deps import strict_domain

        if not value:
            return "A domain is required."
        try:
            validated = strict_domain(value)
        except DomainError as exc:
            return str(exc)
        if get_store().get_app(validated) is not None:
            return f"Application already exists: {validated}"
        return None

    if name == "source":
        return None if value else "A source is required: a Git URL, or a path on this machine."

    if name == "port":
        if not value:
            return None
        try:
            number = int(value)
        except ValueError:
            return f"Not a port number: {value}"
        return None if 1 <= number <= 65535 else "A port is between 1 and 65535."

    if name == "env":
        from wasm.web.views.router import _parse_env_text

        try:
            validate_environment(_parse_env_text(value))
        except EnvironmentValidationError as exc:
            return str(exc)
        return None

    if name == "subdomains":
        try:
            _parse_subdomain_mappings(value)
        except ValueError as exc:
            return str(exc)
        return None

    raise HTTPException(status_code=404, detail=f"No inline check for field: {name}")


@router.post("/apps/new/validate/{field}", response_class=HTMLResponse)
def deploy_validate(field: str, request: Request, body: bytes = Body(default=b"")) -> HTMLResponse:
    """
    Answer one field's inline check for an htmx swap.

    Always 200, whatever the verdict: htmx does not swap an error status, and
    the verdict - including "this is fine", which clears a stale refusal - is
    the fragment itself.

    Args:
        field: The field being checked.
        request: The incoming request.
        body: The urlencoded form, so a check can read sibling fields.

    Returns:
        The check fragment, holding the refusal or nothing.
    """
    submitted = _parse_body(body)
    return page(
        request,
        "fragments/deploy_form_check.html",
        {"check_field": field, "check_error": _check_field(field, submitted)},
    )


@router.get("/apps/new/options", response_class=HTMLResponse)
def deploy_type_options(request: Request) -> HTMLResponse:
    """
    Render the advanced options for the type the operator just picked.

    The type select swaps this fragment on change, so the form only asks the
    questions the chosen deployer will actually read: subdomains, workspaces
    and the database switch for a monorepo, the compose file and profiles for
    a compose stack, nothing for everything else.

    Args:
        request: The incoming request, carrying the form's current values as
            query parameters.

    Returns:
        The options fragment.
    """
    fields: dict[str, list[str]] = {}
    for name, value in request.query_params.multi_items():
        fields.setdefault(name, []).append(value)
    return page(request, "fragments/deploy_form_options.html", {"submitted": _form_values(fields)})


@router.post("/apps/new")
def deploy_submit(request: Request, body: bytes = Body(default=b"")) -> Response:
    """
    Queue a deployment from the form.

    The work is done by :func:`wasm.web.api.apps.create_app`, which is the same
    function the JSON API calls. This route only translates a form submission
    into the request model and a refusal into a screen: a second implementation
    of "deploy an application" is exactly what the panel must never grow.

    Synchronous, like every other page handler: the body arrives as a
    ``Body(bytes)`` parameter FastAPI reads off the event loop, so nothing here
    needs the ``async`` keyword or the threadpool juggling the old handler in
    ``views/router.py`` did.

    Args:
        request: The incoming request.
        body: The urlencoded form submission.

    Returns:
        A redirect to the activity screen, or the form again with the reason it
        was refused.
    """
    from pydantic import ValidationError

    from wasm.web.api.apps import CreateAppRequest, create_app

    submitted = _parse_body(body)

    try:
        advanced = _advanced_options(submitted)
    except EnvironmentValidationError as exc:
        return _deploy_form(
            request, submitted, {"fix": "Check the environment variables.", "output": str(exc)}
        )
    except ValueError as exc:
        return _deploy_form(
            request, submitted, {"fix": "Check the Advanced section.", "output": str(exc)}
        )

    try:
        model = CreateAppRequest(
            domain=submitted["domain"],
            source=submitted["source"],
            app_type=submitted["app_type"],
            port=int(submitted["port"]) if submitted["port"] else None,
            webserver=submitted["webserver"],
            branch=submitted["branch"] or None,
            ssl=submitted["ssl"],
            **advanced,
        )
    except (ValueError, ValidationError) as exc:
        return _deploy_form(request, submitted, {"fix": "Check the form.", "output": str(exc)})

    session = getattr(request.state, "session", {})

    try:
        create_app(model, session)
    except HTTPException as exc:
        # 409 for a domain already deployed, 503 when no port is free. Both are
        # answers to what was typed, so the form comes back with them.
        return _deploy_form(request, submitted, {"fix": str(exc.detail), "output": ""})
    except WASMError as exc:
        return _deploy_form(
            request,
            submitted,
            {"fix": str(exc), "output": getattr(exc, "details", "") or ""},
        )

    # A deployment takes minutes, so the operator is sent where they can watch
    # it rather than left on a form that looks like it did nothing.
    return RedirectResponse("/activity", status_code=303)


# The git webhook section ----------------------------------------------------


def _webhook_context(
    request: Request,
    domain: str,
    *,
    minted: Any = None,
    problem: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Build the context the webhook fragment renders from.

    The secret is never in it unless it was minted by this very request:
    the store keeps it for signature verification, and no later render can
    repeat it. Whether webhooks are enabled is the only thing a re-render
    learns.

    Args:
        request: The incoming request, for the hook URL.
        domain: The application's domain.
        minted: A freshly minted :class:`WebhookSecretResponse`, shown once.
        problem: A ``fix``/``output`` mapping when an action was refused.

    Returns:
        The fragment context, including the recent deliveries that actually
        deployed - the store's history filtered to the webhook trigger.
    """
    from wasm.web.views.deployments import _shape_deployment

    store = get_store()
    enabled = minted is not None or store.get_webhook_secret(domain) is not None
    rows = [
        _shape_deployment(record)
        for record in store.list_deployments(domain=domain)
        if record.triggered_by == DeploymentTrigger.WEBHOOK.value
    ][:WEBHOOK_DELIVERIES]

    return {
        "webhook_domain": domain,
        "webhook_enabled": enabled,
        "webhook_url": f"{str(request.base_url).rstrip('/')}/hooks/deploy/{domain}",
        "webhook_minted": minted,
        "webhook_problem": problem,
        "webhook_rows": rows,
    }


def _webhook_fragment(request: Request, domain: str, **kwargs: Any) -> HTMLResponse:
    """
    Render the webhook section for an htmx swap.

    Refusals render at 200 on purpose: htmx does not swap an error status, so
    a 400 here would leave the screen frozen and report the refusal nowhere.

    Args:
        request: The incoming request.
        domain: The application's domain.
        **kwargs: Forwarded to :func:`_webhook_context`.

    Returns:
        The rendered fragment.
    """
    return page(
        request, "fragments/webhook_section.html", _webhook_context(request, domain, **kwargs)
    )


@router.get("/apps/{domain}/webhook/section", response_class=HTMLResponse)
def webhook_section(domain: str, request: Request) -> HTMLResponse:
    """
    Render the application page's webhook section, for its htmx load.

    Args:
        domain: The application's domain.
        request: The incoming request.

    Returns:
        The fragment, in whatever state the application's webhook is in.
    """
    return _webhook_fragment(request, domain)


# Adapters over the JSON API, exactly like the deployment form above: the
# minting, the rotation, the disabling and their audit trail all live in
# wasm.web.api.hooks, and a second implementation of "issue a webhook secret"
# is what the panel must never grow.


@router.post("/apps/{domain}/webhook/enable", response_class=HTMLResponse)
def webhook_enable(domain: str, request: Request) -> HTMLResponse:
    """
    Mint (or rotate) the webhook secret and show it exactly once.

    Args:
        domain: The application's domain.
        request: The incoming request.

    Returns:
        The fragment with the fresh secret and the hook URL, or with the
        refusal inline.
    """
    from wasm.web.api.hooks import create_webhook_secret

    session = getattr(request.state, "session", {})
    try:
        minted = create_webhook_secret(domain, request, session)
    except HTTPException as exc:
        return _webhook_fragment(request, domain, problem={"fix": str(exc.detail), "output": ""})
    except WASMError as exc:
        return _webhook_fragment(
            request, domain, problem={"fix": str(exc), "output": getattr(exc, "details", "") or ""}
        )
    return _webhook_fragment(request, domain, minted=minted)


@router.post("/apps/{domain}/webhook/disable", response_class=HTMLResponse)
def webhook_disable(domain: str, request: Request) -> HTMLResponse:
    """
    Disable auto-deploy by discarding the application's webhook secret.

    Args:
        domain: The application's domain.
        request: The incoming request.

    Returns:
        The fragment in its disabled state, or with the refusal inline.
    """
    from wasm.web.api.hooks import delete_webhook_secret

    session = getattr(request.state, "session", {})
    try:
        delete_webhook_secret(domain, request, session)
    except HTTPException as exc:
        return _webhook_fragment(request, domain, problem={"fix": str(exc.detail), "output": ""})
    except WASMError as exc:
        return _webhook_fragment(
            request, domain, problem={"fix": str(exc), "output": getattr(exc, "details", "") or ""}
        )
    return _webhook_fragment(request, domain)
