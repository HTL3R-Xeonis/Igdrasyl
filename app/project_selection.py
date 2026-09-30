"""Project selections stored separately from host connection profiles.

TopologyBuilder still needs process-global settings. Apply the selected project
at startup, and use the existing restart flow when changing that selection.
"""

import json
import os
import tempfile
from pathlib import Path
from threading import Lock

import yaml
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


def selection_for_path(path):
    saved = _read()
    key = str(Path(path).resolve()) if path else "default"
    if key in saved:
        return {**saved[key], "legacy": False}
    data = yaml.safe_load(Path(path).read_text()) or {} if path else {}
    return {
        "topology_file": data.get("topology_file"),
        "project_name": (data.get("gns3") or {}).get("project_name"),
        "legacy": bool(data.get("topology_file")),
    }


def get_selection(filename):
    return selection_for_path(settings_profiles.profile_path(filename))


def save_selection(filename, topology_file, project_name, project_id=None):
    host_path = settings_profiles.profile_path(filename)
    if project_id:
        if topology_file:
            raise ValueError("Choose a library project or a server path, not both")
        project = project_library.get(project_id)
        topology_file = str(project_library.topology_path(project_id))
        project_name = project_name.strip() or project["name"]
    path = Path(topology_file).expanduser() if topology_file else None
    if path is not None:
        if not path.is_file():
            raise ValueError("Select an existing topology YAML file on the server")
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise ValueError("Topology file contains invalid YAML") from exc
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("nodes"), list)
            or not isinstance(data.get("edges"), list)
        ):
            raise ValueError("Topology must contain nodes and edges lists")
    selection = {
        "project_id": project_id,
        "topology_file": str(path.resolve()) if path else None,
        "project_name": project_name.strip() or None if path else None,
    }
    with _LOCK:
        saved = _read()
        saved[str(host_path.resolve())] = selection
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
    selection = selection_for_path(settings_path)
    Settings.TOPOLOGY_FILE = selection["topology_file"]
    Settings.GNS3.PROJECT_NAME = selection["project_name"]
