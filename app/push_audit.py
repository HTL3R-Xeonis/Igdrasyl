"""Bounded JSONL history of config pushes. Never stores commands or output."""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from app.config_push import DeviceResult

MAX_RECORDS = 50
_LOCK = threading.Lock()


def _path() -> Path:
    return Path(os.getenv("TOPOLOGYOPERATOR_PUSH_AUDIT_FILE", "push_audit.jsonl"))


def _lines(path: Path, limit: int) -> deque[str]:
    try:
        with path.open(encoding="utf-8", errors="replace") as file:
            return deque(file, maxlen=limit)
    except FileNotFoundError:
        return deque(maxlen=limit)


def record(
    entries: list[DeviceResult], note: str | None = None, *, interrupted: bool = False
) -> None:
    """Add one finished run. An audit I/O failure must not change the push outcome."""
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "status": "interrupted"
        if interrupted
        else ("ok" if all(result.ok for result in entries) else "failed"),
        "note": note,
        "devices": [
            {
                "device": result.device,
                "transport": result.transport,
                "ok": result.ok,
                "executed": result.executed,
                "total_commands": result.total_commands,
            }
            for result in entries
        ],
    }
    line = json.dumps(entry) + "\n"
    path = _path()
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = _lines(path, MAX_RECORDS)
            if len(lines) < MAX_RECORDS and (not lines or lines[-1].endswith("\n")):
                with path.open("a", encoding="utf-8") as file:
                    file.write(line)
                return
            lines.append(line)
            with NamedTemporaryFile(dir=path.parent, delete=False) as file:
                temp = Path(file.name)
                try:
                    file.write(
                        "".join(row.rstrip("\n") + "\n" for row in lines).encode(
                            "utf-8"
                        )
                    )
                    file.close()
                    temp.replace(path)
                finally:
                    temp.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Couldn't record config push audit entry: {}", exc)


def read(limit: int = MAX_RECORDS) -> list[dict[str, Any]]:
    """Read newest first. Ignore incomplete JSON left by an interrupted write."""
    if limit <= 0:
        return []
    with _LOCK:
        lines = _lines(_path(), min(limit, MAX_RECORDS))
    entries = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


@contextmanager
def track(note: str) -> Iterator[list[DeviceResult]]:
    """Record one run whether it returns device results or raises."""
    entries: list[DeviceResult] = []
    try:
        yield entries
    except Exception:
        record(entries, note, interrupted=True)
        raise
    else:
        record(entries, note)
