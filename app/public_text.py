"""Sanitize backend diagnostics before returning them to browser clients."""

from __future__ import annotations

import re
from typing import Any

_ABSOLUTE_PATH = re.compile(r"(?<![:/])/(?:[A-Za-z0-9_.@+ -]+/)+[A-Za-z0-9_.@+ -]+")


# The URL of a logged request ("HTTP GET /maintenance/orphans", "WS
# /ws/console/R1") is not a server file, but looks exactly like a path to
# _ABSOLUTE_PATH - redacting it would turn every request line into
# "HTTP GET [server file]> 200".
_REQUEST_TARGET = re.compile(r"(?:^|(?<= - ))((?:HTTP [A-Z]+|WS) \S+)")


def redact_log_text(text: str) -> str:
    """
    redact_paths for a log line or message: everything but the URL of a
    logged request is redacted, so a file path in an error message after
    it still is.
    """
    match = _REQUEST_TARGET.search(text)
    if match is None:
        return redact_paths(text)
    return (
        redact_paths(text[: match.start(1)])
        + match.group(1)
        + redact_paths(text[match.end(1) :])
    )


def redact_paths(value: Any) -> Any:
    if isinstance(value, str):
        return _ABSOLUTE_PATH.sub("[server file]", value)
    if isinstance(value, list):
        return [redact_paths(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_paths(item) for key, item in value.items()}
    return value
