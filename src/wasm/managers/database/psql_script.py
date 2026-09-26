# Copyright (c) 2024-2025 Yago López Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Find the psql meta-commands in a plain-format dump before psql runs it.

psql reads a file given with ``-f`` as a script: a backslash outside a string,
a quoted identifier or a comment starts a meta-command wherever it appears, and
``\\!`` is a shell, ``\\o`` writes a file, ``\\set ON_ERROR_STOP 0`` makes the rest
of the restore ignore its errors. psql has no switch that turns them off in
``-f`` mode, so the dump is read here first, the way psql's own lexer
(``psqlscan.l``) reads it, and refused if psql would find a meta-command in it.

A real pg_dump plain dump has three kinds of backslash, and each is handled on
purpose:

- COPY data. ``COPY ... FROM stdin;`` is followed by rows in which backslashes
  are escapes (``\\N``, ``\\\\``, ``\\t``), up to a line that is exactly ``\\.``.
  psql sends those lines to the server without lexing them, and so does this.
- ``\\restrict <key>`` and ``\\unrestrict <key>``, which pg_dump writes around
  the dump since PostgreSQL 17.6, 16.10, 15.14, 14.19 and 13.22. They are the
  one meta-command pair accepted, alone on their line: they only restrict psql
  further.
- ``\\connect``, which pg_dump writes with ``--create``. It is refused: it would
  restore into another database than the one the operator chose, and a
  ``--create`` dump cannot be restored into an existing database anyway.

The scanner errs towards refusing. Where psql's reading depends on something
the file does not settle - ``standard_conforming_strings`` before the dump sets
it (``\\'`` ends a plain string when it is on, not when it is off), or the psql
version, which decides whether a ``;`` inside ``CREATE FUNCTION ... BEGIN
ATOMIC`` ends a statement - a construct whose meaning would differ is refused
instead of guessed. pg_dump sets ``standard_conforming_strings = on`` in its
header, before any string that could be ambiguous.

This is not a sandbox for the SQL itself. A restore runs as the cluster
superuser and trusts the dump's statements, which can already run programs as
the ``postgres`` account (``COPY ... TO PROGRAM``) - the same account psql runs
as. So the scanner follows the settings the dump states; SQL that changes
psql's reading behind its back (dynamic SQL flipping a setting) gains nothing
the SQL did not already have. See
:meth:`wasm.managers.database.postgres.PostgresManager.restore`.
"""

from __future__ import annotations

import gzip
import re
from itertools import pairwise
from pathlib import Path

from wasm.core.exceptions import DatabaseBackupError

#: The meta-commands pg_dump itself writes that are accepted, alone on a line.
_ALLOWED_META = re.compile(r"\\(?:restrict|unrestrict)[ \t]+[A-Za-z0-9]+[ \t]*\r?\n?")

#: The line that ends COPY data, exactly as psql's handleCopyIn compares it.
_COPY_END = ("\\.\n", "\\.\r\n")

#: Encodings psql lexes byte-wise differently: their second byte can be ``\``.
#: A dump that switches psql to one could hide a quote from this scanner.
_UNSAFE_ENCODING = re.compile(
    r"\b(?:client_encoding|names)\b.*\b(?:sjis|shift_?jis|mskanji|win932|windows932|"
    r"big5|win950|windows950|gbk|win936|windows936|cp936|uhc|win949|windows949|"
    r"gb18030|johab)\b",
    re.IGNORECASE,
)

#: One token in psql's INITIAL state. Order matters where regexes overlap:
#: ``E'`` before an identifier, a comment before an operator character.
_TOKEN = re.compile(
    r"""
      (?P<ws>[ \t\n\r\f\v]+)
    | (?P<line_comment>--)
    | (?P<block_comment>/\*)
    | (?P<estring>[eE]')
    | (?P<ident>[A-Za-z_\x80-\xff][A-Za-z0-9_$\x80-\xff]*)
    | (?P<dollar>\$(?:[A-Za-z_\x80-\xff][A-Za-z0-9_\x80-\xff]*)?\$)
    | (?P<squote>')
    | (?P<dquote>")
    | (?P<backslash>\\)
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<semicolon>;)
    | (?P<other>[0-9.,:=<>+*|&!@#%^~?\[\]{}]+|.)
    """,
    re.VERBOSE,
)

#: What ends a stretch of string content: a quote, or a run of backslashes.
_STRING_STOP = re.compile(r"'|\\+")

#: What ends a stretch of E'...' content: a quote, or a backslash escape.
_ESCAPE_STOP = re.compile(r"[\\']")

#: What changes a block comment's nesting.
_COMMENT_STOP = re.compile(r"/\*|\*/")

#: Values ``SET standard_conforming_strings`` accepts for "on".
_ON_VALUES = frozenset({"on", "true", "yes", "1"})

#: Words that make psql 14+ start counting BEGIN/END to find a statement's
#: end (CREATE [OR REPLACE] FUNCTION|PROCEDURE ... BEGIN ATOMIC ... END).
_ROUTINE_WORDS = frozenset({"create", "function", "procedure", "or", "replace"})

_NORMAL, _PLAIN, _ESCAPE, _IDENT, _DOLLAR, _COMMENT, _COPY = range(7)


def _refuse(line: int, what: str, details: str) -> DatabaseBackupError:
    """
    Build the error for a dump that will not be handed to psql.

    Args:
        line: 1-based line number in the dump.
        what: What was found.
        details: How to fix it.

    Returns:
        The error, ready to raise.
    """
    return DatabaseBackupError(
        f"The dump was not restored: line {line} {what}",
        details=details,
    )


class _Scanner:
    """psql's lexer, reduced to what decides where a meta-command can start."""

    def __init__(self) -> None:
        """Start before the first line, in psql's INITIAL state."""
        self.state = _NORMAL
        self.comment_depth = 0
        self.dollar_tag = ""
        self.paren_depth = 0
        # psql 14+'s BEGIN ATOMIC heuristic, as psqlscan.l keeps it.
        self.routine_words: list[str] = []
        self.identifier_count = 0
        self.begin_depth = 0
        # Words at parenthesis depth 0 since the last ';' at depth 0, which is
        # where psql before 14 ends a statement, and whether that is also where
        # psql 14+ ended it.
        self.segment: list[str] = []
        self.segment_is_statement = True
        self.statement_empty = True
        self.pending_copy = False
        # standard_conforming_strings as the dump has set it: True for on,
        # None until a plain SET says so or after anything else touches it.
        self.standard_strings: bool | None = None
        self.mentions_standard_strings = False
        self.line = 0

    def feed(self, text: str) -> None:
        """
        Read one line of the dump, newline included.

        Args:
            text: The line, decoded byte for byte (latin-1).

        Raises:
            DatabaseBackupError: When psql would find a meta-command in it, or
                read it in a way this scanner cannot be sure of.
        """
        self.line += 1
        if "\x00" in text:
            raise _refuse(
                self.line,
                "contains a NUL byte",
                "psql stops reading a line at a NUL byte, so the rest of it would not be "
                "what it appears to be. pg_dump never writes one; re-take the dump.",
            )
        if self.state == _COPY:
            if text in _COPY_END:
                self.state = _NORMAL
            return
        if self.state == _NORMAL and _UNSAFE_ENCODING.search(text):
            raise _refuse(
                self.line,
                "switches psql to a client encoding whose characters can contain a backslash",
                "Re-take the dump with the database's own encoding, or with "
                "pg_dump --encoding=UTF8.",
            )

        if "standard_conforming_strings" in text.lower():
            self.mentions_standard_strings = True

        position = 0
        while position < len(text):
            position = self._step(text, position)

        if self.pending_copy:
            self.pending_copy = False
            self.state = _COPY

    def _step(self, text: str, position: int) -> int:
        """
        Consume what starts at ``position`` in the current state.

        Args:
            text: The current line.
            position: Where to continue.

        Returns:
            The position after what was consumed.
        """
        if self.state == _PLAIN:
            return self._plain_string(text, position)
        if self.state == _ESCAPE:
            return self._escape_string(text, position)
        if self.state == _IDENT:
            end = text.find('"', position)
            if end == -1:
                return len(text)
            if text.startswith('""', end):
                return end + 2
            self.state = _NORMAL
            return end + 1
        if self.state == _DOLLAR:
            end = text.find(self.dollar_tag, position)
            if end == -1:
                return len(text)
            self.state = _NORMAL
            return end + len(self.dollar_tag)
        if self.state == _COMMENT:
            return self._block_comment(text, position)
        return self._normal(text, position)

    def _plain_string(self, text: str, position: int) -> int:
        """
        Consume inside a ``'...'`` string.

        Whether a backslash escapes the next character depends on
        ``standard_conforming_strings``, which psql reads from the server after
        every statement. Once the dump has set it on, a backslash is an
        ordinary character. Until then the two readings only disagree about a
        quote that follows an odd run of backslashes (``\\'`` ends the string
        in one and not in the other), so that is refused and everything else
        is read the same either way.

        Args:
            text: The current line.
            position: Where to continue.

        Returns:
            The position after what was consumed.

        Raises:
            DatabaseBackupError: At a quote after an odd run of backslashes.
        """
        if self.standard_strings:
            end = text.find("'", position)
            if end == -1:
                return len(text)
            if text.startswith("''", end):
                return end + 2
            self.state = _NORMAL
            return end + 1
        match = _STRING_STOP.search(text, position)
        if match is None:
            return len(text)
        if match.group() != "'":
            if len(match.group()) % 2 and text.startswith("'", match.end()):
                raise _refuse(
                    self.line,
                    "has a backslash before a quote in a string literal",
                    "Whether that quote ends the string depends on the server's "
                    "standard_conforming_strings, so psql could read the rest of the line "
                    "as commands. Take the dump in pg_dump's custom format (--format custom), "
                    "which psql does not read, or write the value as E'...'.",
                )
            return match.end()
        at = match.start()
        if text.startswith("''", at):
            return at + 2
        self.state = _NORMAL
        return at + 1

    def _escape_string(self, text: str, position: int) -> int:
        """
        Consume inside an ``E'...'`` string, where a backslash escapes anything.

        Args:
            text: The current line.
            position: Where to continue.

        Returns:
            The position after what was consumed.
        """
        match = _ESCAPE_STOP.search(text, position)
        if match is None:
            return len(text)
        at = match.start()
        if text[at] == "\\":
            return at + 2
        if text.startswith("''", at):
            return at + 2
        self.state = _NORMAL
        return at + 1

    def _block_comment(self, text: str, position: int) -> int:
        """
        Consume inside a ``/* ... */`` comment, which nests.

        Args:
            text: The current line.
            position: Where to continue.

        Returns:
            The position after what was consumed.
        """
        match = _COMMENT_STOP.search(text, position)
        if match is None:
            return len(text)
        if match.group() == "/*":
            self.comment_depth += 1
        else:
            self.comment_depth -= 1
            if self.comment_depth == 0:
                self.state = _NORMAL
        return match.end()

    def _normal(self, text: str, position: int) -> int:
        """
        Consume one token outside any literal or comment.

        Args:
            text: The current line.
            position: Where to continue.

        Returns:
            The position after the token.

        Raises:
            DatabaseBackupError: At a meta-command, or at a statement psql
                versions would split differently.
        """
        match = _TOKEN.match(text, position)
        if match is None:  # pragma: no cover - the last alternative matches anything
            return len(text)
        kind, token = match.lastgroup, match.group()
        if self.pending_copy and kind not in ("ws", "line_comment"):
            raise _refuse(
                self.line,
                "continues after a COPY ... FROM stdin statement",
                "psql reads the COPY data from the next line and the rest of this one "
                "afterwards. Put the COPY statement on a line of its own.",
            )
        if kind == "ws":
            return match.end()
        if kind == "line_comment":
            return len(text)
        if kind == "backslash":
            return self._meta_command(text, position)
        if kind == "block_comment":
            self.state, self.comment_depth = _COMMENT, 1
            return match.end()

        self.statement_empty = False
        if kind == "semicolon":
            self._semicolon()
        elif kind == "lparen":
            self.paren_depth += 1
        elif kind == "rparen":
            self.paren_depth = max(0, self.paren_depth - 1)
        elif kind == "ident":
            self._identifier(token.lower())
        elif kind in ("squote", "estring", "dquote", "dollar"):
            if self.paren_depth == 0:
                self.segment.append("?")
            if kind == "squote":
                self.state = _PLAIN
            elif kind == "estring":
                self.state = _ESCAPE
            elif kind == "dquote":
                self.state = _IDENT
            else:
                self.state, self.dollar_tag = _DOLLAR, token
        elif self.paren_depth == 0:
            self.segment.append(token)
        return match.end()

    def _identifier(self, word: str) -> None:
        """
        Track a word the way psqlscan.l's ``{identifier}`` rule does.

        Args:
            word: The word, lower-cased.
        """
        if self.paren_depth == 0:
            self.segment.append(word)
        if self.identifier_count == 0:
            self.routine_words = []
        if word in _ROUTINE_WORDS and self.identifier_count < 4:
            self.routine_words.append(word[0])
        elif self.identifier_count < 4:
            self.routine_words.append("")
        self.identifier_count += 1

        head = [*self.routine_words, "", "", "", ""][:4]
        in_routine = head[0] == "c" and (
            head[1] in ("f", "p") or (head[1] == "o" and head[2] == "r" and head[3] in ("f", "p"))
        )
        if not in_routine or self.paren_depth != 0:
            return
        if word == "begin":
            self.begin_depth += 1
        elif word == "case" and self.begin_depth >= 1:
            self.begin_depth += 1
        elif word == "end" and self.begin_depth > 0:
            self.begin_depth -= 1

    def _semicolon(self) -> None:
        """
        Handle a ``;``, which may end a statement and start COPY data.

        Raises:
            DatabaseBackupError: At a COPY ... FROM stdin that one psql
                version would run and another would not.
        """
        if self.paren_depth != 0:
            return
        is_copy = _is_copy_from_stdin(self.segment)
        ends_statement = self.begin_depth == 0
        if is_copy and not (ends_statement and self.segment_is_statement):
            raise _refuse(
                self.line,
                "has a COPY ... FROM stdin inside a routine body",
                "psql 14 and later read BEGIN ATOMIC bodies as one statement and older "
                "psql does not, so whether the lines after it are data or commands "
                "depends on the client. Move the COPY out of the routine.",
            )
        if ends_statement:
            self._track_standard_strings()
        self.segment = []
        if ends_statement:
            self.pending_copy = is_copy
            self.segment_is_statement = True
            self.statement_empty = True
            self.routine_words = []
            self.identifier_count = 0
        else:
            self.segment_is_statement = False

    def _track_standard_strings(self) -> None:
        """
        Follow ``standard_conforming_strings`` through the statement just ended.

        Only a plain ``SET [SESSION] standard_conforming_strings = on`` makes
        it known. Anything else that names it - ``off``, ``SET LOCAL``,
        ``RESET``, ``set_config(...)`` - makes it unknown again, which only
        costs refusing the one ambiguous sequence.
        """
        words = [word for word in self.segment if word != "session"]
        if (
            len(words) == 4
            and words[:2] == ["set", "standard_conforming_strings"]
            and words[2] in ("=", "to")
            and words[3] in _ON_VALUES
        ):
            self.standard_strings = True
        elif self.mentions_standard_strings:
            self.standard_strings = None
        self.mentions_standard_strings = False

    def _meta_command(self, text: str, position: int) -> int:
        """
        Handle a backslash psql would take for a meta-command.

        Args:
            text: The current line.
            position: Where the backslash is.

        Returns:
            The end of the line, for the one pair that is accepted.

        Raises:
            DatabaseBackupError: For every other meta-command.
        """
        if (
            self.statement_empty
            and not text[:position].strip()
            and _ALLOWED_META.fullmatch(text, position)
        ):
            return len(text)
        command = text[position:].split(None, 1)[0] if text[position:].strip() else "\\"
        if command in ("\\c", "\\connect"):
            raise _refuse(
                self.line,
                f"switches to another database ({command})",
                "The dump was taken with pg_dump --create, which connects to the database "
                "it creates. WASM restores into the database you chose: take the dump "
                "without --create (WASM's own backups are), or remove its CREATE DATABASE "
                "and \\connect lines.",
            )
        raise _refuse(
            self.line,
            f"holds the psql meta-command {command[:40]}",
            "psql would run it on this server, not send it to the database: \\! runs a "
            "shell, \\o writes a file. A dump written by pg_dump has none outside COPY "
            "data; restore only dumps pg_dump produced.",
        )


def _is_copy_from_stdin(words: list[str]) -> bool:
    """
    Report whether a statement makes the server read COPY data from psql.

    Args:
        words: The statement's words at parenthesis depth 0, lower-cased,
            with every literal and quoted identifier as ``?``.

    Returns:
        True for ``COPY ... FROM STDIN``. A subquery's ``FROM stdin`` is inside
        parentheses and is not among the words.
    """
    if not words or words[0] != "copy":
        return False
    return any(pair == ("from", "stdin") for pair in pairwise(words))


def check_plain_dump(path: Path) -> None:
    """
    Refuse a plain-format dump in which psql would find a meta-command.

    The file is read line by line, as bytes split on newlines the way psql's
    ``fgets`` splits them, so a dump of any size costs one line of memory. A
    ``.gz`` file is decompressed on the way, as the restore's staging does.

    Args:
        path: The dump, plain text or gzipped plain text.

    Raises:
        DatabaseBackupError: When the dump is in pg_dump's custom format,
            holds a meta-command outside COPY data, or cannot be read.
    """
    scanner = _Scanner()
    try:
        with gzip.open(path, "rb") if path.suffix == ".gz" else open(path, "rb") as handle:
            if handle.read(5) == b"PGDMP":
                raise DatabaseBackupError(
                    "The dump is in pg_dump's custom format, not plain SQL",
                    details="Restore it with the custom format (--format custom).",
                )
            handle.seek(0)
            for raw in handle:
                scanner.feed(raw.decode("latin-1"))
    except (OSError, EOFError) as exc:
        raise DatabaseBackupError(
            f"Could not read the dump {path}",
            details=f"{exc}. Check that the file is a complete plain or gzipped dump.",
        ) from exc
