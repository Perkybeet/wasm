"""
Input validation helpers for the WASM web API.

These functions are intentionally free of FastAPI/HTTP (and web package)
dependencies so they can be imported and unit-tested in isolation, even when
the optional web stack (fastapi/jose) is not installed. The API endpoints wrap
them and translate a rejection into an HTTP 400 response.

They exist to close path-traversal, arbitrary-file-write and SQL-to-OS
escalation vectors on the (root-privileged) web surface.
"""

import re
from pathlib import Path
from typing import Union

# systemd unit names and nginx/apache site filenames: letters, digits, dot,
# dash and underscore. Must start with an alphanumeric or underscore so a value
# can never be interpreted as an option (leading '-') or a hidden/relative path
# component (leading '.'). No slashes and no '..' anywhere.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_MAX_NAME_LEN = 128

# SQL fragments that escalate plain query access into OS command/file access on
# the database host (i.e. remote code execution as the DB service account).
# Blocking these keeps ordinary SQL working while removing the RCE primitive.
_SQL_OS_ESCALATION_RE = re.compile(
    r"\bCOPY\b[^;]*\bPROGRAM\b"                 # Postgres COPY ... TO/FROM PROGRAM
    r"|\bINTO\s+(?:OUT|DUMP)FILE\b"             # MySQL SELECT ... INTO OUTFILE/DUMPFILE
    r"|\bLOAD\s+DATA\b[^;]*\bINFILE\b"          # MySQL LOAD DATA INFILE
    r"|\bLOAD_FILE\s*\("                        # MySQL LOAD_FILE()
    r"|\bpg_read_file\b|\bpg_read_binary_file\b|\bpg_ls_dir\b"  # Postgres file read
    r"|\blo_import\b|\blo_export\b"             # Postgres large-object file I/O
    r"|\bsys_exec\b|\bsys_eval\b",              # MySQL sys UDFs
    re.IGNORECASE,
)


def is_safe_resource_name(name: str) -> bool:
    """
    Return True if ``name`` is safe to use as a service/site file name.

    Rejects empty values, anything containing a path separator, ``..`` or a NUL
    byte, names longer than 128 chars, and anything outside the allowed charset.
    """
    if not name or len(name) > _MAX_NAME_LEN:
        return False
    if "/" in name or "\\" in name or ".." in name or "\x00" in name:
        return False
    return bool(_SAFE_NAME_RE.match(name))


def is_within_directory(base_dir: Union[str, Path], target: Union[str, Path]) -> bool:
    """
    Return True if ``target`` resolves to a path inside (or equal to) ``base_dir``.

    Resolves symlinks and ``..`` segments before comparing, so it is safe to use
    against attacker-influenced path components.
    """
    base = Path(base_dir).resolve()
    try:
        resolved = Path(target).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return resolved == base or base in resolved.parents


def query_has_os_escalation(query: str) -> bool:
    """
    Return True if a SQL query contains a known OS command/file-access primitive.

    Used to reject queries that would turn the web SQL console into remote code
    execution on the database host (e.g. ``COPY ... TO PROGRAM``).
    """
    return bool(_SQL_OS_ESCALATION_RE.search(query or ""))
