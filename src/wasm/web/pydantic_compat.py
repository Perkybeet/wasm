# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
One import surface for the pydantic names that differ between v1 and v2.

The panel does not choose its pydantic. pip and Fedora install pydantic 2,
but Ubuntu 24.04 and Debian 12 package ``python3-pydantic`` 1.10, and the
.deb depends on the distribution package. FastAPI 0.100+ runs on either
major version, so the rest of the web layer never notices - until a module
imports a name only one major version has. That is how v1.5.0 shipped:
``from pydantic import field_validator`` imported cleanly under pip's
pydantic 2, and ``wasm web start`` died with ImportError on every Ubuntu
24.04 install.

This module bridges exactly the names the web layer uses, nothing more. It
is not a copy of pydantic and must not grow into one: a name goes here only
when the two majors spell it differently, and it keeps v2's spelling with
v2's semantics on both. ``tests/test_architecture.py`` forbids importing the
version-specific names from pydantic anywhere else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pydantic
from pydantic import BaseModel

#: True under pydantic 2. Version detection has to work on both majors, and
#: ``pydantic.VERSION`` is the one spelling they share.
PYDANTIC_V2 = int(pydantic.VERSION.split(".")[0]) >= 2

if TYPE_CHECKING or PYDANTIC_V2:
    from pydantic import field_validator as field_validator
else:
    from pydantic import validator as _v1_validator

    def field_validator(
        field: str, *fields: str, mode: str = "after", check_fields: bool | None = None
    ) -> Any:
        """
        pydantic v2's ``field_validator``, expressed as a v1 ``validator``.

        Only the surface the web layer uses is mapped: ``mode="before"``
        becomes v1's ``pre=True`` and ``mode="after"`` its default, both of
        which mean the same thing they mean in v2. ``allow_reuse`` is always
        set because v1 keeps a global registry of validator names and would
        otherwise refuse the module on a re-import, which is exactly what a
        reloading server does.

        The decorated function must take ``(cls, value)`` and nothing else:
        v2's ``ValidationInfo`` does not exist in v1, so a validator that
        needs more than the value cannot be bridged and must be refactored
        not to.

        Args:
            field: Name of the field to validate.
            *fields: Further field names, validated by the same function.
            mode: ``"before"`` or ``"after"``, with v2's meaning. The other
                v2 modes have no v1 equivalent and are refused.
            check_fields: Passed through to v1's ``check_fields`` untouched.

        Returns:
            The decorator, exactly as v2's ``field_validator`` would return.

        Raises:
            TypeError: When ``mode`` names a v2 mode v1 cannot express.
        """
        if mode not in ("before", "after"):
            raise TypeError(
                f"field_validator mode {mode!r} has no pydantic v1 equivalent; "
                "use 'before' or 'after'"
            )
        kwargs: dict[str, Any] = {"pre": mode == "before", "allow_reuse": True}
        if check_fields is not None:
            kwargs["check_fields"] = check_fields
        return _v1_validator(field, *fields, **kwargs)


def iso_offset_validator(*fields: str) -> Any:
    """
    A field_validator, bound to one or more fields, that attaches a UTC
    offset to a stored timestamp.

    Every response model that carries a timestamp applies this once, as a
    class attribute, instead of writing its own conversion:

    .. code-block:: python

        class DeploymentOut(BaseModel):
            started_at: str | None = None
            finished_at: str | None = None

            _iso_timestamps = iso_offset_validator("started_at", "finished_at")

    See :func:`wasm.core.timeutil.to_iso_offset` for what the conversion
    does. The import is local to this function, not module level: core must
    not depend on the web layer, and nothing here should tempt it to.

    Args:
        *fields: Names of the fields to convert.

    Returns:
        The validator, ready for assignment to a class attribute exactly as
        a hand-written ``@field_validator`` would be.
    """
    from wasm.core.timeutil import to_iso_offset

    def _convert(cls: Any, value: Any) -> Any:
        return to_iso_offset(value)

    return field_validator(*fields, mode="before")(_convert)


def dump_model(model: BaseModel) -> dict[str, Any]:
    """
    Serialise a model to a plain dict, whichever pydantic is installed.

    v2 spells this ``model_dump()`` and v1 ``dict()``; each major warns or
    fails on the other's spelling, so callers use this instead of either.

    Args:
        model: The model instance to serialise.

    Returns:
        The model's fields as a dictionary, as the installed pydantic builds
        it.
    """
    if PYDANTIC_V2:
        return model.model_dump()
    return model.dict()
