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


def create_from_design(name, state: "DesignState"):
    """
    A new library project holding the current design sketch (topology,
    layout and drawings) - what "name it and deploy" needs, without a
    separate create-then-open-then-save round trip.
    :raises ValueError: a bad name, or a design TopologyBuilder won't accept
    """
    text = yaml.safe_dump(
        design.to_topology_yaml_dict(state), sort_keys=False, allow_unicode=True
    )
    project = create(name, text)
    save_design(project["id"], state)
    return project


def delete(project_id):
    """
    Removes a project from the library (its topology, design and metadata).
    Only the library entry goes: a deployment of it in GNS3/ESXi is untouched.
    :raises ValueError/FileNotFoundError: bad id / no such project
    """
    directory = project_path(project_id)
    # Rename first: if removing the tree gets interrupted, no half-deleted
    # project is left listed.
    doomed = directory.with_name(f".deleting-{directory.name}")
    directory.rename(doomed)
    shutil.rmtree(doomed, ignore_errors=True)


_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_DEVICES = 200
_MAX_COMMANDS_PER_DEVICE = 2000
_MAX_COMMAND_CHARS = 4000


def _configs_file(project_id):
    return project_path(project_id) / "configs.json"


def validate_configs(devices) -> dict[str, list[str]]:
    """
    The device -> raw command list shape POST /config-push takes, checked
    before anything is stored or ever sent to a device.
    :raises ValueError: wrong shape, or beyond the size limits
    """
    if not isinstance(devices, dict):
        raise TypeError("configs must map device names to lists of commands")
    if len(devices) > _MAX_DEVICES:
        raise ValueError(f"at most {_MAX_DEVICES} devices per project")
    clean: dict[str, list[str]] = {}
    for name, commands in devices.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("device names must be 1-120 characters")
        if not isinstance(commands, list) or not all(
            isinstance(c, str) for c in commands
        ):
            raise ValueError(f"{name}: commands must be a list of text lines")
        if len(commands) > _MAX_COMMANDS_PER_DEVICE:
            raise ValueError(
                f"{name}: at most {_MAX_COMMANDS_PER_DEVICE} commands per device"
            )
        if any(len(c) > _MAX_COMMAND_CHARS or "\x00" in c for c in commands):
            raise ValueError(
                f"{name}: a command is longer than {_MAX_COMMAND_CHARS} characters "
                f"or contains a NUL byte"
            )
        clean[name] = commands
    return clean


def get_configs(project_id) -> dict:
    """{"devices": {name: [command, ...]}, "auto_push": bool} - empty when none was saved."""
    path = _configs_file(project_id)  # raises FileNotFoundError for an unknown project
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"devices": {}, "auto_push": False}
    except json.JSONDecodeError as exc:
        raise ValueError(f"The saved device configs are unreadable: {exc}") from exc
    return {
        "devices": validate_configs(data.get("devices", {})),
        "auto_push": bool(data.get("auto_push", False)),
    }


def save_configs(project_id, devices, auto_push: bool) -> dict:
    """
    Stores the device command lists with the project (they travel with it:
    into every deployment bundle and back out on resume) plus the
    "push after deploy / resume" switch.
    :raises ValueError: see validate_configs
    """
    clean = validate_configs(devices)
    text = json.dumps({"devices": clean, "auto_push": bool(auto_push)}, indent=2)
    if len(text.encode("utf-8")) > _MAX_CONFIG_BYTES:
        raise ValueError("The configs are larger than 1 MB")
    path = _configs_file(project_id)
    _atomic_text(path, text)
    return {"devices": clean, "auto_push": bool(auto_push)}


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
