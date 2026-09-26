# Copyright (c) 2024-2026 Yago Lopez Prado
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Asking an application whether it came up, and reading what failed when it did not.
"""

from __future__ import annotations

import time
import urllib.request
from collections.abc import Callable
from email.message import Message
from typing import IO
from urllib.error import HTTPError, URLError

from wasm.core.runner import CommandResult

#: A local HTTP request that has not answered in this long is not going to.
PROBE_TIMEOUT = 5


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report a redirect as the answer instead of following it."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        """
        Decline every redirect, so it surfaces as an HTTPError with its code.

        Args:
            req: The request that was redirected.
            fp: The response body.
            code: The redirect status.
            msg: The reason phrase.
            headers: The response headers.
            newurl: Where the redirect points.

        Returns:
            None, which makes urllib raise instead of following.
        """
        return None


def answers(status: int) -> bool:
    """
    Tell whether a status code means the application is up.

    Anything but a server error: a 404 on ``/``, a login page's 401 or a
    redirect to HTTPS all come from a process that started and is routing
    requests. Rolling back a release because its root path is not a page
    would throw away a good build.

    Args:
        status: HTTP status code of the response.

    Returns:
        True for every status below 500.
    """
    return status < 500


def wait_until_healthy(
    url: str,
    *,
    retries: int = 5,
    delay: float = 2.0,
    on_attempt: Callable[[str], None] | None = None,
    accept: Callable[[int], bool] | None = None,
) -> bool:
    """
    Poll an endpoint until it answers 200 or the attempts run out.

    Args:
        url: Endpoint to request.
        retries: Number of attempts.
        delay: Seconds to wait between attempts.
        on_attempt: Called with a description of each failed attempt.
        accept: Decides which status codes count as healthy, such as
            :func:`answers`. When given, redirects are not followed: the
            redirect itself is the answer, and following one to the public
            HTTPS name would probe something other than this process. None
            keeps the strict "200 after redirects" check.

    Returns:
        True when the endpoint answered as required.
    """
    opener = (
        urllib.request.build_opener(_NoRedirect())
        if accept is not None
        else urllib.request.build_opener()
    )
    for attempt in range(retries):
        try:
            # The URL is always http://127.0.0.1:<port><path>, built here; S310
            # guards against a caller-supplied scheme, which cannot occur.
            with opener.open(url, timeout=PROBE_TIMEOUT) as response:
                # With an expectation, it alone decides: an application told
                # to answer 204 is not healthy because it answered 200.
                if accept(response.status) if accept is not None else response.status == 200:
                    return True
                if on_attempt is not None:
                    on_attempt(
                        f"Health check attempt {attempt + 1} failed: "
                        f"HTTP {response.status} is not an expected status"
                    )
        except HTTPError as e:
            # An HTTP error status is still an answer; whether it is a
            # healthy one is the caller's call.
            if accept is not None and accept(e.code):
                return True
            if on_attempt is not None:
                on_attempt(f"Health check attempt {attempt + 1} failed: {e}")
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
