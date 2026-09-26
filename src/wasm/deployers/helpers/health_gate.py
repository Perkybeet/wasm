# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The health gate: restart what serves the active release, and decide whether it is up.

A release becomes active by a deploy, by an update, or by an operator going
back to an earlier one. All three must be judged the same way - a rollback
that activated a release the deploy would have refused, or the reverse, is a
rule with two answers - so the gate is one class both
:class:`~wasm.deployers.base.BaseDeployer` and
:func:`~wasm.deployers.lifecycle.activate_release` build.

What it does not decide is what happens next: the caller re-activates the
release that was serving and says so, because only the caller knows which one
that was.

What it asks is the application's own :class:`HealthCheck`: the path, the
statuses that mean up and how long to wait, as the operator set them with
``wasm app health`` or the console, and 2.0's rule for whatever they did not.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from wasm.core.exceptions import WASMError
from wasm.core.logger import Logger
from wasm.deployers.helpers.health import answers, wait_until_healthy
from wasm.validators.health import parse_health_expect

if TYPE_CHECKING:
    from wasm.core.store import App

#: How long a new release gets to answer before it is rolled back: attempts,
#: and seconds between them. Longer than the post-deploy check, because a
#: failed gate throws a good build away while a failed report only warns.
HEALTH_GATE_ATTEMPTS = 15
HEALTH_GATE_DELAY = 2.0

#: What the gate probes when neither the application nor its deployer says.
DEFAULT_HEALTH_PATH = "/"

#: Seconds a release gets to answer when the application does not say: the
#: attempts and the delay above, which is what every 2.0 gate waited.
DEFAULT_HEALTH_TIMEOUT = int(HEALTH_GATE_ATTEMPTS * HEALTH_GATE_DELAY)

#: How the default expectation reads to an operator.
DEFAULT_EXPECT_DESCRIPTION = "any status below 500"

#: Journal lines attached to a failed health gate, so the deployment record
#: shows why the process did not come up, not only that it did not.
HEALTH_GATE_JOURNAL_LINES = 40

#: How :func:`~wasm.deployers.helpers.health.wait_until_healthy` reports a
#: failed attempt.
_ATTEMPT = re.compile(r"^Health check attempt (\d+) failed: (.*)$", re.DOTALL)

#: The probe's signature: :func:`~wasm.deployers.helpers.health.wait_until_healthy`.
Probe = Callable[..., bool]


@dataclass(frozen=True)
class HealthCheck:
    """
    What the health gate asks of one application.

    The values come from the store, which validated them on the way in, so
    this only interprets them. None for any of them is the 2.0 behaviour.

    Attributes:
        path: The path probed on 127.0.0.1, such as ``/healthz``.
        expect: The statuses that mean up, such as ``200-399``, or None for
            any status below 500.
        timeout: Seconds the application gets to answer, or None for
            :data:`DEFAULT_HEALTH_TIMEOUT`.
    """

    path: str = DEFAULT_HEALTH_PATH
    expect: str | None = None
    timeout: int | None = None

    @classmethod
    def for_app(cls, app: App | None, *, default_path: str = DEFAULT_HEALTH_PATH) -> HealthCheck:
        """
        Read an application's health check off its row.

        Args:
            app: The row, or None for an application not registered yet.
            default_path: The deployer's own path, used when the application
                names none.

        Returns:
            The check.
        """
        if app is None:
            return cls(path=default_path)
        return cls(
            path=app.health_path or default_path,
            expect=app.health_expect,
            timeout=app.health_timeout,
        )

    def url(self, port: int | None) -> str:
        """
        Say what to request.

        Args:
            port: The port the application listens on.

        Returns:
            ``http://127.0.0.1:<port><path>``: always the application itself,
            never a name that could resolve elsewhere.
        """
        return f"http://127.0.0.1:{port}{self.path}"

    def accepts(self, status: int) -> bool:
        """
        Tell whether a status means the application is up.

        Args:
            status: The HTTP status it answered with.

        Returns:
            Whether it is in the expectation, or below 500 without one.
        """
        if self.expect is None:
            return answers(status)
        return any(low <= status <= high for low, high in parse_health_expect(self.expect))

    @property
    def seconds(self) -> int:
        """Seconds the application gets to answer."""
        return self.timeout if self.timeout is not None else DEFAULT_HEALTH_TIMEOUT

    def attempts(self, delay: float) -> int:
        """
        Turn the wait into probes.

        Args:
            delay: Seconds between probes.

        Returns:
            As many probes as fit in :attr:`seconds`, at least one; exactly
            :data:`HEALTH_GATE_ATTEMPTS` without a timeout of its own.
        """
        if self.timeout is None:
            return HEALTH_GATE_ATTEMPTS
        return max(1, math.ceil(self.timeout / delay))

    def describe_expect(self) -> str:
        """Say which statuses count, as an operator reads it."""
        return self.expect if self.expect is not None else DEFAULT_EXPECT_DESCRIPTION


class UnitControl(Protocol):
    """The part of the service manager the gate needs."""

    def restart(self, name: str) -> None:
        """Restart a unit."""

    def logs(self, name: str, lines: int = 50) -> str:
        """Read the last lines of a unit's journal."""


class HealthGate:
    """
    Restart an application and probe it, collecting the evidence when it fails.

    Attributes:
        unit: The unit to restart, or None for a site nothing runs for.
        url: What to probe over HTTP, or None for a site served off disk.
        check: The application's health check, when it has one of its own.
    """

    def __init__(
        self,
        *,
        unit: str | None,
        url: str | None,
        services: UnitControl,
        logger: Logger,
        probe: Probe = wait_until_healthy,
        restart: Callable[[], object] | None = None,
        files_check: Callable[[], bool] | None = None,
        check: HealthCheck | None = None,
        attempts: int | None = None,
        delay: float = HEALTH_GATE_DELAY,
    ) -> None:
        """
        Initialize the gate.

        Args:
            unit: The unit to restart and whose journal is evidence, or None.
            url: What to probe, or None when nothing answers HTTP by itself.
            services: Where restarts and journals come from.
            logger: Where each attempt is reported.
            probe: The HTTP probe. Injectable because the deployers' tests
                replace it where they look it up.
            restart: What restarts the application, when it is not simply
                the unit (a deployer's own ``restart``). Defaults to
                restarting ``unit``, or nothing without one.
            files_check: Decides a site served off disk, which has no URL of
                its own to probe. Without one, such a site passes.
            check: The application's health check: which statuses mean up
                and how long to wait. The caller builds ``url`` from its
                path. None is the 2.0 rule: below 500, for 30 seconds.
            attempts: How many probes before giving up. None derives them
                from the check's timeout.
            delay: Seconds between probes.
        """
        self.unit = unit
        self.url = url
        self._services = services
        self._logger = logger
        self._probe = probe
        self._restart = restart
        self._files_check = files_check
        self.check = check
        self._attempts = (
            attempts if attempts is not None else (check or HealthCheck()).attempts(delay)
        )
        self._accept: Callable[[int], bool] = check.accepts if check is not None else answers
        self._delay = delay
        self._failures: list[str] = []

    def restart_and_probe(self) -> tuple[bool, str]:
        """
        Restart on whatever ``current`` points at, and ask if it is up.

        Returns:
            Whether it is healthy, and when it is not, the evidence: the
            failed probes and the unit's journal, verbatim.
        """
        self._failures = []
        try:
            if self._restart is not None:
                self._restart()
            elif self.unit:
                self._services.restart(self.unit)
        except WASMError as exc:
            # str() of a WASMError carries its details: systemctl's own output.
            return False, self.evidence(str(exc))

        if self.url is None:
            if self._files_check is None or self._files_check():
                return True, ""
            return False, self.evidence("The release has nothing to serve.")

        self._logger.substep(f"Checking: {self.url}")
        healthy = self._probe(
            self.url,
            retries=self._attempts,
            delay=self._delay,
            on_attempt=self._note_attempt,
            accept=self._accept,
        )
        if healthy:
            return True, ""
        return False, self.evidence("The application did not answer the health check.")

    def evidence(self, summary: str) -> str:
        """
        Put together what a failed health gate shows the operator.

        Args:
            summary: What failed, in one line or a command's own output.

        Returns:
            The summary, every failed probe, and the last lines of the unit's
            journal, which is where a process that crashed says why.
        """
        parts = [summary]
        if self._failures:
            parts.append("\n".join(collapse_attempts(self._failures)))
        if self.unit:
            try:
                journal = self._services.logs(self.unit, lines=HEALTH_GATE_JOURNAL_LINES).strip()
            except WASMError as exc:
                journal = f"(the journal could not be read: {exc})"
            if journal:
                parts.append(f"Last lines of the journal of {self.unit}:\n{journal}")
        return "\n\n".join(parts)

    def _note_attempt(self, message: str) -> None:
        """
        Keep a failed probe for the evidence, and log it like any other.

        Args:
            message: What the probe reported.
        """
        self._failures.append(message)
        self._logger.debug(message)


def collapse_attempts(messages: Sequence[str]) -> list[str]:
    """
    Fold consecutive probe failures with the same cause into one line.

    Fifteen identical "connection refused" lines say less than one line that
    names the range, and push the journal, which says why, out of sight.

    Args:
        messages: Failed attempts, in order, as the probe reported them.

    Returns:
        The same information, one line per run of identical causes.
    """
    runs: list[tuple[str, str, str]] = []
    for message in messages:
        match = _ATTEMPT.match(message)
        if match is None:
            runs.append(("", "", message))
            continue
        number, cause = match.groups()
        if runs and runs[-1][0] and runs[-1][2] == cause:
            runs[-1] = (runs[-1][0], number, cause)
        else:
            runs.append((number, number, cause))
    lines: list[str] = []
    for first, last, cause in runs:
        if not first:
            lines.append(cause)
        elif first == last:
            lines.append(f"Health check attempt {first} failed: {cause}")
        else:
            lines.append(f"Health check attempts {first}-{last} failed: {cause}")
    return lines
