# Copyright (c) 2024-2026 Yago Lopez Prado
# Licensed under WASM-NCSAL 1.0 (Commercial use prohibited)
# https://github.com/Perkybeet/wasm/blob/main/LICENSE

"""
Asking an application whether it came up, and reading what failed when it did not.
"""

from __future__ import annotations

import time
import urllib.request
from collections.abc import Callable
from urllib.error import URLError

from wasm.core.runner import CommandResult

#: A local HTTP request that has not answered in this long is not going to.
PROBE_TIMEOUT = 5


def wait_until_healthy(
    url: str,
    *,
    retries: int = 5,
    delay: float = 2.0,
    on_attempt: Callable[[str], None] | None = None,
) -> bool:
    """
    Poll an endpoint until it answers 200 or the attempts run out.

    Args:
        url: Endpoint to request.
        retries: Number of attempts.
        delay: Seconds to wait between attempts.
        on_attempt: Called with a description of each failed attempt.

    Returns:
        True when the endpoint answered 200.
    """
    for attempt in range(retries):
        try:
            # The URL is always http://127.0.0.1:<port><path>, built here; S310
            # guards against a caller-supplied scheme, which cannot occur.
            with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as response:
                if response.status == 200:
                    return True
        # A health check is a probe: any failure to reach the app means "not
        # ready yet", never "abort the deployment".
        except (URLError, OSError, ValueError) as e:
            if on_attempt is not None:
                on_attempt(f"Health check attempt {attempt + 1} failed: {e}")

        if attempt < retries - 1:
            time.sleep(delay)

    return False


def failure_output(result: CommandResult) -> str:
    """
    Combine what a failed command said, whichever stream it said it on.

    npm writes its real diagnosis to stdout and a summary to stderr; pip does
    the reverse. Showing only one of them is how "Build failed" ended up being
    the entire error message.

    Args:
        result: The failed command outcome.

    Returns:
        The combined output, stripped, or an empty string when there was none.
    """
    parts = [part for part in (result.stderr, result.stdout) if part and part.strip()]
    return "\n".join(part.strip() for part in parts)
