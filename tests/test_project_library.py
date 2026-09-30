from pathlib import Path
from unittest.mock import patch

import pytest
from src.graph.environment import Environment

from app import design, project_library, project_selection


@pytest.fixture(autouse=True)
def library_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_PROJECTS_DIR", str(tmp_path / "projects"))


def test_create_upload_download_and_list(client):
    empty = client.post("/projects", json={"name": "Empty lab"})
    assert empty.status_code == 201
    project = empty.json()
    assert project["name"] == "Empty lab"
    assert (
        client.get(f"/projects/{project['id']}/download").text
        == "nodes: []\nedges: []\n"
    )
    topology = Path("tests/fixtures/topology.yaml").read_text()
    uploaded = client.post(
        "/projects", json={"name": "Uploaded lab", "topology_yaml": topology}
    )
    assert uploaded.status_code == 201
    response = client.get(f"/projects/{uploaded.json()['id']}/download")
    assert response.text == topology
    assert "attachment" in response.headers["content-disposition"]
    assert len(client.get("/projects").json()["projects"]) == 2


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[]",
        "nodes: nope\nedges: []",
        "nodes: [{}]\nedges: []",
        "nodes: []\nedges: [[a,b,c,d]]",
        "!!python/object:test {}",
        "[",
    ],
)
def test_bad_upload_is_rejected_without_partial_project(client, text):
    response = client.post("/projects", json={"name": "Invalid", "topology_yaml": text})
    assert response.status_code == 400
    assert project_library.list_projects() == []
    assert list(project_library.root().iterdir()) == []


def test_names_are_labels_not_paths(client, tmp_path):
    first = client.post("/projects", json={"name": "../Lab"}).json()
    second = client.post("/projects", json={"name": "../Lab"}).json()
    assert first["id"] != second["id"]
    assert (
        project_library.topology_path(first["id"]).parent.parent
        == tmp_path / "projects"
    )
    assert client.post("/projects", json={"name": " "}).status_code == 400
    with pytest.raises(ValueError):
        project_library.topology_path("../../escape")
    assert client.get(f"/projects/{'a' * 32}/download").status_code == 404


def test_size_limit(client):
    response = client.post(
        "/projects",
        json={"name": "Large", "topology_yaml": "x" * (project_library.MAX_BYTES + 1)},
    )
    assert response.status_code == 400


def test_select_library_project_preserves_host(client, tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(tmp_path))
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_PROJECT_SELECTIONS", str(tmp_path / "selection.json")
    )
    monkeypatch.delenv("TOPOLOGYOPERATOR_SETTINGS", raising=False)
    host = tmp_path / "host.yml"
    host.write_text("esxi: {ip: 192.0.2.1}\n")
    original = host.read_bytes()
    project = project_library.create("Named project")
    response = client.put(
        "/settings-profiles/host.yml/project", json={"project_id": project["id"]}
    )
    assert response.status_code == 200
    selected = project_selection.get_selection("host.yml")
    assert selected["project_id"] == project["id"]
    assert selected["project_name"] == "Named project"
    assert Path(selected["topology_file"]).read_text() == "nodes: []\nedges: []\n"
    assert host.read_bytes() == original
    assert (
        client.put(
            "/settings-profiles/host.yml/project",
            json={"project_id": project["id"], "topology_file": "other.yml"},
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/settings-profiles/host.yml/project", json={"project_id": "b" * 32}
        ).status_code
        == 404
    )
    assert client.put("/settings-profiles/host.yml/project", json={}).status_code == 200
    assert project_selection.get_selection("host.yml")["topology_file"] is None


def test_design_roundtrip_is_stored_per_project(client):
    first = project_library.create("First")
    second = project_library.create("Second")
    state = design.new_state()
    state.nodes["R1"] = design.DesignNode("R1", "ROUTER", "VPCS", "ON_GNS3")
    state.layout["R1"] = {"x": 12.0, "y": 34.0}
    state.drawings = [{"id": 1, "type": "text", "text": "First layout"}]

    project_library.save_design(first["id"], state)

    restored = project_library.load_design(first["id"])
    assert restored.layout == {"R1": {"x": 12.0, "y": 34.0}}
    assert restored.drawings[0]["text"] == "First layout"
    assert project_library.load_design(second["id"]).layout == {}


def test_open_and_save_library_project_from_design_canvas(client, app_state):
    project = project_library.create("Editable")
    with patch.object(Environment, "get_environment", return_value=Environment.ON_GNS3):
        response = client.post(f"/design/project/{project['id']}/open")
    assert response.status_code == 200
    assert client.get("/design/project").json()["project"]["name"] == "Editable"

    app_state.design.nodes["R1"] = design.DesignNode("R1", "ROUTER", "VPCS", "ON_GNS3")
    app_state.design.layout["R1"] = {"x": 5, "y": 6}
    assert client.put(f"/design/project/{project['id']}").status_code == 200
    assert project_library.load_design(project["id"]).layout["R1"] == {"x": 5, "y": 6}


def test_deployment_preview_is_read_only_and_descriptive(client, tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    host = profiles / "host.yml"
    host.write_text(
        "incremental_deploy: false\nesxi: {ip: 192.0.2.10, gns3_vm_name: GNS3}\n"
    )
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(profiles))
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS", str(host))
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_PROJECT_SELECTIONS", str(tmp_path / "selections.json")
    )
    project = project_library.create(
        "Preview",
        "nodes:\n  - {names: [R1, R2], role: ROUTER, image: VPCS}\nedges:\n  - [R1, eth0, R2, eth0]\n",
    )
    project_selection.save_selection("host.yml", "", "", project["id"])

    response = client.get(f"/projects/{project['id']}/deploy-preview/host.yml")
    assert response.status_code == 200
    preview = response.json()
    assert preview["host"]["active"] is True
    assert preview["selected"] is True
    assert preview["requires_restart"] is False
    assert preview["node_count"] == 2
    assert preview["edge_count"] == 1
    assert preview["roles"] == {"ROUTER": 2}
    assert preview["mode"] == "full"


def test_deploy_requires_active_selected_host(client, tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "host.yml").write_text("esxi: {ip: 192.0.2.10}\n")
    monkeypatch.setenv("TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR", str(profiles))
    monkeypatch.delenv("TOPOLOGYOPERATOR_SETTINGS", raising=False)
    monkeypatch.setenv(
        "TOPOLOGYOPERATOR_PROJECT_SELECTIONS", str(tmp_path / "selections.json")
    )
    project = project_library.create("Deploy")
    response = client.post(f"/projects/{project['id']}/deploy/host.yml")
    assert response.status_code == 409
    assert "Activate" in response.json()["detail"]
