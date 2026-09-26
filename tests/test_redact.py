"""
Build and job output never carries an application's secrets to a reader.

A build runs with the application's environment, so ``echo $API_KEY`` in a
build script - or a tool that prints its configuration on failure - writes the
secret into the captured deployment log, the deployment's error text, the
job's log lines, its log file and its error. All of those are readable with
the ``read`` scope. One scrubber, :class:`wasm.core.redact.Scrubber`, built
from the application's secret-looking values and WASM's own credentials,
replaces them everywhere that text is persisted or published.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.test_deployers import build_deployer
from wasm.core.exceptions import BuildError
from wasm.core.fs import set_fs
from wasm.core.redact import (
    MIN_SECRET_LENGTH,
    Scrubber,
    app_secret_values,
    config_secret_values,
    secret_env_values,
)
from wasm.core.runner import FakeRunner
from wasm.core.store import App, WASMStore
from wasm.deployers.nodejs import NodeJSDeployer
from wasm.deployers.recorder import CapturingLogger, DeploymentRecorder
from wasm.web.jobs import Job, JobContext, JobManager, JobType

DOMAIN = "app.example.com"
SECRET = "sk_live_9f8e7d6c5b4a"
DB_PASSWORD = "hunter2-but-longer"


@pytest.fixture(autouse=True)
def real_filesystem() -> Any:
    """
    Run against the real filesystem seam: these tests read files written.

    Yields:
        Nothing; the seam is reset on both sides.
    """
    set_fs(None)
    yield
    set_fs(None)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    """
    Provide an isolated store, installed as the process-wide singleton.

    Args:
        tmp_path: Per-test temporary directory.

    Yields:
        The store.
    """
    WASMStore.reset_instance()
    instance = WASMStore(tmp_path / "wasm.db")
    try:
        yield instance
    finally:
        instance.close()
        WASMStore.reset_instance()


@pytest.fixture(autouse=True)
def fresh_job_manager(store: Any) -> Any:
    """
    Give every test a job manager of its own, wired to ``store``.

    Args:
        store: The store fixture.

    Yields:
        Nothing.
    """
    JobManager.reset_instance()
    yield
    JobManager.reset_instance()


@pytest.fixture
def deployed_app(tmp_path: Path, store: WASMStore) -> App:
    """
    Register an application whose ``.env`` holds secrets.

    Args:
        tmp_path: Per-test temporary directory.
        store: The store fixture.

    Returns:
        The application row.
    """
    app_path = tmp_path / "apps" / "app"
    app_path.mkdir(parents=True)
    (app_path / ".env").write_text(
        f"API_KEY={SECRET}\n"
        f"DATABASE_URL=postgres://app:{DB_PASSWORD}@localhost/app\n"
        "NODE_ENV=production\n"
        "APP_NAME=storefront\n"
    )
    return store.create_app(App(domain=DOMAIN, app_path=str(app_path)))


# ---------------------------------------------------------------------------
# The scrubber
# ---------------------------------------------------------------------------


class TestScrubber:
    def test_replaces_every_occurrence(self) -> None:
        scrubber = Scrubber([SECRET])

        assert scrubber.scrub(f"key={SECRET} again {SECRET}") == "key=*** again ***"

    def test_leaves_short_values_alone(self) -> None:
        # A four-letter "secret" such as "true" or "test" would blank out
        # every ordinary word that contains it.
        scrubber = Scrubber(["true", "x" * (MIN_SECRET_LENGTH - 1)])

        assert scrubber.scrub("it is true: xxxxx") == "it is true: xxxxx"
        assert not scrubber

    def test_the_longer_of_two_overlapping_secrets_wins(self) -> None:
        scrubber = Scrubber(["abcdef", "abcdefghij"])

        assert scrubber.scrub("abcdefghij") == "***"

    def test_each_line_of_a_multi_line_secret_is_scrubbed(self) -> None:
        key = "-----BEGIN KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END KEY-----"
        scrubber = Scrubber([key])

        assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" not in scrubber.scrub(
            "line printed alone: MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"
        )

    def test_values_can_be_added_later(self) -> None:
        scrubber = Scrubber()
        assert scrubber.scrub(SECRET) == SECRET

        scrubber.add([SECRET])

        assert scrubber.scrub(SECRET) == "***"

    def test_regex_metacharacters_in_a_secret_are_literal(self) -> None:
        scrubber = Scrubber(["a.b*c+d?e"])

        assert scrubber.scrub("a.b*c+d?e and aXbbbcdde") == "*** and aXbbbcdde"


class TestWhatCountsAsSecret:
    def test_secret_named_variables_and_url_passwords(self) -> None:
        values = secret_env_values(
            {
                "API_KEY": SECRET,
                "DB_PASS": "passw0rd-db",
                "AuthToken": "tok-123456",
                "DATABASE_URL": f"postgres://app:{DB_PASSWORD}@localhost/app",
                "NODE_ENV": "production",
                "PUBLIC_URL": "https://example.com/storefront",
            }
        )

        assert SECRET in values
        assert "passw0rd-db" in values
        assert "tok-123456" in values
        assert DB_PASSWORD in values
        assert "production" not in values
        assert "https://example.com/storefront" not in values

    def test_config_credentials(self) -> None:
        values = config_secret_values(
            {
                "databases": {"credentials": {"mysql": {"user": "root", "password": "rootpw-123"}}},
                "notifications": {"smtp": {"host": "mail.example.com", "password": "smtp-pw-99"}},
            }
        )

        assert set(values) == {"rootpw-123", "smtp-pw-99"}

    def test_an_applications_env_file(self, deployed_app: App) -> None:
        values = app_secret_values(DOMAIN)

        assert SECRET in values
        assert DB_PASSWORD in values
        assert "storefront" not in values


# ---------------------------------------------------------------------------
# Deployment history: captured log and error text
# ---------------------------------------------------------------------------


def test_a_build_that_echoes_a_secret_leaves_no_secret_in_the_history(
    tmp_path: Path, store: WASMStore
) -> None:
    fake = FakeRunner()
    fake.script(["npm", "ci"], stdout=f"> echo $API_KEY\n{SECRET}\nadded 42 packages\n")

    deployer = build_deployer(NodeJSDeployer, tmp_path)
    deployer._runner = fake
    deployer.env_vars = {"API_KEY": SECRET}

    def fetch_source() -> bool:
        # The tree appears at fetch time, the way a clone would create it;
        # npm ci is what runs when the project has a lockfile.
        deployer.app_path.mkdir(parents=True, exist_ok=True)
        (deployer.app_path / "package-lock.json").write_text("{}")
        return True

    deployer.fetch_source = fetch_source
    deployer.pre_install = lambda: True
    deployer.post_install = lambda: True
    deployer.package_manager = "npm"

    def build() -> bool:
        raise BuildError("Build failed", details=f"next build: invalid key {SECRET}")

    deployer.build = build

    with pytest.raises(BuildError):
        deployer.deploy()

    record = store.list_deployments(DOMAIN)[0]
    assert record.log_path is not None
    log = Path(record.log_path).read_text()
    assert "added 42 packages" in log, "the rest of the build output is still captured"
    assert SECRET not in log
    assert "***" in log
    assert record.error is not None
    assert SECRET not in record.error
    assert "next build: invalid key ***" in record.error


def test_a_secret_generated_during_the_deploy_is_scrubbed_too(
    tmp_path: Path, store: WASMStore
) -> None:
    # .env.example generation replaces env_vars mid-pipeline; the build that
    # follows runs with the new values, so the scrubber must follow them.
    deployer = build_deployer(NodeJSDeployer, tmp_path)
    deployer.fetch_source = lambda: (tmp_path / "app").mkdir(parents=True, exist_ok=True) or True
    deployer.install_dependencies = lambda: True

    def build() -> bool:
        deployer.env_vars = {"SESSION_SECRET": "generated-0123456789"}
        deployer.logger.debug("SESSION_SECRET is generated-0123456789")
        return True

    deployer.build = build
    deployer.health_check = lambda retries=5, delay=2.0: True

    assert deployer.deploy() is True

    record = store.list_deployments(DOMAIN)[0]
    assert record.log_path is not None
    assert "generated-0123456789" not in Path(record.log_path).read_text()


def test_every_recording_scrubs_the_applications_env(
    tmp_path: Path, store: WASMStore, deployed_app: App
) -> None:
    # Update, rollback and release activation build their recorder directly,
    # not through recorder_for; the application's .env is known to all of them.
    logger = CapturingLogger()
    recorder = DeploymentRecorder(store, DOMAIN, "cli", logger=logger, log_root=tmp_path / "logs")

    recorder.start()
    logger.debug(f"DATABASE_URL=postgres://app:{DB_PASSWORD}@localhost/app")
    recorder.finish_failure(f"migration failed with key {SECRET}")

    record = store.list_deployments(DOMAIN)[0]
    assert record.log_path is not None
    log = Path(record.log_path).read_text()
    assert DB_PASSWORD not in log
    assert record.error is not None
    assert SECRET not in record.error


# ---------------------------------------------------------------------------
# Jobs: log lines, log file, error, stored row
# ---------------------------------------------------------------------------


def _run_job(manager: JobManager, func: Any, kwargs: dict[str, Any] | None = None) -> Job:
    """
    Queue and execute a job on the calling thread.

    Args:
        manager: The job manager.
        func: The job function.
        kwargs: Its keyword arguments.

    Returns:
        The finished job.
    """
    job_id = str(uuid.uuid4())[:8]
    job = Job(id=job_id, type=JobType.DEPLOY, name="deploy", description="deploy")
    manager._jobs[job_id] = job
    manager._open_log(job_id)
    manager._notify_subscribers(job)
    manager._execute_job(job_id, func, (), kwargs or {})
    return job


def _assert_job_is_clean(job: Job, store: WASMStore, secret: str) -> None:
    """
    Check every place a job's text is kept for a secret.

    Args:
        job: The finished job.
        store: The store it was persisted to.
        secret: The value that must not appear.
    """
    assert all(secret not in entry.message for entry in job.logs)
    assert job.error is None or secret not in job.error
    record = store.get_job(job.id)
    assert record is not None
    assert record.error is None or secret not in record.error
    assert record.log_path is not None
    assert secret not in Path(record.log_path).read_text()


def test_a_job_for_an_application_scrubs_its_secrets(store: WASMStore, deployed_app: App) -> None:
    def job(job_context: JobContext | None = None) -> None:
        assert job_context is not None
        job_context.set_metadata("domain", DOMAIN)
        job_context.log(f"build said: {SECRET}")
        job_context.update(f"Connecting to postgres://app:{DB_PASSWORD}@localhost", 50)
        raise BuildError("Build failed", details=f"printenv: API_KEY={SECRET}")

    finished = _run_job(JobManager(), job)

    _assert_job_is_clean(finished, store, SECRET)
    _assert_job_is_clean(finished, store, DB_PASSWORD)
    assert finished.error is not None
    assert "API_KEY=***" in finished.error


def test_secrets_passed_to_the_job_are_scrubbed(store: WASMStore) -> None:
    # A fresh deploy: the application has no .env yet, the secrets arrive in
    # the request's env_vars and reach the build's environment from there.
    def deploy(env_vars: dict[str, str], job_context: JobContext | None = None) -> None:
        assert job_context is not None
        job_context.log(f"$ printenv\nSTRIPE_SECRET_KEY={env_vars['STRIPE_SECRET_KEY']}")
        raise BuildError("Build failed", details=env_vars["STRIPE_SECRET_KEY"])

    finished = _run_job(JobManager(), deploy, {"env_vars": {"STRIPE_SECRET_KEY": SECRET}})

    _assert_job_is_clean(finished, store, SECRET)


def test_wasms_own_credentials_are_scrubbed(store: WASMStore, monkeypatch) -> None:
    monkeypatch.setattr("wasm.core.redact.known_credentials", lambda: [DB_PASSWORD])

    def job(job_context: JobContext | None = None) -> None:
        assert job_context is not None
        job_context.log(f"mysql -p{DB_PASSWORD}")

    finished = _run_job(JobManager(), job)

    _assert_job_is_clean(finished, store, DB_PASSWORD)
