"""
The sketch, layout, labels, notes and holds of ONE host profile.

Every host profile runs its own backend process (deploy/xeonis-backend-launch.py). These five files hold what the user
sees on that host's canvas, so each backend keeps its own set under `<profiles dir>/.state/<profile file>/` instead of
sharing one set in the working directory - otherwise the host that started last shows what another host saved. The launch
script copies a file of the shared set into a profile's folder the first time that profile starts (the shared file stays
as it is). The names here and the ones in that script (it cannot import this package) are kept equal by a test.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app import settings_profiles

STATE_DIR_NAME = ".state"

# environment variable of the file -> its file name; the modules read the variable and fall back to this name
FILES = {
    "TOPOLOGYOPERATOR_DESIGN_FILE": "design_state.json",
    "TOPOLOGYOPERATOR_HOLDS_FILE": "holds.json",
    "TOPOLOGYOPERATOR_LABELS_FILE": "label_layout.json",
    "TOPOLOGYOPERATOR_LAYOUT_FILE": "layout.json",
    "TOPOLOGYOPERATOR_NOTES_FILE": "node_notes.json",
}


def state_dir(filename: str) -> Path:
    """:raises settings_profiles.InvalidProfileName: unsafe filename."""
    settings_profiles._validate_name(filename)
    return settings_profiles._profiles_dir() / STATE_DIR_NAME / filename


def delete(filename: str) -> None:
    """Removes the files of a deleted profile, so a new profile of the same name does not inherit them."""
    shutil.rmtree(state_dir(filename), ignore_errors=True)
