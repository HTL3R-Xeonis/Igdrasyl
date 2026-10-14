"""Managed-project selections stored separately from host profiles.

The browser only exchanges project IDs and display names. Filesystem paths stay
inside this module and are resolved when a host is activated.
"""

import json
import os
import tempfile
from pathlib import Path
from threading import Lock

from src.settings import Settings

from app import project_library, settings_profiles

_LOCK = Lock()


def _store():
    return Path(
        os.getenv("TOPOLOGYOPERATOR_PROJECT_SELECTIONS", "project_selections.json")
    )


def _read():
    path = _store()
    return json.loads(path.read_text()) if path.exists() else {}


def _selection_for_name(filename):
    saved = _read()
    selection = saved.get(filename) or {}
    return {
        "project_id": selection.get("project_id"),
        "project_name": selection.get("project_name"),
    }


def get_selection(filename):
    settings_profiles.profile_path(filename)
    return _selection_for_name(filename)


def save_selection(filename, project_name, project_id=None):
    settings_profiles.profile_path(filename)
    if project_id:
        project = project_library.get(project_id)
        project_name = project_name.strip() or project["name"]
    else:
        project_name = None
    selection = {
        "project_id": project_id,
        "project_name": project_name,
    }
    with _LOCK:
        saved = _read()
        saved[filename] = selection
        store = _store()
        store.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=store.parent, prefix=".project-selection-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(saved, stream, indent=2)
            os.replace(temporary, store)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return selection


def initialise(settings_path):
    filename = Path(settings_path).name if settings_path else None
    selection = _selection_for_name(filename) if filename else {}
    project_id = selection.get("project_id")
    Settings.TOPOLOGY_FILE = (
        str(project_library.topology_path(project_id)) if project_id else None
    )
    Settings.GNS3.PROJECT_NAME = selection.get("project_name")
