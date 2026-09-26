# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Tests for the per-application lock.

Two operations on one application must never interleave: the second fails at
once, naming the first. The lock is real here (``flock`` on a file in the
test's own store directory); a second holder is a second thread, which the
lock refuses exactly as it refuses a second process.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from wasm.core.applock import AppBusyError, app_lock, is_held_here, lock_path
from wasm.core.fs import DryRunFileSystem, set_fs
from wasm.core.store import WASMStore

DOMAIN = "shop.example.com"


@pytest.fixture(autouse=True)
def store(tmp_path: Path) -> Iterator[WASMStore]:
    """A store in the test directory; the locks live beside it."""
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "state" / "wasm.db")
    yield instance
    WASMStore.reset_instance()


class Holder:
    """Holds an application's lock in another thread until told to let go."""

    def __init__(self, domain: str, operation: str) -> None:
        self.domain = domain
        self.operation = operation
        self.acquired = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with app_lock(self.domain, self.operation):
            self.acquired.set()
            self.release.wait(timeout=10)

    def __enter__(self) -> Holder:
        self.thread.start()
        assert self.acquired.wait(timeout=5)
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.thread.join(timeout=5)


def test_the_lock_file_lives_beside_the_store_not_in_the_application(
    store: WASMStore,
) -> None:
    assert lock_path(DOMAIN) == store.db_path.parent / "locks" / f"{DOMAIN}.lock"


def test_a_second_operation_fails_at_once_naming_the_one_running() -> None:
    with Holder(DOMAIN, "update"), pytest.raises(AppBusyError) as refused:
        with app_lock(DOMAIN, "rollback"):
            pytest.fail("the rollback must not run while the update does")

    error = refused.value
    assert error.holder is not None and error.holder.operation == "update"
    assert error.holder.pid == os.getpid()
    assert "update started at" in error.message
    assert "rollback of shop.example.com" in error.message
    assert "Wait for that one to finish" in error.details


def attempt_elsewhere(domain: str, operation: str) -> BaseException | None:
    """
    Try to take a lock from another thread, as a concurrent request would.

    Returns:
        What the attempt raised, or None when it got the lock.
    """
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            with app_lock(domain, operation):
                outcome.append(None)
        except AppBusyError as error:
            outcome.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=5)
    return outcome[0]


def test_the_holder_records_what_it_is_doing_and_clears_it_on_release() -> None:
    path = lock_path(DOMAIN)
    with app_lock(DOMAIN, "migration"):
        record = json.loads(path.read_text())
        assert record["operation"] == "migration"
        assert record["pid"] == os.getpid()
        assert oct(path.stat().st_mode & 0o777) == oct(0o600)
        assert oct(path.parent.stat().st_mode & 0o777) == oct(0o700)
    assert path.read_text() == ""


def test_the_lock_is_free_again_once_the_operation_ends() -> None:
    with Holder(DOMAIN, "update"):
        pass
    with app_lock(DOMAIN, "rollback"):
        assert is_held_here(DOMAIN)
    assert not is_held_here(DOMAIN)


def test_it_is_released_when_the_operation_raises() -> None:
    with pytest.raises(RuntimeError), app_lock(DOMAIN, "update"):
        raise RuntimeError("build failed")
    with Holder(DOMAIN, "rollback"):
        pass


def test_a_nested_operation_in_the_same_thread_reuses_the_lock() -> None:
    """An update's backup, a rollback's restore: inner operations of the holder."""
    with app_lock(DOMAIN, "update"):
        with app_lock(DOMAIN, "restore"):
            assert is_held_here(DOMAIN)
        # Leaving the inner block does not release the outer operation's lock.
        assert isinstance(attempt_elsewhere(DOMAIN, "deploy"), AppBusyError)
    assert not is_held_here(DOMAIN)
    assert attempt_elsewhere(DOMAIN, "deploy") is None


def test_applications_are_locked_independently() -> None:
    with Holder(DOMAIN, "update"), app_lock("blog.example.com", "update"):
        assert is_held_here("blog.example.com")


def test_another_process_holding_the_lock_is_refused_without_a_record() -> None:
    """A holder that has not written its record yet is still named as an operation."""
    path = lock_path(DOMAIN)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(AppBusyError, match="another WASM operation is still running"):
            with app_lock(DOMAIN, "update"):
                pass
    finally:
        os.close(fd)


def test_the_domain_is_the_key_whatever_its_case() -> None:
    with Holder("Shop.Example.com", "update"), pytest.raises(AppBusyError):
        with app_lock(DOMAIN, "rollback"):
            pass


def test_a_rehearsal_creates_no_lock_file() -> None:
    set_fs(DryRunFileSystem())
    with app_lock(DOMAIN, "update"):
        pass
    set_fs(None)
    assert not lock_path(DOMAIN).parent.exists()


def test_a_name_that_is_not_a_domain_is_refused() -> None:
    with pytest.raises(ValueError), app_lock("../../etc/passwd", "update"):
        pass
