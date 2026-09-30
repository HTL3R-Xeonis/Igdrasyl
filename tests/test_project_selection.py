import json

import pytest
from src.settings import Settings

from app import project_selection
from app.topology import load_graph, resolve_project_name


@pytest.fixture
def selection_files(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(tmp_path))
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_PROJECT_SELECTIONS", str(tmp_path / "selections.json")
    )
    monkeypatch.delenv("TOPOLOGYOPERATOR_SETTINGS", raising=False)
    host = tmp_path / "host.yml"
    host.write_text("esxi: {ip: 192.0.2.1}\n")
    topology = tmp_path / "lab.yaml"
    topology.write_text("nodes: []\nedges: []\n")
    return host, topology


def test_new_host_has_no_implicit_topology(selection_files, monkeypatch):
    host, _ = selection_files
    monkeypatch.setattr(Settings, "TOPOLOGY_FILE", "old.yaml")
    monkeypatch.setattr(Settings.GNS3, "PROJECT_NAME", "old")
    project_selection.initialise(host)
    assert Settings.TOPOLOGY_FILE is None
    assert resolve_project_name() is None
    with pytest.raises(ValueError, match="No project selected"):
        load_graph()


def test_legacy_fallback_and_explicit_clear(selection_files):
    host, topology = selection_files
    host.write_text(f"topology_file: {topology}\ngns3: {{project_name: legacy}}\n")
    before = host.read_bytes()
    assert project_selection.get_selection("host.yml")["legacy"] is True
    project_selection.save_selection("host.yml", "", "")
    assert project_selection.get_selection("host.yml")["topology_file"] is None
    assert host.read_bytes() == before


def test_selection_is_independent_and_persistent(selection_files, client):
    host, topology = selection_files
    before = host.read_bytes()
    response = client.put(
        "/settings-profiles/host.yml/project",
        json={"topology_file": str(topology), "project_name": "my-lab"},
    )
    assert response.status_code == 200
    selected = client.get("/settings-profiles/host.yml/project").json()
    assert selected["topology_file"] == str(topology)
    assert selected["project_name"] == "my-lab"
    assert host.read_bytes() == before
    assert json.loads((host.parent / "selections.json").read_text())


def test_selection_rejects_missing_and_invalid_files(selection_files, client):
    _, topology = selection_files
    assert (
        client.put(
            "/settings-profiles/host.yml/project",
            json={"topology_file": "/missing/topology.yml"},
        ).status_code
        == 400
    )
    topology.write_text("not a topology")
    assert (
        client.put(
            "/settings-profiles/host.yml/project", json={"topology_file": str(topology)}
        ).status_code
        == 400
    )


def test_import_requires_selection(client, monkeypatch):
    monkeypatch.setattr(Settings, "TOPOLOGY_FILE", None)
    response = client.post(
        "/project/import", files={"file": ("project.topoproj", b"not used")}
    )
    assert response.status_code == 409
