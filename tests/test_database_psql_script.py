"""
A PostgreSQL restore never lets psql run a meta-command from the dump.

psql reads a plain dump given with ``-f`` as a script: ``\\!`` runs a shell,
``\\o`` writes a file, ``\\set ON_ERROR_STOP 0`` makes the rest of the restore
ignore its errors, wherever psql's lexer finds a backslash outside a literal.
Every case below was fed to psql 16.14 against a real server while this was
written. The hostile ones are meta-commands psql runs - or, for the
standard_conforming_strings and BEGIN ATOMIC cases, runs under the other
setting or the other psql version; the accepted ones restored and ran nothing.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

import pytest

from wasm.core.exceptions import DatabaseBackupError
from wasm.core.runner import FakeRunner
from wasm.core.store import WASMStore
from wasm.managers.database.postgres import PostgresManager
from wasm.managers.database.psql_script import check_plain_dump

PSQL = ("runuser", "-u", "postgres", "--", "psql")

#: An excerpt of what pg_dump 16.14 wrote for a database built to be awkward:
#: the \restrict pair, a BEGIN ATOMIC body with semicolons, a dollar-quoted
#: body holding a quote and a backslash, a view whose ESCAPE is '\', COPY data
#: whose rows start with backslashes (``\\.`` is data, ``\.`` ends it), and a
#: large object written from a '\x...' literal.
PG_DUMP = r'''--
-- PostgreSQL database dump
--

\restrict g47xctehXkxdEsBaSAdBc4jiRhbjRTLESoMUORiAdJASSQGYk42XwCcxOly8EIG

-- Dumped from database version 16.14 (Debian 16.14-1.pgdg13+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg13+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

CREATE SCHEMA "Odd Schema";

ALTER SCHEMA "Odd Schema" OWNER TO postgres;

CREATE FUNCTION public.f_atomic(a integer) RETURNS integer
    LANGUAGE sql
    BEGIN ATOMIC
 SELECT (a + 1);
 SELECT
         CASE
             WHEN (a > 0) THEN 1
             ELSE 0
         END AS "case";
END;

CREATE FUNCTION public.f_dollar(a text) RETURNS text
    LANGUAGE plpgsql
    AS $_$
BEGIN
  -- a comment with a quote ' and backslash \ inside
  RETURN regexp_replace(a, '\s+', ' ', 'g') || $$;$$;
END;
$_$;

CREATE TABLE "Odd Schema"."Weird ""name""" (
    id integer NOT NULL,
    note text,
    raw bytea,
    j jsonb
);

CREATE TABLE public.plain (
    n integer,
    s text DEFAULT 'semi;colon'::text
);

COMMENT ON TABLE public.plain IS 'Comment with ''quotes'' and a backslash \n literal';

CREATE VIEW public.v AS
 SELECT n,
    s
   FROM public.plain
  WHERE (s ~~ like_escape('%\_%'::text, '\'::text));

SELECT pg_catalog.lo_create('16452');

COPY "Odd Schema"."Weird ""name""" (id, note, raw, j) FROM stdin;
1	back\\slash and tab\t and newline\n and quote ' and \\. at line start	\\x00ff5c27	{"a": "b\\\\c"}
2	\\.\n\\.	\\x5c2e0a	[]
3	C:\\path\\to\\	\N	\N
4	-- not a comment /* nor this */ $$ $x$	\N	null
\.

COPY public.plain (n, s) FROM stdin;
1	x\\
\! this line is data, not a command
\.

BEGIN;
SELECT pg_catalog.lo_open('16452', 131072);
SELECT pg_catalog.lowrite(0, '\x5c2e0a5c21206964');
SELECT pg_catalog.lo_close(0);
COMMIT;

ALTER TABLE ONLY "Odd Schema"."Weird ""name"""
    ADD CONSTRAINT "Weird ""name""_pkey" PRIMARY KEY (id);

--
-- PostgreSQL database dump complete
--

\unrestrict g47xctehXkxdEsBaSAdBc4jiRhbjRTLESoMUORiAdJASSQGYk42XwCcxOly8EIG
'''

#: The same database dumped with --inserts: strings span lines, one of which
#: starts with ``\.``, and values end in a backslash right before the quote.
PG_DUMP_INSERTS = r'''SET standard_conforming_strings = on;
INSERT INTO "Odd Schema"."Weird ""name""" VALUES (2, '\.
\.', '\x5c2e0a', '[]');
INSERT INTO "Odd Schema"."Weird ""name""" VALUES (3, 'C:\path\to\', NULL, NULL);
INSERT INTO public.plain VALUES (1, 'x\', '2026-09-26 02:54:43.692761+00');
INSERT INTO public.plain VALUES (2, '
\! still inside the string', NULL);
'''

HEADER = "SET standard_conforming_strings = on;\n"


def _after_header(text: str) -> str:
    """
    Put the header pg_dump writes in front of a case.

    Args:
        text: The case's own lines.

    Returns:
        The dump, with standard_conforming_strings set on first.
    """
    return HEADER + text


#: (dump, what the refusal names). Each ran its payload through real psql.
HOSTILE = [
    pytest.param(_after_header("SELECT 1;\n\\! id\n"), "\\!", id="line"),
    pytest.param(_after_header("SELECT 1; \\! id\n"), "\\!", id="mid-line"),
    pytest.param(_after_header("SELECT 1 \\o /tmp/x\n"), "\\o", id="mid-statement"),
    pytest.param(_after_header("\\set ON_ERROR_STOP 0\nSELECT 1;\n"), "\\set", id="set"),
    pytest.param(_after_header("\\connect other\n"), "another database", id="connect"),
    pytest.param(_after_header("\\c other\n"), "another database", id="c"),
    pytest.param(
        _after_header("SELECT $$\nCOPY t (a) FROM stdin;\n$$;\n\\! id\n\\.\n"),
        "\\!",
        id="copy-hidden-in-a-dollar-quote",
    ),
    pytest.param(
        _after_header("COPY (SELECT * FROM stdin) TO STDOUT;\n\\! id\n\\.\n"),
        "\\!",
        id="from-stdin-in-a-subquery",
    ),
    pytest.param(
        _after_header('COPY "a FROM stdin" TO STDOUT;\n\\! id\n\\.\n'),
        "\\!",
        id="from-stdin-in-an-identifier",
    ),
    pytest.param(
        _after_header("SELECT (1;\nCOPY t FROM stdin;\n\\! id\n\\.\n);\n"),
        "\\!",
        id="copy-inside-parentheses",
    ),
    pytest.param(
        _after_header("COPY t FROM stdin; \\! id\n\\.\n"),
        "continues after a COPY",
        id="command-after-copy-on-its-line",
    ),
    pytest.param(
        _after_header(
            "CREATE FUNCTION f() RETURNS int LANGUAGE sql "
            "BEGIN ATOMIC SELECT 1; COPY t FROM stdin; END;\n\\! id\n\\.\n"
        ),
        "routine body",
        id="copy-in-begin-atomic",
    ),
    pytest.param(
        "SELECT '\\'; \\! id --';\n",
        "backslash before a quote",
        id="standard-strings-unknown",
    ),
    pytest.param(
        "SET standard_conforming_strings = off;\nSELECT '\\'; \\! id --';\n",
        "backslash before a quote",
        id="standard-strings-off",
    ),
    pytest.param(
        _after_header(
            "SELECT set_config('standard_conforming_strings', 'off', false);\n"
            "SELECT '\\'; \\! id --';\n"
        ),
        "backslash before a quote",
        id="standard-strings-flipped-by-a-function",
    ),
    pytest.param(
        _after_header("SELECT 1 /* */ \\! id\n"),
        "\\!",
        id="after-a-closed-comment",
    ),
    pytest.param(
        _after_header("SELECT 1\n\\restrict abc\n;\n"),
        "\\restrict",
        id="restrict-inside-a-statement",
    ),
    pytest.param(_after_header("SET client_encoding = 'SJIS';\n"), "client encoding", id="sjis"),
    pytest.param(_after_header("SELECT 1;\x00\n"), "NUL", id="nul"),
]

#: Hostile-looking text psql reads as data or comment: accepted, and psql
#: ran none of it.
BENIGN = [
    pytest.param(_after_header("/* outer /* inner */ \\! id */ SELECT 1;\n"), id="nested-comment"),
    pytest.param(_after_header("SELECT $a$ \\! id $a$;\nSELECT $$\n\\! id\n$$;\n"), id="dollar"),
    pytest.param(_after_header("SELECT E'it\\'s \\! id';\n"), id="escape-string"),
    pytest.param(_after_header("SELECT '\\! id' -- \\! id\n;\n"), id="string-and-comment"),
    pytest.param(_after_header('SELECT 1 AS "\\! id";\n'), id="quoted-identifier"),
    pytest.param("SELECT '\\\\';\n", id="even-backslashes-before-a-quote"),
]


def _dump(tmp_path: Path, text: str, name: str = "dump.sql") -> Path:
    """
    Write a dump byte for byte.

    Args:
        tmp_path: Directory to write into.
        text: The dump, latin-1 so every byte round-trips.
        name: File name.

    Returns:
        The file's path.
    """
    path = tmp_path / name
    path.write_bytes(text.encode("latin-1"))
    return path


class TestCheckPlainDump:
    """The check reads a dump the way psql's lexer does."""

    def test_a_real_pg_dump_is_accepted(self, tmp_path: Path) -> None:
        check_plain_dump(_dump(tmp_path, PG_DUMP))

    def test_a_real_inserts_dump_is_accepted(self, tmp_path: Path) -> None:
        check_plain_dump(_dump(tmp_path, PG_DUMP_INSERTS))

    def test_a_gzipped_dump_is_read_through(self, tmp_path: Path) -> None:
        path = tmp_path / "dump.sql.gz"
        path.write_bytes(gzip.compress(PG_DUMP.encode()))

        check_plain_dump(path)

    def test_a_gzipped_hostile_dump_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "dump.sql.gz"
        path.write_bytes(gzip.compress((HEADER + "\\! id\n").encode()))

        with pytest.raises(DatabaseBackupError, match="line 2"):
            check_plain_dump(path)

    @pytest.mark.parametrize("text", BENIGN)
    def test_backslashes_psql_reads_as_data_are_accepted(self, tmp_path: Path, text: str) -> None:
        check_plain_dump(_dump(tmp_path, text))

    @pytest.mark.parametrize(("text", "named"), HOSTILE)
    def test_a_meta_command_psql_would_run_is_refused(
        self, tmp_path: Path, text: str, named: str
    ) -> None:
        with pytest.raises(DatabaseBackupError) as caught:
            check_plain_dump(_dump(tmp_path, text))

        assert named in caught.value.message
        assert caught.value.details

    def test_the_refusal_names_the_line(self, tmp_path: Path) -> None:
        text = PG_DUMP.replace("COMMIT;\n", "COMMIT;\n\\! id\n")

        with pytest.raises(DatabaseBackupError) as caught:
            check_plain_dump(_dump(tmp_path, text))

        line = text.splitlines().index("\\! id") + 1
        assert f"line {line} " in caught.value.message

    def test_a_create_dump_is_refused_with_how_to_retake_it(self, tmp_path: Path) -> None:
        text = PG_DUMP.replace(
            "SET row_security = off;\n",
            "SET row_security = off;\nCREATE DATABASE shop;\n\\connect shop\n",
        )

        with pytest.raises(DatabaseBackupError) as caught:
            check_plain_dump(_dump(tmp_path, text))

        assert "--create" in caught.value.details

    def test_a_custom_format_dump_is_named_as_such(self, tmp_path: Path) -> None:
        path = tmp_path / "dump.sql"
        path.write_bytes(b"PGDMP\x01\x0f\x00\x04\x08\x01\x01")

        with pytest.raises(DatabaseBackupError, match="custom format"):
            check_plain_dump(path)

    def test_a_truncated_gzip_is_an_error_not_a_pass(self, tmp_path: Path) -> None:
        path = tmp_path / "dump.sql.gz"
        path.write_bytes(gzip.compress(PG_DUMP.encode())[:40])

        with pytest.raises(DatabaseBackupError, match="Could not read"):
            check_plain_dump(path)


@pytest.fixture
def postgres(runner: FakeRunner, tmp_path: Path) -> Iterator[PostgresManager]:
    """
    Provide a PostgreSQL manager whose ``shop`` database exists.

    Args:
        runner: The fake runner installed process-wide.
        tmp_path: Per-test temporary directory.

    Yields:
        The manager.
    """
    runner.script(list(PSQL), stdout="1\n")
    WASMStore.reset_instance()
    manager = PostgresManager()
    manager.BACKUP_DIR = tmp_path / "backups"
    try:
        yield manager
    finally:
        WASMStore.reset_instance()


class TestRestore:
    """The check runs before the restore touches anything."""

    def test_a_hostile_dump_is_refused_before_the_database_is_dropped(
        self, postgres: PostgresManager, runner: FakeRunner, tmp_path: Path
    ) -> None:
        dump = _dump(tmp_path, PG_DUMP.replace("COMMIT;\n", "COMMIT;\n\\! id\n"))

        with pytest.raises(DatabaseBackupError, match="meta-command"):
            postgres.restore("shop", dump, drop_existing=True)

        # Nothing was dropped, staged or handed to psql.
        assert not any("DROP DATABASE" in (sent or "") for sent in runner.inputs)
        assert not runner.calls_to("cp")
        assert not any("-f" in call for call in runner.calls)

    def test_a_real_dump_is_restored_with_psql(
        self, postgres: PostgresManager, runner: FakeRunner, tmp_path: Path
    ) -> None:
        dump = _dump(tmp_path, PG_DUMP)

        postgres.restore("shop", dump)

        staged = str(postgres.BACKUP_DIR / ".staging" / "postgresql-restore-shop.sql")
        assert runner.calls[-1][: len(PSQL)] == PSQL
        assert runner.calls[-1][-2:] == ("-f", staged)

    def test_a_custom_format_restore_is_not_scanned(
        self, postgres: PostgresManager, runner: FakeRunner, tmp_path: Path
    ) -> None:
        # pg_restore has no meta-commands; its input is a binary archive.
        dump = tmp_path / "dump.custom"
        dump.write_bytes(b"PGDMP\x01\x0f\x00\\! id\n")

        postgres.restore("shop", dump, format="custom")

        assert runner.calls[-1][4] == "pg_restore"
