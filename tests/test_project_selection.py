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
    return host


def test_new_host_has_no_implicit_topology(selection_files, monkeypatch):
    host = selection_files
    monkeypatch.setattr(Settings, "TOPOLOGY_FILE", "old.yaml")
    monkeypatch.setattr(Settings.GNS3, "PROJECT_NAME", "old")
    project_selection.initialise(host)
    assert Settings.TOPOLOGY_FILE is None
    assert resolve_project_name() is None
    with pytest.raises(ValueError, match="No project selected"):
        load_graph()


def test_selection_is_independent_and_persistent(selection_files, client, monkeypatch):
    host = selection_files
    from app import project_library

    monkeypatch.setenv("TOPOLOGYOPERATOR_PROJECTS_DIR", str(host.parent / "projects"))
    project = project_library.create("My lab")
    before = host.read_bytes()
    response = client.put(
        "/settings-profiles/host.yml/project",
        json={"project_id": project["id"], "project_name": "my-lab"},
    )
    assert response.status_code == 200
    selected = client.get("/settings-profiles/host.yml/project").json()
    assert selected["project_id"] == project["id"]
    assert selected["project_name"] == "my-lab"
    assert host.read_bytes() == before
    assert str(host.parent) not in (host.parent / "selections.json").read_text()


def test_selection_rejects_path_fields(selection_files, client):
    response = client.put(
        "/settings-profiles/host.yml/project",
        json={"topology_file": "/missing/topology.yml"},
    )
    assert response.status_code == 422


def test_import_requires_selection(client, monkeypatch):
    monkeypatch.setattr(Settings, "TOPOLOGY_FILE", None)
    response = client.post(
        "/project/import", files={"file": ("project.topoproj", b"not used")}
    )
    assert response.status_code == 409
