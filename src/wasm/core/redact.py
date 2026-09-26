# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
One scrubber for the text WASM keeps about the work it did.

Build output, job log lines and failure messages are raw tool output, and the
tools run with the application's environment: a build script that echoes
``$API_KEY``, a framework that prints its configuration when it fails, a
connection error that quotes the whole ``DATABASE_URL``. That text is stored in
the deployment history, the job log files and the job rows, and all of it is
readable with the ``read`` scope - a far wider audience than the ``.env`` it
came from, which is 0600.

:class:`Scrubber` replaces known secret values with :data:`REDACTED` wherever
that text is persisted or published. It works on values, not on shapes: it
cannot recognise a secret it was not told about, so its inputs matter -
:func:`secret_env_values` for an environment, :func:`app_secret_values` for a
deployed application and :func:`known_credentials` for WASM's own
configuration - and :func:`scrubber_for` combines them for the usual case.

Values shorter than :data:`MIN_SECRET_LENGTH` are never scrubbed: a secret
variable set to ``true`` or ``test`` would otherwise blank out every ordinary
word containing it and make the log unreadable, and a value that short is not
protecting anything worth the cost.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import unquote

from wasm.core.config import REDACTED, Config, redact_secrets
from wasm.core.exceptions import WASMError

logger = logging.getLogger(__name__)

#: The shortest value treated as a secret. See the module docstring.
MIN_SECRET_LENGTH = 6


class Scrubber:
    """
    Replace known secret values in text with :data:`REDACTED`.

    Thread-safe: a job adds its application's values from the worker thread
    while its log lines are being scrubbed, and the replacement pattern is
    swapped in one assignment so a reader never sees a half-built one.
    """

    def __init__(self, values: Iterable[str] = ()) -> None:
        """
        Args:
            values: Secret values to scrub from the start.
        """
        self._lock = threading.Lock()
        self._values: frozenset[str] = frozenset()
        self._pattern: re.Pattern[str] | None = None
        self.add(values)

    def __bool__(self) -> bool:
        """True when there is at least one value to scrub."""
        return self._pattern is not None

    def add(self, values: Iterable[str]) -> None:
        """
        Scrub these values as well from now on.

        A multi-line value (a PEM key) is also added line by line, because a
        tool that prints it usually prints it one line per log call.

        Args:
            values: Secret values. Non-strings, and strings shorter than
                :data:`MIN_SECRET_LENGTH` once trimmed, are ignored.
        """
        fresh: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            lines = (line.strip() for line in value.splitlines())
            for candidate in {value, value.strip(), *lines}:
                if len(candidate.strip()) >= MIN_SECRET_LENGTH:
                    fresh.add(candidate)
        with self._lock:
            if fresh <= self._values:
                return
            self._values = self._values | fresh
            # Longest first, so a secret that contains another is replaced
            # whole instead of leaving its remainder behind.
            ordered = sorted(self._values, key=len, reverse=True)
            self._pattern = re.compile("|".join(re.escape(value) for value in ordered))

    def scrub(self, text: str) -> str:
        """
        Replace every known secret in a piece of text.

        Args:
            text: Text about to be stored or shown.

        Returns:
            The text with each secret value replaced by :data:`REDACTED`.
        """
        pattern = self._pattern
        if pattern is None or not text:
            return text
        return pattern.sub(REDACTED, text)


def secret_env_values(env: Mapping[str, Any]) -> list[str]:
    """
    Pick the values of an environment that are secrets.

    Uses the classifiers WASM already redacts ``.env`` listings with, rather
    than a third opinion: :func:`~wasm.core.config.redact_secrets`, which
    splits a name into words (``api_key``, ``AuthToken``),
    :data:`~wasm.deployers.helpers.env_manager.EnvManager.SECRET_PATTERNS`,
    which matches substrings such as ``_PASS``, and the password inside any
    connection string, whatever the variable is called.

    Args:
        env: Variable name to value. Non-string values are ignored.

    Returns:
        The secret values, including each URL password both as written and
        percent-decoded.
    """
    # Imported here, not at the top: wasm.deployers imports the deployment
    # recorder, which imports this module, so a top-level import would make
    # the core layer depend on the deployers package being importable first.
    from wasm.deployers.helpers.env_manager import URL_CREDENTIALS, EnvManager

    strings = {str(key): value for key, value in env.items() if isinstance(value, str)}
    by_word = redact_secrets(strings)
    values: list[str] = []
    for key, value in strings.items():
        if not value:
            continue
        upper = key.upper()
        if by_word.get(key) == REDACTED or any(p in upper for p in EnvManager.SECRET_PATTERNS):
            values.append(value)
        for match in URL_CREDENTIALS.finditer(value):
            password = match.group(0)[len(match.group("prefix")) : -1]
            values.extend({password, unquote(password)})
    return values


def config_secret_values(config: Any) -> list[str]:
    """
    Collect the secret values of a configuration structure.

    Walks the structure alongside :func:`~wasm.core.config.redact_secrets`'
    output, so a value is a secret here exactly when ``wasm config show``
    would hide it.

    Args:
        config: Configuration mapping, sequence or scalar.

    Returns:
        Every string value the redaction replaces.
    """
    values: list[str] = []

    def walk(original: Any, redacted: Any) -> None:
        if isinstance(original, dict) and isinstance(redacted, dict):
            for key, value in original.items():
                walk(value, redacted.get(key))
        elif isinstance(original, (list, tuple)) and isinstance(redacted, list):
            for item, masked in zip(original, redacted, strict=False):
                walk(item, masked)
        elif isinstance(original, str) and redacted == REDACTED and original != REDACTED:
            values.append(original)

    walk(config, redact_secrets(config))
    return values


def known_credentials() -> list[str]:
    """
    Collect WASM's own credentials: database passwords, SMTP, webhooks.

    Returns:
        The secret values of the loaded configuration; none when it cannot
        be read, which is logged.
    """
    try:
        return config_secret_values(Config().to_dict())
    except (WASMError, OSError) as exc:
        logger.warning("Could not read the configuration to scrub its credentials: %s", exc)
        return []


def app_secret_values(domain: str) -> list[str]:
    """
    Collect the secrets of a deployed application.

    Its ``.env``, the variables recorded on its row and on its unit's row (an
    older deploy kept them there), and its webhook secret. An application that
    is not deployed has none. Reading them is best effort: a store or a file
    that cannot be read is logged and skipped, because the caller is about to
    write a log line or record an outcome, and the one thing worse than a
    partly scrubbed line is losing the deployment's record altogether.

    Args:
        domain: The application's domain.

    Returns:
        The secret values found.
    """
    # Deferred for the same reason as in secret_env_values: these live in the
    # deployers package, which imports this module.
    from wasm.core.store import get_store
    from wasm.core.utils import domain_to_app_name
    from wasm.deployers.helpers.app_env import read_app_env

    values: list[str] = []
    try:
        store = get_store()
        app = store.get_app(domain)
        if app is None:
            return values
        values.extend(secret_env_values(app.env_vars))
        service = store.get_service(domain_to_app_name(domain))
        if service is not None:
            values.extend(secret_env_values(service.environment))
        webhook_secret = store.get_webhook_secret(domain)
        if webhook_secret:
            values.append(webhook_secret)
        values.extend(secret_env_values(read_app_env(app)))
    except (WASMError, OSError, sqlite3.Error) as exc:
        logger.warning("Could not read the secrets of %s to scrub its logs: %s", domain, exc)
    return values


def scrubber_for(domain: str | None = None, env: Mapping[str, Any] | None = None) -> Scrubber:
    """
    Build the scrubber for work on one application.

    Args:
        domain: The application, when the work concerns one.
        env: Extra variables the work runs with, such as the environment a
            fresh deploy was given before any ``.env`` exists.

    Returns:
        A scrubber for WASM's credentials, the application's secrets and the
        secret values of ``env``.
    """
    scrubber = Scrubber(known_credentials())
    if domain:
        scrubber.add(app_secret_values(domain))
    if env:
        scrubber.add(secret_env_values(env))
    return scrubber
