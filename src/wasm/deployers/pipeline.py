# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The deployment pipeline: a list of steps, and an undo for each one.

``BaseDeployer.deploy`` used to be a 120-line try block whose rollback was a
separate method that re-derived, from scratch and by guessing, what the failed
run might have created. It guessed wrong in the interesting cases: a run that
died during ``create_site`` still removed the *pre-existing* service of an app
being redeployed, and a run that died before the store insert left the app row
behind because rollback only deleted rows it could still find by domain.

Here a step declares its own undo, and only the steps that actually ran are
undone, in reverse. That is the difference between "clean up the usual
suspects" and "put the machine back".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from wasm.core.logger import Logger


@dataclass(frozen=True)
class DeployStep:
    """
    One named unit of deployment work.

    Attributes:
        title: What the user is told is happening.
        icon: Logger icon for the step header.
        run: Performs the work. Raises to fail the deployment.
        undo: Reverses the work. Called only if ``run`` was entered, and only
            when a later step failed. None means the step leaves nothing behind.
        skip_if: Evaluated just before the step; when it returns True the step
            is announced as skipped and its undo is never registered.
    """

    title: str
    icon: str
    run: Callable[[], object]
    undo: Callable[[], object] | None = None
    skip_if: Callable[[], bool] | None = None


class StepFailure(Exception):
    """Internal marker: a step raised, and the raiser is preserved as ``cause``."""

    def __init__(self, step: DeployStep, cause: BaseException):
        """
        Args:
            step: The step that failed.
            cause: The original exception.
        """
        super().__init__(f"Step failed: {step.title}")
        self.step = step
        self.cause = cause


def run_pipeline(steps: list[DeployStep], logger: Logger) -> None:
    """
    Execute steps in order, undoing the completed ones if any step fails.

    Args:
        steps: The steps to run, in order.
        logger: Logger used for step headers and rollback reporting.

    Raises:
        BaseException: Whatever the failing step raised, re-raised unchanged
            after the rollback so callers keep the real diagnosis.
    """
    total = len(steps)
    completed: list[DeployStep] = []

    for index, step in enumerate(steps, start=1):
        if step.skip_if is not None and step.skip_if():
            logger.step(index, total, f"Skipping {step.title}", step.icon)
            continue

        logger.step(index, total, step.title, step.icon)
        # The step is recorded before it runs, not after: a step that fails
        # halfway has already created part of what it undoes.
        completed.append(step)
        try:
            step.run()
        except BaseException as exc:
            _undo(completed, logger)
            raise exc


def _undo(completed: list[DeployStep], logger: Logger) -> None:
    """
    Reverse the completed steps, most recent first.

    Args:
        completed: Steps that were entered, in execution order.
        logger: Logger used to report rollback progress.
    """
    undoable = [s for s in completed if s.undo is not None]
    if not undoable:
        return

    logger.warning("Rolling back partial deployment...")
    failures = 0
    for step in reversed(completed):
        if step.undo is None:
            continue
        try:
            step.undo()
        # Rollback is an error boundary: one undo that cannot complete must not
        # stop the remaining ones from running, or the rollback itself leaves
        # the machine in a worse state than the failure did.
        except Exception as exc:  # noqa: BLE001
            failures += 1
            logger.debug(f"Rollback of '{step.title}' failed: {exc}")

    if failures:
        logger.warning(f"Rollback completed with {failures} error(s); check manually")
    else:
        logger.info("Rollback completed successfully")
