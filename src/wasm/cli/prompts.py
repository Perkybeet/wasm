# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Interactive prompts.

Built on questionary rather than inquirer. ``python3-inquirer`` does not exist
in Debian or Ubuntu, so on the distributions most WASM users run, interactive
mode was gated behind a package they could never install; questionary is
packaged everywhere WASM builds.

The shapes here mirror the ones the interactive flow was written against, so
the migration did not mean rewriting fifty-nine call sites under pressure. They
are deliberately thin: a question is a name, a message and whatever that kind
of question needs, and :func:`prompt` returns a dict of answers or None when
the operator cancels.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

try:
    import questionary

    AVAILABLE = True
except ImportError:  # pragma: no cover - questionary is a hard dependency
    AVAILABLE = False


@dataclass
class Question:
    """
    One prompt.

    Attributes:
        name: Key the answer is returned under.
        message: What the operator is asked.
        default: Value used when they press enter.
        validate: Called with the answer; return True or a message explaining
            what is wrong.
    """

    name: str
    message: str
    default: Any = None
    validate: Callable[..., bool | str] | None = None


@dataclass
class List(Question):
    """
    A single choice from a list.

    Attributes:
        choices: Either plain strings, or ``(label, value)`` pairs where the
            label is shown and the value is returned.
    """

    choices: Sequence[Any] = field(default_factory=list)


@dataclass
class Checkbox(Question):
    """Several choices from a list."""

    choices: Sequence[Any] = field(default_factory=list)


@dataclass
class Text(Question):
    """A line of text."""


@dataclass
class Password(Question):
    """A line of text that is not echoed."""


@dataclass
class Confirm(Question):
    """A yes or no."""


def _choices(raw: Sequence[Any]) -> list[questionary.Choice]:
    """
    Turn the call sites' choice format into questionary's.

    Args:
        raw: Strings, or ``(label, value)`` pairs.

    Returns:
        Choices questionary understands.
    """
    out = []
    for choice in raw:
        if isinstance(choice, tuple) and len(choice) == 2:
            label, value = choice
            out.append(questionary.Choice(title=str(label), value=value))
        else:
            out.append(questionary.Choice(title=str(choice), value=choice))
    return out


def _one_argument(validate: Callable[..., bool | str]) -> Callable[[str], bool | str]:
    """
    Adapt a validator to the single argument questionary passes.

    The call sites were written for inquirer, which passes the answers so far
    and then the value, so they read ``lambda _, value: ...``. Absorbing that
    here keeps fifty-nine of them working and stops the difference becoming a
    TypeError the first time an operator types something invalid, which is the
    one moment a prompt has to behave.

    Args:
        validate: A validator taking either one argument or two.

    Returns:
        A validator taking the value alone.
    """
    import inspect

    try:
        takes = len(inspect.signature(validate).parameters)
    except (TypeError, ValueError):
        takes = 1
    if takes >= 2:
        return lambda value: validate(None, value)
    return validate


def _ask(question: Question) -> Any:
    """
    Ask one question.

    Args:
        question: The question to ask.

    Returns:
        The answer, or None when the operator cancelled.
    """
    common: dict[str, Any] = {}
    if question.validate is not None:
        common["validate"] = _one_argument(question.validate)

    if isinstance(question, List):
        return questionary.select(
            question.message, choices=_choices(question.choices), default=question.default
        ).ask()
    if isinstance(question, Checkbox):
        return questionary.checkbox(question.message, choices=_choices(question.choices)).ask()
    if isinstance(question, Password):
        return questionary.password(question.message, **common).ask()
    if isinstance(question, Confirm):
        return questionary.confirm(question.message, default=bool(question.default)).ask()
    return questionary.text(question.message, default=str(question.default or ""), **common).ask()


def prompt(questions: Sequence[Question], **_ignored: Any) -> dict[str, Any] | None:
    """
    Ask a series of questions.

    Args:
        questions: The questions, asked in order.
        **_ignored: Accepted and dropped, so a caller passing a theme from the
            previous library keeps working.

    Returns:
        The answers keyed by question name, or None when the operator cancelled
        any of them. Returning None for a cancellation rather than a partial
        dict is what stops a half-answered form being acted on.
    """
    answers: dict[str, Any] = {}
    for question in questions:
        answer = _ask(question)
        if answer is None:
            return None
        answers[question.name] = answer
    return answers
