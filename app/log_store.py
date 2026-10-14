"""
The application log file with a hard retention limit: at most
MAX_TOTAL_LINES (100 000) lines are ever kept on disk.

Two files are used - the current one (Settings.LOG_FILE_PATH) and its
predecessor (the same name plus ".1"). When the current file reaches half
the limit it replaces the predecessor and a fresh one starts, so the two
together never exceed the limit and the oldest lines simply fall away. A
rename is all a rotation costs: no big file is ever rewritten, unlike
trimming one file back to size, and a line is counted as it is written
rather than found by re-reading the file.

Reading always spans both files, so a view right after a rotation still
shows the lines from before it.
"""

from __future__ import annotations

import os
import re
import threading
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

MAX_TOTAL_LINES = 100_000
LINES_PER_FILE = MAX_TOTAL_LINES // 2

LEVEL_SEVERITY = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

# "2026-09-24 21:34:04.917 | ERROR    | api_handler.py:request:50 - message"
_ENTRY_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} [\d:.]+) \| "
    r"(?P<level>\S+)\s*\| "
    r"(?P<location>\S+) - "
    r"(?P<message>.*)$"
)


def backup_path(path: str | os.PathLike[str]) -> Path:
    return Path(f"{path}.1")


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    last = b""
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            count += chunk.count(b"\n")
            last = chunk[-1:]
    # A last line without a trailing newline still is a line.
    return count + (1 if last and last != b"\n" else 0)


def _keep_last_lines(path: Path, keep: int) -> None:
    """Shrinks an oversized file (e.g. one left by an older, size-based rotation) to its last `keep` lines."""
    with open(path, "rb") as file:
        tail = deque(file, maxlen=keep)
    temporary = path.with_name(path.name + ".trim")
    with open(temporary, "wb") as file:
        file.writelines(tail)
    os.replace(temporary, path)


class LineLimitedFile:
    """
    A loguru sink (`logger.add(sink.write, ...)`) writing to a file that
    is rotated by line count - see this module's docstring.
    """

    def __init__(
        self, path: str | os.PathLike[str], lines_per_file: int = LINES_PER_FILE
    ) -> None:
        self._path = Path(path)
        self._backup = backup_path(self._path)
        self._lines_per_file = lines_per_file
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Whatever an older version left behind may already be over the
        # limit - bring it under before the first write, or the "never more
        # than MAX_TOTAL_LINES" promise would not hold from the start.
        for existing in (self._path, self._backup):
            if existing.exists() and _count_lines(existing) > lines_per_file:
                _keep_last_lines(existing, lines_per_file)

        self._count = _count_lines(self._path)
        # Kept open for the sink's whole life - every write reuses it.
        self._handle = open(self._path, "a", encoding="utf-8")  # noqa: SIM115

    @property
    def line_count(self) -> int:
        """Lines in the current file."""
        return self._count

    def write(self, message: str) -> None:
        lines = max(1, str(message).count("\n"))
        with self._lock:
            if self._count and self._count + lines > self._lines_per_file:
                self._rotate()
            self._handle.write(message)
            self._handle.flush()
            self._count += lines

    def _rotate(self) -> None:
        self._handle.close()
        os.replace(self._path, self._backup)
        self._handle = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
        self._count = 0

    def clear(self) -> None:
        """Deletes every logged line, from both files, and keeps writing to the (now empty) current one."""
        with self._lock:
            self._handle.close()
            self._backup.unlink(missing_ok=True)
            self._handle = open(self._path, "w", encoding="utf-8")  # noqa: SIM115
            self._count = 0

    def stop(self) -> None:
        with self._lock:
            self._handle.close()


def _files(path: str | os.PathLike[str]) -> list[Path]:
    """The log files that exist, oldest first."""
    return [p for p in (backup_path(path), Path(path)) if p.exists()]


def clear_files(path: str | os.PathLike[str]) -> None:
    """Empties the log without a running sink (a sink's own clear() is the normal way)."""
    backup_path(path).unlink(missing_ok=True)
    if Path(path).exists():
        Path(path).write_text("")


def read_lines(path: str | os.PathLike[str], count: int | None = None) -> list[str]:
    """
    The last `count` physical lines (all of them if None) across both log
    files, oldest first, without their trailing newline.
    """
    tail: deque[str] = deque(maxlen=count)
    for file_path in _files(path):
        with open(file_path, encoding="utf-8", errors="replace") as file:
            for line in file:
                tail.append(line.rstrip("\n"))
    return list(tail)


def parse_entries(lines: list[str]) -> list[dict[str, Any]]:
    """
    Groups physical lines into log entries {time, level, location,
    message}. A line that doesn't start a new entry (a traceback's
    continuation) is appended to the previous entry's message, so one
    logged event stays one entry; lines before the first entry stay
    entries of their own with empty time/level/location.
    """
    entries: list[dict[str, Any]] = []
    for line in lines:
        match = _ENTRY_RE.match(line)
        if match:
            entries.append(
                {
                    "time": match.group("time"),
                    "level": match.group("level").strip(),
                    "location": match.group("location"),
                    "message": match.group("message"),
                }
            )
        elif entries:
            entries[-1]["message"] += "\n" + line
        else:
            entries.append({"time": "", "level": "", "location": "", "message": line})
    return entries


def read_entries(
    path: str | os.PathLike[str],
    count: int,
    min_level: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """
    The most recent `count` entries (after filtering), oldest first.
    :param min_level: only entries at or above this level (e.g. "WARNING");
        entries without a level (stray lines) are kept only without a filter
    :param search: case-insensitive text that must appear in the entry
    """
    minimum = LEVEL_SEVERITY.get((min_level or "").upper())
    needle = (search or "").lower()
    # Filtering needs every line; an unfiltered view only the tail.
    lines = read_lines(path, None if (minimum or needle) else count * 20)
    entries = parse_entries(lines)

    def keep(entry: dict[str, Any]) -> bool:
        if minimum is not None and LEVEL_SEVERITY.get(entry["level"], 0) < minimum:
            return False
        haystack = (
            f"{entry['time']} {entry['level']} {entry['location']} {entry['message']}"
        ).lower()
        return not needle or needle in haystack

    return [entry for entry in entries if keep(entry)][-count:]


def info(path: str | os.PathLike[str]) -> dict[str, Any]:
    """How much is logged and where it stands against the retention limit."""
    files = [
        {"name": p.name, "lines": _count_lines(p), "bytes": p.stat().st_size}
        for p in _files(path)
    ]
    return {
        "max_lines": MAX_TOTAL_LINES,
        "lines": sum(f["lines"] for f in files),
        "files": files,
    }


def iter_text(path: str | os.PathLike[str]) -> Iterator[str]:
    """Both log files, oldest first, in chunks - for a download that never holds the whole log in memory."""
    for file_path in _files(path):
        with open(file_path, encoding="utf-8", errors="replace") as file:
            yield from iter(lambda: file.read(256 * 1024), "")


def iter_lines(path: str | os.PathLike[str]) -> Iterator[str]:
    """Every physical line of both log files, oldest first, without the newline - for a download that rewrites each line (e.g. redacts it) and so must never see half of one."""
    for file_path in _files(path):
        with open(file_path, encoding="utf-8", errors="replace") as file:
            for line in file:
                yield line.rstrip("\n")
