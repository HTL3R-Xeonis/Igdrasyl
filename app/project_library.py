"""Named topology documents owned by the application, independent of hosts."""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import yaml
from src.topology_file_validation import TopologyFileValidation

from app import design

if TYPE_CHECKING:
    from app.design import DesignState

MAX_BYTES = 2 * 1024 * 1024


class OfflineValidation(TopologyFileValidation):
    @staticmethod
    def get_available_templates():
        # Template availability belongs to the chosen host, checked at activation.
        return None


def root():
    return Path(os.getenv("TOPOLOGYOPERATOR_PROJECTS_DIR", "projects"))


def project_path(project_id):
    if not re.fullmatch(r"[a-f0-9]{32}", project_id):
        raise ValueError("Invalid project ID")
    path = root() / project_id
    if not path.is_dir():
        raise FileNotFoundError("Project not found")
    return path


def get(project_id):
    return json.loads((project_path(project_id) / "project.json").read_text())


def list_projects():
    if not root().exists():
        return []
    return sorted(
        [
            get(path.name)
            for path in root().iterdir()
            if path.is_dir() and re.fullmatch(r"[a-f0-9]{32}", path.name)
        ],
        key=lambda item: (item["name"].casefold(), item["id"]),
    )


def create(name, topology_yaml=None):
    name = name.strip()
    if not name or len(name) > 120 or any(ord(char) < 32 for char in name):
        raise ValueError("Project name must contain 1–120 printable characters")
    text = "nodes: []\nedges: []\n" if topology_yaml is None else topology_yaml
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Topology YAML must be no larger than 2 MB")
    root().mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".creating-", dir=root()))
    try:
        topology = temporary / "topology.yaml"
        topology.write_text(text, encoding="utf-8")
        try:
            OfflineValidation(str(topology)).validate_file()
        except (ValueError, TypeError, KeyError, RuntimeError, AttributeError) as exc:
            raise ValueError(f"Invalid topology YAML: {exc}") from exc
        project_id = uuid4().hex
        metadata = {"id": project_id, "name": name}
        (temporary / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
        temporary.rename(root() / project_id)
        return metadata
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def topology_path(project_id):
    return project_path(project_id) / "topology.yaml"


def load_design(project_id: str) -> "DesignState":
    directory = project_path(project_id)
    saved = directory / "design.json"
    if saved.exists():
        try:
            return design.from_dict(json.loads(saved.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            raise ValueError(f"Project design data is invalid: {exc}") from exc
    return design.from_topology_yaml(
        (directory / "topology.yaml").read_text(encoding="utf-8")
    )


def save_design(project_id: str, state: "DesignState"):
    directory = project_path(project_id)
    topology_text = yaml.safe_dump(
        design.to_topology_yaml_dict(state), sort_keys=False, allow_unicode=True
    )
    if len(topology_text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Topology YAML must be no larger than 2 MB")
    temporary = _temporary_topology(directory, topology_text)
    try:
        OfflineValidation(str(temporary)).validate_file()
    finally:
        temporary.unlink(missing_ok=True)
    _atomic_text(directory / "topology.yaml", topology_text)
    _atomic_text(directory / "design.json", json.dumps(design.to_dict(state), indent=2))
    return get(project_id)


def _temporary_topology(directory: Path, text: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=".validate-", suffix=".yaml", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    path = Path(name)
    return path


def _atomic_text(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def deployment_preview(project_id: str, filename: str) -> dict:
    """Read-only deployment summary; never contacts or changes a host."""
    from app import project_selection, settings_profiles

    project = get(project_id)
    profile_path = settings_profiles.profile_path(filename)
    host = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    if not isinstance(host, dict):
        raise TypeError("Host profile must be a YAML mapping")
    topology = (
        yaml.safe_load(topology_path(project_id).read_text(encoding="utf-8")) or {}
    )
    groups = topology.get("nodes", [])
    edges = topology.get("edges", [])
    node_count = sum(len(group.get("names") or []) for group in groups)
    roles: dict[str, int] = {}
    for group in groups:
        role = str(group.get("role", "UNKNOWN"))
        roles[role] = roles.get(role, 0) + len(group.get("names") or [])
    profiles = {entry["filename"]: entry for entry in settings_profiles.list_profiles()}
    profile = profiles.get(filename, {})
    selection = project_selection.get_selection(filename)
    esxi = host.get("esxi") or {}
    return {
        "project": project,
        "host": {
            "filename": filename,
            "active": bool(profile.get("active")),
            "esxi_ip": esxi.get("ip"),
            "gns3_vm_name": esxi.get("gns3_vm_name"),
        },
        "node_count": node_count,
        "edge_count": len(edges),
        "roles": roles,
        "mode": "incremental" if host.get("incremental_deploy", True) else "full",
        "selected": selection.get("project_id") == project_id,
        "requires_restart": not profile.get("active")
        or selection.get("project_id") != project_id,
    }
