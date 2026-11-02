"""
Free-text notes the user keeps on nodes (what a box is for, where its login is, what still has to be done).

Same treatment as app/label_layout.py: a per-deployment JSON file (name -> text), git-ignored, never topology truth;
the design sketch keeps its own copy in DesignState.notes (so undo/redo, the autosave and saved library projects
cover it), and a .topoproj bundle carries them as notes.json. An empty note means "no note" and is not stored.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_NOTES_FILE_ENV_VAR = "TOPOLOGYOPERATOR_NOTES_FILE"
_DEFAULT_NOTES_FILE = "node_notes.json"
MAX_LENGTH = 20000


def _notes_path() -> Path:
    return Path(os.getenv(_NOTES_FILE_ENV_VAR, _DEFAULT_NOTES_FILE))


def apply_note(notes: dict[str, str], name: str, text: str) -> None:
    """Sets a node's note in place; blank text removes it. Line endings are normalised to \\n."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_LENGTH:
        raise ValueError(f"a note can be at most {MAX_LENGTH} characters")
    if text.strip():
        notes[name] = text
    else:
        notes.pop(name, None)


def load() -> dict[str, str]:
    """Every saved note; empty when there is none or the file is unreadable."""
    path = _notes_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def save_note(name: str, text: str) -> dict[str, str]:
    """One read-modify-write of the notes file; returns the full set afterwards."""
    notes = load()
    apply_note(notes, name, text)
    _notes_path().write_text(json.dumps(notes))
    return notes


def restrict(notes: dict[str, Any], known_nodes: Any) -> dict[str, str]:
    """`notes` for a project: only nodes in `known_nodes`, only usable text (a hand-edited bundle's garbage is dropped)."""
    clean: dict[str, str] = {}
    for name, text in (notes or {}).items():
        if name not in known_nodes or not isinstance(text, str):
            continue
        try:
            apply_note(clean, name, text)
        except ValueError:
            continue
    return clean


def replace_all(notes: dict[str, Any], known_nodes: Any) -> dict[str, str]:
    """Makes `notes` (a bundle's) the whole saved set, so a note of an earlier project cannot survive on a reused node name."""
    clean = restrict(notes, known_nodes)
    _notes_path().write_text(json.dumps(clean))
    return clean
