"""
Named snapshots of a library project's design (see app/project_library.py):
a lightweight checkpoint under the project you are already working on -
distinct from "Save Project" (silently overwrites the bound project) and
"Save Project As" (creates a whole new, separate top-level library
entry). Lets an operator check in progress without losing their place or
cluttering the project list, then come back to any earlier point later.

Stored as projects/<project_id>/snapshots/<snapshot_id>/ (its own small
directory: snapshot.json metadata + design.json) - design.json is just
design.to_dict's own shape, the same one app/design_store.py already
persists and app/design.py's own from_dict already round-trips, so
restoring a snapshot never asks the template service anything either.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app import design, project_library

_ID_PATTERN = re.compile(r"[a-f0-9]{32}")

MAX_SNAPSHOTS_PER_PROJECT = 50


def _snapshots_dir(project_id: str) -> Path:
    directory = project_library.project_path(project_id) / "snapshots"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _snapshot_path(project_id: str, snapshot_id: str) -> Path:
    if not _ID_PATTERN.fullmatch(snapshot_id):
        raise ValueError("Invalid snapshot ID")
    path = _snapshots_dir(project_id) / snapshot_id
    if not path.is_dir():
        raise FileNotFoundError("Snapshot not found")
    return path


def list_snapshots(project_id: str) -> list[dict[str, Any]]:
    """:raises FileNotFoundError/ValueError: no such project"""
    directory = _snapshots_dir(project_id)
    return sorted(
        (
            json.loads((path / "snapshot.json").read_text())
            for path in directory.iterdir()
            if path.is_dir() and _ID_PATTERN.fullmatch(path.name)
        ),
        key=lambda item: item["created_at"],
        reverse=True,
    )


def create(project_id: str, name: str, state: design.DesignState) -> dict[str, Any]:
    """:raises ValueError: a bad name. :raises FileNotFoundError: no such project"""
    name = name.strip()
    if not name or len(name) > 120 or any(ord(char) < 32 for char in name):
        raise ValueError("Snapshot name must contain 1-120 printable characters")
    directory = _snapshots_dir(project_id)
    existing = sorted(list_snapshots(project_id), key=lambda item: item["created_at"])
    for doomed in existing[: max(0, len(existing) - MAX_SNAPSHOTS_PER_PROJECT + 1)]:
        shutil.rmtree(directory / doomed["id"], ignore_errors=True)

    snapshot_id = uuid4().hex
    snapshot_dir = directory / snapshot_id
    snapshot_dir.mkdir()
    metadata = {
        "id": snapshot_id,
        "name": name,
        "created_at": datetime.now(UTC).isoformat(),
    }
    (snapshot_dir / "snapshot.json").write_text(json.dumps(metadata), encoding="utf-8")
    (snapshot_dir / "design.json").write_text(
        json.dumps(design.to_dict(state), indent=2), encoding="utf-8"
    )
    return metadata


def restore(project_id: str, snapshot_id: str) -> design.DesignState:
    """:raises ValueError/FileNotFoundError: bad id(s), or a corrupt snapshot"""
    directory = _snapshot_path(project_id, snapshot_id)
    try:
        data = json.loads((directory / "design.json").read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Snapshot data is invalid: {exc}") from exc
    return design.from_dict(data)


def delete(project_id: str, snapshot_id: str) -> None:
    """:raises ValueError/FileNotFoundError: bad id(s)"""
    directory = _snapshot_path(project_id, snapshot_id)
    doomed = directory.with_name(f".deleting-{directory.name}")
    directory.rename(doomed)
    shutil.rmtree(doomed, ignore_errors=True)
