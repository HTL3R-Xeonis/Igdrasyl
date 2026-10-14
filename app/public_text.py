"""Sanitize backend diagnostics before returning them to browser clients."""

from __future__ import annotations

import re
from typing import Any

_ABSOLUTE_PATH = re.compile(r"(?<![:/])/(?:[A-Za-z0-9_.@+ -]+/)+[A-Za-z0-9_.@+ -]+")


def redact_paths(value: Any) -> Any:
    if isinstance(value, str):
        return _ABSOLUTE_PATH.sub("[server file]", value)
    if isinstance(value, list):
        return [redact_paths(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_paths(item) for key, item in value.items()}
    return value
