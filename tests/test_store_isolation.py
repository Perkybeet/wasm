"""The test suite never reaches the real store locations."""

from __future__ import annotations

from pathlib import Path

from wasm.core import store as store_module


def test_no_test_can_open_the_real_user_or_system_store(tmp_path: Path) -> None:
    """Both default locations live under this test's directory, not HOME or /var/lib."""
    assert tmp_path in store_module.USER_DB_PATH.parents
    assert tmp_path in store_module.DEFAULT_DB_PATH.parents
    assert Path.home() not in store_module.USER_DB_PATH.parents
