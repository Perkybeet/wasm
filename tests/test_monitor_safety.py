"""
Safety tests for the process monitor.

The monitor used to be an antivirus: it matched a substring against a process
command line and, from a systemd unit running as root, terminated the process
tree and passed the process working directory to ``shutil.rmtree``. A process
with ``cwd=/tmp`` meant deleting ``/tmp``.

These tests pin the decision that replaced it: the monitor observes and
reports, it never acts. Anything that would kill a process or delete a file is
a regression, so the guard test reads the package source rather than any single
code path.
"""

from __future__ import annotations

import ast
import os
import shutil
import smtplib
import tempfile
import types
from pathlib import Path
from typing import Any, ClassVar

import psutil
import pytest

MONITOR_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "wasm" / "monitor"

#: Calls that destroy processes or files. None of them belong in a monitor.
DESTRUCTIVE_CALLS = frozenset(
    {
        "rmtree",
        "unlink",
        "remove",
        "rmdir",
        "removedirs",
        "kill",
        "killpg",
        "terminate",
        "send_signal",
    }
)


class _FakeProcess:
    """Stand-in for ``psutil.Process`` as returned by ``process_iter``."""

    def __init__(self, **info: Any) -> None:
        self.info = info
        self.pid = info["pid"]

    def name(self) -> str:
        return self.info["name"]

    def net_connections(self, kind: str = "inet") -> list[Any]:
        return []

    def open_files(self) -> list[Any]:
        return []

    def children(self, recursive: bool = False) -> list[Any]:
        return []


class _RecordingStore:
    """Captures whatever the monitor decides to persist."""

    def __init__(self) -> None:
        self.saved: list[Any] = []

    def save_many(self, items: list[Any]) -> list[int]:
        self.saved.extend(items)
        return list(range(len(items)))

    # The pre-refactor name, so the same test drives both implementations.
    def save_threats(self, items: list[Any]) -> list[int]:
        return self.save_many(items)

    def purge_older_than(self, days: int) -> int:
        return 0


class _RecordingNotifier:
    """Swallows notifications instead of opening an SMTP connection."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send_observation_alert(self, observations: list[Any]) -> bool:
        self.sent.append(observations)
        return True

    # The pre-refactor name.
    def send_threat_alert(self, reports: list[Any], is_final: bool = False) -> bool:
        return self.send_observation_alert(reports)


def _make_process(**overrides: Any) -> _FakeProcess:
    """
    Build a fake psutil process, defaulting to a plausible miner.

    Args:
        overrides: Fields to replace in the default info dictionary.

    Returns:
        A fake process object accepted by the monitor's collection code.
    """
    info: dict[str, Any] = {
        "pid": 424242,
        "name": "xmrig",
        "username": "nobody",
        "cpu_percent": 99.0,
        "memory_percent": 12.0,
        "cmdline": ["./xmrig", "--donate-level", "1"],
        "create_time": 0.0,
        # A zero ppid keeps the collector from looking up a real parent.
        "ppid": 0,
        "status": "running",
        "num_threads": 4,
        "cwd": "/var/empty",
    }
    info.update(overrides)
    return _FakeProcess(**info)


def _install_processes(monkeypatch: pytest.MonkeyPatch, processes: list[_FakeProcess]) -> None:
    """
    Make ``psutil.process_iter`` yield the given fakes.

    Args:
        monkeypatch: Patching helper, scoped to the test.
        processes: Fake processes to expose.
    """
    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda attrs=None, *args, **kwargs: iter(list(processes)),
    )


def _build_monitor(store: _RecordingStore, notifier: _RecordingNotifier) -> Any:
    """
    Build a ProcessMonitor with its persistence and notification stubbed.

    Args:
        store: Stub observation store.
        notifier: Stub notifier.

    Returns:
        The monitor instance.
    """
    from wasm.monitor.process_monitor import MonitorConfig, ProcessMonitor

    try:
        return ProcessMonitor(config=MonitorConfig(), store=store, notifier=notifier)
    except TypeError:
        # The pre-refactor constructor allowed no injection; reach in instead so
        # the test can still exercise the vulnerable code path.
        monitor = ProcessMonitor(config=MonitorConfig())
        monitor.threat_store = store
        monitor.notifier = notifier
        if hasattr(monitor, "analyzer"):
            monitor.analyzer.api_key = ""
        return monitor


@pytest.fixture
def process_workdir() -> Path:
    """
    A directory under the real system temp dir, standing in for a process cwd.

    Returns:
        Path to the directory, removed when the test ends.
    """
    if not Path("/tmp").is_dir():
        pytest.skip("no /tmp on this system")
    path = Path(tempfile.mkdtemp(prefix="wasm-monitor-test-", dir="/tmp"))
    (path / "important.txt").write_text("do not delete me")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_scan_never_deletes_the_working_directory_of_a_flagged_process(
    monkeypatch: pytest.MonkeyPatch,
    process_workdir: Path,
) -> None:
    """A process whose cwd is under /tmp must not cost anyone that directory."""
    signals_sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals_sent.append((pid, sig)))
    _install_processes(monkeypatch, [_make_process(cwd=str(process_workdir))])

    store = _RecordingStore()
    monitor = _build_monitor(store, _RecordingNotifier())

    # Pre-refactor the scan also hunted for persistence with a real subprocess,
    # which the suite blocks; that happened after the deletion, so the failure
    # is recorded and the filesystem assertions still run.
    failure: BaseException | None = None
    try:
        monitor.scan_once()
    except BaseException as exc:
        failure = exc

    assert process_workdir.is_dir(), (
        f"monitor deleted the working directory of the flagged process ({failure!r})"
    )
    assert (process_workdir / "important.txt").read_text() == "do not delete me"
    assert signals_sent == [], "monitor signalled a process instead of only reporting it"
    assert failure is None, f"scan raised {failure!r}"


def test_scan_records_the_flagged_process_as_an_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection stays, as a signal a human can read, not as an action."""
    _install_processes(monkeypatch, [_make_process()])

    store = _RecordingStore()
    monitor = _build_monitor(store, _RecordingNotifier())

    observations = monitor.scan_once()

    assert len(observations) == 1
    observation = observations[0]
    assert observation.process.pid == 424242
    assert observation.process.name == "xmrig"
    assert observation.signal == "name-pattern"
    assert "xmrig" in observation.detail
    assert store.saved == observations


def test_known_safe_process_is_not_flagged_by_its_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading a miner's name is not being a miner: grep must stay unflagged."""
    _install_processes(
        monkeypatch,
        [
            _make_process(
                pid=17,
                name="grep",
                cpu_percent=1.0,
                memory_percent=0.5,
                cmdline=["grep", "-r", "xmrig", "/var/log"],
            )
        ],
    )

    monitor = _build_monitor(_RecordingStore(), _RecordingNotifier())

    assert monitor.scan_once() == []


def test_observations_survive_a_round_trip_through_the_store(tmp_path: Path) -> None:
    """The persistence layer keeps the signal, not a verdict about it."""
    from wasm.monitor.models import ProcessInfo, ProcessObservation
    from wasm.monitor.observation_store import ObservationStore

    store = ObservationStore(db_path=tmp_path / "observations.db")
    observation = ProcessObservation(
        process=ProcessInfo(pid=99, name="xmrig", user="nobody", cpu_percent=97.5),
        signal="name-pattern",
        severity="warning",
        detail="Executable name matches the known-malware pattern 'xmrig'.",
    )

    (row_id,) = store.save_many([observation])
    stored = store.get(row_id)

    assert stored is not None
    assert stored["process_name"] == "xmrig"
    assert stored["severity"] == "warning"
    assert stored["acknowledged"] == 0
    assert "action_taken" not in stored, "the store still speaks in terms of actions taken"

    assert [r["id"] for r in store.recent()] == [row_id]
    assert store.acknowledge(row_id) is True
    assert store.recent() == []
    assert store.stats() == {
        "total": 1,
        "warning": 1,
        "notice": 0,
        "acknowledged": 1,
        "open": 0,
    }


def _iter_monitor_sources() -> list[Path]:
    """
    List the Python sources of the monitor package.

    Returns:
        Every ``.py`` file shipped in ``wasm.monitor``.
    """
    return sorted(p for p in MONITOR_PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def test_monitor_package_contains_no_destructive_calls() -> None:
    """No kill, no rmtree, no unlink anywhere in the package."""
    sources = _iter_monitor_sources()
    assert sources, f"no sources found under {MONITOR_PACKAGE}"

    offences: list[str] = []
    for path in sources:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                called = func.attr
            elif isinstance(func, ast.Name):
                called = func.id
            else:
                continue
            if called in DESTRUCTIVE_CALLS:
                offences.append(f"{path.name}:{node.lineno} calls {called}()")

    assert offences == [], "destructive calls in the monitor package: " + "; ".join(offences)


def test_monitor_package_does_not_import_shutil_or_signal() -> None:
    """The imports that only served termination and cleanup are gone too."""
    forbidden = {"shutil", "signal"}
    offences: list[str] = []
    for path in _iter_monitor_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            for name in sorted(names & forbidden):
                offences.append(f"{path.name}:{node.lineno} imports {name}")

    assert offences == [], "termination-era imports still present: " + "; ".join(offences)


def test_monitor_package_does_not_call_third_party_apis() -> None:
    """Process data from a customer server never leaves the box."""
    offences: list[str] = []
    for path in _iter_monitor_sources():
        source = path.read_text()
        for needle in ("httpx", "openai", "api.openai.com", "requests."):
            if needle in source:
                offences.append(f"{path.name} mentions {needle}")

    assert offences == [], "outbound analysis leftovers: " + "; ".join(offences)


def _install_psutil_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Replace every psutil call the metric collector makes with fixed values.

    Args:
        monkeypatch: Patching helper, scoped to the test.
    """
    monkeypatch.setattr(psutil, "cpu_percent", lambda interval=None: 12.5)
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 4)
    monkeypatch.setattr(psutil, "getloadavg", lambda: (0.5, 0.4, 0.3))
    monkeypatch.setattr(
        psutil,
        "virtual_memory",
        lambda: types.SimpleNamespace(total=8_000, used=2_000, available=6_000, percent=25.0),
    )
    monkeypatch.setattr(
        psutil,
        "swap_memory",
        lambda: types.SimpleNamespace(total=1_000, used=100, percent=10.0),
    )
    monkeypatch.setattr(
        psutil,
        "disk_partitions",
        lambda all=False: [
            types.SimpleNamespace(device="/dev/sda1", mountpoint="/", fstype="ext4", opts="rw"),
            types.SimpleNamespace(device="proc", mountpoint="/proc", fstype="proc", opts="rw"),
        ],
    )
    monkeypatch.setattr(
        psutil,
        "disk_usage",
        lambda path: types.SimpleNamespace(total=100_000, used=40_000, free=60_000, percent=40.0),
    )
    monkeypatch.setattr(
        psutil,
        "net_io_counters",
        lambda: types.SimpleNamespace(
            bytes_sent=1_111,
            bytes_recv=2_222,
            packets_sent=11,
            packets_recv=22,
        ),
    )
    monkeypatch.setattr(psutil, "boot_time", lambda: 1_000.0)
    monkeypatch.setattr(psutil, "pids", lambda: [1, 2, 3])


def test_collect_resource_metrics_reads_cpu_memory_disk_and_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The part of the monitor that actually earns its keep."""
    from wasm.monitor.metrics import collect_resource_metrics

    _install_psutil_metrics(monkeypatch)
    monkeypatch.setattr("time.time", lambda: 4_600.0)

    metrics = collect_resource_metrics()

    assert metrics.cpu_percent == 12.5
    assert metrics.cpu_count == 4
    assert metrics.load_average == (0.5, 0.4, 0.3)
    assert metrics.memory_total_bytes == 8_000
    assert metrics.memory_used_bytes == 2_000
    assert metrics.memory_percent == 25.0
    assert metrics.swap_percent == 10.0
    assert metrics.net_bytes_sent == 1_111
    assert metrics.net_bytes_recv == 2_222
    assert metrics.process_count == 3
    assert metrics.uptime_seconds == 3_600.0

    # Pseudo filesystems carry no capacity worth reporting.
    assert [d.mountpoint for d in metrics.disks] == ["/"]
    assert metrics.disks[0].percent == 40.0
    assert metrics.disks[0].used_bytes == 40_000


def test_collect_resource_metrics_survives_an_unreadable_mountpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CD-ROM slot that raises must not take the whole scan down."""
    from wasm.monitor.metrics import collect_resource_metrics

    _install_psutil_metrics(monkeypatch)

    def _explode(path: str) -> Any:
        raise PermissionError(path)

    monkeypatch.setattr(psutil, "disk_usage", _explode)

    metrics = collect_resource_metrics()

    assert metrics.disks == ()
    assert metrics.cpu_percent == 12.5


def test_list_processes_maps_psutil_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process collection keeps the fields an operator needs to identify one."""
    from wasm.monitor.metrics import list_processes

    _install_processes(
        monkeypatch,
        [
            _make_process(pid=1, name="systemd", cmdline=["/sbin/init"], cpu_percent=0.1),
            _make_process(pid=2, name="node", cmdline=["node", "server.js"], ppid=1),
        ],
    )

    processes = list_processes()

    assert [p.pid for p in processes] == [1, 2]
    assert processes[1].command == "node server.js"
    assert processes[1].parent_pid == 1
    assert processes[1].parent_name == "systemd"
    assert processes[0].user == "nobody"


def test_installing_the_unit_writes_a_file_and_reloads_through_the_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: Any,
) -> None:
    """systemd is driven through the audited seam, with an absolute ExecStart."""
    from wasm.monitor import process_monitor as module

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wasm_bin = bin_dir / "wasm"
    wasm_bin.write_text("#!/bin/sh\n")
    wasm_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(module, "SYSTEMD_DIR", tmp_path / "systemd")

    monitor = module.ProcessMonitor(config=module.MonitorConfig(), runner=runner)

    assert monitor.install_service() is True

    unit = monitor.unit_path.read_text()
    assert f"ExecStart={wasm_bin} monitor run" in unit
    assert "NoNewPrivileges=true" in unit
    assert ("systemctl", "daemon-reload") in runner.calls


def test_service_health_uses_the_command_runner(runner: Any) -> None:
    """Service checks go through the audited seam, never bare subprocess."""
    from wasm.monitor.metrics import collect_service_health

    runner.script(["systemctl", "is-active"], stdout="active\n")
    runner.script(["systemctl", "is-enabled"], stdout="enabled\n")

    health = collect_service_health(["wasm-example-com"], runner=runner)

    assert len(health) == 1
    assert health[0].unit == "wasm-example-com"
    assert health[0].active is True
    assert health[0].enabled is True
    assert ("systemctl", "is-active", "wasm-example-com") in runner.calls


class _FakeSMTP:
    """Records the arguments the notifier hands to smtplib."""

    instances: ClassVar[list[_FakeSMTP]] = []

    def __init__(self, host: str = "", port: int = 0, **kwargs: Any) -> None:
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.logged_in: tuple[str, str] | None = None
        self.started_tls = False
        self.sent: list[tuple[str, list[str], str]] = []
        _FakeSMTP.instances.append(self)

    def starttls(self, context: Any = None) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        self.logged_in = (username, password)

    def sendmail(self, sender: str, recipients: list[str], message: str) -> None:
        self.sent.append((sender, recipients, message))

    def quit(self) -> None:
        pass


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSMTP]:
    """
    Replace both smtplib entry points with a recording double.

    Returns:
        The double class, whose ``instances`` list holds every connection.
    """
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


def _notifier(**overrides: Any) -> Any:
    """
    Build an EmailNotifier with explicit SMTP settings.

    Args:
        overrides: Fields to replace in the default SMTP configuration.

    Returns:
        The notifier instance.
    """
    from wasm.monitor.email_notifier import EmailNotifier, SMTPConfig

    settings: dict[str, Any] = {
        "host": "smtp.example.com",
        "port": 465,
        "username": "alerts@example.com",
        "password": "hunter2-do-not-log",
        "use_ssl": True,
    }
    settings.update(overrides)
    return EmailNotifier(smtp_config=SMTPConfig(**settings), recipients=["ops@example.com"])


def test_smtp_connection_always_carries_a_timeout(fake_smtp: type[_FakeSMTP]) -> None:
    """A monitor loop with one thread cannot afford an SMTP socket with no deadline."""
    _notifier()._create_connection()

    assert len(fake_smtp.instances) == 1
    timeout = fake_smtp.instances[0].kwargs.get("timeout")
    assert timeout is not None, "SMTP connection opened without a timeout"
    assert 0 < timeout <= 120


def test_starttls_is_used_when_ssl_is_off(fake_smtp: type[_FakeSMTP]) -> None:
    """Credentials never travel over a plaintext session."""
    _notifier(use_ssl=False, use_tls=True, port=587)._create_connection()

    connection = fake_smtp.instances[0]
    assert connection.started_tls is True
    assert connection.kwargs.get("timeout") is not None


def test_plaintext_login_is_refused(fake_smtp: type[_FakeSMTP]) -> None:
    """Sending a password in the clear is a configuration error, not a default."""
    from wasm.core.exceptions import EmailError

    with pytest.raises(EmailError) as excinfo:
        _notifier(use_ssl=False, use_tls=False, port=25)._create_connection()

    assert fake_smtp.instances == []
    assert "hunter2-do-not-log" not in str(excinfo.value)
    assert "hunter2-do-not-log" not in str(getattr(excinfo.value, "details", ""))


def test_non_positive_timeout_is_rejected() -> None:
    """A zero timeout is the hang this test exists to prevent."""
    from wasm.core.exceptions import EmailError
    from wasm.monitor.email_notifier import SMTPConfig

    with pytest.raises(EmailError):
        SMTPConfig(host="smtp.example.com", port=465, username="u", password="p", timeout=0)


def test_smtp_failure_does_not_leak_the_password(
    monkeypatch: pytest.MonkeyPatch,
    fake_smtp: type[_FakeSMTP],
) -> None:
    """Whatever the server says back, the password stays out of the message."""
    from wasm.core.exceptions import EmailError

    def _fail(self: Any, username: str, password: str) -> None:
        raise smtplib.SMTPAuthenticationError(535, f"rejected {password}".encode())

    monkeypatch.setattr(_FakeSMTP, "login", _fail)

    with pytest.raises(EmailError) as excinfo:
        _notifier()._create_connection()

    rendered = f"{excinfo.value} {getattr(excinfo.value, 'details', '')}"
    assert "hunter2-do-not-log" not in rendered
