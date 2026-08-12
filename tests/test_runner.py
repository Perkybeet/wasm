"""
Tests for the command execution seam.

These are the only tests allowed to spawn real processes, because the point of
the module under test is precisely to spawn processes correctly.
"""

from __future__ import annotations

import dataclasses
import gzip
import sys

import pytest

from wasm.core.runner import (
    DEFAULT_TIMEOUT,
    EXIT_NOT_FOUND,
    EXIT_TIMEOUT,
    CommandError,
    CommandResult,
    DryRunRunner,
    FakeRunner,
    SubprocessRunner,
    get_runner,
    is_read_only,
    set_runner,
)

pytestmark = pytest.mark.allow_subprocess


@pytest.fixture
def real() -> SubprocessRunner:
    """Return a real runner."""
    return SubprocessRunner()


class TestArgvOnly:
    """A shell is never involved, so shell metacharacters stay inert."""

    def test_rejects_a_string_command(self, real: SubprocessRunner):
        with pytest.raises(ValueError, match="not a string"):
            real.run("echo hello")  # type: ignore[arg-type]

    def test_rejects_an_empty_argv(self, real: SubprocessRunner):
        with pytest.raises(ValueError, match="must not be empty"):
            real.run([])

    def test_rejects_nul_bytes(self, real: SubprocessRunner):
        with pytest.raises(ValueError, match="NUL"):
            real.run(["echo", "a\x00b"])

    def test_shell_metacharacters_are_literal_data(self, real: SubprocessRunner, tmp_path):
        # If any shell were involved this would create the file.
        victim = tmp_path / "pwned"
        result = real.run(["echo", f"; touch {victim}"])

        assert result.success
        assert not victim.exists()
        assert str(victim) in result.stdout

    def test_a_domain_with_a_semicolon_cannot_run_a_second_command(
        self, real: SubprocessRunner, tmp_path
    ):
        victim = tmp_path / "injected"
        result = real.run(["echo", f"example.com; touch {victim}"])

        assert not victim.exists()
        assert result.success


class TestOutcome:
    """Exit codes, output and failure reporting."""

    def test_captures_stdout(self, real: SubprocessRunner):
        assert real.run(["echo", "hello"]).output == "hello"

    def test_reports_failure_without_raising_by_default(self, real: SubprocessRunner):
        result = real.run([sys.executable, "-c", "raise SystemExit(3)"])

        assert not result.success
        assert result.exit_code == 3
        assert bool(result) is False

    def test_check_raises_on_failure(self, real: SubprocessRunner):
        with pytest.raises(CommandError) as exc:
            real.run([sys.executable, "-c", "raise SystemExit(3)"], check=True)

        assert "exit code 3" in str(exc.value)

    def test_check_returns_the_result_on_success(self, real: SubprocessRunner):
        assert real.run(["echo", "ok"], check=True).output == "ok"

    def test_missing_program_is_not_an_exception(self, real: SubprocessRunner):
        result = real.run(["wasm-no-such-program-exists"])

        assert result.exit_code == EXIT_NOT_FOUND
        assert "not found" in result.stderr

    def test_records_duration(self, real: SubprocessRunner):
        assert real.run(["echo", "x"]).duration >= 0.0


class TestTimeouts:
    """Nothing may block forever."""

    def test_default_timeout_is_finite(self):
        assert DEFAULT_TIMEOUT > 0

    def test_run_enforces_the_deadline(self, real: SubprocessRunner):
        result = real.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)

        assert result.timed_out
        assert result.exit_code == EXIT_TIMEOUT
        assert "timed out" in result.stderr

    def test_stream_enforces_the_deadline(self, real: SubprocessRunner):
        lines: list[str] = []
        result = real.stream(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            on_line=lines.append,
            timeout=1,
        )

        assert result.timed_out


class TestStreaming:
    """Long builds report progress instead of looking frozen."""

    def test_delivers_lines_as_they_appear(self, real: SubprocessRunner):
        lines: list[str] = []
        result = real.stream(
            [sys.executable, "-c", "print('one'); print('two')"],
            on_line=lines.append,
        )

        assert lines == ["one", "two"]
        assert result.success

    def test_merges_stderr_into_the_stream(self, real: SubprocessRunner):
        lines: list[str] = []
        real.stream(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            on_line=lines.append,
        )

        assert set(lines) == {"out", "err"}

    def test_accumulates_output_in_the_result(self, real: SubprocessRunner):
        result = real.stream([sys.executable, "-c", "print('a')"], on_line=lambda _: None)

        assert result.stdout == "a"


class TestCaptureToFile:
    """Database dumps go straight to disk, never through a shell."""

    def test_writes_stdout_to_the_destination(self, real: SubprocessRunner, tmp_path):
        dest = tmp_path / "dump.sql"
        result = real.capture_to_file([sys.executable, "-c", "print('CREATE TABLE t;')"], dest)

        assert result.success
        assert dest.read_text() == "CREATE TABLE t;\n"

    def test_survives_content_that_would_break_shell_quoting(
        self, real: SubprocessRunner, tmp_path
    ):
        # This is the payload that corrupted the old bash -c "echo '...'" path.
        payload = """INSERT INTO t VALUES ('it''s', "$(touch /tmp/x)", `id`);"""
        dest = tmp_path / "dump.sql"
        script = f"print({payload!r})"

        result = real.capture_to_file([sys.executable, "-c", script], dest)

        assert result.success
        assert dest.read_text().strip() == payload

    def test_preserves_binary_output(self, real: SubprocessRunner, tmp_path):
        dest = tmp_path / "dump.bin"
        script = "import sys; sys.stdout.buffer.write(bytes(range(256)))"

        real.capture_to_file([sys.executable, "-c", script], dest)

        assert dest.read_bytes() == bytes(range(256))

    def test_compresses_when_asked(self, real: SubprocessRunner, tmp_path):
        dest = tmp_path / "dump.sql.gz"
        real.capture_to_file([sys.executable, "-c", "print('hello')"], dest, compress=True)

        assert gzip.decompress(dest.read_bytes()) == b"hello\n"

    def test_creates_the_file_unreadable_by_other_users(self, real: SubprocessRunner, tmp_path):
        dest = tmp_path / "dump.sql"
        real.capture_to_file([sys.executable, "-c", "print('secret')"], dest)

        assert dest.stat().st_mode & 0o077 == 0

    def test_removes_the_file_when_the_dump_fails(self, real: SubprocessRunner, tmp_path):
        dest = tmp_path / "dump.sql"
        result = real.capture_to_file(
            [sys.executable, "-c", "import sys; sys.stdout.write('partial'); raise SystemExit(1)"],
            dest,
        )

        assert not result.success
        assert not dest.exists(), "a failed dump must not leave a plausible backup behind"


class TestSecrets:
    """Passwords must not reach argv, logs or results."""

    def test_redacts_secrets_from_the_recorded_command(self, real: SubprocessRunner):
        result = real.run(["echo", "user:hunter2"], secrets=["hunter2"])

        assert "hunter2" not in result.command
        assert "***" in result.command

    def test_passes_data_through_stdin(self, real: SubprocessRunner):
        script = "import sys; sys.stdout.write(sys.stdin.read().upper())"

        result = real.run([sys.executable, "-c", script], input="hunter2")

        assert result.output == "HUNTER2"

    def test_the_secret_never_appears_in_argv(self, real: SubprocessRunner):
        result = real.run([sys.executable, "-c", "pass"], input="hunter2")

        assert "hunter2" not in result.command

    def test_logs_the_command_through_the_hook_already_redacted(self):
        seen: list[tuple[str, ...]] = []
        runner = SubprocessRunner(on_command=seen.append)

        runner.run(["echo", "hunter2"], secrets=["hunter2"])

        assert seen == [("echo", "***")]


class TestFakeRunner:
    """The test double records calls and replays scripted answers."""

    def test_records_every_call(self):
        fake = FakeRunner()

        fake.run(["systemctl", "restart", "wasm-example-com"])

        assert fake.calls == [("systemctl", "restart", "wasm-example-com")]
        assert fake.ran("systemctl", "restart")
        assert not fake.ran("systemctl", "stop")

    def test_succeeds_by_default(self):
        assert FakeRunner().run(["anything"]).success

    def test_replays_scripted_output(self):
        fake = FakeRunner().script(["nginx", "-t"], stdout="syntax is ok")

        assert fake.run(["nginx", "-t"]).output == "syntax is ok"

    def test_a_later_script_wins(self):
        fake = (
            FakeRunner().script(["nginx", "-t"], exit_code=0).script(["nginx", "-t"], exit_code=1)
        )

        assert not fake.run(["nginx", "-t"]).success

    def test_matches_on_a_prefix(self):
        fake = FakeRunner().script(["systemctl"], stdout="active")

        assert fake.run(["systemctl", "is-active", "x"]).output == "active"

    def test_check_still_raises(self):
        fake = FakeRunner().script(["false"], exit_code=1)

        with pytest.raises(CommandError):
            fake.run(["false"], check=True)

    def test_can_pretend_a_program_is_missing(self):
        fake = FakeRunner().only_knows("nginx")

        assert fake.exists("nginx")
        assert not fake.exists("apache2")

    def test_streams_scripted_lines(self):
        fake = FakeRunner().script(["npm", "install"], stdout="added 1\nadded 2")
        lines: list[str] = []

        fake.stream(["npm", "install"], on_line=lines.append)

        assert lines == ["added 1", "added 2"]

    def test_capture_to_file_records_the_destination(self, tmp_path):
        fake = FakeRunner().script(["pg_dump"], stdout="DUMP")
        dest = tmp_path / "db.sql"

        fake.capture_to_file(["pg_dump", "app"], dest)

        assert dest.read_text() == "DUMP"
        assert dest in fake.written

    def test_rejects_a_string_command_like_the_real_one(self):
        with pytest.raises(ValueError):
            FakeRunner().run("echo hi")  # type: ignore[arg-type]


class TestUserSwitching:
    """Running as another account is part of argv, not a shell escape."""

    def test_wraps_the_command_in_runuser(self):
        fake = FakeRunner()
        real = SubprocessRunner()
        argv, _, redacted = real._prepare(["psql", "-c", "SELECT 1"], None, "postgres", ())

        assert argv[:4] == ["runuser", "-u", "postgres", "--"]
        assert redacted[:4] == ("runuser", "-u", "postgres", "--")
        assert fake is not None


class TestGlobalRunner:
    """The process-wide runner can be swapped for tests."""

    def test_defaults_to_the_real_runner(self):
        set_runner(None)

        assert isinstance(get_runner(), SubprocessRunner)

    def test_can_be_replaced(self):
        fake = FakeRunner()
        set_runner(fake)
        try:
            assert get_runner() is fake
        finally:
            set_runner(None)


class TestDryRun:
    """--dry-run is enforced at the seam, so it is true everywhere."""

    def test_a_mutating_command_never_reaches_the_machine(self, tmp_path):
        victim = tmp_path / "created"
        dry = DryRunRunner(SubprocessRunner())

        result = dry.run(["touch", str(victim)])

        assert result.success, "callers must be able to proceed through a rehearsal"
        assert not victim.exists()
        assert dry.skipped == [("touch", str(victim))]

    def test_a_read_only_command_still_runs(self):
        dry = DryRunRunner(SubprocessRunner())

        assert dry.run(["whoami"]).success
        assert dry.skipped == []

    def test_reports_what_would_have_happened(self):
        seen: list[tuple[str, ...]] = []
        dry = DryRunRunner(SubprocessRunner(), on_skip=seen.append)

        dry.run(["systemctl", "restart", "wasm-example-com"])

        assert seen == [("systemctl", "restart", "wasm-example-com")]

    def test_never_writes_a_capture_file(self, tmp_path):
        dest = tmp_path / "dump.sql"
        dry = DryRunRunner(SubprocessRunner())

        dry.capture_to_file(["pg_dump", "app"], dest)

        assert not dest.exists()

    def test_redacts_secrets_from_what_it_records(self):
        dry = DryRunRunner(SubprocessRunner())

        dry.run(["mysql", "-phunter2"], secrets=["hunter2"])

        assert "hunter2" not in " ".join(dry.skipped[0])

    def test_streaming_a_build_is_rehearsed(self):
        dry = DryRunRunner(SubprocessRunner())
        lines: list[str] = []

        dry.stream(["npm", "install"], on_line=lines.append)

        assert lines == []
        assert dry.skipped == [("npm", "install")]


class TestReadOnlyClassification:
    """
    Unknown commands count as mutating.

    A dry run that guesses wrong in the permissive direction performs a real
    destructive action, so the classification errs the other way.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["systemctl", "status", "nginx"],
            ["systemctl", "is-active", "wasm-example-com"],
            ["nginx", "-t"],
            ["certbot", "certificates"],
            ["git", "rev-parse", "HEAD"],
            ["docker", "ps"],
            ["journalctl", "-u", "wasm-example-com"],
            ["node", "--version"],
            ["/usr/bin/whoami"],
        ],
    )
    def test_recognised_as_read_only(self, argv):
        assert is_read_only(argv)

    @pytest.mark.parametrize(
        "argv",
        [
            ["systemctl", "restart", "nginx"],
            ["systemctl", "daemon-reload"],
            ["nginx", "-s", "reload"],
            ["certbot", "certonly", "-d", "example.com"],
            ["git", "clone", "https://example.com/x.git"],
            ["docker", "compose", "up", "-d"],
            ["rm", "-rf", "/var/www/apps/x"],
            ["apt-get", "install", "-y", "nginx"],
            ["npm", "install"],
            ["some-unknown-tool", "--flag"],
            [],
        ],
    )
    def test_treated_as_mutating(self, argv):
        assert not is_read_only(argv)


class TestCommandResult:
    """The result object reads naturally at call sites."""

    def test_is_truthy_on_success(self):
        assert CommandResult(argv=("x",), exit_code=0)

    def test_is_falsy_on_failure(self):
        assert not CommandResult(argv=("x",), exit_code=1)

    def test_is_immutable(self):
        result = CommandResult(argv=("x",), exit_code=0)

        with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
            result.exit_code = 1  # type: ignore[misc]
