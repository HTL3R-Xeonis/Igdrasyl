"""
Tails and parses Settings.LOG_FILE_PATH - the file app/logging_setup.py
points loguru's file sink at, in TopologyBuilder's own format (see that
module's docstring). Kept in its own module, independently testable,
same separation main.py already uses elsewhere.

Parsing (rather than handing the frontend raw text) lets the View
menu's Logs panel color each line by level the same way loguru's own
colorized console output would, instead of a flat wall of monospace
text - see _LINE_RE for the exact format this expects.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.settings import Settings

from app.public_text import redact_paths

# Matches app/logging_setup.py's own _FILE_FORMAT once loguru has
# substituted it: "2026-09-24 21:34:04.917 | ERROR    | api_handler.py:50 - message"
_LINE_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} [\d:.]+) \| "
    r"(?P<level>\S+)\s*\| "
    r"(?P<location>\S+) - "
    r"(?P<message>.*)$"
)


def tail_log_lines(count: int) -> list[dict[str, Any]]:
    """
    The most recent `count` log lines, parsed into
    {time, level, location, message} - a line that doesn't match the
    expected format (e.g. a multi-line traceback's continuation lines)
    is still returned, just with time/level/location empty and the
    whole raw line as `message`, so nothing is silently dropped.
    :param count: how many of the most recent lines to return
    :return: oldest first, same order the file itself has
    """
    path = Path(Settings.LOG_FILE_PATH)
    if not path.exists():
        return []

    with open(path) as file:
        lines = file.readlines()[-count:]

    parsed = []
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        match = _LINE_RE.match(line)
        if match:
            parsed.append(
                {
                    "time": match.group("time"),
                    "level": match.group("level").strip(),
                    "location": match.group("location"),
                    "message": redact_paths(match.group("message")),
                }
            )
        else:
            parsed.append(
                {
                    "time": "",
                    "level": "",
                    "location": "",
                    "message": redact_paths(line),
                }
            )
    return parsed
