# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
The single seam through which WASM executes external processes.

Every call to nginx, systemctl, certbot, git, npm, mysqldump and friends goes
through a ``CommandRunner``. Nothing else in the codebase may import
``subprocess`` directly; a test fixture enforces that rule by making real
process execution fail.

Three properties are non-negotiable here, because each one maps to a defect
class that this module exists to make impossible:

- **Argv only, never a shell.** Commands are sequences of arguments. There is
  no ``shell=`` parameter and no string splitting, so a domain name or a
  database dump can never be reinterpreted as shell syntax.
- **Timeouts are mandatory.** Every call has a deadline. A hung ``git clone``
  or ``certbot`` blocks a deploy, not the process forever.
- **Secrets never travel in argv.** Anything on a command line is visible in
  ``ps`` to every user on the box. Passwords go through ``env`` or ``stdin``,
  and ``redact`` keeps them out of the logs.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from wasm.core.exceptions import WASMError

#: Deadline applied when a caller does not pass one. Chosen to be comfortably
#: longer than any system query (systemctl, nginx -t) and shorter than any
#: operation a user would expect to block on.
DEFAULT_TIMEOUT = 60

#: Exit code convention for a command that never completed.
EXIT_TIMEOUT = -1

#: Exit code POSIX shells use for "command not found".
EXIT_NOT_FOUND = 127

_REDACTED = "***"


class CommandError(WASMError):
    """A command failed and the caller asked for failures to be fatal."""


@dataclass(frozen=True)
class CommandResult:
    """
    Outcome of a single external process execution.

    Attributes:
        argv: The exact argument vector that was executed, already redacted.
        exit_code: Process exit status. Negative values mean the process was
            killed by a signal, except EXIT_TIMEOUT which means it never
            finished.
        stdout: Captured standard output, or empty when streaming.
        stderr: Captured standard error, or empty when streaming.
        duration: Wall-clock seconds the process ran.
        timed_out: True when the deadline was hit.
    """

    argv: tuple[str, ...]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False

    @property
    def success(self) -> bool:
        """True when the process exited cleanly."""
        return self.exit_code == 0

    @property
    def command(self) -> str:
        """The redacted command line, for logs and error messages."""
        return " ".join(self.argv)

    @property
    def output(self) -> str:
        """Stdout with surrounding whitespace removed."""
        return self.stdout.strip()

    def __bool__(self) -> bool:
        """Allow ``if runner.run(...):`` to read naturally."""
        return self.success

    def check(self) -> CommandResult:
        """
        Return self, raising if the command failed.

        Returns:
            This result, so the call can be chained.

        Raises:
            CommandError: When the exit code is non-zero.
        """
        if self.success:
            return self
        raise CommandError(
            f"Command failed with exit code {self.exit_code}: {self.command}",
            details=(self.stderr or self.stdout).strip() or None,
        )


def _redact(argv: Sequence[str], secrets: Iterable[str]) -> tuple[str, ...]:
    """
    Replace every occurrence of a secret in an argument vector.

    Args:
        argv: The argument vector to sanitise.
        secrets: Literal values that must not reach a log.

    Returns:
        The argument vector with secrets substituted.
    """
    values = [s for s in secrets if s]
    if not values:
        return tuple(argv)
    out = []
    for arg in argv:
        for secret in values:
            arg = arg.replace(secret, _REDACTED)
        out.append(arg)
    return tuple(out)


def _validate(argv: Sequence[str]) -> list[str]:
    """
    Reject argument vectors that cannot be executed safely.

    Args:
        argv: Candidate argument vector.

    Returns:
        The vector as a list of strings.

    Raises:
        ValueError: When the vector is empty, is a bare string, or contains a
            NUL byte (which would silently truncate the argument at the execve
            boundary).
    """
    if isinstance(argv, (str, bytes)):
        raise ValueError(
            "argv must be a sequence of arguments, not a string. "
            "Passing a string invites shell-injection through naive splitting."
        )
    args = [str(a) for a in argv]
    if not args:
        raise ValueError("argv must not be empty")
    for arg in args:
        if "\x00" in arg:
            raise ValueError("argv arguments must not contain NUL bytes")
    return args


class CommandRunner(ABC):
    """Executes external processes. The only such thing in the codebase."""

    @abstractmethod
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        input: str | None = None,
        user: str | None = None,
        check: bool = False,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        """
        Execute a command and wait for it to finish.

        Args:
            argv: Program and arguments. Never a shell string.
            cwd: Working directory.
            env: Extra environment variables, merged over the current one.
            timeout: Deadline in seconds.
            input: Data written to the process stdin, then closed. This is how
                secrets are passed to programs that accept them on stdin.
            user: Run as this account instead of the current one.
            check: Raise CommandError instead of returning a failed result.
            secrets: Literal values to redact from the recorded command line.

        Returns:
            The command outcome.

        Raises:
            CommandError: When check is True and the command failed.
        """

    @abstractmethod
    def stream(
        self,
        argv: Sequence[str],
        *,
        on_line: Callable[[str], None],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        user: str | None = None,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        """
        Execute a command, delivering merged output line by line as it appears.

        Long builds must not look frozen. ``on_line`` is called for each line of
        combined stdout and stderr while the process runs.

        Args:
            argv: Program and arguments.
            on_line: Called once per output line, without the trailing newline.
            cwd: Working directory.
            env: Extra environment variables, merged over the current one.
            timeout: Deadline in seconds for the whole command.
            user: Run as this account instead of the current one.
            secrets: Literal values to redact from the recorded command line.

        Returns:
            The command outcome. ``stdout`` holds the accumulated output.
        """

    @abstractmethod
    def capture_to_file(
        self,
        argv: Sequence[str],
        destination: Path,
        *,
        compress: bool = False,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        user: str | None = None,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        """
        Execute a command, writing its stdout straight to a file.

        This exists so that database dumps never pass through a shell or through
        Python memory. The previous implementation rendered a dump into
        ``bash -c "echo '...' > file"``, which corrupted binary dumps and let
        the contents of a database escape into a command line.

        Args:
            argv: Program and arguments producing the dump on stdout.
            destination: File to write. Created with mode 0600.
            compress: Pipe the output through gzip before writing.
            cwd: Working directory.
            env: Extra environment variables, merged over the current one.
            timeout: Deadline in seconds.
            user: Run as this account instead of the current one.
            secrets: Literal values to redact from the recorded command line.

        Returns:
            The command outcome. ``stdout`` is empty; the bytes went to disk.
        """

    def exists(self, program: str) -> bool:
        """
        Report whether a program is present on PATH.

        Args:
            program: Executable name.

        Returns:
            True when the program can be executed.
        """
        return shutil.which(program) is not None


class SubprocessRunner(CommandRunner):
    """The real runner. Executes processes with :mod:`subprocess`."""

    def __init__(self, *, on_command: Callable[[tuple[str, ...]], None] | None = None):
        """
        Args:
            on_command: Optional hook called with each redacted argv before it
                runs. Used to feed the verbose log and the audit trail.
        """
        self._on_command = on_command

    def _prepare(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None,
        user: str | None,
        secrets: Sequence[str],
    ) -> tuple[list[str], dict[str, str], tuple[str, ...]]:
        """
        Validate and decorate a command before execution.

        Args:
            argv: Candidate argument vector.
            env: Extra environment variables.
            user: Account to switch to, if any.
            secrets: Values to redact.

        Returns:
            The final argv, the merged environment, and the redacted argv used
            for logging and for the result.
        """
        args = _validate(argv)
        redacted = _redact(args, secrets)
        if user is not None:
            args = ["runuser", "-u", user, "--", *args]
            redacted = ("runuser", "-u", user, "--", *redacted)
        run_env = dict(os.environ)
        if env:
            run_env.update(env)
        if self._on_command is not None:
            self._on_command(redacted)
        return args, run_env, redacted

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        input: str | None = None,
        user: str | None = None,
        check: bool = False,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        args, run_env, redacted = self._prepare(argv, env, user, secrets)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                args,
                cwd=str(cwd) if cwd else None,
                env=run_env,
                input=input,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            result = CommandResult(
                argv=redacted,
                exit_code=EXIT_TIMEOUT,
                stderr=f"Command timed out after {timeout}s",
                duration=time.monotonic() - started,
                timed_out=True,
            )
        except FileNotFoundError:
            result = CommandResult(
                argv=redacted,
                exit_code=EXIT_NOT_FOUND,
                stderr=f"Command not found: {args[0]}",
                duration=time.monotonic() - started,
            )
        except PermissionError as exc:
            result = CommandResult(
                argv=redacted,
                exit_code=EXIT_NOT_FOUND,
                stderr=str(exc),
                duration=time.monotonic() - started,
            )
        else:
            result = CommandResult(
                argv=redacted,
                exit_code=completed.returncode,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
                duration=time.monotonic() - started,
            )
        return result.check() if check else result

    def stream(
        self,
        argv: Sequence[str],
        *,
        on_line: Callable[[str], None],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        user: str | None = None,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        args, run_env, redacted = self._prepare(argv, env, user, secrets)
        started = time.monotonic()
        collected: list[str] = []
        try:
            process = subprocess.Popen(
                args,
                cwd=str(cwd) if cwd else None,
                env=run_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            return CommandResult(
                argv=redacted,
                exit_code=EXIT_NOT_FOUND,
                stderr=f"Command not found: {args[0]}",
                duration=time.monotonic() - started,
            )

        # Reading the pipe directly would block past the deadline whenever the
        # child goes quiet, which is exactly what a hung build does. A reader
        # thread keeps the deadline enforceable no matter what the child emits.
        assert process.stdout is not None  # noqa: S101 - narrows the type for mypy
        lines: queue.Queue[str | None] = queue.Queue()

        def _pump(stream) -> None:
            try:
                for raw in stream:
                    lines.put(raw.rstrip("\n"))
            finally:
                lines.put(None)

        reader = threading.Thread(target=_pump, args=(process.stdout,), daemon=True)
        reader.start()

        deadline = started + timeout
        timed_out = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = lines.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue
            if line is None:
                break
            collected.append(line)
            on_line(line)

        if timed_out:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            timed_out = True
        reader.join(timeout=1)

        return CommandResult(
            argv=redacted,
            exit_code=EXIT_TIMEOUT if timed_out else process.returncode,
            stdout="\n".join(collected),
            stderr=f"Command timed out after {timeout}s" if timed_out else "",
            duration=time.monotonic() - started,
            timed_out=timed_out,
        )

    def capture_to_file(
        self,
        argv: Sequence[str],
        destination: Path,
        *,
        compress: bool = False,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        user: str | None = None,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        args, run_env, redacted = self._prepare(argv, env, user, secrets)
        destination.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()

        # The file is opened before the process starts and created 0600, so a
        # dump is never briefly world-readable.
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as sink:
                producer = subprocess.Popen(
                    args,
                    cwd=str(cwd) if cwd else None,
                    env=run_env,
                    stdout=subprocess.PIPE if compress else sink,
                    stderr=subprocess.PIPE,
                )
                gzip_proc = None
                if compress:
                    assert producer.stdout is not None  # noqa: S101 - narrows the type
                    gzip_proc = subprocess.Popen(
                        ["gzip", "-c"],
                        stdin=producer.stdout,
                        stdout=sink,
                        stderr=subprocess.PIPE,
                    )
                    # Let the producer receive SIGPIPE if gzip dies.
                    producer.stdout.close()

                try:
                    _, err = producer.communicate(timeout=timeout)
                    gz_err = b""
                    if gzip_proc is not None:
                        _, gz_err = gzip_proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    producer.kill()
                    if gzip_proc is not None:
                        gzip_proc.kill()
                    # A dump that ran out of time has written a prefix of the
                    # data. Leaving it behind would put a truncated file with a
                    # valid name in the backup directory, where it would be
                    # listed as a backup and eventually fed to psql on restore.
                    destination.unlink(missing_ok=True)
                    return CommandResult(
                        argv=redacted,
                        exit_code=EXIT_TIMEOUT,
                        stderr=f"Command timed out after {timeout}s",
                        duration=time.monotonic() - started,
                        timed_out=True,
                    )
        except FileNotFoundError as exc:
            return CommandResult(
                argv=redacted,
                exit_code=EXIT_NOT_FOUND,
                stderr=str(exc),
                duration=time.monotonic() - started,
            )

        exit_code = producer.returncode
        stderr = (err or b"").decode(errors="replace")
        if gzip_proc is not None and gzip_proc.returncode != 0:
            exit_code = exit_code or gzip_proc.returncode
            stderr += (gz_err or b"").decode(errors="replace")

        # A failed dump must not leave a plausible-looking backup behind.
        if exit_code != 0:
            destination.unlink(missing_ok=True)

        return CommandResult(
            argv=redacted,
            exit_code=exit_code,
            stderr=stderr,
            duration=time.monotonic() - started,
        )


#: Programs that only ever report state. A dry run may execute these, because
#: seeing what the machine currently looks like is the whole point of a
#: rehearsal. Anything not listed here is assumed to change something.
READ_ONLY_PROGRAMS: frozenset[str] = frozenset(
    {
        "cat",
        "df",
        "du",
        "getent",
        "grep",
        "head",
        "hostname",
        "id",
        "journalctl",
        "ls",
        "lsb_release",
        "ps",
        "readlink",
        "stat",
        "tail",
        "uname",
        "which",
        "whoami",
    }
)

#: Subcommands that are read-only for programs that both report and mutate.
READ_ONLY_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "systemctl": frozenset(
        {"status", "is-active", "is-enabled", "is-failed", "show", "cat", "list-units"}
    ),
    "nginx": frozenset({"-t", "-T", "-v", "-V"}),
    "apache2ctl": frozenset({"configtest", "-t", "-v", "-V"}),
    "apachectl": frozenset({"configtest", "-t", "-v", "-V"}),
    "certbot": frozenset({"certificates", "plugins", "--version"}),
    "git": frozenset({"status", "log", "show", "rev-parse", "ls-remote", "describe"}),
    "docker": frozenset({"ps", "images", "info", "version", "inspect", "logs"}),
    "apt-get": frozenset({"--version"}),
}


def is_read_only(argv: Sequence[str]) -> bool:
    """
    Report whether a command only observes the system.

    The classification is deliberately conservative: anything not recognised
    counts as mutating, because a dry run that quietly performs a real action
    is worse than one that refuses to guess.

    Args:
        argv: The argument vector to classify.

    Returns:
        True when the command is known not to change anything.
    """
    if not argv:
        return False
    program = Path(argv[0]).name
    if program.endswith("--version") or "--version" in argv:
        return True
    if program in READ_ONLY_PROGRAMS:
        return True
    allowed = READ_ONLY_SUBCOMMANDS.get(program)
    if allowed is None:
        return False
    return any(arg in allowed for arg in argv[1:])


class DryRunRunner(CommandRunner):
    """
    Rehearses instead of acting. This is what ``--dry-run`` actually means.

    The flag used to be wired per command, which meant it was honoured in three
    code paths and silently ignored in the ninety others, including every
    destructive one. Enforcing it here makes it true for the whole program by
    construction: a command that would change the machine cannot reach the
    machine, whatever the calling code believes.

    Read-only probes still run, because a rehearsal that cannot look at the
    system reports fiction.
    """

    def __init__(
        self, inner: CommandRunner, *, on_skip: Callable[[tuple[str, ...]], None] | None = None
    ):
        """
        Args:
            inner: Runner used for the commands that only observe.
            on_skip: Called with each argv that was not executed, so the CLI
                can show the user what a real run would have done.
        """
        self._inner = inner
        self._on_skip = on_skip
        self.skipped: list[tuple[str, ...]] = []

    def _skip(self, argv: Sequence[str], secrets: Sequence[str]) -> CommandResult:
        """
        Record a command that a real run would have executed.

        Args:
            argv: The argument vector that was not run.
            secrets: Values to keep out of the record.

        Returns:
            A successful result, so callers proceed through the rehearsal.
        """
        redacted = _redact(_validate(argv), secrets)
        self.skipped.append(redacted)
        if self._on_skip is not None:
            self._on_skip(redacted)
        return CommandResult(argv=redacted, exit_code=0, stdout="")

    def exists(self, program: str) -> bool:
        return self._inner.exists(program)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        input: str | None = None,
        user: str | None = None,
        check: bool = False,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        if is_read_only(argv):
            return self._inner.run(
                argv,
                cwd=cwd,
                env=env,
                timeout=timeout,
                input=input,
                user=user,
                check=check,
                secrets=secrets,
            )
        return self._skip(argv, secrets)

    def stream(
        self,
        argv: Sequence[str],
        *,
        on_line: Callable[[str], None],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        user: str | None = None,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        if is_read_only(argv):
            return self._inner.stream(
                argv, on_line=on_line, cwd=cwd, env=env, timeout=timeout, user=user, secrets=secrets
            )
        return self._skip(argv, secrets)

    def capture_to_file(
        self,
        argv: Sequence[str],
        destination: Path,
        *,
        compress: bool = False,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        user: str | None = None,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        # Never write the destination: a rehearsal that leaves a file behind is
        # not a rehearsal.
        return self._skip(argv, secrets)


@dataclass
class FakeCommand:
    """A scripted response used by :class:`FakeRunner`."""

    match: tuple[str, ...]
    result: CommandResult


class FakeRunner(CommandRunner):
    """
    A runner for tests. Records what was asked and replays scripted answers.

    Tests assert on the exact argv a manager builds, which is the part that
    matters and the part that used to be untestable.
    """

    def __init__(self, *, default_exit_code: int = 0):
        """
        Args:
            default_exit_code: Exit code returned for commands with no scripted
                response.
        """
        self.calls: list[tuple[str, ...]] = []
        self.inputs: list[str | None] = []
        self.written: dict[Path, tuple[str, ...]] = {}
        self._scripted: list[FakeCommand] = []
        self._default_exit_code = default_exit_code
        self._known_programs: set[str] | None = None

    def script(
        self,
        argv_prefix: Sequence[str],
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
    ) -> FakeRunner:
        """
        Register a canned response for commands starting with a prefix.

        Later registrations win, so a test can override a fixture's default.

        Args:
            argv_prefix: Leading arguments that identify the command.
            stdout: Standard output to return.
            stderr: Standard error to return.
            exit_code: Exit status to return.

        Returns:
            This runner, so calls can be chained.
        """
        self._scripted.append(
            FakeCommand(
                match=tuple(str(a) for a in argv_prefix),
                result=CommandResult(
                    argv=tuple(str(a) for a in argv_prefix),
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                ),
            )
        )
        return self

    def only_knows(self, *programs: str) -> FakeRunner:
        """
        Restrict which programs :meth:`exists` reports as installed.

        Args:
            programs: Executable names that should be considered present.

        Returns:
            This runner, so calls can be chained.
        """
        self._known_programs = set(programs)
        return self

    def exists(self, program: str) -> bool:
        if self._known_programs is None:
            return True
        return program in self._known_programs

    def _lookup(self, argv: Sequence[str]) -> CommandResult:
        """
        Find the scripted response for a call, recording the call first.

        Args:
            argv: The argument vector the code under test built.

        Returns:
            The scripted result, or a default success.
        """
        args = tuple(_validate(argv))
        self.calls.append(args)
        for scripted in reversed(self._scripted):
            if args[: len(scripted.match)] == scripted.match:
                return replace(scripted.result, argv=args)
        return CommandResult(argv=args, exit_code=self._default_exit_code)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        input: str | None = None,
        user: str | None = None,
        check: bool = False,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        self.inputs.append(input)
        result = self._lookup(argv)
        return result.check() if check else result

    def stream(
        self,
        argv: Sequence[str],
        *,
        on_line: Callable[[str], None],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        user: str | None = None,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        result = self._lookup(argv)
        for line in result.stdout.splitlines():
            on_line(line)
        return result

    def capture_to_file(
        self,
        argv: Sequence[str],
        destination: Path,
        *,
        compress: bool = False,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        user: str | None = None,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        result = self._lookup(argv)
        self.written[destination] = result.argv
        if result.success:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(result.stdout)
        return result

    # Assertions -----------------------------------------------------------

    def ran(self, *argv_prefix: str) -> bool:
        """
        Report whether a command starting with the given arguments ran.

        Args:
            argv_prefix: Leading arguments to look for.

        Returns:
            True when at least one recorded call matches.
        """
        prefix = tuple(argv_prefix)
        return any(call[: len(prefix)] == prefix for call in self.calls)

    def calls_to(self, program: str) -> list[tuple[str, ...]]:
        """
        Return every recorded call to a given program.

        Args:
            program: Executable name to filter by.

        Returns:
            The matching argument vectors, in order.
        """
        return [c for c in self.calls if c and c[0] == program]


_default_runner: CommandRunner | None = None


def get_runner() -> CommandRunner:
    """
    Return the process-wide runner, creating the real one on first use.

    Returns:
        The active command runner.
    """
    global _default_runner
    if _default_runner is None:
        _default_runner = SubprocessRunner()
    return _default_runner


def set_runner(runner: CommandRunner | None) -> None:
    """
    Replace the process-wide runner.

    Args:
        runner: The runner to install, or None to reset to the real one.
    """
    global _default_runner
    _default_runner = runner
