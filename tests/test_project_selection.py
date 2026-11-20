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


def test_opening_a_project_makes_it_the_selection_of_the_host(
    selection_files, client, monkeypatch
):
    """Entering the host again opens the project that was open last, not the one saved long ago."""
    from unittest.mock import patch

    from src.graph import Environment

    from app import project_library

    host = selection_files
    monkeypatch.setenv("TOPOLOGYOPERATOR_PROJECTS_DIR", str(host.parent / "projects"))
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS", str(host))
    first = project_library.create("First")
    second = project_library.create("Second")
    project_selection.save_selection("host.yml", "old-gns3-name", first["id"])

    with patch.object(Environment, "get_environment", return_value=Environment.ON_GNS3):
        assert client.post(f"/design/project/{second['id']}/open").status_code == 200
    selected = project_selection.get_selection("host.yml")
    assert selected == {"project_id": second["id"], "project_name": "Second"}

    # opening the project that is selected already keeps the GNS3 project name the user chose
    project_selection.save_selection("host.yml", "my-gns3-name", second["id"])
    with patch.object(Environment, "get_environment", return_value=Environment.ON_GNS3):
        client.post(f"/design/project/{second['id']}/open")
    assert project_selection.get_selection("host.yml")["project_name"] == "my-gns3-name"


def test_another_host_keeps_its_own_selection(selection_files, monkeypatch):
    from app import project_library

    host = selection_files
    other = host.parent / "other.yml"
    other.write_text("esxi: {ip: 192.0.2.2}\n")
    monkeypatch.setenv("TOPOLOGYOPERATOR_PROJECTS_DIR", str(host.parent / "projects"))
    first = project_library.create("First")
    second = project_library.create("Second")
    project_selection.save_selection("other.yml", "", first["id"])

    project_selection.remember_opened("host.yml", second["id"])

    assert project_selection.get_selection("other.yml")["project_id"] == first["id"]
    assert project_selection.get_selection("host.yml")["project_id"] == second["id"]


def test_deleting_a_host_forgets_its_selection_and_state(
    selection_files, client, monkeypatch
):
    from app import profile_state, project_library

    host = selection_files
    monkeypatch.setenv("TOPOLOGYOPERATOR_PROJECTS_DIR", str(host.parent / "projects"))
    project = project_library.create("Mine")
    project_selection.save_selection("host.yml", "", project["id"])
    state = profile_state.state_dir("host.yml")
    state.mkdir(parents=True)
    (state / "layout.json").write_text("{}")

    project_selection.forget("host.yml")
    profile_state.delete("host.yml")

    assert project_selection.get_selection("host.yml")["project_id"] is None
    assert not state.exists()
    project_selection.forget("host.yml")  # nothing left: no error
